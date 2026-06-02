"""Sprint 3 dependencies must be present in requirements.txt.

Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.4 (asyncpg LISTEN) +
§3.5 (sse-starlette EventSourceResponse).
"""
from pathlib import Path


def test_asyncpg_in_requirements():
    reqs = Path(__file__).resolve().parent.parent / "requirements.txt"
    content = reqs.read_text(encoding="utf-8")
    assert "asyncpg" in content, (
        "asyncpg required for async LISTEN connection in Sprint 3 §3.4"
    )


def test_sse_starlette_in_requirements():
    reqs = Path(__file__).resolve().parent.parent / "requirements.txt"
    content = reqs.read_text(encoding="utf-8")
    assert "sse-starlette" in content or "sse_starlette" in content, (
        "sse-starlette required for EventSourceResponse in Sprint 3 §3.5"
    )
