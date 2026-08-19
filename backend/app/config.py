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

    # No default: a shared, publicly-known fallback here would let any deployment that
    # forgets to set this accept tokens forged against that known string. Required like
    # the three below.
    JWT_SECRET: str

    # Off by default. When true, get_current_user/authenticate_ws_user accept any
    # 'mock-'-prefixed or '@'-containing bearer token as that identity with no signature
    # check — needed for local dev and the test suite, but must never be reachable in a
    # deployment that isn't explicitly opted in.
    ALLOW_DEV_AUTH: bool = False

    # Off by default, and deliberately so. Streaming changes how `tool_calls` reach the
    # loop: instead of one complete object they arrive as partial JSON fragments spread
    # across deltas and have to be reassembled. The assembly is unit-tested against the
    # documented delta shape, but no test here can prove the provider actually emits that
    # shape — and if it does not, every write breaks, silently, at the point of dispatch.
    # So the deploy stays on the code path that has been running for months until someone
    # can watch a real streamed turn produce a real write (§8.3).
    STREAM_RESPONSES: bool = False

    # Fail-closed default role for a caller with no explicit permissions row (§6.2 in
    # TDD.md). "viewer" so an unknown grant surface can never write; override only if a
    # deployment genuinely wants the old fail-open behavior.
    DEFAULT_ROLE: str = "viewer"

    # Default project sheet parameters — no built-in fallback; each deployment must
    # provide its own spreadsheet, admin list, and allowed origins via .env.
    DEFAULT_SPREADSHEET_ID: str
    DEFAULT_SHEET_TAB: str = "SD"
    DEFAULT_SHEET_LABEL: str = "FF Migration Tracker"

    # Admin access configuration
    ADMIN_EMAILS: str
    CORS_ORIGINS: str

    # Drive push notifications (§11.2). Both default to empty, and that is a deliberate
    # departure from the required-no-default treatment JWT_SECRET and the three above get.
    # Those are required because a deployment missing them is *insecure or broken*; a
    # deployment missing these simply has no push channel, and falls back to polling
    # `modifiedTime` — slower, entirely correct. Making them required would crash-loop
    # backend and worker on every deployment that had not set them yet, which is the exact
    # failure documented in §3 that cost 21 hours of downtime. Registration refuses with a
    # clear message when they are unset instead (api/admin.py), which fails one endpoint
    # rather than the whole application.
    #
    # PUBLIC_BASE_URL is the externally reachable origin Drive posts back to, e.g.
    # "https://migrationbot.duckdns.org" — its domain must be verified in the Cloud Console
    # project or Drive refuses the channel. DRIVE_WEBHOOK_TOKEN is the shared secret echoed
    # back in X-Goog-Channel-Token; it is what authenticates Drive to an endpoint that
    # cannot carry user credentials, so an empty value disables the receiver outright rather
    # than accepting unauthenticated pushes.
    PUBLIC_BASE_URL: str = ""
    DRIVE_WEBHOOK_TOKEN: str = ""

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
        f"{missing or exc}. Set ADMIN_EMAILS, CORS_ORIGINS, DEFAULT_SPREADSHEET_ID, and "
        "JWT_SECRET in your .env file — these no longer fall back to hardcoded production "
        "values. See .env.example for the expected format."
    ) from exc
