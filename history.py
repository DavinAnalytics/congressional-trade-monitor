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

import random
from datetime import datetime, timedelta

import config
from analyzer import (
    Alert,
    state_read,
    state_write,
    parse_amount_value,
    disclosure_lag_days,
    _get_price,
    _trade_key,
)


# ── Recording ─────────────────────────────────────────────────────────────────

def _record_id(alert: Alert) -> str:
    """
    Stable identity for a fired alert. The seen-state keys already prevent the
    same alert firing twice, so tier+ticker+first trade date is enough to keep
    a re-run from double-recording.
    """
    first = alert.meta.get("first_date") or alert.fired_at[:10]
    return f"{alert.tier}|{alert.ticker}|{first}"


def load_history(filename: str = config.HISTORY_FILE) -> list[dict]:
    """All records in a log, oldest first."""
    records = state_read(filename, [])
    return records if isinstance(records, list) else []


def save_history(records: list[dict], filename: str = config.HISTORY_FILE) -> None:
    """
    Persist a log, keeping only the most recent HISTORY_MAX_RECORDS.

    The Gist API truncates file contents past ~1MB without erroring, which would
    corrupt the log silently. At ~340 bytes per record the cap keeps the file
    well under that while retaining far more history than calibrating the
    conviction weights needs.
    """
    if len(records) > config.HISTORY_MAX_RECORDS:
        dropped = len(records) - config.HISTORY_MAX_RECORDS
        records = records[-config.HISTORY_MAX_RECORDS:]
        print(f"  ✓ Trimmed {dropped} oldest record(s) from {filename} "
              f"(cap {config.HISTORY_MAX_RECORDS})")
    state_write(filename, records)


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
        fired_date = alert.fired_at[:10]
        try:
            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
            fired_dt = datetime.strptime(fired_date, "%Y-%m-%d")
        except ValueError:
            continue

        records.append({
            "id":              rid,
            "fired_date":      alert.fired_at[:10],
            "entry_date":      entry_date,
            "tier":             alert.tier,
            "ticker":           alert.ticker,
            "score":            alert.score,
            "direction":        alert.meta.get("direction", "buy"),
            "n_members":        alert.meta.get("n_members", 0),
            "n_insiders":       alert.meta.get("n_insiders", 0),
            # Whether a CEO/CFO was involved, versus only lower officers or
            # directors — so widening the insider screener can be judged on
            # outcomes instead of argued about.
            "has_top_insider":  alert.meta.get("has_top_insider", False),
            "congress_dollars": alert.meta.get("congress_dollars", 0.0),
            "insider_dollars":  alert.meta.get("insider_dollars", 0.0),
            "dollar_total":     alert.meta.get("dollar_total", 0.0),
            "median_lag_days": alert.meta.get("median_lag_days"),
            # Two baselines. entry_* is the price on the trade date, which measures
            # whether the politician's trade was good. fired_* is the price on the
            # day the alert reached you — the first moment you could have acted.
            # The gap between them is the return the disclosure lag ate, which you
            # never had any way to capture.
            "entry_price":     _get_price(alert.ticker, entry_dt),
            "spy_entry":       _get_price("SPY", entry_dt),
            "fired_price":     _get_price(alert.ticker, fired_dt),
            "spy_fired":       _get_price("SPY", fired_dt),
        })
        known.add(rid)
        added += 1

    if added:
        save_history(records)
        print(f"  ✓ Recorded {added} alert(s) to history")
    return added


# ── Control group ─────────────────────────────────────────────────────────────

def record_control(trades: list[dict], alerts: list[Alert], today: datetime | None = None) -> int:
    """
    Record a sample of congressional trades that did NOT trigger an alert.

    This is the null hypothesis made concrete. An alert hit rate on its own is
    uninterpretable — if un-alerted congressional trades perform just as well,
    the detectors contribute nothing and the headline number is merely the base
    rate of congressional trading. Scored through exactly the same code path as
    alerts so the comparison is like-for-like.

    Sampled deterministically per day so re-running a date does not reshuffle
    which trades were picked.
    """
    today = today or datetime.now()
    fired_date = today.strftime("%Y-%m-%d")

    alerted = {
        _trade_key(t)
        for a in alerts for t in a.trades
        if t.get("source") != "insider"
    }
    eligible = [t for t in trades if _trade_key(t) not in alerted]
    if not eligible:
        return 0

    records = load_history(config.CONTROL_FILE)
    known   = {r["id"] for r in records}

    # Deterministic per-day shuffle: stable across re-runs, varied across days.
    rng = random.Random(fired_date)
    rng.shuffle(eligible)

    try:
        fired_dt = datetime.strptime(fired_date, "%Y-%m-%d")
    except ValueError:
        return 0

    added = 0
    for t in eligible:
        if added >= config.CONTROL_SAMPLE_PER_RUN:
            break
        rid = _trade_key(t)
        if rid in known:
            continue
        try:
            entry_dt = datetime.strptime(t["transaction_date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue

        records.append({
            "id":               rid,
            "fired_date":       fired_date,
            "entry_date":       t["transaction_date"],
            "tier":             "control",
            "ticker":           t["ticker"],
            "score":            0.0,
            "direction":        "buy" if t["type"] == "purchase" else "sell",
            "congress_dollars": parse_amount_value(t.get("amount", "")),
            "median_lag_days":  disclosure_lag_days(t),
            "entry_price":      _get_price(t["ticker"], entry_dt),
            "spy_entry":        _get_price("SPY", entry_dt),
            "fired_price":      _get_price(t["ticker"], fired_dt),
            "spy_fired":        _get_price("SPY", fired_dt),
        })
        known.add(rid)
        added += 1

    if added:
        save_history(records, config.CONTROL_FILE)
        print(f"  ✓ Recorded {added} control trade(s)")
    return added


# ── Forward scoring ───────────────────────────────────────────────────────────

# The two baselines a record is scored from.
#   (prefix, date field, price field, spy field)
# "" is the trade date — did the politician trade well?
# "act_" is the alert date — could YOU have made money acting on this?
# Only the second says whether the monitor is worth running.
BASELINES = [
    ("",     "entry_date", "entry_price", "spy_entry"),
    ("act_", "fired_date", "fired_price", "spy_fired"),
]


def score_all(today: datetime | None = None) -> int:
    """Score both the alert log and the control log. Returns the total scored."""
    return (
        score_history(today, config.HISTORY_FILE)
        + score_history(today, config.CONTROL_FILE)
    )


def score_history(today: datetime | None = None,
                  filename: str = config.HISTORY_FILE) -> int:
    """
    Fill in forward returns for records whose windows have elapsed.
    Returns the number of (record, baseline, window) triples newly scored.

    Each baseline/window gets ret, spy, excess and edge.

    edge is the number that matters. Excess alone answers "did the stock beat
    SPY", which is only the right question for a buy signal: a sell cluster
    predicts *under*performance, so a stock that beats SPY means the signal was
    wrong. edge flips the sign for sells, making it read uniformly as "how much
    the signal was right by" and safe to aggregate across directions.
    """
    today   = today or datetime.now()
    records = load_history(filename)
    if not records:
        return 0

    scored = 0
    for r in records:
        sign = -1 if r.get("direction") == "sell" else 1

        for prefix, date_field, price_field, spy_field in BASELINES:
            base_price = r.get(price_field)
            base_spy   = r.get(spy_field)
            if not base_price or not base_spy:
                continue

            try:
                base_dt = datetime.strptime(r[date_field], "%Y-%m-%d")
            except (ValueError, KeyError):
                continue

            for window in config.WIN_RATE_WINDOWS:
                key = f"{prefix}edge_{window}"
                if key in r:
                    continue  # already scored

                exit_dt = base_dt + timedelta(days=window)
                if exit_dt > today - timedelta(days=1):
                    continue  # window hasn't elapsed yet

                price_exit = _get_price(r["ticker"], exit_dt)
                spy_exit   = _get_price("SPY", exit_dt)
                if not price_exit or not spy_exit:
                    continue

                ret    = (price_exit - base_price) / base_price * 100
                spy    = (spy_exit - base_spy) / base_spy * 100
                excess = ret - spy
                r[f"{prefix}ret_{window}"]    = round(ret, 2)
                r[f"{prefix}spy_{window}"]    = round(spy, 2)
                r[f"{prefix}excess_{window}"] = round(excess, 2)
                r[key]                        = round(sign * excess, 2)
                scored += 1

    if scored:
        save_history(records, filename)
        print(f"  ✓ Scored {scored} window(s) in {filename} against SPY")
    return scored


# ── Significance ──────────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = p * (len(sorted_values) - 1)
    lo  = int(pos)
    hi  = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap(values: list[float], seed: str = "edge") -> dict | None:
    """
    Confidence interval for the mean of `values`, by resampling.

    An average edge cannot on its own be told apart from luck: with a handful of
    alerts and normal market noise, +2% arises by chance routinely. This draws
    len(values) samples *with replacement* many times over, giving a spread of
    averages the same data could plausibly have produced. If most of that spread
    sits above zero, the effect is real; if it straddles zero, there is no
    evidence either way — however good the headline number looks.

    Seeded, so the same records always give the same interval rather than
    jittering week to week.

    Returns None below BOOTSTRAP_MIN_SAMPLES, where the answer would be theatre.
    """
    n = len(values)
    if n < config.BOOTSTRAP_MIN_SAMPLES:
        return None

    rng   = random.Random(seed)
    means = []
    for _ in range(config.BOOTSTRAP_ITERATIONS):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    return {
        "mean":       sum(values) / n,
        "ci_low":     _percentile(means, 0.025),
        "ci_high":    _percentile(means, 0.975),
        # Share of resamples above zero — how confident "this is positive" is.
        "confidence": sum(1 for m in means if m > 0) / len(means),
    }


def bootstrap_difference(a: list[float], b: list[float], seed: str = "diff") -> dict | None:
    """
    Confidence interval for mean(a) − mean(b), resampling each group separately.

    This is the sharpest question the data can answer: not "did alerts make
    money" but "did alerts beat the trades that never alerted". If this interval
    straddles zero, the detectors have not been shown to add anything.
    """
    if len(a) < config.BOOTSTRAP_MIN_SAMPLES or len(b) < config.BOOTSTRAP_MIN_SAMPLES:
        return None

    rng   = random.Random(seed)
    diffs = []
    for _ in range(config.BOOTSTRAP_ITERATIONS):
        ma = sum(a[rng.randrange(len(a))] for _ in range(len(a))) / len(a)
        mb = sum(b[rng.randrange(len(b))] for _ in range(len(b))) / len(b)
        diffs.append(ma - mb)
    diffs.sort()

    return {
        "mean":       sum(a) / len(a) - sum(b) / len(b),
        "ci_low":     _percentile(diffs, 0.025),
        "ci_high":    _percentile(diffs, 0.975),
        "confidence": sum(1 for d in diffs if d > 0) / len(diffs),
    }


# ── Aggregation ───────────────────────────────────────────────────────────────

SCORE_BUCKETS = [("0-40", 0, 40), ("40-70", 40, 70), ("70+", 70, 101)]


def _stats(records: list[dict], key: str) -> dict:
    edges = [r[key] for r in records if key in r]
    if not edges:
        return {"n": 0, "hit_rate": None, "avg_edge": None}
    return {
        "n":        len(edges),
        "hit_rate": sum(1 for e in edges if e > 0) / len(edges),
        "avg_edge": sum(edges) / len(edges),
    }


def _aggregate(records: list[dict], window: int) -> dict:
    """
    Hit rate and mean edge for one set of records, on both baselines.

    Top-level keys stay on the trade-date baseline for backwards compatibility;
    "actionable" holds the same stats measured from the day the alert fired.
    """
    out = _stats(records, f"edge_{window}")
    out["actionable"] = _stats(records, f"act_edge_{window}")
    return out


def _edges(records: list[dict], window: int, prefix: str = "act_") -> list[float]:
    key = f"{prefix}edge_{window}"
    return [r[key] for r in records if key in r]


def horizon_view(records: list[dict], prefix: str = "act_") -> dict:
    """
    Average edge at every horizon in WIN_RATE_WINDOWS, plus whether they agree.

    A genuine signal tends to point the same way at one, two and three months.
    An effect that appears at exactly one horizon and vanishes at the others is
    almost always the window happening to land on a lucky stretch — so reading
    across all three guards against quoting whichever column flatters the
    result. The horizons are already stored, so this costs nothing.
    """
    per_window = {}
    for w in config.WIN_RATE_WINDOWS:
        edges = _edges(records, w, prefix)
        per_window[w] = {
            "n":        len(edges),
            "avg_edge": (sum(edges) / len(edges)) if edges else None,
        }

    signs = {
        (v["avg_edge"] > 0) for v in per_window.values()
        if v["avg_edge"] is not None and v["n"] >= config.BOOTSTRAP_MIN_SAMPLES
    }
    return {
        "per_window": per_window,
        # None when there isn't enough data at enough horizons to judge.
        "consistent": (len(signs) == 1) if signs else None,
    }


def performance_summary(window: int | None = None) -> dict:
    """
    Aggregate scored history by alert tier and by conviction-score bucket.

    The tier breakdown says which signal types earn their place; the score
    breakdown says whether the conviction score is calibrated — if the 70+
    bucket does not beat the 0-40 bucket, the weights in config are wrong.
    """
    window  = window or config.WIN_RATE_PRIMARY
    all_records = load_history()

    # Records written before direction tracking cannot be scored as right or
    # wrong — a sell alert and a buy alert with the same excess mean opposite
    # things. Excluding them is the only honest option; counting them would
    # invert every sell.
    records = [r for r in all_records if "direction" in r]
    legacy  = len(all_records) - len(records)

    by_tier = {}
    for tier in sorted({r["tier"] for r in records}):
        by_tier[tier] = _aggregate([r for r in records if r["tier"] == tier], window)

    by_direction = {}
    for d in sorted({r["direction"] for r in records}):
        by_direction[d] = _aggregate([r for r in records if r["direction"] == d], window)

    by_bucket = {}
    for label, lo, hi in SCORE_BUCKETS:
        in_bucket = [r for r in records if lo <= r.get("score", 0) < hi]
        by_bucket[label] = _aggregate(in_bucket, window)

    # Only cross-signals have an insider leg, so seniority is judged on those
    # alone — mixing in congress-only alerts would swamp the comparison.
    cross = [r for r in records if r["tier"] == "cross_cluster"]
    by_seniority = {
        "CEO/CFO":     _aggregate([r for r in cross if r.get("has_top_insider")], window),
        "other/dir.":  _aggregate([r for r in cross if not r.get("has_top_insider")], window),
    }

    # The comparison that decides whether the detectors earn their keep:
    # alerted trades against un-alerted ones from the same fetches.
    control = [r for r in load_history(config.CONTROL_FILE) if "direction" in r]
    vs_control = {
        "alerted":   _aggregate(records, window),
        "un-alerted": _aggregate(control, window),
    }

    # Significance, always on the actionable baseline — the only one that speaks
    # to whether the monitor is worth running.
    alert_edges   = _edges(records, window)
    control_edges = _edges(control, window)
    significance = {
        "alerted":    bootstrap(alert_edges, seed="alerted"),
        "un-alerted": bootstrap(control_edges, seed="control"),
        "difference": bootstrap_difference(alert_edges, control_edges, seed="diff"),
    }

    unmatured = sum(1 for r in records if f"edge_{window}" not in r)
    return {
        "window":       window,
        "significance": significance,
        "horizons":     horizon_view(records),
        "total":        len(records),
        "unmatured":    unmatured,
        "legacy":       legacy,
        "by_tier":      by_tier,
        "by_direction": by_direction,
        "by_bucket":    by_bucket,
        "by_seniority": by_seniority,
        "vs_control":   vs_control,
    }


def format_summary(summary: dict) -> str:
    """Render performance_summary() as plain text for the console and digest."""
    lines = [
        f"Alert performance vs SPY over {summary['window']} days",
        f"  {summary['total']} recorded · {summary['unmatured']} still maturing",
        "  Edge = how far the signal was right (sells inverted, so higher is better)",
        "  trade-date = from the politician's trade · actionable = from when the alert reached you",
        "  Only 'actionable' says whether the monitor is worth running.",
    ]
    if summary.get("legacy"):
        lines.append(f"  {summary['legacy']} pre-direction record(s) excluded as unscoreable")
    lines.append("")

    def rows(title: str, group: dict) -> None:
        lines.append(f"  {title}")
        if not group:
            lines.append("    (no records yet)")
            return
        for name, s in group.items():
            if not s["n"]:
                lines.append(f"    {name:<16} no matured alerts yet")
                continue
            act = s.get("actionable", {})
            act_str = (
                f"actionable {act['hit_rate']:.0%} / {act['avg_edge']:+.1f}%"
                if act.get("n") else "actionable pending"
            )
            lines.append(
                f"    {name:<16} {s['n']:>3} scored · "
                f"trade-date {s['hit_rate']:.0%} / {s['avg_edge']:+.1f}% · "
                f"{act_str}"
            )

    rows("Alerted vs un-alerted (does the monitor beat picking at random?)",
         summary["vs_control"])
    lines.append("")
    rows("By tier", summary["by_tier"])
    lines.append("")
    rows("By direction", summary["by_direction"])
    lines.append("")
    rows("By conviction score", summary["by_bucket"])
    lines.append("")
    rows("Cross-signals by insider seniority", summary["by_seniority"])

    # ── Significance ──
    lines += ["", "  Is the actionable edge real, or luck?"]
    labels = {
        "alerted":    "alerted",
        "un-alerted": "un-alerted",
        "difference": "alerted − un-alerted",
    }
    for key, label in labels.items():
        b = summary["significance"].get(key)
        if not b:
            lines.append(f"    {label:<21} too few scored alerts to say "
                         f"(need {config.BOOTSTRAP_MIN_SAMPLES}+)")
            continue
        verdict = (
            "REAL — interval clears zero" if b["ci_low"] > 0 else
            "negative — interval below zero" if b["ci_high"] < 0 else
            "no evidence — interval straddles zero"
        )
        lines.append(
            f"    {label:<21} {b['mean']:+.1f}% "
            f"(95% CI {b['ci_low']:+.1f}% to {b['ci_high']:+.1f}%) · {verdict}"
        )
    lines.append("    The last row is the verdict on the detectors themselves.")

    # ── Horizon consistency ──
    h = summary["horizons"]
    lines += ["", "  Does the edge hold across horizons? (real signals do)"]
    for w, s in h["per_window"].items():
        if s["avg_edge"] is None:
            lines.append(f"    {w:>3}d  no matured alerts yet")
        else:
            lines.append(f"    {w:>3}d  {s['avg_edge']:+.1f}% avg edge ({s['n']} scored)")
    if h["consistent"] is None:
        lines.append("    Not enough data at enough horizons to judge yet.")
    elif h["consistent"]:
        lines.append("    Consistent — same direction at every horizon.")
    else:
        lines.append("    MIXED — the edge flips sign between horizons, which usually "
                     "means noise rather than a real effect.")

    lines.append("")
    lines.append("  Treat any row under ~20 scored alerts as noise, not a verdict.")
    return "\n".join(lines)


# ── Main (standalone) ─────────────────────────────────────────────────────────

def main():
    """Score any matured records and print the performance summary."""
    print("Scoring alert history and control group against SPY...")
    score_all()
    print()
    print(format_summary(performance_summary()))


if __name__ == "__main__":
    main()
