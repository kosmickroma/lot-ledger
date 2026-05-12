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
from .circuit_breaker import CircuitBreaker
from .cooldown import wait_for_cooldown_or_exit
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
                      AND (
                        status = 'queued'
                        OR (
                            status = 'failed_retryable'
                            AND (retry_after IS NULL OR retry_after <= NOW())
                        )
                      )
                    ORDER BY RANDOM()
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
    return "failed_retryable"


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
                handle_transient_failure(
                    seed,
                    "mock job not found in verify_remote_state",
                    error_class="remote_error",
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
                handle_transient_failure(
                    seed,
                    f"job missing during verify_remote_state: {job_id}",
                    error_class="remote_error",
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
            handle_transient_failure(
                seed,
                last_error,
                error_class="remote_error",
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

    handle_transient_failure(
        seed,
        outcome.error or outcome.status,
        error_class="remote_error",
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
    breaker = CircuitBreaker.load(campaign_id)

    orphan_summary = reconcile_orphans(campaign_id)
    print(
        "[marathon-runner] "
        f"orphans_reconciled count={orphan_summary['count']} "
        f"completed={orphan_summary['completed']} "
        f"adopted={orphan_summary['adopted']} "
        f"requeued={orphan_summary['requeued']}"
    )

    session_start = asyncio.get_running_loop().time()
    last_break_at = session_start
    seeds_processed = 0

    while True:
        if breaker.is_open():
            await wait_for_cooldown_or_exit(run_id=campaign_id, circuit_breaker=breaker)
            continue

        seed = claim_next_seed(campaign_id, runner_id)
        if seed is None:
            _run_end_reason = "completed"
            print(f"campaign={campaign_key} done — no claimable seeds remaining")
            break

        _current_seed = dict(seed)

        seed_id = int(seed["seed_id"])

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
                handle_transient_failure(
                    seed,
                    outcome.error or outcome.status,
                    error_class="remote_error",
                    from_state="running",
                )
                breaker.record_outcome("error")
        except PropelioAuthError:
            _run_end_reason = "auth_block"
            raise
        except PropelioRateLimitError as exc:
            breaker.trip("rate_limit", cooldown_min=30)
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
            await asyncio.sleep(inter_seed_pause_seconds())
            elapsed_since_last_break = asyncio.get_running_loop().time() - last_break_at
            brk = maybe_take_break(elapsed_since_last_break)
            if brk:
                duration, kind = brk
                print(f"[marathon-runner] {kind}_break {duration:.0f}s")
                await asyncio.sleep(duration)
                last_break_at = asyncio.get_running_loop().time()

        _current_seed = None

    _current_seed = None
    if _run_end_reason is None:
        _run_end_reason = "completed"
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

    print(f"campaign={campaign_key}")
    print(
        "  "
        f"total={total}  queued={counts['queued']}  running={counts['running']}  completed={counts['completed']}  "
        f"verifying={counts['verifying']}  stopping_requested={counts['stopping_requested']}"
    )
    print(
        "  "
        f"failed_retryable={counts['failed_retryable']}  failed_final={counts['failed_final']}  skipped={counts['skipped']}"
    )

    return {"total": total, **counts}
