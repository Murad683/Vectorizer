"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    # Abuse / cost limits. Every job can cost Gemini calls, so each limit is
    # enforced server-side and can be raised in .env.
    max_active_jobs_per_user: int = Field(default=3, ge=1)
    max_jobs_per_user_per_day: int = Field(default=30, ge=1)
    # Hard ceiling on Gemini requests for one job, retries included.
    max_vision_calls_per_job: int = Field(default=25, ge=1)
    # Daily cleanup of uploaded INPUT files (python -m app.cleanup_inputs).
    input_retention_hours: int = Field(default=24, ge=1)
    input_cleanup_max_per_run: int = Field(default=100, ge=1)
    input_cleanup_dry_run: bool = False
    # Reserved: result files (DOCX/PPTX) are never deleted automatically.
    # 0 = off; nothing reads this value yet.
    result_retention_days: int = Field(default=0, ge=0)
    frontend_origin: str = "http://localhost:5173"
    # Dev aid: when set, every vision call saves its image, prompt, raw model
    # output and measured geometry under this directory, so layout/render can
    # be replayed offline (scripts/replay_diagram.py) without a new API call.
    debug_dump_dir: str = ""

    jwt_secret: str = ""
    admin_email: str = ""
    admin_password: str = ""

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""

    @property
    def cookie_secure(self) -> bool:
        """Whether the refresh-token cookie should carry the ``Secure`` flag.

        ``True`` in prod (real HTTPS). ``False`` in dev so the cookie
        still gets stored when the stack is exercised over plain HTTP
        (e.g. via the Docker Compose frontend/backend on a LAN IP,
        outside the ``localhost``-is-a-secure-context browser carve-out).
        """
        return self.environment == "prod"

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
