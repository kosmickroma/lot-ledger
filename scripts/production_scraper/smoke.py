#!/usr/bin/env python3
"""Inline smoke tests for the production scraper.

Run: ``python scripts/production_scraper/smoke.py``

Mirrors strip_runner_smoke.py shape — each test is a top-level ``_test_*``
function. The driver at the bottom invokes them in order, prints
PASS/FAIL per test, and exits non-zero on any failure.

Per PRODUCTION_SCRAPER_SPEC v1.2 §12.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure both the repo root (for ``from api.propelio.*``) and the
# ``scripts/`` directory (for ``from production_scraper.*``) are on sys.path.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]  # …/lot-ledger/
_SCRIPTS = _THIS.parents[1]    # …/lot-ledger/scripts/
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Test #1 — address parser (master_list.txt → normalized queue)
# ---------------------------------------------------------------------------

def _test_address_parser_basic() -> None:
    """A simple file with comments + blanks + a few addresses parses to the
    expected normalized queue in source order."""
    from production_scraper.run import load_master_list

    src = (
        "# comment\n"
        "\n"
        "1234 Main St, Dallas, TX\n"
        "  5678 Oak Ave, Plano, TX  \n"
        "# another comment\n"
        "9012 Pine Rd, Frisco, TX\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        queue = load_master_list(path)
    finally:
        os.unlink(path)
    assert queue == [
        "1234 MAIN ST, DALLAS, TX",
        "5678 OAK AVE, PLANO, TX",
        "9012 PINE RD, FRISCO, TX",
    ], f"unexpected queue: {queue}"


def _test_address_parser_dedup() -> None:
    """Duplicate addresses (case-insensitive) appear only once; first
    occurrence wins for ordering."""
    from production_scraper.run import load_master_list

    src = (
        "1234 Main St, Dallas, TX\n"
        "5678 Oak Ave, Plano, TX\n"
        "1234 main st, dallas, tx\n"  # dup of first, different case
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        queue = load_master_list(path)
    finally:
        os.unlink(path)
    assert queue == [
        "1234 MAIN ST, DALLAS, TX",
        "5678 OAK AVE, PLANO, TX",
    ], f"unexpected queue: {queue}"


def _test_address_parser_rejects_missing_city() -> None:
    """A line without a comma (no city) → ValueError with line number."""
    from production_scraper.run import load_master_list

    src = (
        "1234 Main St, Dallas, TX\n"
        "Bad Line Without Comma\n"
        "9012 Pine Rd, Frisco, TX\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        try:
            load_master_list(path)
        except ValueError as exc:
            assert "line 2" in str(exc).lower() or "line=2" in str(exc).lower(), \
                f"expected line-number in error, got: {exc}"
            return
        raise AssertionError("expected ValueError for missing-comma line")
    finally:
        os.unlink(path)


def _test_address_parser_empty_file() -> None:
    """A file with only comments + blanks (no addresses) → ValueError."""
    from production_scraper.run import load_master_list

    src = "# just a comment\n\n#another\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        try:
            load_master_list(path)
        except ValueError:
            return
        raise AssertionError("expected ValueError for empty file")
    finally:
        os.unlink(path)


def _test_address_parser_bom() -> None:
    """A file with a UTF-8 BOM prefix on the first line still parses
    correctly (the BOM is stripped, not retained in the first address)."""
    from production_scraper.run import load_master_list

    src = "﻿1234 Main St, Dallas, TX\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        queue = load_master_list(path)
    finally:
        os.unlink(path)
    assert queue == ["1234 MAIN ST, DALLAS, TX"], f"unexpected queue: {queue}"


# ---------------------------------------------------------------------------
# Test #2 — profile resolver
# ---------------------------------------------------------------------------

def _test_profile_resolver_seed_5y() -> None:
    """seed_5y returns 60 months × 5 distances."""
    from production_scraper.run import resolve_profile

    p = resolve_profile("seed_5y")
    assert p["months"] == 60, p
    assert p["distances_mi"] == [0.25, 0.5, 1.0, 2.0, 5.0], p


def _test_profile_resolver_monthly_1m() -> None:
    """monthly_1m returns 1 month × 5 distances."""
    from production_scraper.run import resolve_profile

    p = resolve_profile("monthly_1m")
    assert p["months"] == 1, p
    assert p["distances_mi"] == [0.25, 0.5, 1.0, 2.0, 5.0], p


def _test_profile_resolver_unknown_lists_valid() -> None:
    """Unknown profile name → ValueError that names the valid profiles."""
    from production_scraper.run import resolve_profile

    try:
        resolve_profile("nonexistent")
    except ValueError as exc:
        msg = str(exc)
        assert "seed_5y" in msg and "monthly_1m" in msg, \
            f"expected valid-profile-names in error, got: {msg}"
        return
    raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Test #3 — state file load/save + list_sha256
# ---------------------------------------------------------------------------

def _test_state_fresh_load_when_missing() -> None:
    """Loading from an empty state dir returns the fresh schema."""
    from production_scraper.run import load_state

    with tempfile.TemporaryDirectory() as td:
        state = load_state(Path(td))
    assert state == {
        "schema_version": 1,
        "current_pass": None,
        "history": [],
    }, state


def _test_state_round_trip_preserves_fields() -> None:
    """save_state → load_state preserves every field of a non-trivial state."""
    from production_scraper.run import load_state, save_state

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        state = {
            "schema_version": 1,
            "current_pass": {
                "pass_id": "20260526T150000Z-seed_5y",
                "started_at": "2026-05-26T15:00:00+00:00",
                "completed_at": None,
                "profile": "seed_5y",
                "profile_snapshot": {"months": 60, "distances_mi": [0.25, 0.5, 1.0, 2.0, 5.0]},
                "list_path": "scripts/production_scraper/master_list.txt",
                "list_sha256": "abc123",
                "list_snapshot": ["1234 MAIN ST, DALLAS, TX"],
                "addresses": {
                    "1234 MAIN ST, DALLAS, TX": {"status": "pending"},
                },
            },
            "history": [
                {"pass_id": "old", "completed_at": "2026-05-24T08:15:00+00:00", "addresses_done": 100},
            ],
        }
        save_state(td_path, state)
        reloaded = load_state(td_path)
    assert reloaded == state, f"round-trip mismatch:\n got: {reloaded}\nwant: {state}"


def _test_state_atomic_write_leaves_no_tmp() -> None:
    """After a successful save, no .tmp file remains in the state dir."""
    from production_scraper.run import save_state

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        save_state(td_path, {"schema_version": 1, "current_pass": None, "history": []})
        leftover = [p.name for p in td_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == [], f"unexpected .tmp left behind: {leftover}"


def _test_state_atomic_write_preserves_prior_on_crash() -> None:
    """If os.replace fails mid-rename, the prior state.json remains intact."""
    from production_scraper.run import load_state, save_state

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        good = {"schema_version": 1, "current_pass": None, "history": [{"pass_id": "good"}]}
        save_state(td_path, good)

        # Simulate a crash during the next save: monkeypatch os.replace to raise.
        import os as _os
        original_replace = _os.replace
        def _boom(*args, **kwargs):
            raise OSError("simulated crash mid-rename")
        _os.replace = _boom
        try:
            try:
                save_state(td_path, {"schema_version": 1, "current_pass": "BAD", "history": []})
            except OSError:
                pass
        finally:
            _os.replace = original_replace

        reloaded = load_state(td_path)
    assert reloaded == good, f"prior state lost: got {reloaded}"


def _test_list_sha256_normalized_queue() -> None:
    """compute_list_sha256 hashes the joined queue. Reordering changes the hash."""
    from production_scraper.run import compute_list_sha256

    q1 = ["1234 MAIN ST, DALLAS, TX", "5678 OAK AVE, PLANO, TX"]
    q2 = ["5678 OAK AVE, PLANO, TX", "1234 MAIN ST, DALLAS, TX"]
    h1 = compute_list_sha256(q1)
    h2 = compute_list_sha256(q2)
    assert isinstance(h1, str) and len(h1) == 64, f"expected 64-hex SHA-256, got {h1!r}"
    assert h1 != h2, "reorder should change the hash"
    # Stable: same input → same hash
    assert compute_list_sha256(q1) == h1


# ---------------------------------------------------------------------------
# Test #4 — run lock (acquire fresh, stale-PID takeover, live-PID rejection)
# ---------------------------------------------------------------------------

def _test_lock_acquire_on_fresh_state() -> None:
    """Lock acquires cleanly when state dir is empty. Writes PID file."""
    from production_scraper.run import RunLock

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with RunLock(td_path) as lock:
            # Lock file should exist with our PID inside
            lock_path = td_path / "run.lock"
            assert lock_path.exists(), "lock file not created"
            contents = lock_path.read_text(encoding="utf-8")
            assert str(os.getpid()) in contents, f"expected PID in lock file, got: {contents!r}"


def _test_lock_rejects_live_pid() -> None:
    """A second RunLock instance on the same dir fails while the first holds it."""
    from production_scraper.run import RunLock, RunLockBusy

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with RunLock(td_path):
            # Try to acquire a second lock on the same dir — should fail.
            try:
                with RunLock(td_path):
                    raise AssertionError("expected RunLockBusy, but second acquire succeeded")
            except RunLockBusy:
                return


def _test_lock_takes_over_stale_pid() -> None:
    """If the lock file holds a dead PID, the new acquire succeeds (with the
    intent to log a warning — we just verify acquisition here)."""
    from production_scraper.run import RunLock

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        lock_path = td_path / "run.lock"
        # Write a clearly-dead PID. PID 2^31-1 is reserved/won't exist.
        lock_path.write_text("2147483646\nold_timestamp\n", encoding="utf-8")
        with RunLock(td_path):
            contents = lock_path.read_text(encoding="utf-8")
            assert str(os.getpid()) in contents, \
                f"stale-PID takeover should overwrite with our PID, got: {contents!r}"


def _test_lock_released_on_context_exit() -> None:
    """After ``with RunLock(...)`` exits, a new acquire on the same dir
    succeeds (the flock was released, the file was unlinked)."""
    from production_scraper.run import RunLock

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with RunLock(td_path):
            pass
        # Now re-acquire — should work
        with RunLock(td_path):
            pass


# ---------------------------------------------------------------------------
# Test #5 — error classifier (split: 429 vs 401/403 vs non-auth)
# ---------------------------------------------------------------------------

def _make_exc_with_status(status: int) -> Exception:
    """Build an exception that mimics the strip_runner / deep_pull
    convention of carrying a status_code (or a .response.status_code)."""
    exc = Exception(f"HTTP {status} something")
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


def _test_classifier_429_via_status_code() -> None:
    from production_scraper.run import is_429, is_401_or_403
    e = _make_exc_with_status(429)
    assert is_429(e) is True
    assert is_401_or_403(e) is False


def _test_classifier_401_via_status_code() -> None:
    from production_scraper.run import is_429, is_401_or_403
    e = _make_exc_with_status(401)
    assert is_401_or_403(e) is True
    assert is_429(e) is False


def _test_classifier_403_via_status_code() -> None:
    from production_scraper.run import is_429, is_401_or_403
    e = _make_exc_with_status(403)
    assert is_401_or_403(e) is True
    assert is_429(e) is False


def _test_classifier_429_via_message_fragment() -> None:
    from production_scraper.run import is_429, is_401_or_403
    for msg in ("rate limit exceeded", "request was throttled", "too many requests"):
        e = Exception(msg)
        assert is_429(e) is True, f"missed 429 fragment: {msg}"
        assert is_401_or_403(e) is False, f"false-positive 401/403 on: {msg}"


def _test_classifier_401_403_via_message_fragment() -> None:
    from production_scraper.run import is_429, is_401_or_403
    for msg in ("Unauthorized", "FORBIDDEN ACCESS"):
        e = Exception(msg)
        assert is_401_or_403(e) is True, f"missed 401/403 fragment: {msg}"
        assert is_429(e) is False, f"false-positive 429 on: {msg}"


def _test_classifier_non_auth() -> None:
    from production_scraper.run import is_429, is_401_or_403
    e = Exception("connection reset by peer")
    assert is_429(e) is False
    assert is_401_or_403(e) is False


def _test_classifier_does_not_false_positive_on_address_digits() -> None:
    """Regression for 2026-05-27 PM hotfix: error messages that contain
    HTTP-status-like digit sequences as STREET NUMBERS must not classify
    as auth/rate-limit. PropelioScraperError "No parcel match for
    '401 HASSETT AVE…'" was misclassified as 401 unauth, exit-2'ing the
    scraper on the first address."""
    from production_scraper.run import is_429, is_401_or_403
    for msg in (
        "No parcel match for '401 HASSETT AVE, RIVER OAKS, TX'",
        "No parcel match for '403 FOREST DR, ANYWHERE, TX'",
        "No parcel match for '429 SOMEWHERE LN, CITY, TX'",
        "No parcel match for '1401 S FERGUSON PKWY, ANNA, TX'",  # 401 mid-substring
        "lead lookup failed at 4029 OAK ST",
    ):
        e = Exception(msg)
        assert is_429(e) is False, f"false-positive 429 on: {msg!r}"
        assert is_401_or_403(e) is False, f"false-positive 401/403 on: {msg!r}"


def _test_classifier_status_via_response_attribute() -> None:
    """Some exceptions carry status via exc.response.status_code (requests style)."""
    from production_scraper.run import is_429, is_401_or_403

    class _FakeResp:
        status_code = 429

    e = Exception("upstream HTTP error")
    e.response = _FakeResp()  # type: ignore[attr-defined]
    assert is_429(e) is True
    assert is_401_or_403(e) is False


# ---------------------------------------------------------------------------
# Test #6 — repo-root walking-upward sanity check
# ---------------------------------------------------------------------------

def _test_repo_root_finds_real_root() -> None:
    """Walking up from this very smoke.py should find the lot-ledger root
    that contains api/propelio/__init__.py."""
    from production_scraper.run import find_repo_root

    root = find_repo_root(start=Path(__file__).resolve())
    assert (root / "api" / "propelio" / "__init__.py").exists(), \
        f"find_repo_root returned {root} which does not contain api/propelio/__init__.py"


def _test_repo_root_raises_when_not_found() -> None:
    """Walking up from /tmp (no api/propelio anywhere) raises ValueError
    with the documented error wording."""
    from production_scraper.run import find_repo_root

    with tempfile.TemporaryDirectory() as td:
        # The temp dir is somewhere under /tmp; walking up will never hit
        # api/propelio/__init__.py.
        try:
            find_repo_root(start=Path(td))
        except ValueError as exc:
            assert "api/propelio" in str(exc), \
                f"expected api/propelio in error wording, got: {exc}"
            return
    raise AssertionError("expected ValueError when api/propelio not found")


# ---------------------------------------------------------------------------
# Test #7 — PropelioClient.login(force=False)  (api/propelio/scraper.py change)
# ---------------------------------------------------------------------------

def _make_propelio_client_with_fake_post():
    """Build a PropelioClient with proxies disabled, and monkey-patch
    ``client.session.post`` to a recordable fake. Returns
    ``(client, call_log)``."""
    from api.propelio.scraper import PropelioClient
    client = PropelioClient("u@example.com", "p", proxies={})

    call_log: list[dict] = []
    fake_token_counter = {"n": 0}

    class _FakeResponse:
        def __init__(self) -> None:
            fake_token_counter["n"] += 1
            self._n = fake_token_counter["n"]
            self.ok = True
            self.status_code = 200
            self.text = ""
            self.cookies = {}

        def json(self) -> dict:
            return {"token": f"TOKEN_{self._n}"}

    def fake_post(url, **kwargs):
        call_log.append({"url": url, "kwargs": dict(kwargs)})
        return _FakeResponse()

    client.session.post = fake_post  # type: ignore[assignment]
    return client, call_log


def _test_login_default_is_idempotent() -> None:
    """login() default (force=False) on an already-logged-in client is a no-op."""
    client, call_log = _make_propelio_client_with_fake_post()
    client.login()
    assert len(call_log) == 1, f"expected 1 HTTP call after first login(), got {len(call_log)}"
    assert client._logged_in is True
    client.login()  # default force=False
    assert len(call_log) == 1, f"second login() should be no-op, got {len(call_log)} HTTP calls"


def _test_login_force_true_clears_state_and_re_posts() -> None:
    """login(force=True) clears _logged_in + Authorization + token, re-posts,
    and re-establishes them with the fresh response."""
    client, call_log = _make_propelio_client_with_fake_post()
    client.login()
    assert client._token == "TOKEN_1"
    assert client.session.headers.get("Authorization") == "Bearer TOKEN_1"
    assert len(call_log) == 1

    # Force re-login
    client.login(force=True)
    assert len(call_log) == 2, f"force=True should re-POST, got {len(call_log)} HTTP calls"
    assert client._token == "TOKEN_2", f"expected fresh token, got {client._token!r}"
    assert client.session.headers.get("Authorization") == "Bearer TOKEN_2"
    assert client._logged_in is True


def _test_login_force_clears_cookies() -> None:
    """login(force=True) clears the session cookie jar before re-posting
    (so a stale session cookie can't poison the re-login)."""
    client, call_log = _make_propelio_client_with_fake_post()
    client.login()
    client.session.cookies.set("propelio_session", "STALE")
    assert "propelio_session" in client.session.cookies
    client.login(force=True)
    assert "propelio_session" not in client.session.cookies, \
        "force=True should have cleared the cookie jar"


# ---------------------------------------------------------------------------
# Test #8 — call_with_auth_retry (re-login + retry routing)
# ---------------------------------------------------------------------------

class _FakeClient:
    """Stand-in for PropelioClient just for the re-login path tests.
    Tracks ``login(force=...)`` invocations."""
    def __init__(self) -> None:
        self.login_calls: list[bool] = []  # records force= for each call

    def login(self, force: bool = False) -> None:
        self.login_calls.append(force)


def _test_auth_retry_success_path_no_retry() -> None:
    """Call succeeds on first try → no re-login, recovered=False."""
    from production_scraper.run import call_with_auth_retry

    client = _FakeClient()
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return "OK"

    result, recovered = call_with_auth_retry(client, fn)
    assert result == "OK"
    assert recovered is False
    assert calls["n"] == 1
    assert client.login_calls == []  # no re-login


def _test_auth_retry_429_propagates_no_relogin() -> None:
    """429 raises through immediately without attempting re-login."""
    from production_scraper.run import call_with_auth_retry

    client = _FakeClient()
    err = _make_exc_with_status(429)
    def fn():
        raise err

    try:
        call_with_auth_retry(client, fn)
    except Exception as exc:
        assert exc is err, f"expected original exception propagated, got {exc!r}"
        assert client.login_calls == [], "should NOT have called login on 429"
        return
    raise AssertionError("expected exception")


def _test_auth_retry_401_triggers_relogin_and_retry_succeeds() -> None:
    """401 → login(force=True) + retry. Retry succeeds → return (result, True)."""
    from production_scraper.run import call_with_auth_retry

    client = _FakeClient()
    attempts = {"n": 0}
    def fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _make_exc_with_status(401)
        return "RECOVERED"

    result, recovered = call_with_auth_retry(client, fn)
    assert result == "RECOVERED"
    assert recovered is True
    assert attempts["n"] == 2
    assert client.login_calls == [True], f"expected one force-login, got {client.login_calls}"


def _test_auth_retry_401_then_401_propagates_second_exc() -> None:
    """401 → re-login → retry returns 401 again → propagate the retry exception."""
    from production_scraper.run import call_with_auth_retry

    client = _FakeClient()
    exc_a = _make_exc_with_status(401)
    exc_b = _make_exc_with_status(401)
    attempts = {"n": 0}
    def fn():
        attempts["n"] += 1
        raise exc_a if attempts["n"] == 1 else exc_b

    try:
        call_with_auth_retry(client, fn)
    except Exception as exc:
        assert exc is exc_b, f"expected retry's exception, got {exc!r}"
        assert client.login_calls == [True]
        return
    raise AssertionError("expected exception")


def _test_auth_retry_relogin_failure_propagates() -> None:
    """401 detected → login(force=True) itself raises → propagate the login error."""
    from production_scraper.run import call_with_auth_retry

    class _BadLoginClient:
        def login(self, force: bool = False) -> None:
            raise RuntimeError("login backend down")

    client = _BadLoginClient()
    def fn():
        raise _make_exc_with_status(401)

    try:
        call_with_auth_retry(client, fn)
    except RuntimeError as exc:
        assert "login backend down" in str(exc)
        return
    raise AssertionError("expected RuntimeError from login")


def _test_auth_retry_non_auth_error_propagates() -> None:
    """Non-auth exception on first call → propagated, no re-login."""
    from production_scraper.run import call_with_auth_retry

    client = _FakeClient()
    err = RuntimeError("connection reset by peer")
    def fn():
        raise err

    try:
        call_with_auth_retry(client, fn)
    except RuntimeError as exc:
        assert exc is err
        assert client.login_calls == []
        return
    raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------------------
# Test #9 — merge_comps_into_global_with_retry (DB retry wrapper)
# ---------------------------------------------------------------------------

class _FakeConn:
    """Stand-in psycopg2 connection. Tracks commit/rollback/close calls."""
    def __init__(self, label: str) -> None:
        self.label = label
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _install_archive_mocks(*, conn_seq, inner_outcomes):
    """Patch api.propelio.archive's module-level references so the retry
    wrapper runs without a real DB. ``conn_seq`` is the list of _FakeConn
    objects to hand out from get_session_conn. ``inner_outcomes`` is a
    list of (kind, value) pairs: ('return', dict_or_value) or
    ('raise', exception_instance).  Returns a dict with call counters."""
    import api.propelio.archive as archive_mod

    counters = {
        "get": 0, "release": [], "discard": [], "inner": 0,
    }
    conn_iter = iter(conn_seq)
    outcome_iter = iter(inner_outcomes)

    def fake_get_conn():
        counters["get"] += 1
        return next(conn_iter)

    def fake_release(conn):
        counters["release"].append(conn.label)

    def fake_discard(conn):
        counters["discard"].append(conn.label)

    def fake_inner(conn, comps, source):
        counters["inner"] += 1
        kind, value = next(outcome_iter)
        if kind == "raise":
            raise value
        return value

    # Stash originals for restoration.
    originals = {
        "get_session_conn": archive_mod.get_session_conn,
        "release_session_conn": archive_mod.release_session_conn,
        "discard_session_conn": getattr(archive_mod, "discard_session_conn", None),
        "_merge_comps_into_global_inner": getattr(archive_mod, "_merge_comps_into_global_inner", None),
    }
    archive_mod.get_session_conn = fake_get_conn
    archive_mod.release_session_conn = fake_release
    archive_mod.discard_session_conn = fake_discard
    archive_mod._merge_comps_into_global_inner = fake_inner

    def restore():
        for k, v in originals.items():
            if v is None:
                if hasattr(archive_mod, k):
                    delattr(archive_mod, k)
            else:
                setattr(archive_mod, k, v)

    return counters, restore


def _test_merge_retry_success_no_retry() -> None:
    """First call succeeds → no retry, conn released (not discarded)."""
    import api.propelio.archive as archive_mod
    conn_a = _FakeConn("A")
    counters, restore = _install_archive_mocks(
        conn_seq=[conn_a],
        inner_outcomes=[("return", {"inserted": 5, "updated": 2})],
    )
    try:
        result = archive_mod.merge_comps_into_global_with_retry([{"x": 1}], "production_scraper")
    finally:
        restore()
    assert result == {"inserted": 5, "updated": 2}
    assert counters["get"] == 1
    assert counters["release"] == ["A"]
    assert counters["discard"] == []
    assert counters["inner"] == 1


def _test_merge_retry_connection_liveness_recovers() -> None:
    """OperationalError on first call → discard, get new conn, retry succeeds."""
    import api.propelio.archive as archive_mod
    import psycopg2

    conn_a = _FakeConn("A")
    conn_b = _FakeConn("B")
    counters, restore = _install_archive_mocks(
        conn_seq=[conn_a, conn_b],
        inner_outcomes=[
            ("raise", psycopg2.OperationalError("server closed the connection unexpectedly")),
            ("return", {"inserted": 3, "updated": 0}),
        ],
    )
    try:
        result = archive_mod.merge_comps_into_global_with_retry([{"x": 1}], "production_scraper")
    finally:
        restore()
    assert result == {"inserted": 3, "updated": 0}
    assert counters["get"] == 2, "expected two conns acquired"
    assert counters["discard"] == ["A"], "expected first conn discarded"
    assert counters["release"] == ["B"], "expected second conn released to pool"
    assert conn_a.rollback_calls == 1


def _test_merge_retry_connection_liveness_twice_propagates() -> None:
    """OperationalError on retry too → propagate the second exception."""
    import api.propelio.archive as archive_mod
    import psycopg2

    conn_a = _FakeConn("A")
    conn_b = _FakeConn("B")
    second_exc = psycopg2.OperationalError("connection closed again")
    counters, restore = _install_archive_mocks(
        conn_seq=[conn_a, conn_b],
        inner_outcomes=[
            ("raise", psycopg2.OperationalError("first close")),
            ("raise", second_exc),
        ],
    )
    try:
        archive_mod.merge_comps_into_global_with_retry([{"x": 1}], "production_scraper")
    except psycopg2.OperationalError as exc:
        assert exc is second_exc, f"expected the retry's exception, got {exc!r}"
        assert counters["discard"] == ["A", "B"], \
            f"both conns should be discarded; got {counters['discard']}"
        return
    finally:
        restore()
    raise AssertionError("expected OperationalError")


def _test_merge_retry_deterministic_error_no_retry() -> None:
    """A non-OperationalError exception (e.g., constraint violation, statement_timeout,
    generic Exception) is NOT retried — counts as a deterministic failure."""
    import api.propelio.archive as archive_mod

    conn_a = _FakeConn("A")
    err = RuntimeError("syntax error in INSERT")
    counters, restore = _install_archive_mocks(
        conn_seq=[conn_a],
        inner_outcomes=[("raise", err)],
    )
    try:
        archive_mod.merge_comps_into_global_with_retry([{"x": 1}], "production_scraper")
    except RuntimeError as exc:
        assert exc is err
        assert counters["inner"] == 1, "deterministic error should not retry"
        assert counters["release"] == ["A"], "conn should be released (not discarded) on deterministic fail"
        assert conn_a.rollback_calls == 1
        return
    finally:
        restore()
    raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------------------
# Test #10 — shared comp_address_key invariant (spec §3.1)
# ---------------------------------------------------------------------------

def _test_comp_address_key_lives_in_archive() -> None:
    """_comp_address_key is exported by api.propelio.archive and produces
    a non-empty key for a basic fixture. If anyone moves this derivation
    elsewhere (e.g., reimplements inside production_scraper), the import
    fails or the behavior diverges."""
    from api.propelio.archive import _comp_address_key

    fixture = {"address": "1234 MAIN ST, DALLAS, TX 75201"}
    key = _comp_address_key(fixture)
    assert isinstance(key, str) and key, f"expected non-empty string, got {key!r}"


def _test_comp_address_key_not_reimplemented_in_scraper() -> None:
    """No file in production_scraper/ has its own def of _comp_address_key
    or comp_address_key. The canonical derivation must stay in
    api/propelio/archive.py per spec §3.1."""
    scraper_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for py_file in scraper_dir.glob("*.py"):
        if py_file.name == "smoke.py":
            continue  # this file references the name in a string for this test
        text = py_file.read_text(encoding="utf-8")
        if "def _comp_address_key" in text or "def comp_address_key" in text:
            offenders.append(py_file.name)
    assert not offenders, (
        f"comp_address_key MUST live only in api/propelio/archive.py "
        f"(spec §3.1). Found reimplementation in: {offenders}"
    )


# ---------------------------------------------------------------------------
# Test #11 — main() integration: --dry-run on a fixture exits 0 cleanly
# ---------------------------------------------------------------------------

def _test_main_dry_run_clean_exit() -> None:
    """--dry-run validates the list + profile + state and exits 0 with no
    state.json written. Zero Propelio calls."""
    from production_scraper.run import main

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        list_path = td_path / "fixture_list.txt"
        list_path.write_text(
            "# fixture list\n"
            "1234 Main St, Dallas, TX\n"
            "5678 Oak Ave, Plano, TX\n"
            "9012 Pine Rd, Frisco, TX\n",
            encoding="utf-8",
        )
        state_dir = td_path / "state"
        log_dir = td_path / "logs"

        rc = main([
            "--profile", "seed_5y",
            "--list", str(list_path),
            "--state-dir", str(state_dir),
            "--log-dir", str(log_dir),
            "--dry-run",
        ])
    assert rc == 0, f"expected exit 0, got {rc}"
    # Dry-run must not write state.json
    assert not (state_dir / "state.json").exists(), \
        "--dry-run should not write state.json"


def _test_main_dry_run_rejects_unknown_profile() -> None:
    """--profile nonexistent → exit 3 with valid-profile-names in stderr/log."""
    from production_scraper.run import main

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        list_path = td_path / "fixture_list.txt"
        list_path.write_text("1234 Main St, Dallas, TX\n", encoding="utf-8")
        rc = main([
            "--profile", "nonexistent",
            "--list", str(list_path),
            "--state-dir", str(td_path / "state"),
            "--log-dir", str(td_path / "logs"),
            "--dry-run",
        ])
    assert rc == 3, f"expected exit 3 for unknown profile, got {rc}"


def _test_main_dry_run_rejects_missing_list() -> None:
    """--list points to nonexistent file → exit 3."""
    from production_scraper.run import main

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        rc = main([
            "--profile", "seed_5y",
            "--list", str(td_path / "does_not_exist.txt"),
            "--state-dir", str(td_path / "state"),
            "--log-dir", str(td_path / "logs"),
            "--dry-run",
        ])
    assert rc == 3, f"expected exit 3 for missing list, got {rc}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _run_all() -> int:
    """Discover and run every ``_test_*`` at module scope.  Returns the
    process exit code (0 = all pass, 1 = at least one failure)."""
    tests = sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("_test_") and callable(fn)
    )
    fails: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            fails.append(name)
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # pragma: no cover — diagnostic only
            fails.append(name)
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(fails)}/{len(tests)} passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(_run_all())
