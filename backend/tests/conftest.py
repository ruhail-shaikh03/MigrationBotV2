import pytest
import asyncio
from app.db.engine import engine

@pytest.fixture(scope="session", autouse=True)
async def dispose_engine():
    yield
    await engine.dispose()
