# api/geo.py
#
# Geometry helper functions for polygon bounds and point-in-polygon checks.
# Keeps spatial math isolated so DCAD query/classification logic stays focused.
#
# Connects to:
#   api/dcad.py  - used for bounding-box derivation and exact polygon filtering
#   api/main.py  - polygon_bbox imported directly for Redfin grid bounds

from __future__ import annotations

from typing import Iterable


def polygon_bbox(coords: Iterable[Iterable[float]]) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lng, max_lat, max_lng) for a polygon of [lng, lat] pairs."""
    points = list(coords)
    if len(points) < 3:
        raise ValueError("Polygon must contain at least three points")

    lngs = [float(point[0]) for point in points]
    lats = [float(point[1]) for point in points]
    return min(lats), min(lngs), max(lats), max(lngs)


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
