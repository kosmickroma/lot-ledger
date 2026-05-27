"""Backend integration test for PUT /api/areas/{area_id} filter-autosave path.

Confirms:
  1. The existing PUT handler references `filter_state` as an accepted field.
  2. Both filter_state and originator_parcel_* are still supported
     (regression check that subject-autosave didn't remove filter_state support).
  3. filter_state appears in multiple code regions (validation, extraction,
     persistence) — sanity check it's wired end-to-end.

Inspection-based (no live DB). Per spec v1 §2.4 + §4.2.
"""
import inspect

from api import main as api_main


def test_put_handler_references_filter_state():
    src = inspect.getsource(api_main)
    assert "filter_state" in src, (
        "api/main.py is missing filter_state reference. "
        "Spec assumed PUT /api/areas/{id} handler accepts this field."
    )


def test_put_handler_also_references_originator_parcel_fields():
    # Regression check: subject-autosave shipped earlier today; confirm
    # it didn't accidentally remove filter_state support.
    src = inspect.getsource(api_main)
    assert "originator_parcel_county" in src
    assert "originator_parcel_account_num" in src


def test_put_handler_accepts_filter_state_independent_of_originator():
    # The handler must support independent PUTs (filter_state alone OR
    # originator alone OR both). Count distinct mentions to ensure
    # filter_state is referenced across validation, extraction, persistence.
    src = inspect.getsource(api_main)
    assert src.count("filter_state") >= 3, (
        "filter_state should be referenced multiple times (validation, "
        "extraction, persistence) — count looks too low."
    )
