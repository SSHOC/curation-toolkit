"""
Session Log — view and export the record of actions taken with this
installation.

Every login, API write call (merge, delete, PUT), and major operation
(snapshot creation, orphan verification) is appended to logs/session_log.jsonl
by lib.logger.  This page provides filtering, full-text search, and export
as CSV or JSON.

The log is persistent — it survives closing the browser tab and restarting
the app, and is shared across every session run against this installation
(all users, all tabs). Use "Clear log" to permanently delete it.
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import json
import datetime
import streamlit as st
from lib.auth import require_login, render_account_caption
from lib.logger import get_log, get_log_df, clear_log
from lib.snapshot import render_data_status

require_login()

st.set_page_config(page_title="Session Log — Curation Toolkit", page_icon="📋", layout="wide")

env = st.session_state["env"]
st.title("Session Log")
render_account_caption(env)

render_data_status()

df = get_log_df()

if df.empty:
    st.info(
        "No actions recorded yet. "
        "API write calls (merges, deletes, updates) and major operations will appear here."
    )
    st.stop()

# ── Summary ──────────────────────────────────────────────────────────────────
n_api   = int((df["type"] == "api").sum())
n_fail  = int((df["ok"] == False).sum())

c1, c2, c3 = st.columns(3)
c1.metric("Total entries", len(df))
c2.metric("API calls", n_api)
c3.metric("Failures", n_fail)

# ── Filters ───────────────────────────────────────────────────────────────────
fc1, fc2, fc3 = st.columns([1, 1, 3])
with fc1:
    type_filter = st.multiselect("Type", ["action", "api"], default=["action", "api"])
with fc2:
    ok_filter = st.multiselect("Result", ["ok", "failed"], default=["ok", "failed"])
with fc3:
    search = st.text_input("Search description / URL / response", "")

view = df[df["type"].isin(type_filter)].copy()

ok_bool = {True: "ok", False: "failed"}
ok_vals = {v: k for k, v in ok_bool.items()}
view = view[view["ok"].map(ok_bool).isin(ok_filter)]

if search.strip():
    q = search.strip()
    mask = (
        view["description"].str.contains(q, case=False, na=False)
        | view["url"].str.contains(q, case=False, na=False)
        | view["response"].str.contains(q, case=False, na=False)
        | view["request"].str.contains(q, case=False, na=False)
    )
    view = view[mask]

DISPLAY_COLS = ["time", "type", "ok", "method", "status", "response", "description", "url", "request"]
st.dataframe(
    view[DISPLAY_COLS],
    use_container_width=True,
    hide_index=True,
    column_config={
        "time":        st.column_config.TextColumn("Time", width="small"),
        "type":        st.column_config.TextColumn("Type", width="small"),
        "ok":          st.column_config.CheckboxColumn("OK", width="small"),
        "method":      st.column_config.TextColumn("Method", width="small"),
        "status":      st.column_config.TextColumn("Status", width="small"),
        "response":    st.column_config.TextColumn("Response", width="large"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "url":         st.column_config.TextColumn("URL"),
        "request":     st.column_config.TextColumn("Request"),
    },
)

# ── Export ────────────────────────────────────────────────────────────────────
now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
ec1, ec2, ec3 = st.columns(3)

with ec1:
    st.download_button(
        "Export filtered as CSV",
        data=view.to_csv(index=False).encode("utf-8"),
        file_name=f"session_log_{now_str}.csv",
        mime="text/csv",
        use_container_width=True,
    )

with ec2:
    st.download_button(
        "Export full log as JSON",
        data=json.dumps(get_log(), indent=2).encode("utf-8"),
        file_name=f"session_log_{now_str}.json",
        mime="application/json",
        use_container_width=True,
    )

with ec3:
    if st.button("Clear log", type="secondary", use_container_width=True):
        clear_log()
        st.rerun()
