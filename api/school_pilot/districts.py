# School Ratings — static district reference data for the RUNTIME path.
#
# ⚠️ Why this is a hardcoded constant and NOT read from the coverage registry:
# the registry file (docs/AI/SCHOOL_ZONES_COVERAGE_REGISTRY.md) is BOTH
# gitignored AND dockerignored (docs/ + scripts/ are excluded from the built
# container). The runtime read path must never import from scripts/ or depend
# on an operator-only file -- doing so 500s every /api/school-pilot/assign
# request in a deployed container (the packaging bug found 2026-07-21:
# ModuleNotFoundError: No module named 'scripts'). The registry file remains
# what it was meant to be: operator tooling for the ingest guards. Anything
# the *runtime* needs lives here, in api/, as first-party static data.
#
# These 14 Dallas-county name->TEA-district-id pairs are only used to NAME a
# not-yet-ingested district in the "not loaded yet" note. Ingested districts
# self-resolve from their zone rows; this map never affects a district we
# actually have. Keys are the ISD name as it appears in CAD isd_desc
# (upper-cased), matching api/ai/enrichment.py's `dcad:<NAME>` form.

# Dallas county traditional ISDs (TEA 2025 roster). Best-effort naming only.
DALLAS_ISD_NAME_TO_TEA_ID: dict[str, str] = {
    "CARROLLTON-FARMERS BRANCH ISD": "057903",
    "CEDAR HILL ISD": "057904",
    "COPPELL ISD": "057922",
    "DALLAS ISD": "057905",
    "DESOTO ISD": "057906",
    "DUNCANVILLE ISD": "057907",
    "GARLAND ISD": "057909",
    "GRAND PRAIRIE ISD": "057910",
    "HIGHLAND PARK ISD": "057911",
    "IRVING ISD": "057912",
    "LANCASTER ISD": "057913",
    "MESQUITE ISD": "057914",
    "RICHARDSON ISD": "057916",
    "SUNNYVALE ISD": "057919",
}

# Districts with NO attendance zones by design (court-ordered open enrollment).
# Garland ISD is confirmed; extend as the team's roster is verified.
OPEN_ENROLLMENT_DISTRICT_TEA_IDS: set[str] = {"057909"}  # GARLAND ISD
