"""
OpenAIRE Enrichment — find items with a DOI and backfill metadata OpenAIRE
already has on file for it.

Scope (deliberately limited)
  Only fields that are genuinely *missing* on the Marketplace item are ever
  proposed — existing curator-entered values are never shown as
  "conflicting" or overwritten. Fields covered: year, publisher, language,
  discipline, standard, activity, keyword, accessibleAt. License and
  author/contributor enrichment are out of scope: MP's `license` property
  uses a closed SPDX-style vocabulary that OpenAIRE's free-text license
  strings (e.g. "CC BY") don't map onto cleanly, and adding authors risks
  creating duplicate Actor records — actor deduplication is already a
  recurring cleanup task handled by the Actors page.

  Closed vocabularies first, keyword as a last resort: `keyword` is the
  *only* concept-valued Marketplace property with an open (uncontrolled)
  vocabulary — every other one (language, discipline, standard, activity,
  license, ...) is closed. This page always tries to place an OpenAIRE value
  into the closed vocabulary it actually belongs to before ever falling back
  to the generic, open `keyword` property:
    - OpenAIRE's `FOS` (Field of Science) subjects go to `discipline` — the
      same OECD/ÖFOS classification scheme as the Marketplace's own closed
      discipline vocabulary (verified live; see _resolve_discipline()).
      Previously these were simply discarded.
    - OpenAIRE's `keyword`-scheme subjects are matched against `standard`
      and `activity` (both closed) before `sshoc-keyword` (open) — see
      _distribute_subjects() for the exact priority order and why vocabs
      describing the *resource itself* (category, audience, format, ...)
      are deliberately excluded from this cross-matching.

Workflow
  1. Extract DOI items — scan the snapshot for items (in the selected
     categories) that carry a `doi` externalId.
  2. Look up on OpenAIRE — concurrent, rate-limited lookups via
     lib.openaire.fetch_many() (exact DOI match, so no fuzzy-matching risk),
     cached to disk so a re-run doesn't re-spend rate-limit budget. The
     closed `standard`/`activity` vocabularies and the open `sshoc-keyword`
     vocabulary (reusing the Keywords page's session_state["keyword_vocab"]
     if already loaded) are all loaded at this point too.
  3. Review — one row per item, grouped by whether OpenAIRE had anything to
     propose. Proposed fields are shown per item with checkboxes (all
     checked by default) alongside the OpenAIRE record's title, so the
     curator can sanity-check the match before applying.
  4. Apply — one item at a time, deliberately: there is no bulk "apply all"
     action, since a wrong guess written to many items at once would be far
     more costly than the same guess on one. Reuses lib.api.get_item()/
     put_item() (same GET-then-PUT round trip as fix_item_keyword());
     put_item() already logs the write to the Session Log, tagged with a
     source built from the fields actually applied (see _apply_proposal()),
     e.g. "OpenAIRE Enrichment: added year: 2020, publisher: DIGITAL.CSIC to
     'SimilArITI'". On success the item stays right where it was in the
     review list — in the same order, not moved or removed — but its
     expander collapses, is marked "✅ applied", and switches to a read-only
     summary of what was added, instead of disappearing (a curator working
     down the list would otherwise lose their place). It also moves out of
     the "With proposals" bucket into "Applied" in the summary metrics/
     filter/CSV above. A failed attempt stays expanded and interactive in
     the review list, with the error shown, so it can be corrected and
     retried.

Every PUT sends the complete item object returned by GET, with only the
target properties appended (see _apply_proposal()) — same convention as
every other write in this toolkit. Property `concept` payloads are always
*complete, already-existing* concept records fetched from the live API —
nothing is invented. See the "How OpenAIRE fields map to Marketplace
fields" expander on the page itself for the full field-by-field mapping
shown to curators.

Shared state (st.session_state keys)
  openaire_doi_items           – DataFrame from extract_doi_items()
  openaire_lookup               – dict[doi -> fetch_one()-shaped result]
  openaire_apply_status         – dict[persistentId -> (ok, message)] from Apply actions
  keyword_vocab                 – DataFrame of sshoc-keyword concepts (shared with the Keywords page)
  openaire_closed_vocabs        – dict[property type code -> DataFrame], e.g. "standard"/"activity"
  openaire_language_concepts    – dict[iso-639-3 code -> resolved concept dict or None], cached per session
  openaire_discipline_concepts  – dict[discipline code -> resolved concept dict or None], cached per session
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import streamlit as st
import pandas as pd

from lib.auth import require_login, render_account_caption
from lib.mplib import get_util
from lib.snapshot import render_data_status, require_snapshot
from lib.api import get_item, put_item, fetch_all_keyword_concepts, fetch_concepts_by_type, get_concept
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


def _current_concept_labels(props: list, type_code: str) -> list[str]:
    """Labels of every existing property of one concept-valued type (keyword, discipline, standard, activity, ...)."""
    return [
        (p.get("concept") or {}).get("label")
        for p in props
        if (p.get("type") or {}).get("code") == type_code and (p.get("concept") or {}).get("label")
    ]


def _current_values(row) -> dict:
    """Current values of the target fields, so we know what's actually missing."""
    props = row.get("properties") or []
    year = next((p.get("value") for p in props if (p.get("type") or {}).get("code") == "year"), None)
    publisher = next((p.get("value") for p in props if (p.get("type") or {}).get("code") == "publisher"), None)
    lang_prop = next((p for p in props if (p.get("type") or {}).get("code") == "language"), None)
    language = (lang_prop.get("concept") or {}).get("label") if lang_prop else None
    return {
        "year": year,
        "publisher": publisher,
        "language": language,
        "discipline": _current_concept_labels(props, "discipline"),
        "standard": _current_concept_labels(props, "standard"),
        "activity": _current_concept_labels(props, "activity"),
        "keywords": _current_concept_labels(props, "keyword"),
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
            "cur_discipline": cur["discipline"],
            "cur_standard": cur["standard"],
            "cur_activity": cur["activity"],
            "cur_keywords": cur["keywords"],
            "cur_accessibleAt": cur["accessibleAt"],
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Concept resolution ──────────────────────────────────────────────────────────
# Concept payloads must be complete, already-existing concept records — never
# invented. Both helpers below return that exact shape (or omit the field).

def _match_subjects(
    subjects: list[str], vocab_df: pd.DataFrame | None, vocab_code: str,
) -> tuple[list[dict], list[str]]:
    """
    Match OpenAIRE subject strings against an *existing* Marketplace
    vocabulary by case-insensitive label (same rule the Keywords page's
    "Duplicates in other vocabs" tab uses). Only matched, already-existing
    concepts are returned — nothing new is created. Returns
    (matched_concepts, subjects_still_unmatched) so a caller can chain
    several vocabularies in priority order, passing leftovers along.

    `vocab_code` is a fallback only, used when `vocab_df` has no
    `vocabulary_code` column (the keyword_vocab DataFrame — always
    "sshoc-keyword"). Prefer the DataFrame's own per-row vocabulary_code
    when present (fetch_concepts_by_type() results) since the vocabulary
    code is not always the same as the property type code it was fetched by
    — e.g. the `activity` property type's vocabulary is `tadirah2`.
    """
    if vocab_df is None or vocab_df.empty:
        return [], list(subjects)
    by_label = {str(row["label"]).strip().lower(): row for _, row in vocab_df.iterrows()}
    matched, remaining, seen_codes = [], [], set()
    for s in subjects:
        row = by_label.get(str(s).strip().lower())
        if row is not None and row["code"] not in seen_codes:
            seen_codes.add(row["code"])
            row_vocab_code = row.get("vocabulary_code")
            matched.append({
                "code": row["code"],
                "label": row["label"],
                "uri": row["uri"],
                "vocabulary": {"code": row_vocab_code if pd.notna(row_vocab_code) else vocab_code},
            })
        else:
            remaining.append(s)
    return matched, remaining


# Closed vocabularies tried, in this order, before a leftover OpenAIRE
# "keyword"-scheme subject falls back to the open sshoc-keyword vocabulary —
# the "closed vocabs first, keyword as last resort" rule this page follows
# for every concept-valued property. (property type code, proposed-dict key).
#
# Scoped to vocabularies where a label match is a meaningfully reliable
# signal that the subject actually IS that property: standards and research
# activities are both subject/topic-adjacent, same as keywords themselves.
# Vocabularies that describe the *resource* rather than its subject matter
# (resource-category, intended-audience, geographical-availability, formats,
# life-cycle/readiness status, ...) are deliberately excluded — a topic a
# paper discusses is not reliable evidence of what audience/category/format
# the *item itself* has, so a coincidental label match there risks a wrong
# property rather than a merely redundant one.
_CLOSED_SUBJECT_VOCABS = [("standard", "standards"), ("activity", "activities")]


def _distribute_subjects(
    subjects: list[str],
    item: dict,
    closed_vocab_dfs: dict[str, pd.DataFrame],
    keyword_vocab_df: pd.DataFrame | None,
) -> dict[str, list[dict]]:
    """
    Route OpenAIRE "keyword"-scheme subjects to the best-fit Marketplace
    property: each closed vocabulary in _CLOSED_SUBJECT_VOCABS is tried in
    turn (skipped entirely if the item already has that property — this
    page only ever fills in what's missing), and whatever's left over only
    then falls back to the open sshoc-keyword vocabulary. Returns a dict
    keyed by proposed-field name ("standards", "activities", "keywords"),
    omitting any with no matches.
    """
    remaining = list(subjects)
    proposed: dict[str, list[dict]] = {}

    for type_code, field_key in _CLOSED_SUBJECT_VOCABS:
        if item.get(f"cur_{type_code}"):
            continue  # already has this property — not ours to add to
        matched, remaining = _match_subjects(remaining, closed_vocab_dfs.get(type_code), type_code)
        if matched:
            proposed[field_key] = matched

    if not item.get("cur_keywords"):
        matched, remaining = _match_subjects(remaining, keyword_vocab_df, "sshoc-keyword")
        if matched:
            proposed["keywords"] = matched

    return proposed


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


def _resolve_discipline(fos_codes: list[str], cache: dict) -> list[dict]:
    """
    Resolve OpenAIRE FOS classification codes to the Marketplace's closed
    `discipline` vocabulary by exact code — the same OECD/ÖFOS scheme (see
    lib.openaire._extract_fields(); verified live against the Marketplace
    API, e.g. code "601" -> "History, Archaeology"). Caches per code, like
    _resolve_language(). Codes with no match in the Marketplace's copy are
    simply dropped, not proposed.
    """
    resolved = []
    for code in fos_codes:
        if code not in cache:
            try:
                cache[code] = get_concept("discipline", code, API_URL, st.session_state["bearer"])
            except Exception:
                cache[code] = None
        if cache[code]:
            resolved.append(cache[code])
    return resolved


# ── Proposal building ───────────────────────────────────────────────────────────

def _build_proposal(
    item: dict,
    lookup: dict | None,
    closed_vocab_dfs: dict[str, pd.DataFrame],
    keyword_vocab_df: pd.DataFrame | None,
    lang_cache: dict,
    disc_cache: dict,
) -> dict:
    """
    Compare one extracted item's current values against its OpenAIRE lookup
    result. Returns {"status", "message", "proposed"} where `proposed` only
    has keys for fields that are missing on the MP side, present on the
    OpenAIRE side, and — for every concept-valued field — resolvable to an
    existing Marketplace concept. Closed vocabularies (language, discipline,
    standard, activity) are always tried before the open sshoc-keyword
    vocabulary, which only ever catches what's left over (see
    _distribute_subjects()). The OpenAIRE record itself (title, raw
    response, ...) is looked up separately for display — see
    _render_openaire_data().
    """
    if lookup is None:
        return {"status": "error", "message": "Not looked up", "proposed": {}}
    if lookup.get("status") != "found":
        return {
            "status": lookup.get("status", "error"),
            "message": lookup.get("message", ""),
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
    if not item["cur_discipline"] and f.get("fos_codes"):
        matched = _resolve_discipline(f["fos_codes"], disc_cache)
        if matched:
            proposed["disciplines"] = matched
    if f.get("keywords"):
        proposed.update(_distribute_subjects(f["keywords"], item, closed_vocab_dfs, keyword_vocab_df))
    if not item["cur_accessibleAt"] and f.get("urls"):
        proposed["accessibleAt"] = f["urls"]

    return {"status": "found", "message": "", "proposed": proposed}


_LIST_CONCEPT_FIELD_LABELS = {"keywords": "keyword", "disciplines": "discipline",
                               "standards": "standard", "activities": "activity"}


def _field_label(field: str, value) -> str:
    """Human-readable 'field: value' text for one proposed field — used for
    both the review checkboxes and the Session Log source description."""
    if field in _LIST_CONCEPT_FIELD_LABELS:
        prop_name = _LIST_CONCEPT_FIELD_LABELS[field]
        return f"{prop_name}: {', '.join(c['label'] for c in value)}"
    if field == "language":
        return f"language: {value['label']} ({value['code']})"
    if field == "accessibleAt":
        return "accessibleAt: " + ", ".join(value)
    return f"{field}: {value}"


def _field_help(field: str, value) -> str:
    """
    Per-checkbox tooltip explaining how this specific value was derived from
    OpenAIRE — the same rules as the "How OpenAIRE fields map to Marketplace
    fields" expander, but restated against this item's actual value so a
    curator doesn't have to cross-reference the general table while reviewing.
    """
    if field == "year":
        return f"From OpenAIRE's publicationDate — the first 4 digits ({value})."
    if field == "publisher":
        return f"Copied as-is from OpenAIRE's publisher field: '{value}'."
    if field == "language":
        return (
            f"OpenAIRE reported ISO 639-3 code '{value['code']}', resolved to the "
            f"existing Marketplace concept '{value['label']}' — only proposed "
            f"because that exact code already exists in the Marketplace's own "
            f"language vocabulary."
        )
    if field == "disciplines":
        labels = ", ".join(f"'{c['label']}' ({c['code']})" for c in value)
        return (
            f"OpenAIRE classified this item under FOS code(s) matching the Marketplace's "
            f"closed discipline vocabulary by exact code: {labels}. Codes with no match "
            f"in the Marketplace's copy are never proposed."
        )
    if field in ("standards", "activities"):
        vocab_name = "standard" if field == "standards" else "activity (tadirah2)"
        labels = ", ".join(f"'{c['label']}'" for c in value)
        return (
            f"Matched an existing {vocab_name} concept by case-insensitive label against "
            f"an OpenAIRE subject, ahead of the open keyword vocabulary — this page always "
            f"tries closed vocabularies first: {labels}."
        )
    if field == "keywords":
        labels = ", ".join(f"'{c['label']}'" for c in value)
        return (
            f"Matched an existing sshoc-keyword concept by case-insensitive label — the "
            f"last resort after checking the standard and activity vocabularies first — "
            f"against an OpenAIRE subject: {labels}. OpenAIRE subjects with no existing "
            f"match anywhere are never proposed; no new keyword concepts are created."
        )
    if field == "accessibleAt":
        return (
            "OpenAIRE's recorded access URL(s) for this DOI — proposed only "
            "because this item currently has no accessibleAt URL at all."
        )
    return ""


def _apply_proposal(
    category: str, persistent_id: str, fields_to_apply: dict, item_label: str = "",
) -> tuple[bool, str]:
    """
    GET the live item, append the given fields (only if still missing — the
    live item may have changed since the snapshot was taken), and PUT the
    complete object back. Reuses lib.api.get_item()/put_item(), the same
    GET-then-PUT round trip fix_item_keyword() uses; put_item() logs the write,
    tagged with a source describing exactly what was added and to which item —
    built from the fields actually applied, not just the ones requested, so a
    field skipped because it's no longer missing isn't claimed in the log.
    `fields_to_apply["language"]` and every list-valued concept field
    ("disciplines", "standards", "activities", "keywords") are already-
    resolved, complete concept record(s) — see _resolve_language(),
    _resolve_discipline(), _distribute_subjects()/_match_subjects().
    """
    try:
        item = get_item(category, persistent_id, API_URL, st.session_state["bearer"])
    except Exception as e:
        return False, f"GET failed: {e}"

    props = item.setdefault("properties", [])
    existing_codes = {(p.get("type") or {}).get("code") for p in props}
    applied: dict = {}

    if "year" in fields_to_apply and "year" not in existing_codes:
        props.append({"type": {"code": "year"}, "value": fields_to_apply["year"]})
        applied["year"] = fields_to_apply["year"]
    if "publisher" in fields_to_apply and "publisher" not in existing_codes:
        props.append({"type": {"code": "publisher"}, "value": fields_to_apply["publisher"]})
        applied["publisher"] = fields_to_apply["publisher"]
    if "language" in fields_to_apply and "language" not in existing_codes:
        props.append({"type": {"code": "language"}, "concept": fields_to_apply["language"]})
        applied["language"] = fields_to_apply["language"]
    for field, prop_code in (("disciplines", "discipline"), ("standards", "standard"), ("activities", "activity")):
        if field in fields_to_apply and prop_code not in existing_codes:
            for concept in fields_to_apply[field]:
                props.append({"type": {"code": prop_code}, "concept": concept})
            applied[field] = fields_to_apply[field]
    if "keywords" in fields_to_apply and "keyword" not in existing_codes:
        for concept in fields_to_apply["keywords"]:
            props.append({"type": {"code": "keyword"}, "concept": concept})
        applied["keywords"] = fields_to_apply["keywords"]
    if "accessibleAt" in fields_to_apply and not item.get("accessibleAt"):
        item["accessibleAt"] = fields_to_apply["accessibleAt"]
        applied["accessibleAt"] = fields_to_apply["accessibleAt"]

    if not applied:
        return False, "Nothing to apply — item already has these fields."

    target = f"'{item_label}'" if item_label else f"{category}/{persistent_id}"
    field_desc = ", ".join(_field_label(f, v) for f, v in applied.items())
    source = f"OpenAIRE Enrichment: added {field_desc} to {target}"

    return put_item(category, persistent_id, item, API_URL, st.session_state["bearer"], source=source)


def _render_openaire_data(lookup_entry: dict | None) -> None:
    """
    Show OpenAIRE's own data for this DOI — a readable summary plus the
    complete raw API response — so a curator can see everything OpenAIRE
    reported, not just the subset this page turned into proposed fields.
    This is where a subject that didn't map to any closed vocab or keyword
    (and so was silently dropped from the checkboxes above) is still visible.
    """
    if not lookup_entry or lookup_entry.get("status") != "found":
        return
    f = lookup_entry.get("fields") or {}
    raw = lookup_entry.get("raw")

    with st.expander("OpenAIRE response for this DOI"):
        lang = f"{f['language_label']} ({f['language_code']})" if f.get("language_code") else "—"
        st.markdown(f"**Title:** {f.get('title') or '—'}")
        st.markdown(f"**Publication date:** {f.get('publication_date') or '—'}")
        st.markdown(f"**Publisher:** {f.get('publisher') or '—'}")
        st.markdown(f"**Language:** {lang}")
        st.markdown("**Access URLs:** " + (", ".join(f.get("urls") or []) or "—"))

        subjects = (raw.get("subjects") or []) if raw else []
        fos = [s.get("subject", {}).get("value") for s in subjects if (s.get("subject") or {}).get("scheme") == "FOS"]
        kw = [s.get("subject", {}).get("value") for s in subjects if (s.get("subject") or {}).get("scheme") == "keyword"]
        other = [
            s.get("subject", {}) for s in subjects
            if (s.get("subject") or {}).get("scheme") not in ("FOS", "keyword")
        ]
        st.markdown("**FOS subjects** (→ discipline): " + (", ".join(fos) or "—"))
        st.markdown("**Keyword subjects** (→ standard/activity/keyword): " + (", ".join(kw) or "—"))
        if other:
            st.markdown(
                "**Other subjects** (scheme not used by this page): "
                + ", ".join(f"{s.get('value')} ({s.get('scheme')})" for s in other)
            )

        if raw:
            with st.expander("Raw JSON response"):
                st.json(raw)
        else:
            st.caption("Raw response unavailable — this result predates the raw-response cache field; re-run the lookup to fetch it.")


snap = load_snapshot()

if snap.empty:
    st.warning("No snapshot data found.")
    st.stop()

# ── Field mapping reference ─────────────────────────────────────────────────────
with st.expander("How OpenAIRE fields map to Marketplace fields"):
    st.markdown(
        "A field is only ever **proposed when the Marketplace item is missing it** — "
        "an existing curator-entered value is never shown as conflicting or overwritten. "
        "Hover the **?** on any checkbox in the review list below for the same explanation "
        "restated against that item's actual value."
    )
    st.markdown(
        "**Closed vocabularies first, keyword as a last resort.** `keyword` is the only "
        "Marketplace property backed by an *open* vocabulary — every other property below "
        "is a *closed*, controlled one. A value is only ever proposed as a generic keyword "
        "once it's been checked against every closed vocabulary that could be a better fit "
        "and found no home there."
    )
    st.markdown(
        "| OpenAIRE field | → | Marketplace field | How it's used |\n"
        "|---|---|---|---|\n"
        "| `publicationDate` | → | `year` | First 4 digits, copied as-is |\n"
        "| `publisher` | → | `publisher` | Copied as-is |\n"
        "| `language` (ISO 639-3 code) | → | `language` *(closed)* | Looked up in the Marketplace's own language vocabulary by exact code; proposed only if that code already exists there |\n"
        "| `subjects` with `scheme: FOS` (Field of Science, e.g. \"0601 history and archaeology\") | → | `discipline` *(closed)* | The leading code is looked up directly in the Marketplace's discipline vocabulary — the same OECD/ÖFOS classification scheme, e.g. code `601` → \"History, Archaeology\". A code with no match is dropped |\n"
        "| `subjects` with `scheme: keyword` (free-text topics) | → | `standard` *(closed)*, then `activity` *(closed)*, then `keyword` *(open, last resort)* | Each subject is matched by case-insensitive label against the **standard** vocabulary first, then **activity** (research methods/activities), and only subjects matching neither are checked against the open **sshoc-keyword** vocabulary. At every step, only an **already-existing** concept counts as a match — nothing new is ever created, and a subject matching nowhere is simply dropped |\n"
        "| `instances[].urls` | → | `accessibleAt` | Proposed only when the item currently has no access URL at all |\n"
    )
    st.caption(
        "Not every closed vocabulary is checked — `resource-category`, `intended-audience`, "
        "`geographical-availability`, formats, and life-cycle/readiness status all describe "
        "**the resource itself**, not its subject matter. A paper's topic coincidentally "
        "matching one of those labels isn't reliable evidence of what audience, category, or "
        "format the Marketplace item actually has, so those are left out of this matching."
    )
    st.caption(
        "License and author/contributor fields are deliberately out of scope: the "
        "Marketplace's license vocabulary is closed and OpenAIRE's license strings "
        "(e.g. \"CC BY\") don't map onto it cleanly, and adding authors risks creating "
        "duplicate Actor records."
    )

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
                with st.spinner("Loading sshoc-keyword vocabulary (last-resort keyword matching)…"):
                    st.session_state["keyword_vocab"] = fetch_all_keyword_concepts(
                        API_URL, st.session_state["bearer"]
                    )
            closed_vocabs = st.session_state.setdefault("openaire_closed_vocabs", {})
            for type_code, _ in _CLOSED_SUBJECT_VOCABS:
                if type_code not in closed_vocabs:
                    with st.spinner(f"Loading '{type_code}' vocabulary (checked before keyword)…"):
                        closed_vocabs[type_code] = fetch_concepts_by_type(
                            type_code, API_URL, st.session_state["bearer"]
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

            disc_cache = st.session_state.setdefault("openaire_discipline_concepts", {})
            fos_codes = {
                code
                for r in lookup.values()
                if r.get("status") == "found"
                for code in r["fields"].get("fos_codes", [])
            }
            new_fos_codes = fos_codes - disc_cache.keys()
            if new_fos_codes:
                with st.spinner(f"Resolving {len(new_fos_codes)} discipline concept(s)…"):
                    _resolve_discipline(list(new_fos_codes), disc_cache)

            st.session_state.pop("openaire_apply_status", None)
            st.rerun()

# ── Results ────────────────────────────────────────────────────────────────────
lookup: dict | None = st.session_state.get("openaire_lookup")

if doi_df is not None and not doi_df.empty and lookup is not None:
    st.divider()
    st.subheader("Results")

    apply_status: dict = st.session_state.setdefault("openaire_apply_status", {})
    vocab_df: pd.DataFrame | None = st.session_state.get("keyword_vocab")
    closed_vocab_dfs: dict = st.session_state.setdefault("openaire_closed_vocabs", {})
    lang_cache: dict = st.session_state.setdefault("openaire_language_concepts", {})
    disc_cache: dict = st.session_state.setdefault("openaire_discipline_concepts", {})

    records = []
    for _, item in doi_df.iterrows():
        pid = item["persistentId"]
        result = _build_proposal(
            item.to_dict(), lookup.get(item["doi"]), closed_vocab_dfs, vocab_df, lang_cache, disc_cache,
        )
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
    # Includes items already successfully applied (not just still-pending ones)
    # so a curator working down this list sees each item marked done in place,
    # in the same stable order, rather than having it disappear — it only
    # leaves this list by changing category/snapshot and re-extracting.
    review_rows = results_df[results_df["n_proposed"] > 0]
    if not review_rows.empty:
        st.divider()
        n_done = int((review_rows["applied_ok"] == True).sum())  # noqa: E712
        st.markdown(f"**{len(review_rows)} item(s) with at least one proposed field** — {n_done} applied so far.")
        for _, r in review_rows.iterrows():
            pid = r["persistentId"]
            applied = r["applied_ok"] is True
            failed = r["applied_ok"] is False
            if applied:
                title_suffix = " — ✅ applied"
            elif failed:
                title_suffix = " — ❌ last attempt failed"
            else:
                title_suffix = ""
            with st.expander(
                f"{r['label']} — {r['n_proposed']} field(s) proposed{title_suffix}",
                expanded=failed,
            ):
                st.caption(
                    f"{r['category']} / {pid}  ·  "
                    f"[MP entry]({MP_SERVER}{r['category']}/{pid})  ·  "
                    f"DOI: [{r['doi']}](https://doi.org/{r['doi']})  ·  "
                    f"[View on OpenAIRE](https://explore.openaire.eu/search/result?pid={r['doi']})"
                )
                _render_openaire_data(lookup.get(r["doi"]))

                if applied:
                    st.success(r["apply_message"])
                    for field, value in r["proposed"].items():
                        st.caption("✅ " + _field_label(field, value))
                    continue

                selected_fields = {}
                for field, value in r["proposed"].items():
                    if st.checkbox(
                        _field_label(field, value), value=True, key=f"oa_field_{pid}_{field}",
                        help=_field_help(field, value),
                    ):
                        selected_fields[field] = value

                if failed:
                    st.error(r["apply_message"])

                if st.button("Apply to this item", key=f"oa_apply_{pid}", disabled=not selected_fields):
                    ok, msg = _apply_proposal(r["category"], pid, selected_fields, item_label=r["label"])
                    apply_status[pid] = (ok, msg)
                    st.toast(f"{r['label']}: {msg}", icon="✅" if ok else "⚠️")
                    st.rerun()
