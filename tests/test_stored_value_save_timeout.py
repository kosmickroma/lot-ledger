"""tests/test_stored_value_save_timeout.py
Role: Regression guard for Mike's "stored values still not saving, fresh
browser fixes it" bug (2026-06-06).

Root cause: _storedValueSaveField PUT had no timeout. A hung Cloud SQL
or network drop left the fetch awaiting forever; the outer try/finally
in _storedValueProcessQueue never ran; _storedValueInflightField stayed
locked; every subsequent field save was silently skipped at the
`if (_storedValueInflightField) return;` guard. A fresh browser cleared
the JS state and saves worked again.

Fix: AbortController with a 15s timeout in _storedValueSaveField. On
timeout, the fetch rejects with AbortError, the catch sets the Retry
status, the outer finally releases the gate.

Connects to: frontend/map.js _storedValueSaveField
"""
from __future__ import annotations

import re
from pathlib import Path


MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


def test_save_field_uses_per_call_abort_controller() -> None:
    """The PUT must create a fresh AbortController per call so a single
    save's timeout doesn't affect any subsequent save."""
    src = _read()
    assert "const saveAbortController = new AbortController();" in src, (
        "_storedValueSaveField must instantiate a per-call AbortController "
        "(name: saveAbortController) so its timeout is scoped to that one "
        "save attempt."
    )


def test_save_field_arms_15s_timeout() -> None:
    """The timeout must fire abort after 15 seconds — bounded enough to
    catch real hangs, generous enough that healthy Cloud SQL latency
    (<500ms) never triggers it."""
    src = _read()
    pat = re.compile(
        r"const saveAbortTimer = setTimeout\(\(\) => saveAbortController\.abort\(\), 15_000\)",
    )
    assert pat.search(src), (
        "_storedValueSaveField must arm a 15s timeout that calls "
        "saveAbortController.abort()."
    )


def test_save_field_passes_signal_to_fetch() -> None:
    """The fetch must wire the AbortController.signal so the abort
    actually cancels the in-flight request (not just the JS timer)."""
    src = _read()
    pat = re.compile(
        r'fetch\(`/api/areas/\$\{encodeURIComponent\(areaIdAtCall\)\}/stored-value`,\s*\{[^}]*'
        r"signal:\s*saveAbortController\.signal",
        re.DOTALL,
    )
    assert pat.search(src), (
        "The PUT fetch must include signal: saveAbortController.signal so "
        "the abort actually cancels the in-flight request."
    )


def test_save_field_clears_timeout_in_finally() -> None:
    """The timer must be cleared in finally so a fast successful save
    doesn't accidentally abort a subsequent save's still-pending timer."""
    src = _read()
    pat = re.compile(
        r"\}\s*finally\s*\{\s*clearTimeout\(saveAbortTimer\);\s*\}",
        re.DOTALL,
    )
    assert pat.search(src), (
        "_storedValueSaveField must clear saveAbortTimer in a finally "
        "block so timers don't leak across saves."
    )


def test_abort_error_path_releases_gate_via_return() -> None:
    """On AbortError the catch must return (not throw) so the outer
    finally in _storedValueProcessQueue runs and releases the inflight
    gate. The pre-existing AbortError check did exactly this but was a
    bare 'return' with no logging; the new version warns + sets a Retry
    status so the user sees something happened."""
    src = _read()
    pat = re.compile(
        r'if \(err\.name === "AbortError"\) \{.*?'
        r'console\.warn\("\[stored-value\] save aborted.*?'
        r'_storedValueSetStatus\("error", "Retry"\);\s*'
        r'return;',
        re.DOTALL,
    )
    assert pat.search(src), (
        "AbortError branch must warn + set Retry status + return so the "
        "outer finally in _storedValueProcessQueue releases the inflight "
        "gate. Without this, the gate would still lock on every timeout."
    )
