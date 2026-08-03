# Vendored: sshmarketplacelib

This is a copy of the `sshmarketplacelib` Python package from the
[SSHOC/sshompitor](https://github.com/SSHOC/sshompitor) repository, pinned at
commit [`80f5e74`](https://github.com/SSHOC/sshompitor/commit/80f5e74af642913ffa0c9f9adab637ff1825e2a1).

## Why this is vendored instead of a normal pip dependency

The upstream repository's `main` branch is not a stable release target — an
automated job pushes commits to it regularly (observed: a weekly "dashboard
update" job), and the repository has several gigabytes of generated snapshot
data (`data/`, `dashboard_output/`, etc.) committed alongside the actual
library code. Two consequences of installing directly from that repo:

1. **Not reproducible** — installing from `main` HEAD gets whatever code
   happens to be there on install day, not a fixed version. This is what
   caused a real bug (`ValueError: No objects to concatenate` in
   `getContributors()`) to appear on one machine and not another, purely
   because of *when* each machine ran `pip install`.
2. **Slow and wasteful** — `pip install` from that repo has to download the
   entire repository tree at that commit, including the multi-GB data
   folders, even though only the ~100 KB `sshmarketplacelib/` package is
   actually needed. Observed download size: ~830 MB for one `pip install`.

Vendoring the small package directly into this repo fixes both: the exact
code is fixed and reviewable, and there's no multi-hundred-MB download during
setup.

## How this is used

`lib/mplib.py` imports `sshmarketplacelib` with this precedence:
1. A pip-installed `sshmarketplacelib` package, if present.
2. A sibling `../sshompitor` clone, if present (for local development against
   an actively-edited copy of the upstream library).
3. This vendored copy (`vendor/sshmarketplacelib/`) — the path used by
   everyone else, including `run.bat` on Windows.

## Updating this vendored copy

There is no LICENSE file in the upstream repository. It's a small internal
tool published by the SSHOC/DARIAH project for exactly this kind of
consumption (the curation toolkit already depended on it directly via git
before this change), but if that ever becomes a concern, reach out to the
SSHOC/sshompitor maintainers.

To pick up a newer version:
1. Pick a commit from https://github.com/SSHOC/sshompitor (check it against
   real snapshot data first — see the smoke test used when this was set up:
   call `_load_snapshot`, `getContributors`, `_getMPUrl`, `getDuplicates`,
   `getDuplicatedActorsWithItems`, and `getAllProperties` — the six `Util`
   methods this toolkit actually calls).
2. Replace the contents of `vendor/sshmarketplacelib/` with that commit's
   `sshmarketplacelib/` folder only (not the rest of the repo).
3. Update the commit hash in this file.
