# AI module — server-authoritative gating (§A.4 + §A.3.5).
#
# The client's comp_keys list is NEVER trusted to pre-filter. Every exclusion
# reason here is re-derived from the DB: permit_avm, scope (drawn-area polygon),
# geometry presence, and remarks length. Nothing is ever silently dropped —
# every excluded comp comes back named, with a reason (§A.3.5's WYSIWYG rule).
from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import RealDictCursor

from api.ai.config import AI_ENFORCE_PERMIT_AVM, AI_MIN_REMARKS_CHARS
from api.config import get_session_conn, release_session_conn


def _close_ring(coords: list[list[float]]) -> list[list[float]]:
    # PostGIS requires the first coordinate repeated at the end of the ring.
    # saved_areas.polygon is stored NOT closed (mirrors
    # api/propelio/archive.py:load_comps_by_polygon, the proven pattern for
    # this exact bare-array -> GeoJSON problem).
    ring = list(coords)
    if ring and ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return ring


def _area_polygon_geojson(cur, area_id: str) -> str | None:
    cur.execute("SELECT polygon FROM saved_areas WHERE area_id = %s", (area_id,))
    row = cur.fetchone()
    if row is None:
        return None
    polygon = row["polygon"]
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    ring = _close_ring(polygon)
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def fetch_permitted_comps(area_id: str, comp_keys: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Resolve comp_address_key -> comp_id and split into (permitted, excluded).

    Returns:
      permitted: [{comp_id, comp_address_key, address, price, remarks}]
      excluded:  [{comp_id|None, comp_address_key, address, price, reason}]
                 (price carried through: an excluded comp is still a comp, and it
                  still renders in the user's brief — it just has no condition tag.)

    Exclusion reasons (exact strings — the card renders them):
      "not_found"           -> comp_address_key resolved to no row
      "no_geometry"         -> c.geom IS NULL  (cannot prove it's in-area -> fail closed)
      "outside_drawn_area"  -> comp geom not inside saved_areas.polygon  (§A.3.5 — KK scope rule)
      "no_avm_permission"   -> raw_payload->>'permit_avm' IS DISTINCT FROM 'true'   (23.6% of corpus)
      "no_remarks"          -> remarks NULL or length < AI_MIN_REMARKS_CHARS

    area_id is used to re-derive the drawn-area polygon (the scope gate) — it is
    not a filter on comp identity; comp_address_key resolution is global.
    """
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            poly_geojson = _area_polygon_geojson(cur, area_id)

            if poly_geojson:
                cur.execute(
                    """
                    SELECT comp_address_key, comp_id, address, price, remarks,
                           raw_payload->>'permit_avm' AS permit_avm,
                           (geom IS NULL) AS no_geom,
                           COALESCE(
                               ST_Contains(
                                   ST_SetSRID(ST_GeomFromGeoJSON(%(poly)s), 4326),
                                   geom
                               ),
                               false
                           ) AS in_area
                    FROM propelio_comps
                    WHERE comp_address_key = ANY(%(keys)s)
                    """,
                    {"poly": poly_geojson, "keys": comp_keys},
                )
            else:
                # The area has no usable polygon on record — fail closed. Every
                # comp is treated as unproven-in-area rather than silently
                # skipping the scope gate.
                cur.execute(
                    """
                    SELECT comp_address_key, comp_id, address, price, remarks,
                           raw_payload->>'permit_avm' AS permit_avm,
                           (geom IS NULL) AS no_geom,
                           false AS in_area
                    FROM propelio_comps
                    WHERE comp_address_key = ANY(%(keys)s)
                    """,
                    {"keys": comp_keys},
                )
            rows = cur.fetchall()
    finally:
        release_session_conn(conn)

    by_key = {row["comp_address_key"]: row for row in rows}

    permitted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for key in comp_keys:
        row = by_key.get(key)
        if row is None:
            excluded.append({"comp_id": None, "comp_address_key": key, "address": None, "price": None, "reason": "not_found"})
            continue

        comp_id = int(row["comp_id"])
        address = row["address"]
        price = float(row["price"]) if row["price"] is not None else None

        if row["no_geom"]:
            excluded.append({"comp_id": comp_id, "comp_address_key": key, "address": address, "price": price, "reason": "no_geometry"})
            continue
        if not row["in_area"]:
            excluded.append({"comp_id": comp_id, "comp_address_key": key, "address": address, "price": price, "reason": "outside_drawn_area"})
            continue
        if AI_ENFORCE_PERMIT_AVM and row["permit_avm"] != "true":
            excluded.append({"comp_id": comp_id, "comp_address_key": key, "address": address, "price": price, "reason": "no_avm_permission"})
            continue

        remarks = row["remarks"] or ""
        if len(remarks) < AI_MIN_REMARKS_CHARS:
            excluded.append({"comp_id": comp_id, "comp_address_key": key, "address": address, "price": price, "reason": "no_remarks"})
            continue

        permitted.append({
            "comp_id": comp_id,
            "comp_address_key": key,
            "address": address,
            "price": price,
            "remarks": remarks,
        })

    return permitted, excluded
