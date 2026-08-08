"""
history.py — Congressional Trade Monitor
Records every fired alert and scores it against SPY once enough time has passed.

Without this the monitor has no memory: it can tell you a cross-signal fired,
but not whether cross-signals are worth acting on. Each record captures the
alert's conviction score and entry price at fire time, so `score_history()` can
later fill in forward returns and `performance_summary()` can answer the only
question that matters — which tiers and which score ranges actually beat SPY.

Storage rides on the same Gist as seen_trades.json (a Gist holds many files),
so this needs no new credentials or GitHub Actions config.

Public interface:
  record_alerts(alerts) -> int
  score_history() -> int
  performance_summary() -> dict
"""

from datetime import datetime, timedelta

import config
from analyzer import Alert, state_read, state_write, _get_price


# ── Recording ─────────────────────────────────────────────────────────────────

def _record_id(alert: Alert) -> str:
    """
    Stable identity for a fired alert. The seen-state keys already prevent the
    same alert firing twice, so tier+ticker+first trade date is enough to keep
    a re-run from double-recording.
    """
    first = alert.meta.get("first_date") or alert.fired_at[:10]
    return f"{alert.tier}|{alert.ticker}|{first}"


def load_history() -> list[dict]:
    """All recorded alerts, oldest first."""
    records = state_read(config.HISTORY_FILE, [])
    return records if isinstance(records, list) else []


def save_history(records: list[dict]) -> None:
    """
    Persist the alert log, keeping only the most recent HISTORY_MAX_RECORDS.

    The Gist API truncates file contents past ~1MB without erroring, which would
    corrupt the log silently. At ~340 bytes per record the cap keeps the file
    well under that while retaining far more history than calibrating the
    conviction weights needs.
    """
    if len(records) > config.HISTORY_MAX_RECORDS:
        dropped = len(records) - config.HISTORY_MAX_RECORDS
        records = records[-config.HISTORY_MAX_RECORDS:]
        print(f"  ✓ Trimmed {dropped} oldest history record(s) (cap {config.HISTORY_MAX_RECORDS})")
    state_write(config.HISTORY_FILE, records)


def record_alerts(alerts: list[Alert]) -> int:
    """
    Append newly fired alerts to the history log. Returns the number added.
    Entry prices are captured now so forward returns can be computed later
    without needing to re-derive what the price was at fire time.
    """
    if not alerts:
        return 0

    records = load_history()
    known   = {r["id"] for r in records}

    added = 0
    for alert in alerts:
        rid = _record_id(alert)
        if rid in known:
            continue

        entry_date = alert.meta.get("first_date") or alert.fired_at[:10]
        try:
            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
        except ValueError:
            continue

        records.append({
            "id":              rid,
            "fired_date":      alert.fired_at[:10],
            "entry_date":      entry_date,
            "tier":            alert.tier,
            "ticker":          alert.ticker,
            "score":           alert.score,
            "n_members":       alert.meta.get("n_members", 0),
            "n_insiders":      alert.meta.get("n_insiders", 0),
            "dollar_total":    alert.meta.get("dollar_total", 0.0),
            "median_lag_days": alert.meta.get("median_lag_days"),
            "entry_price":     _get_price(alert.ticker, entry_dt),
            "spy_entry":       _get_price("SPY", entry_dt),
        })
        known.add(rid)
        added += 1

    if added:
        save_history(records)
        print(f"  ✓ Recorded {added} alert(s) to history")
    return added


# ── Forward scoring ───────────────────────────────────────────────────────────

def score_history(today: datetime | None = None) -> int:
    """
    Fill in forward returns for records whose windows have elapsed.
    Returns the number of (record, window) pairs newly scored.

    Each window gets ret_N (the ticker's return), spy_N (SPY's return over the
    same span) and excess_N. Excess is the number that matters — beating SPY is
    the bar, since buying the index required no signal at all.
    """
    today   = today or datetime.now()
    records = load_history()
    if not records:
        return 0

    scored = 0
    for r in records:
        entry_price = r.get("entry_price")
        spy_entry   = r.get("spy_entry")
        if not entry_price or not spy_entry:
            continue

        try:
            entry_dt = datetime.strptime(r["entry_date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue

        for window in config.WIN_RATE_WINDOWS:
            key = f"excess_{window}"
            if key in r:
                continue  # already scored

            exit_dt = entry_dt + timedelta(days=window)
            if exit_dt > today - timedelta(days=1):
                continue  # window hasn't elapsed yet

            price_exit = _get_price(r["ticker"], exit_dt)
            spy_exit   = _get_price("SPY", exit_dt)
            if not price_exit or not spy_exit:
                continue

            ret = (price_exit - entry_price) / entry_price * 100
            spy = (spy_exit - spy_entry) / spy_entry * 100
            r[f"ret_{window}"] = round(ret, 2)
            r[f"spy_{window}"] = round(spy, 2)
            r[key]             = round(ret - spy, 2)
            scored += 1

    if scored:
        save_history(records)
        print(f"  ✓ Scored {scored} alert-window(s) against SPY")
    return scored


# ── Aggregation ───────────────────────────────────────────────────────────────

SCORE_BUCKETS = [("0-40", 0, 40), ("40-70", 40, 70), ("70+", 70, 101)]


def _aggregate(records: list[dict], window: int) -> dict:
    """Count, SPY-beat rate, and mean excess return for one set of records."""
    excess = [r[f"excess_{window}"] for r in records if f"excess_{window}" in r]
    if not excess:
        return {"n": 0, "beat_spy": None, "avg_excess": None}
    return {
        "n":          len(excess),
        "beat_spy":   sum(1 for e in excess if e > 0) / len(excess),
        "avg_excess": sum(excess) / len(excess),
    }


def performance_summary(window: int | None = None) -> dict:
    """
    Aggregate scored history by alert tier and by conviction-score bucket.

    The tier breakdown says which signal types earn their place; the score
    breakdown says whether the conviction score is calibrated — if the 70+
    bucket does not beat the 0-40 bucket, the weights in config are wrong.
    """
    window  = window or config.WIN_RATE_PRIMARY
    records = load_history()

    by_tier = {}
    for tier in sorted({r["tier"] for r in records}):
        by_tier[tier] = _aggregate([r for r in records if r["tier"] == tier], window)

    by_bucket = {}
    for label, lo, hi in SCORE_BUCKETS:
        in_bucket = [r for r in records if lo <= r.get("score", 0) < hi]
        by_bucket[label] = _aggregate(in_bucket, window)

    unmatured = sum(1 for r in records if f"excess_{window}" not in r)
    return {
        "window":    window,
        "total":     len(records),
        "unmatured": unmatured,
        "by_tier":   by_tier,
        "by_bucket": by_bucket,
    }


def format_summary(summary: dict) -> str:
    """Render performance_summary() as plain text for the console and digest."""
    lines = [
        f"Alert performance vs SPY over {summary['window']} days",
        f"  {summary['total']} recorded · {summary['unmatured']} still maturing",
        "",
    ]

    def rows(title: str, group: dict) -> None:
        lines.append(f"  {title}")
        if not group:
            lines.append("    (no records yet)")
            return
        for name, s in group.items():
            if not s["n"]:
                lines.append(f"    {name:<16} no matured alerts yet")
            else:
                lines.append(
                    f"    {name:<16} {s['n']:>3} scored · "
                    f"{s['beat_spy']:.0%} beat SPY · "
                    f"{s['avg_excess']:+.1f}% avg excess"
                )

    rows("By tier", summary["by_tier"])
    lines.append("")
    rows("By conviction score", summary["by_bucket"])
    return "\n".join(lines)


# ── Main (standalone) ─────────────────────────────────────────────────────────

def main():
    """Score any matured records and print the performance summary."""
    print("Scoring alert history against SPY...")
    score_history()
    print()
    print(format_summary(performance_summary()))


if __name__ == "__main__":
    main()
