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
