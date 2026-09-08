# Changelog

Notable changes to the Curation Toolkit, newest first. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project doesn't use
version numbers, so entries are dated instead. History before this file
existed is in `git log`.

## 2026-09-08 — OpenAIRE Enrichment

### Added

- **OpenAIRE Enrichment page** (`pages/6_OpenAIRE_Enrichment.py`) — finds
  Marketplace items that carry a DOI and backfills `year`, `publisher`,
  `language`, `keyword`, and `accessibleAt` when they're missing, using
  metadata OpenAIRE already has on file for that DOI.
  - Only genuinely missing fields are ever proposed; existing curator-entered
    values are never touched.
  - `language`/`keyword` proposals are only ever made when they resolve to a
    concept that **already exists** in the Marketplace's `iso-639-3` /
    `sshoc-keyword` vocabularies — nothing new is invented in either
    vocabulary.
  - Apply is one item at a time by design — no bulk "apply all", since a
    wrong guess written to many items at once is far more costly than the
    same guess on one. A successful apply immediately moves the item from
    "With proposals" into its own "Applied" bucket (metric, filter, CSV
    column) so the result is visible without navigating away; a failed
    attempt stays listed, auto-expanded with the error, ready to retry.
  - License and author/contributor enrichment are deliberately out of scope
    (closed SPDX vocabulary and actor-dedup risk, respectively).
- `lib/openaire.py` — OpenAIRE Graph API client (DOI lookup, rate-limited
  and cached batch lookup, field extraction).
- `lib.api.get_concept(vocab_code, concept_code, api_url, bearer)` —
  `GET /api/vocabularies/{vocab}/concepts/{code}`, used to resolve language
  concepts by exact code.

### Changed

- `pages/6_Session_Log.py` renamed to `pages/7_Session_Log.py` so it stays
  last in the sidebar nav, after the new OpenAIRE Enrichment page.
