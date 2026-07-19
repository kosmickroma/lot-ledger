# v2 (2026-07-19): value-down signal flags + other-value-down catch-all appended.
# ⛔ APPEND-ONLY vocabulary — never reword/reorder/REMOVE an existing tag (that forks
# already-extracted meaning). Adding is safe. FLAGS (below) and the flags enumeration
# inside RUBRIC MUST list the SAME tags in the SAME order — the extractor whitelists
# against FLAGS while the model reads RUBRIC; drift silently drops tags as unknown_tag.

PROMPT_VERSION = "v2"   # TREAT LIKE A MIGRATION. Bump on ANY change to RUBRIC —
                        # it is part of the cache key; changing the text without
                        # bumping serves stale extractions forever.

RUBRIC = """You are reading MLS listing remarks for residential sold comps in Dallas, TX.
For EACH listing, extract:
- condition: one of exactly [new-build, remodeled, updated, original-dated, gut-job, unknown]
    new-build = built new/recent construction; remodeled = substantial full renovation;
    updated = partial cosmetic updates; original-dated = livable but not updated;
    gut-job = needs major rehab / sold for repair or as investment shell; unknown = remarks don't say.
- flags: zero or more of exactly [busy-road, quiet-street, backs-commercial, as-is-sale,
    investor-special, foundation-issues, sale-concession, lot-value-only, motivated-seller,
    foreclosure-reo, cash-only, estate-probate, other-value-down]
    Read the MEANING from the remark language (the same idea is phrased many ways); do not keyword-match:
      motivated-seller = seller urgency/motivation ("motivated seller," "must sell," "priced to sell," "bring all offers").
      foreclosure-reo  = bank-owned / foreclosure ("REO," "bank-owned," "HUD home," "foreclosure").
      cash-only        = not financeable ("cash only," "cash or hard money," "no financing," "not FHA/VA eligible").
      estate-probate   = estate / probate / trust sale ("estate sale," "probate," "trust sale," "settling estate").
      other-value-down = ANY clear signal the property or sale is below-retail / value-diminished that does
                         NOT fit a flag above. Do NOT use it for renovation/repair state — that belongs in
                         the condition field, never as a flag. When in doubt, do NOT emit it.
Rules:
- EVERY condition (except unknown) and EVERY flag MUST include "quote": a verbatim
  fragment copied from that listing's remarks that justifies it. No quote -> do not emit the tag.
- The quote must be at least ~12 characters. If the justifying phrase is shorter (e.g. "cash only"),
  quote a longer surrounding fragment that still contains it verbatim (e.g. "sold as-is, cash only").
- Never guess from price or size; only from the remark text.
Return STRICT JSON: [{"comp_id": <int>, "condition": "...", "condition_quote": "...",
"flags": [{"tag": "...", "quote": "..."}]}] — one entry per listing, nothing else."""

CONDITIONS = ["new-build", "remodeled", "updated", "original-dated", "gut-job", "unknown"]
FLAGS      = ["busy-road", "quiet-street", "backs-commercial", "as-is-sale",
              "investor-special", "foundation-issues", "sale-concession", "lot-value-only",
              "motivated-seller", "foreclosure-reo", "cash-only", "estate-probate", "other-value-down"]
CONDITION_ORDER = CONDITIONS   # display order on the card
