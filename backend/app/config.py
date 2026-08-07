from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError
from typing import List

class Settings(BaseSettings):
    # Database and Caching
    DATABASE_URL: str = "postgresql+asyncpg://migrationbot:migrationbot@localhost:5433/migrationbot"
    REDIS_URL: str = "redis://localhost:6379"

    # API Keys & Auth secrets (allow defaults for developer ease or testing fallback)
    DEEPSEEK_API_KEY: str = "mock-deepseek-key"
    GOOGLE_CLIENT_ID: str = "mock-google-id"
    GOOGLE_CLIENT_SECRET: str = "mock-google-secret"
    JWT_SECRET: str = "mock-jwt-secret-at-least-32-characters-long"

    # Default project sheet parameters — no built-in fallback; each deployment must
    # provide its own spreadsheet, admin list, and allowed origins via .env.
    DEFAULT_SPREADSHEET_ID: str
    DEFAULT_SHEET_TAB: str = "SD"
    DEFAULT_SHEET_LABEL: str = "FF Migration Tracker"

    # Admin access configuration
    ADMIN_EMAILS: str
    CORS_ORIGINS: str

    @property
    def admin_emails_list(self) -> List[str]:
        import os
        raw = os.getenv("ADMIN_EMAILS", self.ADMIN_EMAILS)
        return [email.strip().lower() for email in raw.split(",") if email.strip()]

    # Pydantic Configuration to read from environment variables or .env file
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), # Tells Pydantic to check one folder up, too!
        env_file_encoding="utf-8",
        extra="ignore"
    )

try:
    settings = Settings()
except ValidationError as exc:
    missing = ", ".join(sorted({str(err["loc"][0]) for err in exc.errors() if err["type"] == "missing"}))
    raise RuntimeError(
        "Missing required environment variable(s): "
        f"{missing or exc}. Set ADMIN_EMAILS, CORS_ORIGINS, and DEFAULT_SPREADSHEET_ID "
        "in your .env file — these no longer fall back to hardcoded production values. "
        "See .env.example for the expected format."
    ) from exc
