from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

# Create async engine with postgresql+asyncpg
# Using settings.DATABASE_URL
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,  # Set to True for SQL log output if debugging
    future=True,
    pool_pre_ping=True
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# Base class for SQLAlchemy ORM models
class Base(DeclarativeBase):
    pass

# FastAPI Dependency to yield database sessions
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

# Helper to initialize database tables
async def init_db() -> None:
    from app.models import Base as ModelsBase
    async with engine.begin() as conn:
        await conn.run_sync(ModelsBase.metadata.create_all)

# Helper to drop all database tables (useful for test resets)
async def drop_db() -> None:
    from app.models import Base as ModelsBase
    async with engine.begin() as conn:
        await conn.run_sync(ModelsBase.metadata.drop_all)
