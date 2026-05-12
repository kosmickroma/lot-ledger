# scripts/marathon_campaign/circuit_breaker.py
#
# Role: Campaign-level anti-bot circuit-breaker state with best-effort DB
#       persistence and failure diagnostics.
#
# Connects to:
#   api/config.py                        - imports session DB connection helpers
#   propelio_marathon_campaigns table    - stores breaker state per campaign

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging

from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CircuitBreaker:
    campaign_id: int
    failure_count: int = 0
    last_failure_at: datetime | None = None
    cooldown_until: datetime | None = None
    consecutive_rate_limits: int = 0
    error_window: list[str] = field(default_factory=list)
    _persist_fail_count: int = 0
    _last_persist_fail_at: datetime | None = None

    @classmethod
    def load(cls, campaign_id: int) -> "CircuitBreaker":
        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        cb_failure_count,
                        cb_last_failure_at,
                        cb_cooldown_until,
                        cb_consecutive_rate_limits,
                        cb_error_window,
                        cb_persist_fail_count,
                        cb_last_persist_fail_at
                    FROM propelio_marathon_campaigns
                    WHERE campaign_id = %s
                    """,
                    (int(campaign_id),),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release_session_conn(conn)

        if row is None:
            raise ValueError(f"campaign_id not found: {campaign_id}")

        return cls(
            campaign_id=int(campaign_id),
            failure_count=int(row[0] or 0),
            last_failure_at=row[1],
            cooldown_until=row[2],
            consecutive_rate_limits=int(row[3] or 0),
            error_window=list(row[4] or []),
            _persist_fail_count=int(row[5] or 0),
            _last_persist_fail_at=row[6],
        )

    def is_open(self) -> bool:
        if self.cooldown_until and _utcnow() < self.cooldown_until:
            return True

        sample_size = len(self.error_window)
        if sample_size >= 10:
            errors = sum(1 for item in self.error_window if item != "ok")
            if errors / sample_size > 0.30:
                self.cooldown_until = _utcnow() + timedelta(hours=1)
                self.persist()
                return True
        return False

    def record_outcome(self, outcome: str) -> None:
        self.error_window.append(str(outcome or ""))
        if len(self.error_window) > 20:
            self.error_window = self.error_window[-20:]

        if outcome == "ok":
            self.failure_count = 0
            self.consecutive_rate_limits = 0
        else:
            self.failure_count += 1
            self.last_failure_at = _utcnow()
            if outcome == "rate_limit":
                self.consecutive_rate_limits += 1
            else:
                self.consecutive_rate_limits = 0

        self.persist()

    def trip(self, reason: str, cooldown_min: int) -> None:
        self.cooldown_until = _utcnow() + timedelta(minutes=int(cooldown_min))
        self.last_failure_at = _utcnow()
        self.failure_count += 1
        if reason == "rate_limit":
            self.consecutive_rate_limits += 1
        self.error_window.append(reason)
        if len(self.error_window) > 20:
            self.error_window = self.error_window[-20:]
        self.persist()

    def persist(self) -> None:
        try:
            conn = get_session_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE propelio_marathon_campaigns
                        SET
                            cb_failure_count = %s,
                            cb_last_failure_at = %s,
                            cb_cooldown_until = %s,
                            cb_consecutive_rate_limits = %s,
                            cb_error_window = %s,
                            cb_persist_fail_count = %s,
                            cb_last_persist_fail_at = %s,
                            updated_at = NOW()
                        WHERE campaign_id = %s
                        """,
                        (
                            int(self.failure_count),
                            self.last_failure_at,
                            self.cooldown_until,
                            int(self.consecutive_rate_limits),
                            Json(self.error_window),
                            int(self._persist_fail_count),
                            self._last_persist_fail_at,
                            int(self.campaign_id),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                release_session_conn(conn)

            if self._persist_fail_count > 0:
                logger.info(
                    "Circuit breaker persist recovered after %s consecutive failures",
                    self._persist_fail_count,
                )
                self._persist_fail_count = 0
                self._last_persist_fail_at = None
        except Exception as exc:
            self._persist_fail_count += 1
            self._last_persist_fail_at = _utcnow()
            logger.warning(
                "Circuit breaker persist failed (non-fatal, fail#%s at %s): %s | cooldown_until=%s window_size=%s",
                self._persist_fail_count,
                self._last_persist_fail_at.isoformat(),
                exc,
                self.cooldown_until,
                len(self.error_window),
            )
            if self._persist_fail_count >= 10:
                logger.error(
                    "Circuit breaker persist failed %s times in a row; investigate DB connectivity",
                    self._persist_fail_count,
                )
