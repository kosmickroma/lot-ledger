import os

# Own flag, independent of AI_ENABLED. Default OFF; preview/dev only, prod untouched.
# NOTE: main.py never imports this module at top level (mirrors the corrected
# AI_ENABLED lesson — see api/ai's main.py seam comment). It reads the env var
# directly via os.getenv so a broken/deleted api/value_drafts/ package can never
# stop the app from starting. This constant exists for this package's OWN
# internal use (routes.py) only.
VALUE_DRAFTS_ENABLED = os.getenv("VALUE_DRAFTS_ENABLED", "false").strip().lower() == "true"

# Coder spec docs/AI/CODER_SPEC_ROLE_GATE_2026-07-18.md Part 2 — opens AI mode's
# role gate to every signed-in user. Default FALSE; set true only on dev/preview
# Cloud Run services, never prod. Read independently here (NOT imported from
# api.ai.config) for the same reason VALUE_DRAFTS_ENABLED is read directly above
# — a broken/deleted api/ai/ package must not be able to take this router down.
AI_ALL_USERS = os.getenv("AI_ALL_USERS", "false").strip().lower() == "true"
