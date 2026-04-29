from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    port: int


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