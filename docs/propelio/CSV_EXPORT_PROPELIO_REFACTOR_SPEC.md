---
title: CSV Export — Propelio Comp Source Refactor
status: spec v1.1 — Copilot R1 + R2 findings folded
branch: feat/csv-export-propelio-source
created: 2026-05-15
related:
  - "[[CSV_EXPORT_REFACTOR_BRAINSTORM_WIP]]"
  - "[[mike_gcp_handoff_plan]]"
  - "[[PARCEL_DISTANCE_TOOLS_SPEC]]"
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

A new helper maps a `propelio_comps` row into the dict shape the existing CSV writer expects (`sold_price`, `sold_date`, `price_per_sqft`, `yr_built`, `lot_sqft`, `listing_url`, etc.). The CSV writer's **contract** is preserved — the writer still receives the same dict keys it always did. Code changes inside the row-fill block are limited to (a) reading from the new `propelio_comp_by_parcel_key` dict instead of (or in addition to) the existing one, and (b) writing the new `Comp Distance (ft)` value plus the appended `RF_Comp_*` cells.

Field mapping (verified against schema at `api/main.py:297-323` and `scripts/backfill_propelio_comps.py:101-133`):

| CSV writer expects | Propelio column |
|---|---|
| `sold_price` | `price` |
| `sold_date` | `sold_date` (already derived from `close` in existing ingest) |
| `price_per_sqft` | computed: `price / sqft` when both present and `sqft > 0`, else NULL |
| `yr_built` | `year_built` |
| `lot_sqft` | `lot_size` |
| `sqft` | `sqft` (direct) |
| `beds` / `baths` / `dom` | direct mapping |
| `address` / `unit` / `city` / `state` / `zip` | direct mapping |
| `listing_url` | **Fallback hierarchy:** (1) `parsed_payload['link']` if present and non-empty; (2) any URL-like field inside `parsed_payload` (e.g., `parsed_payload['url']`, `parsed_payload['detail_url']`) — implementer's discretion which to probe; (3) **emit empty cell**. Do NOT synthesize a Propelio Genesis URL from a hypothetical `lead_id` — that field is not in the `propelio_comps` schema and is not guaranteed to exist in `parsed_payload`. Better to emit blank than a broken link. |

Anything missing in Propelio → emit empty cell in the CSV. Don't fabricate.

### 3.5 SQL query shape

**Parcel input source.** The parcel set for the lateral join is loaded from the same source `query_sold_parcels` is currently fed — i.e., the parcel rows materialized for the job (`account_num`, `county`, `lat`, `lng`). The implementer should match the existing parcel input contract rather than re-deriving from the cached_jobs JSONB. Re-derivation is only a fallback if the existing path is unavailable.

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

## 4. Code changes (file by file)

| File / location | Change |
|---|---|
| `api/sold.py` | **No changes.** |
| `api/main.py` `_ensure_session_schema` | Add idempotent migration: `ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS propelio_sold_points JSONB`. Pattern matches existing migrations at `api/main.py:532` and `api/main.py:557`. |
| `api/main.py:611-633` `_persist_cached_job` | Persist `propelio_sold_points` alongside existing `sold_points`. Add to the single existing `INSERT ... ON CONFLICT` upsert — atomic. |
| `api/main.py:646` cached_jobs loader | **Footgun warning.** Existing loader is positional SELECT/unpack. Adding `propelio_sold_points` to the SELECT requires careful update of the unpacking tuple in the same commit, or the safer pattern: read `propelio_sold_points` with a separate named lookup (e.g., dict cursor for this one column) to avoid index drift across the codebase. Implementer's discretion, but call this out in the PR description. |
| `api/main.py:2567` analyze pipeline | When `include_sold=true`, also call new `query_propelio_sold_in_polygon(polygon)` and cache result as `propelio_sold_points`. Separate from existing `query_sold_parcels` call. Do **not** modify the analyze response payload shape — `sold_points` remains the only sold field returned to the client. |
| `api/main.py:3047` CSV export entry | Read both `sold_points` (Redfin → RF_ block) and `propelio_sold_points` (Propelio → primary block). If `propelio_sold_points` is NULL, re-resolve via `query_propelio_sold_in_polygon` and persist before generating CSV. |
| `api/main.py:3072-3090` `comp_by_parcel_key` builder | **Keep as-is** — now feeds RF_ block. |
| NEW: parallel `propelio_comp_by_parcel_key` builder | The lateral-nearest query already returns one row per parcel. Key by `(county, account)`. |
| `api/main.py:3071` `_COMP_THRESHOLD` | **Keep** — still used by the Redfin builder for the RF_ block. Don't remove. |
| `api/main.py:3097-3193` Header writer | Insert `Comp Distance (ft)` after the Comp_* group. Insert 10 `RF_Comp_*` headers **before** `Seed Target` so `share_id` remains the final column. |
| `api/main.py:3210-3357` Row-fill | Writer **contract** (dict keys it consumes) is preserved. Code changes inside this block are scoped to: (a) read primary comp from new `propelio_comp_by_parcel_key`, (b) write `Comp Distance (ft)` cell, (c) read auxiliary comp from existing `comp_by_parcel_key` and write into the 10 `RF_Comp_*` cells. The two reads MUST be independent — neither suppresses the other on empty match. |
| NEW: `api/propelio/csv_match.py` | `query_propelio_sold_in_polygon(session_conn, parcel_input, workspace_polygon_geojson) -> dict[(county, account)] -> normalized_row`. Holds both the lateral SQL and the normalizer from §3.4. Returns dict shape ready for the CSV writer. |

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

1. **Pre-deploy baseline.** Pull an export of the `0 CHADWICK DR` workspace on `develop`. Save the CSV.
2. **Deploy to preview** (`lot-ledger-preview`).
3. **Pull the same workspace** on preview. Diff vs baseline:
   - Propelio `Comp_*` columns should now be populated (if any Propelio sold comp exists in the polygon).
   - `Comp Distance (ft)` should be a sensible number.
   - `RF_Comp_*` columns at the far right should match the OLD `Comp_*` values from the baseline (same source, same logic).
4. **Spot-check** a matched comp: `RF_Comp_Sold_Price` and Propelio `Comp_Sold_Price` should differ in the typical Texas-non-disclosure way (Redfin estimate vs MLS actual).
5. **Empty-polygon test.** Create a tiny rural polygon with no Propelio sold comps inside. CSV should have blank Propelio block + blank `Comp Distance` + RF_ block independent (may or may not have data depending on Redfin coverage).
6. **Performance smoke.** A workspace with ~500 parcels. Time the export. Should be no slower than current (lateral nearest with GIST index should be faster than the O(n*m) Python loop, but worst-case parity is acceptable for v1).
7. **Cache verification.** Inspect a `cached_jobs` row post-analyze. Confirm both `sold_points` and `propelio_sold_points` JSONB columns are populated. Re-pull the CSV without re-analyzing — should serve from cache, not re-query.
8. **Analyze endpoint payload unchanged.** Hit `/api/analyze` with the same workspace pre- and post-deploy. Diff the JSON response — `sold_points` field and overall payload shape must be identical. The frontend sold overlay (`map.js:1453`, `:1510`, `:2881`) must render exactly the same.
9. **Cached-job NULL re-resolve path.** Take a `cached_jobs` row that pre-dates the deploy (so `propelio_sold_points IS NULL`). Pull a CSV from it. Confirm: (a) re-resolve fires once, (b) result is persisted back to the row, (c) second export from the same job hits the cache (no re-resolve, faster).
10. **Parcels missing `lat`/`lng`.** Construct or find a workspace with at least one parcel that has NULL or zero `lat`/`lng`. CSV export must not crash; the parcel's Propelio block and `Comp Distance (ft)` cell stay blank; the RF_ block behaves as it always has for that parcel.
11. **Cross-county polygon.** Draw a workspace polygon that straddles two counties (e.g., Dallas + Tarrant border). Confirm `(county, account)` keying produces no collisions and both counties' parcels resolve correctly.
12. **`include_sold=false` job export.** Run an analyze with `include_sold=false`. Confirm CSV export from that job leaves both the Propelio Comp_* block AND the RF_Comp_* block blank for every row, and does not crash.
13. **`EXPLAIN ANALYZE` release gate.** Capture `EXPLAIN ANALYZE` output for the lateral query on a representative workspace (500+ parcels). Confirm the plan uses the GIST index on `propelio_comps.geom` for both the `ST_Within` filter and the `<->` KNN ordering. Attach output to the PR description.

## 8. Out of scope (v2 candidates)

- **OAC (out-of-polygon comps) inclusion in CSV.** Mike hasn't asked. Defer until requested.
- **Global sold-overlay swap.** `query_sold_parcels` is also consumed by the analyze pipeline (`api/main.py:2567`) and the frontend `sold_points` rendering (`frontend/map.js:1453`, `:1510`, `:2881`). Migrating those to Propelio is a separate spec — requires frontend coordination and cache invalidation strategy.
- **Restructured CSV shape.** Multi-sheet workbook, separate sections per source, etc. Mike has not asked. Hold.
- **Comp confidence flag.** Distance is the only quality signal in v1. We can layer in a confidence score later if Mike wants it.

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `propelio_comps` lacks GIST index on geom → lateral query slow | Confirm index exists during implementation. Add if missing. |
| Propelio rows missing `sqft` → `price_per_sqft` always NULL for those | Emit blank cell, don't fabricate. Documented in normalizer §3.4. |
| Propelio rows where `parsed_payload['link']` is absent → `listing_url` blank | Accepted. Do NOT synthesize a fake URL from non-existent `lead_id`. Better blank than broken. Documented in normalizer §3.4. |
| Loader at `api/main.py:646` is positional unpack → adding `propelio_sold_points` to SELECT could misalign tuple unpacking across the codebase | Either update unpacking tuple in the same commit as the SELECT change, or read `propelio_sold_points` via a separate named lookup. Call out in PR description. |
| `share_id` moves off the rightmost column → silently breaks any downstream consumer that reads positionally from the right edge | Insert RF_ block BEFORE `Seed Target`. `share_id` stays last. Validation step #11 (cross-county) implicitly verifies trailing-column shape. |
| Workspace polygon stored as something other than GeoJSON in `cached_jobs` | Verify the polygon format used by `archive.py:598-599` and reuse the exact same translation. |
| Cached jobs with NULL `propelio_sold_points` are re-resolved on first export → slower-than-usual first export per workspace post-deploy | One-time cost, only on cached jobs that pre-date the deploy. Document in release notes. |
| RF_ block contains a comp that's outside the workspace polygon (because Redfin uses radius, not polygon) | This is correct behavior — RF_ is auxiliary reference data, not the truth column. Document in the CSV's intended-use note (if we have one). |
| Distance computed from parcel centroid undersells the match quality for elongated parcels (a 5-acre rectangular lot's edge could be 200ft closer than its centroid) | Accepted tradeoff. Centroid is consistent with how the rest of the app measures. Polygon-edge distance is a v2 enhancement. |

## 10. Constraints honored

- **No M2M relationships.** This refactor is strictly 1:1 (one Propelio comp matched to one parcel) backed by SQL `LIMIT 1`. No join tables introduced.
- **Static `#active-item-slot`.** No frontend changes in this refactor, so this hard rule is trivially honored.
- **No `Co-Authored-By: Claude` trailer.** Commit messages on this branch will not include it.

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

None at spec lock. All Copilot R1 and R2 findings folded in (v1.1). Ready for implementation planning via the writing-plans skill.

## 13. Code anchors (for the implementer)

| What | Where |
|---|---|
| Current CSV export endpoint | `api/main.py:3049` onward; `generate_csv()` around `:3094` |
| Existing Redfin path | `api/sold.py:88` `query_sold_parcels`, unchanged |
| Tight-threshold builder | `api/main.py:3072-3090`, unchanged (now feeds RF_ block) |
| Header writer | `api/main.py:3097-3193` |
| Row-fill | `api/main.py:3210-3357` |
| Existing comp persistence | `api/main.py:611-633` `_persist_cached_job` |
| Analyze pipeline sold call | `api/main.py:2567` |
| Gold-standard polygon query pattern | `api/propelio/archive.py:598-599` |
| Existing haversine | `api/geo.py:46-58` (Python — we'll use PostGIS instead for the lateral query) |
| Frontend sold consumers (DO NOT TOUCH in v1) | `frontend/map.js:1453`, `:1510`, `:2881` |
