from __future__ import annotations

import os

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv(override=True)

_client: aioredis.Redis | None = None


async def init_redis() -> None:
    global _client
    if _client is not None:
        return
    url = os.environ["REDIS_URL"]
    _client = aioredis.from_url(url, decode_responses=True)


def get_redis() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialised — call init_redis() first.")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
