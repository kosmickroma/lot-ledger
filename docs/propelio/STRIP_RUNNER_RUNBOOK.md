# Strip Runner — Operator Runbook

How to run the strip runner against a hand-curated list of Dallas County addresses to warm the global `propelio_comps` cache.

**Design:** see [`STRIP_RUNNER_SPEC.md`](./STRIP_RUNNER_SPEC.md) (v1.3) for rationale and [`STRIP_RUNNER_PLAN.md`](./STRIP_RUNNER_PLAN.md) for the implementation breakdown.

**Branch:** lives on `feat/strip-runner` (not yet merged to `develop` as of 2026-05-14).

---

## 1. Quick Start — From a Fresh Terminal

Six steps. Copy-paste each block. Assumes you've already cloned the repo and the `.venv` exists (one-time setup steps in §1.1 below if not).

**Step 1 — Open a terminal and go to the repo.**

```bash
cd /home/kk/projects/clients/lot-ledger
```

**Step 2 — Switch to the strip runner branch and pull any updates.**

```bash
git checkout feat/strip-runner
git pull
```

**Step 3 — Make sure the log directory exists (only matters on first run after reboot).**

```bash
mkdir -p /tmp/strip_runner_logs
```

**Step 4 — Drop your addresses into a new file under `scripts/strip_runner_addresses/`.**

Pick a descriptive filename (the file becomes part of run history). Two ways to create it:

```bash
# Option A — open it in nano and type/paste the addresses
nano scripts/strip_runner_addresses/<your-run-name>.txt
```

```bash
# Option B — paste the addresses inline (one per line, # for comments)
cat > scripts/strip_runner_addresses/<your-run-name>.txt <<'EOF'
# <your-run-name> — brief description, date
1234 Main St
5678 Oak Ave
9012 Pine Rd
EOF
```

(Optional — commit the address file as run history. `*.txt` is gitignored, so use `-f`.)

```bash
git add -f scripts/strip_runner_addresses/<your-run-name>.txt
git commit -m "chore(strip-runner): <your-run-name> address list"
git push
```

**Step 5 — Kick off the run.**

```bash
.venv/bin/python3 -u scripts/strip_runner.py \
    --addresses scripts/strip_runner_addresses/<your-run-name>.txt 2>&1 \
    | tee /tmp/strip_runner_logs/<your-run-name>.log
```

**Step 6 (optional) — Watch live from a second terminal.**

```bash
tail -f /tmp/strip_runner_logs/<your-run-name>.log
```

`Ctrl-C` on the `tail -f` detaches your viewer; it does **not** stop the runner.

**Three non-obvious flags / details:**

- `.venv/bin/python3` — the system Python doesn't have `dotenv` etc.; the strip runner needs the project venv.
- `-u` — unbuffered stdout. Without this, output is block-buffered when piped through `tee` and you won't see anything for ~10 minutes.
- `tee` is optional but makes after-the-fact log inspection trivial.

### 1.1 One-time setup (only if `.venv` doesn't exist)

```bash
cd /home/kk/projects/clients/lot-ledger
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then verify `.env` has `PROPELIO_USERNAME` and `PROPELIO_PASSWORD`:

```bash
grep -E "^PROPELIO_(USERNAME|PASSWORD)" .env
```

Both should print (values redacted by your shell history but the lines exist).

---

## 2. Address list format

Plain UTF-8 text, one address per line. Lines starting with `#` are comments. Blank lines are ignored.

```
# strip_dallas_south_2026_05_14.txt
# Two rows across south Dallas County, ~1mi spacing.

143 BILINDSAY COVE
2101 INDIA RD
700 WOLF SPRINGS RD
...
```

The runner passes addresses directly to Propelio's `find_lead_id`. DCAD-style addresses (just street portion) work for Dallas — Propelio's fuzzy match handles them. For ambiguous addresses, append `", Dallas TX <ZIP>"` for safety.

**Important:** `*.txt` is in the repo's `.gitignore`. To commit an address file as run history:

```bash
git add -f scripts/strip_runner_addresses/strip_my_run.txt
git commit -m "chore(strip-runner): <run-label> address list"
```

The `-f` is intentional — bypasses the global `*.txt` ignore.

---

## 3. Filter matrix

Each address gets 22 Propelio calls (1 `add_cma` for CMA setup, 21 `search_cma` for data pulls):

| Band | Radii (mi) | Count |
|---|---|---|
| 24 month | 5, 2, 1, 0.5, 0.25 | 5 |
| 12 month | 5, 2, 1, 0.5, 0.25 | 5 |
| 6 month | 5, 2, 1, 0.5, 0.25 | 5 |
| 3 month | 1, 0.5, 0.25 | 3 |
| 1 month | 1, 0.5, 0.25 | 3 |
| **Total** | | **21 pulls + 1 CMA setup** |

The 21 filter combos are hardcoded in `FILTERS` near the top of `scripts/strip_runner.py`. To trim (e.g., drop the 5mi/2mi wide-rings for county-border addresses where most comps spill into neighboring counties), edit the constant directly.

**Why 21:** matches KK's manual workflow — same filter cascade he'd run by hand for one address.

---

## 4. Pacing and runtime

Per address:

- `~3-5s` setup pause (between `add_cma` and the first `search_cma`)
- 21 pulls × `~27s` average inter-pull pause (15-30s 80%, 30-60s 20%)
- `~3-5s` Propelio response per call

**Per-address wall time: ~10-12 minutes.**
**Per-address inter-pause: `~15-45s` after each address completes (except the first).**

| Strip size | Estimated runtime |
|---|---|
| 3 addresses (pilot) | ~30-40 min |
| 10 addresses | ~1.5-2 hours |
| 25 addresses | ~4-5 hours |

These are run-1 conservative defaults. After we observe a few clean runs without rate-limit symptoms, the band-A floor can drop from 15s back to 10s (~50 min off a 25-address run). See spec §14 "Pacing relaxation."

---

## 5. Monitoring during a run

The runner prints one line per call. Format:

```
[12:05:14] address 4/25: 1234 Main St, Dallas TX 75201
[12:05:18]   setup: add_cma ok   cma_id=1756524   (3.8s)
[12:05:22]   pass  1/21   24mo / 5.0mi   returned 287   new 287   addr_total 287
[12:05:48]   pass  2/21   24mo / 2.0mi   returned 142   new  18   addr_total 305
...
[12:24:32]   address done: 21/21 filters ok, 487 net-new comps to cache
```

Columns:

- `returned` — comps Propelio returned for that pull
- `new` — comps that were net-new to `propelio_comps` cache (i.e., `merge_comps_into_global`'s `inserted` count)
- `addr_total` — running sum of `new` for this address, resets per address

**Watching live from another terminal:** if you `tee`'d to a log file:

```bash
tail -f /tmp/strip_runner_logs/strip_my_run.log
```

`Ctrl-C` on `tail -f` detaches — the runner keeps going. To kill the runner itself: `pkill -f strip_runner.py`.

---

## 6. Stopping safely

`Ctrl-C` (SIGINT) in the runner's terminal interrupts on the next sleep boundary — comp writes already committed stay committed (idempotent on `comp_address_key`).

There is no "resume from where I stopped" — the runner is stateless. To resume after a partial run:

1. Edit the address file to remove already-completed addresses (look at the log to see which finished)
2. Re-launch with the trimmed file

Already-cached comps will produce `new 0` on re-pull, so re-running the full file is also safe (just wastes Propelio calls).

---

## 7. Interpreting the end-of-run summary

```
=== strip_runner summary ===
addresses_total:        25
addresses_complete:     22  (all 21 filters fired)
addresses_partial:       2  (some filters errored)
addresses_skipped:       1  (setup failed — lead lookup, cma setup, or burst-error guard)
filter_pulls_total:    504
propelio_returned_sum: 18420  (raw comps returned across all pulls, duplicates included)
comps_net_new_total:  1247  (rows inserted into propelio_comps cache)
elapsed_min:           178
```

**Three "skipped" reasons** (printed under `addresses_skipped:` list):

- `lead lookup failed` — Propelio couldn't resolve the address. Either typo or address not in their index. Manually verify and re-run.
- `cma setup failed` — `add_cma` raised (server error, timeout, etc.). Retry the address by itself.
- `3 consecutive filter errors` — burst-error guard tripped. Usually transient. Retry the address.

**Two "partial" reasons** (printed under `addresses_partial:` list):

- A handful of `search_cma` calls erred but the burst guard didn't trip (recovered on a later pull).

**Net-new vs Propelio returned ratio:** `comps_net_new_total / propelio_returned_sum` is your cache-fill efficiency. Low values (< 5%) mean the area is already well-cached; high values mean fresh ground. South Dallas County border runs often have low ratios because the 5mi+ pulls spill into neighboring counties where past manual pulls hadn't reached.

---

## 8. Common situations and what to expect

### "I see comps appearing outside Dallas County coverage"

Expected. The 5mi and 2mi pulls reach into neighboring counties (Kaufman, Ellis, etc.) where lot-ledger doesn't render CAD parcels. Those comps still land in `propelio_comps` with NULL `parcel_account_num` — useful for future cross-county work, even if they don't show on the current map view.

### "I see 'new' counts of 0 for many pulls in a row"

Expected when the area is already well-cached. Either you (or another operator) already manually pulled this address, OR an earlier address in this run already pulled the same comps at a wider radius. The runner doesn't early-exit on saturation (deferred — see spec §14).

### "The CLI hangs for the first ~10 minutes with no output"

You forgot `-u`. Python buffers stdout when piped. Kill (`Ctrl-C`), restart with `.venv/bin/python3 -u ...`.

### "The runner exited with code 2 and a CRITICAL banner"

Propelio rate-limited or auth-blocked. The summary should have printed before the exit (try/finally pattern). Wait 30+ minutes, then re-run with the same address file — already-done addresses produce 0 net-new and the runner will pick up where it left off effectively.

### "Smoke harness fails after I edit strip_runner.py"

Run: `.venv/bin/python3 scripts/strip_runner_smoke.py` from the repo root. All 32 selftests should pass. If they don't, the failing test will print which assertion failed.

---

## 9. Verifying writes landed

Quick SQL check (run from a Python shell or `psql` on the session DB):

```sql
-- How many strip_runner rows landed
SELECT COUNT(*) FROM propelio_comps WHERE first_seen_source = 'strip_runner';

-- Recent runs
SELECT first_seen_at::date AS run_date, COUNT(*)
FROM propelio_comps
WHERE first_seen_source = 'strip_runner'
GROUP BY 1 ORDER BY 1 DESC;

-- Spot check around a specific pilot address (replace lat/lng)
SELECT first_seen_source, status, COUNT(*)
FROM propelio_comps
WHERE ST_DWithin(
    geom::geography,
    ST_SetSRID(ST_MakePoint(-96.4945, 32.5924), 4326)::geography,
    3218.7  -- 2 miles
)
GROUP BY first_seen_source, status
ORDER BY 1, 2;
```

The app's read query is in `api/propelio/archive.py` (search for `FROM propelio_comps pc`). It does spatial + workspace filtering. If writes are visible in the SQL but missing in the app, hard-refresh the browser (the frontend caches the in-memory comp list per workspace).

---

## 10. File layout

```
scripts/
├── strip_runner.py                      Runner script (~700 LOC)
├── strip_runner_smoke.py                32 inline assertion-based selftests
└── strip_runner_addresses/
    ├── .gitkeep
    ├── pilot_2026_05_14.txt             First pilot run (3 addresses)
    └── <future strips...>

docs/propelio/
├── STRIP_RUNNER_SPEC.md                 Design rationale (v1.3, build-eligible per Copilot R1-R3)
├── STRIP_RUNNER_PLAN.md                 Implementation plan (v1.1, executed)
└── STRIP_RUNNER_RUNBOOK.md              This file
```

---

## 11. Future tuning levers (deferred from v1)

See spec §14 for the full deferred list. Operator-relevant items:

- **Faster pacing for repeat runs:** drop band A floor 15s → 10s. Edit `INTER_PULL_BAND_A_MIN` in `strip_runner.py`.
- **Weekly refresh mode:** rerun the same address list with `[(1, 1.0), (1, 0.5), (1, 0.25)]` only. Trim `FILTERS` to those three entries; loop through your existing strip files.
- **Saturation early-exit:** skip remaining filters when net-new rate drops. Not yet implemented; would save Propelio calls on already-warm areas.
- **Re-login on token expiry:** for runs longer than the Propelio session TTL. Not yet implemented; current behavior is exit-code-2 + you re-run.
