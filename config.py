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

# ── Alert Eligibility ─────────────────────────────────────────────────────────
# Which congressional trades are allowed to become signals at all. Both filters
# apply to alert detection only — win rates, the trade log and the control arm
# still see every trade, so history keeps scoring what these exclude and the
# monthly review can tell you whether excluding it was right.

# Congressional sales are dominated by tax-loss harvesting, scheduled
# liquidations and diversification, so they say little about a member's view of
# the company. Set True to alert on them again.
ALERT_ON_SALES = False

# A member filing this many distinct tickers on one date is rebalancing a
# portfolio, not expressing a view — the largest such filing observed is 294
# tickers on a single day. Set to 0 to disable the filter.
#
# 8 rather than a tighter number: same-day filings cluster at 1-2, 4-6, 8 and
# 31 tickers, so this keeps up to six same-day picks as plausibly deliberate
# while dropping the mechanical blocks. Tightening to 4 leaves so few eligible
# buys that the alert arm may never accumulate enough records to be judged.
REBALANCE_MIN_TICKERS = 8

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

# ── State Files ───────────────────────────────────────────────────────────────

SEEN_TRADES_FILE = "seen_trades.json"

# Fired-alert log, used to measure whether alerts actually beat SPY.
# Lives alongside SEEN_TRADES_FILE in the same Gist (a Gist holds many files).
HISTORY_FILE = "alert_history.json"

# Congressional trades that did NOT trigger an alert, scored identically.
# Without this baseline an alert hit rate is uninterpretable: if un-alerted
# trades perform just as well, the detectors add nothing and the absolute
# number is just the base rate of congressional trading.
CONTROL_FILE = "control_trades.json"

# Control trades sampled per run. The whole 45-day window is ~150 trades, far
# too many to store every run; a sample large enough to compare against the
# handful of alerts is enough.
CONTROL_SAMPLE_PER_RUN = 20

# ── Significance testing ──────────────────────────────────────────────────────

# Resamples used to put a confidence interval around an average edge. An average
# on its own cannot distinguish a real effect from luck; the interval can.
BOOTSTRAP_ITERATIONS = 5_000

# Below this many scored alerts, a bootstrap is theatre — report "too few" instead.
BOOTSTRAP_MIN_SAMPLES = 5

# Minimum matured alerts before the monthly review will recommend changing
# anything. Deliberately higher than the bootstrap floor: showing a wide interval
# is honest, but advising a config change off ten data points is not.
REC_MIN_SAMPLES = 20

# ── Display ───────────────────────────────────────────────────────────────────

# Shared by the digest cards and the monthly review, so a tier is named the same
# everywhere.
TIER_LABELS = {
    "cross_cluster": "CROSS-SIGNAL",
    "cluster":       "CLUSTER",
    "winrate":       "WIN-RATE",
    "watchlist":     "WATCHLIST",
    "control":       "CONTROL",
}

# ── State Retention ───────────────────────────────────────────────────────────
# Both state files live in a Gist, and the Gist API silently truncates file
# contents past ~1MB — which would corrupt state rather than fail loudly. Both
# are therefore capped.

# Drop seen-keys whose newest trade date is older than this. Alerts only ever
# fire on trades inside FETCH_DAYS, so anything older can never re-match; the
# margin over FETCH_DAYS absorbs late filings and amended disclosures.
SEEN_RETENTION_DAYS = 120

# Keep at most this many fired-alert records, newest first. ~8 alerts/day means
# this is roughly a year of history — far more than the 30–90 days needed to
# calibrate SCORE_WEIGHTS.
HISTORY_MAX_RECORDS = 3000

# ── Conviction Scoring ────────────────────────────────────────────────────────

# Max points each component contributes to an alert's 0–100 conviction score.
# Tier base is the floor for the alert type; the rest scale with the evidence.
SCORE_WEIGHTS = {
    "tier_base": {
        "cross_cluster": 30,   # Congress + insider agreement — strongest setup
        "cluster":       22,
        "winrate":       14,
        "watchlist":     10,
    },
    "dollars":      22,   # log-scaled: separates a $1K token buy from $500K
    "participants": 18,   # distinct members, plus insiders on cross-signals
    "freshness":    16,   # decays as disclosure lag approaches CLUSTER_DAYS
    "track_record": 10,   # best historical win rate among participants
    "seniority":     4,   # CEO/CFO outranks other officers (cross-signals only)
}

# Dollar totals at or above this earn full "dollars" points (log-scaled below it).
SCORE_DOLLAR_CAP = 1_000_000

# Participant count at or above this earns full "participants" points.
SCORE_PARTICIPANT_CAP = 5

# How many top-ranked alerts in a digest get an AI context blurb.
# Gemini's free tier is limited, so spend it on the highest-conviction signals.
DIGEST_AI_TOP_N = 3

# Output-token ceilings for the AI blurbs. The free tier is rationed by requests
# per day, not tokens per request, and one run makes at most DIGEST_AI_TOP_N + 1
# calls — so these can be generous. 220 was too tight and cut answers off
# mid-sentence once the prompt carried the full enriched context.
AI_ALERT_MAX_TOKENS   = 450
AI_WEEKLY_MAX_TOKENS  = 600

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
    "Crypto":           ["IBIT", "FBTC", "GBTC", "ETHA", "ARKB", "BTCO", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT"],
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
    "Crypto": [
        "Banking",
        "Financial Services",
        "Digital Assets",
        "Agriculture",  # CFTC oversight of crypto derivatives
        "Securities",
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