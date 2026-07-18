import os

AI_ENABLED     = os.getenv("AI_ENABLED", "false").strip().lower() == "true"
# Coder spec docs/AI/CODER_SPEC_ROLE_GATE_2026-07-18.md Part 2 — opens AI mode's
# role gate to every signed-in user. Default FALSE; set true only on dev/preview
# Cloud Run services, never prod. Read independently in api/value_drafts/config.py
# too — NO cross-module import (a broken/deleted api/ai/ package must not be able
# to take value_drafts down at mount time).
AI_ALL_USERS   = os.getenv("AI_ALL_USERS", "false").strip().lower() == "true"
AI_GCP_PROJECT = os.getenv("AI_GCP_PROJECT", "lot-ledger")   # NOT korvall — see spec §5
AI_LOCATION    = os.getenv("AI_LOCATION", "us-central1")
AI_MODEL       = os.getenv("AI_MODEL", "gemini-2.5-flash-lite")
AI_BATCH_SIZE  = int(os.getenv("AI_BATCH_SIZE", "8"))
AI_MAX_COMPS   = int(os.getenv("AI_MAX_COMPS", "60"))   # hard cap per request (cost + latency guard)

# §A.5.2 — TOTAL wall-clock budget for one /read-comps request, checked between
# batches, vs. the timeout on each individual Vertex call. Must stay well under
# the Cloud Run service's own request timeout (default 300s) — verify before deploy.
AI_TIMEOUT_S       = int(os.getenv("AI_TIMEOUT_S", "90"))
AI_BATCH_TIMEOUT_S = int(os.getenv("AI_BATCH_TIMEOUT_S", "25"))

# §A.5.1.b — a quote shorter than this trivially substring-matches almost any
# remarks text, which would defeat the whole audit mechanism.
AI_MIN_QUOTE_CHARS = int(os.getenv("AI_MIN_QUOTE_CHARS", "12"))

# §A.4 — remarks shorter than this reliably produce "unknown" with no quote;
# sending them wastes tokens for no signal.
AI_MIN_REMARKS_CHARS = int(os.getenv("AI_MIN_REMARKS_CHARS", "40"))

# permit_avm gate on the AI READ. When true, comps the MLS flags
# permit_avm != 'true' are excluded from the read (their listing text is never
# sent to the model). DEFAULT TRUE so prod stays gated by default. Set FALSE on
# dev/preview (KK's call, private testing on his own data) so the AI reads every
# comp — the scorecard shows them either way; this only governs whether we send
# their remarks to Vertex. ⚠️ This is the one gate about third-party TEXT
# transmission, so flipping it is an env decision per environment, not a default.
AI_ENFORCE_PERMIT_AVM = os.getenv("AI_ENFORCE_PERMIT_AVM", "true").strip().lower() == "true"

# Flash-Lite pricing, $/1M tokens — for the est_cost display only. Stamp the date.
AI_PRICE_IN_PER_M  = float(os.getenv("AI_PRICE_IN_PER_M",  "0.10"))
AI_PRICE_OUT_PER_M = float(os.getenv("AI_PRICE_OUT_PER_M", "0.40"))
