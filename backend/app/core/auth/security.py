"""Password hashing (bcrypt via passlib), JWT access tokens (PyJWT), and
refresh-token generation/hashing.

Token durations (Phase 12): the access token was shortened from the
original 24h flat lifetime to 30 minutes now that a refresh-token flow
backs the session — the access token being short-lived is the point,
since refresh (httpOnly cookie) is what keeps the user logged in across
a page reload. The refresh token itself lives 14 days and is rotated on
every use (old one revoked, new one issued) to limit replay risk if a
cookie is ever exfiltrated.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.core.auth.models import Role

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_dummy_hash: str | None = None

_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME = timedelta(minutes=30)
REFRESH_TOKEN_LIFETIME = timedelta(days=14)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plaintext password."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against its stored hash."""
    return _pwd_context.verify(plain, hashed)


def verify_password_dummy(plain: str) -> None:
    """Spend the cost of one bcrypt check without a real account.

    Used for unknown emails so login time does not reveal whether an email
    is registered.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _pwd_context.hash(secrets.token_urlsafe(16))
    _pwd_context.verify(plain, _dummy_hash)


def create_access_token(user_id: UUID, role: Role, email: str) -> str:
    """Create a signed, short-lived JWT carrying the user's id, role, email."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "email": email,
        "iat": now,
        "exp": now + ACCESS_TOKEN_LIFETIME,
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT; raises ``jwt.PyJWTError`` on failure."""
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, hashed_token)``.

    Only the hash is ever persisted — the raw value is set as the cookie
    and never stored, so a database read alone can't produce a usable
    token.
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """Hash a raw refresh token for storage/lookup (SHA-256, hex)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
