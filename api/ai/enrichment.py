# Task A — server-side enrichment (cad_subdivision + isd). See
# docs/AI/CODER_SPEC_FACTS_2026-07-14.md §A.
#
# Enrich AT REQUEST TIME. No backfill, no ALTER TABLE, nothing written to any
# table (Fable ruling, 2026-07-14). A stored column would strand the 468
# Dallas garbage parses until someone re-ran a migration; computing live means
# every parser improvement retroactively repairs the whole corpus on next load.
#
# `enrich_comps()` is the ONLY place these two facts are computed — it must be
# run for the subject too (§A.2b), never re-derived a second way, or subject
# and comp stop speaking the same dialect and the whole guarantee collapses.
from __future__ import annotations

import logging
from typing import Any

from api.config import get_conn, release_conn
from api.counties.dcad import _clean_text, _dcad_subdivision_from_legal
from api.counties.tad import _tad_subdivision_from_legal

logger = logging.getLogger(__name__)

# §A.4 — reject and store NULL rather than a bogus name.
#
# ⛔ DO NOT "simplify" this to a raw character-prefix check (e.g.
# `value.upper().startswith("TR")`). The original spec draft said exactly
# that, and it would have nulled real DFW subdivisions — TRAVIS RANCH,
# TRAILWOOD ESTATES, TRINITY MEADOWS — for merely starting with the letters
# "TR". Checked against the first WORD only, so "TR 5A1" (garbage) rejects
# but "TRAVIS RANCH" (real) doesn't. A wrong rejection only costs a missing
# badge (safe, the errable direction); a wrong acceptance costs a lying
# badge (not safe — the one thing this feature cannot do).
_GARBAGE_FIRST_WORDS = {"BLK", "LOT", "BLOCK", "TR", "TRACT", "ABST"}


def _is_garbage_subdivision(value: str) -> bool:
    if not value:
        return True
    if value.isdigit():
        return True
    first_word = value.split(" ", 1)[0].upper()
    return first_word in _GARBAGE_FIRST_WORDS


def _clean_subdivision(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if _is_garbage_subdivision(text):
        return None
    return text


def _qualify_isd(county: str, raw_isd: str | None) -> str | None:
    # §A.3b — the key MUST be county-qualified. Tarrant code "905" and Collin
    # code "905" are different districts; comparing raw codes across counties
    # would badge two unrelated comps "Same ISD" — a lie on the row.
    text = _clean_text(raw_isd)
    if not text:
        return None
    # ⛔ PLACEHOLDER CODES ARE NOT DISTRICTS. Tarrant carries school_code "000"
    # on 6 parcels — it means "no district," not a district. Left alone, two
    # parcels both holding "000" would badge "Same ISD" as though they shared a
    # school. Same class of lie as a garbage subdivision, same rule: a missing
    # badge is safe, a false one is not. Written generically rather than
    # special-casing "000" — any all-zero / no-digit-or-letter code is a
    # placeholder, whatever county invents the next one.
    if not any(ch.isalnum() for ch in text) or all(ch in "0.-_/ " for ch in text):
        return None
    return f"{county}:{text}"


def _enrich_dcad(account_nums: list[str]) -> dict[str, dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.account_num, p.legal1, a.isd_desc
                FROM parcels p
                LEFT JOIN appraisal a ON a.account_num = p.account_num
                WHERE p.account_num = ANY(%s)
                """,
                (account_nums,),
            )
            rows = cur.fetchall()
    finally:
        release_conn(conn)

    out: dict[str, dict[str, Any]] = {}
    for account_num, legal1, isd_desc in rows:
        out[account_num] = {
            "cad_subdivision": _clean_subdivision(_dcad_subdivision_from_legal(legal1)),
            "isd": _qualify_isd("dcad", isd_desc),
        }
    return out


def _enrich_tad(account_nums: list[str]) -> dict[str, dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_num, legal_descr, school_code FROM tad_parcels WHERE account_num = ANY(%s)",
                (account_nums,),
            )
            rows = cur.fetchall()
    finally:
        release_conn(conn)

    out: dict[str, dict[str, Any]] = {}
    for account_num, legal_descr, school_code in rows:
        legal_clean = _clean_text(legal_descr)
        out[account_num] = {
            "cad_subdivision": _clean_subdivision(_tad_subdivision_from_legal(legal_clean)),
            "isd": _qualify_isd("tad", school_code),
        }
    return out


def _enrich_collin(account_nums: list[str]) -> dict[str, dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_num, subdivision, school_code FROM collin_parcels WHERE account_num = ANY(%s)",
                (account_nums,),
            )
            rows = cur.fetchall()
    finally:
        release_conn(conn)

    out: dict[str, dict[str, Any]] = {}
    for account_num, subdivision, school_code in rows:
        out[account_num] = {
            "cad_subdivision": _clean_subdivision(subdivision),
            "isd": _qualify_isd("collin", school_code),
        }
    return out


def _enrich_denton(account_nums: list[str]) -> dict[str, dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_num, subdivision, isd_desc FROM denton_parcels WHERE account_num = ANY(%s)",
                (account_nums,),
            )
            rows = cur.fetchall()
    finally:
        release_conn(conn)

    out: dict[str, dict[str, Any]] = {}
    for account_num, subdivision, isd_desc in rows:
        out[account_num] = {
            "cad_subdivision": _clean_subdivision(subdivision),
            "isd": _qualify_isd("denton", isd_desc),
        }
    return out


_HANDLERS = {
    "dcad": _enrich_dcad,
    "tad": _enrich_tad,
    "collin": _enrich_collin,
    "denton": _enrich_denton,
}


def enrich_comps(county: str, account_nums: list[str]) -> dict[str, dict[str, Any]]:
    """Look up {cad_subdivision, isd} for every account_num in one county.

    A PURE FUNCTION from the caller's perspective — no DOM, no globals, no
    filter code, nothing written anywhere. One indexed IN-list PK query
    against the parcels DB (`lotledger`, via api.config.get_conn — a second
    connection from the comps DB, never a JOIN). Import the SAME county
    parsers the subject card already renders from (never copy them) — that
    shared import is the only thing guaranteeing subject and comp speak the
    same dialect (§A.1).

    Every account_num passed in gets an entry back, even on failure — either
    real {cad_subdivision, isd} values or {None, None}. Never a missing key.

    §A.3 FAIL-OPEN: if the CAD lookup fails or times out, return nulls for
    every account and carry on. The comp load must not 500, hang, or drop a
    comp over a degraded badge.
    """
    county_key = str(county or "").strip().lower()
    clean_accounts = list(dict.fromkeys(a for a in account_nums if a))
    if not clean_accounts:
        return {}

    empty = {account_num: {"cad_subdivision": None, "isd": None} for account_num in clean_accounts}

    handler = _HANDLERS.get(county_key)
    if handler is None:
        return empty

    try:
        found = handler(clean_accounts)
    except Exception:
        logger.exception(
            "enrich_comps: CAD lookup failed for county=%s (%d accounts) — failing open to nulls",
            county_key,
            len(clean_accounts),
        )
        return empty

    return {**empty, **found}
