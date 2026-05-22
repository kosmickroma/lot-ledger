# Prior-Year Value Fallback — Phase 1 Implementation

**Branch:** `feat/prior-year-value-fallback-2026-05-20`

**Status:** Ready for preview deployment after KK review

---

## Summary

Phase 1 implements query-time fallback for Collin parcels where the current tax year value is null/0 but a last-certified value exists. Recovers ~6,400 Collin parcels currently rendering as "$0 / blank". Also excludes truly-empty pipeline parcels (no addr/owner/legal/value) from /api/analyze across all four counties (~12,000 rows removed from analysis).

**No DB schema changes.** Phase 1 reuses existing `cert_total_value` and `cert_val_year` columns already ingested in Collin parcel table.

---

## Changes

### Backend (api/)

1. **api/counties/collin.py** — `_normalize_collin_row()`
   - Added prior-year fallback logic: if `total_value` is null/0 AND `cert_total_value` > 0, use certified value
   - Tag with provenance: `total_value_source = "prior_year_cert_YYYY"` or `"prior_year_cert"` if year missing
   - Forward-compatible: other counties always get `total_value_source = None`

2. **api/counties/dcad.py** — `build_feature()`
   - Emit `total_value_source` flag in feature properties (empty string for non-Collin rows)
   - Enables frontend to append suffix "(prior year YYYY)" conditionally

3. **api/main.py** — CSV export (4 sites)
   - **Header:** New column "Value Source" at position 13 (after "Total Value")
   - **Parcel row:** Insert value source provenance cell at column 13
   - **Comp row:** Insert value source cell at column 13 (inherited from cached parcel data)
   - **Orphan row:** Insert blank cell at column 13 (orphan comps have no parcel context)
   - **COMPATIBILITY LOCK comments:** Updated to reflect +1 shift (Good Comp now at column 97, was 96)

4. **api/main.py** — Truly-empty filter
   - New function `_is_truly_empty_parcel(row)` — returns True if all of: no addr, no owner, no legal, no tot_val, no cert_val
   - Applied in `/api/analyze` after row merge, before feature building
   - Removes ~2,300 Collin + ~62 Denton + ~14,795 TAD + 0 DCAD pipeline parcels (total ~17k rows)

### Frontend (frontend/map.js)

5. **makePopupHtml()** — Total Value suffix
   - New helper variable `totValDisplay`: appends "(prior year YYYY)" when `properties.total_value_source` is non-empty
   - Applied in both popup table and sidebar detail panel
   - Styling: italic + opacity 0.8 to signal non-current data

---

## Pre-Deploy SQL Audit Queries

Run these on preview AFTER the PR merges to verify recovery numbers:

```sql
-- (a) How many Collin parcels will get the fallback applied?
SELECT COUNT(*) AS will_apply_fallback
FROM collin_parcels
WHERE (total_value IS NULL OR total_value = 0) AND cert_total_value > 0;
-- Expected: ~6,400

-- (b) How many parcels will the truly-empty filter remove (per county)?
SELECT 'collin' AS county, COUNT(*) FROM collin_parcels
WHERE (property_address IS NULL OR property_address = '')
  AND (owner_name IS NULL OR owner_name = '')
  AND (legal_descr IS NULL OR legal_descr = '')
  AND (total_value IS NULL OR total_value = 0)
  AND (cert_total_value IS NULL OR cert_total_value = 0)
UNION ALL
SELECT 'denton', COUNT(*) FROM denton_parcels
WHERE (property_address IS NULL OR property_address = '')
  AND (owner_name IS NULL OR owner_name = '')
  AND (legal1 IS NULL OR legal1 = '')
  AND (tot_val IS NULL OR tot_val = 0)
UNION ALL
SELECT 'tad', COUNT(*) FROM tad_parcels
WHERE (property_address IS NULL OR property_address = '')
  AND (owner_name IS NULL OR owner_name = '')
  AND (legal1 IS NULL OR legal1 = '')
  AND (tot_val IS NULL OR tot_val = 0)
UNION ALL
SELECT 'dcad', COUNT(*) FROM parcels
WHERE (property_address IS NULL OR property_address = '')
  AND (owner_name IS NULL OR owner_name = '')
  AND (legal1 IS NULL OR legal1 = '')
  AND (tot_val IS NULL OR tot_val = 0)
ORDER BY county;
-- Expected: Collin ~2,310, Denton ~62, TAD ~14,795, DCAD ~0

-- (c) Subdivision sensitivity smoke test (optional, for future improvement tracking):
-- Are there multi-account clusters sharing the same cert geometry?
SELECT cert_total_value, COUNT(DISTINCT parcel_key) AS account_count,
       ST_AsText(ST_Centroid(ST_Union(geom))) AS rough_center
FROM collin_parcels
WHERE (total_value IS NULL OR total_value = 0) AND cert_total_value > 0
GROUP BY cert_total_value
HAVING COUNT(DISTINCT parcel_key) > 1
ORDER BY account_count DESC
LIMIT 20;
-- If returns rows: multi-account clusters found. Document as known limitation;
-- consider per-account splitting in Phase 2.
```

---

## Testing

### Unit Tests (New Files)

- **tests/test_collin_prior_year_fallback.py** — 8 cases covering fallback firing conditions, year attribution, edge cases (None, whitespace, both 0)
- **tests/test_truly_empty_filter.py** — 10 cases covering all four empty conditions, partial-empty scenarios, field fallbacks (addr vs property_address, legal1 vs legal_descr)

Run:
```bash
pytest tests/test_collin_prior_year_fallback.py tests/test_truly_empty_filter.py -v
```

### Manual Preview Test Checklist

1. **Load Maxwell Creek polygon** (contains property ID 2695489) → confirm sparse rows are filtered out
2. **Load a polygon with mid-edit Collin parcels** → confirm Total Value now shows fallback value (~$XXX,XXX)
3. **Inspect popup** → confirm "(prior year YYYY)" suffix appears when fallback fired (italic / subtle styling)
4. **Inspect sidebar** → confirm Total Value in detail panel also shows suffix
5. **Download CSV** → confirm:
   - New "Value Source" column at position 13
   - Original "Good Comp" column still correctly positioned (now at position 97, was 96)
   - All other Stored Value columns shifted correctly (now at positions 110-119, was 109-118)
   - Parcel rows with fallback show "prior_year_cert_YYYY" (or "prior_year_cert" if year missing)
   - Comp rows inherit parcel source provenance
   - Orphan rows have blank Value Source cell (as expected)
6. **Run pre-deploy SQL audit** (see above) → verify recovery counts match expectations

---

## Known Limitations & Future Work

1. **Subdivision sensitivity:** If a Collin parcel is subdivided mid-year, `cert_total_value` represents the pre-subdivision polygon. Fallback could 2-3x overvalue subdivided accounts. Document as known v1 limitation; Phase 2 may add per-account splitting.

2. **propelio_comps snapshot drift:** Comps scraped before this PR carry cached `tot_val=0` if their parent parcel was sparse. New jobs see fallback values. Expected version-skew; no code fix (acceptable tradeoff). Users should re-analyze after deploy if consistency matters.

3. **Denton + TAD prior-year fallback deferred:** No cert columns ingested. Requires either (a) source investigation for published prior-year files, or (b) DB-snapshot strategy. Queued for Phase 2 pending workflow impact.

4. **Address fallback for sparse Collin parcels:** Only values fall back in Phase 1. If a parcel has cert value but no current address, row renders with blank address. Deferred to Phase 2 if proven needed.

---

## Rollback Path

Single revert commit (no schema changes, no migration):
```bash
git revert <merge-commit-sha>
gcloud builds submit --config cloudbuild-preview.yaml  # if deployed
```

CSV column shift is a one-time readjustment. If rollback drops Good Comp back to column 96 etc., KK notifies downstream consumers.

---

## Deployment Timeline

1. **Preview only** until KK greenlights (typically 1–2 business days after merge)
2. Run pre-deploy SQL audit queries to verify recovery numbers
3. Manual smoke test on preview (checklist above)
4. KK approves → merge to develop → soak → eventual promote to main (Mike's prod)

---

## Author Notes

- **File:** Implemented per spec docs/PRIOR_YEAR_VALUE_FALLBACK_CODING_SPEC.md (10 edits + tests)
- **Copilot Critique:** Incorporated feedback from docs/PRIOR_YEAR_VALUE_FALLBACK_SPEC.md (phase 2 direction, edge cases, rollback discipline)
- **Backward Compatible:** Legacy cached_jobs rows without `total_value_source` degrade gracefully to unrated; re-analysis activates new behavior
- **No Co-Authored Commits:** Per constraint, all commits authored by KK

