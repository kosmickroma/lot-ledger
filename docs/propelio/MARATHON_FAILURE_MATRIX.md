# MARATHON_FAILURE_MATRIX

Purpose: operator-facing failure handling matrix for the marathon runner, with direct implementation anchors.

Verified against current code on 2026-05-12. Spec section 7 is canonical.

## Failure Matrix

| Failure class | Detection | Response | State transition | Implementation |
|---|---|---|---|---|
| Network timeout | asyncio.TimeoutError, TimeoutError, socket.timeout, ConnectionError, OSError in run loop | Retry with exponential backoff | running -> failed_retryable, retry_after = NOW + 5 min x 2^(attempts-1), capped at 60 min | [network catch path](../../scripts/marathon_campaign/runner.py#L927), [retry math and transition in handle_transient_failure](../../scripts/marathon_campaign/runner.py#L514) |
| Propelio 429 | PropelioRateLimitError catch branch (stub class) | Trip circuit breaker for 30 min and retry | running -> failed_retryable, retry_after = NOW + 30 min x backoff, capped at 60 min | [rate-limit branch](../../scripts/marathon_campaign/runner.py#L909), [breaker trip event+alert](../../scripts/marathon_campaign/runner.py#L910), [retry handling](../../scripts/marathon_campaign/runner.py#L919) |
| Propelio 401/403 | PropelioAuthError catch branch (stub class), including blocked remote status propagation | Immediate stop signal path: CRITICAL alert and re-raise, run ends with auth_block | running -> failed_retryable before re-raise, then run exits auth_block | [auth catch branch](../../scripts/marathon_campaign/runner.py#L893), [auth run-end alert](../../scripts/marathon_campaign/runner.py#L981) |
| Job hung remotely | Local heartbeat wait times out but remote may still be active | verify_remote_state does 3 polls, can complete, re-adopt, or fail with hard timeout under 45-min cap | running -> verifying -> completed OR verifying -> running -> failed_retryable | [verify_remote_state](../../scripts/marathon_campaign/runner.py#L564) |
| Stale orphan | Startup reconcile where heartbeat_at older than 15 minutes in active states | Check remote job and either complete, adopt, requeue, or retryable-fail | running/verifying/stopping_requested -> completed OR queued OR failed_retryable | [reconcile_orphans query and branches](../../scripts/marathon_campaign/runner.py#L334) |
| Unexpected exception | Catch-all Exception in run loop | Log via event stream and retry with backoff | running -> failed_retryable, error_class=unexpected | [catch-all branch](../../scripts/marathon_campaign/runner.py#L935), [handle_transient_failure](../../scripts/marathon_campaign/runner.py#L514) |
| Final retry exhaustion | attempts >= max_attempts in transient handler | No more retries | any active failure path -> failed_final, manual operator action required to requeue | [failed_final branch](../../scripts/marathon_campaign/runner.py#L533), [operator requeue helper](../../scripts/marathon_campaign/runner.py#L1129) |
| Parse error or parcel match failure | No dedicated class yet | Falls through to unexpected catch-all today | running -> failed_retryable, error_class=unexpected | [catch-all branch](../../scripts/marathon_campaign/runner.py#L935) |
| Operator skip | Manual CLI skip command | Mark skipped and do not retry. Only valid from settled states; active states (running/verifying/stopping_requested) must be stopped first via Ctrl+C and reconciled before skip. | queued/failed_retryable/failed_final -> skipped | [skip helper transition](../../scripts/marathon_campaign/runner.py#L1108), [skip CLI command](../../scripts/marathon_campaign/__main__.py#L71) |

## Notes

- The auth-block row was aligned to spec by ensuring the auth catch path routes through transient handling before re-raise.
- Exponential backoff is implemented as 2^(attempts-1), multiplied by retry_min, and clamped to 60 minutes in handle_transient_failure.
- Manual requeue is available for failed_final seeds and resets attempts to 0.

## Optional Operator Commands

- Skip: python -m scripts.marathon_campaign skip --seed-id 42 --reason "operator note"
- Requeue: python -m scripts.marathon_campaign requeue --seed-id 42

References:
- [CLI parser and command handlers](../../scripts/marathon_campaign/__main__.py#L23)
- [operator_skip_seed helper](../../scripts/marathon_campaign/runner.py#L1108)
- [operator_requeue_seed helper](../../scripts/marathon_campaign/runner.py#L1129)
