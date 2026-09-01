"""
fundamentals.py — Congressional Trade Monitor
Validated fundamental snapshots for the dashboard research layer.

Only emits a metric when the underlying data passes sanity checks. Callers
should treat absent fields as unknown — never as zero. These are yfinance
snapshots for screening context, not a substitute for Qualtrim TTM data.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from functools import lru_cache

import yfinance as yf

# ── Sanity bounds ─────────────────────────────────────────────────────────────

MAX_PE = 500.0
MAX_PEG = 50.0
MIN_EPS = 0.01
MIN_MARKET_CAP = 10_000_000  # $10M — below this, fundamentals are unreliable

TV_EXCHANGE = {
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
    "NYQ": "NYSE", "ASE": "AMEX", "BTS": "BATS", "PCX": "NYSE",
    "NASDAQ": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _finite(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _metric(value, *, source: str, reliable: bool = True, note: str = "") -> dict | None:
    """Wrap a scalar in the dashboard's fundamental-field shape."""
    v = _finite(value)
    if v is None:
        return None
    out = {"value": v, "source": source, "reliable": reliable}
    if note:
        out["note"] = note
    return out


def _pct_from_ratio(x) -> float | None:
    """Normalise yfinance growth/margin fields to a percentage."""
    v = _finite(x)
    if v is None:
        return None
    # Margins are 0–1; growth can be >1 for hypergrowth names.
    if abs(v) <= 1.5:
        return round(v * 100, 2)
    return round(v, 2)


def _valid_pe(pe: float | None) -> float | None:
    v = _finite(pe)
    if v is None or v <= 0 or v > MAX_PE:
        return None
    return round(v, 2)


def _valid_peg(peg: float | None) -> float | None:
    v = _finite(peg)
    if v is None or v <= 0 or v > MAX_PEG:
        return None
    return round(v, 2)


def _insider_seniority(title: str) -> str:
    t = title or ""
    if re.search(r"\b(CEO|CFO|Chief Executive|Chief Financial)\b", t, re.I):
        return "CEO/CFO"
    if re.search(r"\b(President|COO|CTO|Chief|EVP|SVP|VP|Vice President)\b", t, re.I):
        return "officer"
    if re.search(r"\bDirector\b", t, re.I):
        return "director"
    return "other"


def insider_seniority_bucket(title: str) -> str:
    """Public helper used by export for seniority breakdowns."""
    return _insider_seniority(title)


def tradingview_symbol(ticker: str, exchange: str | None) -> str:
    ex = TV_EXCHANGE.get((exchange or "").upper(), "NASDAQ")
    return f"{ex}:{ticker.upper()}"


def _quarterly_gross_margin(tk: yf.Ticker) -> tuple[float | None, str]:
    """
    Gross profit / total revenue for the most recent reported quarter.
    Preferred over info.grossMargins because it names the quarter.
    """
    try:
        stmt = tk.quarterly_income_stmt
        if stmt is None or stmt.empty:
            return None, ""
        if "Gross Profit" not in stmt.index or "Total Revenue" not in stmt.index:
            return None, ""
        gp = _finite(stmt.loc["Gross Profit"].iloc[0])
        rev = _finite(stmt.loc["Total Revenue"].iloc[0])
        if gp is None or rev is None or rev <= 0:
            return None, ""
        margin = round(gp / rev * 100, 2)
        col = stmt.columns[0]
        quarter = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
        return margin, quarter
    except Exception:
        return None, ""


def _gross_margin_trend(tk: yf.Ticker, n: int = 4) -> list[dict]:
    """Last n quarters of gross margin from reported financials."""
    out = []
    try:
        stmt = tk.quarterly_income_stmt
        if stmt is None or stmt.empty:
            return out
        if "Gross Profit" not in stmt.index or "Total Revenue" not in stmt.index:
            return out
        for col in list(stmt.columns)[:n]:
            gp = _finite(stmt.loc["Gross Profit", col])
            rev = _finite(stmt.loc["Total Revenue", col])
            if gp is None or rev is None or rev <= 0:
                continue
            d = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
            out.append({"date": d, "margin_pct": round(gp / rev * 100, 2)})
    except Exception:
        pass
    return out


def _historical_pe(tk: yf.Ticker, info: dict) -> dict | None:
    """
    Trailing PE at each quarter-end over ~5 years using price / TTM EPS.
    Returns min, max, median, current, and where current sits in the range.
    """
    try:
        hist = tk.history(period="5y", interval="1d", auto_adjust=True)
        fin = tk.quarterly_income_stmt
        if hist is None or hist.empty or fin is None or fin.empty:
            return None

        eps_row = None
        for label in ("Diluted EPS", "Basic EPS"):
            if label in fin.index:
                eps_row = fin.loc[label]
                break
        if eps_row is None:
            return None

        # Quarter dates oldest → newest
        quarters = []
        for col in reversed(list(fin.columns)):
            eps = _finite(eps_row[col])
            if eps is None or eps <= MIN_EPS:
                continue
            d = col.to_pydatetime() if hasattr(col, "to_pydatetime") else datetime.strptime(str(col)[:10], "%Y-%m-%d")
            quarters.append((d, eps))
        if len(quarters) < 4:
            return None

        pes = []
        for i in range(3, len(quarters)):
            end_date, _ = quarters[i]
            ttm_eps = sum(quarters[j][1] for j in range(i - 3, i + 1))
            if ttm_eps <= MIN_EPS:
                continue
            # Price on or just before quarter end
            sub = hist[hist.index <= end_date.replace(tzinfo=hist.index.tz)]
            if sub.empty:
                continue
            price = _finite(sub["Close"].iloc[-1])
            if price is None:
                continue
            pe = price / ttm_eps
            if 0 < pe <= MAX_PE:
                pes.append(round(pe, 2))

        if len(pes) < 3:
            return None

        current_pe = _valid_pe(info.get("trailingPE"))
        lo, hi = min(pes), max(pes)
        med = round(sorted(pes)[len(pes) // 2], 2)
        pctile = None
        if current_pe is not None and hi > lo:
            pctile = round((current_pe - lo) / (hi - lo) * 100, 1)

        return {
            "min": lo,
            "max": hi,
            "median": med,
            "current": current_pe,
            "percentile": pctile,
            "samples": len(pes),
            "source": "computed from quarterly EPS + daily prices",
            "reliable": True,
        }
    except Exception:
        return None


def _earnings_proximity(info: dict) -> dict | None:
    """Days until/since next known earnings date."""
    ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts))
        days = (dt.date() - datetime.now().date()).days
        label = "today" if days == 0 else (
            f"in {days}d" if days > 0 else f"{abs(days)}d ago"
        )
        return {
            "date": dt.strftime("%Y-%m-%d"),
            "days": days,
            "label": label,
            "source": "yfinance calendar",
            "reliable": True,
        }
    except (TypeError, ValueError, OSError):
        return None


def _headlines(tk: yf.Ticker, limit: int = 5) -> list[dict]:
    out = []
    try:
        for item in (tk.news or [])[:limit]:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            pub = item.get("publisher") or item.get("publisherName") or ""
            ts = item.get("providerPublishTime")
            date = ""
            if ts:
                try:
                    date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError):
                    pass
            out.append({
                "title": title,
                "publisher": pub,
                "date": date,
                "link": item.get("link") or "",
            })
    except Exception:
        pass
    return out


def _compute_peg(trailing_pe: float | None, earnings_growth_pct: float | None) -> dict | None:
    """
  Compute PEG only when earnings growth is meaningfully positive.
  PEG is undefined for zero/negative growth — return None instead of a lie.
    """
    if trailing_pe is None or earnings_growth_pct is None:
        return None
    if earnings_growth_pct <= 0:
        return None
    peg = trailing_pe / earnings_growth_pct
    v = _valid_peg(peg)
    if v is None:
        return None
    return _metric(v, source="trailing PE / yfinance earnings growth %", reliable=True,
                   note="Screening only — verify against Qualtrim TTM growth")


# ── Public API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=128)
def snapshot(ticker: str) -> dict:
    """
    Fundamental snapshot for one ticker. Cached per build run.

    Returns a dict with:
      - `fields`: only validated metrics (each has value, source, reliable)
      - `gross_margin_trend`: quarterly margin history
      - `historical_pe`: 5y PE range context
      - `earnings`: proximity to next earnings
      - `headlines`: recent news titles (not sentiment)
      - `links`: deep links for external research
      - `quality`: overall data-quality summary for bots
    """
    sym = (ticker or "").upper().strip()
    empty = {
        "ticker": sym,
        "fields": {},
        "gross_margin_trend": [],
        "historical_pe": None,
        "earnings": None,
        "headlines": [],
        "links": {},
        "quality": {"usable": False, "reason": "no data"},
    }
    if not sym:
        return empty

    try:
        tk = yf.Ticker(sym)
        info = tk.info or {}
    except Exception:
        return {**empty, "quality": {"usable": False, "reason": "fetch failed"}}

    if info.get("quoteType") not in (None, "EQUITY", "ETF"):
        return {**empty, "quality": {"usable": False, "reason": f"quote type {info.get('quoteType')}"}}

    mcap = _finite(info.get("marketCap"))
    if mcap is not None and mcap < MIN_MARKET_CAP:
        return {**empty, "quality": {"usable": False, "reason": "market cap below floor"}}

    fields = {}
    price = _finite(info.get("currentPrice") or info.get("regularMarketPrice"))
    if price is not None:
        fields["price"] = _metric(round(price, 2), source="yfinance", reliable=True)

    trailing_pe = _valid_pe(info.get("trailingPE"))
    if trailing_pe is not None:
        fields["trailing_pe"] = _metric(trailing_pe, source="yfinance trailingPE", reliable=True)

    forward_pe = _valid_pe(info.get("forwardPE"))
    if forward_pe is not None:
        fields["forward_pe"] = _metric(forward_pe, source="yfinance forwardPE", reliable=True)

    # PEG: prefer yfinance when sane; also compute independently for cross-check
    reported_peg = _valid_peg(info.get("pegRatio"))
    eg_pct = _pct_from_ratio(info.get("earningsGrowth"))
    computed_peg = _compute_peg(trailing_pe, eg_pct)

    if reported_peg is not None:
        reliable = True
        note = ""
        if computed_peg and abs(reported_peg - computed_peg["value"]) > max(1.0, reported_peg * 0.5):
            reliable = False
            note = "yfinance PEG disagrees with PE/growth cross-check — do not auto-filter"
        fields["peg"] = _metric(reported_peg, source="yfinance pegRatio", reliable=reliable, note=note)
    elif computed_peg:
        fields["peg"] = computed_peg

    gm, gm_q = _quarterly_gross_margin(tk)
    if gm is not None:
        fields["gross_margin_pct"] = _metric(
            gm, source=f"quarterly financials ({gm_q})", reliable=True,
        )
    else:
        gm_info = _pct_from_ratio(info.get("grossMargins"))
        if gm_info is not None:
            fields["gross_margin_pct"] = _metric(
                gm_info, source="yfinance grossMargins (TTM est.)", reliable=False,
                note="Prefer Qualtrim for TTM margin",
            )

    om = _pct_from_ratio(info.get("profitMargins"))
    if om is not None:
        fields["operating_margin_pct"] = _metric(
            om, source="yfinance profitMargins", reliable=False,
            note="Net margin, not operating — verify in Qualtrim",
        )

    rg = _pct_from_ratio(info.get("revenueGrowth"))
    if rg is not None:
        fields["revenue_growth_pct"] = _metric(rg, source="yfinance revenueGrowth", reliable=False)

    if eg_pct is not None:
        fields["earnings_growth_pct"] = _metric(eg_pct, source="yfinance earningsGrowth", reliable=False)

    if mcap is not None:
        fields["market_cap"] = _metric(mcap, source="yfinance", reliable=True)

    exchange = info.get("exchange") or info.get("fullExchangeName") or ""
    tv = tradingview_symbol(sym, exchange)

    # Price / gross profit per share (valuation context, not a filter signal)
    ttm_gp = None
    try:
        stmt = tk.quarterly_income_stmt
        if stmt is not None and not stmt.empty and "Gross Profit" in stmt.index:
            gps = [_finite(stmt.loc["Gross Profit", c]) for c in stmt.columns[:4]]
            gps = [g for g in gps if g is not None]
            shares = _finite(info.get("sharesOutstanding"))
            if len(gps) >= 4 and shares and shares > 0 and price:
                ttm_gp_ps = sum(gps) / shares
                if ttm_gp_ps > 0:
                    ttm_gp = round(price / ttm_gp_ps, 2)
    except Exception:
        pass
    if ttm_gp is not None and 0 < ttm_gp < 1000:
        fields["price_to_gross_profit"] = _metric(
            ttm_gp, source="price / TTM gross profit per share", reliable=False,
            note="Computed from last 4 quarters — screening context only",
        )

    hist_pe = _historical_pe(tk, info)
    trend = _gross_margin_trend(tk)
    earnings = _earnings_proximity(info)
    headlines = _headlines(tk)

    n_reliable = sum(1 for f in fields.values() if f and f.get("reliable"))
    usable = n_reliable >= 2 and price is not None

    return {
        "ticker": sym,
        "name": info.get("shortName") or info.get("longName") or "",
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "exchange": exchange,
        "fields": fields,
        "gross_margin_trend": trend,
        "historical_pe": hist_pe,
        "earnings": earnings,
        "headlines": headlines,
        "links": {
            "tradingview": f"https://www.tradingview.com/chart/?symbol={tv}",
            "qualtrim": f"https://www.qualtrim.com/stock/{sym}",
            "openinsider": f"http://openinsider.com/{sym}",
            "sec": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sym}&type=4&dateb=&owner=only&count=40",
            "yahoo": f"https://finance.yahoo.com/quote/{sym}",
        },
        "tradingview_symbol": tv,
        "quality": {
            "usable": usable,
            "reliable_fields": n_reliable,
            "reason": "ok" if usable else "insufficient validated fields",
        },
    }


def clear_cache() -> None:
    snapshot.cache_clear()
