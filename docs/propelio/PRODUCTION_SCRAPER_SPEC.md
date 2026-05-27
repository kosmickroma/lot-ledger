# Production Scraper — Design v1.2

**Status:** Copilot round 2 critique folded in (2026-05-26). **LOCKED for implementation.** Copilot R2 verdict: "After those edits, I do not see a need for another full critique round before implementation."
**Author:** KK + Claude (brainstorm 2026-05-26).
**Sibling to:** `scripts/strip_runner.py` (manual-list runner on `feat/strip-runner`).
**Builds on:** `api/propelio/` infrastructure (client, archive, parcel-match).

---

## Changes v1.1 → v1.2

1. **§7.2 (R2 IMPORTANT #1):** DB retry path no longer assumes a `force_close` parameter on `release_session_conn` that doesn't exist. Implementation pattern: add a wrapper `merge_comps_into_global_with_retry(comps, source)` inside `api/propelio/archive.py` that owns the conn lifecycle for the retry (close + reacquire) without forcing changes on strip_runner / deep_pull callers. The lower-level `merge_comps_into_global` stays untouched.
2. **§7.5 (R2 IMPORTANT #2):** `PropelioClient.login()` is documented to early-return when `_logged_in` is True. v1.2 spec REQUIRES adding a `force=False` parameter; the forced path clears `_logged_in` (and any cached session cookies/tokens) before re-posting credentials. Small client.py change, called out as part of the build scope.
3. **§7.6 (R2 IMPORTANT #3):** Propelio HTTP timeout table corrected — `search_cma` hardcodes `max(self.timeout, 90)` in `api/propelio/scraper.py`. Spec accepts 90s for `search_cma` rather than fighting the client. Worst-case SIGTERM response revised to ~95s (was ~60s).
4. **§7.6 (R2 IMPORTANT #4):** `statement_timeout` injection moved from a non-existent pool-acquire hook to `SET LOCAL statement_timeout = '60s'` inside the merge transaction. Mirrors the existing pattern in `api/main.py`. Applies to all callers of the merge path (acceptable; the timeout is generous).
5. **§7.1 (R2 IMPORTANT #6):** Repo-root sanity check changed from a fixed `__file__.parents[2]` assumption to a walking-upward search for `api/propelio/__init__.py`. Robust to symlinks and future layout drift.
6. **§7.5 (R2 IMPORTANT #7):** Explicit sentence added — a successful re-login + retry **resets `consecutive_errors` to 0** and does NOT contribute toward the burst guard. Closes the false-three-error-address-failure path.
7. **§7.2 (R2 IMPORTANT — DB-retry classification refinement, from R2 §C.4):** The DB retry path now distinguishes two failure classes. **Connection-liveness failures** (psycopg2.OperationalError, socket reset, "connection closed") → close + retry once. **Deterministic failures** (statement_timeout exceedance, syntax/constraint error) → fail-fast, count as errored filter, do NOT retry (retrying would just burn a second 60s timeout for the same root cause).
8. **§7.2 (R2 Nice-to-have #2):** Parcel-match → unmatched-merge double-fallback clarified. If `parcel_match` fails AND the unmatched-merge fallback also fails, that is **one DB failure event** — goes through the same one-retry DB path, counts once toward the burst guard. No silent swallow, no double-count.
9. **§7.1 (R2 Nice-to-have #3):** On startup, scan `state/` for stale `*.tmp` files and unlink them (leftovers from a crashed write-rename sequence). Keeps the state directory tidy.
10. **§6.4 (R2 Nice-to-have #1):** History stays unbounded, but added soft warning at startup if `history` length exceeds 1000 entries (catches a degenerate cron-misconfiguration scenario).
11. **§13 — all 6 open items resolved.** Round-2 verdicts folded in: pacing stays 15-45s (Copilot agrees, defer optimization to first pilot run data), master list stays committed (Copilot agrees), `--profile` stays required (Copilot agrees), `login(force=True)` added (per §7.5), `statement_timeout` in merge transaction (per §7.6), cron over systemd-timer for client ship (Copilot recommends — simpler hand-off, no service-manager complexity unless Mike specifically wants it). §13 now empty / "no open items."

---

## Changes v1.0 → v1.1

1. **§7.2 / §11 (Copilot HIGH — CRITICAL):** `merge_comps_into_global` exception is no longer silently dropped. New policy: close the DB session, retry once on a fresh connection; if retry fails, count as an errored filter (counts toward burst guard). This prevents the "Propelio call succeeded, Cloud SQL hiccupped, pull lost forever" failure mode Copilot flagged.
2. **§7.5 / §11 (Copilot MEDIUM-HIGH):** Auth 401/403 now triggers **one re-login + one retry** in v1, NOT immediate exit. Only 429 stays immediate-exit-2. Required for unattended 5-hour seed runs where Propelio session TTL is likely exceeded.
3. **§7.6 (Copilot MEDIUM):** Explicit bounded timeouts on every external call (Propelio HTTP 60s, psycopg2 connection 10s). SIGTERM now surfaces within 60s worst-case. Second SIGINT bypasses the soft-stop. Documented hard-kill outcome: state preserved at last save, in_progress address redone on resume.
4. **§6.4 (Copilot Open Item #11):** History retention bumped from "last 5" to **unbounded** (slim summary per pass; file stays small even after 1000+ passes; gives KK + Mike an audit trail).
5. **§8 / §13 (Copilot Open Item #12):** Dropped `--limit` flag. YAGNI per Copilot; trivial to add back if cron-chunking emerges as a real need.
6. **§3 (Copilot Open Item #13):** Explicit note that `comp_address_key` derivation in `api.propelio.archive` must stay identical for both source tags (production_scraper, strip_runner) — enforced by reusing `merge_comps_into_global` rather than reimplementing.
7. **§7.1 (Copilot Open Item #14):** Startup hygiene check — verify expected repo root via `__file__.parents[N]` and explicit assertion. Don't trust cron's cwd.
8. **§6.1 / §7.1 (Copilot Open Item #4):** `list_sha256` clarified as the hash of the **normalized address queue** (uppercase-stripped, deduped, in source order), not the file bytes. Reordering = different hash = resume rejected per §7.1.
9. **§13 (Copilot Open Item #2):** Lock-file mechanism explicitly documented as **assuming local POSIX filesystem**. No promises across NFS / SMB / cross-mount. Spec assumes the scraper folder lives on a local filesystem (it does for KK + Mike's deployment).
10. **§13 rewrite:** Open items §13 reduced from 15 to 6 (the rest are resolved by v1.1 changes). Remaining 6 are still genuinely open for round-2 critique or KK product calls.

---

## 1. Purpose

A long-running, **resumable**, **cron-friendly** Propelio comp scraper that sweeps a curated master list of addresses on a small fixed distance matrix per address.

**Two deployment phases:**

| Phase | Profile | Time window | Cadence | Use |
|---|---|---|---|---|
| Phase 1 (one-time seed) | `seed_5y` | 60 months back | Manual, single full pass | Deep-history backfill of the 4-county master list. Run once per area. |
| Phase 2 (production cron) | `monthly_1m` | 1 month back | Cron, recurring | Catches newly-listed comps. Refreshes each address on the cron cadence. |

**Both phases share the same distance matrix:** `[0.25, 0.5, 1.0, 2.0, 5.0]` miles → **5 pulls per address**.

**Success criteria:**

1. KK can launch a Phase 1 seed run on his master list and walk away. If anything crashes (computer reboot, power loss, terminal close, network drop, Cloud SQL timeout), the next launch resumes from the next un-done address.
2. A `--restart` flag forces a fresh full pass.
3. Phase 2 can be wired to cron later with zero code changes — only invocation differs.
4. The tool is **shippable to the client** (Mike): clean folder, README, no operator-secret expectations beyond the existing `.env`.

## 2. Non-goals

The following are explicitly **out of scope** for v1:

- Multi-process / parallel scraping (one process at a time, enforced by lock file)
- Per-pull resume (mid-address crash redoes the whole address — see §7.4)
- Auto-update of the master list from any external source (KK curates manually)
- Auto-recovery of failed-address slots within a single pass (KK re-runs misses by hand or via next pass)
- Email/Slack notifications (operator reads logs)
- Mid-run profile switching (one profile per run, locked at startup)
- Web UI or HTTP endpoint
- Pass-history analytics beyond a short rolling tail

## 3. Architecture

Single Python script under a self-contained folder. Imports two shared modules from `api/propelio/` (already used by strip_runner and the deep-pull experiment).

```
scripts/production_scraper/
├── README.md           — operator guide
├── run.py              — main script (~300 LOC target)
├── profiles.py         — filter profile dict (no logic)
├── master_list.txt     — KK-curated addresses (COMMITTED — ship artifact)
├── smoke.py            — inline self-tests (mock-driven, no Propelio calls)
├── state/              — gitignored
│   ├── state.json      — current pass + per-address checkpoint
│   └── run.lock        — PID lock file (cron-safety)
└── logs/               — gitignored
    ├── <profile>-<YYYY-MM-DD-HHMMSS>.log
    └── latest.log → most-recent (symlink)
```

`.gitignore` additions:

```
scripts/production_scraper/state/
scripts/production_scraper/logs/
```

**Imports** (all read-only, no modifications to `api/propelio/`):

| Imported symbol | Source | Purpose |
|---|---|---|
| `PropelioClient` | `api.propelio.scraper` | Session login, find_lead_id, add_cma, search_cma |
| `merge_comps_into_global` | `api.propelio.archive` | Idempotent comp persistence; called with `source="production_scraper"` |
| `match_comps_to_parcels` | `api.propelio.parcel_match` | Optional parcel match; warn-and-fallback on exception (mirrors deep_pull pattern) |
| `_parse_property` | `api.propelio.scraper` | Per-comp parse from Propelio raw payload (mirrors strip_runner reuse) |

**No new third-party dependencies.** Uses stdlib only beyond what's already in the venv (`psycopg2-binary`, `requests`, `python-dotenv`, etc.).

### 3.1 Shared-cache invariant with strip_runner

Both `strip_runner` (source=`"strip_runner"`) and this scraper (source=`"production_scraper"`) write to the same `propelio_comps` table via the same `merge_comps_into_global` function. Concurrent writes are safe because:

1. `merge_comps_into_global` uses `ON CONFLICT (comp_address_key) DO UPDATE` (see `api/propelio/archive.py:462`) — last-writer-wins on shared rows, no duplicates.
2. The `comp_address_key` derivation lives in `api/propelio/archive.py` and is shared by both callers. **Do not reimplement the key derivation in this scraper.** Any divergence in the key would create duplicate rows under the two source tags.

If the key derivation ever needs to change, both source paths must change together. The smoke test `test_comp_address_key_shared` (§12) is added to enforce this.

## 4. Address list format

Plain UTF-8 text. One address per line. Lines starting with `#` are comments. Blank lines ignored. UTF-8 BOM tolerated (read via `utf-8-sig`).

Each line must be a fully-qualified street address parseable by Propelio's `find_lead_id`. **City is required** — no implicit-city defaults (avoids the silent-fail mode where missing cities crash `find_lead_id` deep in the run).

Example (`master_list.txt`):

```
# Production scraper master list — 4 counties (Dallas, Collin, Denton, Tarrant)
# Edits here take effect on the NEXT run start. Mid-run edits do not affect the current pass.

1234 Main St, Dallas, TX
5678 Oak Ave, Plano, TX

# === Dallas / South ===
700 WOLF SPRINGS RD, DALLAS, TX
2913 S HOUSTON SCHOOL RD, DALLAS, TX
```

**Parser rules:**

1. Strip the BOM with `utf-8-sig`
2. Strip whitespace from each line
3. Skip blank lines
4. Skip lines starting with `#` (after strip)
5. Collapse internal whitespace to single spaces (defensive: Obsidian-paste can leave tabs)
6. Reject lines containing only `,` characters or no commas at all (no city → reject with line number in error)
7. Deduplicate on uppercase-normalized form; warn on duplicate but keep first occurrence

Empty list (no non-comment lines) → exit code 3 with operator-friendly error.

## 5. Filter profiles

`profiles.py`:

```python
# scripts/production_scraper/profiles.py
"""Filter profile definitions. Imported by run.py. No logic — pure data."""

DISTANCES_MI = [0.25, 0.5, 1.0, 2.0, 5.0]  # shared across profiles by design

PROFILES = {
    "seed_5y": {
        "months": 60,
        "distances_mi": DISTANCES_MI,
        "description": "One-time deep-history seed pass (60-month window).",
    },
    "monthly_1m": {
        "months": 1,
        "distances_mi": DISTANCES_MI,
        "description": "Ongoing production sweep (1-month window). Cron target.",
    },
}
```

**Selection:** `--profile <name>` CLI flag (required, no default — explicit choice prevents accidentally running 60mo when cron should run 1mo).

**Per-address pull count:** `len(distances_mi)` = **5**. With ~30s pacing → ~2.5 min per address (plus ~5s `add_cma` setup and ~5s inter-address gap).

**Validation at startup:** unknown profile → exit code 3 with the list of valid profile names.

## 6. State file model

`state/state.json` — single JSON object, atomically rewritten after each address completes.

### 6.1 Schema

```json
{
  "schema_version": 1,
  "current_pass": {
    "pass_id": "20260526T150000Z-seed_5y",
    "started_at": "2026-05-26T15:00:00Z",
    "completed_at": null,
    "profile": "seed_5y",
    "profile_snapshot": {
      "months": 60,
      "distances_mi": [0.25, 0.5, 1.0, 2.0, 5.0]
    },
    "list_path": "scripts/production_scraper/master_list.txt",
    "list_sha256": "abc123...",
    "list_snapshot": [
      "1234 Main St, Dallas, TX",
      "5678 Oak Ave, Plano, TX"
    ],
    "// list_sha256 covers": "SHA-256 of the NORMALIZED address queue joined by '\\n' (uppercase-stripped, deduped, in source order — exactly the work queue). Reorder or edit changes the hash, blocking resume per §7.1.",
    "addresses": {
      "1234 Main St, Dallas, TX": {
        "status": "done",
        "started_at": "2026-05-26T15:00:05Z",
        "completed_at": "2026-05-26T15:02:34Z",
        "filters_ok": 5,
        "filters_errored": 0,
        "comps_returned": 432,
        "comps_new": 87,
        "skip_reason": null,
        "last_error": null
      },
      "5678 Oak Ave, Plano, TX": {
        "status": "pending"
      }
    }
  },
  "history": [
    {
      "pass_id": "20260524T020000Z-seed_5y",
      "completed_at": "2026-05-24T08:15:00Z",
      "profile": "seed_5y",
      "addresses_total": 124,
      "addresses_done": 121,
      "addresses_failed": 3,
      "comps_new_total": 9842
    }
  ]
}
```

### 6.2 Atomic write

Every state write follows: `write to state.json.tmp` → `os.replace(state.json.tmp, state.json)`. POSIX `rename` is atomic; a crash mid-write leaves either the old or the new state — never a corrupt half-file. State is fsync'd before rename.

### 6.3 Address status values

| status | Meaning |
|---|---|
| `pending` | Not yet started in this pass. |
| `in_progress` | Started but not finished. **Only present transiently** between `started_at` write and `completed_at` write. On launch, any `in_progress` address is treated as `pending` (redoing it from scratch is safe). |
| `done` | All filters fired without error. |
| `partial` | Some filters errored but ≥1 succeeded. Counts as "done" for resume purposes (won't be re-attempted in this pass). |
| `failed` | Address-level failure (lead lookup, cma setup, or 3-consecutive-filter-error burst). Counts as "done" for resume purposes. |

### 6.4 History

After a pass completes (every address has terminal status), the `current_pass` is rolled into `history` (slim summary — no per-address detail) and `current_pass` is reset to null. **History is unbounded** — the summary is ~150 bytes per pass, so even 1000 passes is ~150 KB. Mike + KK get a full audit trail for Phase 2 cron operation. KK can manually prune via `jq` if it ever grows uncomfortably.

**v1.2 (R2 Nice-to-have #1):** Startup emits a WARNING log line if `history` length exceeds 1000 entries. Catches degenerate cron-misconfiguration (e.g., cron fires every second by mistake) without blocking the run. Documented in §7.1 step 10.

## 7. Per-run flow

### 7.1 Startup sequence (v1.2)

1. Parse CLI flags.
2. **Repo-root sanity check** (v1.2 — robust to symlinks per R2 IMPORTANT #6): walk upward from `Path(__file__).resolve()` until a directory containing `api/propelio/__init__.py` is found. If the search reaches `/` without finding one → exit code 3 with "scraper invoked outside of a lot-ledger checkout; could not find api/propelio/ in any parent directory". Record the discovered root as `repo_root`.
3. Insert `repo_root` at `sys.path[0]` so `from api.propelio.*` resolves regardless of cwd. (Mirrors strip_runner pattern at scripts/strip_runner.py:28.)
4. Load `.env` from `repo_root / ".env"` (NOT cwd-relative) — `python-dotenv`.
5. Validate profile name; resolve to filter matrix.
6. **Tidy state directory** (v1.2 — R2 Nice-to-have #3): scan `state/` for files matching `*.tmp` and unlink them. These are leftovers from a crashed write-rename sequence (atomic-rename guarantees the final state.json is intact, but the tmp file may be orphaned). Log at INFO if any are removed.
7. Acquire `state/run.lock` via `fcntl.flock(LOCK_EX | LOCK_NB)` on a held file descriptor (FD held open for the entire run; kernel auto-releases on process exit). Write PID + start ISO timestamp into the lock file. **If lock fails:** read the lock file, check if PID is alive (`os.kill(pid, 0)`). If alive → exit code 4 with friendly "another run is in progress (PID=N, started=...)". If dead → log warning ("taking over from crashed PID=N"), overwrite, proceed.
8. Read master list, parse, validate (per §4). Compute the **normalized address queue** (uppercase-stripped, deduped, in source order) — this is the unit that gets hashed AND iterated.
9. Load `state/state.json` if present.
10. **History size guard** (v1.2 — R2 Nice-to-have #1): if `history` length exceeds 1000 entries, log WARNING: "history has N entries (>1000); consider pruning state.json or investigating cron-fire frequency." Does not block startup.
11. Determine pass status:
    - **No state file** → start a fresh pass.
    - **`current_pass` exists with `completed_at` set** → completed; archive to `history`, start a fresh pass.
    - **`current_pass` exists with `completed_at == null` and profile matches and list_sha256 matches** → **resume**: pending + in_progress addresses are the work queue.
    - **`current_pass` exists with `completed_at == null` and profile or list_sha256 differs** → exit code 3 with operator message: "current pass uses profile=X / list_sha256=Y, but you launched with profile=Z / list_sha256=W. Use --restart to abandon the in-progress pass." (Prevents silently mixing seeds and monthlies, OR silently resuming against a reordered/edited list.)
    - **`--restart` flag** → archive any in-progress pass to history with `aborted=true` annotation, start a fresh pass.
12. If fresh pass: create `current_pass` with `list_snapshot = normalized_queue`, all addresses status=`pending`, compute `list_sha256 = SHA-256(normalized_queue joined by '\n')`.
13. Open the log file (see §9), print startup banner, summary of work queue (N pending, M done from previous attempt).

### 7.2 Per-address loop

For each pending address in **original list order** (preserves KK's intentional curation order):

1. Write address status = `in_progress`, `started_at = now`. Save state atomically.
2. **Step A — `find_lead_id`** (mirrors strip_runner §6 step 1):
    - Auth-class exception → exit immediately (§7.5).
    - Other exception → status=`failed`, skip_reason=`lead lookup failed`, last_error=short. Save state. Continue to next address.
3. **Step B — `add_cma`** with `(months, distance=distances_mi[0])` to establish the CMA object. Comps from this call are discarded (per Option A from strip_runner spec §5). Log `setup: add_cma ok cma_id=N (Ns)`.
    - Auth-class → exit immediately.
    - Other → status=`failed`, skip_reason=`cma setup failed`. Save state. Continue.
4. **Step C — 5 × `search_cma`** loop over `distances_mi`:
    - Sleep between pulls per §7.3.
    - **Propelio call failure**:
      - Auth-class 429 (rate limit) → exit immediately per §7.5.
      - Auth-class 401/403 → invoke re-login + retry path per §7.5. If that ultimately fails → exit 2.
      - Other non-auth exception → log, increment `filters_errored`, track `consecutive_errors`. If `consecutive_errors >= 3` → status=`failed`, skip_reason=`3 consecutive filter errors`, save state, continue to next address.
    - Success → parse via `_parse_property`, parcel-match.
    - **Parcel-match + DB write** (v1.2 — R2 Important + Nice-to-have #2): the scraper calls a wrapper `merge_comps_into_global_with_retry(comps, source="production_scraper")` which lives in `api/propelio/archive.py` and owns the connection lifecycle for the retry path. Wrapper logic:
      1. Try `parcel_match` → on exception, log warning, fall back to unmatched comps (mirrors deep_pull).
      2. Call `merge_comps_into_global(matched_or_unmatched_comps, source)`.
      3. On success → return `merge_result`. The caller increments `filters_ok`, resets `consecutive_errors`, accumulates `comps_returned`, `comps_new`. Normal pass line.
      4. On exception → **classify** (v1.2 — R2 IMPORTANT #7):
         - **Connection-liveness failure** (`psycopg2.OperationalError`, "connection closed", "server closed the connection unexpectedly", socket reset) → close the connection (the wrapper has the handle), reacquire a fresh one, retry the merge **once**.
            - Retry success → normal success path (filters_ok increments).
            - Retry fail → errored filter (see (5) below).
         - **Deterministic failure** (statement_timeout exceedance, syntax error, constraint violation, OperationalError that does NOT match the connection-liveness fingerprint) → do NOT retry. Counts as errored filter immediately. Retrying a deterministic failure just burns another 60s for the same root cause.
      5. **Errored filter** path (DB retry failed OR deterministic DB failure OR parcel_match-fallback double-failure — all unified): increment `filters_errored`, increment `consecutive_errors`, log with full error class. If `consecutive_errors >= 3` → address `failed` with skip_reason=`3 consecutive DB+filter errors`. This unifies DB and Propelio failures into the same burst guard (no silent data loss).
5. After loop: status = `done` if `filters_errored == 0` else `partial`. Set `completed_at`. Save state. Log per-address summary line.

**Implementation note:** The wrapper `merge_comps_into_global_with_retry` is new code added to `api/propelio/archive.py`. It does NOT modify the existing `merge_comps_into_global` (which strip_runner and deep_pull continue to call directly). The wrapper internally calls the existing merge function, catches its exceptions, and applies the close+retry policy. This keeps blast radius scoped to the new scraper without forcing changes on the working tools.

### 7.3 Pacing

Inherit strip_runner's two-band pattern:

- **Setup → first pull:** 3-5s uniform random.
- **Inter-pull (within an address):** 15-45s uniform random. (Strip_runner uses the same; spec §7 "non-uniform two-band shape preserved.")
- **Inter-address:** 5-15s uniform random.

5 pulls × ~30s + setup + inter-address ≈ **3 min per address**.

For a 100-address list: **~5 hours per pass**.

### 7.4 Resume granularity

State is per-address, **not** per-pull. If a crash occurs mid-address, the resumed run **redoes the whole address** (all 5 pulls). At 3 min per address, the redo cost is small and the simpler state model is worth it.

### 7.5 Auth handling (v1.1: split 401/403 from 429)

Auth-class classification mirrors strip_runner's `_is_auth_class` helper (HTTP 401, 403, 429, plus message-fragment fallback for "rate limit", "throttle", "unauthorized", "forbidden").

**429 (rate limit) — immediate exit 2:**

1. Log CRITICAL with full address + filter + error + the substring of the response body that triggered classification.
2. Flush log.
3. Save state (current address stays `in_progress` — will be redone on resume).
4. Release lock file.
5. **Exit code 2.** Operator (or cron) interprets as "wait 30+ min, then re-launch."

**401 / 403 (session expiry — likely on long seed_5y runs) — re-login + retry (v1.2):**

1. Log WARNING: "auth 401/403 detected at {address} pass {N} — attempting one re-login + retry."
2. Call `client.login(force=True)`. **Implementation requirement** (R2 IMPORTANT #2): `PropelioClient.login()` currently early-returns when `_logged_in` is True (`api/propelio/scraper.py`). v1.2 spec REQUIRES adding a `force=False` parameter. The forced path clears `_logged_in`, clears any cached session cookies/tokens on the underlying `requests.Session`, then re-posts credentials. This is a small client.py change scoped to the build phase.
3. **Re-login failure** → log CRITICAL, follow the immediate-exit-2 path above.
4. **Re-login success** → retry the failed call (find_lead_id / add_cma / search_cma) **exactly once**.
    - Retry succeeds → continue normal flow. **(R2 IMPORTANT #7): reset `consecutive_errors` to 0. The re-login + retry success does NOT count toward the burst guard. The address is in a healthy state.**
    - Retry returns auth-class again → treat as re-login failure: exit 2.
    - Retry returns non-auth error → fall through to the normal non-auth error path for that step (per §7.2). The non-auth error counts normally (does increment `consecutive_errors` if it's a search_cma failure).
5. Re-login + retry happens **once per address per step**. If a subsequent step in the same address hits 401/403 again, repeat the dance (rare but possible if session expires twice in 3 minutes).

Rationale: Propelio session TTL is unknown but observed to be on the order of hours; a 5-hour seed_5y pass is virtually guaranteed to cross at least one expiry. Without re-login, every long pass requires KK to re-launch.

### 7.6 SIGINT / SIGTERM (v1.1: tighter timeouts per Copilot MEDIUM)

Install a signal handler for SIGINT + SIGTERM:

1. Set a `should_stop = True` flag.
2. **After the current pull completes** (not mid-HTTP-call), check the flag.
3. If set: save state (current address stays `in_progress`), release lock, log "interrupted cleanly," exit 130 (SIGINT) or 143 (SIGTERM).
4. A second Ctrl-C / SIGTERM (within the soft-stop window) escalates to immediate `KeyboardInterrupt` propagation — abrupt exit without state save. Operator escape valve when a single Ctrl-C is taking too long.

**Bounded external-call timeouts** (so SIGTERM surfaces within a known bound):

| Call | Timeout | Implementation |
|---|---|---|
| Propelio `login`, `find_lead_id`, `add_cma` | 60s | Existing `PropelioClient` enforces a per-request timeout per `api/propelio/scraper.py`. Confirmed during R2 critique. |
| Propelio `search_cma` | **90s** (v1.2 — R2 IMPORTANT #3) | `search_cma` hardcodes `timeout=max(self.timeout, 90)` in `api/propelio/scraper.py`. Spec accepts this rather than diverging from strip_runner / deep_pull. |
| psycopg2 connect | 10s | `connect_timeout=10` in `api/config.py`'s pool config. Confirmed during R2 critique. |
| psycopg2 query (`statement_timeout`) | 60s | **v1.2 (R2 IMPORTANT #4):** injected via `SET LOCAL statement_timeout = '60s'` inside the merge transaction itself, mirroring the existing pattern at `api/main.py`. The `merge_comps_into_global_with_retry` wrapper applies this before the INSERT. Affects this scraper's writes only (the wrapper is new and exclusive to this caller). |
| `time.sleep` (pacing) | n/a — interruptible | Default Python `time.sleep` returns immediately on signal delivery. No special handling required. |

**Worst-case SIGTERM response time:** ~95s (one in-flight `search_cma` at 90s + state save + lock release). Spec accepts this as the safety/responsiveness trade-off.

**Documented hard-kill (SIGKILL / power loss) behavior:** state file is preserved at the last `os.replace` boundary (per address). Any address that was `in_progress` at the moment of kill is treated as `pending` on the next resume per §6.3 — fully redone. **No partial-address data loss** because individual comp writes are idempotent on `comp_address_key`.

### 7.7 End-of-pass

When every address has terminal status (`done`, `partial`, `failed`):

1. Set `current_pass.completed_at = now`.
2. Build a summary line: addrs done/partial/failed/total, total comps new, wall time, profile.
3. Append a slim summary to `history` (keep last 5).
4. Reset `current_pass = null`.
5. Save state atomically.
6. Print summary to stdout + log.
7. Release lock.
8. **Exit code 0.**

## 8. CLI surface

```
python -u scripts/production_scraper/run.py [flags]
```

| Flag | Required | Default | Purpose |
|---|---|---|---|
| `--profile NAME` | Yes | — | Filter profile name from `profiles.py`. |
| `--list PATH` | No | `scripts/production_scraper/master_list.txt` | Override master list path (useful for tests). |
| `--state-dir PATH` | No | `scripts/production_scraper/state/` | Override state directory. |
| `--log-dir PATH` | No | `scripts/production_scraper/logs/` | Override log directory. |
| `--restart` | No | false | Abandon any in-progress pass, start fresh. |
| `--dry-run` | No | false | Validate list + profile + state, print work queue, exit 0. Zero Propelio calls. |
| `--mock` | No | false | Skip real Propelio calls; use the smoke-mode persistence stub. For self-tests only. |

(v1.1: `--limit` dropped per Copilot Open Item #12 YAGNI verdict. Trivial to add back if cron-chunking becomes a real need.)

### 8.1 Exit codes

| Code | Meaning |
|---|---|
| 0 | Pass completed cleanly (or dry-run completed). |
| 1 | Clean exit short of pass completion (graceful soft-stop on first SIGINT/SIGTERM after some progress). |
| 2 | Auth/rate block — wait 30+ min, re-launch. 429 = immediate; 401/403 = exit only after re-login + retry both failed. (Matches strip_runner.) |
| 3 | Operator error — bad profile, bad list path, profile/list mismatch on resume, repo-root sanity check failed. |
| 4 | Another run already in progress (lock held by live PID). |
| 130 | SIGINT (Ctrl-C). |
| 143 | SIGTERM (e.g., from `systemd stop`). |

## 9. Logging

**Library:** stdlib `logging` (NOT shell `tee`). Configured once at startup with two handlers:

1. **StreamHandler → stdout** (so cron / nohup / tail-the-terminal works without indirection).
2. **FileHandler → `logs/<profile>-<UTC-ISO-timestamp>.log`** (created at startup).

**Symlink:** `logs/latest.log` updated at startup to point at the new file (via `os.symlink` with prior `unlink`). Operator can always `tail -f scripts/production_scraper/logs/latest.log`.

**Log lines** mirror strip_runner format for operator familiarity:

```
[15:00:00] === production_scraper start | profile=seed_5y | pass_id=... | list=master_list.txt (124 addrs) ===
[15:00:00] resume detected: 87 addrs done, 37 pending, 0 in_progress (treated as pending)
[15:00:01] address 88/124 (queue 1/37): 700 WOLF SPRINGS RD, DALLAS, TX
[15:00:04]   setup: add_cma ok   cma_id=1762345   (3.1s)
[15:00:35]   pass 1/5    60mo / 0.25mi   returned  12   new   4   addr_total   4
[15:01:08]   pass 2/5    60mo / 0.5mi    returned  35   new  18   addr_total  22
...
[15:03:12] address done | filters_ok=5/5 errored=0 | comps_returned=432 new=87
[15:03:12] state saved (37→36 pending)
...
[20:14:33] === pass complete | addrs done=120 partial=2 failed=2 / 124 | comps_new=9842 | wall=5h14m ===
```

**Rotation:** none. KK can manually prune; small footprint.

## 10. Cron integration (Phase 2)

**Sample crontab line** (run nightly at 02:00):

```
0 2 * * * cd /home/kk/projects/clients/lot-ledger && .venv/bin/python -u scripts/production_scraper/run.py --profile monthly_1m >> /var/log/production_scraper.cron 2>&1
```

**Behavior under cron:**

- If a prior cron-run is still going (e.g., it spilled over to >24h) → lock file held → next cron exits code 4 immediately. Logged. No duplicate work.
- If a prior cron-run crashed mid-pass → lock dead → next cron resumes the in-progress pass per §7.1.
- If the prior pass completed cleanly → next cron starts a fresh pass (refreshing every address).

**Suggested cron environment hygiene** (called out in the README):

- Cron has empty `$PATH` and no shell rc files. The crontab line should either `cd` into the repo (so the script imports `api.*` correctly) **and** use the absolute path to the venv Python (no `python` resolution).
- Cron emails on non-zero exit are useful — keep on (or rely on the `>>` redirect for audit log).

## 11. Error catalog (v1.1)

| Error class | Layer | Disposition | State outcome |
|---|---|---|---|
| Missing `.env` / missing DB creds | startup | exit 3 with operator message | state unchanged |
| Profile not in PROFILES | startup | exit 3 | state unchanged |
| Master list file missing | startup | exit 3 | state unchanged |
| Master list empty (after strip) | startup | exit 3 | state unchanged |
| Repo-root sanity check fails | startup | exit 3 with "scraper invoked from unexpected location" | state unchanged |
| Lock held by live PID | startup | exit 4 | state unchanged |
| Profile / list_sha256 mismatch on resume | startup | exit 3, suggest `--restart` | state unchanged |
| `find_lead_id` non-auth fail | per-address | mark address `failed`, continue | per-address |
| `find_lead_id` 401/403 | per-address | re-login + retry once per §7.5; if both fail → exit 2 | current address stays in_progress |
| `add_cma` non-auth fail | per-address | mark address `failed`, continue | per-address |
| `add_cma` 401/403 | per-address | re-login + retry once per §7.5; if both fail → exit 2 | current address stays in_progress |
| `search_cma` non-auth fail (<3 consecutive) | per-pull | log, continue to next pull | per-pull error count |
| `search_cma` non-auth fail (≥3 consecutive) | per-address | mark address `failed`, continue | per-address |
| `search_cma` 401/403 | per-pull | re-login + retry once per §7.5; if both fail → exit 2 | current address stays in_progress |
| `parcel_match` exception | per-pull | warn, fall back to unmatched merge | no impact (mirrors deep_pull) |
| **Merge connection-liveness fail** (v1.2 — `psycopg2.OperationalError`, "connection closed", socket reset) | per-pull | `merge_comps_into_global_with_retry` wrapper closes the connection, reacquires, retries once. If retry succeeds → normal flow. If retry fails → errored filter, counts toward burst guard. | per-pull / per-address depending on burst |
| **Merge deterministic fail** (v1.2 — `statement_timeout` exceedance, syntax error, constraint violation) | per-pull | NO retry (retrying burns another 60s for the same root cause). Immediately counts as errored filter. | per-pull / per-address depending on burst |
| **Parcel-match + unmatched-merge double-fail** (v1.2 — R2 Nice-to-have #2) | per-pull | Single DB-failure event. Goes through the same merge-retry path. Counts once toward burst guard. | per-pull / per-address |
| 429 anywhere | global | exit 2 immediately | current address stays in_progress |
| 401 or 403 anywhere | per-pull | `client.login(force=True)` + retry once; success resets `consecutive_errors` to 0; both-fail → exit 2 | current address stays in_progress on exit |
| psycopg2 connect timeout (10s) | per-pull | treated as a connection-liveness fail per the row above | per-pull / per-address |
| Propelio HTTP timeout (60s for login/find_lead_id/add_cma; 90s for search_cma) | per-pull | treated as a non-auth Propelio exception | per-pull error count |
| SIGINT / SIGTERM (first) | global | save + exit cleanly after current pull, exit 130/143 | current address stays in_progress |
| SIGINT / SIGTERM (second, within soft-stop) | global | abrupt KeyboardInterrupt | state at last save |
| Disk full on state write | per-address save | crash; rely on atomic-rename to leave prior good state | next launch resumes from last save |

## 12. Testing

`smoke.py` mirrors `strip_runner_smoke.py` — inline `_test_*` functions, run on import or via `python smoke.py`.

**Required coverage** (v1.1 expanded per Copilot critique):

1. Address parser:
    - BOM-prefixed file
    - Comments + blanks
    - Whitespace-collapse
    - Duplicate detection
    - Empty file → ValueError
    - Missing-city line → ValueError with line number
2. Profile resolver:
    - Known profile → matrix returned
    - Unknown profile → KeyError with valid-profile-names message
3. State file:
    - Fresh state → schema_version=1, current_pass=null, history=[]
    - Atomic write: writing to tmp + rename preserves prior on crash (simulated via mocked rename failure)
    - Load + save round-trip preserves all fields
    - `current_pass.completed_at` set → next load archives to history
    - `--restart` archives in-progress pass with `aborted=true`
    - History unbounded — 100 fake entries round-trip cleanly
4. Resume logic:
    - in_progress addresses treated as pending on load
    - profile mismatch → resume rejected
    - list_sha256 mismatch → resume rejected
    - clean resume → only pending addresses queue
    - list_sha256 computed over normalized queue (uppercase + dedup + source order)
5. Lock file:
    - Lock acquire on fresh state
    - Lock acquire on stale PID (dead process)
    - Lock acquire fail on live PID
6. Error classifier (reuse the strip_runner pattern OR import directly):
    - 401, 403, 429 status code matrix
    - "rate limit", "throttle", "unauthorized", "forbidden" message-fragment matrix
    - **NEW v1.1:** split-classification — `is_429(exc)` vs `is_401_or_403(exc)` for the re-login routing per §7.5.
7. **NEW v1.1 — Re-login + retry path** (per Copilot MEDIUM-HIGH):
    - On 401/403 in a mocked Propelio call: client.login() invoked, original call retried.
    - On re-login success + retry success → normal flow continues.
    - On re-login fail → exit 2.
    - On retry returning 401/403 again → exit 2.
    - On retry returning non-auth error → fall through to non-auth path.
8. **NEW v1.1 — DB-write retry path** (per Copilot HIGH):
    - On `merge_comps_into_global` exception in a mocked DB layer: connection close + retry on fresh connection.
    - Retry success → normal flow.
    - Retry fail → counts as errored filter (NOT silently swallowed).
    - 3 consecutive merge failures → address `failed` with skip_reason="3 consecutive DB+filter errors".
9. **NEW v1.1 — Shared comp_address_key invariant** (per Copilot Open Item #13):
    - `test_comp_address_key_shared`: builds a fixture comp dict, computes the key via `merge_comps_into_global`'s key-derivation helper. If anyone moves the key-derivation logic out of `api/propelio/archive.py` into a separate file, this test breaks (importable check). Smoke test guards the invariant.
10. **NEW v1.1 — Repo-root sanity check** (per Copilot Open Item #14):
    - Test invokes `run.py` with a fake `__file__` pointing somewhere with no `api/propelio/__init__.py` → exit 3 with the documented error.
11. Mock pull path (uses `_persist_pull_mock`-equivalent — counts returned/new from a per-address dedup set)

**Pilot run before first cron** — KK runs the seed_5y profile against a 3-address pilot list, observes:

- State file appears, populates as expected
- Ctrl-C produces exit 130 with state preserved
- Re-launch resumes and finishes
- `--restart` clears and starts fresh
- Final `current_pass=null`, history has 1 entry

## 13. Open items — none (v1.2)

All 15 items from v1.0 are resolved. Round-2 critique closed the last 6.

### Resolved items (v1.0 → v1.2 trail)

- v1.0 #1 (JSON vs SQLite): R1 Copilot agreed — keep JSON.
- v1.0 #2 (lock mechanism): R1 Copilot agreed — keep flock; documented POSIX-local-FS assumption.
- v1.0 #3 (per-address vs per-pull resume): R1 Copilot agreed — keep per-address.
- v1.0 #4 (list hash sensitivity): clarified to normalized-queue hash (§6.1, §7.1).
- v1.0 #5 (DB-write-fail handling): R1 HIGH → rewritten to retry + escalate (§7.2 + §11). R2 IMPORTANT refinement: classify connection-liveness vs deterministic failures, use a wrapper to own the retry lifecycle.
- v1.0 #6 (SIGTERM): R1 MEDIUM → bounded timeouts + double-signal escape (§7.6). R2 IMPORTANT refinement: accept 90s search_cma timeout (matches actual client code); worst-case SIGTERM ~95s.
- v1.0 #7 (pacing band): R2 Copilot recommendation — keep conservative 15-45s for v1, revisit with pilot-run data.
- v1.0 #8 (Cloud SQL longevity): R1 Copilot reframed — pool-per-call, not session longevity; resolved by §7.2 retry path.
- v1.0 #9 (master list location): R2 Copilot recommendation — keep committed `master_list.txt` for shippability + stable resume hashing.
- v1.0 #10 (--profile required): R2 Copilot agreement — keep required; accidental 60-month worse than minor CLI inconvenience.
- v1.0 #11 (history retention): bumped to unbounded with 1000-entry soft warning (§6.4 + §7.1 step 10).
- v1.0 #12 (--limit): dropped per R1 YAGNI.
- v1.0 #13 (concurrent strip_runner): explicit invariant + smoke test (§3.1 + §12.9).
- v1.0 #14 (cron env hygiene): startup walking-upward repo-root check (§7.1 step 2, v1.2-revised).
- v1.0 #15 (Propelio session expiry): R1 → re-login + retry promoted to v1 (§7.5). R2 IMPORTANT refinement: requires adding `force=True` to `PropelioClient.login()`; reset `consecutive_errors` on successful retry.

## 14. Future / deferred

- **Pass-history dashboard** — small HTML report from the `history` array. Backburner.
- **Multi-profile-per-cron** — alternate weeks between `monthly_1m` and a deeper `quarterly_3m` profile. Easy add to profiles.py + cron.
- **Per-county filter overrides** — some counties might want a different distance set. Profile schema can grow a `per_address_overrides` map later.
- **Slack/email on auth-block** — KK gets pinged when a cron run dies with code 2.
- **Session re-login on token expiry** — promote the deferred item from strip_runner spec §14.

## 15. Ship checklist (before merging to develop)

- [x] Spec round 1 (v1.0) critiqued by Copilot — 2026-05-26
- [x] Spec adjusted to v1.1 folding in R1 critique — 2026-05-26
- [x] Spec round 2 critiqued by Copilot — 2026-05-26
- [x] Spec adjusted to v1.2 folding in R2 critique — 2026-05-26
- [x] Spec **LOCKED at v1.2** — 2026-05-26 (Copilot R2 verdict: "no need for another full critique round before implementation")
- [ ] Implementation completed by Copilot per locked v1.2 spec (includes the two `api/propelio/` additions: `PropelioClient.login(force=False)` parameter, and new `merge_comps_into_global_with_retry` wrapper)
- [ ] `smoke.py` passes (all 11 test groups per §12)
- [ ] Pilot run on 3-address list succeeds (incl. Ctrl-C / resume / --restart / forced re-login / forced DB-write fail / SIGTERM-during-pull / stale-tmp cleanup)
- [ ] Pilot run logs reviewed by Claude
- [ ] README reviewed by KK
- [ ] `master_list.txt` populated from Obsidian Scraper List (4-county sweep)
- [ ] First Phase 1 seed_5y run launched manually, monitored to completion
- [ ] Cron line drafted for Phase 2 (not yet enabled — wait for KK signal)
