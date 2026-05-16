# SentimentMarkets Sentiment API — Usage Guide

**Base URL:** `https://sentimentapi-p.up.railway.app`

---

## Authentication

All requests require a Bearer token in the `Authorization` header.

```
Authorization: Bearer sk-sm-your-api-key
```

API keys are available in two tiers:

| Tier | Rate Limit | Access |
|---|---|---|
| Free | 10 requests/min | Composite score and label only |
| Pro | 120 requests/min | Full breakdown with sub-indices, drivers, and explanation |

---

## Endpoints

### GET /v1/sentiment/{ticker}

Returns the latest pre-computed sentiment score for a US-listed equity ticker.

**Parameters**

| Parameter | Type | Location | Required | Description |
|---|---|---|---|---|
| ticker | string | path | yes | US equity ticker symbol e.g. AAPL |
| detail | string | query | no | `summary` (default) or `full` — full requires Pro tier |
| refresh | boolean | query | no | If true, queues a background refresh and returns current cached score |

**Free Tier Response**

```bash
curl -H "Authorization: Bearer sk-sm-your-key" \
  https://sentimentapi-p.up.railway.app/v1/sentiment/AAPL
```

```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "timestamp": "2026-04-24T14:32:00Z",
  "cache_age_seconds": 480
}
```

**Pro Tier Response**

```bash
curl -H "Authorization: Bearer sk-sm-your-key" \
  "https://sentimentapi-p.up.railway.app/v1/sentiment/AAPL?detail=full"
```

```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "sub_indices": {
    "market": 78,
    "narrative": 69,
    "influencer": 80,
    "macro": 61
  },
  "divergence": "aligned",
  "top_drivers": [
    {
      "signal": "Insider transaction",
      "description": "Insider purchased 12,000 shares",
      "direction": "bullish",
      "magnitude": 0.8,
      "source_layer": "influencer",
      "confidence": 0.9
    },
    {
      "signal": "RSI(14)",
      "description": "RSI(14) = 71.2 (overbought)",
      "direction": "bearish",
      "magnitude": 0.45,
      "source_layer": "market",
      "confidence": 0.78
    }
  ],
  "explanation": "Sentiment is primarily driven by strong insider conviction. Near-term technical conditions show mild overbought pressure.",
  "freshness": {
    "market_as_of": "2026-04-24T14:30:00Z",
    "narrative_as_of": "2026-04-24T14:00:00Z",
    "influencer_as_of": "2026-04-24T08:00:00Z",
    "macro_as_of": "2026-04-24T02:00:00Z"
  },
  "confidence_flags": [],
  "timestamp": "2026-04-24T14:32:00Z",
  "cache_age_seconds": 480
}
```

---

### GET /v1/sentiment/{ticker}/history

Returns historical sentiment scores for a ticker. **Pro tier only.**

```bash
curl -H "Authorization: Bearer sk-sm-your-key" \
  "https://sentimentapi-p.up.railway.app/v1/sentiment/AAPL/history?days=30"
```

**Query Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| days | integer | 30 | Lookback window in days (max 365) |
| interval | string | daily | `daily` (one record per day) or `raw` (every scoring cycle) |

**Response**

```json
{
  "ticker": "AAPL",
  "history": [
    {
      "timestamp": "2026-04-23T14:30:00Z",
      "score": 68,
      "label": "Bullish",
      "confidence": 79,
      "sub_indices": {
        "market": 71,
        "narrative": 65,
        "influencer": 74,
        "macro": 58
      }
    }
  ]
}
```

---

### GET /v1/tickers

Returns the list of tickers in the supported universe.

```bash
curl -H "Authorization: Bearer sk-sm-your-key" \
  https://sentimentapi-p.up.railway.app/v1/tickers
```

**Response**

```json
{
  "universe_size": 502,
  "tickers": ["A", "AA", "AAPL", "ABBV", "..."]
}
```

---

### GET /v1/status

Returns API health and last pipeline run timestamps.

```bash
curl https://sentimentapi-p.up.railway.app/v1/status
```

**Response**

```json
{
  "status": "operational",
  "last_market_run": "2026-04-24T14:30:00Z",
  "last_narrative_run": "2026-04-24T14:00:00Z",
  "last_influencer_run": "2026-04-24T08:00:00Z",
  "last_macro_run": "2026-04-24T02:00:00Z"
}
```

---

### GET /health

Basic health check. No authentication required.

```bash
curl https://sentimentapi-p.up.railway.app/health
```

```json
{ "status": "ok" }
```

---

## Score Labels

| Score | Label |
|---|---|
| 0 – 20 | Strongly Bearish |
| 21 – 40 | Bearish |
| 41 – 60 | Neutral |
| 61 – 80 | Bullish |
| 81 – 100 | Strongly Bullish |

---

## Confidence Score

The `confidence` field (0–100) indicates how reliable the score is. It starts at 100 and is reduced by:

| Condition | Penalty |
|---|---|
| Missing data layer | −15 per layer |
| Stale data source | −10 per source |
| Low signal volume | −20 |
| High divergence between layers | −15 |

A confidence below 60 means the score is based on limited or outdated data and should be interpreted cautiously.

---

## Sub-Indices (Pro Tier)

The composite score is built from four independent sub-indices, each scored 0–100:

| Sub-Index | What It Measures | Sources |
|---|---|---|
| market | What money is doing — price momentum, volume, RSI, options positioning | Alpha Vantage, Finnhub |
| narrative | What the public information environment is saying — news sentiment | Alpha Vantage NEWS_SENTIMENT, Finnhub |
| influencer | What analysts and insiders are doing — ratings, targets, transactions | SEC EDGAR, Finnhub |
| macro | Whether the broader market environment is supportive — VIX, sector trends | Alpha Vantage, Finnhub |

---

## Divergence Field (Pro Tier)

| Value | Meaning |
|---|---|
| `aligned` | All layers broadly agree |
| `moderate_divergence` | Layers show some disagreement (spread > 20 points) |
| `high_divergence` | Layers strongly disagree (spread > 40 points) — interpret with caution |

When `high_divergence` is present, the composite score is capped at 75 regardless of individual layer values.

---

## Freshness (Pro Tier)

The `freshness` object shows when each layer's data was last updated:

- **market_as_of** — refreshes every 15 minutes during market hours
- **narrative_as_of** — refreshes every 30 minutes
- **influencer_as_of** — refreshes every 6 hours
- **macro_as_of** — refreshes daily at 2am UTC

If a layer's `as_of` timestamp is `null`, that layer had no data available and its weight was redistributed to the remaining layers.

---

## No-Data Response

Tickers with insufficient history or outside the supported universe return:

```json
{
  "ticker": "XYZ",
  "status": "insufficient_data",
  "message": "Not enough historical data to compute a reliable sentiment score yet."
}
```

Possible status values:

| Status | Meaning |
|---|---|
| `insufficient_data` | Ticker is supported but has no scored data yet |
| `ticker_not_found` | Ticker is not in the supported universe |
| `temporarily_unavailable` | Temporary service issue |

---

## Error Responses

| HTTP Status | Error Code | Meaning |
|---|---|---|
| 401 | unauthorized | Missing or invalid API key |
| 403 | forbidden | Endpoint requires a higher tier |
| 429 | rate_limit_exceeded | Too many requests — slow down |
| 500 | internal_error | Unexpected server error |

---

## Code Examples

**Python**

```python
import requests

API_KEY = "sk-sm-your-key"
BASE_URL = "https://sentimentapi-p.up.railway.app"

headers = {"Authorization": f"Bearer {API_KEY}"}

# Free tier
response = requests.get(f"{BASE_URL}/v1/sentiment/AAPL", headers=headers)
data = response.json()
print(f"AAPL sentiment: {data['score']} ({data['label']})")

# Pro tier full detail
response = requests.get(
    f"{BASE_URL}/v1/sentiment/AAPL",
    headers=headers,
    params={"detail": "full"}
)
data = response.json()
print(f"Market sub-index: {data['sub_indices']['market']}")
print(f"Explanation: {data['explanation']}")
```

**JavaScript**

```javascript
const API_KEY = "sk-sm-your-key";
const BASE_URL = "https://sentimentapi-p.up.railway.app";

// Free tier
const response = await fetch(`${BASE_URL}/v1/sentiment/AAPL`, {
  headers: { Authorization: `Bearer ${API_KEY}` }
});
const data = await response.json();
console.log(`AAPL: ${data.score} (${data.label})`);

// Pro tier full detail
const proResponse = await fetch(
  `${BASE_URL}/v1/sentiment/AAPL?detail=full`,
  { headers: { Authorization: `Bearer ${API_KEY}` } }
);
const proData = await proResponse.json();
console.log(proData.explanation);
```

**curl**

```bash
# Quick score check
curl -s -H "Authorization: Bearer sk-sm-your-key" \
  https://sentimentapi-p.up.railway.app/v1/sentiment/TSLA | python3 -m json.tool

# Full pro breakdown
curl -s -H "Authorization: Bearer sk-sm-your-key" \
  "https://sentimentapi-p.up.railway.app/v1/sentiment/NVDA?detail=full" | python3 -m json.tool

# Historical scores
curl -s -H "Authorization: Bearer sk-sm-your-key" \
  "https://sentimentapi-p.up.railway.app/v1/sentiment/MSFT/history?days=7" | python3 -m json.tool
```

---

## Supported Universe

502 US-listed equities covering the S&P 500. Full list available at:

```
GET /v1/tickers
```

Any ticker not in the supported universe returns a `ticker_not_found` response.

---

## Data Update Schedule

| Layer | Frequency | Coverage |
|---|---|---|
| Market data | Every 15 min (market hours) | Price, volume, RSI, options |
| News sentiment | Every 30 min | Alpha Vantage, Finnhub news |
| Analyst & insider | Every 6 hours | SEC filings, Finnhub ratings |
| Macro context | Daily at 2am UTC | VIX, sector ETF trends |

Scores are pre-computed and cached — API responses are always under 100ms regardless of which data sources are involved.

---

*SentimentMarkets Sentiment API — built on FastAPI, PostgreSQL, and Redis. Deployed on Railway.*
