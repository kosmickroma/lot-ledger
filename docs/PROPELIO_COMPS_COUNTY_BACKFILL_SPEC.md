---
created: 2026-05-22
status: draft / pending Copilot review
---

# Propelio Comps `parcel_county` Backfill — Spec

## Problem (recap)

`propelio_comps` has **~867 rows** with `parcel_account_num` populated but `parcel_county` NULL. Frontend bucket lookup at `frontend/map.js:5047` requires the (county, account_num) tuple; missing county causes the comp to fall back to Propelio's MLS taxonomy → ends up in the `off_market` bucket instead of its real category (e.g., multifamily for the 6307 / 6325 BANDERA AVE condo investigation).

Root cause (verified via git history):

- `propelio_comp_archive` (the original workspace-scoped storage) never had a `parcel_county` column.
- `scripts/backfill_propelio_comps.py` (shipped 2026-05-04) migrated archive rows → global `propelio_comps`. Line 131 hard-codes `parcel_county = None` since the source had nothing to read.
- `match_comps_to_parcels` only started populating `parcel_county` on new matches after commit `bd398c1` on 2026-05-10.
- A second backfill run occurred 2026-05-11 (presumably after a re-import of archive data), creating the second wave.

The 867 affected rows are exactly the migration tailings. Net new comps written since 2026-05-12 all have `parcel_county` set correctly.

## Goal

Fill in `parcel_county` for the 867 affected rows by joining `parcel_account_num` against each county's parcel table. Then prevent the bug from re-occurring in future re-runs of the backfill script.

## Changes (3 files)

### 1. New script: `scripts/fix_propelio_comps_county_2026_05_22.py`

One-shot migration that:

- Iterates the 4 county parcel tables: `parcels` (dcad), `tad_parcels`, `collin_parcels`, `denton_parcels`.
- For each, runs an `UPDATE propelio_comps p SET parcel_county = '<county>' FROM <table> c WHERE p.parcel_county IS NULL AND p.parcel_account_num IS NOT NULL AND p.parcel_account_num = c.account_num`.
- Per-county order: dcad → tad → collin → denton. If a comp's account_num appears in multiple county tables (cross-county collision — should be rare but possible), the first match wins.
- Prints before/after counts so we can verify the 867 number.
- Wraps each county UPDATE in its own transaction so a partial failure doesn't roll back already-fixed rows.

Skeleton:

```python
COUNTY_TABLES = [
    ("dcad",   "parcels"),
    ("tad",    "tad_parcels"),
    ("collin", "collin_parcels"),
    ("denton", "denton_parcels"),
]

def main():
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM propelio_comps WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL")
            before = cur.fetchone()[0]
            print(f"Before: {before} rows missing parcel_county")

            for county, table in COUNTY_TABLES:
                cur.execute(f"""
                    UPDATE propelio_comps p
                       SET parcel_county = %s
                      FROM {table} c
                     WHERE p.parcel_county IS NULL
                       AND p.parcel_account_num IS NOT NULL
                       AND p.parcel_account_num = c.account_num
                """, (county,))
                print(f"  {county}: updated {cur.rowcount} rows")
                conn.commit()

            cur.execute("SELECT COUNT(*) FROM propelio_comps WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL")
            after = cur.fetchone()[0]
            print(f"After: {after} rows still missing parcel_county")
            print(f"Fixed: {before - after} rows")
    finally:
        release_session_conn(conn)
```

### 2. Fix `scripts/backfill_propelio_comps.py` line 131

Currently:

```python
"parcel_county": None,
```

Change to: call `match_comps_to_parcels` on each batch BEFORE the upsert. This ensures any future re-run of the script populates `parcel_county` correctly even though the source `propelio_comp_archive` doesn't carry it.

Sketch:

```python
from api.propelio.parcel_match import match_comps_to_parcels

# ... existing loop that builds rows ...

# Before upsert: enrich the batch with parcel_county / parcel_geom
comp_dicts = [r["parsed_payload"] for r in batch]
match_comps_to_parcels(comp_dicts)  # mutates in place
for i, c in enumerate(comp_dicts):
    batch[i]["parcel_county"] = c.get("parcel_county")
    batch[i]["parcel_geom"]   = c.get("parcel_geom") or batch[i].get("parcel_geom")
```

Note: `match_comps_to_parcels` expects each comp to have `address`, `lat`, `lng`. The backfill batches read those from `parsed_payload` (the JSONB blob), so they're already there.

### 3. Add a one-line note to `docs/PROPELIO_COMPS_MISSING_COUNTY_INVESTIGATION.md`

> **2026-05-22 resolution:** Backfilled the 867 affected rows via `scripts/fix_propelio_comps_county_2026_05_22.py` and patched the source script (`scripts/backfill_propelio_comps.py:131`) so future re-runs match parcels properly. Verified zero rows still missing `parcel_county` post-migration. See `docs/PROPELIO_COMPS_COUNTY_BACKFILL_SPEC.md`.

## Verification plan

1. **Pre-flight on dev DB:**
   ```sql
   SELECT COUNT(*) FROM propelio_comps WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL;
   -- expect: ~867 (matches investigation doc baseline)
   ```

2. **Run the migration on dev:** `python3 scripts/fix_propelio_comps_county_2026_05_22.py`

3. **Post-migration check:**
   ```sql
   SELECT COUNT(*) FROM propelio_comps WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL;
   -- expect: 0
   ```

4. **Sanity check 6307 / 6325 BANDERA:** find the affected comp rows by their `parcel_account_num`, confirm `parcel_county = 'dcad'` post-migration.

5. **Smoke on preview:** toggle Multifamily off on the BANDERA area → 6307 and 6325 should both hide (since their backing comps now resolve to multifamily bucket, not off_market).

6. **Promote to Mike's prod** only after smoke passes on dev.

## Risk

- **Cross-county account_num collisions:** TX appraisal districts use independent account_num schemes. A 17-digit DCAD account_num is unlikely to collide with TAD's 8-digit, Collin's, or Denton's. We pick first-match-wins ordered dcad→tad→collin→denton; if any ambiguous match shows up, it'll be unusual enough to investigate manually.
- **Frozen-source rows:** if any 867 row's `parcel_account_num` doesn't match ANY of the 4 county tables, it stays NULL. That's actually fine — those comps are genuinely unmatchable to a CAD parcel and the current `off_market` fallback is the right behavior for them. We log them out so KK can eyeball if needed.
- **Concurrent writes during migration:** the UPDATEs operate on `parcel_county IS NULL` rows only. New writes always set parcel_county now (since 2026-05-10 fix) so there's no race risk.

## Rollback plan

If migration goes wrong:
```sql
-- Revert specific county's updates by re-nulling
UPDATE propelio_comps SET parcel_county = NULL WHERE parcel_county = 'dcad' AND <some criteria>;
```
But this is a forward-only fix — there's no reason to roll back a row from "correctly populated county" back to NULL.

## Order of operations

1. KK reviews this spec
2. Copilot critique (per `feedback_db_production_discipline`)
3. Address any feedback
4. Write the two files (new script + script edit)
5. Run on dev DB → verify counts
6. Smoke test on preview (BANDERA filter check)
7. If green: run same script on Mike's prod DB
8. Smoke on Mike's prod
9. Update investigation doc with resolution note

## Out of scope

- Frontend defense (Option B from investigation doc) — not needed once data is clean.
- Duplex-split feature — separate PR, after this lands.
- Schema change to enforce `parcel_county` NOT NULL — could add as a follow-up but optional since `match_comps_to_parcels` already always sets it.
