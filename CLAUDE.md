
# CLAUDE.md — Instructions for Claude Code

## Project
SentimentAPI — a precomputed stock sentiment scoring API.
Full spec is in README.md. Read it before doing anything.

## Stack
- Python 3.11+
- FastAPI
- PostgreSQL via asyncpg
- Redis via redis-py async
- APScheduler for background jobs
- Pydantic v2 for schemas

## Rules
- Never put computation in the API request path (api/ folder)
- All scoring logic lives in pipeline/ only
- All database access goes through db/queries/ only — never raw SQL inline
- Every external API call needs a fallback defined in the same function
- Every function that touches an external source needs a try/except
- Use async/await throughout — no sync blocking calls
- Environment variables only via python-dotenv — never hardcode keys
- Follow the module layout in README.md exactly — do not invent new folders

## Environment variables needed
DATABASE_URL, REDIS_URL, ALPHA_VANTAGE_KEY, FINNHUB_KEY, 
NEWSAPI_KEY, POLYGON_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY

## Market hours
US equity markets are open weekdays 14:30–21:00 UTC (9:30 am–4:00 pm Eastern).
The `market_eod_job` runs at 21:15 UTC on weekdays to capture end-of-day data
and ensure a fresh market sub-index is in Redis before the overnight period.
The staleness checker (`pipeline/confidence/staleness.py`) is market-hours-aware:
it does not penalise scores for being computed outside market hours. A score
produced at Friday 21:15 UTC remains fresh all weekend; the 90-minute threshold
only applies while markets are actually open.

## Test command
pytest tests/

## Run command
uvicorn main:app --reload