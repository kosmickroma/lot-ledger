import os

# Own flag, independent of AI_ENABLED. Default OFF; preview/dev only, prod untouched.
# NOTE: main.py never imports this module at top level (mirrors the corrected
# AI_ENABLED lesson — see api/ai's main.py seam comment). It reads the env var
# directly via os.getenv so a broken/deleted api/value_drafts/ package can never
# stop the app from starting. This constant exists for this package's OWN
# internal use (routes.py) only.
VALUE_DRAFTS_ENABLED = os.getenv("VALUE_DRAFTS_ENABLED", "false").strip().lower() == "true"
