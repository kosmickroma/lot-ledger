---
title: CSV Export — Propelio Comp Source Refactor
status: spec v1 — ready for Copilot R2 review
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

The current CSV has 94 columns. After this refactor it has approximately **105 columns**, laid out:

| Position | Block | Source | Notes |
|---|---|---|---|
| Existing | Parcel data (county, address, lot dims, etc.) | Parcel DB | Untouched |
| **Existing 10 `Comp_*` cols (repurposed)** | **Propelio (primary)** | `propelio_comps`, polygon-bounded, closest match | Was Redfin; now MLS truth |
| **NEW: `Comp Distance (ft)`** | **Propelio match distance** | Computed via PostGIS `ST_Distance` from parcel centroid | Match-quality signal for analysts |
| Existing other cols | unchanged | unchanged | unchanged |
| **NEW far-right block: 10 `RF_Comp_*` cols** | **Redfin (auxiliary)** | `query_sold_parcels` (unchanged code path) | Renamed from original `Comp_*`. Strip in Excel if undesired. |

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

A new helper maps a `propelio_comps` row into the dict shape the existing CSV writer expects (`sold_price`, `sold_date`, `price_per_sqft`, `yr_built`, `lot_sqft`, `listing_url`, etc.). This keeps the CSV row-fill code at `api/main.py:3210-3357` untouched.

Field mapping:

| CSV writer expects | Propelio column |
|---|---|
| `sold_price` | `price` |
| `sold_date` | `sold_date` (derived from `close` in existing ingest) |
| `price_per_sqft` | computed: `price / living_sqft` when both present, else NULL |
| `yr_built` | `year_built` |
| `lot_sqft` | `lot_size` |
| `listing_url` | from `parsed_payload['link']` if present, else build `https://genesis.propelio.com/search/{lead_id}` |
| `address` / `unit` / `city` / `state` / `zip` | direct mapping (same names or close) |
| `beds` / `baths` / `living_sqft` | direct mapping |

Anything missing in Propelio → emit empty cell in the CSV. Don't fabricate.

### 3.5 SQL query shape

The lateral nearest, conceptually (final SQL to be refined during implementation, but this is the shape):

```sql
SELECT
    p.parcel_county,
    p.parcel_account_num,
    p.lat AS parcel_lat,
    p.lng AS parcel_lng,
    c.*,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)::geography,
        ST_SetSRID(ST_MakePoint(c.longitude, c.latitude), 4326)::geography
    ) AS distance_meters
FROM parcels_in_workspace p
CROSS JOIN LATERAL (
    SELECT *
    FROM propelio_comps
    WHERE status = 'sold'
      AND ST_Within(
          ST_SetSRID(ST_MakePoint(longitude, latitude), 4326),
          ST_GeomFromGeoJSON(:workspace_polygon_geojson)
      )
    ORDER BY ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) <-> ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)
    LIMIT 1
) c ON true;
```

`distance_meters * 3.28084` to get feet for the CSV column.

Performance expectation: `propelio_comps` has ~24,847 rows growing. Confirm GIST index on the spatial column during implementation. Confirm explain plan looks reasonable on a representative workspace.

## 4. Code changes (file by file)

| File / location | Change |
|---|---|
| `api/sold.py` | **No changes.** |
| `api/main.py` `_ensure_session_schema` | Add idempotent migration: `ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS propelio_sold_points JSONB`. |
| `api/main.py:611-633` `_persist_cached_job` | Persist `propelio_sold_points` alongside existing `sold_points`. |
| `api/main.py:2567` analyze pipeline | When `include_sold=true`, also call new `query_propelio_sold_in_polygon(polygon)` and cache result as `propelio_sold_points`. Separate from existing `query_sold_parcels` call. |
| `api/main.py:3047` CSV export entry | Read both `sold_points` (Redfin → RF_ block) and `propelio_sold_points` (Propelio → primary block). If `propelio_sold_points` is NULL, re-resolve and persist before generating CSV. |
| `api/main.py:3072-3090` `comp_by_parcel_key` builder | **Keep as-is** — now feeds RF_ block. |
| NEW: parallel `propelio_comp_by_parcel_key` builder | Trivial — the lateral-nearest query already returns one row per parcel. Just key by `(county, account)`. |
| `api/main.py:3071` `_COMP_THRESHOLD` | **Keep** — still used by the Redfin builder for the RF_ block. Don't remove. |
| `api/main.py:3097-3193` Header writer | Insert `Comp Distance (ft)` after the Comp_* group. Append 10 `RF_Comp_*` headers at the far right. |
| `api/main.py:3210-3357` Row-fill | For Propelio block: read from `propelio_comp_by_parcel_key`, write distance. For RF_ block: read from existing `comp_by_parcel_key` (the dict that fed the old Comp_* columns). |
| NEW: `api/propelio/csv_match.py` | `query_propelio_sold_in_polygon(session_conn, workspace_polygon_geojson) -> dict[(county, account)] -> match_row`. Returns dict shape ready for the CSV writer (already normalized). Contains the normalizer too. |

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

## 8. Out of scope (v2 candidates)

- **OAC (out-of-polygon comps) inclusion in CSV.** Mike hasn't asked. Defer until requested.
- **Global sold-overlay swap.** `query_sold_parcels` is also consumed by the analyze pipeline (`api/main.py:2567`) and the frontend `sold_points` rendering (`frontend/map.js:1453`, `:1510`, `:2881`). Migrating those to Propelio is a separate spec — requires frontend coordination and cache invalidation strategy.
- **Restructured CSV shape.** Multi-sheet workbook, separate sections per source, etc. Mike has not asked. Hold.
- **Comp confidence flag.** Distance is the only quality signal in v1. We can layer in a confidence score later if Mike wants it.

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `propelio_comps` lacks GIST index on geom → lateral query slow | Confirm index exists during implementation. Add if missing. |
| Propelio rows missing `living_sqft` → `price_per_sqft` always NULL for those | Emit blank cell, don't fabricate. Documented in normalizer. |
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
  1. Schema migration (`_ensure_session_schema` + `cached_jobs.propelio_sold_points`)
  2. New `query_propelio_sold_in_polygon` + normalizer in `api/propelio/csv_match.py`
  3. Analyze pipeline wiring (call new function, persist new cache column)
  4. CSV header writer changes (Comp Distance + RF_ block)
  5. CSV row-fill changes (parallel builders)
  6. Cache-aware re-resolve in CSV export path
- Deploy to `lot-ledger-preview` via `cloudbuild-preview.yaml` after each substantive commit.
- Smoke test §7 on preview.
- Promote to `develop` (auto-deploys to `lot-ledger-dev`).
- Optional: hold off merging to `main` until Mike confirms the new CSV is what he wants, since the next data ship to his GCP could happen on either side of this change.

## 12. Open questions

None at spec lock. All Copilot R1 findings folded in. Ready for R2 review on the spec itself before implementation begins.

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
