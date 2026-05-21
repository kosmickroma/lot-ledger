---
title: CAD Residential Detail Expansion — DCAD + TAD (Phase 1 = Ingest + Display)
status: DRAFT — pending KK greenlight + Copilot critique
date: 2026-05-21
branch: feat/cad-residential-detail-expansion-2026-05-21 (off develop)
deployment: PREVIEW ONLY for the whole arc; gated promote to develop/main
discovered_via: 2026-05-21 audit after KK observed Collin's CSV ships beds/baths/pool from CAD (not MLS), and asked "do the others have it?"
---

# CAD Residential Detail Expansion Spec

## What this changes

DCAD and TAD both publish structural detail (bedrooms, bathrooms, pool, fireplace, foundation type, heating type, etc.) in source data we already have on disk — we just never pulled those columns. This spec expands both county ingests to capture everything available, surfaces the data through `build_feature` so it lands on `feature.properties` like Collin's does today, and renders the new fields in the Subject Property card + parcel popup.

CSV column additions are split into **Phase 2** (separate spec, separate ship) to avoid bundling a 30-column shift into the same change set — Mike's downstream spreadsheets reference CSV columns by index, and we just shifted them in 2026-05-20.

## Phase 1 scope (this spec)

**IN:**
- DCAD: expand `scripts/build_db.py:_build_res_detail_table` + the `res_detail` table schema to pull all 27 untapped columns from `data/RES_DETAIL.CSV`. Backfill via a one-shot script that mirrors the property_city pattern.
- TAD: expand `scripts/build_tad_db.py` to read all bed/bath/pool/garage/heat/AC/deed/MAPSCO columns from the same ParcelView.shp DBF we already read. Schema additions to `tad_parcels`. Backfill happens automatically since the ingest already reads the shapefile.
- Update SELECTs (`_fetch_dcad_parcel_by_account`, `query_parcels`, `_fetch_tad_parcel_by_account`, `query_tad_parcels`) to project the new fields.
- Update `api/counties/dcad.py:build_feature` to emit the new fields on `feature.properties`. Already exposes beds/baths/pool/stories from today's earlier commit — extend to add fireplace, half_baths, foundation_type, heating_type, ac_type, ext_wall, roof_type, roof_mat, cdu_rating, bldg_class, eff_yr_built, deed_date, garage_capacity, etc.
- Update `api/counties/tad.py:_normalize_tad_row` to pass new TAD fields through into the row dict (build_feature picks them up via row.get keys).
- Update `frontend/map.js:_populateSubjectPropertyCard` meta line to render the additional fields when present (e.g., `4bd · 3.5ba · pool · spa · 2-car garage · brick`).

**OUT (Phase 2, separate ship):**
- CSV column additions for the new fields. Will land as a separate compatibility-lock-aware ship with Mike notification of column-shift.
- Denton residential detail — Denton's published `Parcels_FC.csv` doesn't have bed/bath/pool/garage columns (audited 71 columns + xlsx/gpkg files). Document the gap in master_todo; defer pending Denton publishing more data OR pulling from an alternate source.

## Source data audit (already done)

### DCAD `data/RES_DETAIL.CSV` columns we don't currently ingest

```
NUM_BEDROOMS, NUM_FULL_BATHS, NUM_HALF_BATHS, NUM_FIREPLACES, NUM_KITCHENS,
NUM_WET_BARS, NUM_UNITS, POOL_IND, SPA_IND, SAUNA_IND, SPRINKLER_SYS_IND,
DECK_IND, NUM_STORIES_DESC, BLDG_CLASS_DESC, CDU_RATING_DESC,
CONSTR_FRAM_TYP_DESC, FOUNDATION_TYP_DESC, HEATING_TYP_DESC, AC_TYP_DESC,
FENCE_TYP_DESC, EXT_WALL_DESC, BASEMENT_DESC, ROOF_TYP_DESC, ROOF_MAT_DESC,
EFF_YR_BUILT, ACT_AGE, PCT_COMPLETE
```

27 columns. Sample row 2 (account `00000202819000000`): 4 bedrooms, 3 full baths, 1 half bath, 1 fireplace, 1 kitchen, frame construction, pier-and-beam foundation, central heating + AC, brick veneer ext walls, gable roof with comp shingles, sprinkler system YES, no deck/spa/sauna, 1.5 stories, eff yr 1980, act age 101.

### TAD `ParcelView.shp` DBF columns we don't currently pull

```
Num_Bedroo (Num_Bedrooms truncated), Num_Bathro, Garage_Cap, Swimming_P,
Central_He, Central_Ai, Structure_, Deed_Date, Deed_Book, Deed_Page,
MAPSCO, ARB_Indica, Ag_Code, Instrument, ZipCode, Notice_Dat,
Num_Specia (Num_Special_Dist), Spec1-Spec5, TAD_Map, From_Accts,
Appraisal1, GIS_Link, Overlap_Fl, CALCULATED, Owner_CRRT, Record_Typ
```

15-20 useful columns. All in the same shapefile we already read. No new ingest source.

### Denton `Parcels_FC.csv` — no equivalent fields

Audited all 71 CSV columns + the xlsx/gpkg sibling files. No bedrooms/bathrooms/pool/garage data exposed. Denton may publish residential detail in a separate restricted source we don't have access to (PIA-driven, similar to historical owner data). Document gap; revisit when Denton CAD's data catalog changes.

## Schema migrations

### DCAD: extend `res_detail` table

Idempotent ALTERs in `scripts/build_db.py:_ensure_res_detail_schema()` (new function called before `_build_res_detail_table`). All NULLable to preserve existing rows:

```sql
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_bedrooms INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_full_baths INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_half_baths INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_fireplaces INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_kitchens INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_wet_bars INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_units INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS pool_ind TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS spa_ind TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS sauna_ind TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS sprinkler_sys_ind TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS deck_ind TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS num_stories_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS bldg_class_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS cdu_rating_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS constr_fram_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS foundation_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS heating_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS ac_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS fence_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS ext_wall_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS basement_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS roof_typ_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS roof_mat_desc TEXT;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS eff_yr_built INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS act_age INTEGER;
ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS pct_complete TEXT;
```

27 ALTERs. Safe + additive — no risk to existing rows or columns.

### TAD: extend `tad_parcels` table

Idempotent ALTERs in `scripts/build_tad_db.py:_ensure_table()` (after the CREATE TABLE IF NOT EXISTS, before the indexes):

```sql
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS num_bedrooms INTEGER;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS num_bathrooms INTEGER;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS garage_capacity INTEGER;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS swimming_pool TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS central_heating TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS central_air TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS structure_type TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS deed_date DATE;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS deed_book TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS deed_page TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS instrument TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS mapsco TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS arb_indicator TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS ag_code TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS notice_date DATE;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS zip_code_full TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS num_special_dist INTEGER;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS special_dist_1 TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS special_dist_2 TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS special_dist_3 TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS special_dist_4 TEXT;
ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS special_dist_5 TEXT;
```

22 ALTERs. Same safety profile.

## Ingest expansion

### DCAD: `scripts/build_db.py:_build_res_detail_table`

Expand the row dict to include all 27 new fields. Helpers `_to_int`, `_to_float`, `_clean_text` already exist. Add each field with appropriate type coercion. Patch the `_upsert_rows("res_detail", ...)` call so `update_cols` includes all new columns (so re-runs UPDATE them).

### TAD: `scripts/build_tad_db.py:_extract_row`

Currently extracts ~25 fields from the shapefile record. Expand to extract the additional 22 fields. Append them to the row tuple in the same order as the schema columns. Update `_upsert_batch` SQL to reference the new columns.

**Important compatibility note:** the existing `tad_parcels` INSERT in `_upsert_batch` uses positional placeholders matching the column order in the CREATE TABLE. Adding columns in the middle of the column list will break the insert. Append new columns at the END of the column list — same pattern the TAD `property_city` work used.

## Backfill scripts

### `scripts/build_dcad_res_detail_backfill.py` (new)

Mirrors `scripts/build_dcad_property_city.py` pattern. Reads `RES_DETAIL.CSV`, ALTERs the table (or relies on build_db's _ensure_res_detail_schema to have run first), then runs a temp-table-JOIN UPDATE to populate the new columns on existing 682k res_detail rows. Idempotent + reportable.

```python
# Pseudo:
1. CREATE TEMP TABLE _dcad_res_load (account_num PK + 27 new cols);
2. Bulk-insert from RES_DETAIL.CSV via execute_values batches;
3. UPDATE res_detail SET col = load.col FROM _dcad_res_load load
   WHERE res_detail.account_num = load.account_num;
4. Print verification stats (populated counts per column).
```

### TAD: no separate backfill needed

Running `scripts/build_tad_db.py` against the existing 2026-05-01 shapefile already re-upserts every row. Once the script reads the new columns + writes them, an in-place re-run populates them. (KK confirms before running on prod — it's ~10 min of UPSERT load.)

Could also write a thin `scripts/build_tad_residential_backfill.py` to ONLY update the new columns (read shapefile, UPDATE only the new cols by account_num) for surgical safety. Recommend this for production safety — avoids re-touching other tad_parcels columns.

## SELECT expansion

### `_fetch_dcad_parcel_by_account` + `query_parcels` in `api/main.py` + `api/counties/dcad.py`

Both already JOIN against `res_detail` and project `r.yr_built, r.tot_living_area, r.tot_main_sf`. Add the new columns:

```sql
r.yr_built, r.tot_living_area, r.tot_main_sf,
r.num_bedrooms AS beds, r.num_full_baths AS full_baths, r.num_half_baths AS half_baths,
r.num_fireplaces, r.num_kitchens, r.num_wet_bars, r.num_units,
r.pool_ind AS pool_flag, r.spa_ind, r.sauna_ind,
r.sprinkler_sys_ind, r.deck_ind,
r.num_stories_desc AS stories, r.bldg_class_desc, r.cdu_rating_desc,
r.constr_fram_typ_desc, r.foundation_typ_desc,
r.heating_typ_desc, r.ac_typ_desc,
r.fence_typ_desc, r.ext_wall_desc, r.basement_desc,
r.roof_typ_desc, r.roof_mat_desc,
r.eff_yr_built, r.act_age, r.pct_complete,
```

Note the aliasing: `r.num_bedrooms AS beds`, `r.pool_ind AS pool_flag`, etc. — this aligns DCAD field names with the canonical names already used by Collin (so `build_feature` reads from a single set of row keys regardless of source county).

### TAD SELECTs

Same approach in `_fetch_tad_parcel_by_account` + `query_tad_parcels`. Add aliases:

```sql
num_bedrooms AS beds, num_bathrooms AS baths,
garage_capacity, swimming_pool AS pool_flag,
central_heating, central_air, structure_type,
deed_date, deed_book, deed_page, instrument,
mapsco, arb_indicator, ag_code, notice_date, zip_code_full,
num_special_dist, special_dist_1, special_dist_2, special_dist_3, special_dist_4, special_dist_5,
```

## build_feature expansion (api/counties/dcad.py)

Today's commit 48b6dd0 exposed `beds`, `baths`, `stories`, `pool_flag` on feature properties. Extend the `props` dict to add the rest. All optional — read via `row.get()` and fall back to `"N/A"` / `""` if missing. The card meta line skips N/A values gracefully.

Per-county-agnostic mapping (works for whichever county populates the row):
```python
"half_baths": int(_safe_float(row.get("half_baths"))) if _safe_float(row.get("half_baths")) not in (None, 0.0) else "N/A",
"fireplaces": int(_safe_float(row.get("num_fireplaces"))) if ... else "N/A",
"kitchens": int(_safe_float(row.get("num_kitchens"))) if ... else "N/A",
"spa_flag": _clean_text(row.get("spa_ind")) or "",
"sauna_flag": _clean_text(row.get("sauna_ind")) or "",
"deck_flag": _clean_text(row.get("deck_ind")) or "",
"sprinkler_flag": _clean_text(row.get("sprinkler_sys_ind")) or "",
"foundation_type": _clean_text(row.get("foundation_typ_desc")) or "N/A",
"heating_type": _clean_text(row.get("heating_typ_desc")) or _clean_text(row.get("central_heating")) or "N/A",
"ac_type": _clean_text(row.get("ac_typ_desc")) or _clean_text(row.get("central_air")) or "N/A",
"ext_wall": _clean_text(row.get("ext_wall_desc")) or "N/A",
"roof_type": _clean_text(row.get("roof_typ_desc")) or "N/A",
"roof_mat": _clean_text(row.get("roof_mat_desc")) or "N/A",
"bldg_class": _clean_text(row.get("bldg_class_desc")) or "N/A",
"cdu_rating": _clean_text(row.get("cdu_rating_desc")) or "N/A",
"eff_yr_built": str(row.get("eff_yr_built")) if row.get("eff_yr_built") else "N/A",
"garage_capacity": int(_safe_float(row.get("garage_capacity"))) if _safe_float(row.get("garage_capacity")) not in (None, 0.0) else "N/A",
"deed_date": _clean_text(row.get("deed_date")) or "N/A",
```

## Frontend Subject Property card meta line

Extend `_populateSubjectPropertyCard` to add more fields after the existing `sqft · ac · year · beds · baths · pool · school` line. Suggest a second meta line for additional structural data (otherwise the single line gets too long):

```js
// Meta line 1: dimensional
"2,617 sqft · 0.36 ac · 1983 · 4bd · 3.5ba · 2-car garage · SPL"

// Meta line 2: structural (optional, when data is present)
"Brick · Pier and Beam · Central HVAC · Comp Shingles · Pool · Spa"
```

The second line shows only fields with non-N/A values. Stays compact for counties that don't have the data (TAD/Denton parcels just get an empty second line, hidden via `:empty`).

## Phase 1 testing

- pytest: `tests/test_dcad_res_detail_expansion.py` — exercise the new build_db `_build_res_detail_table` field reads + a mock CSV row → asserts all 27 fields make it to the output dict.
- pytest: `tests/test_tad_residential_expansion.py` — exercise `_extract_row` + `_normalize_tad_row` for the new shapefile fields.
- Integration: after backfill, verify with SQL:
  - DCAD: `SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM res_detail` — expect ~600k populated.
  - TAD: `SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM tad_parcels` — expect ~700k populated.
- Manual smoke test: load Dallas + Tarrant polygons on preview; click parcels; popup + card should show beds/baths/pool when applicable.

## Phase 2 preview (separate ship)

CSV column additions. Carries `compatibility lock` shift discipline — all 4 writerow sites + header coordinated. Mike notification of column-shift. Likely +20-30 new CSV columns. Will require its own spec + Copilot critique.

## Constraints

- **Preview-only deploy for the whole Phase 1 arc** until KK reviews each step. No promote to develop/main without greenlight.
- **Schema migrations idempotent + additive.** Never drop/rename existing columns. All new columns nullable.
- **TAD upsert pattern:** new columns appended at end of column list — DON'T insert in the middle, the positional placeholders in `_upsert_batch` will misalign.
- **No CSV column changes in Phase 1.** Mike's downstream consumers aren't ready for another shift this week.

## Questions for Copilot critique (before coding)

1. **Mapping consistency:** is `r.num_bedrooms AS beds` (DCAD) + `num_bedrooms AS beds` (TAD) + collin_parcels.beds (Collin native column) → consumed as `row.get("beds")` in build_feature → the right unification pattern? Any field name collision risk?

2. **Half-baths handling:** Collin's `baths` column today stores a decimal (3.5 = 3 full + 1 half = 3.5). DCAD splits into `num_full_baths` (int) + `num_half_baths` (int). For DCAD parcels, should build_feature compute `baths = full + 0.5 * half` and emit a single `baths` field? Or emit both as separate props?

3. **Garage capacity:** Collin doesn't expose this on the parcel side today. DCAD/TAD do. Add as a Collin schema/ingest expansion too (look for the field in collin_parcels source) OR leave Collin without garage for now?

4. **Subject Property card meta line:** is a two-line meta acceptable, or should we keep it single-line and only show the most important fields (beds/baths/pool/garage)?

5. **TAD `Num_Bedroo` field is `C` (text) length 2:** can hold strings like "" or "04". Need defensive cast in the ingest. Same for several other "numeric" fields in TAD's DBF that are stored as text.

6. **DCAD `POOL_IND` values:** "Y" / "N" / blank in source. Already noted in earlier `pool_flag` work. Collin uses "T" / "F". Should normalize at ingest time OR at display time? (Display-time is what we have now, but ingest-time normalization is cleaner long-term.)

7. **CSV column adds in Phase 2** — should we group new columns by county (`DCAD - Foundation Type`, `DCAD - Roof Type`, ...) or by concept (one generic `Foundation Type` column that pulls from whichever county the row is from)? Per KK's directive ("we need to know what county"), I default to per-county prefixed for fields that are unique to one county; generic for cross-county-concept fields (Beds, Baths, Pool, Garage which all 3 counties expose).

8. **Pre-existing res_detail rows where new columns are NULL** — anywhere the SELECT projects a NULL column, the JOIN result has NULL → build_feature falls through to "N/A". No code path explodes on NULL. Confirmed safe.

## Out of scope

- Denton residential detail (no source data published)
- DCAD/TAD ingest of NEW source files (e.g., DCAD's separate residential supplements, TAD's separate StandardData files) — Phase 3+ if those become relevant
- CSV column additions (Phase 2)
- HOA polygons / neighborhood polygons / agent + permit + protest data for non-Collin counties — separate items in master_todo

## Cross-refs

- Master todo entry "Bring TAD + Denton + Dallas parcel-detail data up to Collin parity" — this spec implements DCAD + TAD halves of that item.
- Memory: `feedback_db_production_discipline` — spec → critique → preview workflow applies.
- 2026-05-21 city-resolution work (`docs/TAD_CITY_RESOLUTION_CODING_SPEC.md`, `docs/DCAD_CITY_RESOLUTION_CODING_SPEC.md` implicit) — same architectural pattern (source data already on disk, ingest expansion + backfill script).
