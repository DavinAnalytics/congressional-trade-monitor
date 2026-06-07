# Congressional Trade Monitor
**Author:** Davin Kim  
**Status:** ✅ Complete - all modules built and tested  
**Stack:** Python, Requests, BeautifulSoup, pdfplumber, yfinance, smtplib, python-dotenv  
**Purpose:** Personal-use automation tool that monitors congressional stock disclosures, detects high-signal trading patterns, and sends email alerts. GitHub and LinkedIn portfolio project.

---

## What It Does

Congress members are required by the STOCK Act (2012) to publicly disclose stock trades within 45 days. This tool automates monitoring of those disclosures across both chambers, detects meaningful patterns, and alerts via email when a signal fires.

**Core insight driving the design:** The top-performer leaderboard is non-sticky year to year; none of the top performers of 2024 showed up in top performers of 2025. Instead of chasing one politician (e.g. Pelosi), broad monitoring with cluster detection is the smarter decision.

---

## Alert Tiers

| Tier | Signal | Trigger |
|------|--------|---------|
| 🔴 Cluster Alert | 3+ members buy/sell same ticker within 14 days | Strongest signal |
| 🟡 Win-Rate Alert | Member with >60% historical win rate files new trade | Individual quality filter |
| 🟢 Watchlist Alert | Specific named politician files anything | Manual tracking |

---

## Quickstart

```bash
pip install -r requirements.txt

# Set up credentials
cp .env.example .env
# Edit .env with your Gmail sender, app password, and recipient

# Test one full cycle (fetch → analyze → alerts)
python monitor.py --once

# Send a daily digest email
python monitor.py --summary

# Run forever (polls every 4 hours)
python monitor.py
```

---

## File Structure

```
congressional-trade-monitor/
├── config.py                        # Watchlist, alert thresholds, email settings (safe to commit)
├── fetcher.py                       # House + Senate data fetchers
├── analyzer.py                      # Cluster detection + win-rate leaderboard
├── notifier.py                      # Email alert formatting and sending
├── monitor.py                       # Main polling loop
├── .env                             # Your credentials — gitignored, never committed
├── .gitignore                       # Blocks .env and seen_trades.json from git
├── requirements.txt                 # pip install -r requirements.txt
├── seen_trades.json                 # Auto-created state file — gitignored
├── .github/
│   └── workflows/
│       ├── monitor.yml  # Daily 6 AM PST — fetch, analyze, email alerts
│       └── summary.yml  # Sundays 9 PM PST — weekly digest email
└── README.md
```

---

## Automated Scheduling (GitHub Actions)

The monitor runs automatically on GitHub's servers, allowing it to run without the local computer.

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `monitor.yml` | Daily at 6:00 AM PST | Fetches both chambers, detects signals, emails alerts for new trades |
| `summary.yml` | Sundays at 9:00 PM PST | Sends a weekly digest of all alerts and trade activity |

**State persistence:** `seen_trades.json` is stored in a private GitHub Gist between runs so duplicate alerts are never sent. Each run loads the Gist at start and saves back on completion.

**Manual trigger:** Both workflows have a `workflow_dispatch` trigger. You can run either one on demand from the GitHub Actions tab at any time to test.

---

## Data Sources

All data comes directly from official U.S. government sources. No third-party APIs, no keys required, no paywalls.

| Chamber | Source | Method |
|---------|--------|--------|
| Senate | `efdsearch.senate.gov` | Session POST (CSRF + terms agreement) → HTML table parsing |
| House | `disclosures-clerk.house.gov` | POST filing index → pdfplumber PDF parsing |

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

### Win-Rate Calculation
Uses yfinance to pull stock price on `transaction_date`, compares 30/60/90-day forward returns vs. SPY benchmark. A trade is a win if the member outperformed SPY. Minimum 10 scored trades required before a member qualifies for win-rate alerts.

### State management
`seen_trades.json` tracks every trade that has already triggered an alert using a `representative|ticker|date|type` key. On each poll, only truly new trades fire alerts without duplicate emails.

---

## Key Design Decisions

**Build all three alert tiers in one pass** rather than shipping cluster-only first. Adding win-rate later would require refactoring the alert schema that notifier and monitor are already built against.

**PDF parsing over Selenium** for the House. The Clerk search is server-rendered HTML accessible via a plain POST — no headless browser needed.

**Senate HTML over PDF** for the Senate. The eFD viewer renders transactions directly in an HTML table, making PDF download unnecessary and parsing cleaner.

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
```

---

## Configuration (config.py)

```python
CLUSTER_MIN_MEMBERS = 3        # members needed for cluster alert
CLUSTER_DAYS        = 14       # rolling window
WIN_RATE_MIN        = 0.60     # 60% win rate threshold
WIN_RATE_MIN_TRADES = 10       # minimum scored trades
WIN_RATE_PRIMARY    = 60       # days forward vs SPY
POLL_INTERVAL_SECONDS = 14400  # 4 hours
FETCH_DAYS          = 30       # alert window
WATCHLIST           = [...]    # members to always track
```

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
   ```

Test with: `python notifier.py`

**Why this approach:** `config.py` is safe to commit publicly. `.env` is gitignored and stays on your machine only. Anyone cloning the repo copies `.env.example` to `.env` and adds their own credentials.

---

## Next Project

S&P 500 Streamlit dashboard using yfinance — doubles as freelance client demo. Win-rate calculation in this project builds directly toward that.

---

## AI-Assisted Development Note

Built with AI pair-programming assistance. All architectural decisions, source evaluation, and executive calls made by Davin Kim. Key decisions documented throughout this README.
