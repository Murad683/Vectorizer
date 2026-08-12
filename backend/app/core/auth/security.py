"""Password hashing (bcrypt via passlib) and JWT token management (PyJWT).

Token duration: 24 hours. This is a small internal team tool (not a
public consumer app), so a day-long session avoids forcing teachers to
re-login during a workday while still expiring tokens overnight. A
refresh-token flow can be added later if needed.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.core.auth.models import Role

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_ALGORITHM = "HS256"
_TOKEN_LIFETIME = timedelta(hours=24)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plaintext password."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against its stored hash."""
    return _pwd_context.verify(plain, hashed)


def create_access_token(user_id: UUID, role: Role) -> str:
    """Create a signed JWT carrying the user's id and role."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iat": now,
        "exp": now + _TOKEN_LIFETIME,
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT; raises ``jwt.PyJWTError`` on failure."""
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
