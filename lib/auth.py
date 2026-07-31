"""
Authentication helpers for the SSH Open Marketplace API.

Credentials are validated against the live API; the returned bearer token
is stored in st.session_state and forwarded with every subsequent API call.

The account's role (fetched once at login via lib.api.get_current_user() and
stored as st.session_state["current_user"]) is used by is_moderator() to
disable destructive actions the account isn't permitted to perform.
"""

import requests
import streamlit as st
from lib.environments import ENVIRONMENTS, DEFAULT_ENV


def try_login(username: str, password: str, env_name: str) -> str | None:
    """POST credentials to the MP API. Returns the bearer token on success, None on failure."""
    server = ENVIRONMENTS[env_name]["api_url"]
    url = server + "/api/auth/sign-in"
    try:
        resp = requests.post(
            url,
            headers={"Content-type": "application/json"},
            json={"username": username, "password": password},
            timeout=10,
        )
    except requests.RequestException as e:
        st.error(f"Could not reach the Marketplace API: {e}")
        return None
    if resp.status_code == 200:
        return resp.headers.get("Authorization")
    return None


def require_login() -> None:
    """Redirect to the login page and stop rendering if the user is not authenticated."""
    if not st.session_state.get("authenticated"):
        st.switch_page("app.py")
        st.stop()


# Roles that the Marketplace backend actually accepts for the destructive
# actions this toolkit performs (actor/item delete, item/concept merge which
# deletes the losing record). Matches UserRole's hierarchy on the backend:
# ADMINISTRATOR and SYSTEM_MODERATOR both carry MODERATOR authority too.
_MODERATOR_ROLES = {"moderator", "system-moderator", "administrator"}


def is_moderator() -> bool:
    """
    True if the logged-in account has moderator privileges or higher —
    required by the backend for every delete/merge-with-delete action in
    this toolkit. Fails closed (False) if the role couldn't be determined
    (e.g. the GET /api/auth/me call failed at login).
    """
    user = st.session_state.get("current_user") or {}
    return user.get("role") in _MODERATOR_ROLES


def current_role_label() -> str:
    """Human-readable account role for display, or 'unknown' if not determined."""
    user = st.session_state.get("current_user") or {}
    return user.get("role") or "unknown"


def render_account_caption(env: dict) -> None:
    """The 'Environment: ... · Logged in as ...' caption shown at the top of every page."""
    username = st.session_state.get("username", "—")
    st.caption(
        f"Environment: **{env['label']}** — {env['api_url']}"
        f"  ·  Logged in as **{username}** ({current_role_label()})"
    )
