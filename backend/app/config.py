"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings.

    All values are loaded from environment variables (or a local ``.env``
    file). Later phases must read configuration exclusively through this
    class — no scattered ``os.environ`` lookups or magic numbers.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    max_upload_size_mb: int = 30
    max_pages_per_file: int = 50
    frontend_origin: str = "http://localhost:5173"

    jwt_secret: str = ""
    admin_email: str = ""
    admin_password: str = ""

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""

    @property
    def database_url(self) -> str:
        """Plain ``postgresql://`` conninfo string for psycopg connections.

        Alembic's ``env.py`` rewrites this to the ``postgresql+psycopg://``
        SQLAlchemy dialect form itself — this property stays in the plain
        form so it can be passed directly to ``psycopg.connect()``.
        """
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        """Fail fast when required secrets are missing in prod mode."""
        missing: list[str] = []
        if self.environment == "prod":
            if not self.gemini_api_key:
                missing.append("GEMINI_API_KEY")
            if not self.jwt_secret:
                missing.append("JWT_SECRET")
            if not self.admin_email:
                missing.append("ADMIN_EMAIL")
            if not self.admin_password:
                missing.append("ADMIN_PASSWORD")
            if not self.postgres_user:
                missing.append("POSTGRES_USER")
            if not self.postgres_password:
                missing.append("POSTGRES_PASSWORD")
            if not self.postgres_db:
                missing.append("POSTGRES_DB")
        if missing:
            raise ValueError(
                f"{', '.join(missing)} must be set when ENVIRONMENT=prod. "
                "Add them to the environment or the backend/.env file."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
