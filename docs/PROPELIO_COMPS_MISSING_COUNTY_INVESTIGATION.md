---
title: Propelio Comps with Missing parcel_county — Investigation + Fix Design
status: PENDING COPILOT REVIEW
date: 2026-05-20
discovered_via: 6307 BANDERA AVE / 6325 BANDERA AVE multifamily-filter bug (KK)
---

# Investigation Brief — Missing parcel_county in propelio_comps

## Bug surfaced

User clicked a Propelio sold comp at 6307 BANDERA AVE (a Dallas condo, sptd `A13` = "Condominiums"). The comp's matched CAD parcel polygon stayed visible on the map when the **Multifamily** parcel-type filter was toggled off, only disappearing when **Off Market** was also toggled off. Expected: the parcel should hide with the Multifamily toggle since it's a condo (`A13` → multifamily bucket per `api/counties/dcad.py:classify_parcel`).

## Root cause (data layer)

The comp record at `lotledger_sessions.propelio_comps` for 6307 (and 6325) BANDERA has:
- `parcel_account_num = "00000405388400000"` (populated ✓)
- `parcel_county = ""` (empty ✗)

Query showing the problem:

```sql
SELECT comp_id, address, parcel_county, parcel_account_num
FROM propelio_comps
WHERE address ILIKE '%6307 BANDERA%' OR address ILIKE '%6325 BANDERA%';

--  comp_id |        address          | parcel_county | parcel_account_num
-- ---------+-------------------------+---------------+--------------------
--   3169   | 6307 Bandera Avenue ... |               | 00000405388400000
--   3202   | 6325 Bandera Avenue ... |               | 00000405390300000
```

The frontend's `_compPropertyTypeBucket(comp)` in `frontend/map.js:5044-5067` requires **both** `parcel_account_num` AND `parcel_county` to be set in order to look up the matched CAD parcel's `prop_type`. When county is missing, the CAD lookup is skipped → falls back to Propelio's `property_type = "Residential"` → maps to `"single_family"` via `PROPELIO_TYPE_FALLBACK` → which gets routed to the **off_market** bucket per line 5201.

So the comp's effective parcel-type bucket is `off_market`, not `multifamily` — Off Market toggle gates it, Multifamily toggle doesn't.

## Scope of impact

```sql
SELECT COUNT(*) AS total_comps,
       COUNT(*) FILTER (WHERE parcel_account_num IS NOT NULL AND parcel_account_num != '') AS has_account,
       COUNT(*) FILTER (WHERE parcel_account_num IS NOT NULL AND parcel_account_num != ''
                        AND (parcel_county IS NULL OR parcel_county = '')) AS has_acct_no_county
FROM propelio_comps;
--  total | has_account | has_acct_no_county
-- -------+-------------+--------------------
--  59483 |    52697    |        867
```

**867 propelio_comps rows** (~1.5% of all) — they have a `parcel_account_num` (so the address-to-account match WAS performed at scrape time) but `parcel_county` was not also set.

## Timeline — bug is dormant, not active

```sql
SELECT date_trunc('week', first_seen_at) AS week, COUNT(*)
FROM propelio_comps
WHERE parcel_account_num != '' AND (parcel_county IS NULL OR parcel_county = '')
GROUP BY week ORDER BY week DESC;
--  week        | count
-- -------------+-------
--  2026-05-11  |  687
--  2026-05-04  |  180
```

All 867 affected rows were written in **two weeks: 2026-05-04 and 2026-05-11**. Nothing since 2026-05-12. The 2026-05-12 to 14 "Propelio comp pipeline maturation" work (per `_master_todo_done.md`) likely fixed whatever bug was writing comp rows with only `parcel_account_num` and not `parcel_county`. The bug is currently **dormant** — current scrapes write both fields correctly.

## Downstream impact in server-side code (beyond the frontend filter)

`parcel_county` is read in many places that pair it with `parcel_account_num` as a tuple key:

| File:Line | Purpose | Behavior when `parcel_county` empty |
|---|---|---|
| `api/main.py:3458-3471` | Comp-rating bridge JOIN — folds `comp_ratings` into a parcel-level dict | **Skips the row** (`if not parcel_county or not parcel_account_num: continue` at line 3468). So good/bad ratings on these 867 comps never propagate to parcel-row filtering. |
| `api/main.py:3515-3564` | Comp dedup for CSV export | Tuple key `(parcel_county, parcel_account_num)` — duplicates among the 867 may not collapse correctly |
| `api/main.py:3998` | Parcel-key tuple for CSV grouping | Lookup misses → blank CAD cells in CSV |
| `api/main.py:393` | GIST index on `(parcel_county, parcel_account_num)` | Index entry has empty county, index hint may not find these rows |
| `frontend/map.js:5047` (the discovered symptom) | `_compPropertyTypeBucket` matched-parcel lookup | Falls back to Propelio's MLS taxonomy → wrong parcel-type bucket |

So a **frontend-only fix** would mask the multifamily-filter symptom but leave broken: comp ratings, dedup, CSV CAD match, and any future feature keyed on the tuple.

## Fix options

### Option A — Backfill the 867 rows (recommended primary fix)

One-shot SQL: for each affected row, look up `parcel_account_num` against the four county parcels tables (`parcels` for DCAD, `collin_parcels`, `denton_parcels`, `tad_parcels`) and populate the matching county. Most account numbers are unique within their county's table; if the account_num exists in exactly one county table, we know the county.

Pseudocode:
```sql
UPDATE propelio_comps SET parcel_county = 'dcad'
WHERE comp_id IN (...rows where account_num matches dcad parcels...);
-- Repeat for collin, denton, tad
```

Risks:
- Cross-county account_num collision (one account_num matching multiple county tables). Need to verify uniqueness first via `WHERE account_num IN ('00000405388400000', ...)` against each county table — if any returns >1 match, those need manual review.
- Whatever account-format normalization the original scrape applied — backfill needs to mirror it (e.g., DCAD uses 17-digit padded account_nums; backfill must compare in the same format).

Benefit: fixes all downstream consumers in one shot, no code changes anywhere.

### Option B — Frontend `_compPropertyTypeBucket` loosen (defense-in-depth)

Drop the `&& county` requirement; fall back to account-only lookup; verify county only when present. 5-line code change in `frontend/map.js`.

```js
function _compPropertyTypeBucket(comp) {
  const acct = String(comp?.parcel_account_num || "").trim();
  const county = String(comp?.parcel_county || "").trim().toLowerCase();
  if (acct && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features)) {
    for (const f of lastAnalysisGeojson.features) {
      const p = f?.properties || {};
      if (String(p.account_num || "").trim() !== acct) continue;
      if (county && String(p.source_county || "").trim().toLowerCase() !== county) continue;
      // Found match — return p.prop_type (with active/off_market split)
      ...
```

Risks:
- Account_num collision across counties WITHIN the same `lastAnalysisGeojson` (rare, only matters if a polygon spans multiple counties AND two different parcels share an account_num)
- Doesn't fix the server-side breakage (ratings, dedup, CSV)

Benefit: defends against future regressions if comp ingest ever drops county again. Cheap.

### Option C — Investigate the original scrape bug first

Before fixing anything, find the code that wrote those 867 rows in 2026-05-04 and 2026-05-11 with only account_num. If the bug is truly fixed (no rows since 2026-05-12 prove it), great. If not, fix the scrape code to prevent recurrence.

Likely candidate code: the Propelio comp scrape / CMA harvest writer in `api/propelio/` or similar — wherever new `propelio_comps` rows are written. Need to verify the `parcel_account_num` + `parcel_county` are always written together.

### Recommended sequence (KK's stance + my read)

1. **Option C first (15 min)** — confirm the bug is dormant. Find the current writer code, verify it always writes both fields.
2. **Option A second** — backfill the 867 rows.
3. **Option B optional** — add the frontend defense-in-depth IF Option C reveals the bug isn't truly fixed.

## Questions for Copilot review

1. **Is Option A safe?** Specifically: are there any DCAD/Collin/Denton/TAD account_nums that exist in multiple county tables (collision)? If yes, how do we resolve? (Probably won't happen because they're county-internal IDs, but worth verifying with a one-line query.)

2. **What account_num format/normalization should the backfill SQL use?** DCAD pads to 17 digits with leading zeros (e.g., `00000405388400000`). The other counties use different formats (Collin has `2695489:R-10434-00Q-0220-1`). The backfill query needs to match the format propelio_comps actually stores.

3. **Should Option B (frontend defense) ship even if A succeeds?** Argument for: future scrape regressions silently break the multifamily filter again, even after backfill. Argument against: noise — adds branch-and-fallback complexity for a case that should never happen.

4. **Are there other downstream consumers we missed?** I traced 5 sites in `api/main.py` and 1 in `frontend/map.js`. Did I miss anything (e.g., frontend rendering of comp footprints, propelio cache writes, marathon scraper)?

5. **Verification strategy for the backfill** — what's the right pre/post-check? Pre: confirm none of the 867 account_nums collide across counties. Post: confirm `(parcel_county IS NULL OR parcel_county = '') AND parcel_account_num != ''` returns 0 rows after the UPDATE.

6. **Should we add a CHECK constraint** to enforce that `parcel_account_num` and `parcel_county` are always set together going forward? E.g., `CHECK ((parcel_account_num = '' AND parcel_county = '') OR (parcel_account_num != '' AND parcel_county != ''))`. Would catch future regressions at write time.

## Out of scope

- The original 6307/6325 BANDERA bug also surfaced a related question about whether `parcels.sptd_code` being NULL but `appraisal.sptd_code` being populated causes other issues. The `COALESCE(a.sptd_code, p.sptd_code)` in `query_parcels` already handles this for the polygon-analysis fetch (verified at `api/counties/dcad.py:332`). No action needed there.

## Cross-refs

- Master TODO bug entry to add after this is resolved
- `_master_todo_done.md` — the 2026-05-12 to 14 "Propelio comp pipeline maturation" entry — investigate which commits there fixed the original scrape
- [[feedback_db_production_discipline]] — DB-adjacent change rule applies

---

## Copilot prompt — paste into Copilot tomorrow morning

```
@workspace Read docs/PROPELIO_COMPS_MISSING_COUNTY_INVESTIGATION.md end-to-end. Give a rigorous review of the fix options and answer the 6 numbered questions in the "Questions for Copilot review" section.

CONTEXT:
A user reported a parcel-type filter bug today (2026-05-20): toggling off "Multifamily" failed to hide a sold condo comp at 6307 BANDERA AVE (Dallas, sptd A13 = Condominiums). We traced it to 867 of ~59k propelio_comps rows that have parcel_account_num set but parcel_county empty. The frontend lookup at frontend/map.js:5047 requires both fields, so these comps fall back to Propelio's MLS taxonomy → wrong bucket. The bug is dormant — all 867 affected rows were written in two weeks (2026-05-04 and 2026-05-11), nothing since 2026-05-12.

KK's two pushbacks on my initial "one-line frontend loosen" pitch were sharp and correct:
1. The frontend fix could mask other issues we haven't traced
2. If frontend truly fixes it, why backfill at all?

Investigation revealed parcel_county is read in 5+ server-side paths (comp ratings bridge, dedup, CSV CAD-match, GIST index, future tuple-keyed code). Frontend-only fix would leave all those broken. So backfill is the real answer.

The doc proposes three complementary options:
- Option A: backfill the 867 rows (primary fix)
- Option B: frontend defense-in-depth (optional, against future regressions)
- Option C: investigate the original scrape bug to confirm it's truly dormant

WHAT WE WANT:
1. Answer the 6 numbered questions in the doc directly. Be specific.
2. Push back on anything you disagree with. Particularly: is the recommended sequence (C → A → optionally B) correct, or should the order be different?
3. Flag any downstream consumers of parcel_county we missed (5 sites in api/main.py + 1 in frontend/map.js were listed).
4. Comment on the CHECK constraint idea (Q6) — useful or noise?
5. Specifically check the proposed backfill SQL strategy in Option A: anything risky about looking up account_num against all 4 county parcels tables? Cross-county collisions possible?

CONSTRAINTS:
- Don't write code yet. Spec critique only.
- Don't run shell commands. Trust the numbers in the doc.
- Be opinionated. We need pushback, not validation.
- 600-1000 words. Numbered list keyed to the 6 questions plus your pushback section.
```
