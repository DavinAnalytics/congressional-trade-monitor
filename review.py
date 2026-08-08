"""
review.py — Congressional Trade Monitor
Turns the performance log into plain-English findings and concrete actions.

history.py answers "what happened". This answers "so what should change", which
is the question a table of percentages leaves you to work out for yourself at
exactly the moment you are least inclined to.

Every finding states the evidence in words, and anything actionable carries a
copy-pasteable instruction so acting on it is one step rather than a research
project.

Public interface:
  build_recommendations(window) -> list[Finding]
  format_findings(findings) -> str
"""

from dataclasses import dataclass

import config
import history


# ── Findings ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str          # "good" | "watch" | "action" | "pending"
    headline: str
    detail: str            # plain English, no jargon
    action: str = ""       # copy-paste instruction, empty when nothing to do


SEVERITY_MARK = {"good": "✅", "watch": "⚠️", "action": "🔴", "pending": "⏳"}


def _verdict(b: dict | None) -> str:
    """Classify a bootstrap interval: clears zero, below zero, or straddles it."""
    if b is None:
        return "pending"
    if b["ci_low"] > 0:
        return "positive"
    if b["ci_high"] < 0:
        return "negative"
    return "unproven"


def _ci(b: dict) -> str:
    return f"{b['mean']:+.1f}% (95% confidence: {b['ci_low']:+.1f}% to {b['ci_high']:+.1f}%)"


# ── Rules ─────────────────────────────────────────────────────────────────────

def _overall(summary: dict) -> Finding:
    """The headline question: do alerts beat trades that never alerted?"""
    diff = summary["significance"].get("difference")
    v = _verdict(diff)

    if v == "pending":
        n = summary["significance"].get("alerted")
        have = n["mean"] if n else None
        return Finding(
            "pending",
            "Not enough matured alerts to judge the monitor yet",
            "An alert has to age 60 days before its outcome is known, and the "
            "comparison needs at least a handful on both sides. Nothing to decide "
            "this month — the data is still arriving."
            + ("" if have is None else " Early alerts are trending, but that is not evidence yet."),
        )

    if v == "positive":
        return Finding(
            "good",
            "The monitor is beating the baseline",
            f"Alerted trades outperformed un-alerted congressional trades by "
            f"{_ci(diff)} over {summary['window']} days, measured from the day each "
            f"alert reached you. The confidence range sits entirely above zero, so "
            f"this is unlikely to be luck. The detectors are earning their place.",
        )

    if v == "negative":
        return Finding(
            "action",
            "Alerted trades are doing WORSE than un-alerted ones",
            f"Alerts underperformed the trades that did not alert by {_ci(diff)}. "
            f"The whole range is below zero, so this is not noise — the detectors "
            f"are actively selecting worse trades than picking at random from the "
            f"same disclosures. Something in the alert logic is inverted or "
            f"mis-specified.",
            action="investigate why alerted trades underperform un-alerted ones",
        )

    return Finding(
        "watch",
        "No evidence yet that the monitor beats picking at random",
        f"Alerts came in at {_ci(diff)} relative to un-alerted trades. The range "
        f"crosses zero, which means the difference is within what chance produces "
        f"— it is neither proof it works nor proof it doesn't. Keep collecting; "
        f"the range narrows as alerts accumulate.",
    )


def _horizons(summary: dict) -> Finding | None:
    h = summary["horizons"]
    if h["consistent"] is None:
        return None

    per = ", ".join(
        f"{w}d {s['avg_edge']:+.1f}%"
        for w, s in h["per_window"].items() if s["avg_edge"] is not None
    )
    if h["consistent"]:
        return Finding(
            "good",
            "The edge holds at every time horizon",
            f"Average edge was {per}. A real signal points the same way at one, two "
            f"and three months, and this one does. That is a meaningful check "
            f"against a result that only exists because one window happened to land "
            f"on a lucky stretch.",
        )
    return Finding(
        "watch",
        "The edge flips sign between time horizons",
        f"Average edge was {per}. A genuine signal is normally pointing the same "
        f"way at all three; one that appears at a single horizon and reverses at "
        f"the others is usually the window landing on a lucky patch rather than a "
        f"real effect. Treat any positive headline above with extra suspicion "
        f"until this settles.",
    )


def _tiers(records: list[dict], window: int) -> list[Finding]:
    """Per-tier verdicts, so a dud alert type can be switched off on evidence."""
    findings = []
    for tier in sorted({r["tier"] for r in records}):
        edges = history._edges([r for r in records if r["tier"] == tier], window)
        if len(edges) < config.REC_MIN_SAMPLES:
            continue

        b = history.bootstrap(edges, seed=f"tier-{tier}")
        v = _verdict(b)
        label = config.TIER_LABELS.get(tier, tier)

        if v == "negative":
            findings.append(Finding(
                "action",
                f"{label} alerts are losing money",
                f"{len(edges)} matured {label} alerts averaged {_ci(b)}. The whole "
                f"confidence range is below zero, so this tier is reliably picking "
                f"losers rather than merely being unhelpful.",
                action=f"disable the {tier} alert tier "
                       f"({len(edges)} alerts, {b['mean']:+.1f}% edge, "
                       f"CI {b['ci_low']:+.1f}% to {b['ci_high']:+.1f}%)",
            ))
        elif v == "positive":
            findings.append(Finding(
                "good",
                f"{label} alerts are pulling their weight",
                f"{len(edges)} matured {label} alerts averaged {_ci(b)}, entirely "
                f"above zero. Worth keeping, and worth loosening its threshold if "
                f"you want more of them.",
            ))
        else:
            findings.append(Finding(
                "watch",
                f"{label} alerts show no clear effect",
                f"{len(edges)} matured {label} alerts averaged {_ci(b)}. The range "
                f"crosses zero, so there is no evidence this tier helps or hurts. "
                f"Leave it alone and let the sample grow.",
            ))
    return findings


def _direction(records: list[dict], window: int) -> Finding | None:
    """Congressional selling is often tax- or liquidity-driven, not informational."""
    sells = history._edges([r for r in records if r.get("direction") == "sell"], window)
    if len(sells) < config.REC_MIN_SAMPLES:
        return None

    b = history.bootstrap(sells, seed="sells")
    if _verdict(b) != "negative":
        return None

    return Finding(
        "action",
        "Sell alerts are not predicting anything useful",
        f"{len(sells)} matured sell alerts averaged {_ci(b)}. Members sold and the "
        f"stock then did fine — which fits the usual explanation that congressional "
        f"selling is mostly tax and liquidity driven rather than a view on the "
        f"company. Sells are currently the bulk of cluster alerts, so dropping them "
        f"would cut volume sharply.",
        action=f"stop alerting on sell clusters "
               f"({len(sells)} alerts, {b['mean']:+.1f}% edge, "
               f"CI {b['ci_low']:+.1f}% to {b['ci_high']:+.1f}%)",
    )


def _seniority(records: list[dict], window: int) -> Finding | None:
    """Was widening the insider screener to directors worth it?"""
    cross = [r for r in records if r["tier"] == "cross_cluster"]
    top   = history._edges([r for r in cross if r.get("has_top_insider")], window)
    other = history._edges([r for r in cross if not r.get("has_top_insider")], window)
    if len(top) < config.REC_MIN_SAMPLES or len(other) < config.REC_MIN_SAMPLES:
        return None

    d = history.bootstrap_difference(top, other, seed="seniority")
    if d is None or d["ci_low"] <= 0:
        return None

    return Finding(
        "action",
        "CEO/CFO cross-signals clearly beat director-only ones",
        f"Cross-signals involving a CEO or CFO outperformed those with only "
        f"directors and lower officers by {_ci(d)}. The screener was widened to "
        f"include directors to grow the funnel; on this evidence that widening is "
        f"diluting the signal rather than helping.",
        action=f"narrow the insider screener back to CEO/CFO only "
               f"({len(top)} CEO/CFO vs {len(other)} director alerts, "
               f"difference {d['mean']:+.1f}%)",
    )


def _calibration(summary: dict) -> Finding | None:
    """If high-conviction alerts don't beat low ones, the weights are wrong."""
    hi = summary["by_bucket"].get("70+", {}).get("actionable", {})
    lo = summary["by_bucket"].get("0-40", {}).get("actionable", {})
    if not hi.get("n") or not lo.get("n"):
        return None
    if hi["n"] < config.REC_MIN_SAMPLES or lo["n"] < config.REC_MIN_SAMPLES:
        return None

    if hi["avg_edge"] > lo["avg_edge"]:
        return Finding(
            "good",
            "The conviction score is ranking alerts correctly",
            f"High-conviction alerts (70+) averaged {hi['avg_edge']:+.1f}% against "
            f"{lo['avg_edge']:+.1f}% for low-conviction ones (0-40). The score is "
            f"sorting good signals from weak ones, which is what it exists to do.",
        )

    return Finding(
        "action",
        "The conviction score is not sorting alerts correctly",
        f"High-conviction alerts (70+) averaged {hi['avg_edge']:+.1f}% while "
        f"low-conviction ones (0-40) averaged {lo['avg_edge']:+.1f}%. The score is "
        f"supposed to rank alerts by how much they are worth acting on, and right "
        f"now it isn't — the weights are mis-specified, not the alerts themselves.",
        action=f"re-weight the conviction score "
               f"(70+ bucket {hi['avg_edge']:+.1f}% over {hi['n']} alerts vs "
               f"0-40 bucket {lo['avg_edge']:+.1f}% over {lo['n']})",
    )


# ── Assembly ──────────────────────────────────────────────────────────────────

def build_recommendations(window: int | None = None) -> list[Finding]:
    """Every finding the current data supports, most important first."""
    window  = window or config.WIN_RATE_PRIMARY
    summary = history.performance_summary(window)
    records = [r for r in history.load_history() if "direction" in r]

    findings = [_overall(summary)]
    for f in (
        _horizons(summary),
        _direction(records, window),
        _seniority(records, window),
        _calibration(summary),
    ):
        if f:
            findings.append(f)
    findings += _tiers(records, window)

    return findings


def format_findings(findings: list[Finding]) -> str:
    """Render findings as plain text for the console and the monthly email."""
    lines = []
    for f in findings:
        lines.append(f"{SEVERITY_MARK.get(f.severity, '·')}  {f.headline}")
        lines.append(f"    {f.detail}")
        if f.action:
            lines.append(f"    → To act on this, send me: \"{f.action}\"")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Main (standalone) ─────────────────────────────────────────────────────────

def main():
    print("Scoring history, then reviewing...\n")
    history.score_all()
    print(format_findings(build_recommendations()))
    print()
    print(history.format_summary(history.performance_summary()))


if __name__ == "__main__":
    main()
