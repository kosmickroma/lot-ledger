# api/geo.py
#
# Geometry helper functions for polygon bounds and point-in-polygon checks.
# Keeps spatial math isolated so DCAD query/classification logic stays focused.
#
# Connects to:
#   api/dcad.py  - used for bounding-box derivation and exact polygon filtering
#   api/main.py  - polygon_bbox imported directly for Redfin grid bounds

from __future__ import annotations

import math
from typing import Any, Iterable


def polygon_bbox(coords: Iterable[Iterable[float]]) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lng, max_lat, max_lng) for a polygon of [lng, lat] pairs."""
    points = list(coords)
    if len(points) < 3:
        raise ValueError("Polygon must contain at least three points")

    lngs = [float(point[0]) for point in points]
    lats = [float(point[1]) for point in points]
    return min(lats), min(lngs), max(lats), max(lngs)


def polygon_centroid(coords: Iterable[Iterable[float]]) -> tuple[float, float]:
    """Return (lat, lng) centroid for polygon coords in [lng, lat] order."""
    points = [(float(point[0]), float(point[1])) for point in coords]
    if len(points) < 3:
        raise ValueError("Polygon must contain at least three points")

    if points[0] != points[-1]:
        points.append(points[0])

    twice_area = 0.0
    centroid_x = 0.0
    centroid_y = 0.0
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        cross = x1 * y2 - x2 * y1
        twice_area += cross
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross

    if abs(twice_area) < 1e-12:
        lngs = [point[0] for point in points[:-1]]
        lats = [point[1] for point in points[:-1]]
        return sum(lats) / len(lats), sum(lngs) / len(lngs)

    area = twice_area * 0.5
    lng = centroid_x / (6.0 * area)
    lat = centroid_y / (6.0 * area)
    return lat, lng


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in miles between two lat/lng points."""
    radius_miles = 3958.7613
    lat1_rad = math.radians(float(lat1))
    lng1_rad = math.radians(float(lng1))
    lat2_rad = math.radians(float(lat2))
    lng2_rad = math.radians(float(lng2))

    dlat = lat2_rad - lat1_rad
    dlng = lng2_rad - lng1_rad
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng / 2.0) ** 2
    return 2.0 * radius_miles * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lng: float, polygon_coords: Iterable[Iterable[float]]) -> bool:
    """Ray-casting point-in-polygon test for polygon coords in [lng, lat] order."""
    points = [(float(point[0]), float(point[1])) for point in polygon_coords]
    if len(points) < 3:
        return False

    # Ensure the polygon is closed for consistent edge traversal.
    if points[0] != points[-1]:
        points.append(points[0])

    inside = False
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]

        intersects = (y1 > lat) != (y2 > lat)
        if not intersects:
            continue

        # Compute x coordinate where the edge intersects the horizontal ray at `lat`.
        denominator = y2 - y1
        if denominator == 0:
            continue
        x_intersect = (x2 - x1) * (lat - y1) / denominator + x1

        if lng < x_intersect:
            inside = not inside

    return inside


def build_polygon_geojson_feature_collection(
    polygon: list[list[float]] | None,
) -> dict[str, Any] | None:
    """Build a single-feature FeatureCollection from a [[lng, lat], ...] ring."""
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None

    cleaned: list[list[float]] = []
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        try:
            lng, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        cleaned.append([lng, lat])

    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [cleaned]},
                "properties": {},
            }
        ],
    }
