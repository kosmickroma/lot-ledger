"""tests/test_flood_zones_loader.py
Role: Inspection guards for the FEMA NFHL ingest loader.

Asserts the loader script:
  - Declares the right COUNTY_FIPS mapping
  - Handles the -9999 BFE sentinel correctly via _normalize_bfe
  - Uses latin-1 encoding for shapefile reading (Collin/Denton .cpg lie)
  - Wraps TRUNCATE + INSERT in one transaction
  - Builds GIST + county + partial-SFHA-T indexes CONCURRENTLY
  - Uses ST_MakeValid + ST_Multi + ST_GeomFromGeoJSON pattern from ZCTA loader
  - Validates the --snapshot CLI arg

Connects to: scripts/ingest_flood_zones.py
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.ingest_flood_zones import (
    BFE_SENTINEL,
    COUNTY_FIPS,
    _normalize_bfe,
)


LOADER_PY = Path(__file__).resolve().parent.parent / "scripts" / "ingest_flood_zones.py"


def _read() -> str:
    return LOADER_PY.read_text()


def test_county_fips_includes_all_4_dfw_counties() -> None:
    assert COUNTY_FIPS == {
        "dallas": "48113",
        "tarrant": "48439",
        "collin": "48085",
        "denton": "48121",
    }


def test_bfe_sentinel_value_locked() -> None:
    """FEMA's documented sentinel for 'no BFE established'. Don't change."""
    assert BFE_SENTINEL == -9999.0


def test_normalize_bfe_maps_sentinel_to_none() -> None:
    assert _normalize_bfe(-9999.0) is None
    assert _normalize_bfe(-9999) is None  # int form, same value


def test_normalize_bfe_preserves_real_values() -> None:
    assert _normalize_bfe(502.3) == 502.3
    assert _normalize_bfe(0.0) == 0.0
    assert _normalize_bfe(420) == 420.0


def test_normalize_bfe_handles_garbage() -> None:
    """Defensive: bad shapefile rows shouldn't crash the loader."""
    assert _normalize_bfe(None) is None
    assert _normalize_bfe("not a number") is None


def test_loader_uses_latin1_encoding() -> None:
    """Collin + Denton .cpg files claim UTF-8 but actual bytes aren't valid
    UTF-8. Latin-1 is a superset that never errors on Western text and
    FEMA's content is plain ASCII."""
    src = _read()
    assert 'shapefile.Reader(str(shp_path), encoding="latin-1")' in src, (
        "Loader must open shapefiles with encoding='latin-1' — "
        "Collin + Denton .cpg files lie about UTF-8."
    )


def test_loader_truncates_in_same_transaction_as_insert() -> None:
    """Mid-load failure must roll back the whole table — never leave
    flood_zones half-populated. Spec decision #14."""
    src = _read()
    pat = re.compile(
        r"def _truncate_and_insert\(conn, all_rows.*?\n"
        r".*?TRUNCATE flood_zones.*?\n"
        r".*?for i in range\(0, len\(all_rows\), BATCH_SIZE\):.*?\n"
        r".*?execute_values\(.*?\n",
        re.DOTALL,
    )
    assert pat.search(src), (
        "TRUNCATE and INSERT must live in the same _truncate_and_insert "
        "function so they share one transaction."
    )


def test_loader_creates_indexes_concurrently() -> None:
    """Shared Cloud SQL — CREATE INDEX CONCURRENTLY is mandatory so a
    quarterly refresh doesn't block readers."""
    src = _read()
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_geom" in src
    assert "USING GIST (geom)" in src
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_county" in src
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_sfha_t" in src
    assert "WHERE sfha_tf = 'T'" in src, "SFHA index must be partial (T only)"


def test_loader_uses_postgis_makevalid_multi_geomfromgeojson_pattern() -> None:
    """Mirror the ZCTA loader's proven precedent: ST_GeomFromGeoJSON +
    ST_MakeValid + ST_Multi handles multi-part ring topology + invalid
    geometry + MultiPolygon schema match in one nested call."""
    src = _read()
    pat = re.compile(
        r"ST_Multi\(ST_MakeValid\(ST_GeomFromGeoJSON\(%s\)\)\)",
    )
    assert pat.search(src), (
        "INSERT must use ST_Multi(ST_MakeValid(ST_GeomFromGeoJSON(%s))) "
        "to handle multi-part topology + cleanup + MultiPolygon coercion "
        "in one call (mirrors ZCTA loader precedent)."
    )


def test_loader_uses_geo_interface_for_geometry_extraction() -> None:
    """pyshp's shape.__geo_interface__ does correct outer/hole ring
    grouping via winding-order analysis internally. Raw shape.parts
    would require explicit topology handling (the original spec called
    for shapely; precedent shows __geo_interface__ is sufficient)."""
    src = _read()
    assert "shape_rec.shape.__geo_interface__" in src, (
        "Geometry must be extracted via __geo_interface__ (correct ring "
        "grouping) — not raw shape.parts (would require topology lib)."
    )


def test_loader_requires_snapshot_arg() -> None:
    """CLI must require --snapshot so a stale run can't accidentally
    target the wrong dated folder."""
    src = _read()
    assert '"--snapshot"' in src
    assert "required=True" in src, "--snapshot must be required, not optional"


def test_loader_validates_snapshot_dir_exists() -> None:
    """Bad --snapshot arg must error early with a clear message — not
    crash later inside the file-reading loop."""
    src = _read()
    pat = re.compile(
        r'snapshot_dir = ROOT_DIR / "ingest" / "fema" / "nfhl" / args\.snapshot.*?'
        r'if not snapshot_dir\.is_dir\(\):',
        re.DOTALL,
    )
    assert pat.search(src), (
        "Loader must check snapshot dir exists and error early with a "
        "clear message before any DB work."
    )


def test_loader_logs_per_county_progress() -> None:
    """30s silent run is anxiety-inducing — emit per-county progress."""
    src = _read()
    assert "[ingest_flood] reading {county}" in src or 'f"[ingest_flood] reading {county}' in src
    assert "[ingest_flood] inserted" in src or 'f"[ingest_flood] inserted' in src


def test_loader_aborts_before_truncate_if_no_rows_parsed() -> None:
    """If all 4 shapefiles failed to parse, NEVER truncate — otherwise
    a corrupted snapshot folder could wipe a previously-good table."""
    src = _read()
    pat = re.compile(
        r"if not all_rows:.*?"
        r"Aborting before TRUNCATE.*?"
        r"return 1",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Loader must check all_rows is non-empty BEFORE calling "
        "_truncate_and_insert — otherwise a parse-failure run could "
        "blow away the previous good data."
    )


def test_schema_uses_multipolygon_4326() -> None:
    """Match the rest of LotLedger PostGIS — all geom columns are
    GEOMETRY(<type>, 4326)."""
    src = _read()
    assert "GEOMETRY(MultiPolygon, 4326) NOT NULL" in src


def test_schema_has_serial_pk_and_required_audit_columns() -> None:
    """Schema choices per spec: SERIAL PK, ingested_at default now()."""
    src = _read()
    assert "id              SERIAL PRIMARY KEY" in src
    assert "ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()" in src
    assert "source_county   TEXT NOT NULL" in src
    assert "fld_zone        TEXT NOT NULL" in src


def test_loader_uses_execute_values_not_executemany() -> None:
    """psycopg2.executemany issues one network round trip PER ROW against
    Cloud SQL. With 41K polygons + ~20ms RTT that's a 14-minute crawl
    minimum. execute_values does true server-side batching: one round
    trip per chunk.

    Original 2026-06-06 run with executemany took 24+ minutes and stalled.
    Refactor caught + locked in.
    """
    src = _read()
    assert "from psycopg2.extras import execute_values" in src, (
        "Loader must import execute_values for batched INSERTs."
    )
    assert "execute_values(" in src, (
        "Loader must use execute_values inside _truncate_and_insert — "
        "cur.executemany is 100x slower against Cloud SQL."
    )
    # Hard-fail if anyone reverts to executemany in the INSERT path.
    # Match the call site (with paren), not just the string mention — the
    # docstring in the loader explains WHY not executemany and contains
    # the word.
    assert not re.search(r"cur\.executemany\(", src), (
        "Loader must NOT call cur.executemany( in the INSERT path — see "
        "_truncate_and_insert docstring for why. Use execute_values instead."
    )
