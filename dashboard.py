"""dashboard.py — Congressional Trade Monitor visual interface.

Streamlit app that imports existing modules directly (no API layer needed).
Data sources are all public — no secrets required for Streamlit Cloud deployment.

IMPORTANT: Does NOT call analyzer.analyze() because that function modifies
seen_trades.json (the deduplication state used by the real monitoring loop).
Instead, calls individual detectors directly.
"""

import re
import streamlit as st
import pandas as pd
import altair as alt
import yfinance as yf
from datetime import datetime, timedelta

st.set_page_config(
    page_title="Congressional Trade Monitor",
    page_icon=":material/monitoring:",
    layout="wide",
)

from fetcher import fetch_all
from analyzer import (
    compute_win_rates,
    detect_cluster_alerts,
    detect_winrate_alerts,
    detect_watchlist_alerts,
)
from committees import load_all, get_member_committees, flag_conflicts
import config


# ── Caching ────────────────────────────────────────────────────────────────────

MAX_DAYS = 90  # always fetch the full window; slider filters in memory

@st.cache_resource
def _load_committees():
    """Populate the committees module-level cache once per process."""
    load_all()


@st.cache_data(ttl="1h")
def _fetch_trades_raw() -> list[dict]:
    return fetch_all(days=MAX_DAYS)


def _filter_trades(days: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    return [
        t for t in _fetch_trades_raw()
        if datetime.strptime(t["transaction_date"], "%Y-%m-%d") >= cutoff
    ]


@st.cache_data(ttl="1h")
def _get_win_rates() -> dict:
    return compute_win_rates(_fetch_trades_raw())


@st.cache_data(ttl="1h")
def _get_alerts(days: int) -> list:
    trades = _filter_trades(days)
    win_rates = _get_win_rates()
    cluster = detect_cluster_alerts(trades)
    winrate = detect_winrate_alerts(trades, win_rates)
    watchlist = detect_watchlist_alerts(trades)
    combined = cluster + winrate + watchlist
    combined.sort(key=lambda a: {"cluster": 0, "winrate": 1, "watchlist": 2}[a.tier])
    return combined


@st.cache_data(ttl="24h")
def _get_company_name(ticker: str) -> str:
    try:
        name = yf.Ticker(ticker).info.get("shortName") or ticker
        return name.removesuffix(" (The)").removesuffix(", The")
    except Exception:
        return ticker


@st.cache_data(ttl="1h")
def _get_price_history(ticker: str, start_date: str) -> pd.DataFrame:
    """
    Fetch daily close for ticker and SPY from start_date to today, indexed to 100.
    Returns DataFrame with columns [ticker, "SPY"] so both start at 100 for easy comparison.
    """
    import logging, contextlib, io as _io
    tickers = [ticker, "SPY"] if ticker != "SPY" else ["SPY"]
    with contextlib.redirect_stderr(_io.StringIO()):
        try:
            logging.disable(logging.CRITICAL)
            raw = yf.download(tickers, start=start_date, progress=False, auto_adjust=True)
            logging.disable(logging.NOTSET)
        except Exception:
            return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    close = raw["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(name=ticker)
    close = close.dropna(how="all")
    if close.empty:
        return pd.DataFrame()
    return (close / close.iloc[0] * 100).reset_index()


# ── Member detail dialog ───────────────────────────────────────────────────────

@st.dialog("Member Detail", width="large")
def _member_dialog(name: str, days: int):
    trades = _filter_trades(days)
    win_rates = _get_win_rates()

    member_trades = [t for t in trades if t["representative"] == name]
    stats = win_rates.get(name)

    all_conflicts = []
    for t in member_trades:
        all_conflicts.extend(flag_conflicts(name, t["ticker"]))
    unique_conflicts = list(dict.fromkeys(all_conflicts))

    col1, col2, col3 = st.columns(3)
    col1.metric("Win Rate", f"{stats['win_rate']:.0%}" if stats and stats["total"] > 0 else "N/A")
    col2.metric("Trades (window)", len(member_trades))
    col3.metric("Conflicts", len(unique_conflicts))

    mtab1, mtab2 = st.tabs(["Recent Trades", "Committees & Conflicts"])

    with mtab1:
        if member_trades:
            df = pd.DataFrame(member_trades)
            df["transaction_date"] = pd.to_datetime(df["transaction_date"])
            df = df.sort_values("transaction_date", ascending=False)
            st.dataframe(
                df,
                hide_index=True,
                column_config={
                    "representative": None,
                    "asset_description": None,
                    "chamber": None,
                    "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
                    "disclosure_date": st.column_config.TextColumn("Disclosed"),
                    "ptr_link": st.column_config.LinkColumn("Filing"),
                    "ticker": st.column_config.TextColumn("Ticker"),
                    "type": st.column_config.TextColumn("Type"),
                    "amount": st.column_config.TextColumn("Amount"),
                    "owner": st.column_config.TextColumn("Owner"),
                },
            )
        else:
            st.info("No trades in the current window.")

    with mtab2:
        member_data = get_member_committees(name)
        committees = member_data.get("committees", [])
        if committees:
            for c in committees:
                st.write(f"• {c}")
        else:
            st.caption("No committee assignments found in cache.")

        if unique_conflicts:
            st.space("small")
            st.warning("**Potential conflicts detected:**")
            for conflict in unique_conflicts:
                st.write(f"⚠ {conflict}")


# ── Summary helpers ────────────────────────────────────────────────────────────

def _parse_amount(s: str) -> float:
    if not s or s.lower().startswith("none"):
        return 0.0
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+", s)]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2
    return nums[0] if nums else 0.0


def _fmt_dollars(v: float) -> str:
    if v >= 1_000_000: return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:     return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _render_summary(trades: list[dict]):
    from collections import Counter

    if not trades:
        return

    buys  = [t for t in trades if t["type"] == "purchase"]
    sells = [t for t in trades if t["type"] in ("sale", "sale_partial")]

    def _signed_count(v: int) -> str:
        return f"+{v}" if v > 0 else str(v)

    def _signed_dollars(v: float) -> str:
        sign = "+" if v > 0 else "-" if v < 0 else ""
        return f"{sign}{_fmt_dollars(abs(v))}"

    def link_badge(ticker: str, color: str) -> str:
        url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        return (
            f'<a href="{url}" target="_blank" style="text-decoration:none;">'
            f'<span style="background:#1e293b;color:{color};font-family:monospace;'
            f'padding:2px 8px;border-radius:4px;font-weight:600;">{ticker}</span>'
            f'</a>'
        )

    # ── Compute net counts and dollars ────────────────────────────────────────
    buy_counts: dict[str, int] = Counter(t["ticker"] for t in buys)
    sell_counts: dict[str, int] = Counter(t["ticker"] for t in sells)
    buy_dollars: dict[str, float] = {}
    sell_dollars: dict[str, float] = {}
    for t in buys:
        buy_dollars[t["ticker"]] = buy_dollars.get(t["ticker"], 0) + _parse_amount(t["amount"])
    for t in sells:
        sell_dollars[t["ticker"]] = sell_dollars.get(t["ticker"], 0) + _parse_amount(t["amount"])

    all_tickers = set(buy_counts) | set(sell_counts)
    net_counts  = {tk: buy_counts.get(tk, 0) - sell_counts.get(tk, 0) for tk in all_tickers}
    net_dollars = {tk: buy_dollars.get(tk, 0) - sell_dollars.get(tk, 0) for tk in all_tickers}

    top_bought_count   = sorted([(tk, v) for tk, v in net_counts.items()  if v > 0 and net_dollars.get(tk, 0) >= 0], key=lambda x: x[1],  reverse=True)[:5]
    top_bought_dollars = sorted([(tk, v) for tk, v in net_dollars.items() if v > 0 and net_counts.get(tk, 0)  >= 0], key=lambda x: x[1], reverse=True)[:5]
    top_sold_count     = sorted([(tk, v) for tk, v in net_counts.items()  if v < 0 and net_dollars.get(tk, 0) <= 0], key=lambda x: x[1])[:5]
    top_sold_dollars   = sorted([(tk, v) for tk, v in net_dollars.items() if v < 0 and net_counts.get(tk, 0)  <= 0], key=lambda x: x[1])[:5]

    # ── Sector heat ───────────────────────────────────────────────────────────
    sector_net: dict[str, int] = {}
    for ticker, net in net_counts.items():
        for sector, tickers in config.SECTOR_TICKERS.items():
            if ticker.upper() in [t.upper() for t in tickers]:
                sector_net[sector] = sector_net.get(sector, 0) + net
                break

    st.subheader("📊 Activity Summary", divider="gray")

    # Sector heat row
    if sector_net:
        hot_sector  = max(sector_net, key=lambda s: sector_net[s])
        cold_sector = min(sector_net, key=lambda s: sector_net[s])
        h_net = sector_net[hot_sector]
        c_net = sector_net[cold_sector]
        sh1, sh2 = st.columns(2)
        sh1.metric(
            "🔥 Hot Sector (most net buying)",
            hot_sector,
            delta=f"{_signed_count(h_net)} net trades",
        )
        if hot_sector != cold_sector:
            sh2.metric(
                "❄️ Avoid Sector (most net selling)",
                cold_sector,
                delta=f"{_signed_count(c_net)} net trades",
            )

    st.markdown("#### ⚖️ Net Activity")

    nc1, nc2 = st.columns(2)

    with nc1:
        st.markdown("**🟢 Most Net Bought**")
        sc1, sc2 = st.columns(2)
        with sc1:
            st.caption("**By frequency**")
            for tk, v in top_bought_count:
                st.markdown(
                    f'{link_badge(tk, "#16a34a")}: {_signed_count(v)}',
                    unsafe_allow_html=True,
                )
        with sc2:
            st.caption("**By estimated $**")
            for tk, v in top_bought_dollars:
                st.markdown(
                    f'{link_badge(tk, "#16a34a")}: {_signed_dollars(v)}',
                    unsafe_allow_html=True,
                )

    with nc2:
        st.markdown("**🔴 Most Net Sold**")
        sc3, sc4 = st.columns(2)
        with sc3:
            st.caption("**By frequency**")
            for tk, v in top_sold_count:
                st.markdown(
                    f'{link_badge(tk, "#dc2626")}: {_signed_count(v)}',
                    unsafe_allow_html=True,
                )
        with sc4:
            st.caption("**By estimated $**")
            for tk, v in top_sold_dollars:
                st.markdown(
                    f'{link_badge(tk, "#dc2626")}: {_signed_dollars(v)}',
                    unsafe_allow_html=True,
                )

    st.space("small")


# ── Tab renderers ──────────────────────────────────────────────────────────────

_TIER_DISPLAY = {
    "cluster":   ("⚡",  "CLUSTER"),
    "winrate":   ("🏆", "WIN-RATE"),
    "watchlist": ("👁️", "WATCHLIST"),
}


def _fmt_type(tx_type: str) -> str:
    if tx_type == "purchase":     return "🟢 Purchase"
    if tx_type == "sale":         return "🔴 Sale"
    if tx_type == "sale_partial": return "🔴 Partial Sale"
    return tx_type.replace("_", " ").title()


def _render_alerts(alerts: list, win_rates: dict):
    if not alerts:
        st.info("No alerts in the current window.", icon=":material/check_circle:")
        return

    for alert in alerts:
        company = _get_company_name(alert.ticker)
        label = f"{company} ({alert.ticker})" if company != alert.ticker else alert.ticker

        with st.container(border=True):
            emoji, tier_label = _TIER_DISPLAY[alert.tier]
            direction = alert.trades[0]["type"] if alert.trades else "purchase"
            header_color = "#16a34a" if direction == "purchase" else "#dc2626"
            st.markdown(
                f'<div style="background:{header_color};padding:10px 16px;border-radius:6px;margin-bottom:4px;">'
                f'<span style="color:white;font-size:1.15em;font-weight:700;">{emoji} {tier_label} — {label}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.caption(alert.message)

            trade_df = pd.DataFrame(alert.trades)
            cols = [c for c in ["representative", "ticker", "type", "amount", "transaction_date"] if c in trade_df.columns]
            trade_df = trade_df[cols].copy()
            trade_df["type"] = trade_df["type"].apply(_fmt_type)
            trade_df["win_rate"] = trade_df["representative"].apply(
                lambda n: win_rates.get(n, {}).get("win_rate")
            )
            trade_df["committees"] = trade_df["representative"].apply(
                lambda n: ", ".join(get_member_committees(n).get("committees", [])[:2]) or "—"
            )
            st.link_button(
                f"📈 {label} on TradingView",
                f"https://www.tradingview.com/chart/?symbol={alert.ticker}",
            )
            st.dataframe(
                trade_df,
                hide_index=True,
                column_config={
                    "representative": st.column_config.TextColumn("Member"),
                    "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
                    "ticker": st.column_config.TextColumn("Ticker"),
                    "type": st.column_config.TextColumn("Type"),
                    "amount": st.column_config.TextColumn("Amount"),
                    "win_rate": st.column_config.ProgressColumn(
                        "Win Rate", min_value=0, max_value=1, format="percent"
                    ),
                    "committees": st.column_config.TextColumn("Committees"),
                },
            )
            st.caption(f"Detected: {alert.fired_at[:16]}")

            earliest = min(t["transaction_date"] for t in alert.trades)
            hist = _get_price_history(alert.ticker, earliest)
            if not hist.empty:
                hist_long = hist.melt(id_vars="Date", var_name="Symbol", value_name="Value")
                y_min = hist_long["Value"].min()
                y_max = hist_long["Value"].max()
                pad = max((y_max - y_min) * 0.2, 1.5)
                chart = (
                    alt.Chart(hist_long)
                    .mark_line(strokeWidth=2)
                    .encode(
                        x=alt.X("Date:T", title=None),
                        y=alt.Y(
                            "Value:Q",
                            scale=alt.Scale(domain=[y_min - pad, y_max + pad]),
                            title="Indexed to 100 at first trade",
                        ),
                        color=alt.Color(
                            "Symbol:N",
                            scale=alt.Scale(
                                domain=[alert.ticker, "SPY"],
                                range=["#3a86ff", "#e63946"],
                            ),
                            legend=alt.Legend(orient="bottom"),
                        ),
                        tooltip=[
                            alt.Tooltip("Date:T", title="Date"),
                            alt.Tooltip("Symbol:N", title="Symbol"),
                            alt.Tooltip("Value:Q", format=".1f", title="Value"),
                        ],
                    )
                    .properties(height=320, width="container")
                )
                st.caption(f"Price performance since first trade ({earliest}) — indexed to 100 at open")
                st.altair_chart(chart)


def _render_trades(trades: list[dict], days: int, win_rates: dict):
    df = pd.DataFrame(trades)
    if df.empty:
        st.info("No trades in the current window.")
        return

    df["type"] = df["type"].apply(_fmt_type)
    df["win_rate"] = df["representative"].apply(
        lambda n: win_rates.get(n, {}).get("win_rate")
    )
    df["committees"] = df["representative"].apply(
        lambda n: ", ".join(get_member_committees(n).get("committees", [])[:2]) or "—"
    )
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    df = df.sort_values("transaction_date", ascending=False)

    st.caption(f"{len(df)} trade(s) shown — click a row to open member detail")

    event = st.dataframe(
        df,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "representative": st.column_config.TextColumn("Member", pinned=True),
            "ticker": st.column_config.TextColumn("Ticker"),
            "type": st.column_config.TextColumn("Type"),
            "amount": st.column_config.TextColumn("Amount"),
            "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
            "disclosure_date": st.column_config.TextColumn("Disclosed"),
            "ptr_link": st.column_config.LinkColumn("Filing"),
            "win_rate": st.column_config.ProgressColumn(
                "Win Rate", min_value=0, max_value=1, format="percent"
            ),
            "committees": st.column_config.TextColumn("Committees"),
            "asset_description": None,
            "owner": st.column_config.TextColumn("Owner"),
            "chamber": None,
        },
    )

    if event.selection.rows:
        selected_name = df.iloc[event.selection.rows[0]]["representative"]
        _member_dialog(selected_name, days)


def _render_leaderboard(win_rates: dict, days: int):
    rows = [
        {
            "Member": name,
            "Win Rate": stats["win_rate"],
            "Wins": stats["wins"],
            "Scored": stats["total"],
            "Qualifies": stats["qualifies"],
        }
        for name, stats in win_rates.items()
        if stats["total"] > 0
    ]

    if not rows:
        st.info("No members with scoreable trades in this window. Try increasing the days window in the sidebar.")
        return

    df = pd.DataFrame(rows).sort_values("Win Rate", ascending=False)

    show_all = st.toggle("Show non-qualifying members", value=False)
    if not show_all:
        qualifying = df[df["Qualifies"]]
        if qualifying.empty:
            st.info(
                f"No members qualify yet (need ≥{config.WIN_RATE_MIN:.0%} win rate "
                f"and ≥{config.WIN_RATE_MIN_TRADES} scored trades). "
                "Toggle above to see all members."
            )
            return
        df = qualifying

    event = st.dataframe(
        df,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Win Rate": st.column_config.ProgressColumn(
                f"Win Rate vs SPY ({config.WIN_RATE_PRIMARY}d)",
                min_value=0,
                max_value=1,
                format="percent",
            ),
            "Wins": st.column_config.NumberColumn("Wins"),
            "Scored": st.column_config.NumberColumn("Scored Trades"),
            "Qualifies": st.column_config.CheckboxColumn(
                f"✓ Qualifies (≥{config.WIN_RATE_MIN:.0%}, ≥{config.WIN_RATE_MIN_TRADES} trades)"
            ),
        },
    )
    if event.selection.rows:
        selected_name = df.iloc[event.selection.rows[0]]["Member"]
        _member_dialog(selected_name, days)

    st.caption(
        f"Win = member's {config.WIN_RATE_PRIMARY}-day return beats SPY over the same period. "
        "Only purchases are scored. Trades without a full forward window are excluded."
    )


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title(":material/monitoring: Trade Monitor")
    days = st.slider("Days window", min_value=7, max_value=90, value=45)


# ── Load data ──────────────────────────────────────────────────────────────────

with st.status("Loading congressional trade data...", expanded=True) as _status:
    st.write("Loading committee assignments...")
    _load_committees()

    st.write("Fetching Senate and House trade disclosures (may take up to a minute on first load)...")
    _fetch_trades_raw()

    st.write("Scoring trades against SPY performance (yfinance)...")
    win_rates = _get_win_rates()

    st.write("Detecting cluster, win-rate, and watchlist alerts...")
    alerts = _get_alerts(days)

    _status.update(label="Data loaded", state="complete", expanded=False)

trades = _filter_trades(days)

cluster_alerts = [a for a in alerts if a.tier == "cluster"]
winrate_alerts = [a for a in alerts if a.tier == "winrate"]
watchlist_alerts = [a for a in alerts if a.tier == "watchlist"]


# ── Header ─────────────────────────────────────────────────────────────────────

st.title(":material/monitoring: Congressional Trade Monitor")
st.caption(f"Showing **{len(trades)} trades** from the last **{days} days** · Senate + House disclosures")

st.space("small")

with st.container(horizontal=True):
    st.metric("Total Trades", len(trades), border=True)
    st.metric("⚡ Cluster Alerts", len(cluster_alerts), border=True)
    st.metric("🏆 Win-Rate Alerts", len(winrate_alerts), border=True)
    st.metric("👁️ Watchlist Alerts", len(watchlist_alerts), border=True)

st.space("small")

_render_summary(trades)

# ── Tabs ───────────────────────────────────────────────────────────────────────

tab_alerts, tab_trades, tab_leaderboard = st.tabs(
    ["🔔 Alerts", "📋 Trades", "🏆 Leaderboard"],
    on_change="rerun",
)

if tab_alerts.open:
    with tab_alerts:
        _render_alerts(alerts, win_rates)

if tab_trades.open:
    with tab_trades:
        _render_trades(trades, days, win_rates)

if tab_leaderboard.open:
    with tab_leaderboard:
        _render_leaderboard(win_rates, days)
