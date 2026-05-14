#!/usr/bin/env python3
# scripts/strip_runner.py
#
# Strip Runner — runs Propelio comp pulls against a hand-curated address list.
# See docs/propelio/STRIP_RUNNER_SPEC.md (v1.3) for design rationale.
#
# Coupled to api/propelio/deep_pull.py — keep in sync if Propelio response shape changes.
#
# MUST be invoked from the repo root so api.propelio.* imports resolve:
#     cd /path/to/lot-ledger
#     python scripts/strip_runner.py --addresses scripts/strip_runner_addresses/<file>.txt

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


FILTERS: list[tuple[int, float]] = [
    # 24-month band
    (24, 5.0), (24, 2.0), (24, 1.0), (24, 0.5), (24, 0.25),
    # 12-month band
    (12, 5.0), (12, 2.0), (12, 1.0), (12, 0.5), (12, 0.25),
    # 6-month band
    (6, 5.0), (6, 2.0), (6, 1.0), (6, 0.5), (6, 0.25),
    # 3-month band
    (3, 1.0), (3, 0.5), (3, 0.25),
    # 1-month band
    (1, 1.0), (1, 0.5), (1, 0.25),
]
assert len(FILTERS) == 21, "FILTERS must have exactly 21 entries per spec §5"


def load_addresses(path: str) -> list[str]:
    """Read an address list file and return the stripped non-blank, non-comment lines.

    Raises FileNotFoundError if the file does not exist, ValueError if the
    file exists but contains no addresses after stripping comments and blanks.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"address file not found: {path}")

    # utf-8-sig auto-strips the UTF-8 BOM if present, so a BOM-prefixed first
    # line doesn't get parsed as a malformed address. Plain utf-8 leaves the BOM.
    with file_path.open("r", encoding="utf-8-sig") as fh:
        lines = fh.readlines()

    addresses: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        addresses.append(stripped)

    if not addresses:
        raise ValueError(f"address file is empty (no non-comment, non-blank lines): {path}")

    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description="Strip Runner — hand-curated Propelio comp sweep")
    parser.add_argument(
        "--addresses",
        required=True,
        help="Path to address list file (one address per line, # for comments).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the in-process MockPropelioClient instead of real Propelio. For smoke testing.",
    )
    args = parser.parse_args()

    print("strip_runner.py scaffolded; main loop not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
