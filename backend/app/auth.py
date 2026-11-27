from __future__ import annotations

from contextvars import ContextVar
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from typing import Optional

from fastapi import Request, Response

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "sao_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

current_user: ContextVar[Optional[str]] = ContextVar("sao_current_user", default=None)

_FALLBACK_SECRET: Optional[str] = None
_FALLBACK_WARNED = False


def is_valid_username(username: str | None) -> bool:
    return isinstance(username, str) and bool(USERNAME_RE.fullmatch(username))


def normalize_username(raw: str | None) -> str:
    return (raw or "").strip().lower()


def is_auth_enabled() -> bool:
    return os.getenv("AUTH_ENABLED", "").strip().lower() in ("true", "1", "yes")


def get_auth_secret() -> bytes:
    global _FALLBACK_SECRET, _FALLBACK_WARNED
    env_secret = os.getenv("AUTH_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    if _FALLBACK_SECRET is None:
        _FALLBACK_SECRET = secrets.token_hex(32)
    if not _FALLBACK_WARNED:
        logger.warning(
            "AUTH_ENABLED is true but AUTH_SECRET is unset; using an ephemeral process-level secret. "
            "Set AUTH_SECRET in Railway variables so sessions persist across restarts."
        )
        _FALLBACK_WARNED = True
    return _FALLBACK_SECRET.encode("utf-8")


def get_beta_users() -> dict[str, str]:
    """Parse comma-separated username:password pairs from BETA_USERS."""
    raw = os.getenv("BETA_USERS", "")
    users: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item or ":" not in item:
            continue
        raw_user, raw_pass = item.split(":", 1)
        username = normalize_username(raw_user)
        password = raw_pass.strip()
        if not is_valid_username(username) or not password:
            continue
        users[username] = password
    return users


def get_bot_api_credentials() -> tuple[str, str]:
    key = os.getenv("BOT_API_KEY", "").strip()
    bot_user = normalize_username(os.getenv("BOT_API_USER", "paul")) or "paul"
    if not is_valid_username(bot_user):
        bot_user = "paul"
    return key, bot_user


def verify_credentials(username: str, password: str) -> Optional[str]:
    norm_user = normalize_username(username)
    if not is_valid_username(norm_user):
        return None
    users = get_beta_users()
    expected = users.get(norm_user)
    if expected is None:
        return None
    if hmac.compare_digest(expected, password or ""):
        return norm_user
    return None


def create_session_token(
    username: str,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    norm_user = normalize_username(username)
    if not is_valid_username(norm_user):
        raise ValueError("Invalid username for session token.")
    ts = now if now is not None else time.time()
    exp = int(ts + ttl_seconds)
    payload = f"{norm_user}|{exp}"
    sig = hmac.new(get_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session_token(token: Optional[str], now: Optional[float] = None) -> Optional[str]:
    if not token or not isinstance(token, str):
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    username, exp_str, sig = parts
    if not is_valid_username(username):
        return None
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    ts = now if now is not None else time.time()
    if exp <= ts:
        return None
    payload = f"{username}|{exp_str}"
    expected_sig = hmac.new(get_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None
    return username


def authenticate_request(request: Request) -> Optional[str]:
    # 1. Optional machine-to-machine API key (e.g. my-bots-room Building AI Agent)
    bot_key, bot_user = get_bot_api_credentials()
    header_key = (request.headers.get("x-api-key") or "").strip()
    if bot_key and header_key and hmac.compare_digest(bot_key, header_key):
        return bot_user

    # 2. Signed HttpOnly session cookie
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    return verify_session_token(cookie_token)


def is_secure_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    first_proto = forwarded_proto.split(",")[0].strip().lower()
    return first_proto == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=is_secure_request(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request),
    )
