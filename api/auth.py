# api/auth.py
#
# Authentication and authorization utilities for cookie-based sessions,
# password hashing, CSRF validation, role checks, and bootstrap seeding.
#
# Connects to:
#   api/main.py    - route middleware and auth/admin endpoints
#   api/config.py  - session database connection helpers

from __future__ import annotations

import os
import secrets
import time
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, BadTimeSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from api.config import get_session_conn, release_session_conn

SESSION_COOKIE_NAME = "ll_session"
CSRF_COOKIE_NAME = "ll_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_TTL_SECONDS = 8 * 60 * 60

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# In-memory per-IP sliding-window login failure tracking.
_LOGIN_LIMIT_WINDOW_SECONDS = 15 * 60
_LOGIN_LIMIT_MAX_FAILURES = 5
_login_failures: dict[str, list[float]] = {}


class AuthError(HTTPException):
    def __init__(self, detail: str = "Unauthorized"):
        super().__init__(status_code=401, detail=detail)


def _clean_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def cookie_secure_enabled() -> bool:
    return _clean_bool_env("AUTH_COOKIE_SECURE", True)


def trust_proxy_enabled() -> bool:
    return _clean_bool_env("TRUST_PROXY", False)


def ensure_auth_settings() -> None:
    secret = os.getenv("SESSION_SECRET", "").strip()
    if not secret:
        raise ValueError("Missing required environment variable: SESSION_SECRET")


@lru_cache
def _serializer() -> URLSafeTimedSerializer:
    ensure_auth_settings()
    return URLSafeTimedSerializer(os.environ["SESSION_SECRET"])


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_session_cookie_payload(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
        "session_version": int(user.get("session_version", 1)),
    }


def encode_session(payload: dict[str, Any]) -> str:
    return _serializer().dumps(payload)


def decode_session(token: str) -> dict[str, Any]:
    try:
        payload = _serializer().loads(token, max_age=SESSION_TTL_SECONDS)
    except (SignatureExpired, BadTimeSignature, BadSignature):
        raise AuthError("Session expired or invalid")
    if not isinstance(payload, dict):
        raise AuthError("Invalid session payload")
    return payload


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def get_client_ip(request: Request) -> str:
    if trust_proxy_enabled():
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    client = request.client.host if request.client else ""
    return client or "unknown"


def _trim_login_failures(ip: str, now: float) -> list[float]:
    failures = _login_failures.get(ip, [])
    window_start = now - _LOGIN_LIMIT_WINDOW_SECONDS
    failures = [t for t in failures if t >= window_start]
    if failures:
        _login_failures[ip] = failures
    else:
        _login_failures.pop(ip, None)
    return failures


def login_allowed(ip: str) -> tuple[bool, int]:
    now = time.time()
    failures = _trim_login_failures(ip, now)
    if len(failures) < _LOGIN_LIMIT_MAX_FAILURES:
        return True, 0
    retry_after = int(max(1, (failures[0] + _LOGIN_LIMIT_WINDOW_SECONDS) - now))
    return False, retry_after


def record_login_failure(ip: str) -> None:
    now = time.time()
    failures = _trim_login_failures(ip, now)
    failures.append(now)
    _login_failures[ip] = failures


def clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, password_hash, role, is_active,
                       force_password_change, session_version
                FROM users
                WHERE LOWER(email) = LOWER(%s)
                LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "username": row[1],
                "email": row[2],
                "password_hash": row[3],
                "role": row[4],
                "is_active": bool(row[5]),
                "force_password_change": bool(row[6]),
                "session_version": int(row[7] or 1),
            }
    finally:
        release_session_conn(conn)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, password_hash, role, is_active,
                       force_password_change, session_version
                FROM users
                WHERE id = %s
                LIMIT 1
                """,
                (int(user_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "username": row[1],
                "email": row[2],
                "password_hash": row[3],
                "role": row[4],
                "is_active": bool(row[5]),
                "force_password_change": bool(row[6]),
                "session_version": int(row[7] or 1),
            }
    finally:
        release_session_conn(conn)


def get_current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        raise AuthError("Authentication required")
    payload = decode_session(token)
    user_id = payload.get("user_id")
    session_version = payload.get("session_version")
    if user_id is None:
        raise AuthError("Invalid session payload")

    user = get_user_by_id(int(user_id))
    if user is None:
        raise AuthError("User not found")
    if not user.get("is_active"):
        raise AuthError("Account disabled")
    if int(user.get("session_version", 1)) != int(session_version or 0):
        raise AuthError("Session invalidated")
    return user


def require_role(*roles: str) -> Callable[..., dict[str, Any]]:
    async def _dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dependency


def require_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if not cookie_token or not header_token or cookie_token != header_token:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def set_auth_cookies(response: Any, user: dict[str, Any]) -> None:
    token = encode_session(create_session_cookie_payload(user))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=cookie_secure_enabled(),
        samesite="lax",
        path="/",
    )
    csrf_token = generate_csrf_token()
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=cookie_secure_enabled(),
        samesite="lax",
        path="/",
    )


def refresh_session_cookie(response: Any, user: dict[str, Any], request: Request) -> None:
    # Preserve the existing CSRF token if present; rotate only when absent.
    token = encode_session(create_session_cookie_payload(user))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=cookie_secure_enabled(),
        samesite="lax",
        path="/",
    )
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME, "") or generate_csrf_token()
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=cookie_secure_enabled(),
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def write_auth_audit_log(
    *,
    actor: str,
    actor_user_id: int | None,
    action: str,
    target_user: str | None,
    target_user_id: int | None,
    detail: str | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_audit_log (
                    actor, actor_user_id, action, target_user, target_user_id,
                    detail, ip, user_agent
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    actor,
                    actor_user_id,
                    action,
                    target_user,
                    target_user_id,
                    detail,
                    ip,
                    user_agent,
                ),
            )
        conn.commit()
    finally:
        release_session_conn(conn)


def seed_bootstrap_users() -> None:
    dev_email = os.getenv("BOOTSTRAP_DEV_EMAIL", "").strip()
    dev_password = os.getenv("BOOTSTRAP_DEV_PASSWORD", "")
    owner_email = os.getenv("BOOTSTRAP_OWNER_EMAIL", "").strip()
    owner_password = os.getenv("BOOTSTRAP_OWNER_PASSWORD", "")

    missing = [
        name
        for name, value in [
            ("BOOTSTRAP_DEV_EMAIL", dev_email),
            ("BOOTSTRAP_DEV_PASSWORD", dev_password),
            ("BOOTSTRAP_OWNER_EMAIL", owner_email),
            ("BOOTSTRAP_OWNER_PASSWORD", owner_password),
        ]
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            count = int(cur.fetchone()[0] or 0)
            if count > 0:
                conn.commit()
                return

            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active, force_password_change, created_by)
                VALUES (%s,%s,%s,'developer',true,false,'bootstrap')
                """,
                (dev_email.split("@")[0], dev_email.lower(), hash_password(dev_password)),
            )
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active, force_password_change, created_by)
                VALUES (%s,%s,%s,'owner',true,false,'bootstrap')
                """,
                (owner_email.split("@")[0], owner_email.lower(), hash_password(owner_password)),
            )
        conn.commit()
    finally:
        release_session_conn(conn)


def ensure_required_roles_exist() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE role='developer' AND is_active=true")
            dev_count = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND is_active=true")
            owner_count = int(cur.fetchone()[0] or 0)
        conn.commit()
    finally:
        release_session_conn(conn)

    if dev_count < 1:
        raise ValueError("No active developer account found")
    if owner_count < 1:
        raise ValueError("No active owner account found")
