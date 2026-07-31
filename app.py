"""
Login gate for the SSH Open Marketplace Curation Toolkit.

Authenticates against the chosen Marketplace environment and stores the
bearer token in st.session_state.  All other pages call require_login()
which redirects here if the session is not authenticated.

On successful login the user is sent to the Data Source page (1_Data.py)
where they can verify or refresh the local snapshot before starting work.
"""

import streamlit as st
from lib.auth import try_login
from lib.environments import ENVIRONMENTS, DEFAULT_ENV
from lib.logger import log_action
from lib.api import get_current_user

st.set_page_config(page_title="SSH MP Curation Toolkit", page_icon="🔍", layout="centered")

# Already logged in — go straight to the first tool
if st.session_state.get("authenticated"):
    st.switch_page("pages/1_Data.py")

st.title("SSH Open Marketplace — Curation Toolkit")
st.caption("Sign in with your Marketplace account to continue.")

with st.form("login_form"):
    env_name = st.radio(
        "Environment",
        list(ENVIRONMENTS.keys()),
        index=list(ENVIRONMENTS.keys()).index(DEFAULT_ENV),
        horizontal=True,
    )
    st.caption(ENVIRONMENTS[env_name]["api_url"])
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    submitted = st.form_submit_button("Sign in", use_container_width=True)

if submitted:
    if not username or not password:
        st.warning("Please enter both username and password.")
    else:
        with st.spinner("Authenticating…"):
            token = try_login(username, password, env_name)
        if token:
            st.session_state["authenticated"] = True
            st.session_state["bearer"] = token
            st.session_state["username"] = username
            st.session_state["env"] = ENVIRONMENTS[env_name]
            try:
                st.session_state["current_user"] = get_current_user(
                    ENVIRONMENTS[env_name]["api_url"], token
                )
            except Exception:
                # Fails closed: is_moderator() treats an unknown role as no privileges.
                st.session_state["current_user"] = None
                log_action("Could not fetch account role after login", ok=False)
            log_action(f"Login: {username} on {env_name} ({ENVIRONMENTS[env_name]['api_url']})")
            st.switch_page("pages/1_Data.py")
        else:
            st.error("Login failed. Please check your credentials.")
