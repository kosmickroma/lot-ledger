# scripts/marathon_campaign/smoke_phase4b_resilience.py
#
# Role: Phase 4B smoke test for runner resilience helpers using mock/local DB state.
#
# Connects to:
#   api/main.py                             - ensures session schema is present
#   api/config.py                           - session DB connection helpers
#   scripts/marathon_campaign/runner.py     - reconcile_orphans, transient handling

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.runner import (
    exponential_backoff,
    handle_transient_failure,
    reconcile_orphans,
)


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


def _insert_seed(
    campaign_id: int,
    *,
    account_suffix: str,
    status: str,
    attempts: int,
    max_attempts: int,
    heartbeat_age_minutes: int = 20,
    job_id: str | None = None,
) -> int:
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
                    verifying_at,
                    stopping_requested_at,
                    last_transition_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'dcad',
                    32.701,
                    -96.901,
                    %s,
                    32.701,
                    -96.901,
                    'suburban',
                    350,
                    %s::propelio_marathon_seed_state,
                    %s,
                    %s,
                    NOW() - (%s || ' minutes')::interval,
                    %s,
                    NOW(),
                    CASE WHEN %s = 'running' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'verifying' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'stopping_requested' THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"phase4b_{account_suffix}_{uuid4().hex[:8]}",
                    f"{100 + (len(account_suffix) % 50)} Resilience Smoke Ave",
                    status,
                    int(attempts),
                    int(max_attempts),
                    int(heartbeat_age_minutes),
                    job_id,
                    status,
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


def _insert_job(job_id: str, status: str, total_unique: int, net_new: int) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_deep_pull_jobs (
                    job_id,
                    target_address,
                    status,
                    started_at,
                    last_pass_at,
                    next_pass_at,
                    total_unique_comps,
                    net_new_comps,
                    last_error
                )
                VALUES (%s, %s, %s, NOW(), NOW(), NOW(), %s, %s, NULL)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    total_unique_comps = EXCLUDED.total_unique_comps,
                    net_new_comps = EXCLUDED.net_new_comps,
                    last_pass_at = NOW(),
                    next_pass_at = NOW()
                """,
                (
                    job_id,
                    "123 Mock Job Way",
                    status,
                    int(total_unique),
                    int(net_new),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _fetch_seed(seed_id: int) -> tuple[str, str | None, int | None, int | None, datetime | None]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    status::text,
                    last_error_class,
                    comps_captured,
                    net_new_comps,
                    retry_after
                FROM propelio_marathon_seeds
                WHERE seed_id = %s
                """,
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
        raise RuntimeError(f"seed_id missing: {seed_id}")
    return (str(row[0]), row[1], row[2], row[3], row[4])


def _assert_orphan_recovery_failed_retryable(campaign_id: int) -> None:
    seed_id = _insert_seed(
        campaign_id,
        account_suffix="orphan_dead",
        status="running",
        attempts=1,
        max_attempts=5,
        heartbeat_age_minutes=20,
        job_id="fake_dead",
    )
    reconcile_orphans(campaign_id)
    status, last_error_class, _, _, _ = _fetch_seed(seed_id)

    if status != "failed_retryable":
        raise RuntimeError(f"orphan dead seed expected failed_retryable, got {status}")
    if last_error_class != "orphaned_after_crash":
        raise RuntimeError(f"orphan dead seed expected orphaned_after_crash, got {last_error_class}")


def _assert_orphan_adopt_completed(campaign_id: int) -> None:
    job_id = f"smoke_completed_{uuid4().hex[:8]}"
    _insert_job(job_id=job_id, status="completed", total_unique=222, net_new=77)

    seed_id = _insert_seed(
        campaign_id,
        account_suffix="orphan_complete",
        status="running",
        attempts=1,
        max_attempts=5,
        heartbeat_age_minutes=20,
        job_id=job_id,
    )
    reconcile_orphans(campaign_id)
    status, _, comps, net_new, _ = _fetch_seed(seed_id)

    if status != "completed":
        raise RuntimeError(f"orphan adopt expected completed, got {status}")
    if int(comps or 0) != 222:
        raise RuntimeError(f"orphan adopt expected comps_captured=222, got {comps}")
    if int(net_new or 0) != 77:
        raise RuntimeError(f"orphan adopt expected net_new_comps=77, got {net_new}")


def _assert_transient_rate_limit(campaign_id: int) -> None:
    seed_id = _insert_seed(
        campaign_id,
        account_suffix="rate_limit",
        status="running",
        attempts=1,
        max_attempts=5,
        heartbeat_age_minutes=1,
    )
    handle_transient_failure(
        {"seed_id": seed_id, "attempts": 1, "max_attempts": 5},
        Exception("rate limited"),
        error_class="rate_limit",
        from_state="running",
        retry_min=30,
    )
    status, _, _, _, retry_after = _fetch_seed(seed_id)

    if status != "failed_retryable":
        raise RuntimeError(f"rate-limit transient expected failed_retryable, got {status}")
    if retry_after is None:
        raise RuntimeError("rate-limit transient expected retry_after to be set")

    delta_min = (retry_after - datetime.now(timezone.utc)).total_seconds() / 60.0
    if not (28.0 <= delta_min <= 32.0):
        raise RuntimeError(f"rate-limit transient retry_after expected ~30m, got {delta_min:.2f}m")


def _assert_final_failure(campaign_id: int) -> None:
    seed_id = _insert_seed(
        campaign_id,
        account_suffix="final_fail",
        status="running",
        attempts=5,
        max_attempts=5,
        heartbeat_age_minutes=1,
    )
    handle_transient_failure(
        {"seed_id": seed_id, "attempts": 5, "max_attempts": 5},
        Exception("too many attempts"),
        error_class="unexpected",
        from_state="running",
        retry_min=5,
    )
    status, _, _, _, retry_after = _fetch_seed(seed_id)

    if status != "failed_final":
        raise RuntimeError(f"final failure expected failed_final, got {status}")
    if retry_after is not None:
        raise RuntimeError("final failure should not set retry_after")


def _assert_exponential_backoff(campaign_id: int) -> None:
    retry_mins: list[float] = []
    for attempts in (1, 2, 3, 4):
        seed_id = _insert_seed(
            campaign_id,
            account_suffix=f"backoff_{attempts}",
            status="running",
            attempts=attempts,
            max_attempts=8,
            heartbeat_age_minutes=1,
        )
        handle_transient_failure(
            {"seed_id": seed_id, "attempts": attempts, "max_attempts": 8},
            Exception(f"network fail #{attempts}"),
            error_class="network",
            from_state="running",
            retry_min=5,
        )
        status, _, _, _, retry_after = _fetch_seed(seed_id)
        if status != "failed_retryable" or retry_after is None:
            raise RuntimeError(f"backoff attempts={attempts} expected failed_retryable with retry_after")

        retry_delta = (retry_after - datetime.now(timezone.utc)).total_seconds() / 60.0
        retry_mins.append(retry_delta)

    if retry_mins != sorted(retry_mins):
        raise RuntimeError(f"backoff retry minutes not monotonic increasing: {retry_mins}")
    if retry_mins[-1] > 60.8:
        raise RuntimeError(f"backoff cap exceeded: {retry_mins}")

    expected = [5 * exponential_backoff(a) for a in (1, 2, 3, 4)]
    expected = [min(60, x) for x in expected]
    print(f"smoke_phase4b_resilience: backoff_expected={expected} observed={[round(x, 2) for x in retry_mins]}")


def main() -> None:
    _ensure_session_schema()
    campaign_key = f"phase4b_smoke_{uuid4().hex[:8]}"
    campaign_id = _seed_campaign(campaign_key)

    _assert_orphan_recovery_failed_retryable(campaign_id)
    _assert_orphan_adopt_completed(campaign_id)
    _assert_transient_rate_limit(campaign_id)
    _assert_final_failure(campaign_id)
    _assert_exponential_backoff(campaign_id)

    print(f"smoke_phase4b_resilience: PASS campaign_key={campaign_key} campaign_id={campaign_id}")


if __name__ == "__main__":
    main()
