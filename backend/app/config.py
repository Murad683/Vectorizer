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
    # A PDF is a job of many pages, so its request ceiling grows with the page
    # count: max(MAX_VISION_CALLS_PER_JOB, pages * PER_PAGE + EXTRA). Single
    # images and DOCX files keep MAX_VISION_CALLS_PER_JOB.
    vision_calls_per_page: int = Field(default=2, ge=1)
    vision_calls_extra: int = Field(default=5, ge=0)
    # PDF page types: a page whose biggest image covers this share of the page is
    # a scan (even with a hidden OCR text layer); a page with fewer text characters
    # than the minimum and an image or drawing is a scan too. Scans are rendered
    # at this resolution and read as a whole page by the vision model.
    pdf_scan_coverage: float = Field(default=0.80, gt=0, le=1)
    pdf_min_text_chars: int = Field(default=20, ge=1)
    pdf_scan_dpi: int = Field(default=150, ge=72, le=300)
    # Page selection: a PDF with scanned pages waits for its owner to pick the
    # pages to read. False: such a PDF starts at once, as before. A selection
    # nobody makes expires after this many hours. Pages left out of the result
    # as plain pictures are rendered at the lower resolution.
    page_selection_enabled: bool = True
    selection_expiry_hours: int = Field(default=24, ge=1)
    pdf_kept_page_dpi: int = Field(default=110, ge=72, le=300)
    # Daily cleanup of uploaded INPUT files (python -m app.cleanup_inputs).
    input_retention_hours: int = Field(default=24, ge=1)
    input_cleanup_max_per_run: int = Field(default=100, ge=1)
    input_cleanup_dry_run: bool = False
    # Reserved: result files (DOCX/PPTX) are never deleted automatically.
    # 0 = off; nothing reads this value yet.
    result_retention_days: int = Field(default=0, ge=0)
    # Page mode: a photographed/scanned page is read as text + flowchart +
    # figures instead of one flowchart. False restores the old behaviour.
    page_mode_enabled: bool = True
    # Ceiling on the length of ONE page answer (output + thinking tokens). Real
    # answers use about 300-2 000; one in ~28 once ran away to 65 000 tokens
    # ($0.10, two minutes). Do not touch temperature, see gemini_provider.
    page_max_output_tokens: int = Field(default=12000, ge=1000)
    # Gemini list prices in USD per million tokens (gemini-3.1-flash-lite); only
    # used to estimate the cost in the usage log. Update when the model changes.
    gemini_price_in_per_m: float = Field(default=0.25, ge=0)
    gemini_price_out_per_m: float = Field(default=1.50, ge=0)
    # Second line of defence against ghost text (ink-contrast check). Off: on
    # real pages the model leaves ghost text out by itself, while the check
    # dropped real lines when a box was a little off. Needs calibration on
    # more photographs before it is switched on.
    ghost_filter_enabled: bool = False
    # A figure box that covers only part of a figure is widened to the whole
    # figure by looking at the ink on the page (stops at text). false = cut the
    # model's box as it is.
    figure_refine_enabled: bool = True
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
