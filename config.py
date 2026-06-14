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
CLUSTER_MIN_MEMBERS = 2    # members needed to trigger
CLUSTER_DAYS        = 45   # rolling window in days

# 🟡 Win-Rate Alert
WIN_RATE_MIN        = 0.60  # 60% minimum win rate
WIN_RATE_MIN_TRADES = 10    # minimum scored trades before win rate is trusted
WIN_RATE_WINDOWS    = [30, 60, 90]
WIN_RATE_PRIMARY    = 60    # primary scoring window (days vs SPY)

# 🟢 Watchlist — members whose ANY trade triggers an alert
WATCHLIST = [
    "Nancy Pelosi",
    "Josh Gottheimer",
    "Dan Crenshaw",
    "Tommy Tuberville",
    "Mark Warner",
    "Brian Mast",
]

# ── Polling ───────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 14_400   # 4 hours
FETCH_DAYS            = 45       # alert window

# ── State File ────────────────────────────────────────────────────────────────

SEEN_TRADES_FILE = "seen_trades.json"

# ── Data Fetch Limits ─────────────────────────────────────────────────────────

SENATE_FILING_LIMIT = 50
HOUSE_PDF_LIMIT     = 200

# ── Cluster Detection ─────────────────────────────────────────────────────────

# Tickers too common to signal anything in cluster detection
# A cluster of members buying AAPL is probably coincidence, not coordination
CLUSTER_EXCLUDE_TICKERS = {
    "BRK.B", "BRK.A", "SPY", "QQQ", "VOO", "VTI", "IVV",
}

# ── Committee Conflict Detection ──────────────────────────────────────────────

# Maps sectors to the tickers that belong to them
SECTOR_TICKERS = {
    "Semiconductors":   ["NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM", "MU", "AMAT", "LRCX", "KLAC"],
    "Defense":          ["LMT", "RTX", "NOC", "GD", "BA", "HII", "L3H", "LDOS", "CACI", "SAIC"],
    "Healthcare":       ["UNH", "JNJ", "PFE", "ABBV", "CVS", "MCK", "CI", "HCA", "TMO", "ABT"],
    "Energy":           ["XOM", "CVX", "BP", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY"],
    "Finance":          ["JPM", "BAC", "GS", "MS", "WFC", "C", "AXP", "BLK", "SCHW", "COF"],
    "Tech":             ["AAPL", "MSFT", "GOOGL", "META", "AMZN", "CRM", "ORCL", "IBM", "CSCO", "ADBE"],
    "Telecom":          ["T", "VZ", "TMUS", "CMCSA", "CHTR", "DISH"],
    "Agriculture":      ["ADM", "BG", "MOS", "CF", "FMC", "DE", "CTVA"],
    "Transportation":   ["UNP", "CSX", "NSC", "UPS", "FDX", "DAL", "UAL", "LUV"],
    "Pharma":           ["LLY", "MRK", "BMY", "GILD", "BIIB", "AMGN", "REGN", "VRTX"],
}

# Maps committee/subcommittee keywords to the sectors they oversee
# Used for fuzzy matching against member's actual committee assignments
COMMITTEE_SECTORS = {
    "Semiconductors": [
        "International Trade",
        "Commerce, Science",
        "Science and Technology",
        "Strategic Competition",
        "Emerging Threats",
        "Manufacturing",
        "Export",
        "Technology",
    ],
    "Defense": [
        "Armed Services",
        "Strategic Forces",
        "Seapower",
        "Airland",
        "Emerging Threats and Capabilities",
        "Readiness",
        "Intelligence",
        "Cybersecurity",
    ],
    "Healthcare": [
        "Health",
        "Labor, Health",
        "Medicare",
        "Medicaid",
        "Aging",
        "Pharmaceutical",
    ],
    "Energy": [
        "Energy",
        "Environment",
        "Natural Resources",
        "Public Lands",
        "Nuclear",
        "Climate",
    ],
    "Finance": [
        "Banking",
        "Financial Services",
        "Finance",
        "Securities",
        "Insurance",
        "Investment",
        "Economic Policy",
    ],
    "Tech": [
        "Commerce, Science",
        "Science and Technology",
        "Technology",
        "Communications",
        "Consumer Protection",
        "Data Privacy",
        "Telecommunications",
    ],
    "Telecom": [
        "Commerce, Science",
        "Telecommunications",
        "Communications",
        "Technology",
    ],
    "Agriculture": [
        "Agriculture",
        "Nutrition",
        "Forestry",
        "Rural Development",
        "Commodities",
    ],
    "Transportation": [
        "Transportation",
        "Infrastructure",
        "Aviation",
        "Railroads",
        "Highways",
    ],
    "Pharma": [
        "Health",
        "Labor, Health",
        "Pharmaceutical",
        "Aging",
        "Medicare",
    ],
}

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