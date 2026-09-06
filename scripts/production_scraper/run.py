#!/usr/bin/env python3
"""Production scraper — main entry point.

Long-running, resumable, cron-friendly Propelio comp sweep.

See ``docs/propelio/PRODUCTION_SCRAPER_SPEC.md`` (v1.2 locked) for the
full design. This file implements §7 (per-run flow).

Invocation::

    cd /path/to/lot-ledger
    .venv/bin/python -u scripts/production_scraper/run.py --profile seed_5y
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Bootstrap: make ``production_scraper`` importable when this file is
# invoked directly (``python scripts/production_scraper/run.py ...``).
# Python only adds the script's own directory to sys.path, not its parent.
_THIS_FILE = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS_FILE.parents[1]  # …/lot-ledger/scripts/
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from production_scraper.profiles import PROFILES  # noqa: E402  (bootstrap order)

logger = logging.getLogger("production_scraper")


# Fresh-state schema (spec §6.1).
_EMPTY_STATE: dict = {
    "schema_version": 1,
    "current_pass": None,
    "history": [],
}

_STATE_FILENAME = "state.json"
_STATE_TMP_SUFFIX = ".tmp"


# ---------------------------------------------------------------------------
# State file  (spec §6.1 + §7.6 atomic-rename)
# ---------------------------------------------------------------------------

def load_state(state_dir: str | Path) -> dict:
    """Return the state dict from ``state_dir/state.json``.

    If the file does not exist, returns the fresh-state schema. Does NOT
    create the directory.
    """
    state_path = Path(state_dir) / _STATE_FILENAME
    if not state_path.exists():
        # Return a fresh copy each time — never mutate _EMPTY_STATE.
        return json.loads(json.dumps(_EMPTY_STATE))
    with state_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state_dir: str | Path, state: dict) -> None:
    """Atomically write ``state`` to ``state_dir/state.json``.

    Steps:
    1. ``mkdir -p`` the state directory.
    2. Write JSON to a sibling ``state.json.<pid>.tmp`` file.
    3. fsync the temp file.
    4. ``os.replace`` the temp over the final path (POSIX-atomic).

    If step 4 raises, the prior ``state.json`` is unchanged and the temp
    file is left behind (will be cleaned by the startup ``*.tmp`` sweep
    per spec §7.1 step 6).
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    final_path = state_dir / _STATE_FILENAME
    # PID-suffixed temp so concurrent saves (shouldn't happen under the
    # lock) can't clobber each other's tmp files.
    tmp_path = state_dir / f"{_STATE_FILENAME}.{os.getpid()}{_STATE_TMP_SUFFIX}"
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, final_path)


# ---------------------------------------------------------------------------
# List hashing  (spec §6.1 / §7.1)
# ---------------------------------------------------------------------------

def compute_list_sha256(queue: list[str]) -> str:
    """SHA-256 of the normalized address queue joined by ``\\n``.

    Hash is order-sensitive: reordering the queue changes the hash. Used
    to detect "list changed between launches" on resume per §7.1.
    """
    joined = "\n".join(queue)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Run lock  (spec §7.1 step 7)
# ---------------------------------------------------------------------------

class RunLockBusy(Exception):
    """Raised when another live process already holds the run lock."""


class RunLock:
    """fcntl.flock-based exclusive run lock.

    Use as a context manager::

        with RunLock(state_dir):
            ...  # the entire run

    On ``__enter__``:
        - mkdir -p the state dir
        - open ``state_dir/run.lock`` for write
        - try ``fcntl.flock(LOCK_EX | LOCK_NB)``
        - **if lock acquired** → write PID + ISO timestamp, return self
        - **if lock NOT acquired** → read existing PID, check liveness:
            - PID alive → raise ``RunLockBusy`` with friendly diagnostic
            - PID dead  → close fd, unlink stale lock file, retry once
                          (so we take over from a crashed prior run)

    On ``__exit__``:
        - close the fd (releases the flock automatically on POSIX)
        - unlink the lock file

    Assumption: state_dir lives on a local POSIX filesystem (spec §13
    documents the assumption — not safe on NFS / SMB).
    """

    LOCK_FILENAME = "run.lock"

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.lock_path = self.state_dir / self.LOCK_FILENAME
        self._fd: int | None = None
        self._holder_diagnostic: str | None = None

    def __enter__(self) -> "RunLock":
        import fcntl
        from datetime import datetime, timezone

        self.state_dir.mkdir(parents=True, exist_ok=True)

        for attempt in (0, 1):  # at most two tries: first try, then takeover-after-stale
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Lock contended. Read the existing PID and decide.
                try:
                    existing = os.read(fd, 4096).decode("utf-8", errors="replace")
                except Exception:
                    existing = ""
                os.close(fd)
                pid_line = existing.splitlines()[0] if existing.strip() else ""
                try:
                    other_pid = int(pid_line)
                except ValueError:
                    other_pid = -1
                if other_pid > 0 and _pid_alive(other_pid):
                    self._holder_diagnostic = (
                        f"another run is in progress (PID={other_pid}, "
                        f"lock_file={self.lock_path}, contents={existing.strip()!r})"
                    )
                    raise RunLockBusy(self._holder_diagnostic)
                if attempt == 0:
                    # Stale lock — unlink and retry.
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                # Couldn't recover after one retry — bail.
                raise RunLockBusy(
                    f"could not acquire {self.lock_path} after stale-lock cleanup"
                )
            # Acquired. Write our PID + timestamp, then retain the fd.
            now_iso = datetime.now(timezone.utc).isoformat()
            payload = f"{os.getpid()}\n{now_iso}\n".encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self._fd = fd
            return self

        raise RunLockBusy("unreachable lock-acquire loop")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        import fcntl
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Auth-error classifier  (spec §7.5 — split 429 vs 401/403)
# ---------------------------------------------------------------------------
#
# Pattern mirrors api/propelio/deep_pull.py::_classify_propelio_error,
# split into two functions because v1.2 spec §7.5 routes them differently:
# 429 → immediate exit 2 (rate limit, backoff)
# 401/403 → re-login + retry path (session expiry, recoverable)
# Non-auth → fall through to the normal per-pull error path.

# 2026-05-27 PM hotfix: the bare numeric substrings ("401", "403", "429")
# false-positive on non-auth errors whose message text happens to include
# the digit sequence — most notably PropelioScraperError "No parcel match
# for '401 HASSETT AVE, …'" where 401 is the STREET NUMBER, not an HTTP
# status code. Real auth errors are reliably signaled by the exception's
# .status_code attribute (or its .response.status_code). The remaining
# word-based fragments stay because "unauthor", "forbidden", "rate limit",
# "throttle", "too many" don't appear in addresses or normal error text.
_429_FRAGMENTS = ("rate limit", "throttle", "too many")
_AUTH_FRAGMENTS = ("unauthor", "forbidden")


def _status_code_from(exc: Exception) -> int | None:
    """Best-effort extract of an HTTP status code from an exception.
    Mirrors the strip_runner / deep_pull convention: prefer
    ``exc.status_code``, fall back to ``exc.response.status_code``."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def is_429(exc: Exception) -> bool:
    """True iff the exception looks like a rate-limit / 429."""
    if _status_code_from(exc) == 429:
        return True
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _429_FRAGMENTS)


def is_401_or_403(exc: Exception) -> bool:
    """True iff the exception looks like a session-expiry / 401 or 403
    (but NOT a 429 — 429 takes priority because it's the fatal class)."""
    if is_429(exc):
        return False
    status = _status_code_from(exc)
    if status in (401, 403):
        return True
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _AUTH_FRAGMENTS)


# ---------------------------------------------------------------------------
# Auth retry wrapper  (spec §7.5)
# ---------------------------------------------------------------------------

def call_with_auth_retry(client, fn, *args, **kwargs):
    """Invoke ``fn(*args, **kwargs)`` with the auth-retry policy from §7.5.

    Routing:
        - Success on first try → returns ``(result, recovered=False)``.
        - 429 (rate limit)    → re-raises the original exception immediately
                                (caller handles as exit-2).
        - 401/403 (session)   → ``client.login(force=True)`` is invoked;
                                if it raises, that exception propagates.
                                On successful re-login, ``fn`` is invoked
                                one more time:
                                  - retry succeeds → ``(result, recovered=True)``
                                  - retry raises   → that exception propagates
                                    (caller decides exit-2 vs non-auth handling
                                    based on the exception's class via
                                    ``is_429`` / ``is_401_or_403`` / catch-all).
        - Any other exception → re-raises (no re-login, no retry).

    The ``recovered=True`` flag tells the caller to reset
    ``consecutive_errors`` to 0 — the address is in a healthy state.
    """
    try:
        return fn(*args, **kwargs), False
    except Exception as exc:
        if is_429(exc):
            raise
        if not is_401_or_403(exc):
            raise
        # 401/403 — attempt one re-login + retry.
        client.login(force=True)
        return fn(*args, **kwargs), True


# ---------------------------------------------------------------------------
# Repo-root sanity check  (spec §7.1 step 2)
# ---------------------------------------------------------------------------

def find_repo_root(start: str | Path) -> Path:
    """Walk upward from ``start`` until a directory containing
    ``api/propelio/__init__.py`` is found. Return that directory.

    Raises ``ValueError`` if the search reaches the filesystem root
    without finding one.  Robust to symlinks and future layout drift
    (spec §13 — R2 IMPORTANT #6 fix).
    """
    cur = Path(start).resolve()
    if cur.is_file():
        cur = cur.parent

    while True:
        candidate = cur / "api" / "propelio" / "__init__.py"
        if candidate.exists():
            return cur
        parent = cur.parent
        if parent == cur:
            raise ValueError(
                "scraper invoked outside of a lot-ledger checkout; could not "
                f"find api/propelio/__init__.py in any parent of {start!r}"
            )
        cur = parent


def _pid_alive(pid: int) -> bool:
    """Return True iff a process with this PID exists and we have signal
    permission to it. Uses ``kill(pid, 0)`` which doesn't actually send a
    signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # We don't own the process but it exists.
        return True
    return True


# ---------------------------------------------------------------------------
# Pacing (spec §7.3 — mirrors strip_runner's bands intentionally)
# ---------------------------------------------------------------------------

def setup_to_first_pull_sleep_seconds() -> float:
    return random.uniform(3.0, 5.0)


def inter_pull_sleep_seconds() -> float:
    return random.uniform(15.0, 45.0)


def inter_address_sleep_seconds() -> float:
    return random.uniform(5.0, 15.0)


# ---------------------------------------------------------------------------
# Per-address outcome  (spec §7.2)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AddressOutcome:
    status: str  # "done" | "partial" | "failed"
    filters_ok: int
    filters_errored: int
    comps_returned: int
    comps_new: int
    skip_reason: str | None
    last_error: str | None


def _short_err(exc: BaseException) -> str:
    line = (str(exc).strip().splitlines()[0] if str(exc).strip() else "")[:240]
    return f"{type(exc).__name__} {line}".strip()


# ---------------------------------------------------------------------------
# CLI + main  (spec §7 + §8)
# ---------------------------------------------------------------------------

# Shared shutdown flag for signal handlers (spec §7.6).
_SHOULD_STOP = {"flag": False, "second_signal": False}


def _install_signal_handlers() -> None:
    def _handler(signum, frame) -> None:
        if _SHOULD_STOP["flag"]:
            # Second signal — let Python default (KeyboardInterrupt) propagate.
            _SHOULD_STOP["second_signal"] = True
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        _SHOULD_STOP["flag"] = True
        logger.warning(
            "signal %d received — soft-stop after current pull (second signal = abrupt exit)",
            signum,
        )

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="production_scraper",
        description=(
            "Long-running, resumable, cron-friendly Propelio comp sweep. "
            "See docs/propelio/PRODUCTION_SCRAPER_SPEC.md (v1.2 locked)."
        ),
    )
    p.add_argument(
        "--profile",
        required=True,
        help="Filter profile name (e.g., 'seed_5y' or 'monthly_1m'). See profiles.py.",
    )
    p.add_argument(
        "--list",
        dest="list_path",
        default=None,
        help="Path to the master address list (default: scripts/production_scraper/master_list.txt).",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="State directory (default: scripts/production_scraper/state/).",
    )
    p.add_argument(
        "--log-dir",
        default=None,
        help="Log directory (default: scripts/production_scraper/logs/).",
    )
    p.add_argument(
        "--restart",
        action="store_true",
        help="Abandon any in-progress pass (archived with aborted=true) and start fresh.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate list + profile + state, print the work queue, exit 0. Zero Propelio calls.",
    )
    p.add_argument(
        "--mock",
        action="store_true",
        help="Skip real Propelio calls; use the smoke-mode persistence stub. For self-tests.",
    )
    return p


def _setup_logging(log_dir: Path, profile: str) -> Path:
    """Configure stdout + file handlers ONLY on the production_scraper
    logger (not the root logger). Returns the log-file path (also
    symlinked at ``logs/latest.log``).

    Why scope to our logger and not root: ``api/propelio/scraper.py``
    emits very verbose INFO logs (full HTTP bodies, parsed JSON dumps,
    payload echoes). If we raise root to INFO and attach root handlers,
    every one of those library messages floods our log. By attaching
    handlers only to the ``production_scraper`` logger with
    ``propagate=False``, our messages reach our handlers and nothing
    else does — same effective behavior as strip_runner gets by using
    raw ``print()`` instead of the logging module.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    log_path = log_dir / f"{profile}-{ts}.log"
    latest = log_dir / "latest.log"

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S")

    # Idempotent re-init (in case main() is invoked multiple times in tests).
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # do NOT bubble up to root — keeps library noise out

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Symlink dance — best-effort, never fatal.
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(log_path.name)
    except Exception:
        pass

    return log_path


def _scraper_default_dir() -> Path:
    """Where the scraper folder itself lives — used as the default for
    --list / --state-dir / --log-dir."""
    return Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (see spec §8.1)."""
    parser = _build_argparser()
    args = parser.parse_args(argv)

    # Resolve defaults that depend on the scraper folder location.
    scraper_dir = _scraper_default_dir()
    list_path = Path(args.list_path) if args.list_path else scraper_dir / "master_list.txt"
    state_dir = Path(args.state_dir) if args.state_dir else scraper_dir / "state"
    log_dir = Path(args.log_dir) if args.log_dir else scraper_dir / "logs"

    # Repo-root sanity check (spec §7.1 step 2) — robust to symlinks.
    try:
        repo_root = find_repo_root(start=scraper_dir)
    except ValueError as exc:
        print(f"production_scraper: {exc}", file=sys.stderr)
        return 3
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # .env from repo root (NOT cwd).
    try:
        from dotenv import load_dotenv
        load_dotenv(repo_root / ".env")
    except ImportError:
        # python-dotenv missing — fine if env is set elsewhere.
        pass

    # Validate profile (spec §5).
    try:
        profile_cfg = resolve_profile(args.profile)
    except ValueError as exc:
        print(f"production_scraper: {exc}", file=sys.stderr)
        return 3

    # Validate list early so --dry-run can surface parse errors without
    # touching state or log dirs unnecessarily.
    try:
        queue = load_master_list(list_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"production_scraper: {exc}", file=sys.stderr)
        return 3

    list_sha = compute_list_sha256(queue)

    # Logging — only set up for the "real" run path, not pure --dry-run.
    if not args.dry_run:
        _setup_logging(log_dir, args.profile)
        logger.info(
            "=== production_scraper start | profile=%s | list=%s (%d addrs, sha256=%s) ===",
            args.profile, list_path, len(queue), list_sha[:12],
        )

    if args.dry_run:
        # Dry-run: print summary, no state mutation, no lock acquisition.
        print(f"production_scraper --dry-run:")
        print(f"  profile        : {args.profile} (months={profile_cfg['months']}, "
              f"distances_mi={profile_cfg['distances_mi']})")
        print(f"  pulls          : {profile_pulls(profile_cfg)}  (months, miles) per address")
        print(f"  list           : {list_path}")
        print(f"  list_sha256    : {list_sha}")
        print(f"  queue length   : {len(queue)}")
        for i, addr in enumerate(queue[:10], 1):
            print(f"   {i:3d}. {addr}")
        if len(queue) > 10:
            print(f"   ... ({len(queue) - 10} more)")
        print(f"  state-dir      : {state_dir} (not touched in dry-run)")
        print(f"  log-dir        : {log_dir} (not touched in dry-run)")
        return 0

    # Tidy stale .tmp files in state dir (spec §7.1 step 6).
    if state_dir.exists():
        for tmp in state_dir.glob(f"*{_STATE_TMP_SUFFIX}"):
            try:
                tmp.unlink()
                logger.info("removed stale tmp file: %s", tmp.name)
            except Exception:
                pass

    # Acquire the run lock (spec §7.1 step 7).
    try:
        lock_ctx = RunLock(state_dir)
    except Exception as exc:  # pragma: no cover — RunLock construction shouldn't fail
        logger.error("failed to construct RunLock: %s", exc)
        return 3

    try:
        with lock_ctx:
            # History size guard (spec §7.1 step 10).
            state = load_state(state_dir)
            if len(state.get("history", [])) > 1000:
                logger.warning(
                    "state.json history has %d entries (>1000); consider pruning or "
                    "investigating cron-fire frequency",
                    len(state["history"]),
                )

            # Pass-status decision (spec §7.1 step 11).
            state = _initialize_or_resume_pass(
                state=state,
                profile=args.profile,
                profile_cfg=profile_cfg,
                list_path=str(list_path),
                queue=queue,
                list_sha=list_sha,
                restart=args.restart,
            )
            if state is None:
                # Profile/list mismatch on resume → exit 3 already logged.
                return 3
            save_state(state_dir, state)

            # Install signal handlers AFTER lock + state are ready (so a
            # rapid SIGINT during startup doesn't corrupt anything).
            _install_signal_handlers()

            outcome_code = _run_pass(
                state_dir=state_dir,
                state=state,
                profile_cfg=profile_cfg,
                mock=args.mock,
            )
            return outcome_code
    except RunLockBusy as exc:
        msg = f"production_scraper: lock busy — {exc}"
        # Logger may not be configured yet if dry-run skipped setup,
        # but we already configured it before lock acquire above.
        logger.error(msg)
        print(msg, file=sys.stderr)
        return 4


def _initialize_or_resume_pass(
    *,
    state: dict,
    profile: str,
    profile_cfg: dict,
    list_path: str,
    queue: list[str],
    list_sha: str,
    restart: bool,
) -> dict | None:
    """Decide whether to start a fresh pass, resume an in-progress one,
    or reject with a profile/list mismatch (spec §7.1 step 11).

    Returns the updated state dict, or ``None`` if mismatch (caller
    exits 3).
    """
    cur = state.get("current_pass")
    if restart and cur is not None:
        # Archive the in-progress pass as aborted, then start fresh.
        logger.warning("--restart: archiving in-progress pass as aborted")
        _archive_pass(state, aborted=True)
        cur = None
    if cur and cur.get("completed_at"):
        # Prior pass finished cleanly — roll into history, start fresh.
        _archive_pass(state, aborted=False)
        cur = None
    if cur is None:
        state["current_pass"] = _new_pass_dict(
            profile=profile,
            profile_cfg=profile_cfg,
            list_path=list_path,
            queue=queue,
            list_sha=list_sha,
        )
        logger.info("starting fresh pass: %d addresses pending", len(queue))
        return state
    # Resume path — verify profile + list_sha256 match.
    if cur.get("profile") != profile or cur.get("list_sha256") != list_sha:
        logger.error(
            "resume rejected: current pass profile=%s list_sha256=%s, but launched with "
            "profile=%s list_sha256=%s. Use --restart to abandon the in-progress pass.",
            cur.get("profile"), cur.get("list_sha256", "")[:12],
            profile, list_sha[:12],
        )
        return None
    # Resume: any in_progress address is treated as pending (spec §6.3).
    addrs = cur.get("addresses", {})
    n_pending = 0
    n_done = 0
    for addr, slot in addrs.items():
        if slot.get("status") == "in_progress":
            slot["status"] = "pending"
            n_pending += 1
        elif slot.get("status") == "pending":
            n_pending += 1
        else:
            n_done += 1
    logger.info(
        "resume detected: %d addrs done, %d pending (in_progress addresses re-queued)",
        n_done, n_pending,
    )
    return state


def _new_pass_dict(
    *,
    profile: str,
    profile_cfg: dict,
    list_path: str,
    queue: list[str],
    list_sha: str,
) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    pass_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{profile}"
    return {
        "pass_id": pass_id,
        "started_at": started_at,
        "completed_at": None,
        "profile": profile,
        "profile_snapshot": {
            "months": int(profile_cfg["months"]),
            "distances_mi": list(profile_cfg["distances_mi"]),
            "pulls": [[int(m), float(d)] for m, d in profile_pulls(profile_cfg)],
        },
        "list_path": list_path,
        "list_sha256": list_sha,
        "list_snapshot": list(queue),
        "addresses": {addr: {"status": "pending"} for addr in queue},
    }


def _archive_pass(state: dict, *, aborted: bool) -> None:
    """Move current_pass → history (slim summary), reset current_pass."""
    cur = state.get("current_pass")
    if not cur:
        return
    addrs = cur.get("addresses", {})
    summary = {
        "pass_id": cur.get("pass_id"),
        "profile": cur.get("profile"),
        "started_at": cur.get("started_at"),
        "completed_at": cur.get("completed_at") or datetime.now(timezone.utc).isoformat(),
        "addresses_total": len(addrs),
        "addresses_done": sum(1 for s in addrs.values() if s.get("status") == "done"),
        "addresses_partial": sum(1 for s in addrs.values() if s.get("status") == "partial"),
        "addresses_failed": sum(1 for s in addrs.values() if s.get("status") == "failed"),
        "addresses_pending_at_archive": sum(
            1 for s in addrs.values() if s.get("status") in ("pending", "in_progress")
        ),
        "comps_new_total": sum(int(s.get("comps_new") or 0) for s in addrs.values()),
        "aborted": bool(aborted),
    }
    state.setdefault("history", []).append(summary)
    state["current_pass"] = None


# ---------------------------------------------------------------------------
# Per-address loop  (spec §7.2)
# ---------------------------------------------------------------------------

def _run_pass(*, state_dir: Path, state: dict, profile_cfg: dict, mock: bool) -> int:
    """Iterate pending addresses, run each, save state after every one.
    Returns the process exit code (spec §8.1)."""
    cur = state["current_pass"]
    addrs = cur["addresses"]
    queue = cur["list_snapshot"]
    pending = [a for a in queue if addrs.get(a, {}).get("status") in ("pending", "in_progress")]

    if not pending:
        logger.info("no pending addresses — finalizing pass")
        cur["completed_at"] = datetime.now(timezone.utc).isoformat()
        _archive_pass(state, aborted=False)
        save_state(state_dir, state)
        _print_pass_summary(cur, addrs)
        return 0

    client = None
    if not mock:
        client = _build_propelio_client()

    pulls: list[tuple[int, float]] = profile_pulls(profile_cfg)

    soft_stop_made_progress = False
    for idx, address in enumerate(pending, start=1):
        if _SHOULD_STOP["flag"]:
            logger.warning("soft-stop honored before address %d/%d", idx, len(pending))
            save_state(state_dir, state)
            return 1 if soft_stop_made_progress else 130

        # Mark in_progress + persist
        addrs[address] = {
            "status": "in_progress",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state_dir, state)

        logger.info("address %d/%d: %s", idx, len(pending), address)

        try:
            outcome = run_address(
                client=client,
                address=address,
                pulls=pulls,
                mock=mock,
            )
        except _AuthBlockExit as exc:
            # 429 (or unrecoverable 401/403) → save state with this address
            # still in_progress, exit 2.
            logger.critical("auth block — exiting code 2: %s", exc)
            save_state(state_dir, state)
            return 2

        # Persist terminal outcome
        addrs[address] = {
            "status": outcome.status,
            "started_at": addrs[address].get("started_at"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "filters_ok": outcome.filters_ok,
            "filters_errored": outcome.filters_errored,
            "comps_returned": outcome.comps_returned,
            "comps_new": outcome.comps_new,
            "skip_reason": outcome.skip_reason,
            "last_error": outcome.last_error,
        }
        save_state(state_dir, state)
        soft_stop_made_progress = True

        logger.info(
            "address done: status=%s filters_ok=%d/%d errored=%d comps_returned=%d comps_new=%d",
            outcome.status, outcome.filters_ok,
            outcome.filters_ok + outcome.filters_errored,
            outcome.filters_errored,
            outcome.comps_returned, outcome.comps_new,
        )

        # Inter-address pause (unless this was the last one).
        if idx < len(pending) and not _SHOULD_STOP["flag"]:
            time.sleep(inter_address_sleep_seconds())

    # All done — finalize the pass.
    cur["completed_at"] = datetime.now(timezone.utc).isoformat()
    _print_pass_summary(cur, addrs)
    _archive_pass(state, aborted=False)
    save_state(state_dir, state)
    return 0


def _print_pass_summary(cur_pass: dict, addrs: dict) -> None:
    total = len(addrs)
    done = sum(1 for s in addrs.values() if s.get("status") == "done")
    partial = sum(1 for s in addrs.values() if s.get("status") == "partial")
    failed = sum(1 for s in addrs.values() if s.get("status") == "failed")
    comps_returned = sum(int(s.get("comps_returned") or 0) for s in addrs.values())
    comps_new = sum(int(s.get("comps_new") or 0) for s in addrs.values())

    logger.info(
        "=== pass complete | done=%d partial=%d failed=%d / %d | "
        "comps_returned=%d comps_new=%d | profile=%s ===",
        done, partial, failed, total,
        comps_returned, comps_new,
        cur_pass.get("profile"),
    )

    # Enumerate every non-done address (partial + failed) so KK can spot-check
    # them manually without grepping state.json. Each line has the skip_reason
    # (or "n errors" for partial) plus the last_error short summary.
    for addr in cur_pass.get("list_snapshot", []):
        slot = addrs.get(addr) or {}
        status = slot.get("status")
        if status == "partial":
            logger.info(
                "  [partial] %s — filters_ok=%d errored=%d last_error=%s",
                addr,
                int(slot.get("filters_ok") or 0),
                int(slot.get("filters_errored") or 0),
                (slot.get("last_error") or "")[:160],
            )
        elif status == "failed":
            logger.info(
                "  [failed]  %s — reason=%s last_error=%s",
                addr,
                slot.get("skip_reason") or "(none)",
                (slot.get("last_error") or "")[:160],
            )

    if partial == 0 and failed == 0:
        logger.info("  (no failures — all %d addresses completed cleanly)", done)


# ---------------------------------------------------------------------------
# Per-address Propelio orchestration
# ---------------------------------------------------------------------------

class _AuthBlockExit(Exception):
    """Raised inside run_address when 429 or unrecoverable 401/403
    propagates. The run-pass loop catches this and exits code 2."""


def _build_propelio_client():
    """Construct a real PropelioClient from environment credentials."""
    from api.propelio.scraper import PropelioClient
    from api import config as _cfg
    username = getattr(_cfg, "PROPELIO_USERNAME", "") or os.environ.get("PROPELIO_USERNAME", "")
    password = getattr(_cfg, "PROPELIO_PASSWORD", "") or os.environ.get("PROPELIO_PASSWORD", "")
    return PropelioClient(username, password)


def run_address(
    *,
    client,
    address: str,
    pulls: list[tuple[int, float]],
    mock: bool,
) -> AddressOutcome:
    """Run one address: lead lookup → CMA setup → N × search_cma + merge.

    Implements §7.2 of the spec: auth-retry for 401/403, immediate exit
    on 429, burst-error guard at 3 consecutive errors (DB or Propelio).
    Returns an :class:`AddressOutcome` regardless of how the address ends.
    """
    if mock:
        return _run_address_mock(address=address, distances_mi=[d for _, d in pulls])

    # Step 1: find_lead_id
    try:
        result, recovered = call_with_auth_retry(
            client, client.find_lead_id, address,
        )
        lead_id, _subject_sqft, parcel_bundle = result
        if recovered:
            logger.info("recovered from auth 401/403 during find_lead_id; consecutive_errors reset")
    except Exception as exc:
        if is_429(exc):
            raise _AuthBlockExit(f"429 during find_lead_id for {address}: {_short_err(exc)}")
        if is_401_or_403(exc):
            raise _AuthBlockExit(f"unrecoverable 401/403 during find_lead_id for {address}: {_short_err(exc)}")
        logger.warning("lead lookup failed for %s: %s", address, _short_err(exc))
        return AddressOutcome(
            status="failed", filters_ok=0, filters_errored=0,
            comps_returned=0, comps_new=0,
            skip_reason="lead lookup failed", last_error=_short_err(exc),
        )

    confirmation_key = (
        parcel_bundle.get("confirmation_key")
        if isinstance(parcel_bundle, dict) else None
    )

    # Step 2: add_cma — first distance, comps discarded (per strip_runner Option A)
    try:
        envelope, recovered = call_with_auth_retry(
            client, client.add_cma,
            lead_id, confirmation_key,
            months=pulls[0][0], range_mi=pulls[0][1],
        )
        cma_id = _extract_cma_id(envelope)
        if recovered:
            logger.info("recovered from auth 401/403 during add_cma; consecutive_errors reset")
    except Exception as exc:
        if is_429(exc):
            raise _AuthBlockExit(f"429 during add_cma for {address}: {_short_err(exc)}")
        if is_401_or_403(exc):
            raise _AuthBlockExit(f"unrecoverable 401/403 during add_cma for {address}: {_short_err(exc)}")
        logger.warning("cma setup failed for %s: %s", address, _short_err(exc))
        return AddressOutcome(
            status="failed", filters_ok=0, filters_errored=0,
            comps_returned=0, comps_new=0,
            skip_reason="cma setup failed", last_error=_short_err(exc),
        )

    time.sleep(setup_to_first_pull_sleep_seconds())

    # Step 3: N × search_cma
    filters_ok = 0
    filters_errored = 0
    consecutive_errors = 0
    comps_returned_total = 0
    comps_new_total = 0
    last_error: str | None = None

    for pass_num, (months, distance_mi) in enumerate(pulls, start=1):
        if _SHOULD_STOP["flag"]:
            logger.warning(
                "soft-stop honored mid-address before pass %d/%d",
                pass_num, len(pulls),
            )
            break
        if pass_num > 1:
            time.sleep(inter_pull_sleep_seconds())

        # --- Propelio call (with auth-retry) ---
        try:
            envelope, recovered = call_with_auth_retry(
                client, client.search_cma,
                lead_id, cma_id,
                months=months, range_mi=distance_mi,
            )
            if recovered:
                # Successful re-login + retry resets consecutive_errors (spec §7.5).
                consecutive_errors = 0
                logger.info("recovered from auth 401/403 on pass %d; consecutive_errors reset", pass_num)
        except Exception as exc:
            if is_429(exc):
                raise _AuthBlockExit(f"429 on pass {pass_num} for {address}: {_short_err(exc)}")
            if is_401_or_403(exc):
                raise _AuthBlockExit(f"unrecoverable 401/403 on pass {pass_num} for {address}: {_short_err(exc)}")
            filters_errored += 1
            consecutive_errors += 1
            last_error = _short_err(exc)
            logger.warning(
                "pass %d/%d  %dmo / %smi  PROPELIO ERROR: %s",
                pass_num, len(pulls), months, distance_mi, _short_err(exc),
            )
            if consecutive_errors >= 3:
                logger.warning("3 consecutive errors — address-level skip")
                return AddressOutcome(
                    status="failed",
                    filters_ok=filters_ok, filters_errored=filters_errored,
                    comps_returned=comps_returned_total, comps_new=comps_new_total,
                    skip_reason="3 consecutive DB+filter errors",
                    last_error=last_error,
                )
            continue

        # --- Parse + parcel-match + merge (with DB retry) ---
        try:
            comps_for_merge, returned = _parse_and_match_comps(envelope)
            merge_result = _merge_with_retry(comps_for_merge)
            comps_returned_total += returned
            comps_new_total += int(merge_result.get("inserted", 0) or 0)
            filters_ok += 1
            consecutive_errors = 0
            logger.info(
                "pass %d/%d  %dmo / %smi  returned %d  new %d  addr_total %d",
                pass_num, len(pulls), months, distance_mi,
                returned, int(merge_result.get("inserted", 0) or 0), comps_new_total,
            )
        except Exception as exc:
            filters_errored += 1
            consecutive_errors += 1
            last_error = _short_err(exc)
            logger.warning(
                "pass %d/%d  %dmo / %smi  DB/MERGE ERROR: %s",
                pass_num, len(pulls), months, distance_mi, _short_err(exc),
            )
            if consecutive_errors >= 3:
                logger.warning("3 consecutive errors — address-level skip")
                return AddressOutcome(
                    status="failed",
                    filters_ok=filters_ok, filters_errored=filters_errored,
                    comps_returned=comps_returned_total, comps_new=comps_new_total,
                    skip_reason="3 consecutive DB+filter errors",
                    last_error=last_error,
                )
            continue

    status = "done" if filters_errored == 0 else "partial"
    return AddressOutcome(
        status=status,
        filters_ok=filters_ok, filters_errored=filters_errored,
        comps_returned=comps_returned_total, comps_new=comps_new_total,
        skip_reason=None, last_error=last_error,
    )


def _run_address_mock(*, address: str, distances_mi: list[float]) -> AddressOutcome:
    """Mock-mode address run: zero Propelio + DB activity. Counts each
    distance as a successful pull returning 0 new comps."""
    logger.info("[mock] address: %s", address)
    return AddressOutcome(
        status="done",
        filters_ok=len(distances_mi),
        filters_errored=0,
        comps_returned=0, comps_new=0,
        skip_reason=None, last_error=None,
    )


def _extract_cma_id(envelope) -> str:
    """Extract cma_id from add_cma response envelope."""
    if not isinstance(envelope, dict):
        raise ValueError(f"add_cma envelope is not a dict: type={type(envelope)}")
    cma_id = str(envelope.get("id") or "").strip()
    if not cma_id:
        raise ValueError(f"could not extract cma_id from envelope keys={list(envelope.keys())}")
    return cma_id


def _parse_and_match_comps(envelope) -> tuple[list[dict], int]:
    """Pull the sales list out of the search_cma envelope, parse each
    into the shape expected by merge_comps_into_global_with_retry. Returns
    (parsed_for_merge, raw_returned_count)."""
    from dataclasses import asdict
    from api.propelio.scraper import _parse_property
    from api.propelio.parcel_match import match_comps_to_parcels

    raw_sales = []
    if isinstance(envelope, dict):
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else None
        if data:
            raw_sales = data.get("sales") or []
    raw_sales = raw_sales if isinstance(raw_sales, list) else []

    parsed: list[dict] = []
    for raw in raw_sales:
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append(asdict(_parse_property(raw)))
        except Exception:
            continue

    if parsed:
        try:
            matched = match_comps_to_parcels(parsed)
        except Exception as exc:
            logger.warning("parcel_match failed (non-fatal): %s", _short_err(exc))
            matched = parsed
    else:
        matched = []

    return matched, len(raw_sales)


def _merge_with_retry(comps: list[dict]) -> dict:
    """Wrap api.propelio.archive.merge_comps_into_global_with_retry."""
    from api.propelio.archive import merge_comps_into_global_with_retry
    return merge_comps_into_global_with_retry(comps, source="production_scraper")


# ---------------------------------------------------------------------------
# Profile resolver  (spec §5)
# ---------------------------------------------------------------------------

def profile_pulls(profile_cfg: dict) -> list[tuple[int, float]]:
    """The (months, miles) pairs one address is pulled at, in order.

    A profile may spell them out as ``pulls`` (a catch-up sweep wants a short
    window at a wide radius AND a long window at a tight one); otherwise it is
    the classic ``months`` × ``distances_mi`` product the seed pass used. The
    first pair is also what add_cma is set up with.
    """
    if profile_cfg.get("pulls"):
        return [(int(m), float(d)) for m, d in profile_cfg["pulls"]]
    months = int(profile_cfg["months"])
    return [(months, float(d)) for d in profile_cfg["distances_mi"]]


def resolve_profile(name: str) -> dict:
    """Look up a profile by name. Raises ``ValueError`` with the valid
    profile names if ``name`` is unknown.

    Profiles are defined in ``profiles.py`` as a pure-data dict.
    """
    if name in PROFILES:
        return PROFILES[name]
    valid = ", ".join(sorted(PROFILES.keys()))
    raise ValueError(
        f"unknown profile {name!r}; valid profiles: {valid}"
    )


# ---------------------------------------------------------------------------
# Master list parsing  (spec §4)
# ---------------------------------------------------------------------------

def load_master_list(path: str | Path) -> list[str]:
    """Read the master address list and return the normalized address queue.

    Normalization rules (spec §4):

    * Read as ``utf-8-sig`` so a leading BOM doesn't survive into the first
      address.
    * Strip whitespace from each line; collapse internal whitespace runs
      to single spaces.
    * Skip blank lines and lines starting with ``#`` (after strip).
    * Reject lines that contain no commas (no city) — raise ``ValueError``
      with the 1-based line number for operator diagnosis.
    * Uppercase each address for dedup + hashing stability.
    * Deduplicate (first occurrence wins for ordering).
    * Reject an entirely empty list (no non-comment, non-blank lines).
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"master list not found: {path}")

    with file_path.open("r", encoding="utf-8-sig") as fh:
        raw_lines = fh.readlines()

    queue: list[str] = []
    seen: set[str] = set()

    for idx, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue

        # Collapse internal whitespace (defensive against Obsidian paste).
        collapsed = " ".join(stripped.split())

        if "," not in collapsed:
            raise ValueError(
                f"master list parse error at line {idx}: "
                f"missing city/state (no comma in line): {collapsed!r}"
            )

        normalized = collapsed.upper()
        if normalized in seen:
            continue
        seen.add(normalized)
        queue.append(normalized)

    if not queue:
        raise ValueError(
            f"master list is empty (no non-comment, non-blank lines): {path}"
        )

    return queue


if __name__ == "__main__":
    sys.exit(main())
