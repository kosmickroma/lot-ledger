# scripts/marathon_campaign/runner.py
#
# Role: Phase 4A marathon runner happy-path loop with atomic seed claims,
#       heartbeat polling, and mock pull support for smoke tests.
#
# Connects to:
#   api/config.py                               - session DB connection helpers
#   scripts/marathon_campaign/state.py          - FSM transition guard
#   scripts/marathon_campaign/circuit_breaker.py - breaker state
#   scripts/marathon_campaign/cooldown.py       - cooldown wait helper
#   scripts/marathon_campaign/pacing.py         - inter-seed pause + break logic
#   scripts/marathon_campaign/pass_configs.py    - density-based deep pull pass selection
#   api.propelio.deep_pull.run_deep_pull        - real pull worker (non-mock path)

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import random
import signal
import socket
import sys
import uuid
from typing import Any

from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from api.config import get_session_conn, release_session_conn
from .alerts import alert
from .circuit_breaker import CircuitBreaker
from .cooldown import wait_for_cooldown_or_exit
from .events import emit_event
from .pass_configs import passes_for_density_class
from .pacing import inter_seed_pause_seconds, maybe_take_break
from .state import IllegalStateTransition, transition


logger = logging.getLogger(__name__)


_current_seed: dict[str, Any] | None = None
_run_end_reason: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_runner_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Outcome:
    status: str
    total: int | None = None
    net_new: int | None = None
    error: str | None = None


_MOCK_JOBS: dict[str, dict[str, object]] = {}


# TODO(phase4c): replace local stubs with canonical exceptions from api/propelio.
class PropelioAuthError(Exception):
    pass


class PropelioRateLimitError(Exception):
    pass


def get_run_end_reason() -> str | None:
    return _run_end_reason


def stop_deep_pull_remote(job_id: str) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE propelio_deep_pull_jobs SET stop_requested = TRUE WHERE job_id = %s",
                (str(job_id),),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _handle_sigint_transition() -> None:
    global _run_end_reason

    seed = _current_seed
    if seed is None:
        _run_end_reason = "sigint"
        return

    try:
        seed_id = int(seed["seed_id"])
        job_id = str(seed.get("job_id") or "").strip() or None

        if job_id:
            result = transition(seed_id, "running", "stopping_requested")
            if result.success:
                stop_deep_pull_remote(job_id)
        else:
            transition(seed_id, "running", "queued", retry_after=None)
    except IllegalStateTransition:
        # Race-safe: seed may already have transitioned in another worker path.
        pass
    except Exception:
        logger.exception("sigint handler transition failed (non-fatal)")
    finally:
        _run_end_reason = "sigint"


def on_sigint(signum: int, frame: Any) -> None:
    _handle_sigint_transition()
    sys.exit(0)


def setup_sigint_handler() -> None:
    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)


def _truncate_error(value: object, limit: int = 500) -> str:
    return str(value or "")[: int(limit)]


def exponential_backoff(attempts: int) -> int:
    """Return exponential multiplier for retries, starting at 1 for attempt=1."""
    return 2 ** max(0, int(attempts) - 1)


def _retry_delay_minutes(retry_min: int, attempts: int) -> int:
    delay = int(retry_min) * exponential_backoff(int(attempts))
    return max(1, min(60, int(delay)))


def _load_campaign(campaign_key: str) -> dict[str, object] | None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT campaign_id, campaign_key, status
                FROM propelio_marathon_campaigns
                WHERE campaign_key = %s
                """,
                (str(campaign_key or "").strip(),),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def claim_next_seed(campaign_id: int, runner_id: str) -> dict[str, object] | None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT seed_id
                    FROM propelio_marathon_seeds
                    WHERE campaign_id = %s
                                            AND parcel_county = 'dcad'
                      AND (
                        status = 'queued'
                        OR (
                            status = 'failed_retryable'
                            AND (retry_after IS NULL OR retry_after <= NOW())
                        )
                      )
                                        ORDER BY grid_lat ASC, grid_lng ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE propelio_marathon_seeds s
                SET status = 'running',
                    claimed_by = %s,
                    heartbeat_at = NOW(),
                    attempt_started_at = NOW(),
                    attempts = attempts + 1,
                    running_at = NOW(),
                    last_transition_at = NOW(),
                    updated_at = NOW()
                FROM candidate
                WHERE s.seed_id = candidate.seed_id
                RETURNING s.*
                """,
                (int(campaign_id), str(runner_id or "").strip()),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _load_seed_state(seed_id: int) -> dict[str, Any] | None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT seed_id, status::text AS status, attempts, max_attempts, job_id
                FROM propelio_marathon_seeds
                WHERE seed_id = %s
                """,
                (int(seed_id),),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _transition_best_effort(
    seed_id: int,
    from_state: str,
    to_state: str,
    **fields: Any,
) -> bool:
    try:
        result = transition(int(seed_id), str(from_state), str(to_state), **fields)
        if result.success:
            return True
    except IllegalStateTransition:
        pass

    current = _load_seed_state(int(seed_id))
    if not current:
        return False
    current_state = str(current.get("status") or "")
    if not current_state or current_state == from_state:
        return False

    try:
        result = transition(int(seed_id), current_state, str(to_state), **fields)
        return bool(result.success)
    except IllegalStateTransition:
        return False


async def start_deep_pull_for_seed(seed: dict[str, object], mock: bool = False) -> str:
    if mock:
        job_id = f"mock_{uuid.uuid4().hex[:10]}"
        total = random.randint(80, 240)
        net_new = random.randint(10, min(90, total))
        _MOCK_JOBS[job_id] = {
            "created_at": _utcnow(),
            "status": "running",
            "total_unique_comps": total,
            "net_new_comps": net_new,
            "last_error": None,
        }
        return job_id

    from api.propelio.deep_pull import run_deep_pull

    address = str(seed.get("seed_address") or "").strip()
    if not address:
        raise ValueError("seed_address is required")

    job_id = "dp_" + uuid.uuid4().hex[:10]
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_deep_pull_jobs
                    (job_id, target_address, saved_area_id, started_by_user_id, status)
                VALUES (%s, %s, %s, %s, 'queued')
                """,
                (job_id, address, None, None),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    passes = passes_for_density_class(str(seed.get("density_class") or ""))
    asyncio.create_task(run_deep_pull(job_id, passes=passes))
    return job_id


def _fetch_job_snapshot(job_id: str) -> dict[str, Any] | None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT status, total_unique_comps, net_new_comps, last_error
                FROM propelio_deep_pull_jobs
                WHERE job_id = %s
                """,
                (str(job_id),),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except errors.UndefinedTable:
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def reconcile_orphans(campaign_id: int) -> dict[str, int]:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT seed_id, status::text AS status, job_id, heartbeat_at
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                  AND status IN ('running', 'verifying', 'stopping_requested')
                  AND heartbeat_at < NOW() - INTERVAL '15 minutes'
                ORDER BY seed_id
                """,
                (int(campaign_id),),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    summary = {
        "count": 0,
        "completed": 0,
        "adopted": 0,
        "requeued": 0,
    }

    for row in rows:
        seed_id = int(row["seed_id"])
        from_state = str(row.get("status") or "running")
        job_id = str(row.get("job_id") or "").strip() or None
        summary["count"] += 1

        if job_id:
            job = _fetch_job_snapshot(job_id)
            remote_status = str((job or {}).get("status") or "").strip().lower()

            if remote_status in {"completed", "saturated"}:
                if _transition_best_effort(
                    seed_id,
                    from_state,
                    "completed",
                    comps_captured=int((job or {}).get("total_unique_comps") or 0),
                    net_new_comps=int((job or {}).get("net_new_comps") or 0),
                ):
                    summary["completed"] += 1
                continue

            if remote_status == "running":
                conn = get_session_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE propelio_marathon_seeds
                            SET heartbeat_at = NOW(), updated_at = NOW()
                            WHERE seed_id = %s
                            """,
                            (seed_id,),
                        )
                    conn.commit()
                    summary["adopted"] += 1
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    release_session_conn(conn)
                continue

            _transition_best_effort(
                seed_id,
                from_state,
                "failed_retryable",
                retry_after=_utcnow() + timedelta(minutes=5),
                last_error="orphan reconciliation: stale seed after runner crash",
                last_error_class="orphaned_after_crash",
            )
            continue

        if from_state == "verifying":
            _transition_best_effort(seed_id, "verifying", "running")
            from_state = "running"

        if _transition_best_effort(seed_id, from_state, "queued", retry_after=None):
            summary["requeued"] += 1

    return summary


async def _update_heartbeat(seed_id: int) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_marathon_seeds
                SET heartbeat_at = NOW(), updated_at = NOW()
                WHERE seed_id = %s
                """,
                (int(seed_id),),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


async def wait_for_job_with_heartbeat(job_id: str, seed_id: int, timeout_min: int = 15) -> Outcome:
    started = _utcnow()
    deadline = started + timedelta(minutes=int(timeout_min))
    last_heartbeat = started

    while _utcnow() < deadline:
        now = _utcnow()

        if (now - last_heartbeat).total_seconds() >= 30.0:
            await _update_heartbeat(seed_id)
            last_heartbeat = now

        if job_id.startswith("mock_"):
            job = _MOCK_JOBS.get(job_id)
            if job is None:
                return Outcome(status="error", error="mock job not found")
            elapsed = (now - job["created_at"]).total_seconds()  # type: ignore[index]
            if elapsed >= 1.0:
                job["status"] = "completed"
            status = str(job.get("status") or "error")
            if status in {"completed", "saturated"}:
                return Outcome(
                    status=status,
                    total=int(job.get("total_unique_comps") or 0),
                    net_new=int(job.get("net_new_comps") or 0),
                )
            if status in {"error", "blocked", "stopped"}:
                return Outcome(status=status, error=str(job.get("last_error") or status))
            await asyncio.sleep(0.25)
            continue

        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, total_unique_comps, net_new_comps, last_error
                    FROM propelio_deep_pull_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release_session_conn(conn)

        if row is None:
            return Outcome(status="error", error="job missing")

        status = str(row[0] or "").strip().lower()
        total_unique = int(row[1] or 0)
        net_new = int(row[2] or 0)
        last_error = str(row[3] or "").strip() or None

        if status in {"completed", "saturated"}:
            return Outcome(status=status, total=total_unique, net_new=net_new)
        if status in {"error", "blocked", "stopped"}:
            return Outcome(status=status, error=last_error or status)

        await asyncio.sleep(5.0)

    return Outcome(status="timeout")


def handle_transient_failure(
    seed: dict[str, object],
    exc: object,
    *,
    error_class: str,
    from_state: str,
    retry_min: int = 5,
) -> str:
    seed_id = int(seed["seed_id"])
    runtime = _load_seed_state(seed_id) or {}
    attempts = int(runtime.get("attempts") or seed.get("attempts") or 0)
    max_attempts = int(runtime.get("max_attempts") or seed.get("max_attempts") or 5)
    current_state = str(runtime.get("status") or from_state)

    payload = {
        "last_error": _truncate_error(exc),
        "last_error_class": str(error_class or "unexpected")[:64],
    }

    if attempts >= max_attempts:
        _transition_best_effort(seed_id, current_state, "failed_final", **payload)
        emit_event(
            "seed_failed_final",
            campaign=seed.get("campaign_key"),
            seed_id=seed_id,
            error_class=payload["last_error_class"],
            attempts=attempts,
        )
        return "failed_final"

    delay_min = _retry_delay_minutes(int(retry_min), attempts)
    retry_after = _utcnow() + timedelta(minutes=delay_min)
    _transition_best_effort(
        seed_id,
        current_state,
        "failed_retryable",
        retry_after=retry_after,
        **payload,
    )
    emit_event(
        "seed_failed_retryable",
        campaign=seed.get("campaign_key"),
        seed_id=seed_id,
        error_class=payload["last_error_class"],
        retry_after_s=int(delay_min * 60),
        attempts=attempts,
    )
    return "failed_retryable"


def handle_terminal_failure(
    seed: dict[str, Any],
    error: object,
    *,
    error_class: str,
    from_state: str = "running",
) -> None:
    """Mark a seed as permanently skipped due to a non-retryable error.

    Unlike handle_transient_failure, this does not schedule a retry.
    """
    seed_id = int(seed["seed_id"])
    error_text = _truncate_error(error)
    campaign_key = str(seed.get("campaign_key") or "")

    _transition_best_effort(seed_id, from_state=from_state, to_state="skipped")

    # Gate the error-field update on actually being in skipped state to
    # avoid stamping a raced row that transitioned elsewhere.
    updated_rows = 0
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_marathon_seeds
                SET last_error = %s,
                    last_error_class = %s,
                    updated_at = NOW()
                WHERE seed_id = %s
                  AND status = 'skipped'
                """,
                (error_text, str(error_class or "unexpected")[:64], seed_id),
            )
            updated_rows = int(cur.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if updated_rows == 0:
        logger.warning(
            "[marathon] terminal failure stamp skipped - seed %s no longer in 'skipped' state "
            "(probable race). error_class=%s error=%s",
            seed_id,
            error_class,
            error_text,
        )

    emit_event(
        "seed_skipped_terminal",
        campaign=campaign_key,
        seed_id=seed_id,
        error_class=error_class,
        error=error_text,
    )


def _classify_remote_error_and_dispatch(
    seed: dict[str, Any],
    error: object,
    *,
    from_state: str = "running",
    retry_min: int | None = None,
) -> None:
    """Dispatch permanent remote errors to skipped, otherwise retryable.

    retry_min only forwards when explicitly set so None never reaches
    transient helper int() conversion.
    """
    text = str(error or "").lower()
    if "mls_coverage_error" in text or "we don't have coverage" in text:
        handle_terminal_failure(
            seed,
            error,
            error_class="no_coverage",
            from_state=from_state,
        )
    elif "no parcel match" in text or "suggest exact / close / fuzzy all returned no items" in text:
        handle_terminal_failure(
            seed,
            error,
            error_class="no_parcel_match",
            from_state=from_state,
        )
    else:
        kwargs: dict[str, Any] = {
            "error_class": "remote_error",
            "from_state": from_state,
        }
        if retry_min is not None:
            kwargs["retry_min"] = retry_min
        handle_transient_failure(seed, error, **kwargs)


async def verify_remote_state(
    seed: dict[str, object],
    *,
    mock: bool = False,
    wall_start_monotonic: float | None = None,
    wall_cap_seconds: int = 45 * 60,
    verify_polls: int = 3,
    verify_sleep_seconds: float = 60.0,
) -> Outcome:
    seed_id = int(seed["seed_id"])
    job_id = str(seed.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError(f"seed_id={seed_id} missing job_id for verify_remote_state")

    loop = asyncio.get_running_loop()
    if wall_start_monotonic is None:
        wall_start_monotonic = loop.time()

    def over_cap() -> bool:
        return (loop.time() - float(wall_start_monotonic)) >= int(wall_cap_seconds)

    _transition_best_effort(seed_id, str(seed.get("status") or "running"), "verifying")

    for poll_idx in range(int(verify_polls)):
        if over_cap():
            handle_transient_failure(
                seed,
                "verify_remote_state hard timeout",
                error_class="hard_timeout",
                from_state="verifying",
            )
            return Outcome(status="timeout", error="hard_timeout")

        if mock:
            job = _MOCK_JOBS.get(job_id)
            if job is None:
                _classify_remote_error_and_dispatch(
                    seed,
                    "mock job not found in verify_remote_state",
                    from_state="verifying",
                )
                return Outcome(status="error", error="mock job not found")
            remote_status = str(job.get("status") or "running")
            total_unique = int(job.get("total_unique_comps") or 0)
            net_new = int(job.get("net_new_comps") or 0)
            last_error = _truncate_error(job.get("last_error") or remote_status)
        else:
            snapshot = _fetch_job_snapshot(job_id)
            if snapshot is None:
                _classify_remote_error_and_dispatch(
                    seed,
                    f"job missing during verify_remote_state: {job_id}",
                    from_state="verifying",
                )
                return Outcome(status="error", error="job missing")

            remote_status = str(snapshot.get("status") or "").strip().lower()
            total_unique = int(snapshot.get("total_unique_comps") or 0)
            net_new = int(snapshot.get("net_new_comps") or 0)
            last_error = _truncate_error(snapshot.get("last_error") or remote_status)

        if remote_status in {"completed", "saturated"}:
            _transition_best_effort(
                seed_id,
                "verifying",
                "completed",
                comps_captured=total_unique,
                net_new_comps=net_new,
            )
            return Outcome(status=remote_status, total=total_unique, net_new=net_new)

        if remote_status == "error":
            _classify_remote_error_and_dispatch(
                seed,
                last_error,
                from_state="verifying",
            )
            return Outcome(status="error", error=last_error)

        if remote_status == "stopped":
            handle_transient_failure(
                seed,
                last_error,
                error_class="remote_stopped",
                from_state="verifying",
                retry_min=5,
            )
            return Outcome(status="stopped", error=last_error)

        if remote_status == "blocked":
            raise PropelioAuthError(last_error or "remote blocked")

        await _update_heartbeat(seed_id)
        if poll_idx < int(verify_polls) - 1:
            await asyncio.sleep(float(verify_sleep_seconds))

    if over_cap():
        handle_transient_failure(
            seed,
            "verify_remote_state hard timeout after polls",
            error_class="hard_timeout",
            from_state="verifying",
        )
        return Outcome(status="timeout", error="hard_timeout")

    _transition_best_effort(seed_id, "verifying", "running")
    outcome = await wait_for_job_with_heartbeat(job_id, seed_id, timeout_min=15)

    if outcome.status in {"completed", "saturated"}:
        _transition_best_effort(
            seed_id,
            "running",
            "completed",
            comps_captured=int(outcome.total or 0),
            net_new_comps=int(outcome.net_new or 0),
        )
        return outcome

    if outcome.status == "timeout":
        if over_cap():
            handle_transient_failure(
                seed,
                "verify_remote_state hard timeout after second heartbeat window",
                error_class="hard_timeout",
                from_state="running",
            )
            return Outcome(status="timeout", error="hard_timeout")
        return await verify_remote_state(
            {
                **seed,
                "status": "running",
                "job_id": job_id,
            },
            mock=mock,
            wall_start_monotonic=wall_start_monotonic,
            wall_cap_seconds=wall_cap_seconds,
            verify_polls=verify_polls,
            verify_sleep_seconds=verify_sleep_seconds,
        )

    if outcome.status == "stopped":
        handle_transient_failure(
            seed,
            outcome.error or "stopped",
            error_class="remote_stopped",
            from_state="running",
            retry_min=5,
        )
        return outcome

    if outcome.status == "blocked":
        raise PropelioAuthError(outcome.error or "remote blocked")

    _classify_remote_error_and_dispatch(
        seed,
        outcome.error or outcome.status,
        from_state="running",
    )
    return outcome


async def run_campaign(
    campaign_key: str,
    runner_id: str,
    *,
    mock: bool = False,
    max_seeds: int | None = None,
) -> int:
    global _current_seed, _run_end_reason

    _current_seed = None
    _run_end_reason = None
    setup_sigint_handler()

    campaign = _load_campaign(campaign_key)
    if campaign is None:
        raise ValueError(f"campaign not found: {campaign_key}")

    campaign_id = int(campaign["campaign_id"])
    campaign_name = str(campaign.get("campaign_key") or campaign_key)
    breaker = CircuitBreaker.load(campaign_id)

    emit_event(
        "run_start",
        campaign=campaign_name,
        runner_id=runner_id,
        mock=bool(mock),
        max_seeds=max_seeds,
    )

    orphan_summary = reconcile_orphans(campaign_id)
    print(
        "[marathon-runner] "
        f"orphans_reconciled count={orphan_summary['count']} "
        f"completed={orphan_summary['completed']} "
        f"adopted={orphan_summary['adopted']} "
        f"requeued={orphan_summary['requeued']}"
    )
    emit_event(
        "orphans_reconciled",
        campaign=campaign_name,
        count=orphan_summary["count"],
        completed=orphan_summary["completed"],
        adopted=orphan_summary["adopted"],
        requeued=orphan_summary["requeued"],
    )

    session_start = asyncio.get_running_loop().time()
    last_break_at = session_start
    seeds_processed = 0
    breaker_was_open = False

    try:
        while True:
            if breaker.is_open():
                breaker_was_open = True
                await wait_for_cooldown_or_exit(run_id=campaign_id, circuit_breaker=breaker)
                continue
            if breaker_was_open:
                emit_event("breaker_reset", campaign=campaign_name)
                breaker_was_open = False

            seed = claim_next_seed(campaign_id, runner_id)
            if seed is None:
                _run_end_reason = "completed"
                print(f"campaign={campaign_key} done — no claimable seeds remaining")
                break

            _current_seed = dict(seed)
            seed = {**seed, "campaign_key": campaign_name}

            seed_id = int(seed["seed_id"])
            seed_started = asyncio.get_running_loop().time()
            emit_event(
                "seed_claim",
                campaign=campaign_name,
                seed_id=seed_id,
                address=seed.get("seed_address"),
                density=seed.get("density_class"),
                attempts=seed.get("attempts"),
            )

            try:
                job_id = await start_deep_pull_for_seed(seed, mock=mock)
                conn = get_session_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE propelio_marathon_seeds
                            SET job_id = %s, updated_at = NOW()
                            WHERE seed_id = %s
                            """,
                            (job_id, seed_id),
                        )
                    conn.commit()
                    _current_seed = {**seed, "job_id": job_id}
                    seed = {**seed, "job_id": job_id}
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    release_session_conn(conn)

                outcome = await wait_for_job_with_heartbeat(job_id, seed_id)
                if outcome.status in {"completed", "saturated"}:
                    _transition_best_effort(
                        seed_id,
                        "running",
                        "completed",
                        comps_captured=int(outcome.total or 0),
                        net_new_comps=int(outcome.net_new or 0),
                    )
                    emit_event(
                        "seed_completed",
                        campaign=campaign_name,
                        seed_id=seed_id,
                        comps=int(outcome.total or 0),
                        net_new=int(outcome.net_new or 0),
                        duration_s=round(asyncio.get_running_loop().time() - seed_started, 2),
                    )
                    breaker.record_outcome("ok")
                elif outcome.status == "timeout":
                    verified = await verify_remote_state(
                        {
                            **seed,
                            "seed_id": seed_id,
                            "job_id": job_id,
                            "status": "running",
                        },
                        mock=mock,
                    )
                    if verified.status in {"completed", "saturated"}:
                        emit_event(
                            "seed_completed",
                            campaign=campaign_name,
                            seed_id=seed_id,
                            comps=int(verified.total or 0),
                            net_new=int(verified.net_new or 0),
                            duration_s=round(asyncio.get_running_loop().time() - seed_started, 2),
                        )
                        breaker.record_outcome("ok")
                    elif verified.status == "blocked":
                        raise PropelioAuthError(verified.error or "remote blocked")
                    else:
                        breaker.record_outcome("error")
                elif outcome.status == "blocked":
                    raise PropelioAuthError(outcome.error or "remote blocked")
                elif outcome.status == "stopped":
                    handle_transient_failure(
                        seed,
                        outcome.error or "stopped",
                        error_class="remote_stopped",
                        from_state="running",
                        retry_min=5,
                    )
                    breaker.record_outcome("error")
                else:
                    _classify_remote_error_and_dispatch(
                        seed,
                        outcome.error or outcome.status,
                        from_state="running",
                    )
                    breaker.record_outcome("error")
            except PropelioAuthError as exc:
                _run_end_reason = "auth_block"
                handle_transient_failure(
                    seed,
                    exc,
                    error_class="auth_block",
                    from_state="running",
                )
                alert(
                    "CRITICAL",
                    "auth block detected",
                    campaign=campaign_name,
                    seed_id=seed_id,
                    runner_id=runner_id,
                )
                raise
            except PropelioRateLimitError as exc:
                breaker.trip("rate_limit", cooldown_min=30)
                emit_event("breaker_trip", campaign=campaign_name, reason="rate_limit", cooldown_min=30)
                alert(
                    "WARNING",
                    "circuit breaker tripped",
                    campaign=campaign_name,
                    reason="rate_limit",
                    cooldown_min=30,
                )
                handle_transient_failure(
                    seed,
                    exc,
                    error_class="rate_limit",
                    from_state="running",
                    retry_min=30,
                )
                breaker.record_outcome("rate_limit")
            except (asyncio.TimeoutError, TimeoutError, ConnectionError, socket.timeout, OSError) as exc:
                handle_transient_failure(
                    seed,
                    exc,
                    error_class="network",
                    from_state="running",
                )
                breaker.record_outcome("error")
            except Exception as exc:
                handle_transient_failure(
                    seed,
                    exc,
                    error_class="unexpected",
                    from_state="running",
                )
                breaker.record_outcome("error")

            seeds_processed += 1

            if max_seeds is not None and seeds_processed >= int(max_seeds):
                _run_end_reason = "max_seeds_reached"
                print(f"[marathon-runner] max_seeds={max_seeds} reached, exiting clean")
                break

            if not mock:
                pause_s = inter_seed_pause_seconds()
                emit_event("inter_seed_pause", campaign=campaign_name, duration_s=round(pause_s, 2))
                await asyncio.sleep(pause_s)
                elapsed_since_last_break = asyncio.get_running_loop().time() - last_break_at
                brk = maybe_take_break(elapsed_since_last_break)
                if brk:
                    duration, kind = brk
                    print(f"[marathon-runner] {kind}_break {duration:.0f}s")
                    emit_event(
                        "break_started",
                        campaign=campaign_name,
                        kind=kind,
                        duration_s=round(float(duration), 2),
                    )
                    await asyncio.sleep(duration)
                    last_break_at = asyncio.get_running_loop().time()

            _current_seed = None
    finally:
        _current_seed = None
        if _run_end_reason is None:
            _run_end_reason = "completed"
        emit_event(
            "run_end",
            campaign=campaign_name,
            run_end_reason=_run_end_reason,
            seeds_processed=seeds_processed,
            runner_id=runner_id,
        )
        if _run_end_reason == "auth_block":
            alert("CRITICAL", "run ended on auth block", campaign=campaign_name, runner_id=runner_id)

    return seeds_processed


def status_campaign(campaign_key: str) -> dict[str, int]:
    campaign = _load_campaign(campaign_key)
    if campaign is None:
        raise ValueError(f"campaign not found: {campaign_key}")

    campaign_id = int(campaign["campaign_id"])

    counts = {
        "queued": 0,
        "running": 0,
        "completed": 0,
        "verifying": 0,
        "stopping_requested": 0,
        "failed_retryable": 0,
        "failed_final": 0,
        "skipped": 0,
    }

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status::text, COUNT(*)
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                GROUP BY status
                """,
                (campaign_id,),
            )
            rows = cur.fetchall() or []

            cur.execute(
                """
                SELECT
                    COALESCE(SUM(comps_captured), 0),
                    COALESCE(SUM(net_new_comps), 0)
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                  AND status = 'completed'
                """,
                (campaign_id,),
            )
            totals_row = cur.fetchone() or (0, 0)

            cur.execute(
                """
                SELECT MIN(retry_after)
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                  AND status = 'failed_retryable'
                  AND retry_after IS NOT NULL
                """,
                (campaign_id,),
            )
            retry_row = cur.fetchone()

            cur.execute(
                """
                SELECT updated_at
                FROM propelio_marathon_campaigns
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            campaign_row = cur.fetchone()

            cur.execute(
                """
                SELECT
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (completed_at - running_at))
                    ) AS p50_s,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (completed_at - running_at))
                    ) AS p95_s
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                  AND status = 'completed'
                  AND running_at IS NOT NULL
                  AND completed_at IS NOT NULL
                """,
                (campaign_id,),
            )
            duration_row = cur.fetchone() or (None, None)

            cur.execute(
                """
                SELECT COALESCE(SUM(attempts), 0)
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            attempts_row = cur.fetchone() or (0,)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    total = 0
    for status, count in rows:
        key = str(status)
        value = int(count or 0)
        total += value
        if key in counts:
            counts[key] = value

    completed = int(counts["completed"])
    completed_pct = (100.0 * completed / total) if total else 0.0
    total_comps = int((totals_row[0] or 0))
    total_net_new = int((totals_row[1] or 0))
    avg_per_seed = (total_comps / completed) if completed else 0.0
    p50_duration_s = duration_row[0]
    p95_duration_s = duration_row[1]

    total_attempts = int(attempts_row[0] or 0)
    failure_count = int(counts["failed_retryable"] + counts["failed_final"])
    error_rate_pct = (100.0 * failure_count / total_attempts) if total_attempts else None

    net_new_ratio_pct = (100.0 * total_net_new / total_comps) if total_comps else None

    # Deferred KPI notes for v3:
    # - Cap-hit rate requires per-pass telemetry (not available in seed-level totals).
    # - Parcel match rate needs matched/unmatched comp counters.
    # - Duplicate rate is derivable as 1 - net_new_ratio.

    next_retry_at = retry_row[0] if retry_row else None
    next_retry_label = "-"
    if next_retry_at is not None:
        next_retry_label = next_retry_at.astimezone(timezone.utc).strftime("%H:%M")

    breaker = CircuitBreaker.load(campaign_id)
    if breaker.is_open() and breaker.cooldown_until is not None:
        breaker_text = f"open (cooldown until {breaker.cooldown_until.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"
    else:
        breaker_text = "closed"

    last_update = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if campaign_row and campaign_row[0] is not None:
        last_update = campaign_row[0].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"Campaign {campaign_key} - Status")
    print("========================")
    print(f"Total seeds:      {total:>5}")
    print(f"Completed:        {completed:>5}  ({completed_pct:.0f}%)")
    print(f"Failed:           {counts['failed_final']:>5}")
    print(f"Failed retryable: {counts['failed_retryable']:>5} (next retry at {next_retry_label})")
    print(f"Queued:           {counts['queued']:>5}")
    print(f"Verifying:        {counts['verifying']:>5}")
    print(f"Stopping:         {counts['stopping_requested']:>5}")
    print("")
    print(f"Comps captured:   {total_comps:,} total, {total_net_new:,} net-new")
    print(f"Avg per seed:     {avg_per_seed:.0f} comps (over {completed} completed)")
    if p50_duration_s is not None and p95_duration_s is not None and completed > 0:
        print(
            "Pull duration:    "
            f"p50={int(round(float(p50_duration_s)))}s  "
            f"p95={int(round(float(p95_duration_s)))}s  "
            f"(over {completed} completed)"
        )
    else:
        print("Pull duration:    n/a")

    if net_new_ratio_pct is not None:
        print(
            "Net-new ratio:    "
            f"{net_new_ratio_pct:.0f}%       "
            f"({total_net_new} net-new of {total_comps} captured)"
        )
    else:
        print("Net-new ratio:    n/a")

    if error_rate_pct is not None:
        print(
            "Error rate:       "
            f"{error_rate_pct:.1f}%      "
            f"({failure_count} failures over {total_attempts} attempts)"
        )
    else:
        print("Error rate:       n/a")
    print("")
    print(f"Circuit breaker:   {breaker_text}")
    print(f"Last update:      {last_update}")

    return {"total": total, **counts}


_VALID_SKIP_FROM_STATES = frozenset({"queued", "failed_retryable", "failed_final"})


def operator_skip_seed(seed_id: int, reason: str | None = None) -> None:
    seed = _load_seed_state(int(seed_id))
    if not seed:
        raise ValueError(f"seed not found: {seed_id}")

    from_state = str(seed.get("status") or "").strip()
    if from_state == "skipped":
        return

    if not from_state:
        raise ValueError(f"seed has empty status: {seed_id}")

    if from_state not in _VALID_SKIP_FROM_STATES:
        raise ValueError(
            f"cannot skip seed in state '{from_state}'. Stop the runner first "
            "(Ctrl+C) and let the seed settle to queued/failed_retryable/"
            "failed_final, then retry. Orphaned active states are auto-reconciled "
            "on next runner startup."
        )

    transition(
        int(seed_id),
        from_state,
        "skipped",
        last_error=(str(reason or "operator skip")[:500]),
        last_error_class="operator_skip",
    )


def operator_requeue_seed(seed_id: int) -> None:
    seed = _load_seed_state(int(seed_id))
    if not seed:
        raise ValueError(f"seed not found: {seed_id}")

    from_state = str(seed.get("status") or "").strip()
    if from_state != "failed_final":
        raise ValueError(f"seed must be failed_final to requeue, got: {from_state}")

    transition(
        int(seed_id),
        "failed_final",
        "queued",
        attempts=0,
        retry_after=None,
        last_error=None,
        last_error_class=None,
    )
