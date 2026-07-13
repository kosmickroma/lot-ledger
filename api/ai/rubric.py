# Ported VERBATIM from docs/AI/read_comps.py:14-27 (already ran live on Vertex
# against prod data — see docs/AI/AI_CARD_PROTOTYPE_RUN_2026-07-13.md). Do not
# reword, re-order, or "improve" — the tag vocabulary is contractually locked to
# the future one-tap why-enum.

PROMPT_VERSION = "v1"   # TREAT LIKE A MIGRATION. Bump on ANY change to RUBRIC —
                        # it is part of the cache key; changing the text without
                        # bumping serves stale extractions forever.

RUBRIC = """You are reading MLS listing remarks for residential sold comps in Dallas, TX.
For EACH listing, extract:
- condition: one of exactly [new-build, remodeled, updated, original-dated, gut-job, unknown]
    new-build = built new/recent construction; remodeled = substantial full renovation;
    updated = partial cosmetic updates; original-dated = livable but not updated;
    gut-job = needs major rehab / sold for repair or as investment shell; unknown = remarks don't say.
- flags: zero or more of exactly [busy-road, quiet-street, backs-commercial, as-is-sale,
    investor-special, foundation-issues, sale-concession, lot-value-only]
Rules:
- EVERY condition (except unknown) and EVERY flag MUST include "quote": a short verbatim
  fragment copied from that listing's remarks that justifies it. No quote -> do not emit the tag.
- Never guess from price or size; only from the remark text.
Return STRICT JSON: [{"comp_id": <int>, "condition": "...", "condition_quote": "...",
"flags": [{"tag": "...", "quote": "..."}]}] — one entry per listing, nothing else."""

CONDITIONS = ["new-build", "remodeled", "updated", "original-dated", "gut-job", "unknown"]
FLAGS      = ["busy-road", "quiet-street", "backs-commercial", "as-is-sale",
              "investor-special", "foundation-issues", "sale-concession", "lot-value-only"]
CONDITION_ORDER = CONDITIONS   # display order on the card
