# scripts/validate_tad_extract.py
#
# CLI validator for local Tarrant/TAD extracted datasets.
# Enforces TAD ingest rules before adapter/load implementation.
#
# Connects to:
#   scripts/county_adapters/tad.py                      - adapter profile and checks
#   ingest/counties/tarrant/tad/<snapshot>/unzipped/    - extracted source datasets

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.county_adapters.tad import TarrantAdapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate extracted Tarrant/TAD datasets.")
    parser.add_argument(
        "--unzipped-root",
        default="ingest/counties/tarrant/tad/2026-05-01/unzipped",
        help="Path to extracted TAD dataset folder.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.unzipped_root)

    if not root.exists():
        print(f"ERROR: unzipped root does not exist: {root}")
        return 2

    adapter = TarrantAdapter()
    issues = adapter.validate_extract(root)

    print(f"Validated: {root}")
    if not issues:
        print("PASS: no issues found.")
        return 0

    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    infos = [i for i in issues if i.level == "info"]

    for issue in issues:
        location = f" ({issue.path})" if issue.path else ""
        print(f"{issue.level.upper():7} [{issue.code}] {issue.message}{location}")

    print("---")
    print(f"Summary: {len(errors)} error(s), {len(warnings)} warning(s), {len(infos)} info item(s)")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
