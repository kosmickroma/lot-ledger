# scripts/marathon_campaign/smoke_phase7_manual_ops.py
#
# Role: Phase 7 smoke test for optional operator manual commands (skip/requeue).
#
# Connects to:
#   api/main.py                          - ensures schema exists
#   api/config.py                        - DB helpers
#   scripts/marathon_campaign/runner.py  - operator_skip_seed/operator_requeue_seed

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.runner import operator_requeue_seed, operator_skip_seed


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


def _insert_seed(campaign_id: int, status: str, account_suffix: str) -> int:
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
                    queued_at,
                    running_at,
                    failed_final_at,
                    last_transition_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'dcad',
                    32.731,
                    -96.931,
                    %s,
                    32.731,
                    -96.931,
                    'suburban',
                    333,
                    %s::propelio_marathon_seed_state,
                    CASE WHEN %s = 'failed_final' THEN 5 ELSE 0 END,
                    5,
                    NOW(),
                    CASE WHEN %s = 'running' THEN NOW() ELSE NULL END,
                    CASE WHEN %s = 'failed_final' THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"phase7_{account_suffix}_{uuid4().hex[:8]}",
                    f"{400 + len(account_suffix)} Ops Smoke St",
                    status,
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


def _fetch_seed(seed_id: int) -> tuple[str, int, str | None]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status::text, attempts, last_error_class
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
        raise RuntimeError(f"seed missing: {seed_id}")
    return str(row[0]), int(row[1] or 0), row[2]


def main() -> None:
    _ensure_session_schema()
    campaign_key = f"phase7_ops_{uuid4().hex[:8]}"
    campaign_id = _seed_campaign(campaign_key)

    queued_seed = _insert_seed(campaign_id, "queued", "skip")
    operator_skip_seed(queued_seed, reason="manual operator skip")
    status, attempts, err_cls = _fetch_seed(queued_seed)
    if status != "skipped":
        raise RuntimeError(f"skip failed: status={status}")
    if err_cls != "operator_skip":
        raise RuntimeError(f"skip failed: expected operator_skip, got {err_cls}")

    running_seed = _insert_seed(campaign_id, "running", "skip_invalid")
    try:
        operator_skip_seed(running_seed, reason="should fail")
    except ValueError as exc:
        if "cannot skip seed in state 'running'" not in str(exc):
            raise RuntimeError(f"invalid-state skip raised wrong message: {exc}")
    else:
        raise RuntimeError("invalid-state skip should have raised ValueError")
    status, _, _ = _fetch_seed(running_seed)
    if status != "running":
        raise RuntimeError(f"invalid-state skip mutated seed: status={status}")

    final_seed = _insert_seed(campaign_id, "failed_final", "requeue")
    operator_requeue_seed(final_seed)
    status, attempts, _ = _fetch_seed(final_seed)
    if status != "queued":
        raise RuntimeError(f"requeue failed: status={status}")
    if attempts != 0:
        raise RuntimeError(f"requeue failed: attempts expected 0, got {attempts}")

    print("smoke_phase7_manual_ops: PASS")


if __name__ == "__main__":
    main()
