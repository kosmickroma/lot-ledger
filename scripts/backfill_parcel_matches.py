# scripts/backfill_parcel_matches.py
#
# Role: One-shot backfill that fills parcel matches on existing global comps
#       rows where parcel_account_num is still NULL.
#       Idempotent by targeting only currently-unmatched rows.
#
# Connects to:
#   api/config.py               - imports session DB connection helpers
#   api/propelio/parcel_match.py - match_comps_to_parcels batch matcher
#   session DB tables           - reads/writes propelio_comps

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from psycopg2.extras import Json, RealDictCursor

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.propelio.parcel_match import match_comps_to_parcels


BATCH_SIZE = 200


def _load_unmatched_rows(cur: RealDictCursor, batch_size: int, last_comp_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            comp_id,
            parsed_payload
        FROM propelio_comps
        WHERE parcel_account_num IS NULL
          AND parsed_payload IS NOT NULL
          AND parsed_payload->>'address' IS NOT NULL
          AND comp_id > %s
        ORDER BY comp_id ASC
        LIMIT %s
        """,
        (int(last_comp_id), int(batch_size)),
    )
    return list(cur.fetchall() or [])


def run_backfill(*, dry_run: bool = False, batch_size: int = BATCH_SIZE) -> None:
    conn = get_session_conn()
    try:
        processed = 0
        matched = 0
        unmatched = 0
        updated = 0
        batch_num = 0
        last_comp_id = 0

        while True:
            with conn.cursor(cursor_factory=RealDictCursor) as read_cur:
                rows = _load_unmatched_rows(read_cur, batch_size, last_comp_id)
            if not rows:
                break

            last_comp_id = int(rows[-1]["comp_id"])

            batch_num += 1
            processed += len(rows)

            comps: list[dict[str, Any]] = []
            comp_ids: list[int] = []
            for row in rows:
                comp_id = int(row["comp_id"])
                payload = row.get("parsed_payload")
                if not isinstance(payload, dict):
                    unmatched += 1
                    continue
                comps.append(dict(payload))
                comp_ids.append(comp_id)

            if not comps:
                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()
                print(
                    f"[parcel-backfill] batch={batch_num} processed={processed} "
                    f"matched={matched} unmatched={unmatched} updated={updated}",
                    flush=True,
                )
                continue

            try:
                matched_comps = match_comps_to_parcels(comps)
            except Exception as exc:
                # Non-fatal: this batch is considered unmatched and we continue.
                unmatched += len(comps)
                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()
                print(
                    f"[parcel-backfill] batch={batch_num} matcher_error={exc} "
                    f"processed={processed} matched={matched} unmatched={unmatched} updated={updated}",
                    flush=True,
                )
                continue

            with conn.cursor() as write_cur:
                for idx, comp in enumerate(matched_comps):
                    comp_id = comp_ids[idx]
                    parcel_account_num = str(comp.get("parcel_account_num") or "").strip() or None
                    parcel_county = str(comp.get("parcel_county") or "").strip() or None
                    parcel_geom = comp.get("parcel_geom") if isinstance(comp.get("parcel_geom"), (dict, list)) else None

                    if parcel_account_num:
                        matched += 1
                        write_cur.execute(
                            """
                            UPDATE propelio_comps
                            SET
                                parcel_account_num = %s,
                                parcel_county = %s,
                                parcel_geom = %s
                            WHERE comp_id = %s
                              AND parcel_account_num IS NULL
                            """,
                            (
                                parcel_account_num,
                                parcel_county,
                                Json(parcel_geom) if parcel_geom is not None else None,
                                comp_id,
                            ),
                        )
                        updated += int(write_cur.rowcount or 0)
                    else:
                        unmatched += 1

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            print(
                f"[parcel-backfill] batch={batch_num} processed={processed} "
                f"matched={matched} unmatched={unmatched} updated={updated}",
                flush=True,
            )

        if dry_run:
            print("[parcel-backfill] dry-run complete (all writes rolled back)", flush=True)
        else:
            print("[parcel-backfill] complete", flush=True)
        print(
            f"[parcel-backfill] final processed={processed} matched={matched} "
            f"unmatched={unmatched} updated={updated}",
            flush=True,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill parcel matches for propelio_comps rows with NULL parcel_account_num")
    parser.add_argument("--dry-run", action="store_true", help="Execute all statements but roll back every batch")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Rows per batch commit (default: 200)")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    run_backfill(dry_run=bool(args.dry_run), batch_size=max(1, int(args.batch_size)))


if __name__ == "__main__":
    main()
