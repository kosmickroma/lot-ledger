# api/config.py
#
# Environment and Supabase client setup. Single source of truth for all
# credentials and runtime settings. Raises clearly at startup if anything
# is missing — never fails silently mid-request.
#
# Connects to:
#   api/main.py    — imports get_settings() (startup validation) and supabase client
#   api/dcad.py    — imports supabase client for all parcel queries  (Phase 4)
#   scripts/build_db.py — imports supabase client to write DCAD data  (Phase 3)

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv
from supabase import Client, create_client


load_dotenv()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    port: int


@lru_cache
def get_settings() -> Settings:
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_KEY", "").strip()
    port_raw = os.getenv("PORT", "8000").strip() or "8000"

    missing_vars = []
    if not supabase_url:
        missing_vars.append("SUPABASE_URL")
    if not supabase_key:
        missing_vars.append("SUPABASE_KEY")

    if missing_vars:
        missing_list = ", ".join(missing_vars)
        raise ValueError(f"Missing required environment variables: {missing_list}")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc

    return Settings(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        port=port,
    )


@lru_cache
def get_supabase_client() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)


supabase = get_supabase_client()