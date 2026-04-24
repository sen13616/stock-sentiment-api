"""
db/queries/raw_articles.py

All raw_articles table operations. No raw SQL anywhere else in the codebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db.connection import get_pool


async def hash_exists(ticker: str, content_hash: str) -> bool:
    """Return True if an article with this (ticker, content_hash) already exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM raw_articles
            WHERE ticker       = $1
              AND content_hash = $2
            """,
            ticker,
            content_hash,
        )
    return (n or 0) > 0


async def insert_article(
    ticker: str,
    title: str,
    summary: str | None,
    source: str,
    source_url: str | None,
    published_at: datetime,
    provider_sentiment: float | None,
    relevance_score: float | None,
    content_hash: str,
) -> None:
    """
    Insert one article row. Silently ignores duplicates via ON CONFLICT DO NOTHING
    (unique index on (ticker, content_hash)).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO raw_articles
                (ticker, title, summary, source, source_url, published_at,
                 provider_sentiment, relevance_score, content_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (ticker, content_hash) DO NOTHING
            """,
            ticker,
            title,
            summary,
            source,
            source_url,
            published_at,
            provider_sentiment,
            relevance_score,
            content_hash,
        )


async def get_unclustered_articles(
    ticker: str,
    since_hours: float = 48.0,
) -> list[dict]:
    """
    Return articles for `ticker` published in the last `since_hours` hours
    that have no event_cluster_id assigned yet.

    Returns list of dicts with keys: id, title, published_at, relevance_score, source.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, published_at, relevance_score, source
            FROM raw_articles
            WHERE ticker           = $1
              AND published_at    >= $2
              AND event_cluster_id IS NULL
            ORDER BY published_at ASC
            """,
            ticker,
            since,
        )
    return [dict(r) for r in rows]


async def get_articles_since(
    ticker: str,
    since: datetime,
) -> list[dict]:
    """
    Return articles with provider_sentiment for scoring.

    Only rows where provider_sentiment IS NOT NULL are returned.

    Returns
    -------
    list of dicts with keys: published_at, provider_sentiment, relevance_score, source.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT published_at, provider_sentiment, relevance_score, source
            FROM raw_articles
            WHERE ticker               = $1
              AND published_at        >= $2
              AND provider_sentiment IS NOT NULL
            ORDER BY published_at DESC
            """,
            ticker,
            since,
        )
    return [dict(r) for r in rows]


async def set_cluster_ids(article_ids: list[int], cluster_id: str) -> None:
    """Assign `cluster_id` to each article in `article_ids`."""
    if not article_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE raw_articles
            SET event_cluster_id = $1
            WHERE id = ANY($2::int[])
            """,
            cluster_id,
            article_ids,
        )
