# api/main.py
#
# FastAPI application entry point. Defines all HTTP routes and mounts the
# frontend as static files. Validates credentials at startup so the app
# fails loudly if misconfigured rather than on first user request.
#
# Connects to:
#   api/config.py  — startup validation and database connection helpers
#   api/dcad.py    — parcel queries, classification logic, feature builders
#   api/redfin.py  — async Redfin active listing pull
#   api/geo.py     — polygon bbox helper for Redfin query bounds
#   frontend/      — served as static files at root /

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.config import get_conn, get_settings, release_conn
from api.dcad import build_feature, classify_parcel, query_parcels
from api.geo import polygon_bbox
from api.redfin import normalize_addr_key, pull_grid
from api.tad import query_tad_parcels, _classify_tad


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
_job_store: dict[str, dict[str, Any]] = {}
_JOB_TTL_SECONDS = 1800    # 30-minute TTL per session
_JOB_MAX = 50              # max jobs held in memory at once
_REDFIN_ROW_THRESHOLD = 15_000  # auto-disable Redfin above this parcel count

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _evict_stale_jobs() -> None:
    """Remove expired jobs then trim to _JOB_MAX (evict oldest first)."""
    now = time.monotonic()
    expired = [
        jid for jid, job in _job_store.items()
        if now - job.get("created_at", 0) > _JOB_TTL_SECONDS
    ]
    for jid in expired:
        _job_store.pop(jid, None)
    while len(_job_store) >= _JOB_MAX:
        oldest = min(_job_store, key=lambda jid: _job_store[jid].get("created_at", 0))
        _job_store.pop(oldest, None)


def _get_job(job_id: str) -> dict[str, Any] | None:
    """Return job if it exists and has not expired; evicts on TTL miss."""
    job = _job_store.get(job_id)
    if job is None:
        return None
    if time.monotonic() - job.get("created_at", 0) > _JOB_TTL_SECONDS:
        _job_store.pop(job_id, None)
        return None
    return job


class AnalyzeRequest(BaseModel):
    polygon: list[list[float]]
    include_redfin: bool = False


class VerificationRequest(BaseModel):
    verifications: dict[str, str] = {}
    potential_targets: dict[str, str] = {}


class CacheJobRequest(BaseModel):
    rows: list[dict[str, Any]]
    redfin_data: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}


def _normalize_csv_filename(raw: str | None) -> str:
    if not raw:
        return "parcels.csv"

    base = raw.strip().replace("/", " ").replace("\\", " ")
    base = _FILENAME_SAFE_RE.sub("_", base).strip("._ ")
    if not base:
        return "parcels.csv"

    if base.lower().endswith(".csv"):
        stem = base[:-4].rstrip("._ ")
    else:
        stem = base

    if not stem:
        stem = "parcels"

    stem = stem[:96]
    return f"{stem}.csv"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _google_maps_link(row: dict[str, Any]) -> str:
    street_num = str(row.get("street_num", "") or "").strip()
    full_street_name = str(row.get("full_street_name", "") or "").strip()
    zip_code = str(row.get("property_zip", "") or "").strip()[:5]
    query = f"{street_num}+{full_street_name.replace(' ', '+')},+Dallas+TX+{zip_code}"
    query = query.replace("++", "+")
    return f"https://maps.google.com/?q={query}"


# Validate required runtime settings at startup.
get_settings()

app = FastAPI(title="LotLedger")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/hoa")
async def hoa_boundaries() -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT objectid, asso_name, asso_web, status,
                       ST_AsGeoJSON(geom)::json AS geometry
                FROM hoa_boundaries
                ORDER BY asso_name
                """
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)

    features = []
    for row in rows:
        geom = row.pop("geometry", None)
        if geom is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "name": row.get("asso_name") or "",
                "url": row.get("asso_web") or "",
                "status": row.get("status") or "",
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/health/db")
async def health_db_check() -> dict[str, str]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if conn is not None:
            release_conn(conn)
    return {"status": "ok", "db": "ok"}


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    polygon = request.polygon
    include_redfin = bool(request.include_redfin)
    if len(polygon) < 3:
        raise HTTPException(status_code=400, detail="Polygon must have at least 3 points")

    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)

    redfin_data: dict[str, dict] = {}

    # Start Redfin task (optional) but run county DB queries sequentially.
    # This avoids intermittent psycopg2 ThreadedConnectionPool races seen under
    # concurrent thread execution in mixed-county pulls.
    redfin_task = None
    if include_redfin:
        redfin_task = asyncio.create_task(pull_grid(min_lng, min_lat, max_lng, max_lat))

    dcad_result = None
    tad_result = None
    redfin_fetch_ok = False
    failed_sources: list[str] = []

    try:
        dcad_result = await asyncio.to_thread(query_parcels, polygon)
    except Exception:
        failed_sources.append("DCAD")

    try:
        tad_result = await asyncio.to_thread(query_tad_parcels, polygon)
    except Exception:
        failed_sources.append("TAD")

    # Never return silent partial county coverage; fail loudly instead.
    # Cancel any pending Redfin task before raising to avoid a dangling coroutine.
    if failed_sources:
        if redfin_task is not None and not redfin_task.done():
            redfin_task.cancel()
        raise HTTPException(
            status_code=502,
            detail=f"County query failed for: {', '.join(failed_sources)}. No partial results returned.",
        )

    # Merge rows from all counties, deduplicating by (account_num, county).
    all_rows: list[dict[str, Any]] = []
    exempt_set: set[str] = set()
    if dcad_result:
        all_rows.extend(dcad_result.parcels)
        exempt_set.update(dcad_result.exempt_accounts)
    if tad_result:
        all_rows.extend(tad_result.parcels)

    if not all_rows:
        if redfin_task is not None and not redfin_task.done():
            redfin_task.cancel()
        raise HTTPException(status_code=500, detail="Parcel query failed for all counties")

    # Auto-disable Redfin for large area draws — prevents timeouts and memory pressure.
    redfin_skipped = False
    if redfin_task is not None and len(all_rows) > _REDFIN_ROW_THRESHOLD:
        if not redfin_task.done():
            redfin_task.cancel()
        redfin_skipped = True
    elif redfin_task is not None:
        try:
            redfin_data = await redfin_task or {}
            redfin_fetch_ok = True
        except Exception:
            redfin_data = {}

    rows = all_rows
    features: list[dict[str, Any]] = []
    counts = {
        "active": 0,
        "off_market": 0,
        "multifamily": 0,
        "vacant": 0,
        "commercial": 0,
        "exempt": 0,
        "total": len(rows),
    }

    for row in rows:
        parcel_key = str(row.get("parcel_key", "") or "")
        account_num = str(row.get("account_num", "") or "")
        direct_match = parcel_key == account_num if parcel_key else True
        addr_key = normalize_addr_key(str(row.get("property_address", "") or ""))
        on_redfin = addr_key in redfin_data and direct_match
        redfin_listing = redfin_data.get(addr_key) if on_redfin else None
        # TAD rows carry division_cd="TAD" — use TAD classifier; DCAD rows use existing classifier.
        if row.get("division_cd") == "TAD":
            prop_type = _classify_tad(row)
        else:
            prop_type = classify_parcel(row, exempt_set)

        if on_redfin:
            counts["active"] += 1
        elif prop_type == "multifamily":
            counts["multifamily"] += 1
        elif prop_type == "vacant":
            counts["vacant"] += 1
        elif prop_type == "commercial":
            counts["commercial"] += 1
        elif prop_type == "exempt":
            counts["exempt"] += 1
        else:
            counts["off_market"] += 1

        try:
            feature = build_feature(row, prop_type, on_redfin, redfin_listing)
            features.append(feature)
        except ValueError:
            continue

    _evict_stale_jobs()
    job_id = str(uuid.uuid4())
    _job_store[job_id] = {
        "rows": rows,
        "redfin_data": redfin_data,
        "created_at": time.monotonic(),
    }
    return {
        "type": "FeatureCollection",
        "features": features,
        "counts": counts,
        "job_id": job_id,
        "redfin_requested": include_redfin,
        "redfin_ok": redfin_fetch_ok,
        "redfin_skipped": redfin_skipped,
        "source_status": {
            "dcad_ok": dcad_result is not None,
            "tad_ok": tad_result is not None,
        },
    }


@app.post("/api/cache-job")
async def cache_job(request: CacheJobRequest) -> dict[str, str]:
    """Create and store a job from merged/tiled results. Returns job_id for download/export."""
    _evict_stale_jobs()
    job_id = str(uuid.uuid4())
    _job_store[job_id] = {
        "rows": request.rows,
        "redfin_data": request.redfin_data,
        "created_at": time.monotonic(),
    }
    return {"job_id": job_id}


@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest) -> dict[str, Any]:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    verifications = request.verifications or {}
    potential_targets = request.potential_targets or {}
    updates = 0

    for row in rows:
        account_num = str(row.get("account_num", "") or "").strip()
        raw_value = str(verifications.get(account_num, "") or "").strip().lower()
        if raw_value == "yes":
            normalized = "Yes"
        elif raw_value == "no":
            normalized = "No"
        else:
            normalized = ""

        if row.get("verified_vacant") != normalized:
            row["verified_vacant"] = normalized
            updates += 1

        potential_raw = str(potential_targets.get(account_num, "") or "").strip().lower()
        potential_value = "Yes" if potential_raw in {"1", "true", "yes", "y"} else ""
        if row.get("potential_target") != potential_value:
            row["potential_target"] = potential_value
            updates += 1

    return {"ok": True, "updated": updates}


@app.get("/api/download/{job_id}")
async def download(job_id: str, filename: str | None = None) -> StreamingResponse:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    redfin_data: dict[str, dict] = job.get("redfin_data", {})

    download_name = _normalize_csv_filename(filename)

    def generate_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "Property Address",
                "MLS Status",
                "Owner Name",
                "Owner Mailing Address",
                "Owner City",
                "Owner State",
                "Owner Zip",
                "Land Value",
                "Improvement Value",
                "Total Value",
                "Redfin List Price",
                "Land % of Total",
                "Year Built",
                "Living Area (sq ft)",
                "Total Structure Area (sq ft)",
                "State Code",
                "Zoning",
                "Lot Size (sq ft)",
                "Lot Size (acres)",
                "Frontage (ft)",
                "Depth (ft)",
                "School District",
                "Neighborhood Code",
                "Subdivision",
                "Legal Description",
                "Latitude",
                "Longitude",
                "Google Maps Link",
                "Verified Vacant",
                "Potential Target",
                "HOA",
                "HOA URL",
            ]
        )
        buffer.seek(0)
        yield buffer.getvalue()
        buffer.truncate(0)
        buffer.seek(0)

        sorted_rows = sorted(rows, key=lambda r: str(r.get("property_address", "") or ""))
        for row in sorted_rows:
            parcel_key = str(row.get("parcel_key", "") or "")
            account_num = str(row.get("account_num", "") or "")
            direct_match = parcel_key == account_num if parcel_key else True
            addr_key = normalize_addr_key(str(row.get("property_address", "") or ""))
            on_redfin = addr_key in redfin_data and direct_match
            redfin_listing = redfin_data.get(addr_key) if on_redfin else None

            land_val = row.get("land_val")
            impr_val = row.get("impr_val")
            tot_val = row.get("tot_val")
            land_pct = row.get("land_pct")
            area_size = row.get("area_size")
            lot_acres = round(area_size / 43560, 3) if _safe_float(area_size) is not None else ""
            yr_built = row.get("yr_built")
            living_area = row.get("tot_living_area")
            main_area = row.get("tot_main_sf")
            legal_desc = " ".join(
                [
                    str(row.get("legal1", "") or "").strip(),
                    str(row.get("legal2", "") or "").strip(),
                    str(row.get("legal3", "") or "").strip(),
                    str(row.get("legal4", "") or "").strip(),
                    str(row.get("legal5", "") or "").strip(),
                ]
            ).strip()

            writer.writerow(
                [
                    row.get("property_address", ""),
                    "Active" if on_redfin else "Off Market",
                    row.get("owner_name", ""),
                    row.get("owner_address", ""),
                    row.get("owner_city", ""),
                    row.get("owner_state", ""),
                    row.get("owner_zip", ""),
                    round(_safe_float(land_val), 0) if _safe_float(land_val) is not None else "",
                    round(_safe_float(impr_val), 0) if _safe_float(impr_val) is not None else "",
                    round(_safe_float(tot_val), 0) if _safe_float(tot_val) is not None else "",
                    redfin_listing["price"] if redfin_listing and redfin_listing.get("price") else "",
                    round(_safe_float(land_pct), 1) if _safe_float(land_pct) is not None else "",
                    int(yr_built) if _safe_float(yr_built) not in (None, 0.0) else "",
                    int(_safe_float(living_area)) if _safe_float(living_area) not in (None, 0.0) else "",
                    int(_safe_float(main_area)) if _safe_float(main_area) not in (None, 0.0) else "",
                    row.get("state_code", "") or row.get("sptd_code", ""),
                    row.get("zoning", "") or "",
                    round(_safe_float(area_size), 0) if _safe_float(area_size) is not None else "",
                    lot_acres,
                    int(_safe_float(row.get("front_dim"))) if _safe_float(row.get("front_dim")) not in (None, 0.0) else "",
                    int(_safe_float(row.get("depth_dim"))) if _safe_float(row.get("depth_dim")) not in (None, 0.0) else "",
                    row.get("isd_desc", "") or "",
                    row.get("nbhd_cd", "") or "",
                    row.get("legal1", "") or "",
                    legal_desc,
                    row.get("lat", "") or "",
                    row.get("lng", "") or "",
                    _google_maps_link(row),
                    row.get("verified_vacant", "") or "",
                    row.get("potential_target", "") or "",
                    (
                        row.get("hoa_name", "")
                        or ("N/A (Tarrant HOA not loaded)" if row.get("division_cd") == "TAD" else "")
                    ),
                    row.get("hoa_url", "") or "",
                ]
            )
            buffer.seek(0)
            yield buffer.getvalue()
            buffer.truncate(0)
            buffer.seek(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")