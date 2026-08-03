"""
Persistent action and API call log.

Every significant operation (login, merge, delete, snapshot creation) and
every API write call is appended, as one JSON line, to logs/session_log.jsonl
next to this repo. Unlike st.session_state, this survives closing the
browser tab and restarting the app (run.bat, streamlit run, etc.) — the log
is a durable audit trail of everything this installation has ever done, not
just the current browser session. It can be filtered, exported as CSV/JSON,
or cleared from the Session Log page.

Concurrent writers (multiple tabs/sessions against the same running server)
are serialized with a module-level lock so entries never interleave/corrupt
the file; this does not protect against two separate `streamlit run`
processes writing at once, which isn't a scenario this desktop tool expects.

Log entry columns:
  time        – ISO-8601 timestamp (seconds precision)
  type        – "action" for high-level events, "api" for HTTP calls
  ok          – True if the operation succeeded
  description – Human-readable summary
  method      – HTTP verb (api entries only)
  url         – Full request URL (api entries only)
  request     – Abbreviated request body (api entries only, ≤ 2 000 chars)
  status      – HTTP status code as string (api entries only)
  response    – Abbreviated response body, prettified if JSON (api entries only, ≤ 1 000 chars)
"""

import datetime
import json
import pathlib
import threading
import pandas as pd

_COLS = ["time", "type", "ok", "description", "method", "url", "request", "status", "response"]

_LOG_DIR = pathlib.Path(__file__).parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "session_log.jsonl"
_LOCK = threading.Lock()


def _append(entry: dict) -> None:
    _LOG_DIR.mkdir(exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with _LOCK:
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def log_action(description: str, ok: bool = True) -> None:
    """Append a high-level action entry (non-API event) to the persistent log."""
    _append({
        "time":        datetime.datetime.now().isoformat(timespec="seconds"),
        "type":        "action",
        "ok":          ok,
        "description": description,
        "method":      "",
        "url":         "",
        "request":     "",
        "status":      "",
        "response":    "",
    })


def _readable_response(raw: str) -> str:
    """If the response is a standard API error JSON, return a compact readable form."""
    if not raw:
        return raw
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return raw
        parts = []
        if "error" in data:
            parts.append(str(data["error"]))
        if "message" in data:
            parts.append(str(data["message"]))
        if "path" in data:
            parts.append(f"path: {data['path']}")
        if parts:
            return " — ".join(parts)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return raw


def log_api(
    method: str,
    url: str,
    description: str,
    status: int | str,
    request: str = "",
    response: str = "",
    ok: bool | None = None,
) -> None:
    """
    Append an API call entry to the persistent log.

    Parameters
    ----------
    method      HTTP verb (GET, POST, PUT, DELETE).
    url         Full request URL including query string.
    description Short human-readable summary of what the call does.
    status      HTTP status code returned by the server, or "error" on network failure.
    request     Optional abbreviated request body (truncated to 2 000 chars).
    response    Optional response body (truncated to 1 000 chars; JSON error envelopes
                are collapsed to their "error" / "message" fields for readability).
    ok          Explicit success flag.  If None it is inferred as status < 400.
    """
    if ok is None:
        try:
            ok = int(status) < 400
        except (ValueError, TypeError):
            ok = False
    _append({
        "time":        datetime.datetime.now().isoformat(timespec="seconds"),
        "type":        "api",
        "ok":          ok,
        "description": description,
        "method":      method,
        "url":         url,
        "request":     (request or "")[:2000],
        "status":      str(status),
        "response":    _readable_response((response or "")[:1000]),
    })


def get_log() -> list[dict]:
    """Return the full persistent log as a list of dicts, oldest first."""
    if not _LOG_FILE.exists():
        return []
    entries = []
    with _LOCK:
        with open(_LOG_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupted line rather than fail the whole log view
    return entries


def get_log_df() -> pd.DataFrame:
    """Return the persistent log as a DataFrame with the canonical column order."""
    entries = get_log()
    if not entries:
        return pd.DataFrame(columns=_COLS)
    return pd.DataFrame(entries, columns=_COLS)


def clear_log() -> None:
    """Permanently delete all persisted log entries."""
    with _LOCK:
        if _LOG_FILE.exists():
            _LOG_FILE.unlink()
