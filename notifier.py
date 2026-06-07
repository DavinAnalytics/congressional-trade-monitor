"""
notifier.py — Congressional Trade Monitor
Formats Alert objects into emails and sends them via Gmail SMTP.

Each alert tier gets its own email template:
  🔴 Cluster  — urgent, detailed member list + trade table
  🟡 Win-Rate — highlights member's track record
  🟢 Watchlist — clean single-trade summary

Public interface:
  send_alerts(alerts) -> None
  send_summary(alerts, trades) -> None  (daily digest, optional)
"""

import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import config
from analyzer import Alert


# ── Email transport ───────────────────────────────────────────────────────────

def _send_email(subject: str, body_text: str, body_html: str) -> bool:
    """
    Send an email via Gmail SMTP. Returns True on success, False on failure.
    Uses TLS (port 587). Requires an App Password in config.py.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_SENDER
    msg["To"]      = ", ".join(config.EMAIL_RECIPIENTS)

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            server.sendmail(
                config.EMAIL_SENDER,
                config.EMAIL_RECIPIENTS,
                msg.as_string(),
            )
        print(f"  ✓ Email sent: {subject}")
        return True
    except Exception as e:
        print(f"  ✗ Email failed: {e}")
        traceback.print_exc()
        return False


# ── Template helpers ──────────────────────────────────────────────────────────

def _trade_rows_html(trades: list[dict]) -> str:
    """Render a list of trades as an HTML table body."""
    rows = ""
    for t in trades:
        tx_type = t["type"].replace("_", " ").title()
        owner   = f" ({t['owner']})" if t.get("owner") else ""
        rows += f"""
        <tr>
          <td style="padding:6px 12px;">{t['representative']}{owner}</td>
          <td style="padding:6px 12px;font-weight:bold;">{t['ticker']}</td>
          <td style="padding:6px 12px;">{tx_type}</td>
          <td style="padding:6px 12px;">{t['transaction_date']}</td>
          <td style="padding:6px 12px;">{t['amount']}</td>
          <td style="padding:6px 12px;">
            <a href="{t['ptr_link']}" style="color:#2563eb;">Filing ↗</a>
          </td>
        </tr>"""
    return rows


def _trade_rows_text(trades: list[dict]) -> str:
    """Render a list of trades as plain text."""
    lines = []
    for t in trades:
        tx_type = t["type"].replace("_", " ").upper()
        owner   = f" ({t['owner']})" if t.get("owner") else ""
        lines.append(
            f"  {t['representative']}{owner} | {t['ticker']} {tx_type} | "
            f"{t['transaction_date']} | {t['amount']}"
        )
    return "\n".join(lines)


def _base_html(title: str, accent: str, body: str) -> str:
    """Wrap content in a clean, minimal HTML email shell."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
    <div style="background:{accent};padding:20px 28px;">
      <p style="margin:0;color:rgba(255,255,255,.8);font-size:12px;text-transform:uppercase;letter-spacing:.08em;">
        Congressional Trade Monitor
      </p>
      <h1 style="margin:4px 0 0;color:#fff;font-size:22px;font-weight:600;">{title}</h1>
    </div>
    <div style="padding:24px 28px;">
      {body}
    </div>
    <div style="padding:16px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;">
      <p style="margin:0;font-size:12px;color:#9ca3af;">
        Congressional Trade Monitor · {datetime.now().strftime("%B %d, %Y %H:%M")} ·
        Data from efdsearch.senate.gov and disclosures-clerk.house.gov
      </p>
    </div>
  </div>
</body>
</html>"""


# ── Alert formatters ──────────────────────────────────────────────────────────

def _format_cluster(alert: Alert) -> tuple[str, str, str]:
    """Format a 🔴 Cluster Alert."""
    members  = sorted({t["representative"] for t in alert.trades})
    n        = len(members)
    direction = "buying" if alert.trades[0]["type"] == "purchase" else "selling"
    dates    = sorted(t["transaction_date"] for t in alert.trades)

    subject = f"🔴 CLUSTER ALERT — {n} members {direction} {alert.ticker}"

    text = (
        f"CLUSTER ALERT\n"
        f"{'='*50}\n"
        f"Ticker:    {alert.ticker}\n"
        f"Signal:    {n} members {direction} within {config.CLUSTER_DAYS} days\n"
        f"Window:    {dates[0]} → {dates[-1]}\n"
        f"Members:   {', '.join(members)}\n\n"
        f"Trades:\n{_trade_rows_text(alert.trades)}\n\n"
        f"This is a Tier 1 signal — strongest alert in the system."
    )

    table_rows = _trade_rows_html(alert.trades)
    body = f"""
      <p style="font-size:15px;color:#111;margin:0 0 16px;">
        <strong>{n} members of Congress</strong> are {direction}
        <strong>{alert.ticker}</strong> within a {config.CLUSTER_DAYS}-day window.
        This is the strongest signal in the monitor.
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:8px 12px;">Member</th>
            <th style="padding:8px 12px;">Ticker</th>
            <th style="padding:8px 12px;">Type</th>
            <th style="padding:8px 12px;">Date</th>
            <th style="padding:8px 12px;">Amount</th>
            <th style="padding:8px 12px;">Filing</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <p style="margin:20px 0 0;font-size:13px;color:#6b7280;">
        Cluster window: {config.CLUSTER_DAYS} days · Minimum members: {config.CLUSTER_MIN_MEMBERS}
      </p>"""

    html = _base_html(
        title  = f"🔴 Cluster — {alert.ticker}",
        accent = "#dc2626",
        body   = body,
    )
    return subject, text, html


def _format_winrate(alert: Alert, win_rates: dict) -> tuple[str, str, str]:
    """Format a 🟡 Win-Rate Alert with committee conflict context."""
    from committees import flag_conflicts, get_member_committees

    trade   = alert.trades[0]
    member  = trade["representative"]
    ticker  = trade["ticker"]
    stats   = win_rates.get(member, {})
    wr      = stats.get("win_rate", 0)
    wins    = stats.get("wins", 0)
    total   = stats.get("total", 0)
    tx_type = trade["type"].replace("_", " ").title()

    # Committee conflict context
    conflicts   = flag_conflicts(member, ticker)
    member_data = get_member_committees(member)

    # Plain text conflict lines
    if conflicts:
        conflict_text = "\nCommittee Conflicts:\n" + "\n".join(f"  ⚠ {c}" for c in conflicts)
    elif member_data:
        conflict_text = f"\nCommittee Conflicts: None flagged for {ticker}"
    else:
        conflict_text = "\nCommittee Conflicts: No committee data available"

    subject = (
        f"🟡 HIGH WIN-RATE — {member} · "
        f"{ticker} {tx_type.upper()}"
    )

    text = (
        f"WIN-RATE ALERT\n"
        f"{'='*50}\n"
        f"Member:    {member}\n"
        f"Win Rate:  {wr:.0%} ({wins}/{total} trades beat SPY "
        f"over {config.WIN_RATE_PRIMARY} days)\n\n"
        f"New Trade:\n{_trade_rows_text([trade])}"
        f"{conflict_text}\n\n"
        f"Filing: {trade['ptr_link']}"
    )

    # HTML conflict block
    if conflicts:
        conflict_items = "".join(
            f'<li style="margin:4px 0;font-size:13px;color:#374151;">⚠ {c}</li>'
            for c in conflicts
        )
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:14px 16px;background:#fff7ed;border-radius:6px;
                  border-left:3px solid #ea580c;">
        <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#9a3412;">
          ⚠ Potential Committee Conflicts
        </p>
        <ul style="margin:0;padding:0 0 0 16px;">{conflict_items}</ul>
        <p style="margin:8px 0 0;font-size:11px;color:#9ca3af;">
          These committees have oversight authority relevant to {ticker}'s sector.
        </p>
      </div>"""
    elif member_data:
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:12px 16px;background:#f0fdf4;border-radius:6px;
                  border-left:3px solid #86efac;">
        <p style="margin:0;font-size:13px;color:#166534;">
          ✓ No committee conflicts flagged for {ticker}
        </p>
      </div>"""
    else:
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:12px 16px;background:#f9fafb;border-radius:6px;
                  border-left:3px solid #d1d5db;">
        <p style="margin:0;font-size:13px;color:#6b7280;">
          Committee data unavailable for this member
        </p>
      </div>"""

    table_rows = _trade_rows_html([trade])
    body = f"""
      <p style="font-size:15px;color:#111;margin:0 0 16px;">
        <strong>{member}</strong> has a <strong>{wr:.0%} historical win rate</strong>
        ({wins}/{total} purchases beat SPY over {config.WIN_RATE_PRIMARY} days)
        and just filed a new trade.
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:8px 12px;">Member</th>
            <th style="padding:8px 12px;">Ticker</th>
            <th style="padding:8px 12px;">Type</th>
            <th style="padding:8px 12px;">Date</th>
            <th style="padding:8px 12px;">Amount</th>
            <th style="padding:8px 12px;">Filing</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <div style="margin:16px 0 0;padding:14px 16px;background:#fefce8;border-radius:6px;
                  border-left:4px solid #eab308;">
        <p style="margin:0;font-size:13px;color:#713f12;">
          Win rate threshold: {config.WIN_RATE_MIN:.0%} ·
          Min scored trades: {config.WIN_RATE_MIN_TRADES} ·
          Benchmark window: {config.WIN_RATE_PRIMARY}d vs SPY
        </p>
      </div>
      {conflict_html}"""

    html = _base_html(
        title  = f"🟡 High Win-Rate — {member}",
        accent = "#d97706",
        body   = body,
    )
    return subject, text, html


def _format_watchlist(alert: Alert, win_rates: dict) -> tuple[str, str, str]:
    """Format a 🟢 Watchlist Alert with win-rate and committee conflict context."""
    from committees import flag_conflicts, get_member_committees

    trade   = alert.trades[0]
    member  = trade["representative"]
    ticker  = trade["ticker"]
    tx_type = trade["type"].replace("_", " ").title()
    owners  = sorted({t.get("owner", "") for t in alert.trades if t.get("owner")})
    owner_str = f" ({', '.join(owners)})" if owners else ""

    # Win-rate context
    stats = win_rates.get(member, {})
    total = stats.get("total", 0)
    if total >= config.WIN_RATE_MIN_TRADES:
        wr       = stats.get("win_rate", 0)
        wins     = stats.get("wins", 0)
        wr_str   = f"{wr:.0%} win rate ({wins}/{total} trades beat SPY over {config.WIN_RATE_PRIMARY}d)"
        wr_color = "#16a34a" if wr >= config.WIN_RATE_MIN else "#6b7280"
    else:
        wr_str   = f"Insufficient data ({total} scored trades — need {config.WIN_RATE_MIN_TRADES} minimum)"
        wr_color = "#9ca3af"

    # Committee conflict context
    conflicts   = flag_conflicts(member, ticker)
    member_data = get_member_committees(member)
    chamber     = member_data.get("chamber", "")

    subject = (
        f"🟢 WATCHLIST — {member} · "
        f"{ticker} {tx_type.upper()}"
    )

    # Plain text conflict lines
    conflict_text = ""
    if conflicts:
        conflict_text = "\nCommittee Conflicts:\n" + "\n".join(f"  ⚠ {c}" for c in conflicts)
    elif member_data:
        conflict_text = "\nCommittee Conflicts: None flagged for this ticker"
    else:
        conflict_text = "\nCommittee Conflicts: No committee data available"

    text = (
        f"WATCHLIST ALERT\n"
        f"{'='*50}\n"
        f"Member:    {member}{owner_str}\n"
        f"Ticker:    {ticker}\n"
        f"Type:      {tx_type}\n"
        f"Date:      {trade['transaction_date']}\n"
        f"Amount:    {trade['amount']}\n"
        f"Win Rate:  {wr_str}"
        f"{conflict_text}\n\n"
        f"Filing:    {trade['ptr_link']}"
    )

    # HTML conflict block
    if conflicts:
        conflict_items = "".join(
            f'<li style="margin:4px 0;font-size:13px;color:#374151;">⚠ {c}</li>'
            for c in conflicts
        )
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:14px 16px;background:#fff7ed;border-radius:6px;
                  border-left:3px solid #ea580c;">
        <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#9a3412;">
          ⚠ Potential Committee Conflicts
        </p>
        <ul style="margin:0;padding:0 0 0 16px;">{conflict_items}</ul>
        <p style="margin:8px 0 0;font-size:11px;color:#9ca3af;">
          These committees have oversight authority relevant to {ticker}'s sector.
        </p>
      </div>"""
    elif member_data:
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:12px 16px;background:#f0fdf4;border-radius:6px;
                  border-left:3px solid #86efac;">
        <p style="margin:0;font-size:13px;color:#166534;">
          ✓ No committee conflicts flagged for {ticker}
        </p>
      </div>"""
    else:
        conflict_html = f"""
      <div style="margin:12px 0 0;padding:12px 16px;background:#f9fafb;border-radius:6px;
                  border-left:3px solid #d1d5db;">
        <p style="margin:0;font-size:13px;color:#6b7280;">
          Committee data unavailable for this member
        </p>
      </div>"""

    table_rows = _trade_rows_html(alert.trades)
    body = f"""
      <p style="font-size:15px;color:#111;margin:0 0 16px;">
        <strong>{member}</strong> (manually tracked) filed a new trade.
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:8px 12px;">Member</th>
            <th style="padding:8px 12px;">Ticker</th>
            <th style="padding:8px 12px;">Type</th>
            <th style="padding:8px 12px;">Date</th>
            <th style="padding:8px 12px;">Amount</th>
            <th style="padding:8px 12px;">Filing</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <div style="margin:16px 0 0;padding:14px 16px;background:#f9fafb;border-radius:6px;
                  border-left:3px solid {wr_color};">
        <p style="margin:0;font-size:13px;color:#374151;">
          <strong>Historical win rate:</strong> {wr_str}
        </p>
        <p style="margin:6px 0 0;font-size:12px;color:#6b7280;">
          Win rate measures how often purchases beat SPY over {config.WIN_RATE_PRIMARY} days.
        </p>
      </div>
      {conflict_html}"""

    html = _base_html(
        title  = f"🟢 Watchlist — {member}",
        accent = "#16a34a",
        body   = body,
    )
    return subject, text, html


# ── Public interface ──────────────────────────────────────────────────────────

def send_alerts(alerts: list[Alert], win_rates: dict | None = None) -> None:
    """
    Send one email per alert. Called by monitor.py on each poll cycle.
    win_rates dict is passed through for win-rate alert formatting.
    """
    if not alerts:
        print("  No alerts to send.")
        return

    win_rates = win_rates or {}

    for alert in alerts:
        if alert.tier == "cluster":
            subject, text, html = _format_cluster(alert)
        elif alert.tier == "winrate":
            subject, text, html = _format_winrate(alert, win_rates)
        elif alert.tier == "watchlist":
            subject, text, html = _format_watchlist(alert, win_rates)
        else:
            continue

        _send_email(subject, text, html)


def send_summary(alerts: list[Alert], trades: list[dict]) -> None:
    """
    Send a daily digest email summarizing all alerts and trade activity.
    Optional — call from monitor.py once per day if desired.
    """
    now     = datetime.now().strftime("%B %d, %Y")
    n_alert = len(alerts)
    n_trade = len(trades)
    chambers = {t["chamber"] for t in trades}

    subject = f"📊 Daily Digest — {now} · {n_alert} alert(s), {n_trade} trade(s)"

    # Plain text
    text_lines = [
        f"CONGRESSIONAL TRADE MONITOR — Daily Digest",
        f"{now}",
        f"{'='*50}",
        f"Trades fetched:  {n_trade} ({', '.join(sorted(chambers))})",
        f"Alerts fired:    {n_alert}",
        "",
    ]
    if alerts:
        text_lines.append("Alerts:")
        for a in alerts:
            text_lines.append(f"  {a.message}")
    else:
        text_lines.append("No alerts fired today.")
    text = "\n".join(text_lines)

    # HTML
    alert_items = ""
    if alerts:
        for a in alerts:
            color = {"cluster": "#dc2626", "winrate": "#d97706", "watchlist": "#16a34a"}.get(a.tier, "#6b7280")
            alert_items += f"""
            <li style="margin:8px 0;padding:10px 14px;background:#f9fafb;
                        border-left:3px solid {color};border-radius:4px;font-size:14px;">
              {a.message.replace(chr(10), '<br>')}
            </li>"""
    else:
        alert_items = '<li style="color:#6b7280;font-size:14px;">No alerts today.</li>'

    body = f"""
      <div style="display:flex;gap:24px;margin-bottom:20px;">
        <div style="flex:1;padding:16px;background:#f3f4f6;border-radius:6px;text-align:center;">
          <p style="margin:0;font-size:28px;font-weight:700;color:#111;">{n_trade}</p>
          <p style="margin:4px 0 0;font-size:12px;color:#6b7280;text-transform:uppercase;">Trades</p>
        </div>
        <div style="flex:1;padding:16px;background:#f3f4f6;border-radius:6px;text-align:center;">
          <p style="margin:0;font-size:28px;font-weight:700;color:#111;">{n_alert}</p>
          <p style="margin:4px 0 0;font-size:12px;color:#6b7280;text-transform:uppercase;">Alerts</p>
        </div>
      </div>
      <h2 style="font-size:15px;font-weight:600;color:#111;margin:0 0 12px;">Alerts</h2>
      <ul style="list-style:none;margin:0;padding:0;">{alert_items}</ul>"""

    html = _base_html(
        title  = f"Daily Digest — {now}",
        accent = "#1e40af",
        body   = body,
    )
    _send_email(subject, text, html)


# ── Main (test mode) ──────────────────────────────────────────────────────────

def main():
    """
    Send a test watchlist alert to verify email settings.
    Edit config.py with real credentials before running.
    """
    print("Sending test alert...")
    test_alert = Alert(
        tier    = "watchlist",
        ticker  = "NVDA",
        trades  = [{
            "chamber":           "Senate",
            "representative":    "Sheldon Whitehouse",
            "ticker":            "NVDA",
            "asset_description": "NVIDIA Corporation - Common Stock",
            "type":              "sale_partial",
            "transaction_date":  "2026-05-08",
            "disclosure_date":   "06/02/2026",
            "amount":            "$100,001 - $250,000",
            "ptr_link":          "https://efdsearch.senate.gov/search/view/ptr/4aa0094d-d9da-4a05-aa13-6d9f5d376105/",
            "owner":             "Self",
        }],
        message = "🟢 WATCHLIST: Sheldon Whitehouse — NVDA SALE_PARTIAL on 2026-05-08 ($100,001 - $250,000) [Self]",
    )

    send_alerts([test_alert], win_rates={})
    print("\n✓ Check your inbox. If nothing arrived, check EMAIL_SENDER/EMAIL_PASSWORD in config.py")
    print("  Gmail requires an App Password — not your regular login password.")
    print("  Enable at: myaccount.google.com/apppasswords\n")


if __name__ == "__main__":
    main()