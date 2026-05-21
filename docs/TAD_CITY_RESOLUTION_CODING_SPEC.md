---
title: TAD City Resolution — Coding Spec
status: READY FOR COPILOT
date: 2026-05-21
branch: feat/tad-city-resolution-2026-05-21 (off main)
deployment: PREVIEW ONLY until KK greenlights develop merge
discovered_via: 2026-05-21 investigation — DCAD's ACCOUNT_INFO.CSV publishes PROPERTY_CITY (we never pulled it), AND TAD publishes Cities/Cities.shp with CITY_TDC → CITY_NAME (we never ingested it).
---

# TAD City Resolution — Coding Spec

## What this changes

Tarrant County (TAD) parcels currently show as street-only in the Subject Property row and the parcel popup ("2921 GIPSON" with no town). The TAD source data publishes a `Cities/Cities.shp` shapefile we already have on disk. Its DBF contains a `CITY_TDC` (3-char code) → `CITY_NAME` mapping that lines up directly with the `city_code` column we already store in `tad_parcels`.

Implementing this populates `property_city` for every TAD parcel that has a valid `city_code`, which is most of them. Once `property_city` flows through `build_feature` and the per-county address normalizer (`_formatPropertyAddress` in `frontend/map.js`), TAD parcels render as `STREET CITY` cleanly — same shape as Denton already does today.

## Scope (in & out)

**IN:**
- Ingest TAD's `Cities/Cities.shp` DBF into a small lookup table `tad_city_lookup(city_tdc TEXT PRIMARY KEY, city_name TEXT)`
- Add `property_city TEXT` column to `tad_parcels` (idempotent ALTER step in `_run_schema_steps`)
- One-shot backfill: `UPDATE tad_parcels SET property_city = lookup.city_name WHERE tad_parcels.city_code = lookup.city_tdc`
- Update `api/counties/tad.py:_normalize_tad_row` to emit `property_city` in the returned row dict
- Update TAD SELECT statements in `api/main.py:_fetch_tad_parcel_by_account` + `api/counties/tad.py:query_tad_parcels` to include `property_city`
- Update `frontend/map.js:_formatPropertyAddress` so the `tad` case uses the same Denton-style "STREET CITY" formatter instead of street-only
- Tests: small pytest for the formatter case + the lookup table existence + one DB query verifying TAD parcels have property_city populated

**OUT (separate follow-ups):**
- DCAD's `PROPERTY_CITY` column ingest (separate fix, same shape but different source — file separately)
- Neighborhood polygons (`Neighborhoods/NBHD.shp`) — capture in master_todo, ingest later as overlay layer
- Subdivision-suffix parser fallback for TAD parcels missing `city_code` — Phase 2
- ISD-based fallback — Phase 2
- HOA polygons for Tarrant — separate item
- Bedrooms/baths/pool ingest expansion for TAD — separate item

## Files to modify / create

```
scripts/build_tad_city_lookup.py   — NEW: one-shot ingest of Cities.shp DBF
api/main.py                        — add property_city column to schema steps + SELECT in _fetch_tad_parcel_by_account
api/counties/tad.py                — _normalize_tad_row emits property_city + query_tad_parcels SELECT
frontend/map.js                    — _formatPropertyAddress tad case: street-only → "STREET CITY" formatter (same as denton)
tests/test_tad_city_resolution.py  — NEW: formatter cases + lookup table assertion
```

## Source data confirmation

Run by hand from a Python prompt to verify before coding:

```python
import shapefile
r = shapefile.Reader('ingest/counties/tarrant/tad/2026-05-01/unzipped/Cities/Cities')
# Expected fields (already confirmed 2026-05-21):
# ['CITY_TDC', 'C', 3, 0]
# ['CITY_NAME', 'C', 35, 0]
# ['CITY_TEXT', 'C', 50, 0]
# Sample record 0: ['001', 'Azle', 'AZLE CITY LIMITS    001', ...]
```

The DBF has ~40 rows (one per Tarrant city). Just read it once into PostgreSQL.

## Edit 1 — `scripts/build_tad_city_lookup.py` (NEW)

A short standalone script that reads `Cities.shp`'s DBF and upserts to a `tad_city_lookup` table. Idempotent. Pattern after `scripts/build_tad_db.py` for the connection management (read `.env`, use psycopg2, batch upsert).

```python
"""Build/refresh the TAD city-code lookup table from Cities.shp DBF.

Source: ingest/counties/tarrant/tad/<snapshot>/unzipped/Cities/Cities.shp
Target: lotledger.tad_city_lookup (city_tdc PRIMARY KEY, city_name)
"""
import argparse
import os
import shapefile
import psycopg2
from pathlib import Path

DEFAULT_SOURCE = Path(__file__).parent.parent / "ingest/counties/tarrant/tad/2026-05-01/unzipped/Cities/Cities"

def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tad_city_lookup (
                city_tdc TEXT PRIMARY KEY,
                city_name TEXT NOT NULL,
                city_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.commit()

def _upsert_rows(conn, rows):
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO tad_city_lookup (city_tdc, city_name, city_text)
            VALUES (%s, %s, %s)
            ON CONFLICT (city_tdc) DO UPDATE SET
                city_name = EXCLUDED.city_name,
                city_text = EXCLUDED.city_text,
                updated_at = now()
        """, rows)
        conn.commit()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Path to Cities.shp (no extension)")
    args = parser.parse_args()

    reader = shapefile.Reader(args.source, encoding="latin-1")
    fields = [f[0] for f in reader.fields[1:]]
    rows = []
    for rec in reader.iterShapeRecords():
        record = dict(zip(fields, rec.record))
        city_tdc = str(record.get("CITY_TDC") or "").strip()
        city_name = str(record.get("CITY_NAME") or "").strip().upper()
        city_text = str(record.get("CITY_TEXT") or "").strip()
        if city_tdc and city_name:
            rows.append((city_tdc, city_name, city_text))

    print(f"Loaded {len(rows)} city rows from {args.source}")

    # DB connection from env vars
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        _ensure_table(conn)
        _upsert_rows(conn, rows)
        print(f"Upserted {len(rows)} rows to tad_city_lookup")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
```

## Edit 2 — `api/main.py` schema steps

Add idempotent ALTER to add `property_city` column on `tad_parcels`. Find the existing `_run_schema_steps` list and add:

```python
("tad_parcels_property_city", "ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS property_city TEXT"),
```

## Edit 3 — `api/main.py:_fetch_tad_parcel_by_account` SELECT

Find the SELECT statement in `_fetch_tad_parcel_by_account` (~ line 2480 area). Add `t.property_city` to the SELECT list. (Schema column must exist first per Edit 2.)

## Edit 4 — `api/counties/tad.py:query_tad_parcels` (or equivalent) SELECT

Find the polygon-analysis SELECT for TAD. Add `property_city` to the column list.

## Edit 5 — `api/counties/tad.py:_normalize_tad_row` output

The current function maps row columns to a normalized dict. Add `"property_city": _clean_text(raw.get("property_city"))` to the returned dict. (Tests for this function should already be updated to assert this key.)

The wire-up to `build_feature` already exists — `build_feature` reads `row.get("property_city")` at line 500 of dcad.py and emits it into `props.city`. No changes needed there.

## Edit 6 — `frontend/map.js:_formatPropertyAddress` (tad case)

The current `tad` case in `_formatPropertyAddress` returns street-only:

```js
case "tad":
case "dcad": {
  // No reliable city in source. Strip any stray comma fragments.
  return addr.split(",")[0].trim() || addr;
}
```

Change the TAD case to use the Denton-style formatter (since TAD's `addr` is street-only AND `property_city` is now populated separately):

```js
case "tad": {
  // 2026-05-21: TAD now ships property_city via tad_city_lookup join (see
  // scripts/build_tad_city_lookup.py). addr is still street-only from the
  // ParcelView source; property_city comes from the lookup table.
  const street = addr.split(",")[0].trim();
  if (city && street) {
    const lower = street.toLowerCase();
    const cityLower = city.toLowerCase();
    if (!lower.endsWith(cityLower)) {
      return `${street} ${city}`;
    }
  }
  return street || addr;
}
case "dcad": {
  // DCAD has no property_city in source data yet. Strip stray commas.
  // (Separate follow-up: ingest PROPERTY_CITY from ACCOUNT_INFO.CSV.)
  return addr.split(",")[0].trim() || addr;
}
```

## Edit 7 — Backfill SQL (run after Edit 1 ingests the lookup)

One-shot SQL to populate `property_city` on existing rows:

```sql
UPDATE tad_parcels
SET property_city = lookup.city_name
FROM tad_city_lookup lookup
WHERE tad_parcels.city_code = lookup.city_tdc
  AND (tad_parcels.property_city IS NULL OR tad_parcels.property_city = '');
```

Verification queries:

```sql
-- (a) How many TAD parcels got populated?
SELECT COUNT(*) FILTER (WHERE property_city IS NOT NULL AND property_city != '') AS populated,
       COUNT(*) FILTER (WHERE property_city IS NULL OR property_city = '') AS still_missing
FROM tad_parcels;

-- (b) Sanity check on top cities
SELECT property_city, COUNT(*) FROM tad_parcels
WHERE property_city IS NOT NULL AND property_city != ''
GROUP BY property_city ORDER BY COUNT(*) DESC LIMIT 10;
-- Expect: Fort Worth dominates, then Arlington, then Bedford / North Richland Hills / Hurst / etc.

-- (c) What city_codes failed lookup?
SELECT t.city_code, COUNT(*) FROM tad_parcels t
LEFT JOIN tad_city_lookup l ON t.city_code = l.city_tdc
WHERE t.city_code IS NOT NULL AND t.city_code != '' AND l.city_tdc IS NULL
GROUP BY t.city_code ORDER BY COUNT(*) DESC LIMIT 20;
-- Should be near-zero. "000" (UNINCORPORATED) may legitimately not appear in the lookup.
```

## Edit 8 — Pytest tests

`tests/test_tad_city_resolution.py`:

```python
"""Tests for TAD city resolution (Cities.shp DBF → tad_city_lookup → property_city)."""
import pytest
from api.counties.tad import _normalize_tad_row

def test_normalize_passes_through_property_city():
    raw = {"property_city": "ARLINGTON", "city_code": "026", ...}
    normalized = _normalize_tad_row(raw)
    assert normalized["property_city"] == "ARLINGTON"

def test_normalize_handles_missing_city():
    raw = {"city_code": "999", ...}  # no property_city
    normalized = _normalize_tad_row(raw)
    assert normalized.get("property_city") in (None, "")
```

(Frontend formatter is exercised manually on preview; no JS test framework wired up for these tests today.)

## Constraints

- **No DB schema changes outside the one ALTER + one CREATE TABLE.** Both idempotent and additive.
- **No spatial PostGIS work** — purely lookup + JOIN.
- **No co-authored-by trailers.**
- **Preview-only deploy** — do NOT promote to develop or main without explicit greenlight from KK after smoke testing.
- **Don't accidentally remove TAD's existing CSV columns or fields** — additive only.

## Deployment plan

1. Code edits per spec
2. Run `scripts/build_tad_city_lookup.py` locally with `.env` connecting to Mike's prod DB → populates the lookup table (one-time)
3. Manual backfill SQL on Mike's prod DB → populates `property_city` on existing TAD rows
4. Manual preview deploy: `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`
5. Smoke test on preview: load a Fort Worth or Arlington polygon → TAD parcels should show "STREET CITY" in popups + Subject Property row + CSV
6. KK reviews → greenlight → merge to develop → soak → eventually main → Mike's prod

## Rollback

Single revert commit plus optional schema cleanup:
```bash
git revert <merge-commit-sha>
# Optional (only if rolling back DB):
# DROP TABLE tad_city_lookup;
# ALTER TABLE tad_parcels DROP COLUMN property_city;
```

The schema additions are additive — leaving them in place after a code-only revert is harmless.

## Questions for Copilot critique

1. **Should the lookup table be `(city_tdc, city_name)` only, or should we also store the polygon geometry from Cities.shp for future spatial features?** Cost-benefit: storing geometry adds ~5-15 MB but unlocks "show me parcels in [city]" spatial filtering later.

2. **For the few TAD parcels with empty/missing `city_code`** (e.g., the "000" UNINCORPORATED ones), should we leave `property_city` NULL or set a fallback like "UNINCORPORATED"? Frontend handling difference?

3. **Where in `query_tad_parcels` should the JOIN happen?** Two options:
   - (a) JOIN against `tad_city_lookup` at query time in the SELECT (extra JOIN per analyze call but always fresh)
   - (b) Store `property_city` directly on `tad_parcels` via the backfill (no JOIN at query time, denormalized; backfill must re-run when city_code changes on a parcel — rare)
   - The spec proposes (b). Is (a) better for any reason?

4. **What about the CASE LOWER for city_name?** Cities.shp has "Azle" (mixed case). Should we store as "AZLE" (upper, matches Collin/Denton pattern), as "Azle" (source-faithful), or something else for display?

5. **Should the ingest script be idempotent on PRIMARY KEY conflict, or should we DROP/RECREATE the table on each run?** Spec uses ON CONFLICT DO UPDATE. Any concern?

6. **Anything else?** Push back on the spec where you disagree.

## Cross-refs

- Master TODO entry: `📊 Data / ingestion` → "DCAD + TAD city resolution" item #6 (the 2026-05-21 update)
- Adjacent: `docs/PROPELIO_COMPS_MISSING_COUNTY_INVESTIGATION.md` (separate investigation, same overall theme: source data we already have but don't use)
- Memory: `feedback_db_production_discipline` — spec → critique → preview workflow applies
