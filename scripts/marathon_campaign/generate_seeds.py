# scripts/marathon_campaign/generate_seeds.py
#
# Role: Generate marathon seed rows from a DFW grid by snapping each grid
#       intersection to the nearest parcel and classifying density.
#
# Connects to:
#   api/config.py                         - session DB connection helpers
#   api/main.py                           - _ensure_session_schema migration entry
#   propelio_marathon_campaigns table     - inserts/loads campaign row
#   propelio_marathon_seeds table         - inserts idempotent seed rows
#   parcels/tad_parcels/collin_parcels/
#   denton_parcels                        - nearest parcel snap + density counts

from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import islice
from math import cos, radians
from pathlib import Path
import sys
from typing import Iterator

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, get_session_conn, release_conn, release_session_conn
from api.main import _ensure_session_schema


DFW_BBOX = {
    "lat_min": 32.55,
    "lat_max": 33.10,
    "lng_min": -97.30,
    "lng_max": -96.50,
}

GRID_SPACING_MILES = 8.0
SNAP_RADIUS_METERS = 1609.344  # 1 mile
BBOX_PREFILTER_DEGREES = 0.02  # ~1.4mi envelope at DFW lat — bbox prefilter so GIST index kicks in before ST_DWithin


@dataclass(frozen=True)
class GridPoint:
    lat: float
    lng: float


@dataclass(frozen=True)
class SeedCandidate:
    account_num: str
    address_full: str
    lat: float
    lng: float
    county: str


@dataclass(frozen=True)
class ClassifiedSeedCandidate(SeedCandidate):
    parcels_within_1mi: int
    density_class: str


def _mile_to_lat_degrees(miles: float) -> float:
    return float(miles) / 69.0


def _mile_to_lng_degrees(miles: float, latitude: float) -> float:
    denom = 69.172 * cos(radians(latitude))
    if denom <= 0:
        return 0.0
    return float(miles) / denom


def _frange(start: float, stop: float, step: float) -> Iterator[float]:
    value = start
    while value <= stop + 1e-9:
        yield value
        value += step


def _generate_grid_points() -> list[GridPoint]:
    lat_step = _mile_to_lat_degrees(GRID_SPACING_MILES)
    mid_lat = (DFW_BBOX["lat_min"] + DFW_BBOX["lat_max"]) / 2.0
    lng_step = _mile_to_lng_degrees(GRID_SPACING_MILES, mid_lat)

    points: list[GridPoint] = []
    for lat in _frange(DFW_BBOX["lat_min"], DFW_BBOX["lat_max"], lat_step):
        for lng in _frange(DFW_BBOX["lng_min"], DFW_BBOX["lng_max"], lng_step):
            points.append(GridPoint(lat=round(lat, 7), lng=round(lng, 7)))
    return points


def _classify_density(parcel_count: int) -> str:
    if parcel_count > 800:
        return "urban"
    if parcel_count <= 200:
        return "rural"
    return "suburban"


def _chunks(items: list[tuple[int, float, float]], size: int) -> Iterator[list[tuple[int, float, float]]]:
    iterator = iter(items)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _ensure_campaign(cur, campaign_key: str) -> int:
    cur.execute(
        """
        INSERT INTO propelio_marathon_campaigns (campaign_key, status, started_at, updated_at)
        VALUES (%s, 'queued', NOW(), NOW())
        ON CONFLICT (campaign_key) DO UPDATE
            SET updated_at = NOW()
        RETURNING campaign_id
        """,
        (campaign_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("failed to resolve campaign row")
    return int(row[0])


def _snap_grid_point(cur, point: GridPoint) -> SeedCandidate | None:
    cur.execute(
        """
        WITH candidates AS (
            SELECT
                'dcad'::text AS parcel_county,
                p.account_num::text AS account_num,
                p.property_address::text AS address,
                p.centroid AS centroid
            FROM parcels p
            WHERE p.centroid IS NOT NULL
              AND p.property_address IS NOT NULL
              AND p.property_address <> ''
              AND ST_DWithin(
                  p.centroid::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                  %s
              )

            UNION ALL

            SELECT
                'tad'::text AS parcel_county,
                t.account_num::text AS account_num,
                t.situs_addr::text AS address,
                t.centroid AS centroid
            FROM tad_parcels t
            WHERE t.centroid IS NOT NULL
              AND t.situs_addr IS NOT NULL
              AND t.situs_addr <> ''
              AND ST_DWithin(
                  t.centroid::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                  %s
              )

            UNION ALL

            SELECT
                'collin'::text AS parcel_county,
                c.account_num::text AS account_num,
                c.property_address::text AS address,
                c.centroid AS centroid
            FROM collin_parcels c
            WHERE c.centroid IS NOT NULL
              AND c.property_address IS NOT NULL
              AND c.property_address <> ''
              AND ST_DWithin(
                  c.centroid::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                  %s
              )

            UNION ALL

            SELECT
                'denton'::text AS parcel_county,
                d.account_num::text AS account_num,
                d.property_address::text AS address,
                d.centroid AS centroid
            FROM denton_parcels d
            WHERE d.centroid IS NOT NULL
              AND d.property_address IS NOT NULL
              AND d.property_address <> ''
              AND ST_DWithin(
                  d.centroid::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                  %s
              )
        )
        SELECT
            parcel_county,
            account_num,
            address,
            ST_Y(centroid) AS lat,
            ST_X(centroid) AS lng
        FROM candidates
        ORDER BY centroid <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        LIMIT 1
        """,
        (
            point.lng, point.lat, SNAP_RADIUS_METERS,
            point.lng, point.lat, SNAP_RADIUS_METERS,
            point.lng, point.lat, SNAP_RADIUS_METERS,
            point.lng, point.lat, SNAP_RADIUS_METERS,
            point.lng, point.lat,
        ),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return SeedCandidate(
        account_num=str(row[1]),
        address_full=str(row[2]),
        lat=float(row[3]),
        lng=float(row[4]),
        county=str(row[0]),
    )


def _batched_density_counts(cur, candidates: list[SeedCandidate]) -> dict[int, int]:
    if not candidates:
        return {}

    cur.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS tmp_marathon_subject_points (
            subject_id INTEGER PRIMARY KEY,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL
        ) ON COMMIT DROP
        """
    )
    cur.execute("TRUNCATE tmp_marathon_subject_points")

    rows = [(idx, candidate.lat, candidate.lng) for idx, candidate in enumerate(candidates)]
    for chunk in _chunks(rows, 500):
        values_sql = ", ".join(["(%s, %s, %s)"] * len(chunk))
        params: list[float | int] = []
        for subject_id, lat, lng in chunk:
            params.extend([subject_id, lat, lng])
        cur.execute(
            f"INSERT INTO tmp_marathon_subject_points (subject_id, lat, lng) VALUES {values_sql}",
            tuple(params),
        )

    cur.execute(
        """
        WITH subjects AS (
            SELECT
                subject_id,
                ST_SetSRID(ST_MakePoint(lng, lat), 4326) AS geom,
                ST_Expand(ST_SetSRID(ST_MakePoint(lng, lat), 4326), %s) AS bbox
            FROM tmp_marathon_subject_points
        ),
        parcel_counts AS (
            SELECT s.subject_id, COUNT(*) AS cnt
            FROM subjects s
            JOIN parcels p
              ON p.centroid IS NOT NULL
             AND p.centroid && s.bbox
             AND ST_DWithin(p.centroid::geography, s.geom::geography, %s)
            GROUP BY s.subject_id

            UNION ALL

            SELECT s.subject_id, COUNT(*) AS cnt
            FROM subjects s
            JOIN tad_parcels t
              ON t.centroid IS NOT NULL
             AND t.centroid && s.bbox
             AND ST_DWithin(t.centroid::geography, s.geom::geography, %s)
            GROUP BY s.subject_id

            UNION ALL

            SELECT s.subject_id, COUNT(*) AS cnt
            FROM subjects s
            JOIN collin_parcels c
              ON c.centroid IS NOT NULL
             AND c.centroid && s.bbox
             AND ST_DWithin(c.centroid::geography, s.geom::geography, %s)
            GROUP BY s.subject_id

            UNION ALL

            SELECT s.subject_id, COUNT(*) AS cnt
            FROM subjects s
            JOIN denton_parcels d
              ON d.centroid IS NOT NULL
             AND d.centroid && s.bbox
             AND ST_DWithin(d.centroid::geography, s.geom::geography, %s)
            GROUP BY s.subject_id
        )
        SELECT subject_id, SUM(cnt) AS total_cnt
        FROM parcel_counts
        GROUP BY subject_id
        ORDER BY subject_id
        """,
        (
            BBOX_PREFILTER_DEGREES,
            SNAP_RADIUS_METERS,
            SNAP_RADIUS_METERS,
            SNAP_RADIUS_METERS,
            SNAP_RADIUS_METERS,
        ),
    )
    counts = {int(subject_id): int(total_cnt or 0) for subject_id, total_cnt in (cur.fetchall() or [])}
    return {idx: counts.get(idx, 0) for idx in range(len(candidates))}


def _insert_seed(cur, campaign_id: int, point: GridPoint, candidate: SeedCandidate) -> bool:
    cur.execute(
        """
        INSERT INTO propelio_marathon_seeds (
            campaign_id,
            parcel_account_num,
            parcel_county,
            grid_lat,
            grid_lng,
            seed_address,
            seed_lat,
            seed_lng,
            density_class,
            parcels_within_1mi,
            status,
            queued_at,
            last_transition_at,
            created_at,
            updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            'queued', NOW(), NOW(), NOW(), NOW()
        )
        ON CONFLICT (campaign_id, parcel_county, parcel_account_num)
        DO NOTHING
        RETURNING seed_id
        """,
        (
            campaign_id,
            candidate.account_num,
            candidate.county,
            point.lat,
            point.lng,
            candidate.address_full,
            candidate.lat,
            candidate.lng,
            candidate.density_class,
            candidate.parcels_within_1mi,
        ),
    )
    return cur.fetchone() is not None


def generate(campaign_key: str) -> None:
    campaign_key = str(campaign_key or "").strip()
    if not campaign_key:
        raise ValueError("campaign key is required")

    _ensure_session_schema()

    grid_points = _generate_grid_points()

    session_conn = get_session_conn()
    parcel_conn = get_conn()
    try:
        with session_conn.cursor() as session_cur, parcel_conn.cursor() as parcel_cur:
            campaign_id = _ensure_campaign(session_cur, campaign_key)

            inserted = 0
            skipped_no_parcel = 0
            snapped_candidates: list[tuple[GridPoint, SeedCandidate]] = []

            for idx, point in enumerate(grid_points, start=1):
                candidate = _snap_grid_point(parcel_cur, point)
                if candidate is None:
                    skipped_no_parcel += 1
                    if idx % 10 == 0:
                        print(f"[marathon-seeds] snap_progress={idx}/{len(grid_points)} snapped={len(snapped_candidates)} skipped={skipped_no_parcel}", flush=True)
                    continue

                snapped_candidates.append((point, candidate))
                if idx % 10 == 0:
                    print(f"[marathon-seeds] snap_progress={idx}/{len(grid_points)} snapped={len(snapped_candidates)} skipped={skipped_no_parcel}", flush=True)

            print(f"[marathon-seeds] density_batch_start candidates={len(snapped_candidates)}", flush=True)
            density_counts = _batched_density_counts(parcel_cur, [candidate for _, candidate in snapped_candidates])

            urban = 0
            suburban = 0
            rural = 0

            for idx, (point, candidate) in enumerate(snapped_candidates):
                parcel_count = int(density_counts.get(idx, 0))
                classified = ClassifiedSeedCandidate(
                    account_num=candidate.account_num,
                    address_full=candidate.address_full,
                    lat=candidate.lat,
                    lng=candidate.lng,
                    county=candidate.county,
                    parcels_within_1mi=parcel_count,
                    density_class=_classify_density(parcel_count),
                )

                was_inserted = _insert_seed(session_cur, campaign_id, point, classified)
                if not was_inserted:
                    continue

                inserted += 1
                if classified.density_class == "urban":
                    urban += 1
                elif classified.density_class == "suburban":
                    suburban += 1
                else:
                    rural += 1

            session_cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE density_class = 'urban') AS urban,
                    COUNT(*) FILTER (WHERE density_class = 'suburban') AS suburban,
                    COUNT(*) FILTER (WHERE density_class = 'rural') AS rural
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            total, total_urban, total_suburban, total_rural = session_cur.fetchone() or (0, 0, 0, 0)

            session_cur.execute(
                """
                UPDATE propelio_marathon_campaigns
                SET
                    seeds_total = %s,
                    seeds_completed = COALESCE(seeds_completed, 0),
                    seeds_running = COALESCE(seeds_running, 0),
                    seeds_retryable = COALESCE(seeds_retryable, 0),
                    seeds_failed = COALESCE(seeds_failed, 0),
                    seeds_skipped = COALESCE(seeds_skipped, 0),
                    updated_at = NOW()
                WHERE campaign_id = %s
                """,
                (int(total or 0), campaign_id),
            )

        session_conn.commit()
        parcel_conn.commit()
    except Exception:
        session_conn.rollback()
        parcel_conn.rollback()
        raise
    finally:
        release_session_conn(session_conn)
        release_conn(parcel_conn)

    print(
        "[marathon-seeds] "
        f"campaign_key={campaign_key} "
        f"grid_points={len(grid_points)} "
        f"inserted={inserted} "
        f"skipped_no_parcel={skipped_no_parcel}"
    )
    print(
        "[marathon-seeds] "
        f"total={int(total or 0)} "
        f"urban={int(total_urban or 0)} "
        f"suburban={int(total_suburban or 0)} "
        f"rural={int(total_rural or 0)} "
        f"skipped={skipped_no_parcel}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate marathon seed rows by snapping an 8mi DFW grid to parcels"
    )
    parser.add_argument(
        "campaign_key",
        help="Campaign key/name (idempotent: re-running same key does not duplicate seeds)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    generate(args.campaign_key)


if __name__ == "__main__":
    main()
