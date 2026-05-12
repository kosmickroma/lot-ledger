# scripts/marathon_campaign/state.py
#
# Role: Explicit FSM transition guard + atomic seed state updates for
#       Propelio marathon seed rows.
#
# Connects to:
#   api/config.py                  - imports session DB connection helpers
#   propelio_marathon_seeds table  - validates and applies state transitions
#
# Notes:
# - ALLOWED_TRANSITIONS intentionally mirrors the 21-edge v2.2 FSM.
# - State names use queued/running terminology for marathon seed lifecycle.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.config import get_session_conn, release_session_conn


ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    ("queued", "running"),
    ("queued", "skipped"),
    ("running", "completed"),
    ("running", "verifying"),
    ("running", "failed_retryable"),
    ("running", "failed_final"),
    ("running", "stopping_requested"),
    ("running", "queued"),
    ("verifying", "completed"),
    ("verifying", "running"),
    ("verifying", "failed_retryable"),
    ("verifying", "failed_final"),
    ("stopping_requested", "completed"),
    ("stopping_requested", "failed_retryable"),
    ("stopping_requested", "queued"),
    ("stopping_requested", "running"),
    ("failed_retryable", "running"),
    ("failed_retryable", "failed_final"),
    ("failed_retryable", "skipped"),
    ("failed_final", "queued"),
    ("failed_final", "skipped"),
}


class IllegalStateTransition(ValueError):
    """Raised when a transition is not allowed by ALLOWED_TRANSITIONS."""


@dataclass(frozen=True)
class TransitionResult:
    success: bool
    old_state: str | None
    new_state: str | None


def _timestamp_column_for_state(state: str) -> str | None:
    mapping = {
        "queued": "queued_at",
        "running": "running_at",
        "verifying": "verifying_at",
        "stopping_requested": "stopping_requested_at",
        "completed": "completed_at",
        "failed_final": "failed_final_at",
        "skipped": "skipped_at",
    }
    return mapping.get(state)


def transition(seed_id: int, from_state: str, to_state: str, **fields: Any) -> TransitionResult:
    """Atomically transition a marathon seed row if current status matches from_state.

    Returns success=False when the row exists but has already moved to a different
    state. Raises IllegalStateTransition when the edge is not in ALLOWED_TRANSITIONS.
    """
    edge = (str(from_state or "").strip(), str(to_state or "").strip())
    if edge not in ALLOWED_TRANSITIONS:
        raise IllegalStateTransition(f"illegal transition: {edge[0]} -> {edge[1]}")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM propelio_marathon_allowed_transitions
                WHERE from_state = %s::propelio_marathon_seed_state
                  AND to_state = %s::propelio_marathon_seed_state
                """,
                (edge[0], edge[1]),
            )
            if cur.fetchone() is None:
                raise IllegalStateTransition(
                    f"illegal transition (not present in DB table): {edge[0]} -> {edge[1]}"
                )
    finally:
        release_session_conn(conn)

    update_fields: dict[str, Any] = {
        "status": edge[1],
        "last_transition_at": "NOW()",
        "updated_at": "NOW()",
    }

    ts_col = _timestamp_column_for_state(edge[1])
    if ts_col:
        update_fields[ts_col] = "NOW()"

    if edge[1] == "running" and "attempt_started_at" not in fields:
        update_fields["attempt_started_at"] = "NOW()"

    for key, value in fields.items():
        update_fields[key] = value

    assignments: list[str] = []
    values: list[Any] = []
    for key, value in update_fields.items():
        if isinstance(value, str) and value == "NOW()":
            assignments.append(f"{key} = NOW()")
        else:
            assignments.append(f"{key} = %s")
            values.append(value)

    values.extend([int(seed_id), edge[0]])

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE propelio_marathon_seeds
                SET {", ".join(assignments)}
                WHERE seed_id = %s
                  AND status = %s
                RETURNING status
                """
                ,
                tuple(values),
            )
            row = cur.fetchone()

            cur.execute(
                "SELECT status FROM propelio_marathon_seeds WHERE seed_id = %s",
                (int(seed_id),),
            )
            current = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if row is not None:
        return TransitionResult(success=True, old_state=edge[0], new_state=edge[1])

    current_state = str(current[0]) if current else None
    return TransitionResult(success=False, old_state=current_state, new_state=current_state)
