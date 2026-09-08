"""
OpenAIRE Enrichment — find items with a DOI and backfill metadata OpenAIRE
already has on file for it.

Scope (deliberately limited)
  Only fields that are genuinely *missing* on the Marketplace item are ever
  proposed — existing curator-entered values are never shown as
  "conflicting" or overwritten. Five fields are covered: year, publisher,
  language, keyword, accessibleAt. License and author/contributor
  enrichment are out of scope: MP's `license` property uses a closed
  SPDX-style vocabulary that OpenAIRE's free-text license strings (e.g.
  "CC BY") don't map onto cleanly, and adding authors risks creating
  duplicate Actor records — actor deduplication is already a recurring
  cleanup task handled by the Actors page.

Workflow
  1. Extract DOI items — scan the snapshot for items (in the selected
     categories) that carry a `doi` externalId.
  2. Look up on OpenAIRE — concurrent, rate-limited lookups via
     lib.openaire.fetch_many() (exact DOI match, so no fuzzy-matching risk),
     cached to disk so a re-run doesn't re-spend rate-limit budget. The full
     sshoc-keyword vocabulary is also loaded at this point (reusing the
     Keywords page's session_state["keyword_vocab"] if already loaded).
  3. Review — one row per item, grouped by whether OpenAIRE had anything to
     propose. Proposed fields are shown per item with checkboxes (all
     checked by default) alongside the OpenAIRE record's title, so the
     curator can sanity-check the match before applying.
  4. Apply — one item at a time, deliberately: there is no bulk "apply all"
     action, since a wrong guess written to many items at once would be far
     more costly than the same guess on one. Reuses lib.api.get_item()/
     put_item() (same GET-then-PUT round trip as fix_item_keyword());
     put_item() already logs the write to the Session Log. On success the
     item is immediately dropped from the actionable "With proposals" list
     into its own "Applied" bucket (metric, filter, and CSV column) — no
     page navigation needed to see it took effect. A failed attempt stays in
     the list, auto-expanded with the error shown, so it can be retried.

Every PUT sends the complete item object returned by GET, with only the
target properties appended (see _apply_proposal()) — same convention as
every other write in this toolkit. Property `concept` payloads are always
*complete, already-existing* concept records fetched from the live API, in
the same shape fix_item_keyword() already relies on
(`{code, label, uri, vocabulary: {code}}`) — nothing is invented:
  - language: resolved by exact code via lib.api.get_concept("iso-639-3",
    code) — OpenAIRE already reports ISO 639-3 codes, the same vocabulary
    Marketplace uses for this field, so a resolvable code is the common case;
    codes that don't resolve are simply not proposed.
  - keyword: OpenAIRE's subject strings are matched against the *existing*
    sshoc-keyword vocabulary by case-insensitive label (same match rule the
    Keywords page's "Duplicates in other vocabs" tab uses). Only subjects
    with a match are proposed — no new candidate concepts are created.

Shared state (st.session_state keys)
  openaire_doi_items          – DataFrame from extract_doi_items()
  openaire_lookup             – dict[doi -> fetch_one()-shaped result]
  openaire_apply_status       – dict[persistentId -> (ok, message)] from Apply actions
  keyword_vocab               – DataFrame of sshoc-keyword concepts (shared with the Keywords page)
  openaire_language_concepts  – dict[iso-639-3 code -> resolved concept dict or None], cached per session
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import streamlit as st
import pandas as pd

from lib.auth import require_login, render_account_caption
from lib.mplib import get_util
from lib.snapshot import render_data_status, require_snapshot
from lib.api import get_item, put_item, fetch_all_keyword_concepts, get_concept
from lib.openaire import normalize_doi, fetch_many

require_login()

st.set_page_config(page_title="OpenAIRE Enrichment — Curation Toolkit", page_icon="📚", layout="wide")

env = st.session_state["env"]
st.title("OpenAIRE Enrichment")
render_account_caption(env)

MP_SERVER = env["mp_url"]
API_URL = env["api_url"]

require_snapshot()
render_data_status()


@st.cache_data(show_spinner="Loading snapshot…")
def load_snapshot() -> pd.DataFrame:
    return get_util()._load_snapshot()


# ── Snapshot extraction ────────────────────────────────────────────────────────

def _get_doi(row) -> str | None:
    """First doi externalId on the item, normalized to a bare DOI (no doi.org prefix)."""
    for e in row.get("externalIds") or []:
        if not isinstance(e, dict):
            continue
        if (e.get("identifierService") or {}).get("code", "").lower() == "doi":
            doi = normalize_doi(e.get("identifier", ""))
            if doi:
                return doi
    return None


def _current_values(row) -> dict:
    """Current values of the 5 target fields, so we know what's actually missing."""
    props = row.get("properties") or []
    year = next((p.get("value") for p in props if (p.get("type") or {}).get("code") == "year"), None)
    publisher = next((p.get("value") for p in props if (p.get("type") or {}).get("code") == "publisher"), None)
    lang_prop = next((p for p in props if (p.get("type") or {}).get("code") == "language"), None)
    language = (lang_prop.get("concept") or {}).get("label") if lang_prop else None
    keywords = [
        (p.get("concept") or {}).get("label")
        for p in props
        if (p.get("type") or {}).get("code") == "keyword" and (p.get("concept") or {}).get("label")
    ]
    return {
        "year": year,
        "publisher": publisher,
        "language": language,
        "keywords": keywords,
        "accessibleAt": row.get("accessibleAt") or [],
    }


def extract_doi_items(snap: pd.DataFrame, selected_cats: list) -> pd.DataFrame:
    """One row per DOI-bearing item in the selected categories, with current field values."""
    subset = snap[snap["category"].isin(selected_cats)]
    rows = []
    for _, row in subset.iterrows():
        doi = _get_doi(row)
        if not doi:
            continue
        cur = _current_values(row)
        rows.append({
            "persistentId": row.get("persistentId", ""),
            "category": row.get("category", ""),
            "label": row.get("label", ""),
            "doi": doi,
            "cur_year": cur["year"],
            "cur_publisher": cur["publisher"],
            "cur_language": cur["language"],
            "cur_keywords": cur["keywords"],
            "cur_accessibleAt": cur["accessibleAt"],
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Concept resolution ──────────────────────────────────────────────────────────
# Concept payloads must be complete, already-existing concept records — never
# invented. Both helpers below return that exact shape (or omit the field).

def _resolve_keywords(keywords: list[str], vocab_df: pd.DataFrame | None) -> list[dict]:
    """
    Match OpenAIRE subject strings against the *existing* sshoc-keyword
    vocabulary by case-insensitive label (same rule the Keywords page's
    "Duplicates in other vocabs" tab uses). Only matched, already-existing
    concepts are returned — nothing new is created.
    """
    if vocab_df is None or vocab_df.empty:
        return []
    by_label = {str(row["label"]).strip().lower(): row for _, row in vocab_df.iterrows()}
    resolved, seen_codes = [], set()
    for kw in keywords:
        row = by_label.get(str(kw).strip().lower())
        if row is not None and row["code"] not in seen_codes:
            seen_codes.add(row["code"])
            resolved.append({
                "code": row["code"],
                "label": row["label"],
                "uri": row["uri"],
                "vocabulary": {"code": "sshoc-keyword"},
            })
    return resolved


def _resolve_language(language_code: str, cache: dict) -> dict | None:
    """
    Resolve an ISO 639-3 code to the full, existing concept record via
    lib.api.get_concept(), caching per code (shared across all items in this
    run) since only a handful of distinct language codes typically appear.
    Returns None if the code doesn't exist in the Marketplace's copy of the
    vocabulary — in that case the field is simply not proposed.
    """
    if language_code not in cache:
        try:
            cache[language_code] = get_concept(
                "iso-639-3", language_code, API_URL, st.session_state["bearer"]
            )
        except Exception:
            cache[language_code] = None
    return cache[language_code]


# ── Proposal building ───────────────────────────────────────────────────────────

def _build_proposal(item: dict, lookup: dict | None, vocab_df: pd.DataFrame | None, lang_cache: dict) -> dict:
    """
    Compare one extracted item's current values against its OpenAIRE lookup
    result. Returns {"status", "message", "openaire_title", "proposed"} where
    `proposed` only has keys for fields that are missing on the MP side,
    present on the OpenAIRE side, and — for language/keyword — resolvable to
    an existing Marketplace concept.
    """
    if lookup is None:
        return {"status": "error", "message": "Not looked up", "openaire_title": None, "proposed": {}}
    if lookup.get("status") != "found":
        return {
            "status": lookup.get("status", "error"),
            "message": lookup.get("message", ""),
            "openaire_title": None,
            "proposed": {},
        }

    f = lookup["fields"]
    proposed = {}
    if not item["cur_year"] and f.get("year"):
        proposed["year"] = f["year"]
    if not item["cur_publisher"] and f.get("publisher"):
        proposed["publisher"] = f["publisher"]
    if not item["cur_language"] and f.get("language_code"):
        concept = _resolve_language(f["language_code"], lang_cache)
        if concept:
            proposed["language"] = concept
    if not item["cur_keywords"] and f.get("keywords"):
        matched = _resolve_keywords(f["keywords"], vocab_df)
        if matched:
            proposed["keywords"] = matched
    if not item["cur_accessibleAt"] and f.get("urls"):
        proposed["accessibleAt"] = f["urls"]

    return {"status": "found", "message": "", "openaire_title": f.get("title"), "proposed": proposed}


def _apply_proposal(category: str, persistent_id: str, fields_to_apply: dict) -> tuple[bool, str]:
    """
    GET the live item, append the given fields (only if still missing — the
    live item may have changed since the snapshot was taken), and PUT the
    complete object back. Reuses lib.api.get_item()/put_item(), the same
    GET-then-PUT round trip fix_item_keyword() uses; put_item() logs the write.
    `fields_to_apply["language"]` and `["keywords"]` are already-resolved,
    complete concept record(s) — see _resolve_language()/_resolve_keywords().
    """
    try:
        item = get_item(category, persistent_id, API_URL, st.session_state["bearer"])
    except Exception as e:
        return False, f"GET failed: {e}"

    props = item.setdefault("properties", [])
    existing_codes = {(p.get("type") or {}).get("code") for p in props}
    changed = False

    if "year" in fields_to_apply and "year" not in existing_codes:
        props.append({"type": {"code": "year"}, "value": fields_to_apply["year"]})
        changed = True
    if "publisher" in fields_to_apply and "publisher" not in existing_codes:
        props.append({"type": {"code": "publisher"}, "value": fields_to_apply["publisher"]})
        changed = True
    if "language" in fields_to_apply and "language" not in existing_codes:
        props.append({"type": {"code": "language"}, "concept": fields_to_apply["language"]})
        changed = True
    if "keywords" in fields_to_apply and "keyword" not in existing_codes:
        for concept in fields_to_apply["keywords"]:
            props.append({"type": {"code": "keyword"}, "concept": concept})
        changed = True
    if "accessibleAt" in fields_to_apply and not item.get("accessibleAt"):
        item["accessibleAt"] = fields_to_apply["accessibleAt"]
        changed = True

    if not changed:
        return False, "Nothing to apply — item already has these fields."

    return put_item(category, persistent_id, item, API_URL, st.session_state["bearer"])


snap = load_snapshot()

if snap.empty:
    st.warning("No snapshot data found.")
    st.stop()

# ── Configuration ────────────────────────────────────────────────────────────────
st.subheader("Configuration")
ctl_left, ctl_right = st.columns([1, 1])

with ctl_left:
    all_categories = sorted(snap["category"].dropna().unique().tolist())
    selected_cats = st.multiselect("Filter by category", all_categories, default=all_categories)
    token = st.text_input(
        "OpenAIRE access token (optional)",
        type="password",
        help=(
            "Raises the OpenAIRE API rate limit from 60 to 7200 requests/hour. "
            "Get one from the OpenAIRE developer portal — valid for about an hour. "
            "Never written to disk, only kept for this session."
        ),
    )

with ctl_right:
    timeout = st.slider("Timeout per lookup (s)", min_value=5, max_value=30, value=15)
    workers = st.slider("Parallel workers", min_value=1, max_value=10, value=5,
                         help="Requests are also throttled to the applicable hourly rate limit regardless of worker count.")

extract_btn = st.button("Extract DOI items from snapshot", use_container_width=True)

if extract_btn:
    with st.spinner("Scanning snapshot for DOIs…"):
        doi_df = extract_doi_items(snap, selected_cats)
    st.session_state["openaire_doi_items"] = doi_df
    st.session_state.pop("openaire_lookup", None)
    st.session_state.pop("openaire_apply_status", None)

# ── Pre-lookup summary ────────────────────────────────────────────────────────────
doi_df: pd.DataFrame | None = st.session_state.get("openaire_doi_items")

if doi_df is not None:
    st.divider()

    if doi_df.empty:
        st.warning("No items with a DOI found in the selected categories.")
    else:
        unique_dois = doi_df["doi"].drop_duplicates().tolist()

        m1, m2, m3 = st.columns(3)
        m1.metric("Items with a DOI", len(doi_df))
        m2.metric("Unique DOIs", len(unique_dois))
        m3.metric("Categories", doi_df["category"].nunique())

        st.caption("By category: " + ", ".join(
            f"{cat} ({n})" for cat, n in doi_df["category"].value_counts().items()
        ))

        if st.button(f"Look up {len(unique_dois)} DOIs on OpenAIRE", type="primary",
                     use_container_width=True, key="run_lookup"):
            if "keyword_vocab" not in st.session_state:
                with st.spinner("Loading sshoc-keyword vocabulary (for keyword matching)…"):
                    st.session_state["keyword_vocab"] = fetch_all_keyword_concepts(
                        API_URL, st.session_state["bearer"]
                    )
            lookup = fetch_many(unique_dois, token=token or None, workers=workers, timeout=timeout)
            st.session_state["openaire_lookup"] = lookup

            lang_cache = st.session_state.setdefault("openaire_language_concepts", {})
            lang_codes = {
                r["fields"]["language_code"]
                for r in lookup.values()
                if r.get("status") == "found" and r["fields"].get("language_code")
            }
            new_codes = lang_codes - lang_cache.keys()
            if new_codes:
                with st.spinner(f"Resolving {len(new_codes)} language concept(s)…"):
                    for code in new_codes:
                        _resolve_language(code, lang_cache)

            st.session_state.pop("openaire_apply_status", None)
            st.rerun()

# ── Results ────────────────────────────────────────────────────────────────────
lookup: dict | None = st.session_state.get("openaire_lookup")

if doi_df is not None and not doi_df.empty and lookup is not None:
    st.divider()
    st.subheader("Results")

    apply_status: dict = st.session_state.setdefault("openaire_apply_status", {})
    vocab_df: pd.DataFrame | None = st.session_state.get("keyword_vocab")
    lang_cache: dict = st.session_state.setdefault("openaire_language_concepts", {})

    records = []
    for _, item in doi_df.iterrows():
        pid = item["persistentId"]
        result = _build_proposal(item.to_dict(), lookup.get(item["doi"]), vocab_df, lang_cache)
        status_entry = apply_status.get(pid)
        records.append({
            **item.to_dict(), **result,
            "n_proposed": len(result["proposed"]),
            "applied_ok": status_entry[0] if status_entry else None,
            "apply_message": status_entry[1] if status_entry else "",
        })

    results_df = pd.DataFrame(records)

    # An item counts as "with proposals" only until it's been *successfully*
    # applied — once applied it moves to its own bucket instead of lingering
    # in the actionable list, so the curator sees it move rather than having
    # to notice a small checkmark. Failed attempts stay actionable so they
    # can be retried.
    is_pending = (results_df["n_proposed"] > 0) & (results_df["applied_ok"] != True)  # noqa: E712

    n_with_proposals = int(is_pending.sum())
    n_applied = int((results_df["applied_ok"] == True).sum())  # noqa: E712
    n_found_complete = int(((results_df["status"] == "found") & (results_df["n_proposed"] == 0)).sum())
    n_not_found = int((results_df["status"] == "not_found").sum())
    n_errors = int((results_df["status"] == "error").sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("With proposals", n_with_proposals)
    c2.metric("Applied", n_applied)
    c3.metric("Found, nothing missing", n_found_complete)
    c4.metric("Not found on OpenAIRE", n_not_found)
    c5.metric("Lookup errors", n_errors)

    filter_choice = st.radio(
        "Show",
        ["With proposals", "Applied", "Found, nothing missing", "Not found on OpenAIRE", "Lookup errors", "All"],
        horizontal=True,
        key="openaire_result_filter",
    )

    if filter_choice == "With proposals":
        view = results_df[is_pending]
    elif filter_choice == "Applied":
        view = results_df[results_df["applied_ok"] == True]  # noqa: E712
    elif filter_choice == "Found, nothing missing":
        view = results_df[(results_df["status"] == "found") & (results_df["n_proposed"] == 0)]
    elif filter_choice == "Not found on OpenAIRE":
        view = results_df[results_df["status"] == "not_found"]
    elif filter_choice == "Lookup errors":
        view = results_df[results_df["status"] == "error"]
    else:
        view = results_df

    view = view.sort_values(["n_proposed", "persistentId"], ascending=[False, True])
    view = view.assign(**{
        "item link": MP_SERVER + view["category"] + "/" + view["persistentId"],
        "applied": view["applied_ok"].map({True: "✅ applied", False: "❌ failed"}).fillna(""),
    })

    disp = view[["label", "category", "doi", "status", "n_proposed", "applied", "message", "item link"]].reset_index(drop=True)
    st.dataframe(
        disp,
        use_container_width=True,
        column_config={
            "n_proposed": st.column_config.NumberColumn("Proposed fields"),
            "doi": st.column_config.TextColumn("DOI"),
            "item link": st.column_config.LinkColumn("Item"),
        },
        hide_index=True,
    )

    csv = disp.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, "openaire_enrichment_results.csv", "text/csv")

    # ── Apply, one item at a time ────────────────────────────────────────────────
    pending_rows = results_df[is_pending]
    if not pending_rows.empty:
        st.divider()
        st.markdown(f"**{len(pending_rows)} item(s) still have at least one proposed field.**")
        for _, r in pending_rows.iterrows():
            pid = r["persistentId"]
            failed = r["applied_ok"] is False
            with st.expander(
                f"{r['label']} — {r['n_proposed']} field(s) proposed" + (" — ❌ last attempt failed" if failed else ""),
                expanded=failed,
            ):
                st.caption(f"{r['category']} / {pid}  ·  DOI: [{r['doi']}](https://doi.org/{r['doi']})")
                if r["openaire_title"]:
                    st.caption(f"OpenAIRE title: _{r['openaire_title']}_")

                selected_fields = {}
                for field, value in r["proposed"].items():
                    if field == "keywords":
                        label = f"keyword: {', '.join(c['label'] for c in value)}"
                    elif field == "language":
                        label = f"language: {value['label']} ({value['code']})"
                    elif field == "accessibleAt":
                        label = "accessibleAt: " + ", ".join(value)
                    else:
                        label = f"{field}: {value}"
                    if st.checkbox(label, value=True, key=f"oa_field_{pid}_{field}"):
                        selected_fields[field] = value

                if failed:
                    st.error(r["apply_message"])

                if st.button("Apply to this item", key=f"oa_apply_{pid}", disabled=not selected_fields):
                    ok, msg = _apply_proposal(r["category"], pid, selected_fields)
                    apply_status[pid] = (ok, msg)
                    st.toast(f"{r['label']}: {msg}", icon="✅" if ok else "⚠️")
                    st.rerun()
