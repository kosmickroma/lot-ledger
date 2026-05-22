---
created: 2026-05-22
status: v2 — Copilot deep-dive + audit incorporated, ready to execute
updated: 2026-05-22 (post audit + Copilot critique)
---

# Propelio Comps `parcel_county` Backfill — Spec

## Changelog

- **v1 (initial draft):** Backfill strategy + dcad→tad→collin→denton county-order, optimistic on collision risk.
- **v2 (THIS):** Audit run + Copilot review incorporated:
  - Pre-flight collision audit run on dev DB. Real numbers: **826** affected account_nums (not 867 — 41 rows self-healed via live `match_comps_to_parcels` since the original investigation doc).
  - **Zero unmatchable** rows. Every affected account_num matches at least one county table → Copilot Issue 4 (residual NULL audit) is moot.
  - **9 collisions found** — ALL between Collin↔Denton (geographically adjacent, plausible historical numbering overlap). Zero DCAD or TAD collisions.
  - Strategy upgrade: **geo-based tiebreaker** (PostGIS point-in-polygon on the comp's lat/lng) for the 9 collisions, instead of arbitrary county-order policy.
  - Backfill-script patch reworked to be explicit about mutation source (Copilot Issue 2).
  - Sequencing locked: apply backfill-script patch BEFORE running fix script (Copilot Issue 3, race-safety).
  - **Post-fix CHECK constraint** added: `(parcel_account_num IS NULL AND parcel_county IS NULL) OR (parcel_account_num IS NOT NULL AND parcel_county IS NOT NULL)` — bug class can't recur (Copilot Issue 6).
  - Frontend defense (Copilot Issue 5) deferred — not needed for this 826-row batch since all rows resolve cleanly.

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

**Important sequencing:** apply the patch to `scripts/backfill_propelio_comps.py:131` FIRST (see section 2), then run this fix script. Reason: if the legacy backfill script runs WHILE this fix script is running, the legacy script's `ON CONFLICT DO UPDATE` path would set `parcel_county = EXCLUDED.parcel_county = NULL` and undo our fixes (Copilot Issue 3).

One-shot migration that:

- **Phase 1 — Unambiguous matches (817 rows):** for each of the 4 county parcel tables (`parcels`=dcad, `tad_parcels`, `collin_parcels`, `denton_parcels`), run `UPDATE propelio_comps p SET parcel_county = '<county>' FROM <table> c WHERE p.parcel_county IS NULL AND p.parcel_account_num = c.account_num AND p.parcel_account_num NOT IN (<collision set>)`. The `NOT IN (<collision set>)` filter excludes the 9 known collisions so they only get touched in Phase 2.
- **Phase 2 — Collision tiebreaker (9 rows):** for each of the 9 known Collin↔Denton collision account_nums, look up the comp's `lat, lng` and use PostGIS `ST_Contains` against each county's polygon to resolve the right county. Deterministic, audit-able, matches geographic reality.
- **Phase 3 — Verification:** count post-fix rows still missing parcel_county. Expected: **0**.
- **Phase 4 — CHECK constraint:** add `ALTER TABLE propelio_comps ADD CONSTRAINT parcel_attrs_paired CHECK ((parcel_account_num IS NULL AND parcel_county IS NULL) OR (parcel_account_num IS NOT NULL AND parcel_county IS NOT NULL))` so the bug class can't recur.
- Each phase is its own transaction. Partial failure doesn't roll back already-fixed rows.
- Prints before/after counts + per-county breakdown so we can verify the 826 number.

Skeleton:

```python
# Known Collin↔Denton collisions discovered during the audit on 2026-05-22.
# These need geo-resolution; everything else is unambiguous.
KNOWN_COLLISIONS = {
    "1086620", "22308", "1088101", "267311", "1082508",
    "1081340", "215751", "112933", "269435",
}

COUNTY_TABLES = [
    ("dcad",   "parcels"),
    ("tad",    "tad_parcels"),
    ("collin", "collin_parcels"),
    ("denton", "denton_parcels"),
]

def main():
    session_conn = get_session_conn()
    main_conn = get_main_conn()  # for the geo-resolution helper

    try:
        with session_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM propelio_comps
                WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL
            """)
            before = cur.fetchone()[0]
            print(f"Before: {before} rows missing parcel_county")

            # --- PHASE 1: unambiguous (817 rows) ---
            # Skip the 9 collision account_nums so they only get touched in Phase 2.
            collision_list = list(KNOWN_COLLISIONS)
            for county, table in COUNTY_TABLES:
                cur.execute(f"""
                    UPDATE propelio_comps p
                       SET parcel_county = %s
                      FROM {table} c
                     WHERE p.parcel_county IS NULL
                       AND p.parcel_account_num IS NOT NULL
                       AND p.parcel_account_num = c.account_num
                       AND NOT (p.parcel_account_num = ANY(%s))
                """, (county, collision_list))
                print(f"  Phase 1 — {county}: {cur.rowcount} rows updated")
                session_conn.commit()

            # --- PHASE 2: collision resolution via PostGIS containment ---
            cur.execute("""
                SELECT comp_id, parcel_account_num, lat, lng
                  FROM propelio_comps
                 WHERE parcel_county IS NULL
                   AND parcel_account_num IS NOT NULL
                   AND parcel_account_num = ANY(%s)
            """, (collision_list,))
            collision_rows = cur.fetchall()
            print(f"  Phase 2 — {len(collision_rows)} collision rows to resolve via geo")

            for comp_id, acct, lat, lng in collision_rows:
                county = _geo_resolve_county(main_conn, acct, lat, lng)
                if county is None:
                    print(f"    ⚠️  {acct} could not be geo-resolved — leaving NULL")
                    continue
                cur.execute("""
                    UPDATE propelio_comps
                       SET parcel_county = %s
                     WHERE comp_id = %s
                """, (county, comp_id))
                print(f"    {acct} ({lat:.5f},{lng:.5f}) → {county}")
            session_conn.commit()

            # --- PHASE 3: verify ---
            cur.execute("""
                SELECT COUNT(*) FROM propelio_comps
                WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL
            """)
            after = cur.fetchone()[0]
            print(f"After: {after} rows still missing parcel_county")
            print(f"Fixed: {before - after} rows")

            # --- PHASE 4: schema guardrail ---
            if after == 0:
                cur.execute("""
                    ALTER TABLE propelio_comps
                    ADD CONSTRAINT parcel_attrs_paired
                    CHECK (
                        (parcel_account_num IS NULL AND parcel_county IS NULL)
                        OR
                        (parcel_account_num IS NOT NULL AND parcel_county IS NOT NULL)
                    )
                """)
                session_conn.commit()
                print("  CHECK constraint added — bug class can't recur")
            else:
                print(f"  Skipping CHECK constraint — {after} rows still missing parcel_county")
    finally:
        release_session_conn(session_conn)
        release_main_conn(main_conn)


def _geo_resolve_county(main_conn, account_num, lat, lng):
    """For a Collin↔Denton collision, pick whichever county polygon
    actually contains the comp's lat/lng. Returns 'collin' or 'denton'.
    Returns None if neither polygon contains the point (e.g. lat/lng missing
    or comp coords drifted outside both polygons).
    """
    if lat is None or lng is None:
        return None
    with main_conn.cursor() as cur:
        # Try Collin first (alphabetical, but order doesn't matter — we test both).
        cur.execute("""
            SELECT 1 FROM collin_parcels
             WHERE account_num = %s
               AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
             LIMIT 1
        """, (account_num, lng, lat))
        if cur.fetchone():
            return "collin"
        cur.execute("""
            SELECT 1 FROM denton_parcels
             WHERE account_num = %s
               AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
             LIMIT 1
        """, (account_num, lng, lat))
        if cur.fetchone():
            return "denton"
    return None
```

**The 9 known collision account_nums** (all Collin↔Denton, verified on 2026-05-22):
`1086620, 22308, 1088101, 267311, 1082508, 1081340, 215751, 112933, 269435`

### 2. Fix `scripts/backfill_propelio_comps.py` line 131 — REFACTORED per Copilot Issue 2

Currently line 131:

```python
"parcel_county": None,
```

**Copilot Issue 2:** the original sketched patch had nested list/payload confusion that worked by accident. The refactor below makes the mutation source explicit so there's no doubt which dict gets mutated and where the result flows.

Refactor approach: do the matching at the BATCH PROCESSING stage (before `_extract_comp_fields` is called), mutating each comp's `parsed_payload` JSONB blob in place via `match_comps_to_parcels`. Then `_extract_comp_fields` reads `parcel_county` from the same mutated dict — no nested lookup.

```python
# In the main batch loop, after fetching `batch` rows from propelio_comp_archive:
from api.propelio.parcel_match import match_comps_to_parcels

# 1. Pull out each archive row's parsed_payload (the comp dict).
comp_dicts = [row.get("comp_data") for row in batch if row.get("comp_data")]

# 2. Mutate in place: match_comps_to_parcels reads address+lat+lng,
#    writes parcel_county / parcel_account_num / parcel_geom.
match_comps_to_parcels(comp_dicts)

# 3. _extract_comp_fields now reads parcel_county from the mutated dict
#    instead of the hardcoded None. Update line 131:
#       "parcel_county": str(comp_data.get("parcel_county") or "").strip() or None,
```

Then change line 131 itself from `"parcel_county": None,` to:

```python
"parcel_county": str(comp_data.get("parcel_county") or "").strip() or None,
```

This makes the mutation source explicit: `match_comps_to_parcels` mutates `comp_data` (the parsed_payload blob), and `_extract_comp_fields` reads back from the same blob. No nested-list ambiguity.

Note: `match_comps_to_parcels` expects each comp to have `address` and `lat`/`lng` (or `extra.lat`/`extra.lon`). The archive's `comp_data` JSONB carries these from the original Propelio response, so the input shape is already correct.

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
