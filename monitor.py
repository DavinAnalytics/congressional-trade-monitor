"""
monitor.py — Congressional Trade Monitor
Main polling loop. Runs forever, waking every POLL_INTERVAL_SECONDS to:
  1. Fetch fresh trades from both chambers (fetcher.py)
  2. Analyze for signals (analyzer.py)
  3. Send email alerts for anything new (notifier.py)

Usage:
  python monitor.py            # run forever
  python monitor.py --once     # single poll then exit (good for cron/testing)
  python monitor.py --summary  # send daily digest then exit
"""

import sys
import time
import argparse
from datetime import datetime, timedelta

import config
from fetcher            import fetch_all
from openinsider_fetcher import fetch_all as fetch_insider
from analyzer           import analyze, analyze_cross_cluster, compute_win_rates, filter_new_trades
from notifier           import send_alerts, send_summary
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

def poll(wide: bool = False) -> tuple[list, list, dict]:
    """
    Run one full fetch → analyze → alert cycle.

    Args:
        wide: if True, fetch LEADERBOARD_DAYS for win-rate scoring.
              if False, fetch FETCH_DAYS only (faster, for routine polls).

    Returns:
        (alerts, trades, win_rates)
    """
    _banner(f"Poll started — {_now()}")

    # Load committee assignments (cached after first call)
    print("\nLoading committee assignments...")
    load_committees()

    # Fetch — use wider window on first run or when wide=True
    fetch_days = 180 if wide else config.FETCH_DAYS
    print(f"\nFetching trades (last {fetch_days} days)...")
    all_trades = fetch_all(days=fetch_days)

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

    # Fetch insider (CEO/CFO) open-market buys for cross-cluster detection
    print("\nFetching insider buys...")
    insider_trades = fetch_insider(days=config.FETCH_DAYS)

    # Analyze
    print("\nAnalyzing...")
    alerts = analyze(recent)

    # 🔗 Cross-cluster — tickers bought by both Congress and a CEO/CFO
    print("\nDetecting cross-cluster alerts...")
    cross_alerts = analyze_cross_cluster(recent, insider_trades)

    all_alerts = alerts + cross_alerts

    # Win rates (for notifier formatting)
    win_rates = compute_win_rates(all_trades)

    # Send alerts
    print("\nSending alerts...")
    send_alerts(all_alerts, win_rates)

    _banner(f"Poll complete — {len(all_alerts)} alert(s) — {_now()}")
    return all_alerts, all_trades, win_rates


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
            alerts, trades, win_rates = poll(wide=first_run)
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
  python monitor.py               Run forever, polling every 4 hours
  python monitor.py --once        Single poll, print alerts, exit
  python monitor.py --summary     Send daily digest email, exit
  python monitor.py --reset-state Clear the seen-trades memory, exit
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
    args = parser.parse_args()

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
        alerts, trades, _ = poll(wide=True)
        send_summary(alerts, trades)
        print("\n✓ Digest sent.")

    elif args.once:
        _banner("Single poll mode")
        alerts, trades, win_rates = poll(wide=True)
        if not alerts:
            print("\n  No new alerts this cycle.")
        else:
            print(f"\n  {len(alerts)} alert(s):")
            for a in alerts:
                print(f"\n  {a.message}")

    else:
        run_forever()


if __name__ == "__main__":
    main()