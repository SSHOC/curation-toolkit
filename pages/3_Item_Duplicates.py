"""
Item Duplicates — find duplicate items and merge them.

Tab 1 — Find Duplicates
  The user selects which top-level fields to check (label, description,
  accessibleAt) and which categories to include.  Items without an
  accessibleAt URL are excluded before the check — they are stub entries
  that cannot be meaningfully distinguished from each other.

  Results are persisted in st.session_state so that fetching live API data
  for side-by-side comparison does not re-trigger the duplicate scan.

Tab 2 — Merge Items
  Merges two items of the same category by persistentId. Unlike actors,
  the Marketplace API has no single merge call this toolkit relies on for
  items — instead this GETs both records, builds the consolidated result
  locally (unioning contributors, properties, externalIds, accessibleAt,
  media, and relatedItems; keeping as much data as possible), shows the
  curator a full before/after overview, then PUTs the result onto the
  kept item, repoints relatedItems on any other item that referenced the
  merged-away item, and finally deletes it.

Shared state (st.session_state keys)
  item_dup_result   – last getDuplicates() result DataFrame
  item_dup_props    – list of property names used for the last search
  item_dup_filtered – count of items excluded due to missing accessibleAt
  fetched_items     – dict of group_idx → {persistentId: live_item_dict}
  item_merged       – dict of merge keys → success message (Merge Items tab)
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
from lib.auth import require_login
from lib.mplib import get_util
from lib.snapshot import render_data_status, require_snapshot
from lib.api import get_item, merge_items, consolidate_item_payload

require_login()

st.set_page_config(page_title="Item Duplicates — Curation Toolkit", page_icon="📄", layout="wide")

env = st.session_state["env"]
st.title("Item Duplicates")
st.caption(f"Environment: **{env['label']}** — {env['api_url']}")

MP_SERVER = env["mp_url"]

require_snapshot()
render_data_status()


@st.cache_data(show_spinner="Loading snapshot…")
def load_snapshot() -> pd.DataFrame:
    return get_util()._load_snapshot()


def _has_accessible_at(val) -> bool:
    """
    Return True when a snapshot accessibleAt value contains at least one URL.

    The field can appear as None, NaN, an empty list, the string "[]", or a
    non-empty list of URL strings.  All empty/missing forms return False.
    """
    if val is None:
        return False
    if isinstance(val, float) and pd.isna(val):
        return False
    if isinstance(val, list):
        return len(val) > 0
    s = str(val).strip()
    return s not in ("", "[]", "nan")


def _mp_link(mp_server: str, category: str, persistent_id: str) -> str | None:
    """
    Build the Marketplace web URL for an item, e.g.
    https://marketplace.sshopencloud.eu/dataset/2wcOYh — always
    {mp_server}/{category}/{persistentId}, using the item's own singular
    category (not the pluralized API path segment from _CATEGORY_PATH).
    """
    if not category or not persistent_id:
        return None
    return f"{mp_server.rstrip('/')}/{category}/{persistent_id}"


def _render_item_card(item: dict, mp_server: str) -> None:
    """
    Render a compact summary card for a single live API item.

    Shows label, persistent ID, category, status, source system, up to three
    access URLs, contributor names, and the first 400 characters of the
    description. Used inside the side-by-side comparison expanders.
    """
    pid = item.get("persistentId", "—")
    cat = item.get("category", "—")
    status = item.get("status", "—")
    source = (item.get("source") or {}).get("label", "—")
    access_urls = item.get("accessibleAt") or []
    desc = (item.get("description") or "").strip()
    contributors = item.get("contributors") or []
    mp_link = _mp_link(mp_server, item.get("category"), item.get("persistentId"))

    st.markdown(f"**{item.get('label', '—')}**")
    st.caption(f"`{pid}` · {cat}")
    st.caption(f"Status: **{status}**" + (f" · Source: {source}" if source != "—" else ""))
    if mp_link:
        st.markdown(f"[Open in Marketplace]({mp_link})")

    if access_urls:
        for url in (access_urls if isinstance(access_urls, list) else [access_urls])[:3]:
            st.markdown(f"[{url}]({url})")

    if contributors:
        names = [
            c.get("actor", {}).get("name", "")
            for c in contributors[:5]
            if c.get("actor", {}).get("name")
        ]
        if names:
            st.caption("Contributors: " + ", ".join(names))

    if desc:
        st.text(desc[:400] + ("…" if len(desc) > 400 else ""))


def _render_scalar_card(item: dict, mp_server: str) -> None:
    """
    Render only the identity/scalar fields of an item (label, ID, category,
    status, source, version, description) — no list fields. Used for the
    Keep-vs-merge-away identity comparison in the Merge Items tab; list
    fields (contributors, properties, etc.) are shown as checkbox sections
    instead so the curator can pick which entries survive the merge.
    """
    pid = item.get("persistentId", "—")
    cat = item.get("category", "—")
    status = item.get("status", "—")
    source = (item.get("source") or {}).get("label")
    desc = (item.get("description") or "").strip()
    mp_link = _mp_link(mp_server, item.get("category"), item.get("persistentId"))

    st.markdown(f"**{item.get('label', '—')}**")
    st.caption(
        f"`{pid}` · {cat} · status: {status}"
        + (f" · source: {source}" if source else "")
        + (f" · version: {item.get('version')}" if item.get("version") else "")
    )
    if mp_link:
        st.markdown(f"[Open in Marketplace]({mp_link})")
    if desc:
        st.text(desc[:400] + ("…" if len(desc) > 400 else ""))


def _contributor_label(c: dict) -> str:
    name = c.get("actor", {}).get("name", "") or "(unnamed actor)"
    role = c.get("role", {}).get("label") or c.get("role", {}).get("code", "")
    return f"{name} — {role}" if role else name


def _property_label(p: dict) -> str:
    type_label = p.get("type", {}).get("label") or p.get("type", {}).get("code", "")
    concept = p.get("concept")
    value = (concept.get("label") or concept.get("code", "")) if concept else p.get("value", "")
    return f"{type_label}: {value}" if type_label else str(value)


def _extid_label(e: dict) -> str:
    code = e.get("identifierService", {}).get("code", "")
    return f"{code}: {e.get('identifier', '')}" if code else e.get("identifier", "")


def _media_label(m: dict) -> str:
    info = m.get("info") or {}
    return m.get("caption") or info.get("mediaId") or "media item"


def _related_label(r: dict) -> str:
    rel = (r.get("relation") or {}).get("label", "")
    name = r.get("label") or r.get("persistentId", "")
    return f"{rel}: {name}" if rel else name


def _render_full_item_card(item: dict, mp_server: str) -> None:
    """
    Render the full detail of an item — identity fields plus every
    contributor, property, external ID, related item, and media entry
    listed individually (not truncated/summarized). Used for the Outcome
    preview in the Merge Items tab, so the curator sees exactly what the
    merged item will contain.
    """
    _render_scalar_card(item, mp_server)

    access_urls = item.get("accessibleAt") or []
    contributors = item.get("contributors") or []
    properties = item.get("properties") or []
    ext_ids = item.get("externalIds") or []
    related = item.get("relatedItems") or []
    media = item.get("media") or []

    if access_urls:
        st.caption(f"**Accessible at ({len(access_urls)}):**")
        for url in access_urls:
            st.markdown(f"- [{url}]({url})")

    if contributors:
        st.caption(f"**Contributors ({len(contributors)}):**")
        for c in contributors:
            st.caption(f"- {_contributor_label(c)}")

    if properties:
        st.caption(f"**Properties ({len(properties)}):**")
        for p in properties:
            st.caption(f"- {_property_label(p)}")

    if ext_ids:
        st.caption(f"**External IDs ({len(ext_ids)}):**")
        for e in ext_ids:
            st.caption(f"- {_extid_label(e)}")

    if related:
        st.caption(f"**Related items ({len(related)}):**")
        for r in related:
            st.caption(f"- {_related_label(r)}")

    if media:
        st.caption(f"**Media ({len(media)}):**")
        for m in media:
            st.caption(f"- {_media_label(m)}")


def _split_keep_merge(keep_list: list, merge_list: list, key_fn) -> tuple[list, list]:
    """
    Split into (keep_entries, merge_only_entries): keep_list deduped against
    itself, and merge_list with anything already present in keep_list (or
    repeated within merge_list) dropped — so a value shared by both items
    only ever appears once, under "keep".
    """
    keep_keys: set = set()
    keep_entries = []
    for entry in keep_list:
        key = key_fn(entry)
        if key in keep_keys:
            continue
        keep_keys.add(key)
        keep_entries.append(entry)

    seen_merge: set = set()
    merge_entries = []
    for entry in merge_list:
        key = key_fn(entry)
        if key in keep_keys or key in seen_merge:
            continue
        seen_merge.add(key)
        merge_entries.append(entry)

    return keep_entries, merge_entries


def _render_checkbox_field(
    title: str, keep_list: list, merge_list: list,
    key_fn, label_fn, state_prefix: str,
) -> list:
    """
    Render a side-by-side, color-coded checkbox comparison for one field:
    left column = entries from the kept item (green), right column =
    entries the merge-away item would add (blue) — a value present in both
    items only appears once, on the left. All checkboxes start checked (a
    full union, matching the old automatic-merge behaviour). Returns the
    entries the curator left checked, in the same order shown.
    """
    keep_entries, merge_entries = _split_keep_merge(keep_list, merge_list, key_fn)
    st.markdown(f"**{title}**  ·  :green[{len(keep_entries)} from keep]  ·  :blue[{len(merge_entries)} from merge away]")

    selected = []
    col_k, col_m = st.columns(2)
    with col_k:
        if not keep_entries:
            st.caption("_None._")
        for i, entry in enumerate(keep_entries):
            checked = st.checkbox(
                f":green[{label_fn(entry)}]", value=True, key=f"{state_prefix}_keep_{i}",
            )
            if checked:
                selected.append(entry)
    with col_m:
        if not merge_entries:
            st.caption("_None._")
        for i, entry in enumerate(merge_entries):
            checked = st.checkbox(
                f":blue[{label_fn(entry)}]", value=True, key=f"{state_prefix}_merge_{i}",
            )
            if checked:
                selected.append(entry)
    return selected


def _find_referrers(snap: pd.DataFrame, persistent_id: str, exclude_pids: set) -> list[dict]:
    """
    Rows in the snapshot whose relatedItems reference persistent_id — i.e.
    other items that will need their relatedItems repointed once
    persistent_id is merged away. Excludes the merge pair itself.
    """
    if "relatedItems" not in snap.columns:
        return []

    def _references(related) -> bool:
        if not isinstance(related, list):
            return False
        return any(isinstance(r, dict) and r.get("persistentId") == persistent_id for r in related)

    mask = snap["relatedItems"].apply(_references)
    refs = snap[mask]
    if refs.empty:
        return []
    refs = refs[~refs["persistentId"].isin(exclude_pids)]
    cols = [c for c in ["persistentId", "category", "label"] if c in refs.columns]
    return refs[cols].to_dict("records")


snap = load_snapshot()

if snap.empty:
    st.warning("No snapshot data found.")
    st.stop()

tab_find, tab_merge = st.tabs(["Find Duplicates", "Merge Items"])

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Find Duplicates
# ─────────────────────────────────────────────────────────────────────────────
with tab_find:
    col_left, col_right = st.columns([1, 2])

    with col_left:
        all_categories = sorted(snap["category"].dropna().unique().tolist())
        selected_cats = st.multiselect(
            "Filter by category",
            all_categories,
            default=all_categories,
            key="find_cats",
        )

        TOP_LEVEL_CHECKABLE = ["label", "description", "accessibleAt"]
        selected_props = st.multiselect(
            "Check for duplicates in",
            TOP_LEVEL_CHECKABLE,
            default=["label"],
        )

        run_items = st.button("Find Duplicates", use_container_width=True)

    # ── Run duplicate detection ───────────────────────────────────────────────
    if run_items:
        if not selected_props:
            st.warning("Select at least one property to check.")
        else:
            subset = snap[snap["category"].isin(selected_cats)].copy()

            # Filter out items without an access URL — they cannot be meaningfully deduped
            if "accessibleAt" in subset.columns:
                before = len(subset)
                subset = subset[subset["accessibleAt"].apply(_has_accessible_at)]
                filtered = before - len(subset)
            else:
                filtered = 0

            props_csv = ",".join(selected_props)
            result = get_util().getDuplicates(subset, props_csv)

            st.session_state["item_dup_result"] = result
            st.session_state["item_dup_props"] = list(selected_props)
            st.session_state["item_dup_filtered"] = filtered
            st.session_state["fetched_items"] = {}

    # ── Display stored results ────────────────────────────────────────────────
    result: pd.DataFrame | None = st.session_state.get("item_dup_result")
    stored_props: list = st.session_state.get("item_dup_props", [])
    filtered_count: int = st.session_state.get("item_dup_filtered", 0)
    fetched_items: dict = st.session_state.setdefault("fetched_items", {})

    with col_right:
        if result is None:
            st.info("Select options and click **Find Duplicates** to search.")
        elif result.empty:
            if filtered_count:
                st.info(f"Excluded {filtered_count} items without an access URL.")
            st.success("No duplicates found with these settings.")
        else:
            if filtered_count:
                st.info(f"Excluded {filtered_count} items without an access URL.")

            st.metric("Duplicate rows found", len(result))

            disp_cols = ["label", "category", "persistentId"] + [
                c for c in stored_props if c not in ["label", "category", "persistentId"]
            ]
            disp_cols = [c for c in dict.fromkeys(disp_cols) if c in result.columns]

            display = result[disp_cols].copy()
            if "category" in result.columns and "persistentId" in result.columns:
                display["Link"] = [
                    _mp_link(MP_SERVER, cat, pid) or ""
                    for cat, pid in zip(result["category"], result["persistentId"])
                ]
                disp_cols = disp_cols + ["Link"]

            st.dataframe(
                display,
                use_container_width=True,
                column_config={"Link": st.column_config.LinkColumn("Open in MP")},
                hide_index=True,
            )

            csv = display[disp_cols].to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, "item_duplicates.csv", "text/csv")

            st.caption(
                "To merge two of these, copy their `persistentId` values into the "
                "**Merge Items** tab."
            )

    # ── Side-by-side comparison ───────────────────────────────────────────────
    if result is not None and not result.empty and stored_props:
        st.divider()
        st.subheader("Compare duplicate groups")

        key_cols = [c for c in stored_props if c in result.columns]
        # Lists (e.g. accessibleAt) are unhashable — stringify them for groupby
        groupby_df = result.copy()
        for col in key_cols:
            if groupby_df[col].apply(lambda x: isinstance(x, list)).any():
                groupby_df[col] = groupby_df[col].apply(lambda x: ", ".join(str(u) for u in x) if isinstance(x, list) else x)
        groups = list(groupby_df.groupby(key_cols, sort=False))

        for group_idx, (group_key, group_df) in enumerate(groups):
            if isinstance(group_key, str):
                group_key = (group_key,)
            label_str = " · ".join(str(k)[:80] for k in group_key)

            with st.expander(f"{label_str}  ({len(group_df)} items)"):
                rows = group_df.to_dict("records")
                group_fetched = fetched_items.get(group_idx)

                if group_fetched is None:
                    # Show snapshot data + fetch button
                    cols = st.columns(len(rows))
                    for col, row in zip(cols, rows):
                        pid = row.get("persistentId", "")
                        cat = row.get("category", "")
                        mp_url = _mp_link(MP_SERVER, cat, pid)
                        with col:
                            st.markdown(f"**{row.get('label', '')}**")
                            st.caption(f"{cat} · `{pid}`")
                            if mp_url:
                                st.markdown(f"[Open in MP]({mp_url})")

                    if st.button("Fetch live data from API", key=f"fetch_{group_idx}"):
                        group_data = {}
                        for row in rows:
                            pid = row.get("persistentId", "")
                            cat = row.get("category", "")
                            try:
                                group_data[pid] = get_item(
                                    cat, pid, env["api_url"], st.session_state["bearer"]
                                )
                            except Exception as e:
                                group_data[pid] = {"error": str(e), "persistentId": pid}
                        fetched_items[group_idx] = group_data
                        st.rerun()
                else:
                    # Show live data side-by-side
                    cols = st.columns(len(rows))
                    for col, row in zip(cols, rows):
                        pid = row.get("persistentId", "")
                        item = group_fetched.get(pid, {})
                        with col:
                            if "error" in item:
                                st.error(f"Failed to load `{pid}`: {item['error']}")
                            else:
                                _render_item_card(item, MP_SERVER)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Merge Items
# ─────────────────────────────────────────────────────────────────────────────
with tab_merge:
    st.warning(
        "**Merging items is destructive and cannot be undone by this tool.** "
        "If either item originated from an automated harvest/ingest (check the "
        "`source` shown below), a future harvest run may re-create the item "
        "that was merged away — the toolkit has no way to tell an external "
        "harvester that the two records were the same. If a duplicate keeps "
        "reappearing after merging, fix the underlying source feed instead of "
        "re-merging it every time."
    )

    st.markdown(
        "Merge two items of the **same category**. This GETs both records, "
        "builds the merged result locally (unioning contributors, properties, "
        "external IDs, accessible-at URLs, media, and related items so as "
        "little data as possible is lost), and shows you the full outcome "
        "before anything is written. Other items that relate to the merged-away "
        "item are then repointed to the kept item, and the merged-away item is "
        "deleted."
    )
    st.caption(
        "Tip: find `persistentId` values on the **Find Duplicates** tab. "
        "Workflow steps are not supported here — merge the parent workflow instead."
    )

    merge_categories = [c for c in sorted(snap["category"].dropna().unique().tolist()) if c != "step"]
    merge_category = st.selectbox("Category (both items must match)", merge_categories, key="merge_category")

    pid_to_label = dict(zip(snap["persistentId"], snap["label"])) if "label" in snap.columns else {}

    col1, col2 = st.columns(2)
    with col1:
        keep_pid = st.text_input("Keep this item (persistentId)", "", key="merge_keep_pid")
        if keep_pid.strip():
            name = pid_to_label.get(keep_pid.strip())
            st.caption(f"→ {name}" if name else "→ not found in snapshot")
    with col2:
        merge_pid = st.text_input("Merge this item into it (persistentId)", "", key="merge_merge_pid")
        if merge_pid.strip():
            name = pid_to_label.get(merge_pid.strip())
            st.caption(f"→ {name}" if name else "→ not found in snapshot")

    keep_pid, merge_pid = keep_pid.strip(), merge_pid.strip()

    if not keep_pid or not merge_pid:
        st.caption("Enter both persistentIds above to load a preview.")
    elif keep_pid == merge_pid:
        st.warning("Keep and merge items must be different.")
    else:
        item_merged = st.session_state.setdefault("item_merged", {})
        merge_key = f"{merge_category}__{keep_pid}__{merge_pid}"
        already_merged = merge_key in item_merged

        if already_merged:
            # The merge item no longer exists on the API — don't try to re-fetch it.
            st.success(item_merged[merge_key])
            st.caption("Change one of the items above to start a new merge.")
        else:
            keep_item, merge_item, fetch_error = None, None, None
            with st.spinner("Fetching both items…"):
                try:
                    keep_item = get_item(merge_category, keep_pid, env["api_url"], st.session_state["bearer"])
                except Exception as e:
                    fetch_error = f"Could not fetch `{keep_pid}`: {e}"
                if fetch_error is None:
                    try:
                        merge_item = get_item(merge_category, merge_pid, env["api_url"], st.session_state["bearer"])
                    except Exception as e:
                        fetch_error = f"Could not fetch `{merge_pid}`: {e}"

            if fetch_error:
                st.error(fetch_error)
            else:
                exclude_pids = {keep_pid, merge_pid}
                referrers = _find_referrers(snap, merge_pid, exclude_pids=exclude_pids)

                st.markdown("#### Keep vs. merge away")
                col_keep, col_merge = st.columns(2)
                with col_keep:
                    st.caption(":green[**KEEP**]")
                    _render_scalar_card(keep_item, MP_SERVER)
                with col_merge:
                    st.caption(":blue[**MERGE AWAY**] (will be deleted)")
                    _render_scalar_card(merge_item, MP_SERVER)
                st.caption(
                    "Label always comes from the kept item. Description, version, source, "
                    "and thumbnail use the kept item's value, falling back to the merged "
                    "item's if the kept item has none."
                )

                st.divider()
                st.markdown("#### Choose what to keep")
                st.caption(
                    ":green[Green] = already on the kept item. :blue[Blue] = would be added from "
                    "the merge-away item. Everything is checked by default (a full union) — "
                    "uncheck anything you don't want carried over."
                )

                sel_contributors = _render_checkbox_field(
                    "Contributors",
                    keep_item.get("contributors") or [], merge_item.get("contributors") or [],
                    key_fn=lambda c: (c.get("actor", {}).get("id"), c.get("role", {}).get("code")),
                    label_fn=_contributor_label, state_prefix=f"{merge_key}_contrib",
                )
                st.divider()
                sel_properties = _render_checkbox_field(
                    "Properties",
                    keep_item.get("properties") or [], merge_item.get("properties") or [],
                    key_fn=lambda p: (p.get("type", {}).get("code"), (p.get("concept") or {}).get("code"), p.get("value")),
                    label_fn=_property_label, state_prefix=f"{merge_key}_props",
                )
                st.divider()
                sel_ext_ids = _render_checkbox_field(
                    "External IDs",
                    keep_item.get("externalIds") or [], merge_item.get("externalIds") or [],
                    key_fn=lambda e: (e.get("identifierService", {}).get("code"), e.get("identifier")),
                    label_fn=_extid_label, state_prefix=f"{merge_key}_extid",
                )
                st.divider()
                sel_urls = _render_checkbox_field(
                    "Accessible-at URLs",
                    keep_item.get("accessibleAt") or [], merge_item.get("accessibleAt") or [],
                    key_fn=lambda u: u, label_fn=lambda u: u, state_prefix=f"{merge_key}_url",
                )
                st.divider()
                sel_media = _render_checkbox_field(
                    "Media",
                    keep_item.get("media") or [], merge_item.get("media") or [],
                    key_fn=lambda m: (m.get("info") or {}).get("mediaId"),
                    label_fn=_media_label, state_prefix=f"{merge_key}_media",
                )

                st.divider()
                # relatedItems entries between the keep and merge item themselves are
                # dropped before display — they'd become a self-loop once the merge
                # item is deleted, so there's nothing meaningful to choose there.
                keep_related = [r for r in keep_item.get("relatedItems") or [] if r.get("persistentId") not in exclude_pids]
                merge_related = [r for r in merge_item.get("relatedItems") or [] if r.get("persistentId") not in exclude_pids]
                sel_related = _render_checkbox_field(
                    "Related items",
                    keep_related, merge_related,
                    key_fn=lambda r: (r.get("persistentId"), (r.get("relation") or {}).get("code")),
                    label_fn=_related_label, state_prefix=f"{merge_key}_related",
                )

                outcome = consolidate_item_payload(keep_item, merge_item)
                outcome["contributors"] = sel_contributors
                outcome["properties"] = sel_properties
                outcome["externalIds"] = sel_ext_ids
                outcome["accessibleAt"] = sel_urls
                outcome["media"] = sel_media
                outcome["relatedItems"] = sel_related

                st.divider()
                st.markdown("#### Outcome preview")
                _render_full_item_card(outcome, MP_SERVER)

                if referrers:
                    with st.expander(f"{len(referrers)} other item(s) reference the item being merged away"):
                        st.caption(
                            "Their `relatedItems` links will be repointed to the kept item as part of this merge."
                        )
                        referrers_df = pd.DataFrame(referrers)
                        referrers_df["Link"] = [
                            _mp_link(MP_SERVER, cat, pid) or ""
                            for cat, pid in zip(referrers_df["category"], referrers_df["persistentId"])
                        ]
                        st.dataframe(
                            referrers_df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={"Link": st.column_config.LinkColumn("Open in MP")},
                        )
                else:
                    st.caption("No other items in the snapshot reference the item being merged away.")

                st.divider()

                confirmed_cb = st.checkbox(
                    f"I understand that `{merge_pid}` will be permanently deleted and its data "
                    f"folded into `{keep_pid}` as shown above. This cannot be undone.",
                    key=f"merge_confirm_{merge_key}",
                )

                if st.button(
                    f"Merge `{merge_pid}` into `{keep_pid}`",
                    type="primary",
                    key=f"merge_btn_{merge_key}",
                    disabled=not confirmed_cb,
                ):
                    referrer_pairs = [(r["category"], r["persistentId"]) for r in referrers]
                    result_dict = merge_items(
                        merge_category, keep_pid,
                        merge_category, merge_pid,
                        referrer_pairs,
                        env["api_url"], st.session_state["bearer"],
                        payload=outcome,
                    )
                    if result_dict["ok"]:
                        item_merged[merge_key] = result_dict["message"]
                        load_snapshot.clear()
                        st.rerun()
                    else:
                        st.error(result_dict["message"])
                        if result_dict["repointed"]:
                            st.caption("Referrer repointing results so far:")
                            for pid, r_ok, r_msg in result_dict["repointed"]:
                                (st.write if r_ok else st.warning)(f"- `{pid}`: {r_msg}")
