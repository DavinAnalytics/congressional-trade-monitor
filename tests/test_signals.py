"""
tests/test_signals.py — Congressional Trade Monitor

Covers the pure signal logic: amount parsing, disclosure lag, conviction
scoring, cross-signal detection, and alert-history forward scoring.

Network calls (yfinance, Gist) are monkeypatched out — these tests must run
offline and deterministically.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xml.etree import ElementTree as ET

import analyzer
import committees
import config
import export
import history
import notifier
import openinsider_fetcher
import review


# ── Fixtures ──────────────────────────────────────────────────────────────────

def trade(**overrides) -> dict:
    """A congressional trade dict with sensible defaults."""
    base = {
        "chamber":           "House",
        "representative":    "Jane Doe",
        "ticker":            "NVDA",
        "asset_description": "NVIDIA Corporation",
        "type":              "purchase",
        "transaction_date":  "2026-07-01",
        "disclosure_date":   "2026-07-10",
        "amount":            "$1,001 - $15,000",
        "ptr_link":          "https://example.test/filing.pdf",
        "owner":             "",
        "asset_type":        "stock",
    }
    return {**base, **overrides}


def insider_trade(**overrides) -> dict:
    base = {
        "name":             "Jensen Huang",
        "title":            "CEO",
        "ticker":           "NVDA",
        "type":             "purchase",
        "transaction_date": "2026-07-03",
        "disclosure_date":  "2026-07-05",
        "amount":           "$2,400,000",
        "source":           "insider",
        "ptr_link":         "http://openinsider.test/NVDA",
    }
    return {**base, **overrides}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly rather than hitting yfinance if a test forgets to stub prices."""
    monkeypatch.setattr(analyzer, "_download_closes", lambda *a, **k: None)
    analyzer._PRICE_CACHE.clear()


# ── parse_amount_value ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("$1,001 - $15,000",       8_000.5),
    ("$100,001 - $250,000",    175_000.5),
    ("$50,000,000 +",          50_000_000.0),
    ("$2,400,000",             2_400_000.0),
    ("",                       0.0),
    ("None",                   0.0),
    ("no numbers here",        0.0),
])
def test_parse_amount_value(raw, expected):
    assert analyzer.parse_amount_value(raw) == expected


# ── disclosure_lag_days ───────────────────────────────────────────────────────

def test_lag_house_and_senate_normalized_dates():
    assert analyzer.disclosure_lag_days(
        trade(transaction_date="2026-07-01", disclosure_date="2026-07-10")
    ) == 9


def test_lag_missing_disclosure_date_is_none():
    assert analyzer.disclosure_lag_days(trade(disclosure_date="")) is None


def test_lag_unparseable_disclosure_date_is_none():
    assert analyzer.disclosure_lag_days(trade(disclosure_date="07/10/2026")) is None


def test_lag_never_negative():
    """A disclosure dated before the trade is bad data, not a negative lag."""
    assert analyzer.disclosure_lag_days(
        trade(transaction_date="2026-07-10", disclosure_date="2026-07-01")
    ) == 0


# ── enrich_and_score ──────────────────────────────────────────────────────────

def _alert(tier, trades, ticker="NVDA"):
    return analyzer.Alert(tier=tier, ticker=ticker, trades=trades, message=f"{tier} {ticker}")


def test_strong_cross_signal_outranks_weak_stale_watchlist():
    """The whole point of scoring: size, breadth and freshness must dominate tier."""
    strong = _alert("cross_cluster", [
        trade(representative="A", amount="$500,001 - $1,000,000",
              transaction_date="2026-07-01", disclosure_date="2026-07-04", source="congress"),
        trade(representative="B", amount="$250,001 - $500,000",
              transaction_date="2026-07-02", disclosure_date="2026-07-05", source="congress"),
        trade(representative="C", amount="$500,001 - $1,000,000",
              transaction_date="2026-07-03", disclosure_date="2026-07-06", source="congress"),
        insider_trade(),
    ])
    weak = _alert("watchlist", [
        trade(representative="D", amount="$1,001 - $15,000",
              transaction_date="2026-06-01", disclosure_date="2026-07-11"),
    ], ticker="T")

    analyzer.enrich_and_score(strong, win_rates={})
    analyzer.enrich_and_score(weak, win_rates={})

    assert strong.score > weak.score


def test_dollar_size_separates_otherwise_identical_alerts():
    big = _alert("cluster", [
        trade(representative="A", amount="$500,001 - $1,000,000"),
        trade(representative="B", amount="$500,001 - $1,000,000"),
    ])
    small = _alert("cluster", [
        trade(representative="A", amount="$1,001 - $15,000"),
        trade(representative="B", amount="$1,001 - $15,000"),
    ])

    analyzer.enrich_and_score(big, win_rates={})
    analyzer.enrich_and_score(small, win_rates={})

    assert big.score > small.score


def test_fresh_disclosure_outranks_stale():
    fresh = _alert("cluster", [
        trade(representative="A", transaction_date="2026-07-01", disclosure_date="2026-07-03"),
        trade(representative="B", transaction_date="2026-07-02", disclosure_date="2026-07-04"),
    ])
    stale = _alert("cluster", [
        trade(representative="A", transaction_date="2026-07-01", disclosure_date="2026-08-14"),
        trade(representative="B", transaction_date="2026-07-02", disclosure_date="2026-08-15"),
    ])

    analyzer.enrich_and_score(fresh, win_rates={})
    analyzer.enrich_and_score(stale, win_rates={})

    assert fresh.score > stale.score
    assert fresh.meta["median_lag_days"] == 2
    assert stale.meta["median_lag_days"] == 44


def test_missing_lag_scores_between_fresh_and_stale():
    """Unknown lag is missing data, not staleness — it must not bury an alert."""
    def make(disclosure):
        a = _alert("cluster", [
            trade(representative="A", disclosure_date=disclosure),
            trade(representative="B", disclosure_date=disclosure),
        ])
        analyzer.enrich_and_score(a, win_rates={})
        return a

    fresh   = make("2026-07-02")
    unknown = make("")
    stale   = make("2026-08-14")

    assert unknown.meta["median_lag_days"] is None
    assert stale.score < unknown.score < fresh.score


def test_meta_counts_split_congress_and_insiders():
    alert = _alert("cross_cluster", [
        trade(representative="A", source="congress"),
        trade(representative="B", source="congress"),
        insider_trade(),
    ])
    analyzer.enrich_and_score(alert, win_rates={})

    assert alert.meta["n_members"] == 2
    assert alert.meta["n_insiders"] == 1
    assert alert.meta["members"] == ["A", "B"]
    assert alert.meta["has_top_insider"] is True


def test_track_record_only_counts_members_with_enough_trades():
    alert = _alert("watchlist", [trade(representative="A")])
    thin  = {"A": {"win_rate": 0.9, "total": config.WIN_RATE_MIN_TRADES - 1}}
    analyzer.enrich_and_score(alert, win_rates=thin)
    assert alert.meta["best_win_rate"] == 0.0

    alert2 = _alert("watchlist", [trade(representative="A")])
    solid  = {"A": {"win_rate": 0.9, "total": config.WIN_RATE_MIN_TRADES}}
    analyzer.enrich_and_score(alert2, win_rates=solid)
    assert alert2.meta["best_win_rate"] == 0.9
    assert alert2.score > alert.score


def test_score_stays_in_range():
    alert = _alert("cross_cluster", [
        trade(representative=f"M{i}", amount="$1,000,001 - $5,000,000",
              transaction_date="2026-07-01", disclosure_date="2026-07-01")
        for i in range(10)
    ] + [insider_trade()])
    analyzer.enrich_and_score(alert, win_rates={})
    assert 0.0 <= alert.score <= 100.0


def test_price_move_recorded_when_prices_available(monkeypatch):
    prices = {("NVDA", "first"): 100.0, ("SPY", "first"): 400.0}
    latest = {"NVDA": 120.0, "SPY": 420.0}
    monkeypatch.setattr(analyzer, "_get_price", lambda t, d: prices[(t, "first")])
    monkeypatch.setattr(analyzer, "latest_price", lambda t: latest[t])

    alert = _alert("cluster", [trade(representative="A"), trade(representative="B")])
    analyzer.enrich_and_score(alert, win_rates={})

    assert alert.meta["pct_since_trade"] == pytest.approx(20.0)
    assert alert.meta["spy_since_trade"] == pytest.approx(5.0)
    assert alert.meta["excess_since_trade"] == pytest.approx(15.0)


def test_price_move_is_none_when_prices_unavailable():
    alert = _alert("cluster", [trade(representative="A")])
    analyzer.enrich_and_score(alert, win_rates={})
    assert alert.meta["pct_since_trade"] is None
    assert alert.meta["excess_since_trade"] is None


# ── find_cross_signals ────────────────────────────────────────────────────────

def test_cross_signal_matches_shared_ticker_in_window():
    matches = analyzer.find_cross_signals(
        [trade(transaction_date="2026-07-01")],
        [insider_trade(transaction_date="2026-07-03")],
    )
    assert len(matches) == 1
    assert matches[0]["ticker"] == "NVDA"
    assert matches[0]["span_days"] == 2


def test_cross_signal_ignores_congressional_sales():
    matches = analyzer.find_cross_signals(
        [trade(type="sale")],
        [insider_trade()],
    )
    assert matches == []


def test_cross_signal_requires_overlapping_ticker():
    matches = analyzer.find_cross_signals(
        [trade(ticker="AAPL")],
        [insider_trade(ticker="NVDA")],
    )
    assert matches == []


def test_cross_signal_drops_pairs_outside_window():
    matches = analyzer.find_cross_signals(
        [trade(transaction_date="2026-01-01")],
        [insider_trade(transaction_date="2026-07-03")],
    )
    assert matches == []


def test_stale_outlier_does_not_discard_a_tight_pairing():
    """
    Regression: proximity is pairwise, not a span across every trade on the
    ticker. An unrelated older buy must not veto a tight Congress/insider match.
    """
    matches = analyzer.find_cross_signals(
        [
            trade(representative="Old", transaction_date="2026-01-01"),
            trade(representative="Fresh", transaction_date="2026-07-01"),
        ],
        [insider_trade(transaction_date="2026-07-03")],
    )

    assert len(matches) == 1
    assert [t["representative"] for t in matches[0]["congress"]] == ["Fresh"]
    assert matches[0]["span_days"] == 2


def test_out_of_window_trades_are_excluded_from_the_match():
    """Only the trades actually in the overlap should reach the alert."""
    matches = analyzer.find_cross_signals(
        [trade(representative="Fresh", transaction_date="2026-07-01")],
        [
            insider_trade(name="Near", transaction_date="2026-07-03"),
            insider_trade(name="Far",  transaction_date="2026-02-01"),
        ],
    )

    assert len(matches) == 1
    assert [t["name"] for t in matches[0]["insider"]] == ["Near"]


def test_chained_trades_each_pair_with_the_other_side():
    """
    Congress buys 40 days apart both sit within the window of the insider buy
    between them, so both belong in the signal even though they are 40 days
    from each other.
    """
    matches = analyzer.find_cross_signals(
        [
            trade(representative="A", transaction_date="2026-06-01"),
            trade(representative="B", transaction_date="2026-07-11"),
        ],
        [insider_trade(transaction_date="2026-06-21")],
    )

    assert len(matches) == 1
    assert {t["representative"] for t in matches[0]["congress"]} == {"A", "B"}
    assert matches[0]["span_days"] == 40


def test_span_reflects_only_included_trades():
    matches = analyzer.find_cross_signals(
        [
            trade(representative="Old",   transaction_date="2025-12-01"),
            trade(representative="Fresh", transaction_date="2026-07-02"),
        ],
        [insider_trade(transaction_date="2026-07-02")],
    )
    assert matches[0]["span_days"] == 0


# ── history ───────────────────────────────────────────────────────────────────

@pytest.fixture
def stub_state(monkeypatch):
    """In-memory replacement for the Gist/file-backed state store."""
    store: dict[str, object] = {}
    monkeypatch.setattr(history, "state_read", lambda name, default: store.get(name, default))
    monkeypatch.setattr(history, "state_write", lambda name, data: store.__setitem__(name, data))
    return store


def test_record_alerts_captures_entry_prices(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0 if t == "NVDA" else 400.0)

    alert = _alert("cluster", [trade(representative="A"), trade(representative="B")])
    analyzer.enrich_and_score(alert, win_rates={})

    assert history.record_alerts([alert]) == 1
    (record,) = stub_state[config.HISTORY_FILE]
    assert record["ticker"] == "NVDA"
    assert record["entry_price"] == 100.0
    assert record["spy_entry"] == 400.0
    assert record["entry_date"] == "2026-07-01"
    assert record["n_members"] == 2


def test_record_alerts_is_idempotent(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)

    alert = _alert("cluster", [trade(representative="A")])
    analyzer.enrich_and_score(alert, win_rates={})

    assert history.record_alerts([alert]) == 1
    assert history.record_alerts([alert]) == 0
    assert len(stub_state[config.HISTORY_FILE]) == 1


def test_score_history_computes_excess_over_spy(stub_state, monkeypatch):
    entry = datetime(2026, 1, 1)
    stub_state[config.HISTORY_FILE] = [{
        "id": "cluster|NVDA|2026-01-01", "fired_date": "2026-01-01",
        "entry_date": "2026-01-01", "tier": "cluster", "ticker": "NVDA",
        "score": 75.0, "direction": "buy", "entry_price": 100.0, "spy_entry": 400.0,
    }]

    # Ticker +20%, SPY +5% at every horizon → +15% excess.
    monkeypatch.setattr(history, "_get_price",
                        lambda t, d: 120.0 if t == "NVDA" else 420.0)

    scored = history.score_history(today=entry + timedelta(days=200))
    assert scored == len(config.WIN_RATE_WINDOWS)

    (record,) = stub_state[config.HISTORY_FILE]
    for window in config.WIN_RATE_WINDOWS:
        assert record[f"ret_{window}"] == pytest.approx(20.0)
        assert record[f"spy_{window}"] == pytest.approx(5.0)
        assert record[f"excess_{window}"] == pytest.approx(15.0)
        assert record[f"edge_{window}"] == pytest.approx(15.0)


def test_score_history_skips_unmatured_windows(stub_state, monkeypatch):
    entry = datetime(2026, 1, 1)
    stub_state[config.HISTORY_FILE] = [{
        "id": "cluster|NVDA|2026-01-01", "fired_date": "2026-01-01",
        "entry_date": "2026-01-01", "tier": "cluster", "ticker": "NVDA",
        "score": 75.0, "direction": "buy", "entry_price": 100.0, "spy_entry": 400.0,
    }]
    monkeypatch.setattr(history, "_get_price", lambda t, d: 120.0 if t == "NVDA" else 420.0)

    # 45 days in: only the 30-day window has elapsed.
    history.score_history(today=entry + timedelta(days=45))
    (record,) = stub_state[config.HISTORY_FILE]
    assert "edge_30" in record
    assert "edge_60" not in record
    assert "edge_90" not in record


def test_score_history_does_not_rescore(stub_state, monkeypatch):
    stub_state[config.HISTORY_FILE] = [{
        "id": "cluster|NVDA|2026-01-01", "entry_date": "2026-01-01",
        "tier": "cluster", "ticker": "NVDA", "score": 75.0,
        "entry_price": 100.0, "spy_entry": 400.0, "direction": "buy",
        **{f"edge_{w}": 1.0 for w in config.WIN_RATE_WINDOWS},
    }]
    monkeypatch.setattr(history, "_get_price", lambda t, d: 999.0)

    assert history.score_history(today=datetime(2027, 1, 1)) == 0


def test_performance_summary_aggregates_by_tier_and_bucket(stub_state):
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cluster",       "ticker": "A", "score": 80.0, "direction": "buy",  f"edge_{w}":  10.0},
        {"id": "2", "tier": "cluster",       "ticker": "B", "score": 75.0, "direction": "sell", f"edge_{w}":  -2.0},
        {"id": "3", "tier": "cross_cluster", "ticker": "C", "score": 90.0, "direction": "buy",  f"edge_{w}":  20.0},
        {"id": "4", "tier": "watchlist",     "ticker": "D", "score": 20.0, "direction": "buy",  f"edge_{w}": -10.0},
        {"id": "5", "tier": "watchlist",     "ticker": "E", "score": 25.0, "direction": "buy"},  # unmatured
    ]

    summary = history.performance_summary()

    assert summary["total"] == 5
    assert summary["unmatured"] == 1
    cluster = summary["by_tier"]["cluster"]
    assert (cluster["n"], cluster["hit_rate"], cluster["avg_edge"]) == (2, 0.5, 4.0)
    assert summary["by_tier"]["cross_cluster"]["hit_rate"] == 1.0
    assert summary["by_bucket"]["70+"]["n"] == 3
    assert summary["by_bucket"]["0-40"]["n"] == 1
    assert summary["by_bucket"]["40-70"]["n"] == 0


def test_history_is_capped_at_max_records(stub_state, monkeypatch):
    """The Gist truncates past ~1MB without erroring, so the log must self-limit."""
    monkeypatch.setattr(config, "HISTORY_MAX_RECORDS", 5)
    history.save_history([{"id": str(i), "tier": "cluster", "ticker": "A"} for i in range(12)])

    kept = stub_state[config.HISTORY_FILE]
    assert len(kept) == 5
    # Oldest dropped, newest retained.
    assert [r["id"] for r in kept] == ["7", "8", "9", "10", "11"]


def test_history_under_cap_is_untouched(stub_state, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_MAX_RECORDS", 100)
    records = [{"id": str(i)} for i in range(3)]
    history.save_history(records)
    assert stub_state[config.HISTORY_FILE] == records


# ── Seen-state pruning ────────────────────────────────────────────────────────

def test_prune_drops_keys_past_the_retention_window():
    today = datetime(2026, 8, 8)
    old   = "Doe, Jane|NVDA|2026-01-01|purchase"   # >120 days back
    fresh = "Doe, Jane|NVDA|2026-08-01|purchase"

    kept = analyzer._prune_seen({old, fresh}, today=today)
    assert kept == {fresh}


def test_prune_dates_composite_keys_by_their_newest_trade():
    """
    A cluster key holds several trade dates. It stays alive while its newest
    member is in window — dropping it on the oldest would re-alert the cluster.
    """
    today = datetime(2026, 8, 8)
    cluster = (
        "cluster|APH|buy|Larsen, Rick|APH|2026-01-02|purchase"
        "|Newhouse, Dan|APH|2026-08-01|purchase"
    )
    assert analyzer._prune_seen({cluster}, today=today) == {cluster}

    stale = "cluster|APH|buy|A|APH|2026-01-02|purchase|B|APH|2026-01-10|purchase"
    assert analyzer._prune_seen({stale}, today=today) == set()


def test_prune_keeps_keys_with_no_parseable_date():
    """An unrecognized key shape must never be dropped — that would re-alert it."""
    weird = "some-legacy-key-with-no-date"
    assert analyzer._prune_seen({weird}, today=datetime(2026, 8, 8)) == {weird}


def test_prune_keeps_everything_inside_the_window():
    today = datetime(2026, 8, 8)
    keys = {
        f"Doe, Jane|NVDA|2026-0{m}-01|purchase" for m in (6, 7, 8)
    }
    assert analyzer._prune_seen(keys, today=today) == keys


def test_save_seen_prunes_before_writing(monkeypatch):
    written = {}
    monkeypatch.setattr(analyzer, "state_write", lambda name, data: written.update({name: data}))

    old   = "Doe, Jane|NVDA|2020-01-01|purchase"
    fresh = f"Doe, Jane|NVDA|{datetime.now().strftime('%Y-%m-%d')}|purchase"
    analyzer._save_seen({old, fresh})

    assert written[config.SEEN_TRADES_FILE] == [fresh]


# ── Price cache bounding ──────────────────────────────────────────────────────

def test_price_cache_evicts_when_full(monkeypatch):
    """run_forever() lives indefinitely, so the memo must not grow without bound."""
    monkeypatch.setattr(analyzer, "_PRICE_CACHE_MAX", 100)
    analyzer._PRICE_CACHE.clear()

    for i in range(150):
        analyzer._cache_price(("T", f"2026-01-{i:03d}", "first"), float(i))

    assert len(analyzer._PRICE_CACHE) <= 100
    # The most recent write always survives eviction.
    assert analyzer._PRICE_CACHE[("T", "2026-01-149", "first")] == 149.0


# ── Direction awareness ───────────────────────────────────────────────────────

def test_direction_tracks_the_congressional_leg():
    buy  = _alert("cluster", [trade(representative="A", type="purchase")])
    sell = _alert("cluster", [trade(representative="A", type="sale")])
    part = _alert("cluster", [trade(representative="A", type="sale_partial")])

    for a in (buy, sell, part):
        analyzer.enrich_and_score(a, win_rates={})

    assert buy.meta["direction"] == "buy"
    assert sell.meta["direction"] == "sell"
    assert part.meta["direction"] == "sell"


def test_cross_signals_are_always_buys():
    alert = _alert("cross_cluster", [
        trade(representative="A", source="congress"), insider_trade(),
    ])
    analyzer.enrich_and_score(alert, win_rates={})
    assert alert.meta["direction"] == "buy"


def test_congress_and_insider_dollars_are_tracked_apart():
    """A large insider buy must not inflate apparent congressional conviction."""
    alert = _alert("cross_cluster", [
        trade(representative="A", amount="$1,001 - $15,000", source="congress"),
        insider_trade(amount="$2,400,000"),
    ])
    analyzer.enrich_and_score(alert, win_rates={})

    assert alert.meta["congress_dollars"] == pytest.approx(8_000.5)
    assert alert.meta["insider_dollars"] == pytest.approx(2_400_000.0)
    assert alert.meta["dollar_total"] == pytest.approx(2_408_000.5)


def test_score_sizes_on_congressional_dollars_only():
    """Otherwise a whale insider buy would outrank real congressional conviction."""
    small_congress = _alert("cross_cluster", [
        trade(representative="A", amount="$1,001 - $15,000", source="congress"),
        insider_trade(amount="$50,000,000"),
    ])
    big_congress = _alert("cross_cluster", [
        trade(representative="A", amount="$500,001 - $1,000,000", source="congress"),
        insider_trade(amount="$60,000"),
    ])
    for a in (small_congress, big_congress):
        analyzer.enrich_and_score(a, win_rates={})

    assert big_congress.score > small_congress.score


def test_sell_alert_edge_is_inverted(stub_state, monkeypatch):
    """
    The bug this guards: a sell cluster followed by a rally used to be recorded
    as a win, because excess return was aggregated without regard to direction.
    """
    entry = datetime(2026, 1, 1)
    stub_state[config.HISTORY_FILE] = [
        {"id": "buy", "entry_date": "2026-01-01", "tier": "cluster", "ticker": "NVDA",
         "score": 60.0, "direction": "buy", "entry_price": 100.0, "spy_entry": 400.0},
        {"id": "sell", "entry_date": "2026-01-01", "tier": "cluster", "ticker": "NVDA",
         "score": 60.0, "direction": "sell", "entry_price": 100.0, "spy_entry": 400.0},
    ]
    # NVDA +20%, SPY +5% → +15% excess for both records.
    monkeypatch.setattr(history, "_get_price", lambda t, d: 120.0 if t == "NVDA" else 420.0)

    history.score_history(today=entry + timedelta(days=200))
    buy_rec, sell_rec = stub_state[config.HISTORY_FILE]

    w = config.WIN_RATE_PRIMARY
    assert buy_rec[f"excess_{w}"] == pytest.approx(15.0)
    assert sell_rec[f"excess_{w}"] == pytest.approx(15.0)   # same raw excess
    assert buy_rec[f"edge_{w}"] == pytest.approx(15.0)      # buy was right
    assert sell_rec[f"edge_{w}"] == pytest.approx(-15.0)    # sell was wrong


def test_legacy_records_without_direction_are_excluded(stub_state):
    """Pre-direction records are unscoreable — counting them would invert sells."""
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cluster", "ticker": "A", "score": 80.0, f"edge_{w}": 10.0},
        {"id": "2", "tier": "cluster", "ticker": "B", "score": 80.0,
         "direction": "buy", f"edge_{w}": 4.0},
    ]
    summary = history.performance_summary()

    assert summary["legacy"] == 1
    assert summary["total"] == 1
    assert summary["by_tier"]["cluster"]["n"] == 1
    assert summary["by_tier"]["cluster"]["avg_edge"] == pytest.approx(4.0)


def test_summary_breaks_out_by_direction(stub_state):
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cluster", "ticker": "A", "score": 80.0,
         "direction": "buy",  f"edge_{w}": 10.0},
        {"id": "2", "tier": "cluster", "ticker": "B", "score": 80.0,
         "direction": "sell", f"edge_{w}": -6.0},
    ]
    summary = history.performance_summary()

    assert summary["by_direction"]["buy"]["hit_rate"] == 1.0
    assert summary["by_direction"]["sell"]["hit_rate"] == 0.0


# ── Monthly review recommendations ────────────────────────────────────────────

def _records(n, tier="cluster", edge=0.0, direction="buy", **extra):
    w = config.WIN_RATE_PRIMARY
    return [
        {"id": f"{tier}{direction}{i}", "tier": tier, "ticker": "A", "score": 60.0,
         "direction": direction, f"edge_{w}": edge, f"act_edge_{w}": edge, **extra}
        for i in range(n)
    ]


def test_review_says_pending_before_there_is_data(stub_state):
    stub_state[config.HISTORY_FILE] = []
    stub_state[config.CONTROL_FILE] = []
    findings = review.build_recommendations()
    assert findings[0].severity == "pending"
    assert not findings[0].action


def test_review_confirms_a_working_monitor(stub_state):
    stub_state[config.HISTORY_FILE] = _records(25, edge=6.0)
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=0.1)
    overall = review.build_recommendations()[0]
    assert overall.severity == "good"
    assert "beating the baseline" in overall.headline


def test_review_flags_a_monitor_that_underperforms_its_baseline(stub_state):
    stub_state[config.HISTORY_FILE] = _records(25, edge=-4.0)
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=2.0)
    overall = review.build_recommendations()[0]
    assert overall.severity == "action"
    assert overall.action


def test_review_recommends_disabling_a_losing_tier(stub_state):
    stub_state[config.HISTORY_FILE] = (
        _records(25, tier="watchlist", edge=-3.0) + _records(25, tier="cluster", edge=4.0)
    )
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=0.0)
    findings = review.build_recommendations()

    disable = [f for f in findings if "disable the watchlist alert tier" in f.action]
    assert disable, "expected a copy-pasteable disable action for the losing tier"
    assert disable[0].severity == "action"


def test_review_recommends_dropping_unhelpful_sells(stub_state):
    stub_state[config.HISTORY_FILE] = _records(25, direction="sell", edge=-3.0)
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=0.0)
    findings = review.build_recommendations()
    assert any("stop alerting on sell clusters" in f.action for f in findings)


def test_review_recommends_reverting_the_director_widening(stub_state):
    stub_state[config.HISTORY_FILE] = (
        _records(25, tier="cross_cluster", edge=8.0, has_top_insider=True)
        + _records(25, tier="cross_cluster", edge=-2.0, has_top_insider=False)
    )
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=0.0)
    findings = review.build_recommendations()
    assert any("narrow the insider screener back to CEO/CFO" in f.action for f in findings)


def test_review_stays_silent_below_the_recommendation_threshold(stub_state):
    """Wide intervals are honest; advising a config change off 6 points is not."""
    stub_state[config.HISTORY_FILE] = _records(6, tier="watchlist", edge=-3.0)
    stub_state[config.CONTROL_FILE] = _records(6, tier="control", edge=0.0)
    findings = review.build_recommendations()
    assert not any("disable" in f.action for f in findings)


def test_findings_render_with_copy_paste_instructions(stub_state):
    stub_state[config.HISTORY_FILE] = _records(25, tier="watchlist", edge=-3.0)
    stub_state[config.CONTROL_FILE] = _records(25, tier="control", edge=0.0)
    out = review.format_findings(review.build_recommendations())
    assert "To act on this, send me:" in out


# ── Significance & horizons ───────────────────────────────────────────────────

def test_bootstrap_flags_a_clear_positive_effect():
    b = history.bootstrap([8.0, 9.0, 10.0, 11.0, 12.0, 9.5, 10.5, 8.5])
    assert b["ci_low"] > 0
    assert b["confidence"] > 0.95


def test_bootstrap_finds_no_evidence_in_noise():
    """A positive average that luck explains easily must not read as real."""
    b = history.bootstrap([12.0, -11.0, 9.0, -8.0, 14.0, -13.0, 2.0, -3.0])
    assert b["mean"] > 0
    assert b["ci_low"] < 0 < b["ci_high"]     # straddles zero
    assert b["confidence"] < 0.95


def test_bootstrap_returns_none_below_minimum_samples():
    assert history.bootstrap([1.0, 2.0]) is None


def test_bootstrap_is_deterministic():
    """The interval must not jitter between runs on identical data."""
    v = [3.0, -1.0, 5.0, 2.0, -2.0, 4.0]
    assert history.bootstrap(v) == history.bootstrap(v)


def test_bootstrap_difference_detects_no_separation():
    """Identical populations: the detectors add nothing."""
    same = [5.0, -2.0, 7.0, 1.0, -4.0, 3.0]
    d = history.bootstrap_difference(same, list(same))
    assert d["ci_low"] < 0 < d["ci_high"]


def test_bootstrap_difference_detects_real_separation():
    alerted   = [10.0, 11.0, 12.0, 9.0, 10.5, 11.5, 10.0, 12.0]
    unalerted = [0.5, -1.0, 0.0, 1.0, -0.5, 0.2, 0.1, -0.2]
    d = history.bootstrap_difference(alerted, unalerted)
    assert d["ci_low"] > 0
    assert d["confidence"] > 0.95


def test_horizon_view_reports_consistency():
    recs = [{f"act_edge_{w}": 5.0 for w in config.WIN_RATE_WINDOWS} for _ in range(6)]
    h = history.horizon_view(recs)
    assert h["consistent"] is True
    assert h["per_window"][config.WIN_RATE_PRIMARY]["avg_edge"] == pytest.approx(5.0)


def test_horizon_view_flags_a_sign_flip():
    """Edge at one horizon only is the classic false positive."""
    recs = [{"act_edge_30": -2.0, "act_edge_60": 6.0, "act_edge_90": -1.0} for _ in range(6)]
    assert history.horizon_view(recs)["consistent"] is False


def test_horizon_view_is_undecided_without_enough_data():
    assert history.horizon_view([{"act_edge_60": 5.0}])["consistent"] is None


def test_summary_renders_significance_and_horizons(stub_state):
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": f"a{i}", "tier": "cluster", "ticker": "A", "score": 70.0,
         "direction": "buy", f"edge_{w}": 5.0, f"act_edge_{w}": 4.0}
        for i in range(8)
    ]
    stub_state[config.CONTROL_FILE] = [
        {"id": f"c{i}", "tier": "control", "ticker": "C", "score": 0.0,
         "direction": "buy", f"edge_{w}": 0.0, f"act_edge_{w}": 0.0}
        for i in range(8)
    ]
    out = history.format_summary(history.performance_summary())

    assert "Is the actionable edge real, or luck?" in out
    assert "alerted − un-alerted" in out
    assert "Does the edge hold across horizons?" in out


# ── Control group ─────────────────────────────────────────────────────────────

def test_control_excludes_trades_that_triggered_an_alert(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)

    alerted   = trade(representative="A", ticker="NVDA")
    unalerted = trade(representative="B", ticker="AAPL")
    alert = _alert("cluster", [alerted])

    history.record_control([alerted, unalerted], [alert], today=datetime(2026, 8, 8))

    (rec,) = stub_state[config.CONTROL_FILE]
    assert rec["ticker"] == "AAPL"
    assert rec["tier"] == "control"


def test_control_ignores_the_insider_leg_of_cross_signals(stub_state, monkeypatch):
    """Insider rows have no _trade_key and must not break the exclusion set."""
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)

    cong = trade(representative="A", ticker="NVDA", source="congress")
    alert = _alert("cross_cluster", [cong, insider_trade()])

    history.record_control([cong, trade(representative="B", ticker="AAPL")],
                           [alert], today=datetime(2026, 8, 8))

    tickers = {r["ticker"] for r in stub_state[config.CONTROL_FILE]}
    assert tickers == {"AAPL"}


def test_control_sample_is_capped(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)
    monkeypatch.setattr(config, "CONTROL_SAMPLE_PER_RUN", 5)

    trades = [trade(representative=f"M{i}", ticker=f"T{i}") for i in range(50)]
    added = history.record_control(trades, [], today=datetime(2026, 8, 8))

    assert added == 5
    assert len(stub_state[config.CONTROL_FILE]) == 5


def test_control_sampling_is_stable_for_a_given_day(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)
    monkeypatch.setattr(config, "CONTROL_SAMPLE_PER_RUN", 5)
    trades = [trade(representative=f"M{i}", ticker=f"T{i}") for i in range(50)]

    history.record_control(trades, [], today=datetime(2026, 8, 8))
    first = [r["id"] for r in stub_state[config.CONTROL_FILE]]

    stub_state[config.CONTROL_FILE] = []
    history.record_control(trades, [], today=datetime(2026, 8, 8))
    assert [r["id"] for r in stub_state[config.CONTROL_FILE]] == first


def test_control_records_direction_so_sells_invert(stub_state, monkeypatch):
    monkeypatch.setattr(history, "_get_price", lambda t, d: 100.0)
    history.record_control(
        [trade(representative="A", ticker="X", type="sale")], [],
        today=datetime(2026, 8, 8),
    )
    (rec,) = stub_state[config.CONTROL_FILE]
    assert rec["direction"] == "sell"


def test_summary_compares_alerted_against_unalerted(stub_state):
    """The verdict on the detectors: do alerts beat trades that didn't alert?"""
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "a", "tier": "cluster", "ticker": "A", "score": 70.0,
         "direction": "buy", f"edge_{w}": 6.0, f"act_edge_{w}": 2.0},
    ]
    stub_state[config.CONTROL_FILE] = [
        {"id": "c1", "tier": "control", "ticker": "C", "score": 0.0,
         "direction": "buy", f"edge_{w}": 5.0, f"act_edge_{w}": 1.8},
        {"id": "c2", "tier": "control", "ticker": "D", "score": 0.0,
         "direction": "buy", f"edge_{w}": 7.0, f"act_edge_{w}": 2.2},
    ]
    vs = history.performance_summary()["vs_control"]

    assert vs["alerted"]["avg_edge"] == pytest.approx(6.0)
    assert vs["un-alerted"]["avg_edge"] == pytest.approx(6.0)   # detectors add nothing
    assert "un-alerted" in history.format_summary(history.performance_summary())


def test_score_all_scores_both_logs(stub_state, monkeypatch):
    base = {"entry_date": "2026-01-01", "entry_price": 100.0, "spy_entry": 400.0,
            "direction": "buy", "ticker": "NVDA", "tier": "x", "score": 0.0}
    stub_state[config.HISTORY_FILE] = [{**base, "id": "a"}]
    stub_state[config.CONTROL_FILE] = [{**base, "id": "c"}]
    monkeypatch.setattr(history, "_get_price", lambda t, d: 120.0 if t == "NVDA" else 420.0)

    history.score_all(today=datetime(2026, 9, 1))

    w = config.WIN_RATE_PRIMARY
    assert f"edge_{w}" in stub_state[config.HISTORY_FILE][0]
    assert f"edge_{w}" in stub_state[config.CONTROL_FILE][0]


# ── Actionable vs trade-date baseline ─────────────────────────────────────────

def test_actionable_baseline_scores_from_the_alert_date(stub_state, monkeypatch):
    """
    Disclosure lag means the trade-date entry is a price you could never have
    paid. Scoring from the fire date measures the monitor, not the politician.
    """
    stub_state[config.HISTORY_FILE] = [{
        "id": "cluster|NVDA|2026-01-01", "tier": "cluster", "ticker": "NVDA",
        "score": 60.0, "direction": "buy",
        "entry_date": "2026-01-01", "entry_price": 100.0, "spy_entry": 400.0,
        # Alert only reached the user 40 days later, by which point the stock
        # had already run to 130 while SPY was flat.
        "fired_date": "2026-02-10", "fired_price": 130.0, "spy_fired": 400.0,
    }]
    monkeypatch.setattr(history, "_get_price",
                        lambda t, d: 143.0 if t == "NVDA" else 420.0)

    history.score_history(today=datetime(2026, 9, 1))
    (r,) = stub_state[config.HISTORY_FILE]
    w = config.WIN_RATE_PRIMARY

    # From the trade date: +43% vs SPY +5% → +38% edge.
    assert r[f"edge_{w}"] == pytest.approx(38.0)
    # From the alert date: +10% vs SPY +5% → +5% edge. The 30 points in between
    # were eaten by disclosure lag and were never capturable.
    assert r[f"act_edge_{w}"] == pytest.approx(5.0)


def test_actionable_edge_inverts_for_sells_too(stub_state, monkeypatch):
    stub_state[config.HISTORY_FILE] = [{
        "id": "s", "tier": "cluster", "ticker": "NVDA", "score": 60.0,
        "direction": "sell",
        "entry_date": "2026-01-01", "entry_price": 100.0, "spy_entry": 400.0,
        "fired_date": "2026-01-01", "fired_price": 100.0, "spy_fired": 400.0,
    }]
    monkeypatch.setattr(history, "_get_price",
                        lambda t, d: 120.0 if t == "NVDA" else 420.0)

    history.score_history(today=datetime(2026, 9, 1))
    (r,) = stub_state[config.HISTORY_FILE]
    w = config.WIN_RATE_PRIMARY
    assert r[f"act_edge_{w}"] == pytest.approx(-15.0)


def test_summary_reports_both_baselines(stub_state):
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cluster", "ticker": "A", "score": 80.0, "direction": "buy",
         f"edge_{w}": 20.0, f"act_edge_{w}": 2.0},
        {"id": "2", "tier": "cluster", "ticker": "B", "score": 80.0, "direction": "buy",
         f"edge_{w}": 10.0, f"act_edge_{w}": -4.0},
    ]
    s = history.performance_summary()["by_tier"]["cluster"]

    assert s["avg_edge"] == pytest.approx(15.0)             # politician looked great
    assert s["actionable"]["avg_edge"] == pytest.approx(-1.0)  # you would have lost
    assert s["actionable"]["hit_rate"] == 0.5

    out = history.format_summary(history.performance_summary())
    assert "trade-date" in out and "actionable" in out


def test_actionable_pending_when_only_trade_date_matured(stub_state):
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cluster", "ticker": "A", "score": 80.0,
         "direction": "buy", f"edge_{w}": 5.0},
    ]
    assert history.performance_summary()["by_tier"]["cluster"]["actionable"]["n"] == 0
    assert "actionable pending" in history.format_summary(history.performance_summary())


# ── AI text cleanup ───────────────────────────────────────────────────────────

def test_strips_grounding_citation_markers():
    raw = "TSM raised its 2026 outlook. [cite: 1, 2, 3] Revenue rose 36%. [cite: 5, 8]"
    assert notifier._clean_ai_text(raw) == "TSM raised its 2026 outlook. Revenue rose 36%."


def test_strips_unterminated_citation_marker():
    """A truncated response leaves an unclosed marker, seen in live output."""
    raw = "Markel reported Q2 results. Operating income fell. [cite: 1, 2, 3, 4,"
    assert notifier._clean_ai_text(raw) == "Markel reported Q2 results. Operating income fell."


def test_deduplicates_a_repeated_answer():
    """Gemini repeated an entire paragraph verbatim in a live digest."""
    once = "Delaney sold MKL shares. The company missed EPS estimates."
    assert notifier._clean_ai_text(f"{once} {once}") == once


def test_trims_truncated_trailing_sentence():
    raw = "TSM beat estimates. It is ramping 2nm production ahead of schedule due to orders from"
    assert notifier._clean_ai_text(raw) == "TSM beat estimates."


def test_clean_ai_text_passes_through_good_output():
    good = "Nvidia reports Q2 on August 26. Analysts expect strong data-center revenue."
    assert notifier._clean_ai_text(good) == good


def test_clean_ai_text_handles_empty_and_citation_only():
    assert notifier._clean_ai_text(None) is None
    assert notifier._clean_ai_text("") is None
    assert notifier._clean_ai_text("[cite: 1, 2]") is None


def test_seniority_split_covers_cross_signals_only(stub_state):
    """Congress-only alerts have no insider leg and must not dilute the comparison."""
    w = config.WIN_RATE_PRIMARY
    stub_state[config.HISTORY_FILE] = [
        {"id": "1", "tier": "cross_cluster", "ticker": "A", "score": 80.0,
         "direction": "buy", "has_top_insider": True,  f"edge_{w}": 12.0},
        {"id": "2", "tier": "cross_cluster", "ticker": "B", "score": 70.0,
         "direction": "buy", "has_top_insider": False, f"edge_{w}": -3.0},
        {"id": "3", "tier": "cluster", "ticker": "C", "score": 60.0,
         "direction": "buy", f"edge_{w}": 99.0},   # must be ignored here
    ]
    s = history.performance_summary()["by_seniority"]

    assert s["CEO/CFO"]["n"] == 1
    assert s["CEO/CFO"]["avg_edge"] == pytest.approx(12.0)
    assert s["other/dir."]["n"] == 1
    assert s["other/dir."]["avg_edge"] == pytest.approx(-3.0)


# ── Insider feed resilience ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


def test_screener_retries_then_succeeds(monkeypatch):
    """A transient timeout should not cost the whole run's cross-signals."""
    monkeypatch.setattr(openinsider_fetcher.time, "sleep", lambda s: None)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("read timed out")
        return _Resp("<html>ok</html>")

    monkeypatch.setattr(openinsider_fetcher.requests, "get", flaky)

    assert openinsider_fetcher._get_screener_html() == "<html>ok</html>"
    assert len(calls) == 3


def test_screener_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(openinsider_fetcher.time, "sleep", lambda s: None)
    calls = []

    def always_fail(*a, **k):
        calls.append(1)
        raise TimeoutError("read timed out")

    monkeypatch.setattr(openinsider_fetcher.requests, "get", always_fail)

    with pytest.raises(openinsider_fetcher.InsiderFetchError, match="unreachable"):
        openinsider_fetcher._get_screener_html()
    assert len(calls) == openinsider_fetcher.FETCH_ATTEMPTS


def test_pagination_reads_past_the_100_row_cap(monkeypatch):
    """openinsider caps each page at 100 rows and routinely matches more."""
    pages = {}
    for page in (1, 2, 3):
        pages[page] = [
            {"name": f"P{page}N{i}", "ticker": "AAA", "transaction_date": "2026-08-01",
             "amount": "$60,000"}
            for i in range(openinsider_fetcher.ROWS_PER_PAGE)
        ]
    monkeypatch.setattr(openinsider_fetcher, "_get_screener_html", lambda page=1: str(page))
    monkeypatch.setattr(openinsider_fetcher, "_parse_screener", lambda html: pages[int(html)])

    rows = openinsider_fetcher._fetch_screener_rows()
    assert len(rows) == 3 * openinsider_fetcher.ROWS_PER_PAGE


def test_pagination_stops_on_a_short_page(monkeypatch):
    calls = []

    def html(page=1):
        calls.append(page)
        return str(page)

    pages = {
        1: [{"name": f"a{i}", "ticker": "A", "transaction_date": "2026-08-01", "amount": "$1"}
            for i in range(openinsider_fetcher.ROWS_PER_PAGE)],
        2: [{"name": "b", "ticker": "B", "transaction_date": "2026-08-01", "amount": "$1"}],
    }
    monkeypatch.setattr(openinsider_fetcher, "_get_screener_html", html)
    monkeypatch.setattr(openinsider_fetcher, "_parse_screener", lambda h: pages[int(h)])

    rows = openinsider_fetcher._fetch_screener_rows()
    assert calls == [1, 2]                       # did not fetch page 3
    assert len(rows) == openinsider_fetcher.ROWS_PER_PAGE + 1


def test_pagination_dedupes_rows_shared_between_pages(monkeypatch):
    dup = {"name": "X", "ticker": "A", "transaction_date": "2026-08-01", "amount": "$60,000"}
    pages = {
        1: [dup] + [{"name": f"a{i}", "ticker": "A", "transaction_date": "2026-08-01", "amount": "$1"}
                    for i in range(openinsider_fetcher.ROWS_PER_PAGE - 1)],
        2: [dup],
    }
    monkeypatch.setattr(openinsider_fetcher, "_get_screener_html", lambda page=1: str(page))
    monkeypatch.setattr(openinsider_fetcher, "_parse_screener", lambda h: pages[int(h)])

    rows = openinsider_fetcher._fetch_screener_rows()
    assert len(rows) == openinsider_fetcher.ROWS_PER_PAGE   # dup counted once


def test_later_page_outage_keeps_earlier_pages(monkeypatch):
    """A partial outage should cost the extra pages, not the whole run."""
    def html(page=1):
        if page == 1:
            return "1"
        raise openinsider_fetcher.InsiderFetchError("down")

    page1 = [{"name": f"a{i}", "ticker": "A", "transaction_date": "2026-08-01", "amount": "$1"}
             for i in range(openinsider_fetcher.ROWS_PER_PAGE)]
    monkeypatch.setattr(openinsider_fetcher, "_get_screener_html", html)
    monkeypatch.setattr(openinsider_fetcher, "_parse_screener", lambda h: page1)

    rows = openinsider_fetcher._fetch_screener_rows()
    assert len(rows) == openinsider_fetcher.ROWS_PER_PAGE


def test_first_page_outage_still_raises(monkeypatch):
    def html(page=1):
        raise openinsider_fetcher.InsiderFetchError("down")

    monkeypatch.setattr(openinsider_fetcher, "_get_screener_html", html)
    with pytest.raises(openinsider_fetcher.InsiderFetchError):
        openinsider_fetcher._fetch_screener_rows()


def test_outage_is_not_an_empty_result(monkeypatch):
    """
    The bug this guards: fetch_all used to swallow the error and return [],
    making a dead feed indistinguishable from a day with no insider buys.
    """
    monkeypatch.setattr(openinsider_fetcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        openinsider_fetcher.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )

    with pytest.raises(openinsider_fetcher.InsiderFetchError):
        openinsider_fetcher.fetch_all(days=45)


def test_digest_surfaces_warnings(monkeypatch):
    """A thin digest during an outage must say why, not look like a quiet market."""
    sent = {}
    monkeypatch.setattr(notifier, "_send_email",
                        lambda subj, text, html: sent.update(subject=subj, text=text, html=html))
    monkeypatch.setattr(notifier, "generate_alert_context", lambda *a, **k: None)
    monkeypatch.setattr(notifier, "_alert_conflicts", lambda alert: [])

    alert = _alert("cluster", [trade(representative="A"), trade(representative="B")])
    analyzer.enrich_and_score(alert, win_rates={})

    notifier.send_digest([alert], warnings=["Insider feed unavailable this run."])

    assert "Insider feed unavailable this run." in sent["text"]
    assert "Insider feed unavailable this run." in sent["html"]


def test_digest_without_warnings_has_no_warning_block(monkeypatch):
    sent = {}
    monkeypatch.setattr(notifier, "_send_email",
                        lambda subj, text, html: sent.update(text=text, html=html))
    monkeypatch.setattr(notifier, "generate_alert_context", lambda *a, **k: None)
    monkeypatch.setattr(notifier, "_alert_conflicts", lambda alert: [])

    alert = _alert("cluster", [trade(representative="A")])
    analyzer.enrich_and_score(alert, win_rates={})

    notifier.send_digest([alert])
    assert "⚠" not in sent["text"]


def test_format_summary_handles_empty_history(stub_state):
    stub_state[config.HISTORY_FILE] = []
    out = history.format_summary(history.performance_summary())
    assert "0 recorded" in out
    assert "no matured alerts yet" in out or "no records yet" in out


# ── Committee names ───────────────────────────────────────────────────────────

MEMBERDATA_FIXTURE = """<?xml version="1.0"?>
<MemberData>
  <committees>
    <committee comcode="FA00"><committee-fullname>Committee on Foreign Affairs</committee-fullname>
      <subcommittee subcomcode="FA05"><subcommittee-fullname>East Asia and Pacific</subcommittee-fullname></subcommittee>
    </committee>
    <committee comcode="BA00"><committee-fullname>Committee on Financial Services</committee-fullname>
      <subcommittee subcomcode="BA16"><subcommittee-fullname>Capital Markets</subcommittee-fullname></subcommittee>
    </committee>
    <committee comcode="BU00"><committee-fullname>Committee on the Budget</committee-fullname></committee>
    <committee comcode="IG00"><committee-fullname>Permanent Select Committee on Intelligence</committee-fullname></committee>
    <committee comcode="ZS00"><committee-fullname>Select Committee on the  Strategic Competition</committee-fullname></committee>
  </committees>
</MemberData>
"""


def test_committee_names_come_from_the_xml_not_a_hardcoded_map():
    """FA00 is Foreign Affairs. A hand-written map once had it as Financial
    Services, which flagged 54 members with conflicts they do not have."""
    root = ET.fromstring(MEMBERDATA_FIXTURE)
    comms, subs = committees._parse_house_committee_names(root)

    assert comms["FA00"] == "Foreign Affairs"
    assert comms["BA00"] == "Financial Services"
    assert comms["BU00"] == "Budget"
    assert subs["FA05"] == "East Asia and Pacific"
    assert subs["BA16"] == "Capital Markets"


def test_long_committee_forms_are_left_intact():
    root = ET.fromstring(MEMBERDATA_FIXTURE)
    comms, _ = committees._parse_house_committee_names(root)
    assert comms["IG00"] == "Permanent Select Committee on Intelligence"
    # Doubled whitespace in the source is collapsed.
    assert comms["ZS00"] == "Select Committee on the Strategic Competition"


@pytest.mark.parametrize("raw, expected", [
    ("Delaney, April McClain",     "April McClain Delaney"),
    ("Taylor, David J.",           "David J. Taylor"),
    ("John Boozman",               "John Boozman"),
    ("Jerry Moran,",               "Jerry Moran"),
    ("A. Mitchell McConnell, Jr.", "A. Mitchell McConnell, Jr."),
    ("Cisneros, Gilbert",          "Gilbert Cisneros"),
])
def test_display_name_normalizes_both_chambers(raw, expected):
    assert committees.display_name(raw) == expected


# ── Dashboard signal set ──────────────────────────────────────────────────────

def test_dashboard_shows_live_signals_not_just_todays():
    """analyze() suppresses already-emailed alerts, so the page must re-derive
    the full set — otherwise a quiet day renders as zero signals."""
    trades = [
        trade(representative="Rep A", ticker="NVDA", transaction_date="2026-07-01"),
        trade(representative="Rep B", ticker="NVDA", transaction_date="2026-07-02"),
        trade(representative="Rep C", ticker="NVDA", transaction_date="2026-07-03"),
    ]
    signals = export.current_signals(trades, [], {}, fired=[])
    assert signals, "detectors should still find the cluster with nothing new today"
    assert all(s.meta["is_new"] is False for s in signals)


def test_signals_emailed_this_run_are_marked_new():
    trades = [
        trade(representative="Rep A", ticker="NVDA", transaction_date="2026-07-01"),
        trade(representative="Rep B", ticker="NVDA", transaction_date="2026-07-02"),
        trade(representative="Rep C", ticker="NVDA", transaction_date="2026-07-03"),
    ]
    fired = export.current_signals(trades, [], {}, fired=[])
    again = export.current_signals(trades, [], {}, fired=fired)
    assert any(s.meta["is_new"] for s in again)
