import pytest

from assistant.config import Costs
from assistant.costs import leg_costs, round_trip


def test_small_buy_leg():
    c = leg_costs(2500, "buy", Costs())
    assert c.stt == 3  # 2.50 rounded to nearest rupee
    assert c.exchange_txn == pytest.approx(0.07425)
    assert c.sebi == pytest.approx(0.0025)
    assert c.stamp == pytest.approx(0.375)
    assert c.gst == pytest.approx(0.18 * (0.07425 + 0.0025))
    assert c.dp == 0
    assert c.total == pytest.approx(3.465565, abs=1e-6)


def test_small_sell_leg_has_dp_and_no_stamp():
    c = leg_costs(2600, "sell", Costs())
    assert c.stamp == 0 and c.dp == 15.34
    assert c.total == pytest.approx(3 + 0.07722 + 0.0026 + 0.18 * (0.07722 + 0.0026) + 15.34, abs=1e-6)


def test_large_trade_unrounded_matches_hand_calc():
    c = leg_costs(100_000, "buy", Costs(stt_round_to_rupee=False))
    assert c.total == pytest.approx(100 + 2.97 + 0.1 + 15 + 0.18 * 3.07, abs=1e-6)


def test_round_trip_net():
    rt = round_trip(2500, 2600, 1, Costs())
    assert rt["gross_pnl"] == 100
    assert rt["net_pnl"] == pytest.approx(100 - rt["total_costs"], abs=0.01)
    assert rt["total_costs"] == pytest.approx(21.90, abs=0.01)


def test_bad_side():
    with pytest.raises(ValueError):
        leg_costs(100, "short", Costs())
