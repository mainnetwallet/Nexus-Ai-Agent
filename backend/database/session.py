from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config.settings import settings
from backend.database.models import Base

_engine = create_async_engine(f"sqlite+aiosqlite:///{settings.sqlite_path}", echo=False, future=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

ModelT = TypeVar("ModelT")


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
