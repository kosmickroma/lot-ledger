"""Tests for scripts/verify_school_zones_equivalence.py -- the "Gap 1"
acceptance-gate script comparing the static and DB school-lookup paths.
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "Gap 1."

Pure-function tests (no DB, no real assign_static/assign_db calls) for the
comparison logic itself, plus source-inspection guards for the safety
properties the spec calls out (in-process comparison, no SCHOOL_SOURCE flip,
--parcel-sample defaults off / read-only only). The actual live comparison
(seeded mismatch caught + clean pass) is exercised in the mandatory
throwaway-DB rehearsal, not here.
"""
from __future__ import annotations

from pathlib import Path

from scripts.verify_school_zones_equivalence import (
    SMOKE_POINTS,
    _level_signature,
    diff_point,
)

SRC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_school_zones_equivalence.py"


def _read() -> str:
    return SRC_PATH.read_text()


# --- _level_signature ---------------------------------------------------------

def test_level_signature_none_result_is_all_none() -> None:
    assert _level_signature(None, "elementary") == (None, None, None)


def test_level_signature_missing_level_is_all_none() -> None:
    assert _level_signature({"elementary": None}, "elementary") == (None, None, None)


def test_level_signature_extracts_name_rating_score() -> None:
    result = {"elementary": {"name": "Bowie Elementary", "rating": "B", "score": 85, "achievement": {"grade": "B", "score": 80}}}
    assert _level_signature(result, "elementary") == ("Bowie Elementary", "B", 85)


def test_level_signature_ignores_achievement_and_growth() -> None:
    # Per spec: compare name + rating + score only.
    a = {"elementary": {"name": "X", "rating": "A", "score": 90, "achievement": {"grade": "A", "score": 95}}}
    b = {"elementary": {"name": "X", "rating": "A", "score": 90, "achievement": {"grade": "F", "score": 10}}}
    assert _level_signature(a, "elementary") == _level_signature(b, "elementary")


# --- diff_point (monkeypatched assign_static/assign_db) ----------------------

def test_diff_point_no_diffs_when_identical(monkeypatch) -> None:
    import scripts.verify_school_zones_equivalence as mod

    same = {"elementary": {"name": "X", "rating": "A", "score": 90}, "middle": None, "high": None}
    monkeypatch.setattr(mod, "assign_static", lambda lat, lng: dict(same))
    monkeypatch.setattr(mod, "assign_db", lambda lat, lng: dict(same))
    assert mod.diff_point(32.8, -96.8) == []


def test_diff_point_reports_a_mismatched_level(monkeypatch) -> None:
    import scripts.verify_school_zones_equivalence as mod

    monkeypatch.setattr(mod, "assign_static", lambda lat, lng: {
        "elementary": {"name": "X", "rating": "A", "score": 90}, "middle": None, "high": None,
    })
    monkeypatch.setattr(mod, "assign_db", lambda lat, lng: {
        "elementary": {"name": "X", "rating": "B", "score": 85}, "middle": None, "high": None,
        "district_status": "ingested",
    })
    diffs = mod.diff_point(32.8, -96.8)
    assert len(diffs) == 1
    level, static_sig, db_sig = diffs[0]
    assert level == "elementary"
    assert static_sig == ("X", "A", 90)
    assert db_sig == ("X", "B", 85)


def test_diff_point_body_never_compares_district_status() -> None:
    # district_status only exists on the DB response (§6's 3-state
    # contract) -- the static path's shape has no such key, so diff_point
    # must never compare it (that would report a permanent false mismatch
    # on every single point).
    src = _read()
    fn_start = src.index("def diff_point")
    fn_end = src.index("\ndef ", fn_start + 1)
    assert "district_status" not in src[fn_start:fn_end]


# --- smoke points -------------------------------------------------------------

def test_smoke_points_has_3_entries_matching_the_pilot_smoke_test() -> None:
    assert len(SMOKE_POINTS) == 3
    labels = {label for label, _lat, _lng in SMOKE_POINTS}
    assert any("Wilson" in l for l in labels)
    assert any("Bowie" in l for l in labels)
    assert any("Holmes" in l for l in labels)


# --- source-inspection: safety properties ------------------------------------

def test_compares_in_process_no_school_source_env_flip() -> None:
    # Comments legitimately explain WHY this script avoids SCHOOL_SOURCE;
    # the guarantee under test is that nothing actually READS the env var.
    src = _read()
    assert 'os.getenv("SCHOOL_SOURCE"' not in src
    assert "os.environ" not in src


def test_parcel_sample_defaults_to_zero() -> None:
    src = _read()
    assert '"--parcel-sample", type=int, default=0' in src


def test_parcel_sample_query_is_read_only() -> None:
    src = _read()
    fn_start = src.index("def sample_disd_parcel_centroids")
    fn_end = src.index("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end].upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "ALTER "):
        assert verb not in body
    assert "SELECT " in body


def test_exits_nonzero_on_any_mismatch() -> None:
    src = _read()
    assert "return 1 if mismatched else 0" in src
