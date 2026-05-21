---
title: Cloud Run tuning recommendations for LotLedger
status: notes for KK to apply via GCP Console (preview/dev first)
date: 2026-05-21
trigger: post-Phase-1/2 residential detail expansion. Backend latency tightening across save_verification + /api/analyze. Per Copilot review, Cloud Run is under-tuned for this app's traffic shape (sync DB-heavy, small connection pools).
---

# Cloud Run tuning notes

Code-side hot-paths (Phase 1 feature.properties bloat + save_verification cold-instance hydration) are addressed in:
- `feat/save-verification-fast-path-2026-05-21` (this branch) — fast-path + GPU animation gate

**This document is for the COMPLEMENTARY platform-side tuning.**

Apply via GCP Console (per `feedback_gcp_console_first` in memory — KK is learning the console UI). Recommended order: preview first, observe for 24h, then dev, then prod.

## Current state (from cloudbuild yamls)

```
cloudbuild.yaml:33          — only timeout + memory configured
cloudbuild-preview.yaml:45  — only timeout + memory configured
cloudbuild-prod.yaml:46     — only timeout + memory configured
```

No min-instances. No concurrency cap. No CPU boost. Default Cloud Run settings apply:
- min-instances: 0 (cold-start on every burst after idle)
- max-instances: 100
- concurrency: 80 requests per instance
- startup CPU boost: off

## DB connection pool ceiling

`api/config.py:106`:
- Main pool: 20 connections
- Session pool: 5 connections

With default concurrency of 80 per instance, a single instance under load can queue requests behind a small pool. Lower concurrency caps reduce queueing.

## Recommended changes — FREE TUNING ONLY (no recurring cost)

KK explicitly out: any setting that costs extra per month. So:

- ❌ Min instances ≥ 1 — costs ~$10-20/mo/service. SKIP.
- ❌ "CPU always allocated" — always-billable CPU. SKIP.
- ✅ Concurrency cap — free, just a config setting.
- ✅ Startup CPU boost — free; only bursts CPU at startup, doesn't run always-on.

### 1. Lower concurrency to ~20-30 per instance

Why: Default 80 concurrency × 20-connection pool = potential for queuing. The save_verification + /api/analyze paths are sync-DB-heavy (psycopg2 connections held during query). Lowering concurrency means each instance accepts fewer concurrent requests, reducing pool contention. Cloud Run autoscales by spinning up more instances when load arrives, so total throughput stays similar with lower per-instance contention.

Recommended: concurrency = 20 (one slot per main DB pool connection, with headroom).

**Apply via Console:**
1. Cloud Run → service → Edit & Deploy New Revision
2. "Container" tab → "Capacity"
3. Maximum concurrent requests per instance: 20
4. Deploy.

### 2. Startup CPU boost ON

Why: When a new instance spins up (cold start or scaling up under load), the first few seconds get extra CPU. Helps the FastAPI + pandas import speed up. **Free** perf bonus on cold starts — only bursts during startup, not always-on.

**Apply via Console:**
1. Cloud Run → service → Edit & Deploy New Revision
2. "Container" tab → "CPU allocation and pricing"
3. Enable ONLY "Startup CPU boost" (leave "CPU is always allocated" OFF)
4. Deploy.

⚠️ Do NOT enable "CPU is always allocated" — that one DOES cost extra (~30% more).

## Verification

After applying:

1. Open Cloud Run service → Metrics tab
2. Watch:
   - **Request latency (p50, p95)** — should drop during sustained-traffic windows (concurrency cap reduces pool queuing)
   - **Container memory utilization** — should stay under 70%
3. Cold starts will still happen (no min-instances), but startup boost helps the first request after idle.

## What NOT to change without measurement

- Max instances (default 100 is fine — autoscale headroom)
- Request timeout (already 600s — high)
- Memory below 4Gi (won't fit residential-detail-bloated payloads)

## When to revisit (only if KK okays cost)

After applying the free tuning + smoke-testing for 24h, if cold-starts are still a UX problem, consider:

- min-instances = 1 (~$10-20/mo per service) — only if cold-start UX really hurts users
- "CPU is always allocated" (~30% surcharge) — only if cold-start CPU bursts insufficient

For now, NEITHER is on the table per KK's no-extra-cost rule. Documented only for future reference.

## References

- Cloud Run concurrency docs: https://cloud.google.com/run/docs/about-concurrency
- Cloud Run CPU allocation: https://cloud.google.com/run/docs/configuring/cpu-allocation
- Cloud Run min-instances: https://cloud.google.com/run/docs/configuring/min-instances
