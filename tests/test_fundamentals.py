"""Tests for fundamentals.py — validated metric emission."""

import math
from unittest.mock import MagicMock, patch

import fundamentals as fund


def test_finite_rejects_nan():
    assert fund._finite(float("nan")) is None
    assert fund._finite(12.5) == 12.5


def test_insider_seniority_buckets():
    assert fund.insider_seniority_bucket("CEO") == "CEO/CFO"
    assert fund.insider_seniority_bucket("Chief Financial Officer") == "CEO/CFO"
    assert fund.insider_seniority_bucket("VP Sales") == "officer"
    assert fund.insider_seniority_bucket("Director") == "director"


def test_tradingview_symbol_maps_exchange():
    assert fund.tradingview_symbol("nvda", "NMS") == "NASDAQ:NVDA"


def test_compute_peg_requires_positive_growth():
    assert fund._compute_peg(20.0, -5.0) is None
    peg = fund._compute_peg(20.0, 10.0)
    assert peg["value"] == 2.0


def test_snapshot_omits_invalid_pe():
    info = {
        "quoteType": "EQUITY",
        "marketCap": 50_000_000_000,
        "currentPrice": 100,
        "trailingPE": -5,
        "forwardPE": 800,
        "pegRatio": -1,
        "exchange": "NMS",
        "shortName": "Test Co",
    }
    mock_tk = MagicMock()
    mock_tk.info = info
    mock_tk.quarterly_income_stmt = None
    mock_tk.news = []

    with patch("fundamentals.yf.Ticker", return_value=mock_tk):
        fund.clear_cache()
        snap = fund.snapshot("TEST")

    assert "trailing_pe" not in snap["fields"]
    assert "forward_pe" not in snap["fields"]
    assert "peg" not in snap["fields"]
    assert snap["fields"]["price"]["value"] == 100


def test_snapshot_flags_unreliable_peg_when_cross_check_disagrees():
    info = {
        "quoteType": "EQUITY",
        "marketCap": 50_000_000_000,
        "currentPrice": 100,
        "trailingPE": 30,
        "pegRatio": 0.5,
        "earningsGrowth": 0.05,
        "exchange": "NMS",
    }
    mock_tk = MagicMock()
    mock_tk.info = info
    mock_tk.quarterly_income_stmt = None
    mock_tk.news = []

    with patch("fundamentals.yf.Ticker", return_value=mock_tk):
        fund.clear_cache()
        snap = fund.snapshot("TEST2")

    peg = snap["fields"].get("peg")
    assert peg is not None
    assert peg["reliable"] is False


def test_actionability_export_helper():
    import export

    assert export._actionability({"lag_days": 5, "excess": 2, "direction": "buy"})["score"] == "actionable"
    assert export._actionability({"lag_days": 40, "excess": 2, "direction": "buy"})["score"] == "stale"


def test_sector_heatmap_weights_cross_highest():
    import export

    rows = export._sector_heatmap([
        {"ticker": "AAA", "tier": "cross_cluster", "score": 80},
        {"ticker": "BBB", "tier": "watchlist", "score": 80},
    ])
    assert len(rows) >= 1
    if len(rows) >= 2:
        assert rows[0]["weight"] >= rows[1]["weight"]


def test_bot_brief_includes_disclaimer():
    import export

    brief = export._bot_brief({
        "ticker": "X",
        "name": "X Corp",
        "sector": "Tech",
        "signals": [],
        "fundamentals": {"fields": {}, "quality": {}},
        "actionability": {},
        "links": {},
    })
    assert "Qualtrim" in brief["disclaimer"]
