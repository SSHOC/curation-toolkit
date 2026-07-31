"""
Keyword Curation — inspect and clean the sshoc-keyword vocabulary.

Tabs
----
Unused
  Concepts in the vocabulary not referenced by any item in the current
  snapshot.  Batch deletion uses force=true because the API keeps full item
  version history; old revisions may still reference a concept even after it
  has been removed from the current version.

In use
  Concepts actively used on items.  Quality filters surface common issues:
  labels starting with a space, labels without letters, overly long labels.
  Near-duplicate detection groups concepts that are identical after
  normalisation (case-fold, hyphens/underscores → space) and offers a
  one-click merge: re-point all affected items then delete the variants.

Duplicates in other vocabs
  Keywords whose label already exists as a concept in another vocabulary
  (e.g. "xml" in sshoc-keyword vs the "standard" vocab).  A fix control
  re-points affected items to the canonical concept.  Only used keywords
  are shown; unused ones should be removed via the Unused tab.

All concepts
  Full vocabulary listing with usage counts and search filter.

Shared state (st.session_state keys)
  keyword_vocab       – DataFrame of all sshoc-keyword concepts (loaded on demand)
  all_concepts_cache  – DataFrame of concepts across all vocabularies
  kw_delete_status    – last batch-delete result {successes, failures}
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
from lib.auth import require_login, render_account_caption, is_moderator, current_role_label
from lib.mplib import get_util
from lib.api import fetch_all_keyword_concepts, fetch_all_concepts, delete_concept, fix_item_keyword
from lib.snapshot import render_data_status, require_snapshot

require_login()

st.set_page_config(page_title="Keywords — Curation Toolkit", page_icon="🏷️", layout="wide")

env = st.session_state["env"]
st.title("Keyword Curation")
render_account_caption(env)

require_snapshot()
render_data_status()


@st.cache_data(show_spinner="Extracting keywords from snapshot…")
def keywords_from_snapshot() -> pd.DataFrame:
    """One row per (persistentId, keyword_code) with label, category, item label."""
    util = get_util()
    all_props = util.getAllProperties(util._load_snapshot())
    kw = all_props[all_props["type.code"] == "keyword"].copy()
    rename = {
        "concept.code":    "keyword_code",
        "concept.label":   "keyword_label",
        "ts_persistentId": "persistentId",
        "ts_category":     "category",
        "ts_label":        "item_label",
    }
    rename = {k: v for k, v in rename.items() if k in kw.columns}
    return kw.rename(columns=rename)[list(rename.values())].reset_index(drop=True)


# ── Load data ─────────────────────────────────────────────────────────────────
snap_kw = keywords_from_snapshot()
vocab_df: pd.DataFrame | None = st.session_state.get("keyword_vocab")

col_load, _ = st.columns([1, 2])
with col_load:
    if st.button("Load keyword concepts from API", use_container_width=True, key="btn_load_vocab"):
        with st.spinner("Fetching all keyword concepts…"):
            try:
                df = fetch_all_keyword_concepts(env["api_url"], st.session_state["bearer"])
                st.session_state["keyword_vocab"] = df
                st.rerun()
            except Exception as e:
                st.error(f"Failed to fetch concepts: {e}")

if vocab_df is None:
    st.info(
        "Load keyword concepts from the API. "
        "The endpoint returns every concept in the `sshoc-keyword` vocabulary, "
        "including ones not used on any item."
    )
    st.stop()

# ── Compute sets ──────────────────────────────────────────────────────────────
used_codes = set(snap_kw["keyword_code"].dropna())

usage_counts = snap_kw.groupby("keyword_code").size().rename("items_using")
vocab_df = vocab_df.copy()
vocab_df["items_using"] = vocab_df["code"].map(usage_counts).fillna(0).astype(int)

unused = vocab_df[vocab_df["items_using"] == 0].sort_values("label").copy()
in_use = vocab_df[vocab_df["items_using"] >  0].sort_values("items_using", ascending=False).copy()

# ── Metrics ───────────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("Concepts in MP vocab", len(vocab_df))
c2.metric("Used in snapshot", len(in_use))
c3.metric("Unused concepts", len(unused))

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_unused, tab_used, tab_dupes, tab_all = st.tabs([
    f"Unused ({len(unused)})",
    f"In use ({len(in_use)})",
    "Duplicates in other vocabs",
    f"All concepts ({len(vocab_df)})",
])

# ─────────────────────────────────────────────────────────────────────────────
with tab_unused:
    st.markdown(
        "Concepts registered in the vocabulary but not used on any item in the snapshot. "
        "**Candidate** entries were submitted by users and may not have been formally reviewed."
    )
    st.info(
        "Deletion uses `force=true`. "
        "The API keeps a full version history of every item, and older revisions may still "
        "reference a keyword even after it has been removed from the current version. "
        "The API counts those historical references as 'in use' and refuses a plain delete. "
        "`force=true` removes the concept from the vocabulary and clears any lingering "
        "references across all item versions."
    )
    if unused.empty:
        st.success("Every vocabulary concept is used by at least one item.")
    else:
        f1, f2 = st.columns([2, 1])
        with f1:
            search = st.text_input("Filter by label or code", key="unused_search")
        with f2:
            only_candidates = st.checkbox("Candidates only", key="unused_candidates")

        view = unused.copy()
        if search.strip():
            view = view[
                view["label"].str.contains(search.strip(), case=False, na=False)
                | view["code"].str.contains(search.strip(), case=False, na=False)
            ]
        if only_candidates:
            view = view[view["candidate"] == True]  # noqa: E712

        st.caption(f"Showing {len(view)} of {len(unused)} unused concepts")

        display = view[["code", "label", "candidate", "uri", "definition"]].copy()
        display.insert(0, "delete", False)

        edited = st.data_editor(
            display.reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            column_config={
                "delete":    st.column_config.CheckboxColumn("Delete?", default=False),
                "candidate": st.column_config.CheckboxColumn("Candidate"),
                "uri":       st.column_config.LinkColumn("URI"),
            },
            disabled=["code", "label", "candidate", "uri", "definition"],
            key="unused_editor",
        )

        to_delete = edited[edited["delete"]]["code"].tolist()

        # ── Persisted delete result (survives st.rerun) ───────────────────────
        _del_status = st.session_state.get("kw_delete_status")
        if _del_status:
            if _del_status.get("successes"):
                labels = ", ".join(f"`{c}`" for c, _ in _del_status["successes"][:5])
                tail = f" and {len(_del_status['successes']) - 5} more" if len(_del_status["successes"]) > 5 else ""
                st.success(f"Deleted {len(_del_status['successes'])} concept(s): {labels}{tail}")
            for code, msg in _del_status.get("failures", []):
                if "403" in msg:
                    st.error(
                        f"`{code}` — 403 Forbidden: the server refused to delete this concept. "
                        "It may be a system/protected concept, or your account may lack "
                        "vocabulary administrator privileges."
                    )
                else:
                    st.error(f"`{code}` — {msg}")

        if to_delete:
            sel_rows = (
                view[view["code"].isin(to_delete)][["code", "label", "uri", "definition"]]
                .reset_index(drop=True)
            )
            st.dataframe(
                sel_rows.style.set_properties(
                    **{"background-color": "#ffe0e0", "color": "#8b0000"}
                ),
                use_container_width=True,
                hide_index=True,
                column_config={"uri": st.column_config.LinkColumn("URI")},
            )
            st.error(
                f"**{len(to_delete)} concept(s) selected for deletion (force=true).** "
                "This removes the concept from the vocabulary and strips it from any item "
                "that references it. This cannot be undone."
            )
            confirmed = st.checkbox(
                f"I understand that {len(to_delete)} concept(s) will be permanently deleted "
                "and any historical item-version references will be cleared",
                key="unused_confirm",
            )
            if not is_moderator():
                st.warning(
                    f"Deleting concepts requires moderator privileges — your account role "
                    f"is '{current_role_label()}'."
                )
            if st.button(
                f"Delete {len(to_delete)} concept(s)",
                type="primary",
                disabled=not confirmed or not is_moderator(),
                key="btn_delete_concepts",
            ):
                st.session_state.pop("kw_delete_status", None)
                successes, failures = [], []
                bar = st.progress(0, text=f"Deleting… 0 / {len(to_delete)}")
                for i, code in enumerate(to_delete, 1):
                    ok, msg = delete_concept(code)
                    (successes if ok else failures).append((code, msg))
                    bar.progress(i / len(to_delete), text=f"Deleting… {i} / {len(to_delete)}")
                bar.empty()

                st.session_state["kw_delete_status"] = {
                    "successes": successes,
                    "failures": failures,
                }
                deleted_codes = {c for c, _ in successes}
                st.session_state["keyword_vocab"] = st.session_state["keyword_vocab"][
                    ~st.session_state["keyword_vocab"]["code"].isin(deleted_codes)
                ].reset_index(drop=True)
                st.cache_data.clear()
                st.rerun()

        csv = view[["code", "label", "candidate", "uri", "definition"]].to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", csv, "unused_keywords.csv", "text/csv", key="dl_unused")

# ─────────────────────────────────────────────────────────────────────────────
with tab_used:
    st.markdown("Concepts actively used on items. Use the quality filters to find malformed entries.")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_leading_space = st.checkbox("Starts with space", key="f_space")
    with fc2:
        filter_no_letters = st.checkbox("No letters (numbers/symbols only)", key="f_noletters")
    with fc3:
        max_len_enabled = st.checkbox("Longer than … characters", key="f_maxlen_on")
        max_len = st.number_input(
            "Max length", min_value=1, value=50, step=5,
            key="f_maxlen", label_visibility="collapsed",
            disabled=not max_len_enabled,
        )

    view_u = in_use.copy()
    active_filters = []

    if filter_leading_space:
        view_u = view_u[view_u["label"].str.startswith(" ", na=False)]
        active_filters.append("starts with space")
    if filter_no_letters:
        view_u = view_u[~view_u["label"].str.contains(r"[a-zA-Z]", regex=True, na=False)]
        active_filters.append("no letters")
    if max_len_enabled:
        view_u = view_u[view_u["label"].str.len() > max_len]
        active_filters.append(f">{max_len} chars")

    if active_filters:
        st.caption(f"Filters active: {', '.join(active_filters)} — {len(view_u)} match")
    else:
        st.caption(f"{len(view_u)} concepts in use")

    st.dataframe(
        view_u[["code", "label", "items_using", "candidate", "uri"]].reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        column_config={
            "uri":         st.column_config.LinkColumn("URI"),
            "items_using": st.column_config.NumberColumn("Items using"),
            "candidate":   st.column_config.CheckboxColumn("Candidate"),
        },
    )

    # ── Near-duplicate detection ──────────────────────────────────────────────
    st.divider()
    st.markdown("#### Near-duplicate keywords")
    st.caption(
        "Groups of keywords that are identical after normalisation: "
        "case is ignored, hyphens/underscores/slashes are treated as spaces, "
        "and extra whitespace is collapsed."
    )

    import re as _re

    def _norm(label: str) -> str:
        s = str(label).lower().strip()
        s = _re.sub(r"[-_/\\]", " ", s)
        s = _re.sub(r"\s+", " ", s)
        return s

    nd = in_use.copy()
    nd["_norm"] = nd["label"].apply(_norm)
    near_groups = nd[nd.duplicated("_norm", keep=False)].groupby("_norm", sort=False)

    if near_groups.ngroups == 0:
        st.success("No near-duplicate keywords found.")
    else:
        st.metric("Near-duplicate groups", near_groups.ngroups)
        for gi, (norm_key, grp) in enumerate(near_groups):
            total_items = grp["items_using"].sum()
            with st.expander(
                f'"{norm_key}"  —  {len(grp)} variants  ·  {total_items} items total'
            ):
                st.dataframe(
                    grp[["code", "label", "items_using", "candidate", "uri"]].reset_index(drop=True),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "uri":         st.column_config.LinkColumn("URI"),
                        "items_using": st.column_config.NumberColumn("Items using"),
                        "candidate":   st.column_config.CheckboxColumn("Candidate"),
                    },
                )

                st.markdown("**Merge variants**")
                keep_col, _ = st.columns([2, 1])
                codes = grp["code"].tolist()
                with keep_col:
                    keep_code = st.selectbox(
                        "Keep this variant:",
                        options=codes,
                        format_func=lambda c: (
                            f"{grp.loc[grp['code'] == c, 'label'].values[0]}"
                            f"  ({grp.loc[grp['code'] == c, 'items_using'].values[0]} items)"
                        ),
                        key=f"nd_keep_{gi}",
                    )

                keep_row = grp[grp["code"] == keep_code].iloc[0]
                merge_codes = [c for c in codes if c != keep_code]

                keep_uri = keep_row["uri"] if pd.notna(keep_row.get("uri")) else ""
                keep_concept: dict = {
                    "code": keep_code,
                    "label": keep_row["label"],
                    "vocabulary": {"code": "sshoc-keyword"},
                }
                if keep_uri:
                    keep_concept["uri"] = keep_uri

                affected = (
                    snap_kw[snap_kw["keyword_code"].isin(merge_codes)]
                    [["persistentId", "category", "item_label", "keyword_code", "keyword_label"]]
                    .drop_duplicates(["persistentId", "keyword_code"])
                    .reset_index(drop=True)
                )

                if affected.empty:
                    st.info(
                        "No items reference the non-kept variants. "
                        "Delete them from the **Unused** tab."
                    )
                else:
                    n_unique = affected["persistentId"].nunique()
                    st.warning(
                        f"**{n_unique} item(s)** use the non-kept variant(s) and will be "
                        f"re-pointed to **{keep_row['label']}**. "
                        f"The {len(merge_codes)} other variant(s) will then be deleted."
                    )
                    with st.expander("Show affected items"):
                        st.dataframe(
                            affected[["item_label", "persistentId", "category", "keyword_label"]],
                            use_container_width=True,
                            hide_index=True,
                        )

                    confirmed = st.checkbox(
                        f"I understand: re-point {n_unique} item(s) and delete "
                        f"{len(merge_codes)} variant(s)",
                        key=f"nd_confirm_{gi}",
                    )
                    if not is_moderator():
                        st.warning(
                            f"Merging keywords requires moderator privileges (it deletes the "
                            f"absorbed concept) — your account role is '{current_role_label()}'."
                        )
                    if st.button(
                        f"Merge → keep '{keep_row['label']}'",
                        type="primary",
                        disabled=not confirmed or not is_moderator(),
                        key=f"nd_merge_{gi}",
                    ):
                        errors, successes = [], 0
                        bar = st.progress(0, text=f"Re-pointing… 0 / {len(affected)}")
                        for i, (_, row) in enumerate(affected.iterrows(), 1):
                            ok, msg = fix_item_keyword(
                                row["category"],
                                row["persistentId"],
                                row["keyword_code"],
                                "keyword",
                                keep_concept,
                                env["api_url"],
                                st.session_state["bearer"],
                            )
                            if ok:
                                successes += 1
                            else:
                                errors.append(f"`{row['persistentId']}`: {msg}")
                            bar.progress(i / len(affected), text=f"Re-pointing… {i} / {len(affected)}")
                        bar.empty()

                        del_errors = []
                        for code in merge_codes:
                            ok, msg = delete_concept(code)
                            if not ok:
                                del_errors.append(f"`{code}`: {msg}")

                        if successes:
                            st.success(f"Re-pointed {successes} item-keyword reference(s).")
                        if errors:
                            st.warning("Some re-pointing failed:\n" + "\n".join(errors))
                        if del_errors:
                            st.warning("Some deletions failed:\n" + "\n".join(del_errors))
                        else:
                            st.success(f"Deleted {len(merge_codes)} variant concept(s).")

                        st.session_state["keyword_vocab"] = st.session_state["keyword_vocab"][
                            ~st.session_state["keyword_vocab"]["code"].isin(merge_codes)
                        ].reset_index(drop=True)
                        st.cache_data.clear()
                        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
with tab_dupes:
    st.markdown(
        "Find keywords whose label already exists as a concept in another vocabulary — "
        "e.g. `xml` was added as a sshoc-keyword but already exists in the `standard` vocabulary. "
        "Use the fix controls below the table to re-point affected items to the correct concept."
    )

    all_concepts_df: pd.DataFrame | None = st.session_state.get("all_concepts_cache")

    c_load, _ = st.columns([1, 2])
    with c_load:
        if st.button("Load all concepts from API (~15 000)", use_container_width=True,
                     key="btn_load_all_concepts"):
            with st.spinner("Fetching all concepts across all vocabularies…"):
                try:
                    df_all = fetch_all_concepts(env["api_url"], st.session_state["bearer"])
                    st.session_state["all_concepts_cache"] = df_all
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")

    if all_concepts_df is None:
        st.info("Load all concepts above to run the cross-vocabulary comparison.")
    else:
        # Other-vocab concepts: everything that is NOT sshoc-keyword
        other = all_concepts_df[
            all_concepts_df["vocabulary_code"].fillna("") != "sshoc-keyword"
        ].copy()
        other["_norm"] = other["label"].str.strip().str.lower()

        # sshoc-keyword concepts with normalised label
        kw = vocab_df.copy()
        kw["_norm"] = kw["label"].str.strip().str.lower()

        # Inner join on normalised label
        matches = kw.merge(
            other[["_norm", "label", "code", "vocabulary_code", "type_code", "uri"]]
            .drop_duplicates("_norm"),
            on="_norm",
            suffixes=("_kw", "_other"),
        ).drop(columns=["_norm"])

        # Only show keywords that are actually used — unused ones cannot be fixed here
        matches = matches[matches["items_using"] > 0]
        matches = matches.sort_values(["items_using", "label_kw"], ascending=[False, True])

        st.metric("Used keywords also found in another vocabulary", len(matches))

        if matches.empty:
            st.success("No keyword labels overlap with concepts in other vocabularies.")
        else:
            # Optional filter by concept type
            type_options = sorted(matches["type_code"].dropna().unique().tolist())
            selected_types = st.multiselect(
                "Filter by other-vocab concept type", type_options, default=type_options,
                key="dupe_type_filter",
            )
            view_d = matches[matches["type_code"].isin(selected_types)]

            st.dataframe(
                view_d[[
                    "label_kw", "code_kw", "items_using",
                    "vocabulary_code", "type_code", "label_other", "code_other", "uri_other",
                ]].rename(columns={
                    "label_kw":       "keyword label",
                    "code_kw":        "keyword code",
                    "items_using":    "items using",
                    "vocabulary_code":"other vocab",
                    "type_code":      "other type",
                    "label_other":    "other label",
                    "code_other":     "other code",
                    "uri_other":      "other URI",
                }).reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "other URI": st.column_config.LinkColumn("other URI"),
                    "items using": st.column_config.NumberColumn("items using"),
                },
            )

            csv = view_d.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "cross_vocab_duplicates.csv",
                               "text/csv", key="dl_dupes")

            # ── Fix controls ─────────────────────────────────────────────────
            st.divider()
            st.markdown("#### Fix an entry")

            # Build selectbox options from the filtered matches table
            pair_options = {
                f"{row['label_kw']}  →  {row['label_other']} [{row['vocabulary_code']} / {row['type_code']}]": row
                for _, row in view_d.iterrows()
            }
            chosen = st.selectbox(
                "Select keyword → target concept pair:",
                list(pair_options.keys()),
                key="fix_pair_select",
            )
            sel = pair_options[chosen]

            kw_code   = sel["code_kw"]
            new_type  = sel["type_code"]
            new_concept = {
                "code":       sel["code_other"],
                "label":      sel["label_other"],
                "uri":        sel["uri_other"],
                "vocabulary": {"code": sel["vocabulary_code"]},
            }

            affected = (
                snap_kw[snap_kw["keyword_code"] == kw_code]
                [["persistentId", "category", "item_label"]]
                .drop_duplicates("persistentId")
                .reset_index(drop=True)
            )
            n_items = len(affected)

            if n_items > 1:
                st.warning(
                    f"This will affect **{n_items} items**. "
                    "Review the list below before proceeding."
                )
            else:
                st.info(f"1 item affected.")

            st.caption(f"Property type change: `keyword` → `{new_type}`")
            st.dataframe(
                affected,
                use_container_width=True,
                hide_index=True,
                column_config={"persistentId": "Persistent ID", "item_label": "Item label"},
            )

            if st.button(f"Fix {n_items} item(s)", type="primary", key="btn_fix_pair"):
                successes, failures = [], []
                bar = st.progress(0, text=f"Fixing… 0 / {n_items}")
                for i, (_, row) in enumerate(affected.iterrows(), 1):
                    ok, msg = fix_item_keyword(
                        row["category"], row["persistentId"],
                        kw_code, new_type, new_concept,
                        env["api_url"], st.session_state["bearer"],
                    )
                    (successes if ok else failures).append((row["persistentId"], msg))
                    bar.progress(i / n_items, text=f"Fixing… {i} / {n_items}")
                bar.empty()

                for pid, msg in successes:
                    st.success(f"`{pid}` — {msg}")
                for pid, msg in failures:
                    st.error(f"`{pid}` — {msg}")


# ─────────────────────────────────────────────────────────────────────────────
with tab_all:
    st.markdown("Complete concept list with usage counts.")
    search_all = st.text_input("Filter by label or code", key="all_search")
    view_all = vocab_df.copy()
    if search_all.strip():
        view_all = view_all[
            view_all["label"].str.contains(search_all.strip(), case=False, na=False)
            | view_all["code"].str.contains(search_all.strip(), case=False, na=False)
        ]
    st.dataframe(
        view_all[["code", "label", "items_using", "candidate", "uri", "definition"]].reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        column_config={
            "uri":         st.column_config.LinkColumn("URI"),
            "items_using": st.column_config.NumberColumn("Items using"),
            "candidate":   st.column_config.CheckboxColumn("Candidate"),
        },
    )
