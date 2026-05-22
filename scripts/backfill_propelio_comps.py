# scripts/backfill_propelio_comps.py
#
# Role: One-shot backfill from workspace-scoped Propelio archive rows into
#       global comps + workspace ratings tables (Phase 2 Chunk 1).
#       Idempotent via ON CONFLICT DO UPDATE and RETURNING comp_id.
#
# Connects to:
#   api/config.py            - imports session DB connection helpers
#   session DB tables        - reads propelio_comp_archive
#                              writes propelio_comps and comp_ratings

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

from psycopg2.extras import Json, RealDictCursor

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.propelio.parcel_match import match_comps_to_parcels


BATCH_SIZE = 500
VALID_RATINGS = {"good", "bad"}


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_rating(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in VALID_RATINGS:
        return text
    return None


def _parse_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _extract_comp_fields(row: dict[str, Any]) -> dict[str, Any] | None:
    comp_data = row.get("comp_data")
    if not isinstance(comp_data, dict):
        return None

    extra = comp_data.get("extra") if isinstance(comp_data.get("extra"), dict) else {}
    raw = extra.get("raw") if isinstance(extra.get("raw"), dict) else {}

    comp_key = str(row.get("comp_address_key") or "").strip()
    address = str(comp_data.get("address") or "").strip()
    if not comp_key or not address:
        return None

    lat = _to_float(extra.get("lat"))
    lng = _to_float(extra.get("lon") if extra.get("lon") is not None else extra.get("lng"))

    sold_or_close = _parse_iso_date(extra.get("close_date"))

    return {
        "comp_address_key": comp_key,
        "address": address,
        "neighborhood": str(comp_data.get("neighborhood") or "").strip() or None,
        "lat": lat,
        "lng": lng,
        "status": str(comp_data.get("status") or "").strip().lower() or None,
        "price": _to_float(comp_data.get("price")),
        "sold_date": sold_or_close,
        "close_date": sold_or_close,
        "dom": _to_int(extra.get("dom")),
        "beds": _to_float(extra.get("beds")),
        "baths": _to_float(extra.get("baths")),
        "baths_full": _to_int(extra.get("baths_full")),
        "baths_half": _to_int(extra.get("baths_half")),
        "garage": _to_int(extra.get("garage")),
        "sqft": _to_float(comp_data.get("sqft")),
        "lot_size": _to_float(comp_data.get("lot_size")),
        "year_built": _to_int(comp_data.get("year_built")),
        "mls": str(extra.get("mls") or "").strip() or None,
        "property_type": str(extra.get("property_type") or "").strip() or None,
        "property_category": str(extra.get("property_category") or "").strip() or None,
        "list_price": _to_float(extra.get("list_price")),
        "remarks": str(extra.get("remarks") or raw.get("remarks") or "").strip() or None,
        "listing_agent_name": str(raw.get("listing_agent_name") or "").strip() or None,
        "listing_agent_phone": str(raw.get("listing_agent_phone") or "").strip() or None,
        "listing_agent_email": str(raw.get("listing_agent_email") or "").strip() or None,
        "listing_office_name": str(raw.get("listing_office_name") or "").strip() or None,
        "listing_office_phone": str(raw.get("listing_office_phone") or "").strip() or None,
        "buyer_agent_name": str(raw.get("buyer_agent_name") or "").strip() or None,
        "buyer_agent_phone": str(raw.get("buyer_agent_phone") or "").strip() or None,
        "buyer_agent_email": str(raw.get("buyer_agent_email") or "").strip() or None,
        "buyer_office_name": str(raw.get("buyer_office_name") or "").strip() or None,
        "buyer_office_phone": str(raw.get("buyer_office_phone") or "").strip() or None,
        "photo_count": _to_int(raw.get("photo_count")),
        "photos": raw.get("photos") if isinstance(raw.get("photos"), list) else None,
        "parcel_account_num": str(row.get("parcel_account_num") or "").strip() or None,
        # 2026-05-22 fix: parcel_county is no longer hardcoded None. The
        # run_backfill loop now calls match_comps_to_parcels on each batch's
        # comp_data dicts BEFORE this function reads them, mutating them in
        # place with parcel_county set. See docs/PROPELIO_COMPS_COUNTY_BACKFILL_SPEC.md.
        "parcel_county": str(comp_data.get("parcel_county") or "").strip() or None,
        "parcel_geom": row.get("parcel_geom") if isinstance(row.get("parcel_geom"), (dict, list)) else None,
        "parsed_payload": comp_data,
        "raw_payload": raw if raw else None,
        "first_seen_source": "backfill",
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


def _upsert_comp(cur, fields: dict[str, Any]) -> int:
    cur.execute(
        """
        INSERT INTO propelio_comps (
            comp_address_key,
            address,
            neighborhood,
            lat,
            lng,
            geom,
            status,
            last_status,
            price,
            last_price,
            sold_date,
            close_date,
            dom,
            beds,
            baths,
            baths_full,
            baths_half,
            garage,
            sqft,
            lot_size,
            year_built,
            mls,
            property_type,
            property_category,
            list_price,
            remarks,
            listing_agent_name,
            listing_agent_phone,
            listing_agent_email,
            listing_office_name,
            listing_office_phone,
            buyer_agent_name,
            buyer_agent_phone,
            buyer_agent_email,
            buyer_office_name,
            buyer_office_phone,
            photo_count,
            photos,
            parcel_account_num,
            parcel_county,
            parcel_geom,
            parsed_payload,
            raw_payload,
            first_seen_source,
            first_seen_at,
            last_seen_at
        )
        VALUES (
            %(comp_address_key)s,
            %(address)s,
            %(neighborhood)s,
            %(lat)s,
            %(lng)s,
            CASE
                WHEN %(lat)s IS NOT NULL AND %(lng)s IS NOT NULL
                THEN ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)
                ELSE NULL
            END,
            %(status)s,
            %(status)s,
            %(price)s,
            %(price)s,
            %(sold_date)s,
            %(close_date)s,
            %(dom)s,
            %(beds)s,
            %(baths)s,
            %(baths_full)s,
            %(baths_half)s,
            %(garage)s,
            %(sqft)s,
            %(lot_size)s,
            %(year_built)s,
            %(mls)s,
            %(property_type)s,
            %(property_category)s,
            %(list_price)s,
            %(remarks)s,
            %(listing_agent_name)s,
            %(listing_agent_phone)s,
            %(listing_agent_email)s,
            %(listing_office_name)s,
            %(listing_office_phone)s,
            %(buyer_agent_name)s,
            %(buyer_agent_phone)s,
            %(buyer_agent_email)s,
            %(buyer_office_name)s,
            %(buyer_office_phone)s,
            %(photo_count)s,
            %(photos)s,
            %(parcel_account_num)s,
            %(parcel_county)s,
            %(parcel_geom)s,
            %(parsed_payload)s,
            %(raw_payload)s,
            %(first_seen_source)s,
            %(first_seen_at)s,
            %(last_seen_at)s
        )
        ON CONFLICT (comp_address_key) DO UPDATE SET
            address = EXCLUDED.address,
            neighborhood = EXCLUDED.neighborhood,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            geom = CASE
                WHEN EXCLUDED.lat IS NOT NULL AND EXCLUDED.lng IS NOT NULL
                THEN ST_SetSRID(ST_MakePoint(EXCLUDED.lng, EXCLUDED.lat), 4326)
                ELSE propelio_comps.geom
            END,
            last_status = propelio_comps.status,
            status = EXCLUDED.status,
            last_price = propelio_comps.price,
            price = EXCLUDED.price,
            sold_date = EXCLUDED.sold_date,
            close_date = EXCLUDED.close_date,
            dom = EXCLUDED.dom,
            beds = EXCLUDED.beds,
            baths = EXCLUDED.baths,
            baths_full = EXCLUDED.baths_full,
            baths_half = EXCLUDED.baths_half,
            garage = EXCLUDED.garage,
            sqft = EXCLUDED.sqft,
            lot_size = EXCLUDED.lot_size,
            year_built = EXCLUDED.year_built,
            mls = EXCLUDED.mls,
            property_type = EXCLUDED.property_type,
            property_category = EXCLUDED.property_category,
            list_price = EXCLUDED.list_price,
            remarks = EXCLUDED.remarks,
            listing_agent_name = EXCLUDED.listing_agent_name,
            listing_agent_phone = EXCLUDED.listing_agent_phone,
            listing_agent_email = EXCLUDED.listing_agent_email,
            listing_office_name = EXCLUDED.listing_office_name,
            listing_office_phone = EXCLUDED.listing_office_phone,
            buyer_agent_name = EXCLUDED.buyer_agent_name,
            buyer_agent_phone = EXCLUDED.buyer_agent_phone,
            buyer_agent_email = EXCLUDED.buyer_agent_email,
            buyer_office_name = EXCLUDED.buyer_office_name,
            buyer_office_phone = EXCLUDED.buyer_office_phone,
            photo_count = EXCLUDED.photo_count,
            photos = EXCLUDED.photos,
            parcel_account_num = EXCLUDED.parcel_account_num,
            parcel_county = EXCLUDED.parcel_county,
            parcel_geom = EXCLUDED.parcel_geom,
            parsed_payload = EXCLUDED.parsed_payload,
            raw_payload = COALESCE(EXCLUDED.raw_payload, propelio_comps.raw_payload),
            last_seen_at = GREATEST(propelio_comps.last_seen_at, EXCLUDED.last_seen_at)
        RETURNING comp_id
        """,
        {
            **fields,
            "parsed_payload": Json(fields["parsed_payload"]),
            "raw_payload": Json(fields["raw_payload"]) if fields["raw_payload"] is not None else None,
            "parcel_geom": Json(fields["parcel_geom"]) if fields["parcel_geom"] is not None else None,
            "photos": Json(fields["photos"]) if fields["photos"] is not None else None,
        },
    )
    row = cur.fetchone()
    return int(row[0])


def run_backfill(*, dry_run: bool = False, batch_size: int = BATCH_SIZE) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as read_cur:
            read_cur.execute(
                """
                SELECT
                    id,
                    saved_area_id,
                    comp_address_key,
                    comp_data,
                    parcel_geom,
                    parcel_account_num,
                    user_rating,
                    rating_at,
                    first_seen_at,
                    last_seen_at
                FROM propelio_comp_archive
                ORDER BY last_seen_at ASC NULLS FIRST, id ASC
                """
            )

            processed = 0
            skipped = 0
            ratings_written = 0
            while True:
                rows = read_cur.fetchmany(int(batch_size))
                if not rows:
                    break

                # 2026-05-22 fix: enrich each row's comp_data dict with
                # parcel_county / parcel_account_num / parcel_geom BEFORE
                # _extract_comp_fields reads them. The matcher mutates each
                # comp_data dict in place, so _extract_comp_fields picks
                # up the populated parcel_county at line 134.
                # See docs/PROPELIO_COMPS_COUNTY_BACKFILL_SPEC.md.
                comp_dicts = [
                    r.get("comp_data") for r in rows
                    if isinstance(r.get("comp_data"), dict)
                ]
                if comp_dicts:
                    try:
                        match_comps_to_parcels(comp_dicts)
                    except Exception as e:
                        # Non-fatal: matcher failure leaves parcel_county None,
                        # which is the original (broken) behavior. Log and proceed
                        # so a transient DB hiccup doesn't kill the whole backfill.
                        print(f"  WARN: match_comps_to_parcels failed: {e}")

                with conn.cursor() as write_cur:
                    for row in rows:
                        fields = _extract_comp_fields(row)
                        if fields is None:
                            skipped += 1
                            continue

                        comp_id = _upsert_comp(write_cur, fields)
                        processed += 1

                        rating = _normalize_rating(row.get("user_rating"))
                        workspace_id = str(row.get("saved_area_id") or "").strip()
                        if rating and workspace_id:
                            write_cur.execute(
                                """
                                INSERT INTO comp_ratings (
                                    workspace_id,
                                    comp_id,
                                    rating,
                                    rated_at
                                )
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (workspace_id, comp_id)
                                DO UPDATE SET
                                    rating = EXCLUDED.rating,
                                    rated_at = EXCLUDED.rated_at
                                """,
                                (workspace_id, comp_id, rating, row.get("rating_at")),
                            )
                            ratings_written += 1

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                print(
                    f"[backfill] processed={processed} skipped={skipped} "
                    f"ratings_upserted={ratings_written}",
                    flush=True,
                )

        if dry_run:
            print("[backfill] dry-run complete (all writes rolled back)", flush=True)
        else:
            print("[backfill] complete", flush=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill propelio_comps + comp_ratings from propelio_comp_archive")
    parser.add_argument("--dry-run", action="store_true", help="Execute all statements but roll back every batch")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Number of archive rows per commit (default: 500)")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    run_backfill(dry_run=bool(args.dry_run), batch_size=max(1, int(args.batch_size)))


if __name__ == "__main__":
    main()
