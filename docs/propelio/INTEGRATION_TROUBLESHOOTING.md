# Propelio Integration — Troubleshooting Guide

Diagnose silent failures in the Propelio integration: the Refresh flow,
the deep-pull marathon, and the cache pipeline. Each section lists
symptoms first, then root-cause checks, then the fix.

If you don't know what's wrong, start with [Quick triage](#quick-triage).

---

## Quick triage

When something feels off, run these three checks first — they pin almost
every issue we've seen:

```bash
# 1. Are recent Propelio comps actually landing in the cache?
psql "$DATABASE_URL" -c "
  SELECT first_seen_source, COUNT(*),
         MAX(first_seen_at)::timestamp(0) AS latest_insert,
         MAX(last_seen_at)::timestamp(0) AS latest_update
  FROM propelio_comps
  WHERE last_seen_at > NOW() - INTERVAL '3 hours'
  GROUP BY first_seen_source;
"
```

If `latest_insert` is hours old → the Propelio integration isn't writing.

```bash
# 2. Is the marathon runner alive?
psql "$DATABASE_URL" -c "
  SELECT seed_id, status, claimed_by, heartbeat_at::timestamp(0)
  FROM propelio_marathon_seeds
  WHERE status IN ('running','verifying')
  ORDER BY heartbeat_at DESC LIMIT 5;
"
```

Zero rows OR a heartbeat older than 15 minutes → runner is dead.

```bash
# 3. What's the circuit-breaker state for an active campaign?
psql "$DATABASE_URL" -c "
  SELECT campaign_key, status, cb_cooldown_until, cb_failure_count, cb_error_window
  FROM propelio_marathon_campaigns
  WHERE campaign_key = '<your_campaign_here>';
"
```

`cb_cooldown_until` in the future = breaker open, runner won't claim.

---

## Symptom: "I changed filters and clicked Refresh but nothing changed on the map"

### What's actually happening

The Refresh-from-source button POSTs to `/api/propelio/refresh` which calls
`_run_by_polygon(use_cache=False)` and is **supposed to** force a fresh
Propelio call regardless of cache state.

### Root cause check

Look at `api/propelio/routes.py` around line 331 for this gate:

```python
if use_cache and os.environ.get("PHASE_2_CACHE_READ") == "true":
```

Pre-fix (commit `2b8a2ba` on `fix/propelio-refresh-respects-use-cache`),
this line was just `if os.environ.get("PHASE_2_CACHE_READ") == "true":` —
ignoring the caller's `use_cache` flag entirely. Any Refresh click when
that env var was on would return cached comps **without ever calling
Propelio**, silently.

### Diagnosis

1. **Is `PHASE_2_CACHE_READ=true` set in the environment?** Check Cloud Run
   env vars on `lot-ledger-preview` / `lot-ledger-dev`. If set AND your
   branch doesn't have commit `2b8a2ba`, this is the bug.

2. **Confirm by querying recent ingest activity** (the triage query above).
   If `propelio_comps` shows nothing inserted since the last deep-pull job,
   Refresh is no-op'ing.

3. **Verify the fix is in your deployed branch.** Run
   `git log --oneline --grep "respects use_cache"` — should see `2b8a2ba`.

### Fix

Either:
- Cherry-pick commit `2b8a2ba` onto your branch and redeploy, OR
- Flip the env var off if you don't want the Phase 2 cache-read short
  circuit (this disables the entire cache-read optimization, not just
  the bug — only useful as a stopgap)

### Smoke test after fixing

```bash
python -m scripts.propelio_refresh_smoke
```

This calls `_run_by_polygon` directly with `use_cache=False`, then queries
`propelio_comps` to confirm previously-absent pendings now appear.

---

## Symptom: "The deep-pull marathon stopped adding comps to the database"

### What's actually happening

The marathon runner (`scripts/marathon_campaign/runner.py`) is a long-lived
daemon. When it claims a seed, runs all the deep-pull passes, and writes
comps to `propelio_comps`, you see the totals climb. If the daemon dies,
**nothing claims seeds and the totals freeze.** There's no auto-restart.

### Root cause check

Run the triage query #2 above. If no seed has a recent heartbeat, the
runner is gone.

The runner exits cleanly in several cases:
- Circuit breaker tripped (too many recent errors)
- `auth_block` from Propelio (we got rate-limited)
- SIGINT (Ctrl+C / terminal closed)
- Out-of-seeds (campaign actually done)
- Unhandled exception (rare but possible)

None of these cause a crash dump — the process just exits and nobody
notices. The campaign keeps its state and is ready to be resumed.

### Diagnosis

Check `propelio_marathon_campaigns.cb_cooldown_until`:

- **In the past** → breaker cleared, runner just needs to be restarted
- **In the future** → wait for it to clear, then restart
- **Far future (hours)** → consecutive rate limits / auth block — investigate
  why Propelio is rejecting calls before restarting

### Fix

Restart the runner in a session that survives terminal close:

```bash
git checkout feat/marathon-campaign  # runner code lives here, not on develop
tmux new -s marathon
source .venv/bin/activate
python -m scripts.marathon_campaign run --campaign <campaign_key>
# Ctrl+B then D to detach
```

Orphan-reconcile inside the runner will catch any seed left in
`running` or `verifying` with a stale heartbeat and put it back in
the queue. No data is lost.

### Future hardening (open)

The marathon should probably either:
- Auto-restart the daemon when `cb_cooldown_until` clears (systemd unit
  or supervisor that respects the breaker), OR
- Emit a loud alert when last heartbeat is >15 min old (Slack, email, etc.)

Today it just sits silently. See also: [Future hardening](#future-hardening--open-items).

---

## Symptom: "The campaign-aggregates in status CLI all show zero"

### What's actually happening

`propelio_marathon_campaigns` has rollup columns
(`seeds_completed`, `comps_captured`, `net_new_comps`, etc.) that are
supposed to be updated as seeds progress. The per-seed table
(`propelio_marathon_seeds`) tracks state correctly, but the campaign-level
rollup can drift to zero while seeds clearly show progress.

### Diagnosis

Compare what the status CLI says vs the seed table directly:

```bash
# What the campaign aggregates say
psql "$DATABASE_URL" -c "
  SELECT seeds_completed, seeds_failed, seeds_skipped, comps_captured, net_new_comps
  FROM propelio_marathon_campaigns
  WHERE campaign_key = '<key>';
"

# What the seeds table actually has
psql "$DATABASE_URL" -c "
  SELECT status, COUNT(*), SUM(comps_captured), SUM(net_new_comps)
  FROM propelio_marathon_seeds
  WHERE campaign_id = (SELECT campaign_id FROM propelio_marathon_campaigns
                       WHERE campaign_key = '<key>')
  GROUP BY status;
"
```

If the second query has data the first doesn't, **the aggregates are stale.**

### Fix

This is cosmetic — does not affect the runner's ability to claim seeds or
the data layer. Open issue: write a SQL rollup helper that recomputes
campaign aggregates from the seeds table, and call it after every seed
status transition.

Trust the per-seed table, not the campaign aggregates, until this is fixed.

---

## Symptom: "Propelio's UI shows N pendings, but LL shows fewer"

### Three orthogonal causes — work through them in order

#### Cause 1: Polygon containment is stricter than Propelio's radius

Propelio's UI shows comps within a radius of the subject, with the polygon
mostly visual. LL does strict `ST_Contains` on the user's drawn polygon.
Comps that sit slightly outside your polygon but inside Propelio's radius
will appear there and not here. **This is correct behavior, not a bug.**

Confirm by counting strict-containment matches:

```bash
psql "$DATABASE_URL" -c "
  SELECT status, COUNT(*)
  FROM propelio_comps
  WHERE geom IS NOT NULL
    AND ST_Contains(
      (SELECT ST_GeomFromText(
        'POLYGON((' || string_agg(format('%s %s', p[1], p[2]), ',') || '))', 4326)
       FROM saved_areas, LATERAL jsonb_array_elements_text(polygon::jsonb)
       WHERE area_id = '<saved_area_id>'),
      geom)
  GROUP BY status;
"
```

If those numbers match what LL renders, this is the polygon definition.

#### Cause 2: The comps were never pulled in the first place

If a comp is in Propelio's response but not in `propelio_comps`, the
integration never saw it. Most common reason: `months=24` was used
(the default), and that filter is dominated by sold listings — pendings
get cap-clipped out.

Fix: use `months=1` (or 2 or 3) to surface fresh listings. See
[Symptom 1](#symptom-i-changed-filters-and-clicked-refresh-but-nothing-changed-on-the-map)
if your filter change isn't taking effect.

#### Cause 3: Refresh silent-failure (see Symptom 1)

If `PHASE_2_CACHE_READ=true` is set and you don't have commit `2b8a2ba`,
no Refresh ever pulled fresh data and you've been looking at stale cache.

---

## Cap mechanics — what works, what doesn't

We've extensively probed the `POST /legacy/cma/search/{lead_id}/{cma_id}`
endpoint and documented results in
[`memory:propelio-cap-findings-2026-05-13`](../../.claude/projects/-home-kk-projects-clients-lot-ledger/memory/project_propelio_cap_findings_2026_05_13.md).

Short version:

**Works (filter rotation produces fresh slices):**
- `months` (1, 2, 3, 6, 12, 24)
- `range` (string, in miles, e.g. "1", "5")
- `propertyClass` (null or specific class)
- `propertyTypePresets` (array; confirmed values: `SINGLE_FAMILY`, `MULTI_PLEX`)
- `geojson` — sending a custom polygon **does** constrain the search to it

**Doesn't work (silently stripped or ignored):**
- `address: {lat, lon, ...}` — silently stripped, echoed as `{}`
- `lat` / `lon` / `center` / `point` / `subject` at top level — echoed but
  ignored (centroid doesn't move)
- `offset` / `page` — ignored
- `exclude_ids` / `exclude_source_ids` — ignored
- Re-firing the same query — server caches the result, identical comps
  come back

**Pending capture rule of thumb:** `months=1` returns ~12% pendings;
`months=24` returns ~1%. Use tight time windows when pending count matters.

---

## Branch state — heads up

Multiple feature branches are in flight with overlapping fixes:

| Branch | What it has | Refresh-fix included? |
|---|---|---|
| `develop` | Stable baseline | N/A — no Phase 2 cache code, no bug |
| `feat/propelio-deep-pull-experiment` | Phase 2 cache + Refresh button | **No** — has the bug, needs cherry-pick |
| `feat/marathon-campaign` | Branched from above, adds marathon | **No** — has the bug, needs cherry-pick |
| `fix/propelio-refresh-respects-use-cache` | The fix only | Yes — origin |

Before merging any of these to develop, **cherry-pick `2b8a2ba` onto them**
so we don't ship a known-good fix back into a buggy state:

```bash
git checkout feat/marathon-campaign
git cherry-pick 2b8a2ba
git push
```

---

## Future hardening / open items

Things we know need work, but haven't built yet:

- **Auto-restart the marathon runner** when `cb_cooldown_until` clears.
  Today the breaker exits the daemon and nobody restarts it.
- **Alert on heartbeat staleness** — if last marathon heartbeat is >15 min
  old, surface a banner / Slack / email. The integration shouldn't fail
  silently.
- **Campaign-aggregates rollup helper** — recompute from seeds table on
  every status transition so the status CLI never disagrees with reality.
- **Loud surface for Refresh failures** — frontend should show a banner
  if the call returned `{cached: true, phase2_cache: true}` when the user
  expected a fresh pull.

---

## Useful scripts in `scripts/`

| Script | What it does | When to run |
|---|---|---|
| `propelio_refresh_smoke.py` | Calls `_run_by_polygon` directly with `use_cache=False` on the Crest Ridge polygon. Confirms pendings land. | After deploying anything that touches `_run_by_polygon` |
| `propelio_pending_gap.py` | Rotates months × range against an existing CMA and reports per-combo pending counts. | When investigating pending-capture complaints |
| `propelio_property_type_rotation.py` | Tests which `propertyTypePresets` values Propelio accepts. | When adding new property-type filters |
| `propelio_spatial_probe_v2.py` | Tests which body fields the server honors vs strips (noise-floor controlled). | When investigating new bypass parameters |

All probe scripts are READ-ONLY and reuse the existing `PropelioClient`
session. They burn API calls (a few each) but no credits.
