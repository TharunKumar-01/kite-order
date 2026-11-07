"""Paper trading engine (shared by the live journal and the backtest) and pick outcome scoring.

The live paper portfolio is recomputed from scratch on every evening run from the approved
picks + cached prices, which makes it idempotent and catch-up safe (a missed evening is
filled in by the next one)."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .costs import leg_costs

HORIZONS = (1, 3, 5)


@dataclass
class Entry:
    symbol: str                 # yfinance ticker
    entry_date: pd.Timestamp    # session in which to buy at the open
    rank: int = 1
    pick_id: int | None = None
    sector: str = ""


@dataclass
class SimResult:
    positions: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    nifty_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def _factor(df: pd.DataFrame, d) -> float:
    if "raw_close" in df and df.at[d, "raw_close"]:
        return float(df.at[d, "close"] / df.at[d, "raw_close"])
    return 1.0


def simulate(entries: list[Entry], prices: dict[str, pd.DataFrame], calendar: pd.DatetimeIndex,
             cfg: Config, bench: pd.DataFrame | None = None) -> SimResult:
    """Prices are adjusted bars (open/close/raw_close). Each position is tracked in
    entry-day raw price units (adjusted / factor_at_entry) so splits and dividends during
    the hold don't distort P&L, while share counts and costs use real traded prices."""
    p, c = cfg.paper, cfg.costs
    slip = p.slippage_pct / 100
    res = SimResult()
    if not entries:
        return res
    by_day: dict[pd.Timestamp, list[Entry]] = {}
    for e in entries:
        by_day.setdefault(pd.Timestamp(e.entry_date), []).append(e)
    first = min(by_day)
    days = calendar[calendar >= first]
    cash = float(cfg.capital_total)
    open_pos: list[dict] = []
    eq, pid = {}, 0

    for d in days:
        # 1) entries at the open
        for e in sorted(by_day.get(d, []), key=lambda x: x.rank):
            df = prices.get(e.symbol)
            if df is None or d not in df.index or pd.isna(df.at[d, "open"]):
                res.skipped.append({"pick_id": e.pick_id, "symbol": e.symbol, "date": d, "reason": "no price"})
                continue
            if len(open_pos) >= cfg.max_open_positions:
                res.skipped.append({"pick_id": e.pick_id, "symbol": e.symbol, "date": d, "reason": "max positions"})
                continue
            if any(o["symbol"] == e.symbol for o in open_pos):
                res.skipped.append({"pick_id": e.pick_id, "symbol": e.symbol, "date": d, "reason": "already held"})
                continue
            f0 = _factor(df, d)
            ref = float(df.at[d, "open"]) / f0
            fill = ref * (1 + slip)
            budget = min(cfg.max_position_value, cash)
            qty = int(math.floor(budget / fill)) if fill > 0 else 0
            while qty > 0 and qty * fill + leg_costs(qty * fill, "buy", c).total > cash:
                qty -= 1
            if qty < 1:
                res.skipped.append({"pick_id": e.pick_id, "symbol": e.symbol, "date": d, "reason": "insufficient cash"})
                continue
            lc = leg_costs(qty * fill, "buy", c)
            cash -= qty * fill + lc.total
            pid += 1
            pos = {"id": pid, "pick_id": e.pick_id, "symbol": e.symbol, "sector": e.sector, "qty": qty,
                   "entry_date": d, "entry_ref": ref, "entry_price": fill, "entry_costs": lc.total,
                   "f0": f0, "days_held": 0, "status": "open", "last_close": None}
            open_pos.append(pos)
            res.fills.append({"position_id": pid, "side": "buy", "date": d, "ref_price": ref, "fill_price": fill,
                              "qty": qty, "costs": lc.total, "cost_detail": lc.as_dict()})
        # 2) mark / exit at the close
        still = []
        for pos in open_pos:
            df = prices[pos["symbol"]]
            if d not in df.index or pd.isna(df.at[d, "close"]):
                still.append(pos)
                continue
            pos["days_held"] += 1
            close = float(df.at[d, "close"]) / pos["f0"]
            pos["last_close"] = close
            reason = None
            if close <= pos["entry_price"] * (1 - p.stop_loss_pct / 100):
                reason = "stop"
            elif pos["days_held"] >= p.hold_days:
                reason = "time"
            if reason:
                fill = close * (1 - slip)
                lc = leg_costs(pos["qty"] * fill, "sell", c)
                cash += pos["qty"] * fill - lc.total
                gross = (fill - pos["entry_price"]) * pos["qty"]
                pos.update(status="closed", exit_date=d, exit_ref=close, exit_price=fill, exit_costs=lc.total,
                           exit_reason=reason, gross_pnl=gross, net_pnl=gross - pos["entry_costs"] - lc.total)
                res.positions.append(pos)
                res.fills.append({"position_id": pos["id"], "side": "sell", "date": d, "ref_price": close,
                                  "fill_price": fill, "qty": pos["qty"], "costs": lc.total,
                                  "cost_detail": lc.as_dict()})
            else:
                still.append(pos)
        open_pos = still
        eq[d] = cash + sum(o["qty"] * (o["last_close"] if o["last_close"] is not None else o["entry_price"])
                           for o in open_pos)

    for pos in open_pos:  # still open: mark-to-market (gross before exit costs)
        mark = pos["last_close"] if pos["last_close"] is not None else pos["entry_price"]
        pos["gross_pnl"] = (mark - pos["entry_price"]) * pos["qty"]
        est_exit = leg_costs(pos["qty"] * mark, "sell", c).total
        pos["net_pnl"] = pos["gross_pnl"] - pos["entry_costs"] - est_exit
        res.positions.append(pos)
    res.positions.sort(key=lambda x: x["id"])
    res.equity = pd.Series(eq, dtype=float)
    if bench is not None and len(days):
        b = bench.reindex(days)
        start_open = b["open"].iloc[0]
        if pd.notna(start_open) and start_open:
            res.nifty_equity = (cfg.capital_total * b["close"] / start_open).ffill()
    return res


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity / peak) - 1).min() * 100)


# ---------------- outcomes for every pick (signal quality) ----------------
def horizon_returns(df: pd.DataFrame, entry_date: pd.Timestamp, bench: pd.DataFrame | None,
                    sector: pd.DataFrame | None, price_0945: float | None = None) -> dict[int, dict]:
    """Return from entry-day open to close of the h-th trading day (entry day = day 1)."""
    out: dict[int, dict] = {}
    if df is None or entry_date not in df.index:
        return out
    after = df[df.index >= entry_date]
    o = float(after["open"].iloc[0])
    f0 = _factor(df, entry_date)
    for h in HORIZONS:
        if len(after) < h:
            continue
        end = after.index[h - 1]
        r = (float(after["close"].iloc[h - 1]) / o - 1) * 100
        row = {"end_date": end, "ret": r, "nifty_ret": None, "sector_ret": None}
        for key, ref in (("nifty_ret", bench), ("sector_ret", sector)):
            if ref is not None and entry_date in ref.index and end in ref.index and ref.at[entry_date, "open"]:
                row[key] = (float(ref.at[end, "close"]) / float(ref.at[entry_date, "open"]) - 1) * 100
        row["excess_nifty"] = r - row["nifty_ret"] if row["nifty_ret"] is not None else None
        row["excess_sector"] = r - row["sector_ret"] if row["sector_ret"] is not None else None
        row["ret_from_0945"] = ((float(after["close"].iloc[h - 1]) / f0 / price_0945 - 1) * 100
                                if price_0945 else None)
        out[h] = row
    return out


# ---------------- persistence (live) ----------------
def save_simulation(conn: sqlite3.Connection, res: SimResult) -> None:
    conn.execute("DELETE FROM paper_fills")
    conn.execute("DELETE FROM paper_positions")
    sym = lambda s: s[:-3] if s.endswith(".NS") else s  # noqa: E731
    for pos in res.positions:
        conn.execute(
            """INSERT INTO paper_positions(id,pick_id,symbol,qty,entry_date,entry_price,entry_costs,status,
               exit_date,exit_price,exit_costs,exit_reason,last_close,gross_pnl,net_pnl)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos["id"], pos["pick_id"], sym(pos["symbol"]), pos["qty"], pos["entry_date"].date().isoformat(),
             pos["entry_price"], pos["entry_costs"], pos["status"],
             pos["exit_date"].date().isoformat() if pos.get("exit_date") is not None else None,
             pos.get("exit_price"), pos.get("exit_costs"), pos.get("exit_reason"), pos.get("last_close"),
             _r(pos.get("gross_pnl")), _r(pos.get("net_pnl"))))
    for f in res.fills:
        conn.execute("""INSERT INTO paper_fills(position_id,side,date,ref_price,fill_price,qty,costs,cost_detail)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (f["position_id"], f["side"], f["date"].date().isoformat(), f["ref_price"], f["fill_price"],
                      f["qty"], f["costs"], json.dumps(f["cost_detail"])))
    conn.commit()


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)
