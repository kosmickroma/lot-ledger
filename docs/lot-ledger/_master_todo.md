---
created: 2026-05-07
type: master-todo
status: living-document
---

# LotLedger — Master TODO

> **Single source of truth.** Click priority checkboxes during calls to triage. Add notes as sub-bullets. Move done items to the **Done (recent)** section at the bottom.
>
> Format per item: task description on top, three priority checkboxes (HIGH / 2nd / Defer), then any sub-bullets for notes/context. Click whichever priority applies; Obsidian saves the click instantly.

---

## ⚡ In flight right now

- [ ] **Neighborhood overlay POC — TIGER block groups as gold-line toggle** (client validation BEFORE building bigger vision)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - **Branch:** `feat/poc-neighborhood-overlay` (off develop) — gold lines shipped, preview-deployed (rev 00066) 2026-05-08
  - **🛑 PAUSED 2026-05-08 — NEEDS DEBUG before next iteration:** Re-deployed branch to preview after pill commits (`d1ee92c` enrich + `6e28bf9` centroid pills) — NBHD toggle renders but **behavior regressed** vs the gold-lines-only preview from earlier 2026-05-08. Last good preview was rev 00066 (pre-pill). HEAD is `6e28bf9`. Working tree has uncommitted deletion of `frontend/tx_block_groups.geojson` (boundaries moved to backend endpoint — likely fine but verify). Resume: reproduce on preview URL, diff vs rev 00066 commit (`dfa5ca1`), check browser console + `/api/neighborhoods/boundaries` response shape after the enrichment commit.
  - **Original plan:** [[plan_neighborhood_overlay_poc.md]]
  - **Bigger-picture brainstorm (post-first-look):** [[plan_neighborhood_choropleth_brainstorm.md]] — captures the multi-layer vision (choropleth → click-to-analyze → AI-driven seed auto-draw), 10 ranked stat options, defensible pitch language, phasing roadmap
  - **POC iteration 2 spec (ready for Copilot):** [[copilot_neighborhood_choropleth_poc.md]] — transparent purple fills by median appraised value + hover tooltip. Stays on same POC branch. NOT merging to develop until Mike validates.
  - **What's next (only after Mike sees iteration 2):** Phase 2A multi-stat dropdown, Phase 2B click-to-analyze, Phase 3 Census Tracts coarser tier, Phase 4 ACS demographic join — all enumerated in the brainstorm doc

- [x] **Propelio per-address comp integration in lot-ledger-pro** — COMPLETED 2026-05-11 (panel rebuild superseded original chunks 4-6 plan)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Status 2026-05-11:** Original chunks 1-3 shipped 2026-05-08. The "Chunks 4-6 deferred" from the old plan got REPLACED by the 2026-05-10/11 panel-rebuild initiative (parcel-detail panel with photos, agent contacts, schools, full remarks). Now shipped to dev via lot-ledger/develop (`348c978`). The old chunk-5 quota counter is still open but moved to its own item below; chunk-6 radius slider remains deferred (UI now has month + radius numeric inputs in the propelio card).
  - **Reframe:** Mike said the data we have (Redfin) is wrong because TX is non-disclosure → Redfin sold_price is an estimate, not actual close. Propelio uses their broker MLS license to get real closed prices. Validated end-to-end with Williamsburg Rd test ($723,333 ARV matches Viktor's reference). Mike's account = his own paid login, NOT us scraping a third-party — different legal posture than my original brief assumed.
  - **Mike also said: drop Viktor's comp-scoring algorithm.** He's a flipper/teardown buyer; the algorithm's living-area filter (±40%) excludes the comps he most needs (small teardown's relevant comp is the new build that replaced one nearby). Wants raw data, all comps same color, manual selection.
  - **Standalone Propelio CLI:** `/home/kk/projects/clients/real-estate-comps/` — moved out of Downloads, .env migration done, ready to push to https://github.com/kosmickroma/ProLio.git (KK pushes, commands provided; first commit will be clean .env-based version with secrets out of code)
  - **Integration target repo:** `lot-ledger-pro` (https://github.com/kosmickroma/lot-ledger-pro.git) — KK to push current lot-ledger `develop` branch there as the playground; lot-ledger-pro remote already added on existing checkout
  - **Build approach:** I write Copilot spec, Copilot codes, I review (per `feedback_copilot_handoff.md`)
  - **What gets built:**
    - `GET /api/propelio/by-address?address=...` returns raw comp list + rich subject detail (transfer history, owners, loans, valuation, tax)
    - Cache layer 7d TTL, address-keyed, with `balance` quota logging
    - Frontend: existing search → flyTo → "Get Comps" button on parcel popup → fire Propelio call → all ~40 comps render same-color → subject popup gains the Propelio rich data
    - Quota counter in header (X / 500 used this month, read from response `balance` field)
    - Redfin layer stays as broad spatial firehose, Propelio is precision per-address tool
  - **Quota math:** each search ~1 CMA credit (currently 2 in Viktor's code due to wasteful probe; we strip it). Cache hit = 0 credits. Validated by watching `balance` field across calls during cache-layer implementation.
  - **What we drop:** `comp_engine.py` (scoring), `output.py` (Excel), the 4 known bugs in comp_engine (moot — file not vendored). All survive in standalone ProLio repo for future Excel/lender-deliverable use case.
  - **Original deep-dive brief (now partially obsolete):** `/home/kk/.claude/plans/zippy-swimming-iverson.md` — useful context but the "lift algorithm, drop data path" framing is INVERTED in this final plan
  - **Sequencing:** can start on Copilot spec now (don't need lot-ledger-pro pushed to write it); execution after KK pushes both repos.
  - **Chunk 1 status (2026-05-08 evening):** ✅ DONE.
    - `api/propelio/cache.py`, `api/propelio/routes.py`, `api/propelio/scraper.py` (probe stripped) all in place at `/home/kk/projects/clients/lot-ledger-pro/`
    - `app.include_router(propelio_router)` wired in `api/main.py:1114`
    - DB tables `propelio_cache` + `propelio_quota_log` auto-created on import (verified)
    - `.env` populated with: DB_*, SESSION_SECRET, AUTH_COOKIE_SECURE=false, BOOTSTRAP_DEV_*, PROPELIO_USERNAME, PROPELIO_PASSWORD
    - Server boots clean (`Application startup complete.` confirmed)
    - Route registered (`/api/propelio/by-address` shows in OpenAPI)
    - Scraper proven end-to-end: 42 real comps for Williamsburg
    - Auth gate blocks anon curl with 401 (correct security posture); end-to-end HTTP test requires browser login
  - **To test in browser tomorrow:** start uvicorn via `.venv/bin/uvicorn api.main:app --reload --host 0.0.0.0 --port 8000`, login at http://127.0.0.1:8000 with prod credentials (DB is shared with prod), then visit `http://127.0.0.1:8000/api/propelio/by-address?address=4044+Williamsburg+Rd%2C+Dallas%2C+TX+75220` — should return JSON with subject + 42 comps; second visit returns instantly with `cached: true`.
  - **Chunk 2 + Chunk 3 status (2026-05-08 ~23:15 CDT):** ✅ DONE. Full search→Propelio→pin pipeline visible in browser.
    - `frontend/style.css` got `.propelio-pulse-marker` (cyan #06b6d4 pulse glow, mirrors `.saved-parcel-glow` keyframe pattern with different color) + popup styles
    - `frontend/map.js` got `propelioCompLayer`, `firePropelioFetch()`, `_propelioBuildPopup()`, hooks in both `selectSuggestion` (typeahead path) and `doSearch` (free-form Go button path); response also stashed on `window._propelioLast` for future chunks
    - `api/propelio/routes.py` polished: `subject.neighborhood` falls back to `parcel_enrichment.subdivision`, `lot_size`/`sqft`/`year_built`/`lat`/`lon` similarly. Empty-CMA case (Propelio resolved address but no comps in window) now returns 200 with `comps: []` + `warning` field instead of 503.
    - Verified via screenshot 2026-05-08 23:16 CDT: search "4044 Williamsburg Rd, Dallas, TX 75220" → 42 cyan pulse pins around Glenridge Estates → clicked one → popup shows "3947 Beechwood Ln · $1,675,000 · for_sale · 4337 sqft · MLS 20990065 · Glenridge Estates 2" — exact match to JSON comp index 6
  - **Still TODO (Chunks 4–6, deferred):**
    - Chunk 4: enrich existing parcel popups with Propelio subject data (transfer history, owners, loans, valuation, tax) — data is in `window._propelioLast.subject` already, just needs UI surface
    - Chunk 5: quota counter chip in header reading `balance` field (note: `_extract_balance` is currently returning null because Propelio's quota field hasn't been located in the response — needs to grep the raw payload to find the right key)
    - Chunk 6: optional radius slider (defer — not blocking)
    - Polish: `transfer_history` and `raw` are still null in the response because the scraper doesn't surface those in `subject.extra` yet; minor scraper tweak when we want them
    - Polish: same-color cyan pins are 1 color; later we may want a status-color variant (sold/active/pending) but Mike's preference is "all same color, manual selection" so this stays unless feedback changes

- [x] **🌅 FIRST THING TOMORROW MORNING — test username-only auth on develop deploy + update Mike** ← KK going to bed 2026-05-07 ~22:45 EDT
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Branch merged + pushed: `feat/username-only-auth` → `develop` (merge commit `56860e7`)
  - Develop auto-deploys to `lot-ledger-dev` Cloud Run service (`https://lot-ledger-dev-505466930182.us-central1.run.app`) via the `cloudbuild.yaml` GitHub trigger
  - **Test path on the dev URL once auto-deploy completes:**
    1. Existing email login still works (sign in with the email you've always used)
    2. Open Manage Users → Add New User → username + temp password ONLY (do NOT click "+ Add email (optional)") → succeeds
    3. New row in admin table shows username + em-dash in Email column
    4. Sign out, sign in as the new user with **just their username** + temp password → lands on force-change-password screen with all 3 fields empty (autofill stomp clears them)
    5. Set a new password → can use the app
    6. Try to create a duplicate username → 409 "Username or email already exists"
    7. Reset Pw / Disable / Delete on the email-less user → all work
  - **Then update Mike** — let him know he can now create user accounts (e.g. for his VAs) with just a username, no email needed; he picks the temp password and shares it with them via secure channel
  - Already preview-verified on `lot-ledger-preview` tonight (KK confirmed working before bed)
  - Schema migration step is `users_email_drop_not_null` — runs on app startup, idempotent

- [x] **Propelio comp harvest — two-button search split + deep-pull beyond ~100-cap** ✅ COMPLETED 2026-05-13/14
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Shipped:** Get Comps (gold sticky map button) fires the 3-pass Quick Sweep at 1mo/2mo/3mo × 0.25-1.0mi SFR, polygon-constrained via `geojson` param. Custom Search (sidebar) runs manual scrapes with user-set months + range. Marathon scraper (`feat/marathon-campaign` branch) handles bulk-coverage harvest with 6-pass tightened config + Dallas-only + south-to-north + terminal-failure classification for permanent errors.
  - **Cap-bypass discovery:** Propelio's `geojson` polygon param DOES work as a spatial primitive (memory `project_propelio_cap_findings_2026_05_13.md`). The fan-out-across-filter-combos plan from the original spec was replaced by geojson polygon constraint — cleaner architecture, fewer API calls.
  - **Still open (separate item):** "101 total in window" chip wording — still untouched, see Quick wins section below.
  - **Memory:** `project_propelio_comp_harvest.md`, `project_propelio_cap_findings_2026_05_13.md`

- [ ] **CSV export rebuild — Propelio columns + filter-state-aware snapshot** (ACTIVE 2026-05-11; client said critical)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Pulled forward from UI/UX backlog** to active In-Flight per KK 2026-05-11. CSV is the next big chunk after the panel work shipped to dev.
  - **Core asks captured:** (a) all visible data lands in CSV, (b) workspace = the snapshot of what's downloading (filter-state-aware), (c) NO many-to-many — 1:1 parcel:matched-comp or 1:N parcel:multiple-rows is fine, (d) put ALL Propelio columns far-right (don't intermix with existing fields), (e) follow the existing R.F. inline pattern for how comp data joins per parcel.
  - **Current state diagnosis:** CSV has 85 columns, ZERO Propelio columns today, filter state ignored at export. R.F. sold-comp is inline-per-parcel via 10 cols. Propelio data lives in `propelio_cache` table keyed by polygon hash, never joined to the job at export time.
  - **Plan deep-dive ongoing 2026-05-11** — comprehensive proposal being drafted, including column ordering, MLS approach, filter-state plumbing. UX research agent running for industry conventions. See conversation thread + memory for details.
  - **Related deferred items to fold into this chunk:** CSV Account Number column (low-priority addition), CSV Seed Target column investigation, `_google_maps_link` owner-state bug. All listed elsewhere in this todo — bundle.
  - **Branch:** new branch off `lot-ledger/develop` (current state is `348c978`). KK staying on lot-ledger repo per 2026-05-11 decision.

- [ ] **GCP migration to Mike's project** — solo task, ~2 hrs (Phases A-D from `[[mike_gcp_handoff_plan]]`)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Blocked until: Mike's GCP project ID confirmed received, password-reset email decision made
  - Phase A: enable APIs, Cloud SQL provision, PostGIS extension, Artifact Registry repo, 3 IAM grants
  - Phase B: pg_dump both DBs (lotledger + lotledger_sessions), restore to Mike's instance
  - Phase C: deploy Cloud Run, set env vars (no `BOOTSTRAP_DEV_*` on prod), Cloud Build trigger from main
  - Phase D: smoke test together, hand Mike the URL

---

## 🧹 Quick wins (small, knock out anytime)

- [ ] **Remove `console.debug("[restoreFilterState] restored", ...)` log**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - 5 min cleanup, was added for testing

- [ ] **Fix `api/auth.py:342` `.strip()` bug on `BOOTSTRAP_DEV_PASSWORD`**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - 5 min, latent bug; trailing whitespace gets baked into hash. Add `.strip()` to match the email lines that already do
  - Only matters if user table is ever wiped and seeded fresh

- [ ] **Active listing price bubbles** (red bubble, black text, zoom-gated)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - 2-3 hrs
  - Depends on marker decoupling (filtered-out parcels' bubbles also disappear today; need decoupled overlay)

- [ ] **Cmd+Shift+S keyboard shortcut for Save Area**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Power-user shortcut, parallel to existing Cmd+S for snapshot

- [ ] **"Sold Within: clear" → all-time view** (currently uses 9999-day workaround)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Backend should handle null differently from 9999

- [ ] **Sparse-rural sales_count test — is 101 actually saturation?** (5-min verification, BEFORE building the comp harvest deep-pull)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Pull comps for an address you'd expect to have far fewer than 100 closings (rural Hunt County back-corner, brand-new subdivision, etc.)
  - If `sales_count` reports a low number (e.g. 12) → 101 is saturation, deep-pull justified
  - If `sales_count` stays 101 even there → field is broken/static, re-diagnose before designing harvest
  - Result determines whether the Propelio comp harvest item moves forward as planned or pivots

- [ ] **Fix "101 total in window" chip wording** (bundle with comp harvest work)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - The Propelio CMA chip line "X inside · Y outside · Z fetched · 101 total in window" reads as a lie when the 101 is always saturation
  - Two options: drop the "total in window" segment entirely, OR render "100+" when `sales_count == 101`
  - Frontend-only, single string edit at `map.js:3406` (lot-ledger-pro)
  - Don't ship in isolation — fold into the comp harvest UI work so the chip stays consistent with the new Comps button output

---

## 🚀 Workspace pivot — next waves

- [ ] **v0.5 — Workspace materialization rule** (auto-create workspace on first persistent action)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Rule: drawing alone is exploration; saving a parcel/tagging/clicking Save Area auto-creates a workspace silently
  - Two flows (address-first + polygon-first) both materialize via the same trigger
  - Default workspace name = first-saved-parcel address or "Untitled — May 7 14:30"
  - See full spec in `[[to_do_05_08]]` "Workspace Materialization Rule" section
  - Includes: seed-only render mode (workspace with NULL polygon, just a parcel)

- [ ] **v1 collaboration model — decision deferred**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Two options on the table: (A) team-shared workspace writes (refactor PUT/DELETE to be role-gated) OR (B) fork-style writes ("Save as my copy" endpoint, recipients clone instead of edit)
  - **Don't decide until Mike + VAs use v0 share links for a real research session — let usage signal what they need**
  - See `[[to_do_05_08]]` "v1 collaboration model" section for full comparison

- [x] **v1 — Power user + user role tiers** (R3)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Add `power_user` and `user` to existing `developer/owner/member` text+CHECK constraint
  - Update admin endpoint allowlists at `api/main.py:1174,1187,1189` for new roles
  - Audit existing users: any `member` role → bump to `power_user` or `owner` BEFORE migration to avoid silently losing CSV export access
  - See R2/R3 in `[[to_do_05_08]]` Risk Analysis

- [ ] **v1.5 — Public view-only toggle** (Notion-style)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Per-area `is_publicly_viewable` boolean already in schema (added in v0)
  - Need: UI toggle on the area row (owner-only edit), backend allows anonymous when toggle is on
  - Anonymous viewer renders read-only mode (no save, no tag, no CSV)
  - Two share buttons: `[Copy team link]` + `[Copy public link]` (the latter only enabled when toggle is on)

- [ ] **v1.5 — session_tags PK redesign for per-user tags** (R19)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - **HARD BLOCKER** for two-way collaboration on tags
  - Current PK excludes user_id; two users can't independently tag same parcel
  - Need: add `user_id` to PK, backfill existing rows, update upsert conflict target

- [ ] **v2 — Snapshot share links** (separate `snap_<id>` from `area_<id>`)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Share frozen state of a snapshot (vs live re-running area)
  - Same pattern as v0 share infrastructure, different payload source
  - See `[[to_do_05_08]]` v0/v0.5/v1/v1.5/v2 wave structure

- [x] **Good comp / Bad comp parcel tagging** ✅ COMPLETED 2026-05-13 (via `comp_ratings` table — different schema than originally planned, same functionality)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Shipped:** Good/Bad/Clear buttons in comp popups (`_buildRatingButtonsHtml` at map.js). `bad-comp` CSS class desaturates the footprint. Per-workspace state persisted via `comp_ratings` table (Phase 2 canonical store after the 2026-05-13 rewire — superseded the original `session_tags` plan, so the R19 PK redesign blocker is moot for this feature). Ratings ship through share links via fork-handler `comp_ratings` INSERT...SELECT.
  - **Open follow-up:** rate-404 frontend toast for silent-fail feedback (logged in `project_roadmap.md`).

- [ ] **Marker layer decoupling**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Today: filter off "off-market" → sold dots disappear too (wrong)
  - Need: marker overlay (listings/solds) independent of parcel-category filter
  - Fragile refactor (R7); REVERT immediately if existing filter flows regress

---

## 🐛 Bugs / known issues

- [ ] **CSRF protection re-enable on critical writes** (was disabled on login flow)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Logout/change-password/admin endpoints already require CSRF
  - Login itself bypasses for first-load reasons — verify no exposure remaining

- [ ] **Active listing match rate unknown** — depends on addr_key normalization
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Need to run a sample comparison: how many `redfin_active` rows match a parcel?

- [ ] **Large area draw occasional first-tile failure**
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Backburner; needs brainstorm before coding
  - May be related to retry / 502 handling

- [ ] **CSV "Seed Target" column showing empty in a known-bonded area** — investigate
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Observed 2026-05-08 on a Plano (Collin) area with `share_id=area_fXRj8lv3QU`; CSV had the share_id populated (so `job.saved_area_id` was set) but Seed Target was blank for every row
  - Possible: (a) the area genuinely had no bonded seeds because the test save-parcel was on a different area, (b) the schema migration crash earlier that day rolled back a partial bond, (c) an account_num mismatch between `saved_parcels.account_num` and `rows[].account_num` (county-prefix? whitespace?)
  - Lookup query at `api/main.py:2842-2846` doesn't filter by user_id (intentional — recipients should see sender's seeds), so user-scope isn't the issue
  - Repro path: pick a known-bonded area in Cloud SQL Studio, confirm `saved_parcels.area_id = '<area_id>'` row exists, then download CSV and check whether Seed Target marks that account_num
  - Low priority — gold halo on share link works (the user-visible feature), CSV column is a secondary export concern

- [ ] **CSV — "Account Number" column missing entirely** (pre-existing, low priority)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - 1-line fix: add header `"Account Number"` and `row.get("account_num", "")` to the row writer in `api/main.py` (`generate_csv` inside `download_csv` handler)
  - Mike likely wants this for cross-referencing back to county records / appraisal districts; flag it next time he opens an export

- [ ] **`_google_maps_link` uses owner state when property state missing** (pre-existing)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - `api/main.py:1065` falls back from `property_state` to `owner_state` — for absentee owners this gives wrong-state Google Maps queries (saw "PLANO, NY, 75023" for a NY-based owner of a Plano property)
  - All four counties are TX-only, so safe fix is to hardcode `"TX"` rather than reading owner_state at all

- [ ] **Rural SE Dallas zero/null appraised values unconfirmed**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Need to verify whether DCAD source genuinely lacks these or our ingest drops them

- [ ] **Always-clickable parcels regardless of filter** (Mike feedback #2)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Mike wants every parcel clickable even when filter hides it visually
  - Approaches: keep markers rendered + reduce opacity, OR a separate "show all" toggle, OR hit-test the DB on map click

- [ ] **Owner history (4 owners back) — Mike feedback #3**
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **2026-05-14 — research done, PIA emails drafted and ready to send.** See `docs/parcel_history/pia_emails/` (4 ready-to-paste emails for DCAD/TAD/Collin/Denton, all requesting "all available digital years through 2020").
  - **Architecture decided:** separate repo `kosmickroma/parcel-history` + separate DB on same Cloud SQL instance. Keeps lot-ledger clean. Memory: `project_historical_owner_data.md` has full design (schema, phases, backup paths).
  - **Phases:**
    - Phase 1 (immediate): ingest 2021-2025 from each CAD's public download page
    - Phase 2 (after PIA responses): ingest older years from PIA deliveries
    - Phase 3 (backup): Wayback Machine / Regrid / DataTree if PIA stalls
  - **Why "all years":** Mike said "last 4 owners" — needs ~30-40 years for typical property (10-yr turnover average). Let each CAD respond with their full digital archive.
  - **PIA timeline:** 10 business days legal window. Started: [FILL IN DATE SENT]. Expected responses by: [10 business days out].
  - Texas non-disclosure complicates sale-price data; ownership is public record though
  - Sale-price data (MLS) is a separate track — see `[ ] Privy.pro evaluation` and `[ ] NTREIS VOW feed via SimplyRETS` further down in this todo

---

## 🔒 Security audit follow-ups (2026-05-08 Copilot scan)

Captured from a repo-wide scan. Threat model: auth-gated team tool with Mike + VAs as the user base, not internet-public. Real-world severity adjusted from audit severity accordingly. Don't do these today; batch when feeling defensive.

- [ ] **XSS — sanitize popup + list innerHTML sinks** (highest real-world severity; ahead of the rest)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Vector:** user-typed inputs (workspace names, saved area names, parcel notes) flow through `innerHTML` template strings and travel via share links to other authenticated users. Authenticated-user-against-recipient XSS is the realistic failure mode
  - **Existing escape helper** at `map.js:5979` is only used in a few places, not the risky sinks
  - **Sinks to fix:** popup HTML at `map.js:3338` (mounted at 3822 + 3860), anchor href interpolation at 3370/3387/3398/3468, saved items list at 2296 + 2428, HOA tooltip at 1339
  - **Mechanical fix:** thread the existing escape helper through; prefer `textContent` + `setAttribute` over `innerHTML` for any field that originates from a user; add URL allowlist (https only, reject `javascript:` / `data:` / malformed schemes) on href interpolation
  - ~1-2 hrs careful work

- [ ] **Analyze endpoint workload guardrails** (medium real-world)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Vector:** large polygons (think Dallas-County-wide draw) can hammer Cloud SQL CPU/memory. 2×2 tile split helps but no hard caps
  - **Fix:** polygon vertex + bbox area limits at request validation (`main.py:836`); per-user/IP rate limit on heavy endpoints; server-side hard cap on candidate rows in county queries (`dcad.py:267`, `tad.py:122`, `collin.py:60`, `denton.py:65`) + truncation metadata in response

- [ ] **Security headers middleware** (low real-world, easy fix)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Missing: CSP, HSTS, X-Frame-Options (or `frame-ancestors` via CSP), Referrer-Policy, X-Content-Type-Options
  - One-shot middleware in `main.py` — ~30 min

- [ ] **Container runs as root** (low real-world, easy fix)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Add non-root runtime user + least-privilege filesystem perms in `Dockerfile`
  - ~15 min, defense-in-depth for container compromise

- [ ] **Generic exception responses** (low real-world)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Raw DB/exception detail returned at `main.py:1789, 2716, 2816`
  - Replace with generic client-safe message + server-side log with request ID
  - Keep `/health/db` private or make it minimal/non-verbose

- [ ] **Login throttling shared-store migration** (low real-world for now; matters at multi-instance scale)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - In-memory tracker at `auth.py:32` is per-process. Multi-instance Cloud Run = bypass-able; restart clears counters
  - Move to Postgres-backed table (or Redis if we ever add it) with TTL; add username+IP composite key
  - Defer until Cloud Run is scaled past min-instances=1

- [ ] **SRI integrity attributes on CDN scripts** (low)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Some CDN assets at `index.html:244, 245` lack `integrity` + `crossorigin`; Leaflet core already has them — consistent the rest, or self-host pinned

- [ ] **Password policy** (low — only matters past current user count)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Length-only check at `main.py:919`. Add common-password denylist + optional MFA for admin/owner role when user base grows

---

## 🏗️ Infrastructure / DevOps

- [ ] **Disable `kosmickroma@gmail.com` owner account** (Mike's real account `mpdietz@hotmail.com` already exists, partially done as of 2026-05-07)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Both accounts currently `owner` role + active; only Mike's should remain active
  - Use admin panel: find row → Disable
  - Don't do mid-test-flow; wait until v0 has soaked

- [ ] **Update `BOOTSTRAP_OWNER_EMAIL` env var on dev + preview Cloud Run services**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Pure cleanup — doesn't affect runtime (seed only runs on empty users table)
  - Update to Mike's real email for consistency with the user record

- [ ] **GitHub repo transfer to Mike's account**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Option B locked: transfer ownership to Mike, KK stays as collaborator
  - Do AFTER GCP migration soak

- [ ] **Add explicit migration step to Cloud Build pipelines** (R20)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Currently: app-startup hook runs idempotent ALTERs (Option C — works fine for v0)
  - Future: separate migration step in `cloudbuild*.yaml` for clearer ops + rollback
  - Not urgent unless we add destructive migrations

- [ ] **`data/` folder cleanup decision** (added 2026-05-07)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - 1.1 GB of raw DCAD CSVs at repo root, gitignored, referenced by `scripts/build_db.py:148`
  - Options: leave alone (gitignored = invisible in git), rename for clarity, or move outside repo and update script paths
  - Don't delete — would break Dallas DCAD re-ingest

---

## 📊 Data / ingestion

- [x] **Run DeepFin Tarrant active scrape** (Collin / Dallas / Denton already done as of 2026-05-07)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - `redfin_active` row counts: collin=6,834, dallas=9,954, denton=14,073, **tarrant=0 (pending)**
  - Same gates that protected sold scrape

- [ ] **TAD frontage / depth data**
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Not in default ParcelView shapefile export
  - Need separate extract from Tarrant CAD

- [ ] **Tarrant HOA polygons**
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Currently Dallas-only (177 polygons)
  - Tarrant HOA dataset needed; source TBD

- [ ] **Rockwall County ingest** (Phase 5 expansion)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Small, easy — proven workflow from Collin/Denton
  - Do AFTER GCP migration

- [ ] **Harris County (Houston)** — deferred
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Different data format, large scope; only if Mike expands scope

---

## 🎨 UI / UX backlog

- [ ] **School district boundaries** (Phase C)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - **BLOCKED** on TEA shapefile (Texas Education Agency statewide ISD data)
  - Gold line overlay, click popup shows ISD name
  - Investor-core: ISD = resale price ceiling for teardown/flip

- [ ] **F: Active listings in browse mode** (PMTiles overlay)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Backend viewport endpoint, debounced moveend/zoomend trigger
  - Currently active listings only load in draw mode

- [ ] **G: Sold comps in browse mode** (PMTiles overlay)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - **BLOCKED** on PMTiles rebuild post-migration
  - z≤13 cluster bubbles, z≥14 individual dots

- [ ] **PMTiles lot size + area_estimated flag**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Requires DB schema migration + full `build_db.py` re-run + ~45 min PMTiles pipeline
  - Bundle with next annual DCAD data refresh

- [ ] **Responsive minZoom**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - `getOptimalMinZoom()` based on viewport width: <1440px → 14, 1440px+ → 13
  - Implementation needs to be verified perfect before shipping

- [ ] **Large area draw tool revamp**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Smarter initial tile count from bbox area
  - Per-tile progress UX (possibly SSE)
  - Brainstorm before coding

- [ ] **"Refresh data" button on saved areas**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Manually re-trigger live re-analyze
  - Useful once we have dynamic data feeds; placeholder today

- [x] **CSV export review — columns, filtering, ordering, what's exported per workspace** (flagged by KK 2026-05-10)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **PROMOTED 2026-05-11** to active In-Flight item: see "CSV export rebuild" above. Backlog review item retired — work is happening now.

- [ ] **CSV/PDF export "shareable image"** for one-off non-team recipients
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Different from share links (those are team-internal)
  - Only build if Mike asks

- [ ] **Saved-parcel name normalization across counties** (low priority — user can rename anyway)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Observed inconsistency: DCAD saves as "STREET# STREET_NAME" only; Collin saves with full city/state/zip baked in; TAD/Denton vary
  - Source of variance is each county's `property_address` field shape (see `api/counties/{dcad,tad,collin,denton}.py`); shared payload builder at `api/counties/dcad.py:472` exposes `addr` from that field as-is
  - Goal would be uniform "STREET, CITY" display
  - Real work: DCAD has no property_city → needs zip→city table or reverse-geocode; Collin needs a parser to split "ADDR\nCITY, TX 75033"; TAD only has owner mailing city; Denton already exposes property_city
  - Risks: bad parser produces garbled names; CSV "Address" column shape would change if we rewrite payload; existing saved-parcel rows already have old-shape names baked into payload
  - Pragmatic: users type/edit area names anyway, so prefill suggestion just gets overwritten — defer until county data normalization is on the table for another reason

---

## 🎓 Borrowed-workspace UX (deferred — refine after Mike + VAs use it)

- [ ] **Banner on borrowed workspaces** ("Viewing X — created by Y. Save as my copy to modify.")
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - When current workspace is loaded via deep-link (not in user's `_savedAreasCache`), show prominent banner explaining the ownership model
  - Replace "Save Area" CTA with "📋 Save as my copy" while in this state
  - Pure UI affordance — no backend changes needed
  - Solves the confusion KK hit: "I sent it back to dev, started tweaking filters, no Update button"
  - ~30 mins of work
  - Defer until Mike + VAs actually use it; refine based on real usage

- [ ] **Auto-fork on first persistent action (extends v0.5 Workspace Materialization Rule)**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - When user is on a borrowed workspace and makes any persistent action (toggle filter, tag parcel), auto-create their own fork in their sidebar
  - Update button materializes seamlessly — zero friction
  - Cons: sidebar gets a new row they didn't explicitly save (mitigated by clear naming + idempotency on share_id)
  - Maps to the auto-create-on-receive SaaS pattern KK described earlier
  - Defer until banner approach has been observed in real usage

- [ ] **Help button / first-time guide popup**
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Bottom-of-screen onboarding hint: "First time using a shared link? Click Save Area to make it yours and start refining."
  - Could be a one-time dismissable toast on first share-link open
  - Or a persistent help-button (?) in the corner with a panel of tips
  - Defer until borrowed-workspace UX shape is locked

## 🔮 Future / research

- [ ] **Privy.pro test for NTREIS comp data** (~$47/mo)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Sign up, validate sold-comp accuracy vs broker MLS
  - Investigate whether their API supports polygon/spatial queries (may be UI-only)
  - If API works: build comps overlay on top
  - See `[[comps_deep_dive]]` in memory for full option analysis

- [ ] **NTREIS VOW feed via SimplyRETS** (long-term comp data path)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - Requires Mike's broker to apply for VOW data feed access (NOT IDX)
  - $99 one-time + $50/mo per MLS via SimplyRETS
  - Best long-term integration; gating factor is broker cooperation
  - Action: Mike asks his broker to initiate VOW feed request through NTREIS/Trestle

- [ ] **Permit overlay**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Phase 6 enrichment

- [ ] **Code violation overlay**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Phase 6 enrichment

- [ ] **Analyst notes on tagged parcels** (free-text field per tag)
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Phase 6 enrichment

- [ ] **Magic-link invite flow for new VAs**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - If onboarding pain shows up at higher VA volumes
  - Currently Mike creates accounts in admin panel manually

- [ ] **Workspace forking / templating**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Phase 4+ candidate, related to v1 collab model decision

- [ ] **Workspace activity log / audit feed**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - "Who did what when" inside a workspace

- [ ] **"Quick area from address"** — auto-draw radius polygon
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - VA searches address → "Quick area" button auto-creates 0.25 / 0.5 / 1 mile radius workspace
  - Cuts call-handling time dramatically

---

## 📥 Inbox / capture (write here during calls or brainstorms; KK or Claude triages later)

<!-- Drop quick thoughts, ideas, observations here. Move into the right section above when triaging. -->

- 

---

## ✅ Done (recent)

### 2026-05-12 through 2026-05-14 — Propelio comp pipeline maturation + sidebar restructure + intended-target star

> Multi-session sprint covering Phase 2 global comps DB stabilization, comp rating canonical-store rewire, sidebar restructure with role gating, intended-target star, marathon scraper simplification. All shipped to develop unless noted. Branch tip: `46dab46`.

- [x] **Phase 2 global comps DB Chunks 1-3 + 2A polish** — `propelio_comps` spatial cache, parallel writes on every scrape, cache-first read path gated by `PHASE_2_CACHE_READ=true`, archived `propelio_comp_archive` rows preserved for legacy fallback readers. Workspace-save race + net-new metric polish bundle shipped in the 2A pass.

- [x] **Custom Search rename + Get Comps Quick Sweep + sidebar consolidation** — "Refresh from source" → "Custom Search" with progress banner; gold sticky Get Comps fires 3-pass quick recency sweep (1mo/2mo/3mo at 0.25-1.0mi SFR with geojson polygon constraint); old Refresh Recent button retired; Stop button right-aligned in banner; shimmer animation throttled to once-per-minute; saved-parcels list gets max-height scroller.

- [x] **Custom Search target-anchored centering (Phase A)** — Custom Search now picks subject = `_lastSearchedAddress` / outline / popup state (whichever is set), falls back to polygon centroid. Matches Quick Sweep's behavior. Phase B (explicit Target ↔ Area toggle) deferred — Task #19 in tracker, memory `project_center_mode_toggle.md`.

- [x] **Rate-path canonical-store rewire** — `set_comp_rating` now writes to `comp_ratings` (Phase 2 table) via INSERT...ON CONFLICT keyed on `(workspace_id, comp_id)`. Resolves comp_id from `propelio_comps` via `comp_address_key`. Closes the silent-404 + ratings-vanish-on-reload bug. `rate_comp` endpoint also gained `Depends(get_current_user)` (security hardening). Fork handler copies `comp_ratings` rows. One-shot migration backfilled legacy `propelio_comp_archive.user_rating` → `comp_ratings` at startup.

- [x] **Sidebar restructure + power-user role gating** — Custom Search subsection extracted to own block, Prop Filters moved DOWN past saved-* blocks, status filter subsection added to top of Comp Filters body, Map Filter defaults flipped (Multifamily/Commercial/Exempt OFF), single source of truth for Sold/Active/Pending toggles (Map Filters AND Comp Filters surfaces sync). `_isPowerUserOrAbove()` helper + `body.is-not-power-user` CSS class hide `#custom-search-block`, `#numeric-filters`, gold Get Comps button, and right-click parcel save from regular `user`/`member` roles. Right-click handlers bail BEFORE preventDefault so non-power-users get the native browser context menu.

- [x] **Right-click parcel save + shared-link auto-fork** — Right-click a parcel saves it directly via the existing `saveParcel` choke point. `_loadAreaFromShareId` auto-forks shared workspaces into the recipient's account with `Name (2)/(3)` collision-resolved naming. Fork handler copies `propelio_comp_archive` rows so good/bad ratings survive (legacy column kept for fallback readers, no longer maintained by new writes after the canonical-store rewire).

- [x] **Intended-target star** — gold star (always visible, NOT zoom-gated) marks the workspace's bonded originator parcel. `saved_areas` schema gained `originator_parcel_county` + `originator_parcel_account_num` columns. saveParcel is the single choke point for setting the current target (both right-click and popup-save flow through it). State `_currentTargetParcel` derives originator at save-area time; bonded value persists across reloads + ships through shared links via the fork handler.

- [x] **Dynamic browser tab title** — `document.title` reflects the active workspace name; auto-syncs at all `_currentLoadedAreaId` write sites.

- [x] **Get Comps button polish** — shimmer throttled to once/minute, cache-empty chip re-centered after Refresh Recent retirement, sticky button surfaces on saved-area load (not just polygon-draw), `power-user-only` class survives className overwrite per the hot-fix.

- [x] **Muddied colors dedup (3 attempts shipped)** — Comp footprints stacking on same parcel produced muddied colors. Three dedup-key strategies shipped: `parcel_county|parcel_account_num` → `comp_address_key` → 4-decimal rounded lat/lng. Some stacking persists (Propelio geocoder variance > 10m on certain records). Full investigation trail + 5 candidate next strategies + DevTools diagnostic snippet preserved in `project_muddied_colors_backlog.md`.

- [x] **CAD filter count badges show "as-if-toggled-on" baseline** — Off Market / Vacant / Multifamily / Commercial / Exempt count badges show "what would render if this toggle were on" independent of the toggle's own off state. Same pattern previously shipped for Sold/Active/Pending/OAC.

- [x] **Sidebar note text simplified** — Note under OAC row now reads "Some comps filtered out" when filters reduce visibility, blank otherwise (replaces the sold-comp-specific "N of M sold comps found" wording per client).

- [x] **Update button surfaces on propelio filter drift** — Status toggles + numeric filters now trigger Update button visibility, not just Map Filters.

- [x] **Marathon scraper simplification + Dallas focus + permanent-error classification** (on `feat/marathon-campaign` branch) — Single 6-pass config (3 recent-tight + 3 broad 24mo at .25/.5/1.0 mi), Dallas-only filter, south-to-north claim order, `mls_coverage_error` + `no parcel match` → `seed_skipped_terminal` (not retried), 5-min cooldown cap, `is_open()` re-reads DB on every call so operator clears propagate. Marathon NOT merged to develop — stays branch-only.

- [x] **Deep Pull (dev) button removed** — gold Get Comps now handles all user-driven sweep needs; backend `/api/propelio/deep-pull/*` endpoints stay for marathon worker use.

- [x] **Gold target stars at low zoom** — markers on saved parcels visible at zoom < 14 for wide-view navigation. Separate from intended-target star (which is always visible).

- [x] **`cloudbuild.yaml` + `cloudbuild-preview.yaml` declarative env vars** — `DEEP_PULL_EXPERIMENT=true` + `PHASE_2_CACHE_READ=true` set declaratively in both deploy configs (was sticky-only on preview before).

---

### 2026-05-10 — UI restructure initiative — 6 chunks shipped on `feat/workspace-flow` (lot-ledger-pro)

> KK-spec → Copilot-code → Claude-review/commit/deploy workflow proven across 6 sequential chunks. All chunks preview-deployed to `lot-ledger-preview` Cloud Run via `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`. Average build: ~2:45. Branch tip: `a7e4584`. Comprehensive 44-item test list provided for morning verification.

- [x] **Gold target click-catcher** (commit `94d3b93`) — saved-target gold halos are now reliably clickable on the map regardless of (a) whether the underlying parcel is part of the active draw analysis, (b) whether property filters have hidden the parcel polygon, (c) whether the click lands inside vs outside the polygon edge. Architecture: transparent sibling polygon on the default overlay pane, with its own click handler that fetches `/api/parcel/{county}/{account_num}` and opens the same popup as the browse-mode click path. `L.DomEvent.stopPropagation` prevents double-fire. **This resolves the "Saved targets become unclickable when filtered out by property filters" bug** that was previously in the Bugs section.

- [x] **Chunk 1: Quick wins** (commits `610627e` + `fa3ffba`) — three independent small edits in one chunk:
  - Disabled Propelio auto-pull on address search (both `selectSuggestion` typeahead and `doSearch` Go-button paths). `firePropelioFetch` now dead code with a clear `TODO(comps-button)` annotation explaining it'll be reconnected by the planned Comps button per `project_propelio_comp_harvest.md`.
  - `#saved-areas-list` scrollable (max-height 300px + overflow-y auto + slim padding-right).
  - `.filter-swatch` border-radius dots → squares (50% → 2px).

- [x] **Chunk 2: Map Filters restructure** (commit `aa1f569`) — sidebar reshuffle, pure markup + one CSS modifier:
  - R.F. Listings + R.F. Sold moved into a new **Legacy Filters** collapsible card (starts collapsed). IDs preserved (`filter-active`, `filter-sold`).
  - Propelio Sold / Active / Pending toggles moved out of the propelio card's `.propelio-status-row` into Map Filters as `.filter-row`s. New `.filter-swatch.is-dot { border-radius: 50% }` modifier keeps them as dots while sibling parcel-type swatches read as squares. IDs preserved (`prop-status-sold/active/pending`).
  - Outside Area moved to BOTTOM of Map Filters, renamed "Outside Area Comps", ID preserved (`prop-outside-area`). Invisible swatch placeholder keeps grid alignment.
  - All wiring is ID-based — no JS changes were needed.

- [x] **Chunk 3: Comp color system flip** (commit `9f02ada`) — coordinated palette flip across every surface that signals Propelio comp status. The OLD palette (sold=purple, active=red, pending=amber) is gone; NO amber anywhere after this chunk. New palette: sold=red (`#dc2626`), active=green (`#22c55e`), pending=blue two-tone (light `#7dd3fc` fill + darker `#0284c7` stroke). Surfaces touched: `.propelio-footprint-glow` (base + 3 modifiers), `.propelio-fallback-dot` (base + 3 modifiers), the 3 Map-Filters dot swatches in HTML (Pending uses an inline radial-gradient), `PROPELIO_HEADER_COLORS` const in map.js, and the 3 `.propelio-price-label` rules (partial revert of `11e6cdb`'s sold↔active assignment). Orphan `.propelio-status-row/.chip/.swatch` CSS (deferred from chunk 2) deleted as part of this chunk.

- [x] **Chunk 4: Vacant border thickness** (commit `76625da`) — one-line conditional added to `parcelBorderWeight`. Vacant parcels now render at 4px stroke (thickest weight on the map; beats R.F. sold-comp's 3.2 and R.F. active's 2.8). Vacant+comp overlap comes for free: the `.propelio-footprint-glow` renders on a separate layer ON TOP of the parcel polygon, so a vacant-and-comp parcel shows the new comp color (red/green/blue) inside with the thick green vacant ring outside — no extra rendering code needed.

- [x] **Chunk 5: Crisp purple selected outline** (commit `4139b9b` + fix `965b911`) — new visual signal when a user clicks an item from the saved-areas sidebar or the propelio comp list. Crisp 3px purple stroke (`#a855f7`) with ONE subtle drop-shadow only — explicitly NOT the fuzzy multi-shadow stack of `.propelio-fallback-dot`. Lives on its own `selectedOutlinePane` at zIndex 625 (above the gold-halo `savedParcelPane`) so a saved target + selected coexist visibly. Map click clears (separate `map.on("click")` listener runs in both browse and draw modes). Fix `965b911` scopes the outline to `type="parcel"` items only — clicking a saved area (drawn polygon) no longer outlines the whole drawing as if it were a single parcel.

- [x] **Chunk 6: In-map check badge identity swap** (commit `a7e4584`) — pure CSS, two badge color stacks swap visual roles so the new check-on-white-circle shape carries clearer meaning. `.propelio-good-mark` (good-rated comps) flips green → red. `.verify-badge-vacant` restyled from solid green disc with white check → white circle with green check + green ring (mirroring the new good-mark shape, just in green). Net rule: green-check-on-white-circle = "verified vacant", red-check-on-white-circle = "good comp". Popup buttons unchanged; `.verify-badge-not-vacant` (red ✗) and `.verify-badge-target` (gold ★) unchanged.

**Process notes for this initiative:**
- Workflow change agreed mid-session: Copilot edits only; Claude does diff-review + commit + push + manual preview deploy. KK delegated that grunt work explicitly for the duration. Future chunks can flip back to "Copilot commits" if KK prefers.
- Pre-existing TS unused-var warnings (`verifiedVacant`, `markers`) shifted by line numbers across multiple commits — they're not from this session's edits, just diagnostics noise.
- 6-chunk plan + corrections + workflow shape captured in memory `project_high_priority_dump_2026_05_10.md`; can delete that memory once KK confirms the morning test list passes and the initiative is fully signed off.

### 2026-05-08 — `_run_schema_steps` SAVEPOINT fix shipped

- [x] **Per-step SAVEPOINT wrapping** in `api/main.py:_run_schema_steps` — idempotent-skip errors no longer poison the outer Postgres transaction. Replicates the pattern already proven in `_finalize_user_scoping` at lines 382-388 of the same file. Branch `fix/schema-steps-savepoint` → preview rev `00059-8jd` booted clean against the same DB that surfaced the original crash → merged to develop as `5cec9d5`.

### 2026-05-08 — Target-area bonding shipped (workspaces carry their seed targets through share links + CSV)

- [x] **Schema** — `saved_parcels.area_id` FK → `saved_areas(area_id)` ON DELETE CASCADE; partial unique indexes for standalones (`area_id IS NULL`) and bonded copies (`area_id IS NOT NULL`). Commit `37b72ca`.
- [x] **Backend bonding logic** — Save Parcel upserts standalone + optional bonded copy when caller passes `area_id`; Save Area auto-bonds the user's standalones inside the polygon (server-side ray-cast, no PostGIS); share-link + single-area resolvers include `seed_parcels`. Commit `556640c`.
- [x] **CSV "Seed Target" column** — marks parcels bonded to the saved area linked to this job; column is rightmost data column, just before `share_id`. Initial position was between Potential Target and HOA, moved to the right per Mike preference. Commits `1af4957` (initial) + the move.
- [x] **Frontend wire-up** — `saveParcel` sends `area_id` when a workspace is loaded; share-link recipients see seed parcels rendered as gold-glow polygons (deduped); `GET /api/parcels` filters to standalones only so Targets sidebar stays one-row-per-parcel. Commit `fbb4e47`.
- [x] **Save Area name prefill** — when the drawn polygon contains a user's saved standalone, the inline name input pre-fills with that parcel's address (pre-selected so type-to-replace works cleanly). Reuses existing `pointInPolygonLngLat` helper, frontend-only. Commit `fb505ff`. **Partially satisfies** the "Default workspace name = first-saved-parcel address" sub-bullet of the v0.5 Workspace materialization item (suggestion side only — silent auto-create flow still pending).
- [x] **Schema startup crash fix** — removed dead `CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_parcels_user_account` step. Once any user had both a standalone + bonded copy of the same parcel, the CREATE failed with "key is duplicated" (legal under the new partial scheme); the idempotent-error skip in `_run_schema_steps` doesn't use savepoints, so the next step hit "current transaction is aborted" → uvicorn exit. The `DROP IF EXISTS` step that follows handles a missing index cleanly. Commit `5fce44f`.
- [x] **End-to-end verified by KK** — saved parcel → drew area → saved area (auto-bonded) → shared link → another login saw gold halo on the seed parcel. Cross-browser round-trip confirmed.

### 2026-05-08 — Gold pulsing halo for saved parcels shipped (replaces solid orange)

- [x] **Saved-parcel / target polygons render gold** (`#FFD700`, fillOpacity 0.75, weight 4) with a pulsing CSS drop-shadow halo
  - Branch: `feat/target-gold-glow` → merged into develop as `775b2d4`
  - Animation: pure CSS `@keyframes` on `.saved-parcel-glow` class, `will-change: filter` for GPU layer
  - Price labels readable through the gold (opacity drop from 0.95 → 0.75)
  - Sold (purple) / active listing (red) / target ★ badge all unchanged
  - Pattern reference: 9elements neon-glow article + Leaflet.HighlightableLayers; pre-emptive perf insurance via will-change
  - Workflow rhythm validated: KK plans → Claude specs in chunks → Copilot codes one chunk → Claude diff-reviews → preview deploy → KK visual verify → merge

### 2026-05-08 — Username-only auth shipped to develop

- [x] **Make email optional in admin user creation (Option B)** — owner creates user with just username + temp password + role
  - Schema: idempotent migration step `users_email_drop_not_null` (`api/main.py:_run_schema_steps`) drops NOT NULL on `users.email`. Existing emails preserved. Unique-on-LOWER(email) keeps working (PG treats NULL as distinct).
  - Backend: new `get_user_by_username_or_email(identifier)` helper in `api/auth.py` with deterministic ordering (username match wins). LoginRequest gains `identifier` field, `email` kept as backward-compat alias. AdminCreateUserRequest.email becomes Optional, stored NULL when blank. All serializers preserve None for email.
  - Frontend: login form prompts "Username or Email" (text input), admin form puts username first and hides email behind collapsed "+ Add email (optional)" gold link. Admin user table renders em-dash for null-email rows. Force-change-password modal stagger-clears autofill stomps (sync + setTimeout 0 + setTimeout 100).
  - Branch: `feat/username-only-auth` — preview-verified by KK ~22:30 EDT 2026-05-07. Merged + pushed to develop as `56860e7`. Auto-deploys to `lot-ledger-dev` via cloudbuild.yaml.
  - Things deliberately not changed: session cookie payload, itsdangerous serializer, SESSION_SECRET, BOOTSTRAP_*_EMAIL env vars, audit log, Argon2 hashing.

### 2026-05-07 — Quick wins + UX polish shipped (caught up move)

- [x] **Saved-areas UX redesign** — gold Share button matches CSV download styling, row 2 (Rename/Delete) reveals only on active row. Branch `feat/saved-areas-ux`, commit `ae01fdf`. Merged into develop via `feat/filter-arch-and-layout`.
- [x] **Move lot-size filter from main → comps filters block** (frontend-only, ~15 min)
- [x] **Active listing popup polish** — dropped "Redfin:" prefix, moved list price to right side of header (`Active listing $XXX,XXX`)
- [x] **Rename "vs DCAD" → "LP vs CAD" with per-county awareness** — DCAD / TAD / CCAD / Denton CAD
- [x] **Popup flash on draw-mode parcel click** — synchronously open popup on new layer before clearLayers; popup persistence is solid post-fix

### 2026-05-07 — Active item slot saga finally fixed (DO NOT RE-ANIMATE)

> ⚠️ **Lesson burned in over ~10 commits and several hours of debugging.** Keep this in mind any time someone proposes "polishing the slot animation" or "make it slide in nicer". The animation is what caused all the bugs. Don't bring it back without re-reading this section.

**The bug (user-visible):** Click a saved area or target row → "WORKSPACE" / "TARGET" gold label shows at top of right sidebar, but the address (saved area name) does NOT show below it. Slot looks pinched.

**The architecture that caused it:**
- `#active-item-slot` had a class `is-collapsed` toggled by JS
- `.active-item-slot` had `max-height: 120px` + `overflow: hidden` + `transition: max-height 0.25s ease`
- `.active-item-slot.is-collapsed` had `max-height: 0` + `opacity: 0`
- `setActiveItem(type, name)` removed `is-collapsed`; `clearActiveItem()` added it
- This was supposed to slide-in/slide-out the slot when an item is selected/cleared

**Why it broke (root cause):**
The slot would end up stuck at `max-height: 0` even after `setActiveItem` removed the `is-collapsed` class. Diagnostic via `requestAnimationFrame` after click confirmed:
- `nameEl.textContent === "test 40000"` ✓ (text in DOM)
- `nameEl.offsetHeight === 21` ✓ (strong renders correctly)
- `nameEl.display === "block"` ✓
- `slot.offsetHeight === 1` ❌ (slot clipped to 1px)
- `slot.computedMaxHeight === "0px"` ❌ (slot still collapsed)

So the strong rendered fine — but the parent slot was clipping it. The `transition` + `is-collapsed` class machinery had a race we never fully diagnosed (suspected: rapid class toggle in same JS tick gets batched and skipped, OR the analysis flow was re-adding the class somewhere we couldn't find).

**Things tried that DID NOT fix it (don't repeat):**
1. `9e3933e` — `position: sticky; top: 0; z-index: 2` on slot (fixed scrolling-out-of-view but not the clipping)
2. `bcb50da` — bumped `max-height: 80 → 120`, padding, font weights (cosmetic only)
3. `5986e0c` — pulled `clearActiveItem()` out of `clearDrawResults()` to avoid double-toggle in same JS tick
4. `fed2d4d` — moved `setActiveItem` BEFORE `await runAnalysis` so it fires immediately on click
5. `b01d90e` — removed post-analysis `setActiveItem` call that was stomping user's selection during analysis
6. `57e1818` — added `display: block` to `.active-item-name` so the strong renders its own line
7. `a6dab12` — renamed `"Target"` → `"Workspace"` for parcel/saveParcel paths
8. Lots of `debug(slot)` commits (`718dc30`, `20249f4`, `3025278`, `35a8e3e`, `7c81216`) installing mutation observers, computed-style polling, stack traces

None of those resolved the clipping. The strong was always rendering correctly inside a 0-tall parent.

**What actually fixed it (`49d12a2`):**
**Removed the entire `is-collapsed` / `max-height` / transition mechanism.** The slot is now:
- Always visible (no `is-collapsed` class on initial DOM)
- Renders at natural content height (no `max-height`, no `overflow: hidden`, no transition)
- Default placeholder name = `"—"` so it always shows something
- `setActiveItem` and `clearActiveItem` just write text to the two spans — no class toggling, no animation

**Files touched in fix:**
- `frontend/index.html` — slot div lost `is-collapsed`, name strong defaults to `"—"`
- `frontend/style.css` — removed `.active-item-slot.is-collapsed { ... }` rule entirely; removed `overflow: hidden`, `max-height: 120px`, `opacity: 1`, `transition: ...` from `.active-item-slot`
- `frontend/map.js` — `setActiveItem` simplified to text-only writes; `clearActiveItem` resets to placeholder text instead of toggling class

**Future-Claude rules:**
1. **DO NOT** re-introduce a `max-height: 0` / `transition` collapse mechanism for the slot. If someone wants animation, find a different technique (e.g., CSS `grid-template-rows: 0fr → 1fr`, or animating opacity-only with the slot always at content height).
2. **DO NOT** add `overflow: hidden` to `.active-item-slot` — that was a key part of the clipping bug.
3. **DO NOT** add an `is-collapsed`-style class toggle to the slot. Just write text.
4. The slot is now a static info display. It IS the workspace label. That's fine; it doesn't need to slide.

### 2026-05-07 — Verified done via DB / config check

- [x] **Mike's owner account created** (`mpdietz@hotmail.com`, role: owner, active) — note: `kosmickroma@gmail.com` still active too, see Infrastructure section for disable task
- [x] **DeepFin Collin active scrape** — 26,896/26,896 cells complete, 6,834 rows in `redfin_active`
- [x] **DeepFin Dallas + Denton active data populated** — 9,954 + 14,073 rows in `redfin_active`
- [x] **DeepFin sold comp data verified fresh across all 4 counties** — full 1-year window, dates through 2026-05-04/05; counts: collin 10,107, dallas 18,444, denton 23,487, tarrant 26,743

### 2026-05-07 — Share-link fidelity verified end-to-end

- [x] **Post-render reapply pattern** in `restoreSavedArea`, `restoreNamedSession`, `draw:created`, `rerunWithRedfin`, `toggle-redfin` change handler, `rerunWithSold` — fixes off-market parcels not visible after share-load AND sold count mismatch. Verified by KK's full back-and-forth round-trip test.
- [x] **Filter restore on saved-area click** — earlier fix `3b066f4` confirmed working.
- [x] **Role-tier admin actions verified** — Reset Pw (server-gen), Hard Delete (cascade), Disable, defensive validation-error handling all working in admin panel.
- [x] **Full collaboration cycle proven** — dev sends → user-role saves+refines+updates → user sends back → dev sees identical state. Round-trip with byte-identical fidelity.

### 2026-05-07 — v0 share-link MVP shipped

- [x] **P0 — DSN password encoding fix** (`api/config.py` urllib.parse.quote)
- [x] **P1 — Schema startup-runner extension** (idempotent ALTER step list, named steps)
- [x] **V0-01 — share_id + is_publicly_viewable columns + backfill on saved_areas**
- [x] **V0-02 — share_id surfaced in POST/GET /api/areas + new GET /api/areas/{area_id} endpoint**
- [x] **V0-03 — GET /api/area/by-share-id/{share_id} resolver endpoint** (cross-user team-collab read)
- [x] **V0-04 — Share button on saved-area rows with toast + clipboard write**
- [x] **V0-05 — Deep-link handler reads ?area= param, loads workspace via existing restoreSavedArea flow**
- [x] **V0-06 — CSV export rightmost share_id column + cached_jobs.saved_area_id linkage**
- [x] **V0-06b — Backfill in-flight job linkage at Save Area time** (closes the "first CSV after save was empty" gap)
- [x] **Saved-areas UX redesign — two-row hierarchy, gold Share button, smooth row-2 reveal on active**
- [x] **Popup persistence fix** (commit 9e81189 + child-layer metadata fix on GeoJSON children)
- [x] **Cross-browser end-to-end test** — link sent via Telegram from Firefox/Linux opened in Chrome by different user, two workspaces in two tabs

### Earlier May 2026 milestones (from prior daily todos)

- [x] Phase 1 — Saved Areas with full filter persistence
- [x] Phase 2 — Saved Snapshots (formerly "Save Session")
- [x] A4 — Saved areas + parcels migrated from localStorage → Postgres (user-scoped)
- [x] Click-mode toggle (Jump/Stay) for saved area click behavior
- [x] Auth system: cookie sessions, RBAC, login modal, force-password-change, admin panel
- [x] Cloud Run preview environment fully configured + documented
- [x] Three IAM grants for new GCP projects documented
- [x] Cloud SQL postgres password rotation + Secret Manager update + Cloud Run redeploy
- [x] DeepFin Collin active scrape resumed after hard restart (popup flash diagnosis included)
- [x] Sold comp filter restore (root-cause fix in `clearDrawResults` ordering)
- [x] CSV download staged status indicator
- [x] Map filters + Property filters now collapsible
- [x] Always-show all 7 count rows including zero values

---

## How to use this file

1. **During calls or brainstorms:** scroll, click priority checkboxes on items as you go. Add notes as new sub-bullets under any item. Drop loose thoughts into **Inbox / capture** at the bottom of the open sections.
2. **When an item ships:** mark the top checkbox `[x]` and move the whole item to **Done (recent)** under today's date heading.
3. **When new ideas come up:** just add to the right section, copy the priority-checkbox block, leave priority unset until your next triage pass.
4. **Ask Claude periodically** to re-triage Inbox items into proper sections, archive aging Done items, or absorb call-dump notes.

Related living docs:
- `[[to_do_05_08]]` — full v0 execution plan + risk analysis (still useful for reference)
- `[[mike_gcp_handoff_plan]]` — Thursday migration playbook
- `_archive/` — superseded daily todos and consumed prompts
