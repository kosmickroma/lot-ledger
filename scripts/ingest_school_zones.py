#!/usr/bin/env python3
# scripts/ingest_school_zones.py
#
# Per-district ingest for the DB-backed school-zones feature.
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §3, §3a, §4.
#
# ⚠️ THE ONE RULE: this script must NEVER clear the whole table in one shot.
# Loading one district must never be able to wipe another. Every write is a
# scoped DELETE WHERE district_tea_id=%s AND level=ANY(%s) + batched INSERT,
# both in the SAME transaction as scripts/ingest_flood_zones.py's whole-
# table-clear-then-insert pattern -- transaction hygiene copied, delete
# SCOPE deliberately narrowed (never the county-wide clear flood does).
#
# Run (one district per invocation):
#   .venv/bin/python3 scripts/ingest_school_zones.py \
#       --config ingest/schools/<YYYY-MM-DD>/<district>/config.json
#
# config.json shape -- see scripts/school_zones_adapters.py's adapter
# docstrings for the per-adapter level_config shape:
#   {
#     "district_tea_id": "057912", "district_name": "IRVING ISD",
#     "boundary_vintage": "2025-26", "source_kind": "arcgis",
#     "levels": {"elementary": {...}, "middle": {...}, "high": {...}}
#   }
# or for "district_boundary": {"source_kind": "district_boundary",
#   "boundary_file": "...", "campuses": {"elementary": {...}, ...}}
#
# Ratings ingest (separate, no district scoping needed -- PK is
# (campus_tea_id, rating_year), delete-by-year + insert, upsert on
# conflict, prior years untouched):
#   .venv/bin/python3 scripts/ingest_school_zones.py \
#       --ratings ingest/schools/<YYYY-MM-DD>/ratings.json --rating-year 2025
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg2
import pyproj
from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402
from scripts.build_school_pilot_data import is_already_wgs84  # noqa: E402 -- reuse the verified magnitude check
from scripts.migrate_school_zones_schema import ensure_schema_and_indexes  # noqa: E402
from scripts.school_zones_adapters import (  # noqa: E402
    adapter_arcgis_geojson,
    adapter_district_boundary,
    adapter_mymaps_kml,
    adapter_pilot_snapshot,
    pilot_ratings_to_ingest_shape,
)
from scripts.school_zones_registry import expected_counts_for_district  # noqa: E402

_LEVELS = ("elementary", "middle", "high")
BATCH_SIZE = 500

# §3a #4 -- refuse a re-run whose new count for a (district, level) is under
# half of what's already in the table, unless the operator passes --force.
EXISTING_ROWS_FLOOR = 0.5
# §3a #3 -- "wild mismatch" vs the registry's expected campus count. Not
# specified as an exact percentage in the spec; chosen symmetrically (half
# to double) so it catches gross adapter/config bugs without being so tight
# that a normal year-to-year rezoning trips it. Flagged in the build report
# as a judgment call, not a value taken verbatim from the spec.
REGISTRY_MISMATCH_LOW = 0.5
REGISTRY_MISMATCH_HIGH = 2.0


class IngestAbort(Exception):
    """Raised by a guard that must stop the run BEFORE any DB write."""


def _get_ingest_conn() -> "psycopg2.extensions.connection":
    """Gap 2 (docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md, safety
    re-review) -- the restricted school_zones_ingest role (scripts/
    grant_school_zones_role.sql) exists but was unused: ingest connected via
    api.config.get_conn(), the full-write app user, making the role's
    protection theater. All DATA WRITES (scoped delete + insert, ratings
    upsert) now connect via this dedicated DSN instead -- a distinct
    connection from the schema-migration step, which still needs get_conn()
    (the restricted role has no CREATE TABLE/INDEX privilege; it owns
    nothing, per §8's "cannot write anything else").
    SCHOOL_INGEST_DSN is an env var only -- gitignored, never a literal in
    this public repo. A missing/empty value is a hard, clear failure, not a
    silent fallback to the app-user pool."""
    dsn = os.getenv("SCHOOL_INGEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "SCHOOL_INGEST_DSN is not set. Data writes must connect as the "
            "restricted school_zones_ingest role (scripts/grant_school_zones_role.sql), "
            "never the shared app-user pool (api.config.get_conn). Set "
            "SCHOOL_INGEST_DSN to that role's connection string and retry -- "
            "this script will not silently fall back to a more privileged connection."
        )
    return psycopg2.connect(dsn)


def _reproject_coords(coords: Any, transformer: "pyproj.Transformer") -> Any:
    if coords and isinstance(coords[0], (int, float)):
        lng, lat = transformer.transform(coords[0], coords[1])
        return [lng, lat]
    return [_reproject_coords(c, transformer) for c in coords]


def normalize_geom_to_wgs84(geom: dict[str, Any]) -> dict[str, Any]:
    """Reuses build_school_pilot_data.is_already_wgs84's verified magnitude
    check (never trust a source's declared CRS blindly) -- transforms only
    when the coordinates are actually outside lon/lat range."""
    if is_already_wgs84(geom):
        return geom
    transformer = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return {"type": geom["type"], "coordinates": _reproject_coords(geom["coordinates"], transformer)}


def validate_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reproject + validate in memory, entirely before any DB touch (§3a #1).
    Drops (never fabricates) a row with a missing/invalid level, name, or
    geometry."""
    out = []
    for row in raw_rows:
        if row.get("level") not in _LEVELS:
            continue
        if not row.get("campus_name") or not row.get("district_tea_id"):
            continue
        geom = row.get("geom")
        if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        row = dict(row)
        row["geom"] = normalize_geom_to_wgs84(geom)
        out.append(row)
    return out


def levels_in(rows: list[dict[str, Any]]) -> list[str]:
    """§3 -- the delete scope MUST be derived from the validated parsed
    rows, never from config. This is the last over-delete guard: a config
    that says E/M/H but a source that silently yielded only elementary
    means the delete below only ever touches elementary."""
    return sorted({row["level"] for row in rows})


def _existing_counts(conn, district_tea_id: str, levels: list[str]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT level, COUNT(*) FROM school_attendance_zones "
            "WHERE district_tea_id = %s AND level = ANY(%s) GROUP BY level",
            (district_tea_id, levels),
        )
        return dict(cur.fetchall())


def run_guards(conn, district_tea_id: str, rows: list[dict[str, Any]], force: bool) -> list[str]:
    """All guards run BEFORE the transaction opens (§3a). Returns the
    validated `levels` list on success; raises IngestAbort on any failure --
    the caller must not open a transaction if this raises."""
    if not rows:
        raise IngestAbort("zero valid rows parsed -- aborting before any delete (§3a #2)")

    levels = levels_in(rows)
    if not levels:
        raise IngestAbort("levels list is empty after validation -- refusing to scope a delete to nothing")

    new_counts = Counter(row["level"] for row in rows)

    existing = _existing_counts(conn, district_tea_id, levels)
    for lvl in levels:
        existing_n = existing.get(lvl, 0)
        new_n = new_counts.get(lvl, 0)
        if existing_n > 0 and new_n < existing_n * EXISTING_ROWS_FLOOR and not force:
            raise IngestAbort(
                f"{lvl}: new count {new_n} is under {EXISTING_ROWS_FLOOR:.0%} of the "
                f"existing {existing_n} rows for district {district_tea_id} -- refusing "
                f"without --force (§3a #4)"
            )

    expected = expected_counts_for_district(district_tea_id)
    if expected:
        for lvl in levels:
            exp_n = expected.get(lvl)
            got_n = new_counts.get(lvl, 0)
            if exp_n and not (exp_n * REGISTRY_MISMATCH_LOW <= got_n <= exp_n * REGISTRY_MISMATCH_HIGH):
                raise IngestAbort(
                    f"{lvl}: parsed count {got_n} wildly mismatches registry-expected "
                    f"{exp_n} for district {district_tea_id} (§3a #3) -- aborting"
                )
    else:
        print(
            f"[ingest_school_zones] WARNING: district {district_tea_id} not found in "
            "the coverage registry -- skipping the registry count-floor guard (§3a #3)",
            file=sys.stderr,
        )

    return levels


def ingest_district(conn, district_tea_id: str, rows: list[dict[str, Any]], force: bool = False) -> int:
    """Scoped delete + batched insert, ONE transaction. §3's mandatory
    pattern. Returns rows inserted."""
    levels = run_guards(conn, district_tea_id, rows, force)

    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '300s'")
        cur.execute(
            "DELETE FROM school_attendance_zones WHERE district_tea_id = %s AND level = ANY(%s)",
            (district_tea_id, levels),
        )
        template = (
            "(%s, %s, %s, %s, %s, "
            "ST_Multi(ST_MakeValid(ST_GeomFromGeoJSON(%s))), %s, %s, %s, %s)"
        )
        values = [
            (
                row["level"], row["district_tea_id"], row.get("district_name"),
                row.get("campus_tea_id"), row["campus_name"], json.dumps(row["geom"]),
                row.get("boundary_vintage"), row.get("source_url"), row.get("source_kind"),
                row.get("retrieved_at"),
            )
            for row in rows
        ]
        inserted = 0
        for i in range(0, len(values), BATCH_SIZE):
            batch = values[i:i + BATCH_SIZE]
            execute_values(
                cur,
                """
                INSERT INTO school_attendance_zones (
                    level, district_tea_id, district_name, campus_tea_id,
                    campus_name, geom, boundary_vintage, source_url,
                    source_kind, retrieved_at
                ) VALUES %s
                """,
                batch,
                template=template,
                page_size=BATCH_SIZE,
            )
            inserted += len(batch)
            print(f"[ingest_school_zones] {district_tea_id}: inserted {inserted}/{len(values)}")
    conn.commit()
    return inserted


def ingest_ratings(conn, rating_year: int, ratings: dict[str, dict[str, Any]]) -> int:
    """§3's ratings rule: DELETE WHERE rating_year=%s + insert (upsert on
    the PK as a belt-and-suspenders second safeguard against a duplicate
    row within the same year). Never a whole-table clear -- prior years
    survive untouched (PK includes rating_year)."""
    if not ratings:
        return 0
    rows = [
        (
            campus_tea_id, rating_year, info.get("letter"), info.get("score"),
            json.dumps(info["achievement"]) if info.get("achievement") else None,
            json.dumps(info["growth"]) if info.get("growth") else None,
        )
        for campus_tea_id, info in ratings.items()
    ]
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("DELETE FROM school_campus_ratings WHERE rating_year = %s", (rating_year,))
        execute_values(
            cur,
            """
            INSERT INTO school_campus_ratings (
                campus_tea_id, rating_year, letter, score, achievement, growth
            ) VALUES %s
            ON CONFLICT (campus_tea_id, rating_year) DO UPDATE SET
                letter = EXCLUDED.letter, score = EXCLUDED.score,
                achievement = EXCLUDED.achievement, growth = EXCLUDED.growth
            """,
            rows,
            template="(%s, %s, %s, %s, %s, %s)",
            page_size=BATCH_SIZE,
        )
    conn.commit()
    return len(rows)


def _load_rows_from_config(config: dict[str, Any], snapshot_dir: Path) -> list[dict[str, Any]]:
    kind = config["source_kind"]
    district_tea_id = config["district_tea_id"]
    district_name = config.get("district_name")
    vintage = config.get("boundary_vintage")
    if kind == "arcgis":
        raw = adapter_arcgis_geojson(snapshot_dir, config["levels"], district_tea_id, district_name, vintage)
    elif kind == "kml":
        raw = adapter_mymaps_kml(snapshot_dir, config["levels"], district_tea_id, district_name, vintage)
    elif kind == "district_boundary":
        raw = adapter_district_boundary(
            snapshot_dir, config["boundary_file"], config["campuses"],
            district_tea_id, district_name, vintage, config.get("source_url"),
        )
    elif kind == "pilot_snapshot":
        # Gap 4 -- pilot_data_dir may be given relative to the repo root
        # (e.g. "data/school_pilot", the fixed, known location -- not a
        # per-district snapshot folder like the other 3 adapters use).
        pilot_dir = Path(config["pilot_data_dir"])
        if not pilot_dir.is_absolute():
            pilot_dir = ROOT_DIR / pilot_dir
        raw = adapter_pilot_snapshot(pilot_dir, district_tea_id, district_name)
    else:
        raise IngestAbort(f"unknown source_kind {kind!r}")
    return validate_rows(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to a district's config.json under ingest/schools/<date>/<district>/")
    parser.add_argument("--ratings", help="Path to a ratings.json snapshot (campus_tea_id -> {letter,score,achievement,growth})")
    parser.add_argument("--rating-year", type=int, help="Required with --ratings")
    parser.add_argument("--pilot-ratings", help="Path to the pilot's own data/school_pilot/ratings.json (Gap 4 -- grade/score/achievement/growth shape, rating_year read from its own meta)")
    parser.add_argument("--force", action="store_true", help="Bypass the <50%%-of-existing-rows tripwire (§3a #4)")
    args = parser.parse_args()

    if not args.config and not args.ratings and not args.pilot_ratings:
        print("[ingest_school_zones] ERROR: pass --config (zones), --ratings, and/or --pilot-ratings", file=sys.stderr)
        return 1

    # Schema migration needs CREATE TABLE/INDEX privilege the restricted
    # role deliberately does NOT have (§8 -- it owns nothing); this is the
    # only step that still uses the shared app-user pool.
    admin_conn = get_conn()
    try:
        print("[ingest_school_zones] ensuring schema ...")
        ensure_schema_and_indexes(admin_conn)
    finally:
        release_conn(admin_conn)

    # Every DATA WRITE below connects as the restricted school_zones_ingest
    # role (Gap 2) -- a distinct connection from the admin one above.
    conn = _get_ingest_conn()
    try:
        if args.config:
            config_path = Path(args.config).resolve()
            config = json.loads(config_path.read_text())
            rows = _load_rows_from_config(config, config_path.parent)
            try:
                inserted = ingest_district(conn, config["district_tea_id"], rows, force=args.force)
            except IngestAbort as exc:
                print(f"[ingest_school_zones] ABORTED (no write performed): {exc}", file=sys.stderr)
                return 1
            print(f"[ingest_school_zones] {config['district_tea_id']}: {inserted} zone rows loaded")

        if args.ratings:
            if not args.rating_year:
                print("[ingest_school_zones] ERROR: --ratings requires --rating-year", file=sys.stderr)
                return 1
            ratings = json.loads(Path(args.ratings).read_text())
            count = ingest_ratings(conn, args.rating_year, ratings)
            print(f"[ingest_school_zones] ratings: {count} campus rows loaded for {args.rating_year}")

        if args.pilot_ratings:
            year, ratings = pilot_ratings_to_ingest_shape(Path(args.pilot_ratings))
            count = ingest_ratings(conn, year, ratings)
            print(f"[ingest_school_zones] pilot ratings: {count} campus rows loaded for {year}")

        return 0
    except Exception as exc:
        conn.rollback()
        print(f"[ingest_school_zones] FAILED: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
