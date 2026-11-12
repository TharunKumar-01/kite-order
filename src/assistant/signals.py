"""Deterministic signal rules. No LLM, no randomness.

The same `indicator_frame` is used live (last row) and in the backtest (every row),
and every value on row t uses only bars <= t, so there is no look-ahead."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .universe import Stock

PRICE_RULES = ("trend", "breakout", "volume", "not_overextended")


# ---------------- indicators ----------------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI (seeded with a simple average of the first n changes)."""
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return pd.Series(out, index=close.index)
    d = np.diff(c)
    gain, loss = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = gain[:n].mean(), loss[:n].mean()

    def val(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[n] = val(ag, al)
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + gain[i]) / n
        al = (al * (n - 1) + loss[i]) / n
        out[i + 1] = val(ag, al)
    return pd.Series(out, index=close.index)


def indicator_frame(df: pd.DataFrame, bench_close: pd.Series | None, cfg: Config) -> pd.DataFrame:
    """df: adjusted daily bars (open/high/low/close/raw_close/volume)."""
    s = cfg.signals
    w = cfg.weights
    close, vol = df["close"], df["volume"]
    f = pd.DataFrame(index=df.index)
    f["close"] = close
    f["raw_close"] = df.get("raw_close", close)
    f["sma_fast"] = sma(close, s.sma_fast)
    f["sma_slow"] = sma(close, s.sma_slow)
    f["trend"] = ((close > f["sma_fast"]) & (f["sma_fast"] > f["sma_slow"])).astype(float)
    f.loc[f["sma_slow"].isna(), "trend"] = np.nan
    prior_high = close.shift(1).rolling(s.breakout_window, min_periods=s.breakout_window).max()
    f["breakout"] = (close > prior_high).astype(float)
    f.loc[prior_high.isna(), "breakout"] = np.nan
    prior_vol = vol.shift(1).rolling(s.breakout_window, min_periods=s.breakout_window).mean()
    f["volume"] = (vol > s.volume_mult * prior_vol).astype(float)
    f.loc[prior_vol.isna() | (prior_vol <= 0), "volume"] = np.nan
    f["rsi"] = rsi(close, s.rsi_period)
    f["not_overextended"] = (f["rsi"] < s.rsi_max).astype(float)
    f.loc[f["rsi"].isna(), "not_overextended"] = np.nan
    lb = 21 * s.rel_strength_months
    ret = close / close.shift(lb) - 1
    if bench_close is not None and not bench_close.empty:
        b = bench_close.reindex(df.index).ffill()
        bret = b / b.shift(lb) - 1
        f["rel_strength"] = (ret - bret) * 100
    else:
        f["rel_strength"] = ret * 100
    f["ret_1d"] = close.pct_change() * 100
    f["avg_traded_value_20d"] = (df.get("raw_close", close) * vol).rolling(20, min_periods=20).mean()
    rs_c = f["rel_strength"].clip(-w.rs_clip, w.rs_clip).fillna(0)
    f["price_score"] = (w.trend * f["trend"].fillna(0) + w.breakout * f["breakout"].fillna(0)
                        + w.volume * f["volume"].fillna(0) + w.not_overextended * f["not_overextended"].fillna(0)
                        + w.rel_strength * rs_c)
    req = [f[r] == 1 for r in s.require if r in f]
    f["qualifies"] = np.logical_and.reduce(req) if req else True
    return f


# ---------------- candidates & ranking ----------------
@dataclass
class Candidate:
    stock: Stock
    signal_date: pd.Timestamp
    last_close: float           # raw (unadjusted) last close, for affordability
    components: dict[str, float]
    price_score: float
    qualifies: bool
    liquid: bool
    news_score: float = 0.0
    sector_news: float = 0.0
    tailwind: float = 0.0
    headlines: list = field(default_factory=list)
    excluded_reason: str | None = None

    @property
    def total(self) -> float:
        return self.components.get("total", self.price_score)

    def finalize(self, cfg: Config) -> None:
        w = cfg.weights
        tw = float(np.clip(self.tailwind, -2, 2))
        total = (self.price_score + w.news * self.news_score + w.sector_tailwind * tw
                 + w.sector_news * self.sector_news)
        self.components.update({"news": self.news_score, "sector_tailwind": round(tw, 3),
                                "sector_news": self.sector_news, "price_score": round(self.price_score, 3),
                                "total": round(total, 3)})


def build_candidates(universe: list[Stock], prices: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
                     cfg: Config, asof: pd.Timestamp) -> list[Candidate]:
    """Evaluate rules on bars strictly before `asof` (the session date)."""
    bclose = bench["close"] if bench is not None else None
    out = []
    for st in universe:
        df = prices.get(st.yf)
        if df is None:
            continue
        df = df[df.index < asof]
        if len(df) < cfg.signals.sma_slow + 5:
            continue
        f = indicator_frame(df, bclose, cfg)
        r = f.iloc[-1]
        comps = {k: (None if pd.isna(r[k]) else float(r[k])) for k in
                 (*PRICE_RULES, "rel_strength", "rsi", "ret_1d", "avg_traded_value_20d")}
        out.append(Candidate(
            stock=st, signal_date=f.index[-1], last_close=float(r["raw_close"]), components=comps,
            price_score=float(r["price_score"]), qualifies=bool(r["qualifies"]),
            liquid=bool((r["avg_traded_value_20d"] or 0) >= cfg.min_avg_traded_value_20d),
        ))
    return out


def sector_moves(cands: list[Candidate], index_prices: dict[str, pd.DataFrame], cfg: Config,
                 asof: pd.Timestamp) -> dict[str, float]:
    """Last-day % move per sector: sector index if available, else peer average."""
    moves: dict[str, float] = {}
    for sector, tk in cfg.market.sector_index.items():
        df = index_prices.get(tk)
        if df is not None:
            df = df[df.index < asof]
            if len(df) >= 2:
                moves[sector] = float((df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100)
    peers: dict[str, list[float]] = {}
    for c in cands:
        v = c.components.get("ret_1d")
        if v is not None:
            peers.setdefault(c.stock.sector, []).append(v)
    for sector, vals in peers.items():
        if sector not in moves and vals:
            moves[sector] = float(np.mean(vals))
    return moves


def apply_tailwind(cands: list[Candidate], sector_move: dict[str, float], cues: list[dict], cfg: Config) -> None:
    cue_chg = {c["ticker"]: c["change_pct"] for c in cues}
    for c in cands:
        vals = []
        if c.stock.sector in sector_move:
            vals.append(sector_move[c.stock.sector])
        for tag in c.stock.tags:
            for tk, sign in cfg.market.sector_cues.get(tag, {}).items():
                if tk in cue_chg:
                    vals.append(sign * cue_chg[tk])
        c.tailwind = float(np.mean(vals)) if vals else 0.0


def shortlist(cands: list[Candidate], cfg: Config) -> list[Candidate]:
    ok = []
    latest = max((c.signal_date for c in cands), default=None)
    for c in cands:
        c.finalize(cfg)
        if latest is not None and c.signal_date < latest:
            c.excluded_reason = "stale data"
        elif c.last_close > cfg.pick_price_cap:
            c.excluded_reason = "unaffordable"
        elif not c.liquid:
            c.excluded_reason = "illiquid"
        elif not c.qualifies:
            c.excluded_reason = "rules"
        elif c.news_score <= cfg.news.exclude_at_or_below:
            c.excluded_reason = "negative news"
        elif c.total < cfg.signals.min_total_score:
            c.excluded_reason = "low score"
        else:
            ok.append(c)
    ok.sort(key=lambda c: (-c.total, -(c.components.get("rel_strength") or 0), c.stock.symbol))
    return ok[: cfg.shortlist_size]
