from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"

    LOG_LEVEL: str = "INFO"
    # "json" for a log shipper, "text" for a terminal.
    LOG_FORMAT: str = "text"

    # Error tracking. Empty disables it entirely.
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0

    # Comma-separated origins allowed to call this API with credentials.
    # Empty falls back to "*", which is only tolerable in development.
    CORS_ORIGINS: str = ""

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080
    AUTH_COOKIE_NAME: str = "apsara_access_token"
    AUTH_COOKIE_SECURE: bool = False

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Where the web app is reachable — used to build password reset links.
    APP_BASE_URL: str = "http://localhost:3000"

    # Where this API is reachable from the internet — used to tell a seller
    # which URL to register with Meta or Telegram.
    API_BASE_URL: str = "http://localhost:8000"

    # Any provider with an SMTP endpoint works here (SES, Resend, Postmark,
    # Mailgun). With SMTP_HOST unset, mail is written to the log instead of
    # sent, so local development needs no mail account.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "Apsara Assistant <no-reply@apsara.local>"
    SMTP_STARTTLS: bool = True

    # Encrypts stored page/bot tokens. Derived from SECRET_KEY when unset —
    # set it to decouple credential rotation from token readability.
    PLATFORM_TOKEN_KEY: str = ""

    # One Meta app serves every connected page: the app secret signs webhook
    # payloads, and the verify token answers the subscription handshake.
    META_APP_SECRET: str = ""
    META_VERIFY_TOKEN: str = ""
    GRAPH_API_VERSION: str = "v21.0"

    # "inline" processes queued work in the web process, so a single-process
    # deployment needs no separate worker. "worker" leaves it entirely to
    # `python -m app.worker`. Either way the job is persisted first.
    JOB_RUNNER: str = "inline"
    # How long a claimed job may be held before it is assumed orphaned.
    JOB_LEASE_SECONDS: int = 300
    JOB_POLL_SECONDS: float = 2.0
    # Completed queue rows are useful operational evidence, but not forever.
    JOB_RETENTION_DAYS: int = 7

    # Assistant replies one seller may spend per day. 0 disables the ceiling.
    AI_DAILY_REPLY_LIMIT: int = 500

    # Failed sign-ins one account may accumulate before the endpoint starts
    # refusing it, and the sliding window they are counted over. 0 disables
    # throttling — bcrypt alone is not a substitute, so leave it on.
    LOGIN_MAX_ATTEMPTS: int = 10
    # The same ceiling for one client address, across every account it tries.
    # Loose, because an office or a phone network shares one address. 0 counts
    # by account only.
    LOGIN_MAX_ATTEMPTS_PER_IP: int = 50
    LOGIN_ATTEMPT_WINDOW_MINUTES: int = 15

    # Whether X-Forwarded-For can be believed. True only when a proxy the app
    # controls sets it: when nothing strips an inbound header, a caller can put
    # any address there and step around the per-address ceiling at will.
    TRUST_PROXY_HEADERS: bool = False

    # Lifetimes for emailed codes.
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    # Minimum gap between code requests for the same account, in seconds.
    CODE_REQUEST_COOLDOWN_SECONDS: int = 60

    @property
    def login_attempt_window(self) -> timedelta:
        return timedelta(minutes=self.LOGIN_ATTEMPT_WINDOW_MINUTES)

    @property
    def auth_cookie_secure(self) -> bool:
        # HTTPS is mandatory outside local development; do not let a missed
        # environment variable quietly ship a session cookie over HTTP.
        return self.AUTH_COOKIE_SECURE or self.ENVIRONMENT != "development"

    @property
    def cors_origins(self) -> list[str]:
        """Origins permitted to call the API.

        Credentialed requests from any origin is not something to ship, so a
        wildcard is refused outside development rather than quietly allowed.
        """
        configured = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        if configured:
            return configured
        if self.ENVIRONMENT != "development":
            raise RuntimeError(
                "CORS_ORIGINS must be set outside development: allowing "
                "credentialed requests from any origin is not safe to deploy."
            )
        return ["*"]

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
