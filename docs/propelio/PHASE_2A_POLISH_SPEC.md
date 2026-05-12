# Phase 2A — Pass 1 Polish Bundle

> **Status:** spec v1.1, not yet implemented. Small bundle of two unrelated fixes that surfaced during Phase 2A Pass 1 testing.
>
> **Revision v1.1 (2026-05-12):** Applied Copilot first-review cleanups:
> - Schema migration now uses `_run_schema_steps` centralized pattern (matches existing codebase convention)
> - Status endpoint row indexes explicitly mapped (net_new_comps at index 4, subsequent fields shifted)
> - Frontend uses defensive `Number(... || 0)` defaulting for pre-fix jobs without the new field
> - Removed stale "alert(...)" wording — both code paths use banners, not alerts
>
> **Branch:** `feat/propelio-deep-pull-experiment` (continue)
>
> **Scope:** 1 frontend bug fix + 1 backend+frontend feature addition. Single chunk to keep the deploy/review cycle short.

---

## Defensive priorities

1. **Backwards compat:** existing `total_unique_comps` field on `propelio_deep_pull_jobs` stays unchanged. New `net_new_comps` field is additive.
2. **No DB data risk:** schema add only (no migration of existing rows; new column gets default 0).
3. **No breaking changes to frontend:** banner text format change is purely cosmetic.

---

## Fix 1 — Workspace save race condition

### Problem

After Phase 2A Pass 1 shipped, KK observed: drawing a polygon auto-saves the workspace AND cache fires correctly, but the sidebar still shows "Unsaved Area" label.

### Root cause

In `frontend/map.js` line 7034 (in the `map.on("draw:created", ...)` handler), the analysis flow unconditionally calls `setActiveItem("Unsaved area", "Unsaved")` after the polygon analysis completes. This overrides the workspace label that was set by `_autoCacheOnDraw`'s `saveCurrentArea` call (which sets "Workspace" via `setActiveItem`).

Sequence:
1. Draw completes → `void _autoCacheOnDraw()` starts → calls `saveCurrentArea(name)`
2. `saveCurrentArea` API call completes → sets `_currentLoadedAreaId` + `setActiveItem("Workspace", ...)`
3. Analysis API call completes (longer than save) → `setActiveItem("Unsaved area", "Unsaved")` OVERRIDES the workspace label

The workspace IS being saved correctly — the DB row exists. Just the UI label gets visually clobbered.

### Fix

In `frontend/map.js`, line 7034. Wrap the existing call in a defensive check:

```javascript
// Before:
setActiveItem("Unsaved area", "Unsaved");

// After:
if (!_currentLoadedAreaId) {
  setActiveItem("Unsaved area", "Unsaved");
}
```

That's it. 3-line change (1 modification + 2 wrapping lines).

### Why this works

- If auto-save completes first (sets `_currentLoadedAreaId`): analysis skips "Unsaved area" → "Workspace" label stays
- If analysis completes first (`_currentLoadedAreaId` still null): analysis sets "Unsaved area" → auto-save completes later → save's own `setActiveItem("Workspace", ...)` overrides → "Workspace" sticks

Both orderings now resolve to "Workspace" displayed correctly.

### Files modified
- `frontend/map.js` — single block around line 7034

### Smoke test
1. Draw a polygon over Crest Ridge (or any covered area)
2. Cached comps appear
3. **EXPECTED:** within ~1 sec, the active-item slot shows "Workspace: <name>" (NOT "Unsaved Area")
4. Confirm in the saved-areas sidebar: a new workspace row appears
5. Cloud SQL: SELECT * FROM saved_areas WHERE name = <auto-derived> — row exists

---

## Fix 2 — "Net-new to cache" metric on deep-pull

### Problem

The deep-pull banner currently shows "X unique comps so far" — but that's measuring unique-within-this-run (after deduping the 6 passes). Useless to analysts. What they need is **net-new to the global cache**: how many of these comps weren't in propelio_comps before this run.

### Reframe

```
Current banner:  "Pass 5/6 — 233 unique so far"
Better banner:   "Pass 5/6 — 233 captured (47 net-new)"
```

- "47 net-new" = 47 comps weren't in propelio_comps before this run
- The other 186 were already cached (deep-pull refreshed their last_seen_at)

For Quick Refresh later: "Captured 8 · 0 net-new" = saturated, "Captured 8 · 3 net-new" = Propelio has fresh listings.

### Implementation

#### Backend schema

Add to the project's existing centralized post-create migration block via `_run_schema_steps` at `api/main.py:462-533`. This is the established pattern for additive column migrations:

```python
# Inside the _run_schema_steps([...]) list, add a new step:
(
    "deep_pull_jobs_net_new_comps",
    "ALTER TABLE propelio_deep_pull_jobs ADD COLUMN IF NOT EXISTS "
    "net_new_comps INTEGER NOT NULL DEFAULT 0",
),
```

The step name `deep_pull_jobs_net_new_comps` is just an identifier for the migration log. The ALTER is idempotent so re-runs are safe.

**Note:** the `propelio_deep_pull_jobs` table itself is only created when `DEEP_PULL_EXPERIMENT=true` is set, but `_run_schema_steps` runs unconditionally. Make sure the ALTER step is also gated on the table existing OR is positioned to fail-soft if the table is absent.

**Acceptable alternative** (if `_run_schema_steps` gating gets messy): add the ALTER inline inside the existing `if os.environ.get("DEEP_PULL_EXPERIMENT") == "true":` guard immediately after the jobs table CREATE. Less centralized but simpler.

**Existing rows get DEFAULT 0.** That's fine — the metric only matters for NEW runs going forward.

#### Backend runner

In `api/propelio/deep_pull.py`:

The runner currently calls `merge_comps_into_global(matched_for_global, source="deep_pull")` inside `_insert_pass_comps` (around line 256+). That function returns `{"inserted": N, "updated": M}` already. Capture and accumulate.

Inside `_insert_pass_comps`, change:

```python
# Existing:
matched_for_global = match_comps_to_parcels(parsed_for_global)
merge_comps_into_global(matched_for_global, source="deep_pull")
```

To:

```python
matched_for_global = match_comps_to_parcels(parsed_for_global)
merge_result = merge_comps_into_global(matched_for_global, source="deep_pull")

# Update the job's net_new_comps counter (additive, non-fatal)
try:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE propelio_deep_pull_jobs
                SET net_new_comps = net_new_comps + %s
                WHERE job_id = %s
                """,
                (int(merge_result.get("inserted", 0)), job_id),
            )
        conn.commit()
    finally:
        release_session_conn(conn)
except Exception as _exc:
    logger.warning("[deep-pull job=%s] net_new_comps update failed (non-fatal): %s", job_id, _exc)
```

Same try/except/swallow pattern as the existing global write. The metric is best-effort; a failure to update the counter doesn't break the pull.

#### Backend status endpoint

In `api/propelio/routes.py`, the existing `get_deep_pull_status` endpoint at `routes.py:804-857` has a SELECT and response dict mapping. Update both.

**Current SELECT** (routes.py:804-807):
```sql
SELECT job_id, status, passes_completed, total_unique_comps,
       last_pass_at, next_pass_at, last_error, started_at, stop_requested
FROM propelio_deep_pull_jobs WHERE job_id = %s
```

**Updated SELECT** — insert `net_new_comps` after `total_unique_comps`:
```sql
SELECT job_id, status, passes_completed, total_unique_comps, net_new_comps,
       last_pass_at, next_pass_at, last_error, started_at, stop_requested
FROM propelio_deep_pull_jobs WHERE job_id = %s
```

**Updated row index mapping** in the response dict (was: 0=job_id, 1=status, 2=passes_completed, 3=total_unique_comps, 4=last_pass_at, ...). After insertion:

| Index | Field |
|---|---|
| 0 | job_id |
| 1 | status |
| 2 | passes_completed |
| 3 | total_unique_comps |
| **4** | **net_new_comps** (NEW) |
| 5 | last_pass_at |
| 6 | next_pass_at |
| 7 | last_error |
| 8 | started_at |
| 9 | stop_requested |

Add to the response dict:
```python
"net_new_comps": int(row[4] or 0),
```

Shift the subsequent indexes by 1 (last_pass_at goes from row[4] → row[5], etc).

#### Frontend banner

In `frontend/map.js`, `_updateDeepPullBanner` at `map.js:8400`:

```javascript
// Before:
const passCount = `${status.passes_completed}/6`;
textEl.textContent = `${status.status} - Pass ${passCount}, ${status.total_unique_comps} unique so far. Don't refresh.`;

// After:
const passCount = `${status.passes_completed}/6`;
const captured = Number(status?.total_unique_comps || 0);
const netNew = Number(status?.net_new_comps || 0);
textEl.textContent = `${status.status} - Pass ${passCount}, ${captured} captured (${netNew} net-new). Don't refresh.`;
```

Same pattern in `_pollDeepPullStatus` final summary banner at `map.js:8459-8462`:

```javascript
const captured = Number(resp?.total_unique_comps || 0);
const netNew = Number(resp?.net_new_comps || 0);
_showDeepPullBanner(
  `Job ${resp.status} - ${resp.passes_completed}/6 passes, ${captured} captured (${netNew} net-new). Job ID: ${finishedJobId}`
);
```

**Defensive defaulting:** `Number(... || 0)` ensures pre-fix jobs (where `net_new_comps` field is missing or zero) render as "0 net-new" instead of "undefined net-new" or "null net-new." Backward compat with any pending old jobs.

### Files modified
- `api/main.py` — add ALTER TABLE inside the existing DEEP_PULL_EXPERIMENT-guarded DDL block
- `api/propelio/deep_pull.py` — capture & accumulate `inserted` count from merge_comps_into_global
- `api/propelio/routes.py` — include `net_new_comps` in /status response
- `frontend/map.js` — update banner text format in `_updateDeepPullBanner` + `_pollDeepPullStatus`

### Smoke test

1. Pre-test count: `SELECT COUNT(*) FROM propelio_comps` (note this number)
2. Run a deep pull on an already-covered area (Lakewood)
3. Watch banner — should show something like "Pass 3/6 - 198 captured (12 net-new)"
4. Most comps already cached → small net-new count expected (because cache already had them)
5. After completion: post-count comparison → `propelio_comps` count grew by the final `net_new_comps` number
6. Run another deep pull immediately on an UNCOVERED area
7. **EXPECTED:** the new area run shows high net-new (most/all captured = net-new)
8. SQL verify: `SELECT net_new_comps, total_unique_comps FROM propelio_deep_pull_jobs ORDER BY started_at DESC LIMIT 5` — recent runs should have correct numbers

---

## Resolved (Copilot review)

- Schema migration uses `_run_schema_steps` pattern at `main.py:462-533` (with optional fall-back to inline if gating is awkward).
- Status endpoint row indexes explicitly mapped (net_new_comps at index 4, subsequent fields shifted by 1).
- Frontend uses defensive `Number(... || 0)` defaulting so missing fields on pre-fix jobs don't break rendering.
- Both in-progress banner (`_updateDeepPullBanner` at `map.js:8400`) AND completion banner (`_pollDeepPullStatus` at `map.js:8459-8462`) updated for consistency. No `alert()` involved — those code paths use the banner.

---

## What this does NOT do

- **Doesn't change `total_unique_comps` semantics** — it stays as "unique within this job after deduping passes." Backward compat preserved.
- **Doesn't fix the stale-sweep "error" label** — that's item 2.5b in testing notes, separate chunk.
- **Doesn't add daily rate limits on Get Comps** — Phase 2.5.
- **Doesn't tackle the "todo thing on mouse"** — KK hasn't described what this is yet; will address in a separate chunk once we identify it.

---

## Estimated effort

- Fix 1 (workspace race): 3 lines, ~5 min review
- Fix 2 (net-new metric):
  - Schema ALTER: 5 min
  - Runner update: 15 min
  - Status endpoint: 10 min
  - Frontend banner: 10 min
  - Smoke test: 10 min
- **Total: ~45-60 min Copilot work**

Single deploy. KK reviews, smoke-tests, ships.
