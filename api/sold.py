# api/sold.py
#
# Role: DB-backed sold comps query helper for draw mode overlays.
#       Returns sold listing points from redfin_sold within a polygon.
#
# Connects to:
#   api/main.py   - calls query_sold_parcels() from /api/analyze
#   api/config.py - imports get_conn() and release_conn()

from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import RealDictCursor

from api.config import get_conn, release_conn


def query_sold_parcels(polygon: list[list[float]]) -> list[dict[str, Any]]:
    """Return sold listing points within the provided polygon.

    Initial rollout includes Dallas + Tarrant.
    """
    if len(polygon) < 3:
        return []

    ring = [list(pt) for pt in polygon]
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    polygon_geojson = json.dumps({"type": "Polygon", "coordinates": [ring]})

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
                    sqft,
                    beds,
                    baths,
                    yr_built,
                    price_per_sqft,
                    city,
                    listing_url,
                    lat,
                    lng
                FROM redfin_sold
                                WHERE source_county IN ('dallas', 'tarrant')
                                    AND geom IS NOT NULL
                                    AND ST_Within(
                                        geom,
                                        ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
                                    )
                ORDER BY sold_date DESC NULLS LAST
                LIMIT 10000
                """,
                                (polygon_geojson,),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        release_conn(conn)
