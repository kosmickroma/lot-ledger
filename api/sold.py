# api/sold.py
#
# Role: DB-backed sold comps query helper for draw mode overlays.
#       Returns sold listing points from redfin_sold within a bbox.
#
# Connects to:
#   api/main.py   - calls query_sold_parcels() from /api/analyze
#   api/config.py - imports get_conn() and release_conn()

from __future__ import annotations

from typing import Any

from psycopg2.extras import RealDictCursor

from api.config import get_conn, release_conn


def query_sold_parcels(min_lng: float, min_lat: float, max_lng: float, max_lat: float) -> list[dict[str, Any]]:
    """Return Dallas sold listing points within the provided bbox.

    This is intentionally Dallas-only for the first rollout.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    address,
                    sold_price,
                    sold_date,
                    dom,
                    lot_sqft,
                    listing_url,
                    lat,
                    lng
                FROM redfin_sold
                WHERE source_county = 'dallas'
                  AND lat IS NOT NULL
                  AND lng IS NOT NULL
                  AND lng BETWEEN %s AND %s
                  AND lat BETWEEN %s AND %s
                ORDER BY sold_date DESC NULLS LAST
                LIMIT 10000
                """,
                (min_lng, max_lng, min_lat, max_lat),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        release_conn(conn)
