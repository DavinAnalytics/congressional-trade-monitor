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
import os
import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict

import yfinance as yf

import config

# ── Alert data structures ─────────────────────────────────────────────────────

@dataclass
class Alert:
    tier:        str          # "cluster" | "winrate" | "watchlist"
    ticker:      str
    trades:      list[dict]   # the trades that triggered this alert
    message:     str          # human-readable summary
    fired_at:    str = field(default_factory=lambda: datetime.now().isoformat())


# ── Win-rate scoring ──────────────────────────────────────────────────────────

def _get_price(ticker: str, date: datetime) -> float | None:
    """
    Get closing price for a ticker on or near a given date using yfinance.
    Fetches a 10-day window to catch the next trading day after weekends/holidays.
    Suppresses yfinance download noise.
    """
    import logging, contextlib, io as _io
    start = date
    end   = date + timedelta(days=10)
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
    return float(close.iloc[0]) if not close.empty else None


def _score_trade(trade: dict, window_days: int) -> bool | None:
    """
    Score a single trade as win (True), loss (False), or None (unscoreable).
    Win = member's return in `window_days` beats SPY return over same period.
    Only scores purchases (short selling is rare and signal is inverted).
    """
    if trade["type"] not in ("purchase",):
        return None  # only score purchases for now

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

    # Group trades by member
    by_member = defaultdict(list)
    for t in trades:
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


def detect_watchlist_alerts(trades: list[dict]) -> list[Alert]:
    """
    🟢 Watchlist Alert
    Flag any trade from a member on the config watchlist.
    Deduplicates by (member, ticker, date) — spouse + self trades on the
    same ticker/date count as one alert, with all owners grouped together.
    """
    watchlist_lower = [w.lower() for w in config.WATCHLIST]

    # Group by (member, ticker, date) to collapse spouse/self rows
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for trade in trades:
        member = trade["representative"]
        if member.lower() in watchlist_lower:
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


# ── Seen-trades deduplication (Gist-backed for GitHub Actions) ───────────────

def _gist_enabled() -> bool:
    """True if GIST_TOKEN and GIST_ID are set in environment."""
    return bool(os.getenv("GIST_TOKEN") and os.getenv("GIST_ID"))


def _load_seen() -> set[str]:
    """
    Load already-alerted trade keys.
    Reads from GitHub Gist if credentials available, otherwise local file.
    """
    if _gist_enabled():
        try:
            gist_id = os.getenv("GIST_ID")
            token   = os.getenv("GIST_TOKEN")
            resp = requests.get(
                f"https://api.github.com/gists/{gist_id}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            content = resp.json()["files"]["seen_trades.json"]["content"]
            return set(json.loads(content))
        except Exception as e:
            print(f"  ⚠ Could not load Gist state: {e} — starting fresh")
            return set()

    # Local file fallback
    if not os.path.exists(config.SEEN_TRADES_FILE):
        return set()
    try:
        with open(config.SEEN_TRADES_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    """
    Persist seen trade keys.
    Writes to GitHub Gist if credentials available, otherwise local file.
    """
    if _gist_enabled():
        try:
            gist_id = os.getenv("GIST_ID")
            token   = os.getenv("GIST_TOKEN")
            requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={"files": {"seen_trades.json": {"content": json.dumps(sorted(seen), indent=2)}}},
                timeout=15,
            )
            print("  ✓ State saved to Gist")
        except Exception as e:
            print(f"  ⚠ Could not save Gist state: {e}")
        return

    # Local file fallback
    with open(config.SEEN_TRADES_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


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

    # Filter to only new trades for watchlist + win-rate alerts
    # (cluster uses full list to detect patterns across time)
    new_trades, seen = filter_new_trades(trades)
    print(f"  {len(trades)} total trades, {len(new_trades)} new since last run")

    # 🔴 Cluster — run on full trade list (needs historical context)
    print("  Detecting cluster alerts...")
    cluster_alerts = detect_cluster_alerts(trades)

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

    # Combine and sort: cluster > winrate > watchlist
    tier_order = {"cluster": 0, "winrate": 1, "watchlist": 2}
    all_alerts = cluster_alerts + winrate_alerts + watchlist_alerts
    all_alerts.sort(key=lambda a: tier_order[a.tier])

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