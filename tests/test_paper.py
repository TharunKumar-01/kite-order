import pandas as pd
import pytest

from assistant.costs import leg_costs
from assistant.paper import Entry, horizon_returns, max_drawdown, simulate
from conftest import make_bars


def _cal(n=20):
    return pd.bdate_range("2024-01-01", periods=n)


def test_time_exit_and_costs(cfg):
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
    prices = {"A.NS": make_bars(closes)}
    cal = prices["A.NS"].index
    res = simulate([Entry("A.NS", cal[1])], prices, cal, cfg)
    p = res.positions[0]
    slip = cfg.paper.slippage_pct / 100
    assert p["entry_price"] == pytest.approx(101 * (1 + slip))
    assert p["qty"] == int(cfg.max_position_value // p["entry_price"])
    assert p["exit_reason"] == "time" and p["exit_date"] == cal[1 + cfg.paper.hold_days - 1]
    exit_fill = closes[1 + cfg.paper.hold_days - 1] * (1 - slip)
    assert p["exit_price"] == pytest.approx(exit_fill)
    gross = (exit_fill - p["entry_price"]) * p["qty"]
    net = gross - leg_costs(p["qty"] * p["entry_price"], "buy", cfg.costs).total \
        - leg_costs(p["qty"] * exit_fill, "sell", cfg.costs).total
    assert p["gross_pnl"] == pytest.approx(gross) and p["net_pnl"] == pytest.approx(net)


def test_stop_loss(cfg):
    closes = [100, 100, 93, 90, 95, 99, 99]
    prices = {"A.NS": make_bars(closes)}
    cal = prices["A.NS"].index
    p = simulate([Entry("A.NS", cal[1])], prices, cal, cfg).positions[0]
    assert p["exit_reason"] == "stop" and p["exit_date"] == cal[2]


def test_max_open_positions(cfg):
    prices = {f"{s}.NS": make_bars([100] * 10) for s in "ABC"}
    cal = prices["A.NS"].index
    res = simulate([Entry(f"{s}.NS", cal[0], i) for i, s in enumerate("ABC")], prices, cal, cfg)
    assert len(res.positions) == cfg.max_open_positions
    assert [s["reason"] for s in res.skipped] == ["max positions"]


def test_whole_shares_and_unaffordable(cfg):
    prices = {"X.NS": make_bars([3000] * 8)}
    cal = prices["X.NS"].index
    res = simulate([Entry("X.NS", cal[0])], prices, cal, cfg)
    assert not res.positions and res.skipped[0]["reason"] == "insufficient cash"


def test_horizon_returns_vs_nifty():
    stock = make_bars([100, 110, 120, 130, 140, 150, 160], opens=[100, 105, 120, 130, 140, 150, 160])
    bench = make_bars([1000] * 7, opens=[1000] * 7)
    hr = horizon_returns(stock, stock.index[1], bench, None)
    assert hr[1]["ret"] == pytest.approx((110 / 105 - 1) * 100)  # entry-day open -> entry-day close
    assert hr[5]["ret"] == pytest.approx((150 / 105 - 1) * 100)  # -> close of 5th trading day
    assert hr[5]["excess_nifty"] == pytest.approx(hr[5]["ret"])


def test_max_drawdown():
    assert max_drawdown(pd.Series([100, 120, 90, 130])) == pytest.approx(-25.0)
