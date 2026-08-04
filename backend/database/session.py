from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, TypeVar

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config.settings import settings
from backend.database.models import Base

_engine = create_async_engine(f"sqlite+aiosqlite:///{settings.sqlite_path}", echo=False, future=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

ModelT = TypeVar("ModelT")

# Columns added to `memory_entries` after its original release (Memory
# Improvements: importance scoring, categories, archive/forget, expiration,
# duplicate merging). `Base.metadata.create_all` only creates tables that
# don't exist yet -- it never alters an existing table -- so anyone with a
# pre-existing nexus_agent.db needs these columns added in place. Each tuple
# is (column_name, DDL type + default) exactly as it should appear after
# "ADD COLUMN". Keep this in sync with the Mapped columns on MemoryEntry.
_MEMORY_ENTRY_MIGRATIONS: list[tuple[str, str]] = [
    ("category", "VARCHAR(32) DEFAULT 'general'"),
    ("importance", "FLOAT DEFAULT 0.5"),
    ("content_hash", "VARCHAR(64)"),
    ("access_count", "INTEGER DEFAULT 0"),
    ("last_accessed_at", "DATETIME"),
    ("archived", "BOOLEAN DEFAULT 0"),
    ("archived_at", "DATETIME"),
    ("expires_at", "DATETIME"),
    ("merged_count", "INTEGER DEFAULT 0"),
]


async def _migrate_memory_entries(conn: Any) -> None:
    """Best-effort additive migration: add any Memory Improvements columns
    missing from an existing `memory_entries` table. No-op on a fresh DB
    (create_all already created the table with every column) and safe to
    run every startup (only issues ALTER TABLE for columns that aren't
    already there)."""
    result = await conn.execute(text("PRAGMA table_info(memory_entries)"))
    existing = {row[1] for row in result.fetchall()}  # row[1] = column name
    for column_name, ddl_type in _MEMORY_ENTRY_MIGRATIONS:
        if column_name not in existing:
            await conn.execute(text(f"ALTER TABLE memory_entries ADD COLUMN {column_name} {ddl_type}"))


_WALLET_MIGRATIONS: list[tuple[str, str]] = [
    # enabled: independent per-wallet on/off switch added alongside the
    # pre-existing exclusive is_active flag -- see WalletRecord.enabled.
    # Existing rows default to 1 (enabled) so upgrading never silently
    # disables wallets someone already imported.
    ("enabled", "BOOLEAN DEFAULT 1"),
]


async def _migrate_wallets(conn: Any) -> None:
    """Best-effort additive migration: add any wallet columns missing from
    an existing `wallets` table. No-op on a fresh DB."""
    result = await conn.execute(text("PRAGMA table_info(wallets)"))
    existing = {row[1] for row in result.fetchall()}
    for column_name, ddl_type in _WALLET_MIGRATIONS:
        if column_name not in existing:
            await conn.execute(text(f"ALTER TABLE wallets ADD COLUMN {column_name} {ddl_type}"))


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_memory_entries(conn)
        await _migrate_wallets(conn)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def list_all(model: type[ModelT], order_by: Any = None, limit: int | None = 100) -> list[ModelT]:
    """
    Shared "select rows, newest/priority first, optionally capped" query
    used by routes_tasks/routes_reports/routes_wallet -- previously each
    route duplicated this session/select/scalars boilerplate. Route-specific
    JSON shaping still happens in each route module; this only removes the
    repeated query plumbing. limit=None means no cap (matches the original
    uncapped list_wallets query).
    """
    async with get_session() as session:
        stmt = select(model)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())
