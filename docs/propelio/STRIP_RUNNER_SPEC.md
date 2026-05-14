# Strip Runner — Design v1

**Status:** Draft for Copilot deep-dive review.
**Author:** KK + Claude (brainstorm 2026-05-14).
**Successor to:** the marathon scraper at `scripts/marathon_campaign/` (which remains untouched on `feat/marathon-campaign`).

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
python scripts/strip_runner.py --addresses scripts/strip_runner_addresses/strip_dallas_south.txt
```

Per-run flow:

1. Read and validate the address list from the text file
2. `PropelioClient(...).login()` once at start
3. For each address in order: 1 × `find_lead_id` → 1 × `add_cma` (filter 1) → 20 × `search_cma` (filters 2-21)
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

`FILTERS[0]` (`(24, 5.0)`) is the **creation filter** — runs via `add_cma`, which creates the CMA on Propelio's side and returns a `cma_id`. `FILTERS[1:]` (the remaining 20) run via `search_cma` against that same `cma_id`, varying months and range only.

**Note:** This re-introduces the 2mi and 5mi pulls that the current marathon `pass_configs.py` deliberately dropped. KK confirmed he wants the wide rings back for this test.

## 6. Per-address execution

This is the heart of the script. Per address:

1. `lead_id, subject_lot_sqft, parcel_bundle = client.find_lead_id(address)` — extract `confirmation_key` from `parcel_bundle`. On exception, skip this address (see §8).
2. **Pass 1 (creation):**
   - `envelope = client.add_cma(lead_id, confirmation_key, months=24, range_mi=5.0)`
   - `cma_id = _extract_cma_id(envelope)`
   - `comps = _parse_cma_envelope_comps(envelope)`
   - Process and persist (see step 4)
3. **Passes 2-21 (reuse):**
   - For each `(months, range_mi)` in `FILTERS[1:]`:
     - Jittered sleep (see §7)
     - `envelope = client.search_cma(lead_id, cma_id, months=months, range_mi=range_mi)`
     - `comps = _parse_cma_envelope_comps(envelope)`
     - Process and persist (see step 4)
4. **Persist per pull:**
   - `parsed = [asdict(_parse_property(raw)) for raw in comps]`
   - `matched = match_comps_to_parcels(parsed)` — on parcel-match exception, log WARNING and continue with `matched = parsed` (mirror existing `deep_pull.py` behavior)
   - `merge_result = merge_comps_into_global(matched, source="strip_runner")`
   - `inserted = int(merge_result.get("inserted", 0))` — this is the per-pull net-new count

Total Propelio interactions per address: **21** (1 add_cma + 20 search_cma).
Total CMAs created per address on Propelio's side: **1**.

## 7. Pacing

Tighter than the current marathon. No coffee/lunch breaks. KK starts and stops the script manually when he wants longer pauses.

**Inter-pull (within one address, between filter pulls):**

- 80% of pauses: `random.uniform(10, 30)` seconds
- 20% of pauses: `random.uniform(30, 60)` seconds
- Average ~22s. The two-band distribution avoids a uniform-distribution fingerprint without modeling explicit "distraction" breaks.

**Inter-address (between addresses):**

- `random.uniform(15, 45)` seconds. Average ~30s.

**No `maybe_take_break` logic.** No multi-minute or multi-hour pauses.

Estimated wall time:

- Per address: 21 pulls × ~22s pause + ~3-5s Propelio response per pull ≈ **8-12 min**
- 25-address strip: **~3-5 hours**

## 8. Error handling

Three error classes only. No retries. No backoff. No state persisted between filters or addresses.

| Class | Detection | Response |
|---|---|---|
| **Auth / rate** | `_classify_propelio_error(exc) == "blocked"` (reuse helper from `api/propelio/deep_pull.py`) | Log `CRITICAL [address] [filter] auth/rate block: <exc>` and exit with code 2. KK investigates before re-running. |
| **Address-level** (lead lookup) | Any exception from `client.find_lead_id(...)` | Log `WARNING [address] lead lookup failed: <exc>`. Skip ALL remaining filters for this address. Continue to next address. |
| **Filter-level** (CMA call) | Any non-auth exception from `client.add_cma(...)` or `client.search_cma(...)` | Log `WARNING [address] <M>mo/<R>mi cma call failed: <exc>`. Continue to next filter for the same address. |

**Parcel-match exceptions are not address-level or filter-level failures** — they're handled inside the persist step with a WARNING log and a fallback to unmatched merge, exactly as `deep_pull.py` does today. The pull still counts as successful.

**If the run is killed mid-strip (Ctrl-C or crash):** no recovery. KK re-runs from the start. Comp writes are idempotent on `propelio_comps.comp_address_key`, so duplicates dedupe naturally inside `merge_comps_into_global`.

## 9. Per-pull terminal logging

Every pull writes one aligned line to stdout so KK can scan progress in real time during a multi-hour run. Format:

```
[12:05:14] address 4/25: 1234 Main St, Dallas TX 75201
[12:05:15]   pass  1/21   24mo / 5.0mi   returned 287   new 287   addr_total 287
[12:05:41]   pass  2/21   24mo / 2.0mi   returned 142   new  18   addr_total 305
[12:06:08]   pass  3/21   24mo / 1.0mi   returned  87   new   6   addr_total 311
[12:06:34]   pass  4/21   24mo / 0.5mi   returned  42   new   2   addr_total 313
...
[12:24:32]   pass 21/21    1mo / 0.25mi  returned   3   new   0   addr_total 487
[12:24:32]   address done: 21/21 filters ok, 487 comps written, 423 net-new
[12:24:55]
[12:24:55] address 5/25: 5678 Oak Ave, Dallas TX 75223
...
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
[12:24:32]   address done: 20/21 filters ok, 1 errored, 423 net-new comps to cache
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

**Open question for Copilot:** the three helpers from `deep_pull.py` are underscore-prefixed (informally "private"). Two options:

1. **Import directly** — keeps strip_runner DRY. Risk: a future refactor of `deep_pull.py` could break strip_runner silently because the helpers aren't part of any public API contract.
2. **Re-implement locally** — strip_runner becomes fully isolated from `deep_pull.py`. Risk: the parsing logic drifts if Propelio's response shape changes and only one side gets updated.

Recommendation in this spec: **import directly**, with a comment in `strip_runner.py` flagging the coupling. The functions are small enough that drift risk is low, and the underscore prefix in `deep_pull.py` is more about Python style than enforcement.

## 13. Branch and commit strategy

- Feature branch: `feat/strip-runner` off `develop`
- This spec lives at `docs/propelio/STRIP_RUNNER_SPEC.md` and travels with the feature branch (precedent: marathon spec lives at `docs/propelio/MARATHON_CAMPAIGN_SPEC.md` on `feat/marathon-campaign`).
- After Copilot review, write implementation plan via `superpowers:writing-plans`.
- After first successful test run, decide: merge to `develop` or keep on `feat/strip-runner` indefinitely. This is a local-operator tool; it will not be deployed to Cloud Run.

## 14. Deferred (out of scope for v1, parked for later)

- **Weekly refresh mode:** rerun a saved address list with `[(1, 1.0), (1, 0.5), (1, 0.25)]` only — fast cache freshening pass
- **Saturation early-exit:** skip remaining filters when net-new rate falls below a threshold (e.g., 3 consecutive pulls with `new < 2`)
- **Persist address list to DB:** `strip_runner_addresses` table with per-run history and per-address run counters
- **Concurrency:** 2-3 addresses in parallel against Propelio (would need careful session/rate-limit handling)
- **Per-comp link-back deep links:** the listing-URL work already tracked in memory under `project_propelio_listing_url.md`

---

## Review notes for Copilot

Things I want a deep-dive review on:

1. **Section 6 (per-address execution)** — am I correct that `client.add_cma` + 20 × `client.search_cma` with the same `cma_id` is the right Propelio shape for what KK wants? Or does each filter combo need its own `add_cma` (new CMA per filter)? The current `deep_pull.py` reuses one CMA, but it only runs 6 passes. Does Propelio's `search_cma` cleanly support 20 sequential filter changes against one CMA?

2. **Section 7 (pacing)** — is 10-60s inter-pull + 15-45s inter-address aggressive enough to trip Propelio's anti-bot, or appropriately conservative? The current marathon uses 15-120s inter-pass and 30-180s inter-seed, but KK explicitly wants tighter pacing.

3. **Section 8 (error handling)** — is "log filter-level errors and continue to the next filter for the same address" correct, or should a filter error invalidate the rest of that address's pulls? Could a transient `search_cma` failure corrupt the CMA state on Propelio's side and make subsequent pulls return garbage?

4. **Section 12 (open question)** — import vs. re-implement the three helpers from `deep_pull.py`. Weigh in.

5. **Anything else** that looks load-bearing-fragile.
