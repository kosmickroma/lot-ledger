---
title: CSV Export — Propelio Comp Source Refactor
status: spec v1.3 — live-data verification of parsed_payload completed; URL emission locked to blank
branch: feat/csv-export-propelio-source
created: 2026-05-15
related:
  - "[[CSV_EXPORT_REFACTOR_BRAINSTORM_WIP]]"
  - "[[mike_gcp_handoff_plan]]"
  - "[[PARCEL_DISTANCE_TOOLS_SPEC]]"
  - "[[project_propelio_listing_url]]"
---

# CSV Export — Propelio Comp Source Refactor

## 1. Goal

Make the CSV export's primary sold-comp columns reflect MLS truth (Propelio) instead of Redfin estimates, while preserving Redfin data as an auxiliary far-right column block that Mike can keep or strip with one Excel column-delete.

## 2. Problem statement

Texas is a non-disclosure state. Redfin sold prices in the CSV are estimates; Propelio MLS data is our source of truth and the sidebar UI already uses it. The CSV is the only path still on the old Redfin data.

Concrete symptom that surfaced this: pulling an export for a small/edge polygon (e.g., `0 CHADWICK DR`, a vacant HOA-owned parcel at the south Dallas border) returns every `Comp_*` column empty, despite the workspace having context.

Root cause is two compounding issues:

1. **Wrong source.** `api/sold.py:88` `query_sold_parcels` reads `redfin_sold` in the parcel DB.
2. **Tight match radius.** `api/main.py:3071` `_COMP_THRESHOLD = 0.00135` ≈ 150m at Texas latitude. Most suburban/edge parcels don't have a comparable sold within one city block even when good data exists nearby.

## 3. Design

### 3.1 Column layout

The current CSV has 94 columns. After this refactor it has approximately **105 columns**. The two trailing columns today are `Seed Target` and `share_id` (in that order) at `api/main.py:3191-3192`. **`share_id` must stay the last column** to protect any downstream consumer that reads positionally from the right edge. The RF_ block is inserted just before `Seed Target`.

| Position (left → right) | Block | Source | Notes |
|---|---|---|---|
| Existing | Parcel data (county, address, lot dims, etc.) | Parcel DB | Untouched |
| **Existing 10 `Comp_*` cols (repurposed)** | **Propelio (primary)** | `propelio_comps`, polygon-bounded, closest match | Was Redfin; now MLS truth |
| **NEW: `Comp Distance (ft)`** | **Propelio match distance** | Computed via PostGIS `ST_Distance` from parcel centroid | Match-quality signal for analysts |
| Existing middle cols | unchanged | unchanged | unchanged |
| **NEW block: 10 `RF_Comp_*` cols** | **Redfin (auxiliary)** | `query_sold_parcels` (unchanged code path) | Was the old `Comp_*` data, now relabeled. Strip in Excel if undesired. |
| `Seed Target` | unchanged | unchanged | Stays in original position |
| `share_id` | unchanged | unchanged | **Must remain the final column** |

**The `RF_` prefix** is plain underscore — `RF_Comp_Sold_Price`, `RF_Comp_Sold_Date`, etc. Avoids periods that occasionally trip Excel formula parsers.

**The `Redfin List Price` column** (active listings, not sold) is independent at `api/main.py:3109` and stays untouched — different data path.

### 3.2 Match algorithm for the Propelio block

For each workspace:

1. Query `propelio_comps` for rows where `status = 'sold'` AND `ST_Within(geom, workspace_polygon)`. Mirrors the gold-standard pattern at `api/propelio/archive.py:598-599`.
2. For each parcel in the workspace, find the **closest** in-polygon Propelio sold comp via SQL lateral nearest using `ST_Distance` on geography. No radius cap — if it's in the polygon, it's eligible.
3. Distance reference point is the **parcel centroid** (existing lat/lng we already store). Not polygon edge — keeps the math consistent with how the rest of the app measures distance (target-star, popup distance row, etc.).
4. Fill the 10 `Comp_*` columns inline using a normalizer that maps Propelio row shape → existing CSV writer dict shape (see §3.4).
5. Write the new `Comp Distance (ft)` column with the computed distance in feet.
6. If a polygon has zero in-polygon Propelio sold comps → every parcel's Propelio Comp_* cells stay blank and `Comp Distance (ft)` stays blank. **No fallback to Redfin or anywhere else.** The `RF_*` block on the far right is independent and may or may not have data on the same row.

### 3.3 Cache architecture

`cached_jobs` already has a `sold_points` JSONB column populated by the analyze pipeline from `query_sold_parcels`. That stays exactly as-is and now feeds the `RF_*` block. Old cached jobs remain valid for this path.

We add a **parallel** column `cached_jobs.propelio_sold_points` (JSONB) for the new Propelio match results. Populated by a new fetch in the analyze pipeline. Old cached jobs have NULL here, which triggers a re-resolve on first export — sidesteps any version-flag or invalidation machinery.

### 3.4 Propelio-row normalizer

A new helper maps a `propelio_comps` row into the dict shape the existing CSV writer **actually consumes**. The CSV writer's **contract** is preserved — the writer still receives the same dict keys it always did. Code changes inside the row-fill block are limited to (a) reading the primary comp from the new `propelio_comp_by_parcel_key` dict, (b) writing the new `Comp Distance (ft)` value, and (c) reading the auxiliary comp from the existing `comp_by_parcel_key` for the `RF_Comp_*` cells.

**Verified against actual code** (subagent verification 2026-05-15): the CSV writer at `api/main.py:3345-3354` reads **exactly these 10 keys** from the comp dict — `sold_price`, `sold_date`, `price_per_sqft`, `yr_built`, `sqft`, `lot_sqft`, `beds`, `baths`, `dom`, `listing_url`. No other comp-dict keys are consumed. Earlier draft versions of this spec listed `address`/`unit`/`city`/`state`/`zip` as "direct mapping" — those keys (a) don't exist on `propelio_comps` (only `address` does) and (b) are never read by the writer anyway, so they are removed from the mapping entirely.

Field mapping (verified against schema at `api/main.py:297-323` and `scripts/backfill_propelio_comps.py:101-133`):

| CSV writer expects | Propelio column / derivation |
|---|---|
| `sold_price` | `price` |
| `sold_date` | `sold_date` (already derived from `close` in existing ingest) |
| `price_per_sqft` | computed: `_safe_float(price) / _safe_float(sqft)` when both present and `sqft > 0`, else NULL. **Use the existing `_safe_float` helper at `api/main.py:1264-1270`** — do NOT roll a new try/except. |
| `yr_built` | `year_built` |
| `lot_sqft` | `lot_size` |
| `sqft` | `sqft` |
| `beds` / `baths` / `dom` | direct mapping (same names) |
| `listing_url` | **Always emit blank in v1.** Live-data verification (2026-05-15, 500 latest `propelio_comps` rows): **zero rows contain `link`, `url`, `detail_url`, or any URL-like top-level key in `parsed_payload`**. The only URLs that exist are inside `parsed_payload.extra.raw.photos[].url` and those point to image binaries, not listing detail pages. Propelio's per-comp listing-page deep link requires a `lead_id` that is **not in `propelio_comps`** data (see `[[project_propelio_listing_url]]`). Per-comp deep linking needs a backend resolver endpoint — v2 scope. For v1, normalizer sets `listing_url = ""` for every Propelio row. The `RF_Comp_Listing URL` (Redfin block, far right) continues to use Redfin's real `listing_url` which is populated in `redfin_sold`. |

**Implementation notes.**
1. `parsed_payload` can be NULL or non-dict on older rows — guard with `isinstance(parsed_payload, dict)` before any key probing for non-URL data.
2. Even though `listing_url` is hard-coded blank, the normalizer MUST still emit the `"listing_url"` key (with empty string value) so the CSV writer's `comp.get("listing_url", "")` call is fed a known-clean shape.

Anything missing in Propelio → emit empty cell in the CSV. Don't fabricate.

### 3.5 SQL query shape

**Parcel input contract** (explicit — Copilot R3 clarification). `query_sold_parcels` at `api/sold.py:88` is fed a `polygon`, not parcel rows. The new `query_propelio_sold_in_polygon` function takes BOTH parameters explicitly:

- `parcel_input`: a list of tuples `(account_num: str, county: str, lat: float, lng: float)`, derived from the job's parcel rows already materialized in `cached_jobs.rows` JSONB for that `job_id`. The analyze pipeline already has these rows in scope at the point `query_sold_parcels` is called (`api/main.py:2618-2628` merges/dedupes the per-county query results into the unified `all_rows` list). The implementer pulls `(account_num, county, lat, lng)` from those rows.
- `workspace_polygon_geojson`: the polygon string from `cached_jobs.polygon` (or the in-flight polygon at analyze time). See §3.6 for empty-polygon handling.

**`propelio_comps` schema reminder** (from `api/main.py:297-323`, GIST index at `api/main.py:350`):
- Coordinate columns are `lat` and `lng` (not `latitude`/`longitude`)
- There is a materialized `geom` column with a GIST index — use it directly for both filtering and KNN ordering so the planner uses the index. Constructing `ST_MakePoint(lng, lat)` on the comp side bypasses the GIST index.

**Query shape** (parcel list passed in as parameter tuples; LEFT JOIN LATERAL so parcels with no in-polygon match still appear with NULL comp columns):

```sql
WITH parcels_for_job(account_num, county, lat, lng) AS (
    VALUES %s  -- psycopg2 mogrify many tuples, or equivalent
)
SELECT
    p.account_num,
    p.county,
    p.lat AS parcel_lat,
    p.lng AS parcel_lng,
    c.*,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)::geography,
        c.geom::geography
    ) AS distance_meters
FROM parcels_for_job p
LEFT JOIN LATERAL (
    SELECT *
    FROM propelio_comps pc
    WHERE pc.status = 'sold'
      AND ST_Within(pc.geom, ST_GeomFromGeoJSON(%(polygon_geojson)s))
    ORDER BY pc.geom <-> ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)
    LIMIT 1
) c ON true;
```

Key correctness points:
- `LEFT JOIN LATERAL ... ON true` is the right form — `CROSS JOIN LATERAL` does NOT take an `ON` clause. `LEFT JOIN LATERAL ... ON true` lets the lateral run for every parcel and yields NULL rows where no comp matched (preserves the "blank cells on empty polygon" promise).
- `ST_Within(pc.geom, polygon)` filters using the GIST index on `pc.geom`.
- `ORDER BY pc.geom <-> point LIMIT 1` is the PostGIS KNN pattern that uses the GIST index for ordering.
- `LIMIT 1` enforces the 1:1 match — honors the no-M2M constraint.
- Distance is computed once per row in PostGIS using `geography` for accuracy.
- `distance_meters * 3.28084` → feet for the CSV column.

**Performance expectation.** `propelio_comps` is ~24,847 rows growing. GIST index already exists on `geom` at `api/main.py:350`. Capture `EXPLAIN ANALYZE` on a representative workspace during implementation and confirm both the `ST_Within` filter and the KNN order use the index. Add as release gate (see §7).

### 3.6 Empty / invalid polygon handling (Copilot R3 BLOCKER fix)

**The problem.** The merged-job export path stores `cached_jobs.polygon` as an empty list `[]` at `api/main.py:2819`. Other code paths may also produce NULL or otherwise-invalid polygon values. If the new `query_propelio_sold_in_polygon` or its CSV re-resolve trigger fails on empty/invalid polygons, existing merged-job exports would break — a worst-case silent regression.

**The rule.** `query_propelio_sold_in_polygon` MUST be defensive about polygon validity. The contract:

| Polygon state | Behavior |
|---|---|
| Valid GeoJSON polygon with ≥3 points | Run the lateral query normally. |
| Empty list `[]` | Skip the SQL entirely. Return empty dict (no matches for any parcel). Log a single info-level message: `"propelio_sold: empty polygon for job_id={...}, skipping fetch"`. Do NOT raise. |
| `None` / NULL | Same as empty list — skip + log + empty result. |
| Malformed GeoJSON (e.g., 2-point list, non-list value) | Same as empty list — skip + log + empty result. Do NOT attempt to parse and crash; defensive `try/except (ValueError, TypeError)` around `ST_GeomFromGeoJSON` invocation. |

**Downstream effect.** When the result is empty, the CSV export proceeds normally:
- Every parcel's Propelio `Comp_*` cells stay blank
- Every parcel's `Comp Distance (ft)` cell stays blank
- The `RF_Comp_*` block on the same row is **completely independent** and fills normally from `sold_points` (which has its own behavior on empty polygons — unchanged, not our concern)
- CSV export succeeds. No HTTP 500. No analyst-facing failure.

**Why this is a blocker, not a warning.** Existing merged-job exports work today. If the new code path crashes or fails on empty polygons, we ship a regression at the moment Mike's data ship needs the CSV path stable. This rule MUST be implemented in the v1 cut.

## 4. Code changes (file by file)

| File / location | Change |
|---|---|
| `api/sold.py` | **No changes.** |
| `api/main.py` `_ensure_session_schema` | Add idempotent migration: `ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS propelio_sold_points JSONB`. Pattern matches existing migrations at `api/main.py:532` and `api/main.py:557`. |
| `api/main.py:611-633` `_persist_cached_job` | Persist `propelio_sold_points` alongside existing `sold_points`. Add to the single existing `INSERT ... ON CONFLICT` upsert and to the `UPDATE SET` clause — atomic with the existing write (same transaction). |
| `api/main.py:646` cached_jobs loader | **MANDATED (Copilot R3):** read `propelio_sold_points` via **named extraction** — either a separate `SELECT propelio_sold_points FROM cached_jobs WHERE job_id = %s` with a dict cursor, OR convert the existing load to `psycopg2.extras.RealDictCursor` so column access is by name. Do NOT add the new column to the existing positional SELECT + tuple unpack at `:668` — the positional-unpack footgun is too sharp and any future column addition would silently misalign. This is no longer discretionary. |
| `api/main.py:2557-2575` analyze pipeline | When `include_sold=true`, ALSO append `query_propelio_sold_in_polygon(parcel_input, polygon)` to the existing `asyncio.gather(*tasks, return_exceptions=True)` tasks list at `:2557-2575` — mirror the conditional append pattern Redfin/sold uses at `:2568-2569`. **Soft-fail mandated:** if the new call returns an `Exception`, log at warning level and treat as empty result. Mirror the `isinstance(raw_results[i], Exception)` pattern at `:2596-2599`. Propelio failure MUST NOT fail the analyze response. **Do NOT add `propelio_sold_points` as a top-level key in the analyze JSON response** — `sold_points` remains the only sold-related top-level field returned to the client. The new value is cached for CSV export only. |
| `api/main.py:3047` CSV export entry | Read `sold_points` (Redfin → RF_ block) via existing path and `propelio_sold_points` (Propelio → primary block) via the new named-extraction path. **Re-resolve flow with §3.6 polygon guard:** if `propelio_sold_points IS NULL`, call `query_propelio_sold_in_polygon(parcel_input, polygon)`. The function itself applies the §3.6 empty-polygon guard internally, so an empty/invalid polygon returns empty dict cleanly. Persist the result back to `cached_jobs.propelio_sold_points` (even if empty — caches the "we tried, nothing found" answer to skip re-resolve next time). Then generate CSV. |
| `api/main.py:3072-3090` `comp_by_parcel_key` builder | **Keep as-is** — now feeds RF_ block. |
| NEW: parallel `propelio_comp_by_parcel_key` builder | The lateral-nearest query already returns one row per parcel. Key by `(county, account)`. |
| `api/main.py:3071` `_COMP_THRESHOLD` | **Keep** — still used by the Redfin builder for the RF_ block. Don't remove. |
| `api/main.py:3097-3193` Header writer | Insert `Comp Distance (ft)` after the Comp_* group. Insert 10 `RF_Comp_*` headers **before** `Seed Target` so `share_id` remains the final column. |
| `api/main.py:3210-3357` Row-fill | Writer **contract** (dict keys it consumes) is preserved. Code changes inside this block are scoped to: (a) read primary comp from new `propelio_comp_by_parcel_key`, (b) write `Comp Distance (ft)` cell, (c) read auxiliary comp from existing `comp_by_parcel_key` and write into the 10 `RF_Comp_*` cells. The two reads MUST be independent — neither suppresses the other on empty match. |
| NEW: `api/propelio/csv_match.py` | `query_propelio_sold_in_polygon(session_conn, parcel_input, workspace_polygon_geojson) -> dict[(county, account)] -> normalized_row`. Holds: (a) the §3.6 polygon-validity guard, (b) the lateral SQL from §3.5, (c) the normalizer from §3.4. Returns dict shape ready for the CSV writer. **Match existing inline procedural style** — do NOT introduce helper classes or abstraction layers. Reuse `_safe_float` from `api/main.py:1264-1270`. |

## 5. Data flow

```
analyze request
    ↓
include_sold=true branch
    ↓
    ├── query_sold_parcels(parcel_conn)          → sold_points JSONB        (RF_ block source)
    └── query_propelio_sold_in_polygon(session_conn) → propelio_sold_points JSONB (Propelio block source)
    ↓
_persist_cached_job  →  cached_jobs row with both JSONB columns
    ↓
CSV export request
    ↓
load cached_jobs row
    ↓
    ├── if propelio_sold_points is NULL → re-resolve + persist
    ↓
build comp_by_parcel_key (Redfin) + propelio_comp_by_parcel_key (Propelio)
    ↓
write CSV: parcel cols + Propelio Comp_* + Comp Distance + other cols + RF_Comp_*
```

## 6. Schema migration

```sql
ALTER TABLE cached_jobs
ADD COLUMN IF NOT EXISTS propelio_sold_points JSONB;
```

Idempotent. Runs in `_ensure_session_schema` on app startup like all our other migrations. No downtime, no data backfill.

## 7. Validation plan

> **Baseline-capture discipline.** No automated tests exist for CSV export (subagent verified — see §9 risk row). The 14 manual steps below ARE the regression safety net. Before deploying commit 1 (the schema migration), capture **baseline CSVs of at least 3 representative workspaces** on current `develop`: (i) a known-bad coverage case like `0 CHADWICK DR`, (ii) a high-density urban workspace, (iii) a merged-job export with empty polygon. Save them in `tests/csv_baselines/` (gitignored) so post-deploy diffs are precise.

1. **Pre-deploy baseline.** Pull an export of the `0 CHADWICK DR` workspace on `develop`. Save the CSV. (Plus the other 2 baselines per the discipline above.)
2. **Deploy to preview** (`lot-ledger-preview`).
3. **Pull the same workspace** on preview. Diff vs baseline:
   - Propelio `Comp_*` columns should now be populated (if any Propelio sold comp exists in the polygon).
   - `Comp Distance (ft)` should be a sensible number.
   - `RF_Comp_*` columns should match the OLD `Comp_*` values from the baseline (same source, same logic).
   - **Trailing-column check:** `Seed Target` is the second-to-last column; `share_id` is the absolute last column. Diff `head -1 baseline.csv | tr ',' '\n' | tail -3` vs the new CSV — must match exactly.
4. **Spot-check** a matched comp: `RF_Comp_Sold_Price` and Propelio `Comp_Sold_Price` should differ in the typical Texas-non-disclosure way (Redfin estimate vs MLS actual).
5. **Empty-in-polygon test (no Propelio sold comps inside).** Create a tiny rural polygon with no Propelio sold comps inside. CSV should have blank Propelio block + blank `Comp Distance` + RF_ block independent (may or may not have data depending on Redfin coverage).
6. **🚨 Empty/invalid polygon test (merged-job path) — §3.6 enforcement.** Take a `cached_jobs` row produced by the merged-job export path (`polygon = []` at `api/main.py:2819`). Pull its CSV. Confirm: (a) export does NOT fail/raise/500, (b) every parcel's Propelio `Comp_*` cells are blank, (c) every parcel's `Comp Distance (ft)` is blank, (d) RF_Comp_* cells fill normally from existing `sold_points`. Also test a `cached_jobs` row with `polygon = NULL` and a row with malformed polygon (e.g., 1-point list) — all three cases must behave identically. This is the Copilot R3 blocker test.
7. **Performance smoke.** A workspace with ~500 parcels. Time the export. Should be no slower than current (lateral nearest with GIST index should be faster than the O(n*m) Python loop, but worst-case parity is acceptable for v1).
8. **Cache verification.** Inspect a `cached_jobs` row post-analyze. Confirm both `sold_points` and `propelio_sold_points` JSONB columns are populated. Re-pull the CSV without re-analyzing — should serve from cache, not re-query.
9. **Analyze endpoint payload unchanged.** Hit `/api/analyze` with the same workspace pre- and post-deploy. Diff the JSON response — `sold_points` field and overall payload shape must be identical, and NO `propelio_sold_points` top-level field appears. The frontend sold overlay (`map.js:7567-7571` confirmed by subagent) must render exactly the same.
10. **Cached-job NULL re-resolve path.** Take a `cached_jobs` row that pre-dates the deploy (so `propelio_sold_points IS NULL`). Pull a CSV from it. Confirm: (a) re-resolve fires once, (b) result (even if empty) is persisted back to the row, (c) second export from the same job hits the cache (no re-resolve, faster). Repeat with a pre-deploy row whose polygon is empty (§3.6) — re-resolve fires, returns empty, persists empty, second export hits cache.
11. **Parcels missing `lat`/`lng`.** Construct or find a workspace with at least one parcel that has NULL or zero `lat`/`lng`. CSV export must not crash; the parcel's Propelio block and `Comp Distance (ft)` cell stay blank; the RF_ block behaves as it always has for that parcel.
12. **Cross-county polygon.** Draw a workspace polygon that straddles two counties (e.g., Dallas + Tarrant border). Confirm `(county, account)` keying produces no collisions, both counties' parcels resolve correctly, AND the trailing-column check from step 3 still passes (last column remains `share_id`).
13. **`include_sold=false` job export.** Run an analyze with `include_sold=false`. Confirm CSV export from that job leaves both the Propelio Comp_* block AND the RF_Comp_* block blank for every row, and does not crash.
14. **Live `parsed_payload` URL key verification.** ✅ **COMPLETED 2026-05-15.** Inspected 500 latest `propelio_comps.parsed_payload` rows: zero contain `link`/`url`/`detail_url`/`web_url`/`listing_url`/`property_url`/`href`/`permalink`. The only URLs in payloads point to photo binaries at `extra.raw.photos[].url`. Propelio per-comp deep link requires `lead_id` not in our schema. Result folded into §3.4: `listing_url` hard-coded blank for v1; v2 can add a backend resolver endpoint for per-comp deep linking (see `[[project_propelio_listing_url]]`).
15. **`EXPLAIN ANALYZE` release gate.** Capture `EXPLAIN ANALYZE` output for the lateral query on a representative workspace (500+ parcels). Confirm the plan uses the GIST index on `propelio_comps.geom` for both the `ST_Within` filter and the `<->` KNN ordering. Attach output to the PR description.
16. **Soft-fail confirmation.** Temporarily induce a failure in `query_propelio_sold_in_polygon` (e.g., pass a deliberately-bad SQL in a test branch, or kill the session DB connection mid-call). Confirm: (a) analyze response still returns 200 with empty Propelio data, (b) warning log line emitted, (c) CSV export from that cached job fills Propelio cells blank and RF_ cells normally. Revert the induced failure.

## 8. Out of scope (v2 candidates)

- **OAC (out-of-polygon comps) inclusion in CSV.** Mike hasn't asked. Defer until requested.
- **Global sold-overlay swap.** `query_sold_parcels` is also consumed by the analyze pipeline (`api/main.py:2567`) and the frontend `sold_points` rendering (`frontend/map.js:1453`, `:1510`, `:2881`). Migrating those to Propelio is a separate spec — requires frontend coordination and cache invalidation strategy.
- **Restructured CSV shape.** Multi-sheet workbook, separate sections per source, etc. Mike has not asked. Hold.
- **Comp confidence flag.** Distance is the only quality signal in v1. We can layer in a confidence score later if Mike wants it.

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Empty/invalid polygon on merged jobs (R3 BLOCKER)** would fail re-resolve if not guarded → silently breaks existing merged-job CSV exports | §3.6 polygon-validity guard in `query_propelio_sold_in_polygon`. Empty/NULL/malformed polygon → skip SQL, return empty dict, log info-level message, never raise. Validation step #6 is the gate. |
| **No automated tests for CSV export exist** — the 14-step manual validation is the only regression safety net | Capture baseline CSVs of 3 representative workspaces pre-deploy (per §7 preamble). Diff post-deploy. Document the gap; automated tests are a v2 enhancement, not a v1 blocker. |
| GIST index on `propelio_comps.geom` — assumed but verify | **Confirmed by subagent at `api/main.py:350-352`.** No action needed. |
| Propelio rows missing `sqft` → `price_per_sqft` always NULL for those | Emit blank cell, don't fabricate. Use `_safe_float` guard. Documented in normalizer §3.4. |
| Propelio per-comp data has no listing-URL key (verified 2026-05-15) | Accepted. `Comp Listing URL` is hard-coded blank for the Propelio block in v1. Per-comp deep linking is a v2 enhancement requiring a backend resolver endpoint (`[[project_propelio_listing_url]]`). RF_Comp_Listing URL (Redfin block) remains populated. |
| Loader at `api/main.py:646` is positional unpack → adding `propelio_sold_points` to SELECT could misalign tuple unpacking | **Mandated named extraction** (separate `SELECT` or `RealDictCursor`). No discretion. |
| `share_id` moves off the rightmost column → silently breaks any downstream consumer reading positionally from the right edge | Insert RF_ block BEFORE `Seed Target`. `share_id` stays last. Validation steps #3 and #12 explicitly verify trailing-column shape (`tail -3` head-row check). |
| Workspace polygon stored as something other than GeoJSON in `cached_jobs` | Confirmed: `_persist_cached_job` stores polygon via `Json(polygon)` (`api/main.py:633`). Mirror the GeoJSON-string translation that `archive.py:598-599` uses. The §3.6 guard handles all non-conforming shapes (empty list, NULL, malformed) safely. |
| Cached jobs with NULL `propelio_sold_points` are re-resolved on first export → slower-than-usual first export per workspace post-deploy | One-time cost, only on cached jobs that pre-date the deploy. Document in release notes. Empty-polygon merged jobs cache the "empty result" (per §4) so subsequent exports are fast. |
| RF_ block contains a comp that's outside the workspace polygon (because Redfin uses radius, not polygon) | Correct behavior — RF_ is auxiliary reference data, not the truth column. Document in CSV intended-use note. |
| Distance computed from parcel centroid undersells the match quality for elongated parcels (e.g., 5-acre rectangular lot's edge could be 200ft closer than its centroid) | Accepted tradeoff. Centroid is consistent with how the rest of the app measures distance. Polygon-edge distance is a v2 enhancement. |
| Propelio query fails (network blip, malformed payload, SQL error) → could break the whole analyze response | **Mandated soft-fail** in §4. Mirrors existing Redfin/sold pattern at `api/main.py:2596-2599`. Validation step #16 confirms. |

## 10. Constraints honored

- **No M2M relationships.** This refactor is strictly 1:1 (one Propelio comp matched to one parcel) backed by SQL `LIMIT 1`. No join tables introduced. The subtle case (one Propelio comp being the closest match for multiple parcels) is N:1 from parcels to comps — many-to-one, not many-to-many.
- **Static `#active-item-slot`.** No frontend changes in this refactor, so this hard rule is trivially honored.
- **No `Co-Authored-By: Claude` trailer.** Commit messages on this branch will not include it.
- **Match existing inline procedural style.** The CSV writer at `api/main.py:3094-3357` is pure procedural — no helper classes, no abstraction layers, header strings inline, NULL handling via inline `if x is not None else ""`. The new `csv_match.py` module and its callers MUST follow this style. Do NOT introduce a `CompMatcher` class, `NormalizerProtocol`, or any other abstraction. Reuse `_safe_float` from `api/main.py:1264-1270` for numeric coercion. The only acceptable "new" structure is a small set of module-level functions in `csv_match.py`.

## 11. Branch + ship strategy

- Branch: `feat/csv-export-propelio-source` off `develop` (cut at spec time).
- Phased commits within the branch:
  1. **Schema migration commit — its own deploy gate.** `_ensure_session_schema` adds `cached_jobs.propelio_sold_points`. Deploy to preview, verify the column exists, then proceed. No read or write code yet depends on it — this lets the column exist before any code path that could fail on a missing column.
  2. New `query_propelio_sold_in_polygon` + normalizer in `api/propelio/csv_match.py` (no callers yet — pure addition).
  3. Analyze pipeline wiring (call new function, persist new cache column via the updated `_persist_cached_job`).
  4. CSV header writer changes (`Comp Distance (ft)` after Comp_* group, RF_Comp_* block before `Seed Target`).
  5. CSV row-fill changes (parallel comp_by_parcel_key builders; new distance write).
  6. Cache-aware re-resolve in CSV export path (NULL `propelio_sold_points` → fetch + persist before generating CSV).
- Deploy to `lot-ledger-preview` via `cloudbuild-preview.yaml` after each substantive commit (especially commit 1 as the schema gate).
- Smoke test §7 on preview. Capture `EXPLAIN ANALYZE` for §7 step 13 release gate.
- Promote to `develop` (auto-deploys to `lot-ledger-dev`).
- Optional: hold off merging to `main` until Mike confirms the new CSV is what he wants, since the next data ship to his GCP could happen on either side of this change.

## 12. Open questions

**None.** Spec is fully locked at v1.3. All Copilot R1/R2/R3 findings + two subagent verification passes + live-data verification of `parsed_payload` URL keys folded in. The §7 step 14 live-data inspection completed 2026-05-15 (folded into §3.4 and §9). Ready for phased implementation per §11.

## 13. Code anchors (for the implementer)

| What | Where |
|---|---|
| Current CSV export endpoint | `api/main.py:3049` onward; `generate_csv()` around `:3094` |
| Existing Redfin path | `api/sold.py:88` `query_sold_parcels`, unchanged |
| Tight-threshold builder | `api/main.py:3072-3090`, unchanged (now feeds RF_ block) |
| Header writer (static list, extensible) | `api/main.py:3097-3193`; trailing two columns `Seed Target` then `share_id` at `:3191-3192` |
| Row-fill (pure procedural, inline NULL handling) | `api/main.py:3210-3357`; comp-dict reads at `:3345-3354` |
| `_safe_float` helper (reuse for numeric coercion) | `api/main.py:1264-1270` |
| Multi-county parcel merge + dedup | `api/main.py:2618-2628` (gold-standard `(account_num, county)` keying) |
| Merged-job empty-polygon storage | `api/main.py:2819` (writes `polygon = []` — drives the §3.6 guard) |
| Existing comp persistence | `api/main.py:611-633` `_persist_cached_job` (single atomic upsert) |
| Cached_jobs loader (positional unpack footgun) | `api/main.py:646-668` |
| Analyze pipeline sold call | `api/main.py:2557-2575` (tasks list, `asyncio.gather`); soft-fail at `:2596-2599` |
| Analyze response shape | `api/main.py:2651` — `sold_points` only; do NOT add `propelio_sold_points` |
| Gold-standard polygon query pattern | `api/propelio/archive.py:598` |
| Existing haversine (Python — not used in this refactor) | `api/geo.py:46-58` |
| Frontend sold consumers (DO NOT TOUCH in v1) | `frontend/map.js:7567-7571` (subagent-verified shape) |
| Existing distance prior-art (parcel-to-target rendering) | `frontend/map.js` per commit `21aeebb`; see `[[PARCEL_DISTANCE_TOOLS_SPEC]]` |
| Historical context commits | `707a009` (share_id moved to rightmost — DO NOT undo); `cc3a71d` (where `_COMP_THRESHOLD = 0.00135` was first introduced, no design debate); `8be6669` (cached_jobs.sold_points day-one); `966d503` / `e2ebe6d` (multi-county sequential-vs-concurrent lessons) |

## 14. Changelog

- **v1.0** (commit `20385a8`): Initial formal spec. Folded Copilot R1 findings on the design summary (scope-creep avoidance, schema-shape drift call-out).
- **v1.1** (commit `270d21b`): Folded Copilot R2 findings. Fixed broken `CROSS JOIN LATERAL ... ON true` syntax → `LEFT JOIN LATERAL`. Corrected `latitude`/`longitude` → `lat`/`lng`/`geom`. Pinned `sqft` (not `living_sqft`). Removed `lead_id` URL synthesis. Protected `share_id` positional contract (RF_ block before `Seed Target`). Resolved row-fill-untouched contradiction. Added 6 validation tests. Schema migration as own deploy gate.
- **v1.2** (commit `13c770d`): Folded Copilot R3 + two subagent verification passes (schema verification + architecture history). **Added §3.6 empty-polygon handling (R3 BLOCKER fix)** — merged-job exports with `polygon = []` no longer break. Mandated **named extraction** at cached_jobs loader (was discretionary). Mandated **soft-fail** error handling on the new query (mirror Redfin pattern). Mandated **no top-level `propelio_sold_points`** in analyze response. Pinned **URL fallback probe order** (`link` → `url` → `detail_url`). Removed non-existent `unit`/`city`/`state`/`zip` from normalizer. Added live-data verification step (§7 step 14). Added baseline-CSV capture discipline. Documented absent automated tests as primary risk. Mandated reuse of `_safe_float` + no new abstractions (§10). Added historical commit anchors (§13). Added validation steps #6 (merged-job empty polygon — the blocker test) and #16 (soft-fail confirmation). Fixed validation trailing-column verification (was implicit, now explicit `tail -3` diff in steps 3 + 12).
- **v1.3** (this commit): Live-data verification of `parsed_payload` URL keys completed. 500 latest `propelio_comps` rows show **zero** rows with `link`/`url`/`detail_url`/etc. — none of the probed keys exist. §3.4 normalizer updated to hard-code `listing_url = ""` for the Propelio block in v1; per-comp deep linking is deferred to v2 pending a backend resolver endpoint (`[[project_propelio_listing_url]]`). §7 step 14 marked complete. §9 risk row updated. §12 open-questions cleared. Spec is fully locked, ready for Phase 1 implementation.
