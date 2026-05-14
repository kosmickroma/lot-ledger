# Strip Runner — Design v1.2

**Status:** Copilot rounds 1 and 2 review folded in (2026-05-14). Build-eligible pending KK approval.
**Author:** KK + Claude (brainstorm 2026-05-14).
**Successor to:** the marathon scraper at `scripts/marathon_campaign/` (which remains untouched on `feat/marathon-campaign`).

## Changes v1.1 → v1.2

1. **§6 / §8 (Copilot R2 finding R2-1, IMPORTANT):** `add_cma` failure now correctly treated as **address-level** skip, not filter-level. Filter-level error row narrowed to `search_cma` only. Prevents the `cma_id`-unbound crash in the `search_cma` loop.
2. **§9 (Copilot R2 finding R2-2, nice-to-have):** Added `setup: add_cma ok cma_id=… (Ns)` log line between address header and pass 1. Closes the silent-gap concern during a multi-hour run.
3. **§6 / §7 (Copilot R2 finding R2-3, nice-to-have):** 3-5s fixed-range pause after `add_cma` before the first `search_cma`. Closes the immediate-burst gap on the same CMA object.
4. **§7 / §14 editorial:** Moved the "drop band A floor to 10s on run 2" relaxation note out of a §7 parenthetical and into §14 as an explicit deferred item. Added KK's future-tuning principle to §7 (*speed up gradually but always preserve the non-uniform two-band shape*).

## Changes v1 → v1.1

1. **§5 / §6 (Copilot R1 finding #1 — Option A):** `add_cma` reduced to CMA-setup; comps from it are discarded; all 21 filter pulls now run via `search_cma`. Per-address Propelio calls go from 21 to 22.
2. **§3 / §8 / §14 (Copilot R1 finding #2):** Token-expiry exit documented as expected, idempotent re-run path captured, re-login-on-expiry parked.
3. **§7 (Copilot R1 finding #3):** Pacing average corrected (22s → 27s); band A floor raised to 15s for conservative-first-run.
4. **§3 / §12 (Copilot R1 finding #4):** Repo-root invocation requirement made explicit.
5. **§8 (Copilot R1 finding #5):** 3-consecutive-filter-errors guard added.

---

## 1. Purpose

Run Propelio comp pulls against a hand-curated list of addresses with a fixed 21-filter matrix per address. Bypass the marathon machinery entirely. KK supervises start/stop manually.

**Why a new tool instead of fixing the marathon runner:** the marathon runner is too complex for what KK actually needs — its FSM, circuit breaker, orphan reconciler, "human break" pacing, and grid generator together produce a system that "fails and sits a lot" with output quality KK is not happy with. KK has been manually pulling addresses across Dallas with different filters and getting better coverage. Strip Runner formalizes the manual workflow: he picks the addresses and the filters, the script just executes them in order.

**Success criterion:** KK kicks off a run against a ~25-address strip, walks away for 3-5 hours, comes back to a complete or near-complete result with a clear list of any failures he should spot-check manually.

## 2. Non-goals

The following are deliberately out of scope:

- Auto-recovery of failed seeds or addresses (KK re-runs misses by hand)
- Multi-process / multi-runner safety
- Grid generation or auto address discovery
- Density classification
- Stealth "human break" simulation (no coffee/lunch breaks)
- DB-tracked run state or campaign rows
- Saturation early-exit (we run all 21 filters per address regardless of duplicate rate)
- Concurrency between addresses
- Web UI or API endpoint

## 3. Architecture

Single Python script, sequential loop, one Propelio session per run.

```
scripts/
├── strip_runner.py                          (~200 lines, single file)
└── strip_runner_addresses/
    └── strip_dallas_south.txt               (KK populates by paste)
```

Invocation:

```
# Must be run from the repo root so the api.propelio.* imports resolve.
cd /path/to/lot-ledger
python scripts/strip_runner.py --addresses scripts/strip_runner_addresses/strip_dallas_south.txt
```

Per-run flow:

1. Read and validate the address list from the text file
2. `PropelioClient(...).login()` once at start
3. For each address in order: 1 × `find_lead_id` → 1 × `add_cma` (CMA setup only, comps discarded — see §5/§6) → 21 × `search_cma` (one per filter)
4. After each Propelio call: parse comps, parcel-match, merge into global `propelio_comps` cache (same code path `api/propelio/deep_pull.py` uses today)
5. Per-pull log line to stdout
6. Jitter sleep between pulls within an address; tighter sleep between addresses
7. On address-level error: skip remaining filters for that address, continue to next
8. On filter-level error: log and continue to next filter
9. On auth/rate error: log CRITICAL and exit immediately
10. End-of-run stdout summary

The script keeps **no persistent state** across runs. Re-running the same address file is safe: `merge_comps_into_global` writes are idempotent on `comp_address_key`.

## 4. Address list format

Plain UTF-8 text. One address per line. Lines starting with `#` are comments. Blank lines are ignored. No CSV, no quoting, no headers.

Example:

```
# strip_dallas_south.txt
# Test strip across the south side of Dallas County, 2026-05-14

1234 Main St, Dallas TX 75201
5678 Oak Ave, Dallas TX 75223

9012 Pine Rd, Dallas TX 75228
```

Validation rules at startup:

- File must exist and be readable
- After stripping comments/blanks, must contain ≥1 address
- Each remaining line is trimmed; no further normalization (Propelio's `find_lead_id` handles fuzzy address matching)

If validation fails, print the reason and exit 1 before touching Propelio.

## 5. Filter matrix

Constant inside `strip_runner.py`:

```python
FILTERS: list[tuple[int, float]] = [
    # 24-month band
    (24, 5.0), (24, 2.0), (24, 1.0), (24, 0.5), (24, 0.25),
    # 12-month band
    (12, 5.0), (12, 2.0), (12, 1.0), (12, 0.5), (12, 0.25),
    # 6-month band
    (6, 5.0), (6, 2.0), (6, 1.0), (6, 0.5), (6, 0.25),
    # 3-month band
    (3, 1.0), (3, 0.5), (3, 0.25),
    # 1-month band
    (1, 1.0), (1, 0.5), (1, 0.25),
]  # 21 entries
assert len(FILTERS) == 21
```

All 21 filters run via `search_cma`. The preceding `add_cma` call exists only to create the CMA on Propelio's side and obtain a `cma_id` — its returned comps are **discarded**. This is because for pre-existing leads (which KK has been hand-pulling for weeks, so the strip will be mostly pre-existing) `add_cma` returns the previously-cached CMA with whatever filter was last active in the Propelio UI, ignoring the months/range we send. `search_cma`'s docstring confirms it honors the filter parameters on every call, so we route all data pulls through it. Resolution of Copilot v1 review finding #1 (Option A).

**Note:** This re-introduces the 2mi and 5mi pulls that the current marathon `pass_configs.py` deliberately dropped. KK confirmed he wants the wide rings back for this test.

## 6. Per-address execution

This is the heart of the script. Per address:

1. `lead_id, subject_lot_sqft, parcel_bundle = client.find_lead_id(address)` — extract `confirmation_key` from `parcel_bundle`. On exception, skip this address (see §8).
2. **CMA setup (no data pull):**
   - `envelope = client.add_cma(lead_id, confirmation_key, months=FILTERS[0][0], range_mi=FILTERS[0][1])` — purpose: obtain `cma_id` only
   - `cma_id = _extract_cma_id(envelope)`
   - **Comps from this envelope are discarded.** See §5 for the rationale (pre-existing-lead behavior).
   - **On exception from `add_cma` or `_extract_cma_id` (non-auth):** treat as address-level — log `WARNING [address] cma setup failed: <exc>`, skip remaining steps for this address, continue to next address. Cannot fall through to the search_cma loop because `cma_id` is unbound (Copilot v1.1 review finding R2-1).
   - **Setup → first pull pause:** `time.sleep(random.uniform(3, 5))` before entering step 3. Closes the immediate-burst gap between `add_cma` and the first `search_cma` against the same CMA (Copilot v1.1 review finding R2-3).
3. **21 filter pulls** — for each `(months, range_mi)` in `FILTERS` (all 21 entries, including `FILTERS[0]`):
   - Jittered sleep (see §7) before each pull *except* the very first of the address (the 3-5s setup gap from step 2 already separates `add_cma` from the first `search_cma`)
   - `envelope = client.search_cma(lead_id, cma_id, months=months, range_mi=range_mi)`
   - `comps = _parse_cma_envelope_comps(envelope)`
   - Process and persist (see step 4)
4. **Persist per pull:**
   - `parsed = [asdict(_parse_property(raw)) for raw in comps]`
   - `matched = match_comps_to_parcels(parsed)` — on parcel-match exception, log WARNING and continue with `matched = parsed` (mirror existing `deep_pull.py` behavior)
   - `merge_result = merge_comps_into_global(matched, source="strip_runner")`
   - `inserted = int(merge_result.get("inserted", 0))` — this is the per-pull net-new count

Total Propelio interactions per address: **22** (1 add_cma setup + 21 search_cma).
Total CMAs created per address on Propelio's side: **1**.

## 7. Pacing

Tighter than the current marathon. No coffee/lunch breaks. KK starts and stops the script manually when he wants longer pauses.

**Inter-pull (within one address, between filter pulls):**

- 80% of pauses: `random.uniform(15, 30)` seconds — band A
- 20% of pauses: `random.uniform(30, 60)` seconds — band B
- Weighted average: 0.8 × 22.5 + 0.2 × 45 ≈ **27s**. The two-band distribution avoids a uniform-distribution fingerprint without modeling explicit "distraction" breaks.

**Inter-address (between addresses):**

- `random.uniform(15, 45)` seconds. Average ~30s.

**Setup → first pull (after `add_cma`, before first `search_cma`):**

- `random.uniform(3, 5)` seconds (fixed-range, no jitter band). Closes the immediate-burst gap on the same CMA. See §6 step 2.

**No `maybe_take_break` logic.** No multi-minute or multi-hour pauses.

**Future-tuning principle (KK):** *speed up gradually across runs, but always preserve the non-uniform two-band shape — don't fully look like a bot.* Floor reductions and upper-bound trims are deferred to runs 2+ once we observe whether Propelio rate-limits the burst pattern. See §14 for the parked relaxation knob.

Estimated wall time:

- Per address: 21 pauses × ~27s + 1 setup pause × ~4s + 22 calls × ~3-5s response ≈ **10-12 min**
- 25-address strip (including 24 × ~30s inter-address pauses): **~4-5 hours**

## 8. Error handling

Three error classes only. No retries. No backoff. No state persisted between filters or addresses.

| Class | Detection | Response |
|---|---|---|
| **Auth / rate** | `_classify_propelio_error(exc) == "blocked"` (reuse helper from `api/propelio/deep_pull.py`) | Log `CRITICAL [address] [filter] auth/rate block: <exc>` and exit with code 2. KK investigates before re-running. |
| **Address-level** (lead lookup) | Any exception from `client.find_lead_id(...)` | Log `WARNING [address] lead lookup failed: <exc>`. Skip ALL remaining filters for this address. Continue to next address. |
| **Filter-level** (`search_cma` only) | Any non-auth exception from `client.search_cma(...)` | Log `WARNING [address] <M>mo/<R>mi cma call failed: <exc>`. Continue to next filter for the same address — **unless** the burst-filter-errors guard below trips. |
| **CMA setup failure** (`add_cma` / `_extract_cma_id`) | Any non-auth exception from `client.add_cma(...)` or `_extract_cma_id(...)` in §6 step 2 | Log `WARNING [address] cma setup failed: <exc>`. **Address-level skip**, not filter-level — `cma_id` is unbound, so the `search_cma` loop cannot proceed. Continue to next address. (Copilot v1.1 review finding R2-1.) |
| **Burst filter errors** | 3+ consecutive filter-level errors against the same address | Log `WARNING [address] 3 consecutive filter errors — skipping remaining filters for this address` and treat as address-level. Continue to next address. Prevents misreading a broken session as a normal partial run (Copilot v1 review finding #5). |

**Parcel-match exceptions are not address-level or filter-level failures** — they're handled inside the persist step with a WARNING log and a fallback to unmatched merge, exactly as `deep_pull.py` does today. The pull still counts as successful.

**If the run is killed mid-strip (Ctrl-C or crash):** no recovery. KK re-runs from the start. Comp writes are idempotent on `propelio_comps.comp_address_key`, so duplicates dedupe naturally inside `merge_comps_into_global`.

**Expected mid-run exit on token expiry (Copilot v1 review finding #2):** Propelio's auth tokens have a TTL and the script does not re-authenticate. For runs that outlive the token, the first call after expiry returns a 401/403 → `_classify_propelio_error` returns `"blocked"` → CRITICAL exit with code 2. This is an expected, recoverable failure mode:

1. The last successful address is visible in the terminal log
2. Re-run the same address file; already-done addresses produce 0 net-new comps (idempotent global cache)
3. Optionally trim the address file to skip the already-done prefix to save Propelio pulls

Re-login-on-expiry is parked in §14.

## 9. Per-pull terminal logging

Every pull writes one aligned line to stdout so KK can scan progress in real time during a multi-hour run. Format:

```
[12:05:14] address 4/25: 1234 Main St, Dallas TX 75201
[12:05:18]   setup: add_cma ok   cma_id=cma_a1b2c3d4   (3.8s)
[12:05:22]   pass  1/21   24mo / 5.0mi   returned 287   new 287   addr_total 287
[12:05:48]   pass  2/21   24mo / 2.0mi   returned 142   new  18   addr_total 305
[12:06:15]   pass  3/21   24mo / 1.0mi   returned  87   new   6   addr_total 311
[12:06:41]   pass  4/21   24mo / 0.5mi   returned  42   new   2   addr_total 313
...
[12:24:39]   pass 21/21    1mo / 0.25mi  returned   3   new   0   addr_total 487
[12:24:39]   address done: 21/21 filters ok, 487 net-new comps to cache
[12:25:02]
[12:25:02] address 5/25: 5678 Oak Ave, Dallas TX 75223
...
```

The `setup:` line (Copilot v1.1 review finding R2-2) prints between the address header and pass 1 so KK isn't staring at a silent 3-8s gap during the `add_cma` call + setup pause. It shows the `cma_id` for cross-referencing Propelio manually if needed, and the elapsed time of the `add_cma` round-trip. If CMA setup fails, the line reads:

```
[12:25:08]   setup: add_cma failed — HTTPError 503; skipping address
```

Column meanings:

- `returned` — number of comps Propelio returned for that pull
- `new` — number of those comps that were net-new to `propelio_comps` (i.e., `merge_result["inserted"]`)
- `addr_total` — running sum of `new` counts for the current address (resets at each new address)

Errors print inline with the same indentation so the visual rhythm survives:

```
[12:18:42]   pass 12/21    6mo / 2.0mi   ERROR HTTPError 502 — continuing
```

Per-address footer line summarizes:

```
[12:24:39]   address done: 20/21 filters ok, 1 errored, 423 net-new comps to cache
```

The single net-new number is the sum of per-pull `new` counts for the address. We avoid double-reporting "comps written" since `merge_comps_into_global` only inserts net-new rows — "written" and "net-new" are the same metric in our pipeline.

Or for an address-level skip:

```
[12:30:01]   address skipped: lead lookup failed
```

**Implementation note:** use `print()` with a hand-formatted `[HH:MM:SS]` prefix rather than the `logging` module. Cleaner column alignment, no log-level noise. Stderr is reserved for unexpected Python tracebacks only.

## 10. End-of-run summary

Plain stdout, after the main loop exits (or after auth-block exit):

```
=== strip_runner summary ===
addresses_total:        25
addresses_complete:     22  (all 21 filters fired)
addresses_partial:       2  (some filters errored)
addresses_skipped:       1  (lead lookup failed)
filter_pulls_total:    504
propelio_returned_sum: 18420  (raw comps returned across all pulls, duplicates included)
comps_net_new_total:  1247  (rows inserted into propelio_comps cache)
elapsed_min:           178
```

The two-number split lets KK eyeball efficiency: 18420 returned vs 1247 net-new means ~6.8% net-new rate. Low rates indicate the area is already well-cached or pulls overlap heavily.

If `addresses_partial > 0` or `addresses_skipped > 0`, also print the lists:

```
addresses_partial:
  - 5678 Oak Ave, Dallas TX 75223  (1 filter errored)
  - 8888 Elm St, Dallas TX 75215   (3 filters errored)
addresses_skipped:
  - 9999 Bad St, Dallas TX 00000   (lead lookup failed)
```

That's KK's manual re-run list.

## 11. What this design explicitly does NOT use

Listed so the Copilot review doesn't suggest reintroducing them:

- `scripts/marathon_campaign/` — the entire package
- `propelio_marathon_seeds`, `propelio_marathon_campaigns`, `propelio_marathon_allowed_transitions` tables
- `CircuitBreaker`, `wait_for_cooldown_or_exit`
- `transition()`, `IllegalStateTransition`, the 8-state FSM
- `reconcile_orphans`, orphan recovery, `heartbeat_at` tracking
- `inter_seed_pause_seconds`, `maybe_take_break` from `pacing.py`
- `emit_event`, `alert` from `events.py` / `alerts.py`
- `passes_for_density_class`, `density_class` classification
- `propelio_deep_pull_jobs` rows (we call `client.add_cma` / `client.search_cma` directly, bypassing the deep-pull job abstraction)
- `propelio_deep_pull_experiment` per-pass dedup table

## 12. Dependencies (explicit reuse from existing code)

- `api/propelio/scraper.py` — `PropelioClient`, `_parse_property`
- `api/propelio/archive.py` — `merge_comps_into_global`
- `api/propelio/parcel_match.py` — `match_comps_to_parcels`
- `api/propelio/config.py` — `PROPELIO_USERNAME`, `PROPELIO_PASSWORD` env vars
- `api/propelio/deep_pull.py` — `_classify_propelio_error`, `_parse_cma_envelope_comps`, `_extract_cma_id`

**Decision (resolved by Copilot v1 review):** import the three underscore-prefixed helpers from `deep_pull.py` directly. They're each 2-5 lines of defensive list/dict access; drift risk is low and DRY wins. `strip_runner.py` must include a comment flagging the coupling:

```python
# Coupled to api/propelio/deep_pull.py — keep in sync if Propelio response shape changes.
from api.propelio.deep_pull import (
    _classify_propelio_error,
    _parse_cma_envelope_comps,
    _extract_cma_id,
)
```

**Repo-root invocation requirement:** the imports above (and all `api.propelio.*` imports) only resolve when Python is launched from the repo root. Running `python strip_runner.py` from within `scripts/` will fail with `ModuleNotFoundError`. The invocation example in §3 already enforces this with the explicit `cd` step.

## 13. Branch and commit strategy

- Feature branch: `feat/strip-runner` off `develop`
- This spec lives at `docs/propelio/STRIP_RUNNER_SPEC.md` and travels with the feature branch (precedent: marathon spec lives at `docs/propelio/MARATHON_CAMPAIGN_SPEC.md` on `feat/marathon-campaign`).
- After Copilot review, write implementation plan via `superpowers:writing-plans`.
- After first successful test run, decide: merge to `develop` or keep on `feat/strip-runner` indefinitely. This is a local-operator tool; it will not be deployed to Cloud Run.

## 14. Deferred (out of scope for v1, parked for later)

- **Weekly refresh mode:** rerun a saved address list with `[(1, 1.0), (1, 0.5), (1, 0.25)]` only — fast cache freshening pass
- **Saturation early-exit:** skip remaining filters when net-new rate falls below a threshold (e.g., 3 consecutive pulls with `new < 2`)
- **Pacing relaxation (run 2+):** after run 1 succeeds without rate-limit symptoms, drop band A floor 15s → 10s and/or reduce band B weight (20% → 10%) to compress wall time. Apply per KK's future-tuning principle in §7 — preserve the two-band shape, don't fully look like a bot.
- **Re-login on token expiry:** detect 401/403 from `_classify_propelio_error`, instantiate a fresh `PropelioClient`, call `login()`, retry the failed call. Eliminates the mid-run exit described in §8. Easy ~15-line addition; deferred so v1 stays minimal.
- **Persist address list to DB:** `strip_runner_addresses` table with per-run history and per-address run counters
- **Concurrency:** 2-3 addresses in parallel against Propelio (would need careful session/rate-limit handling)
- **Per-comp link-back deep links:** the listing-URL work already tracked in memory under `project_propelio_listing_url.md`

---

## Copilot review — outcomes (2026-05-14)

### Round 1 (v1 → v1.1)

Five findings, no blockers. All addressed in v1.1:

| # | Section | Severity | Resolution |
|---|---|---|---|
| 1 | §5, §6 | IMPORTANT | Option A adopted — `add_cma` is CMA-setup only; comps discarded; all 21 pulls run through `search_cma`. Per-address Propelio calls: 22 (was 21). |
| 2 | §3, §8, §14 | IMPORTANT | Token-expiry exit documented as expected + idempotent re-run path; re-login parked in §14. |
| 3 | §7 | IMPORTANT | Pacing avg corrected (22s → 27s); band A floor raised to 15s for conservative-first-run; revisit after run 1. |
| 4 | §3, §12 | IMPORTANT | Repo-root invocation requirement made explicit in invocation block and §12. |
| 5 | §8 | NICE-TO-HAVE | 3-consecutive-filter-errors guard added to error table. |

The `search_cma` CMA-state-corruption concern in the original review note for §8 was explicitly addressed by Copilot: each `search_cma` POST is stateless, a failed call leaves the CMA intact, so continuing to the next filter is safe.

### Round 2 (v1.1 → v1.2)

R1 findings 1-4 all held under fresh scrutiny. One new IMPORTANT and two nice-to-haves. All addressed in v1.2:

| # | Section | Severity | Resolution |
|---|---|---|---|
| R2-1 | §6, §8 | IMPORTANT | `add_cma` failure was incorrectly classified as filter-level; would have crashed the script with `NameError: cma_id` when the search_cma loop ran. Reclassified to address-level skip. Filter-level row narrowed to `search_cma` only. |
| R2-2 | §9 | NICE-TO-HAVE | Added `setup: add_cma ok cma_id=... (Ns)` log line between address header and pass 1, so KK doesn't see a silent 3-8s gap during the CMA setup phase. |
| R2-3 | §6, §7 | NICE-TO-HAVE | Added `random.uniform(3, 5)` fixed-range pause after `add_cma` before the first `search_cma`. Closes the immediate-burst gap (the only point where two calls hit the same CMA object back-to-back). |

Copilot's round-2 verdict: with R2-1 resolved, the spec is build-eligible.
