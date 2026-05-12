# scripts/marathon_campaign/smoke_phase6_observability.py
#
# Role: Phase 6 smoke test for structured events, status formatting, and alert routing.
#
# Connects to:
#   api/main.py                          - ensures schema exists
#   api/config.py                        - session DB helpers
#   scripts/marathon_campaign/runner.py  - run_campaign and status_campaign observability
#   scripts/marathon_campaign/alerts.py  - alert routing behavior

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.alerts import alert
from scripts.marathon_campaign.runner import run_campaign, status_campaign


def _seed_campaign(campaign_key: str, status: str = "running") -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_marathon_campaigns (campaign_key, status, started_at, updated_at)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (campaign_key) DO UPDATE SET updated_at = NOW()
                RETURNING campaign_id
                """,
                (campaign_key, status),
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
    attempts: int = 0,
    max_attempts: int = 5,
    comps_captured: int | None = None,
    net_new_comps: int | None = None,
    retry_after_expr: str | None = None,
) -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            retry_clause = "NULL" if retry_after_expr is None else retry_after_expr
            cur.execute(
                f"""
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
                    retry_after,
                    comps_captured,
                    net_new_comps,
                    queued_at,
                    running_at,
                    completed_at,
                    failed_final_at,
                    last_transition_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'dcad',
                    32.721,
                    -96.921,
                    %s,
                    32.721,
                    -96.921,
                    'suburban',
                    360,
                    %s::propelio_marathon_seed_state,
                    %s,
                    %s,
                    {retry_clause},
                    %s,
                    %s,
                    NOW(),
                    CASE WHEN %s = 'running' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'completed' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'failed_final' THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"phase6_{account_suffix}_{uuid4().hex[:8]}",
                    f"{300 + (len(account_suffix) % 50)} Observability Way",
                    status,
                    int(attempts),
                    int(max_attempts),
                    comps_captured,
                    net_new_comps,
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


def _seed_mock_run_campaign(campaign_key: str) -> int:
    campaign_id = _seed_campaign(campaign_key, status="running")
    for idx in range(2):
        _insert_seed(
            campaign_id,
            account_suffix=f"mock_{idx}",
            status="queued",
            attempts=0,
            max_attempts=5,
        )
    return campaign_id


def _assert_event_capture() -> None:
    campaign_key = f"phase6_evt_{uuid4().hex[:8]}"
    _seed_mock_run_campaign(campaign_key)

    err = io.StringIO()
    with redirect_stderr(err):
        asyncio.run(
            run_campaign(
                campaign_key=campaign_key,
                runner_id=f"smoke-{uuid4().hex[:8]}",
                mock=True,
                max_seeds=2,
            )
        )

    event_lines: list[dict] = []
    for line in err.getvalue().splitlines():
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "event" in parsed:
            event_lines.append(parsed)

    event_types = [str(item.get("event")) for item in event_lines]
    expected_subsequence = ["run_start", "seed_claim", "seed_completed", "seed_claim", "seed_completed", "run_end"]

    idx = 0
    for event_type in event_types:
        if idx < len(expected_subsequence) and event_type == expected_subsequence[idx]:
            idx += 1

    if idx != len(expected_subsequence):
        raise RuntimeError(f"event sequence missing expected subsequence. got={event_types}")


def _assert_status_formatting() -> None:
    campaign_key = f"phase6_status_{uuid4().hex[:8]}"
    campaign_id = _seed_campaign(campaign_key, status="running")

    _insert_seed(
        campaign_id,
        account_suffix="completed",
        status="completed",
        attempts=1,
        max_attempts=5,
        comps_captured=300,
        net_new_comps=100,
    )
    _insert_seed(campaign_id, account_suffix="queued", status="queued")
    _insert_seed(
        campaign_id,
        account_suffix="retryable",
        status="failed_retryable",
        attempts=2,
        max_attempts=5,
        retry_after_expr="NOW() + INTERVAL '10 minutes'",
    )
    _insert_seed(campaign_id, account_suffix="final", status="failed_final", attempts=5, max_attempts=5)

    out = io.StringIO()
    with redirect_stdout(out):
        status_campaign(campaign_key)

    rendered = out.getvalue()
    checks = [
        "Total seeds:" in rendered and "4" in rendered,
        "Completed:" in rendered and "1" in rendered,
        "Avg per seed:" in rendered and "300" in rendered,
        "circuit breaker:   closed" in rendered.lower(),
    ]
    if not all(checks):
        raise RuntimeError(f"status formatting assertions failed. output=\n{rendered}")


def _assert_alert_routing() -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        alert("ERROR", "test", foo="bar")

    rendered = err.getvalue()
    if "[ALERT severity=ERROR]" not in rendered:
        raise RuntimeError(f"missing alert prefix in output: {rendered}")
    if "foo=bar" not in rendered:
        raise RuntimeError(f"missing alert context in output: {rendered}")


def main() -> None:
    _ensure_session_schema()
    _assert_event_capture()
    _assert_status_formatting()
    _assert_alert_routing()
    print("smoke_phase6_observability: PASS")


if __name__ == "__main__":
    main()
