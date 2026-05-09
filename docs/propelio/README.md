# Propelio Integration — Documentation Index

Living documentation for the Propelio comp integration in `lot-ledger-pro`.

## What this is

`lot-ledger-pro` is a fork-style playground of `lot-ledger` (cloned from
`develop`) where we're building a per-address Propelio CMA integration.
The host app is otherwise identical — same FastAPI backend, same Leaflet
frontend, same Cloud SQL.

The integration's defining moment was discovering that `redfin_sold.sold_price`
in our existing pipeline is a **Redfin estimate**, not an MLS-actual close
price (Texas is a non-disclosure state). Propelio uses their broker's MLS
license to get the real numbers. So Propelio is the source of truth for
sold-comp pricing in this app.

## Doc index

| File | Purpose |
|---|---|
| [`SESSION_2026-05-08.md`](./SESSION_2026-05-08.md) | Extreme-detail log of the build session that got Phase 1 (cyan dots) shipped |
| [`ROADMAP.md`](./ROADMAP.md) | What's next — open chunks, polish, hardening, future ideas |
| [`SPEC.md`](./SPEC.md) | Phase 1 spec — original 6-chunk Copilot build (cyan dots, address-driven). Chunks 1–3 done. Cyan render gets retired in Phase 2. |
| [`SPEC_V2_POLYGON.md`](./SPEC_V2_POLYGON.md) | **Phase 2 spec — polygon-driven pulls + purple footprint render.** Active build. Six chunks for Copilot. |

## Quick reference

- **Live preview URL:** `https://lot-ledger-preview-505466930182.us-central1.run.app`
- **Validated test address:** `4044 Williamsburg Rd, Dallas, TX 75220`
  (returns 42 comps · ARV ~$723k from 3 sold; matches Viktor's CLI reference)
- **Validated test addresses (other active markets):**
  `6710 Northport Dr, Dallas, TX 75230`,
  `9012 Hunters Creek Dr, Dallas, TX 75243`,
  `2929 Crestmoore Cir, Dallas, TX 75205`
- **Known empty-result address:** `5528 Victor St, Dallas, TX` (low-velocity
  market, demonstrates the friendly empty-CMA chip)

## Critical files in this repo

| Path | Role |
|---|---|
| `api/propelio/scraper.py` | Vendored Propelio HTTP client (login, parcel suggest, parcel detail, withaddress, CMA) |
| `api/propelio/config.py` | `.env`-driven config (PROPELIO_USERNAME / PROPELIO_PASSWORD / PROPELIO_PROXY_URL) |
| `api/propelio/cache.py` | 7-day Postgres cache + quota log; auto-creates tables on first import |
| `api/propelio/routes.py` | FastAPI router: `GET /api/propelio/by-address?address=...` |
| `api/main.py` | `app.include_router(propelio_router)` (line 1114) |
| `frontend/map.js` | `propelioCompLayer`, `firePropelioFetch`, `_propelioBuildPopup`, `propelioCmaChip`, hooks in `selectSuggestion` and `doSearch` |
| `frontend/style.css` | `.propelio-pulse-marker` cyan glow keyframe (mirrors `.saved-parcel-glow` pattern) |
| `.env.example` | Template; copy to `.env` and fill in real values locally |

## Source repo (standalone Propelio CLI)

The vendored scraper code originates from a standalone CLI at
`/home/kk/projects/clients/real-estate-comps/`, intended to be pushed to
`https://github.com/kosmickroma/ProLio.git`. That CLI also contains the
`comp_engine.py` (algorithmic scoring) and `output.py` (Excel ARV report)
which we deliberately **did not vendor** here — Mike prefers raw data and
manual selection over algorithmic ranking. Those files survive in ProLio
for future use cases (lender-deliverable workbooks, batch ARV exports).
