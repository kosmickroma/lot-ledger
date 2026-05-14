"""
Central configuration for the comp engine.

Secrets (Propelio credentials, optional outbound proxy) come from environment
variables — typically loaded from a `.env` file at the repo root. See
`.env.example` for the keys this file expects. Tunable scoring constants
remain inline below.
"""

import os
from dotenv import load_dotenv

# Load .env if present (no-op if the file is missing or values are already
# set in the real environment).
load_dotenv()

# --- Propelio API credentials ---------------------------------------------
PROPELIO_USERNAME = os.getenv("PROPELIO_USERNAME", "")
PROPELIO_PASSWORD = os.getenv("PROPELIO_PASSWORD", "")

# --- HTTP / network knobs (used by scraper.py) ----------------------------
# Optional outbound proxy. Set PROPELIO_PROXY_URL in .env to enable; leave
# unset for direct connection (typical for legitimate single-account use).
_proxy_url = os.getenv("PROPELIO_PROXY_URL", "").strip()
PROPELIO_PROXIES = (
    {"http": _proxy_url, "https": _proxy_url} if _proxy_url else {}
)
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# --- Comp filtering / scoring (used by comp_engine.py) --------------------
RADIUS_MILES = 0.5
RADIUS_EXPANSION_STEPS = (0.25, 0.5, 0.75, 1.0, 1.5)
MIN_POOL_BEFORE_RADIUS_EXPAND = 5
LOT_SIZE_TOLERANCE = 0.33
LIVING_AREA_TOLERANCE = 0.40
CONFIDENCE_THRESHOLD = 0.25
CROSS_SUBDIVISION_CONF_MULT = 0.8
MAJOR_STREET_BOUNDARY_MULT = 0.9
MAJOR_STREET_MIN_DISTANCE_FT = 40
MAJOR_STREET_MAX_DISTANCE_FT = 900
NEW_BUILD_YEARS = 5
NEW_BUILDS_MIN_YEAR = 2015     # only used when --new-builds is set on the CLI
TOP_COMP_COUNT = 3
EXPAND_NEIGHBORHOOD_CONF_MULT = 0.6
EXPAND_NEIGHBORHOOD_DISTANCE_FLAG_MI = 0.3

# --- Output ---------------------------------------------------------------
OUTPUT_FILE = "output/comps_report.xlsx"
