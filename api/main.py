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
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.config import get_conn, get_settings, release_conn
from api.dcad import build_feature, classify_parcel, query_parcels, summarize_counts
from api.geo import polygon_bbox
from api.redfin import pull_grid


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
_job_store: dict[str, list[dict[str, Any]]] = {}


class AnalyzeRequest(BaseModel):
    polygon: list[list[float]]

# Validate required runtime settings at startup.
get_settings()

app = FastAPI(title="LotLedger")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


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
    if len(polygon) < 3:
        raise HTTPException(status_code=400, detail="Polygon must have at least 3 points")

    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)

    redfin_addresses: set[str] = set()
    try:
        parcel_result, redfin_addresses = await asyncio.gather(
            asyncio.to_thread(query_parcels, polygon),
            pull_grid(min_lng, min_lat, max_lng, max_lat),
        )
    except Exception:
        try:
            parcel_result = await asyncio.to_thread(query_parcels, polygon)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = parcel_result.parcels
    exempt_set = parcel_result.exempt_accounts
    features: list[dict[str, Any]] = []

    for row in rows:
        parcel_key = str(row.get("parcel_key", "") or "")
        account_num = str(row.get("account_num", "") or "")
        direct_match = parcel_key == account_num if parcel_key else True
        on_redfin = str(row.get("property_address", "") or "") in redfin_addresses and direct_match
        prop_type = classify_parcel(row, exempt_set)
        if prop_type == "exempt" and not on_redfin:
            continue
        try:
            feature = build_feature(row, prop_type, on_redfin)
            features.append(feature)
        except ValueError:
            continue

    job_id = str(uuid.uuid4())
    _job_store[job_id] = rows
    counts = summarize_counts(rows, exempt_set, redfin_addresses)

    return {
        "type": "FeatureCollection",
        "features": features,
        "counts": counts,
        "job_id": job_id,
        "redfin_ok": len(redfin_addresses) > 0,
    }


@app.get("/api/download/{job_id}")
async def download(job_id: str) -> StreamingResponse:
    rows = _job_store.get(job_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Job not found")

    def generate_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "Account",
                "Status",
                "Address",
                "Owner",
                "Land Val",
                "Impr Val",
                "Tot Val",
                "Land %",
                "Lot Size (sq ft)",
                "Lot Size (acres)",
                "Frontage (ft)",
                "Depth (ft)",
                "Year Built",
                "Living Area (sq ft)",
                "Zoning",
                "School District",
                "Neighborhood",
                "Legal",
            ]
        )
        buffer.seek(0)
        yield buffer.getvalue()
        buffer.truncate(0)
        buffer.seek(0)

        for row in rows:
            land_val = row.get("land_val")
            impr_val = row.get("impr_val")
            tot_val = row.get("tot_val")
            land_pct = row.get("land_pct")
            area_size = row.get("area_size")
            writer.writerow(
                [
                    row.get("account_num", ""),
                    row.get("sptd_code", ""),
                    row.get("property_address", ""),
                    row.get("owner_name", ""),
                    f"{land_val:,.0f}" if land_val is not None else "",
                    f"{impr_val:,.0f}" if impr_val is not None else "",
                    f"{tot_val:,.0f}" if tot_val is not None else "",
                    f"{land_pct:.1f}" if land_pct is not None else "",
                    f"{area_size:,.0f}" if area_size is not None else "",
                    f"{area_size / 43560:.3f}" if area_size is not None else "",
                    f"{int(row['front_dim'])}" if row.get("front_dim") else "",
                    f"{int(row['depth_dim'])}" if row.get("depth_dim") else "",
                    row.get("yr_built", "") or "",
                    f"{int(row['tot_living_area']):,}" if row.get("tot_living_area") else "",
                    row.get("zoning", "") or "",
                    row.get("isd_desc", "") or "",
                    row.get("nbhd_cd", "") or "",
                    row.get("legal1", "") or "",
                ]
            )
            buffer.seek(0)
            yield buffer.getvalue()
            buffer.truncate(0)
            buffer.seek(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=parcels.csv"},
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")