"""
openinsider_fetcher.py — Congressional Trade Monitor (insider extension)
Fetches and normalizes corporate insider trades from openinsider.com.

The screener URL is pre-filtered to:
  - open-market purchases only (xp=1)
  - CEO + CFO titles only (isofficer=1, isceo=1, iscfo=1)
  - last 45 days (fd=45)

Parses the results table (<table class="tinytable">) into the same flat-dict
shape used elsewhere in the project, with a "source": "insider" discriminator
so cross-cluster code can tell insider trades apart from congressional ones.

Public interface: fetch_all(days) -> list[dict]
"""

import os
import re
import time
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

import config

# ── Config ────────────────────────────────────────────────────────────────────

MIN_TRADE_VALUE  = 50_000                                     # drop sub-$50k buys (micro-cap noise)
MIN_MARKET_CAP_M = int(os.getenv("MIN_MARKET_CAP_M", "300"))  # $M floor; override via .env

# openinsider is a single unauthenticated endpoint and does time out in practice.
# A failed fetch silently disables cross-signal detection for the whole run, so
# it is worth a few retries before giving up.
FETCH_ATTEMPTS        = 3
FETCH_BACKOFF_SECONDS = 5
FETCH_TIMEOUT_SECONDS = 30

# openinsider serves at most 100 rows per page regardless of the rows= parameter,
# and the screener routinely matches more than that: page 2 comes back full of
# distinct filings. Reading only page 1 silently discarded the rest, which became
# actively harmful once directors were added to the filter — they compete for the
# same 100 slots and would otherwise push CEO/CFO buys out of view.
ROWS_PER_PAGE   = 100
FETCH_MAX_PAGES = 3


class InsiderFetchError(RuntimeError):
    """
    openinsider could not be reached after retries.

    Deliberately distinct from returning an empty list: "the feed is down" makes
    cross-signals impossible, while "no insiders bought anything" is a real
    market observation. Collapsing the two silently switches off the strongest
    signal in the monitor with no indication that anything went wrong.
    """

# Pre-filtered screener: open-market buys, insiders, last 45 days, 100 rows.
#
# isofficer=1 is broad — openinsider returns any Section 16 officer under it, so
# VPs already come through alongside CEOs and CFOs. isdirector=1 widens it further
# to board members, who the insider-trading literature finds informative (weaker
# than CEO/CFO, but well above noise). The binding constraint here is funnel width:
# only about one ticker per 45-day window overlaps with congressional buying at all.
# Seniority remains the discriminator — the conviction score credits CEO/CFO only —
# and history.py records it per alert so this widening can be judged on outcomes
# rather than argued about.
SCREENER_URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=45&fdr=&td=0&tdr="
    "&fdlyl=&fdlyh=&daysago=&xp=1&vl=&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999"
    "&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&ov=&ovh=&or=&orh=&btd=0&btdtdr="
    "&isofficer=1&iscob=0&isceo=1&iscoo=0&iscfo=1&ispres=0&isvp=0&istd=0&isdirector=1"
    "&istenpercent=0&lasthalf=0&lastmonth=0&last2months=0&last3months=0&last6months=0"
    "&lastyear=0&years=0&hdegrees=0&hsectors=0&tab=jqgrid&page=1&rows=100&sidx=&sord=asc"
)

OPENINSIDER_BASE = "https://openinsider.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_screener(html: str) -> list[dict]:
    """Parse the openinsider results table into raw insider trade dicts."""
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_="tinytable")
    if not table:
        return []

    # Header-driven column mapping (resilient to column-order changes).
    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    col = {h: i for i, h in enumerate(headers)}

    def idx(name: str, default: int) -> int:
        return col.get(name, default)

    trades = []
    body = table.find("tbody") or table
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 13:
            continue  # not a data row

        def text(name: str, default: int) -> str:
            return cells[idx(name, default)].get_text(strip=True)

        ticker = text("ticker", 3).upper()
        if not ticker:
            continue

        tx_date = _parse_date(text("trade date", 2))
        if tx_date is None:
            continue

        # NOTE: openinsider's screener table exposes no 10b5-1 flag — that status
        # lives only on each filing's SEC Form 4 detail page. The feed is already
        # restricted to open-market purchases (xp=1), where 10b5-1 plans are rare
        # (10b5-1 is overwhelmingly a sales mechanism), so no exclusion is applied.

        link_a = cells[idx("trade date", 2)].find("a") or cells[idx("ticker", 3)].find("a")
        ptr_link = (
            link_a["href"] if link_a and link_a.get("href", "").startswith("http")
            else f"{OPENINSIDER_BASE}/{ticker}"
        )

        value = _to_float(text("value", 12))
        trades.append({
            "source":            "insider",
            "name":              text("insider name", 5),
            "title":             text("title", 6),
            "company":           text("company name", 4),
            "ticker":            ticker,
            "type":              "purchase",  # screener filtered to open-market buys
            "transaction_date":  tx_date.strftime("%Y-%m-%d"),
            "disclosure_date":   text("filing date", 1)[:10],
            "shares":            _to_int(text("qty", 9)),
            "price":             _to_float(text("price", 8)),
            "value":             value,
            "amount":            text("value", 12),  # raw string — notifier._fmt_amount reuse
            "ptr_link":          ptr_link,
        })

    return trades


# ── Fetching ──────────────────────────────────────────────────────────────────

def _get_screener_html(page: int = 1) -> str:
    """
    GET one screener page, retrying with linear backoff.
    Raises InsiderFetchError if every attempt fails.
    """
    url = SCREENER_URL.replace("page=1", f"page={page}")

    last_error = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_error = e
            if attempt < FETCH_ATTEMPTS:
                wait = FETCH_BACKOFF_SECONDS * attempt
                print(f"  ⚠ openinsider page {page} attempt {attempt}/{FETCH_ATTEMPTS} "
                      f"failed ({e}) — retrying in {wait}s")
                time.sleep(wait)

    raise InsiderFetchError(
        f"openinsider unreachable after {FETCH_ATTEMPTS} attempts: {last_error}"
    )


def _fetch_screener_rows() -> list[dict]:
    """
    Read screener pages until one comes back short or the page cap is reached.

    Only the first page is required — if a later page fails after its retries,
    keep what was already collected rather than losing the whole run to a
    partial outage.
    """
    rows: list[dict] = []
    seen: set[tuple] = set()

    for page in range(1, FETCH_MAX_PAGES + 1):
        try:
            page_rows = _parse_screener(_get_screener_html(page))
        except InsiderFetchError:
            if page == 1:
                raise
            print(f"  ⚠ openinsider page {page} unavailable — continuing with {len(rows)} row(s)")
            break

        for r in page_rows:
            key = (r["name"], r["ticker"], r["transaction_date"], r["amount"])
            if key not in seen:
                seen.add(key)
                rows.append(r)

        if len(page_rows) < ROWS_PER_PAGE:
            break

    return rows


# ── Unified entry point ───────────────────────────────────────────────────────

def fetch_all(days: int = config.FETCH_DAYS) -> list[dict]:
    """
    Fetch insider open-market buys from openinsider.com.
    Returns normalized insider trade dicts sorted by transaction_date desc.

    Raises InsiderFetchError if the feed is unreachable — callers must not treat
    that as "no insider buys", since it makes cross-signals impossible.
    """
    print("Fetching insider data from openinsider.com...")
    trades = _fetch_screener_rows()
    print(f"  {len(trades)} filing(s) read from screener")

    # Defensive window filter (the URL already constrains to fd=45).
    cutoff = datetime.now() - timedelta(days=days)
    trades = [
        t for t in trades
        if _parse_date(t["transaction_date"]) and _parse_date(t["transaction_date"]) >= cutoff
    ]

    # Value threshold — drop micro-cap noise before the costlier market-cap pass.
    before = len(trades)
    trades = [t for t in trades if t["value"] >= MIN_TRADE_VALUE]
    print(f"  Filtered {before - len(trades)} rows below ${MIN_TRADE_VALUE // 1000}k threshold")

    # Market-cap filter (openinsider's screener has no market-cap param, so look it
    # up via yfinance). Memoize per unique ticker; keep buys whose cap is unknown.
    min_cap = MIN_MARKET_CAP_M * 1_000_000
    caps: dict[str, float | None] = {}
    kept = []
    for t in trades:
        ticker = t["ticker"]
        if ticker not in caps:
            caps[ticker] = _fetch_market_cap(ticker)
        cap = caps[ticker]
        if cap is None or cap >= min_cap:
            kept.append(t)
    trades = kept

    trades.sort(key=lambda t: t["transaction_date"], reverse=True)

    print(f"  ✓ {len(trades)} buys passed filters "
          f"(value >= ${MIN_TRADE_VALUE // 1000}k, market cap >= ${MIN_MARKET_CAP_M}M)")
    return trades


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _to_float(raw: str) -> float:
    """Strip $ , + % and parse to float. Returns 0.0 on failure."""
    cleaned = re.sub(r"[$,+%\s]", "", raw or "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _to_int(raw: str) -> int:
    return int(_to_float(raw))


def _fetch_market_cap(ticker: str) -> float | None:
    """Market cap in dollars via yfinance, or None if unavailable."""
    import logging, contextlib, io as _io
    with contextlib.redirect_stderr(_io.StringIO()):
        try:
            logging.disable(logging.CRITICAL)
            cap = yf.Ticker(ticker).info.get("marketCap")
            logging.disable(logging.NOTSET)
        except Exception:
            return None
    return float(cap) if cap else None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    trades = fetch_all()

    print(f"\n{'═'*60}")
    print(f"  Sample insider buys (first 5)")
    print(f"{'═'*60}")
    for t in trades[:5]:
        print(f"\n  {t['name']} ({t['title']})")
        print(f"    Ticker:  {t['ticker']} — {t['company']}")
        print(f"    Date:    {t['transaction_date']}")
        print(f"    Shares:  {t['shares']:,} @ ${t['price']}")
        print(f"    Value:   {t['amount']}")
        print(f"    Link:    {t['ptr_link']}")

    print(f"\n✓ openinsider_fetcher.py complete.\n")


if __name__ == "__main__":
    main()
