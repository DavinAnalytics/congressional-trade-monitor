# Congressional Trade Monitor — New Dashboard Tools Reference

**Dashboard:** https://davinanalytics.github.io/congressional-trade-monitor/  
**Raw JSON:** https://davinanalytics.github.io/congressional-trade-monitor/data.json  
**Updates:** Rebuilt daily by GitHub Actions. Timestamp in `generated_at`.

---

## New data endpoints in `data.json`

| Key | Description |
|-----|-------------|
| `briefs` | Structured per-ticker research object (one entry per live signal ticker) |
| `dossiers` | Full ticker dossier — signals, trade counts, fundamentals, conflicts, links, AI context |
| `fundamentals` | Per-ticker fundamental snapshot from yfinance |
| `sector_heatmap` | Tier-weighted sector attention scores |
| `insider_seniority` | Insider title breakdown (totals + per-signal) |
| `cross` | Congress + insider overlap shortlist |
| `prices` | Now includes `markers` array for trade dates on charts |

Existing keys (`alerts`, `trades`, `insider`, `leaderboard`, `performance`, `findings`) are unchanged.

---

## New dashboard tabs

### Cross-signals

Table of tickers bought by both Congress and corporate insiders within the 45-day window. Columns: ticker, congress count, insider count, span (days), first/last trade dates. Tickers are clickable.

### Bot research

- Sector attention map (tier-weighted)
- Insider seniority breakdown (CEO/CFO, officer, director, other)
- Per-signal seniority table
- JSON brief preview
- **Copy research brief** — single ticker JSON to clipboard
- **Copy all live signals** — batch JSON of all current briefs

---

## Ticker dossier (click any ticker)

Opens a modal with everything for one name:

- Live signal tier, conviction score, actionability chip
- Deep links: TradingView, Qualtrim, OpenInsider, Yahoo, SEC
- Fundamental snapshot strip
- Quarterly gross margin sparkline
- TradingView daily chart embed
- Price vs SPY chart with trade-date markers
- Gemini AI context (when available from email digest)
- Earnings proximity + recent headline list
- Congress/insider buy counts and seniority summary
- Committee conflict warnings
- Copy research brief button

---

## Signal card additions

Each signal card now includes:

- **Actionability chip** — `actionable`, `aging`, `stale`, or `unknown`
- **Fundamental strip** — trailing P/E, forward P/E, PEG, gross margin, revenue growth, price/gross profit
- **5y P/E range** — min, median, max, current percentile (when computable)
- **Deep links** row
- **Insider seniority** line + CEO/CFO flag
- **AI context** block (top digest signals only)
- **News/earnings** — headline titles + next earnings date
- **Trade markers** on price chart (gold = congress, green = insider)
- **Copy research brief** button
- Insider table now has a **Seniority** column

---

## `briefs[ticker]` structure

```json
{
  "ticker": "NVDA",
  "name": "NVIDIA Corporation",
  "sector": "Technology",
  "generated_for": "congressional-trade-monitor",
  "signal": {
    "tier": "cross_cluster",
    "label": "CROSS-SIGNAL",
    "conviction": 87,
    "direction": "buy",
    "is_new": true,
    "congress_dollars": 75000,
    "insider_dollars": 500000,
    "lag_days": 12,
    "pct_since_trade": 8.4,
    "spy_since_trade": 2.1,
    "excess_vs_spy": 6.3,
    "conflicts": ["Rep Smith: Armed Services"],
    "insider_seniority": { "CEO/CFO": 1, "officer": 0, "director": 0, "other": 0 }
  },
  "actionability": { "score": "actionable", "reason": "fresh disclosure, move not extended" },
  "fundamentals": {
    "trailing_pe": { "value": 27.5, "reliable": true, "source": "yfinance trailingPE" },
    "peg": { "value": 0.63, "reliable": false, "source": "yfinance pegRatio" }
  },
  "fundamental_quality": { "usable": true, "reliable_fields": 6, "reason": "ok" },
  "historical_pe": {
    "min": 18, "max": 65, "median": 35, "current": 27.5,
    "percentile": 22, "samples": 18,
    "source": "computed from quarterly EPS + daily prices"
  },
  "gross_margin_trend": [
    { "date": "2026-04-30", "margin_pct": 74.9 },
    { "date": "2026-01-31", "margin_pct": 73.2 }
  ],
  "earnings": { "date": "2026-08-26", "days": 14, "label": "in 14d", "source": "yfinance calendar" },
  "headlines": [
    { "title": "...", "publisher": "Reuters", "date": "2026-08-30", "link": "https://..." }
  ],
  "ai_context": "Gemini blurb from email digest, if persisted",
  "links": {
    "tradingview": "https://www.tradingview.com/chart/?symbol=NASDAQ:NVDA",
    "qualtrim": "https://www.qualtrim.com/stock/NVDA",
    "openinsider": "http://openinsider.com/NVDA",
    "yahoo": "https://finance.yahoo.com/quote/NVDA",
    "sec": "https://www.sec.gov/cgi-bin/browse-edgar?..."
  },
  "disclaimer": "Fundamentals are yfinance snapshots for screening — verify material metrics in Qualtrim before auto-filtering. News headlines are not sentiment scores. Congressional amounts are disclosure-bracket midpoints."
}
```

---

## Fundamental fields reference

| Field | Source | `reliable` when |
|-------|--------|-----------------|
| `price` | yfinance | always |
| `trailing_pe` | yfinance trailingPE | positive, < 500 |
| `forward_pe` | yfinance forwardPE | positive, < 500 |
| `peg` | yfinance pegRatio or computed | flagged `false` if cross-check disagrees; omitted if earnings growth ≤ 0 |
| `gross_margin_pct` | quarterly financials | from reported quarter; falls back to yfinance TTM est. (`reliable: false`) |
| `operating_margin_pct` | yfinance profitMargins | always `false` (net margin, not operating) |
| `revenue_growth_pct` | yfinance | always `false` |
| `earnings_growth_pct` | yfinance | always `false` |
| `market_cap` | yfinance | always |
| `price_to_gross_profit` | computed (price / TTM GP per share) | always `false` |

Fields with failed sanity checks are **omitted**, not set to zero.

---

## Actionability values

| `actionability.score` | Condition |
|-----------------------|-----------|
| `actionable` | Disclosure lag ≤ 14 days and excess return vs SPY ≤ 15% |
| `aging` | Lag 15–30 days, or moderate move |
| `stale` | Lag > 30 days, or excess return vs SPY > 15% |
| `unknown` | Missing lag or price data |

---

## Insider seniority buckets

| Bucket | Title patterns matched |
|--------|------------------------|
| `CEO/CFO` | CEO, CFO, Chief Executive, Chief Financial |
| `officer` | President, COO, CTO, Chief, EVP, SVP, VP, Vice President |
| `director` | Director |
| `other` | Everything else |

Available on: insider table rows (`seniority`), signal cards, `briefs.signal.insider_seniority`, `insider_seniority.totals`, `insider_seniority.on_signals`.

---

## Sector heatmap weights

| Tier | Weight |
|------|--------|
| `cross_cluster` | 4 |
| `cluster` | 3 |
| `winrate` | 2 |
| `watchlist` | 1 |

Each sector entry: `{ "sector", "weight", "signals" }`. Weight = sum of (tier weight × conviction/100) across live signals in that sector.

---

## Chart markers (`prices[ticker].markers`)

```json
{ "date": "2026-07-15", "label": "Rep Smith", "kind": "congress" }
{ "date": "2026-07-18", "label": "J Huang", "kind": "insider" }
```

---

## AI context availability

- Generated by Gemini during the daily email digest
- Persisted to gist as `ai_context.json`
- Surfaced on signal cards and dossiers for signals that received a digest blurb (top 3 by conviction per run)
- May be `null` / absent for lower-ranked signals or if Gemini was unavailable

---

## What's still not on the dashboard

- Live quotes / order book
- Intraday data
- Real-time filing push
- Qualtrim-grade TTM financials
- Sentiment scores
- Portfolio/position data
