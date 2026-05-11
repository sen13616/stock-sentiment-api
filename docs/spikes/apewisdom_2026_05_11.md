# ApeWisdom API Spike — 2026-05-11

**Purpose:** Verify ApeWisdom API health, response format, rate limits, and S&P 500 coverage before Sprint B (Phase 2 item 2.3).

---

## 1. Endpoint & Request

```
GET https://apewisdom.io/api/v1.0/filter/all-stocks/
```

No authentication required. No API key. No query parameters needed for default page 1.

Pagination: append `?page=N` (9 pages total, 100 results per page, 851 tickers).

## 2. Response Format

HTTP 200, `Content-Type: application/json`, ~12 KB per page.

```json
{
  "count": 851,
  "pages": 9,
  "current_page": 1,
  "results": [
    {
      "rank": 1,
      "ticker": "MU",
      "name": "Micron Technology",
      "mentions": 603,
      "upvotes": 3370,
      "rank_24h_ago": 1,
      "mentions_24h_ago": 223
    }
  ]
}
```

### Fields per result

| Field | Type | Description |
|-------|------|-------------|
| `rank` | int | Current rank by mention volume |
| `ticker` | string | Stock ticker symbol |
| `name` | string | Company name |
| `mentions` | int | Total mentions (trailing window, likely 48h) |
| `upvotes` | int | Net upvotes on posts containing ticker |
| `rank_24h_ago` | int | Rank 24 hours ago (0 = not ranked) |
| `mentions_24h_ago` | int/null | Mentions 24 hours ago (null = not tracked) |

### Critical finding: NO sentiment field

Checked for: `sentiment`, `sentiment_score`, `bullish`, `bearish`, `score`, `positive`, `negative`. **None exist.** The API returns mention volume and upvote counts only — not a sentiment score.

This contradicts the Phase 2 plan's assumption that ApeWisdom provides a "pre-aggregated retail sentiment score." The ingestion code must derive a signal from `mentions` and `upvotes` rather than consuming a sentiment value directly.

## 3. Rate Limiting

Three calls made ~10 seconds apart:

| Call | Status | Response headers |
|------|--------|-----------------|
| 1 | 200 | No rate-limit headers |
| 2 | 200 | No rate-limit headers |
| 3 | 200 | No rate-limit headers |

No `X-RateLimit-*`, `Retry-After`, or `RateLimit-*` headers observed. Server is Cloudflare (`cf-cache-status: DYNAMIC`). Cloudflare may enforce undocumented limits under heavy load.

**Recommendation:** Use a conservative semaphore (Sem(1), 2s delay) to avoid triggering Cloudflare bot protection. No formal rate-limit contract exists.

## 4. Service Health

- Website live at `https://apewisdom.io/`
- Tracks 15.1K+ stock mentions across Reddit subreddits
- API docs page at `/api/` (minimal)
- No pricing page, no authentication, no documented rate limits
- Service appears community-run; no SLA

## 5. S&P 500 Coverage

Coverage is **sparse and popularity-biased**. From page 1 (top 100 by mentions):

- **Present:** MU, AMD, INTC, NVDA, MSFT, TSLA, META, AAPL, AMZN, GOOG/GOOGL, AVGO, QCOM, ORCL, ADBE, CSCO (heavily skewed toward tech/meme stocks)
- **Absent from page 1:** JPM, WMT, PG, JNJ, UNH, V, MA, BAC, HD, PFE, and most non-tech S&P 500 constituents

Many S&P 500 tickers (AAL, ABBV, CMS, CNC, COO, CPB) are absent entirely from all 851 tracked tickers. Realistically, ~200–300 of our 502 tickers will have zero ApeWisdom data on any given day.

**Implication:** ApeWisdom signals will be structurally sparse. The scoring pipeline already handles missing signals gracefully (shrinkage toward neutral in `compute_sub_index`), so this is acceptable but should be noted in confidence scoring.

## 6. Implications for Sprint B (Item 2.3)

| Plan assumption | Reality | Impact |
|-----------------|---------|--------|
| ApeWisdom returns a sentiment score | Returns mention count + upvotes only | Must z-score normalize mentions into [0,100] signal |
| Pre-aggregated retail sentiment | Raw volume metric | Need rolling z-score (200-obs window per phase2_plan) |
| Broad S&P 500 coverage | ~200–300 of 502 tickers on a typical day | Missing tickers get no ApeWisdom signal; shrinkage handles it |
| Rate limits documented | No documentation, Cloudflare fronted | Use conservative Sem(1) + 2s delay |

### Recommended ingestion approach

1. Fetch all 9 pages (851 tickers) every 6 hours via `influencer_job` cadence or new `apewisdom_job`
2. Store `mentions` and `upvotes` as separate `raw_signals` entries: `signal_type='ape_mentions'` and `signal_type='ape_upvotes'`
3. Z-score normalize using 200-observation rolling window (per phase2_plan.md)
4. Apply `w_src=0.30`, half-life=6h, `w_conf=1.0` (non-textual, no FinBERT)
5. Map z-score to [0,100]: `score = 50 + 10 * z` clamped to [0, 100]

---

**Spike status:** Complete. API is healthy, unauthenticated, and returns usable data — but the absence of a native sentiment field means the ingestion layer must do its own normalization rather than passthrough scoring.
