# Propelio Integration — Roadmap

What's next, ranked roughly by value × effort. Items in **Open** are
unfinished. Items in **Done** are checked off and kept here for reference
context.

## 🟠 Performance — Cap GPU/CPU drain from purple footprint pulse animations

KK noticed 2026-05-09 that his laptop fan ramps to "screaming banshee"
levels when lot-ledger is open with Propelio comps loaded. As soon as
the tab closes the fan calms down. **Almost certainly the pulsing CSS
animations** — each `.propelio-footprint-glow` element has:

- Two layered `drop-shadow()` filters
- A keyframe (`propelioFootprintPulse`) that mutates both filters AND
  `fill-opacity` every 2.2s
- `will-change: filter` GPU hint

Multiplied by 50-100 footprints per polygon pull + the button shimmer +
the gold saved-parcel pulse already in lot-ledger, the GPU compositor
is doing a lot of Gaussian-blur recomputation.

Possible mitigations (try in order, cheapest first):

1. **Drop the inner `drop-shadow` layer** — keep just one. Halves filter cost.
2. **Slow the animation** to 4-6s ease-in-out. Less per-frame work.
3. **Use `transform: scale()`** instead of filter changes — GPU-friendly.
4. **Remove the second `drop-shadow` from the keyframe** so only the
   filter on the base class animates, not the keyframe steps.
5. **Pause animations when map is idle** — listener on map move/zoom
   end + idle timer to pause `animation-play-state: paused`.
6. **Use SVG filter defined once + `filter: url(#...)` reference**
   instead of CSS `drop-shadow`. More efficient browser-side.
7. **Box-shadow on a wrapper `g` element** instead of drop-shadow on
   each path. Single shadow, applied as composite.

For now (KK's request 2026-05-09): leave the visual unchanged, this is
investigation-and-fix-later.

## 🟠 Post-Chunk-C — Investigate parcel match rate in production

Smoke test on Glenridge Estates polygon showed 98% match rate
(58/59 comps got a `parcel_geom` from the parcel DB and rendered as
purple glowing footprints, only 1 fell back to a dot).

**In real use 2026-05-09**, KK observed that *significantly more comps
fall back to dots vs footprints* than 98% would predict. Hypotheses
to investigate:

1. **Cross-county address-format differences.** TAD uses `situs_addr`,
   Collin/Denton use `property_address` like DCAD. Their normalizers
   may produce different keys for same physical address (e.g., TAD has
   ZIP appended in some rows; subdivisions formatted differently).
2. **Direction prefixes** (`N`, `S`, `E`, `W`) — `normalize_addr_key`
   in `api/redfin.py` doesn't currently strip them. Propelio might
   send "4044 Williamsburg Rd" while DCAD has "4044 N Williamsburg Rd"
   (or vice versa).
3. **Apartment/unit suffixes** stripped from Propelio's address but
   present in parcel address.
4. **Bounding-box slack too tight** in `parcel_match.py:_comps_bbox`
   (currently 0.001 deg ~ 110m). Edge comps near the polygon boundary
   might miss parcels stored just outside the slack.
5. **Off-DFW comps** when polygon spillover crosses into uncovered
   counties (Ellis, Kaufman, Rockwall, Johnson, etc.) — those comps
   never have parcel_geom because we don't have their parcel data.
   Those CORRECTLY fall back to dots; only the in-DFW dots are bugs.

**Diagnostic approach (when we get to it):**
- Add a `match_diagnostic` log per pull: list 10 unmatched in-DFW comps
  with their normalized key vs nearest parcel's normalized key, side by
  side. The mismatch pattern should reveal the dominant cause in 2 min.
- Cross-reference the in-polygon lat/lng of each unmatched comp against
  the parcel-table footprint count — if there ARE parcels at that
  location but the join missed, it's a normalizer issue. If no parcels
  are there, it's a coverage gap.

## 🔵 Post-Phase-2 — UX session (deferred, real signal worth its own pass)

KK noted 2026-05-09 that the team has reported the app is **"not very
user friendly"** during testing. Specific cause(s) unknown — could be
search/draw flow steps, sidebar density, color coding, mental model,
filter discoverability. Worth a structured pass:

- Collect the team's actual quotes/notes (not paraphrased)
- Screen-record someone using it cold — find the 3-5 highest-friction moments
- Ranked list of fixes; cluster into a Phase 3 "UX polish" sprint

Related design call (also deferred): **two-tier filter model** when we
expose Propelio pull-time controls (months, radius). Pull-time filters
go behind a small "settings" gear or collapsible section (only power
users like Mike touch them; defaults at 24mo / 7.5mi work for most
team members). Display-time filters (status, sold-within, lot, sqft,
year, price — Chunk D's spec) stay primary and always-visible. The
"Get Comps" pill button uses whatever pull-time settings are in place.
Decision logged to avoid relitigating; UX session validates whether
this layering actually works for the team.

## 🔴 Phase 3.5 — Anchor parcel as Propelio subject (CRITICAL, post-Phase-3)

**KK 2026-05-09 design clarification:** the current polygon flow uses
the parcel **closest to the polygon's centroid** as the Propelio
subject. That's wrong for Mike's actual workflow. He saves a target
property, draws a polygon around it — the **target is the anchor**, not
some random centroid house.

Why it matters: Propelio's CMA returns 100 comps **ranked by relevance
to the subject** (lot size match, age match, proximity, subdivision).
If the subject is a centroid parcel that isn't the user's target, the
100 most-relevant comps are about the wrong house.

**Fix (3 hr work, do AFTER Phase 3 lands):**

1. `saved_areas` schema gets `anchor_parcel_id` TEXT column
2. UI prompts "Which saved parcel is your target?" on area save
3. `/by-polygon` and `/refresh` use anchor_parcel_id → first saved_parcel
   in polygon → centroid fallback
4. Comps now ranked relative to the user's actual target

See SPEC_V3_WORKSPACE.md "DESIGN CLARIFICATION — Workspace = Anchor
Parcel + Polygon" section for full detail.

## 🟢 Active build — Phase 3 (workspace-anchored comps + filters + good/bad)

**Spec:** [`SPEC_V3_WORKSPACE.md`](./SPEC_V3_WORKSPACE.md). Six chunks
for Copilot, ~10 hours of execution. Locked-in design from KK's
brainstorm 2026-05-09:

- **Drop point-in-polygon filter** → show all 100 Propelio comps with
  the polygon mask still applied for visual context (Chunk A)
- **`propelio_comp_archive` table** in the SESSION DB tied to
  `saved_areas` via FK with cascade delete (Chunk B)
- **Append-only smart-merge on refresh** — never delete, only update +
  insert. Combats the 100-cap over time as Propelio's pool churns
- **Two-tier filter card** — API-side (months, range with explicit
  Refresh button) + client-side (lot, sqft, year, price, status — all
  instant, no credit burn) (Chunk C)
- **Status colors** — sold=`#8b5cf6` purple, active=`#dc2626` red,
  pending=`#f59e0b` amber (Chunk A)
- **Good/bad comp tagging** — replaces verify-vacant pattern for
  Propelio comps. Bad → dull color on map, removed from sidebar list,
  skipped in CSV export (Chunks D + E)
- **Sidebar comp list** — only non-bad comps, sortable, click-to-fly,
  hover-to-highlight (Chunk D)
- **Backburner probe of Propelio's 100-cap** — see if pagination breaks
  it (Chunk F, ~30 min)

## ✅ Done — Phase 2 (polygon-driven pulls + purple footprints)

**Spec:** [`SPEC_V2_POLYGON.md`](./SPEC_V2_POLYGON.md). Six chunks (A–F)
for Copilot. Total estimate ~6 hours of Copilot work + ~1 hour of review.

The user draws a polygon → flashy purple "Get Comps" pill button appears
→ click pulls Propelio (one credit, polygon-driven) → results render as
transparent purple glowing parcel footprints (not dots) → sidebar card
with filter strip narrows the 100-pool → save area persists comps to a
per-area archive → reopen area = zero credits.

Key design decisions (settled with KK 2026-05-09):
- Pull-on-button (not auto-pull) — explicit credit burn, no surprises
- Footprint-only render when DB has the parcel; dot fallback when not
- Legacy redfin_sold + active layers stay, default OFF, localStorage persists
- 1:N saved_area → comp_archive with UNIQUE(saved_area_id, comp_address_key)
  per the no-M2M memory rule
- Propelio-purple distinct from existing redfin_sold purple
- Transparent fill so satellite shows underneath
- "Get Comps" button: purple pill, white text, gradient, gentle pulse
  glow matching the parcel overlay

## 🟡 Phase 1 retros + completed work

### CMA auto-generation for newly-created leads (the actual root cause)

**Updated diagnosis 2026-05-09 ~01:30 CDT:** the issue isn't filter
narrowness — it's that Propelio's API doesn't auto-generate the CMA when
we create a new lead via `POST /legacy/leads/withaddress`. Verified by
comparing lead creation timestamps:

| Address | Lead created | Comps returned |
|---|---|---|
| 4044 Williamsburg Rd | 2025-09-10 (8 months ago) | 42 |
| 3710 Elsie Faye Heggins St | 2026-05-09 (brand new) | 0 |
| 5528 Victor St | 2026-05-08 (brand new) | 0 |

Williamsburg is an old lead in Mike's account. Mike (or someone) opened
it in Propelio's web UI back in September, the UI triggered CMA generation,
and that populated CMA has been sitting in Propelio's DB ever since. New
leads we create via API have empty CMAs because the UI's
generation-trigger call never fires.

**This means:** the fix isn't "widen the filter" — it's "find the API
call Propelio's web UI makes when it generates a CMA for a lead."

**Path forward (one focused step):**

1. KK or I open propelio.com → log in → Network tab in dev tools
2. Navigate to a lead that has no CMA yet (the recent Elsie Faye Heggins
   or Victor St leads work — both got created tonight, both empty)
3. Click "Comps" / "CMA" / whatever the lead-details page calls it
4. Watch Network tab for the call that returns ~40 properties — that's
   the missing trigger. Capture URL + method + payload shape.
5. Port to `scraper.py` as a "force generate" step inside `find_lead_id()`,
   immediately after `withaddress` lands the lead_id and before
   `get_cma()` runs.
6. Optionally accept widen kwargs (months, range, lot caps) and pass them
   into that generation call so the same code path also handles widening.

Failed reverse-engineering attempts from the build session (kept here so
we don't repeat them):

| Variant | Result |
|---|---|
| `GET /legacy/cma/{lead_id}?months=24&range=2.0` | 400 — "Invalid longitude argument" |
| `GET /legacy/cma/{lead_id}?lat=&lon=&months=24&range=2.0` | 400 same error |
| `POST /legacy/cma/{lead_id}` with full mirrored params + lat/lon | 500 — generic api_error |
| `PATCH/PUT/POST /legacy/cma/{cma_internal_id}` (using CMA's own id) | HTML error page (wrong path) |
| `POST /legacy/cma` with lead_id in body | HTML page (not the API) |

**Quick free probe before the dev-tools session:** if Mike opens propelio.com
on the empty leads (8344577 for Elsie Faye Heggins, 8344548 for Victor St)
and just clicks through the comps view, Propelio's UI will trigger the
generation call internally. After that, **re-searching those addresses
on our preview should suddenly return comps from the populated CMA.**
That's a one-click verification of the entire theory before any code
work.

### Chunk 4 — Subject popup enrichment

**Problem:** When a user searches an address, we drop ~40 cyan comp pins
on the map and show a CMA chip, but the **subject parcel itself** doesn't
get its existing lot-ledger popup enriched with Propelio's rich subject
data. Propelio gives us full transfer history (every prior owner, every
sale price, every recording date), current loans, valuation estimate,
tax detail, preforeclosure flags. All of this is sitting in
`window._propelioLast.subject` after a search and unused.

**Per Mike's earlier guidance:** keep cyan-pin popups separate from the
existing parcel popups (they're a clear visual signal of "this is
Propelio data, not our DB"). For subject parcel enrichment specifically,
TBD whether it goes:

- (a) into the existing parcel popup (merged), or
- (b) into a separate sidebar panel (better separation), or
- (c) a click-to-toggle "show Propelio data" button on the existing popup

Probably (b) for safety — easier to revert if Mike doesn't like it.

### Chunk 5 — Quota counter

**Problem:** Every fresh search burns 1 of Mike's 500/mo CMA credits.
He has no in-app visibility into his quota — has to log into propelio.com
to check.

**Status:** Half-built. `routes.py` calls `cache.log_quota(balance, ...)`
on every cache miss. `cache.latest_quota_balance()` returns the most
recent value. **But `_extract_balance` in routes.py is currently
returning `null`** because the field isn't where we expected (we tried
`balance`, `remaining`, `remainingConsumables`, `consumablesRemaining`
across `subject_extra`, `valuation`, `raw`, `withaddress`).

**Next:** dump the full Propelio response after a fresh fetch and grep
for the actual quota field. Likely lives in the withaddress response
(we saw `"balance":0` early in the session) or somewhere account-scoped.
Once found:

1. Update `_extract_balance` to read from the right path
2. Add `GET /api/propelio/quota` route that returns
   `{"balance": int, "as_of": iso}` from `cache.latest_quota_balance()`
3. Add a header chip in the frontend that polls on app load + after every
   Propelio fetch
4. Color-code: green > 100, yellow 20–100, red < 20

## 🟡 Polish / round off the edges

### Comp remarks truncation

Currently truncated at 280 chars with `…` suffix and a max-height
scrollable region. Some MLS remarks are very long; KK wanted to "note
that and get it later." Options:
- Add a "Show more" expand toggle in the popup
- Pop a modal with full remarks on click
- Just bump the limit to 600

### Color collision check

Cyan #06b6d4 was chosen for Propelio pulse pins. Saved-parcel outlines
also have cyan-ish accents elsewhere in the app. KK noted the color "may
blend in" but hasn't reported actual confusion. Watch for feedback. If
they collide, switch to magenta (#ec4899) or lime (#84cc16).

### Status-color variants for comps

Currently all Propelio pins are the same cyan, regardless of status
(sold/active/pending). Mike's stated preference: "all same color, manual
selection." But once he uses it for a while he might want sold/active
visually distinguished. Hold this off until he asks.

### `transfer_history` and `raw` are still null on subject

The scraper attaches `parcel_enrichment`, `valuation`, and `cma_*` fields
to subject.extra, but doesn't surface the raw parcel detail's
`transferHistory` array as a structured field. The route's payload has
`subject.transfer_history: null` and `subject.raw: null` placeholders.

Fix: in `scraper.py:search_properties`, after the `parcel_bundle.get('enrichment')`
block, also extract `transferHistory` from the parcel detail (it lives
in the raw parcel response one level above what `_parcel_subject_enrichment`
flattens) and stash it on `subject.extra["transfer_history"]`. Required
for Chunk 4.

## 🟢 Hardening (do before showing to other users)

### Move Cloud Run secrets to Secret Manager

Currently Cloud Run env vars hold `PROPELIO_USERNAME`, `PROPELIO_PASSWORD`,
`DB_PASSWORD`, `SESSION_SECRET`, etc. as plaintext. Same posture lot-ledger
prod uses. For a public-facing tool, move to Secret Manager:

- `gcloud secrets create propelio-password --data-file=...`
- `gcloud run services update lot-ledger-preview --update-secrets=PROPELIO_PASSWORD=propelio-password:latest`
- Repeat for the rest

### Push the standalone CLI to ProLio

`/home/kk/projects/clients/real-estate-comps/` is set up with `git init`
+ `git remote add origin https://github.com/kosmickroma/ProLio.git` but
**never pushed**. Code is local-only. If KK's laptop dies the standalone
CLI work is gone. (The vendored copy in lot-ledger-pro is safe — that
got pushed.)

### Auto-deploy trigger

`lot-ledger-preview` currently requires a manual `gcloud builds submit`
to redeploy. Set up a Cloud Build trigger on `kosmickroma/lot-ledger-pro`
develop branch so pushes auto-deploy:

```
gcloud builds triggers create github \
  --repo-name=lot-ledger-pro \
  --repo-owner=kosmickroma \
  --branch-pattern=^develop$ \
  --build-config=cloudbuild-preview.yaml
```

Saves the manual step. Mike can then push from the GitHub UI to ship a
hotfix.

## 🔵 Future / "nice to have" ideas

### CSV export with full Propelio fields

Lot-ledger's existing CSV download includes 10 inline Redfin sold-comp
columns. Add a parallel set of Propelio-driven columns when comps were
fetched: actual close price, close date, DOM, MLS, list price delta, etc.
Could either replace the Redfin columns or coexist (clearly labeled).

### Save Propelio comp lists per-target

Mike picks 3–5 comps manually for a target → saves as a "comp set" tied
to the target parcel. Lives in DB. Recallable later for the same parcel.
Could feed an Excel report (resurrects `output.py` from the standalone
ProLio repo — it has `arv_summary` + `generate_excel`).

### Compare-two-addresses side-by-side

Search two addresses → render both subjects → render both comp pools as
two distinguishable colors → see overlapping comps. Useful when Mike's
deciding between two targets in adjacent neighborhoods.

### Recency weighting on comps

Right now the popup shows DOM and close date but the user has to mentally
weight "180 days ago" vs "30 days ago." A small visual indicator (e.g.,
opacity proportional to recency) would help eyeballing.

## ✅ Done (last session)

- [x] Reframe scope: drop Viktor's algorithm, vendor only the scraper
- [x] Move standalone CLI from `Downloads/` to `projects/clients/real-estate-comps/`
- [x] Migrate plaintext creds to `.env` + `python-dotenv`
- [x] Disable proxy (was returning 407 with dead creds)
- [x] Set up `lot-ledger-pro` as a clone of lot-ledger develop
- [x] Vendor `scraper.py` + `config.py` into `api/propelio/`; relative imports
- [x] Chunk 1 — backend route + cache + quota log (Copilot built per spec)
- [x] Strip the wasteful CMA probe in `scraper.py:find_lead_id` (saves 1 credit/call)
- [x] Local boot + smoke tests
- [x] Chunks 2 + 3 — frontend pulse pins + popup
- [x] Cloud Run preview deploy via existing `cloudbuild-preview.yaml` pipeline
- [x] Add `requests` to requirements.txt (was missing on Cloud Run)
- [x] Polish: richer popups (DOM, list_price, close_date, garage,
      property_type, baths breakdown, MLS update timestamp, marketing remarks)
- [x] Polish: bottom-left CMA settings chip with Propelio's filter +
      Propelio's own ARV
- [x] Polish: friendly empty-result UX with three known-active test addresses
- [x] Subject neighborhood / lot_size / sqft / year_built fall back to
      `parcel_enrichment` when the CMA's top-level subject is sparse
- [x] Push commit `837a421` to `kosmickroma/lot-ledger-pro` develop
- [x] Cookie-staleness diagnosis on Windows machine (no code change needed)
