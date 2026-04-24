from __future__ import annotations

import os

import asyncpg
from dotenv import load_dotenv

load_dotenv(override=True)

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = os.environ["DATABASE_URL"]
    # asyncpg expects postgresql:// — strip SQLAlchemy-style dialect suffix if present
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgres://"
    )


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(dsn=_dsn())


async def get_pool() -> asyncpg.Pool:
    """Return the connection pool, initialising it automatically if needed."""
    global _pool
    if _pool is None:
        await init_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
