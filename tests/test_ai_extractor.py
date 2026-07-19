"""Comp value-down signal extraction — _verify_and_collect() pure-function
tests. See docs/AI/CODER_SPEC_SIGNAL_EXTRACTION_2026-07-19.md §Acceptance.

⛔ There were NO existing extractor tests before this file (Fable finding) —
modeled on tests/test_ai_comparator.py / tests/test_ai_enrichment.py: no
Vertex, no DB, fabricated model rows fed straight into _verify_and_collect()
alongside a fabricated `remarks` string per comp, exactly as extract() calls
it once a real Vertex response comes back (extractor.py:230).
"""
from __future__ import annotations

from api.ai.extractor import _verify_and_collect
from api.ai.rubric import FLAGS, PROMPT_VERSION


def _comp(comp_id, remarks, address="123 Main St", key="k1"):
    return {"comp_id": comp_id, "comp_address_key": key, "address": address, "remarks": remarks}


def _run(chunk, model_rows):
    results: dict[int, dict] = {}
    rejected_detail: list[dict] = []
    dropped = _verify_and_collect(chunk, model_rows, results, rejected_detail)
    return results, rejected_detail, dropped


# ── Item 1 — each new named tag fires ───────────────────────────────────────

def test_new_named_tags_survive_with_a_sufficient_quote() -> None:
    cases = [
        ("motivated-seller", "Motivated seller, priced to sell fast."),
        ("foreclosure-reo", "Bank-owned REO property, sold as-is."),
        ("cash-only", "Sold as-is, cash only, no financing."),
        ("estate-probate", "Estate sale, settling estate, priced accordingly."),
    ]
    for tag, remarks in cases:
        chunk = [_comp(1, remarks)]
        # Quote the whole remarks string -- well over 12 chars, verbatim.
        rows = [{"comp_id": 1, "condition": "unknown", "flags": [{"tag": tag, "quote": remarks}]}]
        results, rejected, dropped = _run(chunk, rows)
        assert dropped == 0, (tag, rejected)
        assert results[1]["flags"] == [{"tag": tag, "quote": remarks}], tag


# ── Item 2 — the minimal-phrase pair (⛔ the real test, not a prompt string-
# match: this locks the exact failure mode the rubric's quote-length
# guidance exists to prevent, at the seam that enforces it) ─────────────────

def test_minimal_phrase_pair_surrounding_fragment_survives() -> None:
    remarks = "Handyman special, sold as-is, cash only, no repairs."
    chunk = [_comp(1, remarks)]
    quote = "sold as-is, cash only"   # >= 12 chars, verbatim substring
    assert len(quote) >= 12
    rows = [{"comp_id": 1, "condition": "unknown", "flags": [{"tag": "cash-only", "quote": quote}]}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 0, rejected
    assert results[1]["flags"] == [{"tag": "cash-only", "quote": quote}]


def test_minimal_phrase_pair_bare_short_trigger_drops_quote_too_short() -> None:
    remarks = "Handyman special, sold as-is, cash only, no repairs."
    chunk = [_comp(1, remarks)]
    quote = "cash only"   # 9 chars, verbatim, but below AI_MIN_QUOTE_CHARS (12)
    assert len(quote) == 9
    rows = [{"comp_id": 1, "condition": "unknown", "flags": [{"tag": "cash-only", "quote": quote}]}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 1
    assert rejected[0]["reason"] == "quote_too_short"
    assert rejected[0]["tag"] == "cash-only"
    assert results[1]["flags"] == []


# ── Item 3 — catch-all ───────────────────────────────────────────────────────

def test_other_value_down_catch_all_survives() -> None:
    remarks = "Seller relocating overseas, must close before month end, priced below market."
    chunk = [_comp(1, remarks)]
    quote = "priced below market"
    rows = [{"comp_id": 1, "condition": "unknown", "flags": [{"tag": "other-value-down", "quote": quote}]}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 0, rejected
    assert results[1]["flags"] == [{"tag": "other-value-down", "quote": quote}]


# ── Item 4 — whitelist ───────────────────────────────────────────────────────

def test_all_13_flags_are_whitelisted() -> None:
    assert len(FLAGS) == 13
    rows_flags = []
    quote_by_tag = {}
    for tag in FLAGS:
        quote = f"quote for {tag} goes here verbatim"
        assert len(quote) >= 12
        quote_by_tag[tag] = quote
        rows_flags.append({"tag": tag, "quote": quote})
    remarks = " ".join(quote_by_tag.values())
    chunk = [_comp(1, remarks)]
    rows = [{"comp_id": 1, "condition": "unknown", "flags": rows_flags}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 0, rejected
    assert {f["tag"] for f in results[1]["flags"]} == set(FLAGS)


def test_unknown_tag_drops_unknown_tag() -> None:
    remarks = "This is a totally ordinary remarks string with no signals."
    chunk = [_comp(1, remarks)]
    rows = [{
        "comp_id": 1, "condition": "unknown",
        "flags": [{"tag": "made-up-tag", "quote": "ordinary remarks string"}],
    }]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 1
    assert rejected[0]["reason"] == "unknown_tag"
    assert rejected[0]["tag"] == "made-up-tag"
    assert results[1]["flags"] == []


# ── Item 5 — anti-hallucination (⛔ gate 3, extractor.py:172-173) ────────────

def test_quote_not_in_remarks_drops_quote_not_in_remarks() -> None:
    remarks = "Charming updated home in a quiet cul-de-sac, move-in ready."
    chunk = [_comp(1, remarks)]
    # Plausible-looking tag/quote pair, but the quote is NOT a substring of
    # remarks -- the model would have to have hallucinated it.
    quote = "bank foreclosure REO sale"
    rows = [{"comp_id": 1, "condition": "unknown", "flags": [{"tag": "foreclosure-reo", "quote": quote}]}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 1
    assert rejected[0]["reason"] == "quote_not_in_remarks"
    assert rejected[0]["tag"] == "foreclosure-reo"
    assert results[1]["flags"] == []


# ── Item 6 — PROMPT_VERSION bumped + part of the cache key ─────────────────

def test_prompt_version_is_v2() -> None:
    assert PROMPT_VERSION == "v2"


def test_prompt_version_is_the_one_routes_py_caches_on() -> None:
    # api/ai/routes.py keys its per-process RAM cache on (comp_id,
    # PROMPT_VERSION) (routes.py:57/63) -- confirm it imports the SAME live
    # constant from rubric.py, not a stale copy, so bumping rubric.py
    # automatically busts the cache key (Part 3: the whole migration).
    from api.ai import routes
    assert routes.PROMPT_VERSION == "v2"
    assert routes.PROMPT_VERSION == PROMPT_VERSION


# ── Item 7 (optional) — silent-omission backfill ─────────────────────────────

def test_comp_omitted_by_model_is_backfilled_as_unknown_no_flags() -> None:
    chunk = [_comp(1, "some remarks", key="k1"), _comp(2, "other remarks", key="k2")]
    # Model returns a row for comp 1 only; comp 2 is silently omitted, but
    # the batch still completed -- it must still be cached (extractor.py's
    # §A.7 guarantee), not left to be re-sent on the next click.
    rows = [{"comp_id": 1, "condition": "unknown", "flags": []}]
    results, rejected, dropped = _run(chunk, rows)
    assert dropped == 0, rejected
    assert results[2] == {"condition": "unknown", "condition_quote": None, "flags": []}
