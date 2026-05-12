# scripts/marathon_campaign/smoke_phase1_fsm.py
#
# Role: Tiny Phase 1 smoke test for marathon seed FSM transitions.
#       Verifies one valid lifecycle and one invalid edge rejection.
#
# Connects to:
#   api/main.py                             - runs _ensure_session_schema migration
#   api/config.py                           - session DB connection helpers
#   scripts/marathon_campaign/state.py      - transition() and IllegalStateTransition
#   propelio_marathon_campaigns/seeds       - inserts and validates DB lifecycle

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_session_conn, release_session_conn
from api.main import _ensure_session_schema
from scripts.marathon_campaign.state import IllegalStateTransition, transition


def main() -> None:
    _ensure_session_schema()

    campaign_key = f"smoke_{uuid4().hex[:10]}"

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_marathon_campaigns (campaign_key, status)
                VALUES (%s, 'queued')
                RETURNING campaign_id
                """,
                (campaign_key,),
            )
            campaign_id = int(cur.fetchone()[0])

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
                    last_transition_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', NOW(), NOW())
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"acct_{uuid4().hex[:8]}",
                    "test",
                    32.77,
                    -96.80,
                    "123 Smoke Test Ln",
                    32.77,
                    -96.80,
                    "suburban",
                    350,
                ),
            )
            lifecycle_seed_id = int(cur.fetchone()[0])

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
                    last_transition_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', NOW(), NOW())
                RETURNING seed_id
                """,
                (
                    campaign_id,
                    f"acct_{uuid4().hex[:8]}",
                    "test",
                    32.78,
                    -96.81,
                    "456 Invalid Edge Dr",
                    32.78,
                    -96.81,
                    "suburban",
                    410,
                ),
            )
            invalid_seed_id = int(cur.fetchone()[0])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    ok1 = transition(lifecycle_seed_id, "queued", "running")
    ok2 = transition(lifecycle_seed_id, "running", "completed")

    if not ok1.success or not ok2.success:
        raise RuntimeError("valid lifecycle transitions failed")

    invalid_rejected = False
    try:
        transition(invalid_seed_id, "queued", "completed")
    except IllegalStateTransition:
        invalid_rejected = True

    if not invalid_rejected:
        raise RuntimeError("invalid transition queued -> completed was not rejected")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, running_at, completed_at
                FROM propelio_marathon_seeds
                WHERE seed_id = %s
                """,
                (lifecycle_seed_id,),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if not row or row[0] != "completed" or row[1] is None or row[2] is None:
        raise RuntimeError("completed seed lifecycle row is missing expected timestamps")

    print(f"smoke_ok campaign_id={campaign_id} seed_id={lifecycle_seed_id} invalid_seed_id={invalid_seed_id}")


if __name__ == "__main__":
    main()
