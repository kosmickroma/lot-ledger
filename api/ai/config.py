import os

AI_ENABLED     = os.getenv("AI_ENABLED", "false").strip().lower() == "true"
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

# Flash-Lite pricing, $/1M tokens — for the est_cost display only. Stamp the date.
AI_PRICE_IN_PER_M  = float(os.getenv("AI_PRICE_IN_PER_M",  "0.10"))
AI_PRICE_OUT_PER_M = float(os.getenv("AI_PRICE_OUT_PER_M", "0.40"))
