"""On-demand scan (/scan in Telegram, `cli scan`).

Runs the same screen as the morning job (prices, rules, sector and global cues, news) at any time,
then checks your Kite holdings against simple rules: book profit, protect profit, weak, or hold.
Nothing is stored as a pick (the morning run stays the tracked experiment) and nothing is sold:
every message is a flag for you to decide on."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html import escape

import pandas as pd

from . import market_data as md
from .orders import tp_label
from . import news as newsmod
from .config import Config
from .signals import rsi, sma
from .telegram_bot import FOOTER, esc
from .universe import load_universe, yf_ticker
from .util import now_ist

log = logging.getLogger(__name__)
MARKET_CLOSE = time(15, 30)
Buttons = list[list[tuple[str, str]]] | None


def scan_session(now: datetime) -> date:
    """The session the scan prepares for. Before the close only completed daily bars are used (today's
    partial bar is excluded); after the close today's bar is complete, so the scan is for the next day."""
    return now.date() if now.time() < MARKET_CLOSE else now.date() + timedelta(days=1)


def clean_symbol(kite_symbol: str) -> str:
    """Kite trading symbol -> NSE symbol for Yahoo (drops series suffixes like -BE)."""
    return re.sub(r"-(BE|BZ|SM|ST|EQ)$", "", kite_symbol or "")


# ---------------- holdings check ----------------
@dataclass
class HoldingCheck:
    symbol: str
    qty: int
    avg: float
    last: float
    pnl: float
    pnl_pct: float
    flag: str = "hold"            # book / protect / weak / hold / nodata
    reasons: list[str] = field(default_factory=list)
    stop_hint: float | None = None
    has_sell_gtt: bool = False

    @property
    def value(self) -> float:
        return self.qty * self.last


def trend_stats(df: pd.DataFrame | None, cfg: Config) -> dict | None:
    """Indicators from adjusted daily bars (only completed bars should be passed in)."""
    if df is None or len(df) < 60:
        return None
    c = df["close"]
    out = {"close": float(c.iloc[-1]), "sma_fast": float(sma(c, cfg.review.fast_sma).iloc[-1]),
           "sma50": float(sma(c, cfg.signals.sma_fast).iloc[-1]),
           "sma200": float(sma(c, cfg.signals.sma_slow).iloc[-1]) if len(c) >= cfg.signals.sma_slow else None,
           "rsi": float(rsi(c, cfg.signals.rsi_period).iloc[-1]),
           "ret5": float((c.iloc[-1] / c.iloc[-6] - 1) * 100) if len(c) >= 6 else 0.0}
    # raw (unadjusted) level of the fast average, for a stop-loss hint in today's prices
    f = float(df["raw_close"].iloc[-1] / c.iloc[-1]) if c.iloc[-1] else 1.0
    out["sma_fast_raw"] = out["sma_fast"] * f
    return out


def classify(h: HoldingCheck, st: dict | None, news_score: float, cfg: Config) -> HoldingCheck:
    r = cfg.review
    if st is None:
        h.flag = "nodata"
        h.reasons.append("no price history")
        return h
    fading = st["close"] < st["sma_fast"]
    hot = st["rsi"] >= cfg.signals.rsi_max
    below50 = st["close"] < st["sma50"]
    downtrend = below50 and st["sma200"] is not None and st["sma50"] < st["sma200"]
    bad_news = news_score <= cfg.news.exclude_at_or_below
    if h.pnl_pct >= r.take_profit_pct:
        if fading:
            h.reasons.append(f"closed below its {r.fast_sma}-day average (momentum fading)")
        if hot:
            h.reasons.append(f"RSI {st['rsi']:.0f}, stretched")
        if bad_news:
            h.reasons.append("negative news")
        if h.reasons:
            h.flag = "book"
        else:
            h.flag = "protect"
            h.reasons.append("trend still up")
            h.stop_hint = st["sma_fast_raw"] if st["sma_fast_raw"] < h.last else None
        return h
    if downtrend:
        h.reasons.append("downtrend (below 50-day avg, which is below 200-day avg)")
    elif below50 and st["ret5"] <= -r.weak_drop_5d_pct:
        h.reasons.append(f"fell {st['ret5']:.1f}% in 5 days and is below its 50-day avg")
    if bad_news:
        h.reasons.append("negative news")
    if h.reasons:
        h.flag = "weak"
    return h


def check_holdings(holdings: list[dict], gtts: list[dict], prices: dict[str, pd.DataFrame],
                   news_scores: dict[str, float], cfg: Config, session: date) -> list[HoldingCheck]:
    sells = {(g.get("condition") or {}).get("tradingsymbol") for g in gtts if g.get("status") == "active"
             and any(l.get("transaction_type") == "SELL" for l in g.get("orders") or [])}
    cut = pd.Timestamp(session)
    out = []
    for h in holdings:
        q = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
        if q <= 0:
            continue
        sym = h.get("tradingsymbol") or ""
        avg, last = float(h.get("average_price") or 0), float(h.get("last_price") or 0)
        pnl = float(h["pnl"]) if h.get("pnl") is not None else (last - avg) * q
        hc = HoldingCheck(sym, q, avg, last, pnl, (last / avg - 1) * 100 if avg else 0.0,
                          has_sell_gtt=sym in sells)
        df = prices.get(yf_ticker(clean_symbol(sym)))
        st = trend_stats(df[df.index < cut] if df is not None else None, cfg)
        out.append(classify(hc, st, news_scores.get(sym, 0.0), cfg))
    order = {"book": 0, "weak": 1, "protect": 2, "hold": 3, "nodata": 4}
    out.sort(key=lambda x: (order[x.flag], -x.pnl_pct))
    return out


def holdings_review_text(checks: list[HoldingCheck], cfg: Config) -> str:
    if not checks:
        return "<b>📋 Your holdings</b>\nNo holdings in Kite."

    def money(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f}"

    lines = [f"<b>📋 Your holdings: rules check</b> (profit flag at ≥{cfg.review.take_profit_pct:g}%)"]
    groups = [("book", "📤 <b>Profit + weakening</b> (rules flag booking profit)"),
              ("weak", "⚠️ <b>Weak</b> (rules flag a possible exit)"),
              ("protect", "🔒 <b>In profit, trend intact</b> (rules flag protecting the gain)")]
    for flag, title in groups:
        g = [c for c in checks if c.flag == flag]
        if not g:
            continue
        lines += ["", title]
        for c in g:
            gtt = " · has sell GTT" if c.has_sell_gtt else ""
            lines.append(f"• <b>{escape(c.symbol)}</b> {c.qty} · {c.pnl_pct:+.1f}% ({money(c.pnl)}) · "
                         f"value ₹{c.value:,.0f}{gtt}\n    {esc('; '.join(c.reasons))}")
            if flag == "book":
                lines.append(f"    Sell now in Kite, or set a target: <code>sell {escape(c.symbol)} all @"
                             f"{c.last * 1.01:.2f}</code>")
            elif flag == "protect" and c.stop_hint:
                lines.append(f"    e.g. a stop-loss GTT in Kite near the {cfg.review.fast_sma}-day avg "
                             f"₹{c.stop_hint:,.2f} locks in most of the gain")
    hold = [c for c in checks if c.flag == "hold"]
    if hold:
        lines += ["", "✅ <b>No rule flag</b>: " + ", ".join(
            f"{escape(c.symbol)} {c.pnl_pct:+.1f}%" for c in hold)]
    nod = [c for c in checks if c.flag == "nodata"]
    if nod:
        lines.append("❔ No price history (not checked): " + ", ".join(escape(c.symbol) for c in nod))
    return "\n".join(lines)


def rotation_text(checks: list[HoldingCheck], buy_symbols: list[str], cfg: Config) -> str | None:
    out = [c for c in checks if c.flag in ("book", "weak")]
    held = {c.symbol for c in checks}
    fresh = [s for s in buy_symbols if s not in held]
    if not out or not fresh:
        return None
    freed = sum(c.value for c in out)
    n = max(1, min(len(fresh), int(freed // cfg.kite.max_order_value)))
    return (f"🔄 <b>Rotation idea</b>: exiting {', '.join(escape(c.symbol) for c in out)} would free "
            f"≈₹{freed:,.0f}, enough for {n} of today's buy ideas at ₹{cfg.kite.max_order_value:,.0f} each: "
            f"{', '.join(escape(s) for s in fresh[:n])}. Costs (~₹16 DP per sell + taxes) apply on every exit.")


def weakest_text(cands: list, cfg: Config) -> str | None:
    """Universe stocks in a downtrend with the worst 3-month strength vs Nifty."""
    latest = max((c.signal_date for c in cands), default=None)
    weak = [c for c in cands if c.liquid and c.signal_date == latest and c.components.get("trend") == 0
            and c.components.get("rel_strength") is not None]
    weak.sort(key=lambda c: c.components["rel_strength"])
    weak = weak[: cfg.review.weakest_count]
    if not weak:
        return None
    return ("📉 <b>Weakest right now</b> (not in an uptrend, worst 3-month strength vs Nifty), so avoid buying: "
            + ", ".join(f"{escape(c.stock.symbol)} {c.components['rel_strength']:+.0f}%" for c in weak)
            + "\n<i>Delivery (CNC) can't be sold short, so these aren't sell trades unless you own them.</i>")


# ---------------- the scan ----------------
def run_scan(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[tuple[str, Buttons]]:
    """Returns the messages to send, each with optional inline buttons."""
    from .jobs import compute_picks, header_text, pick_text
    from .kite import Kite, KiteError

    now = now or now_ist()
    session = scan_session(now)
    result = compute_picks(conn, cfg, session)
    msgs: list[tuple[str, Buttons]] = []
    head = header_text(result, session).split("\n", 1)
    stamp = f"<b>🔎 Manual scan – {now.strftime('%a %d %b %H:%M')} IST</b> (for the {session.strftime('%a %d %b')} session)"
    msgs.append((stamp + ("\n" + head[1] if len(head) > 1 else "") +
                 "\n<i>Not saved as picks: the paper portfolio only tracks the morning run.</i>", None))

    picks = result["picks"]
    cap = cfg.kite.max_order_value
    if not picks:
        msgs.append(("No buy ideas: nothing passed the rules, affordability, liquidity and score threshold.", None))
    for i, c in enumerate(picks, 1):
        text = pick_text(i, c, cfg).rsplit("\n", 1)[0]  # drop the paper-entry line
        buttons = ([[(f"🛒 Buy {c.stock.symbol} ₹{cap:,.0f} @now · SL {cfg.kite.default_sl_pct:g}%"
                      + tp_label(cfg.kite), f"ordsym:{c.stock.symbol}")]] if cfg.kite.real_orders else None)
        msgs.append((text, buttons))

    # holdings (needs today's Kite login)
    try:
        kite = Kite(conn)
        holdings, gtts = kite.holdings(), kite.gtts()
    except KiteError as e:
        log.info("scan without holdings: %s", e)
        msgs.append(("📋 Holdings not checked: log in to Kite today with /kitelogin, then /scan again.", None))
        holdings = None
    if holdings is not None:
        uni = {s.symbol: s for s in load_universe()}
        syms = sorted({h.get("tradingsymbol") for h in holdings
                       if int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0)) > 0})
        prices = md.get_prices(conn, [yf_ticker(clean_symbol(s)) for s in syms], cfg.market.history_days)
        queries = {}
        for s in syms:
            st = uni.get(clean_symbol(s))
            queries[s] = f'"{st.name}" stock NSE' if st else f'"{clean_symbol(s)}" share NSE'
        fetched = newsmod.fetch_many(queries, cfg) if queries else {}
        scores = {}
        for s, hl in fetched.items():
            st = uni.get(clean_symbol(s))
            name, aliases = (st.name, st.aliases) if st else (clean_symbol(s), ())
            scores[s] = float(newsmod.net_score([h for h in hl if newsmod.mentions(h, name, clean_symbol(s), aliases)],
                                                cfg))
        checks = check_holdings(holdings, gtts, prices, scores, cfg, session)
        msgs.append((holdings_review_text(checks, cfg), None))
        rot = rotation_text(checks, [c.stock.symbol for c in picks], cfg)
        if rot:
            msgs.append((rot, None))

    weak = weakest_text(result.get("cands") or [], cfg)
    if weak:
        msgs.append((weak, None))
    msgs.append((FOOTER.replace("Paper-traded; you place any real order yourself.",
                                "Rules-based flags only; every order needs your Confirm tap."), None))
    return msgs
