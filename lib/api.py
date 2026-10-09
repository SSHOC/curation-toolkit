"""
Thin wrappers around the SSH Open Marketplace REST API.

All write operations (PUT, POST, DELETE) are logged via lib.logger so
they appear in the Session Log page.  Read operations used only during
snapshot creation are not individually logged (the snapshot function
logs a single summary entry on completion).

Actor helpers
-------------
fetch_all_actors()           – paginated GET /api/actors → DataFrame
_check_one_actor()           – GET /api/actors/{id}?items=true with retry
verify_orphans()             – concurrent batch verification of actor candidates
delete_actor()               – DELETE /api/actors/{id}
get_actor()                  – GET /api/actors/{id} (full record, used before merge and in manual-merge preview)
_consolidate_actor_payload() – build PUT-ready ActorCore merging attrs from multiple actors
merge_actors()               – 3-step merge: GET all → PUT consolidated → POST merge

Item helpers
------------
get_item()                   – GET /api/{category-path}/{persistentId}
put_item()                   – PUT (update) an item, logs the call
delete_item()                – DELETE /api/{category-path}/{persistentId}
fix_item_keyword()           – replace a keyword property on an item and PUT it back
consolidate_item_payload()   – build a PUT-ready item merging attrs from two items
repoint_related_item()       – swap a relatedItems reference on one item for another
merge_items()                – GET both → consolidate → PUT keep → repoint referrers → DELETE merge item
                                (or, with use_native=True, _merge_items_native())
_merge_items_native()        – opt-in alternative using the Marketplace's own merge endpoint

Note on the Marketplace's own per-category merge endpoint (POST
/api/{category-path}/merge?with={persistentIds}, body = *Core payload; plus
a GET .../{id}/merge "preview" that computes the same server-side): its
behavior isn't documented beyond the OpenAPI shape and is easy to misread as
"absorb `with` into {persistentId}", the way actor merging works. It does
not work that way — investigated empirically against Stage (2026-10-09):
every persistentId passed in `with` is hard-deleted, including one you
might expect to survive, and the request body always becomes a *new* item
with a freshly minted persistentId. Since the Marketplace serves no
redirect from a retired persistentId, that breaks existing links to EITHER
merged item, not just the discarded one — merge_items() instead fetches
both full records, builds the consolidated payload itself, and PUTs it onto
the item the curator chose to keep, preserving that persistentId. The native
endpoint is still available as an opt-in (use_native=True) for when no
persistentId involved needs to stay stable, since in exchange it repoints
relatedItems on every other item that referenced either merged id —
server-side, against live data, more thorough than repoint_related_item()'s
local-snapshot-driven search.

Concept / vocabulary helpers
----------------------------
fetch_all_keyword_concepts() – paginated GET /api/concept-search?types=keyword
fetch_concepts_by_type()     – paginated GET /api/concept-search?types={code}, any one property type
fetch_all_concepts()         – paginated GET /api/concept-search (all types and vocabs)
get_concept()                – GET /api/vocabularies/{vocab}/concepts/{code}; single concept by code, or None if absent
delete_concept()             – DELETE /api/vocabularies/{vocab}/concepts/{code}?force=true

Auth / user helpers
--------------------
get_current_user()           – GET /api/auth/me; the logged-in account, including its role

Snapshot creation
-----------------
create_snapshot_from_api()   – fetch all 5 item categories and save as full_items_{ts}.json
_fetch_page()                – single-page GET with retry, used by create_snapshot_from_api
"""

import time
import requests
import pandas as pd
import streamlit as st
from lib.logger import log_action, log_api


def fetch_all_actors(api_url: str, bearer: str) -> pd.DataFrame:
    """
    Fetch all actors from GET /api/actors (paginated, 100/page) using the
    provided bearer token. Returns a DataFrame with columns:
    id, name, email, website.
    """
    url = f"{api_url}/api/actors"
    headers = {"Authorization": bearer}

    resp = requests.get(f"{url}?perpage=100&page=1", headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    total_pages = data.get("pages", 1)
    all_actors = list(data.get("actors", []))

    bar = st.progress(1 / max(total_pages, 1), text=f"Loading actors… 1 / {total_pages}")
    for page in range(2, total_pages + 1):
        r = requests.get(f"{url}?perpage=100&page={page}", headers=headers, timeout=15)
        r.raise_for_status()
        all_actors.extend(r.json().get("actors", []))
        bar.progress(page / total_pages, text=f"Loading actors… {page} / {total_pages}")
    bar.empty()

    if not all_actors:
        return pd.DataFrame(columns=["id", "name", "email", "website", "item_count"])

    df = pd.json_normalize(all_actors)
    for col in ["email", "website", "items"]:
        if col not in df.columns:
            df[col] = pd.NA
    df["item_count"] = df["items"].apply(
        lambda x: len(x) if isinstance(x, list) else (0 if pd.isna(x) else int(x))
    )
    result = df[["id", "name", "email", "website", "item_count"]].copy()
    log_action(f"Fetched {len(result)} actors from API ({api_url})", source="Actors")
    return result


def _check_one_actor(actor_id: int, api_url: str, bearer: str, retries: int = 3) -> tuple[int, bool | None]:
    """
    GET /api/actors/{id}?items=true with retry on transient errors.
    Returns (actor_id, has_items). None means uncertain after all retries.
    """
    url = f"{api_url}/api/actors/{actor_id}?items=true"
    headers = {"Authorization": bearer}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                try:
                    items = resp.json().get("items", [])
                    return actor_id, len(items) > 0 if isinstance(items, list) else bool(items)
                except ValueError:
                    return actor_id, None  # malformed JSON
            if resp.status_code == 404:
                return actor_id, False  # actor gone — no items by definition
            if resp.status_code >= 500 and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            return actor_id, None  # 4xx or exhausted retries
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(0.5)
                continue
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
        except Exception:
            return actor_id, None
    return actor_id, None


def verify_orphans(
    candidate_ids: list[int],
    api_url: str,
    bearer: str,
    batch_size: int = 50,
) -> dict[int, bool | None]:
    """
    Verify each candidate actor in batches. Within each batch requests run
    concurrently; a short pause between batches reduces pressure on the API.
    Returns a dict mapping actor_id → has_items (True/False/None-if-failed).
    Renders a Streamlit progress bar while running.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict[int, bool | None] = {}
    total = len(candidate_ids)
    bar = st.progress(0, text=f"Verifying… 0 / {total}")

    for batch_start in range(0, total, batch_size):
        batch = candidate_ids[batch_start : batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {
                pool.submit(_check_one_actor, aid, api_url, bearer): aid
                for aid in batch
            }
            for future in as_completed(futures):
                aid, has_items = future.result()
                results[aid] = has_items
        done = min(batch_start + batch_size, total)
        bar.progress(done / total, text=f"Verifying… {done} / {total}")
        if done < total:
            time.sleep(0.3)  # brief pause between batches

    bar.empty()
    orphaned = sum(1 for v in results.values() if v is False)
    uncertain = sum(1 for v in results.values() if v is None)
    log_action(
        f"Verified {total} actor candidates — "
        f"{orphaned} orphaned, {uncertain} uncertain",
        source="Actors",
    )
    return results


def delete_actor(actor_id: int) -> tuple[bool, str]:
    """DELETE /api/actors/{actor_id}. Returns (success, message)."""
    env = st.session_state["env"]
    bearer = st.session_state["bearer"]
    url = f"{env['api_url']}/api/actors/{actor_id}?force=false"
    try:
        resp = requests.delete(url, headers={"Authorization": bearer}, timeout=15)
        ok = resp.status_code in (200, 204)
        log_api(
            "DELETE", url,
            f"Delete actor {actor_id} (force=false)",
            status=resp.status_code,
            response=resp.text[:300],
            ok=ok,
            source=f"Actors: deleted actor {actor_id}",
        )
        if ok:
            return True, f"Actor {actor_id} deleted."
        return False, f"API returned {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as e:
        log_api("DELETE", url, f"Delete actor {actor_id} — request failed", status="error",
                response=str(e), ok=False, source=f"Actors: deleted actor {actor_id}")
        return False, f"Request failed: {e}"


def fetch_all_keyword_concepts(api_url: str, bearer: str) -> pd.DataFrame:
    """
    GET /api/concept-search?types=keyword — paginate through all pages.
    No auth required for reads, but bearer is passed for consistency.
    Returns a DataFrame with columns: code, label, uri, notation, candidate, definition.
    """
    url = f"{api_url}/api/concept-search"
    headers = {"Authorization": bearer}
    params = {"types": "keyword", "perpage": 100, "page": 1}

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    total_pages = data.get("pages", 1)
    all_concepts = list(data.get("concepts", []))

    if total_pages > 1:
        bar = st.progress(1 / total_pages, text=f"Loading concepts… 1 / {total_pages}")
        for page in range(2, total_pages + 1):
            r = requests.get(url, headers=headers,
                             params={**params, "page": page}, timeout=15)
            r.raise_for_status()
            all_concepts.extend(r.json().get("concepts", []))
            bar.progress(page / total_pages, text=f"Loading concepts… {page} / {total_pages}")
        bar.empty()

    if not all_concepts:
        return pd.DataFrame(columns=["code", "label", "uri", "notation", "candidate", "definition"])

    df = pd.json_normalize(all_concepts)
    for col in ["code", "label", "uri", "notation", "candidate", "definition"]:
        if col not in df.columns:
            df[col] = pd.NA
    return df[["code", "label", "uri", "notation", "candidate", "definition"]].copy()


def fetch_concepts_by_type(property_type_code: str, api_url: str, bearer: str) -> pd.DataFrame:
    """
    GET /api/concept-search?types={property_type_code} — paginate through all
    pages for a single property type's vocabulary. Unlike
    fetch_all_keyword_concepts() (which hardcodes "sshoc-keyword" for its one
    known caller), this also returns the vocabulary code, since it isn't
    always the same as the property type code (e.g. the `activity` property
    type's vocabulary is `tadirah2`) — needed to build a correct
    `concept: {..., vocabulary: {code}}` payload for a type this generic.

    Used for the Marketplace's smaller closed vocabularies (e.g. `standard`,
    `activity` — tens to a couple hundred concepts), not large ones like
    `discipline` (1400+) or `object-format` (1900+), where a single-code
    lookup via get_concept() is the better fit when the target code is
    already known, or fetch_all_concepts() if a full unfiltered scan is
    truly needed.

    Returns a DataFrame with columns: code, label, uri, notation, candidate,
    vocabulary_code.
    """
    url = f"{api_url}/api/concept-search"
    headers = {"Authorization": bearer}
    params = {"types": property_type_code, "perpage": 100, "page": 1}

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    total_pages = data.get("pages", 1)
    all_concepts = list(data.get("concepts", []))

    for page in range(2, total_pages + 1):
        r = requests.get(url, headers=headers, params={**params, "page": page}, timeout=15)
        r.raise_for_status()
        all_concepts.extend(r.json().get("concepts", []))

    cols = ["code", "label", "uri", "notation", "candidate", "vocabulary_code"]
    if not all_concepts:
        return pd.DataFrame(columns=cols)

    df = pd.json_normalize(all_concepts)
    if "vocabulary.code" in df.columns:
        df = df.rename(columns={"vocabulary.code": "vocabulary_code"})
    for col in cols:
        if col not in df.columns:
            df[col] = pd.NA
    return df[cols].copy()


def fetch_all_concepts(api_url: str, bearer: str) -> pd.DataFrame:
    """
    GET /api/concept-search (all types, all vocabularies) — paginated.
    Returns a DataFrame with columns: code, label, uri, notation,
    candidate, definition, vocabulary_code, type_code.
    """
    url = f"{api_url}/api/concept-search"
    headers = {"Authorization": bearer}
    params = {"perpage": 100, "page": 1}

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    total_pages = data.get("pages", 1)
    all_concepts = list(data.get("concepts", []))

    bar = st.progress(1 / total_pages, text=f"Loading concepts… 1 / {total_pages}")
    for page in range(2, total_pages + 1):
        r = requests.get(url, headers=headers, params={**params, "page": page}, timeout=15)
        r.raise_for_status()
        all_concepts.extend(r.json().get("concepts", []))
        bar.progress(page / total_pages, text=f"Loading concepts… {page} / {total_pages}")
    bar.empty()

    if not all_concepts:
        return pd.DataFrame(columns=["code", "label", "uri", "notation",
                                     "candidate", "definition", "vocabulary_code", "type_code"])

    df = pd.json_normalize(all_concepts)
    if "vocabulary.code" in df.columns:
        df = df.rename(columns={"vocabulary.code": "vocabulary_code"})
    elif "vocabulary_code" not in df.columns:
        df["vocabulary_code"] = pd.NA

    # types is a list; take the code of the first entry
    if "types" in df.columns:
        df["type_code"] = df["types"].apply(
            lambda t: t[0]["code"] if isinstance(t, list) and t else pd.NA
        )
    else:
        df["type_code"] = pd.NA

    for col in ["code", "label", "uri", "notation", "candidate", "definition"]:
        if col not in df.columns:
            df[col] = pd.NA

    return df[["code", "label", "uri", "notation",
               "candidate", "definition", "vocabulary_code", "type_code"]].copy()


# Maps the singular category name used inside item records to the plural
# path segment used by the REST API (e.g. "tool-or-service" → "tools-services").
_CATEGORY_PATH = {
    "tool-or-service":   "tools-services",
    "training-material": "training-materials",
    "dataset":           "datasets",
    "publication":       "publications",
    "workflow":          "workflows",
    "step":              "steps",
}


def _item_url(api_url: str, category: str, persistent_id: str) -> str:
    """Build the canonical REST URL for a single item."""
    path = _CATEGORY_PATH.get(category, category + "s")
    return f"{api_url}/api/{path}/{persistent_id}"


def get_item(category: str, persistent_id: str, api_url: str, bearer: str) -> dict:
    """
    Fetch a single item from the live API and return the full JSON record.

    Raises requests.HTTPError on non-2xx responses.
    """
    resp = requests.get(
        _item_url(api_url, category, persistent_id),
        headers={"Authorization": bearer},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def put_item(category: str, persistent_id: str, item_data: dict,
             api_url: str, bearer: str, source: str = "") -> tuple[bool, str]:
    """
    PUT a full item record back to the API (update in place).

    item_data should be the dict returned by get_item(), modified as needed.
    Returns (success, message) and logs the call to the session log.

    `source` is passed straight through to log_api() — put_item() is shared by
    several features (Keywords' fix_item_keyword, Item Duplicates' merge_items/
    repoint_related_item, and the OpenAIRE Enrichment page), so unlike most
    other write helpers in this module it can't infer on its own which one is
    calling; each caller supplies its own human-readable "<feature>: <what
    changed>" string.
    """
    import json as _json
    url = _item_url(api_url, category, persistent_id)
    resp = requests.put(
        url,
        headers={"Content-Type": "application/json", "Authorization": bearer},
        json=item_data,
        timeout=30,
    )
    ok = resp.status_code in (200, 201)
    log_api(
        "PUT", url,
        f"Update {category}/{persistent_id}",
        status=resp.status_code,
        request=_json.dumps({"persistentId": persistent_id, "category": category,
                             "label": item_data.get("label", "")})[:500],
        response=resp.text[:300],
        ok=ok,
        source=source,
    )
    if ok:
        return True, "Updated."
    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def fix_item_keyword(
    category: str,
    persistent_id: str,
    old_concept_code: str,
    new_type_code: str,
    new_concept: dict,
    api_url: str,
    bearer: str,
) -> tuple[bool, str]:
    """
    GET the item, replace every property whose type=keyword and
    concept.code=old_concept_code with new_type_code / new_concept, PUT back.
    """
    try:
        item = get_item(category, persistent_id, api_url, bearer)
    except Exception as e:
        return False, f"GET failed: {e}"

    changed = False
    for prop in item.get("properties", []):
        if (prop.get("type", {}).get("code") == "keyword"
                and prop.get("concept", {}).get("code") == old_concept_code):
            prop["type"]["code"] = new_type_code
            prop["concept"] = new_concept
            changed = True

    if not changed:
        return False, "Property not found in item."

    new_label = new_concept.get("label") or new_concept.get("code") or new_type_code
    return put_item(
        category, persistent_id, item, api_url, bearer,
        source=f"Keywords: replaced keyword '{old_concept_code}' with '{new_label}' on {category}/{persistent_id}",
    )


def delete_item(category: str, persistent_id: str, api_url: str, bearer: str) -> tuple[bool, str]:
    """
    DELETE /api/{category-path}/{persistentId}.

    Unlike delete_actor()/delete_concept(), item deletion has no ?force=
    query param in the API — the endpoint either succeeds or refuses
    (e.g. a workflow with steps still attached), and refusals surface via
    a non-2xx status here.
    """
    url = _item_url(api_url, category, persistent_id)
    try:
        resp = requests.delete(url, headers={"Authorization": bearer}, timeout=15)
        ok = resp.status_code in (200, 204)
        log_api(
            "DELETE", url,
            f"Delete {category}/{persistent_id}",
            status=resp.status_code,
            response=resp.text[:300],
            ok=ok,
            source=f"Item Duplicates: deleted merged-away item {category}/{persistent_id}",
        )
        if ok:
            return True, f"{category}/{persistent_id} deleted."
        return False, f"API returned {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as e:
        log_api("DELETE", url, f"Delete {category}/{persistent_id} — request failed",
                status="error", response=str(e), ok=False,
                source=f"Item Duplicates: deleted merged-away item {category}/{persistent_id}")
        return False, f"Request failed: {e}"


def _dedupe_related_items(related: list[dict], exclude_persistent_ids: set) -> list[dict]:
    """
    Dedupe a list of relatedItems entries by (persistentId, relation code),
    dropping any entry that points at one of `exclude_persistent_ids`
    (used to strip self-references between the two items being merged).
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in related:
        pid = r.get("persistentId")
        if not pid or pid in exclude_persistent_ids:
            continue
        rel_code = (r.get("relation") or {}).get("code", "")
        key = (pid, rel_code)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def consolidate_item_payload(keep_item: dict, merge_item: dict) -> dict:
    """
    Build a PUT-ready payload for `keep_item` that folds in as much data as
    possible from `merge_item` before the latter is deleted. Returns a full
    item dict (a copy of keep_item with fields merged in) — safe to PUT
    directly, following the same GET-then-PUT round-trip already used by
    fix_item_keyword().

    - label: always keep item's (the curator picks which label survives by
      choosing which item is "keep" in the UI)
    - description / version: keep item's value; falls back to the merge
      item's if the keep item has none
    - contributors: union, deduped by (actor.id, role.code)
    - properties: union, deduped by (type.code, concept.code, value) —
      preserves keywords/concepts/free-text values from both items
    - externalIds: union, deduped by (identifierService.code, identifier)
    - accessibleAt: union of URLs, order preserved, deduped
    - media: union, deduped by info.mediaId
    - relatedItems: union of both items' relations to *other* items, deduped
      by (persistentId, relation code); references between the keep and
      merge item themselves are dropped (they would become a self-loop
      once the merge item is deleted)
    - source / sourceItemId / thumbnail: keep item's; falls back to the
      merge item's if the keep item has none
    """
    merged = dict(keep_item)

    for field in ("description", "version"):
        if not merged.get(field):
            merged[field] = merge_item.get(field, merged.get(field))

    seen_contrib: set[tuple] = set()
    contributors = []
    for item in (keep_item, merge_item):
        for c in item.get("contributors", []) or []:
            key = (c.get("actor", {}).get("id"), c.get("role", {}).get("code"))
            if key in seen_contrib:
                continue
            seen_contrib.add(key)
            contributors.append(c)
    merged["contributors"] = contributors

    seen_props: set[tuple] = set()
    properties = []
    for item in (keep_item, merge_item):
        for p in item.get("properties", []) or []:
            key = (
                p.get("type", {}).get("code"),
                (p.get("concept") or {}).get("code"),
                p.get("value"),
            )
            if key in seen_props:
                continue
            seen_props.add(key)
            properties.append(p)
    merged["properties"] = properties

    seen_ext: set[tuple] = set()
    ext_ids = []
    for item in (keep_item, merge_item):
        for e in item.get("externalIds", []) or []:
            key = (e.get("identifierService", {}).get("code"), e.get("identifier"))
            if key in seen_ext:
                continue
            seen_ext.add(key)
            ext_ids.append(e)
    merged["externalIds"] = ext_ids

    seen_urls: set = set()
    urls = []
    for item in (keep_item, merge_item):
        for u in item.get("accessibleAt", []) or []:
            if u not in seen_urls:
                seen_urls.add(u)
                urls.append(u)
    merged["accessibleAt"] = urls

    seen_media: set = set()
    media = []
    for item in (keep_item, merge_item):
        for m in item.get("media", []) or []:
            mid = (m.get("info") or {}).get("mediaId")
            if mid in seen_media:
                continue
            seen_media.add(mid)
            media.append(m)
    merged["media"] = media

    exclude = {keep_item.get("persistentId"), merge_item.get("persistentId")}
    related = list(keep_item.get("relatedItems", []) or []) + list(merge_item.get("relatedItems", []) or [])
    merged["relatedItems"] = _dedupe_related_items(related, exclude)

    for field in ("source", "sourceItemId", "thumbnail"):
        if not merged.get(field):
            merged[field] = merge_item.get(field, merged.get(field))

    return merged


def repoint_related_item(
    category: str, persistent_id: str,
    old_persistent_id: str, new_persistent_id: str,
    api_url: str, bearer: str,
) -> tuple[bool, str]:
    """
    GET the item, replace every relatedItems entry pointing at
    old_persistent_id with one pointing at new_persistent_id (same
    relation code), dedupe against any entry already pointing at
    new_persistent_id, drop any resulting self-reference, and PUT back.

    Used to keep other items' relatedItems links intact after the item
    they used to point to has been merged away.
    """
    try:
        item = get_item(category, persistent_id, api_url, bearer)
    except Exception as e:
        return False, f"GET failed: {e}"

    related = item.get("relatedItems", []) or []
    changed = False
    seen: set[tuple] = set()
    new_related = []
    for r in related:
        pid = r.get("persistentId")
        if pid == old_persistent_id:
            pid = new_persistent_id
            changed = True
        rel_code = (r.get("relation") or {}).get("code", "")
        key = (pid, rel_code)
        if pid == persistent_id or key in seen:
            continue  # drop self-reference or now-duplicate entry
        seen.add(key)
        r = dict(r)
        r["persistentId"] = pid
        new_related.append(r)

    if not changed:
        return False, "No relatedItems entry pointing at the merged-away item was found."

    item["relatedItems"] = new_related
    return put_item(
        category, persistent_id, item, api_url, bearer,
        source=f"Item Duplicates: repointed relatedItems from {old_persistent_id} "
               f"to {new_persistent_id} on {category}/{persistent_id}",
    )


def _merge_items_native(
    category: str, keep_pid: str, merge_pid: str, payload: dict, api_url: str, bearer: str,
) -> dict:
    """
    Merge via the Marketplace's own POST /api/{category-path}/merge?with={ids}
    endpoint, instead of merge_items()'s default GET-consolidate-PUT-repoint-
    DELETE flow.

    Investigated empirically against Stage (2026-10-09) since its behavior
    isn't documented beyond the OpenAPI shape, and it does NOT work the way
    actor merging does:
      - EVERY persistentId listed in `with` is hard-deleted — including
        keep_pid. There is no way to tell the API "preserve this identity."
      - `payload` becomes the content of a brand-new item with a freshly
        minted persistentId. Confirmed via repeated live tests (both
        single-id and two-id `with` lists).
      - The Marketplace serves no redirect from a retired persistentId, so
        every existing link/citation to EITHER merged item breaks, not just
        the discarded one.
      - In exchange, the API repoints relatedItems on every *other* item
        that referenced either merged id — server-side, against live data —
        confirmed via a live referrer item. This is more thorough than
        repoint_related_item(), which only catches referrers already found
        via a local snapshot search.

    Because of the persistentId loss, this is opt-in only (see the "use
    native merge endpoint" toggle on the Merge Items UI) — never the default.

    Returns {"ok", "message", "repointed": [], "new_persistent_id"}. The
    empty "repointed" list (vs. merge_items()'s per-referrer results) reflects
    that the API does this itself; there is nothing for us to report per item.
    """
    import json as _json
    path = _CATEGORY_PATH.get(category, category + "s")
    url = f"{api_url}/api/{path}/merge"
    resp = requests.post(
        url,
        params=[("with", keep_pid), ("with", merge_pid)],
        headers={"Content-Type": "application/json", "Authorization": bearer},
        json=payload,
        timeout=30,
    )
    ok = resp.status_code in (200, 201)
    new_pid = None
    if ok:
        try:
            new_pid = resp.json().get("persistentId")
        except ValueError:
            ok = False

    log_api(
        "POST", f"{url}?with={keep_pid}&with={merge_pid}",
        f"Native merge {category}/{keep_pid},{merge_pid}",
        status=resp.status_code,
        request=_json.dumps({"label": payload.get("label", "")})[:500],
        response=resp.text[:300],
        ok=ok,
        source=(
            f"Item Duplicates: native-merged {category}/{keep_pid} and {category}/{merge_pid} "
            f"into new item {new_pid}" if ok else
            f"Item Duplicates: native merge of {category}/{keep_pid} and {category}/{merge_pid} failed"
        ),
    )
    if not ok:
        return {
            "ok": False,
            "message": f"Native merge failed: HTTP {resp.status_code}: {resp.text[:300]}",
            "repointed": [], "new_persistent_id": None,
        }

    log_action(
        f"Native-merged {category}/{keep_pid} and {category}/{merge_pid} into new item {new_pid}",
        source="Item Duplicates",
    )
    return {
        "ok": True,
        "message": (
            f"Merged into new item `{new_pid}`. Both `{keep_pid}` and `{merge_pid}` were deleted — "
            f"the Marketplace does not redirect from retired persistentIds, so update any external "
            f"links that pointed to either one."
        ),
        "repointed": [],
        "new_persistent_id": new_pid,
    }


def merge_items(
    keep_category: str, keep_pid: str,
    merge_category: str, merge_pid: str,
    referrers: list[tuple[str, str]],
    api_url: str, bearer: str,
    payload: dict | None = None,
    use_native: bool = False,
) -> dict:
    """
    Merge merge_pid into keep_pid (both must be the same category):
      1. GET both items
      2. Build a consolidated payload for the keep item (consolidate_item_payload),
         unless `payload` is already supplied
      3. PUT the payload onto the keep item
      4. Repoint relatedItems on every (category, persistentId) pair in
         `referrers` from merge_pid to keep_pid
      5. DELETE the merge item

    `payload`, if given, is PUT as-is instead of the automatically
    consolidated result — used by the Merge Items UI when the curator has
    hand-picked which contributors/properties/external IDs/accessibleAt
    URLs/media/relatedItems survive the merge via checkboxes.

    `use_native`, if True, uses the Marketplace's own merge endpoint instead
    (see _merge_items_native()) — opt-in only, since it deletes *both*
    persistentIds and mints a new one rather than preserving keep_pid.

    Referrer-repointing failures are collected but do not abort the merge;
    the merge item is only deleted once the keep item has been updated
    successfully. Returns:
        {"ok": bool, "message": str, "repointed": [(persistentId, ok, msg), ...],
         "new_persistent_id": str | None}
    """
    if keep_category != merge_category:
        return {"ok": False, "message": "Items must be the same category to merge.",
                "repointed": [], "new_persistent_id": None}

    try:
        keep_item = get_item(keep_category, keep_pid, api_url, bearer)
        merge_item = get_item(merge_category, merge_pid, api_url, bearer)
    except Exception as e:
        return {"ok": False, "message": f"Failed to fetch items before merge: {e}",
                "repointed": [], "new_persistent_id": None}

    if payload is None:
        payload = consolidate_item_payload(keep_item, merge_item)

    if use_native:
        return _merge_items_native(keep_category, keep_pid, merge_pid, payload, api_url, bearer)

    ok, msg = put_item(
        keep_category, keep_pid, payload, api_url, bearer,
        source=f"Item Duplicates: merged {merge_category}/{merge_pid} into {keep_category}/{keep_pid}",
    )
    if not ok:
        return {"ok": False, "message": f"Failed to update keep item: {msg}",
                "repointed": [], "new_persistent_id": None}

    repointed: list[tuple[str, bool, str]] = []
    for ref_category, ref_pid in referrers:
        r_ok, r_msg = repoint_related_item(ref_category, ref_pid, merge_pid, keep_pid, api_url, bearer)
        repointed.append((ref_pid, r_ok, r_msg))

    del_ok, del_msg = delete_item(merge_category, merge_pid, api_url, bearer)
    if not del_ok:
        return {
            "ok": False,
            "message": f"Kept item was updated, but deleting the merged-away item failed: {del_msg}",
            "repointed": repointed,
            "new_persistent_id": None,
        }

    log_action(
        f"Merged item {merge_category}/{merge_pid} into {keep_category}/{keep_pid}",
        source="Item Duplicates",
    )
    return {
        "ok": True,
        "message": f"Merged {merge_category}/{merge_pid} into {keep_category}/{keep_pid}.",
        "repointed": repointed,
        "new_persistent_id": None,
    }


def get_concept(vocab_code: str, concept_code: str, api_url: str, bearer: str) -> dict | None:
    """
    GET /api/vocabularies/{vocab_code}/concepts/{concept_code} — the full concept
    record (code, label, uri, vocabulary, notation, candidate) as the API itself
    returns it, suitable for reuse as-is in a property's `concept` field when
    building a PUT payload (same idea as the new_concept dict built from
    fetch_all_concepts()/fetch_all_keyword_concepts() rows for fix_item_keyword()).

    Returns None if the vocabulary has no concept with that code (HTTP 404).
    No auth is actually required for reads, but bearer is passed for consistency
    with the rest of this module.
    """
    from urllib.parse import quote
    url = f"{api_url}/api/vocabularies/{vocab_code}/concepts/{quote(concept_code, safe='')}"
    resp = requests.get(url, headers={"Authorization": bearer}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def delete_concept(concept_code: str, vocab_code: str = "sshoc-keyword") -> tuple[bool, str]:
    """
    DELETE /api/vocabularies/{vocab_code}/concepts/{concept_code}?force=true

    force=true is required when the concept is referenced by existing item properties;
    the API will also remove those property references from the affected items.

    Concept codes come from the API as plain JSON strings (e.g. "10+languages").
    In JSON, '+' is a literal plus sign, not a space.  We must encode the code as
    an opaque string — quote() only, no unquote_plus — so that a '+' in the code
    becomes '%2B' in the URL path and the server decodes it back to the original '+'.
    """
    from urllib.parse import quote
    env = st.session_state["env"]
    bearer = st.session_state["bearer"]
    safe_code = quote(concept_code, safe="")
    url = f"{env['api_url']}/api/vocabularies/{vocab_code}/concepts/{safe_code}?force=true"
    try:
        resp = requests.delete(url, headers={"Authorization": bearer}, timeout=15)
        ok = resp.status_code in (200, 204)
        log_api(
            "DELETE", url,
            f"Delete concept '{concept_code}' from vocab '{vocab_code}' (force=true)",
            status=resp.status_code,
            response=resp.text[:300],
            ok=ok,
            source=f"Keywords: deleted concept '{concept_code}' from '{vocab_code}'",
        )
        if ok:
            return True, f"Concept '{concept_code}' deleted."
        return False, f"API returned {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as e:
        log_api("DELETE", url, f"Delete concept '{concept_code}' — request failed",
                status="error", response=str(e), ok=False,
                source=f"Keywords: deleted concept '{concept_code}' from '{vocab_code}'")
        return False, f"Request failed: {e}"


_CATEGORY_FETCH = [
    ("tools-services",     "tools"),
    ("publications",       "publications"),
    ("training-materials", "trainingMaterials"),
    ("workflows",          "workflows"),
    ("datasets",           "datasets"),
]


_SNAPSHOT_PERPAGE = 20   # smaller pages → shorter per-request response time
_SNAPSHOT_TIMEOUT = 60   # seconds per request
_SNAPSHOT_RETRIES = 3    # retries on timeout / 5xx before giving up


def _fetch_page(url: str, headers: dict, page: int, retries: int = _SNAPSHOT_RETRIES) -> dict:
    """GET a single paginated page with retry on timeout or 5xx."""
    paged = f"{url}?perpage={_SNAPSHOT_PERPAGE}&page={page}"
    for attempt in range(retries):
        try:
            resp = requests.get(paged, headers=headers, timeout=_SNAPSHOT_TIMEOUT)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
            # 5xx — wait and retry
        except requests.exceptions.Timeout:
            pass  # retry below
        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # 1 s, 2 s back-off
    raise RuntimeError(
        f"Failed to fetch {paged} after {retries} attempts "
        f"(timeout={_SNAPSHOT_TIMEOUT}s)"
    )


def create_snapshot_from_api(api_url: str, bearer: str, data_dir, env_label: str = "") -> tuple[bool, str]:
    """
    Fetch all items from all 5 categories and save as full_items_{ts}.json.
    Uses small page sizes and retries to handle slow category endpoints.
    Returns (success, message).
    """
    import json
    import time as _time
    import pathlib as _pathlib

    headers = {"Authorization": bearer}
    all_items: list = []
    n_cats = len(_CATEGORY_FETCH)
    bar = st.progress(0.0, text="Starting…")

    for cat_idx, (path, items_key) in enumerate(_CATEGORY_FETCH):
        url = f"{api_url}/api/{path}"

        # First page — also gives us the total page count
        try:
            data = _fetch_page(url, headers, page=1)
        except Exception as e:
            bar.empty()
            return False, f"Failed to fetch {path}: {e}"

        total_pages = data.get("pages", 1)
        all_items.extend(data.get(items_key, []))
        bar.progress(
            (cat_idx + 1 / max(total_pages, 1)) / n_cats,
            text=f"{path}: page 1 / {total_pages}",
        )

        for page in range(2, total_pages + 1):
            try:
                r = _fetch_page(url, headers, page=page)
                all_items.extend(r.get(items_key, []))
            except Exception as e:
                bar.empty()
                return False, f"Failed fetching {path} page {page}: {e}"
            bar.progress(
                (cat_idx + page / total_pages) / n_cats,
                text=f"{path}: page {page} / {total_pages}",
            )

    bar.empty()
    ts = int(_time.time())
    out_path = _pathlib.Path(data_dir) / f"full_items_{ts}.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(all_items, fh)
    except Exception as e:
        log_action(f"Snapshot creation failed: {e}", ok=False, source="Data Source")
        return False, f"Failed to save snapshot: {e}"

    # Write sidecar metadata so the Data page can show which environment this came from
    import datetime as _dt
    meta_path = out_path.with_suffix(".meta")
    try:
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({
                "source": "api",
                "env_label": env_label,
                "api_url": api_url,
                "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            }, fh, indent=2)
    except Exception:
        pass  # metadata is best-effort

    msg = f"Created snapshot {out_path.name} with {len(all_items)} items from {api_url}"
    log_action(msg, source="Data Source")
    return True, f"Created {out_path.name} with {len(all_items)} items."


def get_actor(actor_id: int, api_url: str, bearer: str) -> dict:
    """Fetch the full actor record from GET /api/actors/{id}. Raises on error."""
    resp = requests.get(
        f"{api_url}/api/actors/{actor_id}",
        headers={"Authorization": bearer},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_current_user(api_url: str, bearer: str) -> dict:
    """
    Fetch the logged-in account from GET /api/auth/me. Raises on error.

    Returns a UserDto dict including `role` (one of "contributor",
    "system-contributor", "moderator", "system-moderator", "administrator"),
    used to gate UI actions the account doesn't have permission to perform.
    """
    resp = requests.get(
        f"{api_url}/api/auth/me",
        headers={"Authorization": bearer},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _consolidate_actor_payload(actors: list[dict]) -> dict:
    """
    Build a PUT-ready ActorCore payload that preserves all attributes across
    a set of actors being merged. The first actor in the list is the 'keep' actor.

    - name: keep actor's name
    - email / website: keep actor's value; falls back to first non-empty value
      from the other actors if the keep actor has none
    - externalIds: union of all actors', deduped by (service code, identifier)
    - affiliations: keep actor's affiliations only (affiliations from discarded
      actors reference actors that will be deleted)
    """
    keep = actors[0]

    email = keep.get("email") or ""
    if not email:
        email = next((a["email"] for a in actors[1:] if a.get("email")), "")

    website = keep.get("website") or ""
    if not website:
        website = next((a["website"] for a in actors[1:] if a.get("website")), "")

    seen: set[tuple] = set()
    ext_ids: list[dict] = []
    for actor in actors:
        for eid in actor.get("externalIds", []):
            code = eid.get("identifierService", {}).get("code", "")
            identifier = eid.get("identifier", "")
            if (code, identifier) not in seen:
                seen.add((code, identifier))
                ext_ids.append({
                    "identifierService": {"code": code},
                    "identifier": identifier,
                })

    affiliations = [
        {"id": aff["id"]}
        for aff in keep.get("affiliations", [])
        if aff.get("id")
    ]

    payload: dict = {"name": keep["name"]}
    if email:
        payload["email"] = email
    if website:
        payload["website"] = website
    if ext_ids:
        payload["externalIds"] = ext_ids
    if affiliations:
        payload["affiliations"] = affiliations
    return payload


def merge_actors(keep_id: int, merge_ids: list) -> tuple[bool, str]:
    """
    Merge actors into keep_id:
      1. GET all actors to collect email, website, externalIds
      2. PUT keep actor with the consolidated attributes
      3. POST /api/actors/{keep_id}/merge?with={merge_ids}
    This ensures no data is silently dropped when the discarded actors have
    attributes the keep actor lacks.
    """
    env = st.session_state["env"]
    bearer = st.session_state["bearer"]
    api_url = env["api_url"]

    # ── Step 1: fetch all actors ──────────────────────────────────────────────
    try:
        actors = [get_actor(keep_id, api_url, bearer)]
        for mid in merge_ids:
            actors.append(get_actor(mid, api_url, bearer))
    except Exception as e:
        return False, f"Failed to fetch actor data before merge: {e}"

    # ── Step 2: consolidate and update keep actor ─────────────────────────────
    payload = _consolidate_actor_payload(actors)
    put_url = f"{api_url}/api/actors/{keep_id}"
    try:
        put_resp = requests.put(
            put_url,
            headers={"Content-Type": "application/json", "Authorization": bearer},
            json=payload,
            timeout=15,
        )
        put_resp.raise_for_status()
        log_api("PUT", put_url,
                f"Consolidate attributes on actor {keep_id} before merge",
                status=put_resp.status_code,
                request=str(payload)[:500],
                response=put_resp.text[:300],
                ok=True,
                source=f"Actors: merged actor(s) {merge_ids} into {keep_id}")
    except Exception as e:
        log_api("PUT", put_url,
                f"Failed to consolidate actor {keep_id} before merge",
                status="error", response=str(e), ok=False,
                source=f"Actors: merged actor(s) {merge_ids} into {keep_id}")
        return False, f"Failed to update keep actor before merge: {e}"

    # ── Step 3: merge ─────────────────────────────────────────────────────────
    with_param = ",".join(str(i) for i in merge_ids)
    merge_url = f"{api_url}/api/actors/{keep_id}/merge?with={with_param}"
    try:
        resp = requests.post(
            merge_url,
            headers={"Content-Type": "application/json", "Authorization": bearer},
            timeout=15,
        )
        ok = resp.status_code == 200
        log_api(
            "POST", merge_url,
            f"Merge actor(s) {merge_ids} into {keep_id}",
            status=resp.status_code,
            response=resp.text[:300],
            ok=ok,
            source=f"Actors: merged actor(s) {merge_ids} into {keep_id}",
        )
        if ok:
            return True, f"Actor(s) {merge_ids} merged into {keep_id}."
        return False, f"API returned {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as e:
        log_api("POST", merge_url,
                f"Merge actors {merge_ids} into {keep_id} — request failed",
                status="error", response=str(e), ok=False,
                source=f"Actors: merged actor(s) {merge_ids} into {keep_id}")
        return False, f"Request failed: {e}"
