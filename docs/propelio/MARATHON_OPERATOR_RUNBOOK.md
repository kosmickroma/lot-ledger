# MARATHON_OPERATOR_RUNBOOK

This runbook covers day-to-day operation of marathon campaigns, including startup, safe stopping, status interpretation, and incident response.

## 1. Quick Start

The standard flow is generate seeds for a campaign, run the campaign loop, and check status from a second terminal while it is working.

```bash
python -m scripts.marathon_campaign.generate_seeds dfw_v1
python -m scripts.marathon_campaign run --campaign dfw_v1
python -m scripts.marathon_campaign status --campaign dfw_v1
```

## 2. Starting A Session

Start with the run command and expect an orphan-reconcile line first, then seed claims, pacing pauses, and occasional break messages as the runner cycles through work.

```bash
python -m scripts.marathon_campaign run --campaign dfw_v1
python -m scripts.marathon_campaign run --campaign dfw_v1 --max-seeds 25
```

## 3. Stopping Safely

Use Ctrl+C for graceful SIGINT handling; in-flight seeds with remote jobs move to stopping_requested and request remote stop, while pre-job claimed seeds go back to queued so they can be claimed later.

```bash
# In runner terminal
Ctrl+C

# Alternative bounded run for controlled exits
python -m scripts.marathon_campaign run --campaign dfw_v1 --max-seeds 50
```

## 4. Status Checking

Run status from another terminal to inspect FSM counts, completed output totals, retry timing, breaker state, and KPI health.

```bash
python -m scripts.marathon_campaign status --campaign dfw_v1
watch -n 30 "python -m scripts.marathon_campaign status --campaign dfw_v1"
```

## 5. If Propelio Blocks You (auth_block)

If run_end_reason is auth_block, do not immediately restart: notify KK/Mike, wait for Propelio block pressure to clear, then restart the same campaign key and let orphan reconcile recover in-flight rows.

```bash
python -m scripts.marathon_campaign run --campaign dfw_v1
python -m scripts.marathon_campaign status --campaign dfw_v1
```

## 6. Skip A Problematic Seed

Use skip for seeds you want permanently out of retry circulation, typically after validating the record is bad or unproductive; stop the runner first if the seed is active.

```bash
python -m scripts.marathon_campaign skip --seed-id 42 --reason "bad address normalization"
python -m scripts.marathon_campaign skip --seed-id 108 --reason "known non-target parcel"
```

## 7. Requeue A failed_final Seed

Use requeue to give an exhausted seed a fresh retry budget; this resets attempts to 0 and sends the seed back to queued.

```bash
python -m scripts.marathon_campaign requeue --seed-id 42
```

## 8. New Metro Campaign Launch

To launch a new metro, edit the bbox constants in generate_seeds.py (for example the DFW_BBOX pattern), choose a new campaign key, and generate a fresh seed set for that key.

```bash
python -m scripts.marathon_campaign.generate_seeds phoenix_v1
python -m scripts.marathon_campaign run --campaign phoenix_v1
```

## 9. End-Of-Day Metrics

Interpret KPIs as operational signals: low net-new ratio usually means saturated coverage, high error rate indicates instability or blocking pressure, and high p95 pull duration indicates remote slowness.

```bash
python -m scripts.marathon_campaign status --campaign dfw_v1
```

## 10. Red Flags Requiring Immediate Attention

Treat these as urgent: run ends with auth_block, error rate above 20 percent, repeated breaker trips within one session, or stale heartbeat rows suggesting a crashed shared runner.

```bash
python -m scripts.marathon_campaign status --campaign dfw_v1
```

## 11. Log Files

Set MARATHON_LOG_DIR to enable per-campaign daily JSONL logs while still writing events to stderr, then use jq to filter run lifecycle and alert events quickly.

```bash
export MARATHON_LOG_DIR=./logs
python -m scripts.marathon_campaign run --campaign dfw_v1 2> marathon.stderr.log
jq -c 'select(.event=="run_start" or .event=="run_end" or .event=="alert")' ./logs/marathon_dfw_v1_$(date -u +%F).log
```

## 12. Environment Variables Reference

The MARATHON_* environment variables currently control log teeing, alert email stubbing, and cooldown wait behavior; set these in the shell or deployment runtime before starting runs.

- MARATHON_LOG_DIR: optional directory for per-campaign daily JSON log files written by event emitter.
- MARATHON_ALERT_EMAIL_ENABLED: set to 1 to print "would email" alert stubs for WARNING/ERROR/CRITICAL.
- MARATHON_MAX_COOLDOWN_WAIT_HOURS: max cooldown wait before graceful exit in breaker-open loops; 0 means wait indefinitely.

```bash
export MARATHON_LOG_DIR=./logs
export MARATHON_ALERT_EMAIL_ENABLED=1
export MARATHON_MAX_COOLDOWN_WAIT_HOURS=6
```
