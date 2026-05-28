# Duplexes Property Type Spec (DCAD) — v2

**Status:** v2 — all 8 open items resolved 2026-05-28. Ready for independent
review, then implementation plan.

**Date opened:** 2026-05-26 (brainstorm)
**v2 resolved:** 2026-05-28
**Author:** KK + Claude
**Branch:** `feat/duplexes-property-type-2026-05-28` (off `develop`). **Do NOT
merge to `develop`/`main` during the vacation merge-hold — preview deploys
only.**
**Client driver:** Mike — wants 2-4 unit duplexes broken out from multifamily
so analyst workflows can target them as their own bucket (different investment
thesis from apartments / condos).

## Blast-radius / safety summary (read first)

- **`prop_type` is computed at runtime, never stored.** On every `/api/analyze`
  request, `classify_parcel(row, exempt_set)` reads `row["sptd_code"]` and
  returns the bucket string (`api/counties/dcad.py:709`). `build_db.py` ingests
  only the raw `sptd_code` (`scripts/build_db.py:419`) — there is **no
  `prop_type` column** for DCAD anywhere in the schema (verified: SQL grep
  empty).
- **Therefore: no schema migration, no backfill, no DB writes.** Zero risk to
  Mike's live data. Redeploy and `/api/analyze` immediately returns `duplexes`
  for B12.
- **Additive.** We add a 6th bucket beside the existing 5. The *only* behavioral
  change to existing code is one classifier branch: `B12` moves out of the
  shared `multifamily` set into its own. `B11` / `A13` / `A14` stay multifamily.
- **One frozen surface:** the PMTiles browse layer bakes classification at
  export time (`scripts/export_pmtiles.py`). Browse mode shows `B12` as
  multifamily until the tiles are rebuilt — the deferred, accepted inconsistency
  window. Draw mode + analyze API are correct immediately on deploy.
- **(Aside)** A stored `prop_type` column *is* read at `api/main.py:2932`, but
  that is **Collin** raw data. Collin is out of scope and DCAD never touches it.
  No interaction.

## Why

The map's "Multifamily" bucket currently lumps three very different property
types together:

- **Apartments** (DCAD SPTD `B11`) — 5+ unit, commercial-scale
- **Condominiums** (DCAD SPTD `A13`) — share parcel geometry, condo HOA
- **Duplexes** (DCAD SPTD `B12`) — 2-4 unit small residential

Mike's investment thesis for 2-4 unit duplexes is closer to single-family flip
economics than to apartments. Lumping them with apartments makes it hard to
surface duplex opportunities without also surfacing every apartment building.
Splitting `B12` into its own type gives the team a precise filter target.

## Locked design decisions (brainstorm 2026-05-26, reaffirmed 2026-05-28)

| Question | Answer |
|---|---|
| Scope of v1 | **DCAD (Dallas County) only.** TAD/Collin/Denton keep their current multifamily mapping for `B12`-equivalents until separate per-county passes — each county uses different state codes and needs its own audit. |
| Classifier rule | **Trust the SPTD code.** `B12` → `duplexes`, no runtime `num_units` check. Texas appraisal convention: `B11 = 5+ unit`, `B12 = 2-4 unit` at the source. A 5+ unit property coded `B12` would be a rare DCAD anomaly — ignored. `num_units` belt-and-suspenders rejected as YAGNI. |
| Color | **`#9C7B8C`** muted mauve. Border **`#7E6373`** (~10% darker). KK's pick from three "bland" options. Distinct from sold purple, commercial brown, multifamily near-black. |
| Comp bucket | **Duplex Propelio comps get their own bucket**, symmetric with parcel-side classification. New `parcelTypeDuplexes` toggle gates comps too. |
| PMTiles rebuild | **Deferred.** Code ships through preview → dev → prod first; browse layer catches up when KK runs the ~45-min export. Inconsistency window accepted for validation. |

## Resolved open items (v2)

1. **Filter checkbox label** → **"Duplexes"** (plural, matches Apartments /
   Townhouses convention).
2. **`getStatusLabel` popup text** → **"DUPLEXES"** (matches the label).
3. **Propelio spillover comps (no CAD match)** → **conservative for v1.** See
   §Comp-bucket behavior. Matched comps get `duplexes` automatically; unmatched
   spillover stays routed to `multifamily`. Accurate-spillover upgrade is a
   fast-follow gated on a read-only `propelio_cache` check (does Propelio
   actually populate `Duplex`/`Triplex`/`Quadruplex` categories?).
4. **Checkbox sidebar position** → **directly after Multifamily** in the
   Property Type Filters block.
5. **Status-badge order** → **Duplexes badge directly after Multifamily** badge.
6. **Verification plan** → see §Verification matrix.
7. **Mike heads-up email** → see §Mike heads-up draft.
8. **PMTiles export script** → `scripts/export_pmtiles.py` (builds the GeoJSON
   and *prints* the `tippecanoe` command; does not execute it). Source of truth:
   `docs/PMTILES_PLAN.md`. Deferred; dry-run against a dev tiles bucket before
   prod.
- **Default visibility** → **OFF.** `DEFAULT_FILTERS.duplexes = false`, matching
  the convention that only `off_market` + `vacant` are ON by default. Saved-area
  `filter_state` restore defaults a missing `duplexes` key to `false` (hidden) —
  backwards-compatible, no surprise reveals.

## DCAD SPTD code reference (unchanged buckets except B12)

| SPTD | Label | Current bucket | New bucket (v1) |
|------|-------|----------------|-----------------|
| A11 | Single Family Residences | single_family | unchanged |
| A12 | Townhouses | single_family (fall-through) | unchanged |
| A13 | Condominiums | multifamily | unchanged |
| A14 | (not in DCAD's actual data) | multifamily (defensive dead-code) | unchanged |
| A20 | Mobile Home on Owners Land | single_family (fall-through) | unchanged |
| **B11** | **Apartments** | **multifamily** | **unchanged** |
| **B12** | **Duplexes** | **multifamily** | **→ duplexes** |
| C11 | Vacant Lots/Tracts (SFR) | vacant / exempt (nominal) | unchanged |
| C12 | Vacant Lots/Tracts (Commercial) | vacant / exempt / commercial | unchanged |
| C13 | Vacant Lots/Tracts (Industrial) | commercial | unchanged |
| F10 | Commercial Improvements | commercial | unchanged |
| F20 | Industrial Improvements | commercial | unchanged |
| X11 | Totally Exempt Property | exempt | unchanged |
| D10 | Qualified Agricultural Land | exempt | unchanged |
| (all others) | — | single_family fall-through | unchanged |

**Trailers note:** A20 / M31 / M32 all fall through to single_family. No action
needed for trailers in v1.

## Classifier change (`api/counties/dcad.py:725`)

```python
# Current (dcad.py:725)
if sptd in {"B11", "B12", "A14", "A13"}:
    return "multifamily"

# New
if sptd == "B12":
    return "duplexes"
if sptd in {"B11", "A14", "A13"}:
    return "multifamily"
```

Order matters: the `B12` check must come first, then the residual multifamily
check. `A14` stays as defensive dead-code; DCAD's actual data doesn't ship it.

## Architecture surface (verified line numbers, 2026-05-28 deep dive)

### Backend (`api/`)

1. **`api/counties/dcad.py:725`** — the classifier change above. Single function
   (`classify_parcel`).
2. **`api/main.py:1187`** — counts init dict (analyze loop A). Add
   `"duplexes": 0`.
3. **`api/main.py:1201-1210`** — classification→count branch chain. Add a
   `duplexes` branch (before the `else` catch-all).
4. **`api/main.py:3476`** — empty-state counts dict (early-return path). Add
   `"duplexes": 0`.
5. **`api/main.py:3502`** — counts init dict (analyze loop B). Add
   `"duplexes": 0`.
6. **`api/main.py:3533-3542`** — classification→count branch chain (loop B). Add
   a `duplexes` branch.
7. **CSV export** — `prop_type` is a string column; new value `"duplexes"` flows
   in additively. No column shift, no header change.

### Frontend (`frontend/map.js`)

1. **`COLORS` (line 23)** — add `duplexes: "#9C7B8C"`.
2. **`BORDER_COLORS` (line 34)** — add `duplexes: "#7E6373"`.
3. **`TYPE_LABELS` (line 202)** — add `duplexes: "Duplexes"`.
4. **`DEFAULT_FILTERS` (line 221)** — add `duplexes: false`.
5. **`FILTER_INPUT_IDS` (line 233)** — add `duplexes: "filter-duplexes"`.
6. **`captureFilterState` (line 1720)** — include `duplexes` in the serialized
   filter state. On **restore**, a missing `duplexes` key must default to
   `false` (merge over `DEFAULT_FILTERS`). *Reviewer/plan: confirm the restore
   path merges new keys via `DEFAULT_FILTERS` spread so old `filter_state` blobs
   don't crash or hide other buckets.*
7. **`parcelType*` mapping (line 6856)** — add
   `parcelTypeDuplexes: filterState.duplexes`.
8. **`_compPropertyTypeBucket` (line 6868)** — a comp matched to a CAD parcel
   with `prop_type === "duplexes"` returns `"duplexes"` automatically once the
   value exists (line 6883 `return baseType`). **No code change needed on the
   matched path.**
9. **`compPassesPropelioFilters` comp gate (line 7019)** — add, mirroring the
   multifamily line:
   ```js
   if (bucket === "duplexes" && filters.parcelTypeDuplexes === false) return false;
   ```
10. **`getStatusLabel` (line 8559)** — add a `duplexes` → `"DUPLEXES"` branch.
11. **`PROPELIO_CATEGORY_TO_BUCKET` (line 246)** — **v1: leave `Duplex` /
    `Triplex` / `Quadruplex` → `multifamily`** (conservative spillover). Upgrade
    to `→ duplexes` is the fast-follow (see item #3).

### Frontend HTML (`frontend/index.html`)

1. New checkbox row `filter-duplexes` in the Property Type Filters block,
   **directly after Multifamily** (`filter-multifamily` is at `index.html:221`).
2. New **Duplexes status badge** (with count from the backend `counts` dict),
   directly after the Multifamily badge.

**No separate comp-side checkbox.** Verified: there is exactly one Property Type
checkbox per type (only `filter-multifamily` exists — no comp twin). The comp
gate reuses the parcel-side toggle via the `parcelTypeDuplexes:
filterState.duplexes` mapping (`map.js:6856`), so the single Duplexes checkbox
gates **both** parcels and comps. *(This corrects the v1 assumption of a
separate Comp Filters checkbox.)*

### PMTiles (deferred)

`scripts/export_pmtiles.py` bakes classification at export time → browse layer
shows `B12` as multifamily-black until rebuilt. Dry-run against a dev tiles
bucket before prod. Deferred per locked decision.

## Comp-bucket behavior (resolves #3)

- **Matched comps** (have `parcel_account_num` + `parcel_county` resolving to a
  CAD parcel in `lastAnalysisGeojson`): `_compPropertyTypeBucket` returns the
  matched parcel's `prop_type`. After the classifier change a matched B12 parcel
  carries `prop_type === "duplexes"`, so the comp is bucketed `duplexes`
  automatically and gated by the new `parcelTypeDuplexes` toggle. **Free,
  symmetric, no decision.**
- **Spillover comps** (no CAD match → fall back to Propelio category): a code
  comment at `map.js:6887` notes Propelio's `property_category` /
  `property_type` may not be populated in the current API shape (verified
  empirically 2026-05-19). **v1 keeps these routed to `multifamily`** — zero
  behavior change, no dependency on unreliable category data.
- **Fast-follow (post-v1):** read-only check of `propelio_cache` to confirm
  whether `Duplex`/`Triplex`/`Quadruplex` categories are actually populated. If
  yes, remap those three in `PROPELIO_CATEGORY_TO_BUCKET` → `duplexes` for
  accurate spillover bucketing. Tracked, not in v1 scope.

## Intended behavior change (call out for Mike)

After the split, the **Multifamily toggle no longer surfaces B12 duplexes** —
anyone (including Mike) who flips Multifamily ON to see duplexes must now also
enable the new **Duplexes** toggle. This is by design (the whole point of the
split) but is a real change in the Multifamily toggle's behavior. → Mike
heads-up email.

## Verification matrix (resolves #6)

Preview-first. All read paths; nothing destructive.

**A. Backend / API**
- Over an area containing a known DCAD `B12` parcel, `GET /api/analyze` returns
  `feature.properties.prop_type === "duplexes"` for it; `counts.duplexes >= 1`;
  that parcel is **not** in `counts.multifamily`.
- `B11` still → `multifamily`; `A13`/`A14` still → `multifamily`.

**B. Draw mode (frontend)**
- B12 parcel renders fill `#9C7B8C`, border `#7E6373`.
- Popup status reads **"DUPLEXES"**.
- Duplexes checkbox toggles its visibility; **Multifamily checkbox no longer
  affects it**.
- Duplexes badge shows the correct count.

**C. Browse mode (PMTiles) — inconsistency window**
- Until tiles rebuild, B12 still renders multifamily-black; Duplexes toggle has
  no effect in browse. Confirm this matches the documented accepted state.

**D. CSV export**
- `prop_type` column emits `"duplexes"` for B12 rows; column order + headers
  unchanged.

**E. Comps**
- A sold comp matched to a B12 CAD parcel → bucket `duplexes`; Duplexes comp
  toggle hides/shows it.
- A spillover comp (no CAD match) → routes to `multifamily` (v1); confirm it is
  not unexpectedly hidden.

**F. Saved-area backwards-compat**
- Load a saved area created before this change (its `filter_state` lacks
  `duplexes`) → `duplexes` defaults to `false` (hidden), no console error, no
  crash, other buckets unaffected.
- Save a new area → `filter_state` now includes `duplexes`.

**G. Roles (all 5: developer / owner / power_user / user / member)**
- Duplexes checkbox + badge render for each role; no role sees a broken/empty
  filter block.

**H. Regression**
- Existing buckets (off_market / vacant / multifamily / commercial / exempt):
  counts, colors, filters all unchanged.

## Mike heads-up draft (resolves #7)

> **Subject:** Heads-up — new "Duplexes" property type in LotLedger
>
> Quick FYI before this goes live: I split 2-4 unit duplexes (DCAD code B12)
> out of the "Multifamily" bucket into their own **Duplexes** type — its own
> color and its own filter toggle.
>
> Two things that affect you:
> 1. **CSV / Sheets:** the `prop_type` column now has a new value, `duplexes`.
>    Any filter or formula keyed on `prop_type = "multifamily"` will **no longer
>    include** 2-4 unit duplexes. If you want the old combined view, change it to
>    include both (e.g. `prop_type` in `("multifamily","duplexes")`).
> 2. **Map:** the **Multifamily** toggle no longer shows duplexes — use the new
>    **Duplexes** toggle (off by default, like the other type filters).
>
> Browse-mode parcel colors catch up after a tile rebuild I'll run separately;
> draw-mode (your normal search) is correct right away. Same kind of heads-up as
> the parcel-ratings column change.

## CSV / spreadsheet impact

Additive only: `prop_type` gains a possible value `"duplexes"`. No column shift,
no new columns. Mike's existing `prop_type == "multifamily"` selectors won't
break — they just no longer match what used to be 2-4 unit duplexes.

## Out of scope (v1)

- TAD / Collin / Denton duplex classification (separate per-county passes).
- Backfill / rewrite of historical CSV exports that emitted "multifamily" for
  B12.
- PMTiles rebuild itself (deferred; KK runs the pipeline).
- Mike-side spreadsheet template updates (KK handles via the heads-up email).
- `num_units`-based stricter check (rejected — trust SPTD).
- Accurate Propelio spillover routing (fast-follow, gated on `propelio_cache`
  verification).

## Pitfalls

1. **Stacking** — DCAD condos share one polygon across units; the
   `condoOutlineSeen` dedup (keyed by geometry shape, `map.js:9334`,
   `docs/RENDERING_RULES.md:31`) collapses them. Duplexes are *probably* 1:1
   (one tax parcel / polygon per duplex). **Verify during impl** by sampling a
   few B12 parcels. If they do stack, the existing geometry-keyed dedup catches
   them for free.
2. **PMTiles drift** — browse vs draw mismatch until rebuild. Documented;
   accepted window.
3. **Saved-area `filter_state`** — missing `duplexes` key must default `false`.
   Now consistent with the default-OFF decision.
4. **Comp spillover** — resolved (#3); conservative v1.
5. **Mike's spreadsheets** — heads-up email before prod.

## Implementation order

1. Backend: classifier (`dcad.py:725`) + counts dicts/branches (`main.py`).
   Smallest blast radius, verify on preview first.
2. Frontend: `COLORS` / `BORDER_COLORS` / `TYPE_LABELS` / `DEFAULT_FILTERS` /
   `FILTER_INPUT_IDS` → checkbox + badge (index.html) → comp gate + mapping →
   `getStatusLabel`.
3. PMTiles rebuild — last, separate, dev-bucket dry-run first.

## Flagged for the independent reviewer

- **#3 spillover routing** — is conservative (→ multifamily) right for v1, or
  should we do the `propelio_cache` check now and route accurately? Weigh
  effort/risk.
- **`captureFilterState` restore** — confirm new keys merge via `DEFAULT_FILTERS`
  so old `filter_state` blobs are safe.
- **Stacking** — confirm B12 is 1:1 parcel:polygon (sample query).
- **Any missed touch point** — the risk profile here is *incompleteness* across
  ~a dozen additive edits, not complexity. Hunt for a surface I didn't list.

## Related memory + docs

- `docs/CLIENT_BRIEFING.md:508` — PMTiles rebuild cadence + classification drift.
- `docs/RENDERING_RULES.md:31`, `docs/CODE_GUIDE.md:311` — `condoOutlineSeen`
  stacking dedup.
- memory `feedback_db_production_discipline` — preview-first for prod-data-
  adjacent changes (this is classifier-only, no schema change, but Mike's prod
  is live).
- memory `feedback_role_aware_always` — verify the new filter across all 5 roles.
- memory `feedback_filter_defaults` — only off_market + vacant ON by default;
  new types default OFF.
