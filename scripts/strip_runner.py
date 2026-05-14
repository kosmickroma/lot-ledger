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
