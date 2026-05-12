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
#   api.propelio.deep_pull.run_deep_pull        - real pull worker (non-mock path)

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import random
import socket
import uuid

from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from api.config import get_session_conn, release_session_conn
from .circuit_breaker import CircuitBreaker
from .cooldown import wait_for_cooldown_or_exit
from .pacing import inter_seed_pause_seconds, maybe_take_break
from .state import transition


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

    asyncio.create_task(run_deep_pull(job_id))
    return job_id


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
                return Outcome(status="error", error=str(job.get("last_error") or status))
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
            return Outcome(status="error", error=last_error or status)

        await asyncio.sleep(5.0)

    return Outcome(status="timeout")


def _set_retryable(seed_id: int, from_state: str, error_msg: str, error_class: str) -> None:
    retry_after = _utcnow() + timedelta(minutes=5)
    transition(
        int(seed_id),
        str(from_state),
        "failed_retryable",
        retry_after=retry_after,
        last_error=str(error_msg or "")[:500],
        last_error_class=str(error_class or "runner_error")[:64],
    )


async def run_campaign(
    campaign_key: str,
    runner_id: str,
    *,
    mock: bool = False,
    max_seeds: int | None = None,
) -> None:
    campaign = _load_campaign(campaign_key)
    if campaign is None:
        raise ValueError(f"campaign not found: {campaign_key}")

    campaign_id = int(campaign["campaign_id"])
    breaker = CircuitBreaker.load(campaign_id)

    session_start = asyncio.get_running_loop().time()
    last_break_at = session_start
    seeds_processed = 0

    while True:
        if breaker.is_open():
            await wait_for_cooldown_or_exit(run_id=campaign_id, circuit_breaker=breaker)
            continue

        seed = claim_next_seed(campaign_id, runner_id)
        if seed is None:
            print(f"campaign={campaign_key} done — no claimable seeds remaining")
            break

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
            except Exception:
                conn.rollback()
                raise
            finally:
                release_session_conn(conn)

            outcome = await wait_for_job_with_heartbeat(job_id, seed_id)
            if outcome.status in {"completed", "saturated"}:
                transition(
                    seed_id,
                    "running",
                    "completed",
                    comps_captured=int(outcome.total or 0),
                    net_new_comps=int(outcome.net_new or 0),
                )
                breaker.record_outcome("ok")
            else:
                _set_retryable(seed_id, "running", outcome.error or outcome.status, "runner_error")
                breaker.record_outcome("error")
        except Exception as exc:
            try:
                _set_retryable(seed_id, "running", str(exc), "unexpected")
            except Exception:
                pass
            breaker.record_outcome("error")

        seeds_processed += 1

        if max_seeds is not None and seeds_processed >= int(max_seeds):
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
