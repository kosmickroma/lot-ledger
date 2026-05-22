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

- [ ] **🆕 NEXT SESSION PICKUP — 2026-05-21 end-of-day state**
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Shipped to develop today (commit `55f86c7`):**
    - DCAD residential detail (Phase 1+2 morning): 27 RES_DETAIL.CSV columns ingested, canonical keys flow through `feature.properties`, +27 CSV columns at 74-99
    - `merged_rows` leak fix: dcad.py was silently dropping the new keys; fix shipped
    - save_verification fast-path: branches on `_job_store` warmth, thin JSONB queries on cold instances
    - Animation gate: `.saved-parcel-glow` paused at zoom < 15
    - Cloud Run free tuning: concurrency 80 → 20, startup CPU boost on (preview + dev only)
    - **Denton Phase 3** (afternoon): 306,688 parcels ingested from 2025 certified data (`denton_improvement_detail` table), 95.9% residential coverage. Pool, garage, stories, deck derived from sub-area detail rows. Plumbing reinterpreted as bath count (decimal half-baths). Total Rooms / Outdoor Fireplaces / End Unit added as canonical keys.
    - **Phase 4 CSV**: +5 columns at 100-104 (Interior Finish, Flooring, Total Rooms, Outdoor Fireplaces, End Unit). Total CSV columns 146 → 151.
    - Quick fixes: half_baths=0 now displays as "0" not "N/A"; "Baths (derived)" row dropped; Actual Age derived from yr_built when CAD doesn't publish.
  - **On preview only (NOT promoted to main / Mike's prod):**
    - Everything above is on develop @ `lot-ledger-dev`. Mike's prod still at the pre-2026-05-21 state.
    - **Before promoting to main / Mike's prod**, draft column-shift heads-up for Mike (CSV total cols went 119 → 146 morning, then 146 → 151 afternoon = +32 new columns total. Cells 1-99 unchanged. Anything past col 99 has shifted. Spreadsheets that reference by header name = fine. Spreadsheets that reference by column letter past col 99 = need re-mapping.)
  - **Likely next-session items (pick whichever fits the moment):**
    - [ ] TAD-half PR — spec is already in `docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md` v3. Next step is downloading TAD's "Residential Comp Attribute Data" file from `tad.org/data-download/` (browser-only — bot 403s), then mirror the Denton pattern. ~12-15 fields available including garage_capacity which DCAD doesn't have.
    - [ ] Mike CSV heads-up draft + prod promote (when ready). Don't promote without notifying him.
    - [ ] Regular-users-inherit-filters regression — LIVE PROD BUG, see Bugs section below
    - [ ] Mike's GCP SQL password rotation — still HIGH, untouched
    - [ ] Phase 5 Denton extras KK didn't request today but data is there: Open Porch sqft (485k coverage), Bonus Room sqft (48k), Outdoor Kitchen, Balcony, Storage / Barn / Stables, Gazebo, Tennis Court, Basement Finished, Detached Living, etc. — all sub-area detail rows we don't currently surface.
    - [ ] Phase 1.5 structured panel for Subject Property card (committed in v3 spec; Core/Structure/Mechanical/Amenities/Record sections).
  - **Deferred per KK call today (in master_todo below, NOT cleared):**
    - Multi-year prior owner ingest (Denton + DCAD) — back-burner per KK
    - DCAD garage capacity from supplemental release — 2nd priority
    - TAD/Collin paid/PIA paths for richer residential detail — out for now

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

- [ ] **Data ship to Mike's GCP project — TOMORROW** — see `[[mike_gcp_handoff_plan]]` v2 (2026-05-14)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Scope:** ONE-TIME SNAPSHOT of data only. NOT the full app migration. Lot-ledger keeps running on our infra.
  - **Pre-call (KK solo, ~30 min):** CSV export review (your stated blocker), confirm strip row 3 finished, verify data inventory matches §2 of runbook
  - **On call with Mike (~30-45 min):** Mike creates GCP project, attaches billing, adds KK as Owner → Path A. If Mike refuses access → Path B (3 sub-options for delivery)
  - **Solo work after call (~3-5 hrs):** provision Cloud SQL + GCS in Mike's project, export from our prod, import into Mike's instance, copy PMTiles, write welcome README, screenshot exchange with Mike
  - **End state:** Mike's GCP has live Cloud SQL with `lotledger` + `lotledger_sessions` DBs, two GCS buckets (snapshot files + tiles), welcome README, IAM clean
  - Full runbook with 11 error-recovery scenarios + verification queries Mike can run himself at `[[mike_gcp_handoff_plan]]`

- [ ] **Full GCP app migration to Mike's project (Phase 2 — 2-4 weeks out)** — solo task, ~8-12 hrs spread across days
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Blocked until: data ship done + soak time on dev + password-reset email + access-model code change
  - Phase 1 (post-data-ship): password-reset email flow, access-model code (no developer role on Mike's prod), audit log panel
  - Phase 2: Cloud Run service `lot-ledger-prod` in Mike's project, Cloud Build trigger watching `main`, runtime SA + 5 IAM grants, env vars (no `BOOTSTRAP_DEV_*`), domain mapping + SSL
  - Phase 3: DNS cutover, final smoke test together, hand Mike the URL
  - Phase 4: GitHub repo ownership transfer to Mike, sunset our dev (or keep for ongoing dev), update memory + README
  - Detailed playbook to be drafted closer to date; see archived v1 plan `[[_archive/mike_gcp_handoff_plan_v1_2026-05-06]]` for phase-by-phase reference

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

- [ ] **Marker layer decoupling**
  - [ ] HIGH
  - [ ] 2nd
  - [ ] Defer
  - Today: filter off "off-market" → sold dots disappear too (wrong)
  - Need: marker overlay (listings/solds) independent of parcel-category filter
  - Fragile refactor (R7); REVERT immediately if existing filter flows regress

---

## 🐛 Bugs / known issues

- [ ] **🚨 Regular users silently inherit Property Filters from shared/saved workspaces** (regression from 2026-05-20 Property Filters→comps ship — LIVE ON MIKE'S PROD)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Symptom:** A regular `user`-role account opens a saved area or share link that a power-user-or-above saved with Property Filters configured (e.g., lot_sqft_min=5000, appr_val_min=300000). The filter values silently restore into the global `numericFilters` and now — after today's `Property Filters → comps` ship — also gate the Propelio comp overlay. Regular user sees fewer comps than the power user does, with no way to fix it (Property Filters UI is `.power-user-only` → hidden).
  - **Root cause:** `frontend/map.js:1307` in `restoreFilterState()`:
    ```js
    if (state.numeric && typeof state.numeric === "object") Object.assign(numericFilters, state.numeric);
    ```
    This line copies `state.numeric` into the module-global `numericFilters` regardless of role. Pre-2026-05-20 the inheritance was a silent no-op for comps (Property Filters only gated CAD parcels). Today's change made `compPassesPropelioFilters` also read `numericFilters`, so the silent inheritance is now silently destructive.
  - **Affected users:** any account with role `user` or `member` who opens a workspace whose `filter_state.numeric` was populated by a `power_user` / `owner` / `developer`. Mike's prod has been on the new behavior since revision `lot-ledger-00012-9cx` (deployed 2026-05-20 evening). If Mike's two non-owner users are `user`-role, they're hitting this RIGHT NOW.
  - **Proposed fix (one-line gate, frontend-only):**
    ```js
    if (state.numeric && typeof state.numeric === "object" && _isPowerUserOrAbove()) {
      Object.assign(numericFilters, state.numeric);
    }
    ```
    Comp Filters (`compNumericFilters` line 1308) and `propelioFilterState` line 1318 stay as-is — those have visible UIs for regular users. Only the `.power-user-only` Property Filters block is the problem.
  - **Why filed instead of shipped:** KK chose 2026-05-20 to defer the fix to dedicated investigation rather than rush a same-day patch. Confirm impact on the two real prod users first (check their roles), then decide whether to hotfix or include in next planned ship.
  - **Verification before fixing:**
    1. Query Mike's GCP `users` table to confirm the two non-owner accounts' roles (`SELECT username, role FROM users WHERE role NOT IN ('owner','developer','power_user')`)
    2. Reproduce locally: log in as a `user`-role test account on preview, open a workspace that was saved with Property Filters set, confirm comp count drops vs what an `owner` sees on the same workspace
    3. Apply the one-line fix on a hotfix branch → preview → verify regular user now sees comps as if no inherited filter
  - **Related:** [[csv-export-shipments-2026-05-20]] for the Property Filters → comps work that triggered this; [[feedback_db_production_discipline]] for the discipline rule we should have applied during today's spec but missed the role-inheritance edge case.

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
  - **2026-05-14 PIA send status:**
    - ✅ **DCAD** — original (historical rolls) sent to `openrecords@dcad.org`. Lot-dimension addendum sent as reply to same thread. 10-day clock running.
    - ✅ **TAD** — original sent. Lot-dimension addendum sent as reply to same thread. 10-day clock running.
    - ✅ **Denton CAD** — original sent, auto-acknowledged. **Tracking ID: `624156fHVDEPeyqnGxBithsqoK`** (helpdesk.dentoncad.com). Lot-dimension addendum sent as reply to same thread. 10-day clock running.
    - ❌ **Collin CAD** — `webmaster@collincad.org` BOUNCED. Use JustFOIA portal instead: `https://collincadtx.justfoia.com/publicportal/home/newrequest`. Expanded email at `docs/lot-ledger/parcel_history/pia_emails/03_collin_cad.md` includes BOTH historical rolls + lot dimensions as one request. **Send tomorrow morning.**
  - **Tomorrow morning checklist:**
    - [ ] Send Collin via JustFOIA portal
    - [ ] Check sent folder — confirm all 4 originals + 3 follow-ups went through
    - [ ] Capture any new tracking IDs (TAD, DCAD may also have ticketing systems)
    - [ ] Update memory `project_historical_owner_data.md` with final send dates + tracking IDs once all 4 are out
    - [ ] If any bounced overnight, paste the bounce to Claude to find the right channel
  - **Expected response window:** ~2026-05-28 (10 business days from 2026-05-14). Follow up by ~2026-06-02 (3-day grace) on any non-responders.
  - **Architecture decided:** separate repo `kosmickroma/parcel-history` + separate DB on same Cloud SQL instance. Keeps lot-ledger clean. Memory: `project_historical_owner_data.md` has full design (schema, phases, backup paths).
  - **Phases:**
    - Phase 1 (now): ingest 2021-2025 from each CAD's public download page — start building tonight/tomorrow morning so pipelines are ready when PIA data lands (see separate todo entry below)
    - Phase 2 (after PIA responses): ingest older years from PIA deliveries
    - Phase 3 (backup): Wayback Machine / Regrid / DataTree if PIA stalls
  - **Why "all years":** Mike said "last 4 owners" — needs ~30-40 years for typical property (10-yr turnover average). Let each CAD respond with their full digital archive.
  - Texas non-disclosure complicates sale-price data; ownership is public record though
  - Sale-price data (MLS) is a separate track — see `[ ] Privy.pro evaluation` and `[ ] NTREIS VOW feed via SimplyRETS` further down in this todo

- [ ] **parcel-history repo — Phase 1 scaffold + DCAD ingestion (start now while PIA clock runs)**
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Why now:** PIA responses are 10 business days out (~2026-05-28). If we build the ingestion pipeline against the publicly-available 2021-2025 DCAD data tonight or tomorrow morning, the pipes will be ready and tested when the PIA archive years arrive — just point them at the new files.
  - **Scope of Phase 1 build:**
    1. New repo `kosmickroma/parcel-history` (private GitHub) — clone of the lot-ledger pattern but minimal (just the bits we need)
    2. New DB `parcel_history` on the same Cloud SQL instance
    3. `ownership_snapshots` table per the schema in memory `project_historical_owner_data.md` (county, property_id, snapshot_year, owner_name, owner_address, deed_date, source_file, UNIQUE constraint)
    4. DCAD ingestion script — parse the publicly-available 2021-2025 certified rolls into the table
    5. Smoke test: confirm join from DCAD account_num back to lot-ledger's `parcels` table works
  - **Out of scope for Phase 1:** TAD/Collin/Denton ingestion (until we see their file formats), read-only HTTP endpoint, lot-ledger UI integration
  - **Estimated effort:** ~2-3 hours for DCAD-only Phase 1. Brainstorm → spec → plan loop applies given new repo + new DB.
  - **Reference:** Full architecture and schema sketch in memory `project_historical_owner_data.md`

---

## 🔒 Security audit follow-ups (2026-05-08 Copilot scan)

Captured from a repo-wide scan. Threat model: auth-gated team tool with Mike + VAs as the user base, not internet-public. Real-world severity adjusted from audit severity accordingly. Don't do these today; batch when feeling defensive.

- [ ] **🚨 Rotate Mike's GCP SQL password + scrub from git history** (flagged 2026-05-20 during cleanup)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Issue:** `docs/lot-ledger/Mikes GCP SQL Password.md` was committed to the repo and contains the live Cloud SQL password for Mike's `real-estate-map-tool` project. Anyone with read access to the repo (including the GitHub history, even if the file is deleted later) can recover it.
  - **Immediate impact:** the password is exposed to everyone who has cloned the repo, including any historical clones. Repo is private but the password should be treated as already-compromised.
  - **Action plan when ready:**
    1. Rotate the Cloud SQL password in Mike's GCP project (Cloud Console → SQL → Users → reset password for the lot-ledger app user)
    2. Update the secret in Secret Manager (or wherever the app reads it from) on Mike's prod + dev Cloud Run services
    3. Verify the new password works (deploy a no-op rebuild, smoke test login)
    4. Remove the file from the working tree
    5. Scrub it from git history via `git filter-repo` or `bfg` — non-trivial because all clones need to re-clone
    6. Force-push the rewritten history (coordinate with anyone else who has a clone)
    7. Optionally enable secret-scanning on the repo (GitHub built-in feature)
  - **Why not now:** KK confirmed 2026-05-20 — he wants to do it but not in the middle of other work. Bumped to top of security list as HIGH.

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

- [ ] **Neighborhood polygon shapes from all CADs** (2026-05-21 — KK ask, captured during TAD city work)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - **What we already have on disk that we don't ingest:**
    - **TAD** — `Neighborhoods/NBHD.shp` (code-only, no name baked in yet; pair with TAD lookup CSV to get names)
    - **TAD** — `Subdivisions/Subdivision.shp` (polygons + names)
    - **Collin** — `Collin_CAD_Neighborhood_List_20260502.csv` + `Collin_CAD_Abstract_Subdivision_List_20260502.csv`
    - **Denton** — `Subdivisons_View_-...csv` and likely more in the unzipped folder
    - **DCAD** — `NBHD_CD` is already on parcels but we have no shape file or name lookup
  - **Why we want them:** investor-relevant overlay (clusters of comparable parcels, subdivision-scoped comp filtering, "show me parcels in Park Carillon" type queries). Mirrors the gold-line approach for TIGER block groups (POC `feat/poc-neighborhood-overlay`).
  - **Effort:** ~1-2 hours per county to ingest the shapefile + create lookup. Compose with existing neighborhood-overlay POC work.

- [ ] **HOA polygons for Tarrant + Collin + Denton** (2026-05-21 — currently Dallas-only with 177 polygons)
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Dallas HOA polygons came from a Dallas-specific source. Other counties need their own data source identified.
  - Tarrant HOA entry already exists in master_todo `📊 Data / ingestion` — keep this consolidated there. Add Collin + Denton when we know where the data lives.

- [ ] **DCAD garage capacity** — not in `data/RES_DETAIL.CSV` (audited 2026-05-21). Probably exists in a supplemental DCAD release we don't currently pull (optional appraisal-roll subscription, CommercialPropertyData supplements, or similar). Worth a re-audit of dallascad.org's open-data portal when the gap becomes annoying. TAD has Garage_Cap directly in their ParcelView shapefile (landing in the next TAD-half PR).
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer

- [ ] **Multi-year prior owner ingest (Denton + DCAD)** — 2026-05-21 KK directive ("pull previous years and pull them — back burner list"). Today's Denton APPRAISAL_INFO.TXT (2025) only has CURRENT year owner. To get prior owner chains, need to download + ingest multiple prior-year APPRAISAL_INFO files from `dentoncad.net/data/_uploaded/files/datafiles/<year>/CertifiedDataAllProperty/` (2019-2024 all available, ~4-5GB each, ~30GB+ total). For DCAD, same idea — historical RES_DETAIL.CSV / ACCOUNT_INFO files would need to be obtained. Both would feed a new `owner_history` table keyed by (county, prop_id, tax_year, owner_name). County Clerk deed records are the legal source of truth for ownership chain but a separate data pipeline (Denton + Dallas County Clerks). KK said back-burner — defer until after current data-richness sweep wraps.
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer

- [ ] **TAD half — bring TAD parcel-detail up to DCAD/Denton parity** (2026-05-21 — last county remaining after Phase 1+3 wins today)
  - [x] HIGH
  - [ ] 2nd
  - [ ] Defer
  - **Status update (2026-05-21 EOD):** DCAD ✅ DONE (Phase 1+2, commit `5bbbf7a` morning). Denton ✅ DONE (Phase 3+4, commit `55f86c7` afternoon). Only **TAD remaining**.
  - **Spec ready:** `docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md` v3 covers TAD. Section: "TAD source data audit".
  - **Concrete next steps:**
    1. Open `tad.org/data-download/` in browser (bot 403s — must use browser). Look for "Residential Comp Attribute Data" download for tax year 2024 (or latest).
    2. Drop in `ingest/counties/tarrant/tad/2026-05-01/unzipped/ResCompAttribute/`. Likely 100-300MB.
    3. Audit columns vs canonical residential keys (foundation_type, roof_material, etc.).
    4. Build `scripts/build_tad_comp_attribute.py` mirroring the Denton ingest pattern. Mirror new `tad_improvement_detail` table.
    5. Update SELECTs in `api/counties/tad.py:query_tad_parcels` + `_fetch_tad_parcel_by_account` with LATERAL JOIN (same shape as Denton/DCAD).
    6. Update `_normalize_tad_row` to carry new canonical keys (avoid the dict-rebuilder leak class that hit DCAD).
    7. Backfill + preview-deploy + smoke.
  - **Bonus TAD-only field:** `Garage_Capacity` (already in ParcelView shapefile DBF — empirically verified). DCAD doesn't have this; TAD does. Pull it into the canonical contract.
  - **What's stable from Phase 1-4:** build_feature canonical-key emission, popup row layout, Subject Property card lines 2-3. TAD parcels start populating once the table exists + SELECT projects the keys. Zero frontend code needed.

- [ ] **TAD frontage / depth data**
  - [ ] HIGH
  - [x] 2nd
  - [ ] Defer
  - Not in default ParcelView shapefile export
  - Need separate extract from Tarrant CAD

- [ ] **DCAD + TAD city resolution for parcel addresses** (deferred 2026-05-20 — back of list)
  - [ ] HIGH
  - [ ] 2nd
  - [x] Defer
  - **Symptom:** Subject Property row + first-save Workspace name show street-only for Dallas County (DCAD) and Tarrant County (TAD) parcels, because neither county's source data exposes `property_city`. Collin + Denton already render cleanly as "STREET CITY" (shipped 2026-05-20 via the `_formatPropertyAddress` per-county normalizer).
  - **Why deferred:** KK's call (2026-05-20) — knock out more visible hotfix items first; revisit when city display becomes a real-world friction point.
  - **Options on the table (pick when re-opened):**
    1. **⭐ RECOMMENDED — Census TIGER Places shapefile + PostGIS point-in-polygon backfill.** Ingest `tl_2024_48_place.shp` (Texas places — every incorporated city + CDP as authoritative polygons with official names) into a `places` table. One-time spatial backfill per county: `UPDATE dcad_parcels SET property_city = (SELECT name FROM places WHERE ST_Contains(places.geom, parcel_centroid))`. Cleaner than a ZIP table because ZIPs don't align with city boundaries (one ZIP can span multiple cities; cities have multiple ZIPs). After backfill, `property_city` is just populated — same render path as Collin/Denton, no per-county branching. **Bonus:** also fixes `_google_maps_link` "PLANO, NY" absentee bug and unlocks future town-keyed features (school overlays, comp-by-town filters, CSV).
    2. **OpenStreetMap admin boundaries** (alternative to TIGER). `admin_level=8` polygons for US cities — often more current than TIGER but slightly less authoritative. Free via Geofabrik download. Same spatial-backfill workflow as option 1.
    3. **Owner-occupied heuristic** (partial, cheap stopgap). When `owner_zip == property_zip` AND `owner_state == "TX"`, use `owner_city`. Works for owner-occupied parcels (decent fraction of Mike's teardown targets); absentees still city-less. Note: this is *exactly* the surface that produced the "PLANO, NY" Google Maps link bug — same data, different guard. Could ship in a day as a stopgap before the TIGER ingest.
    4. **ZIP → city lookup table** (deprecated in favor of option 1). Hardcoded Dallas County (~80 ZIPs) + Tarrant County (~50 ZIPs) map. Worse than TIGER because of the ZIP-vs-city-boundary mismatch; only listed for completeness.
    5. **Nominatim / Mapbox reverse-geocode API.** Accurate, no maintenance table, but external dependency + rate limits + per-call latency. Not great for batch backfill; could be a fallback when point-in-polygon misses.
    6. **🌟 NEW 2026-05-21 — the answer was already in our source data.** Investigation revealed:
       - **DCAD:** `data/ACCOUNT_INFO.CSV` has a `PROPERTY_CITY` column populated for 849,099 / 861,357 parcels (98.6%). Our `build_db.py` ingest never pulled it. Cities include DALLAS (381k), GARLAND (DALLAS CO) (76k), IRVING (61k), MESQUITE (47k), etc. Top format quirk: multi-county cities have `(DALLAS CO)` suffix — strip at display.
       - **TAD:** `ingest/counties/tarrant/tad/2026-05-01/unzipped/Cities/Cities.shp` is a published shapefile with `CITY_TDC` (3-digit code) → `CITY_NAME` mapping. Our `tad_parcels.city_code` column already stores codes like "026", "024", "001" — they map directly to TAD's `CITY_TDC`. We've been sitting on this lookup file the whole time, never ingested.
       - **Additional TAD signals available** for cross-validation/fallback: subdivision name suffix (e.g., `VALLEY VIEW ADDITION-ARLINGTON`), ISD descriptor (`ISD 901` = Arlington ISD), owner_city when owner-occupied.
       - **Recommended fix:** ingest the Cities.shp DBF into a tiny `tad_city_lookup (city_tdc, city_name)` table + one-shot UPDATE on `tad_parcels.property_city`. Mirror pattern for DCAD by adding `PROPERTY_CITY` to the `ACCOUNT_INFO.CSV` ingest.
       - **Effort:** ~1-2 hours each. ZERO spatial/PostGIS work. ZERO inference. ZERO external dependencies. **Demote TIGER Places option 1 to fallback-only — only relevant if Cities.shp / PROPERTY_CITY has gaps.**
       - **Branch in flight (2026-05-21):** `feat/tad-city-resolution-2026-05-21` — lead with TAD per KK.

    7. **DCAD `nbhd_cd` prefix decode** (DCAD-only, NOT a primary solution). 2026-05-20 investigation showed the 2-letter prefix in DCAD's `nbhd_cd` (e.g., "1DSA04" → "DS" → DALLAS) appears to encode the city for ~95% of Dallas parcels (DS/IS/MS/GS/LS/US/SS/ES/OS/PS/YS are mostly single-city). BUT: validation used owner-occupied parcels (owner_zip == property_zip + TX state) as a proxy, which excludes out-of-state absentees and is not authoritative. **DCAD has no published `nbhd_cd → city` lookup table** ingested; we'd need to request it from DCAD directly OR do a TIGER spatial cross-check to confirm. **Not a substitute for TIGER** — TIGER is uniform across all four counties; `nbhd_cd` only helps DCAD and only with unverified confidence. Could serve as a **cross-check** ("does TIGER's derived city match what DCAD's nbhd_cd prefix predicts?") if useful.
  - **Code touchpoints when ready:**
    - `api/counties/dcad.py:495-521` — `build_feature` props dict, currently `"city": _clean_text(row.get("property_city"))` (always empty for DCAD)
    - `api/counties/tad.py:247` — same shape, currently uses `owner_zip` as `property_zip`
    - `api/main.py:_fetch_dcad_parcel_by_account` (line 2222), `_fetch_tad_parcel_by_account` — SELECT statements would need to pull whatever new source columns the chosen option needs (e.g., `owner_zip`, `owner_city` if heuristic; nothing extra if ZIP table)
    - `frontend/map.js:_formatPropertyAddress` — already in place, will just consume the new `props.city` once backend populates it
  - **Related items / memory:**
    - Existing 🎨 UI/UX backlog item "Saved-parcel name normalization across counties" — broader cross-county uniformity goal, this is the data-source slice
    - Existing 🐛 Bugs / known issues item "`_google_maps_link` uses owner state when property state missing" — same root cause, different surface (Maps URL)
    - Memory `project_save_vs_update_model.md` — Subject Property row design context

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

## ✅ Done archive

Completed items live in [[_master_todo_done]] — split out 2026-05-16 to keep this active list scannable. Mark items `[x]` here when they ship, then move the entry over to the archive under that day's date heading.

---

## How to use this file

1. **During calls or brainstorms:** scroll, click priority checkboxes on items as you go. Add notes as new sub-bullets under any item. Drop loose thoughts into **Inbox / capture** at the bottom of the open sections.
2. **When an item ships:** mark the top checkbox `[x]`, then cut the whole entry out of this file and paste it into [[_master_todo_done]] under today's date heading.
3. **When new ideas come up:** just add to the right section, copy the priority-checkbox block, leave priority unset until your next triage pass.
4. **Ask Claude periodically** to re-triage Inbox items into proper sections, archive aging Done items, or absorb call-dump notes.

Related living docs:
- `[[to_do_05_08]]` — full v0 execution plan + risk analysis (still useful for reference)
- `[[mike_gcp_handoff_plan]]` — Thursday migration playbook
- `_archive/` — superseded daily todos and consumed prompts
