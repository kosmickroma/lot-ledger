# Production Scraper

Long-running, resumable, cron-friendly Propelio comp sweep.

**Spec:** `docs/propelio/PRODUCTION_SCRAPER_SPEC.md` (v1.2 locked).
**Sibling:** `scripts/strip_runner.py` (manual-list runner; different use case).

---

## What it does

Walks a curated master list of addresses and, for each one, pulls Propelio
comps across a fixed distance matrix at a configurable time window. Saves
per-address progress to a local state file so it can be killed, crashed,
or rebooted at any time and resume cleanly on the next launch.

Two phases of use:

| Phase | Profile | Time window | When |
|---|---|---|---|
| Seed | `seed_5y` | 60 months back | One-time, deep-history backfill per area |
| Production | `monthly_1m` | 1 month back | Cron-recurring, catches newly-listed comps |

Both share the same distance matrix: `[0.25, 0.5, 1.0, 2.0, 5.0]` miles
→ 5 pulls per address.

---

## Quick start

```bash
# 1. Add addresses to the master list
#    (one per line, '#' comments OK, city required)
$EDITOR scripts/production_scraper/master_list.txt

# 2. Dry-run to validate everything BEFORE burning Propelio calls
.venv/bin/python -u scripts/production_scraper/run.py \
    --profile seed_5y \
    --dry-run

# 3. Real run (seed pass — 5y back, all addresses)
.venv/bin/python -u scripts/production_scraper/run.py \
    --profile seed_5y

# 4. After seed is done, the monthly cron uses --profile monthly_1m
.venv/bin/python -u scripts/production_scraper/run.py \
    --profile monthly_1m
```

**Important:** Must be invoked from the repo root (or with `--list` /
`--state-dir` / `--log-dir` overrides). The script will hard-fail with
exit 3 if it can't find `api/propelio/` somewhere above its own location.

---

## CLI reference

```
python -u scripts/production_scraper/run.py [flags]

Required:
  --profile NAME           Filter profile from profiles.py
                           (currently: seed_5y, monthly_1m)

Optional:
  --list PATH              Master address list
                           (default: scripts/production_scraper/master_list.txt)
  --state-dir PATH         State directory
                           (default: scripts/production_scraper/state/)
  --log-dir PATH           Log directory
                           (default: scripts/production_scraper/logs/)
  --restart                Abandon any in-progress pass, start fresh
  --dry-run                Validate + print queue, exit 0, no Propelio calls
  --mock                   Skip real Propelio + DB; counts as 5 ok pulls per addr
                           (for smoke tests / self-checks only)
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Pass completed cleanly (or dry-run completed) |
| 1 | Clean exit short of completion (soft-stop after some progress) |
| 2 | Auth/rate block — wait 30+ min and re-launch |
| 3 | Operator error (bad profile, missing list, resume mismatch, etc.) |
| 4 | Another run already in progress (lock held by live PID) |
| 130 | SIGINT (Ctrl-C) |
| 143 | SIGTERM (e.g., `systemctl stop`) |

---

## Resume behavior

State is per-address. After every address completes, the entire state is
atomically rewritten (`state.json.tmp` → `os.replace` → `state.json`). On
launch:

* **Fresh run** (no state file, or prior pass `completed_at` is set) →
  snapshot the current master list, mark every address `pending`, start
  processing.
* **Resume** (state file exists, `completed_at` is null, profile + list
  hash match) → skip done addresses, treat any `in_progress` as pending,
  pick up.
* **Profile or list mismatch** → exit 3 with a message asking you to
  `--restart`.
* **Lock held by live PID** → exit 4 (another run is in progress).

**A mid-address crash redoes the whole address** (5 pulls, ~3 min). All
writes are idempotent on `comp_address_key` so re-pulling is safe — it
just refreshes the row's `last_seen_at` and any updated fields.

### `--restart`

Use when:
- You changed the master list (added/reordered addresses) and want a
  fresh pass against the new list.
- You're switching profiles (`seed_5y` ↔ `monthly_1m`).
- A previous pass is wedged and you've decided to abandon it.

`--restart` archives the in-progress pass to history with
`aborted: true` and starts a brand-new pass.

---

## Logs

* Each run writes to
  `scripts/production_scraper/logs/<profile>-<UTC-timestamp>.log`.
* `logs/latest.log` is a symlink to the most recent log file —
  `tail -f scripts/production_scraper/logs/latest.log` always tracks
  the live run.
* No rotation; prune manually if needed (logs are small).

---

## State file

Path: `scripts/production_scraper/state/state.json`

Shape (per `docs/propelio/PRODUCTION_SCRAPER_SPEC.md` §6):

```json
{
  "schema_version": 1,
  "current_pass": {
    "pass_id": "20260526T150000Z-seed_5y",
    "started_at": "...",
    "completed_at": null,
    "profile": "seed_5y",
    "profile_snapshot": { "months": 60, "distances_mi": [0.25, 0.5, 1.0, 2.0, 5.0] },
    "list_path": "...",
    "list_sha256": "...",
    "list_snapshot": [ "1234 MAIN ST, DALLAS, TX", ... ],
    "addresses": {
      "1234 MAIN ST, DALLAS, TX": {
        "status": "done",
        "completed_at": "...",
        "filters_ok": 5,
        "filters_errored": 0,
        "comps_returned": 432,
        "comps_new": 87
      }
    }
  },
  "history": [
    { "pass_id": "...", "addresses_done": 121, "comps_new_total": 9842, ... }
  ]
}
```

* `state/` is gitignored — never committed.
* History is unbounded (a startup warning fires if it exceeds 1000 entries,
  in case of cron misconfiguration).

---

## Cron setup (Phase 2)

Sample crontab line (run nightly at 02:00 local):

```cron
0 2 * * * cd /home/kk/projects/clients/lot-ledger && .venv/bin/python -u scripts/production_scraper/run.py --profile monthly_1m >> /var/log/production_scraper.cron 2>&1
```

Cron-safety notes:

* The script's repo-root check walks upward looking for `api/propelio/`
  so it works even if the `cd` is wrong — but log a CRITICAL on misuse.
* Use the absolute path to the venv Python; cron's `$PATH` is empty.
* If a prior cron run is still going (e.g., took longer than 24h), the
  next cron exits code 4 (lock held). No duplicate work, no races.
* If a prior cron run crashed mid-pass, the next cron resumes naturally.
* If the prior pass completed cleanly, the next cron starts a fresh pass
  (refreshing every address against the current 1-month window).

---

## Troubleshooting

### Exit 2 (auth block)

Propelio rate-limited or auth-blocked. The session-expiry path
(401/403) is auto-retried once with a forced re-login; exit 2 means
either the retry failed OR a 429 was returned. Wait 30+ minutes, then
re-launch with the same flags. State preserves; you pick up where you
left off.

### Exit 3 (operator error)

The log line tells you exactly what's wrong: unknown profile, missing
master list, profile/list mismatch on resume, repo-root not found, etc.
Read it, fix it, retry.

### Exit 4 (lock busy)

Another run is in progress. Check `scripts/production_scraper/state/run.lock`
— it contains the holder's PID. If that PID is dead but the lock wasn't
cleaned (e.g., SIGKILL), this script's next launch will take it over
automatically; you only see exit 4 when the PID is genuinely alive.

### Stale `*.tmp` files in `state/`

Atomic-rename leftovers from a crashed write. The next launch sweeps
them on startup (logged at INFO). You can also remove them manually
without consequence — the canonical state lives in `state.json`.

### "address marked failed — re-run it?"

The next pass (monthly cron) automatically re-attempts every address,
including ones previously marked `failed`. For a manual one-off retry
of a single failed address: `--restart` and re-run.

---

## Self-tests

```bash
.venv/bin/python scripts/production_scraper/smoke.py
```

Should print `44/44 passed` (or higher as the test suite grows).
The smoke tests cover: address parser, profile resolver, state file
(round-trip + atomic write), list hash, run lock (acquire / takeover /
release), auth-error classifier, login(force=True), call_with_auth_retry,
merge_comps_into_global_with_retry, shared comp_address_key invariant,
repo-root sanity check, main() dry-run integration.

Zero real Propelio or DB calls.

---

## What this scraper does NOT do

(Spec §2 "Non-goals". If you find yourself wanting any of these,
either rethink the goal or open a separate spec.)

- Multi-process / parallel scraping (one process at a time)
- Per-pull resume (mid-address crash redoes the whole address)
- Auto-update of the master list from any external source
- Auto-recovery of failed-address slots within a single pass
- Email/Slack notifications
- Mid-run profile switching
- Web UI or HTTP endpoint
- Pass-history analytics beyond the slim summaries kept in state.json
