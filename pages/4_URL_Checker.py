"""
URL Checker — batch-verify HTTP(S) links extracted from snapshot items.

Two modes are supported:
  accessibleAt only  – checks only the primary access URL(s) for each item.
  All URLs           – scans every field in every item for http(s) links
                       (thumbnails, external IDs, media, etc.).

URLs are checked concurrently using a ThreadPoolExecutor.  Each URL receives
a HEAD request; servers that reject HEAD (405/501) are retried with a
streaming GET to avoid downloading the full body.

Results are sorted so items with the most broken links appear first.

Shared state (st.session_state keys)
  url_check_input    – DataFrame of (persistentId, category, label, field, url)
  url_check_mode     – human-readable scope label for display
  url_check_results  – url_check_input extended with status, ok, error columns
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import re
import streamlit as st
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.auth import require_login, render_account_caption
from lib.mplib import get_util
from lib.snapshot import render_data_status, require_snapshot

require_login()

st.set_page_config(page_title="URL Checker — Curation Toolkit", page_icon="🔗", layout="wide")

env = st.session_state["env"]
st.title("URL Checker")
render_account_caption(env)

MP_SERVER = env["mp_url"]

require_snapshot()
render_data_status()

_URL_RE = re.compile(r'https?://[^\s"\'<>\[\]{}|\\^`]+'  )


@st.cache_data(show_spinner="Loading snapshot…")
def load_snapshot() -> pd.DataFrame:
    return get_util()._load_snapshot()


def _check_one(url: str, timeout: int) -> dict:
    """
    Check a single URL and return a result dict with keys: url, status, ok, error.

    Sends a HEAD request first.  If the server returns 405 or 501 (method not
    allowed / not implemented) it falls back to a streaming GET so the body is
    never downloaded.  Network errors are captured and returned as error strings
    rather than raised as exceptions, so concurrent execution is never interrupted.
    """
    try:
        r = requests.head(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "SSHOMPCurationBot/1.0"},
        )
        if r.status_code in (405, 501):
            r = requests.get(
                url, timeout=timeout, stream=True,
                headers={"User-Agent": "SSHOMPCurationBot/1.0"},
            )
            r.close()
        return {"url": url, "status": r.status_code, "ok": r.status_code < 400, "error": ""}
    except requests.exceptions.Timeout:
        return {"url": url, "status": None, "ok": False, "error": "Timeout"}
    except requests.exceptions.ConnectionError:
        return {"url": url, "status": None, "ok": False, "error": "Connection error"}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)[:120]}


def _extract_urls_from_value(val) -> list[str]:
    """Recursively pull http(s) URLs out of strings, lists, and dicts."""
    if val is None:
        return []
    if isinstance(val, str):
        found = _URL_RE.findall(val)
        return [u.rstrip(".,;)'\">") for u in found]
    if isinstance(val, list):
        out = []
        for item in val:
            out.extend(_extract_urls_from_value(item))
        return out
    if isinstance(val, dict):
        out = []
        for v in val.values():
            out.extend(_extract_urls_from_value(v))
        return out
    return []


def extract_urls(snap: pd.DataFrame, selected_cats: list, mode: str) -> pd.DataFrame:
    """
    Extract all URLs from the snapshot and return one row per (item, url) pair.

    Parameters
    ----------
    snap          Full snapshot DataFrame.
    selected_cats List of category strings to include.
    mode          "accessibleAt" → only the accessibleAt field;
                  anything else  → every field in every row.

    Returns a DataFrame with columns: persistentId, category, label, field, url.
    Duplicate URLs within the same item are deduplicated in "all" mode.
    """
    subset = snap[snap["category"].isin(selected_cats)]
    rows = []

    if mode == "accessibleAt":
        if "accessibleAt" not in subset.columns:
            return pd.DataFrame()
        for _, row in subset.iterrows():
            for url in _extract_urls_from_value(row["accessibleAt"]):
                rows.append({
                    "persistentId": row.get("persistentId", ""),
                    "category": row.get("category", ""),
                    "label": row.get("label", ""),
                    "field": "accessibleAt",
                    "url": url,
                })
    else:
        for _, row in subset.iterrows():
            seen: set[str] = set()
            for col in subset.columns:
                for url in _extract_urls_from_value(row.get(col)):
                    if url not in seen:
                        seen.add(url)
                        rows.append({
                            "persistentId": row.get("persistentId", ""),
                            "category": row.get("category", ""),
                            "label": row.get("label", ""),
                            "field": col,
                            "url": url,
                        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


snap = load_snapshot()

if snap.empty:
    st.warning("No snapshot data found.")
    st.stop()

# ── Controls ──────────────────────────────────────────────────────────────────
st.subheader("Configuration")
ctl_left, ctl_right = st.columns([1, 1])

with ctl_left:
    all_categories = sorted(snap["category"].dropna().unique().tolist())
    selected_cats = st.multiselect("Filter by category", all_categories, default=all_categories)

    mode_label = st.radio(
        "URL scope",
        ["accessibleAt only", "All URLs in entry"],
        help=(
            "**accessibleAt only** — checks primary access URLs for each item.\n\n"
            "**All URLs** — scans every field (thumbnails, media, external IDs, …) for http(s) links."
        ),
    )
    mode = "accessibleAt" if "accessibleAt" in mode_label else "all"

with ctl_right:
    timeout = st.slider("Timeout per URL (s)", min_value=3, max_value=30, value=10)
    workers = st.slider("Parallel workers", min_value=5, max_value=80, value=20)

extract_btn = st.button("Extract URLs from snapshot", use_container_width=True)

if extract_btn:
    with st.spinner("Scanning snapshot for URLs…"):
        url_df = extract_urls(snap, selected_cats, mode)
    st.session_state["url_check_input"] = url_df
    st.session_state["url_check_mode"] = mode_label
    st.session_state.pop("url_check_results", None)

# ── Pre-check summary ─────────────────────────────────────────────────────────
url_df: pd.DataFrame | None = st.session_state.get("url_check_input")

if url_df is not None:
    st.divider()

    if url_df.empty:
        st.warning("No URLs found with the current settings.")
    else:
        n_unique = url_df["url"].nunique()
        n_items = url_df["persistentId"].nunique()

        m1, m2, m3 = st.columns(3)
        m1.metric("Unique URLs", n_unique)
        m2.metric("Items", n_items)
        m3.metric("Total references", len(url_df))

        st.caption(f"Scope: **{st.session_state.get('url_check_mode', '')}**")

        if st.button(f"Check {n_unique} URLs", type="primary", use_container_width=True,
                     key="run_check"):
            unique_urls = url_df["url"].drop_duplicates().tolist()
            results_map: dict[str, dict] = {}
            done = 0

            bar = st.progress(0, text=f"Checking… 0 / {len(unique_urls)}")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_check_one, u, timeout): u for u in unique_urls}
                for future in as_completed(futures):
                    res = future.result()
                    results_map[res["url"]] = res
                    done += 1
                    bar.progress(done / len(unique_urls),
                                 text=f"Checking… {done} / {len(unique_urls)}")
            bar.empty()

            result_df = url_df.copy()
            result_df["status"] = result_df["url"].map(
                lambda u: results_map.get(u, {}).get("status"))
            result_df["ok"] = result_df["url"].map(
                lambda u: results_map.get(u, {}).get("ok", False))
            result_df["error"] = result_df["url"].map(
                lambda u: results_map.get(u, {}).get("error", ""))
            st.session_state["url_check_results"] = result_df
            st.rerun()

# ── Results ───────────────────────────────────────────────────────────────────
results: pd.DataFrame | None = st.session_state.get("url_check_results")

if results is not None and not results.empty:
    st.divider()
    st.subheader("Results")

    ok_count = int(results.drop_duplicates("url")["ok"].sum())
    broken_count = int((~results.drop_duplicates("url")["ok"]).sum())
    timeout_count = int(
        results.drop_duplicates("url")["error"].str.contains("Timeout", na=False).sum()
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OK (< 400)", ok_count)
    c2.metric("Broken / unreachable", broken_count - timeout_count)
    c3.metric("Timed out", timeout_count)
    c4.metric("Unique URLs checked", results["url"].nunique())

    status_filter = st.radio(
        "Show",
        ["All", "Broken only", "OK only"],
        horizontal=True,
        key="url_result_filter",
    )

    # Count broken URLs per item, then sort: most broken items first,
    # broken URLs within each item before OK ones.
    broken_per_item = (
        results.groupby("persistentId")["ok"]
        .apply(lambda s: (~s).sum())
        .rename("item_broken")
    )
    view = results.merge(broken_per_item, on="persistentId")

    if status_filter == "Broken only":
        view = view[~view["ok"]]
    elif status_filter == "OK only":
        view = view[view["ok"]]

    view = view.sort_values(
        ["item_broken", "persistentId", "ok"],
        ascending=[False, True, True],
    )

    view["item link"] = MP_SERVER + view["category"] + "/" + view["persistentId"]

    disp = view[
        ["item_broken", "label", "category", "field", "url", "status", "error", "item link"]
    ].reset_index(drop=True)

    st.dataframe(
        disp,
        use_container_width=True,
        column_config={
            "item_broken": st.column_config.NumberColumn("Broken URLs", help="Broken URLs in this item"),
            "url": st.column_config.LinkColumn("URL"),
            "item link": st.column_config.LinkColumn("Item"),
            "status": st.column_config.NumberColumn("HTTP"),
            "error": st.column_config.TextColumn("Error"),
        },
        hide_index=True,
    )

    csv = disp.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, "url_check_results.csv", "text/csv")
