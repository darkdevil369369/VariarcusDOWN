import math

import pytest

from bot.config import load_config
from bot.feeds.sim import SimFeed
from bot.feeds.venues import ArcusFeed, BinanceFeed, BybitFeed, OkxFeed, VariationalFeed
from bot.leadlag import LeadLag
from bot.maker import MakerStrategy
from bot.market import ArcusBook, Market
from bot.strategy import Strategy

LEADERS = ["variational", "binance", "bybit", "okx"]


@pytest.fixture
def cfg():
    return load_config()


def mk():
    return {s: Market(s, LEADERS) for s in ("BTC", "ETH")}


# ---------------------------------------------------------------- book
def test_book_sequence_and_gap():
    b = ArcusBook()
    b.snapshot([["100", "1"], ["99", "2"]], [["101", "1"]], seq=10, ts=0)
    assert b.valid and b.best_bid == 100 and b.best_ask == 101
    assert b.update([["100", "0"]], [], seq=11, ts=0)          # delete best bid
    assert b.best_bid == 99
    assert b.update([["100", "5"]], [], seq=11, ts=0)          # duplicate ignored
    assert b.best_bid == 99
    assert not b.update([], [], seq=13, ts=0)                  # gap
    assert b.health == "RESYNC" and not b.valid


def test_book_walk_respects_limit_and_depth():
    b = ArcusBook()
    b.snapshot([["99", "1"]], [["100", "1"], ["101", "1"], ["105", "10"]], seq=1, ts=0)
    qty, px = b.walk("buy", 250, limit=101)
    assert qty == pytest.approx(2) and px == pytest.approx(100.5)   # only 201 USD available
    qty, px = b.walk_qty("sell", 0.5, limit=98)
    assert qty == 0.5 and px == 99
    assert b.vwap_for("buy", 10_000) == 0.0                          # book too thin


# ---------------------------------------------------------------- feed parsing
def test_arcus_feed_parses_snapshot_and_delta():
    m = mk()
    f = ArcusFeed(m, "BTC")
    f.on_message({"type": "subscribed", "channel": "l2OrderbookUpdates", "id": "BTC-USD",
                  "contents": {"bids": [["100", "1"]], "asks": [["101", "2"]], "lastSequenceId": 5}})
    f.on_message({"type": "channel_data", "channel": "l2OrderbookUpdates", "id": "BTC-USD",
                  "contents": {"bids": [["100.5", "1"]], "asks": [], "lastSequenceId": 6, "globalSequenceId": 99}})
    f.on_message({"type": "channel_data", "channel": "l2OrderbookUpdates", "id": "ETH-USD",
                  "contents": {"bids": [["1", "1"]], "asks": [], "lastSequenceId": 7, "globalSequenceId": 100}})
    assert m["BTC"].arcus.best_bid == 100.5 and m["BTC"].arcus.seq == 6


def test_leader_feeds_parse():
    m = mk()
    VariationalFeed(m).on_message({"channel": "instrument_price:P-BTC-USDC-3600",
                                   "pricing": {"price": "86162.06", "underlying_price": "86201.63"}})
    BinanceFeed(m).on_message({"stream": "ethusdt@bookTicker", "data": {"s": "ETHUSDT", "b": "4000.1", "a": "4000.2"}})
    BybitFeed(m).on_message({"topic": "orderbook.1.BTCUSDT", "type": "snapshot",
                             "data": {"s": "BTCUSDT", "b": [["86000", "1"]], "a": [["86001", "2"]]}})
    OkxFeed(m).on_message({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                           "data": [{"bids": [["86002", "1", "0", "1"]], "asks": [["86003", "1", "0", "1"]], "ts": "1"}]})
    assert m["BTC"].leaders["variational"].mid == pytest.approx(86162.06)
    assert m["ETH"].leaders["binance"].bid == 4000.1
    assert m["BTC"].leaders["bybit"].ask == 86001
    assert m["BTC"].leaders["okx"].bid == 86002


# ---------------------------------------------------------------- strategy (deterministic clock)
def _run_sim(cfg, seconds, lag_ms, seed=7, tmp_path=None, cls=Strategy):
    markets = mk()
    sim = SimFeed(markets, arcus_lag_ms=lag_ms, seed=seed)
    ll = LeadLag(markets, cfg)
    strat = cls(markets, cfg, data_dir=tmp_path, leadlag=ll)
    t = 1000.0
    for _ in range(int(seconds / 0.05)):
        sim.step(t)
        for k in range(5):                       # strategy at 100 Hz between 20 Hz market steps
            strat.tick(t + k * 0.01)
            ll.tick(t + k * 0.01)
        t += 0.05
    return strat, ll


def test_profitable_when_arcus_lags_more_than_our_latency(cfg, tmp_path):
    cfg["leadlag"]["qualify"] = True
    strat, ll = _run_sim(cfg, 600, lag_ms=1200, tmp_path=tmp_path)
    st = strat.stats
    assert st.wins + st.losses >= 10
    assert st.realized > 0
    assert all(t.notional <= cfg.paper.order_notional_usd + 1e-6 for t in strat.trades)
    assert (tmp_path / "trades.csv").exists()
    lags = {(r["symbol"], r["leader"]): r for r in ll.summary()}
    assert 700 <= lags[("BTC", "binance")]["xcorr_lag_ms"] <= 1600


def test_no_edge_without_lag(cfg, tmp_path):
    """With qualification on, a leader Arcus does not lag must never trigger."""
    cfg["leadlag"]["qualify"] = True
    strat, _ = _run_sim(cfg, 600, lag_ms=0, tmp_path=tmp_path)
    assert strat.stats.wins + strat.stats.losses == 0


def test_cap_never_exceeded(cfg, tmp_path):
    cfg["paper"]["order_notional_usd"] = 400
    strat, _ = _run_sim(cfg, 300, lag_ms=1200, tmp_path=tmp_path)
    # with two symbols and 400/entry, the second entry must be clipped to 100
    assert strat.stats.wins + strat.stats.losses > 0
    assert strat.open_notional() <= cfg["paper"]["max_notional_usd"] + 1e-6
    assert max(t.notional for t in strat.trades) <= 400 + 1e-6


def test_daily_loss_limit_blocks_entries(cfg, tmp_path):
    strat, _ = _run_sim(cfg, 5, lag_ms=1200, tmp_path=tmp_path)
    strat.stats.day_pnl = -1000
    assert strat._risk_block(0) == "daily loss limit"
    assert math.isfinite(strat.stats.equity)


def test_arcus_resync_sent_once_per_gap(monkeypatch):
    m = mk()
    f = ArcusFeed(m, "BTC")
    calls = []
    monkeypatch.setattr(f, "_resync", lambda: calls.append(1))
    snap = {"type": "subscribed", "channel": "l2OrderbookUpdates", "id": "BTC-USD",
            "contents": {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastSequenceId": 1}}
    f.on_message(snap)
    for seq in (5, 6, 7, 8):     # gap, then more deltas before the snapshot
        f.on_message({"type": "channel_data", "channel": "l2OrderbookUpdates", "id": "BTC-USD",
                      "contents": {"bids": [], "asks": [], "lastSequenceId": seq, "globalSequenceId": seq}})
    assert len(calls) == 1
    f.on_message(snap)
    assert f.resync_at is None and m["BTC"].arcus.health == "OK"


def test_default_trades_without_qualification_and_reports_gaps(cfg, tmp_path):
    strat, _ = _run_sim(cfg, 120, lag_ms=1200, tmp_path=tmp_path)
    assert strat.stats.wins + strat.stats.losses > 0
    assert strat.diag["BTC"]["best_edge"] > strat.diag["BTC"]["need"]


def test_maker_exit_books_small_gaps_without_exit_fee(cfg, tmp_path):
    assert cfg.paper.exit_mode == "maker"
    strat, _ = _run_sim(cfg, 300, lag_ms=1200, tmp_path=tmp_path)
    makers = [t for t in strat.trades if t.exit_reason == "maker_tp"]
    assert makers
    for t in makers:
        # only the entry taker fee is paid
        assert t.fees_usd == pytest.approx(t.notional * cfg.paper.taker_fee_bps / 1e4, rel=0.02)
        assert t.net_usd > 0


def test_taker_mode_still_works(cfg, tmp_path):
    cfg["paper"]["exit_mode"] = "taker"
    strat, _ = _run_sim(cfg, 120, lag_ms=1200, tmp_path=tmp_path)
    assert strat.trades and all(t.exit_reason != "maker_tp" for t in strat.trades)


def test_maker_mm_quotes_fill_without_fees_and_never_cross(cfg, tmp_path):
    assert cfg.strategy.mode == "maker_mm"
    strat, _ = _run_sim(cfg, 600, lag_ms=1200, tmp_path=tmp_path, cls=MakerStrategy)
    assert strat.trades
    for t in strat.trades:
        if t.exit_reason == "maker_exit":
            assert t.fees_usd == 0
        else:   # stop / max hold exits pay one taker fee
            assert t.fees_usd == pytest.approx(t.notional * cfg.paper.taker_fee_bps / 1e4, rel=0.05)
    assert strat.open_notional() <= cfg.paper.max_notional_usd + 1e-6


def test_maker_mm_quote_placement():
    from bot.config import load_config
    c = load_config()
    m = mk()
    m["BTC"].arcus.snapshot([["100000", "5"]], [["100000.1", "5"]], seq=1, ts=0)
    st = MakerStrategy(m, c)
    st._quote_flat(0.0, "BTC", 100000.05, m["BTC"].arcus)
    q = st.quotes["BTC"]
    assert q["bid"]["next_px"] <= 100000 and q["ask"]["next_px"] >= 100000.1   # never crossing
    assert q["bid"]["next_px"] == pytest.approx(100000.05 * (1 - 1.2e-4))
    assert st._active_px("BTC", "bid", 0.0) is None                          # not live before rtt/2
    assert st._active_px("BTC", "bid", 1.0) == q["bid"]["px"]


def test_arcus_trades_feed_and_queue_fill():
    from bot.config import load_config
    c = load_config()
    m = mk()
    f = ArcusFeed(m, "BTC")
    f.on_message({"type": "subscribed", "channel": "l2OrderbookUpdates", "id": "BTC-USD",
                  "contents": {"bids": [["100000", "0.5"]], "asks": [["100000.1", "0.4"]], "lastSequenceId": 1}})
    st = MakerStrategy(m, c)
    st._set_quote("BTC", "ask", 100000.1, 0.0)
    assert st._check_fill("BTC", "ask", 1.0) is None          # live, 0.4 BTC queued ahead of us
    assert st.quotes["BTC"]["ask"]["queue"] == pytest.approx(0.4)
    f.on_message({"type": "channel_data", "channel": "trades", "id": "BTC-USD",
                  "contents": [{"price": "100000.1", "size": "0.3", "side": "BUY", "timestamp": 1, "sequenceNumber": 1}]})
    assert st._check_fill("BTC", "ask", 1.1) is None          # 0.1 still ahead
    f.on_message({"type": "channel_data", "channel": "trades", "id": "BTC-USD",
                  "contents": [{"price": "100000.1", "size": "0.2", "side": "SELL", "timestamp": 2, "sequenceNumber": 2},
                               {"price": "100000.1", "size": "0.2", "side": "BUY", "timestamp": 3, "sequenceNumber": 3}]})
    assert st._check_fill("BTC", "ask", 1.2) == pytest.approx(100000.1)   # SELL print ignored, BUY print reaches us
