"""Telegram -> Kite GTT ordering: parse a message, build a capped plan, confirm, place,
then track the trigger until it fills / fails, placing or cleaning up the stop-loss.

Every write goes through kite.Kite.place_gtt / delete_gtt (GTT only, CNC + LIMIT, value cap).
Only triggers recorded in `kite_orders` are ever touched; the user's own GTTs are left alone."""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Config, load_config
from .costs import leg_costs
from .db import kv_get, kv_set
from .kite import GTTOrder, KiteError, band_tick, tick_round
from .util import iso_utc, now_ist

log = logging.getLogger(__name__)

NOW_WORDS = {"now", "market", "mkt", "cmp", "current", "ltp"}


class OrderError(ValueError):
    """A user-facing problem with an order request."""


@dataclass
class OrderRequest:
    symbol: str | None = None
    qty: int | None = None
    amount: float | None = None
    price: float | None = None          # None = "now"
    sl_price: float | None = None
    sl_pct: float | None = None
    no_sl: bool = False


@dataclass
class Plan:
    symbol: str
    qty: int
    mode: str                            # now / price
    ltp: float
    trigger: float
    limit: float
    sl_trigger: float | None
    sl_limit: float | None
    sl_pct: float | None
    notes: list[str] = field(default_factory=list)
    tick: float = 0.05

    @property
    def value(self) -> float:
        return self.qty * self.limit


# ------------------------------------------------------------------ parsing
_NUM = r"(\d+(?:\.\d+)?)"
_AMOUNT_RE = re.compile(rf"^(?:₹|rs\.?|inr){_NUM}(k)?$|^{_NUM}(k)?(?:rs|₹|inr)$", re.I)


def _num(tok: str) -> float:
    try:
        v = float(tok.replace(",", ""))
    except ValueError:
        raise OrderError(f"'{tok}' isn't a number") from None
    if not v > 0 or math.isinf(v):
        raise OrderError("numbers must be positive")
    return v


def parse_order(text: str) -> OrderRequest | None:
    """`buy [SYMBOL] <qty | ₹amount> [@ price | @now] [sl <price | N%> | sl none]`.
    Returns None when the message isn't a buy order at all; raises OrderError when it is but is malformed."""
    t = (text or "").strip()
    if not re.match(r"^buy\b", t, re.I):
        return None
    t = re.sub(r"(?<=\d),(?=\d)", "", t)                    # 2,500 -> 2500
    t = re.sub(r"(₹|rs\.?|inr)\s+(?=\d)", r"\1", t, flags=re.I)   # ₹ 2500 -> ₹2500
    t = re.sub(r"@\s*", "@", t)
    toks = t.split()[1:]
    req = OrderRequest()
    i = 0
    if i < len(toks) and re.fullmatch(r"[A-Za-z][A-Za-z0-9&\-]{0,19}", toks[i]) \
            and toks[i].lower() not in NOW_WORDS | {"at", "sl", "stop", "for"}:
        req.symbol = toks[i].upper()
        i += 1
    while i < len(toks):
        tok, low = toks[i], toks[i].lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if low in ("sl", "stop", "stoploss", "sl@"):
            if nxt is None:
                raise OrderError("give a stop-loss after 'sl', e.g. sl 6% or sl 3950")
            if nxt.lower() in ("none", "no", "off", "0"):
                req.no_sl = True
            elif nxt.endswith("%"):
                req.sl_pct = _num(nxt[:-1])
            else:
                req.sl_price = _num(nxt.lstrip("@₹"))
            i += 2
            continue
        if low == "at" or low == "@":
            if nxt is None:
                raise OrderError("give a price after 'at'")
            tok, low = "@" + nxt, "@" + nxt.lower()
            i += 1
        if low.startswith("@") or low in NOW_WORDS:
            v = low.lstrip("@").lstrip("₹")
            if v in NOW_WORDS:
                req.price = None
            else:
                req.price = _num(v)
            i += 1
            continue
        if low in ("shares", "share", "sh", "qty", "for"):
            i += 1
            continue
        m = _AMOUNT_RE.match(tok)
        if m:
            amt = _num(m.group(1) or m.group(3))
            if m.group(2) or m.group(4):
                amt *= 1000
            req.amount = amt
            i += 1
            continue
        if re.fullmatch(r"\d+", tok):
            req.qty = int(tok)
            i += 1
            continue
        raise OrderError(f"didn't understand '{tok}'")
    if req.qty is None and req.amount is None:
        raise OrderError("say how many shares (e.g. 2) or an amount (e.g. ₹2500)")
    if req.qty is not None and req.amount is not None:
        raise OrderError("give either shares or an amount, not both")
    if req.qty is not None and req.qty < 1:
        raise OrderError("quantity must be at least 1")
    return req


# ------------------------------------------------------------------ planning
def build_plan(req: OrderRequest, symbol: str, ltp: float, cfg: Config | None = None,
               tick: float | None = None) -> Plan:
    cfg = cfg or load_config()
    k = cfg.kite
    if not ltp > 0:
        raise OrderError(f"no current price for {symbol}")
    tick = tick or band_tick(ltp)

    def rnd(p: float) -> float:  # round to this stock's price step
        return tick_round(p, tick)
    notes: list[str] = []
    if req.price is None:
        mode = "now"
        trigger = rnd(ltp * (1 + k.now_trigger_gap_pct / 100))
        limit = rnd(trigger * (1 + k.now_limit_buffer_pct / 100))
    else:
        mode = "price"
        dev = abs(req.price / ltp - 1) * 100
        if dev > k.max_price_deviation_pct:
            raise OrderError(f"₹{req.price:,.2f} is {dev:.1f}% from the last price ₹{ltp:,.2f} "
                             f"(limit {k.max_price_deviation_pct:g}%). Typo?")
        limit = rnd(req.price)
        if limit < ltp * (1 - k.now_trigger_gap_pct / 100):
            trigger = limit                     # wait for the price to come down to it
        else:                                   # at/above market: fire on a small uptick, buy at <= limit
            trigger = rnd(ltp * (1 + k.now_trigger_gap_pct / 100))
            if limit < trigger:
                notes.append(f"fires when price ticks up to ₹{trigger:,.2f}, then a limit buy at ₹{limit:,.2f} "
                             "waits in the order book")
    if req.amount is not None:
        if req.amount > k.max_order_value:
            raise OrderError(f"₹{req.amount:,.0f} is above your per-order cap ₹{k.max_order_value:,.0f}")
        qty = int(req.amount // limit)
        if qty < 1:
            raise OrderError(f"₹{req.amount:,.0f} doesn't buy one share at ₹{limit:,.2f}")
    else:
        qty = int(req.qty)
    if qty * limit > k.max_order_value:
        raise OrderError(f"{qty} × ₹{limit:,.2f} = ₹{qty * limit:,.0f} is above your per-order cap "
                         f"₹{k.max_order_value:,.0f} (config kite.max_order_value)")
    sl_trigger = sl_limit = sl_pct = None
    if not req.no_sl:
        if req.sl_price is not None:
            sl_trigger = rnd(req.sl_price)
            sl_pct = (1 - sl_trigger / limit) * 100
        else:
            sl_pct = req.sl_pct if req.sl_pct is not None else k.default_sl_pct
            sl_trigger = rnd(limit * (1 - sl_pct / 100))
        if sl_trigger >= limit or sl_trigger >= ltp:
            raise OrderError(f"stop-loss ₹{sl_trigger:,.2f} must be below both the buy price and the last price")
        if sl_pct > k.max_sl_pct:
            raise OrderError(f"stop-loss {sl_pct:.1f}% below is more than the {k.max_sl_pct:g}% limit")
        if sl_pct < 0.5:
            raise OrderError("stop-loss is too close to the buy price (< 0.5%)")
        sl_limit = rnd(sl_trigger * (1 - k.sl_limit_buffer_pct / 100))
    return Plan(symbol, qty, mode, round(ltp, 2), trigger, limit, sl_trigger, sl_limit,
                round(sl_pct, 2) if sl_pct is not None else None, notes, tick)


@dataclass
class SellRequest:
    symbol: str
    qty: int | None = None              # None = all
    price: float | None = None
    profit: float | None = None         # rupees, after sell charges
    pct: float | None = None            # % above average buy price


def parse_sell(text: str) -> SellRequest | None:
    """`sell SYMBOL <qty | all> (@price | profit <₹amount> | target <N%>)`.
    None if not a sell order; OrderError if it is but is malformed. ("sold ..." is the manual log.)"""
    t = (text or "").strip()
    if not re.match(r"^sell\b", t, re.I):
        return None
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    t = re.sub(r"@\s*", "@", t)
    toks = t.split()[1:]
    if not toks or not re.fullmatch(r"[A-Za-z][A-Za-z0-9&\-]{0,19}", toks[0]):
        raise OrderError("which stock? e.g. sell JPL all @1455")
    req = SellRequest(symbol=toks[0].upper())
    i, qty_set = 1, False
    while i < len(toks):
        low = toks[i].lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if low in ("all", "everything"):
            req.qty, qty_set = None, True
        elif re.fullmatch(r"\d+", low) and not qty_set:
            req.qty, qty_set = int(low), True
        elif low in ("shares", "share", "sh", "qty"):
            pass
        elif low in ("at", "@") or low.startswith("@"):
            v = nxt if low in ("at", "@") else low[1:]
            if v is None:
                raise OrderError("give a price after @")
            req.price = _num(v.lstrip("₹"))
            i += 1 if low in ("at", "@") else 0
        elif low in ("profit", "gain"):
            if nxt is None:
                raise OrderError("give an amount after 'profit', e.g. profit 4000")
            v = re.sub(r"^(₹|rs\.?|inr)", "", nxt, flags=re.I)
            req.profit = _num(v[:-1]) * 1000 if v.lower().endswith("k") else _num(v)
            i += 1
        elif low in ("target", "tgt"):
            if nxt is None or not nxt.endswith("%"):
                raise OrderError("use e.g. target 5% (above your average price)")
            req.pct = _num(nxt[:-1])
            i += 1
        else:
            raise OrderError(f"didn't understand '{toks[i]}'")
        i += 1
    if not qty_set:
        raise OrderError("say how many shares or 'all', e.g. sell JPL all @1455")
    if req.qty is not None and req.qty < 1:
        raise OrderError("quantity must be at least 1")
    if sum(x is not None for x in (req.price, req.profit, req.pct)) != 1:
        raise OrderError("give exactly one of: @price, profit ₹amount, target N%")
    return req


def _tick_up(p: float, tick: float = 0.05) -> float:
    return round(math.ceil(round(p / tick, 6)) * tick, 2)



def build_sell_plan(req: SellRequest, held: int, avg: float | None, ltp: float,
                    cfg: Config | None = None, tick: float | None = None) -> Plan:
    cfg = cfg or load_config()
    k = cfg.kite
    tick = tick or band_tick(ltp or 1)
    if held < 1:
        raise OrderError(f"you don't hold any {req.symbol} (settled or T1) in Kite")
    qty = held if req.qty is None else req.qty
    if qty > held:
        raise OrderError(f"you hold {held} {req.symbol}, can't sell {qty}")
    if not ltp > 0:
        raise OrderError(f"no current price for {req.symbol}")
    if req.price is not None:
        limit = tick_round(req.price, tick)
    else:
        if not avg:
            raise OrderError("Kite doesn't show an average price for this holding; give @price instead")
        if req.pct is not None:
            limit = _tick_up(avg * (1 + req.pct / 100), tick)
        else:
            limit = _tick_up(avg + req.profit / qty, tick)
            for _ in range(3):  # include the sell-side charges so the profit is what you keep
                limit = _tick_up(avg + (req.profit + leg_costs(limit * qty, "sell", cfg.costs).total) / qty, tick)
    if limit < ltp * (1 + k.now_trigger_gap_pct / 100):
        raise OrderError(f"₹{limit:,.2f} is not above the last price ₹{ltp:,.2f}. A target sell must be higher; "
                         "to sell right now, use the Kite app.")
    if limit > ltp * (1 + k.max_target_pct / 100):
        raise OrderError(f"₹{limit:,.2f} is more than {k.max_target_pct:g}% above the last price ₹{ltp:,.2f}. Typo?")
    trigger = _tick_up(limit + max(tick, limit * 0.0005), tick)  # fires once already above the limit -> fills
    notes = []
    if avg:
        gross = (limit - avg) * qty
        net = gross - leg_costs(limit * qty, "sell", cfg.costs).total
        notes.append(f"avg buy ₹{avg:,.2f}; profit ≈ ₹{gross:,.0f} before / ₹{net:,.0f} after sell charges")
    return Plan(req.symbol, qty, "target", round(ltp, 2), trigger, limit, None, None, None, notes, tick)


def get_ltp(symbol: str) -> float:
    """Latest price from Yahoo (Kite quotes need the paid data plan). Can lag by a minute or so."""
    import yfinance as yf
    t = yf.Ticker(f"{symbol}.NS")
    try:
        p = float(t.fast_info["last_price"])
        if p > 0:
            return p
    except Exception:  # noqa: BLE001
        pass
    h = t.history(period="5d", interval="1d")
    if h is None or h.empty:
        raise OrderError(f"couldn't get a price for {symbol} (is it an NSE symbol?)")
    return float(h["Close"].dropna().iloc[-1])


# ------------------------------------------------------------------ state helpers
def ordering_enabled(conn: sqlite3.Connection) -> bool:
    return kv_get(conn, "orders_disabled") != "1"


def set_ordering(conn: sqlite3.Connection, enabled: bool) -> int:
    """Kill switch. Disabling also voids unconfirmed drafts. Returns drafts voided."""
    kv_set(conn, "orders_disabled", "0" if enabled else "1")
    if enabled:
        return 0
    cur = conn.execute("UPDATE kite_orders SET status='cancelled', note='kill switch', updated_at=? "
                       "WHERE status='draft'", (iso_utc(),))
    conn.commit()
    return cur.rowcount


def _orders_today(conn: sqlite3.Connection, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    return conn.execute("SELECT COUNT(*) FROM kite_orders WHERE confirmed_at >= ? AND status NOT IN "
                        "('failed','cancelled','stale','draft') AND side='BUY'", (start,)).fetchone()[0]


def latest_approved_symbol(conn: sqlite3.Connection, now: datetime) -> tuple[str, int] | None:
    r = conn.execute("SELECT id, symbol FROM picks WHERE status='approved' AND run_date=? "
                     "ORDER BY id DESC LIMIT 1", (now.date().isoformat(),)).fetchone()
    return (r["symbol"], r["id"]) if r else None


def create_draft(conn: sqlite3.Connection, plan: Plan, pick_id: int | None = None) -> int:
    now = iso_utc()
    side = "SELL" if plan.mode == "target" else "BUY"
    cur = conn.execute(
        """INSERT INTO kite_orders(pick_id,symbol,qty,mode,ltp,trigger_price,limit_price,sl_trigger,sl_limit,
           sl_pct,status,sl_status,note,created_at,updated_at,side,tick)
           VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?)""",
        (pick_id, plan.symbol, plan.qty, plan.mode, plan.ltp, plan.trigger, plan.limit, plan.sl_trigger,
         plan.sl_limit, plan.sl_pct, "none" if plan.sl_trigger is None else None, "; ".join(plan.notes) or None,
         now, now, side, plan.tick))
    conn.commit()
    return int(cur.lastrowid)


def buy_expiry(confirmed: datetime, cfg: Config, conn: sqlite3.Connection | None = None):
    """(IST date, "HH:MM") when an untriggered BUY GTT confirmed at `confirmed` (IST) is deleted, or None.
    Same trading day if confirmed before the cut-off, otherwise the next trading day."""
    from .universe import is_trading_day
    cut = cfg.kite.buy_valid_until
    if not cut:
        return None
    d = confirmed.date()
    if confirmed.strftime("%H:%M") >= cut or not is_trading_day(d, conn):
        d += timedelta(days=1)
        while not is_trading_day(d, conn):
            d += timedelta(days=1)
    return d, cut


def _expiry_phrase(exp) -> str:
    if exp is None:
        return "Trigger stays valid up to a year."
    d, cut = exp
    day = "today" if d == now_ist().date() else d.strftime("%a %d %b")
    return f"If it hasn't fired by {cut} {day}, I delete it (and its stop-loss)."


def draft_text(conn: sqlite3.Connection, oid: int, cfg: Config | None = None) -> str:
    cfg = cfg or load_config()
    o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
    if o["side"] == "SELL":
        return _sell_draft_text(o, cfg)
    value = o["qty"] * o["limit_price"]
    charges = leg_costs(value, "buy", cfg.costs).total
    expiry = _expiry_phrase(buy_expiry(now_ist(), cfg, conn))
    if o["mode"] == "now":
        how = (f"Trigger ₹{o['trigger_price']:,.2f} → limit ₹{o['limit_price']:,.2f}\n"
               f"<i>“Now” = fires on a {cfg.kite.now_trigger_gap_pct:g}% uptick from ₹{o['ltp']:,.2f}. "
               "A GTT can't be a true market order; if the price only falls, it won't fire. "
               f"{expiry}</i>")
    elif o["trigger_price"] == o["limit_price"]:
        how = (f"Buys at ₹{o['limit_price']:,.2f} when the price comes down to it "
               f"(last ₹{o['ltp']:,.2f}). {expiry}")
    else:
        how = (f"Trigger ₹{o['trigger_price']:,.2f} → limit ₹{o['limit_price']:,.2f} (last ₹{o['ltp']:,.2f}). "
               f"{expiry}")
    if o["sl_trigger"]:
        sl = (f"Stop-loss: SELL {o['qty']} if price ≤ ₹{o['sl_trigger']:,.2f} "
              f"(−{o['sl_pct']:.1f}%), limit ₹{o['sl_limit']:,.2f}")
    else:
        sl = "Stop-loss: <b>none</b>"
    lines = [f"🛒 <b>Confirm order #{oid}</b>",
             f"BUY <b>{o['symbol']}</b> × {o['qty']}  (NSE · delivery/CNC · limit)",
             how,
             f"Value ≈ ₹{value:,.0f} (cap ₹{cfg.kite.max_order_value:,.0f}) · buy charges ≈ ₹{charges:,.2f}",
             sl]
    if o["note"]:
        lines.append(f"<i>Note: {o['note']}</i>")
    lines.append(f"<i>Price from Yahoo, may lag. Button valid {cfg.kite.draft_ttl_seconds // 60} min. "
                 "Your decision, not advice.</i>")
    return "\n".join(lines)


def _sell_draft_text(o, cfg: Config) -> str:
    value = o["qty"] * o["limit_price"]
    lines = [f"💰 <b>Confirm target sell #{o['id']}</b>",
             f"SELL <b>{o['symbol']}</b> × {o['qty']}  (NSE · delivery/CNC · limit)",
             f"Trigger ₹{o['trigger_price']:,.2f} → sell at ₹{o['limit_price']:,.2f} (last ₹{o['ltp']:,.2f}, "
             f"+{(o['limit_price'] / o['ltp'] - 1) * 100:.1f}%)",
             f"Value ≈ ₹{value:,.0f} · sell charges ≈ ₹{leg_costs(value, 'sell', cfg.costs).total:,.2f}"]
    if o["note"]:
        lines.append(o["note"])
    lines.append("<i>Fires when the price is already above your sell price, so it fills straight away. "
                 "Valid up to a year. Needs CDSL authorisation (DDPI, or authorise the holding that day) "
                 "or the sell is rejected. Last price from Kite. Button valid "
                 f"{cfg.kite.draft_ttl_seconds // 60} min.</i>")
    return "\n".join(lines)


def _tick(o) -> float:
    return o["tick"] or band_tick(o["ltp"])


def _upd(conn: sqlite3.Connection, oid: int, **kv) -> None:
    kv["updated_at"] = iso_utc()
    cols = ", ".join(f"{k}=?" for k in kv)
    conn.execute(f"UPDATE kite_orders SET {cols} WHERE id=?", (*kv.values(), oid))
    conn.commit()


def cancel_draft(conn: sqlite3.Connection, oid: int) -> str:
    cur = conn.execute("UPDATE kite_orders SET status='cancelled', updated_at=? WHERE id=? AND status='draft'",
                       (iso_utc(), oid))
    conn.commit()
    return "Cancelled. Nothing was sent to Kite." if cur.rowcount else "That order is no longer a draft."


# ------------------------------------------------------------------ placing
def confirm(conn: sqlite3.Connection, oid: int, kite, now: datetime | None = None,
            cfg: Config | None = None) -> str:
    """Place the buy GTT (+ stop-loss GTT) for a draft. Exactly-once: the draft is claimed atomically."""
    cfg = cfg or load_config()
    now = now or now_ist()
    o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
    if o is None:
        return "Unknown order."
    if o["status"] != "draft":
        return f"Order #{oid} is already {o['status']}."
    age = now.astimezone(timezone.utc) - datetime.fromisoformat(o["created_at"])
    if age > timedelta(seconds=cfg.kite.draft_ttl_seconds):
        _upd(conn, oid, status="stale")
        return "⌛ This confirmation expired (price may have moved). Send the order again."
    if not ordering_enabled(conn):
        _upd(conn, oid, status="cancelled", note="kill switch")
        return "⛔ Ordering is switched off (/stop). Send /orderson to allow orders again."
    if _orders_today(conn, now) >= cfg.kite.max_orders_per_day:
        _upd(conn, oid, status="cancelled", note="daily limit")
        return f"⛔ Daily limit reached ({cfg.kite.max_orders_per_day} orders, config kite.max_orders_per_day)."
    cur = conn.execute("UPDATE kite_orders SET status='placing', confirmed_at=?, updated_at=? "
                       "WHERE id=? AND status='draft'", (iso_utc(), iso_utc(), oid))
    conn.commit()
    if cur.rowcount != 1:
        return f"Order #{oid} is already being handled."
    sym, qty = o["symbol"], o["qty"]
    if o["side"] == "SELL":
        return _confirm_sell(conn, o, kite)
    try:
        held_before, _ = kite.held_qty(sym)
    except KiteError as e:
        _upd(conn, oid, status="failed", note=f"holdings check: {e}")
        return f"❌ Not placed: couldn't read your holdings ({e})."
    try:
        buy_id = kite.place_gtt(GTTOrder(sym, "BUY", qty, o["trigger_price"], o["limit_price"], o["ltp"], _tick(o)))
    except KiteError as e:
        _upd(conn, oid, status="failed", note=str(e)[:300])
        return f"❌ Kite rejected the buy trigger: {e}"
    _upd(conn, oid, status="placed", buy_gtt_id=buy_id, held_before=held_before)
    msg = [f"✅ Buy trigger placed in Kite (GTT {buy_id}): {sym} × {qty}, limit ₹{o['limit_price']:,.2f}."]
    if o["sl_trigger"]:
        if held_before > 0:
            _upd(conn, oid, sl_status="deferred")
            msg.append(f"You already hold {held_before} {sym}, so the stop-loss will be placed once this buy "
                       "fills (so it can never sell your earlier shares).")
        else:
            msg.append(_place_sl(conn, oid, kite))
    msg.append("Track with /orders · cancel with /cancel " + str(oid))
    return "\n".join(msg)


def _confirm_sell(conn: sqlite3.Connection, o, kite) -> str:
    oid, sym, qty = o["id"], o["symbol"], o["qty"]
    try:
        held, _, _ = kite.holding(sym)
    except KiteError as e:
        _upd(conn, oid, status="failed", note=f"holdings check: {e}")
        return f"❌ Not placed: couldn't read your holdings ({e})."
    if held < qty:
        _upd(conn, oid, status="failed", note=f"holds {held}")
        return f"❌ Not placed: you now hold {held} {sym}, fewer than {qty}."
    try:
        gid = kite.place_gtt(GTTOrder(sym, "SELL", qty, o["trigger_price"], o["limit_price"], o["ltp"], _tick(o)))
    except KiteError as e:
        _upd(conn, oid, status="failed", note=str(e)[:300])
        return f"❌ Kite rejected the sell trigger: {e}"
    _upd(conn, oid, status="placed", buy_gtt_id=gid, held_before=held)
    return (f"✅ Target sell placed in Kite (GTT {gid}): {sym} × {qty} at ₹{o['limit_price']:,.2f} "
            f"(trigger ₹{o['trigger_price']:,.2f}).\nTrack with /orders · cancel with /cancel {oid}")


def _place_sl(conn: sqlite3.Connection, oid: int, kite) -> str:
    o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
    try:
        sl_id = kite.place_gtt(GTTOrder(o["symbol"], "SELL", o["qty"], o["sl_trigger"], o["sl_limit"],
                                        o["ltp"], _tick(o)))
    except KiteError as e:
        _upd(conn, oid, sl_status="failed", note=f"stop-loss: {e}"[:300])
        return f"⚠️ Stop-loss trigger was NOT accepted ({e}). Set it in Kite yourself."
    _upd(conn, oid, sl_gtt_id=sl_id, sl_status="active")
    return f"🛡 Stop-loss trigger placed (GTT {sl_id}): SELL {o['qty']} at ≤ ₹{o['sl_trigger']:,.2f}."


def _delete(kite, gtt_id: int | None) -> str | None:
    if not gtt_id:
        return None
    try:
        g = kite.gtt(gtt_id)
        if g.get("status") == "active":
            kite.delete_gtt(gtt_id)
        return None
    except KiteError as e:
        return f"couldn't delete GTT {gtt_id} ({e}); delete it in Kite > Orders > GTT"


def cancel_order(conn: sqlite3.Connection, oid: int, kite) -> str:
    o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
    if o is None:
        return "Unknown order."
    if o["status"] == "draft":
        return cancel_draft(conn, oid)
    if o["status"] not in ("placed", "triggered", "filled"):
        return f"Order #{oid} is {o['status']}; nothing to cancel."
    problems = []
    if o["status"] == "placed":
        problems.append(_delete(kite, o["buy_gtt_id"]))
        problems.append(_delete(kite, o["sl_gtt_id"]))
        _upd(conn, oid, status="cancelled", sl_status="deleted" if o["sl_gtt_id"] else o["sl_status"])
        done = (f"Cancelled order #{oid}: " + ("sell trigger removed." if o["side"] == "SELL" else
                "buy trigger" + (" and stop-loss trigger" if o["sl_gtt_id"] else "") + " removed."))
    else:
        problems.append(_delete(kite, o["sl_gtt_id"]))
        _upd(conn, oid, sl_status="deleted")
        done = (f"Removed the stop-loss trigger for #{oid}. The buy already triggered; "
                "an open limit order (if any) must be cancelled in the Kite app.")
    problems = [p for p in problems if p]
    return done + ("\n⚠️ " + "\n⚠️ ".join(problems) if problems else "")


# ------------------------------------------------------------------ tracking
def _trigger_result(g: dict) -> tuple[str | None, str | None]:
    """(order status, rejection reason) of a triggered GTT's order."""
    for leg in g.get("orders") or []:
        res = (leg.get("result") or {})
        orr = res.get("order_result") or {}
        if orr or res:
            return orr.get("status") or res.get("status"), orr.get("rejection_reason") or None
    return None, None


def sync(conn: sqlite3.Connection, kite, now: datetime | None = None) -> list[str]:
    """Advance every open order from Kite's GTT state. Returns messages for Telegram."""
    now = now or now_ist()
    msgs: list[str] = []
    rows = conn.execute("SELECT * FROM kite_orders WHERE status IN ('placed','triggered') "
                        "OR (status='filled' AND sl_status='active')").fetchall()
    for o in rows:
        oid, sym = o["id"], o["symbol"]
        try:
            if o["side"] == "SELL":
                m = _sync_sell(conn, o, kite, now)
                if m:
                    msgs.append(m)
                continue
            if o["status"] == "placed":
                g = kite.gtt(o["buy_gtt_id"])
                st = g.get("status")
                if st == "triggered":
                    ost, why = _trigger_result(g)
                    day = (g.get("updated_at") or now.strftime("%Y-%m-%d"))[:10]
                    if ost and ost.lower() in ("rejected", "failed", "cancelled"):
                        _upd(conn, oid, status="failed", triggered_on=day, note=(why or ost)[:300])
                        err = _delete(kite, o["sl_gtt_id"])
                        msgs.append(f"❌ #{oid} {sym}: buy trigger fired but the order was {ost.lower()}"
                                    f"{': ' + why if why else ''}. Stop-loss removed." + (f"\n⚠️ {err}" if err else ""))
                        continue
                    _upd(conn, oid, status="triggered", triggered_on=day)
                    msgs.append(f"⚡ #{oid} {sym}: buy trigger fired, limit order at ₹{o['limit_price']:,.2f} "
                                "sent to the exchange.")
                    o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
                elif st in ("cancelled", "deleted", "expired", "disabled", "rejected"):
                    _upd(conn, oid, status="cancelled", note=f"buy GTT {st}")
                    err = _delete(kite, o["sl_gtt_id"])
                    msgs.append(f"ℹ️ #{oid} {sym}: buy trigger is {st} (in Kite); stop-loss removed."
                                + (f"\n⚠️ {err}" if err else ""))
                    continue
                else:
                    m = _maybe_expire_buy(conn, o, kite, now)
                    if m:
                        msgs.append(m)
                    continue
            if o["status"] == "triggered":
                held, day_price = kite.held_qty(sym)
                if held >= (o["held_before"] or 0) + o["qty"]:
                    price = day_price or o["limit_price"]
                    _upd(conn, oid, status="filled", fill_price=price)
                    conn.execute("INSERT INTO real_trades(pick_id,symbol,side,qty,price,traded_at,note) "
                                 "VALUES(?,?,?,?,?,?,?)", (o["pick_id"], sym, "buy", o["qty"], price, iso_utc(),
                                                           f"kite gtt #{oid}" + ("" if day_price else " (≈limit)")))
                    conn.commit()
                    msg = f"✅ #{oid} {sym}: bought {o['qty']} @ ₹{price:,.2f}."
                    if o["sl_status"] in ("deferred", "failed") and o["sl_trigger"]:
                        msg += "\n" + _place_sl(conn, oid, kite)
                    msgs.append(msg)
                    continue
                trig_day = o["triggered_on"] or now.date().isoformat()
                if now.date().isoformat() > trig_day or now.strftime("%H:%M") >= "15:35":
                    _upd(conn, oid, status="unfilled")
                    err = _delete(kite, o["sl_gtt_id"])
                    msgs.append(f"ℹ️ #{oid} {sym}: the limit buy at ₹{o['limit_price']:,.2f} didn't fill and "
                                "lapsed at the close. Stop-loss removed." + (f"\n⚠️ {err}" if err else ""))
                continue
            if o["status"] == "filled" and o["sl_gtt_id"] and o["sl_status"] == "active":
                g = kite.gtt(o["sl_gtt_id"])
                st = g.get("status")
                if st == "triggered":
                    ost, why = _trigger_result(g)
                    if ost and ost.lower() in ("rejected", "failed", "cancelled"):
                        _upd(conn, oid, sl_status="failed", note=(why or ost)[:300])
                        msgs.append(f"⚠️ #{oid} {sym}: stop-loss fired but the sell was {ost.lower()}"
                                    f"{': ' + why if why else ''}. Check Kite now.")
                        continue
                    price = kite.sold_price_today(sym)
                    _upd(conn, oid, status="stopped", sl_status="triggered")
                    conn.execute("INSERT INTO real_trades(pick_id,symbol,side,qty,price,traded_at,note) "
                                 "VALUES(?,?,?,?,?,?,?)", (o["pick_id"], sym, "sell", o["qty"],
                                                           price or o["sl_limit"], iso_utc(),
                                                           f"stop-loss gtt #{oid}" + ("" if price else " (≈limit)")))
                    conn.commit()
                    msgs.append(f"🛑 #{oid} {sym}: stop-loss fired, sell limit ₹{o['sl_limit']:,.2f} sent"
                                + (f", sold @ ₹{price:,.2f}" if price else "") + ". Check Kite for the fill.")
                elif st in ("cancelled", "deleted", "expired", "disabled", "rejected"):
                    _upd(conn, oid, status="closed", sl_status=st)
                    msgs.append(f"ℹ️ #{oid} {sym}: stop-loss trigger is {st} in Kite. Position is no longer "
                                "protected by this bot.")
        except KiteError as e:
            log.warning("sync #%s failed: %s", oid, e)
    return msgs


def _maybe_expire_buy(conn: sqlite3.Connection, o, kite, now: datetime) -> str | None:
    """Delete a still-waiting BUY GTT (and its stop-loss) once its trading day's cut-off has passed."""
    if not o["confirmed_at"]:
        return None
    confirmed = datetime.fromisoformat(o["confirmed_at"]).astimezone(now.tzinfo or timezone.utc)
    exp = buy_expiry(confirmed, load_config(), conn)
    if exp is None:
        return None
    d, cut = exp
    if now.date() < d or (now.date() == d and now.strftime("%H:%M") < cut):
        return None
    errs = [e for e in (_delete(kite, o["buy_gtt_id"]), _delete(kite, o["sl_gtt_id"])) if e]
    if errs:
        return f"⚠️ #{o['id']} {o['symbol']}: tried to remove the unfired buy trigger but " + "; ".join(errs)
    _upd(conn, o["id"], status="expired", sl_status="deleted" if o["sl_gtt_id"] else o["sl_status"],
         note=f"not triggered by {cut}")
    return (f"⌛ #{o['id']} {o['symbol']}: buy trigger at ₹{o['trigger_price']:,.2f} didn't fire by {cut}, "
            "so I deleted it" + (" and its stop-loss" if o["sl_gtt_id"] else "") + ". Nothing was bought.")


def _sync_sell(conn: sqlite3.Connection, o, kite, now: datetime) -> str | None:
    oid, sym = o["id"], o["symbol"]
    if o["status"] == "placed":
        g = kite.gtt(o["buy_gtt_id"])
        st = g.get("status")
        if st in ("cancelled", "deleted", "expired", "disabled", "rejected"):
            _upd(conn, oid, status="cancelled", note=f"sell GTT {st}")
            return f"ℹ️ #{oid} {sym}: target sell trigger is {st} in Kite."
        if st != "triggered":
            return None
        ost, why = _trigger_result(g)
        day = (g.get("updated_at") or now.strftime("%Y-%m-%d"))[:10]
        if ost and ost.lower() in ("rejected", "failed", "cancelled"):
            _upd(conn, oid, status="failed", triggered_on=day, note=(why or ost)[:300])
            hint = " If it mentions authorisation: enable DDPI or authorise holdings in Kite." if why else ""
            return f"❌ #{oid} {sym}: target hit but the sell was {ost.lower()}{': ' + why if why else ''}.{hint}"
        _upd(conn, oid, status="triggered", triggered_on=day)
        o = conn.execute("SELECT * FROM kite_orders WHERE id=?", (oid,)).fetchone()
        first = f"⚡ #{oid} {sym}: target reached, sell order at ₹{o['limit_price']:,.2f} sent."
    else:
        first = None
    sold_q, sold_p = kite.sold_today(sym)
    held, _, _ = kite.holding(sym)
    if sold_q >= o["qty"] or held <= (o["held_before"] or 0) - o["qty"]:
        price = sold_p or o["limit_price"]
        _upd(conn, oid, status="sold", fill_price=price)
        conn.execute("INSERT INTO real_trades(pick_id,symbol,side,qty,price,traded_at,note) VALUES(?,?,?,?,?,?,?)",
                     (o["pick_id"], sym, "sell", o["qty"], price, iso_utc(),
                      f"target gtt #{oid}" + ("" if sold_p else " (≈limit)")))
        conn.commit()
        return (first + "\n" if first else "") + f"✅ #{oid} {sym}: sold {o['qty']} @ ₹{price:,.2f}."
    trig_day = o["triggered_on"] or now.date().isoformat()
    if now.date().isoformat() > trig_day or now.strftime("%H:%M") >= "15:35":
        _upd(conn, oid, status="unfilled")
        return (first + "\n" if first else "") + (
            f"ℹ️ #{oid} {sym}: the sell at ₹{o['limit_price']:,.2f} didn't fill and lapsed at the close. "
            "Send the sell again if you still want it.")
    return first


def orders_text(conn: sqlite3.Connection, limit: int = 10) -> str:
    rows = conn.execute("SELECT * FROM kite_orders WHERE status NOT IN ('draft','stale') "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    head = "🟢 Ordering ON" if ordering_enabled(conn) else "⛔ Ordering OFF (/orderson to enable)"
    if not rows:
        return f"{head}\nNo orders yet. Example: <code>buy HAL ₹2500 @now sl 6%</code>"
    lines = [head, "<b>Recent orders</b>"]
    for o in rows:
        sl = f" · SL ₹{o['sl_trigger']:,.2f} ({o['sl_status'] or '-'})" if o["sl_trigger"] else " · no SL"
        fill = f" filled ₹{o['fill_price']:,.2f}" if o["fill_price"] else ""
        if o["side"] == "SELL":
            sl = ""
        lines.append(f"#{o['id']} {o['side']} {o['symbol']} × {o['qty']} @ ₹{o['limit_price']:,.2f} — "
                     f"<b>{o['status']}</b>{fill}{sl}")
    lines.append("Cancel: /cancel &lt;#&gt;")
    return "\n".join(lines)


def open_order_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM kite_orders WHERE status IN ('placed','triggered') "
                        "OR (status='filled' AND sl_status='active')").fetchone()[0]


def sync_tick(conn: sqlite3.Connection, cfg: Config, now: datetime, send) -> None:
    """Every ~5 min (scheduler tick and the bot's own loop; a file lock stops double messages):
    advance bot orders and report any GTT in the account that fired, failed, expired or vanished.
    Needs today's Kite login; nags once a day during market hours if it's missing."""
    import fcntl

    from .config import DATA_DIR
    from .kite import Kite
    lock_f = open(DATA_DIR / "orders.lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_f.close()
        return
    try:
        try:
            kite = Kite(conn)
        except KiteError:
            day = now.date().isoformat()
            watching = open_order_count(conn) > 0 or "active" in (kv_get(conn, "gtt_snapshot") or "")
            if watching and now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "15:30" \
                    and kv_get(conn, "kite_nag") != day:
                kv_set(conn, "kite_nag", day)
                send("🔑 Not logged in to Kite today, so I can't watch your GTTs (fired / failed alerts) or "
                     "track bot orders. Send /kitelogin.")
            return
        for m in sync(conn, kite, now):
            send(m)
        for m in watch_gtts(conn, kite.gtts()):
            send(m)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def gtt_outcome(g: dict) -> str:
    """Plain-English result of a GTT."""
    st = g.get("status")
    if st == "active":
        return "waiting"
    if st == "triggered":
        ost, why = _trigger_result(g)
        if ost and ost.lower() in ("rejected", "failed", "cancelled"):
            return f"fired, but the order was REJECTED{': ' + why if why else ''}"
        if ost:
            return "fired, order sent to the exchange (it's a limit order: check the fill in Kite / /holdings)"
        return "fired"
    return {"cancelled": "cancelled", "deleted": "deleted", "expired": "expired (1-year validity ended)",
            "disabled": "disabled by Kite", "rejected": "rejected by Kite"}.get(st, str(st))


def _gtt_brief(g: dict) -> str:
    c = g.get("condition") or {}
    trig = c.get("trigger_values") or []
    legs = []
    for i, leg in enumerate(g.get("orders") or []):
        t = trig[i] if i < len(trig) else (trig[0] if trig else 0)
        legs.append(f"{leg.get('transaction_type')} {leg.get('quantity')} @ trigger ₹{float(t or 0):,.2f} → "
                    f"₹{float(leg.get('price') or 0):,.2f}")
    return f"{c.get('tradingsymbol', '?')} " + " / ".join(legs) + f" (GTT {g.get('id')})"


def watch_gtts(conn: sqlite3.Connection, gtts: list[dict]) -> list[str]:
    """Compare the account's GTTs with the last snapshot; report status changes of GTTs the user made.
    (Bot-made GTTs are reported by sync() with more detail.) First run only records a baseline."""
    import json
    bot_ids = {int(x) for r in conn.execute("SELECT buy_gtt_id, sl_gtt_id FROM kite_orders")
               for x in (r["buy_gtt_id"], r["sl_gtt_id"]) if x}
    raw = kv_get(conn, "gtt_snapshot")
    prev: dict[str, dict] = json.loads(raw) if raw else {}
    cur = {str(g["id"]): g for g in gtts if g.get("id") is not None}
    msgs = []
    if raw is not None:
        for gid, g in cur.items():
            if int(gid) in bot_ids:
                continue
            was = (prev.get(gid) or {}).get("status")
            st = g.get("status")
            if was == st:
                continue
            if was is None and st == "active":
                msgs.append(f"👤 New GTT in your account: {_gtt_brief(g)}")
            elif was == "active" or was is None:
                icon = "⚠️" if "REJECTED" in gtt_outcome(g) else ("⚡" if st == "triggered" else "ℹ️")
                msgs.append(f"{icon} Your GTT {_gtt_brief(g)}: {gtt_outcome(g)}")
        for gid, info in prev.items():
            if gid not in cur and info.get("status") == "active" and int(gid) not in bot_ids:
                msgs.append(f"ℹ️ Your GTT {info.get('brief', gid)} is gone from Kite (deleted).")
    snap = {gid: {"status": g.get("status"), "brief": _gtt_brief(g)} for gid, g in cur.items()}
    kv_set(conn, "gtt_snapshot", json.dumps(snap))
    return msgs


def holdings_text(holdings: list[dict], gtts: list[dict]) -> str:
    """Portfolio view from Kite holdings, with the sell GTTs (targets / stop-losses) covering each one."""
    from html import escape
    rows = []
    for h in holdings:
        q = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
        if q <= 0:
            continue
        avg, last = float(h.get("average_price") or 0), float(h.get("last_price") or 0)
        rows.append((h.get("tradingsymbol"), q, int(h.get("t1_quantity", 0)), avg, last,
                     float(h.get("day_change_percentage") or 0), float(h.get("day_change") or 0),
                     h.get("pnl")))
    if not rows:
        return "No holdings in Kite."
    sells: dict[str, list[str]] = {}
    for g in gtts:
        if g.get("status") != "active":
            continue
        c = g.get("condition") or {}
        sym, trig = c.get("tradingsymbol"), c.get("trigger_values") or []
        for i, leg in enumerate(g.get("orders") or []):
            if leg.get("transaction_type") != "SELL":
                continue
            t = float(trig[i] if i < len(trig) else trig[0])
            kind = "🎯" if t > float(c.get("last_price") or 0) else "🛡"
            sells.setdefault(sym, []).append(f"{kind} {leg.get('quantity')} @ ₹{t:,.2f}")
    rows.sort(key=lambda r: r[1] * r[4], reverse=True)
    inv = sum(r[1] * r[3] for r in rows)
    cur = sum(r[1] * r[4] for r in rows)
    day = sum(r[1] * r[6] for r in rows)                      # Kite's per-share day change x qty
    pnl = sum(float(r[7]) if r[7] is not None else (r[4] - r[3]) * r[1] for r in rows)  # Kite's own P&L
    lines = [f"<b>Holdings: {len(rows)}</b>  invested ₹{inv:,.0f} → now ₹{cur:,.0f}",
             f"P&amp;L <b>{'+' if pnl >= 0 else '−'}₹{abs(pnl):,.0f}</b> ({pnl / inv * 100 if inv else 0:+.2f}%) · "
             f"today {'+' if day >= 0 else '−'}₹{abs(day):,.0f}", ""]
    for sym, q, t1, avg, last, dchg, _, kpnl in rows:
        p = float(kpnl) if kpnl is not None else (last - avg) * q
        pct = (last / avg - 1) * 100 if avg else 0
        mark = "🟢" if p >= 0 else "🔴"
        line = (f"{mark} <b>{escape(str(sym))}</b> {q}{f' ({t1} T1)' if t1 else ''} · avg ₹{avg:,.2f} · "
                f"LTP ₹{last:,.2f}\n    {'+' if p >= 0 else '−'}₹{abs(p):,.0f} ({pct:+.1f}%) · "
                f"today {dchg:+.1f}% · value ₹{q * last:,.0f}")
        if sym in sells:
            line += "\n    GTT: " + ", ".join(sells[sym])
        else:
            line += "\n    <i>no sell GTT (no target / stop-loss)</i>"
        lines.append(line)
    lines.append("\n🎯 target above price · 🛡 stop-loss below. Prices from Kite (LTP can lag outside market hours).")
    return "\n".join(lines)


def gtt_list_text(conn: sqlite3.Connection, gtts: list[dict], show_all: bool = False, limit: int = 30) -> str:
    """All GTTs in the Kite account (read-only view). Bot-created ones are tagged with their order #."""
    from html import escape
    ours: dict[int, tuple[int, str]] = {}
    for r in conn.execute("SELECT id, side, buy_gtt_id, sl_gtt_id FROM kite_orders "
                          "WHERE buy_gtt_id IS NOT NULL OR sl_gtt_id IS NOT NULL"):
        if r["buy_gtt_id"]:
            ours[int(r["buy_gtt_id"])] = (r["id"], "target" if r["side"] == "SELL" else "buy")
        if r["sl_gtt_id"]:
            ours[int(r["sl_gtt_id"])] = (r["id"], "stop-loss")
    rows = [g for g in gtts if show_all or g.get("status") == "active"]
    rows.sort(key=lambda g: g.get("created_at") or "", reverse=True)
    if not rows:
        return "No active GTTs in Kite." + ("" if show_all else " (/gtt all also shows fired/cancelled ones)")
    title = "All GTTs (recent first)" if show_all else f"Active GTTs in Kite: {len(rows)}"
    lines = [f"<b>{title}</b>"]
    for g in rows[:limit]:
        c = g.get("condition") or {}
        sym = escape(str(c.get("tradingsymbol", "?")))
        trig = c.get("trigger_values") or []
        legs = []
        for i, leg in enumerate(g.get("orders") or []):
            t = trig[i] if i < len(trig) else (trig[0] if trig else None)
            legs.append(f"{leg.get('transaction_type')} {leg.get('quantity')} · trigger ₹{float(t or 0):,.2f} → "
                        f"₹{float(leg.get('price') or 0):,.2f}")
        tag = ours.get(int(g.get("id", 0)))
        who = f"🤖 bot #{tag[0]} {tag[1]}" if tag else "👤 yours"
        kind = "OCO" if g.get("type") == "two-leg" else "single"
        status = "" if g.get("status") == "active" else f" — <b>{escape(gtt_outcome(g))}</b>"
        last = c.get("last_price")
        lines.append(f"\n<b>{sym}</b> ({kind}, GTT {g.get('id')}){status}\n  " + "\n  ".join(legs)
                     + f"\n  {who} · created {str(g.get('created_at', ''))[:10]}"
                     + (f" · LTP at creation ₹{float(last):,.2f}" if last else ""))
    if len(rows) > limit:
        lines.append(f"\n…and {len(rows) - limit} more")
    lines.append("\nRemove a bot order: /cancel &lt;#&gt;. Your own GTTs: edit/delete in Kite > Orders > GTT.")
    return "\n".join(lines)
