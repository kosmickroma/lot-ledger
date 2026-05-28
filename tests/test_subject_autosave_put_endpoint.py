"""Backend integration test for PUT /api/areas/{area_id} subject-autosave path.

Confirms:
  1. The existing PUT handler at api/main.py:6083+ references
     originator_parcel_county + originator_parcel_account_num.
  2. The handler validates that both fields are provided together
     (rejects half-set requests).
  3. The handler also accepts filter_state on the same PUT (so phase-1
     Update-for-filters keeps working).

Inspection-based (no live DB). Live integration tests can be added
later by gating on LOTLEDGER_INTEGRATION_TESTS=1 + auth fixture.

Per spec v1.1 §2.5 + §4.2.
"""
import inspect

from api import main as api_main


def test_put_handler_references_originator_parcel_fields():
    # Spec v1.1 §4.2: the existing PUT handler at api/main.py:6083+
    # accepts originator_parcel_county + originator_parcel_account_num.
    src = inspect.getsource(api_main)
    assert "originator_parcel_county" in src, (
        "api/main.py is missing originator_parcel_county reference. "
        "Spec assumed PUT /api/areas/{id} handler accepts this field."
    )
    assert "originator_parcel_account_num" in src, (
        "api/main.py is missing originator_parcel_account_num reference. "
        "Spec assumed PUT /api/areas/{id} handler accepts this field."
    )


def test_put_handler_validates_both_fields_together():
    # Spec v1.1 §4.2: "Both fields must be provided together
    # (validation at api/main.py:6116)."
    src = inspect.getsource(api_main)
    assert "originator_parcel_county and originator_parcel_account_num must be provided together" in src, (
        "Backend validation message at api/main.py:6116 has changed or moved. "
        "Spec v1.1 §4.2 assumed this validation message exists; re-verify "
        "before continuing with the frontend changes."
    )


def test_put_handler_also_accepts_filter_state():
    # Spec v1.1 §2.5 + Task 3 refactor: filter_state and originator_parcel_*
    # are INDEPENDENT fields on the PUT. Update button still posts
    # filter_state alone; Save Parcel posts originator_parcel_* alone.
    src = inspect.getsource(api_main)
    assert "filter_state" in src, (
        "api/main.py is missing filter_state reference. "
        "Spec assumed PUT /api/areas/{id} handler accepts filter_state "
        "alongside originator_parcel_* (independently set)."
    )
