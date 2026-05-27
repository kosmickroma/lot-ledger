# Copilot Critique Prompt — Production Scraper Spec v1.0

**Use this prompt verbatim in Copilot chat.** It is self-contained — Copilot does not see the conversation that produced the spec.

---

## Prompt to paste into Copilot

You are doing a **deep critique** of a design spec for a new long-running, resumable, cron-friendly Propelio comp scraper. The goal of this critique round is to surface every reliability concern, race condition, edge case, ambiguity, or design weakness **before any code is written**. KK has been explicit: **"I do not want any issues with this thing."** Treat that as the bar.

### What to read first

1. **The spec** — read this in full, multiple times:
   `docs/propelio/PRODUCTION_SCRAPER_SPEC.md` (v1.0)

2. **Prior-art reference** — the spec is modeled after, and reuses libraries from, the existing Strip Runner. Read these to understand patterns being inherited and what you'd be diverging from:
   - `docs/propelio/STRIP_RUNNER_SPEC.md` (note: lives on the `feat/strip-runner` branch — if not visible from your current branch, fetch / switch / cherry-pick the file for reference, or read it from the worktree at `/home/kk/projects/clients/lot-ledger-strip/docs/propelio/STRIP_RUNNER_SPEC.md`)
   - `docs/propelio/STRIP_RUNNER_RUNBOOK.md` (same location notes apply)
   - `scripts/strip_runner.py` (same)
   - `scripts/strip_runner_smoke.py` (same)
   - `api/propelio/archive.py` — `merge_comps_into_global` is what we'll be writing through
   - `api/propelio/scraper.py` — `PropelioClient`, `_parse_property`
   - `api/propelio/parcel_match.py` — `match_comps_to_parcels` and its failure modes
   - `api/propelio/deep_pull.py` — patterns the new spec borrows from

3. **The 15 open items** the spec author has explicitly flagged for your scrutiny — §13 of the spec. Each one is a design choice the author isn't sure about. Challenge each one specifically; don't paraphrase the spec back, propose alternatives.

### What KK wants

- A scraper that **never silently corrupts state**.
- A scraper that **survives** power loss, computer reboot, terminal close, network drop, Cloud SQL connection drop, Propelio auth block, SIGTERM from cron/systemd, and SIGKILL — and resumes correctly on next launch.
- A scraper that is **safe to fire from cron** alongside other workloads (no duplicate runs, no race with `strip_runner` writing to the same DB table).
- A scraper that is **shippable to the client** (Mike) — clean code, clean folder, clean README, no operator-implicit-knowledge assumptions.

### Where to look externally

You should **leave the codebase** and pull in external knowledge. Specifically:

- **GitHub repos** for well-known long-running Python scrapers / job-queue patterns. Look at how they handle:
  - Atomic state-file writes (vs SQLite WAL, vs separate journal file)
  - Process locking (cron-safe; cross-mount; NFS-aware if applicable)
  - SIGTERM graceful shutdown with bounded shutdown time
  - DB-connection longevity over multi-hour runs (Postgres `tcp_keepalives`, `keepidle`, `idle_in_transaction_session_timeout` interplay; SQLAlchemy / psycopg2 reconnect patterns)
  - Resume semantics when the input list mutates between runs
  - **Specifically search GitHub for:** `flock state.json scraper`, `cron python scraper resumable`, `psycopg2 reconnect long running`, `sqlite-utils job queue`, `huey state file`, `dramatiq cron`, `prefect resume`, `airflow checkpoint`. Pull out concrete patterns; cite the repo.

- **Python stdlib / well-documented patterns** for:
  - `fcntl.flock` portability and failure modes on macOS / Linux / NFS
  - `os.replace` vs `os.rename` atomicity guarantees on the same filesystem (and what breaks when state-dir and tmp-dir are different mounts)
  - `signal.signal` interaction with blocking I/O in `requests` (does a SIGTERM during an in-flight HTTP call surface immediately, or wait for the I/O to return?)
  - `logging` configuration that survives `os.execv` / daemonization / cron rewrites

- **Postgres / Cloud SQL** documentation on:
  - Connection-drop recovery patterns under long-running idle (Mike's GCP Cloud SQL instance has dropped strip_runner connections mid-merge before — see the `strip_tarrant_rows6_7_8_resume2` notes in the spec context)
  - Whether `merge_comps_into_global`'s ON CONFLICT pattern is concurrent-write safe when strip_runner and production_scraper are both writing the same row (different `first_seen_source`)

- **Cron / systemd-timer reliability folklore**: cron-vs-systemd-timer tradeoffs for the Phase 2 cadence; `flock(1)` wrapper as a belt-and-suspenders option around the Python lock.

### How to deliver findings

Structure your response in three sections:

1. **CRITICAL findings** — design flaws that, if shipped, will cause data corruption, silent data loss, duplicate writes, lock-up under cron, or unrecoverable state. Each finding gets:
   - **What the spec says** (quote with section number)
   - **Why it's broken** (concrete failure scenario)
   - **Recommended fix** (specific, implementable)
   - **External citation** if relevant (repo + file or doc URL)

2. **IMPORTANT findings** — non-critical reliability or operability gaps. Same format.

3. **Nice-to-have findings** — polish, log-quality, operator-ergonomics suggestions. Same format.

Plus, at the end:

4. **Verdict on the 15 open items in §13** — one line per item: "agree with spec / disagree, see Finding #N / partially agree, refinement: ..."

5. **One-paragraph "would you ship this?" summary** — if no, what's the minimum bar to ship.

### Out of scope for this critique

- Performance optimization (KK explicitly: "do not worry about speeding it up, it's ok slow and steady").
- Multi-tenancy / multi-user concerns.
- Web UI.
- Anything in §2 ("Non-goals") of the spec — don't suggest adding those.

### Style

- **Be specific.** "Consider concurrency" is useless. "If two processes race on `os.replace` between line X and line Y of the loop, state.json will lose the address-N completion entry — fix by holding the file lock through both the read and write" is useful.
- **Show your work.** When citing external sources, link to the repo + file + line if possible.
- **Push back hard.** KK has been burned by under-critiqued specs before. The strip_runner spec went through three Copilot critique rounds before locking; this one should aim to be that thorough on round 1.

---

## After Copilot responds

Claude will fold IMPORTANT and CRITICAL findings into a v1.1 of the spec, re-circulate for a round-2 critique if the changes are substantial, and only then hand the locked spec to Copilot for implementation.
