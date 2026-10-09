# Changelog

Notable changes to the Curation Toolkit, newest first. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project doesn't use
version numbers, so entries are dated instead. History before this file
existed is in `git log`.

## 2026-10-09 — Applied items stay in the review list (OpenAIRE Enrichment)

### Changed

- A successfully-applied item no longer disappears from the per-item review
  list — it stays in the same position, collapsed, marked **✅ applied**,
  with a read-only summary of what was added, instead of only being
  reachable by switching the "Show" filter to **Applied**. Fixes losing
  your place when working down a batch of items one by one. Failed attempts
  are unaffected — still expanded, interactive, and retryable. The summary
  metrics/filter/CSV above (With proposals / Applied bucket) are unchanged.

## 2026-10-09 — Show the actual OpenAIRE response (OpenAIRE Enrichment)

### Added

- Each reviewable item now has a **"View on OpenAIRE"** link
  (`explore.openaire.eu/search/result?pid={doi}`) alongside the existing
  DOI link, which goes to the publisher, not OpenAIRE.
- A new **"OpenAIRE response for this DOI"** expander shows a readable
  summary (title, publication date, publisher, language, and every `FOS`
  and `keyword` subject OpenAIRE reported — including ones that didn't map
  to any proposal and so were previously invisible), plus a nested **"Raw
  JSON response"** with the complete, unprocessed OpenAIRE record. Addresses
  not being able to tell what data actually came from OpenAIRE versus this
  toolkit's interpretation of it.
- `lib.openaire.fetch_one()` now also returns the raw API record (`raw`
  key); cached alongside the extracted fields. Cache version bumped so
  previously-cached entries (which lack `raw`) are transparently refetched.

### Changed

- `_build_proposal()` no longer returns `openaire_title` — superseded by
  the response expander, which reads the title (and everything else)
  straight from the cached lookup result.

## 2026-10-09 — Closed vocabularies before keyword (OpenAIRE Enrichment)

### Added

- OpenAIRE Enrichment now always tries to place a value into the closed
  Marketplace vocabulary it actually belongs to before ever falling back to
  the open `keyword` property (`keyword` is the *only* concept-valued
  property with an open vocabulary — every other one is closed):
  - OpenAIRE's `FOS` (Field of Science) subjects now map to the closed
    `discipline` property — verified live that it's the same OECD/ÖFOS
    classification scheme (e.g. code `601` → "History, Archaeology"), so
    the subject's leading code is looked up directly. These were
    previously discarded entirely.
  - OpenAIRE's `keyword`-scheme subjects are now matched against the
    closed `standard` and `activity` (`tadirah2`) vocabularies, in that
    order, before falling back to the open `sshoc-keyword` vocabulary —
    only what matches nowhere is dropped. Vocabularies describing the
    *resource itself* rather than its subject matter (resource category,
    intended audience, geography, formats, life-cycle/readiness status)
    are deliberately excluded, since a topic a paper discusses isn't
    reliable evidence of what the Marketplace item's own metadata should
    say.
  - `lib.api.fetch_concepts_by_type()` — a new generic paginated
    concept-vocabulary fetch (any one property type, with its true
    `vocabulary_code`), used for the `standard`/`activity` checks.
  - The in-app "How OpenAIRE fields map to Marketplace fields" expander
    and every proposal checkbox's tooltip were updated to describe the
    new priority order.

## 2026-10-09 — Field mapping transparency (OpenAIRE Enrichment)

### Added

- A **"How OpenAIRE fields map to Marketplace fields"** expander at the top
  of the OpenAIRE Enrichment page, laying out which OpenAIRE field feeds
  which Marketplace field and under what condition (e.g. `subjects` only
  when `scheme: keyword`, matched against an *already-existing*
  `sshoc-keyword` concept by case-insensitive label).
- Every proposal checkbox in the review list now carries a tooltip
  restating that rule against the item's actual value, e.g. *"OpenAIRE
  reported ISO 639-3 code 'eng', resolved to the existing Marketplace
  concept 'English'…"* — so curators can see how a specific value was
  derived without leaving the checkbox or cross-referencing the general
  table.

## 2026-10-09 — Source column on the duplicates list (Item Duplicates)

### Added

- The Find Duplicates summary table now shows each item's **source**
  (harvester/system, e.g. `CLARIN Resource Families`) or `(no source —
  manually added)` when it has none, instead of omitting the field
  entirely. Included in the CSV export. Often the deciding factor for which
  duplicate to keep.

## 2026-10-09 — Native merge endpoint toggle (Item Duplicates)

### Added

- Item Duplicates' Merge Items tab gained a **"Use the Marketplace's native
  merge endpoint instead"** checkbox, opt-in per merge. Investigated the
  endpoint's behavior empirically against Stage first, since it isn't
  documented beyond its OpenAPI shape:
  - `POST /api/{category-path}/merge?with={ids}` hard-deletes **every**
    persistentId in `with` — including the one a curator would pick as
    "keep" — and returns a **brand-new** item with a freshly minted
    persistentId. It does not work like actor merging (`POST
    /api/actors/{keep_id}/merge?with=...`), which does preserve an
    identity.
  - Since the Marketplace serves no redirect from a retired persistentId,
    using it breaks existing links to *either* merged item, not just the
    discarded one — which is why it stays opt-in rather than becoming the
    default.
  - It does repoint `relatedItems` on other referencing items server-side
    against live data, more thoroughly than this toolkit's own
    snapshot-driven `repoint_related_item()` — the toggle exists for cases
    where that's worth the persistentId loss.
  - `lib.api.merge_items()` gained a `use_native` parameter (default
    `False`) delegating to the new `lib.api._merge_items_native()`; both
    return an added `new_persistent_id` key.

## 2026-09-30 — Session Log "Source" column

### Added

- Every Session Log entry now carries a **Source**: which page/feature
  triggered it and what it did in plain terms, e.g. `OpenAIRE Enrichment:
  added year: 2020, publisher: DIGITAL.CSIC to 'SimilArITI'` or `Keywords:
  replaced keyword 'xml' with 'XML' on tool-or-service/I5f6Tb` — so entries
  from different tools can be told apart at a glance instead of all reading
  as a generic `Update tool-or-service/…`. New **Feature** filter on the
  Session Log page groups entries by the part of Source before the first
  colon; free-text search now also matches Source. Entries logged before
  this column existed show `(unspecified)`.
  - `lib.logger.log_action()` / `log_api()` gained an optional `source`
    parameter.
  - `lib.api.put_item()` — shared by Keywords, Item Duplicates, and OpenAIRE
    Enrichment — gained an optional `source` parameter each caller fills in;
    every other write helper in `lib/api.py` (single-page functions) builds
    its own `source` string internally.

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
