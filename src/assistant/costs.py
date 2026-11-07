"""Zerodha equity delivery (CNC) cost model. Percent values in config are percentages
(0.1 means 0.1%). VERIFY the defaults against https://zerodha.com/brokerage-calculator"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import Costs


@dataclass
class LegCosts:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp + self.gst + self.dp

    def as_dict(self) -> dict:
        d = {k: round(v, 4) for k, v in asdict(self).items()}
        d["total"] = round(self.total, 4)
        return d


def leg_costs(value: float, side: str, c: Costs) -> LegCosts:
    """Charges for one leg. `value` = price * qty in INR. side: 'buy' or 'sell'."""
    if side not in ("buy", "sell"):
        raise ValueError(side)
    brokerage = c.brokerage_delivery
    stt = value * c.stt_pct / 100
    if c.stt_round_to_rupee:
        stt = float(int(stt + 0.5))
    exch = value * c.exchange_txn_pct / 100
    sebi = value * c.sebi_per_crore / 1e7
    stamp = value * c.stamp_buy_pct / 100 if side == "buy" else 0.0
    gst = (brokerage + exch + sebi) * c.gst_pct / 100
    dp = c.dp_charge_per_sell if side == "sell" else 0.0
    return LegCosts(brokerage, stt, exch, sebi, stamp, gst, dp)


def round_trip(buy_price: float, sell_price: float, qty: int, c: Costs) -> dict:
    b = leg_costs(buy_price * qty, "buy", c)
    s = leg_costs(sell_price * qty, "sell", c)
    gross = (sell_price - buy_price) * qty
    return {"buy": b.as_dict(), "sell": s.as_dict(), "gross_pnl": round(gross, 2),
            "total_costs": round(b.total + s.total, 2), "net_pnl": round(gross - b.total - s.total, 2)}
