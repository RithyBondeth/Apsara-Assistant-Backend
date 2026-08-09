from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Where the web app is reachable — used to build password reset links.
    APP_BASE_URL: str = "http://localhost:3000"

    # Any provider with an SMTP endpoint works here (SES, Resend, Postmark,
    # Mailgun). With SMTP_HOST unset, mail is written to the log instead of
    # sent, so local development needs no mail account.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "Apsara Assistant <no-reply@apsara.local>"
    SMTP_STARTTLS: bool = True

    # Lifetimes for emailed codes.
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    # Minimum gap between code requests for the same account, in seconds.
    CODE_REQUEST_COOLDOWN_SECONDS: int = 60

    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "ap-southeast-1"

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
