#!/usr/bin/env python3
# scripts/backfill_property_zip_from_zcta.py
#
# Populate <county>_parcels.property_zip by spatial-joining each parcel's
# centroid against zcta_polygons (ST_Covers). Uses backfill_audit_rows as
# a run-id audit ledger for precise per-row rollback (NOT timestamp-scoped,
# which would clobber rows updated by unrelated processes in the same window).
#
# Companion to scripts/ingest_zcta_polygons.py. Run that first to load the
# ZCTA polygons and create the audit-ledger table.
#
# CLI:
#   --county [tad|collin|denton|dcad|all]   (required)
#   --mode   [fill|verify|force]            (default: fill)
#   --run-id <uuid>                         (optional; auto-generated)
#   --no-vacuum                             (skip post-fill VACUUM ANALYZE)
#
# Modes:
#   fill   — UPDATE rows where property_zip IS NULL OR ''
#   verify — SELECT-only audit: count + sample of rows whose existing
#            property_zip[:5] disagrees with the ZCTA-derived zip5
#   force  — UPDATE every row (overwrites existing zips); prints a loud banner
#
# Per-county PK (verified via pg_index on prod DB):
#   tad_parcels, collin_parcels, denton_parcels  → parcel_key (btree)
#   parcels (DCAD)                                → account_num (btree)
#
# Run:
#   .venv/bin/python3 scripts/backfill_property_zip_from_zcta.py --county tad --mode fill

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


# Per-county: (table_name, PK column). PK differs by county; verified on prod DB.
COUNTY_TABLES: dict[str, tuple[str, str]] = {
    "tad":    ("tad_parcels",    "parcel_key"),
    "collin": ("collin_parcels", "parcel_key"),
    "denton": ("denton_parcels", "parcel_key"),
    "dcad":   ("parcels",        "account_num"),
}

BATCH_SIZE = 25000


def _quote_ident(ident: str) -> str:
    """Quote a SQL identifier. Whitelist alphanum + underscore."""
    if not ident.replace("_", "").isalnum():
        raise ValueError(f"Unsafe identifier: {ident!r}")
    return f'"{ident}"'


def _fetch_target_pks(conn, table: str, pk_col: str, mode: str) -> list:
    """Return ordered list of PK values to process based on mode."""
    tbl, pk = _quote_ident(table), _quote_ident(pk_col)
    if mode in ("fill", "verify"):
        where = (
            "WHERE centroid IS NOT NULL "
            f"AND ({pk} IS NOT NULL) "
            "AND (property_zip IS NULL OR TRIM(property_zip) = '')"
        )
    elif mode == "force":
        where = f"WHERE centroid IS NOT NULL AND ({pk} IS NOT NULL)"
    else:
        raise ValueError(f"unknown mode: {mode}")

    with conn.cursor() as cur:
        cur.execute(f"SELECT {pk} FROM {tbl} {where} ORDER BY {pk}")
        return [r[0] for r in cur.fetchall()]


def _count_null_centroid(conn, table: str) -> int:
    tbl = _quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE centroid IS NULL")
        return cur.fetchone()[0]


def _fill_batch(conn, table: str, pk_col: str, lo, hi, run_id: str, county: str, force: bool) -> int:
    """UPDATE...RETURNING into audit ledger for one batch. Returns rows touched."""
    tbl, pk = _quote_ident(table), _quote_ident(pk_col)
    null_check = "" if force else "AND (p.property_zip IS NULL OR TRIM(p.property_zip) = '')"

    sql = (
        "WITH updated AS ("
        f"  UPDATE {tbl} p "
        "  SET property_zip = z.zcta5, updated_at = now() "
        "  FROM zcta_polygons z "
        "  WHERE ST_Covers(z.geom, p.centroid) "
        "    AND p.centroid IS NOT NULL "
        f"    {null_check} "
        f"    AND p.{pk} >= %s AND p.{pk} <= %s "
        f"  RETURNING p.{pk} "
        ") "
        "INSERT INTO backfill_audit_rows (run_id, county, pk_col, pk_value) "
        f"SELECT %s, %s, %s, {pk} FROM updated"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (lo, hi, run_id, county, pk_col))
        rows = cur.rowcount
    conn.commit()
    return max(rows, 0)


def _verify_county(conn, county: str, table: str, pk_col: str) -> None:
    """SELECT-only: count + sample of rows whose existing zip disagrees with ZCTA."""
    tbl, pk = _quote_ident(table), _quote_ident(pk_col)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {tbl} p "
            "WHERE p.centroid IS NOT NULL "
            "  AND p.property_zip IS NOT NULL "
            "  AND TRIM(p.property_zip) <> '' "
            "  AND EXISTS ("
            "    SELECT 1 FROM zcta_polygons z "
            "    WHERE ST_Covers(z.geom, p.centroid) "
            "      AND z.zcta5 <> LEFT(REGEXP_REPLACE(p.property_zip, '[^0-9]', '', 'g'), 5)"
            "  )"
        )
        diverge = cur.fetchone()[0]

        cur.execute(
            f"SELECT p.{pk}, p.property_zip, "
            "  (SELECT z.zcta5 FROM zcta_polygons z "
            "   WHERE ST_Covers(z.geom, p.centroid) LIMIT 1) AS zcta_zip "
            f"FROM {tbl} p "
            "WHERE p.centroid IS NOT NULL "
            "  AND p.property_zip IS NOT NULL "
            "  AND TRIM(p.property_zip) <> '' "
            "  AND LEFT(REGEXP_REPLACE(p.property_zip, '[^0-9]', '', 'g'), 5) <> "
            "      (SELECT z.zcta5 FROM zcta_polygons z WHERE ST_Covers(z.geom, p.centroid) LIMIT 1) "
            "LIMIT 10"
        )
        samples = cur.fetchall()

    print(f"[verify] county={county} divergent_rows={diverge}")
    if samples:
        print("[verify]   sample (pk, existing_zip, zcta_zip):")
        for pk_val, existing, zcta in samples:
            print(f"[verify]     {pk_val!r:25} existing={existing!r:15} zcta={zcta!r}")


def _vacuum_analyze(conn, table: str) -> None:
    """VACUUM ANALYZE must run outside a transaction."""
    tbl = _quote_ident(table)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"VACUUM ANALYZE {tbl}")
    finally:
        conn.autocommit = False


def _process_county(county: str, mode: str, run_id: str, do_vacuum: bool) -> int:
    table, pk_col = COUNTY_TABLES[county]
    conn = get_conn()
    try:
        if mode == "verify":
            _verify_county(conn, county, table, pk_col)
            return 0

        if mode == "force":
            print(f"[backfill] !!! FORCE MODE — OVERWRITING ALL ROWS for county={county} !!!")

        null_centroids = _count_null_centroid(conn, table)

        pks = _fetch_target_pks(conn, table, pk_col, mode)
        total = len(pks)
        if total == 0:
            print(
                f"[backfill] county={county} run_id={run_id} total=0 filled=0 "
                f"skipped_null_centroid={null_centroids}"
            )
            if do_vacuum:
                _vacuum_analyze(conn, table)
            return 0

        filled = 0
        force_flag = (mode == "force")
        for chunk_start in range(0, total, BATCH_SIZE):
            chunk = pks[chunk_start:chunk_start + BATCH_SIZE]
            lo, hi = chunk[0], chunk[-1]
            n = _fill_batch(conn, table, pk_col, lo, hi, run_id, county, force_flag)
            filled += n
            print(
                f"[backfill]   batch {chunk_start // BATCH_SIZE + 1}: "
                f"range=({lo!r}, {hi!r}) touched={n}"
            )

        print(
            f"[backfill] county={county} run_id={run_id} total={total} filled={filled} "
            f"skipped_null_centroid={null_centroids}"
        )

        if do_vacuum:
            print(f"[backfill] running VACUUM ANALYZE {table} ...")
            _vacuum_analyze(conn, table)
            print("[backfill] vacuum complete")

        return 0
    finally:
        release_conn(conn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--county",
        required=True,
        choices=list(COUNTY_TABLES.keys()) + ["all"],
    )
    parser.add_argument("--mode", default="fill", choices=["fill", "verify", "force"])
    parser.add_argument(
        "--run-id",
        default=str(uuid.uuid4()),
        help="UUID tagging every audit row in this run (default: auto-generated).",
    )
    parser.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM ANALYZE post-fill.")
    args = parser.parse_args()

    counties = list(COUNTY_TABLES.keys()) if args.county == "all" else [args.county]

    print(f"[backfill] run_id={args.run_id} mode={args.mode} counties={counties}")

    for county in counties:
        try:
            rc = _process_county(county, args.mode, args.run_id, do_vacuum=(not args.no_vacuum))
            if rc != 0:
                return rc
        except Exception as e:
            print(f"[backfill] ERROR processing county={county}: {e}", file=sys.stderr)
            raise

    return 0


if __name__ == "__main__":
    sys.exit(main())
