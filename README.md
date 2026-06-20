# Congressional Trade Monitor
**Author:** Davin Kim  
**Status:** ✅ Complete - all modules built and tested  
**Live dashboard:** https://congressional-trade-monitor.streamlit.app/  
**Stack:** Python, Requests, BeautifulSoup, pdfplumber, yfinance, smtplib, python-dotenv, Streamlit, Altair, google-genai (Gemini 2.0 Flash)  
**Purpose:** Personal-use automation tool that monitors congressional stock disclosures **and corporate insider (CEO/CFO) open-market buys**, detects high-signal trading patterns — including tickers accumulated by Congress and company executives at the same time — sends email alerts on schedule, and provides a visual dashboard for exploratory analysis.

---

## What It Does

Congress members are required by the STOCK Act (2012) to publicly disclose stock trades within 45 days. This tool automates monitoring of those disclosures across both chambers, detects meaningful patterns, and alerts via email when a signal fires.

**Core insight driving the design:** The top-performer leaderboard is non-sticky year to year; none of the top performers of 2024 showed up in top performers of 2025. Instead of chasing one politician (e.g. Pelosi), broad monitoring with cluster detection is the smarter decision.

**Insider cross-referencing:** Beyond Congress, the monitor also scrapes open-market purchases by corporate CEOs and CFOs (via OpenInsider) and raises a **Cross-Signal** alert when the same ticker is being accumulated by both Congress and a company's own executives within the 45-day window — a stronger conviction signal than either source alone.

---

## Alert Tiers

| Tier | Signal | Trigger |
|------|--------|---------|
| ⚡ Cluster Alert | 2+ members buy/sell same ticker within 45 days | Strongest congressional signal; email includes Gemini AI context explaining why the cluster may be forming |
| 🏆 Win-Rate Alert | Member with >60% historical win rate files new trade | Individual quality filter |
| 👁️ Watchlist Alert | Specific named politician files anything | Manual tracking |
| 🔗 Cross-Signal Alert | Same ticker bought by **both** Congress and a corporate CEO/CFO within 45 days | Combined-conviction signal; email includes Gemini AI context |

Alert header color in the dashboard and email reflects trade direction: **green** for net buy activity, **red** for net sell activity — independent of tier.

---

## Quickstart

```bash
pip install -r requirements.txt

# Set up credentials
cp .env.example .env
# Edit .env with your Gmail sender, app password, and recipient

# Launch the visual dashboard (no credentials required)
python -m streamlit run dashboard.py

# Preview filtered OpenInsider CEO/CFO buys
python openinsider_fetcher.py

# Test one full cycle (fetch → analyze → alerts)
python monitor.py --once

# Send a daily digest email
python monitor.py --summary

# Run forever (polls every 4 hours)
python monitor.py
```

---

## Dashboard

`dashboard.py` is a Streamlit app that provides a read-only visual interface over the same data sources the monitor uses. It **never calls `analyzer.analyze()`** — only the individual detectors — so it cannot mutate `seen_trades.json` or trigger duplicate email alerts.

```bash
python -m streamlit run dashboard.py
```

> Use `python -m streamlit` (not `streamlit`) to ensure the same Python environment that has all dependencies installed.

### Tabs

| Tab | Contents |
|-----|----------|
| 🔔 Alerts | All fired alerts with colored buy/sell header, trades table, win rate, committee assignments, Altair price chart vs SPY, TradingView link |
| 📋 Trades | Full trade log filterable by sector and type; win rate progress bar; committee column; click any row for member detail modal |
| 🏆 Leaderboard | Win rate rankings; click any row for member detail modal |
| 🏢 Insider Buys | CEO/CFO open-market buys (date, name, title, company, ticker, total value, shares, price), sorted by total value descending, with rows highlighted when Congress also bought the ticker in the same window; a correlations panel listing every ticker appearing in both datasets (most recent signal first); and a **Top 5 Buys** section charting each of the five largest buys vs SPY since the purchase date |

### Activity Summary

Between the metric header and the tabs, a summary panel shows:

- **🔥 Hot Sector / ❄️ Avoid Sector** — derived from net buy vs sell activity across `config.SECTOR_TICKERS`; Avoid Sector only shown when traded tickers span more than one sector
- **⚖️ Net Activity** — top 5 most net-bought and most net-sold tickers, by trade count and estimated dollar volume; each ticker is a clickable TradingView link

### Sidebar Controls

| Control | Effect |
|---------|--------|
| Days window (7–90) | Filters trades in memory — no re-fetch |
| Sector filter | Filters Trades tab by `config.SECTOR_TICKERS` membership |
| Trade type | Purchase / Sale / Partial Sale |
| Refresh Data | Clears all caches and re-fetches from live government sources |

Trade data and win rates are cached for 1 hour. The days slider, sector, and type filters all apply in memory instantly — only "Refresh Data" triggers a live fetch.

### Price Performance Charts

The Alerts tab and the Insider Buys "Top 5" section share a single chart renderer (`_render_price_chart`), so both look identical: an Altair line chart indexed to 100 at the start date (the first trade in the alert window, or the insider's purchase date), comparing the stock (blue, `#3a86ff`) vs SPY (red, `#e63946`). Y-axis is zoomed to the actual data range so small divergences are visible.

Price history (`_get_price_history`) is fetched from yfinance at daily granularity (`interval="1d"`) and the index is reduced to bare dates, so the x-axis shows one clean tick per day (`Jun 11`, `Jun 12`, …) with no intraday "12 PM" labels even on very short windows. Only dates where **both** the ticker and SPY have a posted close are plotted, so the two lines always span the same range — smaller or foreign tickers that lag SPY by a day on yfinance no longer leave one line trailing past the other.

---

## File Structure

```
congressional-trade-monitor/
├── config.py            # Watchlist, alert thresholds, email settings (safe to commit)
├── fetcher.py           # House + Senate data fetchers
├── openinsider_fetcher.py # OpenInsider CEO/CFO open-market buy scraper (value + market-cap filtered)
├── analyzer.py          # Cluster + cross-signal detection, win-rate leaderboard
├── committees.py        # Committee assignments + conflict detection (official gov sources)
├── notifier.py          # Email alert formatting and sending
├── monitor.py           # Main polling loop
├── dashboard.py         # Streamlit visual dashboard (read-only, no side effects)
├── .env                 # Your credentials — gitignored, never committed
├── .env.example         # Credential template — committed, no real values
├── .gitignore           # Blocks .env and seen_trades.json from git
├── requirements.txt     # pip install -r requirements.txt
├── seen_trades.json     # Auto-created state file — gitignored
├── .github/
│   └── workflows/
│       └── monitor.yml  # Daily 6 AM PST — alerts Mon-Sat, digest on Sunday
└── README.md
```

---

## Automated Scheduling (GitHub Actions)

The monitor runs automatically on GitHub's servers, allowing it to run without the local computer.

| Day | Schedule | What it does |
|-----|----------|--------------|
| Monday – Saturday | 6:00 AM PST | Fetches both chambers, detects signals, emails alerts for new trades |
| Sunday | 6:00 AM PST | Sends a weekly digest: sector accumulation vs distribution table, top signals of the week, and Gemini-grounded legislative intelligence |

Both are handled by a single `monitor.yml` workflow. The script checks the day of week and runs `--once` or `--summary` accordingly.

**State persistence:** `seen_trades.json` is stored in a private GitHub Gist between runs so duplicate alerts are never sent. Each run loads the Gist at start and saves back on completion.

**Manual trigger:** The workflow has a `workflow_dispatch` trigger. You can run it on demand from the GitHub Actions tab at any time.

---

## AI-Powered Features (Gemini 2.0 Flash)

All AI features use Google Search grounding, so Gemini pulls real-time search results rather than relying on training data. Every feature degrades gracefully — if `GEMINI_API_KEY` is not set, the email still sends with all deterministic content intact and the AI blocks are simply omitted.

### Alert Context (Cluster + Cross-Signal emails)

When a cluster or cross-cluster alert fires, `generate_alert_context()` in `notifier.py` makes one grounded Gemini call asking why the signal might be forming right now. The response (2–3 sentences) appears in the email as a blue **"AI Context · Gemini + Google Search"** block:

> *"Jensen Huang testified before the Senate Commerce Committee on AI export controls on June 17. The Semiconductor Export Reform Act cleared committee markup June 18. Two of the three congressional buyers sit on Science & Technology subcommittees with direct chip-policy authority."*

**Cost:** ~$0.035 per alert (Google Search grounding). Free tier covers 1,500 grounded calls/day — well within limits for personal use.

### Weekly Digest (Sunday email)

The Sunday digest is rebuilt from a sparse alert list into a four-section intelligence report:

| Section | Source |
|---------|--------|
| Stats (Trades / Alerts / Sectors Active) | Deterministic |
| **Sector Activity table** — buy count, sell count, net ▲▼ per sector | Deterministic from `config.SECTOR_TICKERS` |
| **Strongest Signals** — top 5 alerts with color-coded tier badges | From `alerts` list |
| **Legislative Intelligence** — 3–4 grounded bullet points on bills/regulatory actions that advanced this week and their sector implications | `generate_weekly_intelligence()` → Gemini + Google Search |

**Cost:** 1 grounded call per Sunday ≈ $0.035/week ≈ $1.82/year.

---

## Data Sources

Congressional and committee data comes directly from official U.S. government sources; corporate insider buys come from OpenInsider (a free aggregator of SEC Form 4 filings). No paid APIs, no keys required, no paywalls.

| Source | Endpoint | Method |
|--------|----------|--------|
| Senate | `efdsearch.senate.gov` | Session POST (CSRF + terms agreement) → HTML table parsing |
| House | `disclosures-clerk.house.gov` | POST filing index → pdfplumber PDF parsing |
| Corporate Insiders (CEO/CFO) | `openinsider.com` | Pre-filtered screener (open-market buys, CEO+CFO, 45 days) → HTML table parsing |
| Committee Assignments | `clerk.house.gov/xml/lists/MemberData.xml` | XML parsing — House members + committee codes |
| Committee Assignments | `senate.gov/general/committee_assignments/assignments.htm` | HTML parsing — Senate members + committees |

### Why not the popular free APIs?

Every third-party aggregator was evaluated and rejected during development:

- **House/Senate Stock Watcher** — Dead as of early 2026. S3 bucket 403, domain unreachable.
- **Financial Modeling Prep (FMP)** — Congressional endpoints paywalled after August 2025.
- **Capitol Trades** — No public API.
- **Capitol Trace** — Auth failures.
- **Quiver Quantitative** — Requires authentication.

**Decision:** Build directly against official government sources. Permanent, free, zero third-party dependency.

---

## Architecture

### Senate: HTML parsing (no PDF needed)
The Senate eFD viewer pages render transaction data as a clean HTML table. No PDF download required.

**Session flow:**
1. GET `/search/home/` → receive CSRF cookie
2. POST `/search/home/` with `prohibition_agreement=1` → unlock filing access
3. POST `/search/report/data/` with CSRF → get JSON filing index (91 filings found in test)
4. GET each `/search/view/ptr/{uuid}/` → parse HTML transaction table

### House: PDF parsing
House PTR filings are only available as PDFs. The Clerk search endpoint returns server-rendered HTML (confirmed — not a React SPA), so a plain POST gives us the full filing index. Each PDF is parsed with pdfplumber using regex to extract ticker, type, date, and amount.

### Committee conflict detection
`committees.py` fetches committee assignments for all 535 members from official government XML and HTML sources. On every watchlist alert, the member's committees and subcommittees are cross-referenced against sector-to-committee mappings in `config.py`. If a member sits on a committee with oversight authority over the traded ticker's sector, the conflict is flagged in the email.

**Name format fix:** Trade disclosures return names as `"Last, First Middle"` (e.g. `"Taylor, David J."`) while the committee cache keys names as `"First Last"`. `get_member_committees()` detects the comma-separated format and retries with both `"First Middle Last"` and `"First Last"` (dropping the middle initial), dramatically improving committee coverage.

**Example:** Whitehouse sells NVDA → flagged for sitting on Commerce/Science/Transportation, International Trade subcommittee (chip export policy), and Emerging Threats and Capabilities.

### Unified output schema
Both chambers normalize to the same dict so all downstream modules are chamber-agnostic:

```python
{
    "chamber":           "Senate" | "House",
    "representative":    "Sheldon Whitehouse",
    "ticker":            "NVDA",
    "asset_description": "NVIDIA Corporation - Common Stock",
    "type":              "purchase" | "sale" | "sale_partial",
    "transaction_date":  "2026-05-08",
    "disclosure_date":   "06/02/2026",
    "amount":            "$100,001 - $250,000",
    "ptr_link":          "https://...",
    "owner":             "Self" | "Spouse" | "Dependent Child" | "",
}
```

### Insider extension & cross-signal detection
`openinsider_fetcher.py` scrapes a pre-filtered OpenInsider screener (open-market purchases only, CEO + CFO titles, last 45 days) and parses the results HTML table into the same flat-dict shape used elsewhere, tagged with `"source": "insider"`:

```python
{
    "source":           "insider",
    "name":             "Andrew Anagnost",
    "title":            "Pres, CEO",
    "company":          "Autodesk, Inc.",
    "ticker":           "ADSK",
    "type":             "purchase",
    "transaction_date": "2026-06-16",
    "disclosure_date":  "2026-06-16",
    "shares":           2460,
    "price":            202.66,
    "value":            498544.0,
    "amount":           "+$498,544",
    "ptr_link":         "https://openinsider.com/ADSK",
}
```

**Noise filtering at the scraper boundary** (so analyzer, notifier, and dashboard all receive a clean list):
- **Minimum trade value** — drops buys under `MIN_TRADE_VALUE` ($50k), removing micro-cap penny-stock noise.
- **Minimum market cap** — the OpenInsider screener exposes no market-cap parameter, so market cap is looked up per unique ticker via yfinance and buys under `MIN_MARKET_CAP_M` ($300M, overridable via the `MIN_MARKET_CAP_M` env var) are dropped. Tickers with no market-cap data are kept, so a transient yfinance miss never silently discards a legitimate large-cap.

**Cross-signal detection** (`find_cross_signals` / `detect_cross_cluster_alerts` in `analyzer.py`) groups congressional purchases and insider buys by ticker and fires a 🔗 alert when both appear on the same ticker within `CLUSTER_DAYS` (45). The alert email lists the congressional buys and the insider buys in separate tables and reports the days between the first and last signal. Existing congressional detectors and email templates are untouched — the cross-signal path is purely additive and runs alongside them in `monitor.poll()`.

### Win-Rate Calculation
Uses yfinance to pull stock price on `transaction_date`, compares 30/60/90-day forward returns vs. SPY benchmark. A trade is a win if the member outperformed SPY. Minimum 10 scored trades required before a member qualifies for win-rate alerts.

### State management
`seen_trades.json` tracks every trade that has already triggered an alert using a `representative|ticker|date|type` key. On each poll, only truly new trades fire alerts without duplicate emails. Cross-signal alerts dedupe against the same file using a `crosscluster|ticker|<participants>` key, so an overlap re-fires only when a new buyer joins it.

---

## Key Design Decisions

**Build all three alert tiers in one pass** rather than shipping cluster-only first. Adding win-rate later would require refactoring the alert schema that notifier and monitor are already built against.

**PDF parsing over Selenium** for the House. The Clerk search is server-rendered HTML accessible via a plain POST. No headless browser needed.

**Senate HTML over PDF** for the Senate. The eFD viewer renders transactions directly in an HTML table, making PDF download unnecessary and parsing cleaner.

**Official government sources for committee data** rather than third-party APIs. Both the House Clerk XML and Senate.gov HTML are free, permanent, and require no authentication.

**Rate limiting by design**: polls every 4 hours (6 requests/day per source), 200 PDF cap per run, `seen_trades.json` prevents re-downloading already-processed filings.

---

## Tested Output (June 6, 2026)

```
Senate: 171 trades (last 180 days, 50 filings parsed)
House:  129 trades (last 180 days, 200 PDFs parsed)
Total:  300 trades · Runtime: ~2.5 minutes

Win-Rate Leaderboard:
  Markwayne Mullin     77%  (24/31 beat SPY over 60d) ⭐
  John Boozman         65%  (26/40 beat SPY over 60d) ⭐

Alert fired:
  🟢 Whitehouse — NVDA SALE_PARTIAL $100,001-$250,000 [Self, Spouse]
     ⚠ Commerce, Science, and Transportation (oversees Semiconductors — NVDA)
     ⚠ International Trade, Customs, and Global Competitiveness (oversees Semiconductors — NVDA)
     ⚠ Emerging Threats and Capabilities (oversees Semiconductors — NVDA)
```

---

## Configuration (config.py)

```python
CLUSTER_MIN_MEMBERS = 2        # members needed for cluster alert (lowered from 3)
CLUSTER_DAYS        = 45       # rolling window (extended from 30)
WIN_RATE_MIN        = 0.60     # 60% win rate threshold
WIN_RATE_MIN_TRADES = 10       # minimum scored trades
WIN_RATE_PRIMARY    = 60       # days forward vs SPY
POLL_INTERVAL_SECONDS = 14400  # 4 hours
FETCH_DAYS          = 45       # alert window
WATCHLIST           = [        # members whose any trade triggers an alert
    "Nancy Pelosi",
    "Josh Gottheimer",
    "Dan Crenshaw",
    "Tommy Tuberville",
    "Mark Warner",
    "Brian Mast",
]
SECTOR_TICKERS      = {...}    # sector → ticker mappings for conflict detection
COMMITTEE_SECTORS   = {...}    # committee keywords → sector mappings
```

**Watchlist rationale:** Pelosi (Paul's options trades historically correlated with legislation), Gottheimer (semiconductor trades near CHIPS Act votes, Financial Services committee), Crenshaw (defense/energy trades, Armed Services committee), Tuberville (defense/energy trades, Senate Armed Services, single-handedly held up military appointments), Warner (tech/finance background, Senate Intelligence + Finance committees), Mast (active defense sector trades, Armed Services committee).

---

## Email Setup (Gmail)

Credentials are stored in `.env`, NOT in source code.

1. Create a dedicated Gmail account for sending alerts
2. Enable 2FA on that account
3. Go to **myaccount.google.com/apppasswords** → create App Password
4. Copy `.env.example` to `.env` and fill in your values:
   ```
   ALERT_EMAIL_SENDER=your_alert_account@gmail.com
   ALERT_EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
   ALERT_EMAIL_RECIPIENTS=your_email@gmail.com
   GEMINI_API_KEY=your_gemini_api_key   # optional — get one free at aistudio.google.com
   # Optional: minimum market cap (in $M) for OpenInsider CEO/CFO buys (default 300)
   MIN_MARKET_CAP_M=300
   ```
   For GitHub Actions, also add `GEMINI_API_KEY` as a repository secret (Settings → Secrets and variables → Actions).

Test with: `python notifier.py`

**Why this approach:** `config.py` is safe to commit publicly. `.env` is gitignored and stays on your machine only. Anyone cloning the repo copies `.env.example` to `.env` and adds their own credentials.

---

## AI-Assisted Development Note

Built with AI pair-programming assistance. All architectural decisions, source evaluation, and executive calls made by Davin Kim. Key decisions documented throughout this README.