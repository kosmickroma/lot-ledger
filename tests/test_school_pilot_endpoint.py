"""Endpoint + guarded-mount tests for the school-ratings pilot.
See docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 3.

⚠️ Deviation from the plan's literal Step 1 snippet: the plan pointed at
tests/test_filter_autosave_put_endpoint.py / test_multiuser_collab_sprint1_
endpoints.py as precedent for a `TestClient` + auth-override + app-
construction pattern. Neither file (nor anything else in this suite) actually
uses `fastapi.testclient.TestClient` — this codebase's established
convention for endpoint tests is (a) call the async handler function
directly with a hand-built `user` dict, bypassing FastAPI's `Depends()`
resolution entirely (Depends(...) is just a default-value marker outside a
real request, so a plain function call is unaffected by it), and (b)
source-inspect api/main.py for the guarded-mount structure, exactly the
style test_filter_autosave_put_endpoint.py already uses for a different
conditional-behavior regression check. Followed the actual convention rather
than introducing a new (untested-in-this-repo) TestClient + module-reload
pattern for one endpoint.
"""
from __future__ import annotations

import asyncio
import inspect

from api import main as api_main
from api.school_pilot.routes import school_pilot_assign


def test_assign_endpoint_returns_levels() -> None:
    fake_user = {"id": 1, "role": "owner"}
    result = asyncio.run(school_pilot_assign(lat=32.8, lng=-96.8, user=fake_user))
    assert set(result.keys()) == {"elementary", "middle", "high"}


def test_assign_endpoint_returns_json_serializable_shape() -> None:
    import json
    fake_user = {"id": 1, "role": "user"}
    result = asyncio.run(school_pilot_assign(lat=0.0, lng=0.0, user=fake_user))
    # Outside any DISD zone -- must be all-null, never raise, never guess.
    assert result == {"elementary": None, "middle": None, "high": None}
    json.dumps(result)  # must round-trip through FastAPI's JSON response encoding


def test_assign_endpoint_does_not_gate_on_role() -> None:
    # §5.1/§9.4 -- login-gated (get_current_user), not role-gated. Any
    # authenticated user dict shape reaches the same in-memory lookup.
    for role in ("owner", "developer", "power_user", "user", "member"):
        result = asyncio.run(school_pilot_assign(lat=32.8, lng=-96.8, user={"id": 1, "role": role}))
        assert set(result.keys()) == {"elementary", "middle", "high"}


def test_school_pilot_routes_module_has_no_sql() -> None:
    # §9.4 -- the handler issues no SQL of its own, verified via code: no
    # cursor/execute/get_session_conn reference anywhere in the module.
    from api.school_pilot import routes as school_pilot_routes
    src = inspect.getsource(school_pilot_routes)
    for forbidden in ("execute(", "get_session_conn", "cursor(", "SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in src, f"school_pilot routes.py must issue no SQL of its own: found {forbidden!r}"


def test_school_pilot_routes_uses_get_current_user_dependency() -> None:
    from api.school_pilot import routes as school_pilot_routes
    src = inspect.getsource(school_pilot_routes)
    assert "get_current_user" in src
    assert "Depends(get_current_user)" in src


# --- Guarded mount (api/main.py) --------------------------------------------

def test_guarded_mount_reads_school_pilot_env_directly() -> None:
    src = inspect.getsource(api_main)
    assert 'os.getenv("SCHOOL_PILOT", "false")' in src, (
        "api/main.py must read SCHOOL_PILOT directly via os.getenv, mirroring "
        "the AI_ENABLED / VALUE_DRAFTS_ENABLED seams (never a top-level import "
        "of api.school_pilot)."
    )


def test_guarded_mount_conditionally_imports_and_mounts_router() -> None:
    src = inspect.getsource(api_main)
    assert "from api.school_pilot.routes import router as school_pilot_router" in src
    assert "app.include_router(school_pilot_router)" in src


def test_guarded_mount_is_not_a_top_level_import() -> None:
    # A broken/deleted api/school_pilot/ package must never be able to stop
    # the app from starting -- confirmed by the absence of an unconditional
    # (column-0, unindented) top-level import anywhere in api/main.py. Every
    # reference must be indented (i.e. inside the `if os.getenv(...)` guard).
    src = inspect.getsource(api_main)
    for line in src.splitlines():
        if line.startswith(("import api.school_pilot", "from api.school_pilot")):
            raise AssertionError(f"api.school_pilot must never be imported at module top level: {line!r}")


def test_guarded_mount_appears_before_static_files_catchall() -> None:
    src = inspect.getsource(api_main)
    mount_idx = src.index('if os.getenv("SCHOOL_PILOT"')
    static_idx = src.index('app.mount("/", StaticFiles(directory=FRONTEND_DIR)')
    assert mount_idx < static_idx, (
        "the guarded school-pilot mount must be registered BEFORE the "
        "StaticFiles catch-all, or /api/school-pilot/* 404s"
    )


def test_school_pilot_router_absent_when_env_unset(monkeypatch) -> None:
    # Regression guard for the module import performed at api.main module
    # load time: with SCHOOL_PILOT unset, api.main.app must carry no
    # /api/school-pilot/* route. api.main is already imported (by the tests
    # above and the app itself) with the flag unset in this test run, so we
    # assert against the live singleton rather than reloading the whole
    # 9000+-line module (which no other test in this suite does either).
    import os
    assert os.getenv("SCHOOL_PILOT") != "true", (
        "this test run must have SCHOOL_PILOT unset for the assertion below "
        "to be meaningful"
    )
    paths = {getattr(r, "path", None) for r in api_main.app.routes}
    assert "/api/school-pilot/assign" not in paths
