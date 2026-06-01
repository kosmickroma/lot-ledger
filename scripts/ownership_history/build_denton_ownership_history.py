#!/usr/bin/env python3
"""Ingest Denton CAD multi-year certified rolls into ownership_snapshots.

Long-format owner-history loader (one row per account x year). Idempotent —
re-run safely when new years arrive (drop folders into the historical dir and
re-run). Loads py_owner_name + deed_dt per prop_id (stripped of leading zeros
to match denton_parcels.account_num), county='denton'.

Source: Denton CAD publishes a per-year fixed-width PACS "Appraisal Export"
zip; the file we read is APPRAISAL_INFO.TXT, byte-position-defined per
TP_Legacy_8.0.32_AppraisalExportLayout.xlsx (485 fields, of which we use 6).
~4 GB per year, ~410k parcels. Variable row length across vintages (sampled
8687-9247 chars in 2020-2025).

Run (points at the MAIN worktree's gitignored data; uses the same DB env as
DCAD/Collin ingest, i.e. Mike's prod Cloud SQL):

    python scripts/ownership_history/build_denton_ownership_history.py \
        --historical-dir /home/kk/projects/clients/lot-ledger/ingest/counties/denton/cad/historical_owners

Layout (mixed-case, varies per year — recursive find for *APPRAISAL_INFO.TXT
required):
    <year>/<varies>/.../<dated_id>_APPRAISAL_INFO.TXT
where <varies> spans e.g. '2020-certified-data-all-property',
'CertifiedData-All Property', '2021 AllProperty-AllFiles/CertifiedData-All
Property', etc.

Field mapping (PACS 1-based inclusive → Python 0-based exclusive):
    prop_id        chars  1-12     s[0:12]      → account_num (strip leading zeros)
    prop_val_yr    chars 18-22     s[17:22]     → snapshot_year sanity-check
    sup_num        chars 23-34     s[22:34]     → filter to "000000000000" (certified)
    py_owner_id    chars 597-608   s[596:608]   → multi-owner dedup tiebreaker
    py_owner_name  chars 609-678   s[608:678]   → owner_name
    deed_dt        chars 2034-2058 s[2033:2058] → deed_txfr_date (MMDDYYYY or MM-DD-YYYY)

Multi-owner dedup: when a prop_id appears with multiple py_owner_id values in
the same file (PACS exposes a row per owner_id within a property), the LOWEST
py_owner_id wins. Deterministic, stable across years for stable joint-
ownership groups, doesn't manufacture false flips in the v2 distinct-owners
algorithm. Same-py_owner_id-different-name (shouldn't happen in PACS) keeps
first-seen and logs a debug count.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Make the repo root importable when run as a plain script from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from psycopg2.extras import execute_values  # noqa: E402

from api.config import get_conn, release_conn  # noqa: E402

COUNTY = "denton"
BATCH_SIZE = 50_000
MIN_ROW_LEN = 2058  # last extracted field ends at char 2058; shorter rows are unusable
DEFAULT_HISTORICAL_DIR = str(
    Path(__file__).resolve().parents[2]
    / "ingest" / "counties" / "denton" / "cad" / "historical_owners"
)


def _find_year_files(historical_dir: str) -> dict[int, str]:
    """{year: APPRAISAL_INFO.TXT path} for each 4-digit year folder present.

    Denton layout varies per vintage; the TXT lives nested inside a subfolder
    whose name changes year to year (e.g. '2020-certified-data-all-property',
    'CertifiedData-All Property'). We recursively search for files matching
    '*APPRAISAL_INFO.TXT' (case-insensitive).

    Rules:
      * Exactly one match per year folder → use it.
      * Zero matches → warning, year skipped.
      * Two or more matches → SystemExit with conflicting paths (don't pick
        arbitrarily; a year with two appraisal exports is an operator error
        that needs human review).
      * Files inside any '__MACOSX/' subtree are ignored (AppleDouble
        sidecars created when these zips are opened on macOS).
      * Year folders outside 1900-2100 are rejected (defensive against an
        accidental '0000' folder producing snapshot_year=0 rows).
    """
    found: dict[int, str] = {}
    if not os.path.isdir(historical_dir):
        return found
    for entry in sorted(os.listdir(historical_dir)):
        if not (entry.isdigit() and len(entry) == 4):
            continue
        year_int = int(entry)
        if not (1900 <= year_int <= 2100):
            continue
        year_path = os.path.join(historical_dir, entry)
        if not os.path.isdir(year_path):
            continue
        matches: list[str] = []
        for root, dirs, files in os.walk(year_path):
            # Don't descend into Mac AppleDouble sidecars.
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for fname in files:
                if fname.upper().endswith("APPRAISAL_INFO.TXT"):
                    matches.append(os.path.join(root, fname))
        if not matches:
            print(
                f"  WARNING: no *APPRAISAL_INFO.TXT under {year_path}/; "
                "skipping this year"
            )
            continue
        if len(matches) > 1:
            joined = "\n  ".join(sorted(matches))
            raise SystemExit(
                f"Multiple APPRAISAL_INFO.TXT files found under {year_path}/:\n"
                f"  {joined}\n"
                "Refusing to pick arbitrarily. Move/remove the extras so only "
                "the certified Appraisal Export remains, then re-run."
            )
        found[year_int] = matches[0]
    return dict(sorted(found.items()))


def _parse_pacs_date(raw: str | None) -> dt.date | None:
    """Parse PACS deed_dt → date; None on blank/zero/unparseable/out-of-range.

    Accepts BOTH:
      * 'MMDDYYYY'   (8 digits, no separator)  — seen in 2020-2022 sample
      * 'MM-DD-YYYY' (8 digits with dashes)    — defensive for vintage drift

    Year-range guard: 1900 <= year <= 2100. Out-of-range parsed dates (e.g.
    placeholder year 5000) return None rather than poisoning downstream
    sort/group operations.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text in ("00000000", "00-00-0000"):
        return None
    parsed: dt.datetime | None = None
    if len(text) == 8 and text.isdigit():
        try:
            parsed = dt.datetime.strptime(text, "%m%d%Y")
        except ValueError:
            parsed = None
    elif "-" in text:
        try:
            parsed = dt.datetime.strptime(text, "%m-%d-%Y")
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    if not (1900 <= parsed.year <= 2100):
        return None
    return parsed.date()


def _row_to_record(
    line: str, year: int, source_file: str
) -> tuple[tuple, int] | None:
    """Slice one fixed-width line → (record, py_owner_id) or None to skip.

    Returns None when:
      * line is shorter than MIN_ROW_LEN (last field unreachable)
      * sup_num is not '000000000000' (non-certified row)
      * prop_id is empty after lstripping leading zeros
      * py_owner_name is blank/whitespace

    Caller increments per-reason counters via _stream_dedup_records (which
    knows what was skipped because it strips/inspects line itself for the
    counters); _row_to_record is the simple "valid row → tuple" mapper used
    by tests for shape verification. NOTE: returning None loses the skip
    reason — _stream_dedup_records re-does the checks inline so it can count
    each reason; this helper is kept simple for test surface.
    """
    if len(line) < MIN_ROW_LEN:
        return None
    sup_num_raw = line[22:34]
    if sup_num_raw != "000000000000":
        return None
    prop_id = line[0:12].strip().lstrip("0")
    if not prop_id:
        return None
    owner_name = line[608:678].strip()
    if not owner_name:
        return None
    py_owner_id_raw = line[596:608].strip()
    py_owner_id = int(py_owner_id_raw) if py_owner_id_raw.isdigit() else 0
    deed = _parse_pacs_date(line[2033:2058])
    record = (COUNTY, prop_id, year, owner_name, deed, source_file)
    return (record, py_owner_id)


def _check_propyear_matches_folder(
    first_data_line: str, expected_year: int, source_file: str
) -> None:
    """Sanity-check the first data row's prop_val_yr against the folder year.

    Prints a warning on mismatch but does not fail — catches the foot-gun
    where a file gets placed in the wrong year folder (which would otherwise
    silently write ~400k rows under the wrong snapshot_year). Silent on
    short rows or unparseable prop_val_yr (treated as 'no anchor available,
    trust the folder')."""
    if len(first_data_line) < 22:
        return
    raw = first_data_line[17:22].strip()
    if not raw:
        return
    try:
        seen = int(raw)
    except ValueError:
        return
    if seen != expected_year:
        print(
            f"  WARNING: {source_file} has prop_val_yr={seen} on its first row "
            f"but is in folder for year={expected_year}; ingesting under "
            f"folder year (file may have been placed in the wrong folder)"
        )


def _stream_dedup_records(
    path: str, year: int, source_file: str, batch_size: int = BATCH_SIZE
) -> Iterator[list[tuple]]:
    """Stream a Denton APPRAISAL_INFO.TXT, dedup by prop_id, yield batches.

    Algorithm:
      1. Open with encoding='latin-1' (byte-clean superset; safe for any
         fixed-width byte-position layout).
      2. For the first non-blank line, sanity-check prop_val_yr against the
         folder year (warn-only).
      3. For each row:
         a. Skip if shorter than MIN_ROW_LEN (count).
         b. Skip if sup_num != "000000000000" (non-certified).
         c. Skip if prop_id is empty after lstrip('0').
         d. Skip if py_owner_name is blank.
         e. Insert into dedup dict keyed by prop_id; lowest py_owner_id
            wins. Same-id-different-name keeps first-seen and increments a
            debug counter.
      4. After EOF, yield the deduped records in `batch_size` chunks.
      5. Print per-file summary (rows scanned, skipped by reason, unique
         emitted, dedup collisions, same-id-different-name count).

    Memory: ~400k unique parcels × ~150 B per tuple = ~60-80 MB peak.
    The 4 GB raw file is line-streamed and never held in memory.
    """
    rows_scanned = 0
    short_rows = 0
    non_cert_rows = 0
    blank_propid = 0
    blank_owner = 0
    dedup_replaced = 0
    same_pyownerid_different_name = 0

    # dedup[prop_id] = (record_tuple, py_owner_id_int)
    dedup: dict[str, tuple[tuple, int]] = {}
    propyear_checked = False

    with open(path, "r", encoding="latin-1", newline="") as fh:
        for raw_line in fh:
            # Strip CRLF/LF only; do NOT strip interior padding.
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            rows_scanned += 1

            if not propyear_checked:
                propyear_checked = True
                _check_propyear_matches_folder(line, year, source_file)

            if len(line) < MIN_ROW_LEN:
                short_rows += 1
                continue
            sup_num_raw = line[22:34]
            if sup_num_raw != "000000000000":
                non_cert_rows += 1
                continue
            prop_id = line[0:12].strip().lstrip("0")
            if not prop_id:
                blank_propid += 1
                continue
            owner_name = line[608:678].strip()
            if not owner_name:
                blank_owner += 1
                continue
            py_owner_id_raw = line[596:608].strip()
            py_owner_id = int(py_owner_id_raw) if py_owner_id_raw.isdigit() else 0
            deed = _parse_pacs_date(line[2033:2058])
            record = (COUNTY, prop_id, year, owner_name, deed, source_file)

            existing = dedup.get(prop_id)
            if existing is None:
                dedup[prop_id] = (record, py_owner_id)
            elif py_owner_id < existing[1]:
                dedup[prop_id] = (record, py_owner_id)
                dedup_replaced += 1
            elif py_owner_id == existing[1] and existing[0][3] != owner_name:
                same_pyownerid_different_name += 1
            # else: higher or equal py_owner_id with same name → keep existing

    print(
        f"  {source_file}: scanned={rows_scanned:,} "
        f"short={short_rows:,} non_cert={non_cert_rows:,} "
        f"blank_propid={blank_propid:,} blank_owner={blank_owner:,} "
        f"unique={len(dedup):,} dedup_replaced={dedup_replaced:,} "
        f"same_id_diff_name={same_pyownerid_different_name:,}"
    )

    # Yield deduped records in bounded batches.
    batch: list[tuple] = []
    for record, _py_owner_id in dedup.values():
        batch.append(record)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _create_table() -> None:
    """Create ownership_snapshots if it doesn't exist. DCAD ingest already
    creates it; this is a no-op when DCAD has run first, but kept so this
    script can stand alone."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS ownership_snapshots ("
                "  county TEXT NOT NULL,"
                "  account_num TEXT NOT NULL,"
                "  snapshot_year INTEGER NOT NULL,"
                "  owner_name TEXT,"
                "  deed_txfr_date DATE,"
                "  source_file TEXT NOT NULL,"
                "  ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
                "  PRIMARY KEY (county, account_num, snapshot_year))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_ownership_snap_acct "
                "ON ownership_snapshots (county, account_num)"
            )
        conn.commit()
    finally:
        release_conn(conn)


def _upsert(records: list[tuple]) -> None:
    if not records:
        return
    conn = get_conn()
    try:
        sql = (
            "INSERT INTO ownership_snapshots "
            "(county, account_num, snapshot_year, owner_name, deed_txfr_date, source_file) "
            "VALUES %s "
            "ON CONFLICT (county, account_num, snapshot_year) DO UPDATE SET "
            "  owner_name = EXCLUDED.owner_name,"
            "  deed_txfr_date = EXCLUDED.deed_txfr_date,"
            "  source_file = EXCLUDED.source_file,"
            "  ingested_at = NOW()"
        )
        with conn.cursor() as cur:
            for i in range(0, len(records), 10000):
                execute_values(cur, sql, records[i : i + 10000], page_size=1000)
                print(f"  upserted {min(i + 10000, len(records)):,} / {len(records):,}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def _ingest_year(year: int, path: str, batch_size: int = BATCH_SIZE) -> int:
    source_file = os.path.basename(path)
    total = 0
    for batch in _stream_dedup_records(path, year, source_file, batch_size):
        _upsert(batch)
        total += len(batch)
    print(f"{year}: upserted {total:,} records from {source_file}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Denton CAD ownership history.")
    ap.add_argument("--historical-dir", default=DEFAULT_HISTORICAL_DIR)
    args = ap.parse_args()

    year_files = _find_year_files(args.historical_dir)
    if not year_files:
        raise SystemExit(
            f"No *APPRAISAL_INFO.TXT found under {args.historical_dir}/<year>/. "
            "Download + extract the Denton CAD Certified Data Files first."
        )
    print(f"Years found: {sorted(year_files)}")
    _create_table()
    total = 0
    for year, path in year_files.items():
        total += _ingest_year(year, path)
    print(f"\nDone — {total:,} rows upserted across {len(year_files)} years.")


if __name__ == "__main__":
    main()
