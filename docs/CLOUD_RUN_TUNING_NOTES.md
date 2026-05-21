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

## Recommended changes

### 1. Min instances ≥ 1 (preview + dev first)

Why: Cold-start on Cloud Run involves spinning up a Python interpreter, importing FastAPI + pandas + psycopg2, and warming the connection pools. Currently ~5-15s cold-start latency. Setting min=1 keeps one instance always warm — eliminates cold-start for the first user request.

Cost: ~$10-20/month per service for the always-on instance. Worth it on preview/dev for testing UX. Prod: optional once we measure real traffic patterns.

**Apply via Console:**
1. Cloud Run → `lot-ledger-preview` → Edit & Deploy New Revision
2. Container, Networking, Security → "Container" tab → "Scaling"
3. Set Min instances to 1
4. Deploy.

Repeat for `lot-ledger-dev`. **Skip prod for now** until preview/dev confirmed working.

### 2. Lower concurrency to ~20-30 per instance

Why: Default 80 concurrency × 20-connection pool = potential for queuing. The save_verification + /api/analyze paths are sync-DB-heavy (psycopg2 connections held during query). Lowering concurrency means each instance accepts fewer concurrent requests, reducing pool contention. Cloud Run autoscales by spinning up more instances when load arrives, so total throughput stays similar with lower per-instance contention.

Recommended: concurrency = 20 (one slot per main DB pool connection, with headroom).

**Apply via Console:**
1. Cloud Run → service → Edit & Deploy New Revision
2. "Container" tab → "Capacity"
3. Maximum concurrent requests per instance: 20
4. Deploy.

### 3. Startup CPU boost ON

Why: When a new instance spins up (min=0 cold start or scaling up under load), the first few seconds get extra CPU. Helps the FastAPI + pandas import speed up. Free perf bonus on cold starts.

**Apply via Console:**
1. Cloud Run → service → Edit & Deploy New Revision
2. "Container" tab → "CPU allocation and pricing"
3. Enable "CPU is always allocated" OR enable "Startup CPU boost"
4. Deploy.

CPU is always allocated: keeps CPU available even between requests. Better for sync workloads. ~30% extra cost.
Startup CPU boost: only burst at start. Cheaper but only helps cold starts.

### 4. (Optional) Memory increase

Current: 4Gi (preview), 4Gi (prod) per cloudbuild yamls.

With Phase 1 bloat, larger /api/analyze responses + cached_jobs.rows hydration uses more memory per request. For 11k-parcel jobs, peak memory may spike to ~600 MB during JSON parse.

Recommended: bump to 6Gi or 8Gi on preview if you see memory pressure. Easy console edit; ~$5-10/month per service.

## Verification

After applying:

1. Open Cloud Run service → Metrics tab
2. Watch:
   - **Request latency (p50, p95, p99)** — should drop after min-instances=1 takes effect
   - **Instance count over time** — should show 1 minimum
   - **Container memory utilization** — should stay under 70%
3. Test save_verification download on a 1000+ parcel polygon — first call after idle should now feel snappier (no cold start).

## What NOT to change without measurement

- Max instances (default 100 is fine — autoscale headroom)
- Request timeout (already 600s — high)
- Memory below 4Gi (won't fit residential-detail-bloated payloads)

## When to revisit

After applying the above + smoke-testing for 24h:

- If still slow on cold starts → bump min-instances to 2-3
- If still slow on bursts → re-measure concurrency vs pool size, may want to bump pool to 30
- If memory pressure → bump memory to 6Gi
- If CPU bound → bump CPU allocation from default 1 to 2

## References

- Cloud Run concurrency docs: https://cloud.google.com/run/docs/about-concurrency
- Cloud Run CPU allocation: https://cloud.google.com/run/docs/configuring/cpu-allocation
- Cloud Run min-instances: https://cloud.google.com/run/docs/configuring/min-instances
