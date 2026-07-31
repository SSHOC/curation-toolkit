"""
Data Source — manage the local snapshot used by all curation tools.

Shows all full_items_*.json files in the data/ directory with their age,
size, and source environment (read from the sidecar .meta.json file written
when the snapshot was created).  The most recently modified file is the
active snapshot.

Two ways to refresh the data:
  Download from GitHub  – fast, always Production data, sourced from the
                          sshompitor repository's data/ directory.
  Fetch fresh from API  – slow (several minutes), creates a snapshot from
                          the currently logged-in environment (recommended
                          when working on Stage).
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import streamlit as st
from lib.auth import require_login, current_role_label
from lib.snapshot import (
    get_latest_snapshot_info,
    fetch_latest_from_github,
    read_snapshot_meta,
    _DATA_DIR,
)
from lib.api import create_snapshot_from_api

require_login()

st.set_page_config(page_title="Data Source — Curation Toolkit", page_icon="🗄️", layout="wide")

env = st.session_state["env"]
username = st.session_state.get("username", "—")

st.title("Data Source")

# ── Environment ───────────────────────────────────────────────────────────────
with st.container(border=True):
    col_e1, col_e2 = st.columns(2)
    col_e1.markdown(f"**Environment:** {env['label']}")
    col_e1.caption(env["api_url"])
    col_e2.markdown(f"**Logged in as:** {username} ({current_role_label()})")
    col_e2.caption(env["mp_url"])

st.divider()

# ── Snapshot status ───────────────────────────────────────────────────────────
st.subheader("Local snapshot")

all_snapshots = sorted(
    _DATA_DIR.glob("full_items_*.json"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

if not all_snapshots:
    st.warning("No snapshot found in data/. Use the options below to get one.")
else:
    import datetime

    rows = []
    for i, p in enumerate(all_snapshots):
        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        age = datetime.datetime.now() - mtime
        age_days = age.total_seconds() / 86400
        if age_days < 1:
            age_str = f"{int(age.total_seconds() // 3600)}h ago"
        elif age_days < 2:
            age_str = "1 day ago"
        else:
            age_str = f"{int(age_days)} days ago"

        size_mb = p.stat().st_size / 1_048_576
        meta = read_snapshot_meta(p)
        env_label = meta.get("env_label", "")
        source = meta.get("source", "")
        rows.append({
            "active": i == 0,
            "environment": env_label if env_label else "—",
            "source": source.capitalize() if source else "—",
            "file": p.name,
            "date": mtime.strftime("%Y-%m-%d %H:%M"),
            "age": age_str,
            "size": f"{size_mb:.1f} MB",
        })

    import pandas as pd

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "active": st.column_config.CheckboxColumn("Active", width="small",
                help="The most recent file is used by all tools."),
            "environment": st.column_config.TextColumn("Environment"),
            "source": st.column_config.TextColumn("Source"),
        },
    )

    latest_path, latest_age, latest_dt = all_snapshots[0], \
        datetime.datetime.now() - datetime.datetime.fromtimestamp(all_snapshots[0].stat().st_mtime), \
        datetime.datetime.fromtimestamp(all_snapshots[0].stat().st_mtime)
    age_days_latest = latest_age.total_seconds() / 86400

    if age_days_latest >= 3:
        st.warning(
            f"The active snapshot is **{int(age_days_latest)} days old**. "
            "Consider refreshing before curation."
        )
    else:
        st.success("Snapshot is up to date.")

st.divider()

# ── Refresh actions ───────────────────────────────────────────────────────────
st.subheader("Get data")

is_stage = env["label"] != "Production"

if is_stage:
    st.info(
        "The GitHub snapshot archive contains **Production data only**. "
        "For Stage, use **Fetch fresh from API** to build a snapshot from the Stage environment."
    )

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Download from GitHub**")
    st.caption(
        "Downloads the latest snapshot from the sshompitor repository. "
        "Fast — typically a few seconds. **Production data only.**"
    )
    if st.button(
        "Download latest snapshot",
        use_container_width=True,
        type="secondary" if is_stage else "primary",
    ):
        with st.spinner("Downloading…"):
            ok, msg = fetch_latest_from_github()
        if ok:
            st.success(msg)
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        else:
            st.error(msg)

with col2:
    st.markdown("**Fetch fresh from API**")
    st.caption(
        "Queries the live Marketplace API and builds a new snapshot. "
        "Takes several minutes. "
        + ("**Recommended for Stage.**" if is_stage else "Use when you need data newer than what's on GitHub.")
    )
    if st.button(
        "Create fresh snapshot",
        use_container_width=True,
        type="primary" if is_stage else "secondary",
    ):
        with st.spinner("Fetching all items from the Marketplace API…"):
            ok, msg = create_snapshot_from_api(
                env["api_url"], st.session_state["bearer"], _DATA_DIR,
                env_label=env.get("label", ""),
            )
        if ok:
            st.success(msg)
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        else:
            st.error(msg)

# ── Cleanup old snapshots ─────────────────────────────────────────────────────
if len(all_snapshots) > 1:
    st.divider()
    with st.expander(f"Remove old snapshots ({len(all_snapshots) - 1} older file(s))"):
        st.caption("Only the newest file is used. Remove older ones to save disk space.")
        for p in all_snapshots[1:]:
            c1, c2 = st.columns([4, 1])
            c1.write(p.name)
            if c2.button("Delete", key=f"del_{p.name}"):
                p.unlink()
                st.rerun()
