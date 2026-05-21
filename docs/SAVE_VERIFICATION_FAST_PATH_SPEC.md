---
title: save_verification fast-path — kill the full cached_jobs.rows hydration
status: DRAFT for KK greenlight + Copilot critique
date: 2026-05-21
branch: feat/save-verification-fast-path-2026-05-21 (off develop, after Phase 1+2 merges)
deployment: PREVIEW ONLY for the arc; gated promote
trigger: "Saving tags…" step on CSV download is significantly slower after Phase 1+2 residential detail expansion (cached_jobs.rows blob ~30% bigger). Copilot + KK confirmed root cause is backend hydration cost in save_verification, not frontend or extra DB roundtrips.
---

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

### New helper

```python
def _load_verification_inputs(job_id: str, user_id: int) -> tuple[list, int, list[tuple[str, str]]] | None:
    """Thin replacement for _get_job in save_verification.
    Returns (polygon, parcel_count, account_county_pairs) or None.

    Performance: bypasses the full cached_jobs.rows hydration. For an 11k-parcel
    job, this drops the call from ~1-2s to ~200ms on a cold instance.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.polygon,
                    jsonb_array_length(c.rows) AS parcel_count,
                    el->>'account_num' AS account_num,
                    COALESCE(el->>'division_cd', '') AS division_cd
                FROM cached_jobs c,
                LATERAL jsonb_array_elements(c.rows) AS el
                WHERE c.job_id = %s AND c.user_id = %s
                """,
                (job_id, int(user_id)),
            )
            results = cur.fetchall()
            if not results:
                return None
            polygon = results[0][0] or []
            parcel_count = results[0][1] or 0
            pairs = [(str(r[2] or "").strip(), _row_county_from_div(r[3]))
                     for r in results
                     if r[2]]  # skip rows without account_num
            return polygon, parcel_count, pairs
    finally:
        release_session_conn(conn)


def _row_county_from_div(division_cd_value: str | None) -> str:
    """Inline replicate _row_county's logic but for a raw division_cd value
    (not a row dict). Kept in sync with _row_county."""
    division = str(division_cd_value or "").upper()
    if division == "TAD":
        return "tad"
    if division == "COLLIN":
        return "collin"
    if division == "DENTON":
        return "denton"
    return "dcad"
```

### Refactored save_verification

```python
@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest, req: Request,
                             user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)

    # Replace: _get_job(job_id, user_id) + ['rows'] enumeration
    # With: thin JSONB-projection query returning only what we need.
    inputs = _load_verification_inputs(job_id, int(user["id"]))
    if inputs is None:
        raise HTTPException(status_code=404, detail="Job not found")
    polygon, parcel_count, account_county_pairs = inputs

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

## Performance estimate

For an 11k-parcel job on a cold Cloud Run instance:
- **Before:** ~1.5-2.5 seconds for save_verification (mostly JSON parse cost)
- **After:** ~200-400 ms (single thin SQL query + small Python iteration)
- **Speedup:** ~5-10x

For a typical 100-500-parcel job:
- **Before:** ~200-400 ms
- **After:** ~100-200 ms
- **Speedup:** ~2x

Worst case unchanged: if `_get_job` was already warm in `_job_store`, the Python-side iteration was fast. The fix mostly helps cold-instance hits and very large jobs.

## Test plan

- Unit test: `_load_verification_inputs` with a fixture cached_jobs row containing mixed DCAD/TAD/Collin/Denton parcels. Assert correct (account_num, county) extraction.
- Integration: time `POST /api/job/{job_id}/verification` before + after on a 5k-parcel polygon. Expect 3-5x improvement.
- Regression: confirm session_tags writes are identical pre/post (same upsert/delete count, same content).
- Edge cases:
  - Cached job exists but `rows` is empty array → expect `parcel_count = 0`, `pairs = []`. No tags written.
  - User-id mismatch → 404 (same as before).
  - Job expired and evicted from _job_store but still in cached_jobs → query still works (we don't depend on _job_store).

## Risks

- **Drift risk:** `_row_county_from_div` must stay in sync with `_row_county`. Add a unit test that exercises both with the same `division_cd` inputs and confirms identical outputs.
- **JSONB query plan:** `LATERAL jsonb_array_elements()` on a 5MB blob isn't free server-side. Should be much faster than pulling the blob to the client + parsing, but worth measuring. If PG is the bottleneck instead of the client, we may need an index or alternate approach.
- **Connection pool**: extra DB call from save_verification → keep using `get_session_conn` + `release_session_conn` for safety.

## Out of scope

- Migrating `_get_job` callers to the thin path (CSV download still needs the full blob — different problem).
- A persistent in-memory cache that scales across Cloud Run instances (would help but adds complexity).
- Frontend changes (api contract unchanged).

## Questions for Copilot critique

1. Is `LATERAL jsonb_array_elements(c.rows)` the right pattern, or is there a faster JSONB extraction (e.g., `jsonb_path_query_array` with explicit projection)?
2. Should we add an index on `cached_jobs(job_id, user_id)` to ensure the lookup is point-fast even before JSONB extraction? (Probably already indexed via PRIMARY KEY on job_id.)
3. Risk of returning a 1-row tuple-per-parcel result set (potentially 11k+ rows) over the wire — is that faster than the JSONB blob even with the LATERAL overhead?
4. Should `_load_verification_inputs` cache its result for repeated calls within the same session? (Probably no — each save_verification is one-shot.)
5. Drift hazard between `_row_county_from_div` (string input) and `_row_county` (dict input). Worth refactoring to share via a helper that takes the raw division string?
6. Are there other endpoints with the same pattern of "hydrate full cached_jobs.rows just to read account_num + county"? Worth pre-emptively factoring out?

## Followups

- Apply the same thin-extraction pattern to other endpoints that walk cached_jobs.rows for narrow purposes.
- Long term: split cached_jobs into `cached_jobs_meta` (small) + `cached_jobs_rows` (large) so save_verification doesn't even touch the rows table. Bigger refactor — not blocking this fix.
