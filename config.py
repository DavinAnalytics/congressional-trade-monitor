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
NEW_LISTING_DAYS    = 30   # a ticker younger than this cannot form a cluster

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

# Gemini alert blurbs persisted from the digest for the dashboard research layer.
AI_CONTEXT_FILE = "ai_context.json"

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

# Maps a market industry to the sector whose committees oversee it.
#
# Keyed on industry, not ticker, on purpose. A ticker list is unbounded and goes
# stale: the hand-maintained one this replaced covered 101 tickers against 631
# actually traded (12.8%), listed "L3H" — not a real ticker, L3Harris is LHX —
# still carried the delisted DISH, and held GOOGL but not GOOG, so the same
# company flagged a conflict under one share class and not the other. The
# industry taxonomy is bounded and stable, so this table stops rotting the day
# it is written. Industries with no real committee oversight are deliberately
# absent: no sector means no flag, which is the honest answer.
INDUSTRY_SECTORS = {
    "Semiconductors":                          "Semiconductors",
    "Semiconductor Equipment & Materials":     "Semiconductors",

    "Aerospace & Defense":                     "Defense",

    "Software - Infrastructure":               "Tech",
    "Software - Application":                  "Tech",
    "Information Technology Services":         "Tech",
    "Computer Hardware":                       "Tech",
    "Consumer Electronics":                    "Tech",
    "Electronic Components":                   "Tech",
    "Scientific & Technical Instruments":      "Tech",
    "Internet Content & Information":          "Tech",
    "Internet Retail":                         "Tech",
    "Electronic Gaming & Multimedia":          "Tech",

    "Telecom Services":                        "Telecom",
    "Communication Equipment":                 "Telecom",
    "Entertainment":                           "Telecom",
    "Advertising Agencies":                    "Telecom",
    "Publishing":                              "Telecom",

    "Healthcare Plans":                        "Healthcare",
    "Medical Devices":                         "Healthcare",
    "Medical Instruments & Supplies":          "Healthcare",
    "Medical Care Facilities":                 "Healthcare",
    "Medical Distribution":                    "Healthcare",
    "Diagnostics & Research":                  "Healthcare",
    "Health Information Services":             "Healthcare",

    "Drug Manufacturers - General":            "Pharma",
    "Drug Manufacturers - Specialty & Generic": "Pharma",
    "Biotechnology":                           "Pharma",

    "Oil & Gas Integrated":                    "Energy",
    "Oil & Gas E&P":                           "Energy",
    "Oil & Gas Midstream":                     "Energy",
    "Oil & Gas Refining & Marketing":          "Energy",
    "Oil & Gas Equipment & Services":          "Energy",
    "Uranium":                                 "Energy",
    "Thermal Coal":                            "Energy",
    "Solar":                                   "Energy",
    "Utilities - Regulated Electric":          "Energy",
    "Utilities - Regulated Gas":               "Energy",
    "Utilities - Regulated Water":             "Energy",
    "Utilities - Renewable":                   "Energy",
    "Utilities - Independent Power Producers": "Energy",

    "Banks - Diversified":                     "Finance",
    "Banks - Regional":                        "Finance",
    "Capital Markets":                         "Finance",
    "Financial Data & Stock Exchanges":        "Finance",
    "Asset Management":                        "Finance",
    "Credit Services":                         "Finance",
    "Financial Conglomerates":                 "Finance",
    "Insurance - Diversified":                 "Finance",
    "Insurance - Life":                        "Finance",
    "Insurance - Property & Casualty":         "Finance",
    "Insurance - Reinsurance":                 "Finance",
    "Insurance Brokers":                       "Finance",

    "Agricultural Inputs":                     "Agriculture",
    "Farm Products":                           "Agriculture",
    "Farm & Heavy Construction Machinery":     "Agriculture",
    "Food Distribution":                       "Agriculture",
    "Packaged Foods":                          "Agriculture",

    "Railroads":                               "Transportation",
    "Airlines":                                "Transportation",
    "Integrated Freight & Logistics":          "Transportation",
    "Marine Shipping":                         "Transportation",
    "Trucking":                                "Transportation",

    "Gold":                                    "Mining",
    "Silver":                                  "Mining",
    "Copper":                                  "Mining",
    "Aluminum":                                "Mining",
    "Steel":                                   "Mining",
    "Coking Coal":                             "Mining",
    "Other Industrial Metals & Mining":        "Mining",
    "Other Precious Metals & Mining":          "Mining",
}

# Tickers whose sector the industry taxonomy cannot supply. Funds carry no
# industry at all, and a few operating companies file under an industry that
# hides what they actually are — MSTR is "Software", COIN is "Capital Markets".
# Checked before INDUSTRY_SECTORS, so an entry here always wins. Deliberately
# short: this is the exception list, not the map.
SECTOR_TICKERS = {
    "Crypto":         ["IBIT", "FBTC", "GBTC", "ETHA", "ARKB", "BTCO",
                       "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT"],
    "Mining":         ["GLD", "IAU", "GLDM", "SGOL", "OUNZ", "PHYS",
                       "SLV", "SIVR", "PSLV", "PPLT", "PALL",
                       "GDX", "GDXJ", "SIL", "LITP"],
    "Energy":         ["USO", "USOU", "UNG", "XLE", "TPYP", "URA"],
    "Semiconductors": ["SMH", "SOXX"],
    "Tech":           ["IGV"],
}

# Ticker → industry lookups are cached here so a run costs one yfinance call
# per ticker never seen before, and nothing for one already known.
SECTOR_CACHE_FILE = "ticker_sectors.json"

# Phrases where a keyword means something else entirely. Removed from an
# assignment name before matching, so "Intelligence" reaches Defense Intelligence
# and the Open Source Intelligence subcommittee without also reaching every
# Artificial Intelligence panel. Same class of error as "Technology" matching
# inside "Biotechnology", which whole-word matching already handles.
AMBIGUOUS_PHRASES = [
    "Artificial Intelligence",
]

# Maps committee/subcommittee keywords to the sectors they oversee
# Used for fuzzy matching against member's actual committee assignments
COMMITTEE_SECTORS = {
    "Semiconductors": [
        "International Trade",
        "Commerce, Science",
        "Science, Space, and Technology",
        "Research and Technology",
        "Strategic Competition",
        "Emerging Threats",
        "Manufacturing",
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
        "Aging",
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
        "Science, Space, and Technology",
        "Technology",
        "Communications",
        "Data Privacy",
        "Telecommunications",
    ],
    "Telecom": [
        "Commerce, Science",
        "Telecommunications",
        "Communications",
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
        "Aviation",
        "Railroads",
        "Highways",
    ],
    "Pharma": [
        "Health",
        "Labor, Health",
        "Aging",
    ],
    "Mining": [
        "Natural Resources",
        "Interior",
        "Public Lands",
        "Mining",
    ],
    "Crypto": [
        "Banking",
        "Financial Services",
        "Digital Assets",
        "Commodity Markets",  # CFTC oversight of crypto derivatives
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