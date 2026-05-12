# api/propelio/deep_pull.py
#
# Experimental multi-pass Propelio deep-pull runner and session-DB persistence.
# Executes jittered, sequential CMA passes and records per-pass comp snapshots.
#
# Connects to:
#   api/config.py             - session DB connection helpers
#   api/propelio/scraper.py   - PropelioClient and CMA calls
#   api/propelio/archive.py   - _comp_address_key dedup key helper
#   api/propelio/config.py    - Propelio credentials from env

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn
from api.propelio.archive import _comp_address_key, merge_comps_into_global  # intentional experimental reuse — see spec
from api.propelio.config import PROPELIO_PASSWORD, PROPELIO_USERNAME
from api.propelio.scraper import PropelioClient, _parse_property
from dataclasses import asdict


logger = logging.getLogger(__name__)

PASSES = [
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5, "label": "blocks"},
    {"months": 24, "range_mi": 1.0, "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0, "label": "broader"},
    {"months": 24, "range_mi": 5.0, "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]

SATURATION_MIN_PASSES = 3
SATURATION_THRESHOLD = 0.05
STALE_JOB_THRESHOLD_MINUTES = 5


def _jittered_pass_sleep_seconds() -> float:
    """Inter-pass sleep with heavy-tailed mixed distribution.

    80% of pauses fall in the 'normal' band (15-45s — quick filter tweaks,
    matches typical analyst cadence). 20% are 'distracted' (45-120s — got a
    call, took a break, walked over to a colleague). The two-band shape
    avoids creating a uniform-distribution fingerprint that pattern-detection
    systems (Cloudflare, etc.) could profile against. Average gap ~37s, but
    individual runs vary widely.
    """
    if random.random() < 0.8:
        return random.uniform(15, 45)
    return random.uniform(45, 120)


def _classify_propelio_error(exc: Exception) -> str:
    """Return blocked for 401/403/429-like errors, else error."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    if status_code in (401, 403, 429):
        return "blocked"

    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "throttle" in msg or "too many" in msg:
        return "blocked"
    if "401" in msg or "403" in msg or "unauthor" in msg or "forbidden" in msg:
        return "blocked"
    return "error"


def _claim_job_for_running(job_id: str) -> str | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET status = 'running',
                    next_pass_at = NULL,
                    last_error = NULL
                WHERE job_id = %s
                  AND status = 'queued'
                RETURNING target_address
                """,
                (job_id,),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return str(row[0] or "").strip() or None
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _update_job_ids(job_id: str, *, lead_id: str, cma_id: str) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET lead_id = %s,
                    cma_id = %s
                WHERE job_id = %s
                """,
                (str(lead_id or "").strip(), str(cma_id or "").strip(), job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _set_next_pass_at(job_id: str, seconds_from_now: float) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET next_pass_at = NOW() + (%s * INTERVAL '1 second')
                WHERE job_id = %s
                """,
                (float(seconds_from_now), job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _is_stop_requested(job_id: str) -> bool:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT stop_requested
                FROM propelio_deep_pull_jobs
                WHERE job_id = %s
                LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
        return bool(row[0]) if row is not None else True
    finally:
        release_session_conn(conn)


def _get_job_status(job_id: str) -> str | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM propelio_deep_pull_jobs
                WHERE job_id = %s
                LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return str(row[0] or "").strip().lower() or None
    finally:
        release_session_conn(conn)


def _insert_pass_comps(job_id: str, pass_num: int, pass_config: dict[str, Any], comps: list[dict[str, Any]]) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT comp_address_key
                FROM propelio_deep_pull_experiment
                WHERE job_id = %s
                """,
                (job_id,),
            )
            seen_keys = {str(r[0]) for r in (cur.fetchall() or []) if r and r[0]}

            months = int(pass_config.get("months") or 24)
            range_mi = float(pass_config.get("range_mi") or 0.0)
            pass_label = str(pass_config.get("label") or "").strip() or None

            for comp in comps or []:
                if not isinstance(comp, dict):
                    continue
                comp_key = _comp_address_key(comp)
                if not comp_key:
                    continue

                is_first_seen = comp_key not in seen_keys
                seen_keys.add(comp_key)

                cur.execute(
                    """
                    INSERT INTO propelio_deep_pull_experiment (
                        job_id,
                        pass_num,
                        months,
                        range_mi,
                        pass_label,
                        comp_address_key,
                        comp_data,
                        is_first_seen_in_job
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, pass_num, comp_address_key) DO NOTHING
                    """,
                    (
                        job_id,
                        int(pass_num),
                        months,
                        range_mi,
                        pass_label,
                        comp_key,
                        Json(comp),
                        bool(is_first_seen),
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    # Phase 2 Chunk 2: parallel write to global propelio_comps table (non-fatal)
    try:
        parsed_for_global = []
        for raw_comp in comps or []:
            if not isinstance(raw_comp, dict):
                continue
            try:
                parsed_for_global.append(asdict(_parse_property(raw_comp)))
            except Exception:
                continue
        if parsed_for_global:
            merge_comps_into_global(parsed_for_global, source="deep_pull")
    except Exception as _exc:
        logger.warning("[deep-pull job=%s] global write failed (non-fatal): %s", job_id, _exc)


def _refresh_job_counts(job_id: str, pass_num: int) -> dict[str, int]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE pass_num = %s) AS returned,
                    COUNT(*) FILTER (WHERE pass_num = %s AND is_first_seen_in_job) AS new_comps,
                    COUNT(DISTINCT comp_address_key) AS total_unique
                FROM propelio_deep_pull_experiment
                WHERE job_id = %s
                """,
                (int(pass_num), int(pass_num), job_id),
            )
            row = cur.fetchone() or (0, 0, 0)
            returned = int(row[0] or 0)
            new_comps = int(row[1] or 0)
            total_unique = int(row[2] or 0)

            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET passes_completed = GREATEST(passes_completed, %s),
                    total_unique_comps = %s,
                    last_pass_at = NOW(),
                    next_pass_at = NULL
                WHERE job_id = %s
                """,
                (int(pass_num), total_unique, job_id),
            )
        conn.commit()
        return {"returned": returned, "new": new_comps, "total_unique": total_unique}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _mark_job_status(job_id: str, status: str, error_msg: str | None = None) -> None:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"queued", "running", "completed", "stopped", "error", "saturated", "blocked"}:
        raise ValueError(f"invalid deep-pull status: {status!r}")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET status = %s,
                    last_error = %s,
                    last_pass_at = NOW(),
                    next_pass_at = NULL,
                    stop_requested = CASE WHEN %s = 'stopped' THEN TRUE ELSE stop_requested END
                WHERE job_id = %s
                """,
                (normalized_status, (str(error_msg)[:500] if error_msg else None), normalized_status, job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _check_saturation(counts: dict[str, int], pass_num: int) -> bool:
    if int(pass_num) < SATURATION_MIN_PASSES:
        return False
    total_unique = max(int(counts.get("total_unique", 0) or 0), 1)
    new_count = int(counts.get("new", 0) or 0)
    return (new_count / total_unique) < SATURATION_THRESHOLD


def _extract_cma_id(envelope: dict[str, Any]) -> str:
    if not isinstance(envelope, dict):
        raise ValueError(f"add_cma envelope is not a dict: type={type(envelope)}")
    cma_id = str(envelope.get("id") or "").strip()
    if not cma_id:
        raise ValueError(f"could not extract cma_id from envelope keys={list(envelope.keys())}")
    return cma_id


def _parse_cma_envelope_comps(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        return []
    data = envelope.get("data")
    if not isinstance(data, dict):
        return []
    sales = data.get("sales")
    if not isinstance(sales, list):
        return []
    return [item for item in sales if isinstance(item, dict)]


async def run_deep_pull(job_id: str) -> None:
    """Detached worker that executes multi-pass deep pull for one job."""
    try:
        target_address = _claim_job_for_running(job_id)
        if target_address is None:
            return

        client = PropelioClient(username=PROPELIO_USERNAME, password=PROPELIO_PASSWORD)
        await asyncio.to_thread(client.login)

        if _is_stop_requested(job_id):
            current_status = _get_job_status(job_id)
            if current_status not in {"error", "blocked", "completed", "saturated", "stopped"}:
                _mark_job_status(job_id, "stopped")
            return

        first_cfg = PASSES[0]
        logger.info(
            "[deep-pull job=%s] Pass 1: months=%d range=%.2fmi label=%s",
            job_id,
            first_cfg["months"],
            first_cfg["range_mi"],
            first_cfg["label"],
        )

        lead_id, subject_lot_sqft, parcel_bundle = await asyncio.to_thread(client.find_lead_id, target_address)
        confirmation_key = parcel_bundle.get("confirmation_key") if isinstance(parcel_bundle, dict) else None
        logger.info(
            "[deep-pull job=%s] lead resolved: lead_id=%s subject_lot=%s has_confirmation_key=%s",
            job_id,
            lead_id,
            subject_lot_sqft,
            bool(confirmation_key),
        )

        cma_envelope = await asyncio.to_thread(
            client.add_cma,
            lead_id,
            confirmation_key,
            months=first_cfg["months"],
            range_mi=first_cfg["range_mi"],
        )
        cma_id = _extract_cma_id(cma_envelope)
        _update_job_ids(job_id, lead_id=str(lead_id), cma_id=cma_id)

        comps = _parse_cma_envelope_comps(cma_envelope)
        _insert_pass_comps(job_id, pass_num=1, pass_config=first_cfg, comps=comps)
        counts = _refresh_job_counts(job_id, pass_num=1)
        logger.info(
            "[deep-pull job=%s] Pass 1 done: returned=%d new=%d total_unique=%d",
            job_id,
            counts["returned"],
            counts["new"],
            counts["total_unique"],
        )

        if _check_saturation(counts, pass_num=1):
            _mark_job_status(job_id, "saturated")
            return

        for pass_num in range(2, len(PASSES) + 1):
            if _is_stop_requested(job_id):
                current_status = _get_job_status(job_id)
                if current_status not in {"error", "blocked", "completed", "saturated", "stopped"}:
                    _mark_job_status(job_id, "stopped")
                return

            sleep_s = _jittered_pass_sleep_seconds()
            _set_next_pass_at(job_id, sleep_s)
            logger.info("[deep-pull job=%s] sleeping %.1fs before pass %d", job_id, sleep_s, pass_num)
            await asyncio.sleep(sleep_s)

            if _is_stop_requested(job_id):
                current_status = _get_job_status(job_id)
                if current_status not in {"error", "blocked", "completed", "saturated", "stopped"}:
                    _mark_job_status(job_id, "stopped")
                return

            cfg = PASSES[pass_num - 1]
            logger.info(
                "[deep-pull job=%s] Pass %d: months=%d range=%.2fmi label=%s",
                job_id,
                pass_num,
                cfg["months"],
                cfg["range_mi"],
                cfg["label"],
            )
            cma_response = await asyncio.to_thread(
                client.search_cma,
                lead_id,
                cma_id,
                months=cfg["months"],
                range_mi=cfg["range_mi"],
            )
            comps = _parse_cma_envelope_comps(cma_response)
            _insert_pass_comps(job_id, pass_num=pass_num, pass_config=cfg, comps=comps)
            counts = _refresh_job_counts(job_id, pass_num=pass_num)
            logger.info(
                "[deep-pull job=%s] Pass %d done: returned=%d new=%d total_unique=%d",
                job_id,
                pass_num,
                counts["returned"],
                counts["new"],
                counts["total_unique"],
            )

            if _check_saturation(counts, pass_num=pass_num):
                logger.info("[deep-pull job=%s] saturated after pass %d", job_id, pass_num)
                _mark_job_status(job_id, "saturated")
                return

        _mark_job_status(job_id, "completed")
        logger.info("[deep-pull job=%s] completed all %d passes", job_id, len(PASSES))

    except Exception as exc:
        terminal_status = _classify_propelio_error(exc)
        logger.exception("[deep-pull job=%s] terminating with status=%s", job_id, terminal_status)
        try:
            _mark_job_status(job_id, terminal_status, error_msg=str(exc)[:500])
        except Exception as inner:
            logger.exception("[deep-pull job=%s] FAILED to persist terminal status: %s", job_id, inner)
