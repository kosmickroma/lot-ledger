# scripts/marathon_campaign/smoke_phase4a_runner.py
#
# Role: Phase 4A smoke test for happy-path runner flow in --mock-pull mode.
#
# Connects to:
#   api/main.py                          - ensures schema exists
#   api/config.py                        - session DB connections
#   scripts/marathon_campaign/runner.py  - run_campaign + status

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.runner import run_campaign


def _seed_smoke_campaign(campaign_key: str) -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_marathon_campaigns (campaign_key, status, started_at, updated_at)
                VALUES (%s, 'queued', NOW(), NOW())
                ON CONFLICT (campaign_key) DO UPDATE SET updated_at = NOW()
                RETURNING campaign_id
                """,
                (campaign_key,),
            )
            campaign_id = int(cur.fetchone()[0])

            for idx in range(3):
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
                        updated_at,
                        attempts
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'queued', NOW(), NOW(), NOW(), NOW(), 0)
                    ON CONFLICT (campaign_id, parcel_county, parcel_account_num)
                    DO UPDATE SET
                        status = 'queued',
                        attempts = 0,
                        retry_after = NULL,
                        claimed_by = NULL,
                        job_id = NULL,
                        comps_captured = NULL,
                        net_new_comps = NULL,
                        queued_at = NOW(),
                        running_at = NULL,
                        completed_at = NULL,
                        last_transition_at = NOW(),
                        updated_at = NOW()
                    """,
                    (
                        campaign_id,
                        f"smoke_acct_{idx}_{uuid4().hex[:8]}",
                        "dcad",
                        32.70 + idx * 0.01,
                        -96.90 - idx * 0.01,
                        f"{100 + idx} Runner Smoke Ln",
                        32.70 + idx * 0.01,
                        -96.90 - idx * 0.01,
                        "suburban",
                        400,
                    ),
                )
        conn.commit()
        return campaign_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def _assert_completed(campaign_id: int) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_count,
                    COUNT(*) FILTER (WHERE comps_captured IS NOT NULL) AS with_total,
                    COUNT(*) FILTER (WHERE net_new_comps IS NOT NULL) AS with_net_new,
                    COUNT(*) AS total
                FROM propelio_marathon_seeds
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            completed_count, with_total, with_net_new, total = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if int(total or 0) != 3:
        raise RuntimeError(f"expected 3 seeds, got {int(total or 0)}")
    if int(completed_count or 0) != 3:
        raise RuntimeError(f"expected 3 completed seeds, got {int(completed_count or 0)}")
    if int(with_total or 0) != 3:
        raise RuntimeError(f"expected comps_captured on all seeds, got {int(with_total or 0)}")
    if int(with_net_new or 0) != 3:
        raise RuntimeError(f"expected net_new_comps on all seeds, got {int(with_net_new or 0)}")


async def _run() -> None:
    _ensure_session_schema()
    campaign_key = f"phase4a_smoke_{uuid4().hex[:8]}"
    campaign_id = _seed_smoke_campaign(campaign_key)

    await run_campaign(
        campaign_key=campaign_key,
        runner_id=f"smoke-{uuid4().hex[:8]}",
        mock=True,
        max_seeds=3,
    )

    _assert_completed(campaign_id)
    print(f"smoke_phase4a_runner: PASS campaign_key={campaign_key} campaign_id={campaign_id}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
