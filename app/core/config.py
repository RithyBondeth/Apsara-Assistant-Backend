from __future__ import annotations

from functools import cached_property

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Environment ──────────────────────────────────────────────────────────
    # local | dev | production — drives CORS, docs exposure, and logging.
    ENVIRONMENT: str = "local"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── Core (required) ──────────────────────────────────────────────────────
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # ── Password reset & OTP login ───────────────────────────────────────────
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5           # wrong-code guesses before a code is burned
    # Throttle the unauthenticated "send me a link/code" endpoints, per client IP.
    AUTH_RATE_LIMIT: int = 5
    AUTH_RATE_WINDOW_SECONDS: int = 300
    # Base URL of the web app, used to build the password-reset link in emails.
    FRONTEND_BASE_URL: str = "http://localhost:3000"

    # ── Email delivery (SMTP) ────────────────────────────────────────────────
    # When SMTP_HOST is blank, transactional emails (reset links, OTP codes) are
    # logged instead of sent — convenient for local dev. Set these in production.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = "Apsara Assistant <no-reply@apsara.example.com>"

    # Fernet key for encrypting stored secrets (integration tokens) at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If blank, a key is derived from SECRET_KEY (fine for dev; set an explicit
    # key in production so rotating SECRET_KEY doesn't orphan encrypted data).
    ENCRYPTION_KEY: str = ""

    # ── AI ───────────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""

    # ── Public URL (used to build platform webhook callback URLs) ────────────
    # e.g. https://api.apsara.example.com
    PUBLIC_BASE_URL: str = ""

    # ── Rate limiting (public website widget endpoint) ──────────────────────
    WEBSITE_RATE_LIMIT: int = 20          # max requests per window, per (integration, IP)
    WEBSITE_RATE_WINDOW_SECONDS: int = 60

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed frontend origins, e.g.
    # "http://localhost:3000,https://app.apsara.example.com"
    CORS_ORIGINS: str = "http://localhost:3000"

    # ── Storage: AWS S3 (optional) ───────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "ap-southeast-1"

    # ── Storage: Cloudinary (image uploads) ──────────────────────────────────
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("ENVIRONMENT")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        allowed = {"local", "dev", "production"}
        if v not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {sorted(allowed)}")
        return v

    @cached_property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.SMTP_HOST)


settings = Settings()
