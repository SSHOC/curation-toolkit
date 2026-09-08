"""
OpenAIRE Graph API client — look up DOIs and extract the bibliographic
metadata this toolkit knows how to backfill onto Marketplace items.

The public research-products endpoint is queried by exact DOI (`pid` filter),
so a match is authoritative — no fuzzy title matching involved. Unauthenticated
requests are capped at 60/hour; a personal access token
(https://graph.openaire.eu/docs/apis/authentication) raises that to 7200/hour.
fetch_many() throttles request dispatch to stay under whichever cap applies,
and checks an on-disk cache (data/openaire_cache.json, gitignored along with
the rest of data/) first so repeated runs don't re-spend rate-limit budget on
DOIs already looked up recently.

normalize_doi()  – strip doi.org URL / "doi:" prefixes so the bare DOI can be
                    used both as the API's `pid` value and the cache key
fetch_one()      – single-DOI lookup with retry/backoff and 429 handling
fetch_many()     – concurrent batch lookup with rate limiting, caching, and a
                    Streamlit progress bar
"""

import datetime
import json
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st

_API_URL = "https://api.openaire.eu/graph/v3/research-products"
_CACHE_PATH = pathlib.Path(__file__).parent.parent / "data" / "openaire_cache.json"
_CACHE_VERSION = 1
_CACHE_TTL_DAYS = 30  # not-found results are re-checked periodically as OpenAIRE's harvest catches up

_UNAUTH_PER_HOUR = 60
_AUTH_PER_HOUR = 7200

_DOI_PREFIX_RE = re.compile(r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)


def normalize_doi(raw: str) -> str:
    """Strip a doi.org URL or 'doi:' prefix, returning the bare DOI (e.g. '10.5281/zenodo.123')."""
    if not raw:
        return ""
    return _DOI_PREFIX_RE.sub("", raw.strip()).strip()


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # cache is best-effort


def _cache_entry_valid(entry: dict | None) -> bool:
    if not entry or entry.get("version") != _CACHE_VERSION:
        return False
    try:
        fetched = datetime.datetime.fromisoformat(entry["fetched_at"])
    except Exception:
        return False
    return (datetime.datetime.now() - fetched).days < _CACHE_TTL_DAYS


def _extract_fields(record: dict) -> dict:
    """
    Map a raw OpenAIRE research-product record onto the fields this toolkit
    proposes for missing Marketplace properties: year, publisher, language,
    keyword, accessibleAt.
    """
    pub_date = record.get("publicationDate") or None
    year = pub_date[:4] if pub_date and re.match(r"^\d{4}", pub_date) else None

    lang = record.get("language") or {}
    lang_code = lang.get("code")
    lang_label = lang.get("label")
    if not lang_code or lang_code.lower() == "und":
        lang_code = lang_label = None

    # Only "keyword"-scheme subjects are free-text-like; "FOS" (Field of
    # Science) subjects are classification codes (e.g. "05 social sciences"),
    # not suitable as sshoc-keyword candidates.
    keywords = []
    for s in record.get("subjects") or []:
        sub = (s or {}).get("subject") or {}
        if sub.get("scheme") == "keyword" and sub.get("value"):
            val = sub["value"].strip()
            if val and val not in keywords:
                keywords.append(val)

    urls = []
    for inst in record.get("instances") or []:
        for u in inst.get("urls") or []:
            if u and u.startswith(("http://", "https://")) and u not in urls:
                urls.append(u)

    return {
        "title": record.get("mainTitle"),
        "publication_date": pub_date,
        "year": year,
        "publisher": record.get("publisher") or None,
        "language_code": lang_code,
        "language_label": lang_label,
        "keywords": keywords,
        "urls": urls,
        "openaire_id": record.get("id"),
    }


def fetch_one(doi: str, token: str | None = None, timeout: int = 15, retries: int = 3) -> dict:
    """
    Look up a single (already-normalized) DOI. Returns one of:
      {"status": "found", "fields": {...}}   – see _extract_fields() for keys
      {"status": "not_found"}                – OpenAIRE has no record for this DOI
      {"status": "error", "message": str}    – network/HTTP failure after retries
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(retries):
        try:
            resp = requests.get(_API_URL, params={"pid": doi}, headers=headers, timeout=timeout)
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(1.0)
                continue
            return {"status": "error", "message": "Timeout"}
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(1.0)
                continue
            return {"status": "error", "message": str(e)[:150]}

        if resp.status_code == 429:
            if attempt < retries - 1:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 ** attempt * 2)
                continue
            return {"status": "error", "message": "Rate limited (429)"}

        if resp.status_code >= 500:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"status": "error", "message": f"HTTP {resp.status_code}"}

        if resp.status_code >= 400:
            return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:150]}"}

        try:
            data = resp.json()
        except ValueError:
            return {"status": "error", "message": "Malformed JSON response"}

        results = data.get("results") or []
        if not results:
            return {"status": "not_found"}
        return {"status": "found", "fields": _extract_fields(results[0])}

    return {"status": "error", "message": "Exhausted retries"}


class _RateLimiter:
    """Throttles request *dispatch* to at most `per_hour` per hour, shared across worker threads."""

    def __init__(self, per_hour: int):
        self._min_interval = 3600.0 / per_hour
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + self._min_interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def fetch_many(
    dois: list[str],
    token: str | None = None,
    workers: int = 5,
    timeout: int = 15,
    use_cache: bool = True,
) -> dict[str, dict]:
    """
    Look up a list of (already-normalized, deduplicated) DOIs. Cached results
    younger than _CACHE_TTL_DAYS are reused without a network call; the rest
    are fetched concurrently through a shared rate limiter sized to the
    unauthenticated (60/hour) or authenticated (7200/hour) cap. Renders a
    Streamlit progress bar for the live-lookup portion and persists new
    results ("found"/"not_found" only — "error" results are always retried
    on the next run) back to the on-disk cache.

    Returns a dict mapping doi -> the same per-DOI result shape as fetch_one().
    """
    cache = _load_cache() if use_cache else {}
    results: dict[str, dict] = {}
    to_fetch: list[str] = []

    for doi in dois:
        entry = cache.get(doi)
        if use_cache and _cache_entry_valid(entry):
            results[doi] = entry["result"]
        else:
            to_fetch.append(doi)

    if not to_fetch:
        return results

    per_hour = _AUTH_PER_HOUR if token else _UNAUTH_PER_HOUR
    limiter = _RateLimiter(per_hour)
    total = len(to_fetch)
    done = 0
    bar = st.progress(0, text=f"Looking up on OpenAIRE… 0 / {total}")

    def _job(doi: str):
        limiter.wait()
        return doi, fetch_one(doi, token=token, timeout=timeout)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
        futures = {pool.submit(_job, doi): doi for doi in to_fetch}
        for future in as_completed(futures):
            doi, result = future.result()
            results[doi] = result
            if use_cache and result.get("status") in ("found", "not_found"):
                cache[doi] = {
                    "version": _CACHE_VERSION,
                    "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "result": result,
                }
            done += 1
            bar.progress(done / total, text=f"Looking up on OpenAIRE… {done} / {total}")

    bar.empty()
    if use_cache:
        _save_cache(cache)

    return results
