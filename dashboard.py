"""dashboard.py — Congressional Trade Monitor visual interface.

Streamlit app that imports existing modules directly (no API layer needed).
Data sources are all public — no secrets required for Streamlit Cloud deployment.

IMPORTANT: Does NOT call analyzer.analyze() because that function modifies
seen_trades.json (the deduplication state used by the real monitoring loop).
Instead, calls individual detectors directly.
"""

import streamlit as st
import pandas as pd

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

@st.cache_resource
def _load_committees():
    """Populate the committees module-level cache once per process."""
    load_all()


@st.cache_data(ttl="1h")
def _fetch_trades(days: int) -> list[dict]:
    return fetch_all(days=days)


@st.cache_data(ttl="1h")
def _get_win_rates(days: int) -> dict:
    trades = _fetch_trades(days)
    return compute_win_rates(trades)


@st.cache_data(ttl="1h")
def _get_alerts(days: int) -> list:
    trades = _fetch_trades(days)
    win_rates = _get_win_rates(days)
    cluster = detect_cluster_alerts(trades)
    winrate = detect_winrate_alerts(trades, win_rates)
    watchlist = detect_watchlist_alerts(trades)
    combined = cluster + winrate + watchlist
    combined.sort(key=lambda a: {"cluster": 0, "winrate": 1, "watchlist": 2}[a.tier])
    return combined


# ── Member detail dialog ───────────────────────────────────────────────────────

@st.dialog("Member Detail", width="large")
def _member_dialog(name: str, days: int):
    trades = _fetch_trades(days)
    win_rates = _get_win_rates(days)

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
                    "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
                    "disclosure_date": st.column_config.TextColumn("Disclosed"),
                    "ptr_link": st.column_config.LinkColumn("Filing"),
                    "ticker": st.column_config.TextColumn("Ticker"),
                    "type": st.column_config.TextColumn("Type"),
                    "amount": st.column_config.TextColumn("Amount"),
                    "chamber": st.column_config.TextColumn("Chamber"),
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


# ── Tab renderers ──────────────────────────────────────────────────────────────

def _render_alerts(alerts: list):
    if not alerts:
        st.info("No alerts in the current window.", icon=":material/check_circle:")
        return

    for alert in alerts:
        with st.container(border=True):
            if alert.tier == "cluster":
                st.error(f"**🔴 CLUSTER — {alert.ticker}**\n\n{alert.message}")
            elif alert.tier == "winrate":
                st.warning(f"**🟡 WIN-RATE — {alert.ticker}**\n\n{alert.message}")
            else:
                st.success(f"**🟢 WATCHLIST — {alert.ticker}**\n\n{alert.message}")

            trade_df = pd.DataFrame(alert.trades)
            cols = [c for c in ["representative", "ticker", "type", "amount", "transaction_date", "chamber"] if c in trade_df.columns]
            st.dataframe(
                trade_df[cols],
                hide_index=True,
                column_config={
                    "representative": st.column_config.TextColumn("Member"),
                    "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
                    "ticker": st.column_config.TextColumn("Ticker"),
                    "type": st.column_config.TextColumn("Type"),
                    "amount": st.column_config.TextColumn("Amount"),
                    "chamber": st.column_config.TextColumn("Chamber"),
                },
            )
            st.caption(f"Detected: {alert.fired_at[:16]}")


def _render_trades(trades: list[dict], chamber_filter: str, type_filter: str, days: int):
    df = pd.DataFrame(trades)
    if df.empty:
        st.info("No trades in the current window.")
        return

    if chamber_filter != "All":
        df = df[df["chamber"] == chamber_filter]
    if type_filter != "All":
        df = df[df["type"] == type_filter]

    if df.empty:
        st.info("No trades match the current filters.")
        return

    df["conflict"] = df.apply(
        lambda r: " | ".join(flag_conflicts(r["representative"], r["ticker"])) or "", axis=1
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
            "chamber": st.column_config.TextColumn("Chamber"),
            "ticker": st.column_config.TextColumn("Ticker"),
            "type": st.column_config.TextColumn("Type"),
            "amount": st.column_config.TextColumn("Amount"),
            "transaction_date": st.column_config.DateColumn("Tx Date", format="MMM DD, YYYY"),
            "disclosure_date": st.column_config.TextColumn("Disclosed"),
            "ptr_link": st.column_config.LinkColumn("Filing"),
            "conflict": st.column_config.TextColumn("⚠ Conflict"),
            "asset_description": None,
            "owner": st.column_config.TextColumn("Owner"),
        },
    )

    if event.selection.rows:
        selected_name = df.iloc[event.selection.rows[0]]["representative"]
        _member_dialog(selected_name, days)


def _render_leaderboard(win_rates: dict):
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

    st.dataframe(
        df,
        hide_index=True,
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

    st.caption(
        f"Win = member's {config.WIN_RATE_PRIMARY}-day return beats SPY over the same period. "
        "Only purchases are scored. Trades without a full forward window are excluded."
    )


def _render_members(trades: list[dict], win_rates: dict, days: int):
    if not trades:
        st.info("No trades in the current window.")
        return

    members = sorted({t["representative"] for t in trades})
    rows = []
    for m in members:
        member_trades = [t for t in trades if t["representative"] == m]
        chamber = next((t["chamber"] for t in member_trades), "")
        stats = win_rates.get(m)
        rows.append({
            "Member": m,
            "Chamber": chamber,
            "Trades": len(member_trades),
            "Win Rate": stats["win_rate"] if stats and stats["total"] > 0 else None,
            "Qualifies": stats["qualifies"] if stats else False,
        })

    member_df = pd.DataFrame(rows)

    st.caption("Click a row to open member detail — committees, conflicts, and recent trades.")

    event = st.dataframe(
        member_df,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Win Rate": st.column_config.ProgressColumn(
                "Win Rate vs SPY",
                min_value=0,
                max_value=1,
                format="percent",
            ),
            "Qualifies": st.column_config.CheckboxColumn("✓ Qualifies"),
        },
    )

    if event.selection.rows:
        selected_name = member_df.iloc[event.selection.rows[0]]["Member"]
        _member_dialog(selected_name, days)


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title(":material/monitoring: Trade Monitor")
    days = st.slider("Days window", min_value=7, max_value=90, value=45)
    chamber_filter = st.selectbox("Chamber", ["All", "Senate", "House"])
    type_filter = st.selectbox(
        "Trade type",
        ["All", "purchase", "sale", "sale_partial"],
        format_func=lambda x: {
            "All": "All types",
            "purchase": "Purchase",
            "sale": "Sale",
            "sale_partial": "Partial Sale",
        }.get(x, x),
    )
    st.divider()
    if st.button(":material/refresh: Refresh Data", type="primary"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Trade data and win rates are cached for 1 hour.")


# ── Load data ──────────────────────────────────────────────────────────────────

_load_committees()

with st.spinner("Fetching congressional trades from government sources..."):
    trades = _fetch_trades(days)

with st.spinner("Scoring win rates against SPY (yfinance)..."):
    win_rates = _get_win_rates(days)

with st.spinner("Detecting alerts..."):
    alerts = _get_alerts(days)

cluster_alerts = [a for a in alerts if a.tier == "cluster"]
winrate_alerts = [a for a in alerts if a.tier == "winrate"]
watchlist_alerts = [a for a in alerts if a.tier == "watchlist"]


# ── Header ─────────────────────────────────────────────────────────────────────

st.title(":material/monitoring: Congressional Trade Monitor")
st.caption(f"Showing **{len(trades)} trades** from the last **{days} days** · Senate + House disclosures")

st.space("small")

with st.container(horizontal=True):
    st.metric("Total Trades", len(trades), border=True)
    st.metric("🔴 Cluster Alerts", len(cluster_alerts), border=True)
    st.metric("🟡 Win-Rate Alerts", len(winrate_alerts), border=True)
    st.metric("🟢 Watchlist Alerts", len(watchlist_alerts), border=True)

st.space("small")


# ── Tabs ───────────────────────────────────────────────────────────────────────

tab_alerts, tab_trades, tab_leaderboard, tab_members = st.tabs(
    ["🔴 Alerts", "📋 Trades", "🏆 Leaderboard", "👤 Members"],
    on_change="rerun",
)

if tab_alerts.open:
    with tab_alerts:
        _render_alerts(alerts)

if tab_trades.open:
    with tab_trades:
        _render_trades(trades, chamber_filter, type_filter, days)

if tab_leaderboard.open:
    with tab_leaderboard:
        _render_leaderboard(win_rates)

if tab_members.open:
    with tab_members:
        _render_members(trades, win_rates, days)
