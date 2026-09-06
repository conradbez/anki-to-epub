"""Password hashing and opaque-token helpers (stdlib only).

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user random salt. We never
store or log the plaintext. Session ids and CSRF tokens are just URL-safe random
strings.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 200_000
_ALGO = "sha256"


def hash_password(password: str) -> tuple[str, str]:
    """Return ``(hash_hex, salt_hex)`` for a new password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(digest, expected)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, form_value: str | None) -> bool:
    """Double-submit CSRF check: the cookie and the form field must match."""
    if not cookie_value or not form_value:
        return False
    return hmac.compare_digest(cookie_value, form_value)
