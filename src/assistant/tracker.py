"""Pick tracker: every morning pick is treated as bought (paper only) and followed daily.

Each pick is "bought" at its run day's open (+ slippage) for up to `max_position_value`, sold if a close is at or
below the paper stop-loss, or after `paper.hold_days` trading days. No capital or position limits, so every
suggestion is judged, whether you approved it or not. For picks that are losing or trailing Nifty, the evening
message lists the likely reasons from the data: market, sector, stock-specific moves, a faded gap-up, an
overstretched entry, a give-back after an early gain, or negative news. Learning tool, not advice.
"""
from __future__ import annotations

import logging
from html import escape as esc
import math
import sqlite3
from collections import Counter
from datetime import date

import pandas as pd

from .config import Config
from .costs import leg_costs
from .universe import yf_ticker

log = logging.getLogger(__name__)


def _ratio(df: pd.DataFrame, d) -> float:
    if "raw_close" in df and df.at[d, "raw_close"]:
        return float(df.at[d, "close"] / df.at[d, "raw_close"])
    return 1.0


def _ref_ret(ref: pd.DataFrame | None, start, end) -> float | None:
    if ref is None or start not in ref.index or end not in ref.index or not ref.at[start, "open"]:
        return None
    return (float(ref.at[end, "close"]) / float(ref.at[start, "open"]) - 1) * 100


def follow_pick(r, df: pd.DataFrame | None, bench: pd.DataFrame | None, sector: pd.DataFrame | None,
                scores: dict[str, float], cfg: Config) -> dict | None:
    """One pick's paper trade up to the latest bar in `df` (adjusted bars, may include raw_close)."""
    D = pd.Timestamp(r["run_date"])
    if df is None or D not in df.index or pd.isna(df.at[D, "open"]):
        return None
    p = cfg.paper
    slip = p.slippage_pct / 100
    f0 = _ratio(df, D)
    after = df[df.index >= D].dropna(subset=["close"])
    entry = float(df.at[D, "open"]) / f0 * (1 + slip)
    qty = max(int(math.floor(cfg.max_position_value / entry)), 1)
    exit_day, reason, days = None, None, 0
    hi = lo = entry
    for d, row in after.iterrows():
        days += 1
        hi = max(hi, float(row.get("high", row["close"])) / f0)
        lo = min(lo, float(row.get("low", row["close"])) / f0)
        close = float(row["close"]) / f0
        if close <= entry * (1 - p.stop_loss_pct / 100):
            exit_day, reason = d, "stop"
            break
        if days >= p.hold_days:
            exit_day, reason = d, "time"
            break
    end = exit_day if exit_day is not None else after.index[-1]
    last = float(after.at[end, "close"]) / f0 * ((1 - slip) if exit_day is not None else 1)
    buy_c = leg_costs(qty * entry, "buy", cfg.costs).total
    sell_c = leg_costs(qty * last, "sell", cfg.costs).total
    prev = df[df.index < D]
    prev_close = float(prev["close"].iloc[-1]) / f0 if len(prev) else None
    open_raw = float(df.at[D, "open"]) / f0
    close0 = float(df.at[D, "close"]) / f0
    nifty, sect = _ref_ret(bench, D, end), _ref_ret(sector, D, end)
    ret = (last / entry - 1) * 100
    return {
        "pick_id": r["id"], "symbol": r["symbol"], "name": r["name"], "sector": r["sector"],
        "run_date": D.date(), "rank": r["rank"], "status": r["status"], "days": days, "qty": qty,
        "entry": entry, "last": last, "ret": ret, "net": qty * (last - entry) - buy_c - sell_c,
        "invested": qty * entry + buy_c,
        "nifty": nifty, "sector_ret": sect, "excess": ret - nifty if nifty is not None else None,
        "max_up": (hi / entry - 1) * 100, "max_dn": (lo / entry - 1) * 100,
        "closed": reason, "exit_date": exit_day.date() if exit_day is not None else None,
        "gap": (open_raw / prev_close - 1) * 100 if prev_close else None,
        "post_open0": (close0 / open_raw - 1) * 100 if open_raw else None,
        "rsi": scores.get("rsi"), "breakout": scores.get("breakout"), "news": scores.get("news"),
        "rel_strength": scores.get("rel_strength"),
    }


def is_weak(t: dict) -> bool:
    return t["ret"] < 0 or (t["excess"] is not None and t["excess"] < -1)


def diagnose(t: dict, news: list[tuple[int, str]] | None = None) -> list[tuple[str, str]]:
    """(key, text) reasons for a losing / lagging pick, most important first."""
    out: list[tuple[str, str]] = []
    ret, nifty, sect = t["ret"], t["nifty"], t["sector_ret"]
    if nifty is not None and nifty <= -1 and ret - nifty >= -1.5:
        out.append(("market", f"🌧 whole market fell (Nifty {nifty:+.1f}%), the stock moved with it"))
    if sect is not None and sect <= -1 and ret - sect >= -1.5 and not (nifty is not None and sect - nifty > -1):
        out.append(("sector", f"🏭 its sector fell ({sect:+.1f}%, Nifty {nifty:+.1f}%)" if nifty is not None
                    else f"🏭 its sector fell ({sect:+.1f}%)"))
    ref = sect if sect is not None else nifty
    if ref is not None and ret - ref < -2:
        out.append(("stock", f"📉 stock-specific: {ret - ref:+.1f} pts vs its {'sector' if sect is not None else 'Nifty'}"))
    if t["gap"] is not None and t["gap"] >= 1 and (t["post_open0"] or 0) < -0.5:
        out.append(("gap", f"⤵️ bought into a {t['gap']:+.1f}% gap-up open that faded the same day "
                           f"({t['post_open0']:+.1f}% open→close)"))
    if t["rsi"] is not None and t["rsi"] >= 68:
        out.append(("stretched", f"🔥 already stretched when picked (RSI {t['rsi']:.0f})"))
    if t["max_up"] >= 3 and ret < 0:
        out.append(("giveback", f"↩️ was up {t['max_up']:+.1f}% at best, then gave it back"))
    for score, title in (news or [])[:1]:
        out.append(("news", f"📰 negative news since: “{esc(title)}”"))
    if not out:
        if abs(ret) < 2 and t["days"] <= 5:
            out.append(("noise", "〰️ small move, within normal day-to-day noise so far"))
        else:
            out.append(("unclear", "❔ no clear cause in prices or news"))
    return out


def track(conn: sqlite3.Connection, cfg: Config, prices: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
          since: date, until: date) -> list[dict]:
    rows = conn.execute("SELECT * FROM picks WHERE run_date>=? AND run_date<=? ORDER BY run_date, rank",
                        (since.isoformat(), until.isoformat())).fetchall()
    out = []
    for r in rows:
        scores = {s["component"]: s["value"] for s in
                  conn.execute("SELECT component, value FROM pick_scores WHERE pick_id=?", (r["id"],))}
        sec = prices.get(cfg.market.sector_index.get(r["sector"] or "", ""))
        t = follow_pick(r, prices.get(yf_ticker(r["symbol"])), bench, sec, scores, cfg)
        if t:
            out.append(t)
    return out


def negative_news(tracked: list[dict], cfg: Config, limit: int = 8) -> dict[str, list[tuple[int, str]]]:
    """Recent negative headlines for weak picks (titles are untrusted text: escaped when shown)."""
    from . import news as newsmod
    weak = [t for t in tracked if is_weak(t) and not t["closed"]][:limit]
    if not weak:
        return {}
    q = {t["symbol"]: f'"{t["name"] or t["symbol"]}" stock NSE' for t in weak}
    try:
        fetched = newsmod.fetch_many(q, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("tracker news failed: %s", e)
        return {}
    out = {}
    for sym, items in fetched.items():
        neg = sorted((s, newsmod.sanitize(h.title)) for h in items
                     if (s := newsmod.headline_score(h.title, cfg)) < 0)
        if neg:
            out[sym] = neg
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def tracker_text(tracked: list[dict], cfg: Config, run_date: date,
                 news: dict[str, list[tuple[int, str]]] | None = None, detail_limit: int = 25) -> str:
    news = news or {}
    lines = ["<b>📒 Pick tracker</b> (paper only: every morning pick treated as bought at that day's open, "
             f"₹{cfg.max_position_value:,.0f} each, {cfg.paper.stop_loss_pct:g}% stop on the close, "
             f"{cfg.paper.hold_days}-day hold)"]
    if not tracked:
        return "\n".join(lines + ["No picks tracked yet. The first ones appear the evening after a morning run."])

    n = len(tracked)
    up = sum(t["ret"] > 0 for t in tracked)
    beat = [t for t in tracked if t["excess"] is not None]
    nb = sum(t["excess"] > 0 for t in beat)
    avg = sum(t["ret"] for t in tracked) / n
    avg_x = sum(t["excess"] for t in beat) / len(beat) if beat else None
    net = sum(t["net"] for t in tracked)
    lines.append(f"<b>{n} picks</b>: {up} up · {nb}/{len(beat)} beating Nifty · avg {avg:+.2f}% "
                 f"(vs Nifty {_pct(avg_x)}) · net ₹{net:+,.0f} after charges")
    by_status: dict[str, list[float]] = {}
    for t in tracked:
        by_status.setdefault(t["status"], []).append(t["ret"])
    if len(by_status) > 1:
        lines.append("By your call: " + " · ".join(f"{k} {len(v)}: avg {sum(v) / len(v):+.2f}%"
                                                  for k, v in sorted(by_status.items())))

    reasons: Counter = Counter()
    detail = sorted(tracked, key=lambda t: (t["run_date"], t["rank"]), reverse=True)[:detail_limit]
    for t in sorted(tracked, key=lambda t: (t["run_date"], t["rank"])):
        if is_weak(t):
            for key, _ in diagnose(t, news.get(t["symbol"])):
                reasons[key] += 1
    lines.append("")
    for t in detail:
        icon = "🟢" if t["ret"] > 0 and (t["excess"] is None or t["excess"] >= -1) else "🔴" if t["ret"] < 0 else "🟡"
        state = (f"stopped {t['exit_date']:%d %b}" if t["closed"] == "stop" else
                 f"sold {t['exit_date']:%d %b} (hold over)" if t["closed"] == "time" else f"day {t['days']}")
        lines.append(f"{icon} <b>{esc(t['symbol'])}</b> {t['run_date']:%d %b} [{t['status']}] · {state} · "
                     f"₹{t['entry']:,.2f} → ₹{t['last']:,.2f} <b>{t['ret']:+.1f}%</b> (₹{t['net']:+,.0f}) · "
                     f"Nifty {_pct(t['nifty'])}"
                     + (f" · sector {_pct(t['sector_ret'])}" if t["sector_ret"] is not None else "")
                     + f" · best {t['max_up']:+.1f}% / worst {t['max_dn']:+.1f}%")
        if is_weak(t):
            for _, why in diagnose(t, news.get(t["symbol"]))[:3]:
                lines.append(f"     {why}")
    if len(tracked) > len(detail):
        lines.append(f"<i>…and {len(tracked) - len(detail)} older picks (counted above)</i>")
    if reasons:
        names = {"market": "market fell", "sector": "sector fell", "stock": "stock-specific",
                 "gap": "faded gap-up", "stretched": "stretched entry", "giveback": "gave back gains",
                 "news": "negative news", "noise": "noise", "unclear": "unclear"}
        lines += ["", "<b>Why the weak ones were weak</b> (count): "
                  + " · ".join(f"{names[k]} {v}" for k, v in reasons.most_common())]
    lines.append("<i>Reasons are read from prices and headlines after the fact, not proven causes.</i>")
    return "\n".join(lines)
