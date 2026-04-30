# scripts/load_hoa.py
#
# One-time / repeatable loader that downloads the City of Dallas HOA boundary
# GeoJSON from the public ArcGIS feature service and upserts all records into
# the hoa_boundaries PostGIS table used for spatial joins at query time.
#
# Run any time the city updates its HOA data:
#   python -m scripts.load_hoa
#
# Source URL (confirmed working as of 2026-04-30):
#   https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/HomeownerAssociations/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson
#
# Connects to:
#   api/config.py  - imports shared database connection helpers
#   PostgreSQL     - writes hoa_boundaries table (created if not exists)

from __future__ import annotations

import json
import urllib.request

from psycopg2.extras import execute_values

from api.config import get_conn, release_conn

HOA_GEOJSON_URL = (
    "https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/"
    "HomeownerAssociations/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson"
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hoa_boundaries (
    objectid    INTEGER PRIMARY KEY,
    asso_name   TEXT,
    asso_web    TEXT,
    asso_type   TEXT,
    status      TEXT,
    geom        GEOMETRY(Geometry, 4326)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS hoa_boundaries_geom_idx
    ON hoa_boundaries USING GIST(geom);
"""

UPSERT_SQL = """
INSERT INTO hoa_boundaries (objectid, asso_name, asso_web, asso_type, status, geom)
VALUES %s
ON CONFLICT (objectid) DO UPDATE SET
    asso_name = EXCLUDED.asso_name,
    asso_web  = EXCLUDED.asso_web,
    asso_type = EXCLUDED.asso_type,
    status    = EXCLUDED.status,
    geom      = EXCLUDED.geom;
"""


def _fetch_geojson() -> dict:
    print(f"Fetching HOA boundaries from Dallas ArcGIS...")
    with urllib.request.urlopen(HOA_GEOJSON_URL, timeout=60) as r:
        return json.load(r)


def _clean(val: object) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text if text else None


def load_hoa() -> None:
    data = _fetch_geojson()
    features = data.get("features", [])
    print(f"  Downloaded {len(features)} HOA boundary features")

    records: list[tuple] = []
    skipped = 0
    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if geom is None:
            skipped += 1
            continue
        objectid = props.get("OBJECTID")
        if objectid is None:
            skipped += 1
            continue
        records.append((
            int(objectid),
            _clean(props.get("ASSO_NAME")),
            _clean(props.get("Asso_WEB")),
            _clean(props.get("Asso_Type")),
            _clean(props.get("Status")),
            json.dumps(geom),
        ))

    if skipped:
        print(f"  Skipped {skipped} features (missing geometry or OBJECTID)")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(CREATE_INDEX_SQL)
            conn.commit()
            print("  Table and index ready")

            execute_values(
                cur,
                UPSERT_SQL,
                [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in records],
                template="(%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))",
            )
            conn.commit()
            print(f"  Upserted {len(records)} HOA boundaries into hoa_boundaries table")
    finally:
        release_conn(conn)


if __name__ == "__main__":
    load_hoa()
    print("Done.")
