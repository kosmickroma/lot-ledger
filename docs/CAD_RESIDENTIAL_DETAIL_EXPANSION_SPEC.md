---
title: CAD Residential Detail Expansion — DCAD + TAD (Phase 1 = Ingest + Display)
status: v2 — Copilot critique 1 folded in, ready for second-round critique
date: 2026-05-21
branch: feat/cad-residential-detail-expansion-2026-05-21 (off develop)
deployment: PREVIEW ONLY for the whole arc; gated promote to develop/main
discovered_via: 2026-05-21 audit after KK observed Collin's CSV ships beds/baths/pool from CAD (not MLS), and asked "do the others have it?"
revisions:
  v1 (initial): 2026-05-21 morning
  v2: 2026-05-21 afternoon, after Copilot's round-1 critique
  v3 (this): 2026-05-21 evening, after Copilot round-2 + KK greenlight to code

## v3 locked decisions

1. **Canonical key cleanup:**
   - One name only: `roof_material` (drop `roof_mat` spelling everywhere).
   - `bldg_class` is **DCAD-only** (drop the speculative Collin `class_cd` mapping — different concept).
   - **Added `structure_type` as canonical key** (TAD-sourced today, future-extensible).
   - **Stories precedence:** numeric `stories` wins. If only DCAD's `stories_desc` text is present, parse the leading word:
     ```
     "ONE STORY"               → 1.0
     "ONE AND ONE HALF STORIES" → 1.5
     "TWO STORIES"             → 2.0
     "TWO AND ONE HALF STORIES" → 2.5
     "THREE STORIES"           → 3.0
     other / unparseable       → None (and keep stories_desc text in prop)
     ```
2. **DISTINCT ON strategy:** convert `res_detail` JOIN in `query_parcels` to a **LATERAL one-row pick** (matches existing `land_detail` pattern). Eliminates dedup uncertainty at the root. No ORDER BY tie-breaker needed.
3. **Null vs "N/A" hygiene** flagged as KNOWN SMELL — deferred to separate "API contract hygiene" PR. New fields in this spec follow the existing "N/A" inline convention to stay consistent within Phase 1.
4. **Date format for new date fields:** **ISO 8601 strings `"YYYY-MM-DD"`** (e.g., `deed_date`, `notice_date`). Source-text parsed at ingest; unparseable → NULL, never raw text.
5. **TAD schema signature check** at ingest startup — `information_schema` query confirms expected column names + order before first batch. Complements `_assert_batch_invariants`.
6. **`county_source` CSV column** controlled enum: `"DCAD" | "TAD" | "Collin" | "Denton"` (PascalCase). Positioned as first metadata column near parcel identity.
7. **Phase 1.5 guardrail:** Phase 1 canonical keys MUST map 1:1 into Phase 1.5 structured-panel sections without renaming. Spec'd separately when Phase 1 ships.
---

## Changelog

**v2 (this rev — Copilot round-1 critique folded in):**

- Added explicit **canonical-field contract section** so the per-county alias mapping (Collin native column → DCAD `r.num_bedrooms AS beds` → TAD `num_bedrooms AS beds`) has one authoritative source.
- **Pool / spa / sauna / sprinkler / deck flag normalization moved to INGEST TIME** (canonical "T"/"F"/NULL stored in DB), not display time. Eliminates the brittle multi-encoding conditional in the frontend formatter and simplifies CSV Phase 2.
- TAD ingest gains explicit **defensive-cast requirement** for all DBF text-typed numeric fields (Num_Bedroo, Num_Bathro, Garage_Cap, etc.) — blank/padded/garbage coerces to NULL, never raises.
- TAD `_upsert_batch` gains an **invariant assertion** at run start: `len(column_list) == len(placeholders) == len(row_tuple)`. One sanity check up front, not silent positional corruption mid-run.
- TAD backfill: **surgical residential-only script promoted from "recommend" to DEFAULT for production.** Full re-ingest is reserved for controlled rebuild windows.
- DCAD `query_parcels` SELECT gets an explicit **ORDER BY tie-breaker** to make `DISTINCT ON (p.account_num)` deterministic if join multiplicity ever surfaces.
- Half-baths: **derive `baths = full + 0.5 * half` for display, AND preserve `full_baths` + `half_baths` as separate props** for audit. Future-proof against decimal-baths sources.
- Subject card meta line 2: **explicit token priority order** (structure → foundation → HVAC → roof → amenities, cap 5 tokens) — prevents visual churn across parcels.
- Test plan expanded to **regression-test ALL res_detail readers** (CSV export + comp-matching), not just popup/card.
- **Phase 1.5 commitment** added: move to structured Core/Structure/Mechanical/Amenities sections on the card before string-concat debt accumulates. Spec'd separately.
- Phase 2 preview gains a **`county_source` CSV column** so generic concept columns retain provenance without prefix bloat.
- Denton + DCAD-supplemental gaps explicitly documented as "current public bundle parity" — not absolute ceiling.

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

## Canonical-field contract (v2 addition)

This is the **authoritative list of normalized property keys** that flow into
`feature.properties` via `build_feature`. Per-county SQL SELECTs alias their
local column names INTO these canonical keys, so downstream code (frontend
card, CSV writer, comp matcher, popup) reads from a single set of names.

| Canonical prop key | Type / format | Collin source | DCAD source | TAD source |
|---|---|---|---|---|
| `beds` | int or "N/A" | `collin_parcels.beds` | `res_detail.num_bedrooms AS beds` | `tad_parcels.num_bedrooms AS beds` |
| `full_baths` | int or "N/A" | (derive from baths if needed) | `res_detail.num_full_baths AS full_baths` | (none — TAD lumps as `num_bathrooms`) |
| `half_baths` | int or "N/A" | (derive from baths if needed) | `res_detail.num_half_baths AS half_baths` | (none) |
| `baths` | decimal (`X.X`) or "N/A" | `collin_parcels.baths` | computed: `full + 0.5 * half` | `tad_parcels.num_bathrooms AS baths` |
| `stories` | int or float or "N/A" | `collin_parcels.stories` | computed from `num_stories_desc` lookup | (none — TAD doesn't expose) |
| `stories_desc` | text or "N/A" | (none) | `res_detail.num_stories_desc AS stories_desc` | (none) |
| `pool_flag` | "T"/"F"/"" (canonical) | `collin_parcels.pool_flag` (normalized at ingest) | `res_detail.pool_ind AS pool_flag` (normalized at ingest) | `tad_parcels.swimming_pool AS pool_flag` (normalized at ingest) |
| `spa_flag` | "T"/"F"/"" | (none) | `res_detail.spa_ind AS spa_flag` | (none) |
| `sauna_flag` | "T"/"F"/"" | (none) | `res_detail.sauna_ind AS sauna_flag` | (none) |
| `sprinkler_flag` | "T"/"F"/"" | (none) | `res_detail.sprinkler_sys_ind AS sprinkler_flag` | (none) |
| `deck_flag` | "T"/"F"/"" | (none) | `res_detail.deck_ind AS deck_flag` | (none) |
| `fireplaces` | int or "N/A" | (none) | `res_detail.num_fireplaces AS fireplaces` | (none) |
| `kitchens` | int or "N/A" | (none) | `res_detail.num_kitchens AS kitchens` | (none) |
| `wet_bars` | int or "N/A" | (none) | `res_detail.num_wet_bars AS wet_bars` | (none) |
| `units` | int or "N/A" | `collin_parcels.units` | `res_detail.num_units AS units` | (none) |
| `garage_capacity` | int or "N/A" | (audit TBD) | (none — DCAD doesn't expose) | `tad_parcels.garage_capacity AS garage_capacity` |
| `bldg_class` | text or "N/A" | `collin_parcels.class_cd`? | `res_detail.bldg_class_desc AS bldg_class` | (none) |
| `cdu_rating` | text or "N/A" | (none) | `res_detail.cdu_rating_desc AS cdu_rating` | (none) |
| `foundation_type` | text or "N/A" | (none) | `res_detail.foundation_typ_desc AS foundation_type` | (none) |
| `construction_frame_type` | text or "N/A" | (none) | `res_detail.constr_fram_typ_desc AS construction_frame_type` | (none) |
| `heating_type` | text or "N/A" | (none — Collin has heating?) | `res_detail.heating_typ_desc AS heating_type` | derived from `central_heating` Y/N |
| `ac_type` | text or "N/A" | (none) | `res_detail.ac_typ_desc AS ac_type` | derived from `central_air` Y/N |
| `ext_wall` | text or "N/A" | (none) | `res_detail.ext_wall_desc AS ext_wall` | (none) |
| `roof_type` | text or "N/A" | (none) | `res_detail.roof_typ_desc AS roof_type` | (none) |
| `roof_material` | text or "N/A" | (none) | `res_detail.roof_mat_desc AS roof_material` | (none) |
| `basement` | text or "N/A" | (none) | `res_detail.basement_desc AS basement` | (none) |
| `fence_type` | text or "N/A" | (none) | `res_detail.fence_typ_desc AS fence_type` | (none) |
| `eff_yr_built` | int or "N/A" | (none) | `res_detail.eff_yr_built AS eff_yr_built` | (none) |
| `act_age` | int or "N/A" | (none) | `res_detail.act_age AS act_age` | (none) |
| `pct_complete` | text or "N/A" | (none) | `res_detail.pct_complete AS pct_complete` | (none) |
| `deed_date` | text/date or "N/A" | `collin_parcels.deed_date` | (none — DCAD has it elsewhere?) | `tad_parcels.deed_date AS deed_date` |
| `mapsco` | text or "N/A" | (none) | (none — DCAD has MAPSCO in ACCOUNT_INFO but separate) | `tad_parcels.mapsco AS mapsco` |
| `tad_arb_indicator` | text or "N/A" (TAD-only) | — | — | `tad_parcels.arb_indicator` |

**Drift-prevention rule:** any code path that needs a county-specific field MUST either (a) use the canonical key from this table, or (b) explicitly document the per-county field name + the divergence reason. New canonical keys added here go through a quick spec amendment, not silent code drift.

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

**Flag normalization at ingest (v2):** for `pool_ind`, `spa_ind`, `sauna_ind`, `sprinkler_sys_ind`, `deck_ind` — DCAD source uses `"Y"` / `"N"` / `""`. Normalize at row-construction time:

```python
def _normalize_flag(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    t = text.strip().upper()
    if t in {"Y", "T", "TRUE", "YES", "1"}:
        return "T"
    if t in {"N", "F", "FALSE", "NO", "0"}:
        return "F"
    return None  # unknown → null, NOT silent F (preserves "unknown" vs "no")
```

Same helper reused by Collin's ingest (refactor to dcad.py shared module) and the TAD ingest below. **Canonical stored value is "T" / "F" / NULL** — no Y/N/T/F divergence in the DB.

### TAD: `scripts/build_tad_db.py:_extract_row`

Currently extracts ~25 fields from the shapefile record. Expand to extract the additional 22 fields. Append them to the row tuple in the same order as the schema columns. Update `_upsert_batch` SQL to reference the new columns.

**Defensive casting requirement (v2 — Copilot pushback):** TAD's DBF stores numeric fields as **TEXT** columns of varying widths (`Num_Bedroo` is `C[2]`, `Num_Bathro` is `C[2]`, `Garage_Cap` is `C[2]`, `Year_Built` is `C[4]`, etc.). Source values can be blank, padded with spaces, leading zeros, or contain unexpected non-numeric tokens. Every cast in `_extract_row` MUST be defensive:

```python
def _safe_int_text(value: object) -> int | None:
    """Coerce DBF text → int. Returns None for blank/garbage/non-numeric.
    NEVER raises. Logs once-per-distinct-bad-value at DEBUG level only."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (ValueError, TypeError):
        return None
```

Same pattern via `_safe_float_text` for decimal-valued text columns. **Existing `_to_int` / `_to_float` helpers should be reviewed and hardened if they're not already safe** — one bad cast in a 760k-row batch UPSERT can poison a long run.

**UPSERT invariant assertion (v2 — Copilot pushback):** the existing `tad_parcels` INSERT in `_upsert_batch` uses positional placeholders matching the column order in the CREATE TABLE. Adding columns in the middle of the column list silently mis-maps values across all rows in the batch. **Add a length/order sanity check at run start:**

```python
def _assert_batch_invariants(column_list: list[str], placeholder_count: int, sample_row: tuple) -> None:
    """One-shot assertion at batch start. Catches the silent positional-
    misalignment bug class before any data is written."""
    assert len(column_list) == placeholder_count, (
        f"TAD UPSERT misalignment: {len(column_list)} columns vs "
        f"{placeholder_count} placeholders"
    )
    assert len(column_list) == len(sample_row), (
        f"TAD UPSERT misalignment: {len(column_list)} columns vs "
        f"{len(sample_row)} tuple values in sample row"
    )
```

Run this once per batch start, not per-row. Append new columns at the END of the column list AND the row tuple — same pattern the TAD `property_city` work used.

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

### TAD: surgical residential-only backfill is DEFAULT for production (v2)

**Default path:** `scripts/build_tad_residential_backfill.py` (new) — reads the same shapefile but ONLY writes to the new columns. UPDATE-by-account-num, never INSERT, never touches owner_name/situs_addr/value fields. Blast radius limited to the new fields. Idempotent + reportable.

**Reserved for controlled rebuild windows only:** running `scripts/build_tad_db.py` in full re-ingest mode. Full re-upsert re-touches every column on every row — fine for an initial bake or annual refresh, dangerous for incremental "add a few new columns" work because any unrelated bug in the broader ingest could corrupt healthy data.

Pre-deploy verification on the surgical script:

```sql
-- (a) Before run
SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM tad_parcels;
-- Expect: 0 (column just added)

-- (b) After surgical backfill
SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM tad_parcels;
-- Expect: ~700k populated (most TAD parcels have a bedroom count in source)

-- (c) Confirm un-touched columns are unchanged
SELECT COUNT(*) FILTER (WHERE owner_name IS NOT NULL) FROM tad_parcels;
-- Should match pre-run count exactly (surgical script did not touch this column)
```

## SELECT expansion

### `_fetch_dcad_parcel_by_account` + `query_parcels` in `api/main.py` + `api/counties/dcad.py`

Both already JOIN against `res_detail` and project `r.yr_built, r.tot_living_area, r.tot_main_sf`. Add the new columns.

**`query_parcels` DISTINCT ON tie-breaker (v2 — Copilot pushback):** the current `SELECT DISTINCT ON (p.account_num) ...` block has no `ORDER BY` clause, which means PostgreSQL picks an arbitrary row when join multiplicity surfaces. With `res_detail` joined LEFT (one row per account), this is fine today, but adding the new columns adds JOIN surface area and the dedup nondeterminism is a latent bug class. Add an explicit ORDER BY:

```sql
SELECT DISTINCT ON (p.account_num)
    p.account_num, ...,
    r.yr_built, r.tot_living_area, ...,
    r.num_bedrooms AS beds, r.num_full_baths AS full_baths, ...
FROM parcels p
LEFT JOIN appraisal a ...
LEFT JOIN res_detail r ...
LEFT JOIN land_detail l ...
WHERE ...
ORDER BY p.account_num, r.yr_built DESC NULLS LAST, l.area_size DESC NULLS LAST
-- tie-breaker: prefer the res_detail row with most recent yr_built, then
-- the land_detail row with largest area_size. Makes dedup deterministic.
```

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

## Frontend Subject Property card meta lines

Extend `_populateSubjectPropertyCard` to render TWO meta lines. Line 1 is the dimensional/identity strip already there; Line 2 is structural detail when present.

**Line 1 (dimensional + bath derivation, v2 clarification):**
```
2,617 sqft · 0.36 ac · 1983 · 4bd · 3.5ba · 2-car garage · SPL
```
- `baths` rendered as the DERIVED decimal (`3` if half_baths is 0; `3.5` if 1 half-bath).
- `full_baths` and `half_baths` remain on `feature.properties` for popup / CSV / audit but the card displays the derived form for compactness.

**Line 2 — explicit token priority order + 5-token cap (v2 — Copilot pushback):**

Tokens are pushed in this order; first 5 with non-N/A values win:

1. Structure type (e.g., "1-Story Frame" — derived from `construction_frame_type` + `stories_desc`)
2. Foundation type (e.g., "Pier and Beam" — from `foundation_type`)
3. HVAC summary ("Central HVAC" / "Central Heat" / "Central AC" — from `heating_type` + `ac_type`)
4. Roof material (e.g., "Comp Shingles" — from `roof_material`)
5. Exterior wall (e.g., "Brick Veneer" — from `ext_wall`)
6. Amenity flags: "Pool", "Spa", "Sauna", "Sprinkler", "Deck", "Fireplace" — render only those with canonical `"T"` in their flag prop.

Cap at 5 tokens for visual consistency parcel-to-parcel. If a parcel has 10 truthy fields, prioritize the first 5 in the list above. (Future Phase 1.5 fixes this with a structured panel instead of more tokens.)

```js
// DCAD parcel example (rich source):
// Line 2: "1-Story Frame · Pier and Beam · Central HVAC · Comp Shingles · Pool"

// TAD parcel example (less rich):
// Line 2: "Central Heat · Central AC · Pool"

// Denton parcel example (no structural data):
// Line 2: (empty, hidden via :empty CSS rule)
```

**Phase 1.5 commitment (v2 addition):** the two-line meta is acceptable for Phase 1 but is string-concatenation debt waiting to happen. Phase 1.5 (separate spec) replaces the meta lines with a structured panel:

- **Core** (sqft, lot, year built)
- **Structure** (beds, full/half baths, garage, stories)
- **Mechanical** (HVAC types, foundation, roof type/material)
- **Amenities** (pool, spa, sauna, fireplaces, sprinkler, deck — as iconified chips)
- **Record** (deed date, eff yr built, building class, CDU rating)

Each section collapses to "—" if its data is unavailable for the parcel's county. Phase 1.5 is the natural successor when this card gains real estate (e.g., expanded sidebar mode).

## Phase 1 testing

**Unit tests (pytest):**
- `tests/test_dcad_res_detail_expansion.py` — exercise `_build_res_detail_table` field reads with a mock CSV row → asserts all 27 fields make it to the output dict. Include hostile-input cases: blank, padded, garbage non-numeric for the int fields; Y/N/T/F/blank for flag fields; verify `_normalize_flag` outputs canonical "T"/"F"/None.
- `tests/test_tad_residential_expansion.py` — exercise `_extract_row` for the new shapefile fields including DBF text-typed numeric hostile inputs. Confirm `_safe_int_text` and `_safe_float_text` coerce to None without raising.
- `tests/test_tad_upsert_invariant.py` — assert `_assert_batch_invariants` catches column/placeholder/tuple-length mismatches and passes when aligned.
- `tests/test_build_feature_res_detail_props.py` — confirm `build_feature` emits ALL canonical residential keys on `props`. Mock a Collin row, DCAD row, TAD row, and Denton row — each populating only its available fields. Assert no KeyError, no exception, correct N/A fallbacks where data is absent.

**Regression coverage (v2 expansion — Copilot pushback):**
- **CSV export paths** — ANY consumer of `r.yr_built` / `r.tot_living_area` / `r.tot_main_sf` in `_run_download_csv` MUST be regression-tested with null-heavy parcel data. Grep coverage: `grep -n "tot_living_area\|tot_main_sf\|yr_built" api/main.py` and walk every site.
- **Comp-matching paths** — `compPassesPropelioFilters` and downstream filter code already use `props.sqft` (= tot_living_area). Confirm none of the NEW props (`fireplaces`, `foundation_type`, etc.) are accidentally read in a way that breaks when null.
- **Cached job consumers** — `cached_jobs.rows` is a JSON snapshot of build_feature output. Old cached jobs will not have the new props until the next /api/analyze run. The frontend card must gracefully render N/A when a prop key is missing (not just when its value is "N/A"). Test by loading a cached_jobs row from yesterday + confirming card renders cleanly.

**Integration verification (after backfill, before greenlight):**
- DCAD: `SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM res_detail` — expect ~600k populated.
- DCAD: `SELECT COUNT(*) FILTER (WHERE pool_ind = 'T') FROM res_detail` — sample for sanity; in Dallas County ~10-15% of residential parcels expected.
- TAD: `SELECT COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) FROM tad_parcels` — expect ~700k populated.
- TAD: `SELECT COUNT(*) FILTER (WHERE pool_flag = 'T') FROM tad_parcels` — sample for sanity.
- Surgical-backfill safety check (TAD): `SELECT COUNT(*) FILTER (WHERE owner_name IS NOT NULL) FROM tad_parcels` BEFORE and AFTER — must match exactly (surgical script didn't touch owner data).

**Manual smoke test on preview:**
- Load Dallas + Tarrant + Collin polygons in three separate workspaces.
- Click 5 parcels per county. Popup + card should show beds/baths/pool/structural fields when data is available.
- Verify Denton parcels show only the pre-existing fields (no spurious "N/A"-fills cluttering the meta line).

## Phase 2 preview (separate ship)

CSV column additions. Carries `compatibility lock` shift discipline — all 4 writerow sites + header coordinated. Mike notification of column-shift. Likely +20-30 new CSV columns. Will require its own spec + Copilot critique.

**v2 addition — `county_source` CSV column:** when introducing the new generic concept columns (Beds, Baths, Pool Flag, Garage Capacity), add a separate `County Source` column that records which county the row's data came from (DCAD / TAD / Collin / Denton). This way:
- Generic concept columns stay short ("Beds", not "DCAD Beds" / "TAD Beds" / "Collin Beds" × 3 redundant columns)
- Data provenance preserved — downstream consumers (Mike's spreadsheets, future filter UIs) can pivot by source
- Reduces CSV column count growth (otherwise we'd need 3-4× as many columns)

Column-naming convention recap for Phase 2:
- **Generic concept columns** (Beds, Baths, Pool Flag, Garage Capacity, Year Built, Living Area, Heating Type, AC Type, Roof Material, Pool Flag, Spa Flag, etc.) — exposed in cross-county data. Single column. Provenance via `County Source`.
- **County-prefixed columns** for unique fields ("DCAD CDU Rating", "DCAD Foundation Type" if not generic enough, "TAD ARB Indicator", "TAD MAPSCO", "Denton - Homestead", etc.) — only one county exposes these.

KK to confirm the generic-vs-prefixed cutover in Phase 2 spec; current default: anything 2+ counties expose → generic. Anything unique to 1 county → prefixed.

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
