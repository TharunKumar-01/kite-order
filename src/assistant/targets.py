"""Per-stock profit targets from the stock's own price history.

A quiet large-cap rarely moves 25% in a few weeks; a volatile mid-cap often does. So instead of one fixed %,
the target is the rise this stock reached (intraday high vs. a day's close) within the holding period in
`auto_target_quantile` of all past windows (0.5 = the typical best move). Also reports how an OCO with this
target and stop-loss would have played out from every past day: target first, stop first, or neither.
History only: past moves don't promise future ones.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np

from .config import Config

log = logging.getLogger(__name__)

_cache: dict[tuple[str, date], object] = {}


@dataclass
class TargetStats:
    symbol: str
    days: int          # holding window looked at
    pct: float         # suggested target, % above the buy price
    reach_rate: float  # share of past windows whose high reached +pct (ignores the stop)
    tgt_first: float   # with the stop-loss too: target hit first
    sl_first: float    # stop-loss hit first (same-day both counts as stop)
    neither: float     # neither within `days`
    years: float
    n: int

    def line(self, sl_pct: float | None) -> str:
        s = (f"📊 {self.symbol}, last {self.years:.0f}y: rose +{self.pct:g}% within {self.days} trading days "
             f"in {self.reach_rate * 100:.0f}% of windows")
        if sl_pct:
            s += (f". With a {sl_pct:g}% stop: target first {self.tgt_first * 100:.0f}% · "
                  f"stop first {self.sl_first * 100:.0f}% · neither {self.neither * 100:.0f}%")
        return s + " (history, not a forecast)"


def _history(symbol: str):
    key = (symbol, date.today())
    if key not in _cache:
        import yfinance as yf
        df = yf.Ticker(f"{symbol}.NS").history(period="5y", interval="1d", auto_adjust=True)
        _cache.clear() if len(_cache) > 200 else None
        _cache[key] = df
    return _cache[key]


def outcomes(close: np.ndarray, high: np.ndarray, low: np.ndarray, days: int,
             tgt_pct: float, sl_pct: float | None) -> tuple[float, float, float, float, int]:
    """(reach_rate, target_first, stop_first, neither, n) over every start day with a full window after it."""
    n = len(close) - days
    if n <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0
    reach = tf = sf = 0
    for i in range(n):
        up = close[i] * (1 + tgt_pct / 100)
        dn = close[i] * (1 - sl_pct / 100) if sl_pct else -1.0
        hi, lo = high[i + 1:i + 1 + days], low[i + 1:i + 1 + days]
        hit_up = np.flatnonzero(hi >= up)
        hit_dn = np.flatnonzero(lo <= dn)
        if hit_up.size:
            reach += 1
        u = hit_up[0] if hit_up.size else days
        d = hit_dn[0] if hit_dn.size else days
        if d < days and d <= u:
            sf += 1
        elif u < days:
            tf += 1
    return reach / n, tf / n, sf / n, (n - tf - sf) / n, n


def suggest_target(symbol: str, cfg: Config, sl_pct: float | None,
                   fixed_pct: float | None = None) -> TargetStats | None:
    """The per-stock target and its history, or None if there isn't enough data (caller falls back).
    With `fixed_pct` (the user typed a target) only the history for that target is computed."""
    k = cfg.kite
    days = k.auto_target_days or cfg.paper.hold_days
    try:
        df = _history(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("target history for %s failed: %s", symbol, e)
        return None
    if df is None or len(df) < days + 250:
        return None
    df = df.dropna(subset=["Close", "High", "Low"])
    close, high, low = (df[c].to_numpy(dtype=float) for c in ("Close", "High", "Low"))
    if fixed_pct is not None:
        pct = round(fixed_pct, 2)
    else:
        n = len(close) - days
        best = np.array([high[i + 1:i + 1 + days].max() / close[i] - 1 for i in range(n)]) * 100
        pct = float(np.quantile(best, 1 - k.auto_target_quantile))
        pct = min(max(round(pct * 2) / 2, k.auto_target_min_pct), k.auto_target_max_pct)
    reach, tf, sf, ne, n = outcomes(close, high, low, days, pct, sl_pct)
    return TargetStats(symbol, days, pct, reach, tf, sf, ne, len(close) / 248, n)
