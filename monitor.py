"""
monitor.py — Congressional Trade Monitor
Main polling loop. Runs forever, waking every POLL_INTERVAL_SECONDS to:
  1. Fetch fresh trades from both chambers (fetcher.py)
  2. Analyze for signals (analyzer.py)
  3. Send email alerts for anything new (notifier.py)

Usage:
  ./.venv/bin/python monitor.py            # run forever
  ./.venv/bin/python monitor.py --once     # single poll then exit (good for cron/testing)
  ./.venv/bin/python monitor.py --summary  # send daily digest then exit
"""

import sys
import time
import argparse
from datetime import datetime, timedelta

import config
import export
import history
import review
from fetcher            import fetch_all
from openinsider_fetcher import fetch_all as fetch_insider, InsiderFetchError
from analyzer           import analyze, analyze_cross_cluster, compute_win_rates, enrich_and_score
from notifier           import send_digest, send_summary, send_monthly_review
from committees         import load_all as load_committees


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _banner(msg: str) -> None:
    width = 60
    print(f"\n{'═'*width}")
    print(f"  {msg}")
    print(f"{'═'*width}")


# ── Single poll cycle ─────────────────────────────────────────────────────────

def poll(wide: bool = False) -> tuple[list, list, dict, list]:
    """
    Run one full fetch → analyze → alert cycle.

    Args:
        wide: if True, fetch LEADERBOARD_DAYS for win-rate scoring.
              if False, fetch FETCH_DAYS only (faster, for routine polls).

    Returns:
        (alerts, trades, win_rates, insider_trades)
    """
    _banner(f"Poll started — {_now()}")

    # Load committee assignments (cached after first call)
    print("\nLoading committee assignments...")
    load_committees()

    # Fetch — use wider window on first run or when wide=True
    fetch_days = 180 if wide else config.FETCH_DAYS
    print(f"\nFetching trades (last {fetch_days} days)...")
    # Collected here rather than at the insider fetch below: a chamber can go
    # dark too, and the digest has to say so.
    warnings: list[str] = []
    all_trades = fetch_all(days=fetch_days, warnings=warnings)

    # For alerts, only look at the recent window
    if wide:
        cutoff = datetime.now() - timedelta(days=config.FETCH_DAYS)
        recent = [
            t for t in all_trades
            if datetime.strptime(t["transaction_date"], "%Y-%m-%d") >= cutoff
        ]
        print(f"  {len(recent)} trades in alert window (last {config.FETCH_DAYS} days)")
    else:
        recent = all_trades

    # Fetch insider open-market buys for cross-cluster detection.
    # A feed outage must not masquerade as "no insiders bought anything" — carry
    # on with the congressional alerts, but say plainly that cross-signals could
    # not run this cycle.
    print("\nFetching insider buys...")
    try:
        insider_trades = fetch_insider(days=config.FETCH_DAYS)
    except InsiderFetchError as e:
        insider_trades = []
        warnings.append(
            "Insider feed unavailable this run — cross-signals could not be detected. "
            "Congressional alerts below are unaffected."
        )
        print(f"  ⚠ {e}")
        print("  ⚠ CROSS-SIGNAL DETECTION DISABLED FOR THIS RUN")

    # Analyze
    print("\nAnalyzing...")
    alerts = analyze(recent)

    # 🔗 Cross-cluster — tickers bought by both Congress and a company insider
    print("\nDetecting cross-cluster alerts...")
    cross_alerts = analyze_cross_cluster(recent, insider_trades)

    # Win rates feed the conviction score, so they must be computed before ranking.
    win_rates = compute_win_rates(all_trades)

    # analyze() already scored its own alerts; score the cross-cluster ones, then
    # rank the merged list so the digest leads with the strongest signal.
    for alert in cross_alerts:
        enrich_and_score(alert, win_rates)
    all_alerts = sorted(alerts + cross_alerts, key=lambda a: a.score, reverse=True)

    # Send one ranked digest
    print("\nSending digest...")
    send_digest(all_alerts, warnings)

    # Record what fired so its forward performance can be measured later, plus a
    # sample of trades that did NOT fire — without that baseline an alert hit
    # rate cannot be told apart from the base rate of congressional trading.
    history.record_alerts(all_alerts)
    history.record_control(recent, all_alerts)

    _banner(f"Poll complete — {len(all_alerts)} alert(s) — {_now()}")
    return all_alerts, all_trades, win_rates, insider_trades


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_forever() -> None:
    """
    Poll on startup, then sleep POLL_INTERVAL_SECONDS between polls.
    First poll uses wide window (180 days) to build win-rate baseline.
    Subsequent polls use FETCH_DAYS only for speed.
    """
    print(f"""
╔══════════════════════════════════════════════════════════╗
║         Congressional Trade Monitor — RUNNING           ║
║  Poll interval : every {config.POLL_INTERVAL_SECONDS//3600}h {(config.POLL_INTERVAL_SECONDS%3600)//60:02d}m                          ║
║  Alert window  : last {config.FETCH_DAYS} days                          ║
║  Watchlist     : {len(config.WATCHLIST)} members                              ║
║  Press Ctrl+C to stop                                    ║
╚══════════════════════════════════════════════════════════╝
    """)

    first_run = True
    last_daily_digest = None

    while True:
        try:
            alerts, trades, win_rates, _insider = poll(wide=first_run)
            first_run = False

            # Send daily digest once per day around 8 AM
            now = datetime.now()
            if (
                last_daily_digest is None or
                (now - last_daily_digest).days >= 1
            ) and now.hour >= 8:
                print("\nSending daily digest...")
                send_summary(alerts, trades)
                last_daily_digest = now

        except KeyboardInterrupt:
            print("\n\nMonitor stopped by user. Goodbye.")
            sys.exit(0)
        except Exception as e:
            print(f"\n⚠ Poll error: {e}")
            print("  Sleeping 5 minutes before retry...")
            time.sleep(300)
            continue

        # Sleep until next poll
        next_poll = datetime.now() + timedelta(seconds=config.POLL_INTERVAL_SECONDS)
        print(f"\n  Next poll: {next_poll.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Sleeping {config.POLL_INTERVAL_SECONDS // 3600}h "
              f"{(config.POLL_INTERVAL_SECONDS % 3600) // 60:02d}m ...")

        try:
            time.sleep(config.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\n\nMonitor stopped by user. Goodbye.")
            sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Congressional Trade Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./.venv/bin/python monitor.py               Run forever, polling every 4 hours
  ./.venv/bin/python monitor.py --once        Single poll, print alerts, exit
  ./.venv/bin/python monitor.py --once --export   ...and rebuild the static dashboard
  ./.venv/bin/python monitor.py --summary     Send weekly digest email, exit
  ./.venv/bin/python monitor.py --monthly     Email the monthly "is this working?" review, exit
  ./.venv/bin/python monitor.py --performance Score past alerts against SPY, print, exit
  ./.venv/bin/python monitor.py --reset-state Clear the seen-trades memory, exit
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Send a daily digest email and exit",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear seen_trades.json (Gist or local file) and exit — next run re-alerts all recent trades",
    )
    parser.add_argument(
        "--performance",
        action="store_true",
        help="Score past alerts against SPY and print the performance summary, then exit",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Also rebuild the static dashboard (site/index.html) after the poll",
    )
    parser.add_argument(
        "--monthly",
        action="store_true",
        help="Email the monthly review: is the monitor actually working? Then exit",
    )
    args = parser.parse_args()

    if args.performance:
        _banner("Alert performance")
        history.score_all()
        print()
        print(review.format_findings(review.build_recommendations()))
        print()
        print(history.format_summary(history.performance_summary()))
        return

    if args.monthly:
        # No fetch — this reads the logs the daily runs already wrote, so it is
        # cheap enough to run straight after the normal poll.
        _banner("Monthly review")
        history.score_all()
        findings = review.build_recommendations()
        performance = history.format_summary(history.performance_summary())
        print(review.format_findings(findings))
        send_monthly_review(findings, performance)
        print("\n✓ Monthly review sent.")
        return

    if args.reset_state:
        from analyzer import _save_seen, _gist_enabled
        _banner("Resetting seen-state")
        _save_seen(set())
        where = "GitHub Gist" if _gist_enabled() else f"local file ({config.SEEN_TRADES_FILE})"
        print(f"\n✓ Seen-state cleared in {where}.")
        print("  The next run will treat all recent trades as new and re-alert them once.")
        return

    if args.summary:
        _banner("Sending daily digest")
        alerts, trades, win_rates, insider = poll(wide=True)
        # Score any alerts whose forward window has now elapsed, so the weekly
        # email can report whether past signals actually beat SPY.
        history.score_all()
        performance = history.format_summary(history.performance_summary())
        send_summary(alerts, trades, performance)
        print("\n✓ Digest sent.")
        if args.export:
            export.build(alerts, trades, insider, win_rates)

    elif args.once:
        _banner("Single poll mode")
        alerts, trades, win_rates, insider = poll(wide=True)
        if not alerts:
            print("\n  No new alerts this cycle.")
        else:
            print(f"\n  {len(alerts)} alert(s), ranked by conviction:")
            for i, a in enumerate(alerts, 1):
                print(f"\n  #{i} [{a.score:.0f}/100] {a.message}")
        if args.export:
            export.build(alerts, trades, insider, win_rates)

    else:
        run_forever()


if __name__ == "__main__":
    main()