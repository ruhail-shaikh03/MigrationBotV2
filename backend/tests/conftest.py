import re
import pytest
import asyncio
from app.config import settings
from app.db.engine import engine


def require_test_database() -> None:
    """
    Guard against destructive fixtures (drop_db()) running against a non-test
    database. DATABASE_URL's database name must contain "test" (case-insensitive)
    — e.g. `migrationbot_test`, matching the CI convention (.github/workflows/ci.yml).
    Refuses loudly instead of silently dropping whatever tables it finds — pytest
    run from backend/ with a .env pointed at a real database is exactly how a table
    gets dropped by accident.
    """
    db_url = settings.DATABASE_URL
    match = re.search(r"/([^/?]+)(?:\?.*)?$", db_url)
    db_name = match.group(1) if match else ""
    if "test" not in db_name.lower():
        raise RuntimeError(
            "Refusing to run destructive database fixtures: DATABASE_URL does not "
            f"point at a database whose name contains 'test' (got {db_name!r}). "
            "Point DATABASE_URL at a dedicated test database, e.g. "
            "postgresql+asyncpg://user:pass@host:port/migrationbot_test"
        )


@pytest.fixture(scope="session", autouse=True)
async def dispose_engine():
    yield
    await engine.dispose()
