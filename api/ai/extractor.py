# AI module — Vertex REST extraction (§A.5).
#
# Call shape ported from docs/AI/read_comps.py:34-49 (proven working). Auth is
# NOT ported verbatim: the prototype shells out to `gcloud auth print-access-token`,
# and there is no gcloud binary in the Cloud Run container. This uses Application
# Default Credentials instead (§A.5, per Fable + Copilot review).
#
# NEVER log remarks, quotes, addresses, or Vertex request/response bodies — at
# ANY level, including exception handlers (§A.8.1). Only counts/ids/tokens/cost/
# duration/error class or status may be logged.
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any

import google.auth
from google.auth.transport.requests import Request as GARequest

from api.ai.config import (
    AI_BATCH_SIZE,
    AI_BATCH_TIMEOUT_S,
    AI_GCP_PROJECT,
    AI_LOCATION,
    AI_MIN_QUOTE_CHARS,
    AI_MODEL,
    AI_TIMEOUT_S,
)
from api.ai.rubric import CONDITIONS, FLAGS, RUBRIC

_creds = None
_creds_lock = threading.Lock()


def _token() -> str:
    global _creds
    with _creds_lock:   # uvicorn serves concurrently; refresh is NOT thread-safe
        if _creds is None:
            _creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _creds.valid:
            _creds.refresh(GARequest())
        return _creds.token


def _norm(s: str) -> str:
    # Applied IDENTICALLY to quotes and remarks before the substring check
    # (§A.5.1.a). MLS remarks are full of smart quotes / en-dashes / NBSP and
    # the model re-emits them inconsistently; a naive casefold+strip would
    # false-reject real quotes.
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    return s.casefold().strip()


def _vertex_url() -> str:
    return (
        f"https://{AI_LOCATION}-aiplatform.googleapis.com/v1/projects/{AI_GCP_PROJECT}"
        f"/locations/{AI_LOCATION}/publishers/google/models/{AI_MODEL}:generateContent"
    )


def _call_vertex(chunk: list[dict[str, Any]], timeout_s: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    docs = "\n\n".join(f"[comp_id {c['comp_id']}]\n{c['remarks']}" for c in chunk)
    body = {
        "contents": [{"role": "user", "parts": [{"text": RUBRIC + "\n\nLISTINGS:\n\n" + docs}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        _vertex_url(),
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )

    for attempt in range(2):   # 1 retry on 429/5xx with a short backoff
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                out = json.load(r)
            usage = out.get("usageMetadata", {})
            text = out["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text), {
                "tokens_in": usage.get("promptTokenCount", 0),
                "tokens_out": usage.get("candidatesTokenCount", 0),
            }
        except urllib.error.HTTPError as exc:
            if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                time.sleep(1.5)
                continue
            raise
    raise RuntimeError("unreachable")   # loop always returns or raises


def _reject(rejected_detail, comp, tag, quote, reason) -> None:
    # §A.6.1 — display-only, returned to an authenticated owner/developer and
    # rendered transiently. NEVER logged (§A.8.1 stands absolutely).
    rejected_detail.append({
        "comp_address_key": comp["comp_address_key"] if comp else None,
        "address": comp["address"] if comp else None,
        "tag": tag,
        "quote": quote if isinstance(quote, str) else None,
        "reason": reason,
    })


def _verify_and_collect(
    chunk: list[dict[str, Any]],
    model_rows: list[dict[str, Any]],
    results: dict[int, dict[str, Any]],
    rejected_detail: list[dict[str, Any]],
) -> int:
    """Validate the model's rows against THIS chunk only. Returns the number of
    tags dropped this call (appended to rejected_detail). Never repairs, never
    fuzzy-matches — a failed check drops the tag outright (§A.5.1)."""
    sent_ids = {c["comp_id"] for c in chunk}
    by_comp_id = {c["comp_id"]: c for c in chunk}
    remarks_norm_by_id = {c["comp_id"]: _norm(c["remarks"]) for c in chunk}
    before = len(rejected_detail)

    for row in model_rows:
        if not isinstance(row, dict):
            _reject(rejected_detail, None, None, None, "malformed_row")
            continue
        try:
            cid = int(row.get("comp_id", -1))
        except (TypeError, ValueError):
            _reject(rejected_detail, None, row.get("condition"), row.get("condition_quote"), "malformed_row")
            continue
        if cid not in sent_ids:
            # ⛔ §A.5.1.c — the model can return a comp_id that was never in this
            # batch. Accepting it would let a hallucinated id plus a boilerplate
            # quote poison the cache for a comp the model never read. We don't
            # know which real comp (if any) this was meant for, so comp identity
            # is None — only the attempted tag/quote survive for tuning.
            _reject(rejected_detail, None, row.get("condition"), row.get("condition_quote"), "comp_id_not_in_batch")
            continue

        comp = by_comp_id[cid]
        remarks_norm = remarks_norm_by_id[cid]
        out: dict[str, Any] = {"condition": "unknown", "condition_quote": None, "flags": []}

        condition = row.get("condition")
        quote = row.get("condition_quote")
        if condition == "unknown":
            pass   # no quote required for "unknown"
        elif condition not in CONDITIONS:
            _reject(rejected_detail, comp, condition, quote, "unknown_tag")
        elif not (isinstance(quote, str) and len(_norm(quote)) >= AI_MIN_QUOTE_CHARS):
            _reject(rejected_detail, comp, condition, quote, "quote_too_short")
        elif _norm(quote) not in remarks_norm:
            _reject(rejected_detail, comp, condition, quote, "quote_not_in_remarks")
        else:
            out["condition"] = condition
            out["condition_quote"] = quote

        for flag in row.get("flags") or []:
            if not isinstance(flag, dict):
                _reject(rejected_detail, comp, None, None, "malformed_row")
                continue
            tag = flag.get("tag")
            fquote = flag.get("quote")
            if tag not in FLAGS:
                _reject(rejected_detail, comp, tag, fquote, "unknown_tag")
            elif not (isinstance(fquote, str) and len(_norm(fquote)) >= AI_MIN_QUOTE_CHARS):
                _reject(rejected_detail, comp, tag, fquote, "quote_too_short")
            elif _norm(fquote) not in remarks_norm:
                _reject(rejected_detail, comp, tag, fquote, "quote_not_in_remarks")
            else:
                out["flags"].append({"tag": tag, "quote": fquote})

        results[cid] = out

    # §A.7's cache guarantee is "every comp whose batch COMPLETED gets cached" —
    # not "every comp the model bothered to return a row for". Fill in any comp
    # sent in this (completed) batch that the model silently omitted, so it is
    # never re-sent to the model on a later click.
    for c in chunk:
        results.setdefault(c["comp_id"], {"condition": "unknown", "condition_quote": None, "flags": []})

    return len(rejected_detail) - before


def extract(
    to_read: list[dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], dict[str, int], int, list[dict[str, Any]], bool]:
    """
    to_read: [{comp_id, comp_address_key, address, price, remarks}, ...] — already
    permit + scope + remarks gated by gate.fetch_permitted_comps, and already
    excludes anything served from cache.

    Returns (results_by_comp_id, usage{tokens_in,tokens_out}, rejected_quotes,
    rejected_detail, partial). rejected_detail (§A.6.1) is the per-tag detail
    behind the rejected_quotes count — display-only, never logged, and empty
    for any comp served from cache (rejections only happen at extraction time).

    Raises only when the FIRST batch fails outright (zero batches completed) —
    the route turns that into a 502. Any later-batch failure, or hitting the
    overall deadline, sets `partial=True` and returns what completed; it is not
    an error (§A.5.2 / §A.6 step 9).
    """
    results: dict[int, dict[str, Any]] = {}
    tokens_in = 0
    tokens_out = 0
    rejected_quotes = 0
    rejected_detail: list[dict[str, Any]] = []
    partial = False

    batches = [to_read[i:i + AI_BATCH_SIZE] for i in range(0, len(to_read), AI_BATCH_SIZE)]
    deadline = time.monotonic() + AI_TIMEOUT_S

    for i, chunk in enumerate(batches):
        if time.monotonic() >= deadline:
            partial = True   # NOT an error — return what we have, honestly labeled
            break
        try:
            rows, usage = _call_vertex(chunk, AI_BATCH_TIMEOUT_S)
        except Exception:
            if i == 0:
                raise   # zero batches completed -> caller returns 502
            partial = True
            break
        tokens_in += usage["tokens_in"]
        tokens_out += usage["tokens_out"]
        rejected_quotes += _verify_and_collect(chunk, rows, results, rejected_detail)

    return results, {"tokens_in": tokens_in, "tokens_out": tokens_out}, rejected_quotes, rejected_detail, partial
