---
title: Prior-Year Value Fallback for Sparse CAD Parcels
status: DRAFT — pending Copilot critique
date: 2026-05-20
author: KK + Claude
---

# Prior-Year Value Fallback Spec

## Problem

Some CAD parcels in our DB have current-year tax/value/owner/address/legal fields empty even though a real house sits on the polygon (verified via satellite view). Investigation traced the root cause to **CAD staff workflow**: GIS staff draw the parcel polygon first; appraisal staff backfill the tax record days-to-weeks later. Until the appraisal entry lands, the parcel appears in our shapefile-based ingestion but with empty attribute fields.

User-visible symptom: in CSV exports and on-map popups, these parcels show as `Off Market / Owner: N/A / Total Value: $0 / Legal Description: blank / Year Built: N/A`. They clutter the workflow because Mike can't act on them (no owner to contact, no value to assess) yet they're visually present and clipped by polygon analysis.

Validation: We confirmed one specific Collin parcel (Property ID `2695489`) shows the same empty state in Collin CAD's OWN public portal, with the GIS staff annotation "Edited by CCAD_Maps 11 hours ago". It's a real new-construction or recently-subdivided parcel that hasn't been appraised yet. We're not missing data Collin has — Collin doesn't have it yet.

## Data findings (DB query, Mike's prod, 2026-05-20)

| County | Total parcels | No current value | With addr still populated | With owner still populated | With legal still populated | Has cert_total_value (last year) ingested |
|---|---|---|---|---|---|---|
| **DCAD** (Dallas) | 759,193 | **0** | — | — | — | — (tax-roll-primary ingest, no gap) |
| **Collin** | 432,565 | 9,608 | 6,216 | 7,283 | 7,298 | **6,436** ⭐ |
| **Denton** | 375,884 | 1,666 | 62 | 62 | 62 | 0 (no cert columns in our DB) |
| **TAD** (Tarrant) | 761,119 | 21,876 | 14,795 | 14,807 | 14,561 | 0 (no cert columns in our DB) |

**Architectural insight:** DCAD ingests from `ACCOUNT_INFO.CSV` (tax-roll-primary) → geometry joined in secondarily → zero sparse rows because the CSV always has owner/addr/legal. The other three counties ingest from a shapefile-primary source (Collin: `parcels_with_appraisal_data_R5.shp`; Denton: GeoJSON; TAD: `ParcelView_4326.shp`) where the DBF/property table is THE source of truth. When DBF attributes are empty for a record, our DB inherits the gap.

**Collin's lucky break:** its shapefile DBF carries BOTH current-year values AND last-certified values (`cert_total_value`, `cert_val_year`, `cert_appraised_value`) per parcel. We already ingest these columns but never use them for fallback display. **6,436 of 9,608** Collin no-current-value parcels have `cert_total_value > 0`. Implementing a query-time fallback recovers them today with zero ingest changes.

**Denton + TAD don't carry cert columns** at all in our DB. Recovering their mid-edit parcels requires either (a) finding a published prior-year source from each CAD, (b) building a DB-snapshot strategy that preserves last-good values before each re-ingest, or (c) cross-joining against a future historical parcels DB (see [[project_historical_owner_data.md]] — the `parcel-history` repo Phase 1 scaffold KK has on the master_todo).

## Proposal

### Phase 1 — Collin-only, query-time fallback (recommended ship now)

**Scope:** Update `api/counties/collin.py` `build_feature` and `_build_collin_row` (and wherever else collin parcel data is shaped for /api/analyze) to use `cert_total_value` as fallback when `total_value` is null/0. Similarly map `cert_appraised_value` → `appraised_value` if relevant. Add a `value_source` marker on the returned feature properties: `"current"` (default) or `"prior_year_cert_<YYYY>"` (when fallback fired).

**UX surfaces:**
- **Popup:** show "Total Value: $XXX,XXX (prior year YYYY)" when value_source != current. Subtle visual difference (italic or light gray) so analyst notices but doesn't get confused.
- **CSV:** add a new column "Value Source" (e.g., `current` / `prior_year_cert_2024`) so the spreadsheet retains provenance. OR append "(prior year YYYY)" inline to the existing Total Value cell. (Copilot input requested.)
- **Filtering:** Property Filter Appraised Value gates work normally on the fallback value — no special treatment. The intent of the filter ("show me parcels worth $X+") is satisfied either way.

**Truly-empty filter:** For Collin parcels where neither current NOR cert value exists AND owner/addr/legal are all empty (~2,310 rows — the brand-new pipeline parcels), exclude from `/api/analyze` results entirely. They're noise for Mike's workflow and not actionable.

**Estimated recovery for Collin:** ~6,400 parcels regain valid values. ~2,300 brand-new parcels get filtered out. Net: cleaner CSV, fewer "Off Market $0" surprises.

### Phase 2 — Denton + TAD (deferred, requires investigation)

Two sub-paths to evaluate (Copilot input requested on which to pursue):

**Path A: Source-side investigation.** Check Denton CAD + Tarrant Appraisal District public data downloads to see whether they publish prior-year certified-roll files we don't currently ingest. Extend `scripts/build_denton.py` + `scripts/build_tad_db.py` to read both years and add `cert_*` columns mirroring Collin's schema. If a clean prior-year source exists, this is the right architectural fit.

**Path B: DB-snapshot strategy.** Before each re-ingest of Denton/TAD, snapshot the existing `<county>_parcels` table into `<county>_parcels_prior` (preserving last-known-good values per parcel_key). At query time, fall back to the prior table when current is empty. Cheaper to build but produces "snapshot whenever we last re-ran" semantics rather than "official last-certified year" semantics.

**Path C: Wait for parcel-history repo.** [[project_historical_owner_data.md]] is already on the roadmap as a separate repo + DB for multi-year historical CAD data. If that gets built (PIA responses expected ~2026-05-28), the fallback can JOIN against it cleanly. Defers Denton+TAD recovery by weeks.

Recommend deferring Phase 2 until Phase 1 ships and we see workflow impact. KK + Mike use Phase 1 → if Denton/TAD sparse rows become noticeable friction, prioritize Phase 2.

### Phase 3 — Cross-cutting cleanup (small, ship with Phase 1)

Add the truly-empty filter to all four counties (gate on: addr empty AND owner empty AND legal empty AND value=0). For DCAD this is a no-op (0 sparse rows). For Denton + TAD it removes the all-blank fully-pipeline parcels even before Path A/B/C lands — partial improvement without waiting.

## Edge cases & risks

1. **Parcels under contest/protest.** `cert_total_value` reflects last final certified value; if the parcel is currently being contested (protests phase), current_value may legitimately be $0/null until the contest resolves. Using cert as fallback may misrepresent the current state. Likely acceptable — most users prefer "last known value" over "blank" — but worth surfacing in UX with the prior-year tag.

2. **Significant value changes year-over-year.** A teardown could legitimately have last-year value at $400k (with house) but this-year reassessed lower after demo. Using cert in that case shows stale info. Hard to detect automatically. Tag in UX makes this user-detectable.

3. **`cert_total_value` itself is null.** ~3,172 Collin parcels have neither. These are brand-new, truly empty — Phase 3 filter removes them.

4. **Mid-flight ingest re-run.** When Collin re-ingests with a fresher snapshot (next month, next quarter), the 6,400 parcels we're rescuing today likely will have populated current values — fallback stops firing for them. Clean.

5. **Caching layer.** `propelio_comp_archive` and `cached_jobs` snapshot parcel data per job. Pre-fix CSV exports for old jobs will continue to show $0 in those captured rows. New jobs benefit. No backfill of historical jobs.

6. **`parcel_geom` deduplication.** Sparse parcels still have geometry. Phase 3 filter removes them — verify this doesn't break other pre-cached comp matches that referenced their account_num. Should be safe (orphan comps just become unmatched comps; we already handle that path).

## Questions for Copilot critique

1. **Phase 1 scope** — is the Collin-only query-time fallback complete? Anything in the codebase we'd miss (e.g., CSV column 12 `tot_val` in `api/main.py:4088` reads from `_cad` lookup; does that lookup hit the same `build_feature` path, or a separate SQL fetch that wouldn't apply the fallback)?

2. **UX flagging** — column-in-CSV vs inline-suffix vs both? What's least confusing for Mike's day-to-day analyst workflow?

3. **Phase 2 direction** — Path A (source files), Path B (DB snapshots), or Path C (parcel-history repo)? Which has the best cost/benefit?

4. **Edge case 1 (contests)** — is the tag UX sufficient, or do we need to filter out cert-fallback parcels that match known protest patterns?

5. **Naming** — `value_source` field on feature properties: better name? Concern is the field showing up in CSV column headers and being unclear to non-engineers.

6. **Schema migration risk** — Phase 1 doesn't touch DB schema (uses existing Collin columns), so it's safe. But if Phase 2 Path A lands, we'd ADD `cert_total_value` columns to Denton + TAD tables. Migration discipline reminder per [[feedback_db_production_discipline]].

7. **Filter criterion correctness** — for Phase 3's "truly empty" filter, are the four conditions (no addr, no owner, no legal, no value) tight enough? Could a legitimate parcel slip through if it has, say, a zoning code but nothing else?

## Out of scope

- Implementing parcel-history repo (separate initiative)
- ZIP→city resolution for DCAD/TAD parcel addresses (separate deferred item)
- Multi-year ownership history (PIA-driven, ~2026-05-28)
- Performance optimization (Phase 1 is O(1) per row — no concern)

## Memory cross-refs

- [[csv-export-shipments-2026-05-20]] — shipped on the same hotfix branch
- [[feedback_db_production_discipline]] — discipline rule gating this work
- [[project_historical_owner_data.md]] — overlapping infrastructure
