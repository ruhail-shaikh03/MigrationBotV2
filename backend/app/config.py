from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional

class Settings(BaseSettings):
    # Database and Caching
    DATABASE_URL: str = "postgresql+asyncpg://migrationbot:migrationbot@localhost:5433/migrationbot"
    REDIS_URL: str = "redis://localhost:6379"

    # API Keys & Auth secrets (allow defaults for developer ease or testing fallback)
    DEEPSEEK_API_KEY: str = "mock-deepseek-key"
    GOOGLE_CLIENT_ID: str = "mock-google-id"
    GOOGLE_CLIENT_SECRET: str = "mock-google-secret"
    JWT_SECRET: str = "mock-jwt-secret-at-least-32-characters-long"

    # Default project sheet parameters
    DEFAULT_SPREADSHEET_ID: str = "17mrUyJbhOhBbaQYzQ4iPFH6kPPHBjqOR3dt2EWGCDUA"
    DEFAULT_SHEET_TAB: str = "SD"
    DEFAULT_SHEET_LABEL: str = "FF Migration Tracker"

    # Admin access configuration
    ADMIN_EMAILS: str = "ruhail.rizwan@tmcltd.com"
    CORS_ORIGINS: str = "https://migrationbot.duckdns.org,http://localhost:3000"

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

settings = Settings()
