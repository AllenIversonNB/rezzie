"""App login (single password) + encryption for platform credentials at rest."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import settings

SESSION_COOKIE = "rezzie_session"
SESSION_TTL = 90 * 24 * 3600


def _secret() -> bytes:
    key = settings.secret_key
    if not key:
        # Generate once and persist so restarts don't log you out. Setting
        # SECRET_KEY as a Fly secret is still preferred.
        key = db.kv_get("_generated_secret_key")
        if not key:
            key = secrets.token_urlsafe(48)
            db.kv_set("_generated_secret_key", key)
        settings.secret_key = key
    return key.encode()


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"rezzie-fernet:" + _secret()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ------------------------------------------------------------- sessions

def make_session() -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{exp}.{secrets.token_hex(8)}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        exp_s, nonce, sig = token.split(".")
    except ValueError:
        return False
    payload = f"{exp_s}.{nonce}"
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return False
    # Rotating the password invalidates sessions issued before the change.
    issued_floor = db.kv_get("_sessions_valid_after", 0)
    return int(exp_s) > time.time() and int(exp_s) - SESSION_TTL >= issued_floor - 1


def check_password(pw: str) -> bool:
    if not settings.app_password:
        return False
    return hmac.compare_digest(pw.encode(), settings.app_password.encode())


# Login throttling: 5 attempts per 15 minutes per client IP.
_attempts: dict[str, deque[float]] = defaultdict(deque)


def login_allowed(ip: str) -> bool:
    q = _attempts[ip]
    now = time.time()
    while q and now - q[0] > 900:
        q.popleft()
    return len(q) < 5


def record_failed_login(ip: str) -> None:
    _attempts[ip].append(time.time())
