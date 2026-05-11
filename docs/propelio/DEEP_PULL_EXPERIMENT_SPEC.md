# Propelio Deep-Pull Experiment — Copilot Build Spec

> **Status:** experimental scaffold on branch `feat/propelio-deep-pull-experiment`. Throwaway tables, throwaway UI. Goal: prove the multi-pass saturation strategy works before designing the permanent comps DB.
>
> **Branch:** `feat/propelio-deep-pull-experiment` (off `develop`)
> **Deploys to:** `lot-ledger-preview` Cloud Run service via `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`. DO NOT push to `develop` until KK approves.

---

## What we're testing

When a user clicks "Deep Pull" on a workspace, the backend cycles through 6 Propelio CMA passes with proximity-first ordering and jittered stealth pacing. Each pass stores its raw results in a throwaway table with pass metadata. We then inspect SQL to validate: are we actually getting more unique comps than a single Propelio pull? At what saturation point do additional passes stop adding new comps?

This is a research scaffold. It does NOT touch the production scraper, the existing comp archive, or the cache layer.

---

## Files to create or modify

### Create
- `api/propelio/deep_pull.py` — all experimental logic (runner, pass config, DB writes)

### Modify
- `api/main.py` — add the two CREATE TABLE statements to the existing startup table-creation block (find the section that creates `saved_areas`, etc.)
- `api/propelio/routes.py` — add 3 new endpoints under `/api/propelio/deep-pull/`. Do NOT touch existing endpoints.
- `frontend/map.js` — add Deep Pull button handler + status polling. Place new code at the end of the propelio section; do NOT refactor existing propelio code.
- `frontend/index.html` — add button markup + banner markup inside the propelio sidebar card (find `prop-status-row` or wherever Get Comps lives).
- `frontend/style.css` — minor styles for `.deep-pull-banner` (status display) and `.deep-pull-dev-only` (button).

### Do NOT modify
- `api/propelio/scraper.py` — use it as-is. Import `search_properties` and `search_cma` from it.
- `api/propelio/archive.py` — no integration with workspace archive in this experiment.
- `api/propelio/cache.py` — this experiment bypasses caching entirely.
- `api/propelio/parcel_match.py` — out of scope.

---

## Schema (add to `api/main.py` startup table block)

```sql
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
        CHECK (status IN ('queued', 'running', 'completed', 'stopped', 'error', 'saturated')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_pass_at TIMESTAMPTZ,
    next_pass_at TIMESTAMPTZ,
    passes_completed INTEGER NOT NULL DEFAULT 0,
    total_unique_comps INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_deep_pull_jobs_status
    ON propelio_deep_pull_jobs (status, next_pass_at);

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
);

CREATE INDEX IF NOT EXISTS idx_deep_pull_exp_job
    ON propelio_deep_pull_experiment (job_id, pass_num);
```

These are explicitly experimental table names. They will be dropped once the real comps DB lands.

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

PACING_MIN_SECONDS = 30
PACING_MAX_SECONDS = 60
SATURATION_MIN_PASSES = 3        # don't saturate before pass 3
SATURATION_THRESHOLD = 0.05      # stop if new comps < 5% of total
```

---

## The runner (in `deep_pull.py`)

```python
async def run_deep_pull(job_id: str) -> None:
    """Background task. Cycles through PASSES with jittered pacing.

    Stealth rules baked in:
    - One Propelio call at a time. Sequential awaits. No asyncio.gather.
    - Jittered sleep BEFORE each pass (including pass 1) using random.uniform().
    - Reuse session (scraper handles this — don't re-login per pass).
    - Reuse lead_id + cma_id captured on pass 1 for all subsequent passes
      (call search_cma directly instead of search_properties).
    - If any pass raises an exception (especially 429, auth fail, anything non-2xx),
      mark job status='error', store the exception message in last_error, RAISE.
      DO NOT retry. DO NOT exponential-backoff. Fail-stop.
    - Check stop_requested at the top of each iteration; bail if set.
    """
```

Pseudocode:

```
1. Open new DB connection
2. SELECT row from propelio_deep_pull_jobs WHERE job_id = ?
   If not found OR status not in ('queued', 'running'): return
3. UPDATE status='running', started_at=NOW() if first run
4. For pass_num, pass_config in enumerate(PASSES, start=1):
   a. Re-read job row, check stop_requested. If True: UPDATE status='stopped'; return
   b. sleep_seconds = random.uniform(PACING_MIN_SECONDS, PACING_MAX_SECONDS)
   c. UPDATE next_pass_at = NOW() + sleep_seconds
   d. await asyncio.sleep(sleep_seconds)
   e. logger.info("[deep-pull job=%s] Pass %d starting: months=%d range=%.2fmi label=%s",
                  job_id, pass_num, pass_config["months"], pass_config["range_mi"], pass_config["label"])
   f. If pass_num == 1:
        # First pass: full search_properties (gets lead_id + cma_id + first comps)
        subject, comps = await asyncio.to_thread(
            scraper.search_properties,
            job_row.target_address,
            radius=None,  # use Propelio default
            months=pass_config["months"],
            range_mi=pass_config["range_mi"],
        )
        # Extract lead_id and cma_id from the scraper for reuse
        lead_id = subject.extra.get("cma_id") or scraper_last_lead_id  # however the scraper exposes this
        cma_id = subject.extra.get("cma_id") or ...
        UPDATE propelio_deep_pull_jobs SET lead_id = ?, cma_id = ? WHERE job_id = ?
      Else:
        # Subsequent passes: reuse lead_id + cma_id via search_cma
        comps = await asyncio.to_thread(
            scraper.search_cma_widened,  # or whatever wrapper makes this clean
            lead_id, cma_id,
            months=pass_config["months"],
            range_mi=pass_config["range_mi"],
        )
   g. logger.info("[deep-pull job=%s] Pass %d returned %d comps", job_id, pass_num, len(comps))
   h. For each comp in comps:
        comp_addr_key = compute_address_key(comp)  # reuse archive._comp_address_key logic
        is_first_seen = NOT EXISTS in this job
        INSERT INTO propelio_deep_pull_experiment (job_id, pass_num, months, range_mi, label,
                                                    comp_address_key, comp_data, is_first_seen_in_job)
        ON CONFLICT (job_id, pass_num, comp_address_key) DO NOTHING
   i. SELECT
        COUNT(*) FILTER (WHERE pass_num = ?) AS returned_this_pass,
        COUNT(*) FILTER (WHERE pass_num = ? AND is_first_seen_in_job) AS new_this_pass,
        COUNT(DISTINCT comp_address_key) AS total_unique
      FROM propelio_deep_pull_experiment WHERE job_id = ?
   j. UPDATE propelio_deep_pull_jobs
        SET passes_completed = ?,
            total_unique_comps = ?,
            last_pass_at = NOW()
   k. logger.info("[deep-pull job=%s] Pass %d done: returned=%d new=%d total_unique=%d",
                   job_id, pass_num, returned, new_this_pass, total_unique)
   l. Saturation check:
      If pass_num >= SATURATION_MIN_PASSES AND new_this_pass / max(total_unique, 1) < SATURATION_THRESHOLD:
        logger.info("[deep-pull job=%s] Saturated after pass %d", job_id, pass_num)
        UPDATE status='saturated'
        return
5. UPDATE status='completed'
```

**Note on extracting lead_id + cma_id:** the scraper module today doesn't return them cleanly from `search_properties`. You may need to either:
- Add a thin wrapper in `deep_pull.py` that calls `scraper.find_lead_id()` + `scraper.add_cma()` + `scraper.search_cma()` directly and captures all three IDs, OR
- Add a minimal optional return value to the scraper that exposes lead_id and cma_id alongside the comps.

Use whichever is less invasive. The wrapper approach is preferred since it doesn't modify the scraper.

---

## Endpoints (in `api/propelio/routes.py`)

All require `Depends(get_current_user)`.

```python
from pydantic import BaseModel
from typing import Optional

class DeepPullStartRequest(BaseModel):
    target_address: str
    saved_area_id: Optional[str] = None

@router.post("/api/propelio/deep-pull/start")
async def start_deep_pull(
    request: DeepPullStartRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Insert job row with status='queued', spawn background task, return job_id.

    Validates:
    - target_address is non-empty
    - user role allows experiment (developer or owner)
    """
    if user.get("role") not in ("developer", "owner"):
        raise HTTPException(status_code=403, detail="Deep pull is developer-only")
    job_id = "dp_" + secrets.token_urlsafe(8)
    # INSERT INTO propelio_deep_pull_jobs (job_id, target_address, saved_area_id,
    #                                       started_by_user_id, status='queued')
    asyncio.create_task(run_deep_pull(job_id))
    return {"job_id": job_id}

@router.get("/api/propelio/deep-pull/status/{job_id}")
async def get_deep_pull_status(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Returns job state + per-pass breakdown.

    Response shape:
    {
      "job_id": ...,
      "status": "queued|running|completed|stopped|error|saturated",
      "passes_completed": N,
      "total_unique_comps": N,
      "last_pass_at": "...",
      "next_pass_at": "...",
      "last_error": "..." or null,
      "per_pass": [
        { "pass_num": 1, "months": 24, "range_mi": 0.25, "label": "tightest",
          "returned": 47, "new": 47, "completed_at": "..." },
        ...
      ]
    }
    """

@router.post("/api/propelio/deep-pull/stop/{job_id}")
async def stop_deep_pull(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Sets stop_requested=true. Runner checks this at top of next iteration."""
```

---

## Frontend

### `frontend/index.html`

Find the propelio sidebar card (look for the existing Get Comps / Refresh buttons). Add adjacent to those buttons:

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

Add minimal styles at the end of the file:

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
  animation: deep-pull-spin 1.5s linear infinite;
}
@keyframes deep-pull-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
```

### `frontend/map.js`

At the end of the propelio section, add (no edits to existing functions):

```javascript
// =============================================================================
// EXPERIMENTAL: Propelio Deep Pull (dev-only, throwaway scaffold)
// =============================================================================
let _deepPullPollTimer = null;
let _activeDeepPullJobId = null;

function _maybeShowDeepPullButton() {
  const btn = document.getElementById("btn-deep-pull");
  if (!btn) return;
  const role = (currentUser && currentUser.role) || "";
  if (role === "developer" || role === "owner") {
    btn.style.display = "inline-block";
  } else {
    btn.style.display = "none";
  }
}

async function startDeepPull() {
  // Derive target address from current workspace/search
  const address = lastSearchedAddress || lastTargetAddress || null;
  if (!address) {
    alert("No target address. Search for an address or load a saved workspace first.");
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
    _showDeepPullBanner("Pass 0/6, queued — first pass in ~30-60s...");
    _deepPullPollTimer = setInterval(_pollDeepPullStatus, 5000);
  } catch (err) {
    console.error("[deep-pull] start failed:", err);
    alert("Deep pull failed to start: " + (err.message || err));
  }
}

async function _pollDeepPullStatus() {
  if (!_activeDeepPullJobId) return;
  try {
    const resp = await _apiJson(`/api/propelio/deep-pull/status/${_activeDeepPullJobId}`);
    console.log("[deep-pull] status tick:", resp);
    _updateDeepPullBanner(resp);
    if (["completed", "saturated", "stopped", "error"].includes(resp.status)) {
      clearInterval(_deepPullPollTimer);
      _deepPullPollTimer = null;
      const finishedJobId = _activeDeepPullJobId;
      _activeDeepPullJobId = null;
      console.log("[deep-pull] FINAL summary:", resp);
      _hideDeepPullBanner();
      alert(
        `Deep pull ${resp.status}.\n\n` +
        `Passes completed: ${resp.passes_completed}\n` +
        `Total unique comps: ${resp.total_unique_comps}\n\n` +
        `Job ID: ${finishedJobId}\n` +
        `Check the DB for full pass-by-pass breakdown.`
      );
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
  textEl.textContent = `${status.status} — Pass ${status.passes_completed}/6, ${status.total_unique_comps} unique comps so far`;
}

function _hideDeepPullBanner() {
  const banner = document.getElementById("deep-pull-banner");
  if (banner) banner.classList.add("hidden");
}

// Wire up the buttons
document.getElementById("btn-deep-pull")?.addEventListener("click", startDeepPull);
document.getElementById("btn-deep-pull-stop")?.addEventListener("click", stopDeepPull);

// Call _maybeShowDeepPullButton() wherever currentUser is set after login
```

You'll need to find where `currentUser` is populated after auth check and call `_maybeShowDeepPullButton()` there.

---

## Smoke test (KK runs after Copilot ships)

1. `git status` clean, on branch `feat/propelio-deep-pull-experiment`
2. Deploy: `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`
3. Open `https://lot-ledger-preview-505466930182.us-central1.run.app`
4. Log in as developer
5. Search for a known address (e.g., one with a workspace already saved)
6. Click "Deep Pull (dev)" button
7. Watch console.log stream — should see status ticks every 5s
8. Banner should update through Pass 1 → 2 → 3 → ...
9. Wait ~5-10 minutes for full 6-pass run (or saturation early-exit)
10. After completion, inspect DB via Cloud SQL Studio:

```sql
-- Job summary
SELECT * FROM propelio_deep_pull_jobs ORDER BY started_at DESC LIMIT 5;

-- Per-pass breakdown
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

-- Compare deep-pull total vs what a single Propelio pull would give
SELECT
  COUNT(DISTINCT comp_address_key) AS unique_comps
FROM propelio_deep_pull_experiment
WHERE job_id = '<latest_job_id>';
```

**What "success" looks like:**
- Pass 1 returns some number, all marked `is_first_seen_in_job = true`
- Pass 2 returns more than pass 1 (broader radius), with mixed new/duplicate
- By pass 4-5, `new_in_pass / total` is dropping
- Saturation may kick in at pass 3-4 for dense suburban areas; rural may go all 6
- Total unique comps > what a single Propelio pull would return for the same address

**What failure looks like:**
- Total unique comps = single-pass count (variation in radius didn't expose new comps) — strategy doesn't work
- Propelio errored on pass 2+ (429, auth fail) — pacing/session issues
- Job stuck in "running" — runner crashed silently

---

## What this experiment does NOT do (yet)

- No write to permanent `propelio_comps` table (doesn't exist yet)
- No CSV integration
- No workspace integration beyond capturing `saved_area_id` for reference
- No frontend rendering of the temp table data — inspect via SQL during this experiment
- No variation of months axis — only distance (keeps experiment focused)
- No retry/backoff (fail-stop on any error, as designed)
- No metric beyond unique comp counts (no quality scoring, no comparison to existing archive)

---

## When the experiment is "done"

KK runs deep pulls against 3-5 different known addresses (one urban, one suburban, one rural). For each:
- Total comps captured > single-pass count? (yes = strategy works)
- Pacing actually stealth-looking? (logs show jittered intervals 30-60s)
- Errors? (any 4xx/5xx from Propelio = redesign)

If yes-yes-no across all test cases, we proceed to build the permanent `propelio_comps` table and wire the deep-pull into production-real comps.
