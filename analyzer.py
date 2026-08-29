"""
analyzer.py — Congressional Trade Monitor
Takes the unified trade list from fetcher.fetch_all() and returns alerts.

Three alert tiers:
  🔴 ClusterAlert  — 3+ members same ticker/direction within 14 days
  🟡 WinRateAlert  — high win-rate filer (>60%, min 10 scored trades) files new trade
  🟢 WatchlistAlert — any trade from a watchlist member

Win-rate scoring uses yfinance to pull historical prices and compare
each member's past trades against SPY over 30/60/90-day windows.

Public interface:
  analyze(trades) -> list[Alert]
"""

import json
import math
import os
import re
import requests
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict

import yfinance as yf

import config

# ── Alert data structures ─────────────────────────────────────────────────────

@dataclass
class Alert:
    tier:        str          # "cluster" | "winrate" | "watchlist" | "cross_cluster"
    ticker:      str
    trades:      list[dict]   # the trades that triggered this alert
    message:     str          # human-readable summary
    fired_at:    str = field(default_factory=lambda: datetime.now().isoformat())
    score:       float = 0.0  # 0–100 conviction, set by enrich_and_score()
    meta:        dict = field(default_factory=dict)  # enrichment: dollars, lag, price move


# ── Trade field helpers ───────────────────────────────────────────────────────

def parse_amount_value(s: str) -> float:
    """
    Numeric midpoint of a disclosure amount range, in dollars.
    Disclosures report brackets ("$1,001 - $15,000"), not exact figures, so the
    midpoint is the best available point estimate. Returns 0.0 when unparseable.
    """
    if not s or s.strip().lower().startswith("none"):
        return 0.0
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+", s)]
    if not nums:
        return 0.0
    return (nums[0] + nums[1]) / 2 if len(nums) >= 2 else nums[0]


def disclosure_lag_days(trade: dict) -> int | None:
    """
    Days between when a trade was executed and when it was disclosed.
    Congress may file up to 45 days late, so a small lag means the signal is
    still fresh while a large one means the move is likely already priced in.
    Returns None when the disclosure date is missing or unparseable.
    """
    disclosed = trade.get("disclosure_date", "")
    if not disclosed:
        return None
    try:
        tx   = datetime.strptime(trade["transaction_date"], "%Y-%m-%d")
        disc = datetime.strptime(disclosed[:10], "%Y-%m-%d")
    except (ValueError, KeyError):
        return None
    return max(0, (disc - tx).days)


# ── Win-rate scoring ──────────────────────────────────────────────────────────

# Win-rate scoring, price-move enrichment and alert-history all look up the same
# (ticker, date) pairs — SPY especially, once per scored trade. One cache across
# them turns the dominant cost of a run into a handful of downloads.
_PRICE_CACHE: dict[tuple[str, str, str], float | None] = {}

# run_forever() keeps one process alive indefinitely, and each poll adds entries
# for dates it has never seen, so the cache needs a ceiling. Evicting the oldest
# tenth on overflow keeps the hot working set (the current alert window) intact.
_PRICE_CACHE_MAX = 20_000


def _cache_price(key: tuple[str, str, str], price: float | None) -> float | None:
    if len(_PRICE_CACHE) >= _PRICE_CACHE_MAX:
        for stale in list(_PRICE_CACHE)[: _PRICE_CACHE_MAX // 10]:
            del _PRICE_CACHE[stale]
    _PRICE_CACHE[key] = price
    return price


def _download_closes(ticker: str, start: datetime, end: datetime):
    """Download a close-price series, suppressing yfinance's download noise."""
    import logging, contextlib, io as _io
    with contextlib.redirect_stderr(_io.StringIO()):
        try:
            logging.disable(logging.CRITICAL)
            df = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
            )
            logging.disable(logging.NOTSET)
        except Exception:
            return None
    if df.empty:
        return None
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close if not close.empty else None


def _get_price(ticker: str, date: datetime) -> float | None:
    """
    Get closing price for a ticker on or near a given date using yfinance.
    Fetches a 10-day window and takes the first close in it, so a date landing
    on a weekend or holiday resolves to the next trading day.
    Memoized for the life of the process.
    """
    cache_key = (ticker.upper(), date.strftime("%Y-%m-%d"), "first")
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]

    closes = _download_closes(ticker, date, date + timedelta(days=10))
    return _cache_price(cache_key, float(closes.iloc[0]) if closes is not None else None)


def latest_price(ticker: str) -> float | None:
    """
    Most recent close for a ticker — the last close in a trailing 10-day window,
    so a long weekend or holiday still resolves.
    """
    today = datetime.now()
    cache_key = (ticker.upper(), today.strftime("%Y-%m-%d"), "last")
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]

    closes = _download_closes(ticker, today - timedelta(days=10), today + timedelta(days=1))
    return _cache_price(cache_key, float(closes.iloc[-1]) if closes is not None else None)


def _is_new_listing(ticker: str, as_of: datetime) -> bool:
    """
    True when `ticker` had less than NEW_LISTING_DAYS of price history as of
    `as_of` — i.e. it had only just started trading.

    Unknown prices read as False. A yfinance outage must never silently
    suppress alerts; the guard only fires on positive evidence of a new listing.
    """
    lookback = config.NEW_LISTING_DAYS * 3
    closes = _download_closes(
        ticker, as_of - timedelta(days=lookback), as_of + timedelta(days=10)
    )
    if closes is None:
        return False
    first = closes.index[0]
    return (as_of.date() - first.date()).days < config.NEW_LISTING_DAYS


def _score_trade(trade: dict, window_days: int) -> bool | None:
    """
    Score a single trade as win (True), loss (False), or None (unscoreable).
    Win = member's return in `window_days` beats SPY return over same period.
    Only scores purchases (short selling is rare and signal is inverted).
    """
    if trade["type"] not in ("purchase",):
        return None  # only score purchases for now
    if trade.get("asset_type") == "option":
        return None  # options don't map to a simple stock-vs-SPY return

    try:
        tx_date  = datetime.strptime(trade["transaction_date"], "%Y-%m-%d")
        end_date = tx_date + timedelta(days=window_days)
    except ValueError:
        return None

    # Don't score trades where the forward window hasn't elapsed yet
    if end_date > datetime.now() - timedelta(days=1):
        return None

    ticker = trade["ticker"]

    # Get entry and exit prices for the trade ticker
    price_entry = _get_price(ticker, tx_date)
    price_exit  = _get_price(ticker, end_date)
    if price_entry is None or price_exit is None or price_entry == 0:
        return None

    # Get SPY prices over same window as benchmark
    spy_entry = _get_price("SPY", tx_date)
    spy_exit  = _get_price("SPY", end_date)
    if spy_entry is None or spy_exit is None or spy_entry == 0:
        return None

    member_return = (price_exit - price_entry) / price_entry
    spy_return    = (spy_exit  - spy_entry)    / spy_entry

    return member_return > spy_return


def compute_win_rates(trades: list[dict]) -> dict[str, dict]:
    """
    Compute win rate for every member in the trade list.
    Returns dict keyed by representative name:
      {
        "wins": int,
        "total": int,
        "win_rate": float,
        "qualifies": bool,   # meets WIN_RATE_MIN and WIN_RATE_MIN_TRADES
      }
    """
    print("  Computing win rates (this may take a minute — yfinance lookups)...")

    # Rebalancing filings are excluded here too. A win rate built from them
    # measures a diversified portfolio against SPY, which converges on a coin
    # flip by construction — and because win_rates feeds the conviction score's
    # track-record component, that noise would leak into how alerts are ranked.
    # Direction is not filtered here: _score_trade already scores purchases only,
    # and that must hold regardless of config.ALERT_ON_SALES.
    scoreable = drop_rebalancing(trades)

    # Group trades by member
    by_member = defaultdict(list)
    for t in scoreable:
        by_member[t["representative"]].append(t)

    stats = {}
    window = config.WIN_RATE_PRIMARY

    for member, member_trades in by_member.items():
        wins  = 0
        total = 0
        for t in member_trades:
            result = _score_trade(t, window)
            if result is None:
                continue
            total += 1
            if result:
                wins += 1

        win_rate  = wins / total if total > 0 else 0.0
        qualifies = (
            total    >= config.WIN_RATE_MIN_TRADES and
            win_rate >= config.WIN_RATE_MIN
        )
        stats[member] = {
            "wins":      wins,
            "total":     total,
            "win_rate":  win_rate,
            "qualifies": qualifies,
        }

    return stats


# ── Alert detectors ───────────────────────────────────────────────────────────

def drop_rebalancing(trades: list[dict]) -> list[dict]:
    """
    Remove trades filed on a member-day that looks like a portfolio move.

    A member filing many distinct tickers on one date is moving a portfolio, not
    making a call on any of them; that pattern covers roughly three-quarters of
    the trade log, and it is not confined to sales.

    Both directions count toward the day's ticker total, whatever the caller
    intends to keep: a member who sells ten tickers and buys three on one date is
    rebalancing, and counting only the buys would let those three through.
    """
    if not config.REBALANCE_MIN_TICKERS:
        return list(trades)

    per_day: dict[tuple, set] = defaultdict(set)
    for t in trades:
        per_day[(t["representative"], t["transaction_date"])].add(t["ticker"])

    return [
        t for t in trades
        if len(per_day[(t["representative"], t["transaction_date"])])
        < config.REBALANCE_MIN_TICKERS
    ]


def alertable_trades(trades: list[dict]) -> list[dict]:
    """
    The subset of congressional trades allowed to become signals.

    Rebalancing goes first, then sales — the latter because selling is dominated
    by tax-loss harvesting, scheduled liquidations and diversification, so a
    member selling says far less about their view of a company than one buying.

    Excluded trades are not discarded. They never enter the seen-state, so
    history.record_control samples them into the control arm and they keep being
    scored — which is what makes this decision reversible on evidence.
    """
    out = drop_rebalancing(trades)
    if not config.ALERT_ON_SALES:
        out = [t for t in out if t["type"] == "purchase"]
    return out


def detect_cluster_alerts(trades: list[dict]) -> list[Alert]:
    """
    🔴 Cluster Alert
    Find tickers where CLUSTER_MIN_MEMBERS+ distinct members traded in the same direction
    within CLUSTER_DAYS days of each other.
    """
    alerts = []

    # Group by (ticker, direction)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        direction = "buy" if t["type"] == "purchase" else "sell"
        groups[(t["ticker"], direction)].append(t)

    for (ticker, direction), group in groups.items():
        if ticker in config.CLUSTER_EXCLUDE_TICKERS:
            continue
        if len(group) < config.CLUSTER_MIN_MEMBERS:
            continue

        # Check if any subset of CLUSTER_MIN_MEMBERS+ trades falls within the window
        group.sort(key=lambda t: t["transaction_date"])
        dates = [
            datetime.strptime(t["transaction_date"], "%Y-%m-%d")
            for t in group
        ]

        # Sliding window: find clusters within CLUSTER_DAYS
        for i in range(len(dates)):
            window_trades = [
                group[j] for j in range(i, len(group))
                if (dates[j] - dates[i]).days <= config.CLUSTER_DAYS
            ]
            members_in_window = {t["representative"] for t in window_trades}

            if len(members_in_window) >= config.CLUSTER_MIN_MEMBERS:
                # A ticker that only began trading inside the window explains the
                # convergence by itself: a new listing is a common external event
                # every member reacts to at once, not evidence that they know the
                # same thing. Suppressed rather than scored — the trades stay out
                # of every alert, so record_control sweeps them into the control
                # arm and keeps measuring them.
                if _is_new_listing(ticker, dates[i]):
                    break

                action   = "buying" if direction == "buy" else "selling"
                names    = ", ".join(sorted(members_in_window))
                earliest = window_trades[0]["transaction_date"]
                latest   = window_trades[-1]["transaction_date"]

                alerts.append(Alert(
                    tier    = "cluster",
                    ticker  = ticker,
                    trades  = window_trades,
                    message = (
                        f"⚡ CLUSTER: {len(members_in_window)} members {action} "
                        f"{ticker} between {earliest} and {latest}\n"
                        f"Members: {names}"
                    ),
                ))
                break  # one alert per (ticker, direction) pair

    return alerts


def detect_winrate_alerts(
    new_trades: list[dict],
    win_rates:  dict[str, dict],
) -> list[Alert]:
    """
    🟡 Win-Rate Alert
    Flag new trades from members who qualify as high win-rate filers.
    """
    alerts = []
    seen_members = set()

    for trade in new_trades:
        member = trade["representative"]
        if member in seen_members:
            continue

        stats = win_rates.get(member, {})
        if not stats.get("qualifies", False):
            continue

        seen_members.add(member)
        wr   = stats["win_rate"]
        wins = stats["wins"]
        tot  = stats["total"]

        alerts.append(Alert(
            tier    = "winrate",
            ticker  = trade["ticker"],
            trades  = [trade],
            message = (
                f"🏆 WIN-RATE: {member} filed a new trade ({trade['ticker']} "
                f"{trade['type'].upper()})\n"
                f"Historical win rate: {wr:.0%} ({wins}/{tot} trades beat SPY "
                f"over {config.WIN_RATE_PRIMARY}d)"
            ),
        ))

    return alerts


def _canonical_name(name: str) -> str:
    """
    Reduce a member name to a lowercase "first last" form for matching.
    Handles both formats in play:
      "Last, First [Middle]" — used by the trade disclosure fetcher
      "First [Middle] Last"  — used by config.WATCHLIST
    Mirrors the name handling in committees.get_member_committees().
    """
    key = name.lower().strip()
    if "," in key:
        last, _, first = key.partition(",")
        last  = last.strip()
        first = first.strip().split()[0] if first.strip() else ""  # drop middle initial
        return f"{first} {last}".strip()
    parts = key.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1]}"
    return key


def detect_watchlist_alerts(trades: list[dict]) -> list[Alert]:
    """
    🟢 Watchlist Alert
    Flag any trade from a member on the config watchlist.
    Deduplicates by (member, ticker, date) — spouse + self trades on the
    same ticker/date count as one alert, with all owners grouped together.
    """
    watchlist_canon = {_canonical_name(w) for w in config.WATCHLIST}

    # Group by (member, ticker, date) to collapse spouse/self rows
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for trade in trades:
        member = trade["representative"]
        if _canonical_name(member) in watchlist_canon:
            key = (member, trade["ticker"], trade["transaction_date"])
            groups[key].append(trade)

    alerts = []
    for (member, ticker, date), group in groups.items():
        owners    = sorted({t.get("owner", "") for t in group if t.get("owner")})
        owner_str = f" [{', '.join(owners)}]" if owners else ""
        tx_type   = group[0]["type"]
        amount    = group[0]["amount"]

        alerts.append(Alert(
            tier    = "watchlist",
            ticker  = ticker,
            trades  = group,
            message = (
                f"👁️ WATCHLIST: {member} — "
                f"{ticker} {tx_type.upper()} "
                f"on {date} "
                f"({amount}){owner_str}"
            ),
        ))

    return alerts


# ── Cross-cluster detection (congressional + insider overlap) ────────────────

def find_cross_signals(
    congress_trades: list[dict],
    insider_trades:  list[dict],
    window_days:     int = config.CLUSTER_DAYS,
) -> list[dict]:
    """
    Find tickers bought by BOTH a member of Congress and a company insider
    (any Section 16 officer) within `window_days` of each other.

    Proximity is pairwise: a trade qualifies if it is within `window_days` of at
    least one trade on the *other* side. Measuring the span across every trade on
    the ticker instead would let one unrelated older buy push a genuinely tight
    Congress/insider pairing outside the window and discard the whole signal.

    Only congressional purchases count on the congressional side; insider trades
    are all open-market buys by construction (openinsider_fetcher).

    Returns one match dict per qualifying ticker, carrying only the trades that
    are actually part of the overlap:
      {ticker, congress: [...], insider: [...], first: datetime, last: datetime, span_days}
    Pure function — no I/O — so the dashboard can reuse it.
    """
    def by_ticker(trades: list[dict], purchases_only: bool) -> dict[str, list[tuple[dict, datetime]]]:
        grouped: dict[str, list[tuple[dict, datetime]]] = defaultdict(list)
        for t in trades:
            if purchases_only and t["type"] != "purchase":
                continue
            grouped[t["ticker"].upper()].append(
                (t, datetime.strptime(t["transaction_date"], "%Y-%m-%d"))
            )
        return grouped

    cong_by_ticker = by_ticker(congress_trades, purchases_only=True)
    ins_by_ticker  = by_ticker(insider_trades,  purchases_only=False)

    matches = []
    for ticker in set(cong_by_ticker) & set(ins_by_ticker):
        congress = cong_by_ticker[ticker]
        insider  = ins_by_ticker[ticker]

        def near(date: datetime, others: list[tuple[dict, datetime]]) -> bool:
            return any(abs((date - other).days) <= window_days for _, other in others)

        cong_hit = [(t, d) for t, d in congress if near(d, insider)]
        ins_hit  = [(t, d) for t, d in insider  if near(d, congress)]

        # The relation is symmetric, so either side being non-empty implies both.
        if not cong_hit:
            continue

        dates = [d for _, d in cong_hit + ins_hit]
        first, last = min(dates), max(dates)
        matches.append({
            "ticker":    ticker,
            "congress":  [t for t, _ in cong_hit],
            "insider":   [t for t, _ in ins_hit],
            "first":     first,
            "last":      last,
            "span_days": (last - first).days,
        })

    return matches


def detect_cross_cluster_alerts(
    congress_trades: list[dict],
    insider_trades:  list[dict],
) -> list[Alert]:
    """
    🔗 Cross-Cluster Alert
    Fire when 1+ congressional buy AND 1+ insider open-market buy hit the same
    ticker within the cluster window. Stronger conviction signal than either alone.
    """
    alerts = []
    for m in find_cross_signals(congress_trades, insider_trades):
        # Tag congressional trades with a source so the email formatter can split
        # the two groups; insider trades already carry source="insider".
        tagged = [{**t, "source": "congress"} for t in m["congress"]] + m["insider"]
        members  = sorted({t["representative"] for t in m["congress"]})
        insiders = sorted({t["name"] for t in m["insider"]})

        alerts.append(Alert(
            tier    = "cross_cluster",
            ticker  = m["ticker"],
            trades  = tagged,
            message = (
                f"🔗 CROSS-SIGNAL: {m['ticker']} — {len(m['congress'])} congressional "
                f"buy(s) + {len(m['insider'])} insider buy(s), {m['span_days']} days apart\n"
                f"Congress: {', '.join(members)} | Insiders: {', '.join(insiders)}"
            ),
        ))

    return alerts


# ── Conviction scoring ────────────────────────────────────────────────────────

def _price_move(ticker: str, since: datetime) -> tuple[float | None, float | None]:
    """
    Percent move in `ticker` and in SPY from `since` to the latest close.
    Answers "have I already missed it?" — the disclosure lag means the market
    has often had weeks to react before an alert can fire.
    """
    entry = _get_price(ticker, since)
    now   = latest_price(ticker)
    spy_entry = _get_price("SPY", since)
    spy_now   = latest_price("SPY")

    pct     = (now - entry) / entry * 100 if entry and now else None
    spy_pct = (spy_now - spy_entry) / spy_entry * 100 if spy_entry and spy_now else None
    return pct, spy_pct


def enrich_and_score(alert: Alert, win_rates: dict[str, dict] | None = None) -> None:
    """
    Populate alert.meta with the facts a trader needs to triage, and alert.score
    with a 0–100 conviction number. Mutates in place.

    Scoring is deliberately transparent (weighted components in config, no
    fitted model) because there is no outcome data to fit against yet —
    history.py accumulates it so these weights can be checked later.
    """
    win_rates = win_rates or {}
    w = config.SCORE_WEIGHTS

    insider  = [t for t in alert.trades if t.get("source") == "insider"]
    congress = [t for t in alert.trades if t.get("source") != "insider"]

    members = sorted({t["representative"] for t in congress if t.get("representative")})

    # Congressional disclosures are brackets ("$1,001 - $15,000") reduced to a
    # midpoint; insider values are exact transaction amounts. Summing the two into
    # one figure lets a large insider buy masquerade as congressional conviction,
    # so they are tracked apart and only totalled for display.
    congress_dollars = sum(parse_amount_value(t.get("amount", "")) for t in congress)
    insider_dollars  = sum(parse_amount_value(t.get("amount", "")) for t in insider)
    dollar_total     = congress_dollars + insider_dollars

    # Direction of the congressional side. A sell cluster predicts underperformance,
    # so downstream performance scoring must not treat it like a buy. Cross-signals
    # have no congressional side other than purchases, hence the "buy" default.
    direction = "sell" if congress and congress[0]["type"] != "purchase" else "buy"

    # Congressional lags only — insiders file Form 4 within two business days,
    # so mixing the two would wash out the staleness signal.
    lags = [d for d in (disclosure_lag_days(t) for t in congress) if d is not None]
    median_lag = int(statistics.median(lags)) if lags else None

    dates = sorted(t["transaction_date"] for t in alert.trades)
    first_date = dates[0] if dates else None

    pct = spy_pct = None
    if first_date:
        try:
            pct, spy_pct = _price_move(alert.ticker, datetime.strptime(first_date, "%Y-%m-%d"))
        except ValueError:
            pass

    best_win_rate = 0.0
    for m in members:
        s = win_rates.get(m, {})
        if s.get("total", 0) >= config.WIN_RATE_MIN_TRADES:
            best_win_rate = max(best_win_rate, s.get("win_rate", 0.0))

    has_top_insider = any(
        re.search(r"\b(CEO|CFO|Chief Executive|Chief Financial)\b", t.get("title", ""), re.I)
        for t in insider
    )

    alert.meta = {
        "n_members":          len(members),
        "n_insiders":         len(insider),
        "members":            members,
        "direction":          direction,
        "congress_dollars":   congress_dollars,
        "insider_dollars":    insider_dollars,
        "dollar_total":       dollar_total,
        "median_lag_days":    median_lag,
        "first_date":         first_date,
        "last_date":          dates[-1] if dates else None,
        "pct_since_trade":    pct,
        "spy_since_trade":    spy_pct,
        "excess_since_trade": (pct - spy_pct) if pct is not None and spy_pct is not None else None,
        "best_win_rate":      best_win_rate,
        "has_top_insider":    has_top_insider,
    }

    # ── Components, each scaled to 0–1 then weighted ──
    # Sized on the congressional leg alone: this component measures congressional
    # conviction, and a large insider buy already earns credit through the
    # participant and seniority components.
    floor = 3.0  # log10($1,000) — below this a trade is a rounding error
    cap   = math.log10(config.SCORE_DOLLAR_CAP)
    if congress_dollars > 0:
        dollar_frac = (math.log10(max(congress_dollars, 1)) - floor) / (cap - floor)
    else:
        dollar_frac = 0.0
    dollar_frac = min(1.0, max(0.0, dollar_frac))

    participants = len(members) + len(insider)
    part_frac = min(1.0, participants / config.SCORE_PARTICIPANT_CAP)

    # No disclosure date is missing data, not evidence of staleness — score it
    # neutral so unparsed filings aren't silently buried.
    fresh_frac = (
        1.0 - min(median_lag, config.CLUSTER_DAYS) / config.CLUSTER_DAYS
        if median_lag is not None else 0.5
    )

    score = (
        w["tier_base"].get(alert.tier, 10)
        + w["dollars"]      * dollar_frac
        + w["participants"] * part_frac
        + w["freshness"]    * fresh_frac
        + w["track_record"] * best_win_rate
        + (w["seniority"] if has_top_insider else 0)
    )
    alert.score = round(min(100.0, max(0.0, score)), 1)


# ── Seen-trades deduplication (Gist-backed for GitHub Actions) ───────────────

def _gist_enabled() -> bool:
    """True if GIST_TOKEN and GIST_ID are set in environment."""
    return bool(os.getenv("GIST_TOKEN") and os.getenv("GIST_ID"))


def _gist_headers() -> dict:
    return {
        "Authorization": f"token {os.getenv('GIST_TOKEN')}",
        "Accept": "application/vnd.github.v3+json",
    }


def state_read(filename: str, default):
    """
    Read a JSON state file from the GitHub Gist when credentials are present,
    otherwise from a local file of the same name. Returns `default` if absent
    or unreadable. A single Gist holds every state file this project keeps.
    """
    if _gist_enabled():
        try:
            resp = requests.get(
                f"https://api.github.com/gists/{os.getenv('GIST_ID')}",
                headers=_gist_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            files = resp.json()["files"]
            if filename not in files:
                return default
            return json.loads(files[filename]["content"])
        except Exception as e:
            print(f"  ⚠ Could not load Gist state ({filename}): {e} — starting fresh")
            return default

    if not os.path.exists(filename):
        return default
    try:
        with open(filename) as f:
            return json.load(f)
    except Exception:
        return default


def state_write(filename: str, data) -> None:
    """Persist a JSON state file to the Gist when available, else a local file."""
    content = json.dumps(data, indent=2)

    if _gist_enabled():
        try:
            requests.patch(
                f"https://api.github.com/gists/{os.getenv('GIST_ID')}",
                headers=_gist_headers(),
                json={"files": {filename: {"content": content}}},
                timeout=15,
            )
            print(f"  ✓ State saved to Gist ({filename})")
        except Exception as e:
            print(f"  ⚠ Could not save Gist state ({filename}): {e}")
        return

    with open(filename, "w") as f:
        f.write(content)


# Every seen-key embeds the transaction date(s) it was built from, in ISO form:
#   trade        → "Larsen, Rick|APH|2026-07-08|purchase"
#   cluster      → "cluster|APH|buy|<trade key>|<trade key>|..."
#   crosscluster → "crosscluster|APH|<participant key>|..."
# so the newest date in a key dates the key itself, whatever its shape.
_KEY_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── Ticker → sector (industry taxonomy, cached) ───────────────────────────────

_SECTOR_CACHE: dict[str, str] | None = None


def _industry_cache() -> dict[str, str]:
    """Ticker → industry, loaded once per process. "" means looked up and the
    ticker has no industry — a fund, or a symbol yfinance does not know."""
    global _SECTOR_CACHE
    if _SECTOR_CACHE is None:
        _SECTOR_CACHE = state_read(config.SECTOR_CACHE_FILE, {})
    return _SECTOR_CACHE


def _fetch_industry(ticker: str) -> str:
    """The industry yfinance reports for a ticker, or "" if it has none."""
    import logging, contextlib, io as _io
    with contextlib.redirect_stderr(_io.StringIO()):
        try:
            logging.disable(logging.CRITICAL)
            info = yf.Ticker(ticker).info
        except Exception:
            return ""
        finally:
            logging.disable(logging.NOTSET)
    return info.get("industry") or ""


def sector_map(tickers) -> dict[str, str]:
    """
    Sector for each ticker, "" where it has none. Resolved as a batch so a page
    or digest covering hundreds of tickers costs one state write, not hundreds.

    An explicit SECTOR_TICKERS entry wins; otherwise the ticker's industry is
    looked up (cached) and mapped through INDUSTRY_SECTORS.
    """
    wanted = {t.upper() for t in tickers}
    cache  = _industry_cache()

    missing = sorted(wanted - cache.keys())
    for ticker in missing:
        cache[ticker] = _fetch_industry(ticker)
    if missing:
        state_write(config.SECTOR_CACHE_FILE, cache)

    overrides = {
        t.upper(): sector
        for sector, tickers_ in config.SECTOR_TICKERS.items()
        for t in tickers_
    }
    return {
        ticker: overrides.get(ticker)
                or config.INDUSTRY_SECTORS.get(cache.get(ticker, ""), "")
        for ticker in wanted
    }


def sector_of(ticker: str) -> str:
    """Sector for one ticker, "" when it has none."""
    return sector_map([ticker])[ticker.upper()]


def _prune_seen(seen: set[str], today: datetime | None = None) -> set[str]:
    """
    Drop seen-keys too old to ever match again.

    Alerts only fire on trades inside config.FETCH_DAYS, so once a key's newest
    trade date falls outside SEEN_RETENTION_DAYS it cannot be re-detected and
    keeping it only grows the state file toward the Gist truncation limit.
    Keys with no parseable date are kept — an unrecognized shape should never be
    silently discarded, since dropping a live key means re-alerting it.
    """
    cutoff = (today or datetime.now()) - timedelta(days=config.SEEN_RETENTION_DAYS)

    kept = set()
    for key in seen:
        dates = _KEY_DATE_RE.findall(key)
        if not dates:
            kept.add(key)
            continue
        try:
            newest = datetime.strptime(max(dates), "%Y-%m-%d")
        except ValueError:
            kept.add(key)
            continue
        if newest >= cutoff:
            kept.add(key)
    return kept


def _load_seen() -> set[str]:
    """Load already-alerted trade keys."""
    return set(state_read(config.SEEN_TRADES_FILE, []))


def _save_seen(seen: set[str]) -> None:
    """Persist seen trade keys, pruning ones that can no longer match."""
    pruned = _prune_seen(seen)
    dropped = len(seen) - len(pruned)
    if dropped:
        print(f"  ✓ Pruned {dropped} expired seen-key(s) (>{config.SEEN_RETENTION_DAYS}d old)")
    state_write(config.SEEN_TRADES_FILE, sorted(pruned))


def _trade_key(trade: dict) -> str:
    """Unique key for a trade — used to avoid re-alerting."""
    return f"{trade['representative']}|{trade['ticker']}|{trade['transaction_date']}|{trade['type']}"


def filter_new_trades(trades: list[dict]) -> tuple[list[dict], set[str]]:
    """
    Return only trades not previously seen, plus the updated seen set.
    """
    seen = _load_seen()
    new  = [t for t in trades if _trade_key(t) not in seen]
    return new, seen


def mark_seen(trades: list[dict], seen: set[str]) -> None:
    """Add trades to the seen set and persist."""
    for t in trades:
        seen.add(_trade_key(t))
    _save_seen(seen)


def _cluster_key(alert: Alert) -> str:
    """
    Unique key for a cluster alert — used to avoid re-alerting the same cluster
    every run. Built from the full set of participating trades, so a new member
    or trade joining the cluster produces a fresh alert while an unchanged
    cluster stays deduped.
    """
    direction = "buy" if alert.trades[0]["type"] == "purchase" else "sell"
    parts = sorted(_trade_key(t) for t in alert.trades)
    return f"cluster|{alert.ticker}|{direction}|" + "|".join(parts)


def _cross_key(alert: Alert) -> str:
    """
    Unique key for a cross-cluster alert — used to avoid re-alerting.
    Built from the full set of participating trades, so a new buyer joining a
    ticker produces a fresh alert while an unchanged overlap stays deduped.
    """
    parts = []
    for t in alert.trades:
        if t.get("source") == "insider":
            parts.append(f"{t['name']}|{t['ticker']}|{t['transaction_date']}|insider")
        else:
            parts.append(_trade_key(t))
    return f"crosscluster|{alert.ticker}|" + "|".join(sorted(parts))


def analyze_cross_cluster(
    congress_trades: list[dict],
    insider_trades:  list[dict],
) -> list[Alert]:
    """
    Detect cross-cluster alerts, dedupe against the shared seen set, and persist.
    Mirrors analyze() — reuses the same seen_trades.json / Gist state, no new file.
    Called by monitor.py on every poll cycle.
    """
    raw = detect_cross_cluster_alerts(alertable_trades(congress_trades), insider_trades)
    if not raw:
        return []

    seen  = _load_seen()
    fresh = []
    for alert in raw:
        key = _cross_key(alert)
        if key not in seen:
            seen.add(key)
            fresh.append(alert)

    if fresh:
        _save_seen(seen)
    return fresh


# ── Main entry point ──────────────────────────────────────────────────────────

def analyze(trades: list[dict]) -> list[Alert]:
    """
    Run all three alert detectors against the trade list.
    Filters out already-seen trades before alerting.
    Returns list of Alert objects sorted by tier priority (cluster first).

    Called by monitor.py on every poll cycle.
    """
    if not trades:
        print("  No trades to analyze.")
        return []

    # Drop what is not a signal before anything is detected. Win rates below
    # still use the full list — that is a historical measure, not an alert.
    alertable = alertable_trades(trades)
    print(f"  {len(trades) - len(alertable)} of {len(trades)} trades excluded "
          f"(sales / rebalancing), {len(alertable)} alert-eligible")

    # Filter to only new trades for watchlist + win-rate alerts
    # (cluster uses full list to detect patterns across time)
    new_trades, seen = filter_new_trades(alertable)
    print(f"  {len(new_trades)} new since last run")

    # 🔴 Cluster — run on full trade list (needs historical context), then dedup
    # against seen state so the same cluster doesn't re-email every run.
    print("  Detecting cluster alerts...")
    cluster_alerts = []
    for alert in detect_cluster_alerts(alertable):
        key = _cluster_key(alert)
        if key not in seen:
            seen.add(key)
            cluster_alerts.append(alert)

    # Win rates — computed once, used by win-rate detector
    print("  Scoring win rates...")
    win_rates = compute_win_rates(trades)

    # 🟡 Win-rate — only alert on new trades from high-performers
    print("  Detecting win-rate alerts...")
    winrate_alerts = detect_winrate_alerts(new_trades, win_rates)

    # 🟢 Watchlist — only alert on new trades from watched members
    print("  Detecting watchlist alerts...")
    watchlist_alerts = detect_watchlist_alerts(new_trades)

    # Mark all new trades as seen
    mark_seen(new_trades, seen)

    # Combine and rank by conviction score rather than tier — a large, fresh,
    # multi-member cluster should outrank a token watchlist buy regardless of tier.
    print("  Scoring alert conviction...")
    all_alerts = cluster_alerts + winrate_alerts + watchlist_alerts
    for alert in all_alerts:
        enrich_and_score(alert, win_rates)
    all_alerts.sort(key=lambda a: a.score, reverse=True)

    return all_alerts


# ── Main (standalone test) ────────────────────────────────────────────────────

def main():
    """
    Run analyzer against live fetcher output and print alerts.
    Fetches 90 days for win-rate scoring, alerts only on last FETCH_DAYS.
    """
    from fetcher import fetch_all

    # Fetch wide window for win-rate scoring base
    LEADERBOARD_DAYS = 180
    print(f"Fetching trades (last {LEADERBOARD_DAYS} days for win-rate base)...")
    all_trades = fetch_all(days=LEADERBOARD_DAYS)

    # Alerts only fire on recent trades
    recent_cutoff = datetime.now() - timedelta(days=config.FETCH_DAYS)
    recent_trades = [
        t for t in all_trades
        if datetime.strptime(t["transaction_date"], "%Y-%m-%d") >= recent_cutoff
    ]
    print(f"  {len(recent_trades)} trades in alert window (last {config.FETCH_DAYS} days)")

    print("\nAnalyzing recent trades...")
    alerts = analyze(recent_trades)

    print(f"\n{'═'*60}")
    print(f"  {len(alerts)} alert(s) fired")
    print(f"{'═'*60}")

    if not alerts:
        print("\n  No alerts. Market's quiet (or the data is thin).")
    else:
        for alert in alerts:
            print(f"\n{alert.message}")
            print(f"  Filed: {alert.fired_at}")

    # Win-rate leaderboard — use wide window, computed once
    print(f"\n{'═'*60}")
    print(f"  Win-Rate Leaderboard (top 10, min {config.WIN_RATE_MIN_TRADES} scored trades)")
    print(f"{'═'*60}")
    win_rates = compute_win_rates(all_trades)
    qualified = [
        (name, s) for name, s in win_rates.items()
        if s["total"] >= config.WIN_RATE_MIN_TRADES
    ]
    if not qualified:
        print(f"\n  No members with {config.WIN_RATE_MIN_TRADES}+ scored trades yet.")
        print(f"  Tip: increase LEADERBOARD_DAYS above or lower WIN_RATE_MIN_TRADES in config.py")
    else:
        qualified.sort(key=lambda x: x[1]["win_rate"], reverse=True)
        for name, s in qualified[:10]:
            bar = "█" * int(s["win_rate"] * 20)
            tag = " ⭐" if s["qualifies"] else ""
            print(
                f"  {name:<30} {s['win_rate']:>5.0%}  {bar:<20} "
                f"({s['wins']}/{s['total']}){tag}"
            )

    print("\n✓ analyzer.py complete.\n")


if __name__ == "__main__":
    main()