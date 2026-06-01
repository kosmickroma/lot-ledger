#!/usr/bin/env python3
"""Ingest Tarrant Appraisal District (TAD) multi-year certified rolls into ownership_snapshots.

Long-format owner-history loader (one row per account x year). Idempotent —
re-run safely when new years arrive (drop folders into the historical dir and
re-run). Loads Owner_Name + Deed_Date per Account_Num (leading zeros stripped
to match tarrant_parcels.account_num), county='tad'.

Source: tad.org's public "Standard Data" downloads publish each year's
certified roll as a single pipe-delimited (|) ASCII text file with a header
row. ~700 MB per year, ~750k rows (includes Personal Property + Minerals
which are filtered out at ingest time).

Run (points at the MAIN worktree's gitignored data; uses the same DB env as
DCAD/Collin/Denton ingest, i.e. Mike's prod Cloud SQL):

    python scripts/ownership_history/build_tad_ownership_history.py \
        --historical-dir /home/kk/projects/clients/lot-ledger/ingest/counties/tarrant/tad/historical_owners

Layout:
    <year>/PropertyData_<year>(Certified)/PropertyData_<year>.txt

Field mapping (TAD pipe-delimited header → ownership_snapshots):
    RP             → row filter: keep 'R' (Residential) + 'C' (Commercial);
                     drop 'P' (Personal Property) + 'M' (Minerals).
    Account_Num    → account_num (kept as 8-char zero-padded numeric like
                     "00000051" — matches the existing tad_parcels table's
                     format. Verified 2026-06-01: stripping to "51" matched
                     only ~28% of TAD parcels; preserving the padded form
                     matches ~93%, which is the same shape as the other
                     counties' coverage.)
    Record_Type    → row filter: keep "AAAA" (the primary real-estate record
                     per the doc); reject anything else ("LOCA" location
                     rows etc., which carry blank/garbage Owner_Name).
    Appraisal_Year → first-row sanity check vs folder year (warn only).
    Owner_Name     → owner_name (after .strip()).
    Deed_Date      → deed_txfr_date (MM/DD/YYYY per doc).

Note on the pipe-delimited vs fixed-width ambiguity: TAD's public
documentation (Standard Distribution Data, 2022) describes the format with
Position/Length tables, suggesting fixed-width by byte offset. The actual
shipped files are pipe-delimited with a header row — the Position/Length
table just describes logical field order, not byte offsets. Verified by
inspection of PropertyData_2022.txt before writing this loader.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Make the repo root importable when run as a plain script from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from psycopg2.extras import execute_values  # noqa: E402

from api.config import get_conn, release_conn  # noqa: E402

COUNTY = "tad"
BATCH_SIZE = 50_000
DEFAULT_HISTORICAL_DIR = str(
    Path(__file__).resolve().parents[2]
    / "ingest" / "counties" / "tarrant" / "tad" / "historical_owners"
)
# csv.field_size_limit defaults to 131,072 chars per field, plenty for TAD's
# 643-char rows; not raising it here so a degenerate row stays a hard error.


def _find_year_files(historical_dir: str) -> dict[int, str]:
    """{year: PropertyData_<year>.txt path} for each 4-digit year folder present.

    TAD layout (mixed-case, with parens):
        <year>/PropertyData_<year>(Certified)/PropertyData_<year>.txt

    Recursive find for the *.txt under each year folder. Matches files whose
    basename matches 'PropertyData_<year>.txt' case-insensitively. Same
    "exactly one match per year folder or warn/fail loudly" rule as the
    Denton ingest — but TAD's per-year layout is consistent enough that the
    one-match path is the normal case.

    Rejects 4-digit folders outside 1900-2100 (defensive against an accidental
    '0000' folder producing snapshot_year=0 rows).
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
        target_lower = f"propertydata_{entry}.txt"
        matches: list[str] = []
        for root, dirs, files in os.walk(year_path):
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for fname in files:
                if fname.lower() == target_lower:
                    matches.append(os.path.join(root, fname))
        if not matches:
            print(
                f"  WARNING: no PropertyData_{entry}.txt under {year_path}/; "
                "skipping this year"
            )
            continue
        if len(matches) > 1:
            joined = "\n  ".join(sorted(matches))
            raise SystemExit(
                f"Multiple PropertyData_{entry}.txt files found under {year_path}/:\n"
                f"  {joined}\n"
                "Refusing to pick arbitrarily. Move/remove the extras so only "
                "the certified roll remains, then re-run."
            )
        found[year_int] = matches[0]
    return dict(sorted(found.items()))


def _parse_deed_date(raw: str | None) -> dt.date | None:
    """Parse TAD deed_date → date; None on blank/sentinel/unparseable.

    TAD's doc reads `MM\\DD\\YYYY` (probably a doc-render glitch) but the
    actual shipped format is `MM-DD-YYYY` with dashes. We accept BOTH
    `MM-DD-YYYY` and `MM/DD/YYYY` defensively against vintage drift.

    Known TAD sentinels for "no recorded deed" → None:
      * blank / whitespace
      * `00/00/0000`, `00-00-0000`
      * `12/31/1900`, `12-31-1900`  (used very widely on city/county
        government-owned parcels and pre-PACS records)

    Year-range guard 1900-2100 matches the sibling ingests; only 1900
    dates that survive the sentinel check would actually reach the guard.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # Explicit sentinels for "no recorded deed". 12-31-1900 is TAD's
    # internal "no data" placeholder; 00/00/0000 covers truly empty values
    # that the exporter still padded into the field.
    sentinels = {
        "00/00/0000", "00-00-0000",
        "12/31/1900", "12-31-1900",
    }
    if text in sentinels:
        return None
    parsed: dt.datetime | None = None
    if "-" in text:
        try:
            parsed = dt.datetime.strptime(text, "%m-%d-%Y")
        except ValueError:
            parsed = None
    elif "/" in text:
        try:
            parsed = dt.datetime.strptime(text, "%m/%d/%Y")
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    if not (1900 <= parsed.year <= 2100):
        return None
    return parsed.date()


def _row_to_record(
    row: dict[str, str], year: int, source_file: str
) -> tuple | None:
    """Map one pipe-delimited row → upsert tuple, or None to skip.

    Skip when:
      * RP not in {'R', 'C'} (Personal Property / Minerals — out of scope)
      * Record_Type != 'AAAA' (not the primary real-estate record;
        LOCA records carry blank/garbage Owner_Name)
      * Account_Num empty after lstrip('0')
      * Owner_Name blank/whitespace

    Returns (county, account_num, year, owner_name, deed, source_file).
    """
    rp = (row.get("RP") or "").strip().upper()
    if rp not in ("R", "C"):
        return None
    record_type = (row.get("Record_Type") or "").strip().upper()
    if record_type != "AAAA":
        return None
    acct = (row.get("Account_Num") or "").strip()
    # Skip rows whose account_num is empty or all-zeros (TAD pads to 8
    # zeros for malformed/placeholder records); keep the zero-padded form
    # otherwise so it matches tad_parcels.account_num directly.
    if not acct or acct.lstrip("0") == "":
        return None
    owner = (row.get("Owner_Name") or "").strip()
    if not owner:
        return None
    deed = _parse_deed_date(row.get("Deed_Date"))
    return (COUNTY, acct, year, owner, deed, source_file)


def _check_appraisal_year_matches_folder(
    first_row: dict[str, str], expected_year: int, source_file: str
) -> None:
    """Sanity-check first data row's Appraisal_Year against the folder year.

    Prints a warning on mismatch but does not fail — catches the foot-gun
    where a file gets placed in the wrong year folder. Silent on missing
    or unparseable Appraisal_Year."""
    raw = (first_row.get("Appraisal_Year") or "").strip()
    if not raw:
        return
    try:
        seen = int(raw)
    except ValueError:
        return
    if seen != expected_year:
        print(
            f"  WARNING: {source_file} has Appraisal_Year={seen} on its first "
            f"row but is in folder for year={expected_year}; ingesting under "
            f"folder year (file may have been placed in the wrong folder)"
        )


def _stream_record_batches(
    path: str, year: int, source_file: str, batch_size: int = BATCH_SIZE
) -> Iterator[list[tuple]]:
    """Yield bounded batches of upsert tuples, streaming the file row-by-row.

    Peak memory is one batch (never the whole file or year), so ingesting a
    ~750k-row TAD roll (~700 MB on disk) stays at a few MB resident.

    Encoding is cp1252 (Windows-1252). `file(1)` reports the 2022 file as
    ASCII, but 2021 contains at least one byte 0x92 (right single quotation
    mark in cp1252 — likely from an owner name like "BARNEY'S TRUST" that
    was generated on a Windows system). cp1252 is a superset of ASCII so
    pure-ASCII years decode identically; the smart-quote byte resolves to
    U+2019 instead of crashing the ingest. cp1252 doesn't have undefined
    bytes in the 0x80-0xFF range we'd actually expect, so a strict-mode
    decode is safe. Pipe delimiter (|) per the file format; csv.DictReader
    parses on the header row.

    Filters at the row level via _row_to_record (RP in {R,C}, Record_Type
    == AAAA, non-blank account+owner). Per-file summary at end logs scanned
    vs emitted counts so PP/Mineral skip rate is visible without WARN noise
    (those skips are by-design and expected to be a large fraction).
    """
    scanned = 0
    skipped_non_real = 0    # RP not in {R,C} (Personal Property / Minerals)
    skipped_non_aaaa = 0    # Record_Type != AAAA (e.g. LOCA)
    skipped_blank_acct = 0
    skipped_blank_owner = 0
    emitted = 0

    batch: list[tuple] = []
    with open(path, "r", encoding="cp1252", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="|")
        checked_year = False
        for row in reader:
            scanned += 1
            if not checked_year:
                checked_year = True
                _check_appraisal_year_matches_folder(row, year, source_file)
            # Inline the skip classifier so we can count by reason without
            # re-parsing — _row_to_record returns None for any skip reason
            # but doesn't distinguish them, so we re-check the cheap fields
            # here (the body of _row_to_record stays simple for tests).
            rp = (row.get("RP") or "").strip().upper()
            if rp not in ("R", "C"):
                skipped_non_real += 1
                continue
            record_type = (row.get("Record_Type") or "").strip().upper()
            if record_type != "AAAA":
                skipped_non_aaaa += 1
                continue
            acct = (row.get("Account_Num") or "").strip()
            # Skip empty or all-zeros account_nums; keep the padded form
            # otherwise to match tad_parcels.account_num (8-char zero-padded).
            if not acct or acct.lstrip("0") == "":
                skipped_blank_acct += 1
                continue
            owner = (row.get("Owner_Name") or "").strip()
            if not owner:
                skipped_blank_owner += 1
                continue
            deed = _parse_deed_date(row.get("Deed_Date"))
            rec = (COUNTY, acct, year, owner, deed, source_file)
            batch.append(rec)
            emitted += 1
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch

    print(
        f"  {source_file}: scanned={scanned:,} "
        f"non_real(PP+M)={skipped_non_real:,} non_aaaa={skipped_non_aaaa:,} "
        f"blank_acct={skipped_blank_acct:,} blank_owner={skipped_blank_owner:,} "
        f"emitted={emitted:,}"
    )


def _create_table() -> None:
    """Create ownership_snapshots if it doesn't exist (DCAD ingest already
    creates it; this is a no-op when DCAD has run first, but kept so this
    script can stand alone)."""
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
    for batch in _stream_record_batches(path, year, source_file, batch_size):
        _upsert(batch)
        total += len(batch)
    print(f"{year}: upserted {total:,} records from {source_file}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest TAD ownership history.")
    ap.add_argument("--historical-dir", default=DEFAULT_HISTORICAL_DIR)
    args = ap.parse_args()

    year_files = _find_year_files(args.historical_dir)
    if not year_files:
        raise SystemExit(
            f"No PropertyData_<year>.txt found under {args.historical_dir}/<year>/. "
            "Download the TAD Standard Data PropertyData-FullSet(Certified) files first."
        )
    print(f"Years found: {sorted(year_files)}")
    _create_table()
    total = 0
    for year, path in year_files.items():
        total += _ingest_year(year, path)
    print(f"\nDone — {total:,} rows upserted across {len(year_files)} years.")


if __name__ == "__main__":
    main()
