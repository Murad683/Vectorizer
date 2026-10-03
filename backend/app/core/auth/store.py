"""PostgreSQL-backed user persistence.

Same approach as ``app/core/queue/store.py``: psycopg (v3), hand-written
SQL, no ORM, schema owned by Alembic migrations. See that module's
docstring for the full reasoning.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row

from app.core.auth.models import RefreshToken, Role, User

logger = logging.getLogger(__name__)


def _row_to_user(row: DictRow) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        hashed_password=row["hashed_password"],
        role=Role(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


def _row_to_refresh_token(row: DictRow) -> RefreshToken:
    return RefreshToken(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        revoked=row["revoked"],
    )


class UserStore:
    """Synchronous, psycopg-backed user store."""

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._conn.execute("SET TIME ZONE 'UTC'")

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def create_user(
        self,
        *,
        user_id: UUID,
        email: str,
        hashed_password: str,
        role: Role = Role.MEMBER,
    ) -> User:
        """Insert a new user; raises ``psycopg.errors.UniqueViolation`` on
        duplicate email."""
        now = datetime.now(timezone.utc)
        row = self._conn.execute(
            """
            INSERT INTO users (id, email, hashed_password, role, is_active, created_at)
            VALUES (%s, %s, %s, %s, TRUE, %s)
            RETURNING *
            """,
            (user_id, email, hashed_password, role.value, now),
        ).fetchone()
        assert row is not None
        return _row_to_user(row)

    def get_user_by_email(self, email: str) -> User | None:
        """Return the user with the given email, or None."""
        row = self._conn.execute(
            "SELECT * FROM users WHERE email = %s", (email,)
        ).fetchone()
        return _row_to_user(row) if row is not None else None

    def get_user_by_id(self, user_id: UUID) -> User | None:
        """Return the user with the given id, or None."""
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row is not None else None

    def list_users(self) -> list[User]:
        """Return all users, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_user(r) for r in rows]

    def has_any_admin(self) -> bool:
        """Check whether at least one admin user exists."""
        row = self._conn.execute(
            "SELECT 1 FROM users WHERE role = %s LIMIT 1", (Role.ADMIN.value,)
        ).fetchone()
        return row is not None

    def update_user(self, user_id: UUID, **fields: Any) -> User | None:
        """Update arbitrary fields on a user; returns the updated user."""
        if not fields:
            return self.get_user_by_id(user_id)
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values: list[Any] = [
            v.value if isinstance(v, Role) else v for v in fields.values()
        ]
        values.append(user_id)
        row = self._conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = %s RETURNING *",  # noqa: S608
            values,
        ).fetchone()
        return _row_to_user(row) if row is not None else None

    def delete_user(self, user_id: UUID) -> bool:
        """Delete a user row; return True if a row was deleted."""
        cursor = self._conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        return cursor.rowcount > 0


class RefreshTokenStore:
    """Synchronous, psycopg-backed refresh-token store.

    Only hashed tokens are ever persisted (see
    ``security.generate_refresh_token``) — ``get_valid_by_hash`` is the
    only read path callers should use for validating an incoming cookie,
    since it already excludes revoked/expired rows.
    """

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._conn.execute("SET TIME ZONE 'UTC'")

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def create(
        self, *, token_id: UUID, user_id: UUID, token_hash: str, expires_at: datetime
    ) -> RefreshToken:
        """Insert a new refresh-token row and return it."""
        now = datetime.now(timezone.utc)
        row = self._conn.execute(
            """
            INSERT INTO refresh_tokens
                (id, user_id, token_hash, issued_at, expires_at, revoked)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            RETURNING *
            """,
            (token_id, user_id, token_hash, now, expires_at),
        ).fetchone()
        assert row is not None
        return _row_to_refresh_token(row)

    def get_valid_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Return the token row if it exists, is unrevoked, and unexpired."""
        row = self._conn.execute(
            """
            SELECT * FROM refresh_tokens
            WHERE token_hash = %s AND revoked = FALSE AND expires_at > %s
            """,
            (token_hash, datetime.now(timezone.utc)),
        ).fetchone()
        return _row_to_refresh_token(row) if row is not None else None

    def revoke_by_hash(self, token_hash: str) -> None:
        """Mark the token with this hash as revoked (no-op if not found)."""
        self._conn.execute(
            "UPDATE refresh_tokens SET revoked = TRUE WHERE token_hash = %s",
            (token_hash,),
        )
