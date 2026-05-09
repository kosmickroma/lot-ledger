# Propelio Integration — Roadmap

What's next, ranked roughly by value × effort. Items in **Open** are
unfinished. Items in **Done** are checked off and kept here for reference
context.

## 🔴 Highest priority (blocks Mike's actual workflow)

### Filter widening — get any address to return comps

**Problem:** Propelio's account-default CMA filter is narrow (~0.5mi,
6mo, lot ≤ subject's acreage). Active markets fit; quiet ones don't.
Currently we have zero override capability — we just pass through whatever
Propelio's auto-CMA returned.

**Two paths, do whichever lands first:**

- **Path A — change Propelio's account defaults (zero engineering).**
  Mike (or KK on his behalf) logs into propelio.com → CMA settings →
  widens defaults globally. Months 6 → 24. Range 0.5 → 2.0. Loosen the
  lot-size band. Those defaults flow into every API call we make from
  his account. **Try this first** — could solve the problem without code.
- **Path B — reverse-engineer Propelio's filter API.**
  Open browser dev tools on propelio.com while adjusting CMA filter
  sliders. Network tab will show the exact PATCH/POST shape they use to
  mutate a CMA's params. Mirror that in `scraper.py` as a `widen_params=...`
  kwarg. Pass through from the route as `?radius=&months=&...` query
  params. Tested-failed shapes are already documented in
  [`SESSION_2026-05-08.md`](./SESSION_2026-05-08.md) Phase 6.

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
