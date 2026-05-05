#!/usr/bin/env python3
"""
One-time migration: create functional B-tree indexes on address columns used by
/api/address/suggest. Without these, every cold typeahead query is a full seq-scan
of ~2.1M rows across four county tables and regularly hits the 900ms statement_timeout.

Run once against the live database, then you're done:
    python scripts/add_address_search_indexes.py

Each index takes 30–90 seconds on a 500k-row table. The script runs them
sequentially and prints progress. Safe to re-run (IF NOT EXISTS).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.config import get_conn, release_conn  # noqa: E402

INDEXES = [
    (
        "parcels",
        "idx_parcels_property_address_upper",
        "upper(property_address)",
    ),
    (
        "tad_parcels",
        "idx_tad_parcels_situs_addr_upper",
        "upper(situs_addr)",
    ),
    (
        "collin_parcels",
        "idx_collin_parcels_property_address_upper",
        "upper(property_address)",
    ),
    (
        "denton_parcels",
        "idx_denton_parcels_property_address_upper",
        "upper(property_address)",
    ),
]


def main() -> None:
    conn = get_conn()
    # autocommit required for CREATE INDEX CONCURRENTLY
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for table, idx_name, expr in INDEXES:
                print(f"  {table}: creating {idx_name} ...", end=" ", flush=True)
                cur.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name} ON {table} ({expr})"
                )
                print("done")
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.autocommit = False
        release_conn(conn)

    print("All indexes created. Address search will now use index scans.")


if __name__ == "__main__":
    main()
