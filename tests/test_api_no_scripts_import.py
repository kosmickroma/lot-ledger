"""Close the class of the 2026-07-21 packaging bug: api/ must NEVER import
from scripts/ (docs/ + scripts/ are dockerignored, so any such import 500s
every request in the deployed container). This test would have caught the
ModuleNotFoundError before it ever reached preview."""
import ast
import pathlib

API_DIR = pathlib.Path(__file__).resolve().parent.parent / "api"


def test_api_never_imports_from_scripts() -> None:
    offenders = []
    for path in API_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "scripts":
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "scripts" for a in node.names):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "api/ must not import from scripts/ (dockerignored -> 500 in prod). Offenders: "
        + ", ".join(offenders)
    )


def test_zones_db_imports_without_scripts_on_path() -> None:
    # Simulate the deployed container where scripts/ does not exist: block it,
    # then import the runtime DB path -- it must succeed.
    import importlib
    import sys

    class _BlockScripts:
        def find_spec(self, name, path=None, target=None):
            if name == "scripts" or name.startswith("scripts."):
                raise ModuleNotFoundError(f"No module named {name!r} (blocked to simulate deployed container)")
            return None

    for m in [m for m in sys.modules if m == "scripts" or m.startswith("scripts.") or m == "api.school_pilot.zones_db"]:
        del sys.modules[m]
    finder = _BlockScripts()
    sys.meta_path.insert(0, finder)
    try:
        mod = importlib.import_module("api.school_pilot.zones_db")
        assert hasattr(mod, "assign_db")
    finally:
        sys.meta_path.remove(finder)
