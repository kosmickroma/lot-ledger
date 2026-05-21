---
title: save_verification fast-path — kill the full cached_jobs.rows hydration
status: v2 — Copilot round-1 critique folded in, awaiting KK greenlight to code
date: 2026-05-21
branch: feat/save-verification-fast-path-2026-05-21 (off develop, after Phase 1+2 merges)
deployment: PREVIEW ONLY for the arc; gated promote
trigger: "Saving tags…" step on CSV download is significantly slower after Phase 1+2 residential detail expansion (cached_jobs.rows blob ~30% bigger). Copilot + KK confirmed root cause is backend hydration cost in save_verification, not frontend or extra DB roundtrips.
revisions:
  v1 (initial): 2026-05-21
  v2 (this): 2026-05-21 — Copilot round-1 critique folded in
---

## v2 locked decisions (from Copilot critique)

1. **Split metadata from pair expansion** — duplicating polygon + parcel_count across 11k+ result rows partially defeats the bandwidth savings. Use TWO queries: one cheap point-lookup for (polygon, parcel_count); one LATERAL scan for the (account_num, division_cd) pairs.
2. **Unify county derivation** — single `_county_from_division(division_cd: str | None) -> str` helper. Both `_row_county` (dict input) and the new code path delegate to it. Eliminates drift; no parity unit test needed.
3. **Generalize the helper name** — `_load_cached_account_county_pairs(job_id, user_id)` (reusable for any future callers needing the same pattern), plus a separate `_load_cached_job_metadata(job_id, user_id)` for polygon + count.
4. **Performance claim language** — downgrade from "5-10x" promise to "likely multi-x improvement, especially on cold large jobs." Test will measure actual impact.
5. **Test plan** — add a coarse benchmark fixture for local measurement. Not a CI assertion (no perf gate), just durable evidence the fix works.
6. **In-scope addition** — verify nothing in save_verification's response-shaping path AFTER the tag write hydrates the full row blob.
7. **Out of scope** — broader cached_jobs schema refactor (e.g., split into cached_jobs_meta + cached_jobs_rows tables). Stays a follow-up.

## Problem

`POST /api/job/{job_id}/verification` (api/main.py:3174) does:

1. `_get_job(job_id, user_id)` — hydrates the FULL `cached_jobs.rows` JSONB blob into Python (~5 MB and ~550k dict entries for an 11k-parcel job after the residential-detail expansion).
2. Iterates every row in Python: `for row in rows: row.get("account_num"); _row_county(row)`.
3. Uses only TWO fields per row (`account_num` + `division_cd`). The other ~50 keys per parcel are pulled across the wire and parsed for nothing.

The hot path was acceptable before — pre-Phase-1 rows were ~30% smaller, the in-memory `_job_store` cache often hit (no DB load), and parcel counts were smaller. Today's bigger rows + cold-instance hits on Cloud Run + 11k-parcel polygons reveal the cost.

**Bottleneck breakdown for a cold-instance 11k-parcel save_verification:**
- Postgres returns ~5 MB JSONB → ~50-100ms
- psycopg2 + json parse into Python dicts → ~500-1500ms
- Python iteration over rows → ~50-100ms
- session_tags batch UPSERT/DELETE → ~100-300ms
- `_persist_session_sync` (analysis_sessions UPSERT) → ~50ms

The Python parse step alone scales linearly with the row blob size — the change Phase 1+2 introduced.

## Solution: thin JSONB-projection query

Replace `_get_job` + Python-side row iteration with a server-side JSONB projection that pulls ONLY `(account_num, division_cd)` per parcel + the `polygon` field once.

### New helpers (v2: split + unified)

```python
def _county_from_division(division_cd: str | None) -> str:
    """Single source of truth for division_cd → county lookup.
    Used by both _row_county (dict input) and the new fast-path code.
    Eliminates the drift hazard Copilot flagged in round-1."""
    division = str(division_cd or "").strip().upper()
    if division == "TAD":
        return "tad"
    if division == "COLLIN":
        return "collin"
    if division == "DENTON":
        return "denton"
    return "dcad"


def _row_county(row: dict[str, Any]) -> str:
    """Existing function refactored to delegate to _county_from_division.
    Backwards-compatible — same return values, same input contract."""
    return _county_from_division(row.get("division_cd"))


def _load_cached_job_metadata(job_id: str, user_id: int) -> tuple[list, int] | None:
    """Cheap point-lookup of (polygon, parcel_count) for a cached job.

    Returns None if job not found or user-id mismatch.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT polygon, jsonb_array_length(rows) AS parcel_count
                FROM cached_jobs
                WHERE job_id = %s AND user_id = %s
                """,
                (job_id, int(user_id)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return (row[0] or []), int(row[1] or 0)
    finally:
        release_session_conn(conn)


def _load_cached_account_county_pairs(job_id: str, user_id: int) -> list[tuple[str, str]]:
    """Stream (account_num, county) tuples for every parcel in a cached job.

    Uses LATERAL jsonb_array_elements server-side so the only data crossing
    the wire is the two narrow fields, not the full ~50-key row dict.
    Generic naming: this is reusable for any future endpoint that needs
    account/county pairs from cached job rows without the full payload.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    el->>'account_num'                              AS account_num,
                    COALESCE(el->>'division_cd', '')                AS division_cd
                FROM cached_jobs c,
                LATERAL jsonb_array_elements(c.rows) AS el
                WHERE c.job_id = %s AND c.user_id = %s
                """,
                (job_id, int(user_id)),
            )
            pairs: list[tuple[str, str]] = []
            for account_num_raw, division_cd_raw in cur:
                account_num = str(account_num_raw or "").strip()
                if not account_num:
                    continue
                pairs.append((account_num, _county_from_division(division_cd_raw)))
            return pairs
    finally:
        release_session_conn(conn)
```

Two queries instead of one combined. Polygon + count come back as a single tiny tuple (~few KB). Pairs come back as N narrow tuples (~30 bytes each). Combined wire size dramatically smaller than the original 5 MB blob + duplicated metadata across N rows.

### Refactored save_verification (v2: two-query)

```python
@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest, req: Request,
                             user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)

    user_id = int(user["id"])

    # v2 (Copilot critique): two queries instead of one. The cheap metadata
    # lookup runs first + acts as the existence/ownership check (404 if not
    # found). The pair-streaming query runs second only if metadata exists.
    metadata = _load_cached_job_metadata(job_id, user_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="Job not found")
    polygon, parcel_count = metadata

    account_county_pairs = _load_cached_account_county_pairs(job_id, user_id)

    verifications = request.verifications or {}
    potential_targets = request.potential_targets or {}

    def _normalize_verification(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw == "yes":
            return "Yes"
        if raw == "no":
            return "No"
        return ""

    def _normalize_target(value: Any) -> str:
        raw = str(value or "").strip().lower()
        return "Yes" if raw in {"1", "true", "yes", "y"} else ""

    try:
        # Ensure parent session row exists before child tags
        _persist_session_sync(
            job_id,
            polygon,
            parcel_count,
            _counties_from_pairs(account_county_pairs),
            int(user["id"]),
            None,
        )

        upsert_rows: dict[tuple[str, str, str], tuple[str, str, str, str, str]] = {}
        delete_rows: set[tuple[str, str, str]] = set()

        # Same logic as before, just iterate the thin tuples.
        for account_num, county in account_county_pairs:
            if not account_num:
                continue
            verification_value = _normalize_verification(verifications.get(account_num, ""))
            key_ver = (account_num, county, "verification")
            if verification_value:
                upsert_rows[key_ver] = (job_id, account_num, county, "verification", verification_value)
            else:
                delete_rows.add(key_ver)

            target_value = _normalize_target(potential_targets.get(account_num, ""))
            key_target = (account_num, county, "target")
            if target_value:
                upsert_rows[key_target] = (job_id, account_num, county, "target", target_value)
            else:
                delete_rows.add(key_target)

        # ... rest unchanged: session_tags UPSERT + DELETE via execute_values + executemany
```

### Helper: `_counties_from_pairs`

```python
def _counties_from_pairs(pairs: list[tuple[str, str]]) -> list[str]:
    """Replacement for _counties_from_rows that operates on (account, county) pairs."""
    return sorted(set(county for _, county in pairs if county))
```

## What does NOT change

- `_get_job` itself stays — used by other endpoints that genuinely need the full row data (CSV download, /api/job/{id}/results, the parcel detail panel).
- `_row_county` stays for those other paths.
- `cached_jobs.rows` schema unchanged.
- The session_tags upsert + delete logic — unchanged.
- Frontend `persistTagStateForExport` — unchanged. Same API contract, same JSON request shape.

## Performance estimate (v2: downgraded language per Copilot)

Likely multi-x improvement, especially on cold large jobs. Exact numbers TBD via local benchmark fixture. The biggest win is avoiding the psycopg2 + Python JSON decode of the full ~5 MB row blob; if that parse step dominates today, the speedup will be substantial. If wire size and DB-side JSON expansion contribute more than expected, real-world impact lands closer to 3-6x. We'll measure rather than promise.

Worst case unchanged: warm `_job_store` cache hits today are fast, but `_job_store` is per-Cloud-Run-instance and gets cold easily on autoscaling. The fix mostly helps cold-instance hits and very large jobs — exactly where the current pain is.

## Test plan (v2: + benchmark fixture)

- **Unit tests:**
  - `_county_from_division` with all known division_cd inputs ("TAD", "COLLIN", "DENTON", "RES", "COM", "", None) → assert correct mapping.
  - `_load_cached_job_metadata` with a fixture cached_jobs row → confirms polygon + count extraction; with user-id mismatch → returns None.
  - `_load_cached_account_county_pairs` with a fixture cached_jobs row containing mixed DCAD/TAD/Collin/Denton parcels → assert correct (account_num, county) extraction. Includes a row with no `account_num` → skipped silently.
- **Regression test:** simulate a save_verification call with both implementations (old _get_job + iter path AND new fast-path) → assert identical session_tags writes (upsert + delete content match).
- **Benchmark fixture** (v2 addition — Copilot pushback): `tests/bench/test_save_verification_perf.py` — coarse measurement using `time.perf_counter`. Run against an 11k-parcel local fixture. Output before/after numbers. **Not asserted in CI** (no perf gate), but durable evidence the fix worked. Run manually when reviewing the change.
- **Edge cases:**
  - Cached job exists but `rows` is empty array → expect `parcel_count = 0`, `pairs = []`. No tags written.
  - User-id mismatch → 404 (same as before).
  - Job expired and evicted from `_job_store` but still in `cached_jobs` → query still works (we don't depend on `_job_store`).
- **In-scope audit (v2):** confirm nothing in save_verification's response-shaping path AFTER the tag write hydrates the full row blob. The response is currently `{"ok": True, "saved_at": ...}` — no row data. ✅ Already narrow end-to-end. Spec asserts this in code review.

## Risks (v2: resolved)

- ~~**Drift risk:** `_row_county_from_div` must stay in sync with `_row_county`.~~ **RESOLVED via v2 decision #2** — single `_county_from_division` helper, no parallel implementations.
- **JSONB query plan:** `LATERAL jsonb_array_elements()` on a 5MB blob isn't free server-side. Should be much faster than pulling the blob to the client + parsing, but worth measuring via benchmark fixture. If PG-side expansion is the bottleneck instead of client decode, may need to revisit. (Copilot called this low-risk because the job_id point-filter keeps cardinality bounded to one parent row.)
- **Connection pool:** two queries now where there used to be one. Each correctly returns its conn via `release_session_conn`. Low risk per Copilot review.
- **Redundant call audit:** confirm no code path now does `_load_cached_*` + `_get_job` redundantly for the same job in a single request. The save_verification path drops `_get_job` entirely — verified.

## Out of scope

- Migrating `_get_job` callers to the thin path (CSV download still needs the full blob — different problem).
- A persistent in-memory cache that scales across Cloud Run instances (would help but adds complexity).
- Frontend changes (api contract unchanged).

## Copilot round-1 questions — all resolved in v2

1. ✅ LATERAL is right. Don't use jsonb_path_query_array (rebuilds JSON).
2. ✅ Existing PK on cached_jobs.job_id is sufficient.
3. ✅ Per-row tuple result is still faster than full blob + client decode.
4. ✅ No caching needed — one-shot per request.
5. ✅ Resolved via unified `_county_from_division` helper (v2 decision #2).
6. ✅ Generalized helper name `_load_cached_account_county_pairs` (v2 decision #3) leaves room for future reuse.

## Followups

- Apply the same thin-extraction pattern to other endpoints that walk cached_jobs.rows for narrow purposes.
- Long term: split cached_jobs into `cached_jobs_meta` (small) + `cached_jobs_rows` (large) so save_verification doesn't even touch the rows table. Bigger refactor — not blocking this fix.
