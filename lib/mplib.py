"""
Thin wrapper around the sshmarketplacelib (sshompitor) helper.

Import precedence (see vendor/NOTICE.md for why):
1. A pip-installed `sshmarketplacelib` package, if present.
2. A sibling `../sshompitor` clone, if present — for local development
   against an actively-edited copy of the upstream library.
3. The vendored copy bundled in this repo (`vendor/sshmarketplacelib/`) —
   pinned to a known-good commit, no network access or extra download
   required. This is the path everyone else uses, including run.bat.

`get_util()` is cached with `st.cache_resource` so the snapshot is loaded
from disk only once per Streamlit server process.
"""

import sys
import pathlib
import streamlit as st

try:
    from sshmarketplacelib.helper import Util
except ImportError:
    _SSHOMPITOR = pathlib.Path(__file__).parent.parent.parent / "sshompitor"
    _VENDORED = pathlib.Path(__file__).parent.parent / "vendor"
    _FALLBACK = _SSHOMPITOR if _SSHOMPITOR.is_dir() else _VENDORED
    if str(_FALLBACK) not in sys.path:
        sys.path.insert(0, str(_FALLBACK))
    from sshmarketplacelib.helper import Util


@st.cache_resource(show_spinner="Loading Marketplace snapshot…")
def get_util() -> Util:
    """
    Return a cached Util instance backed by the local snapshot.

    Uses cache_resource (process-level) rather than cache_data (session-level)
    so the snapshot DataFrame is shared across browser sessions and not
    re-parsed on every rerun.
    """
    return Util()
