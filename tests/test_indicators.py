import numpy as np
import pandas as pd
import pytest

from assistant.signals import indicator_frame, rsi, sma
from conftest import make_bars

# Classic Wilder RSI example (StockCharts "RSI" ChartSchool table).
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
          46.28, 46.00, 46.03, 46.41, 46.22]


def test_rsi_known_values():
    r = rsi(pd.Series(CLOSES), 14)
    assert r.iloc[:14].isna().all()
    # Hand calculation for the first value: gains over the first 14 changes sum to 3.34,
    # losses to 1.40 -> RS = (3.34/14)/(1.40/14) -> RSI = 70.46. (StockCharts prints 70.53
    # because its sheet uses unrounded closes; with the rounded closes above it is 70.46.)
    assert r.iloc[14] == pytest.approx(100 - 100 / (1 + 3.34 / 1.40), abs=1e-6)
    # Subsequent values use Wilder smoothing; each is within 0.1 of the published table.
    for i, pub in zip(range(14, 19), [70.53, 66.32, 66.55, 69.41, 66.36]):
        assert r.iloc[i] == pytest.approx(pub, abs=0.1)


def test_rsi_all_gains_is_100():
    assert rsi(pd.Series(np.arange(1, 30, dtype=float)), 14).iloc[-1] == 100


def test_sma():
    s = sma(pd.Series([1, 2, 3, 4, 5], dtype=float), 3)
    assert s.isna().sum() == 2 and s.iloc[-1] == 4


def test_rules_fire_on_uptrend_breakout(cfg):
    closes = list(np.linspace(100, 200, 260)) + [215]
    df = make_bars(closes)
    df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000
    f = indicator_frame(df, None, cfg).iloc[-1]
    assert f["trend"] == 1 and f["breakout"] == 1 and f["volume"] == 1
    # steady straight-line rise -> RSI 100 -> overextended
    assert f["not_overextended"] == 0


def test_no_lookahead(cfg):
    rng = np.random.default_rng(1)
    closes = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, 400))
    df = make_bars(closes)
    bench = make_bars(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, 400)))["close"]
    full = indicator_frame(df, bench, cfg)
    t = 300
    trunc = indicator_frame(df.iloc[: t + 1], bench.iloc[: t + 1], cfg)
    # Changing/removing the future must not change any value at or before t.
    pd.testing.assert_frame_equal(full.iloc[: t + 1], trunc, check_exact=False)
    shocked = df.copy()
    shocked.iloc[t + 1:, shocked.columns.get_loc("close")] *= 3
    pd.testing.assert_frame_equal(indicator_frame(shocked, bench, cfg).iloc[: t + 1], trunc, check_exact=False)
