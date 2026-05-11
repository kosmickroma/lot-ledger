# Propelio Integration — Copilot Build Spec

> Six-chunk integration plan. Each chunk is independently testable.
> Run them in order. Stop and run the smoke test at the end of each chunk
> before moving to the next.

## Context

A vendored Propelio API client already lives at `api/propelio/`:

- `api/propelio/scraper.py` — HTTP client (login, parcel suggest, parcel detail, lead creation, CMA pull)
- `api/propelio/config.py` — `.env`-driven config (`PROPELIO_USERNAME`, `PROPELIO_PASSWORD`, `PROPELIO_PROXY_URL`)
- `api/propelio/__init__.py` — empty placeholder

The scraper already works end-to-end: `search_properties(address: str)` returns `(subject_property, [comp_properties])` after a single CMA call. **Do not rewrite the scraper.** You'll consume it.

The lot-ledger app uses FastAPI + Postgres (psycopg2) + Leaflet frontend. DB connections come from `api.config.get_conn()` and `api.config.release_conn()`. See `api/sold.py` for a clean example of querying patterns.

## Goal

Per-address comp pulls. User searches an address → backend hits Propelio → returns subject parcel detail + ~40 nearby comps → frontend renders comps as same-color pins on the map and the subject popup gains rich Propelio data (transfer history, owners, loans, valuation, tax). Algorithm-driven scoring is **out of scope** — the user picks comps manually.

The Propelio account has a **500 CMA/month quota**. Every cache miss costs 1 credit. Every cache hit costs 0. Cache TTL: **7 days**.

---

## Chunk 1 — Backend route + cache + balance logger

**Files to create:**

- `api/propelio/routes.py` — FastAPI router
- `api/propelio/cache.py` — cache + quota helpers + table creation

**Files to modify:**

- `api/propelio/scraper.py` — strip the wasteful CMA probe (saves 1 credit per call)
- `api/main.py` — register the new router at startup

**Files to NOT touch:**

- `api/propelio/config.py`
- `api/sold.py`, `api/redfin.py`, `api/auth.py`
- Anything in `frontend/`
- Anything in `api/counties/`

### 1.1 — Strip the CMA probe in `scraper.py`

Inside `find_lead_id()` (search the file for `_legacy_cma_probe` or `CMA probe`), there is a probe call to `GET /legacy/cma/{lead_id}` that runs purely to verify CMA availability. The full CMA pull happens later in `get_cma()`. The probe duplicates that call and burns an extra credit.

Remove the probe call and any logging that goes with it. Keep the surrounding code intact. The function should still return `(lead_id, subject_lot_sqft, parcel_bundle)` exactly as before.

### 1.2 — `api/propelio/cache.py`

Two Postgres tables, both auto-created on first import via an `ensure_tables()` function called at module load:

```sql
CREATE TABLE IF NOT EXISTS propelio_cache (
    address_key TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS propelio_quota_log (
    id          SERIAL PRIMARY KEY,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    balance     INTEGER,
    address_key TEXT
);

CREATE INDEX IF NOT EXISTS propelio_quota_log_fetched_idx
    ON propelio_quota_log (fetched_at DESC);
```

Use `api.config.get_conn()` / `release_conn()` for connection management. Match the pattern in `api/sold.py`.

**Functions to expose:**

```python
CACHE_TTL_DAYS = 7

def normalize_address_key(raw: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation. Use this
    consistently for cache keys. There's an existing `normalize_addr_key`
    in `api/redfin.py` — reuse it if it fits, otherwise mirror its style."""

def get_cached(address_key: str) -> dict | None:
    """Return cached payload if fetched within CACHE_TTL_DAYS, else None."""

def put_cached(address_key: str, payload: dict) -> None:
    """Upsert cache. Last writer wins."""

def log_quota(balance: int | None, address_key: str) -> None:
    """Insert a row into propelio_quota_log."""

def latest_quota_balance() -> int | None:
    """Return the most recent balance value in the last 30 days, or None."""

def ensure_tables() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS. Called once at module import."""
```

Call `ensure_tables()` at the bottom of the module so it runs on first import.

### 1.3 — `api/propelio/routes.py`

```python
"""FastAPI router for the Propelio per-address comp endpoint."""
from fastapi import APIRouter, HTTPException, Query
import logging
import asyncio

from . import cache as cache_mod
from . import scraper as scraper_mod

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/propelio", tags=["propelio"])
```

**One endpoint:**

```
GET /api/propelio/by-address?address=<raw user address>
```

Behavior:

1. Compute `address_key = cache_mod.normalize_address_key(address)`.
2. `cached = cache_mod.get_cached(address_key)` — if hit, return immediately with `{"cached": true, ...payload}`.
3. On miss, call the scraper. The scraper is synchronous; wrap it in `asyncio.to_thread(...)` so the FastAPI event loop isn't blocked.
4. The scraper returns `(subject, comps_list)` — `subject` is a `Property` dataclass, `comps_list` is `List[Property]`.
5. Build the response payload (see "Response shape" below). Convert `Property` dataclasses to dicts via `asdict()`.
6. Extract the `balance` field from `subject.extra` if present (the scraper currently surfaces it under `valuation` or the raw withaddress response — check both). Call `cache_mod.log_quota(balance, address_key)`.
7. `cache_mod.put_cached(address_key, payload)`.
8. Return payload with `{"cached": false, ...}`.

**Response shape:**

```json
{
  "cached": false,
  "fetched_at": "2026-05-09T01:30:00Z",
  "balance": 42,
  "subject": {
    "address": "4044 Williamsburg Rd, Dallas, TX 75220",
    "lot_size": 10139,
    "sqft": 1413,
    "year_built": 1952,
    "neighborhood": "Glenridge Estates 3",
    "lat": 32.878624,
    "lon": -96.844657,
    "parcel_enrichment": { ... full parcel detail ... },
    "valuation": { ... estimate, low, high ... },
    "transfer_history": [ ... if available ... ],
    "raw": { ... full Propelio property block, kept for chunks 4+ ... }
  },
  "comps": [
    {
      "address": "...",
      "price": 665000,
      "lot_size": 7800,
      "sqft": null,
      "year_built": null,
      "status": "sold",
      "neighborhood": "Glenridge Estates",
      "lat": 32.87,
      "lon": -96.84,
      "extra": { "sold_date": "...", "dom": null, "mls": null }
    },
    ...
  ]
}
```

Don't filter or score the comps. Return everything the scraper returned.

**Error handling:**

| Scraper raises / returns | HTTP response |
|---|---|
| `PropelioScraperError` (auth/network) | `503 {"detail": "Propelio service unavailable", "error": str(e)}` |
| `ValueError` (address not found) | `404 {"detail": "Address not resolvable on Propelio"}` |
| Empty comps list | Return 200 with empty `comps` array (not an error) |
| Anything else | `500 {"detail": "Unexpected error", "error": str(e)}` and `logger.exception(...)` |

### 1.4 — Wire the router into `api/main.py`

Find where other routers are included (search for `app.include_router` or `from .auth`). Add:

```python
from api.propelio.routes import router as propelio_router
app.include_router(propelio_router)
```

Place it next to the other router includes. **Do not** modify any other route or import.

### 1.5 — Chunk 1 smoke test

1. Make sure `.env` in lot-ledger-pro/ has real `PROPELIO_USERNAME` and `PROPELIO_PASSWORD` values (copy from `/home/kk/projects/clients/real-estate-comps/.env` if needed).
2. From repo root with venv active: `uvicorn api.main:app --reload`
3. In a second terminal:
   ```bash
   curl -s "http://localhost:8000/api/propelio/by-address?address=4044+Williamsburg+Rd%2C+Dallas%2C+TX+75220" | head -50
   ```
4. Expect: 200 response, `cached: false`, subject with neighborhood "Glenridge Estates 3", ~40 comps in the array.
5. Run the same curl again immediately. Expect: 200 response, `cached: true`, response time under 100ms.
6. Check the `propelio_quota_log` table has one row with the balance value.

If all six pass, Chunk 1 is done. **Stop and report.**

---

## Chunk 2 — Frontend "Get Comps" button on parcel popup

*Detailed spec follows after Chunk 1 lands. Brief outline:*

- Add a "Get Comps & ARV" button to the parcel popup in `frontend/map.js` (find the existing parcel popup builder)
- Click handler fires `fetch('/api/propelio/by-address?address=' + encodeURIComponent(parcel.property_address))`
- Loading state while waiting (button text → "Loading...", disabled)
- Success → store result on `window._propelioLast` for chunks 3 & 4 to consume; trigger a custom event `propelio:loaded` with the payload
- Error → toast notification with the error detail

## Chunk 3 — Render comps as same-color pins

*Detailed spec follows. Brief outline:*

- Listen for `propelio:loaded` event
- Clear any previous Propelio layer
- Render each comp as a same-color pin (decide a distinct color from existing layers — suggest `#9333ea` purple, all comps identical styling)
- Pin click → popup with comp details (price, sqft, lot, year, status, sold_date, MLS)
- Subject parcel gets a special marker (gold, larger)
- Layer toggles into existing layer-control sidebar

## Chunk 4 — Subject popup enrichment

*Detailed spec follows. Brief outline:*

- Extend the existing parcel popup to surface fields from `propelio.subject.parcel_enrichment` and `propelio.subject.raw`:
  - Transfer history (sortable list of prior owners + sale dates + amounts)
  - Current loans (lender, amount, type, recording date)
  - Valuation estimate (value, low, high, confidence rating)
  - Tax detail
  - Owner contact info if available
- Render in collapsible sections to keep the popup compact

## Chunk 5 — Quota counter in header

*Detailed spec follows. Brief outline:*

- Header chip: "X / 500 CMAs this month"
- Reads from `GET /api/propelio/quota` (new endpoint, returns `{"balance": int, "as_of": ISO}`)
- Polled on app load and after every Propelio fetch
- Yellow at <100 remaining, red at <20

## Chunk 6 — Optional radius slider

*Detailed spec follows. Brief outline:*

- Slider on the parcel popup or a dedicated control panel
- Default 0.5mi, options 0.25 / 0.5 / 0.75 / 1.0
- 0.25 filters the existing returned pool client-side (no extra credit burn)
- 1.0+ requires a fresh fetch with `?radius=1.0` query param → backend passes through to scraper

---

## Constraints across all chunks

- **No algorithm/scoring.** All comps render same color. No top-3, no confidence, no ARV math.
- **No pushes / commits without explicit user approval.** The user runs git.
- **Stop at the end of each chunk and run the smoke test.** Wait for review before continuing.
- **Don't touch existing routes, layers, or behavior** unless a chunk explicitly says to.
- **Match the codebase's style** — read 2-3 nearby files first to see the conventions (logging, error handling, type hints).
