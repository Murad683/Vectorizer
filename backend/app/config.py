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
    gemini_model: str = "gemini-2.5-flash"
    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    max_upload_size_mb: int = 30
    max_pages_per_file: int = 50
    frontend_origin: str = "http://localhost:5173"

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        """Fail fast when required secrets are missing in prod mode."""
        if self.environment == "prod" and not self.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY must be set when ENVIRONMENT=prod. "
                "Add it to the environment or the backend/.env file."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
