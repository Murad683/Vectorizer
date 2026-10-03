"""User model and role enum."""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class Role(str, Enum):
    """User roles — string enum for JSON serialisation and JWT payloads."""

    ADMIN = "admin"
    MEMBER = "member"


class User(BaseModel):
    """A registered user account."""

    id: UUID
    email: str
    hashed_password: str
    role: Role = Role.MEMBER
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RefreshToken(BaseModel):
    """A refresh-token row. The raw token is never stored — only its hash."""

    id: UUID
    user_id: UUID
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool = Field(default=False)
