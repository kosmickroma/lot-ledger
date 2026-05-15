# api/propelio/csv_match.py
#
# Role: Direct-join lookup of each parcel's own last Propelio sold record
#       for the CSV export. Match key is (parcel_account_num, parcel_county).
#       No spatial logic, no polygon - the parcel set is already polygon-
#       filtered by the analyze pipeline.
#
# Connects to:
#   api.main.py - CSV export calls query_propelio_sold_for_parcels();
#                 caller manages the session connection.

from __future__ import annotations

import logging
from typing import Any

from psycopg2.extras import RealDictCursor


logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> float | None:
    """Local copy to avoid circular import with api.main."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def query_propelio_sold_for_parcels(
    session_conn,
    parcel_input: list[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Look up each parcel's own most-recent Propelio sold record.

    Direct join on (parcel_account_num, parcel_county) against propelio_comps.
    When a parcel has multiple sold records (rare - e.g., resold), the most
    recent (highest sold_date) wins.

    Args:
        session_conn: Open psycopg2 connection to the session DB.
        parcel_input: List of (account_num, county) tuples. County values
                      MUST use Propelio's lowercase shortname convention
                      ("dcad", "tad", "collin", "denton") - the caller is
                      responsible for mapping from any other convention.

    Returns:
        Dict keyed by (county, account_num). Each value is a normalized comp
        dict ready for the CSV writer. Parcels with no Propelio sold record
        are absent from the dict (writer treats missing as blank cells).
        Returns empty dict if parcel_input is empty or if the SQL fails
        (soft-fail: logged at warning, never raises).
    """
    if not parcel_input:
        return {}

    sql = """
        SELECT DISTINCT ON (parcel_county, parcel_account_num)
            parcel_county,
            parcel_account_num,
            price,
            sold_date,
            year_built,
            lot_size,
            sqft,
            beds,
            baths,
            dom
        FROM propelio_comps
        WHERE status = 'sold'
          AND parcel_account_num IS NOT NULL
          AND parcel_county IS NOT NULL
          AND (parcel_account_num, parcel_county) IN %s
        ORDER BY parcel_county, parcel_account_num, sold_date DESC NULLS LAST
    """

    parcel_tuples = tuple(parcel_input)
    result: dict[tuple[str, str], dict] = {}
    try:
        with session_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (parcel_tuples,))
            for row in cur.fetchall():
                county = row["parcel_county"]
                account_num = row["parcel_account_num"]
                result[(county, account_num)] = _normalize_propelio_row(row)
    except Exception as exc:
        logger.warning(
            "propelio_sold: query failed: %s: %s (parcel_count=%d)",
            type(exc).__name__,
            exc,
            len(parcel_input),
        )
        return {}

    return result


def _normalize_propelio_row(row: dict) -> dict:
    """Coerce propelio_comps row to JSON-safe dict the CSV writer expects."""
    sold_date = row.get("sold_date")
    if sold_date is not None and hasattr(sold_date, "isoformat"):
        sold_date = sold_date.isoformat()

    return {
        "sold_price": _safe_float(row.get("price")),
        "sold_date": sold_date,
        "yr_built": _safe_float(row.get("year_built")),
        "lot_sqft": _safe_float(row.get("lot_size")),
        "sqft": _safe_float(row.get("sqft")),
        "beds": _safe_float(row.get("beds")),
        "baths": _safe_float(row.get("baths")),
        "dom": _safe_float(row.get("dom")),
        "listing_url": "",  # v1: Propelio per-comp deep link requires lead_id we don't have
        "price_per_sqft": _compute_price_per_sqft(row.get("price"), row.get("sqft")),
    }


def _compute_price_per_sqft(price: Any, sqft: Any) -> float | None:
    p = _safe_float(price)
    s = _safe_float(sqft)
    if p is not None and s is not None and s > 0:
        return p / s
    return None
