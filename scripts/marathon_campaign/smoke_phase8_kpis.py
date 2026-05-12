# scripts/marathon_campaign/smoke_phase8_kpis.py
#
# Role: Phase 8 smoke test for status_campaign KPI lines (duration percentiles, net-new ratio, error rate).
#
# Connects to:
#   api/main.py                          - ensures schema exists
#   api/config.py                        - DB helpers
#   scripts/marathon_campaign/runner.py  - status_campaign KPI output under test

from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.runner import status_campaign


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


def _insert_completed(campaign_id: int, suffix: str, duration_s: int, comps: int, net_new: int, attempts: int) -> None:
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
                    comps_captured,
                    net_new_comps,
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
                    32.741,
                    -96.941,
                    %s,
                    32.741,
                    -96.941,
                    'suburban',
                    420,
                    'completed',
                    %s,
                    5,
                    %s,
                    %s,
                    NOW(),
                    NOW() - (%s || ' seconds')::interval,
                    NOW(),
                    NOW(),
                    NOW(),
                    NOW()
                )
                """,
                (
                    campaign_id,
                    f"phase8_done_{suffix}_{uuid4().hex[:8]}",
                    f"{500 + len(suffix)} KPI Done Ave",
                    int(attempts),
                    int(comps),
                    int(net_new),
                    int(duration_s),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _insert_other(campaign_id: int, suffix: str, status: str, attempts: int, retry_minutes: int | None = None) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            retry_expr = "NULL" if retry_minutes is None else f"NOW() + INTERVAL '{int(retry_minutes)} minutes'"
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
                    queued_at,
                    failed_final_at,
                    last_transition_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'dcad',
                    32.742,
                    -96.942,
                    %s,
                    32.742,
                    -96.942,
                    'suburban',
                    420,
                    %s::propelio_marathon_seed_state,
                    %s,
                    5,
                    {retry_expr},
                    NOW(),
                    CASE WHEN %s = 'failed_final' THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW(),
                    NOW()
                )
                """,
                (
                    campaign_id,
                    f"phase8_{status}_{suffix}_{uuid4().hex[:8]}",
                    f"{550 + len(suffix)} KPI Other Ave",
                    status,
                    int(attempts),
                    status,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _extract_int(label: str, text: str) -> int:
    m = re.search(label + r"(\d+)s", text)
    if not m:
        raise RuntimeError(f"missing metric pattern: {label} in output\n{text}")
    return int(m.group(1))


def main() -> None:
    _ensure_session_schema()
    campaign_key = f"phase8_kpi_{uuid4().hex[:8]}"
    campaign_id = _seed_campaign(campaign_key)

    _insert_completed(campaign_id, "a", duration_s=300, comps=200, net_new=50, attempts=2)
    _insert_completed(campaign_id, "b", duration_s=360, comps=300, net_new=100, attempts=3)
    _insert_completed(campaign_id, "c", duration_s=400, comps=400, net_new=80, attempts=2)

    _insert_other(campaign_id, "d", status="failed_retryable", attempts=1, retry_minutes=15)
    _insert_other(campaign_id, "e", status="failed_final", attempts=1)
    _insert_other(campaign_id, "f", status="queued", attempts=0)
    _insert_other(campaign_id, "g", status="queued", attempts=0)

    out = io.StringIO()
    with redirect_stdout(out):
        status_campaign(campaign_key)
    rendered = out.getvalue()

    if "Pull duration:" not in rendered:
        raise RuntimeError(f"missing Pull duration line\n{rendered}")
    p50 = _extract_int(r"p50=", rendered)
    p95 = _extract_int(r"p95=", rendered)
    if p50 != 360:
        raise RuntimeError(f"expected p50=360, got {p50}\n{rendered}")
    if not (390 <= p95 <= 399):
        raise RuntimeError(f"expected p95 around 398, got {p95}\n{rendered}")

    if "Net-new ratio:" not in rendered:
        raise RuntimeError(f"missing Net-new ratio line\n{rendered}")
    if "230 net-new of 900 captured" not in rendered:
        raise RuntimeError(f"unexpected net-new ratio totals\n{rendered}")

    if "Error rate:" not in rendered:
        raise RuntimeError(f"missing Error rate line\n{rendered}")
    if "2 failures over 9 attempts" not in rendered:
        raise RuntimeError(f"unexpected error-rate denominator/numerator\n{rendered}")

    print("smoke_phase8_kpis: PASS")


if __name__ == "__main__":
    main()
