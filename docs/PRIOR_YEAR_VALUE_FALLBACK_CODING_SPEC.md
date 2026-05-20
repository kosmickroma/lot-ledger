---
title: Prior-Year Value Fallback — Phase 1 Coding Spec (Copilot-ready)
status: READY FOR COPILOT
date: 2026-05-20
branch: feat/prior-year-value-fallback-2026-05-20 (off main)
deployment: PREVIEW ONLY until KK greenlights develop merge
critique_source: docs/PRIOR_YEAR_VALUE_FALLBACK_SPEC.md + Copilot critique (2026-05-20)
---

# Phase 1 Coding Spec — Prior-Year Value Fallback

## What this changes

When a Collin parcel has `total_value = 0/null` but `cert_total_value > 0` (last year's certified value), use the certified value as the displayed `tot_val` and tag the row with provenance. Also exclude truly-empty parcels (no addr/owner/legal/value/cert_value) from `/api/analyze` results across all four counties.

Recovers ~6,400 Collin parcels currently rendering as "$0 / blank". Removes ~2,310 brand-new pipeline parcels from noise. No DB schema changes. No new ingest. No risk to existing functionality on the happy path.

## Scope (in & out)

**IN:**
- Collin-only prior-year value fallback (other counties don't have cert columns; skipped per Copilot critique recommendation)
- Truly-empty filter applied to all four counties (DCAD will no-op; Collin/Denton/TAD will benefit)
- New CSV column `Value Source` to surface provenance
- Popup display suffix "(prior year YYYY)" on Collin parcels where fallback fired
- Pre-deploy SQL audit query bundled in PR description

**OUT (Phase 2+):**
- Denton + TAD prior-year fallback (no cert columns ingested; requires source investigation — separate work)
- ZIP→City resolver for sparse parcels (separate deferred item)
- Schema migrations (Phase 1 reuses existing columns)
- propelio_comps snapshot fallback (documented as expected version-skew)

## Files to modify

```
api/counties/collin.py        — _normalize_collin_row (primary change)
api/counties/dcad.py          — build_feature (consume total_value_source if present)
api/main.py                   — CSV header, parcel writer, comp writer, orphan writer (4 sites)
api/main.py                   — _api_analyze / per-county fetch wrappers (apply truly-empty filter)
```

## Edit 1 — `api/counties/collin.py` `_normalize_collin_row` (the core change)

**Location:** around lines 209-220 (current implementation reads `total_value` only).

**Behavior:**

```python
def _normalize_collin_row(raw: dict[str, Any]) -> dict[str, Any]:
    sptd_code = _clean_text(raw.get("state_cd"))
    land_val = _safe_float(raw.get("land_value"))
    tot_val_current = _safe_float(raw.get("total_value"))
    cert_tot_val = _safe_float(raw.get("cert_total_value"))

    # Prior-year fallback: if current is null/0 but cert is real, use cert.
    # `cert_total_value` represents Collin's last final certified value, baked
    # into the shapefile DBF — when GIS staff have drawn a polygon but the
    # appraisal team hasn't entered this year's value yet, this carries the
    # last-known-good value forward. Tag with provenance for UX surfacing.
    total_value_source: str | None = None
    if (tot_val_current is None or tot_val_current <= 0) and cert_tot_val and cert_tot_val > 0:
        tot_val = cert_tot_val
        cert_year = _clean_text(raw.get("cert_val_year")) or ""
        total_value_source = f"prior_year_cert_{cert_year}" if cert_year else "prior_year_cert"
    else:
        tot_val = tot_val_current
        # total_value_source stays None — caller skips emit
```

**Emit in returned dict** (alongside existing `tot_val` at line 267):

```python
return {
    # ... existing fields unchanged ...
    "tot_val": tot_val,
    "total_value_source": total_value_source,  # None when current-year is used
    # ... rest unchanged ...
}
```

**Notes:**
- `cert_total_value` and `cert_val_year` are already SELECT'd by the Collin SQL fetch (collin.py:93 and main.py:2386). No SQL changes needed.
- Existing CSV column `Cert Total Value` (col 77 area) stays untouched — analysts still see the raw cert value separately.

## Edit 2 — `api/counties/dcad.py` `build_feature` (propagate the flag)

**Location:** `build_feature` function around line 476-525, where `props` dict is assembled.

`build_feature` is shared across all four counties. It currently formats `tot_val` from `row['tot_val']` as `f"${row['tot_val']:,.0f}"`. Add the source flag to `props`:

```python
props = {
    # ... existing keys unchanged ...
    "tot_val": "Ag-exempt" if ag_zero else (f"${row['tot_val']:,.0f}" if _safe_float(row.get("tot_val")) is not None else "N/A"),
    "total_value_source": _clean_text(row.get("total_value_source")),  # "" when not set
    # ... rest unchanged ...
}
```

**Important:** for DCAD/TAD/Denton rows, `row.get("total_value_source")` returns None → `_clean_text(None)` returns `""`. Only Collin rows ever populate this field. Forward-compatible if other counties add the same pattern later.

## Edit 3 — CSV header (`api/main.py` line 3590ish)

**Location:** the header `writer.writerow([...])` block.

Add new column `Value Source` immediately after `Total Value` (column 12). This pushes every column to its right +1, including all the COMPATIBILITY-LOCK'd positions (Good Comp at 96, RF_Comp series, Seed Target, share_id, Stored Values cells).

**Header diff:**

```python
writer.writerow([
    # ... columns 1-11 unchanged ...
    "Land Value",                # 10
    "Improvement Value",         # 11
    "Total Value",               # 12
    "Value Source",              # 13 ← NEW: "current" / "prior_year_cert_YYYY" / ""
    "Redfin List Price",         # 14 (was 13)
    "Land % of Total",           # 15 (was 14)
    # ... shift everything else +1 ...
])
```

**CRITICAL:** every other writerow in `_run_download_csv` (parcel rows, comp rows, orphan rows) must insert an empty/filled cell at the same position to keep columns aligned. See edits 4-6.

## Edit 4 — CSV parcel row writer (`api/main.py` line 3812ish)

Insert the new cell after `tot_val`:

```python
writer.writerow([
    # ... cells 1-11 unchanged ...
    round(_safe_float(row.get("land_val")), 0) if _safe_float(row.get("land_val")) is not None else "",   # 10
    round(_safe_float(row.get("impr_val")), 0) if _safe_float(row.get("impr_val")) is not None else "",   # 11
    round(_safe_float(row.get("tot_val")), 0) if _safe_float(row.get("tot_val")) is not None else "",     # 12
    str(row.get("total_value_source") or ""),                                                             # 13 ← NEW
    # ... rest +1 ...
])
```

## Edit 5 — CSV comp row writer (`api/main.py` line 4075ish)

Insert at the same position (column 13). The comp row pulls from `_cad` (cached parcel data):

```python
writer.writerow([
    # ... cells 1-11 unchanged ...
    round(_safe_float(_cad.get("land_val")), 0) if _safe_float(_cad.get("land_val")) is not None else "",      # 10
    round(_safe_float(_cad.get("impr_val")), 0) if _safe_float(_cad.get("impr_val")) is not None else "",      # 11
    round(_safe_float(_cad.get("tot_val")), 0) if _safe_float(_cad.get("tot_val")) is not None else "",        # 12
    str(_cad.get("total_value_source") or "") if _cad else "",                                                 # 13 ← NEW
    # ... rest +1 ...
])
```

## Edit 6 — CSV orphan row writer

Same insertion at column 13 — orphan comps have no `_cad` parent, so leave the cell blank:

```python
    "",                                                                                                   # 13 Value Source (orphan: blank)
```

## Edit 7 — COMPATIBILITY LOCK comments (4 sites)

Three of the four `writer.writerow` sites have a COMPATIBILITY LOCK comment near the Good Comp position (96, now 97). Update the comments to reflect the new shift:

```
# COMPATIBILITY LOCK: a future contributor inserting a column anywhere in
# this CSV must coordinate updates at all four writerow sites (header,
# parcel row, comp row, orphan row) AND update the column-position
# comments below. Good Comp at column 97 (was 96, shifted +1 by
# Value Source at 13). RF_Comp series 98-107. Seed Target 108. share_id
# 109. Stored Values cells 110-119.
```

## Edit 8 — Popup display suffix (`api/counties/dcad.py` `build_feature` or downstream consumer)

When `total_value_source` is non-empty, the popup should show `Total Value: $XXX,XXX (prior year YYYY)`. Implementation can be either:

**Option A (server-side, simpler):** mutate `props["tot_val"]` in `build_feature` when source is set:

```python
if total_value_source := _clean_text(row.get("total_value_source")):
    year_match = total_value_source.split("_")[-1] if total_value_source.startswith("prior_year_cert_") else ""
    suffix = f" (prior year {year_match})" if year_match else " (prior year)"
    props["tot_val"] = props["tot_val"] + suffix
```

**Option B (frontend, cleaner separation):** keep `props.tot_val` numeric-clean and add `props.total_value_source` to feature properties. Frontend popup builder appends suffix conditionally.

**Recommend Option B** — keeps the value string regex-safe for Property Filters' `passesNumericFilters` (which parses `tot_val` for numeric comparison). Frontend popup code lives in `frontend/map.js` — find the parcel popup builder (likely around `_buildParcelDetailTableRow` / `_panelDisplayValue`) and append the suffix when `properties.total_value_source` is non-empty.

**Both popup and side panel must surface the suffix.** Search for any `tot_val` / `total_value` consumer in `frontend/map.js` and add the suffix logic at each display site.

## Edit 9 — Truly-empty filter (`api/main.py` per-county fetch or `/api/analyze`)

**Location:** wherever rows are collected after per-county fetch but before being saved to `cached_jobs.rows`.

**Filter condition** (applies to all four counties):

```python
def _is_truly_empty_parcel(row: dict[str, Any]) -> bool:
    """A parcel with no usable descriptive data — exclude from /api/analyze.

    These are GIS-pipeline parcels: CAD staff drew the polygon but the
    appraisal team hasn't entered the tax record yet. No owner to
    contact, no value to assess, no legal description. Mike's workflow
    can't act on them. Once the CAD completes data entry and we
    re-ingest, they'll auto-populate via UPSERT and reappear.
    """
    addr = _clean_text(row.get("property_address")) or _clean_text(row.get("addr"))
    owner = _clean_text(row.get("owner_name"))
    legal = _clean_text(row.get("legal1")) or _clean_text(row.get("legal_descr"))
    tot_val = _safe_float(row.get("tot_val")) or _safe_float(row.get("total_value"))
    cert_val = _safe_float(row.get("cert_total_value"))
    return (
        not addr
        and not owner
        and not legal
        and (tot_val is None or tot_val <= 0)
        and (cert_val is None or cert_val <= 0)
    )
```

**Apply at the end of `/api/analyze` per-county fetch**, after `_normalize_<county>_row` but before the rows are merged into the analysis output:

```python
rows = [r for r in rows if not _is_truly_empty_parcel(r)]
```

**Important:** apply on the merged-row data (post `_normalize_*` call) so all counties share the same gate logic. DCAD will no-op (0 sparse rows). Collin/Denton/TAD remove their respective sparse counts.

**Confirm filter is in the main SELECT chain, not a post-fetch gate**, so `/api/analyze` result counts AND sidebar count badges see consistent numbers. If currently post-fetch, that's fine — but verify counts in the response match polygon coverage minus truly-empty.

## Edit 10 — Pre-deploy SQL audit (PR description, not code)

Copilot: bundle these queries in the PR description so KK can run them post-merge for verification:

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
SELECT 'denton', COUNT(*) FROM denton_parcels WHERE ... -- similar
UNION ALL
SELECT 'tad', COUNT(*) FROM tad_parcels WHERE ... -- similar (no cert_total_value column, omit that clause)
UNION ALL
SELECT 'dcad', COUNT(*) FROM parcels WHERE ... -- similar (no cert_total_value column);

-- (c) Subdivision sensitivity smoke test: are there multi-account
-- clusters sharing the same cert geometry that could 2-3x overvalue?
SELECT cert_total_value, COUNT(DISTINCT parcel_key) AS account_count,
       ST_AsText(ST_Centroid(ST_Union(geom))) AS rough_center
FROM collin_parcels
WHERE (total_value IS NULL OR total_value = 0) AND cert_total_value > 0
GROUP BY cert_total_value
HAVING COUNT(DISTINCT parcel_key) > 1
ORDER BY account_count DESC
LIMIT 20;
-- If any rows return with account_count > 1, document as known limitation;
-- consider per-account splitting in a follow-up (Phase 2 nice-to-have).
```

## Edge cases — explicit handling required

1. **`cert_total_value > 0` but `cert_val_year` empty.** Fallback fires; tag uses `"prior_year_cert"` (no year). Caller emits suffix `(prior year)` only. Already handled in Edit 1.

2. **Both `total_value` and `cert_total_value` are 0/null.** Fallback skipped. If addr/owner/legal also empty → truly-empty filter removes the parcel from analysis entirely. If only value is missing → row still rendered with `Total Value: $0` (acceptable — user sees data quality issue and can investigate).

3. **`total_value > 0` always wins.** Cert is only fallback. Current-year data is always preferred when available — no risk of stale data overwriting fresh.

4. **Property Filter regex safety.** With Option B (numeric-clean `tot_val` in feature properties), `frontend/map.js:passesNumericFilters` parses `tot_val` cleanly. Verify the regex still matches by checking the existing parse path at map.js:218-219 and map.js:243-244 — should be unaffected because `tot_val` field stays purely numeric format.

5. **Comp matched to parcel where fallback fired.** Comp row in CSV gets the fallback `tot_val` via `_cad.get("tot_val")` (cached from analyze). `Value Source` column in comp row also gets `prior_year_cert_YYYY`. Document this in PR — users see comps inheriting the parent parcel's prior-year tag.

## Tests to write (light)

Copilot: add a small test file `tests/test_collin_prior_year_fallback.py` with these cases:

```python
def test_fallback_fires_when_current_zero_and_cert_positive():
    row = {"total_value": 0, "cert_total_value": 250000, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 250000
    assert normalized["total_value_source"] == "prior_year_cert_2024"

def test_fallback_skipped_when_current_positive():
    row = {"total_value": 300000, "cert_total_value": 250000, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 300000
    assert normalized["total_value_source"] is None

def test_fallback_skipped_when_both_zero():
    row = {"total_value": 0, "cert_total_value": 0}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 0
    assert normalized["total_value_source"] is None

def test_fallback_with_missing_cert_year():
    row = {"total_value": 0, "cert_total_value": 250000, "cert_val_year": ""}
    normalized = _normalize_collin_row(row)
    assert normalized["total_value_source"] == "prior_year_cert"  # no year suffix
```

Plus one truly-empty filter test in `tests/test_truly_empty_filter.py`:

```python
def test_truly_empty_parcel_excluded():
    row = {"property_address": "", "owner_name": "", "legal1": "", "tot_val": 0, "cert_total_value": 0}
    assert _is_truly_empty_parcel(row) is True

def test_sparse_but_has_owner_kept():
    row = {"property_address": "", "owner_name": "JOHN DOE", "legal1": "", "tot_val": 0, "cert_total_value": 0}
    assert _is_truly_empty_parcel(row) is False
```

## Deployment plan

**Branch:** `feat/prior-year-value-fallback-2026-05-20` (already created, off main)
**Preview only.** Do NOT promote to develop or main without explicit greenlight from KK after testing on preview.

**Steps after Copilot ships diff:**
1. KK + Claude review diff
2. Run pre-deploy SQL audit queries (Edit 10) — confirm counts match expectations
3. Manual preview deploy: `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`
4. Smoke test on preview:
   - Load Maxwell Creek polygon → confirm `2695489` no longer appears (truly-empty filter)
   - Load a polygon with mid-edit parcels → confirm Total Value now shows fallback value
   - CSV download → confirm new `Value Source` column at position 13 + all shifted columns aligned
   - Popup → confirm "(prior year YYYY)" suffix on fallback parcels
   - Run Subdivision sensitivity smoke test (Edit 10c) — document any multi-account clusters found
5. KK reviews → greenlight → merge to develop
6. Soak on dev → eventually promote to main + deploy to Mike's prod

## Rollback path

Single revert commit (no DB schema changes, no migration):

```bash
git revert <merge-commit-sha>
gcloud builds submit --config cloudbuild-prod.yaml --project=real-estate-map-tool  # if already on prod
```

CSV column shift can break downstream consumers if anyone references column positions by index. KK notified Mike on the 2026-05-20 ship that the Good Comp column shifted +1; this would be ANOTHER +1. Bundle the notification.

## Out of scope / documented version-skew

1. **propelio_comps snapshot staleness.** Old comp snapshots (pre-this-PR) hold the cached `tot_val=0` from when their parent parcel was sparse. New jobs see the fallback value. Acceptable drift; document in PR.

2. **Denton + TAD prior-year fallback.** Their schemas don't have cert columns; Path A/B investigation queued for Phase 2.

3. **Address fallback for sparse Collin parcels.** Only value falls back in Phase 1. If a Collin parcel has cert value but no current address, the row still renders with blank address. Could add address fallback later but adds complexity; defer until proven needed.

## Cross-refs

- `docs/PRIOR_YEAR_VALUE_FALLBACK_SPEC.md` — original critique-level spec
- `memory/csv-export-shipments-2026-05-20.md` — column-shift discipline precedent
- `memory/feedback_db_production_discipline.md` — spec→critique→preview workflow this followed
