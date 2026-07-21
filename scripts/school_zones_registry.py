"""Parser for docs/AI/SCHOOL_ZONES_COVERAGE_REGISTRY.md -- the hand-maintained
per-district expected-campus-count registry (docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md
§3a #3 "registry count floor" guard, and the Dallas-county ISD-name -> TEA
district-id crosswalk the runtime DB path uses for district_status).

Pure Markdown-table parsing, no network, no DB. The registry lives under
docs/ (gitignored, never committed -- this repo is public), so callers MUST
tolerate it being absent: return an empty registry rather than raising. A
missing registry degrades the count-floor guard to "skipped, warn" (§3a #3
is a defensive guard, not the ingest's only safety net -- the <50%-vs-
existing-rows tripwire, §3a #4, is DB-driven and always available) and
degrades the ISD-name crosswalk to "can't name the district" (matches spec
§6's own stated Tarrant/Collin fallback: a generic "not loaded yet").
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "AI" / "SCHOOL_ZONES_COVERAGE_REGISTRY.md"
)

_LEVEL_COL = {"E": "elementary", "M": "middle", "H": "high"}

# One row: | status | ISD | TEA# | E | M | H | class | source | vintage | spans |
_ROW_RE = re.compile(
    r"^\|\s*(?P<status>[^|]*)\|\s*(?P<isd>[^|]*)\|\s*(?P<tea>[^|]*)\|\s*"
    r"(?P<e>[^|]*)\|\s*(?P<m>[^|]*)\|\s*(?P<h>[^|]*)\|"
)


def _parse_int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.isdigit() else None


def load_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{tea_district_id: {"name": ISD name, "elementary": E, "middle": M,
    "high": H}}. Returns {} if the registry file is absent (never raises --
    see module docstring)."""
    p = path or REGISTRY_PATH
    if not p.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in p.read_text().splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        tea = m.group("tea").strip()
        if not re.fullmatch(r"\d{6}", tea):
            continue  # header / separator / non-data row
        out[tea] = {
            "name": m.group("isd").strip(),
            "elementary": _parse_int(m.group("e")),
            "middle": _parse_int(m.group("m")),
            "high": _parse_int(m.group("h")),
        }
    return out


def expected_counts_for_district(district_tea_id: str, path: Path | None = None) -> dict[str, int] | None:
    """{"elementary": N, "middle": N, "high": N} for the registry count-floor
    guard, or None if the district isn't in the registry (unknown ISD, or
    registry file absent) -- callers must treat None as "skip this guard,"
    never as "expect zero.\""""
    info = load_registry(path).get(district_tea_id)
    if not info:
        return None
    return {
        lvl: info[lvl] for lvl in ("elementary", "middle", "high") if info.get(lvl) is not None
    }


def dallas_isd_name_to_tea_id(path: Path | None = None) -> dict[str, str]:
    """{ISD NAME (upper, as it appears in CAD isd_desc columns): tea_district_id}
    for Dallas-county rows only -- this is the crosswalk the DB runtime path
    (api/school_pilot/zones_db.py) uses to turn the parcel's `dcad:<name>`
    isd string into a district_tea_id for district_status. Built once from
    the SAME registry doc, never hand-duplicated."""
    p = path or REGISTRY_PATH
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    in_dallas = False
    for line in p.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_dallas = stripped.startswith("## Dallas County")
            continue
        if not in_dallas:
            continue
        m = _ROW_RE.match(stripped)
        if not m:
            continue
        tea = m.group("tea").strip()
        if not re.fullmatch(r"\d{6}", tea):
            continue
        out[m.group("isd").strip().upper()] = tea
    return out
