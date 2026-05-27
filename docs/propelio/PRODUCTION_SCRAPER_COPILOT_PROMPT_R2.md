# Copilot Critique Prompt — Production Scraper Spec v1.1 (Round 2)

**Use this prompt verbatim in Copilot chat.** It is self-contained.

---

## Prompt to paste into Copilot

This is **round 2** of the deep-critique loop on the production scraper spec. Round 1 surfaced 3 substantive findings (DB-write durability, auth re-login in v1, tighter SIGTERM) plus 12 open-item resolutions. All of those have been folded into v1.1. Your round-2 job is narrower but more code-grounded: **verify the round-1 fixes are correct against the actual code** they touch, weigh in on the 6 remaining open items, and flag any new issues that v1.1's added complexity introduced.

KK is unchanged on the bar: "I do not want any issues with this thing." We are still pre-implementation. This is the last critique gate before we lock the spec and hand it to Copilot for the build.

### What to read

1. **The updated spec:**
   `docs/propelio/PRODUCTION_SCRAPER_SPEC.md` (now v1.1)
   - Pay specific attention to the **Changes v1.0 → v1.1** section at the top — that's the full delta from round-1.
   - §3.1, §6.1, §6.4, §7.1, §7.2, §7.5, §7.6, §8, §11, §12, §13 all have material changes.

2. **The round-1 prompt** for context on what's already been asked:
   `docs/propelio/PRODUCTION_SCRAPER_COPILOT_PROMPT.md`

3. **Code that round-1 fixes assume things about** — verify the assumptions:
   - `api/propelio/scraper.py` — for §7.5 re-login (the `_logged_in` cache, `login()` semantics, request timeouts) and §7.6 (Propelio HTTP timeouts).
   - `api/propelio/archive.py` — for §3.1 (comp_address_key derivation centralization) and §7.2 (merge_comps_into_global error surface).
   - `api/config.py` — for §7.2 (connection-pool checkout/release; how to force-close + reacquire) and §7.6 (where to inject `statement_timeout`).

### Round-2 questions (verification + open items + new issues)

#### A. Verify round-1 fixes against the actual code

For each item below, **read the named code file**, then state whether the spec's assumption holds. If it doesn't, propose the minimum-blast-radius change.

1. **§7.2 — DB retry assumes "close connection, reacquire, retry" works.** Read `api/config.py`. Does `release_session_conn` support `force_close=True` (or equivalent)? Does the pool re-establish a fresh socket on the next `get_session_conn()` call after a force-close? If the pool is sticky in some way, the retry would just re-use the broken connection. Confirm or propose a fix.

2. **§7.5 — Re-login assumes `PropelioClient.login()` can be invoked while `_logged_in == True`.** Read `api/propelio/scraper.py`. Does `login()` early-return when already logged in? If yes, what's the canonical way to force a re-login? Is there a `force=True` parameter, or do we need to add one? If we need to add one, that's a small change to the client — flag it and propose the diff.

3. **§7.6 — Bounded Propelio HTTP timeouts assume `requests.Session.get/post` calls accept a `timeout`.** Read `api/propelio/scraper.py`. Do the existing call sites already pass `timeout=`? If not, where's the safest single place to inject the default (Session wrapper? Per-call?) without breaking strip_runner or deep_pull which share the client?

4. **§7.6 — Postgres `statement_timeout` injection.** Read `api/config.py`. Is there a pool-acquire hook where we can `SET statement_timeout = '60s'` per checkout? Open item §13.5 already flags the (a) pool-wide vs (b) scraper-only choice — give your verdict here, grounded in what the pool actually exposes.

5. **§3.1 — `comp_address_key` centralization assumption.** Read `api/propelio/archive.py`. Confirm the key derivation lives in exactly one place and is reused by both strip_runner and any other caller. If it's been copy-pasted anywhere, flag it as a pre-existing risk we should fix as part of this work (or at least document).

#### B. Weigh in on the 6 remaining open items in §13

The 6 items remaining in §13 are either KK product calls or round-2 targets. Give a 1-2 sentence recommendation on each, marking it as **KK-call** (defer to KK) or **technical-call** (you have an opinion):

1. **Inter-pull pacing band** (8-20s vs 15-45s for 5-pull addresses)
2. **Master list location** (committed `master_list.txt` vs `.gitignore`'d + `master_list.example.txt`)
3. **`--profile` required vs default `monthly_1m`**
4. **`PropelioClient.login(force=True)`** — covered by A.2 above; carry the verdict here
5. **`statement_timeout` location** — covered by A.4 above; carry the verdict here
6. **cron vs systemd-timer for Phase 2** — what would you recommend for client-ship?

#### C. New issues introduced by v1.1

The v1.1 changes added complexity. Surface anything new you spot. Specifically watch for:

1. **Re-login + retry interactions with the burst guard.** §7.5 says re-login + retry happens "once per address per step." If `search_cma` pass 2 hits 401 → re-login + retry succeeds → pass 3 hits 401 again (session expired again) → re-login + retry. Does this confuse `consecutive_errors`? Should `consecutive_errors` be reset on a successful retry?

2. **DB retry + parcel_match warn-and-fallback.** §7.2 says `parcel_match` failures fall back to unmatched merge (mirroring deep_pull). What if the unmatched merge also fails? Is that retried, escalated, or silently dropped?

3. **Atomic state save during the soft-stop window.** §7.6 says SIGTERM saves state after the current pull. What if SIGTERM arrives DURING the `os.replace(state.json.tmp, state.json)` operation? POSIX says rename is atomic — but is the surrounding "open → write → fsync → rename" sequence interrupt-safe? Specifically, if SIGTERM arrives between fsync and rename, do we lose the address completion?

4. **`statement_timeout = 60s` collision with the DB-write retry.** A `statement_timeout` exceedance is itself an exception inside `merge_comps_into_global`. The retry path closes the connection and retries. But if the underlying issue is a slow query (not a transient hiccup), the retry will also time out at 60s. Is that the intended fail path? Should we use a different timeout for the retry attempt?

5. **History unbounded growth in a degenerate case.** §6.4 says ~150 bytes per pass. What if a cron misconfiguration causes 86,400 passes per day (e.g., cron fires every second by mistake)? The state file grows ~12 MB/day. Should we add a soft warning if history > N entries?

6. **Repo-root sanity check against `__file__.parents[2]`.** §7.1 assumes the script lives at `scripts/production_scraper/run.py`. What if someone symlinks or moves the script? Is the path assertion robust enough, or should we check for `api/propelio/` regardless of how we got there?

### Deliverable format

Same shape as round-1:

1. **CRITICAL findings** (data corruption / silent loss class)
2. **IMPORTANT findings** (reliability / operability)
3. **Nice-to-have findings** (polish)

Plus:

4. **Verdicts on Section A (5 items):** per-item, "spec is correct / spec needs change because X"
5. **Verdicts on Section B (6 items):** per-item, KK-call vs technical-call recommendation
6. **Verdicts on Section C (6 items):** per-item, "spec covers this / spec needs X added"
7. **Bottom line:** would you ship v1.1 (with any minor adjustments you proposed) as the locked spec? Or does a v1.2 round-3 critique need to happen?

### Out of scope

- Anything in §2 (Non-goals).
- Performance / speed optimization.
- Re-litigating items already resolved in v1.1 unless you find a NEW reason they should be reopened.

### Style

- Be specific — file + line + concrete failure scenario, not abstract concerns.
- If you cite external sources (GitHub, Postgres docs, requests docs), link the URL.
- Don't paraphrase the spec back. Push back where you disagree.

---

## After Copilot responds

Claude will fold any new CRITICAL or IMPORTANT findings into v1.2 (or decline with rationale if they're nice-to-have / out-of-scope). When the spec is locked, Claude writes the **implementation prompt** for Copilot to actually build the tool.
