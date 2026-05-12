# Phase 2A — Pass 1 Polish Bundle

> **Status:** spec v1, not yet implemented. Small bundle of two unrelated fixes that surfaced during Phase 2A Pass 1 testing.
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

Add a new column to `propelio_deep_pull_jobs` in the `_ensure_session_schema()` block at `api/main.py:~184`:

```python
# Add as part of the existing CREATE TABLE — but since the table already
# exists (Chunk 1), this needs ALTER TABLE ADD COLUMN IF NOT EXISTS:
cur.execute(
    "ALTER TABLE propelio_deep_pull_jobs "
    "ADD COLUMN IF NOT EXISTS net_new_comps INTEGER NOT NULL DEFAULT 0"
)
```

Place this alongside the existing experimental table DDL, inside the existing `if os.environ.get("DEEP_PULL_EXPERIMENT") == "true":` guard.

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

In `api/propelio/routes.py`, the existing `get_deep_pull_status` endpoint already returns the job row's fields. The new `net_new_comps` column comes along for free — just add to the SELECT and to the response dict.

Modify the SELECT to include `net_new_comps`. Modify the response dict to include `"net_new_comps": int(row[N] or 0)` where N is the new column's position.

(Copilot: find the exact SELECT in routes.py and add the column properly.)

#### Frontend banner

In `frontend/map.js`, `_updateDeepPullBanner` (around line 8390-ish):

```javascript
// Before:
const passCount = `${status.passes_completed}/6`;
textEl.textContent = `${status.status} - Pass ${passCount}, ${status.total_unique_comps} unique so far. Don't refresh.`;

// After:
const passCount = `${status.passes_completed}/6`;
const captured = status.total_unique_comps || 0;
const netNew = status.net_new_comps || 0;
textEl.textContent = `${status.status} - Pass ${passCount}, ${captured} captured (${netNew} net-new). Don't refresh.`;
```

Same pattern in `_pollDeepPullStatus` for the final summary message:

```javascript
_showDeepPullBanner(
  `Job ${resp.status} - ${resp.passes_completed}/6 passes, ${resp.total_unique_comps} captured (${resp.net_new_comps || 0} net-new). Job ID: ${finishedJobId}`
);
```

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

## Open question (resolve in review or implementation)

**Should `_pollDeepPullStatus` also surface "net_new" in the post-completion `alert(...)` (currently shows total uniques)?**

Looking at the existing experimental code at `_pollDeepPullStatus`, the dev-button completion path shows the totals. We've already updated the in-progress banner — should the completion popup also reflect net-new?

**Recommendation:** YES, update for consistency. Banner format should be the same whether running or final.

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
