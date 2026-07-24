"""
Actors — Browse, Duplicates, Orphaned, and Manual Merge tabs.

Browse
  Shows every actor × item appearance from the snapshot (one row per
  actor-role-item combination).  Filterable by category, role, and name.
  If actor data has been loaded from the live API, email filtering is also available.

Duplicates
  Scans the snapshot for actors with identical name (and optionally website).
  Matching can be case-insensitive (default), so "John Smith" and "john smith"
  are grouped together. Groups are classified as high-confidence when actors in
  the group also share the same email address. Supports one-click merge with
  attribute consolidation (email, website, externalIds are unioned across all
  actors being merged).

Orphaned
  Identifies actors that exist in the API but are credited on no item.
  Uses a two-phase approach: snapshot cross-reference to find candidates,
  then live API verification to confirm before offering deletion.

Manual Merge
  Lets a curator merge two or more actors directly by ID, for cases the
  automatic Duplicates scan doesn't catch. Includes a name search (case-
  insensitive, supports `*`/`?` wildcards) to look up IDs before merging.

Shared state (st.session_state keys)
  actor_details       – DataFrame of all actors loaded from the live API
  actor_dup_summary   – last duplicate-search summary result
  actor_dup_full      – last duplicate-search full result
  merged_groups       – dict of already-merged group keys → success message
  expander_open       – dict tracking which expanders should stay open after merge
  orphan_verified     – dict of actor_id → has_items (True/False/None) from live check
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import re
import fnmatch
from collections import Counter
import streamlit as st
import pandas as pd
from lib.auth import require_login
from lib.mplib import get_util
from lib.api import merge_actors, delete_actor, fetch_all_actors, verify_orphans, get_actor
from lib.snapshot import render_data_status, require_snapshot

require_login()

st.set_page_config(page_title="Actors — Curation Toolkit", page_icon="👤", layout="wide")

env = st.session_state["env"]
st.title("Actors")
st.caption(f"Environment: **{env['label']}** — {env['api_url']}")

MP_SERVER = env["mp_url"]

require_snapshot()
render_data_status()


@st.cache_data(show_spinner="Loading actors from snapshot…")
def load_appearances() -> pd.DataFrame:
    """One row per actor × item appearance."""
    util = get_util()
    df = util.getContributors()
    if df.empty:
        return df
    df["MPUrl"] = MP_SERVER + df["category"] + "/" + df["persistentId"]
    return df


@st.cache_data(show_spinner="Loading actors from snapshot…")
def load_unique_actors() -> pd.DataFrame:
    """One row per unique actor (deduplicated by id)."""
    util = get_util()
    c = util.getContributors()
    if c.empty:
        return pd.DataFrame()
    cols_wanted = ["actor.id", "actor.name"]
    for col in ["actor.website", "actor.externalIds", "actor.affiliations"]:
        if col in c.columns:
            cols_wanted.append(col)
    actors = (
        c[cols_wanted]
        .drop_duplicates(subset=["actor.id"])
        .rename(columns={
            "actor.id":           "id",
            "actor.name":         "name",
            "actor.website":      "website",
            "actor.externalIds":  "externalIds",
            "actor.affiliations": "affiliations",
        })
    )
    return util._getMPUrl(actors)


def _group_confidence(actor_ids: list[int], actor_details: pd.DataFrame | None) -> str:
    """
    Classify a duplicate group as 'high' or 'low' confidence.

    'High' means at least two actors in the group share the same non-empty
    email address — strong evidence they are the same person.
    'Low' means only the name matched; manual review is recommended.

    Returns 'low' whenever actor_details is not loaded (email not available).
    """
    if actor_details is None or actor_details.empty:
        return "low"
    emails = (
        actor_details.loc[actor_details["id"].isin(actor_ids), "email"]
        .dropna().astype(str).str.strip()
        .loc[lambda s: s != ""]
    )
    counts = Counter(emails.tolist())
    return "high" if any(v >= 2 for v in counts.values()) else "low"


# ── Shared: actor data loaded from the live API ───────────────────────────────
actor_details: pd.DataFrame | None = st.session_state.get("actor_details")

with st.container(border=True):
    col_info, col_btn = st.columns([3, 1])
    with col_info:
        if actor_details is not None:
            n_email = int(actor_details["email"].notna().sum())
            st.success(
                f"{len(actor_details)} actors loaded from API  ·  "
                f"{n_email} have an email address"
            )
        else:
            st.info(
                "Load actor data from the API to enable email filtering, "
                "duplicate confidence scoring, and orphan detection."
            )
    with col_btn:
        btn_label = "Refresh actors" if actor_details is not None else "Load actors from API"
        if st.button(
            btn_label,
            use_container_width=True,
            type="secondary" if actor_details is not None else "primary",
            key="load_actors_top",
        ):
            with st.spinner("Fetching actors from API…"):
                try:
                    st.session_state["actor_details"] = fetch_all_actors(
                        env["api_url"], st.session_state["bearer"]
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to fetch actors: {e}")

st.divider()

tab_browse, tab_dupes, tab_orphans, tab_manual = st.tabs(
    ["Browse", "Duplicates", "Orphaned", "Manual Merge"]
)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Browse
# ─────────────────────────────────────────────────────────────────────────────
with tab_browse:
    appearances = load_appearances()

    if appearances.empty:
        st.warning("No actor data found in snapshot.")
    else:
        disp = appearances.copy()
        if actor_details is not None and not actor_details.empty:
            disp = disp.merge(
                actor_details[["id", "email"]].rename(columns={"id": "actor.id"}),
                on="actor.id",
                how="left",
            )

        col_filter, col_main = st.columns([1, 3])

        with col_filter:
            categories = sorted(disp["category"].dropna().unique().tolist())
            selected_cats = st.multiselect("Category", categories, default=categories, key="br_cat")

            roles = sorted(disp["role.label"].dropna().unique().tolist())
            selected_roles = st.multiselect("Role", roles, default=roles, key="br_role")

            name_query = st.text_input("Actor name contains", "", key="br_name")

            email_query = ""
            if actor_details is not None:
                email_query = st.text_input("Email contains", "", key="br_email")
            else:
                st.caption("Load actor data above to enable email filter.")

        with col_main:
            mask = (
                disp["category"].isin(selected_cats)
                & disp["role.label"].isin(selected_roles)
            )
            if name_query.strip():
                mask &= disp["actor.name"].str.contains(name_query.strip(), case=False, na=False)
            if email_query.strip() and "email" in disp.columns:
                mask &= disp["email"].fillna("").str.contains(email_query.strip(), case=False)

            filtered = disp[mask].reset_index(drop=True)

            m1, m2, m3 = st.columns(3)
            m1.metric("Rows shown", len(filtered))
            m2.metric("Unique actors", filtered["actor.name"].nunique())
            m3.metric("Unique items", filtered["persistentId"].nunique())

            DISPLAY_COLS = [
                "actor.name", "email", "role.label",
                "label", "category", "persistentId", "actor.website", "MPUrl",
            ]
            show_cols = [c for c in DISPLAY_COLS if c in filtered.columns]
            st.dataframe(
                filtered[show_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "actor.name":    st.column_config.TextColumn("Actor"),
                    "role.label":    st.column_config.TextColumn("Role"),
                    "email":         st.column_config.TextColumn("Email"),
                    "actor.website": st.column_config.LinkColumn("Website"),
                    "MPUrl":         st.column_config.LinkColumn("Item link", display_text="Open"),
                },
            )
            csv = filtered[show_cols].to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "actors.csv", "text/csv", key="dl_browse")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Duplicates
# ─────────────────────────────────────────────────────────────────────────────
with tab_dupes:
    actors = load_unique_actors()
    actor_details = st.session_state.get("actor_details")

    if actors.empty:
        st.warning("No actor data found in snapshot.")
    else:
        col_left, col_mid, _ = st.columns([1, 1, 1])
        with col_left:
            selected_props = st.multiselect(
                "Match on",
                ["name", "website"],
                default=["name"],
                key="dup_props",
            )
        with col_mid:
            case_insensitive = st.checkbox(
                "Case-insensitive matching",
                value=True,
                key="dup_case_insensitive",
                help="Treat 'John Smith' and 'john smith' as the same value. "
                     "Leading/trailing whitespace is also ignored.",
            )
        run_dupes = st.button("Find Duplicates", key="btn_find_dupes")

        if run_dupes:
            if not selected_props:
                st.warning("Select at least one property to match on.")
            else:
                try:
                    match_actors = actors
                    if case_insensitive:
                        match_actors = actors.copy()
                        for prop in selected_props:
                            if prop in match_actors.columns:
                                match_actors[prop] = match_actors[prop].map(
                                    lambda v: v.strip().lower() if isinstance(v, str) else v
                                )
                    result_full, result_summary = get_util().getDuplicatedActorsWithItems(
                        match_actors, ",".join(selected_props)
                    )
                    st.session_state["actor_dup_summary"] = result_summary
                    st.session_state["actor_dup_full"] = result_full
                    st.session_state["actor_dup_id_to_name"] = dict(zip(actors["id"], actors["name"]))
                except Exception as e:
                    st.error(f"Error running duplicate check: {e}")

        result_summary = st.session_state.get("actor_dup_summary")
        result_full    = st.session_state.get("actor_dup_full")

        if result_summary is None:
            st.info("Click **Find Duplicates** to scan the snapshot.")
        elif result_summary.empty:
            st.success("No duplicate actors found.")
        else:
            n_groups = result_summary["name"].nunique()
            id_to_name = st.session_state.get("actor_dup_id_to_name", {})

            groups = []
            for name, group in result_summary.groupby("name"):
                actor_ids = group["id"].tolist()
                conf = _group_confidence(actor_ids, actor_details)
                orig_names = list(dict.fromkeys(id_to_name.get(a, "") for a in actor_ids))
                display_name = " / ".join(n for n in orig_names if n) or name
                groups.append((name, display_name, group, conf))

            high_conf = [(n, d, g, c) for n, d, g, c in groups if c == "high"]
            low_conf  = [(n, d, g, c) for n, d, g, c in groups if c == "low"]

            m1, m2, m3 = st.columns(3)
            m1.metric("Duplicate groups", n_groups)
            m2.metric("High confidence", len(high_conf),
                      help="Name + email match — very likely the same person.")
            m3.metric("Low confidence", len(low_conf),
                      help="Name match only — review before merging.")

            merged_groups = st.session_state.setdefault("merged_groups", {})
            expander_open = st.session_state.setdefault("expander_open", {})

            def _render_group(i, name, display_name, group, conf, section_prefix):
                actor_ids = group["id"].tolist()
                key = f"{section_prefix}{i}_{re.sub(r'[^a-zA-Z0-9]', '_', name)[:30]}"
                badge  = "★ High confidence" if conf == "high" else "Low confidence"
                header = f"{display_name} — {len(group)} actors  ·  {badge}"

                with st.expander(header, expanded=expander_open.get(key, conf == "high")):
                    rows = []
                    for _, actor_row in group.iterrows():
                        actor_id = int(actor_row["id"])
                        item_url, item_label = "", ""
                        if result_full is not None:
                            contrib = result_full[result_full["id"] == actor_id]
                            if not contrib.empty:
                                first = contrib.iloc[0]
                                item_url   = MP_SERVER + first.get("category", "") + "/" + first.get("persistentId", "")
                                item_label = first.get("label", "")
                        row = {
                            "id":           actor_id,
                            "items":        len(actor_row["itemPersistentId"]),
                            "example item": item_label,
                            "item link":    item_url,
                        }
                        if actor_details is not None:
                            match = actor_details.loc[actor_details["id"] == actor_id, "email"]
                            row["email"] = match.iloc[0] if not match.empty else ""
                        rows.append(row)

                    st.dataframe(
                        pd.DataFrame(rows),
                        use_container_width=True,
                        column_config={"item link": st.column_config.LinkColumn("Item link")},
                        hide_index=True,
                    )

                    if conf == "high":
                        st.info(
                            "These actors share the same name **and** email address — "
                            "they are almost certainly the same person."
                        )

                    st.divider()

                    options = {
                        f"ID {row['id']}  ({len(row['itemPersistentId'])} item(s))": int(row["id"])
                        for _, row in group.iterrows()
                    }
                    keep_label = st.selectbox(
                        "Keep this actor (merge others into it):",
                        list(options.keys()),
                        key=f"sel_{key}",
                    )
                    keep_id   = options[keep_label]
                    merge_ids = [a for a in actor_ids if a != keep_id]

                    already_merged = key in merged_groups
                    if already_merged:
                        st.success(merged_groups[key])

                    if st.button(
                        f"Merge {len(merge_ids)} actor(s) into ID {keep_id}",
                        key=f"btn_{key}",
                        type="primary",
                        disabled=already_merged,
                    ):
                        ok, msg = merge_actors(keep_id, merge_ids)
                        if ok:
                            merged_groups[key] = msg
                            expander_open[key] = True
                            load_unique_actors.clear()
                            st.rerun()
                        else:
                            st.error(msg)

            if high_conf:
                st.markdown("### Suggested merges")
                st.caption(
                    "These groups match on both **name and email address** — "
                    "merging them is strongly recommended."
                )
                for i, (name, display_name, group, conf) in enumerate(high_conf):
                    _render_group(i, name, display_name, group, conf, "hc_")

            if low_conf:
                if high_conf:
                    st.markdown("### Other name matches")
                    st.caption("Name match only — review before merging.")
                for i, (name, display_name, group, conf) in enumerate(low_conf):
                    _render_group(i, name, display_name, group, conf, "lc_")

            st.divider()
            with st.expander("Full detail table"):
                if result_full is not None:
                    full_cols = [
                        c for c in ["id", "name", "website", "role.label", "label", "category", "persistentId"]
                        if c in result_full.columns
                    ]
                    st.dataframe(result_full[full_cols], use_container_width=True, hide_index=True)

            csv = result_summary.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "actor_duplicates.csv", "text/csv", key="dl_dupes")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — Manual Merge
#
# NOTE: this block runs (in script order) before the Orphaned tab below,
# even though it appears after it visually. The Orphaned tab calls
# st.stop() when actor data isn't loaded yet, and st.stop() halts the
# entire script — not just its own `with` block — which would otherwise
# prevent this tab from ever rendering.
# ─────────────────────────────────────────────────────────────────────────────
with tab_manual:
    manual_actor_details: pd.DataFrame | None = st.session_state.get("actor_details")

    st.markdown(
        "Merge two actors directly by ID — useful when you already know which "
        "records are duplicates, or after finding candidates with the search "
        "below. This uses the same logic as the Duplicates tab: email, website, "
        "and external IDs are consolidated onto the kept actor before the merge."
    )

    # ── Optional helper: find actor IDs by name ────────────────────────────────
    with st.expander("Look up actor IDs by name"):
        st.caption(
            "Case-insensitive. Use `*` / `?` as wildcards (e.g. `kurzmeier*` or "
            "`*university*`); without wildcards it's a substring match."
        )
        search_source = (
            manual_actor_details
            if manual_actor_details is not None and not manual_actor_details.empty
            else load_unique_actors()
        )
        search_query = st.text_input("Actor name search", "", key="manual_search_query")
        if search_query.strip():
            pattern = search_query.strip()

            def _name_matches(name) -> bool:
                name = "" if pd.isna(name) else str(name)
                if "*" in pattern or "?" in pattern:
                    return fnmatch.fnmatch(name.lower(), pattern.lower())
                return pattern.lower() in name.lower()

            name_matches = search_source[search_source["name"].apply(_name_matches)]
            st.caption(f"{len(name_matches)} actor(s) matched.")
            show_cols = [c for c in ["id", "name", "email", "website"] if c in name_matches.columns]
            st.dataframe(
                name_matches[show_cols].fillna(""),
                use_container_width=True,
                hide_index=True,
                column_config={"website": st.column_config.LinkColumn("Website")},
            )

    st.divider()

    # ── Merge by ID ─────────────────────────────────────────────────────────────
    st.markdown("#### Merge by ID")

    col1, col2 = st.columns(2)
    with col1:
        manual_keep_id = st.number_input(
            "Keep this actor (ID)",
            min_value=1, step=1, value=None,
            key="manual_keep_id", placeholder="e.g. 1234",
        )
    with col2:
        manual_merge_id = st.number_input(
            "Merge this actor into it (ID)",
            min_value=1, step=1, value=None,
            key="manual_merge_id", placeholder="e.g. 5678",
        )

    if manual_keep_id is None or manual_merge_id is None:
        st.caption("Enter both IDs above to resolve the actors and continue.")
    elif int(manual_keep_id) == int(manual_merge_id):
        st.warning("Keep ID and merge ID must be different.")
    else:
        keep_id, merge_id = int(manual_keep_id), int(manual_merge_id)

        manual_merged = st.session_state.setdefault("manual_merged", {})
        merge_key = f"{keep_id}__{merge_id}"
        already_merged = merge_key in manual_merged

        if already_merged:
            # The merge actor no longer exists on the API — don't try to re-fetch it.
            st.success(manual_merged[merge_key])
            st.caption("Change one of the IDs above to start a new merge.")
        else:
            preview_rows = []
            fetch_error = None
            with st.spinner("Resolving actor names…"):
                for role, aid in [("Keep", keep_id), ("Merge", merge_id)]:
                    try:
                        a = get_actor(aid, env["api_url"], st.session_state["bearer"])
                        preview_rows.append({
                            "role":    role,
                            "id":      aid,
                            "name":    a.get("name", ""),
                            "email":   a.get("email", ""),
                            "website": a.get("website", ""),
                        })
                    except Exception as e:
                        fetch_error = f"Could not fetch actor ID {aid}: {e}"
                        break

            if fetch_error:
                st.error(fetch_error)
            else:
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

                confirmed_cb = st.checkbox(
                    f"I understand that actor ID {merge_id} above will be permanently "
                    f"merged into ID {keep_id}. This cannot be undone.",
                    key=f"manual_confirm_{merge_key}",
                )

                if st.button(
                    f"Merge ID {merge_id} into ID {keep_id}",
                    type="primary",
                    key=f"manual_btn_{merge_key}",
                    disabled=not confirmed_cb,
                ):
                    ok, msg = merge_actors(keep_id, [merge_id])
                    if ok:
                        manual_merged[merge_key] = msg
                        load_unique_actors.clear()
                        st.rerun()
                    else:
                        st.error(msg)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Orphaned Actors
# ─────────────────────────────────────────────────────────────────────────────
with tab_orphans:
    orphan_actor_details: pd.DataFrame | None = st.session_state.get("actor_details")

    st.markdown(
        "Actors in the Marketplace may exist without being credited on any item — "
        "for example, leftover entries from past imports or merges. "
        "This tool finds and removes them."
    )

    if orphan_actor_details is None:
        st.info("Load actor data from the API (button at the top of the page) to continue.")
        st.stop()

    # ── Step 1: snapshot cross-reference → candidates ────────────────────────
    st.markdown("#### Step 1 — Cross-reference with snapshot")
    st.caption(
        "The local snapshot lists every actor credited on at least one item. "
        "Actors absent from the snapshot are *candidates* — they may be orphaned, "
        "or the snapshot may simply be stale. The next step confirms which is which."
    )

    snapshot_actor_ids = set(load_unique_actors()["id"].dropna().astype(int))
    candidates = (
        orphan_actor_details[~orphan_actor_details["id"].isin(snapshot_actor_ids)]
        .copy()
        .reset_index(drop=True)
    )

    col_m1, col_m2, _ = st.columns(3)
    col_m1.metric("Total actors (API)", len(orphan_actor_details))
    col_m2.metric("Not in snapshot", len(candidates),
                  help="Absent from the current local snapshot — not yet confirmed orphans.")

    if candidates.empty:
        st.success("Every actor in the API appears in the snapshot — no candidates to investigate.")
        st.stop()

    # ── Step 2: live API verification ────────────────────────────────────────
    st.divider()
    st.markdown("#### Step 2 — Verify candidates via the live API")
    st.caption(
        f"Calls `GET /api/actors/{{id}}?items=true` for each of the {len(candidates)} candidates "
        "to confirm whether the API itself considers them item-free."
    )

    verified: dict | None = st.session_state.get("orphan_verified")

    v_col, b_col, _ = st.columns([2, 1, 1])
    with b_col:
        batch_size = st.number_input(
            "Batch size", min_value=10, max_value=200, value=50, step=10,
            key="orphan_batch_size",
            help="Requests per batch. Each batch runs concurrently; a short pause separates batches.",
        )
    with v_col:
        if st.button(
            f"Verify {len(candidates)} candidates via API",
            use_container_width=True,
            key="btn_verify_orphans",
        ):
            verified = verify_orphans(
                candidates["id"].astype(int).tolist(),
                env["api_url"],
                st.session_state["bearer"],
                batch_size=int(batch_size),
            )
            st.session_state["orphan_verified"] = verified
            st.rerun()

    if verified is None:
        st.info("Run the verification above to see confirmed orphans.")
        st.stop()

    candidates["api_has_items"] = candidates["id"].map(lambda i: verified.get(int(i)))
    confirmed_orphans = candidates[candidates["api_has_items"] == False].copy().reset_index(drop=True)  # noqa: E712
    uncertain = candidates[candidates["api_has_items"].isna()]

    col_m3, _, _ = st.columns(3)
    col_m3.metric("Confirmed orphans (API)", len(confirmed_orphans),
                  help="API returned items=[] for these actors.")

    if not uncertain.empty:
        with st.expander(f"{len(uncertain)} actor(s) could not be verified — API errors or timeouts"):
            st.caption("Excluded from the delete list. Re-run verification to retry.")
            st.dataframe(uncertain[["id", "name", "email", "website"]].fillna(""),
                         use_container_width=True, hide_index=True)

    if confirmed_orphans.empty:
        st.success("All candidates have items on the live API — nothing to delete.")
        st.stop()

    # ── Step 3: review and delete ─────────────────────────────────────────────
    st.divider()
    st.markdown("#### Step 3 — Review and delete")
    st.markdown(
        "Check the actors you want to remove. "
        "Actors affiliated with other actors will be **refused by the API** "
        "and reported below — they are never silently skipped."
    )

    display = confirmed_orphans[["id", "name", "email", "website"]].fillna("").copy()
    display.insert(0, "delete", False)

    edited = st.data_editor(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "delete":  st.column_config.CheckboxColumn("Delete?", default=False),
            "id":      st.column_config.NumberColumn("ID"),
            "website": st.column_config.LinkColumn("Website"),
        },
        disabled=["id", "name", "email", "website"],
        key="orphan_editor",
    )

    to_delete_orphans = edited[edited["delete"]]["id"].tolist()

    if not to_delete_orphans:
        st.info("No actors selected — tick 'Delete?' in the table above.")
    else:
        st.error(
            f"**{len(to_delete_orphans)} actor(s) selected for deletion.**  \n"
            "This cannot be undone. Actors affiliated with others will be refused by the API."
        )
        confirmed_cb = st.checkbox(
            f"I understand that up to {len(to_delete_orphans)} actor(s) will be permanently deleted",
            key="orphan_confirm",
        )
        if st.button(
            f"Delete {len(to_delete_orphans)} actor(s)",
            type="primary",
            disabled=not confirmed_cb,
            key="btn_delete_orphans",
        ):
            successes, failures = [], []
            bar = st.progress(0, text=f"Deleting… 0 / {len(to_delete_orphans)}")
            for i, actor_id in enumerate(to_delete_orphans, 1):
                ok, msg = delete_actor(int(actor_id))
                (successes if ok else failures).append((actor_id, msg))
                bar.progress(i / len(to_delete_orphans), text=f"Deleting… {i} / {len(to_delete_orphans)}")
            bar.empty()

            if successes:
                st.success(f"Deleted {len(successes)} actor(s).")
            if failures:
                st.warning(f"{len(failures)} actor(s) not deleted:")
                for aid, msg in failures:
                    st.write(f"- ID {aid}: {msg}")

            deleted_ids = {aid for aid, _ in successes}
            st.session_state["actor_details"] = orphan_actor_details[
                ~orphan_actor_details["id"].isin(deleted_ids)
            ].reset_index(drop=True)
            for aid in deleted_ids:
                st.session_state["orphan_verified"].pop(int(aid), None)
            st.rerun()

