# scripts/backfill_extra_cols.py
#
# Role: One-shot backfill for typed Propelio columns on existing global
#       comps rows. Populates only currently-NULL typed columns from
#       raw_payload and leaves already-populated values untouched.
#
# Connects to:
#   api/config.py            - imports session DB connection helpers
#   session DB tables        - reads/writes propelio_comps

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from psycopg2.extras import RealDictCursor

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn


BATCH_SIZE = 200


def _pick(*vals: Any) -> Any:
    for val in vals:
        if val not in (None, ""):
            return val
    return None


def _txt(v: Any) -> str | None:
    if v is None:
        return None
    text = str(v).strip()
    return text or None


def _int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> bool | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        return v
    text = str(v).strip().lower()
    if text in {"true", "t", "1", "yes"}:
        return True
    if text in {"false", "f", "0", "no"}:
        return False
    return None


def _timestamptz(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None

    text = str(v).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _load_rows(cur: RealDictCursor, batch_size: int, last_comp_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            comp_id,
            raw_payload
        FROM propelio_comps
        WHERE comp_id > %s
          AND raw_payload IS NOT NULL
          AND (
            address_city IS NULL
            OR address_zip IS NULL
            OR address_subdivision IS NULL
            OR school_district IS NULL
            OR elementary_school IS NULL
            OR middle_school IS NULL
            OR high_school IS NULL
            OR stories IS NULL
            OR pool IS NULL
            OR unit_count IS NULL
            OR listing_timestamp IS NULL
            OR status_timestamp IS NULL
            OR photo_timestamp IS NULL
          )
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
        updated = 0
        skipped = 0
        batch_num = 0
        last_comp_id = 0

        while True:
            with conn.cursor(cursor_factory=RealDictCursor) as read_cur:
                rows = _load_rows(read_cur, batch_size, last_comp_id)
            if not rows:
                break

            last_comp_id = int(rows[-1]["comp_id"])
            batch_num += 1
            processed += len(rows)

            with conn.cursor() as write_cur:
                for row in rows:
                    comp_id = int(row["comp_id"])
                    raw = row.get("raw_payload")
                    if not isinstance(raw, dict):
                        skipped += 1
                        continue

                    extracted = {
                        "address_city": _txt(raw.get("address_city")),
                        "address_zip": _txt(raw.get("address_zip")),
                        "address_subdivision": _txt(raw.get("address_subdivision")),
                        "school_district": _txt(raw.get("school_district")),
                        "elementary_school": _txt(raw.get("elementary_school")),
                        "middle_school": _txt(_pick(raw.get("middle_school"), raw.get("junior_high_school"), raw.get("intermediate_school"))),
                        "high_school": _txt(_pick(raw.get("high_school"), raw.get("senior_high_school"))),
                        "stories": _int(raw.get("stories")),
                        "pool": _bool(raw.get("pool")),
                        "unit_count": _int(raw.get("unit_count")),
                        "listing_timestamp": _timestamptz(raw.get("listing_timestamp")),
                        "status_timestamp": _timestamptz(raw.get("status_timestamp")),
                        "photo_timestamp": _timestamptz(raw.get("photo_timestamp")),
                    }

                    if all(value is None for value in extracted.values()):
                        skipped += 1
                        continue

                    write_cur.execute(
                        """
                        UPDATE propelio_comps
                        SET
                            address_city = COALESCE(address_city, %s),
                            address_zip = COALESCE(address_zip, %s),
                            address_subdivision = COALESCE(address_subdivision, %s),
                            school_district = COALESCE(school_district, %s),
                            elementary_school = COALESCE(elementary_school, %s),
                            middle_school = COALESCE(middle_school, %s),
                            high_school = COALESCE(high_school, %s),
                            stories = COALESCE(stories, %s),
                            pool = COALESCE(pool, %s),
                            unit_count = COALESCE(unit_count, %s),
                            listing_timestamp = COALESCE(listing_timestamp, %s),
                            status_timestamp = COALESCE(status_timestamp, %s),
                            photo_timestamp = COALESCE(photo_timestamp, %s)
                        WHERE comp_id = %s
                        """,
                        (
                            extracted["address_city"],
                            extracted["address_zip"],
                            extracted["address_subdivision"],
                            extracted["school_district"],
                            extracted["elementary_school"],
                            extracted["middle_school"],
                            extracted["high_school"],
                            extracted["stories"],
                            extracted["pool"],
                            extracted["unit_count"],
                            extracted["listing_timestamp"],
                            extracted["status_timestamp"],
                            extracted["photo_timestamp"],
                            comp_id,
                        ),
                    )
                    updated += int(write_cur.rowcount or 0)

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            print(
                f"[extra-backfill] batch={batch_num} processed={processed} "
                f"updated={updated} skipped={skipped}",
                flush=True,
            )

        if dry_run:
            print("[extra-backfill] dry-run complete (all writes rolled back)", flush=True)
        else:
            print("[extra-backfill] complete", flush=True)
        print(
            f"[extra-backfill] final processed={processed} updated={updated} skipped={skipped}",
            flush=True,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill typed Propelio columns from raw_payload")
    parser.add_argument("--dry-run", action="store_true", help="Execute all statements but roll back every batch")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Rows per batch commit (default: 200)")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    run_backfill(dry_run=bool(args.dry_run), batch_size=max(1, int(args.batch_size)))


if __name__ == "__main__":
    main()
