# SSH Open Marketplace — Curation Toolkit

A local Streamlit web application for curating the [SSH Open Marketplace](https://marketplace.sshopencloud.eu/) (SSHOMP). It reads data from a local [sshompitor](https://github.com/SSHOC/sshompitor) snapshot for fast offline analysis and writes back to the live Marketplace API for any changes.

---

## Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Windows quick-start (no Python required)](#windows-quick-start-no-python-required)
4. [Setup](#setup)
5. [Running the app](#running-the-app)
6. [Login & environments](#login--environments)
7. [Tool reference](#tool-reference)
   - [Data Source](#data-source)
   - [Actors](#actors)
   - [Item Duplicates](#item-duplicates)
   - [URL Checker](#url-checker)
   - [Keywords](#keywords)
   - [Session Log](#session-log)
8. [Data freshness](#data-freshness)
9. [API reference](#api-reference)
10. [Library reference](#library-reference)
11. [Architecture](#architecture)
12. [Caveats & known limitations](#caveats--known-limitations)
13. [Troubleshooting](#troubleshooting)

---

## Overview

The SSH Open Marketplace accumulates data quality issues over time: duplicate actors, broken access URLs, orphaned entries, and keywords that duplicate concepts already defined in controlled vocabularies. These issues are difficult to spot and fix through the standard Marketplace web interface.

This toolkit provides a set of purpose-built curation screens that:

- Load the full Marketplace dataset from a local snapshot for fast, offline analysis
- Verify findings against the live API before making any changes
- Write changes back through the official Marketplace REST API with appropriate safeguards
- Record every API write call and major action in an exportable session log

The toolkit is intended for Marketplace **moderators and administrators**. All write operations require a valid account token and respect the API's own permission checks. On login the toolkit also fetches the account's role and automatically disables buttons the account isn't permitted to use — see [Account role detection](#login--environments).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Tested on 3.10–3.12 |
| Marketplace account | Moderator or administrator role required for write operations |

No local clone of sshompitor is required. The `sshmarketplacelib` Python package is installed directly from GitHub as part of the normal dependency install (a plain HTTPS download — no `git` executable needed), and a bundled `config.yaml` provides the configuration it needs. Snapshot data is downloaded on first use from the Data Source page.

> **On Windows and don't want to install Python yourself?** See [Windows quick-start](#windows-quick-start-no-python-required) below — `run.bat` sets everything up for you.

---

## Windows quick-start (no Python required)

For a Windows machine with no Python (or any other dependency) installed: double-click **`run.bat`** in the project folder.

The first run downloads a private, self-contained copy of Python into a `python-embed` folder next to the script and installs the app's dependencies into it — this takes a few minutes and needs an internet connection. Every run after that just starts the app in a few seconds. A browser tab opens automatically once it's ready.

This doesn't touch any Python already installed on the machine, and doesn't require Git. If something goes wrong partway through setup, just re-run `run.bat` — or delete the `python-embed` folder first for a completely clean reinstall.

Two things to expect on first run, both normal:
- **Windows Defender SmartScreen** may flag the download as from an "unrecognized publisher" — click "More info → Run anyway".
- **Windows Firewall** may ask for permission when the local web server starts — click "Allow".

---

## Setup

_(Skip this section if you used `run.bat` above.)_

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

This installs all required packages including `sshmarketplacelib` (pulled directly from the [sshompitor GitHub repository](https://github.com/SSHOC/sshompitor)):

```
streamlit>=1.35, pandas, numpy, pyyaml, pillow, requests, fastparquet, sshmarketplacelib
```

### 2. Create the data directory

```bash
python3 setup.py
```

Creates the `data/` directory where snapshot files will be stored. No symlinks or local sshompitor clone required.

### 3. Download the first snapshot

Start the app, log in, and use the **Data Source** page to download the current snapshot from GitHub or fetch fresh data from the API.

---

## Running the app

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in a browser. After login you are taken directly to the Data Source page.

---

## Login & environments

The login screen lets you choose between **Production** and **Stage** before signing in.

| Environment | API base URL |
|---|---|
| Production | `https://marketplace-api.sshopencloud.eu` |
| Stage | `https://sshoc-marketplace-api-stage.acdh-dev.oeaw.ac.at` |

Credentials are sent via `POST /api/auth/sign-in`. On success the `Authorization` header from the response is stored as a bearer token in session state and attached to all subsequent API requests.

Navigating directly to any tool page without being logged in redirects back to the login screen.

> **Recommendation**: always test changes on **Stage** first. The Stage environment is a full copy of Production and its data can be reset.

### Account role detection

Immediately after login the toolkit also calls `GET /api/auth/me` and stores the
returned account (including its `role`) in `st.session_state["current_user"]`. The
role — `contributor`, `system-contributor`, `moderator`, `system-moderator`, or
`administrator` — is shown next to the username in the header caption on every page
(and on the Data Source page).

Buttons that delete something on the server, or merge in a way that deletes the
losing record, are automatically disabled for accounts without at least **moderator**
privileges, since that's what the Marketplace API itself requires for those actions —
clicking them as a plain `contributor` account would otherwise just fail with a 403.
A warning explaining the required role is shown above any disabled button. This
covers: Actors → Orphaned → delete, Item Duplicates → Merge Items, and Keywords →
delete unused concepts / near-duplicate merge. Actions that only need `contributor`
(actor merge, plain item edits, the Keywords cross-vocab "Fix" button) are never
gated, since any authenticated account can already perform them.

If the `GET /api/auth/me` call fails for any reason, the toolkit fails closed — the
role is treated as unknown and every moderator-gated button stays disabled until you
log in again.

---

## Tool reference

### Data Source

**File:** `pages/1_Data.py`  
**Landing page after login.**

Shows the current environment, logged-in user, and all snapshot files in `data/`. The most recently modified file is the active snapshot used by all tools.

#### Snapshot table

Lists every `full_items_*.json` file with its date, age, size, **environment** (Production or Stage), and **source** (GitHub or API). The environment and source are read from a sidecar `full_items_{ts}.meta.json` file written whenever a snapshot is created. Files that pre-date this feature show `—` in those columns.

#### Get data

| Button | What it does |
|---|---|
| **Download latest snapshot** | Queries the GitHub Contents API for the newest snapshot in the sshompitor repository and streams it to `data/`. Fast (typically a few seconds). **Production data only.** |
| **Create fresh snapshot** | Fetches all items from the live Marketplace API across all five categories and saves a new `full_items_{timestamp}.json`. Takes several minutes depending on environment. |

> **Stage environment:** the GitHub snapshot archive only contains Production data. When working on Stage, always use **Create fresh snapshot** to build a snapshot from the Stage API. The UI highlights this option as primary and shows a reminder banner when Stage is selected.

After either operation all page caches are cleared automatically and the page reloads.

#### Old snapshot cleanup

If multiple snapshots are present an expander lists older files with individual **Delete** buttons.

---

### Actors

**File:** `pages/2_Actors.py`

All actor-related curation in one place. Actors are the people and organisations credited on Marketplace items. "Contributor" is one of the roles an actor can have; the term "actor" is used throughout this page.

A shared **Load actors from API** bar at the top of the page fetches all ~9,000 actors from `GET /api/actors` and stores them in `st.session_state["actor_details"]`. This data is used across all four tabs and only needs to be fetched once per session.

---

#### Tab 1 — Browse

Displays every actor × item appearance from the snapshot as a filterable table (one row per actor–role–item combination). Built from `Util.getContributors()`.

**Filters** (applied together):

| Filter | Behaviour |
|---|---|
| Category | Multiselect; defaults to all |
| Role | Multiselect; defaults to all |
| Actor name | Case-insensitive substring match |
| Email | Case-insensitive substring match; available only after actor data is loaded |

**Metrics** show the number of rows, unique actors, and unique items matching the current filters. Results can be downloaded as CSV.

---

#### Tab 2 — Duplicates

Finds actors that share a `name` or `website` value. These are common when items are imported from different sources with slightly different contributor metadata.

##### Confidence scoring

If actor data has been loaded from the API, groups are classified by confidence:

| Confidence | Condition |
|---|---|
| **High** (★) | At least two actors in the group share the same non-empty email address |
| **Low** | Name match only; email is absent or different across actors |

High-confidence groups are shown first under **Suggested merges** and open expanded by default. Low-confidence groups appear under **Other name matches**.

##### Running the search

1. Select which field(s) to match on: `name`, `website`.
2. Optionally leave **Case-insensitive matching** checked (default) so values like `John Smith` and `john smith` (or with stray leading/trailing whitespace) are treated as the same value. Uncheck it to require an exact, case-sensitive match.
3. Click **Find Duplicates**.

Group headers show the original casing of the matched names (joined with `/` when they differ), even when the underlying match was case-insensitive.

Results are stored in session state and persist while navigating between tabs.

##### Merge behaviour

For each duplicate group an expander shows a table of actors with IDs, item counts, example item links, and emails (if loaded). Select which actor to keep using the **Keep this actor** selectbox, then click **Merge**.

The merge performs three steps to preserve all data:

1. `GET /api/actors/{id}` for every actor in the group to retrieve full records
2. `PUT /api/actors/{keep_id}` with a consolidated payload — email, website, and externalIds are **unioned** across all actors (keep actor's values take priority, falling back to the first non-empty value from the others); affiliations are kept from the keep actor only
3. `POST /api/actors/{keep_id}/merge?with={other_ids}` to transfer item associations and delete the merged actors

This three-step approach ensures no email addresses, websites, or external identifiers are silently lost when actors are merged.

The merge button is disabled after a successful merge (tracked in session state) so it cannot be clicked twice.

---

#### Tab 3 — Orphaned

Finds actors that exist in the Marketplace but are not credited on any item, and allows bulk deletion.

**Step 1 — Cross-reference with snapshot**

Actors absent from the local snapshot are flagged as candidates. A stale snapshot may produce false positives.

**Step 2 — Verify candidates via the live API**

For each candidate calls `GET /api/actors/{id}?items=true` to confirm the API itself considers the actor item-free. Requests run in configurable batches (default: 50 concurrent). Each request retries up to three times on 5xx, timeout, and connection errors. Actors that cannot be confirmed are classified as **uncertain** and excluded from deletion.

**Step 3 — Review and delete**

Confirmed orphans are shown in an editable table. All rows start unchecked; actors must be selected explicitly. Deletion calls `DELETE /api/actors/{id}?force=false` — actors affiliated with other actors are refused by the API and reported individually.

---

#### Tab 4 — Manual Merge

Merges two or more actors directly by ID, for duplicates the automatic Duplicates scan doesn't group together (e.g. names that differ by more than casing/whitespace).

**Find actor IDs** — a name search over the loaded actor data (or the snapshot, if actor data hasn't been loaded). Case-insensitive; supports `*` and `?` as wildcards (e.g. `kurzmeier*`, `*university*`). Without wildcards it's a plain substring match. Results show `id`, `name`, `email` (if loaded), and `website` so a curator can look up the IDs to merge.

**Merge by ID** — enter a **Keep** ID and one or more **Merge** IDs (comma-separated). Once both are filled in, the tool fetches each actor live via `GET /api/actors/{id}` and shows a preview table (name/email/website per ID) before anything happens. A confirmation checkbox — scoped to that specific keep/merge ID combination — must be ticked before the merge button is enabled. The merge itself calls the same `merge_actors()` used by the Duplicates tab (attribute consolidation, then `POST /api/actors/{keep_id}/merge`), so no data is lost and the button is disabled again once that combination has been merged.

---

### Item Duplicates

**File:** `pages/3_Item_Duplicates.py`

Two tabs: finding duplicate items, and merging two of them together.

#### Tab 1 — Find Duplicates

Scans snapshot items for shared values in selected fields.

Items without an `accessibleAt` URL are **excluded before the search**. These are stub or incomplete entries that cannot be meaningfully distinguished from each other by URL and would produce noise in the results. The count of excluded items is shown when the search runs.

**How to use:**
1. Select one or more categories.
2. Choose which fields to check: `label`, `description`, `accessibleAt`.
3. Click **Find Duplicates**.

Results persist in session state while navigating the page.

##### Summary table

Shows matched items with label, category, persistentId, and a clickable link to each item in the Marketplace. Downloadable as CSV.

##### Side-by-side comparison

Below the summary table each duplicate group appears as a collapsible expander. By default it shows snapshot data (label, category, ID, MP link). Click **Fetch live data from API** to load the full current record for each item and display them side by side. Each card shows label, status, source system, access URLs, contributor names, and description. Fetched data is cached per group in session state so it is not re-fetched when other controls are interacted with.

> **Note:** `accessibleAt` is a list value and cannot be used directly as a groupby key. It is converted to a comma-separated string for grouping purposes; the original list values are preserved in the data.

#### Tab 2 — Merge Items

Merges two items of the **same category** by `persistentId` (found via name lookup — typing an ID shows the matching snapshot label right below the field — or copied over from the Find Duplicates tab). Workflow steps are not supported — merge the parent workflow instead.

The Marketplace API has a dedicated per-category merge endpoint (`POST /api/{category-path}/merge?with={ids}`), but this tool deliberately does **not** use it. Instead it:

1. `GET`s both full item records and shows a **Keep vs. merge away** identity comparison (label, status, source, version, description, Marketplace link).
2. Shows a **"Choose what to keep"** section: for each of `contributors`, `properties` (keyword/concept/free-text values), `externalIds`, `accessibleAt` URLs, and `media`, a side-by-side pair of columns — :green[green, left] = already on the kept item, :blue[blue, right] = would be added from the merge-away item — each entry with its own checkbox, all checked by default (a full union). A value present on both items is shown once, on the green/keep side. Uncheck anything that shouldn't survive the merge. `relatedItems` get the same treatment, except any relation between the keep and merge item themselves is dropped before it's even shown (it would otherwise become a dangling self-reference once the merge item is deleted).
3. `label` always comes from the kept item (that's how you choose which one to keep); `description`, `version`, `source`, `sourceItemId`, and `thumbnail` use the kept item's value, falling back to the merged item's if the kept item has none — these aren't checkbox-driven since there's only one value to pick, not a list to curate.
4. Shows an **Outcome preview** — the fully expanded result of your checkbox choices plus the scalar fallbacks above — and a list of any other snapshot items that reference the merge-away item, so the curator sees exactly what will change before anything is written.
5. On confirmation: `PUT`s the curated payload onto the kept item, repoints `relatedItems` on every referrer found in step 4 (GET → swap the reference → PUT, same pattern as the Keywords page's `fix_item_keyword()`), then `DELETE`s the merged-away item. A referrer that fails to repoint is reported individually and does not abort the merge.

> **Warning shown in the UI:** if either item came from an automated harvest/ingest (check its `source` field), a future harvest run may re-create the item that was just merged away, since the toolkit has no way to tell an external harvester the two records were the same. If a duplicate keeps reappearing, fix the source feed rather than re-merging it every time.

The confirmation checkbox and merge button are scoped to the specific (category, keep ID, merge ID) combination, and — like the Actors Manual Merge tab — once a merge succeeds the tool shows the stored success message instead of re-fetching the now-deleted item on rerun.

---

### URL Checker

**File:** `pages/4_URL_Checker.py`

Checks whether URLs extracted from the snapshot are reachable.

#### Scope modes

| Mode | What is checked |
|---|---|
| `accessibleAt only` | The primary access URL(s) for each item |
| `All URLs in entry` | Every `http(s)` URL in any field (thumbnails, media, external IDs, descriptions, …) |

#### Workflow

1. Select categories and scope, then click **Extract URLs**. Scans the snapshot and reports unique URL count — no HTTP requests yet.
2. Set **Timeout** and **Parallel workers**, then click **Check URLs**.
3. Each URL receives an HTTP `HEAD` request. Servers returning `405` or `501` are retried with a streaming `GET` (body not downloaded).

#### Results

Sorted by items with the most broken links first, then by item, then broken-before-OK within each item. Filter to Broken only / OK only / All. The same URL appearing on multiple items is checked once and joined to all rows. Downloadable as CSV.

---

### Keywords

**File:** `pages/5_Keywords.py`

Audits and curates the `sshoc-keyword` vocabulary.

Unlike other Marketplace vocabularies (languages, disciplines, standards), the keyword vocabulary grows organically and is not formally governed. This leads to three classes of quality issues:

1. **Unused concepts** — keywords registered but not applied to any item
2. **Malformed labels** — keywords with leading spaces, no alphabetical characters, or excessive length
3. **Cross-vocabulary duplicates** — keywords whose labels already exist as concepts in a controlled vocabulary

The screenshot below illustrates all three classes at once:

![Keyword quality issues visible in the Marketplace editor](docs/keywords-quality-issues-example.png)

#### Loading data

Click **Load keyword concepts from API** to fetch all concepts in the `sshoc-keyword` vocabulary from `GET /api/concept-search?types=keyword`. Keywords are also extracted from the local snapshot via `Util.getAllProperties()` to compute usage counts.

---

#### Tab 1 — Unused

Concepts in the vocabulary not referenced by any item in the current snapshot.

**Filters:** free-text search on label or code; **Candidates only** toggle.

**Deletion:** tick `Delete?` for one or more rows. Selected rows are immediately highlighted in red below the table as a confirmation preview. Confirm the checkbox and click **Delete**. Calls `DELETE /api/vocabularies/sshoc-keyword/concepts/{code}?force=true`.

`force=true` is required because the API keeps a full version history of every item. Older revisions may still reference a keyword even after it has been removed from the current version; the API counts those historical references as active and refuses a plain delete. `force=true` removes the concept and purges all historical references.

Delete results persist visually until the next delete operation — a success message or per-concept error appears above the table and is not cleared by page reruns. **403 Forbidden** errors are explained explicitly: the concept may be a system/protected entry or the account may lack vocabulary administrator privileges.

---

#### Tab 2 — In use

All concepts appearing on at least one item, sorted by usage count descending.

**Quality filters** (independent, combinable):

| Filter | Detects |
|---|---|
| Starts with space | Labels like `" machine learning"` that sort and display incorrectly |
| No letters | Labels composed entirely of numbers or symbols |
| Longer than N characters | Labels that are suspiciously long — often pasted description text |

**Near-duplicate detection**

Below the quality filters a **Near-duplicate keywords** section groups concepts that are identical after normalisation:

- Labels are lowercased and stripped
- Hyphens, underscores, slashes, and backslashes are replaced with spaces
- Multiple spaces are collapsed

This catches cases like `Machine Learning` / `machine learning` / `machine-learning` which are three separate vocabulary entries for the same concept.

Each group of near-duplicates appears in an expander showing all variants with their usage counts. A **Merge variants** control inside the expander lets you:

1. Select which variant to keep
2. Review the list of affected items (items that use one of the non-kept variants)
3. Confirm and click **Merge**

The merge re-points all affected items to the kept concept (using `GET → modify property → PUT` per item) and then deletes the merged variants with `force=true`. Both steps are logged in the session log.

---

#### Tab 3 — Duplicates in other vocabs

Finds **used** keywords whose label also exists as a concept in another Marketplace vocabulary. A common high-volume case is language names entered as keywords:

![Keywords entered as language names](docs/keywords-as-languages-example.png)

Only used keywords are shown here — unused ones belong in Tab 1 and cannot be fixed here because fixing requires updating item properties.

**How to use:**
1. Click **Load all concepts from API (~15 000)** to fetch all concepts across all vocabularies.
2. The tool performs a case-insensitive label match between `sshoc-keyword` concepts and other-vocabulary concepts.
3. Use the concept type filter to narrow results.
4. Select a keyword → target concept pair, review the affected items, and click **Fix N item(s)**.

For each affected item: GET the full payload, replace the matching property (type `keyword`, matching concept code) with the target type and concept, PUT the modified payload back.

---

#### Tab 4 — All concepts

Complete `sshoc-keyword` vocabulary with usage counts. Searchable by label or code.

---

### Session Log

**File:** `pages/6_Session_Log.py`

Records every significant action and API write call made with this installation, persisted to disk (`logs/session_log.jsonl`) — it survives closing the browser and restarting the app.

![Session Log page](docs/session-log-example.png)

#### What is logged

| Entry type | Examples |
|---|---|
| `action` | Login, snapshot download, actor fetch, orphan verification summary |
| `api` | `DELETE /api/actors/{id}`, `POST /api/actors/{id}/merge`, `PUT /api/{path}/{id}`, `DELETE /api/vocabularies/…/concepts/{code}` |

Every API write entry records: timestamp, HTTP method, full URL, request summary, HTTP status code, and an abbreviated response. API error responses in the standard JSON envelope (`{"status": 403, "error": "Forbidden", "path": "…"}`) are automatically collapsed to a readable `Forbidden — path: /api/…` form in the Response column.

#### Column order

Columns are ordered to surface the most useful information first: `time → type → ok → method → status → response → description → url → request`. The wide URL and request body columns are at the right so they do not crowd out the response.

#### Filters

Filter by entry type (action / api), result (ok / failed), and free-text search across description, URL, request, and response.

#### Export

- **Export filtered as CSV** — the current filtered view
- **Export full log as JSON** — the complete unfiltered log as a JSON array

**Clear log** permanently deletes `logs/session_log.jsonl`. This cannot be undone — export first if you need a record. Short of that, the log persists indefinitely: across browser sessions, server restarts, and (since it's one shared file) across every session run against this installation.

#### Sidebar indicator

Every page shows a `Session log: N entries` count in the sidebar once entries exist.

---

## Data freshness

The local snapshot is a JSON export of the full Marketplace catalogue. A **data age badge** appears in the sidebar of every page:

- **Green** — snapshot is less than 3 days old
- **Orange** — snapshot is 3 or more days old

If no snapshot is found when navigating to any tool page, a full-page prompt offers GitHub download or API creation to recover without navigating away.

A stale snapshot affects: orphaned actor detection, unused keyword detection, actor and item duplicate detection, and cross-vocab duplicate coverage. Refresh before starting a curation session if the snapshot is more than a day old.

> **Stage environment:** the GitHub archive contains Production snapshots only. When working on Stage, use **Create fresh snapshot** on the Data Source page.

---

## API reference

All write operations target the environment selected at login.

| Operation | Method | Endpoint | Notes |
|---|---|---|---|
| Login | POST | `/api/auth/sign-in` | Returns bearer token in `Authorization` header |
| List actors | GET | `/api/actors?perpage=100&page={n}` | ~95 pages for full list |
| Get actor | GET | `/api/actors/{id}` | Full record including externalIds, affiliations |
| Check actor items | GET | `/api/actors/{id}?items=true` | Authoritative orphan check |
| Update actor | PUT | `/api/actors/{id}` | Used before merge to consolidate attributes |
| Merge actors | POST | `/api/actors/{id}/merge?with={ids}` | Comma-separated IDs to absorb |
| Delete actor | DELETE | `/api/actors/{id}?force=false` | Refused if actor has affiliations |
| Get item | GET | `/api/{category-path}/{persistentId}` | Full item payload |
| Update item | PUT | `/api/{category-path}/{persistentId}` | Requires full item payload |
| Delete item | DELETE | `/api/{category-path}/{persistentId}` | No `?force=`; used to remove the merged-away item after an items merge |
| List keyword concepts | GET | `/api/concept-search?types=keyword&perpage=100` | ~27 pages |
| List all concepts | GET | `/api/concept-search?perpage=100` | ~152 pages; all vocabularies |
| Delete concept | DELETE | `/api/vocabularies/{vocab}/concepts/{code}?force=true` | Also clears historical item-version references |
| Fetch category items | GET | `/api/{category-path}?perpage=20&page={n}` | Used when creating a fresh snapshot; small pages reduce per-request timeouts |

### Concept code URL encoding

Concept codes arrive from the API as plain JSON string values — in JSON `+` is a literal plus sign, not a space. Before building a DELETE URL the toolkit passes the raw code through `urllib.parse.quote(code, safe="")` without any prior decoding step. This encodes `+` as `%2B`, `%` as `%25`, and other special characters accordingly, so the server decodes the path back to the original code string stored in the database. An earlier approach used `unquote_plus` before `quote`, which incorrectly treated `+` as a space and produced `%20` in the URL, causing 404 responses for any concept whose code contains a plus sign (e.g. `10+languages`).

### Category path mapping

| Snapshot `category` | API path segment |
|---|---|
| `tool-or-service` | `tools-services` |
| `training-material` | `training-materials` |
| `dataset` | `datasets` |
| `publication` | `publications` |
| `workflow` | `workflows` |
| `step` | `steps` |

---

## Library reference

### `lib/environments.py`

Defines the two target environments (Production and Stage) with their API base URLs and frontend URLs. Add new entries here to make additional environments selectable at login.

### `lib/auth.py`

`try_login(username, password, env_name)` — posts credentials to `/api/auth/sign-in` and returns the bearer token string, or `None` on failure.

`require_login()` — called at the top of every page. Redirects to the login page and stops rendering if the session is unauthenticated.

### `lib/mplib.py`

`get_util()` — returns a cached `Util` instance (`@st.cache_resource`). Tries the pip-installed `sshmarketplacelib` package first; falls back to a sibling `../sshompitor/` clone if not installed. Using `cache_resource` means the snapshot DataFrame is loaded once per server process and shared across browser sessions.

### `lib/api.py`

All functions that communicate with the live Marketplace API. Every write function calls `log_api()` automatically.

| Function | Description |
|---|---|
| `fetch_all_actors(api_url, bearer)` | Paginated `GET /api/actors`; returns DataFrame `[id, name, email, website, item_count]` |
| `verify_orphans(ids, api_url, bearer, batch_size)` | Batched concurrent `GET /api/actors/{id}?items=true` with retries; returns `dict[id → bool\|None]` |
| `delete_actor(actor_id)` | `DELETE /api/actors/{id}?force=false` |
| `get_actor(actor_id, api_url, bearer)` | `GET /api/actors/{id}`; returns full actor record including externalIds and affiliations |
| `get_current_user(api_url, bearer)` | `GET /api/auth/me`; returns the logged-in account including its `role`, used by `lib.auth.is_moderator()` to gate destructive buttons |
| `_consolidate_actor_payload(actors)` | Merges email, website, and externalIds from a list of actor records; used before merge to preserve all attributes |
| `merge_actors(keep_id, merge_ids)` | 3-step: GET all actors → PUT consolidated attributes → `POST /api/actors/{id}/merge?with={ids}` |
| `get_item(category, persistent_id, api_url, bearer)` | `GET /api/{path}/{id}`; returns full item dict |
| `put_item(category, persistent_id, item_data, api_url, bearer)` | `PUT /api/{path}/{id}`; logs the call |
| `delete_item(category, persistent_id, api_url, bearer)` | `DELETE /api/{path}/{id}` (no `?force=`, unlike actors/concepts) |
| `fix_item_keyword(category, persistent_id, old_code, new_type, new_concept, api_url, bearer)` | GET item → replace matching keyword property → PUT back |
| `consolidate_item_payload(keep_item, merge_item)` | Unions contributors, properties, externalIds, accessibleAt, media, and relatedItems (dropping self-references) from two item records into a PUT-ready payload |
| `repoint_related_item(category, persistent_id, old_pid, new_pid, api_url, bearer)` | GET item → swap a `relatedItems` reference from `old_pid` to `new_pid`, deduping/dropping self-references → PUT back |
| `merge_items(keep_category, keep_pid, merge_category, merge_pid, referrers, api_url, bearer, payload=None)` | GET both → PUT `payload` (or `consolidate_item_payload`'s result if `payload` is omitted) onto keep item → repoint every referrer → DELETE merge item. The Merge Items UI always passes its own checkbox-curated `payload`. |
| `fetch_all_keyword_concepts(api_url, bearer)` | Paginated `GET /api/concept-search?types=keyword` |
| `fetch_all_concepts(api_url, bearer)` | Paginated `GET /api/concept-search` (all types and vocabularies) |
| `delete_concept(concept_code, vocab_code)` | `DELETE /api/vocabularies/{vocab}/concepts/{code}?force=true`; URL-encodes the concept code |
| `create_snapshot_from_api(api_url, bearer, data_dir, env_label)` | Fetches all items from all 5 categories, saves `full_items_{ts}.json` and a sidecar `full_items_{ts}.meta.json` |

### `lib/snapshot.py`

`get_latest_snapshot_info()` — returns `(path, age_timedelta, snapshot_datetime)` for the newest snapshot.

`fetch_latest_from_github()` — downloads the newest snapshot from the sshompitor repository; writes a sidecar meta file recording `source: github, env_label: Production`.

`require_snapshot()` — stops page rendering with a recovery UI if no snapshot is present.

`render_data_status()` — renders the age badge, GitHub refresh button, and session log count in the sidebar.

`read_snapshot_meta(path)` — reads the sidecar `*.meta.json` for a snapshot file; returns `{}` if not found.

### `lib/logger.py`

Maintains a persistent log at `logs/session_log.jsonl` (one JSON object per line), gitignored since entries can include account details and API responses.

`log_action(description, ok)` — records a high-level event.

`log_api(method, url, description, status, request, response, ok)` — records an HTTP API call. API error responses in standard JSON envelope format are automatically formatted into a readable summary.

`get_log()` / `get_log_df()` — read the full log back (list of dicts / DataFrame).

`clear_log()` — permanently deletes the log file.

`get_log()` / `get_log_df()` — retrieve the current log as a list or DataFrame.

---

## Architecture

### Data flow

```
GitHub (SSHOC/sshompitor)
        │  fetch_latest_from_github()  →  writes .meta.json (source=github, env=Production)
        ▼
data/full_items_*.json  ◄── also created by create_snapshot_from_api() (writes .meta.json with env label)
        │
        ▼
sshmarketplacelib.Util  (loaded once, cached via st.cache_resource)
        │
        ├── getContributors()            ──►  2_Actors (Browse + Duplicates + Orphaned)
        ├── _load_snapshot()             ──►  3_Item_Duplicates, 4_URL_Checker
        ├── getDuplicates()              ──►  3_Item_Duplicates
        ├── getDuplicatedActorsWithItems ──►  2_Actors (Duplicates tab)
        └── getAllProperties()           ──►  5_Keywords

Live Marketplace API  ◄──►  lib/api.py  ◄──►  all write operations + read-back for side-by-side compare
                                  │
                                  └── lib/logger.py  ──►  6_Session_Log
```

### Page structure

| File | Page | Role |
|---|---|---|
| `app.py` | Login | Entry point; redirects to Data Source after login |
| `pages/1_Data.py` | Data Source | Landing page; snapshot management with environment labels |
| `pages/2_Actors.py` | Actors | Browse contributions; find and merge duplicates; remove orphaned actors; manually merge by ID |
| `pages/3_Item_Duplicates.py` | Item Duplicates | Item field duplicate detection with side-by-side live comparison; merge two items by persistentId |
| `pages/4_URL_Checker.py` | URL Checker | Concurrent URL reachability check |
| `pages/5_Keywords.py` | Keywords | Keyword vocabulary curation including near-duplicate merge |
| `pages/6_Session_Log.py` | Session Log | Audit log with export |

### Session state

| Key | Type | Set by | Used by |
|---|---|---|---|
| `authenticated` | `bool` | Login | All pages |
| `bearer` | `str` | Login | All API calls |
| `username` | `str` | Login | Data Source display |
| `env` | `dict` | Login | All pages |
| `current_user` | `dict \| None` | Login | `lib.auth.is_moderator()` / `current_role_label()` — gates destructive buttons on every page; `None` if `GET /api/auth/me` failed (fails closed) |
| `actor_details` | `DataFrame` | Actors page | Actors (all four tabs) |
| `actor_dup_summary` | `DataFrame` | Actors — Duplicates tab | Actors — Duplicates tab |
| `actor_dup_full` | `DataFrame` | Actors — Duplicates tab | Actors — Duplicates tab |
| `actor_dup_id_to_name` | `dict` | Actors — Duplicates tab | Restores original-case names for display after case-insensitive matching |
| `merged_groups` | `dict` | Actors — Duplicates tab | Disables merge buttons after merge |
| `expander_open` | `dict` | Actors — Duplicates tab | Keeps expanders open after merge |
| `orphan_verified` | `dict` | Actors — Orphaned tab | Verification results cache |
| `manual_merged` | `dict` | Actors — Manual Merge tab | Disables merge button after merge, keyed by keep/merge ID combination |
| `item_dup_result` | `DataFrame` | Item Duplicates | Item Duplicates (survives fetch-button reruns) |
| `item_dup_props` | `list` | Item Duplicates | Item Duplicates |
| `item_dup_filtered` | `int` | Item Duplicates | Count of excluded no-accessibleAt items |
| `fetched_items` | `dict` | Item Duplicates — Find Duplicates tab | Live API data per duplicate group |
| `item_merged` | `dict` | Item Duplicates — Merge Items tab | Disables merge button after merge, keyed by category/keep/merge persistentId combination |
| `url_check_input` | `DataFrame` | URL Checker | URL Checker |
| `url_check_results` | `DataFrame` | URL Checker | URL Checker |
| `keyword_vocab` | `DataFrame` | Keywords | Keywords (all tabs) |
| `all_concepts_cache` | `DataFrame` | Keywords — Tab 3 | Cross-vocab comparison |
| `kw_delete_status` | `dict` | Keywords — Tab 1 | Persists delete result across reruns |

> The session log itself is **not** a session_state key — it's persisted to `logs/session_log.jsonl` on disk (see [`lib/logger.py`](#libloggerpy)), independent of any browser session.

### Caching

| Decorator | Used for | Invalidated when |
|---|---|---|
| `@st.cache_resource` | `Util()` instance (loads the snapshot once per server process) | `st.cache_resource.clear()` after snapshot refresh |
| `@st.cache_data` | Per-page derived DataFrames (appearances, unique actors, keyword extraction, snapshot load) | `st.cache_data.clear()` after snapshot refresh, or `.clear()` on the specific function after a merge |

---

## Caveats & known limitations

**Snapshot staleness.** All analysis that reads from the snapshot reflects the state of the Marketplace at snapshot time. Changes made through the toolkit or by other users will not be visible until the snapshot is refreshed.

**Concept delete and item history.** The API maintains a full version history of every item. Older revisions may still reference a keyword that was removed from the current version. The API counts those as active and refuses a plain delete — this is why `force=true` is required.

**403 on concept delete.** Some vocabulary concepts are system entries or are in a protected state. The API returns HTTP 403 regardless of `force=true`. The error message displayed identifies this case explicitly. These concepts cannot be deleted via the toolkit.

**Concurrent item edits.** The keyword fix and near-duplicate keyword merge workflows use GET-modify-PUT. If another user edits the same item concurrently their changes will be overwritten. Use during low-traffic periods and on Stage before Production.

**Merge only transfers item associations.** `POST /api/actors/{id}/merge` moves item credits to the kept actor but does not copy email, website, or external identifiers. The toolkit works around this by consolidating attributes in a PUT call before the merge, but only the kept actor's affiliations are retained.

**`force=false` on actor deletion.** The API refuses to delete an actor that is the affiliation target of another actor. Such actors must have their affiliation relationship removed first, or be merged into the affiliated actor.

**Item PUT requires full payload.** Every item update sends the complete object returned by GET with only the target fields modified. Fields absent from the GET response will be absent from the PUT and may be cleared server-side.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'sshmarketplacelib'`**  
Run `pip install -r requirements.txt`. If pip cannot reach GitHub, install manually:  
`pip install git+https://github.com/SSHOC/sshompitor.git#egg=sshmarketplacelib`.  
As a last resort, clone sshompitor into `../sshompitor/` — `lib/mplib.py` finds it automatically.

**No snapshot found**  
Run `python3 setup.py` to create the `data/` directory, then use the Data Source page to download a snapshot.

**Login fails with a network error**  
The selected API server is unreachable. Check your connection and verify the environment is online. The Stage environment is occasionally offline for maintenance.

**GitHub download re-downloads the same file**  
The local filename must exactly match the GitHub filename (including the Unix timestamp). If the file was renamed the comparison fails and the file is re-downloaded.

**Orphan verification produces many `uncertain` results**  
The API is under load. Lower the batch size (try 20–30) and re-run. Uncertain actors are excluded from deletion.

**Concept delete fails with 403**  
This is a server-side permissions decision. The concept may be a system entry, or the logged-in account may lack vocabulary administrator privileges. The error is shown explicitly in the UI and in the Session Log.

**Concept delete returns 404 after the URL encoding fix**  
Verify the concept still exists in the vocabulary — it may have already been deleted in a previous session or by another user.

**Item PUT returns HTTP 422 after keyword fix**  
The target concept's `type_code` is not valid for this property type in the Marketplace schema. Review the type in the cross-vocabulary match.

**After merging actors the group still appears in results**  
The snapshot has not been refreshed. The merge is complete on the server. Use the Data Source page to download a fresh snapshot, then re-run duplicate detection.

**Session log is empty**  
The log is in-memory only and resets when the server restarts or **Clear log** is clicked. Export before closing the browser if you need a record.
