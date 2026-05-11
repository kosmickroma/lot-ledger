# Propelio Deep-Pull Experiment — Copilot Build Spec

> **Status:** experimental scaffold on branch `feat/propelio-deep-pull-experiment`. Throwaway tables, throwaway UI. Goal: prove the multi-pass saturation strategy works before designing the permanent comps DB.
>
> **Branch:** `feat/propelio-deep-pull-experiment` (off `develop`)
> **Deploys to:** `lot-ledger-preview` Cloud Run service via `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`. DO NOT push to `develop` until KK approves.
>
> **Revision:** v3 (2026-05-11). Addresses Copilot second-review findings: PropelioClient constructor needs credentials (scraper.py:374-388), find_lead_id 3-tuple unpacking with parcel_bundle.confirmation_key (scraper.py:712-762, 1023-1031), cma_id top-level envelope extraction (scraper.py:1033), stale-sweeper now sets stop_requested AND adds next_pass_at check, runner re-checks stop_requested AFTER sleep before each Propelio call, error classification prefers structured status_code attribute before message-text fallback, cloudbuild-preview.yaml uses --update-env-vars arg form (not env block). Skipped per KK: queued-job sweeper (vanishingly rare), endpoint ownership check (already role-gated to dev+owner), endpoint env-guard (failure mode is acceptable).

---

## Defensive priorities — what we're paranoid about

Two operational concerns override convenience throughout this spec.

### 1. DB safety (dev's CSV download MUST keep working)

- All experimental tables live INSIDE `_ensure_session_schema()` at `api/main.py:184` using `get_session_conn()`. NO new connection patterns. NO new startup blocks.
- ALL DDL is wrapped in `if os.environ.get("DEEP_PULL_EXPERIMENT") == "true":` — tables only get CREATE'd when running on `lot-ledger-preview` Cloud Run where that env var is set. Dev's Cloud Run never sets it, never creates them.
- All deep-pull reads/writes use `get_session_conn()` / `release_session_conn()` — same pattern as `saved_areas` and other production tables.
- This experiment is read-only against production tables. It does NOT modify `saved_areas`, `propelio_comp_archive`, `propelio_cache`, `saved_parcels`, `users`, or anything else.

### 2. Stealth (Propelio must NOT flag us)

- ONE `PropelioClient` instance per job, reused across all 6 passes. Session cookies are preserved; we look like one human exploring filter options.
- Pre-job warmup: `random.uniform(5, 15)` seconds sleep BEFORE pass 1 (not just between passes 2-6).
- Inter-pass pacing: `random.uniform(30, 60)` seconds jittered each time, no fixed cadence.
- Sequential ONLY. No `asyncio.gather`. One Propelio HTTP request in flight at any moment.
- Fail-stop on any non-2xx from Propelio. NO retry. NO exponential backoff.
- Error classification: if Propelio returns 401, 403, or 429 → set status='blocked' (NOT generic 'error'). This is the visible signal that we tripped detection. Stop immediately. Surface in the status response.

---

## What we're testing

When a user clicks "Deep Pull" on a workspace, the backend cycles through 6 Propelio CMA passes with proximity-first ordering and jittered stealth pacing. Each pass stores its raw results in a throwaway table with pass metadata. We then inspect SQL to validate: are we actually getting more unique comps than a single Propelio pull? At what saturation point do additional passes stop adding new comps?

This is a research scaffold. It does NOT touch the production scraper, the existing comp archive, or the cache layer.

---

## Files to create or modify

### Create
- `api/propelio/deep_pull.py` — all experimental logic (runner, pass config, DB writes)

### Modify
- `api/main.py` — add the DDL block INSIDE `_ensure_session_schema()` (around line 184, in the same `with conn.cursor() as cur:` block where `saved_areas` etc. are created). Wrap in env-var guard. See Schema section.
- `api/propelio/routes.py` — add 3 new endpoints. Use BARE paths (the router already has `prefix="/api/propelio"` at line 37). Do NOT touch existing endpoints.
- `frontend/map.js` — add new state variable + Deep Pull section. Hook `_maybeShowDeepPullButton()` into both auth success points. Do NOT refactor existing functions.
- `frontend/index.html` — add button markup + banner markup inside the propelio sidebar card.
- `frontend/style.css` — minor styles at end of file.
- `cloudbuild-preview.yaml` — set `DEEP_PULL_EXPERIMENT=true` env var on the preview Cloud Run service.

### Do NOT modify
- `api/propelio/scraper.py` — use it as-is. The runner instantiates `PropelioClient` directly and calls its public methods.
- `api/propelio/archive.py` — no integration with workspace archive. Import `_comp_address_key` directly with an inline comment marking it as intentional experimental reuse.
- `api/propelio/cache.py` — bypassed entirely.
- `api/propelio/parcel_match.py` — out of scope.
- `cloudbuild.yaml` (dev deploy config) — do NOT set the env var here. Dev must never create these tables.

---

## Schema

Insert this DDL INSIDE the existing `_ensure_session_schema()` function in `api/main.py` (around line 184), in the same `with conn.cursor() as cur:` block where `saved_areas`, `users`, etc. are created. The block must come AFTER existing CREATE TABLE statements so FK references to `users(id)` resolve correctly.

Wrap the entire block in env-var guard:

```python
# Inside _ensure_session_schema(), after the existing CREATE TABLE blocks:
if os.environ.get("DEEP_PULL_EXPERIMENT") == "true":
    cur.execute("""
        CREATE TABLE IF NOT EXISTS propelio_deep_pull_jobs (
            job_id TEXT PRIMARY KEY,
            saved_area_id TEXT,
            started_by_user_id INTEGER REFERENCES users(id),
            target_address TEXT NOT NULL,
            target_lat NUMERIC,
            target_lng NUMERIC,
            lead_id TEXT,
            cma_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'completed', 'stopped',
                                  'error', 'saturated', 'blocked')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_pass_at TIMESTAMPTZ,
            next_pass_at TIMESTAMPTZ,
            passes_completed INTEGER NOT NULL DEFAULT 0,
            total_unique_comps INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            stop_requested BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_deep_pull_jobs_status
            ON propelio_deep_pull_jobs (status, next_pass_at)
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS propelio_deep_pull_experiment (
            id BIGSERIAL PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES propelio_deep_pull_jobs(job_id) ON DELETE CASCADE,
            pass_num INTEGER NOT NULL,
            months INTEGER NOT NULL,
            range_mi NUMERIC NOT NULL,
            pass_label TEXT,
            comp_address_key TEXT NOT NULL,
            comp_data JSONB NOT NULL,
            is_first_seen_in_job BOOLEAN NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (job_id, pass_num, comp_address_key)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_deep_pull_exp_job
            ON propelio_deep_pull_experiment (job_id, pass_num)
    """)
```

**Status enum includes 'blocked'** — this is the explicit signal for Propelio flagging events (401/403/429).

**`cloudbuild-preview.yaml`** — verified at lines 35-47, the deploy step uses `gcloud run deploy` with `--flag=value` arguments in an `args:` list (NOT a structured env block). ADD this ONE LINE to that args list, alongside `--memory=1Gi`, `--quiet`, etc.:

```yaml
      - '--update-env-vars=DEEP_PULL_EXPERIMENT=true'
```

Use `--update-env-vars` (NOT `--set-env-vars`) so existing service env config is preserved. Verify dev's `cloudbuild.yaml` does NOT have this variable.

These are experimental table names. They will be dropped once the real comps DB lands.

---

## Pass configuration (in `deep_pull.py`)

```python
PASSES = [
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5,  "label": "blocks"},
    {"months": 24, "range_mi": 1.0,  "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0,  "label": "broader"},
    {"months": 24, "range_mi": 5.0,  "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]

PRE_JOB_WARMUP_MIN_SECONDS = 5
PRE_JOB_WARMUP_MAX_SECONDS = 15
PACING_MIN_SECONDS = 30
PACING_MAX_SECONDS = 60
SATURATION_MIN_PASSES = 3        # don't saturate before pass 3
SATURATION_THRESHOLD = 0.05      # stop if new_this_pass / total < 5%
STALE_JOB_THRESHOLD_MINUTES = 5  # used by /status endpoint sweeper
```

---

## The runner (in `deep_pull.py`)

**Critical:** the FULL body is wrapped in `try/except` so any uncaught exception persists a terminal status before exit. `asyncio.create_task` swallows exceptions silently otherwise — by the time anyone notices, the job is stuck in 'running' forever.

```python
import asyncio
import logging
import os
import random
import secrets

from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn
from api.propelio.scraper import PropelioClient
from api.propelio.archive import _comp_address_key  # intentional experimental reuse — see spec
from api.propelio.config import PROPELIO_USERNAME, PROPELIO_PASSWORD  # constructor requires these (scraper.py:374-388)

logger = logging.getLogger(__name__)


def _classify_propelio_error(exc: Exception) -> str:
    """Return 'blocked' for 401/403/429-equivalent failures from Propelio, else 'error'.

    Strategy: prefer structured status_code if it leaks through (e.g., from a wrapped
    requests.HTTPError or a future enhancement to PropelioScraperError). Fall back to
    message-text heuristics, since PropelioScraperError today embeds the HTTP code in
    the message string — see scraper.py:835 (add_cma), 892 (search_cma), 443 (login).
    """
    # First check structured attributes on the exception or its attached response
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    if status_code in (401, 403, 429):
        return "blocked"

    # Fall back to message-text heuristics — current scraper embeds HTTP codes inline
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "throttle" in msg or "too many" in msg:
        return "blocked"
    if "401" in msg or "403" in msg or "unauthor" in msg or "forbidden" in msg:
        return "blocked"
    return "error"


async def run_deep_pull(job_id: str) -> None:
    """Background task. Cycles through PASSES with jittered pacing.

    The full body is wrapped in try/except. ANY exception persists a terminal
    status before exit. This is critical because asyncio.create_task swallows
    exceptions silently.
    """
    client = None
    try:
        # 1. Load job row + mark running
        target_address = _claim_job_for_running(job_id)
        if target_address is None:
            return  # not found or not eligible

        # 2. Pre-job warmup sleep — looks like a human pausing before clicking
        warmup_s = random.uniform(PRE_JOB_WARMUP_MIN_SECONDS, PRE_JOB_WARMUP_MAX_SECONDS)
        logger.info("[deep-pull job=%s] warmup sleep %.1fs before pass 1", job_id, warmup_s)
        await asyncio.sleep(warmup_s)

        # 3. ONE PropelioClient for the entire job — session cookies reused across passes.
        # PropelioClient constructor (scraper.py:374-388) REQUIRES username + password as
        # named args; will raise PropelioScraperError("credentials are not configured") if
        # they're empty strings. Credentials come from api/propelio/config.py which loads
        # PROPELIO_USERNAME / PROPELIO_PASSWORD from env (.env or Cloud Run env vars).
        client = PropelioClient(username=PROPELIO_USERNAME, password=PROPELIO_PASSWORD)
        await asyncio.to_thread(client.login)

        # 4. Pass 1: capture lead_id + cma_id (DO NOT go through search_properties,
        #    which doesn't return lead_id). Call find_lead_id + add_cma directly.
        if _is_stop_requested(job_id):
            _mark_job_status(job_id, 'stopped')
            return

        logger.info("[deep-pull job=%s] Pass 1: months=%d range=%.2fmi label=%s",
                    job_id, PASSES[0]["months"], PASSES[0]["range_mi"], PASSES[0]["label"])

        # find_lead_id returns (lead_id, subject_lot_sqft, parcel_bundle) per scraper.py:712-762.
        # parcel_bundle is a DICT with keys {'valuation', 'enrichment', 'confirmation_key'} —
        # see scraper.py:757-761.
        lead_id, subject_lot_sqft, parcel_bundle = await asyncio.to_thread(
            client.find_lead_id, target_address
        )
        confirmation_key = (
            parcel_bundle.get("confirmation_key") if isinstance(parcel_bundle, dict) else None
        )

        # add_cma signature (scraper.py:789-851): (lead_id, confirmation_key, months, range_mi).
        # Passing confirmation_key matches the proven production flow at scraper.py:1023-1031.
        # Returns a dict envelope whose top-level "id" key is the cma_id (scraper.py:1033).
        cma_envelope = await asyncio.to_thread(
            client.add_cma,
            lead_id,
            confirmation_key,
            months=PASSES[0]["months"],
            range_mi=PASSES[0]["range_mi"],
        )
        cma_id = _extract_cma_id(cma_envelope)
        _update_job_ids(job_id, lead_id=lead_id, cma_id=cma_id)

        comps = _parse_cma_envelope_comps(cma_envelope)
        _insert_pass_comps(job_id, pass_num=1, pass_config=PASSES[0], comps=comps)
        counts = _refresh_job_counts(job_id, pass_num=1)
        logger.info("[deep-pull job=%s] Pass 1 done: returned=%d new=%d total_unique=%d",
                    job_id, counts["returned"], counts["new"], counts["total_unique"])

        if _check_saturation(counts, pass_num=1):
            _mark_job_status(job_id, 'saturated')
            return

        # 5. Passes 2-6: reuse lead_id + cma_id via search_cma on the SAME client
        for pass_num in range(2, len(PASSES) + 1):
            if _is_stop_requested(job_id):
                _mark_job_status(job_id, 'stopped')
                return

            sleep_s = random.uniform(PACING_MIN_SECONDS, PACING_MAX_SECONDS)
            _set_next_pass_at(job_id, sleep_s)
            logger.info("[deep-pull job=%s] sleeping %.1fs before pass %d", job_id, sleep_s, pass_num)
            await asyncio.sleep(sleep_s)

            # CRITICAL: re-check stop_requested AFTER sleeping, BEFORE the outbound Propelio call.
            # The /status endpoint's stale-sweeper may have set stop_requested while we slept
            # (the sweeper marks stale jobs with status='error' AND stop_requested=TRUE). We must
            # NOT fire an outbound API call after being marked stopped — that would leak one
            # unnecessary Propelio request that contributes nothing and looks botty.
            if _is_stop_requested(job_id):
                logger.info("[deep-pull job=%s] stop_requested set during sleep — bailing before pass %d",
                            job_id, pass_num)
                # Status was already set by whoever flipped stop_requested. Just exit.
                return

            cfg = PASSES[pass_num - 1]
            logger.info("[deep-pull job=%s] Pass %d: months=%d range=%.2fmi label=%s",
                        job_id, pass_num, cfg["months"], cfg["range_mi"], cfg["label"])

            cma_response = await asyncio.to_thread(
                client.search_cma,
                lead_id,
                cma_id,
                months=cfg["months"],
                range_mi=cfg["range_mi"],
            )
            comps = _parse_cma_envelope_comps(cma_response)
            _insert_pass_comps(job_id, pass_num=pass_num, pass_config=cfg, comps=comps)
            counts = _refresh_job_counts(job_id, pass_num=pass_num)
            logger.info("[deep-pull job=%s] Pass %d done: returned=%d new=%d total_unique=%d",
                        job_id, pass_num, counts["returned"], counts["new"], counts["total_unique"])

            if _check_saturation(counts, pass_num=pass_num):
                logger.info("[deep-pull job=%s] saturated after pass %d", job_id, pass_num)
                _mark_job_status(job_id, 'saturated')
                return

        # 6. Completed all passes without saturating
        _mark_job_status(job_id, 'completed')
        logger.info("[deep-pull job=%s] completed all %d passes", job_id, len(PASSES))

    except Exception as exc:
        terminal_status = _classify_propelio_error(exc)
        logger.exception("[deep-pull job=%s] terminating with status=%s", job_id, terminal_status)
        try:
            _mark_job_status(job_id, terminal_status, error_msg=str(exc)[:500])
        except Exception as inner:
            logger.exception("[deep-pull job=%s] FAILED to persist terminal status: %s", job_id, inner)
        # DO NOT re-raise — we're a detached task, there's no upstream catcher.
```

### Helper functions to implement in `deep_pull.py`

Each gets its own connection from `get_session_conn()`, commits, releases. Standard pattern.

```python
def _claim_job_for_running(job_id: str) -> str | None:
    """SELECT job, verify eligible, UPDATE status='running'. Returns target_address or None."""

def _update_job_ids(job_id: str, *, lead_id: str, cma_id: str) -> None:
    """UPDATE propelio_deep_pull_jobs SET lead_id, cma_id WHERE job_id = ?"""

def _set_next_pass_at(job_id: str, seconds_from_now: float) -> None:
    """UPDATE next_pass_at = NOW() + (interval). Cosmetic — for /status visibility."""

def _is_stop_requested(job_id: str) -> bool:
    """SELECT stop_requested FROM propelio_deep_pull_jobs WHERE job_id = ?"""

def _insert_pass_comps(job_id: str, pass_num: int, pass_config: dict, comps: list[dict]) -> None:
    """For each comp: compute comp_address_key (import from archive),
    determine is_first_seen_in_job, INSERT ... ON CONFLICT DO NOTHING."""

def _refresh_job_counts(job_id: str, pass_num: int) -> dict:
    """Re-aggregate counts from the experiment table, UPDATE the jobs row,
    return {returned, new, total_unique}."""

def _mark_job_status(job_id: str, status: str, error_msg: str | None = None) -> None:
    """UPDATE status (and last_error if provided). Sets last_pass_at = NOW()."""

def _check_saturation(counts: dict, pass_num: int) -> bool:
    """Return True if pass_num >= SATURATION_MIN_PASSES AND
    counts['new'] / max(counts['total_unique'], 1) < SATURATION_THRESHOLD."""

def _extract_cma_id(envelope: dict) -> str:
    """Pull cma_id from the add_cma response. Matches the proven production path at
    api/propelio/scraper.py:1033 which does: `cma_id = str(add_payload.get("id") or "")`.

    The envelope is the dict returned from `client.add_cma(...)` — see scraper.py:851
    which returns the first element of an unwrapped list envelope. The top-level
    "id" key is what we want; envelope["data"] holds the comp sales list.

    Concrete implementation:
        if not isinstance(envelope, dict):
            raise ValueError(f"add_cma envelope is not a dict: type={type(envelope)}")
        cma_id = str(envelope.get("id") or "")
        if not cma_id:
            raise ValueError(f"could not extract cma_id from envelope keys={list(envelope.keys())}")
        return cma_id
    """

def _parse_cma_envelope_comps(envelope: dict) -> list[dict]:
    """Extract the sales list from the CMA envelope as a list of dicts ready
    for storage. Reference scraper._parse_cma_response in api/propelio/scraper.py:1113.
    If it's importable safely, import it. Otherwise duplicate the parsing logic
    inline with a comment marking the source line."""
```

---

## Endpoints (in `api/propelio/routes.py`)

The router has `prefix="/api/propelio"` at line 37. **Use BARE paths only** — do NOT prefix again, or routes will register at `/api/propelio/api/propelio/...` and be unreachable.

All endpoints require `Depends(get_current_user)` and follow the existing route style in this file.

```python
import asyncio
import secrets
from pydantic import BaseModel
from typing import Optional
from fastapi import Depends, HTTPException
from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn
from api.auth import get_current_user
from api.propelio.deep_pull import run_deep_pull


class DeepPullStartRequest(BaseModel):
    target_address: str
    saved_area_id: Optional[str] = None


@router.post("/deep-pull/start")  # → /api/propelio/deep-pull/start
async def start_deep_pull(
    request: DeepPullStartRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Insert job row with status='queued', spawn background task, return job_id.
    Restricted to developer + owner roles.
    """
    if user.get("role") not in ("developer", "owner"):
        raise HTTPException(status_code=403, detail="Deep pull is developer-only")
    address = (request.target_address or "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="target_address is required")

    job_id = "dp_" + secrets.token_urlsafe(8)

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_deep_pull_jobs
                    (job_id, target_address, saved_area_id, started_by_user_id, status)
                VALUES (%s, %s, %s, %s, 'queued')
                """,
                (job_id, address, request.saved_area_id, int(user["id"])),
            )
            conn.commit()
    finally:
        release_session_conn(conn)

    asyncio.create_task(run_deep_pull(job_id))
    return {"job_id": job_id}


@router.get("/deep-pull/status/{job_id}")  # → /api/propelio/deep-pull/status/{job_id}
async def get_deep_pull_status(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Returns job state + per-pass breakdown.
    Also runs STALE-JOB SWEEPER inline: if status='running' AND no progress for
    >5 minutes AND we're past next_pass_at, mark as 'error' with
    'Worker interrupted (stale)' AND set stop_requested=TRUE so that if a worker
    later resumes from a sleep, it bails BEFORE making the next Propelio call.

    The next_pass_at check prevents false-positive stale flagging during
    legitimate 30-60s sleeps. Defends against Cloud Run mid-job instance restarts.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            # Stale-job sweep. Sets stop_requested=TRUE so the worker (if still alive
            # and just slow-waking from sleep) refuses to fire the next Propelio call.
            # next_pass_at clause avoids flagging legitimate inter-pass sleeps.
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET status='error',
                    last_error='Worker interrupted (stale)',
                    stop_requested=TRUE
                WHERE job_id = %s
                  AND status='running'
                  AND started_at < NOW() - INTERVAL '5 minutes'
                  AND (last_pass_at IS NULL OR last_pass_at < NOW() - INTERVAL '5 minutes')
                  AND (next_pass_at IS NULL OR next_pass_at < NOW())
                """,
                (job_id,),
            )

            cur.execute(
                """
                SELECT job_id, status, passes_completed, total_unique_comps,
                       last_pass_at, next_pass_at, last_error, started_at, stop_requested
                FROM propelio_deep_pull_jobs WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Job not found")

            cur.execute(
                """
                SELECT pass_num,
                       months,
                       range_mi,
                       pass_label,
                       COUNT(*) AS returned,
                       COUNT(*) FILTER (WHERE is_first_seen_in_job) AS new_comps,
                       MAX(fetched_at) AS completed_at
                FROM propelio_deep_pull_experiment
                WHERE job_id = %s
                GROUP BY pass_num, months, range_mi, pass_label
                ORDER BY pass_num
                """,
                (job_id,),
            )
            per_pass = [
                {
                    "pass_num": r[0],
                    "months": r[1],
                    "range_mi": float(r[2]),
                    "label": r[3],
                    "returned": r[4],
                    "new": r[5],
                    "completed_at": r[6].isoformat() if r[6] else None,
                }
                for r in cur.fetchall()
            ]
            conn.commit()
            return {
                "job_id": row[0],
                "status": row[1],
                "passes_completed": row[2],
                "total_unique_comps": row[3],
                "last_pass_at": row[4].isoformat() if row[4] else None,
                "next_pass_at": row[5].isoformat() if row[5] else None,
                "last_error": row[6],
                "stop_requested": bool(row[8]),
                "per_pass": per_pass,
            }
    finally:
        release_session_conn(conn)


@router.post("/deep-pull/stop/{job_id}")  # → /api/propelio/deep-pull/stop/{job_id}
async def stop_deep_pull(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Sets stop_requested=true. Runner checks between passes and bails."""
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE propelio_deep_pull_jobs SET stop_requested = TRUE WHERE job_id = %s",
                (job_id,),
            )
            conn.commit()
    finally:
        release_session_conn(conn)
    return {"ok": True}
```

---

## Frontend

### `frontend/index.html`

Find the propelio sidebar card (search for the existing Get Comps / Refresh buttons). Add adjacent:

```html
<button id="btn-deep-pull"
        class="btn-secondary deep-pull-dev-only"
        style="display:none;">
  Deep Pull (dev)
</button>

<div id="deep-pull-banner" class="deep-pull-banner hidden">
  <span class="deep-pull-banner-icon">⟳</span>
  <span id="deep-pull-banner-text" class="deep-pull-banner-text">Deep pull in progress...</span>
  <button id="btn-deep-pull-stop" class="btn-tertiary">Stop</button>
</div>
```

### `frontend/style.css`

Add at end of file:

```css
.deep-pull-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin: 6px 0;
  background: rgba(247, 227, 161, 0.18);
  border: 1px solid rgba(212, 175, 55, 0.6);
  border-radius: 4px;
  font-size: 12px;
}
.deep-pull-banner.hidden { display: none; }
.deep-pull-banner-icon {
  display: inline-block;
  animation: deep-pull-spin 1.5s linear infinite;
}
@keyframes deep-pull-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
```

### `frontend/map.js` — IMPORTANT VARIABLE-NAME CORRECTIONS FROM REV 1

**Verified variable names in this codebase (Copilot first review caught these):**
- Auth user variable is `_currentUser` (underscore prefix). Confirmed at `map.js:7708`.
- `_currentLoadedAreaId` exists at `map.js:529`. Usable as-is.
- `lastSearchedAddress` and `lastTargetAddress` do NOT exist. You must ADD a new state variable.
- Login success handler is around `map.js:7793`. `initAuth` success is around `map.js:8255`.
- The codebase has NO toast helper. Use `console.log` and the in-page banner only — no `alert()`.

#### Step 1 — add new state variable

Near the top of `map.js` with the other module-level state (around line 525-535 where `_currentLoadedAreaId` lives), add:

```javascript
// Tracks the most recent address the user searched or selected via the address typeahead.
// Used by the Deep Pull (experimental) feature to know what target to scrape.
let _lastSearchedAddress = null;
```

#### Step 2 — populate `_lastSearchedAddress` in the existing search flow

Look at `frontend/map.js:4678` for `selectSuggestion(idx)` and `map.js:4748` for `doSearch()`.

Inside `selectSuggestion(idx)` at `map.js:4678`, the local variable `item` is defined at `map.js:4679` and holds the suggestion object with an `.address` property (rendered from `item.address` at `map.js:4613`). Assign:

```javascript
_lastSearchedAddress = item.address;
```

Inside `doSearch()` at `map.js:4748`, the local variable `q` holds the typed query, defined at `map.js:4749`. Assign:

```javascript
_lastSearchedAddress = q;
```

Place each assignment immediately after the variable's existing definition in those functions. Do not modify any existing logic.

#### Step 3 — add the Deep Pull section at the END of `map.js`

```javascript
// =============================================================================
// EXPERIMENTAL: Propelio Deep Pull (dev-only, throwaway scaffold)
// Removed once the permanent comps DB + production deep-pull is built.
// =============================================================================

let _deepPullPollTimer = null;
let _activeDeepPullJobId = null;

function _maybeShowDeepPullButton() {
  const btn = document.getElementById("btn-deep-pull");
  if (!btn) return;
  const role = (_currentUser && _currentUser.role) || "";
  if (role === "developer" || role === "owner") {
    btn.style.display = "inline-block";
  } else {
    btn.style.display = "none";
  }
}

async function startDeepPull() {
  const address = _lastSearchedAddress;
  if (!address) {
    console.warn("[deep-pull] no target address. Search for an address first.");
    _showDeepPullBanner("No target address — search for an address first");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }
  console.log("[deep-pull] starting for address:", address);
  try {
    const resp = await _apiJson("/api/propelio/deep-pull/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        target_address: address,
        saved_area_id: _currentLoadedAreaId || null,
      }),
    });
    _activeDeepPullJobId = resp.job_id;
    console.log("[deep-pull] job started:", resp);
    _showDeepPullBanner("Pass 0/6, queued — warming up...");
    _deepPullPollTimer = setInterval(_pollDeepPullStatus, 5000);
  } catch (err) {
    console.error("[deep-pull] start failed:", err);
    _showDeepPullBanner("Deep pull failed to start (see console)");
    setTimeout(_hideDeepPullBanner, 4000);
  }
}

async function _pollDeepPullStatus() {
  if (!_activeDeepPullJobId) return;
  try {
    const resp = await _apiJson(`/api/propelio/deep-pull/status/${_activeDeepPullJobId}`);
    console.log("[deep-pull] status tick:", resp);
    _updateDeepPullBanner(resp);
    if (["completed", "saturated", "stopped", "error", "blocked"].includes(resp.status)) {
      clearInterval(_deepPullPollTimer);
      _deepPullPollTimer = null;
      const finishedJobId = _activeDeepPullJobId;
      _activeDeepPullJobId = null;
      console.log("[deep-pull] FINAL summary:", resp);
      _showDeepPullBanner(
        `Job ${resp.status} — ${resp.passes_completed}/6 passes, ${resp.total_unique_comps} unique comps. Job ID: ${finishedJobId}`
      );
      setTimeout(_hideDeepPullBanner, 6000);
    }
  } catch (err) {
    console.error("[deep-pull] poll failed:", err);
  }
}

async function stopDeepPull() {
  if (!_activeDeepPullJobId) return;
  console.log("[deep-pull] stop requested for", _activeDeepPullJobId);
  try {
    await _apiJson(`/api/propelio/deep-pull/stop/${_activeDeepPullJobId}`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch (err) {
    console.error("[deep-pull] stop failed:", err);
  }
}

function _showDeepPullBanner(text) {
  const banner = document.getElementById("deep-pull-banner");
  const textEl = document.getElementById("deep-pull-banner-text");
  if (banner) banner.classList.remove("hidden");
  if (textEl) textEl.textContent = text;
}

function _updateDeepPullBanner(status) {
  const textEl = document.getElementById("deep-pull-banner-text");
  if (!textEl) return;
  const passCount = `${status.passes_completed}/6`;
  textEl.textContent = `${status.status} — Pass ${passCount}, ${status.total_unique_comps} unique so far`;
}

function _hideDeepPullBanner() {
  const banner = document.getElementById("deep-pull-banner");
  if (banner) banner.classList.add("hidden");
}

// Wire up button click handlers after DOM is ready
document.getElementById("btn-deep-pull")?.addEventListener("click", startDeepPull);
document.getElementById("btn-deep-pull-stop")?.addEventListener("click", stopDeepPull);
```

#### Step 4 — call `_maybeShowDeepPullButton()` after `_currentUser` is assigned

Two entry points where `_currentUser` is set:
- **Login success handler** around `map.js:7793` — when the login modal succeeds and `_currentUser` is assigned.
- **`initAuth()` success path** around `map.js:8255` — when the page-load auth check finds an existing session.

In BOTH spots, add a call to `_maybeShowDeepPullButton()` IMMEDIATELY AFTER `_currentUser` is set. Do not modify other behavior in those handlers.

**No `alert()` anywhere** — banner messages + console logs only.

---

## Smoke test (KK runs after Copilot ships)

1. `git status` clean, on branch `feat/propelio-deep-pull-experiment`
2. Confirm `cloudbuild-preview.yaml` has `DEEP_PULL_EXPERIMENT=true` set
3. Confirm `cloudbuild.yaml` (dev deploy config) does NOT have that env var
4. Deploy: `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`
5. Open `https://lot-ledger-preview-505466930182.us-central1.run.app`
6. Log in as developer
7. Search for a known address (urban first — e.g., a downtown Dallas property)
8. Click "Deep Pull (dev)" button
9. Console.log stream should tick every 5s
10. Banner should advance through Pass 1 → 2 → 3 → ...
11. Wait ~5-10 minutes for full 6-pass run or earlier saturation
12. After completion, inspect DB via Cloud SQL Studio:

```sql
-- Job summary (most recent first)
SELECT job_id, target_address, status, passes_completed,
       total_unique_comps, last_error, last_pass_at, started_at
FROM propelio_deep_pull_jobs
ORDER BY started_at DESC
LIMIT 5;

-- Per-pass breakdown for the latest job
SELECT
  pass_num,
  months,
  range_mi,
  pass_label,
  COUNT(*) AS comps_in_pass,
  COUNT(*) FILTER (WHERE is_first_seen_in_job) AS new_in_pass
FROM propelio_deep_pull_experiment
WHERE job_id = '<latest_job_id>'
GROUP BY 1, 2, 3, 4
ORDER BY 1;

-- Total unique
SELECT COUNT(DISTINCT comp_address_key) AS unique_comps
FROM propelio_deep_pull_experiment
WHERE job_id = '<latest_job_id>';
```

**What "success" looks like:**
- Pass 1 returns N comps, all `is_first_seen_in_job = true`
- Pass 2 returns more (broader radius), with mixed new/duplicate
- By pass 4-5, `new_in_pass / total` is dropping
- Saturation may fire at pass 3-4 in dense suburban; rural may go all 6
- Total unique comps > single-pass count for the same address
- No `blocked` or `error` statuses

**What failure looks like:**
- `status='blocked'` — Propelio flagged us. STOP, investigate before retrying.
- `status='error'` — something else broke. Check `last_error`.
- Job stuck in 'running' — stale-job sweeper hasn't fired. Hit the /status endpoint to trigger sweep, or check `started_at` is old enough.
- Total unique = single-pass — strategy didn't expose more comps. Re-evaluate pass shape.

---

## What this experiment does NOT do (yet)

- No write to permanent `propelio_comps` table (doesn't exist yet)
- No CSV integration
- No workspace integration beyond capturing `saved_area_id` for reference
- No frontend rendering of the temp table data — inspect via SQL
- No variation of months axis — only distance
- No retry/backoff (fail-stop only)
- No per-pass detail UI (banner is summary-only)

---

## When the experiment is "done"

KK runs deep pulls against 3-5 addresses (urban, suburban, rural). For each:
- Total comps captured > single-pass count? (yes = strategy works)
- Pacing stealth-looking? (logs show jittered 30-60s + warmup 5-15s)
- Any `blocked` statuses? (any = redesign before proceeding)

If yes-yes-no across all test cases, proceed to permanent `propelio_comps` table + production deep-pull integration.
