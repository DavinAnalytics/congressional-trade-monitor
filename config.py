"""
config.py — Congressional Trade Monitor
All settings live here. Sensitive values (email credentials) are loaded
from a .env file via python-dotenv — never hardcoded, never committed.

Setup:
  1. pip install python-dotenv
  2. Copy .env.example to .env and fill in your credentials
  3. .env is gitignored — your credentials stay local
"""

import os
from dotenv import load_dotenv

# Load .env file from the project root (same folder as this file)
load_dotenv()

# ── Email Settings ────────────────────────────────────────────────────────────
# These are read from .env — do not hardcode values here

EMAIL_SENDER     = os.getenv("ALERT_EMAIL_SENDER", "")
EMAIL_PASSWORD   = os.getenv("ALERT_EMAIL_PASSWORD", "")
EMAIL_RECIPIENTS = [r.strip() for r in os.getenv("ALERT_EMAIL_RECIPIENTS", "").split(",") if r.strip()]

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Alert Thresholds ──────────────────────────────────────────────────────────

# 🔴 Cluster Alert
CLUSTER_MIN_MEMBERS = 3    # members needed to trigger
CLUSTER_DAYS        = 14   # rolling window in days

# 🟡 Win-Rate Alert
WIN_RATE_MIN        = 0.60  # 60% minimum win rate
WIN_RATE_MIN_TRADES = 10    # minimum scored trades before win rate is trusted
WIN_RATE_WINDOWS    = [30, 60, 90]
WIN_RATE_PRIMARY    = 60    # primary scoring window (days vs SPY)

# 🟢 Watchlist — members whose ANY trade triggers an alert
WATCHLIST = [
    "Nancy Pelosi",
    "Sheldon Whitehouse",
    "David McCormick",
    "Josh Gottheimer",
    "Dan Crenshaw",
    "Marjorie Taylor Greene",
]

# ── Polling ───────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 14_400   # 4 hours
FETCH_DAYS            = 30       # alert window

# ── State File ────────────────────────────────────────────────────────────────

SEEN_TRADES_FILE = "seen_trades.json"

# ── Data Fetch Limits ─────────────────────────────────────────────────────────

SENATE_FILING_LIMIT = 50
HOUSE_PDF_LIMIT     = 200

# ── Validation ────────────────────────────────────────────────────────────────

def validate():
    """Call at startup to catch missing credentials early."""
    missing = []
    if not EMAIL_SENDER:
        missing.append("ALERT_EMAIL_SENDER")
    if not EMAIL_PASSWORD:
        missing.append("ALERT_EMAIL_PASSWORD")
    if not EMAIL_RECIPIENTS:
        missing.append("ALERT_EMAIL_RECIPIENTS")
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your credentials."
        )