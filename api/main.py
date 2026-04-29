# api/main.py
#
# FastAPI application entry point. Defines all HTTP routes and mounts the
# frontend as static files. Validates credentials at startup so the app
# fails loudly if misconfigured rather than on first user request.
#
# Connects to:
#   api/config.py  — startup validation and Supabase client
#   api/dcad.py    — parcel queries, classification logic  (Phase 4)
#   api/redfin.py  — async Redfin active listing pull      (Phase 4)
#   frontend/      — served as static files at root /

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.config import get_settings, supabase


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Validate required runtime settings at startup.
get_settings()

app = FastAPI(title="LotLedger")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
async def health_db_check() -> dict[str, str]:
    try:
        # Lightweight query confirms Supabase auth and DB connectivity.
        supabase.table("parcels").select("account_num").limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "db": "ok"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")