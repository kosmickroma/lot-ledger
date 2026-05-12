# Marathon Seeding Campaign — Spec v2.2

> **Status:** Draft v2.2, incorporating Copilot's v2.1 round-3 review
> (2 new BLOCKERS, 5 new IMPORTANTs). Both round-3 BLOCKERS patched;
> all round-3 IMPORTANTs resolved inline. **Build-eligible.**
>
> ## Changes v2.1 → v2.2
>
> 1. **BLOCKER (v2.1 ALLOWED_TRANSITIONS gap):** Added missing edges:
>    - `('verifying', 'failed_final')` — exhausted retries from verify path
>    - `('stopping_requested', 'in_progress')` — reconcile adopts running orphan
>    - `('in_progress', 'pending')` — SIGINT clean-escape with no job_id
> 2. **BLOCKER (v2.1 SIGINT crash):** SIGINT handler now wraps the
>    entire if/else in a single `try/except IllegalStateTransition`.
>    No more crash on edge cases.
> 3. **IMPORTANT (start_deep_pull_for_seed scope):** Added §4.8.1
>    listing the three required upstream refactors (pass config
>    threading, nullable user_id, async event loop strategy). Must be
>    completed BEFORE Phase 4 coding.
> 4. **IMPORTANT (circuit breaker DB-down):** `persist()` now wraps DB
>    write in try/except, logs WARNING and swallows.
> 5. **IMPORTANT (NULL retry_after trap):** Claim SQL now includes
>    `OR retry_after IS NULL` so a future caller forgetting to set the
>    field doesn't silently break the queue.
> 6. **MINOR:** Added stub definitions for `update_seed_job_id` and
>    `wait_for_cooldown_or_exit` (referenced in §4.2 but not defined).
>
> Previous versions: v1 archived, v2/v2.1 in git history.
>
> **Previous versions:** v1 archived at `MARATHON_CAMPAIGN_SPEC_v1_archived.md`.
> v2 history preserved in git.
>
> ## Changes v2 → v2.1
>
> 1. **BLOCKER 1 fixed** (claim SQL): claim CTE now includes
>    `failed_retryable` rows whose `retry_after` has elapsed.
> 2. **BLOCKER 2 fixed** (heartbeat gap): run loop reordered — pauses
>    and breaks happen AFTER work completes, not before claim. Seeds
>    only enter `in_progress` immediately before the deep-pull starts.
> 3. **IMPORTANT 1 fixed** (FSM): explicit `ALLOWED_TRANSITIONS` table
>    added in §2.3.
> 4. **IMPORTANT 2 + 3 + 4 fixed** (attempts + retry exhaustion +
>    adopted heartbeat): `handle_transient_failure` now explicitly
>    defined with attempts/max_attempts check; `verify_remote_state`
>    loops back into `wait_for_job_with_heartbeat` on adopt path so
>    heartbeat continues.
> 5. **IMPORTANT 5 fixed** (circuit breaker persistence): new
>    `propelio_circuit_breaker_state` table; load on startup, persist
>    on every change.
> 6. **IMPORTANT 6 fixed** (SIGINT job_id guard): handler null-checks
>    before remote stop.
> 7. **IMPORTANT 7 fixed** (start_deep_pull_for_seed integration):
>    explicit definition in §4.8 — direct Python call to existing
>    `deep_pull` module, no HTTP indirection.
> 8. **IMPORTANT 8 fixed** (backoff jitter): retry formula now
>    includes ±25% jitter.
> 9. **NICE-TO-HAVE 1, 2** addressed inline.
> 10. **NICE-TO-HAVE 3** (RANDOM scaling) acknowledged but deferred to v3.
>
> **Goal:** Systematically seed the `propelio_comps` cache across the DFW
> metro by running deep-pulls on a pre-computed grid of addresses. Spread
> over multiple days during a normal-looking workday with stealth pacing,
> resumable across interruptions, defensive against Propelio anti-bot.
> End state: 95%+ of organic team Get-Comps clicks hit warm cache.

---

## Locked-in design decisions (KK product calls)

1. **Day-level variability is operator-controlled.** KK starts/stops the
   script at natural-looking times each day. Takes days off manually.
   Script doesn't model a templated workday calendar. **Simpler code,
   maximally natural.**

2. **Grid: pure 8mi spacing.** Clean, auditable, easy to reason about.

3. **Traversal: randomized seed order each session.** Within a run,
   pick seeds in shuffled order, not geographic sweep. **Defeats spatial
   fingerprint without losing grid auditability.**

4. **Filter pattern stays consistent per seed** — mimics analyst
   zoom-out behavior. Shuffling within a seed would look weirder, not
   more natural.

5. **Density-adaptive passes:**
   - Urban + suburban: full 6 passes starting at 0.25mi
   - Rural: 5 passes starting at 0.5mi (skip 0.25mi)
   - Density classified by parcel count within 1mi of subject
     (heuristic threshold: ≤200 parcels = rural)

6. **Freshness display in popups:** show `Captured YYYY-MM-DD`. If
   `first_seen_at::date = today`, prepend 🔥. Bundle into Phase 0.

7. **Pacing target:** 2-3 days for full DFW sweep (~30 pulls/day),
   operator-adjustable.

---

## Defensive priorities (unchanged from v1)

1. Don't burn Propelio. Stealth is non-negotiable.
2. Don't lose progress. State persists in DB.
3. Don't over-scrape. Idempotent re-runs.
4. Observable end-to-end.
5. Pre-work done first.

---

## State machine — formal FSM (Copilot BLOCKER 1)

```
pending → in_progress → completed
                     ↘ verifying → completed | failed_retryable
                     ↘ failed_retryable → (retry, bounded) → in_progress
                                       ↘ failed_final
                     ↘ stopping_requested → completed | failed_retryable
        ↘ skipped (manual operator action)
```

**Terminal states:** `completed`, `failed_final`, `skipped`.
**Retryable states:** `failed_retryable`.
**Active states:** `in_progress`, `verifying`, `stopping_requested`.
**Initial:** `pending`.

DB-enforced via CHECK constraint:
```sql
CHECK (status IN (
  'pending', 'in_progress', 'verifying', 'completed',
  'stopping_requested', 'failed_retryable', 'failed_final', 'skipped'
))
```

State transitions enforced in code via a guard helper that rejects
illegal moves and logs the attempted illegal transition.

---

## Phase 0 — Pre-work (expanded scope)

### 0.1 Schema migration

Adds 13 typed columns from data audit + density classification + freshness
display dependencies. Single migration step in `_run_schema_steps`:

```python
(
    "propelio_comps_extra_typed_v1",
    """
    ALTER TABLE propelio_comps
      ADD COLUMN IF NOT EXISTS address_city TEXT,
      ADD COLUMN IF NOT EXISTS address_zip TEXT,
      ADD COLUMN IF NOT EXISTS address_subdivision TEXT,
      ADD COLUMN IF NOT EXISTS school_district TEXT,
      ADD COLUMN IF NOT EXISTS elementary_school TEXT,
      ADD COLUMN IF NOT EXISTS middle_school TEXT,
      ADD COLUMN IF NOT EXISTS high_school TEXT,
      ADD COLUMN IF NOT EXISTS stories INTEGER,
      ADD COLUMN IF NOT EXISTS pool BOOLEAN,
      ADD COLUMN IF NOT EXISTS unit_count INTEGER,
      ADD COLUMN IF NOT EXISTS listing_timestamp TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS status_timestamp TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS photo_timestamp TIMESTAMPTZ
    """,
),
```

Separate backfill script `scripts/backfill_extra_cols.py` populates these
from existing `raw_payload` for the 3,898 rows already in `propelio_comps`.
Idempotent (`WHERE col IS NULL`).

### 0.2 Update merge_comps_into_global

Extend `archive.py:merge_comps_into_global` to populate the new columns
on every insert/update. Pulls from `extra` or `raw` JSON keys via same
pattern as existing extractions.

### 0.3 Freshness display in popup

Backend: in `archive.py:load_comps_by_polygon` returned dict, add:
```python
comp["first_seen_at"] = first_seen_at.isoformat() if first_seen_at else None
comp["last_seen_at"] = last_seen_at.isoformat() if last_seen_at else None
comp["first_seen_source"] = first_seen_source
```

Frontend: in the unified CAD+MLS popup builder (`frontend/map.js`,
search for popup HTML construction), add a row near the top:

```javascript
const firstSeen = comp.first_seen_at ? new Date(comp.first_seen_at) : null;
const isToday = firstSeen && firstSeen.toDateString() === new Date().toDateString();
const freshLabel = isToday
  ? `🔥 Captured today`
  : firstSeen
    ? `Captured ${firstSeen.toLocaleDateString()}`
    : '';
```

Single line, prepended with fire emoji on same-day captures. No
color/badge complexity for v1.

### 0.4 Add `'campaign_seed'` to allowed source values

No schema change — `first_seen_source` is plain TEXT. Just add to the
marathon scraper's writes.

**Phase 0 estimated effort:** 2-2.5 hours Copilot.

---

## Phase 1 — Grid generation + density classification

### 1.1 DFW bounding box

```python
DFW_BBOX = {
    "lat_min": 32.55,
    "lat_max": 33.10,
    "lng_min": -97.30,
    "lng_max": -96.50,
}
```

~60mi × 50mi rectangle covering Dallas + Tarrant + south Collin + south
Denton counties.

### 1.2 Grid points

8mi cell spacing. ~60-80 grid intersections within bbox.

### 1.3 Snap to real address

For each grid point, find nearest parcel:
```sql
SELECT account_num, address_full, lat, lng, county
FROM parcels
WHERE ST_DWithin(geom::geography, ST_MakePoint(:lng, :lat)::geography, 1609.344)
ORDER BY geom <-> ST_MakePoint(:lng, :lat)
LIMIT 1;
```

Skip grid points with no parcel within 1mi (water, highways, undeveloped).

### 1.4 Density classification

For each snapped seed parcel, count nearby parcels:
```sql
SELECT COUNT(*) FROM parcels
WHERE ST_DWithin(geom::geography, :subject_geom::geography, 1609.344);
```

Classification:
- `> 800` parcels → `'urban'`
- `200-800` parcels → `'suburban'`
- `≤ 200` parcels → `'rural'`

Density class determined ONCE at seed-generation time. Stored on the
seed row. Doesn't re-evaluate during runtime.

Urban + suburban get full 6-pass config. Rural gets 5-pass (skip 0.25).

### 1.5 Seed identity (Copilot IMPORTANT)

Use `parcel_account_num` + `parcel_county` as stable identity, NOT
address text. Addresses normalize differently across data sources.
Account num is canonical.

`seed_address` field on the row is for display only.

### 1.6 Output

Writes directly to `propelio_campaign_seeds` table (see Phase 2). No
intermediate JSON file. Status starts as `'pending'`.

---

## Phase 2 — Campaign state schema (with Copilot's safety additions)

### 2.1 `propelio_campaign_seeds`

```sql
CREATE TABLE IF NOT EXISTS propelio_campaign_seeds (
    seed_id              SERIAL PRIMARY KEY,
    campaign_name        TEXT NOT NULL,

    -- Identity (Copilot IMPORTANT)
    parcel_account_num   TEXT NOT NULL,
    parcel_county        TEXT NOT NULL,

    -- Grid origin (audit trail)
    grid_lat             NUMERIC(10,7) NOT NULL,
    grid_lng             NUMERIC(10,7) NOT NULL,

    -- Seed display info
    seed_address         TEXT NOT NULL,
    seed_lat             NUMERIC(10,7) NOT NULL,
    seed_lng             NUMERIC(10,7) NOT NULL,

    -- Density classification (drives pass config)
    density_class        TEXT NOT NULL
                         CHECK (density_class IN ('urban', 'suburban', 'rural')),
    parcels_within_1mi   INTEGER NOT NULL,

    -- Run state
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN (
                            'pending', 'in_progress', 'verifying',
                            'completed', 'stopping_requested',
                            'failed_retryable', 'failed_final', 'skipped'
                         )),

    -- Concurrency / ownership
    claimed_by           TEXT,         -- runner_instance_id
    heartbeat_at         TIMESTAMPTZ,  -- updated periodically during pull

    -- Linked deep-pull job
    job_id               TEXT,

    -- Retry/backoff
    attempts             INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    retry_after          TIMESTAMPTZ,
    last_error           TEXT,
    last_error_class     TEXT,         -- 'network' | 'timeout' | 'auth' | 'rate_limit' | 'parse' | 'other'

    -- Timestamps
    started_at           TIMESTAMPTZ,
    attempt_started_at   TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metrics
    comps_captured       INTEGER,
    net_new_comps        INTEGER,

    UNIQUE (campaign_name, parcel_county, parcel_account_num)
);

CREATE INDEX idx_campaign_seeds_status
    ON propelio_campaign_seeds (campaign_name, status);
CREATE INDEX idx_campaign_seeds_heartbeat
    ON propelio_campaign_seeds (heartbeat_at)
    WHERE status IN ('in_progress', 'verifying', 'stopping_requested');
CREATE INDEX idx_campaign_seeds_retry_after
    ON propelio_campaign_seeds (campaign_name, retry_after)
    WHERE status = 'failed_retryable';
```

### 2.2 `propelio_campaign_runs` (NEW — Copilot IMPORTANT)

Per-session control plane for clean ops/postmortems:

```sql
CREATE TABLE IF NOT EXISTS propelio_campaign_runs (
    run_id            SERIAL PRIMARY KEY,
    campaign_name     TEXT NOT NULL,
    runner_instance_id TEXT NOT NULL,  -- hostname-pid-uuid
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at          TIMESTAMPTZ,
    end_reason        TEXT,            -- 'clean_stop' | 'sigint' | 'timeout' | 'rate_limit_circuit' | 'auth_block' | 'no_pending_seeds' | 'crash'
    seeds_attempted   INTEGER NOT NULL DEFAULT 0,
    seeds_completed   INTEGER NOT NULL DEFAULT 0,
    seeds_failed      INTEGER NOT NULL DEFAULT 0,
    comps_captured    INTEGER NOT NULL DEFAULT 0,
    net_new_comps     INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);
```

Every script invocation creates a `propelio_campaign_runs` row. End
reason set on exit for postmortems.

### 2.3 State transitions enforced in code

Helper module `scripts/marathon_campaign/state.py` exposes:
```python
def transition(seed_id, from_state, to_state, **fields):
    # Validates allowed transition against ALLOWED_TRANSITIONS table below
    # Atomic UPDATE with WHERE status = from_state
    # Returns True if succeeded, False if state already changed
    # Raises if transition not in ALLOWED_TRANSITIONS
```

#### ALLOWED_TRANSITIONS (v2.1 IMPORTANT 1)

Canonical table — every legal state transition. Rejecting anything
else is a guard against silent corruption.

```python
ALLOWED_TRANSITIONS = {
    # From pending — fresh seed or after orphan reconcile
    ('pending', 'in_progress'),     # claim_next_seed (normal path)
    ('pending', 'skipped'),         # manual operator action

    # From in_progress — active pull
    ('in_progress', 'completed'),         # successful pull
    ('in_progress', 'verifying'),         # local timeout, must verify remote
    ('in_progress', 'failed_retryable'),  # transient error, will retry
    ('in_progress', 'failed_final'),      # max_attempts exhausted
    ('in_progress', 'stopping_requested'),# SIGINT received with job_id
    ('in_progress', 'pending'),           # v2.2: SIGINT before job_id set

    # From verifying — checking remote state after local timeout
    ('verifying', 'completed'),           # remote confirms done
    ('verifying', 'in_progress'),         # adopted back — re-poll
    ('verifying', 'failed_retryable'),    # remote error, retry later
    ('verifying', 'failed_final'),        # v2.2: exhausted retries from verify path

    # From stopping_requested — SIGINT cleanup path
    ('stopping_requested', 'completed'),      # remote finished before stop
    ('stopping_requested', 'failed_retryable'),# remote stopped, retry later
    ('stopping_requested', 'pending'),        # no job_id (caught before start)
    ('stopping_requested', 'in_progress'),    # v2.2: reconcile adopts running orphan

    # From failed_retryable — backoff window
    ('failed_retryable', 'in_progress'),  # claim_next_seed picks it up
    ('failed_retryable', 'failed_final'), # max_attempts reached on retry
    ('failed_retryable', 'skipped'),      # manual operator action

    # From failed_final — terminal, requires manual requeue
    ('failed_final', 'pending'),          # operator `requeue` command
    ('failed_final', 'skipped'),          # operator decides to skip

    # No transitions FROM completed or skipped — terminal.
}
```

Total: 21 explicit edges (v2.2: was 18). Any other UPDATE attempt raises
`IllegalStateTransition`. Implementer should add a property-based test
that fuzzes attempted transitions and verifies illegal ones are
rejected.

Terminal states (no outbound edges): `completed`, `skipped`.
Quasi-terminal (require operator intervention): `failed_final`.

This prevents illegal moves and acts as natural optimistic concurrency.

---

## Phase 3 — Within-day pacing (workday rhythm)

KK controls macro (start/stop the script, choose days). Script handles
micro within an active session:

### 3.1 Inter-seed pacing

After each completed pull:
- 80%: pause 30-90s ("analyst looking at result, making notes")
- 20%: pause 90-180s ("analyst distracted, deeper look")

Existing `_jittered_pass_sleep_seconds()` for inter-pass within a pull.

### 3.2 Coffee/lunch breaks during a session

Even with operator-controlled day boundaries, within a multi-hour
session we want natural breaks (8+ hour grind without a pause looks
robotic). Heuristic:

- Time-since-last-break > 100-130 min (randomized) AND
- Random roll triggers → take a break
  - 70%: short break 8-15 min ("coffee")
  - 25%: medium break 25-40 min ("lunch")
  - 5%: long break 50-75 min ("meeting/errand")

So in a 12-hour session, expect 5-8 breaks. KK can still manually pause
or stop whenever — these are just additional natural-looking gaps.

### 3.3 No fixed clock anchors

Don't say "lunch at 12:30." Say "after ~100-130min working, maybe take
a break." Times emerge naturally from session length, no fixed schedule
to fingerprint.

---

## Phase 4 — Runner script (with all safety fixes)

### 4.1 Entry point

`scripts/marathon_campaign.py`

```bash
# Generate seeds for a new campaign
python -m scripts.marathon_campaign generate --campaign dfw_v1

# Run/resume a campaign (operator-friendly)
python -m scripts.marathon_campaign run --campaign dfw_v1

# Status (any time)
python -m scripts.marathon_campaign status --campaign dfw_v1

# Manual ops
python -m scripts.marathon_campaign skip --seed-id 42 --reason "..."
python -m scripts.marathon_campaign requeue --seed-id 42
```

### 4.2 Run loop pseudocode (v2.1 BLOCKER 2 fix: pauses moved AFTER work)

Critical change from v2: the seed is claimed ONLY immediately before
the deep-pull starts. Pauses and breaks happen AFTER work completes,
BEFORE the next claim. This eliminates the heartbeat gap during long
breaks that previously created false orphans.

```python
def run_campaign(campaign_name, runner_id):
    # Open a campaign_run row + register signal handlers
    run_id = create_run(campaign_name, runner_id)
    session_start_time = NOW()
    register_sigint_handler(run_id)

    # Recover orphans from previous crashed runs (Copilot v1 BLOCKER 3)
    reconcile_orphans(campaign_name)

    # Load persisted circuit breaker state (v2.1 IMPORTANT 5)
    circuit_breaker = CircuitBreaker.load_from_db()

    try:
        while True:
            # Check anti-bot kill switch FIRST
            if circuit_breaker.is_open():
                wait_for_cooldown_or_exit(run_id)
                continue

            # Claim atomically — only when ready to start NOW (BLOCKER 2)
            seed = claim_next_seed(campaign_name, runner_id)
            if seed is None:
                exit_clean(run_id, reason='no_pending_seeds')
                return

            # Start deep-pull IMMEDIATELY after claim — no gap, no break
            try:
                job_id = start_deep_pull_for_seed(seed)
                update_seed_job_id(seed.seed_id, job_id)

                # Poll with heartbeat (30s heartbeat updates inside)
                outcome = wait_for_job_with_heartbeat(
                    job_id, seed.seed_id, timeout_min=15
                )

                if outcome in ('completed', 'saturated'):
                    transition(seed.seed_id, 'in_progress', 'completed',
                               completed_at=NOW(),
                               comps_captured=outcome.total_comps,
                               net_new_comps=outcome.net_new)
                    circuit_breaker.record_outcome('ok')
                elif outcome == 'timeout':
                    # Don't fail yet — verify remote state (v1 BLOCKER 4)
                    transition(seed.seed_id, 'in_progress', 'verifying')
                    verify_remote_state(seed)  # Handles its own re-poll
                elif outcome == 'error':
                    handle_transient_failure(seed, outcome.error,
                                              error_class='remote_error')
                    circuit_breaker.record_outcome('error')
                # 'blocked' / 'stopped' handled inside wait fn via raise

            except PropelioAuthError as exc:
                # 401/403 — immediate full stop, never resume same day
                handle_auth_anomaly(seed, exc)
                exit_emergency(run_id, reason='auth_block')
                return
            except PropelioRateLimitError as exc:
                # 429 — circuit-break with cooldown
                circuit_breaker.trip('rate_limit', cooldown_min=30)
                circuit_breaker.record_outcome('rate_limit')
                handle_transient_failure(seed, exc,
                                          error_class='rate_limit',
                                          retry_min=30)
                continue  # skip pause, jump to top to enter cooldown
            except (NetworkError, TimeoutError) as exc:
                handle_transient_failure(seed, exc, error_class='network')
                circuit_breaker.record_outcome('error')
            except Exception as exc:
                handle_unexpected(seed, exc)
                circuit_breaker.record_outcome('error')

            # === Pauses happen HERE — after work completes ===
            # Seed is now in terminal/retryable state (not in_progress).
            # No live in_progress row during the pause = no heartbeat gap.
            # This eliminates the false-orphan race from v2.
            inter_seed_pause()
            maybe_take_break(session_start_time)

    finally:
        if not run_ended:
            exit_emergency(run_id, reason='crash')
```

**Why this order matters (the v2 BLOCKER 2 root cause):** Previously,
the seed sat in `in_progress` for the inter-seed pause + any break (up
to 40 min for a "lunch"). That exceeded the 15-min orphan threshold,
creating false orphans and double-claim risk if a second runner started
or if `reconcile_orphans` ran from a parallel process. With pauses
after work, the seed is never `in_progress` longer than the actual
pull duration (~5-7 min).

### 4.3 Atomic claim_next_seed (Copilot v1 BLOCKER 2 / v2 BLOCKER 1 fix)

```sql
WITH candidate AS (
    SELECT seed_id FROM propelio_campaign_seeds
    WHERE campaign_name = :campaign_name
      AND (
        status = 'pending'
        OR (
          status = 'failed_retryable'
          -- v2.2: NULL retry_after also eligible. Protects against a
          -- future caller forgetting to set the field; without this,
          -- such rows would be silently invisible to the claim query
          -- (NULL <= NOW() evaluates to NULL = falsy).
          AND (retry_after IS NULL OR retry_after <= NOW())
        )
      )
    ORDER BY RANDOM()  -- Randomized traversal (KK decision)
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE propelio_campaign_seeds s
SET status = 'in_progress',
    claimed_by = :runner_id,
    heartbeat_at = NOW(),
    attempt_started_at = NOW(),
    attempts = attempts + 1,
    updated_at = NOW()
FROM candidate
WHERE s.seed_id = candidate.seed_id
RETURNING s.*;
```

`FOR UPDATE SKIP LOCKED` guarantees only one runner picks up each row.
The OR clause in the candidate CTE handles both fresh pending seeds AND
seeds whose retry backoff has elapsed.

### 4.4 Orphan recovery (Copilot BLOCKER 3)

On startup, before claiming any new work:
```python
def reconcile_orphans(campaign_name):
    # Find rows stuck in active states with stale heartbeat
    stuck = query("""
        SELECT seed_id, status, job_id, heartbeat_at
        FROM propelio_campaign_seeds
        WHERE campaign_name = :campaign_name
          AND status IN ('in_progress', 'verifying', 'stopping_requested')
          AND heartbeat_at < NOW() - INTERVAL '15 minutes'
    """, campaign_name=campaign_name)

    for row in stuck:
        if row.job_id:
            # Check actual remote job state
            remote_status = check_deep_pull_status(row.job_id)
            if remote_status in ('completed', 'saturated'):
                # Job finished but our state didn't catch it
                transition(row.seed_id, row.status, 'completed', ...)
            elif remote_status in ('running',):
                # Job actually still running — adopt it
                transition(row.seed_id, row.status, 'in_progress',
                           heartbeat_at=NOW())
            else:
                # Job dead or unknown — requeue
                transition(row.seed_id, row.status, 'failed_retryable',
                           retry_after=NOW() + 5_minutes,
                           last_error='orphaned_after_crash')
        else:
            # No job_id, just unstick
            transition(row.seed_id, row.status, 'pending')
```

### 4.5 Verifying state for timeout (Copilot v1 BLOCKER 4 + v2 IMPORTANT 4 fix)

```python
def verify_remote_state(seed) -> None:
    """
    Called when local 15-min timeout fires but job may still be running
    remotely. Polls remote 3 times with 60s sleeps, then either resolves
    or re-enters heartbeat loop (v2 IMPORTANT 4 fix — don't fall through).
    """
    for attempt in range(3):
        sleep(60)
        remote = check_deep_pull_status(seed.job_id)
        if remote in ('completed', 'saturated'):
            transition(seed.seed_id, 'verifying', 'completed',
                       completed_at=NOW(), ...)
            return
        elif remote == 'error':
            handle_transient_failure(seed, RemoteError(remote),
                                     error_class='remote_error',
                                     from_state='verifying')
            return
        elif remote == 'stopped':
            # Likely a SIGINT or external stop
            handle_transient_failure(seed, RemoteStopped(),
                                     error_class='remote_stopped',
                                     from_state='verifying',
                                     retry_min=5)
            return
        elif remote == 'blocked':
            # Auth issue caught remotely — escalate
            raise PropelioAuthError(f"Job {seed.job_id} blocked remotely")
        # If still 'running', loop

    # 3 minutes of grace exhausted, still running — adopt back AND re-poll
    # (v2 IMPORTANT 4 fix: don't fall through to next seed; this would
    # orphan the adopted job since no one else is heartbeating it.)
    transition(seed.seed_id, 'verifying', 'in_progress', heartbeat_at=NOW())

    # Re-enter the heartbeat loop for this job with a tighter cap.
    # Total wall time so far: 15 min local + 3 min verify = 18 min.
    # Allow another 15 min before re-verifying, hard cap at 45 min total.
    if seed.attempt_started_at and (NOW() - seed.attempt_started_at) > 45_minutes:
        # Hard cap reached — give up
        handle_transient_failure(seed, RuntimeError("hard cap 45min exceeded"),
                                 error_class='hard_timeout')
        return

    outcome = wait_for_job_with_heartbeat(seed.job_id, seed.seed_id,
                                          timeout_min=15)
    if outcome in ('completed', 'saturated'):
        transition(seed.seed_id, 'in_progress', 'completed', ...)
    elif outcome == 'timeout':
        # Recurse into verify_remote_state — bounded by 45min hard cap above
        transition(seed.seed_id, 'in_progress', 'verifying')
        verify_remote_state(seed)
    else:
        handle_transient_failure(seed, outcome.error,
                                 error_class='post_adopt_error')
```

### 4.6 SIGINT handling (Copilot v2.2 BLOCKER fix: try/except scope)

```python
def on_sigint(signum, frame):
    """
    SIGINT handler. Guards against partial-state corruption.

    v2.2 fix: try/except now wraps the ENTIRE if/else block so both
    branches' state transitions are protected. v2.1 only caught
    transition errors in the job_id branch — the no-job-id branch
    could crash on edge cases (e.g., seed already raced to another
    state by reconcile).
    """
    if current_seed is None:
        # No seed in flight — clean exit
        set_run_end_reason('sigint')
        sys.exit(0)

    try:
        if current_seed.job_id:
            # Job already started — request remote stop, leave to reconcile
            transition(current_seed.seed_id, 'in_progress',
                       'stopping_requested')
            stop_deep_pull_remote(current_seed.job_id)
        else:
            # Seed claimed but job_id never set (SIGINT between claim
            # and start_deep_pull_for_seed). Revert to pending so it
            # gets re-picked next session. Safe because no remote work
            # has started yet. Requires ('in_progress', 'pending') in
            # ALLOWED_TRANSITIONS (added in v2.2).
            transition(current_seed.seed_id, 'in_progress', 'pending')
    except IllegalStateTransition:
        # Race condition: seed already moved to terminal state by some
        # other code path (reconcile, or concurrent worker). Ignore —
        # whatever transitioned it is the canonical state now.
        pass
    except Exception as exc:
        # Any unexpected error in the SIGINT path — log and proceed to
        # exit. Don't let the handler itself crash the process.
        log_error(f"SIGINT handler error: {exc}", exc_info=True)

    set_run_end_reason('sigint')
    sys.exit(0)
```

### 4.7 handle_transient_failure (v2.1 IMPORTANT 2 + 3 fix)

```python
def handle_transient_failure(
    seed,
    exc,
    *,
    error_class: str,
    from_state: str = 'in_progress',
    retry_min: int | None = None,
) -> None:
    """
    Centralized retry/exhaustion logic. Handles attempts/max_attempts
    check that was missing in v2 (IMPORTANT 3) and adds backoff jitter
    (IMPORTANT 8). Used from all transient-failure code paths so
    behavior is consistent.
    """
    # v2.1 IMPORTANT 2: check exhaustion BEFORE scheduling retry
    if seed.attempts >= seed.max_attempts:
        transition(seed.seed_id, from_state, 'failed_final',
                   last_error=str(exc)[:500],
                   last_error_class=error_class,
                   completed_at=NOW())
        log_warning(
            f"Seed {seed.seed_id} ({seed.seed_address}) exhausted "
            f"{seed.max_attempts} attempts (last error: {error_class})"
        )
        return

    # Exponential backoff with jitter (v2.1 IMPORTANT 8 fix)
    if retry_min is None:
        retry_min = 5 * (2 ** seed.attempts)  # 5, 10, 20, 40 min
    jitter_factor = 1.0 + random.uniform(-0.25, 0.25)  # ±25%
    retry_delay_s = int(retry_min * 60 * jitter_factor)
    retry_after = NOW() + timedelta(seconds=retry_delay_s)

    transition(seed.seed_id, from_state, 'failed_retryable',
               last_error=str(exc)[:500],
               last_error_class=error_class,
               retry_after=retry_after)
```

Called from:
- Run-loop `except (NetworkError, TimeoutError)` block
- Run-loop `except PropelioRateLimitError` block (with `retry_min=30`)
- `verify_remote_state` on remote error/stopped/hard-timeout paths

### 4.8 start_deep_pull_for_seed (v2.1 IMPORTANT 7 fix)

The marathon script runs as a separate Python process. Two integration
options were considered:

| Option | Pros | Cons | Decision |
|---|---|---|---|
| HTTP POST to /deep-pull/start | Same path as Mike's UI, integration-tested | Adds auth/HTTP layer for no benefit, harder to test locally | ❌ |
| Direct import of deep_pull module | Same Python process, no auth/HTTP, easy to test | Tighter coupling to internal API | ✅ |

**Decision: direct import.** The marathon script is internal tooling
and lives in the same repo. Coupling is fine.

```python
async def start_deep_pull_for_seed(seed) -> str:
    """
    Trigger a deep-pull for a seed. Returns job_id.
    Uses density-adaptive pass config.
    """
    from api.propelio.deep_pull import (
        create_deep_pull_job,
        run_deep_pull_in_background,
    )

    passes = (
        PASSES_URBAN_SUBURBAN
        if seed.density_class in ('urban', 'suburban')
        else PASSES_RURAL
    )

    job_id = create_deep_pull_job(
        target_address=seed.seed_address,
        passes=passes,
        source='campaign_seed',
    )
    # Kick off as asyncio task — returns immediately
    asyncio.create_task(run_deep_pull_in_background(job_id))
    return job_id


def check_deep_pull_status(job_id: str) -> str:
    """Direct DB query for job status — no HTTP."""
    row = query(
        "SELECT status FROM propelio_deep_pull_jobs WHERE job_id = %s",
        job_id,
    ).fetchone()
    return row[0] if row else 'unknown'


def stop_deep_pull_remote(job_id: str) -> None:
    """Direct DB update — flags the runner to stop on next pass check."""
    query(
        "UPDATE propelio_deep_pull_jobs SET stop_requested = TRUE "
        "WHERE job_id = %s AND status IN ('queued', 'running')",
        job_id,
    )
```

### 4.8.1 Upstream refactors required BEFORE Phase 4 coding (v2.2 IMPORTANT)

Copilot round-3 review identified three concrete blockers in the
existing `deep_pull.py` / `routes.py` that prevent direct invocation
from the marathon script. These MUST be resolved before runner
implementation begins. Doing them as separate small PRs keeps blast
radius contained.

**Prerequisite 1 — Extract `create_deep_pull_job`:**
The job INSERT is currently inlined in `routes.py:761-790` (the
FastAPI handler for POST `/deep-pull/start`). Extract into
`deep_pull.py` as a callable:

```python
# In api/propelio/deep_pull.py
def create_deep_pull_job(
    *,
    target_address: str,
    passes: list[dict] = None,            # v2.2 prereq 2
    source: str = 'user',
    started_by_user_id: int | None = None,  # v2.2 prereq 3
    saved_area_id: str | None = None,
) -> str:
    """Insert a new deep_pull_job row and return job_id."""
    ...
```

Update the route handler to call this. Behavior unchanged for existing
users.

**Prerequisite 2 — Make PASSES configurable per job:**
Currently `deep_pull.py` has a module-level `PASSES` list that
`run_deep_pull(job_id)` reads directly. The marathon needs to supply
either `PASSES_URBAN_SUBURBAN` or `PASSES_RURAL` per seed.

Two implementation options:

(a) **Store in DB column** — add `passes_config JSONB` column to
    `propelio_deep_pull_jobs`. `create_deep_pull_job` writes it; the
    worker reads it.

(b) **Function parameter** — `run_deep_pull(job_id, passes=None)`,
    where `passes=None` falls back to module-level default.

**Recommendation: option (a).** The job row already represents a
self-contained unit of work; embedding its pass config keeps the
state coherent and lets us audit "what config was used for this job"
after the fact.

Migration:
```sql
ALTER TABLE propelio_deep_pull_jobs
  ADD COLUMN IF NOT EXISTS passes_config JSONB;
-- Existing rows fall back to module PASSES default at runtime
```

**Prerequisite 3 — Make `started_by_user_id` nullable:**
The route handler currently requires a user (from
`Depends(get_current_user)`). The marathon script has no session
user.

Simplest fix: drop the NOT NULL constraint (if any) on
`started_by_user_id`. The marathon writes `NULL` here. We already
have `source='campaign_seed'` which clearly identifies the origin.

Migration:
```sql
ALTER TABLE propelio_deep_pull_jobs
  ALTER COLUMN started_by_user_id DROP NOT NULL;
```

Alternative: add a sentinel "system user" row with a known ID and use
that. More clutter for no benefit.

**Prerequisite 4 — Async/sync integration strategy:**
`run_deep_pull(job_id)` is `async def`. The marathon runner pseudocode
is synchronous. Calling `asyncio.create_task()` from sync code raises
`RuntimeError: no running event loop`.

**Decision: marathon runner uses `asyncio.run()` at the top level.**
Make `run_campaign` async, all helpers async-aware. Then
`asyncio.create_task(run_deep_pull_in_background(job_id))` works
because we're inside the event loop.

Alternative considered: `threading.Thread(target=asyncio.run, ...)`.
Rejected — adds thread-safety concerns to the seed state machine.
Single event loop is simpler.

Update the pseudocode in §4.2 to reflect:
```python
async def run_campaign(campaign_name, runner_id):
    ...

# Entry point
if __name__ == "__main__":
    asyncio.run(run_campaign(campaign_name, runner_id))
```

**Estimated prereq work:** 1.5-2 hours total across the three small
refactors. Should be done as Phase 0.5 (between current Phase 0 and
the campaign-specific Phase 1).

### 4.10 Helper stubs referenced from §4.2 (v2.2 minor fix)

Small helpers called from the run loop but not previously defined:

```python
def update_seed_job_id(seed_id: int, job_id: str) -> None:
    """Write the deep-pull job_id back to the seed row after start."""
    query("""
        UPDATE propelio_campaign_seeds
        SET job_id = %s, heartbeat_at = NOW(), updated_at = NOW()
        WHERE seed_id = %s
    """, job_id, seed_id)


async def wait_for_cooldown_or_exit(run_id: int) -> None:
    """
    Called when circuit_breaker.is_open() returns True. Sleep until
    cooldown_until (refreshed periodically in case it gets extended),
    then return so the run loop can re-check and proceed.

    Normally never exits — operator decides via Ctrl-C when to give up.
    A long cooldown is weathered in-place, not killed.

    Safety guard: if MARATHON_MAX_COOLDOWN_WAIT_HOURS env var is set
    AND total cooldown wait exceeds it, log CRITICAL and exit cleanly
    so the session doesn't sit indefinitely without operator awareness.
    """
    max_wait_hours = float(os.environ.get('MARATHON_MAX_COOLDOWN_WAIT_HOURS', '0'))
    wait_started_at = NOW()

    log_info(f"Circuit breaker open, entering cooldown wait")
    while circuit_breaker.is_open():
        # Safety guard: max-total-wait timeout (operator-configurable)
        if max_wait_hours > 0:
            waited = (NOW() - wait_started_at).total_seconds() / 3600
            if waited >= max_wait_hours:
                log_critical(
                    f"Circuit breaker cooldown exceeded "
                    f"MARATHON_MAX_COOLDOWN_WAIT_HOURS={max_wait_hours}h. "
                    f"Exiting session for operator review."
                )
                send_alert_email(
                    subject="Marathon: cooldown timeout exceeded",
                    body=f"Run {run_id} exited after {waited:.1f}h in cooldown",
                )
                exit_emergency(run_id, reason='cooldown_timeout')
                return

        cooldown_left = (circuit_breaker.cooldown_until - NOW()).total_seconds()
        if cooldown_left <= 0:
            # Cooldown expired but maybe rolling window is still elevated.
            # Sleep a short tick and re-check.
            await asyncio.sleep(60)
        else:
            # Sleep until cooldown ends + 30s buffer
            sleep_s = min(cooldown_left + 30, 600)  # max 10min ticks
            log_info(f"Cooldown {sleep_s:.0f}s remaining")
            await asyncio.sleep(sleep_s)
    log_info(f"Circuit breaker reset, resuming campaign")
```

Both are async-aware per the §4.8.1 prerequisite 4 decision (async
runner at top level).

### 4.9 Anti-bot circuit breaker (Copilot v2 IMPORTANT 5 fix: persistence)

```python
class CircuitBreaker:
    """
    State is persisted to propelio_circuit_breaker_state (single-row table).
    Reloads on every script start so a crash near the trip threshold
    doesn't reset the counter and pile on errors.
    """
    def __init__(self, error_window, cooldown_until, consecutive_rate_limits):
        self.error_window = error_window  # deque of last 20 outcomes
        self.cooldown_until = cooldown_until
        self.consecutive_rate_limits = consecutive_rate_limits

    @classmethod
    def load_from_db(cls) -> 'CircuitBreaker':
        row = query("SELECT * FROM propelio_circuit_breaker_state WHERE id = 1").fetchone()
        if not row:
            insert_default_row()
            return cls(deque(maxlen=20), None, 0)
        return cls(
            error_window=deque(row.error_window or [], maxlen=20),
            cooldown_until=row.cooldown_until,
            consecutive_rate_limits=row.consecutive_rate_limits or 0,
        )

    # Track persist failures across the session for chronic-issue debugging
    _persist_fail_count: int = 0
    _last_persist_fail_at: datetime | None = None

    def persist(self):
        """
        Persist state to single-row table. Swallow DB errors —
        in-memory state is authoritative for the session; persistence
        is best-effort to survive crashes. A transient DB blip should
        not kill the campaign. Tracks consecutive failures for chronic
        issue diagnosis.
        """
        try:
            query("""
                UPDATE propelio_circuit_breaker_state
                SET error_window = %s,
                    cooldown_until = %s,
                    consecutive_rate_limits = %s,
                    updated_at = NOW()
                WHERE id = 1
            """, list(self.error_window), self.cooldown_until,
                 self.consecutive_rate_limits)
            # Reset on success
            if self._persist_fail_count > 0:
                log_info(
                    f"Circuit breaker persist recovered after "
                    f"{self._persist_fail_count} consecutive failures"
                )
                self._persist_fail_count = 0
                self._last_persist_fail_at = None
        except Exception as exc:
            self._persist_fail_count += 1
            self._last_persist_fail_at = NOW()
            log_warning(
                f"Circuit breaker persist failed (non-fatal, "
                f"fail#{self._persist_fail_count} at {self._last_persist_fail_at.isoformat()}): "
                f"{exc}. "
                f"In-memory state: cooldown_until={self.cooldown_until}, "
                f"window_size={len(self.error_window)}, "
                f"rate_limit_streak={self.consecutive_rate_limits}"
            )
            if self._persist_fail_count >= 10:
                log_error(
                    f"Circuit breaker persist failed {self._persist_fail_count} times "
                    f"in a row — investigate DB connectivity"
                )

    def record_outcome(self, outcome: str):
        self.error_window.append(outcome)
        if outcome == 'rate_limit':
            self.consecutive_rate_limits += 1
        else:
            self.consecutive_rate_limits = 0
        self.persist()  # Persist on every change

    def is_open(self) -> bool:
        if self.cooldown_until and NOW() < self.cooldown_until:
            return True
        # > 30% errors in last 20 pulls → trip
        if len(self.error_window) >= 10:  # need min samples
            recent_errors = sum(1 for o in self.error_window if o != 'ok')
            if recent_errors / len(self.error_window) > 0.3:
                self.cooldown_until = NOW() + timedelta(hours=1)
                self.persist()
                return True
        return False

    def trip(self, reason: str, cooldown_min: int):
        self.cooldown_until = NOW() + timedelta(minutes=cooldown_min)
        self.persist()
```

Schema for circuit breaker state (add to Phase 0):

```sql
CREATE TABLE IF NOT EXISTS propelio_circuit_breaker_state (
    id                       INTEGER PRIMARY KEY DEFAULT 1
                             CHECK (id = 1),  -- single-row enforced
    error_window             JSONB NOT NULL DEFAULT '[]'::jsonb,
    cooldown_until           TIMESTAMPTZ,
    consecutive_rate_limits  INTEGER NOT NULL DEFAULT 0,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO propelio_circuit_breaker_state (id) VALUES (1)
ON CONFLICT DO NOTHING;
```

Auth errors (401/403) still bypass circuit breaker — they're an
instant shutdown with operator notification, unaffected by rolling
window.

---

## Phase 5 — Pass configs

### 5.1 Three pass families based on density

```python
PASSES_URBAN_SUBURBAN = [
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5,  "label": "blocks"},
    {"months": 24, "range_mi": 1.0,  "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0,  "label": "broader"},
    {"months": 24, "range_mi": 5.0,  "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]

PASSES_RURAL = [
    # Skip 0.25 (too tight for rural — returns near-zero)
    {"months": 24, "range_mi": 0.5,  "label": "blocks"},
    {"months": 24, "range_mi": 1.0,  "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0,  "label": "broader"},
    {"months": 24, "range_mi": 5.0,  "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]
```

### 5.2 Time-axis saturation handling (Copilot IMPORTANT 6)

Time-axis passes (3mo, 12mo) were proposed in v1 for saturation
defense. Copilot rightly noted that adding them doesn't mathematically
solve cap-hit, only helps if you ACT on the saturation signal.

**Decision deferred for v2.** First campaign uses radius-only passes
above. If end-of-day metrics show high cap-hit rate (e.g., >40% of
passes return 100), we add a follow-up tactic in v3:

- Detect cap-hit per pass (returns 99-100)
- For passes that capped, fire a tighter follow-up (e.g., 12mo at same
  radius) within the same seed run
- Recursive but bounded: max 2 follow-up depth

Adding this now would inflate per-seed time. Skip for v2, gather data,
decide for v3.

### 5.3 The 5mi pass is NOT dropped (Copilot IMPORTANT)

In v1 I proposed dropping 5mi for campaign because neighbors cover it.
Copilot pushed back — neighbors might not actually cover sparse zones.

**v2 decision:** keep 5mi pass for both urban/suburban and rural. The
8mi grid spacing means each cell IS the responsibility of its center
seed. Don't rely on neighbors.

---

## Phase 6 — Observability

### 6.1 Per-run heartbeat update

During `wait_for_job_with_heartbeat`:
```python
def wait_for_job_with_heartbeat(job_id, seed_id, timeout_min=15):
    deadline = NOW() + timeout_min * 60
    while NOW() < deadline:
        sleep(30)
        # Update heartbeat
        update_seed_heartbeat(seed_id)
        # Check job status
        status = check_deep_pull_status(job_id)
        if status in ('completed', 'saturated', 'error', 'blocked', 'stopped'):
            return status
    return 'timeout'
```

Heartbeat every 30s during a long pull. Stale heartbeat (15+ min) =
orphan signal for next reconcile.

### 6.2 Daily log file

`logs/marathon_{campaign_name}_{YYYY_MM_DD}.log`. Structured JSON lines
preferable for grep/jq friendliness.

```json
{"ts": "2026-05-12T09:00:00Z", "event": "run_start", "runner_id": "host-1234-abcd", "campaign": "dfw_v1"}
{"ts": "2026-05-12T09:00:30Z", "event": "seed_claim", "seed_id": 1, "address": "2451 Crest Ridge Dr", "density": "suburban"}
{"ts": "2026-05-12T09:06:42Z", "event": "seed_completed", "seed_id": 1, "comps": 398, "net_new": 47, "duration_s": 372}
{"ts": "2026-05-12T09:06:42Z", "event": "inter_seed_pause", "duration_s": 67}
...
```

### 6.3 End-of-run summary

When script exits cleanly (no_pending_seeds or sigint), write a summary
log line and the `propelio_campaign_runs` row with metrics rollup.

CLI status command queries the runs table:
```
Campaign dfw_v1 — Status
========================
Total seeds:       72
Completed:         43  (60%)
Failed:            2
Failed retryable:  3 (next retry at 14:23)
Pending:           24

Last run:          2026-05-12 09:00 to 17:43 (runner: host-1234)
Comps captured:    18,247 total, 5,832 net-new
Avg per seed:      264 comps

Circuit breaker:   closed (no recent issues)
Health:            ✅ all systems normal
```

### 6.4 Alerting (Copilot IMPORTANT)

Two channels:
1. **Stderr/log:** always
2. **Email:** for severity ≥ WARNING via existing SMTP setup (or
   external service like Resend, configurable)

Severity routing:
- `INFO`: log only
- `WARNING`: log + email batched (1 email per day)
- `ERROR`: log + email immediately
- `CRITICAL` (auth block, circuit breaker tripped): log + email + (optional) webhook for SMS later

Operator config in env vars / .env file.

---

## Phase 7 — Failure handling matrix

| Failure Class | Detection | Response | State Transition |
|---|---|---|---|
| Network timeout | aiohttp timeout exception | Retry with backoff | → `failed_retryable`, `retry_after` = NOW + 5min × 2^attempts |
| Connection error | aiohttp connection refused | Same as timeout | Same |
| Propelio 5xx | HTTP status 5xx | Retry with backoff | Same |
| Propelio 429 (rate limit) | HTTP 429 or "throttle" in error msg | Trip circuit breaker, cooldown 30-60 min | → `failed_retryable`, `retry_after` = NOW + cooldown |
| Propelio 401/403 (auth) | HTTP 401/403 | IMMEDIATE STOP, alert CRITICAL | → `failed_final`, exit script |
| Job hung remotely | local timeout 15min, remote still running | Move to `verifying`, poll | → `verifying` → adopt OR fail |
| Parse error | JSON decode or unexpected shape | Log full payload, mark | → `failed_retryable`, attempts++ |
| Parcel match fails | match returns no parcel | Non-fatal, comp lands with NULL parcel | (no state change — comp inserted) |
| Stale orphan | heartbeat > 15min, runner abandoned | Reconcile from remote state | → pending OR completed |
| Operator skip | manual CLI command | Mark as skipped, never retried | → `skipped` |

Retry budget per seed: `attempts < max_attempts` (default 3). Once
exceeded → `failed_final`, requires manual `requeue` op.

---

## Phase 8 — Quality KPIs (Copilot NICE-TO-HAVE)

End-of-day metrics beyond raw counts:

- **Cap-hit rate**: % of passes that returned 99-100 (saturation
  indicator)
- **Net-new ratio**: net_new / comps_captured (cache freshness)
- **Parcel match rate**: matched / total (geocoding health)
- **Duplicate rate**: comps already in cache before this pull
- **Pull duration distribution**: p50, p95, p99
- **Error rate**: failures / attempts

Logged at end-of-day. Surfaced in `status` CLI. Used to detect drift —
if cap-hit jumps from 15% to 50%, we know coverage is degrading.

---

## Phase 9 — Pre-launch checks (expanded)

- [ ] Phase 0 migrations applied on preview + production
- [ ] Phase 0 backfill complete on existing 3,898 comps
- [ ] `merge_comps_into_global` populates new typed columns
- [ ] Freshness display visible in popup (manual test)
- [ ] `propelio_campaign_seeds` + `propelio_campaign_runs` tables exist
- [ ] Seed generation produces sensible addresses + density (eyeball 10)
- [ ] State transition guards reject illegal moves (unit test)
- [ ] Orphan recovery tested (manually create stale `in_progress` row,
      verify reconcile fixes it)
- [ ] SIGINT tested (send SIGINT mid-pull, verify state, restart, verify
      pickup)
- [ ] Atomic claim tested (run two instances simultaneously, verify no
      double-pull)
- [ ] Circuit breaker tested (inject errors, verify trip + cooldown)
- [ ] Daily log file generated
- [ ] End-of-run summary written
- [ ] Email alerts test (warning + critical)

---

## Phase 10 — Operator runbook (NEW)

`docs/propelio/MARATHON_OPERATOR_RUNBOOK.md` (to be written
post-implementation). Topics:

- How to start a session (just `marathon run --campaign X`)
- How to safely stop (Ctrl-C — script handles graceful exit)
- How to check status from another terminal
- What to do if Propelio blocks you (the auth_block path)
- How to manually skip a problematic seed
- How to requeue a `failed_final` seed
- How to launch a new campaign for a new metro
- How to interpret end-of-day metrics
- Red flags and what to do about them

---

## Estimated effort

| Phase | Effort | Owner |
|---|---|---|
| 0. Schema + merge update + freshness | 2-2.5 hr | Copilot |
| 1. Grid + density classification | 1.5 hr | Copilot |
| 2. State schema | 1 hr | Copilot |
| 3. Within-day pacing | 1 hr | Copilot |
| 4. Runner script + all safety mechanisms | 4-5 hr | Copilot |
| 5. Pass configs | 30 min | Copilot |
| 6. Observability + logging + alerting | 2 hr | Copilot |
| 7. Failure handling | 1.5 hr | Copilot |
| 8. KPIs | 1 hr | Copilot |
| 9. Test run | 2 hr | KK + Copilot |
| 10. Runbook | 1 hr | KK |
| **Total** | **18-20 hr** | |

Realistic: 4-5 working days with Copilot reviewing each phase before
moving to next.

---

## What's explicitly out of scope for v2

- Day-level workday simulation (operator-controlled)
- Photo download / backfill (separate effort, Phase 2.6+)
- Multi-county auto-expansion beyond DFW bbox (manual bbox config)
- ML-based saturation detection
- Web UI (CLI status is sufficient)
- Time-axis passes (deferred until v3 based on cap-hit data)

---

## Changes from v1 summary

**Resolved blockers:**
- B1: formal FSM with terminal/retryable states + DB check
- B2: atomic claim with FOR UPDATE SKIP LOCKED + runner_id
- B3: orphan recovery on startup, scans stale active rows
- B4: timeout → verifying → poll remote before fail
- B5: SIGINT writes stopping_requested, reconcile picks up next run
- B6: day variability handled by operator (KK control), not coded
- B7: pure 8mi grid + randomized traversal order (KK decision)

**Resolved importants:**
- Pass sequence: consistent per seed (KK insight), density-adaptive
- 429 vs auth: separate handling, circuit breaker for 429
- Retry policy: exponential backoff with jitter, per-seed budget
- Seed identity: parcel_account_num + county, not address text
- Schema additions: claimed_by, heartbeat, retry_after, attempt_started_at
- Time-axis: deferred to v3 with measurable saturation triggers
- 5mi pass: kept for all density classes
- Campaign control plane: propelio_campaign_runs table added
- Anti-bot kill switch: circuit breaker with cooldown
- Photo bundling: strict separation, deferred
- Alerting: log + email + severity routing

**Resolved nice-to-haves:**
- Quality KPIs added to end-of-day metrics

**New from KK product calls:**
- Freshness display in popups (bundled into Phase 0)
- Density classification stored on seed row
- Density-adaptive pass selection (urban/suburban full, rural skip 0.25)
