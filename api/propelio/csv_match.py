# api/propelio/csv_match.py
#
# Role: Propelio-sourced comp matching for CSV export.
#       Queries propelio_comps for sold listings within workspace polygon,
#       performs spatial nearest-neighbor join to parcels, and returns
#       normalized rows ready for the CSV writer.
#
# Connects to:
#   api/main.py        - CSV export calls query_propelio_sold_in_polygon()
#   api.config         - get_session_conn() / release_session_conn()
#   api.main           - _safe_float()

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from api.main import _safe_float

logger = logging.getLogger(__name__)


def query_propelio_sold_in_polygon(
    session_conn,
    parcel_input: list[tuple[str, str, float, float]],
    polygon: list[list[float]] | None,
) -> dict[tuple[str, str], dict]:
    """Query propelio_comps for sold listings within a polygon.

    Returns dict keyed by (county, account_num) tuple. Value is a normalized
    row dict ready for the CSV writer. Rows with no in-polygon match are
    excluded from the result.

    Args:
        session_conn: Open psycopg2 connection to the session DB.
        parcel_input: List of (account_num, county, lat, lng) tuples. The
                      parcels for this workspace. May be empty.
        polygon: Raw polygon from cached_jobs.polygon — a list of [lng, lat]
                 pairs forming a ring, or [] for empty, or None for NULL.
                 Not a GeoJSON string; converted internally.

    Returns:
        Dict keyed by (county, account_num). Returns empty dict if:
        - polygon is None, empty, malformed, or has < 3 points
        - parcel_input is empty
        - SQL fails (logged at warning level, never raises)
    """

    # ─── Validate polygon (§3.6 polygon-validity guard) ───
    polygon_validity = _validate_polygon(polygon)
    if polygon_validity is not None:
        reason, parcel_count = polygon_validity
        logger.info(
            "propelio_sold: skipping polygon-bounded match (polygon=%s, parcel_count=%d)",
            reason,
            parcel_count,
        )
        return {}

    # ─── Empty parcel input ───
    if not parcel_input:
        logger.info("propelio_sold: skipping polygon-bounded match (polygon=empty_input, parcel_count=0)")
        return {}

    # ─── Convert polygon to GeoJSON with closed ring ───
    ring = list(polygon)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    polygon_geojson = json.dumps({"type": "Polygon", "coordinates": [ring]})

    # ─── Execute lateral query ───
    result: dict[tuple[str, str], dict] = {}
    try:
        with session_conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build VALUES clause with named parameters for parcels
            values_strs = ", ".join(
                f"(%(p{i}_0)s, %(p{i}_1)s, %(p{i}_2)s, %(p{i}_3)s)"
                for i in range(len(parcel_input))
            )
            
            # Build parameters dict: parcel tuples + polygon
            params: dict[str, Any] = {"polygon_geojson": polygon_geojson}
            for i, (accnum, county, lat, lng) in enumerate(parcel_input):
                params[f"p{i}_0"] = accnum
                params[f"p{i}_1"] = county
                params[f"p{i}_2"] = lat
                params[f"p{i}_3"] = lng
            
            # Construct final query
            sql_query = f"""
            WITH parcels_for_job(account_num, county, parcel_lat, parcel_lng) AS (
                VALUES {values_strs}
            )
            SELECT
                p.account_num,
                p.county,
                p.parcel_lat,
                p.parcel_lng,
                c.price,
                c.sold_date,
                c.year_built,
                c.lot_size,
                c.sqft,
                c.beds,
                c.baths,
                c.dom,
                ST_Distance(
                    ST_SetSRID(ST_MakePoint(p.parcel_lng, p.parcel_lat), 4326)::geography,
                    c.geom::geography
                ) AS distance_meters
            FROM parcels_for_job p
            LEFT JOIN LATERAL (
                SELECT *
                FROM propelio_comps pc
                WHERE pc.status = 'sold'
                  AND ST_Within(pc.geom, ST_GeomFromGeoJSON(%(polygon_geojson)s))
                ORDER BY pc.geom <-> ST_SetSRID(ST_MakePoint(p.parcel_lng, p.parcel_lat), 4326)
                LIMIT 1
            ) c ON true;
            """
            
            cur.execute(sql_query, params)
            
            for row in cur.fetchall():
                # Skip rows where the lateral side is NULL (no in-polygon match)
                if row["price"] is None:
                    continue
                
                county = row["county"]
                account_num = row["account_num"]
                distance_meters = row["distance_meters"]
                
                normalized = _normalize_propelio_row(row, distance_meters)
                result[(county, account_num)] = normalized
    except Exception as exc:
        logger.warning(
            "propelio_sold: query failed: %s: %s (parcel_count=%d)",
            type(exc).__name__,
            exc,
            len(parcel_input),
        )
        return {}

    return result


def _validate_polygon(polygon: Any) -> tuple[str, int] | None:
    """Validate polygon input. Return (reason, parcel_count) if invalid, else None.

    Reasons: none, empty, not_a_list, too_few_points, malformed_vertices.
    parcel_count is 0 here (used for logging context; caller will substitute real count).
    """
    if polygon is None:
        return ("none", 0)
    
    if polygon == []:
        return ("empty", 0)
    
    if not isinstance(polygon, list):
        return ("not_a_list", 0)
    
    if len(polygon) < 3:
        return ("too_few_points", 0)
    
    # Check all elements are 2-element [lng, lat] pairs of floats
    for pt in polygon:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return ("malformed_vertices", 0)
        try:
            float(pt[0])
            float(pt[1])
        except (TypeError, ValueError):
            return ("malformed_vertices", 0)
    
    return None


def _normalize_propelio_row(row: dict, distance_meters: float | None) -> dict:
    """Normalize a propelio_comps row into the shape the CSV writer consumes.

    Args:
        row: Dict with keys matching the SELECT in _LATERAL_SQL.
        distance_meters: Computed distance from parcel centroid to comp.

    Returns:
        Dict with keys: sold_price, sold_date, yr_built, lot_sqft, sqft, beds,
        baths, dom, listing_url, price_per_sqft, distance_feet.
    """
    return {
        "sold_price": row.get("price"),
        "sold_date": row.get("sold_date"),
        "yr_built": row.get("year_built"),
        "lot_sqft": row.get("lot_size"),
        "sqft": row.get("sqft"),
        "beds": row.get("beds"),
        "baths": row.get("baths"),
        "dom": row.get("dom"),
        "listing_url": "",  # v1: hard-coded blank per spec
        "price_per_sqft": _compute_price_per_sqft(row.get("price"), row.get("sqft")),
        "distance_feet": (distance_meters * 3.28084) if distance_meters is not None else None,
    }


def _compute_price_per_sqft(price: Any, sqft: Any) -> float | None:
    """Safely compute price per sqft, reusing _safe_float for coercion.

    Returns None if either input is None, sqft <= 0, or division fails.
    """
    price_safe = _safe_float(price)
    sqft_safe = _safe_float(sqft)
    
    if price_safe is not None and sqft_safe is not None and sqft_safe > 0:
        return price_safe / sqft_safe
    
    return None
