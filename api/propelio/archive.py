# api/propelio/archive.py
#
# Session-DB archive for workspace-scoped Propelio comps.
# Stores append-only comp history per saved area and preserves user ratings.
#
# Connects to:
#   api/config.py          - session DB connection helpers
#   api/redfin.py          - normalize_addr_key key canonicalization
#   api/propelio/routes.py - merge + load helpers for by-polygon/refresh endpoints

from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn
from api.redfin import normalize_addr_key


def _comp_address_key(comp: dict[str, Any]) -> str:
    address = str(comp.get("address") or "").strip()
    street_only = address.split(",", 1)[0].strip() if address else ""
    key = normalize_addr_key(street_only or address).strip().upper()
    if key:
        return key

    extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
    mls = str(extra.get("mls") or "").strip().upper()
    if mls:
        return f"MLS:{mls}"

    lat_raw = extra.get("lat")
    lng_raw = extra.get("lon", extra.get("lng"))
    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except (TypeError, ValueError):
        return ""
    return f"LL:{lat:.6f},{lng:.6f}"


def ensure_tables() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS propelio_comp_archive (
                    id                 SERIAL PRIMARY KEY,
                    saved_area_id      TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    comp_address_key   TEXT NOT NULL,
                    comp_mls           TEXT,
                    comp_data          JSONB NOT NULL,
                    parcel_geom        JSONB,
                    parcel_account_num TEXT,
                    status             TEXT,
                    last_status        TEXT,
                    last_price         NUMERIC,
                    user_rating        TEXT,
                    rating_at          TIMESTAMPTZ,
                    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (saved_area_id, comp_address_key)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS propelio_comp_archive_area
                    ON propelio_comp_archive (saved_area_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS propelio_comp_archive_rating
                    ON propelio_comp_archive (saved_area_id, user_rating)
                    WHERE user_rating IS NOT NULL
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def merge_comps_into_archive(saved_area_id: str, comps: list[dict[str, Any]]) -> dict[str, int]:
    normalized_area_id = str(saved_area_id or "").strip()
    if not normalized_area_id:
        raise ValueError("saved_area_id is required")

    inserted = 0
    updated = 0

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            for comp in comps or []:
                if not isinstance(comp, dict):
                    continue

                comp_key = _comp_address_key(comp)
                if not comp_key:
                    continue

                extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
                comp_mls = str(extra.get("mls") or "").strip() or None
                parcel_geom = comp.get("parcel_geom")
                parcel_account_num = str(comp.get("parcel_account_num") or "").strip() or None
                status = str(comp.get("status") or "").strip().lower() or None

                price_raw = comp.get("price")
                try:
                    last_price = float(price_raw) if price_raw is not None else None
                except (TypeError, ValueError):
                    last_price = None

                cur.execute(
                    """
                    INSERT INTO propelio_comp_archive (
                        saved_area_id,
                        comp_address_key,
                        comp_mls,
                        comp_data,
                        parcel_geom,
                        parcel_account_num,
                        status,
                        last_status,
                        last_price
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (saved_area_id, comp_address_key)
                    DO UPDATE SET
                        comp_mls = EXCLUDED.comp_mls,
                        comp_data = EXCLUDED.comp_data,
                        parcel_geom = EXCLUDED.parcel_geom,
                        parcel_account_num = EXCLUDED.parcel_account_num,
                        status = EXCLUDED.status,
                        last_status = EXCLUDED.last_status,
                        last_price = EXCLUDED.last_price,
                        last_seen_at = NOW()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        normalized_area_id,
                        comp_key,
                        comp_mls,
                        Json(comp),
                        Json(parcel_geom) if isinstance(parcel_geom, (dict, list)) else None,
                        parcel_account_num,
                        status,
                        status,
                        last_price,
                    ),
                )
                row = cur.fetchone()
                if row and bool(row[0]):
                    inserted += 1
                else:
                    updated += 1

            cur.execute(
                """
                SELECT COUNT(*)
                FROM propelio_comp_archive
                WHERE saved_area_id = %s
                """,
                (normalized_area_id,),
            )
            total = int(cur.fetchone()[0] or 0)

        conn.commit()
        return {"inserted": inserted, "updated": updated, "total": total}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def load_archived_comps(saved_area_id: str) -> list[dict[str, Any]]:
    normalized_area_id = str(saved_area_id or "").strip()
    if not normalized_area_id:
        return []

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    comp_data,
                    parcel_geom,
                    parcel_account_num,
                    status,
                    user_rating
                FROM propelio_comp_archive
                WHERE saved_area_id = %s
                ORDER BY last_seen_at DESC, id DESC
                """,
                (normalized_area_id,),
            )
            rows = cur.fetchall() or []

        hydrated: list[dict[str, Any]] = []
        for comp_data, parcel_geom, parcel_account_num, status, user_rating in rows:
            comp = dict(comp_data) if isinstance(comp_data, dict) else {}
            comp["parcel_geom"] = parcel_geom if isinstance(parcel_geom, (dict, list)) else None
            comp["parcel_account_num"] = str(parcel_account_num or "").strip() or None
            if status and not comp.get("status"):
                comp["status"] = status
            comp["user_rating"] = str(user_rating).strip().lower() if user_rating is not None else None
            hydrated.append(comp)

        return hydrated
    finally:
        release_session_conn(conn)


ensure_tables()
