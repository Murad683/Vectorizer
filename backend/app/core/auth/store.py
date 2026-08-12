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

from app.core.auth.models import Role, User

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
