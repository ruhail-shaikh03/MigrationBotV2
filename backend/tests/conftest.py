import re
import pytest
import asyncio
from unittest.mock import AsyncMock, patch
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


@pytest.fixture(autouse=True)
def cold_row_cache():
    """Start every test with an empty row cache, whether or not Redis is running.

    `sheets/rows_cache.py` keys on `(spreadsheet_id, active_tab)`, which is correct in
    production — one key, one tab, one set of rows. In the suite it is not: almost every
    read test builds a different fixture under the *same* `spreadsheet_id="sheet-123"` and
    `active_tab="SD"`, so they all collide on one key.

    That was harmless until agent reads started going through this cache. After that, on a
    machine with no Redis every call quietly degraded to a live scan and the suite passed;
    in CI, where Redis is a real service, the first test to run warmed the key and the next
    five read *its* rows instead of their own mock's — `data_quality` counted two blank
    statuses where its fixture has one, and a module breakdown came back "SD" for a fixture
    whose only value is "Sales & Distribution". Five failures that reproduce nowhere except
    CI, and none of them a real defect.

    So the default is a cache that is always cold and never fills: reads under test hit
    their own mock service, deterministically, in both places. `test_rows_cache.py` — the
    file that exists to test caching — patches `redis_client` itself inside each test, and
    that patch nests inside this one and wins.
    """
    client = AsyncMock()
    client.get.return_value = None
    with patch("app.sheets.rows_cache.redis_client", client):
        yield


@pytest.fixture(autouse=True)
def cold_record_cache():
    """Keep the durable Postgres tier out of every test that is not about it.

    The same collision the Redis fixture above exists to prevent, with the safety net
    removed. `sheet_records` is keyed on `(spreadsheet_id, tab)` too, so the suite's shared
    `sheet-123`/`SD` fixtures land on top of each other exactly as they did in Redis — but
    Postgres has no 60-second TTL, so a row written by one test stays visible to every test
    that runs after it, in that run and in the next one against the same database. In CI,
    where Postgres is a real service, that turns cross-test contamination from a five-test
    flake into a persistent one.

    Patching the two entry points rather than the session factory keeps the failure mode
    honest: `read_tab` returning None is the same answer a cold or unverifiable tab gives in
    production, so tests exercise the live-scan path deliberately instead of by accident.
    `test_records_cache.py` patches these back to the real implementations for the tests
    whose subject they are, and those patches nest inside this one and win.
    """
    with patch("app.sheets.records_cache.read_tab", AsyncMock(return_value=None)), \
         patch("app.sheets.records_cache.store_tab", AsyncMock(return_value=None)), \
         patch("app.sheets.records_cache.mark_dirty", AsyncMock(return_value=None)):
        yield
