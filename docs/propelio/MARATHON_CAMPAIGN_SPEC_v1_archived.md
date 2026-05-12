# Marathon Seeding Campaign — Spec v1

> **Status:** Draft, awaiting Copilot deep review.
>
> **Goal:** Build a script that systematically seeds the `propelio_comps`
> cache across the DFW metro by running deep-pulls on a pre-computed grid
> of addresses, simulating a normal analyst workday, resumable across
> days. End state: 95%+ of organic team Get-Comps clicks hit warm cache
> sub-second.
>
> **Owner:** TBD (Copilot build candidate, KK reviews).

---

## Defensive priorities

1. **Don't burn Propelio.** The campaign WILL run for weeks. Any
   anti-bot flag = catastrophic data loss. Stealth pacing is
   non-negotiable.
2. **Don't lose progress.** Run resumes across days, machine reboots,
   Ctrl-C interrupts. State persists in DB, not memory.
3. **Don't over-scrape.** Idempotent — re-running on already-completed
   seeds is a no-op. ON CONFLICT in propelio_comps does the dedup work.
4. **Observable.** Operator can see where the campaign is at any time,
   end-of-day summary, alerts on errors.
5. **Pre-work done before run.** Schema additions from
   `DATA_AUDIT_PRE_CAMPAIGN.md` must ship first.

---

## Phase 0 — Pre-work (must complete before campaign launches)

### 0.1 Migration — add typed columns

Single migration to `api/main.py:_run_schema_steps`:

```python
(
    "propelio_comps_extra_typed_cols_v1",
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

Plus a separate one-shot backfill script `scripts/backfill_extra_cols.py`
that reads `raw_payload` and populates the new columns for existing
rows. Idempotent — `WHERE col IS NULL`.

### 0.2 Update merge_comps_into_global

In `api/propelio/archive.py:284`, add the 13 new columns to the INSERT
column list, EXCLUDED-in-UPDATE list, and extract values from `extra`
or `raw` JSON. Same pattern as existing extractions.

### 0.3 Add `'campaign_seed'` source value

No schema change needed — `first_seen_source` is plain TEXT. Just add
to the marathon scraper's writes.

**Estimated Phase 0 effort:** 1-1.5 hours Copilot.

---

## Phase 1 — Grid generation

### 1.1 DFW bounding box

```python
DFW_BBOX = {
    "lat_min": 32.55,   # ~south Dallas / Cedar Hill
    "lat_max": 33.10,   # ~Plano / Frisco
    "lng_min": -97.30,  # ~Fort Worth west
    "lng_max": -96.50,  # ~Rowlett / Mesquite east
}
```

Approximately 60mi × 50mi rectangle covering Dallas County + Tarrant
County + south Collin County + south Denton County.

### 1.2 Grid spacing

**8mi cell spacing** between grid points. Why:
- Each deep-pull captures comps up to 10mi from subject
- 8mi spacing gives 25% overlap between adjacent cells (10-8 = 2mi
  overlap radius)
- Full coverage with redundancy
- Yields ~60-80 grid points across DFW bbox

### 1.3 Snap to real addresses

For each grid intersection point:
1. Query `parcels` table: `SELECT account_num, address, lat, lng FROM
   parcels WHERE ST_DWithin(geom::geography,
   ST_MakePoint(grid_lng, grid_lat)::geography, 1609.344)
   ORDER BY ST_Distance LIMIT 1`
2. If no parcel within 1mi → skip (we're outside our parcel data
   coverage, probably water/highway)
3. Store the resulting address as a seed

Output: `scripts/generate_campaign_seeds.py` writes to
`campaign_seeds.json` (or directly to DB table — see Phase 2).

### 1.4 Expected output

~60-80 seed addresses, mostly residential SFH, spread evenly across DFW.

---

## Phase 2 — Campaign state schema

### 2.1 New table: `propelio_campaign_seeds`

```sql
CREATE TABLE IF NOT EXISTS propelio_campaign_seeds (
    seed_id          SERIAL PRIMARY KEY,
    campaign_name    TEXT NOT NULL,
    grid_lat         NUMERIC(10,7) NOT NULL,
    grid_lng         NUMERIC(10,7) NOT NULL,
    seed_address     TEXT NOT NULL,
    seed_lat         NUMERIC(10,7),
    seed_lng         NUMERIC(10,7),
    seed_account_num TEXT,
    seed_county      TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
        -- pending | in_progress | completed | failed | skipped
    job_id           TEXT,  -- FK-ish to propelio_deep_pull_jobs.job_id
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    comps_captured   INTEGER,
    net_new_comps    INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_name, seed_address)
);

CREATE INDEX idx_campaign_seeds_status
    ON propelio_campaign_seeds (campaign_name, status);
```

`campaign_name` lets us run multiple campaigns over time
(e.g., `'dfw_v1_2026_05'` for initial seed, `'dfw_refresh_2026_q3'` later).

### 2.2 Status state machine

```
pending → in_progress → completed
                      ↘ failed → (retry once) → completed | failed_final
                      ↘ skipped (manual op)
```

### 2.3 Why a table, not a file

- Survives machine reboots
- Multiple operators can see progress
- Joins to `propelio_deep_pull_jobs` via job_id
- Easy reporting: `SELECT status, COUNT(*) FROM
  propelio_campaign_seeds WHERE campaign_name = 'dfw_v1' GROUP BY 1`

---

## Phase 3 — Workday simulator

### 3.1 Daily schedule

| Time | Activity |
|---|---|
| 9:00 AM | Start work (`marathon start`) |
| 9:00 – 10:00 AM | Working block — deep-pulls sequential |
| 10:00 – 10:10 AM | Coffee break (8-12 min random) |
| 10:10 AM – 12:30 PM | Working block |
| 12:30 – 1:00 PM | Lunch (28-35 min random) |
| 1:00 – 3:00 PM | Working block |
| 3:00 – 3:10 PM | Coffee break |
| 3:10 – 8:30 PM | Working block |
| 8:30 PM | Soft stop — no new pulls accepted |
| 8:30 – 9:15 PM | Finish whatever's in flight |
| 9:15 PM | Hard stop — kill any pull still running |

### 3.2 Inter-pull pacing (within working blocks)

Already implemented via existing `_jittered_pass_sleep_seconds()`:
80/20 normal/distracted split. Reuse as-is.

Add **inter-SEED pacing** (between pulls, not within passes):
- 80% case: 30-90s (normal "looking at result" pause)
- 20% case: 90-180s (looking at result, taking notes,
  doing something else)

### 3.3 Daily rhythm randomization

- Coffee break time: 9:55-10:15 (not exact 10:00)
- Lunch time: 12:15-12:45 (not exact 12:30)
- Each break duration: random within the band
- Don't be predictable

### 3.4 Working block "task switch" extras

Every 3-5 pulls within a working block, add an extra 5-15s pause.
Mimics analyst pausing to "switch tasks" or read something.

---

## Phase 4 — Runner script

### 4.1 Entry point

`scripts/marathon_campaign.py`

```bash
# Generate seeds (one-time per campaign)
python -m scripts.marathon_campaign generate --campaign dfw_v1

# Run the campaign (resumes from last state)
python -m scripts.marathon_campaign run --campaign dfw_v1

# Status check
python -m scripts.marathon_campaign status --campaign dfw_v1

# Manually mark a seed as skipped
python -m scripts.marathon_campaign skip --seed-id 42 --reason "duplicate"
```

### 4.2 Run loop pseudocode

```python
def run_campaign(campaign_name: str):
    while True:
        if outside_working_hours():
            sleep_until_9am()
            continue

        if is_break_time():
            take_break()
            continue

        seed = claim_next_seed(campaign_name)
        if seed is None:
            # No more pending seeds — campaign done
            log_completion()
            return

        try:
            job_id = start_deep_pull(seed.address)
            update_seed_in_progress(seed.seed_id, job_id)
            wait_for_job_completion(job_id, timeout_min=15)
            mark_completed(seed, job_metrics)
        except Exception as exc:
            handle_failure(seed, exc)

        inter_seed_pause()  # 30-90s typical
```

### 4.3 Soft stop / hard stop

- At 8:30pm: set internal flag, no new `claim_next_seed`
- Loop drains: waits for current pull to finish (max 45 min)
- At 9:15pm: if pull still running, call existing
  `/deep-pull/stop/{job_id}` to terminate, mark seed as failed,
  exit cleanly

### 4.4 Ctrl-C handling

- Catch SIGINT
- Mark current seed back to 'pending' (so it retries next day)
- Stop any in-flight job
- Exit cleanly

---

## Phase 5 — Time-axis passes (KK's saturation insight)

For the campaign deep-pulls specifically, use an EXTENDED `PASSES`
config that includes shorter time windows:

```python
CAMPAIGN_PASSES = [
    # Recent activity
    {"months": 3,  "range_mi": 0.5,  "label": "recent_tight"},
    {"months": 12, "range_mi": 1.0,  "label": "mid_window"},

    # Standard radius sweep (unchanged from current PASSES)
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5,  "label": "blocks"},
    {"months": 24, "range_mi": 1.0,  "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0,  "label": "broader"},
    {"months": 24, "range_mi": 5.0,  "label": "wider"},
    # Skip 10mi rural fallback for campaign (covered by next grid cell)
]
```

Total: 7 passes vs current 6. Inter-pass pacing makes total runtime
~6-8 minutes per seed vs current 5-7.

The 3mo × 0.5mi pull is a "freshness probe" — if it returns 80+ comps,
we're saturated even on recent listings, signal to widen later.

For user-facing deep-pull, keep current 6 passes (no shorter time
windows). Two pass configs serve different purposes.

---

## Phase 6 — Observability

### 6.1 Stdout/file logging

`logs/marathon_{campaign_name}_{YYYY_MM_DD}.log` per day. Each event
logged with timestamp:
```
2026-05-12 09:00:00 [START] Campaign dfw_v1 day 1 start
2026-05-12 09:00:30 [SEED 1/72] 2451 Crest Ridge Dr → job dp_xyz
2026-05-12 09:06:42 [SEED 1/72] completed: 398 comps, 47 net-new
2026-05-12 09:06:42 [PAUSE] 67s inter-seed
2026-05-12 09:07:49 [SEED 2/72] 1234 Oak St → job dp_abc
...
2026-05-12 12:30:18 [LUNCH] 32 min break
2026-05-12 13:02:15 [SEED 28/72] ...
...
2026-05-12 20:30:00 [SOFT_STOP] No new pulls
2026-05-12 20:38:11 [SEED 47/72] completed (drain mode)
2026-05-12 20:38:11 [END] Day 1: 47 seeds, 12,847 comps captured,
                          3,201 net-new, 4 failures, 0 blocked
```

### 6.2 End-of-day summary

Written to file + (optional) emailed to KK:
```
Marathon Campaign Day 1 Summary — dfw_v1
=========================================
Total seeds attempted:  47
Completed:              43
Failed:                 4
Skipped:                0

Comps captured:         12,847
Net-new to cache:       3,201
Avg per seed:           273 comps

Runtime:                10h 38m active
Pulls/hour avg:         ~5
Propelio errors:        0 blocked, 4 transient (retried successfully)

Status:                 25 seeds remaining → 5-6 more days estimated
```

### 6.3 Real-time status query

```bash
python -m scripts.marathon_campaign status --campaign dfw_v1
```
Returns same info as end-of-day summary but for current state.

---

## Phase 7 — Failure handling

### 7.1 Transient errors (timeout, network blip)

- Retry once after 2 min pause
- If retry succeeds: mark completed, continue
- If retry fails: mark failed, move on

### 7.2 Propelio 401/403/429 (the BIG bad)

- Stop the campaign IMMEDIATELY
- Mark current seed as failed with error
- Alert operator (log+email if configured)
- Set campaign_name status table column or just rely on the operator
  noticing the daily summary

### 7.3 Schema/code errors

- Log full traceback
- Mark seed as failed_final (no retry)
- Continue to next seed

### 7.4 Parcel match failures

- Not fatal — comp goes into cache with parcel_account_num=NULL
- Backfill script can pick it up later

---

## Phase 8 — Pre-launch checks

Before kicking off the campaign:

- [ ] Phase 0 migrations applied to preview DB
- [ ] Phase 0 migrations applied to production DB
- [ ] `merge_comps_into_global` populates new columns
- [ ] Backfill script run on existing 3,898 comps
- [ ] Seed generation produces sensible addresses (manual eyeball
      check on 5-10 random ones)
- [ ] `campaign_seeds` table populated for `dfw_v1`
- [ ] Test run: 3 seeds in dev environment, verify data lands correctly
- [ ] Logs/observability working
- [ ] Soft-stop / hard-stop tested
- [ ] Ctrl-C tested
- [ ] Resumability tested (kill mid-pull, restart, verifies state)

---

## Open questions for Copilot review

1. **Grid spacing — is 8mi too sparse or too dense?** Tradeoff:
   sparse = less coverage redundancy, dense = more pulls = longer
   campaign. Current proposal is 8mi for 25% overlap.

2. **Parcel-snap for grid points — what if multiple campaigns of
   the same area cause double-coverage?** Mitigation: each campaign
   has a name; we can check "if any seed within 0.5mi was completed
   in last 30 days, skip this one."

3. **Workday hours — does 9am-9pm look like a normal analyst, or
   should we vary?** Some analysts work 8-6. Should we randomize the
   start time each day (8:50am-9:15am)?

4. **Lunch break — should it be at lunch?** A salaried desk worker
   eats around noon. Off-peak (1:30pm late lunch) might look more
   varied. Vary across days?

5. **Should we skip the 5mi pass entirely for campaign?** The 5mi
   pass is wide. If adjacent seeds in the grid are 8mi apart, the
   5mi pass overlaps neighbors significantly. Drop it for campaign
   speed?

6. **What's the "saturation signal" we'd act on?** If 3mo×0.5mi
   returns 100, we know we're missing recent activity. Do we:
   - Trigger a follow-up tighter pull within the campaign run?
   - Or just log it and the next campaign refresh catches it?

7. **Concurrent pulls — should the campaign respect any future
   global single-pull lock (per item 2.56 in testing notes)?** Right
   now we allow concurrent pulls. Marathon would generally run alone,
   but if Mike clicks Get Comps during a marathon day, that's
   simultaneous activity from one account.

8. **Email alerts — operator preference?** If Propelio errors, who
   gets notified? SMS, email, or just stderr?

9. **Seed retry policy — when does a failed seed graduate to
   failed_final?** Current proposal: retry once same-day, then move
   on. If still failed at end of campaign, manual review.

10. **Photo download — bundle into the campaign or strict
    separation?** Audit says defer photos. But if we're running 47
    pulls/day for a week, that's the perfect cover for slow-rolling
    photo backfills. Worth considering vs strict separation.

---

## Estimated effort

| Phase | Effort | Owner |
|---|---|---|
| 0. Pre-work migration + merge update | 1-1.5 hr | Copilot |
| 1. Grid generation | 1 hr | Copilot |
| 2. Campaign state schema + ORM | 1 hr | Copilot |
| 3. Workday simulator | 1.5 hr | Copilot |
| 4. Runner script | 2 hr | Copilot |
| 5. Time-axis passes config | 30 min | Copilot |
| 6. Observability/logging | 1 hr | Copilot |
| 7. Failure handling | 1 hr | Copilot |
| 8. Test run | 1 hr | KK + Copilot |
| **Total** | **10-11 hours** | |

Realistic: spread over 2-3 working days with review/iteration gates
at end of each phase.

---

## Out of scope for v1

- Photo downloads (Phase 2.6+)
- Multi-county expansion beyond DFW (parametrize bbox, add config)
- Auto-rescheduling based on Propelio rate-limit signals
- ML-based saturation detection (Phase 2.3 in testing notes)
- A web UI for monitoring (CLI status command is sufficient for v1)

---

## Shipping checklist

- [ ] Phase 0 ships to dev branch
- [ ] Phase 0 backfill run on shared dev DB
- [ ] Phases 1-7 land on a feature branch
- [ ] 3-seed test run in dev
- [ ] Code review pass
- [ ] Merge to develop
- [ ] First production run: 1 day, monitored closely
- [ ] If clean, run continuous days until DFW grid is exhausted
- [ ] Schedule monthly refresh campaign (`dfw_refresh_2026_06`)
