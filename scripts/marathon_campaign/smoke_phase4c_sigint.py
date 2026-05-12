# scripts/marathon_campaign/smoke_phase4c_sigint.py
#
# Role: Phase 4C smoke test for SIGINT transition handling and clean shutdown safety.
#
# Connects to:
#   api/main.py                          - ensures schema exists
#   api/config.py                        - session DB connections
#   scripts/marathon_campaign/runner.py  - SIGINT transition helper + module state

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign import runner


def _seed_campaign(campaign_key: str) -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_marathon_campaigns (campaign_key, status, started_at, updated_at)
                VALUES (%s, 'running', NOW(), NOW())
                ON CONFLICT (campaign_key) DO UPDATE SET updated_at = NOW()
                RETURNING campaign_id
                """,
                (campaign_key,),
            )
            campaign_id = int(cur.fetchone()[0])
        conn.commit()
        return campaign_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _insert_seed(campaign_id: int, *, account_suffix: str, status: str, job_id: str | None = None) -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
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
                    attempts,
                    max_attempts,
                    heartbeat_at,
                    job_id,
                    queued_at,
                    running_at,
                    completed_at,
                    last_transition_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'dcad',
                    32.711,
                    -96.911,
                    %s,
                    32.711,
                    -96.911,
                    'suburban',
                    320,
                    %s::propelio_marathon_seed_state,
                    1,
                    5,
                    NOW(),
                    %s,
                    NOW(),
                    CASE WHEN %s = 'running' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'completed' THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"phase4c_{account_suffix}_{uuid4().hex[:8]}",
                    f"{200 + (len(account_suffix) % 50)} Sigint Smoke Dr",
                    status,
                    job_id,
                    status,
                    status,
                ),
            )
            seed_id = int(cur.fetchone()[0])
        conn.commit()
        return seed_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _insert_job(job_id: str, *, stop_requested: bool = False) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_deep_pull_jobs (
                    job_id,
                    target_address,
                    status,
                    stop_requested,
                    started_at,
                    last_pass_at,
                    next_pass_at,
                    total_unique_comps,
                    net_new_comps,
                    last_error
                )
                VALUES (%s, %s, 'running', %s, NOW(), NOW(), NOW(), 0, 0, NULL)
                ON CONFLICT (job_id) DO UPDATE SET
                    stop_requested = EXCLUDED.stop_requested,
                    status = EXCLUDED.status,
                    last_pass_at = NOW(),
                    next_pass_at = NOW()
                """,
                (job_id, "1 Signal Test Ln", bool(stop_requested)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _fetch_seed_status(seed_id: int) -> str:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status::text FROM propelio_marathon_seeds WHERE seed_id = %s",
                (int(seed_id),),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if row is None:
        raise RuntimeError(f"seed missing: {seed_id}")
    return str(row[0])


def _fetch_job_stop_requested(job_id: str) -> bool:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stop_requested FROM propelio_deep_pull_jobs WHERE job_id = %s",
                (str(job_id),),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if row is None:
        raise RuntimeError(f"job missing: {job_id}")
    return bool(row[0])


def _assert_case_with_job(campaign_id: int) -> None:
    job_id = f"sigint_job_{uuid4().hex[:8]}"
    _insert_job(job_id, stop_requested=False)
    seed_id = _insert_seed(campaign_id, account_suffix="with_job", status="running", job_id=job_id)

    runner._current_seed = {"seed_id": seed_id, "job_id": job_id}
    runner._handle_sigint_transition()

    status = _fetch_seed_status(seed_id)
    stop_requested = _fetch_job_stop_requested(job_id)
    if status != "stopping_requested":
        raise RuntimeError(f"case with job expected stopping_requested, got {status}")
    if not stop_requested:
        raise RuntimeError("case with job expected deep-pull stop_requested=TRUE")


def _assert_case_no_job(campaign_id: int) -> None:
    seed_id = _insert_seed(campaign_id, account_suffix="no_job", status="running", job_id=None)

    runner._current_seed = {"seed_id": seed_id, "job_id": None}
    runner._handle_sigint_transition()

    status = _fetch_seed_status(seed_id)
    if status != "queued":
        raise RuntimeError(f"case no job expected queued, got {status}")


def _assert_case_no_seed() -> None:
    runner._current_seed = None
    runner._handle_sigint_transition()


def _assert_case_race_completed(campaign_id: int) -> None:
    seed_id = _insert_seed(campaign_id, account_suffix="race_completed", status="completed", job_id=None)

    runner._current_seed = {"seed_id": seed_id, "job_id": None}
    runner._handle_sigint_transition()

    status = _fetch_seed_status(seed_id)
    if status != "completed":
        raise RuntimeError(f"race case expected completed to remain unchanged, got {status}")


def main() -> None:
    _ensure_session_schema()
    campaign_key = f"phase4c_smoke_{uuid4().hex[:8]}"
    campaign_id = _seed_campaign(campaign_key)

    _assert_case_with_job(campaign_id)
    _assert_case_no_job(campaign_id)
    _assert_case_no_seed()
    _assert_case_race_completed(campaign_id)

    runner._current_seed = None
    print(f"smoke_phase4c_sigint: PASS campaign_key={campaign_key} campaign_id={campaign_id}")


if __name__ == "__main__":
    main()
