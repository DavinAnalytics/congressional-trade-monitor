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
import fundamentals
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
from notifier import load_ai_context

SITE_DIR = Path(__file__).parent / "site"
TEMPLATE = SITE_DIR / "template.html"

MAX_ALERT_CHARTS = 10
MAX_INSIDER_CHARTS = 6
CHART_DAYS = 180

TIER_WEIGHTS = {
    "cross_cluster": 4,
    "cluster":       3,
    "winrate":       2,
    "watchlist":     1,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sector_of(ticker: str) -> str:
    return sector_of(ticker)


def _actionability(alert: dict) -> dict:
    """Whether a signal is still worth acting on, for bots and the UI."""
    lag = alert.get("lag_days")
    excess = alert.get("excess")
    direction = alert.get("direction", "buy")
    score = "unknown"
    reason = "insufficient timing data"
    if lag is not None:
        ran = excess is not None and (
            (direction == "buy" and excess > 15) or
            (direction == "sell" and excess < -15)
        )
        if lag <= 14 and not ran:
            score, reason = "actionable", "fresh disclosure, move not extended"
        elif lag > 30 or ran:
            score = "stale"
            reason = "stale disclosure" if lag > 30 else "price move likely absorbed edge"
        else:
            score, reason = "aging", "disclosure aging or moderate move"
    return {"score": score, "reason": reason}


def _price_series(ticker: str, start: datetime, markers: list[dict] | None = None) -> dict | None:
    """
    Ticker and SPY closes over the window, each indexed to 100 at the start so
    the two lines are directly comparable. Optional markers label trade dates.
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

    dates = [d.strftime("%Y-%m-%d") for d in common]
    out = {
        "dates":  dates,
        "ticker": [round(float(tk.loc[d]) / t0 * 100, 2) for d in common],
        "spy":    [round(float(spy.loc[d]) / s0 * 100, 2) for d in common],
        "markers": [],
    }
    if markers:
        date_set = set(dates)
        for m in markers:
            d = m.get("date", "")[:10]
            if d in date_set:
                out["markers"].append({
                    "date":   d,
                    "label":  m.get("label", ""),
                    "kind":   m.get("kind", "trade"),
                })
    return out


def _chart_markers_for_ticker(ticker: str, alert_rows: list[dict],
                              insider_trades: list[dict]) -> list[dict]:
    """Trade-date markers for price charts."""
    sym = ticker.upper()
    markers = []
    for a in alert_rows:
        if a["ticker"].upper() != sym:
            continue
        for t in a.get("congress", []):
            markers.append({"date": t["date"], "label": t["member"][:20], "kind": "congress"})
        for t in a.get("insider", []):
            markers.append({"date": t["date"], "label": t["name"][:20], "kind": "insider"})
    for t in insider_trades:
        if t.get("ticker", "").upper() == sym:
            markers.append({
                "date":  t.get("transaction_date", ""),
                "label": (t.get("name") or "")[:20],
                "kind":  "insider",
            })
    # De-dupe by date+kind
    seen = set()
    out = []
    for m in markers:
        key = (m["date"], m["kind"])
        if key not in seen and m["date"]:
            seen.add(key)
            out.append(m)
    return out


def _insider_seniority_breakdown(insider_rows: list[dict],
                                 alert_rows: list[dict]) -> dict:
    """CEO/CFO vs other officers vs directors across insider buys and signals."""
    buckets = {"CEO/CFO": 0, "officer": 0, "director": 0, "other": 0}
    by_ticker: dict[str, dict] = {}

    def _bump(title: str, value: float = 1):
        b = fundamentals.insider_seniority_bucket(title)
        buckets[b] = buckets.get(b, 0) + 1

    for row in insider_rows:
        _bump(row.get("title", ""))
        tk = row.get("ticker", "").upper()
        if tk:
            slot = by_ticker.setdefault(tk, {"CEO/CFO": 0, "officer": 0, "director": 0, "other": 0})
            slot[fundamentals.insider_seniority_bucket(row.get("title", ""))] += 1

    signal_seniority = []
    for a in alert_rows:
        if not a.get("insider"):
            continue
        counts = {"CEO/CFO": 0, "officer": 0, "director": 0, "other": 0}
        for t in a["insider"]:
            counts[fundamentals.insider_seniority_bucket(t.get("title", ""))] += 1
        signal_seniority.append({
            "ticker": a["ticker"],
            "tier":   a["tier"],
            "score":  a["score"],
            "counts": counts,
            "has_top_insider": counts["CEO/CFO"] > 0,
        })

    return {
        "totals": buckets,
        "by_ticker": by_ticker,
        "on_signals": signal_seniority,
    }


def _sector_heatmap(alert_rows: list[dict],
                    fundamentals_map: dict | None = None) -> list[dict]:
    """Tier-weighted congressional attention by sector."""
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    fundamentals_map = fundamentals_map or {}
    for a in alert_rows:
        # Prefer the monitor's industry→sector map, then yfinance sector from
        # the fundamental snapshot, so new tickers don't collapse into Unknown.
        fund = fundamentals_map.get(a["ticker"]) or {}
        sec = _sector_of(a["ticker"]) or fund.get("sector") or "Unknown"
        w = TIER_WEIGHTS.get(a["tier"], 1)
        scores[sec] = scores.get(sec, 0) + w * (a["score"] / 100)
        counts[sec] = counts.get(sec, 0) + 1
    rows = [
        {"sector": s, "weight": round(scores[s], 2), "signals": counts[s]}
        for s in scores
    ]
    rows.sort(key=lambda r: r["weight"], reverse=True)
    return rows


def _ticker_dossier(ticker: str, alert_rows: list[dict], trades: list[dict],
                    insider_rows: list[dict], win_rates: dict,
                    fund: dict, ai_ctx: str | None) -> dict:
    """Unified research view for one ticker."""
    sym = ticker.upper()
    signals = [a for a in alert_rows if a["ticker"].upper() == sym]
    cong = [t for t in trades if t.get("ticker", "").upper() == sym
            and t.get("type") == "purchase"]
    ins = [t for t in insider_rows if t.get("ticker", "").upper() == sym]
    members = {t.get("representative", "") for t in cong}
    member_stats = []
    for m in members:
        s = win_rates.get(m, {})
        if s.get("total", 0) >= config.WIN_RATE_MIN_TRADES:
            member_stats.append({
                "member": display_name(m),
                "win_rate": round(s.get("win_rate", 0), 4),
                "qualifies": s.get("qualifies", False),
            })
    member_stats.sort(key=lambda x: x["win_rate"], reverse=True)

    conflicts = []
    for m in members:
        conflicts += [f"{display_name(m)}: {c}" for c in flag_conflicts(m, sym)]

    sen = {"CEO/CFO": 0, "officer": 0, "director": 0, "other": 0}
    for t in ins:
        sen[fundamentals.insider_seniority_bucket(t.get("title", ""))] += 1

    best_signal = signals[0] if signals else None
    action = _actionability(best_signal) if best_signal else {"score": "none", "reason": "no live signal"}

    return {
        "ticker": sym,
        "name": fund.get("name", ""),
        "sector": fund.get("sector") or _sector_of(sym),
        "signals": signals,
        "congress_buys": len(cong),
        "insider_buys": len(ins),
        "members": member_stats,
        "conflicts": conflicts,
        "insider_seniority": sen,
        "fundamentals": fund,
        "actionability": action,
        "ai_context": ai_ctx,
        "links": fund.get("links", {}),
        "tradingview_symbol": fund.get("tradingview_symbol", ""),
    }


def _bot_brief(dossier: dict) -> dict:
    """Structured export for Grok / other bots — one object per ticker."""
    sig = dossier["signals"][0] if dossier["signals"] else None
    fields = dossier.get("fundamentals", {}).get("fields", {})
    flat_fund = {}
    for k, v in fields.items():
        if v and v.get("value") is not None:
            flat_fund[k] = {
                "value": v["value"],
                "reliable": v.get("reliable", False),
                "source": v.get("source", ""),
            }
    return {
        "ticker": dossier["ticker"],
        "name": dossier.get("name", ""),
        "sector": dossier.get("sector", ""),
        "generated_for": "congressional-trade-monitor",
        "signal": None if not sig else {
            "tier": sig["tier"],
            "label": sig["label"],
            "conviction": sig["score"],
            "direction": sig["direction"],
            "is_new": sig.get("is_new", False),
            "congress_dollars": sig.get("congress_dollars"),
            "insider_dollars": sig.get("insider_dollars"),
            "lag_days": sig.get("lag_days"),
            "pct_since_trade": sig.get("pct_since"),
            "spy_since_trade": sig.get("spy_since"),
            "excess_vs_spy": sig.get("excess"),
            "conflicts": sig.get("conflicts", []),
            "insider_seniority": dossier.get("insider_seniority", {}),
        },
        "actionability": dossier.get("actionability", {}),
        "fundamentals": flat_fund,
        "fundamental_quality": dossier.get("fundamentals", {}).get("quality", {}),
        "historical_pe": dossier.get("fundamentals", {}).get("historical_pe"),
        "gross_margin_trend": dossier.get("fundamentals", {}).get("gross_margin_trend", []),
        "earnings": dossier.get("fundamentals", {}).get("earnings"),
        "headlines": dossier.get("fundamentals", {}).get("headlines", []),
        "ai_context": dossier.get("ai_context"),
        "links": dossier.get("links", {}),
        "disclaimer": (
            "Fundamentals are yfinance snapshots for screening — verify material "
            "metrics in Qualtrim before auto-filtering. News headlines are not "
            "sentiment scores. Congressional amounts are disclosure-bracket midpoints."
        ),
    }


# ── Payload ───────────────────────────────────────────────────────────────────

def _signature(alert) -> tuple:
    parts = []
    for t in alert.trades:
        who = t.get("name") or t.get("representative", "")
        parts.append(f"{who}|{t.get('ticker','')}|{t.get('transaction_date','')}")
    return (alert.tier, alert.ticker, tuple(sorted(parts)))


def _alert_id(tier: str, ticker: str, first_date: str | None) -> str:
    return f"{tier}|{ticker}|{first_date or ''}"


def current_signals(recent: list[dict], insider_trades: list[dict],
                    win_rates: dict, fired: list) -> list:
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


def _alert_payload(alerts: list, ai_store: dict) -> list[dict]:
    out = []
    for a in alerts:
        members = a.meta.get("members", [])
        conflicts = []
        for m in members:
            conflicts += [f"{display_name(m)}: {c}"
                          for c in flag_conflicts(m, a.ticker)]

        insider = [t for t in a.trades if t.get("source") == "insider"]
        congress = [t for t in a.trades if t.get("source") != "insider"]
        first = a.meta.get("first_date")
        aid = _alert_id(a.tier, a.ticker, first)
        ai = (ai_store.get(aid) or {}).get("context")

        row = {
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
            "first_date": first,
            "conflicts": conflicts,
            "congress":  [_trade_row(t) for t in congress],
            "insider":   [_insider_row(t) for t in insider],
            "has_top_insider": a.meta.get("has_top_insider", False),
            "ai_context": ai,
        }
        row["actionability"] = _actionability(row)
        out.append(row)
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
    title = t.get("title", "")
    return {
        "name":      t.get("name", ""),
        "title":     title,
        "seniority": fundamentals.insider_seniority_bucket(title),
        "company":   t.get("company", ""),
        "ticker":    t.get("ticker", ""),
        "date":      t.get("transaction_date", ""),
        "shares":    t.get("shares", 0),
        "price":     t.get("price", 0.0),
        "value":     t.get("value", 0.0),
        "link":      t.get("ptr_link", ""),
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
    print("  Collecting dashboard data...")
    fundamentals.clear_cache()

    cutoff = datetime.now() - timedelta(days=config.FETCH_DAYS)
    recent = [
        t for t in trades
        if datetime.strptime(t["transaction_date"], "%Y-%m-%d") >= cutoff
    ]
    signals = current_signals(recent, insider_trades, win_rates, alerts)
    ai_store = load_ai_context()
    alert_rows = _alert_payload(signals, ai_store)
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

    # Tickers needing fundamentals: all live signals + cross overlaps
    fund_tickers = list(dict.fromkeys(
        [a["ticker"] for a in alert_rows] + [c["ticker"] for c in cross]
    ))
    print(f"    fundamentals 0/{len(fund_tickers)}", end="\r")
    fundamentals_map = {}
    for i, tk in enumerate(fund_tickers, 1):
        fundamentals_map[tk] = fundamentals.snapshot(tk)
        print(f"    fundamentals {i}/{len(fund_tickers)}", end="\r")
    print(f"    fundamentals {len(fund_tickers)} ticker(s)          ")

    alert_charts = list(dict.fromkeys(a["ticker"] for a in alert_rows))[:MAX_ALERT_CHARTS]
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
        markers = _chart_markers_for_ticker(tk, alert_rows, insider_trades)
        series = _price_series(tk, start, markers)
        if series:
            fund = fundamentals_map.get(tk) or fundamentals.snapshot(tk)
            series["tradingview_symbol"] = fund.get("tradingview_symbol", "")
            prices[tk] = series
        print(f"    charts {i}/{len(chart_tickers)}", end="\r")
    print(f"    charted {len(prices)} ticker(s)          ")

    insider_payload = [
        {**_insider_row(t), "also_congress": t["ticker"].upper() in congress_tickers}
        for t in insider_trades
    ]

    dossiers = {}
    for tk in fund_tickers:
        fund = fundamentals_map[tk]
        ai_ctx = next(
            (a.get("ai_context") for a in alert_rows if a["ticker"] == tk and a.get("ai_context")),
            None,
        )
        dossiers[tk] = _ticker_dossier(
            tk, alert_rows, trades, insider_trades, win_rates, fund, ai_ctx,
        )

    briefs = {tk: _bot_brief(d) for tk, d in dossiers.items()}

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
            "cross":    len(cross),
        },
        "alerts":      alert_rows,
        "trades":      [_trade_row(t) for t in trades],
        "insider":     insider_payload,
        "leaderboard": _leaderboard(win_rates),
        "cross":       cross,
        "prices":      prices,
        "fundamentals": fundamentals_map,
        "dossiers":    dossiers,
        "briefs":      briefs,
        "sector_heatmap": _sector_heatmap(alert_rows, fundamentals_map),
        "insider_seniority": _insider_seniority_breakdown(insider_payload, alert_rows),
        "performance": performance,
        "performance_text": history.format_summary(performance),
        "findings": [
            {"severity": f.severity, "headline": f.headline,
             "detail": f.detail, "action": f.action}
            for f in findings
        ],
    }


def build(alerts: list, trades: list[dict], insider_trades: list[dict],
          win_rates: dict) -> Path:
    data = collect(alerts, trades, insider_trades, win_rates)

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "data.json").write_text(json.dumps(data, separators=(",", ":")))

    template = TEMPLATE.read_text()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out = SITE_DIR / "index.html"
    out.write_text(template.replace("/*__DATA__*/null", payload))

    kb = out.stat().st_size / 1024
    print(f"  ✓ Built {out} ({kb:.0f} KB)")
    return out


def main():
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

    build([], trades, insider, compute_win_rates(trades))


if __name__ == "__main__":
    main()
