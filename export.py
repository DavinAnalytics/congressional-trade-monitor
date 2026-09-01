"""
export.py — Congressional Trade Monitor
Builds the GitHub Pages dashboard: a single self-contained HTML file,
regenerated daily by the GitHub Actions run that already fetched the data.

Public interface:
  build(alerts, trades, insider_trades, win_rates) -> Path
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import config
import history
import review
from analyzer import (
    parse_amount_value,
    sector_of,
    disclosure_lag_days,
    find_cross_signals,
    enrich_and_score,
    alertable_trades,
    detect_cluster_alerts,
    detect_winrate_alerts,
    detect_watchlist_alerts,
    detect_cross_cluster_alerts,
    _download_closes,
)
from committees import flag_conflicts, get_member_committees, display_name

SITE_DIR = Path(__file__).parent / "site"
TEMPLATE = SITE_DIR / "template.html"

# Charted tickers, budgeted per section rather than from one shared pool: the
# page now lists every live signal, so a single cap let ~19 alert tickers take
# all the slots and the insider section rendered empty.
MAX_ALERT_CHARTS = 10
MAX_INSIDER_CHARTS = 6
CHART_DAYS = 180


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sector_of(ticker: str) -> str:
    return sector_of(ticker)


def _price_series(ticker: str, start: datetime) -> dict | None:
    """
    Ticker and SPY closes over the window, each indexed to 100 at the start so
    the two lines are directly comparable. Only dates where both have a close
    are kept, so neither line trails past the other.
    """
    end = datetime.now() + timedelta(days=1)
    tk = _download_closes(ticker, start, end)
    spy = _download_closes("SPY", start, end)
    if tk is None or spy is None:
        return None

    common = [d for d in tk.index if d in set(spy.index)]
    if len(common) < 2:
        return None

    t0, s0 = float(tk.loc[common[0]]), float(spy.loc[common[0]])
    if not t0 or not s0:
        return None

    return {
        "dates":  [d.strftime("%Y-%m-%d") for d in common],
        "ticker": [round(float(tk.loc[d]) / t0 * 100, 2) for d in common],
        "spy":    [round(float(spy.loc[d]) / s0 * 100, 2) for d in common],
    }


# ── Payload ───────────────────────────────────────────────────────────────────

def _signature(alert) -> tuple:
    """
    Identity of an alert by the trades that produced it — stable across two
    detector runs over the same data, so a signal rebuilt for the dashboard can
    be matched against the one the digest actually emailed.
    """
    parts = []
    for t in alert.trades:
        who = t.get("name") or t.get("representative", "")
        parts.append(f"{who}|{t.get('ticker','')}|{t.get('transaction_date','')}")
    return (alert.tier, alert.ticker, tuple(sorted(parts)))


def current_signals(recent: list[dict], insider_trades: list[dict],
                    win_rates: dict, fired: list) -> list:
    """
    Every signal live in the current window, ranked — not just the ones that
    fired today.

    The digest deliberately suppresses signals it has already emailed, so the
    alerts monitor.poll() hands over are a one-day delta and would leave this
    page reading "0 signals" on any quiet day. The detectors themselves are
    pure — analyze() is what touches the seen-state — so re-running them here
    rebuilds the full picture without making the next real run go quiet.
    """
    eligible = alertable_trades(recent)
    alerts = (
        detect_cluster_alerts(eligible)
        + detect_winrate_alerts(eligible, win_rates)
        + detect_watchlist_alerts(eligible)
        + detect_cross_cluster_alerts(eligible, insider_trades)
    )
    fired_sigs = {_signature(a) for a in fired}
    for a in alerts:
        enrich_and_score(a, win_rates)
        a.meta["is_new"] = _signature(a) in fired_sigs
    alerts.sort(key=lambda a: a.score, reverse=True)
    return alerts


def _alert_payload(alerts: list) -> list[dict]:
    out = []
    for a in alerts:
        members = a.meta.get("members", [])
        conflicts = []
        for m in members:
            conflicts += [f"{display_name(m)}: {c}"
                          for c in flag_conflicts(m, a.ticker)]

        insider = [t for t in a.trades if t.get("source") == "insider"]
        congress = [t for t in a.trades if t.get("source") != "insider"]

        out.append({
            "tier":      a.tier,
            "label":     config.TIER_LABELS.get(a.tier, a.tier),
            "ticker":    a.ticker,
            "score":     a.score,
            "headline":  a.message.splitlines()[0],
            "direction": a.meta.get("direction", "buy"),
            "is_new":    a.meta.get("is_new", False),
            "members":   [display_name(m) for m in members],
            "congress_dollars": a.meta.get("congress_dollars", 0.0),
            "insider_dollars":  a.meta.get("insider_dollars", 0.0),
            "lag_days":  a.meta.get("median_lag_days"),
            "pct_since": a.meta.get("pct_since_trade"),
            "spy_since": a.meta.get("spy_since_trade"),
            "excess":    a.meta.get("excess_since_trade"),
            "first_date": a.meta.get("first_date"),
            "conflicts": conflicts,
            "congress":  [_trade_row(t) for t in congress],
            "insider":   [_insider_row(t) for t in insider],
        })
    return out


def _trade_row(t: dict) -> dict:
    return {
        "chamber":    t.get("chamber", ""),
        "member":     display_name(t.get("representative", "")),
        "ticker":     t.get("ticker", ""),
        "type":       t.get("type", ""),
        "date":       t.get("transaction_date", ""),
        "disclosed":  t.get("disclosure_date", ""),
        "lag":        disclosure_lag_days(t),
        "amount":     t.get("amount", ""),
        "value":      parse_amount_value(t.get("amount", "")),
        "owner":      t.get("owner", ""),
        "asset_type": t.get("asset_type", "stock"),
        "sector":     _sector_of(t.get("ticker", "")),
        "link":       t.get("ptr_link", ""),
    }


def _insider_row(t: dict) -> dict:
    return {
        "name":    t.get("name", ""),
        "title":   t.get("title", ""),
        "company": t.get("company", ""),
        "ticker":  t.get("ticker", ""),
        "date":    t.get("transaction_date", ""),
        "shares":  t.get("shares", 0),
        "price":   t.get("price", 0.0),
        "value":   t.get("value", 0.0),
        "link":    t.get("ptr_link", ""),
    }


def _leaderboard(win_rates: dict) -> list[dict]:
    rows = []
    for member, s in win_rates.items():
        if s["total"] < config.WIN_RATE_MIN_TRADES:
            continue
        data = get_member_committees(member) or {}
        rows.append({
            "member":     display_name(member),
            "wins":       s["wins"],
            "total":      s["total"],
            "win_rate":   round(s["win_rate"], 4),
            "qualifies":  s["qualifies"],
            "chamber":    data.get("chamber", ""),
            "committees": (data.get("committees") or [])[:6],
        })
    rows.sort(key=lambda r: r["win_rate"], reverse=True)
    return rows


def collect(alerts: list, trades: list[dict], insider_trades: list[dict],
            win_rates: dict) -> dict:
    """
    Assemble everything the page needs into one JSON-serialisable dict.

    `alerts` is what the digest emailed this run; the page shows every live
    signal and marks those as new.
    """
    print("  Collecting dashboard data...")

    cutoff = datetime.now() - timedelta(days=config.FETCH_DAYS)
    recent = [
        t for t in trades
        if datetime.strptime(t["transaction_date"], "%Y-%m-%d") >= cutoff
    ]
    signals = current_signals(recent, insider_trades, win_rates, alerts)

    alert_rows = _alert_payload(signals)
    congress_tickers = {t["ticker"].upper() for t in trades if t["type"] == "purchase"}

    cross = [
        {
            "ticker":    m["ticker"],
            "congress":  len(m["congress"]),
            "insider":   len(m["insider"]),
            "span_days": m["span_days"],
            "first":     m["first"].strftime("%Y-%m-%d"),
            "last":      m["last"].strftime("%Y-%m-%d"),
        }
        for m in find_cross_signals(trades, insider_trades)
    ]

    # Charts: the highest-scoring alerts, then the biggest insider buys. Each
    # section gets its own budget so a busy alert window cannot starve the other.
    alert_charts = list(dict.fromkeys(
        a["ticker"] for a in alert_rows
    ))[:MAX_ALERT_CHARTS]
    insider_charts = [
        t for t in dict.fromkeys(
            x["ticker"] for x in sorted(insider_trades,
                                        key=lambda x: x.get("value", 0), reverse=True)
        ) if t not in alert_charts
    ][:MAX_INSIDER_CHARTS]
    chart_tickers = alert_charts + insider_charts

    start = datetime.now() - timedelta(days=CHART_DAYS)
    prices = {}
    for i, tk in enumerate(chart_tickers, 1):
        series = _price_series(tk, start)
        if series:
            prices[tk] = series
        print(f"    charts {i}/{len(chart_tickers)}", end="\r")
    print(f"    charted {len(prices)} ticker(s)          ")

    findings = review.build_recommendations()
    performance = history.performance_summary()

    return {
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window_days":   CHART_DAYS,
        "alert_window":  config.FETCH_DAYS,
        "cluster_days":  config.CLUSTER_DAYS,
        "min_members":   config.CLUSTER_MIN_MEMBERS,
        "win_rate_min_trades": config.WIN_RATE_MIN_TRADES,
        "counts": {
            "trades":   len(trades),
            "alerts":   len(alert_rows),
            "new":      sum(1 for a in alert_rows if a["is_new"]),
            "insider":  len(insider_trades),
            "members":  len({t["representative"] for t in trades}),
        },
        "alerts":      alert_rows,
        "trades":      [_trade_row(t) for t in trades],
        "insider":     [
            {**_insider_row(t), "also_congress": t["ticker"].upper() in congress_tickers}
            for t in insider_trades
        ],
        "leaderboard": _leaderboard(win_rates),
        "cross":       cross,
        "prices":      prices,
        "performance": performance,
        "performance_text": history.format_summary(performance),
        "findings": [
            {"severity": f.severity, "headline": f.headline,
             "detail": f.detail, "action": f.action}
            for f in findings
        ],
    }


# ── Build ─────────────────────────────────────────────────────────────────────

def build(alerts: list, trades: list[dict], insider_trades: list[dict],
          win_rates: dict) -> Path:
    """Write site/index.html with the data inlined. Returns the output path."""
    data = collect(alerts, trades, insider_trades, win_rates)

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "data.json").write_text(json.dumps(data, separators=(",", ":")))

    template = TEMPLATE.read_text()
    # json.dumps output is embedded in a <script> block, so the only sequence
    # that can break out is "</script>" inside a string value.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out = SITE_DIR / "index.html"
    out.write_text(template.replace("/*__DATA__*/null", payload))

    kb = out.stat().st_size / 1024
    print(f"  ✓ Built {out} ({kb:.0f} KB)")
    return out


def main():
    """Standalone build — re-fetches everything. CI uses build() from monitor."""
    from fetcher import fetch_all
    from openinsider_fetcher import fetch_all as fetch_insider, InsiderFetchError
    from analyzer import compute_win_rates
    from committees import load_all

    load_all()
    trades = fetch_all(days=CHART_DAYS)
    try:
        insider = fetch_insider(days=config.FETCH_DAYS)
    except InsiderFetchError as e:
        print(f"  ⚠ {e}")
        insider = []

    # No alerts to mark as new — collect() runs the detectors itself.
    build([], trades, insider, compute_win_rates(trades))


if __name__ == "__main__":
    main()
