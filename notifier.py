"""
notifier.py — Congressional Trade Monitor
Formats Alert objects into emails and sends them via Gmail SMTP.

Every alert from a run goes into one digest, ranked by conviction score, so a
busy day produces a single triageable email rather than a dozen ignored ones.
Each alert renders as a card carrying the numbers that decide whether it is
actionable: disclosed size, disclosure lag, price move since the trade date,
committee conflicts, and — for the top few — AI context from Gemini.

Public interface:
  send_digest(alerts) -> None
  send_summary(alerts, trades, performance) -> None  (weekly digest)
"""

import os
import smtplib
import time
from html import escape
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

import config
from analyzer import Alert, parse_amount_value


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


# ── Amount formatting ─────────────────────────────────────────────────────────

def _fmt_amount(s: str) -> str:
    """Convert a disclosure range string to a midpoint estimate, e.g. '~$8K'."""
    if not s or s.lower().startswith("none"):
        return "—"
    v = parse_amount_value(s)
    if v <= 0:
        return s
    return _fmt_dollars(v)


def _fmt_dollars(v: float) -> str:
    """Format a dollar figure compactly, e.g. '~$8K', '~$1.2M'."""
    if v >= 1_000_000:
        return f"~${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"~${v / 1_000:.0f}K"
    return f"~${v:.0f}"


# ── Template helpers ──────────────────────────────────────────────────────────

def _trade_rows_html(trades: list[dict]) -> str:
    """Render a list of trades as an HTML table body."""
    rows = ""
    for t in trades:
        tx_type = t["type"].replace("_", " ").title()
        owner   = f" ({t['owner']})" if t.get("owner") else ""
        color   = "#16a34a" if t["type"] == "purchase" else "#dc2626"
        rows += f"""
        <tr>
          <td style="padding:6px 12px;">{t['representative']}{owner}</td>
          <td style="padding:6px 12px;font-weight:bold;">{t['ticker']}</td>
          <td style="padding:6px 12px;"><span style="color:{color};font-weight:600;">{tx_type}</span></td>
          <td style="padding:6px 12px;">{t['transaction_date']}</td>
          <td style="padding:6px 12px;">{_fmt_amount(t['amount'])}</td>
          <td style="padding:6px 12px;">
            <a href="{t['ptr_link']}" style="color:#2563eb;">Filing ↗</a>
          </td>
        </tr>"""
    return rows


def _trade_rows_text(trades: list[dict]) -> str:
    """Render a list of trades as plain text."""
    lines = []
    for t in trades:
        tx_type  = t["type"].replace("_", " ").upper()
        owner    = f" ({t['owner']})" if t.get("owner") else ""
        tx_emoji = "🟢" if t["type"] == "purchase" else "🔴"
        lines.append(
            f"  {t['representative']}{owner} | {t['ticker']} {tx_emoji} {tx_type} | "
            f"{t['transaction_date']} | {_fmt_amount(t['amount'])}"
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


# ── AI context ────────────────────────────────────────────────────────────────

# Gemini model is overridable via env so a future deprecation is a config change,
# not a code change. gemini-2.0-flash was retired June 2026.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Google Search grounding has a much lower free-tier quota than plain generation.
# Once a grounded call 429s, flip this so the rest of the run skips straight to
# the non-grounded path instead of burning a failed grounded call every time.
_grounding_exhausted = False


def _gemini_generate(prompt: str, max_tokens: int, use_search: bool = True) -> str | None:
    """
    Single entry point for all Gemini calls.
    Tries a grounded (Google Search) call first; on a 429 quota error, falls back
    to a non-grounded call so the email still gets AI context. Returns None only
    if there is no API key or every attempt fails.
    """
    global _grounding_exhausted

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
        from google.genai import errors
    except ImportError as e:
        print(f"  ⚠ Gemini SDK not installed: {e}")
        return None

    client = genai.Client(api_key=api_key)

    def _call(grounded: bool):
        # Disable "thinking" — 2.5 models otherwise spend the output-token budget on
        # internal reasoning, truncating these short summaries to a few words.
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        if grounded:
            cfg.tools = [types.Tool(google_search=types.GoogleSearch())]
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        return resp.text.strip() if resp.text else None

    # Attempt grounded first (unless already known-exhausted), then non-grounded.
    grounded_first = use_search and not _grounding_exhausted
    for grounded in ([True, False] if grounded_first else [False]):
        for attempt in range(2):
            try:
                return _call(grounded)
            except errors.ClientError as e:
                if getattr(e, "code", None) == 429:
                    if grounded:
                        _grounding_exhausted = True  # don't retry grounded; drop to fallback
                        break
                    if attempt == 0:
                        time.sleep(2)  # transient rate limit — brief backoff then retry
                        continue
                    return None
                print(f"  ⚠ Gemini error: {e}")
                return None
            except Exception as e:
                print(f"  ⚠ Gemini error: {e}")
                return None
    return None


def generate_alert_context(alert: Alert, conflicts: list[str] | None = None) -> str | None:
    """
    Explain why a signal formed, grounded in real-time Google Search.
    Fed the full enriched picture — size, timing, disclosure lag, insiders and
    committee conflicts — because a prompt carrying only a ticker and three
    names can only produce generic commentary.
    """
    m = alert.meta
    members   = m.get("members", [])
    direction = "buying" if alert.trades[0]["type"] == "purchase" else "selling"

    facts = [f"{len(members)} member(s) of Congress are {direction} {alert.ticker}"]
    if members:
        facts.append("Members: " + ", ".join(members[:5]) + ("..." if len(members) > 5 else ""))
    if m.get("dollar_total"):
        facts.append(f"Combined disclosed size: {_fmt_dollars(m['dollar_total'])}")
    if m.get("first_date"):
        facts.append(f"Trade dates: {m['first_date']} to {m.get('last_date', m['first_date'])}")
    if m.get("median_lag_days") is not None:
        facts.append(f"Disclosed {m['median_lag_days']} days after execution")
    if m.get("n_insiders"):
        titles = sorted({t.get("title", "") for t in alert.trades if t.get("source") == "insider"})
        facts.append(f"Also bought by {m['n_insiders']} company insider(s): {', '.join(filter(None, titles))}")
    if conflicts:
        facts.append("Relevant committee assignments: " + "; ".join(conflicts))

    prompt = (
        f"Today is {datetime.now().strftime('%B %d, %Y')}.\n"
        f"Congressional trading signal:\n- " + "\n- ".join(facts) + "\n\n"
        f"In 2–3 sentences, explain what is happening with {alert.ticker} right now "
        f"that could explain this activity. Focus on recent news, earnings, legislation, "
        f"or regulatory developments. If the committee assignments above are relevant to "
        f"the company, say how. Be specific and factual; do not restate the numbers above."
    )
    return _gemini_generate(prompt, max_tokens=220)


# ── Alert cards ───────────────────────────────────────────────────────────────

TIER_LABELS = {
    "cross_cluster": "CROSS-SIGNAL",
    "cluster":       "CLUSTER",
    "winrate":       "WIN-RATE",
    "watchlist":     "WATCHLIST",
}

# (foreground, background) per tier — shared by the digest cards and the summary.
TIER_COLORS = {
    "cross_cluster": ("#7c3aed", "#ede9fe"),
    "cluster":       ("#dc2626", "#fee2e2"),
    "winrate":       ("#d97706", "#fef3c7"),
    "watchlist":     ("#16a34a", "#dcfce7"),
}


def _alert_conflicts(alert: Alert) -> list[str]:
    """
    Committee conflicts for every member in an alert, as "Member: Committee"
    lines. Applies to all tiers — a cluster forming among members who oversee
    the sector is a materially different signal from one that isn't.
    """
    from committees import flag_conflicts

    lines = []
    for member in alert.meta.get("members", []):
        lines += [f"{member}: {c}" for c in flag_conflicts(member, alert.ticker)]
    return lines


def _metrics(alert: Alert) -> list[tuple[str, str]]:
    """The at-a-glance numbers for a card, as (label, value) pairs."""
    m = alert.meta
    out = []

    # Congressional and insider dollars are different kinds of number — a
    # disclosure-bracket midpoint versus an exact transaction value — so they are
    # shown apart. The total is kept alongside, but only where both exist and it
    # cannot be mistaken for congressional conviction on its own.
    congress_dollars = m.get("congress_dollars", 0.0)
    insider_dollars  = m.get("insider_dollars", 0.0)

    if congress_dollars:
        out.append(("Congress size", _fmt_dollars(congress_dollars)))
    if insider_dollars:
        out.append(("Insider size", _fmt_dollars(insider_dollars)))
    if congress_dollars and insider_dollars:
        out.append(("Combined", _fmt_dollars(congress_dollars + insider_dollars)))

    lag = m.get("median_lag_days")
    if lag is None:
        out.append(("Disclosure lag", "unknown"))
    else:
        freshness = "fresh" if lag <= 14 else ("aging" if lag <= 30 else "stale")
        out.append(("Disclosure lag", f"{lag}d ({freshness})"))

    pct = m.get("pct_since_trade")
    if pct is not None:
        spy   = m.get("spy_since_trade")
        since = f"{pct:+.1f}%"
        if spy is not None:
            since += f" (SPY {spy:+.1f}%)"
        # Same number means opposite things by direction: a sell cluster followed
        # by a rally is a signal that has been wrong so far, not a win.
        excess = m.get("excess_since_trade")
        if excess is not None:
            right = excess > 0 if m.get("direction", "buy") == "buy" else excess < 0
            since += " ✓" if right else " ✗"
        out.append(("Since trade date", since))

    if m.get("best_win_rate"):
        out.append(("Best member win rate", f"{m['best_win_rate']:.0%}"))

    return out


def _alert_card(alert: Alert, rank: int, with_ai: bool) -> tuple[str, str]:
    """
    Render one alert as a (plain_text, html) block for the digest.

    with_ai controls the Gemini call — only the top-ranked cards get one, so a
    noisy day cannot burn the free-tier quota on low-conviction signals.
    """
    label     = TIER_LABELS.get(alert.tier, alert.tier.upper())
    fg, bg    = TIER_COLORS.get(alert.tier, ("#6b7280", "#f3f4f6"))
    m         = alert.meta
    conflicts = _alert_conflicts(alert)
    metrics   = _metrics(alert)

    insider  = [t for t in alert.trades if t.get("source") == "insider"]
    congress = [t for t in alert.trades if t.get("source") != "insider"]

    context = generate_alert_context(alert, conflicts) if with_ai else None

    # ── Plain text ──
    text_lines = [
        f"#{rank} · [{label}] {alert.ticker} · conviction {alert.score:.0f}/100",
        f"  {alert.message.splitlines()[0]}",
    ]
    text_lines += [f"  {k}: {v}" for k, v in metrics]
    if conflicts:
        text_lines.append("  ⚠ Committee conflicts:")
        text_lines += [f"      {c}" for c in conflicts]
    text_lines.append("")
    text_lines.append(_trade_rows_text(congress))
    if insider:
        text_lines.append("  Insider buys:")
        text_lines += [
            f"      {t['name']} ({t['title']}) | {t['transaction_date']} | {_fmt_amount(t['amount'])}"
            for t in insider
        ]
    if context:
        text_lines += ["", f"  Why this matters: {context}"]
    text = "\n".join(text_lines)

    # ── HTML ──
    metric_cells = "".join(
        f"""
        <td style="padding:8px 12px;vertical-align:top;">
          <p style="margin:0;font-size:10px;color:#6b7280;text-transform:uppercase;
                    letter-spacing:.05em;">{k}</p>
          <p style="margin:2px 0 0;font-size:14px;font-weight:600;color:#111;">{escape(v)}</p>
        </td>"""
        for k, v in metrics
    )
    metrics_html = f"""
      <table style="width:100%;border-collapse:collapse;background:#f9fafb;border-radius:6px;
                    margin:12px 0;"><tr>{metric_cells}</tr></table>""" if metrics else ""

    conflict_html = ""
    if conflicts:
        items = "".join(
            f'<li style="margin:3px 0;font-size:12px;color:#374151;">{escape(c)}</li>'
            for c in conflicts
        )
        conflict_html = f"""
      <div style="margin:12px 0;padding:12px 14px;background:#fff7ed;border-radius:6px;
                  border-left:3px solid #ea580c;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#9a3412;">
          ⚠ Committees with oversight relevant to {alert.ticker}
        </p>
        <ul style="margin:0;padding:0 0 0 16px;">{items}</ul>
      </div>"""

    insider_html = ""
    if insider:
        ins_rows = "".join(
            f"""
        <tr>
          <td style="padding:6px 12px;">{escape(t['name'])}</td>
          <td style="padding:6px 12px;">{escape(t['title'])}</td>
          <td style="padding:6px 12px;">{t['transaction_date']}</td>
          <td style="padding:6px 12px;font-weight:600;">{_fmt_amount(t['amount'])}</td>
          <td style="padding:6px 12px;"><a href="{t['ptr_link']}" style="color:#2563eb;">Filing ↗</a></td>
        </tr>"""
            for t in insider
        )
        insider_html = f"""
      <p style="margin:14px 0 6px;font-size:12px;font-weight:600;color:#374151;">Insider buys</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:7px 12px;">Insider</th>
            <th style="padding:7px 12px;">Title</th>
            <th style="padding:7px 12px;">Date</th>
            <th style="padding:7px 12px;">Value</th>
            <th style="padding:7px 12px;">Filing</th>
          </tr>
        </thead>
        <tbody>{ins_rows}</tbody>
      </table>"""

    context_html = ""
    if context:
        context_html = f"""
      <div style="margin:12px 0 0;padding:12px 14px;background:#eff6ff;border-radius:6px;
                  border-left:4px solid #3b82f6;">
        <p style="margin:0 0 6px;font-size:10px;font-weight:600;color:#1d4ed8;
                  text-transform:uppercase;letter-spacing:.06em;">AI Context · Gemini + Google Search</p>
        <p style="margin:0;font-size:13px;color:#1e3a5f;line-height:1.5;">{escape(context)}</p>
      </div>"""

    cong_label = ""
    if insider:
        cong_label = '<p style="margin:14px 0 6px;font-size:12px;font-weight:600;color:#374151;">Congressional buys</p>'

    html = f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px 18px;margin:0 0 16px;">
      <div style="margin:0 0 8px;">
        <span style="font-size:13px;color:#9ca3af;font-weight:600;">#{rank}</span>
        <span style="background:{bg};color:{fg};font-size:10px;font-weight:700;
                     padding:2px 7px;border-radius:4px;margin:0 6px;">{label}</span>
        <span style="font-size:18px;font-weight:700;color:#111;">{alert.ticker}</span>
        <span style="float:right;font-size:13px;font-weight:700;color:{fg};">
          {alert.score:.0f}<span style="color:#9ca3af;font-weight:400;">/100</span>
        </span>
      </div>
      <p style="margin:0;font-size:13px;color:#4b5563;">{escape(alert.message.splitlines()[0])}</p>
      {metrics_html}{conflict_html}{cong_label}
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:7px 12px;">Member</th>
            <th style="padding:7px 12px;">Ticker</th>
            <th style="padding:7px 12px;">Type</th>
            <th style="padding:7px 12px;">Date</th>
            <th style="padding:7px 12px;">Amount</th>
            <th style="padding:7px 12px;">Filing</th>
          </tr>
        </thead>
        <tbody>{_trade_rows_html(congress)}</tbody>
      </table>
      {insider_html}{context_html}
    </div>"""

    return text, html


# ── Public interface ──────────────────────────────────────────────────────────

def send_digest(alerts: list[Alert], warnings: list[str] | None = None) -> None:
    """
    Send every alert from a run as one email, ranked by conviction score.

    One email per alert meant a busy day produced a dozen messages that all went
    unread. Ranking puts the strongest signal in the subject line so the inbox
    alone is enough to triage.

    `warnings` carries degradations that make the digest incomplete — a feed
    outage, say — so a thin digest is never mistaken for a quiet market.
    """
    warnings = warnings or []

    if not alerts:
        print("  No alerts to send.")
        for w in warnings:
            print(f"  ⚠ {w}")
        return

    ranked = sorted(alerts, key=lambda a: a.score, reverse=True)
    top    = ranked[0]

    tier_counts = {}
    for a in ranked:
        label = TIER_LABELS.get(a.tier, a.tier.upper())
        tier_counts[label] = tier_counts.get(label, 0) + 1
    breakdown = " · ".join(f"{n} {label.lower()}" for label, n in tier_counts.items())

    subject = (
        f"⚡ {len(ranked)} signal{'s' if len(ranked) != 1 else ''} — "
        f"top: {top.ticker} {TIER_LABELS.get(top.tier, top.tier)} ({top.score:.0f}/100)"
    )

    cards = [
        _alert_card(a, rank=i + 1, with_ai=i < config.DIGEST_AI_TOP_N)
        for i, a in enumerate(ranked)
    ]

    warning_text = "".join(f"\n⚠ {w}\n" for w in warnings)
    text = (
        "CONGRESSIONAL TRADE MONITOR — Signal Digest\n"
        f"{datetime.now().strftime('%B %d, %Y %H:%M')}\n"
        f"{'='*60}\n"
        f"{len(ranked)} alert(s): {breakdown}\n"
        "Ranked by conviction score (size, participants, freshness, track record).\n"
        f"{warning_text}\n"
        + "\n\n".join(t for t, _ in cards)
    )

    warning_html = ""
    if warnings:
        items = "".join(
            f'<p style="margin:4px 0;font-size:13px;color:#7f1d1d;">⚠ {escape(w)}</p>'
            for w in warnings
        )
        warning_html = f"""
      <div style="margin:0 0 16px;padding:12px 14px;background:#fef2f2;border-radius:6px;
                  border-left:4px solid #dc2626;">{items}</div>"""

    body = f"""
      <p style="font-size:14px;color:#374151;margin:0 0 4px;">
        <strong>{len(ranked)} alert(s)</strong> — {escape(breakdown)}
      </p>
      <p style="font-size:12px;color:#6b7280;margin:0 0 20px;">
        Ranked by conviction score: disclosed size, participant count, disclosure
        freshness, and member track record.
      </p>
      {warning_html}{''.join(h for _, h in cards)}"""

    html = _base_html(
        title  = f"{len(ranked)} Signal{'s' if len(ranked) != 1 else ''} — top: {top.ticker}",
        accent = "#1e3a5f",
        body   = body,
    )
    _send_email(subject, text, html)


def _sector_net_activity(trades: list[dict]) -> list[tuple[str, int, int]]:
    """Map trades to sectors; return (sector, buy_count, sell_count) sorted by net buys."""
    ticker_sector: dict[str, str] = {}
    for sector, tickers in config.SECTOR_TICKERS.items():
        for t in tickers:
            ticker_sector[t.upper()] = sector

    buys: dict[str, int] = {}
    sells: dict[str, int] = {}
    for trade in trades:
        sector = ticker_sector.get(trade["ticker"].upper())
        if not sector:
            continue
        if trade["type"] == "purchase":
            buys[sector] = buys.get(sector, 0) + 1
        else:
            sells[sector] = sells.get(sector, 0) + 1

    all_sectors = set(buys) | set(sells)
    result = [(s, buys.get(s, 0), sells.get(s, 0)) for s in all_sectors]
    result.sort(key=lambda x: x[1] - x[2], reverse=True)
    return result


def generate_weekly_intelligence(
    sector_activity: list[tuple[str, int, int]],
) -> str | None:
    """Weekly legislative + regulatory intelligence, grounded in real-time search."""
    top_accumulated = [s for s, b, sl in sector_activity if b > sl][:3]
    top_distributed = [s for s, b, sl in sector_activity if sl > b][:2]

    prompt = (
        f"Today is {datetime.now().strftime('%B %d, %Y')}. "
        f"US congressional trading this week shows accumulation in: "
        f"{', '.join(top_accumulated) or 'mixed sectors'}. "
        f"Distribution in: {', '.join(top_distributed) or 'none notable'}. "
        f"In 3–4 bullet points, summarize what US legislation or regulatory actions "
        f"advanced this week that could explain or relate to this trading activity. "
        f"Name specific bills, agencies, and companies where possible. "
        f"If nothing notable, say so briefly."
    )
    return _gemini_generate(prompt, max_tokens=300)


def send_summary(alerts: list[Alert], trades: list[dict], performance: str | None = None) -> None:
    """
    Send the weekly Sunday digest email.
    Sections: sector activity, strongest signals, alert performance vs SPY
    (from history.format_summary), legislative intelligence (Gemini).
    """
    now      = datetime.now().strftime("%B %d, %Y")
    n_alert  = len(alerts)
    n_trade  = len(trades)
    chambers = sorted({t["chamber"] for t in trades})

    sector_activity  = _sector_net_activity(trades)
    legislative_text = generate_weekly_intelligence(sector_activity)

    subject = f"📊 Weekly Digest — {now} · {n_alert} alert(s), {n_trade} trade(s)"

    # ── Plain text ──────────────────────────────────────────────────────────────
    text_lines = [
        "CONGRESSIONAL TRADE MONITOR — Weekly Digest",
        now,
        "=" * 50,
        f"Trades:  {n_trade} ({', '.join(chambers)})",
        f"Alerts:  {n_alert}",
        "",
        "── Sector Activity ──",
    ]
    for sector, buys, sells in sector_activity:
        net   = buys - sells
        arrow = f"▲{net}" if net > 0 else (f"▼{abs(net)}" if net < 0 else "=")
        text_lines.append(f"  {sector:<20} {buys} buys  {sells} sells  {arrow}")

    text_lines += ["", "── Strongest Signals ──"]
    if alerts:
        for a in alerts[:5]:
            label = TIER_LABELS.get(a.tier, a.tier.upper())
            text_lines.append(f"  [{label}] {a.ticker}: {a.message.splitlines()[0]}")
    else:
        text_lines.append("  No alerts this week.")

    if performance:
        text_lines += ["", "── Alert Performance ──", performance]

    if legislative_text:
        text_lines += ["", "── Legislative Intelligence (Gemini) ──", legislative_text]

    text = "\n".join(text_lines)

    # ── HTML ────────────────────────────────────────────────────────────────────
    sector_rows = ""
    for sector, buys, sells in sector_activity:
        net = buys - sells
        if net > 0:
            net_html = f'<span style="color:#16a34a;font-weight:600;">▲ {net}</span>'
        elif net < 0:
            net_html = f'<span style="color:#dc2626;font-weight:600;">▼ {abs(net)}</span>'
        else:
            net_html = '<span style="color:#9ca3af;">—</span>'
        sector_rows += f"""
        <tr style="border-bottom:1px solid #f3f4f6;">
          <td style="padding:7px 12px;font-size:13px;">{sector}</td>
          <td style="padding:7px 12px;font-size:13px;text-align:center;color:#16a34a;">{buys}</td>
          <td style="padding:7px 12px;font-size:13px;text-align:center;color:#dc2626;">{sells}</td>
          <td style="padding:7px 12px;font-size:13px;text-align:center;">{net_html}</td>
        </tr>"""

    signal_items = ""
    if alerts:
        for a in alerts[:5]:
            fg, bg = TIER_COLORS.get(a.tier, ("#6b7280", "#f3f4f6"))
            label  = TIER_LABELS.get(a.tier, a.tier.upper())
            first_line = a.message.splitlines()[0]
            signal_items += f"""
        <li style="margin:6px 0;padding:10px 14px;background:#f9fafb;border-radius:6px;
                   font-size:13px;display:flex;align-items:center;gap:10px;">
          <span style="background:{bg};color:{fg};font-size:10px;font-weight:700;
                       padding:2px 7px;border-radius:4px;white-space:nowrap;">{label}</span>
          <span>{first_line}</span>
        </li>"""
    else:
        signal_items = '<li style="color:#6b7280;font-size:13px;padding:8px 0;">No alerts this week.</li>'

    if performance:
        performance_html = f"""
      <h2 style="font-size:14px;font-weight:600;color:#111;margin:24px 0 10px;">Alert Performance</h2>
      <pre style="margin:0;padding:14px 16px;background:#f9fafb;border-radius:6px;
                  border-left:4px solid #6b7280;font-size:12px;line-height:1.5;
                  color:#374151;overflow-x:auto;white-space:pre;">{escape(performance)}</pre>"""
    else:
        performance_html = ""

    if legislative_text:
        formatted = escape(legislative_text).replace("\n", "<br>")
        legislative_html = f"""
      <h2 style="font-size:14px;font-weight:600;color:#111;margin:24px 0 10px;">Legislative Intelligence</h2>
      <div style="padding:14px 16px;background:#eff6ff;border-radius:6px;border-left:4px solid #3b82f6;">
        <p style="margin:0 0 6px;font-size:11px;font-weight:600;color:#1d4ed8;
                  text-transform:uppercase;letter-spacing:.06em;">Gemini + Google Search</p>
        <p style="margin:0;font-size:13px;color:#1e3a5f;line-height:1.6;">{formatted}</p>
      </div>"""
    else:
        legislative_html = ""

    body = f"""
      <div style="display:flex;gap:16px;margin-bottom:20px;">
        <div style="flex:1;padding:14px;background:#f3f4f6;border-radius:6px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#111;">{n_trade}</p>
          <p style="margin:4px 0 0;font-size:11px;color:#6b7280;text-transform:uppercase;">Trades</p>
        </div>
        <div style="flex:1;padding:14px;background:#f3f4f6;border-radius:6px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#111;">{n_alert}</p>
          <p style="margin:4px 0 0;font-size:11px;color:#6b7280;text-transform:uppercase;">Alerts</p>
        </div>
        <div style="flex:1;padding:14px;background:#f3f4f6;border-radius:6px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#111;">{len(sector_activity)}</p>
          <p style="margin:4px 0 0;font-size:11px;color:#6b7280;text-transform:uppercase;">Sectors Active</p>
        </div>
      </div>

      <h2 style="font-size:14px;font-weight:600;color:#111;margin:0 0 10px;">Sector Activity</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <thead>
          <tr style="background:#f3f4f6;text-align:left;">
            <th style="padding:7px 12px;font-size:12px;font-weight:600;color:#374151;">Sector</th>
            <th style="padding:7px 12px;font-size:12px;font-weight:600;color:#16a34a;text-align:center;">Buys</th>
            <th style="padding:7px 12px;font-size:12px;font-weight:600;color:#dc2626;text-align:center;">Sells</th>
            <th style="padding:7px 12px;font-size:12px;font-weight:600;color:#374151;text-align:center;">Net</th>
          </tr>
        </thead>
        <tbody>{sector_rows}</tbody>
      </table>

      <h2 style="font-size:14px;font-weight:600;color:#111;margin:0 0 10px;">Strongest Signals</h2>
      <ul style="list-style:none;margin:0 0 4px;padding:0;">{signal_items}</ul>
      {performance_html}{legislative_html}"""

    html = _base_html(
        title  = f"Weekly Digest — {now}",
        accent = "#1e40af",
        body   = body,
    )
    _send_email(subject, text, html)


# ── Main (test mode) ──────────────────────────────────────────────────────────

def _sample_alerts() -> list[Alert]:
    """Two alerts of different strength, for checking digest ranking and layout."""
    from analyzer import enrich_and_score

    strong = Alert(
        tier    = "cross_cluster",
        ticker  = "NVDA",
        trades  = [
            {
                "chamber": "Senate", "representative": "Tommy Tuberville", "ticker": "NVDA",
                "asset_description": "NVIDIA Corporation - Common Stock", "type": "purchase",
                "transaction_date": "2026-07-20", "disclosure_date": "2026-07-28",
                "amount": "$250,001 - $500,000", "owner": "Self", "source": "congress",
                "ptr_link": "https://efdsearch.senate.gov/search/view/ptr/4aa0094d/",
            },
            {
                "chamber": "House", "representative": "Josh Gottheimer", "ticker": "NVDA",
                "asset_description": "NVIDIA Corporation", "type": "purchase",
                "transaction_date": "2026-07-24", "disclosure_date": "2026-08-01",
                "amount": "$500,001 - $1,000,000", "owner": "", "source": "congress",
                "ptr_link": "https://disclosures-clerk.house.gov/20026543.pdf",
            },
            {
                "name": "Jensen Huang", "title": "CEO", "ticker": "NVDA", "type": "purchase",
                "transaction_date": "2026-07-22", "disclosure_date": "2026-07-24",
                "amount": "$2,400,000", "source": "insider",
                "ptr_link": "http://openinsider.com/screener?s=NVDA",
            },
        ],
        message = "🔗 CROSS-SIGNAL: NVDA — 2 congressional buy(s) + 1 insider buy(s), 4 days apart",
    )

    weak = Alert(
        tier    = "watchlist",
        ticker  = "T",
        trades  = [{
            "chamber": "Senate", "representative": "Sheldon Whitehouse", "ticker": "T",
            "asset_description": "AT&T Inc.", "type": "purchase",
            "transaction_date": "2026-06-14", "disclosure_date": "2026-07-26",
            "amount": "$1,001 - $15,000", "owner": "Spouse",
            "ptr_link": "https://efdsearch.senate.gov/search/view/ptr/4aa0094d/",
        }],
        message = "👁️ WATCHLIST: Sheldon Whitehouse — T PURCHASE on 2026-06-14 ($1,001 - $15,000) [Spouse]",
    )

    for a in (strong, weak):
        enrich_and_score(a, win_rates={})
    return [strong, weak]


def main():
    """
    Render the digest to an HTML file without sending, so layout and ranking can
    be checked without spending an email or a Gemini call. Pass --send to
    actually deliver it and verify SMTP credentials.
    """
    import sys

    alerts = _sample_alerts()

    if "--send" in sys.argv:
        print("Sending test digest...")
        send_digest(alerts)
        print("\n✓ Check your inbox. If nothing arrived, check ALERT_EMAIL_SENDER/PASSWORD in .env")
        print("  Gmail requires an App Password — not your regular login password.")
        print("  Enable at: myaccount.google.com/apppasswords\n")
        return

    cards = [_alert_card(a, rank=i + 1, with_ai=False) for i, a in enumerate(alerts)]
    html  = _base_html("Digest Preview", "#1e3a5f", "".join(h for _, h in cards))

    out = "digest_preview.html"
    with open(out, "w") as f:
        f.write(html)

    for text, _ in cards:
        print(text)
        print()
    print(f"✓ Wrote {out} — open it to check layout.")
    print("  Re-run with --send to email it instead.")


if __name__ == "__main__":
    main()