---
created: 2026-05-22
status: handoff
purpose: Orient a fresh Claude Code chat that's picking up after this session.
---

# Handoff to Next Chat — 2026-05-22 end-of-session

**You're a fresh Claude Code chat continuing on LotLedger (real estate parcel analysis for KK's client Mike).** This doc gives you the minimum context to be useful immediately. The previous chat (which can be reopened for backup) has all the deep history.

## Read these first (in order)

The user's auto-memory at `/home/kk/.claude/projects/-home-kk-projects-clients-lot-ledger/memory/MEMORY.md` is the canonical index. **Read at minimum:**

1. `memory/project_status.md` — what's live where
2. `memory/project_architecture.md` — FastAPI + Leaflet + Cloud SQL stack overview
3. `memory/project_mike_migration_2026_05_18_complete.md` — IMPORTANT: dev + prod + scraper all share Mike's GCP DB. No separate "dev DB" anymore.
4. `memory/feedback_*` — KK's working-style rules. Notably:
   - `feedback_git.md` — commit + push when KK explicitly asks; draft message if ambiguous
   - `feedback_db_production_discipline.md` — DB-touching changes: spec → Copilot critique → fix → preview. NO ad-hoc Claude-driven DB writes.
   - `feedback_proportional_response.md` — match answer size to question size
   - `feedback_copilot_iteration_loop.md` — spec → critique → adjust → code → verify → commit
   - `feedback_no_coauthor_trailer.md` — never add `Co-Authored-By: Claude` to commits
5. `docs/lot-ledger/_master_todo.md` — comprehensive open items list

## State as of this handoff (2026-05-22)

### What's live everywhere (Mike's prod + dev + preview all on same DB)

Today's shipped work:
- **DCAD residential detail expansion** (Phase 1+2 morning) — 27 res_detail.csv columns ingested + canonical keys + popup panel + CSV columns 74-99. Affected `parcels` table on Mike's GCP.
- **Denton residential detail expansion** (Phase 3+4 afternoon) — 306,688 parcels ingested into new `denton_improvement_detail` table from 2025 certified data extract. Includes pool/garage/stories detection from sub-area det_type_codes, plumbing-as-bath-count, Total Rooms / Outdoor Fireplaces / End Unit canonical keys. CSV adds 5 new columns at 100-104.
- **save_verification fast-path** — branches on `_job_store` warmth, thin JSONB queries on cold instances.
- **Animation gate** — `.saved-parcel-glow` paused at zoom < 15.
- **Cloud Run free tuning** — concurrency 80→20 + startup CPU boost (preview + dev only; Mike's prod still on defaults).
- **Saved-area UX hotfixes** — workspace-box pencil always visible after rename/reload (was disappearing); saved-area-list hover tooltip on truncated names.
- **Parcel popup full address** — header now shows "STREET, CITY, TX ZIP" across all 4 counties via `_formatFullPropertyAddress`. NO CITY placeholder filtered out.
- **CNTY overlay expanded 7→61 counties** — 5 concentric rings around DFW. Per-county canvas-measureText label sizing (~13px max, never overflows polygon). Centered on polygon mass-center, anchor-locked via `transform: translate(-50%, -50%)`.
- **Saved-list search no-match bugfix** — typing a query with zero matches no longer hides the entire section.
- **propelio_comps parcel_county backfill (BANDERA bug)** — 828 rows fixed (817 unambiguous + 9 Collin↔Denton geo-resolved via PostGIS ST_Contains). CHECK constraint added preventing recurrence. Source backfill script patched to call `match_comps_to_parcels` so future re-runs match cleanly. See `docs/PROPELIO_COMPS_COUNTY_BACKFILL_SPEC.md`.

CSV column total: 119 → 151 → unchanged (no additions after the propelio fix).

### Tree status

Develop and main are in sync. All today's branches are merged:
- `feat/saved-area-name-polish-2026-05-21` ✓ merged
- `feat/popup-full-address-2026-05-22` ✓ merged
- `feat/county-borders-expand-2026-05-22` ✓ merged
- `fix/saved-list-search-no-results-2026-05-22` ✓ merged
- `fix/propelio-comps-county-backfill-2026-05-22` ✓ merged (DB-side fix already executed)

Open older branches (not from today, don't touch unless KK asks):
- `feat/marathon-campaign`, `feat/poc-neighborhood-overlay`, `feat/status-badge-oac-aware`, `feat/strip-runner`

### Today's prod deployments (Mike's `real-estate-map-tool` project)

- `lot-ledger-00013-hrn` — early-day day-of-work merge
- `lot-ledger-00014-w9b` — popup full-address
- `lot-ledger-00015-ts6` — county borders + search-no-match
- `lot-ledger-00016-*` — propelio_comps fix scripts/spec (firing as of handoff)

## Top open items KK may want to tackle next

(See `docs/lot-ledger/_master_todo.md` for the full list. Brief preview here:)

1. **🆕 The new feature KK wants to brainstorm** — start of next chat will be a brainstorm session. Be ready to be a thought partner.
2. **TIGER Places + PostGIS city resolution** — 🏆 reinforced 2026-05-22 as the ONE right fix for the "NO CITY" unincorporated-county problem. ~1-2 hours of work. Empirically validated as superior to ZIP-lookup or owner_city heuristic. Free Census data.
3. **Multifamily / duplex split** — pull duplexes (state_use A12) out of the multifamily bucket into their own property type. KK mentioned bundling this with the BANDERA bug investigation but the BANDERA bug got resolved standalone, so the duplex split is now independent. ~half day.
4. **TAD-half residential detail PR** — spec at `docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md` v3. Mirror the Denton pattern.
5. **Regular-users-inherit-filters regression** — LIVE PROD BUG (see master_todo Bugs section)
6. **Mike's GCP SQL password rotation** — still HIGH, untouched

## Critical context

- **Mike is live with real users on his GCP.** Any DB change goes through spec → Copilot critique → fix → preview workflow. KK is strict about this.
- **There is no separate dev DB anymore** (since 2026-05-18 migration). dev + preview + Mike's prod all share Mike's GCP DB. Schema changes affect everyone immediately.
- **CSV column-shift heads-up for Mike** — pending if any new CSV additions ship before notifying him. Cells 1-99 unchanged today; positions past col 99 shifted. KK has not yet drafted the heads-up email.

## How to run things

- **Preview build (frontend):** `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`
- **Preview URL:** https://lot-ledger-preview-qa7hokv3ma-uc.a.run.app
- **Dev URL:** dev.lotledger.com (or via Cloud Run)
- **Mike's prod URL:** https://lot-ledger-oxs3z6a2sa-uc.a.run.app
- **Prod build (Mike's GCP):** `gcloud builds submit --config cloudbuild-prod.yaml --project=real-estate-map-tool`
- **Strip-runner (Propelio sweep):** see `scripts/strip_runner.py`, address lists in `lot-ledger-strip/scripts/strip_runner_addresses/`. Use stdbuf for unbuffered streaming.

## When KK says "deep dive on this", the pattern is

1. Read relevant code first (don't ask, just look)
2. State the actual file/line numbers + your reading
3. Give the recommendation + the main tradeoff
4. Ask before writing code if it's nontrivial
5. Match response length to question — short questions get short answers

## Working style summary

- **Trust but verify.** When you say something works, check the file/DB state to confirm.
- **Spec before code on anything DB-touching.** Use the structure: Problem → Goal → Changes (per-file) → Verification plan → Risk → Rollback → Out of scope.
- **Copilot is the second-opinion gate** for nontrivial work. KK pastes Copilot's response and you incorporate it.
- **Preview-only by default.** KK explicitly says "ok now promote to develop/main/prod" when ready.
- **Never amend commits.** Always create new commits. Never skip pre-commit hooks.
- **Never push with --force unless KK explicitly says so.**

## Backup chat

If you need full conversation history, the previous Claude Code session can be reopened from FleetView. Earlier in the day we shipped Denton + DCAD residential detail expansions, then dove into the BANDERA bug, which is now closed.

---

**Start the next chat by reading this file + the memory index, then ask KK what feature he wants to brainstorm.**
