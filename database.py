"""Async SQLAlchemy and SQLite WAL configuration."""

from __future__ import annotations

from collections.abc import AsyncIterator
import os
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import BASE_DIR, settings
from models import Base


def _database_url() -> str:
    if configured_url := os.environ.get("DATABASE_URL"):
        return configured_url

    configured_path = Path(str(settings.database_path))
    if not configured_path.is_absolute():
        configured_path = BASE_DIR / configured_path
    return f"sqlite+aiosqlite:///{configured_path.resolve()}"


DATABASE_URL = _database_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=bool(getattr(settings, "database_echo", False)),
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """Apply connection-level SQLite safety and concurrency settings."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


async def init_db() -> None:
    """Create the initial schema and verify that SQLite is using WAL."""
    database = engine.url.database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        result = await connection.execute(text("PRAGMA journal_mode"))
        journal_mode = str(result.scalar_one()).lower()
        if journal_mode != "wal" and database != ":memory:":
            raise RuntimeError(f"SQLite WAL mode was not enabled: {journal_mode}")


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that scopes one async session to one request."""
    async with async_session() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def database_status(session: AsyncSession) -> dict[str, str | bool]:
    await session.execute(text("SELECT 1"))
    result = await session.execute(text("PRAGMA journal_mode"))
    return {
        "connected": True,
        "journal_mode": str(result.scalar_one()).lower(),
    }


async def dispose_engine() -> None:
    await engine.dispose()
