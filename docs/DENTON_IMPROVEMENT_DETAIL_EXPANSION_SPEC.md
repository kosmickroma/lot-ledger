---
title: Denton residential detail expansion — Phase 3 (mirror DCAD Phase 1 pattern)
status: DRAFT v1 for KK greenlight + Copilot critique
date: 2026-05-21
branch: feat/denton-improvement-detail-2026-05-21
deployment: PREVIEW ONLY for whole arc; gated promote to develop/main
parent docs:
  - docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md (DCAD canonical-field contract)
  - docs/CAD_DATA_SOURCES_DEEP_DIVE_2026_05_21.md (county data audit)
---

# Denton Improvement Detail Expansion Spec

## What this changes

Surfaces residential detail (foundation, roof, exterior wall, heating/cooling, bedrooms, fireplaces, condition rating, sprinkler, interior finish, flooring, etc.) for Denton parcels — using the **Denton CAD 2025 certified data extract** (`CertifiedDataAllProperty/Denton_AppraisalDataExtracts_ALL(CERTIFIED).zip`, 414 MB, freely downloadable from `dentoncad.net`).

Mirrors the DCAD Phase 1 pattern via the canonical-field contract in `docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md`. Same `feature.properties` keys (`props.foundation_type`, `props.roof_material`, etc.) — frontend popup + Subject Property card automatically render the new fields without any UI code change.

## Audit summary (completed 2026-05-21)

**Source files on disk at `ingest/counties/denton/cad/certified_2025/`:**
- `2025-07-28_2025_APPRAISAL_HEADER.TXT` (246 bytes — file metadata)
- `2025-07-28_2025_APPRAISAL_IMPROVEMENT_INFO.TXT` (47 MB) — one row per improvement (~404k rows)
- `2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL.TXT` (935 MB) — one row per detail (sub-area)
- `2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` (350 MB) — **3,934,349 attribute rows** covering 307,673 parcels
- `2025-07-28_2025_APPRAISAL_LAND_DETAIL.TXT` (75 MB) — land segments
- `layout/TP_Legacy_8.0.32_AppraisalExportLayout.xlsx` — field-width spec

**Format**: Fixed-width text (mainframe-era TrueAutomation CAMA export). Need byte-offset parser, not CSV reader. Layout xlsx documents start/end columns per field.

**Schema is 3-level relational:**

```
Property (denton_parcels.account_num)
  ↓ 1:N
Improvement (imprv_type_cd: 'R'=Residential, 'I'=MiscImp, 'M'=Mobile)
  ↓ 1:N
ImprovementDetail (imprv_det_type_cd: 'MA'=MainArea, 'AG'=AttachedGarage, 'OP'=OpenPorch, etc.)
  ↓ 1:N
Attributes (imprv_attr_desc: 'Foundation', 'Roof Covering', 'Bedrooms', etc.)
```

**JOIN coverage to our existing `denton_parcels`:**
- Total denton_parcels: 375,884
- Residential parcels (state_cd ILIKE 'A%'): 299,877
- Parcels with improvement attrs in certified: 307,673
- DB ∩ certified_attr: **301,935 = 100.7% of residential** (>100% because attribute file includes some commercial too)
- Join key: `denton_parcels.account_num` = `imprv_attr.prop_id` (zero-padded 12-char → lstrip leading zeros)

## Field mapping vs DCAD canonical contract

| DCAD canonical key | Denton attribute description | Sample codes |
|---|---|---|
| `foundation_type` | Foundation | SLAB / PIER/BEAM / PIER / MASON / CONCRETE B |
| `roof_material` | Roof Covering | Compositio / Asphalt / Metal / Slate / Spanish Ti / Copper / Wood Shake |
| `roof_type` | Roof Style | Gable / Hip / Mansard / Flat / Dome |
| `ext_wall` | Exterior Wall | Brick Vene / Stucco / Hardboard / Cedar / Log / Adobe Bloc / Aluminum s |
| `heating_type` | Heating/Cooling (parse: CH*) | CHCA / CH / Gas Stove / Fireplace / Fuel Furna |
| `ac_type` | Heating/Cooling (parse: *CA / Central Air) | CHCA (Central Heat + Central Air) |
| `beds` | Bedrooms (preferred) → Number of Bedrooms (fallback) | 1-9+ |
| `fireplaces` | Fireplace (preferred — numeric) → Fireplaces (fallback — size code) | 0-6, or S1/S2/D1 |
| `cdu_rating` | Condition | Excellent / Good / Average / Fair / Best / High / Low |
| `bldg_class` | Construction Style | Ranch / Contemporary / A Frame / Mediterranean / Reinforced |
| `sprinkler_flag` | Sprinkler System | Y / N / AVG / GOOD / EXCELLENT (normalize to T/F/'' via _normalize_flag) |
| `eff_yr_built` | (from imprv_detail.yr_built where depreciation_yr > yr_built) | numeric year |
| `pct_complete` | (not in attr file — may be in IMPROVEMENT_DETAIL imprv_det_val/imprv_det_area calc) | derived |
| `full_baths` | (not directly — Denton uses "Plumbing" count) | derived heuristic |
| `half_baths` | (not directly) | derived heuristic |
| `baths` | (derived from Plumbing) | numeric |
| `stories` | (not in attr file — may be in imprv_det_class_cd) | derived from FB1 / FB2 etc. class codes |
| **NEW (vs DCAD):** | Interior Finish | Sheetrock / Drywall / Plaster / Concrete |
| **NEW:** | Flooring | Carpet / Tile / Wood / Vinyl / Marble / Concrete |
| **NEW:** | Plumbing (fixture count) | numeric 0-12 |

**Out of scope (defer):**
- `garage_capacity` — Denton's "Attached Garage" is in IMPROVEMENT_DETAIL (separate sub-area row), not a per-house attribute. Can derive from presence/area of AG details, but not direct field. Defer to a follow-up.
- Spa/Sauna/Deck — Denton doesn't expose these as standard attributes (probably bundled in "Accessories" Allowance code). Skip.
- Half-bath split — Denton lumps as "Plumbing" count.
- Stories — encoded in class code (FB1 = First-floor 1-Story?), needs decoder.

## Schema design

### Decision: new `denton_improvement_detail` table (mirrors DCAD's `res_detail`)

Same architectural pattern as DCAD. One row per Denton residential parcel. LEFT JOIN at query time in `api/counties/denton.py:query_denton_parcels`. Keeps Denton ingest expansion isolated from existing `denton_parcels` schema (no risk to existing fields).

```sql
CREATE TABLE IF NOT EXISTS denton_improvement_detail (
    prop_id           TEXT PRIMARY KEY,    -- zero-padded? or stripped? See "Key normalization" below.
    -- Improvement-level summary (from IMPROVEMENT_INFO, primary residential improvement)
    imprv_id          TEXT,
    imprv_type_cd     TEXT,                -- 'R' / 'I' / 'M'
    imprv_homesite    TEXT,                -- 'Y' / 'N'
    imprv_val         NUMERIC(14,2),
    -- Main detail summary (from IMPROVEMENT_DETAIL, the 'MA' Main Area row)
    main_det_id       TEXT,
    main_det_class    TEXT,                -- e.g. 'FB1' (first-floor 1-story?)
    yr_built          INTEGER,
    eff_yr_built      INTEGER,
    main_area_sqft    NUMERIC(15,2),
    -- Aggregated attributes (one cell per DCAD canonical key)
    foundation_type   TEXT,
    roof_material     TEXT,
    roof_type         TEXT,
    ext_wall          TEXT,
    heating_type      TEXT,
    ac_type           TEXT,
    beds              INTEGER,
    fireplaces        INTEGER,
    cdu_rating        TEXT,
    bldg_class        TEXT,                -- from "Construction Style"
    sprinkler_flag    TEXT,                -- canonical 'T'/'F'/'' per _normalize_flag
    plumbing_count    INTEGER,
    interior_finish   TEXT,                -- NEW (Denton-only)
    flooring          TEXT,                -- NEW (Denton-only)
    -- Provenance
    source_snapshot   DATE,
    ingested_at       TIMESTAMP WITH TIME ZONE DEFAULT now(),
    CONSTRAINT denton_improvement_detail_prop_id_key UNIQUE (prop_id)
);
CREATE INDEX IF NOT EXISTS idx_denton_imprv_detail_prop_id ON denton_improvement_detail (prop_id);
```

### Key normalization (locked decision)

Denton certified files use zero-padded 12-char prop_ids ("000000000008"). Our `denton_parcels.account_num` stores stripped integer-as-text ("8"). **Strip leading zeros at ingest time** so `denton_improvement_detail.prop_id` = "8", matching `denton_parcels.account_num`. Saves a CAST at every query.

## Ingest expansion

### New script: `scripts/build_denton_improvement_detail.py`

Mirrors `scripts/build_dcad_res_detail_backfill.py` pattern. Idempotent + reportable.

**Stages:**

1. **Parse `APPRAISAL_HEADER.TXT`** for snapshot date + appraisal year (sanity-check we're reading the right file).
2. **Stream `APPRAISAL_IMPROVEMENT_INFO.TXT`** — build dict `improvements_by_prop_id` keyed by (prop_id, imprv_id) → {type_cd, homesite, val}. Prefer rows with `imprv_homesite='Y' AND imprv_type_cd='R'`. If multiple residential improvements per parcel, take highest `imprv_val` (the main house).
3. **Stream `APPRAISAL_IMPROVEMENT_DETAIL.TXT`** — build dict `main_detail_by_prop_id` keyed by prop_id → {imprv_det_id, det_class, yr_built, eff_yr_built, area}. Pick the `imprv_det_type_cd='MA'` (Main Area) row from the selected improvement.
4. **Stream `APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT`** — build dict `attrs_by_prop_id` keyed by prop_id → {foundation: ..., roof_material: ..., beds: ..., ...}. Apply per-attribute normalization rules (see "Attribute normalization" below).
5. **Compose final per-parcel rows** — merge improvements + main detail + attrs.
6. **Bulk-upsert** via temp-table-JOIN pattern (same as DCAD backfill). Idempotent.
7. **Report** — coverage stats, field-population percentages, sanity-check counts (e.g., parcels with bedrooms > 20 = data quality fail count).

### Attribute normalization rules

For each parcel, walk all its attr rows. For each canonical key, pick the BEST value per these rules:

- **`foundation_type`**: first non-empty `Foundation` value. Expand truncated codes via lookup (`CONCRETE B` → `CONCRETE BLOCK`, `PIER/BEAM` → `Pier and Beam`).
- **`roof_material`**: first non-empty `Roof Covering` value. Expand (`Compositio` → `Composition`, `Spanish Ti` → `Spanish Tile`).
- **`roof_type`**: first non-empty `Roof Style` value.
- **`ext_wall`**: first non-empty `Exterior Wall` value. Expand (`Brick Vene` → `Brick Veneer`, `Aluminum s` → `Aluminum Siding`).
- **`heating_type`** + **`ac_type`**: parse `Heating/Cooling` code. CHCA → heating=Central, ac=Central. CH → heating=Central, ac=None. Allowance / blank → both N/A.
- **`beds`**: from `Bedrooms` attribute (codes 1-9, '9+' → 9). If missing, fall back to `Number of Bedrooms` BUT only if value is between 0 and 20 (defensive — corrupt rows have values like 16770). If still missing or invalid, set None.
- **`fireplaces`**: from `Fireplace` (numeric count). If missing, parse `Fireplaces` size code (S1/S2/S3 → assume 1, D1/D2 → assume 1, ODFP-* → outdoor 1). Cap at 10.
- **`cdu_rating`**: from `Condition` attribute. Expand abbreviated codes if needed.
- **`bldg_class`**: from `Construction Style` attribute.
- **`sprinkler_flag`**: `Sprinkler System` value → canonical T/F/'' via `_normalize_flag` (Y, GOOD, EXCELLENT, AVG → 'T'; N, NONE, * → 'F'; blank → '').
- **`plumbing_count`**: from `Plumbing` attribute. Defensive int parse (drop if > 30).
- **`interior_finish`**: from `Interior Finish` attribute.
- **`flooring`**: from `Flooring` attribute. Skip if value is "Allowance" (placeholder).

### Code-expansion lookup

Build a static dict in the ingest script for the most common truncated codes:

```python
_DENTON_CODE_EXPANSIONS = {
    # Foundation
    "CONCRETE B": "Concrete Block",
    "PIER/BEAM": "Pier and Beam",
    "SLAB": "Slab",
    "PIER": "Pier",
    "MASON": "Masonry",
    # Roof Covering
    "Compositio": "Composition",
    "Spanish Ti": "Spanish Tile",
    "Asphalt": "Asphalt",
    "Metal": "Metal",
    "Slate": "Slate",
    "Roll": "Roll Roofing",
    "Shake": "Shake",
    "Fiberglass": "Fiberglass",
    "Copper": "Copper",
    # Exterior Wall
    "Brick Vene": "Brick Veneer",
    "Aluminum s": "Aluminum Siding",
    "Asphalt Si": "Asphalt Siding",
    "Asbestos S": "Asbestos Siding",
    "Concrete B": "Concrete Block",
    "Concrete T": "Concrete Tilt-up",
    "Adobe Bloc": "Adobe Block",
    # Heating/Cooling — special: parse the code rather than expand
    # Construction Style
    "Contempora": "Contemporary",
    "French Pro": "French Provincial",
    "Masonary o": "Masonry on Frame",
    "Mediterran": "Mediterranean",
    "Prefabrica": "Prefabricated",
    "Reinforced": "Reinforced Concrete",
}
```

Apply at ingest time (so DB stores readable values) AND at display time (for any new codes added later). Don't lose the raw code — keep as `_raw_*` columns if we ever need to debug.

## SELECT expansion

### `api/counties/denton.py:query_denton_parcels`

LEFT JOIN `denton_improvement_detail` on `denton_parcels.account_num = denton_improvement_detail.prop_id`. Project the new aliased canonical keys:

```sql
SELECT
  -- existing columns ...
  d.foundation_type,
  d.roof_material,
  d.roof_type,
  d.ext_wall,
  d.heating_type,
  d.ac_type,
  d.beds,
  d.fireplaces,
  d.cdu_rating,
  d.bldg_class,
  d.sprinkler_flag,
  d.plumbing_count,
  d.interior_finish,
  d.flooring,
  d.eff_yr_built,
  d.main_area_sqft AS main_area
FROM denton_parcels p
LEFT JOIN denton_improvement_detail d ON d.prop_id = p.account_num
WHERE ...
```

Same pattern + naming as DCAD's res_detail join.

### Single-parcel `_fetch_denton_parcel_by_account_num`

Apply same expansion. Mirrors `_fetch_dcad_parcel_by_account` pattern.

### `build_feature` (api/counties/denton.py)

Already canonical-key-aware (after DCAD work). The new fields from the SELECT flow into `row.get('foundation_type')`, etc., and build_feature's props dict picks them up. **No build_feature code changes needed** — same canonical contract.

## Backfill plan

Single run of `scripts/build_denton_improvement_detail.py` against Mike's production DB. Idempotent — re-runs safe.

Expected duration:
- File read: ~3.9 million attribute rows = ~30-60 sec parse
- DB upsert: ~300k rows × 18 columns = ~30 sec via temp-table-JOIN
- Total: ~1.5-2 min

Pre-deploy verification queries:

```sql
-- (a) Coverage
SELECT
  COUNT(*) AS total_parcels,
  COUNT(d.prop_id) AS with_detail,
  ROUND(100.0 * COUNT(d.prop_id) / COUNT(*), 1) AS pct
FROM denton_parcels p
LEFT JOIN denton_improvement_detail d ON d.prop_id = p.account_num
WHERE p.state_cd ILIKE 'A%';
-- Expect: ~300k total, ~300k with_detail, ~100%

-- (b) Field-population sanity
SELECT
  COUNT(*) FILTER (WHERE foundation_type IS NOT NULL) AS has_foundation,
  COUNT(*) FILTER (WHERE roof_material IS NOT NULL) AS has_roof,
  COUNT(*) FILTER (WHERE beds IS NOT NULL) AS has_beds,
  COUNT(*) FILTER (WHERE beds > 20) AS bad_bed_count   -- should be 0 after defensive parse
FROM denton_improvement_detail;

-- (c) Top foundation types (smoke check)
SELECT foundation_type, COUNT(*)
FROM denton_improvement_detail
WHERE foundation_type IS NOT NULL
GROUP BY foundation_type ORDER BY COUNT(*) DESC LIMIT 10;
```

## Frontend impact (KK confirmed 2026-05-21: same DCAD plug-in pattern)

**Zero new frontend code.** Phase 1 (DCAD) already built the canonical residential key consumers in two places:

1. **Parcel popup `parcel-panel-cad` table** (`frontend/map.js` lines ~7150) — 30+ rows under Neighborhood (Beds, Full Baths, Foundation, Heating, AC, Roof Type, Roof Material, Pool, Spa, Sauna, etc.). Reads from `props.X` canonical keys. Currently shows "N/A" for Denton parcels because they don't populate those props yet.
2. **Subject Property card line 2 + line 3** (`_populateSubjectPropertyCard` in `frontend/map.js`) — structure / foundation / HVAC / roof / amenity tokens. Same canonical-key read pattern.

Once `denton_improvement_detail` ingests + the `query_denton_parcels` SELECT projects the canonical keys, Denton parcels surface in both UI surfaces with NO additional code changes.

What you'll see post-deploy:
- **Subject Property card** for a Denton parcel: line 2 with e.g. "Slab · Brick Veneer · Composition · Central HVAC · Gable"
- **Parcel popup**: all residential rows under Neighborhood populate with Denton's foundation/roof/HVAC/beds/etc.

The frontend was DESIGNED for this — it's county-agnostic by design (Phase 1 architecture).

## CSV columns (KK confirmed 2026-05-21: reuse + add new beside)

**Generic concept columns already exist (Phase 2)** — Beds, Full Baths, Half Baths, Pool Flag, Foundation Type, Construction Frame Type, Exterior Wall, Heating Type, AC Type, Roof Type, Roof Material, Fence Type, Basement, Building Class, CDU Rating, Effective Year Built, Spa Flag, Sauna Flag, Sprinkler Flag, Deck Flag, etc. These columns LIVE in `api/main.py` writerow at cols 74-99.

After this PR, Denton parcels populate these columns identically to DCAD. No CSV header changes needed for existing columns.

**NEW Denton-specific columns to add** (right beside the existing residential block, before "Current Market Value" at col 100):

- `Interior Finish` (NEW) — Denton publishes Sheetrock/Drywall/Plaster/Concrete. DCAD doesn't have this.
- `Flooring` (NEW) — Carpet/Tile/Wood/Vinyl. Denton-only.
- `Plumbing Fixtures` (NEW) — Denton's numeric plumbing count. Probably more meaningful than DCAD's separate full/half_baths.

These 3 new columns slot in after col 99 (`Deck Flag`), shifting the existing col 100 (`Current Market Value`) to 103, etc. **All COMPATIBILITY LOCK comments at Good Comp / Stored Values sites need their offset history updated** — same pattern as Phase 2 shifts.

For Phase 4 (CSV expansion for Denton-specific fields), this becomes a follow-up ship: header + parcel writerow + comp writerow + COMPATIBILITY LOCK comment refresh. Same shape as the County Source + 26-residential expansion we did this morning.

If KK wants to keep this PR scope tight (data flow only), CSV expansion ships separately as Phase 4 (mirroring the DCAD Phase 1 → Phase 2 split). Defaulting to "include Phase 4 in this PR" unless KK pushes back — KK's direction was "reuse existing CSV cols + add new ones beside" which is a single PR.

## Test plan

**Unit tests (`tests/test_denton_improvement_detail_parse.py`):**
- Fixed-width parser with sample row → asserts correct field extraction
- Code expansion (truncated → readable)
- Bedroom sanity check (drop if > 20)
- Plumbing sanity check
- Heating/Cooling parser (CHCA → heating=Central, ac=Central)
- Sprinkler flag normalization

**Integration smoke (preview):**
- Run backfill against Mike's prod DB
- Verify coverage queries pass
- Load a Denton polygon on preview
- Pick parcels with known characteristics — confirm popup shows correct values
- Download CSV — verify new residential columns populate for Denton rows

**Regression coverage:**
- Existing DCAD parcels still populate (confirm Phase 1 unaffected)
- Collin parcels still populate (beds/baths/pool from Collin)
- TAD parcels still N/A (TAD-half PR not yet shipped)

## Risks + mitigations

1. **Truncated codes are ambiguous.** "Concrete B" is in both Foundation ("Concrete Block") AND Exterior Wall ("Concrete Block"). Expansion lookup must be scoped per-attribute, not global.
2. **Multiple Heating/Cooling rows per parcel.** Sample showed CHCA + Allowance. Pick the more specific code first (longer / not "Allowance").
3. **Duplicate attributes (Fireplace vs Fireplaces, Bedrooms vs Number of Bedrooms).** Pick by attr_desc priority + defensive parse.
4. **5,738 prop_ids in attrs but not in our denton_parcels.** Either commercial parcels (state code C* / F*) OR new parcels added after our Parcels_FC.csv snapshot. Treat as OK — they don't break anything; we just don't surface them in the UI. If we ever refresh denton_parcels from Parcels_FC.csv, may need to re-run this backfill to catch new accounts.
5. **Mike's prod DB has live users.** Backfill runs an ALTER TABLE (new table) + INSERT — no risk to existing data. Schema change is additive.
6. **Source data refreshes.** 2025 certified is stable until Denton publishes 2026 certified (July 2026). When that happens, swap to new files + re-run script. No schema migration needed.

## Open questions for Copilot critique

1. **Schema design: separate table vs columns on denton_parcels.** I chose separate table (mirrors DCAD). Is that the right call vs. flat columns on `denton_parcels`?
2. **prop_id format**: strip leading zeros at ingest (my decision) vs preserve zero-padded. The DB stores stripped → CAST-free join. Any risk?
3. **Multi-improvement parcels**: I pick "highest-value residential improvement" as the canonical one. Real-world example: a primary house + guest house. The guest house wouldn't be the "main" but might have different attributes. Should we surface both, or accept the lossy aggregation?
4. **Truncated code expansion**: per-attribute scope. Foundation-codes-lookup vs Wall-codes-lookup vs Roof-codes-lookup. Manageable but adds maintenance burden. Worth it for readable display?
5. **Defensive parsing thresholds**: I set `beds > 20 = drop`. Should "20" be a config or hardcoded? Real edge case: large boarding houses?
6. **Heating/Cooling parsing**: CHCA → heating=Central, ac=Central. CH → heating=Central, ac=None. What about codes I don't yet know about (e.g., HC, AC, Gas, Electric)? Should I fall back to storing the raw code if unknown?
7. **Backfill performance**: 4M attribute rows in memory feasible? Or should I stream-and-batch?
8. **The data quality issues with corrupt "Number of Bedrooms" values (16770, 19227, 2088)** — should we log these for Denton's data team to know? Or just silently drop?
9. **Anything I missed in the audit?**

## Out of scope (deferred)

- Garage capacity (would need to parse IMPROVEMENT_DETAIL `AG` sub-areas)
- Stories (would need to decode `imprv_det_class_cd` like FB1/FB2)
- Half-bath split (Denton lumps as Plumbing count)
- Spa/Sauna/Deck (Denton bundles in Accessories "Allowance")
- TAD-half PR (separate spec)
- Collin paid/PIA path (deferred per KK no-extra-cost rule)
- CSV column additions (Phase 4 separate ship — same pattern as DCAD Phase 2)
