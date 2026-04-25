"""
main.py — FastAPI application entry point.

Lifespan events:
  startup  → init DB pool, init Redis, start APScheduler
  shutdown → stop scheduler, close Redis, close DB pool
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import health, history, sentiment, status, tickers
from db.connection import close_pool, init_pool
from db.redis import close_redis, init_redis
from pipeline.scheduler import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    await init_pool()
    await init_redis()
    scheduler.start()
    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    await close_redis()
    await close_pool()


app = FastAPI(
    title="SentientMarkets Sentiment API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sentientmarkets.vercel.app",
        "https://themarketmood-ai.vercel.app",
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(sentiment.router, prefix="/v1")
app.include_router(history.router,   prefix="/v1")
app.include_router(tickers.router,   prefix="/v1")
app.include_router(status.router,    prefix="/v1")
