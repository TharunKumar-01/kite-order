"""Telegram: a small synchronous sender for jobs (Bot API over HTTPS) and the
long-polling approval bot (python-telegram-bot v20+). Only TELEGRAM_CHAT_ID is served."""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

import requests

from .config import load_config, load_secrets
from .orders import tp_label
from .util import redact

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000
FOOTER = "<i>Learning tool, not advice. Paper-traded; you place any real order yourself.</i>"


def esc(s) -> str:
    return html.escape(str(s), quote=False)


class TelegramClient:
    def __init__(self, dry_run: bool = False):
        s = load_secrets()
        self.token, self.chat_id = s.telegram_bot_token, s.telegram_chat_id
        self.dry_run = dry_run or not (self.token and self.chat_id)
        if not dry_run and self.dry_run:
            log.warning("Telegram not configured (.env); printing messages instead of sending")

    def _call(self, method: str, payload: dict) -> dict | None:
        url = API.format(token=self.token, method=method)
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=15)
                data = r.json()
                if data.get("ok"):
                    return data["result"]
                retry_after = (data.get("parameters") or {}).get("retry_after")
                if retry_after:
                    time.sleep(min(int(retry_after), 30))
                    continue
                log.error("Telegram %s failed: %s", method, redact(str(data.get("description"))))
                return None
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram %s error (attempt %d): %s", method, attempt + 1, redact(str(e)))
                time.sleep(2 * (attempt + 1))
        return None

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> int | None:
        """Send HTML text; returns message_id. buttons: rows of (label, callback_data)."""
        chunks = split_message(text)
        if self.dry_run:
            for ch in chunks:
                print(strip_html(ch))
            if buttons:
                print("   " + "  ".join(f"[{b[0]}]" for row in buttons for b in row))
            print("-" * 60)
            return None
        mid = None
        for i, ch in enumerate(chunks):
            payload = {"chat_id": self.chat_id, "text": ch, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                               for row in buttons]}
            res = self._call("sendMessage", payload)
            if res:
                mid = res["message_id"]
        return mid

    def get_me(self) -> dict | None:
        return None if self.dry_run else self._call("getMe", {})


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)[:limit]
    if cur:
        out.append(cur)
    return out


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def alert(text: str) -> None:
    """Best-effort error/notice message; never raises."""
    try:
        TelegramClient().send(text)
    except Exception as e:  # noqa: BLE001
        log.error("could not send alert: %s", redact(str(e)))


# =====================================================================================
# Approval bot (long polling)
# =====================================================================================
HELP = (
    "<b>Commands</b>\n"
    "/scan – run the screen now: 5 buy ideas + a check of your holdings (profit to book, weak ones)\n"
    "/status – open paper positions + today's picks\n"
    "/pnl – paper P&amp;L vs Nifty, hit rates\n"
    "/pause – stop sending picks\n"
    "/resume – resume picks\n"
    "/help – this message\n"
    "/kitelogin – daily Kite login link\n"
    "/kitetest – place + delete a harmless test GTT\n"
    "/orders – your bot-placed Kite orders\n"
    "/gtt – every active GTT in your Kite account (/gtt all = incl. fired/failed/cancelled)\n"
    "/holdings – your Kite holdings, P&amp;L and which have a target/stop-loss GTT\n"
    "(I check every 5 min and message you when any GTT fires, fails or expires)\n"
    "/cancel N – remove order N's triggers\n"
    "/stop – switch Kite ordering off · /orderson – back on\n\n"
    "<b>Order via Kite GTT</b> (you confirm every order):\n"
    "<code>buy HAL 2 @4210 sl 3950</code>\n"
    "<code>buy HAL ₹2500 @now sl 6% tp 10%</code>\n"
    "<code>buy ₹2500</code> (last approved pick, now, default SL)\n"
    "<code>sl none</code> / <code>tp none</code> = no stop-loss / no target. Stop-loss + target go in one "
    "OCO sell GTT: whichever price comes first sells, the other is cancelled.\n"
    "/oco N [20%] – add a target to filled order N (replaces its stop-loss GTT with an OCO)\n"
    "<b>Target sell</b> (shares you hold):\n"
    "<code>sell JPL all @1455</code> · <code>sell JPL 12 profit 4000</code> · "
    "<code>sell JPL all target 5%</code>\n\n"
    "Log a manual fill: <code>bought HAL 1 4210.5</code> or <code>sold HAL 1 4400</code>"
)

TRADE_RE = re.compile(r"^\s*(bought|sold)\s+([A-Za-z0-9&\-]+)\s+(\d+)\s+(\d+(?:\.\d+)?)\s*$", re.I)


def parse_trade(text: str) -> tuple[str, str, int, float] | None:
    m = TRADE_RE.match(text or "")
    if not m:
        return None
    side = "buy" if m.group(1).lower() == "bought" else "sell"
    qty, price = int(m.group(3)), float(m.group(4))
    if qty <= 0 or price <= 0:
        return None
    return side, m.group(2).upper(), qty, price


def handle_decision(conn, pick_id: int, action: str, now_ist: datetime) -> tuple[str, str]:
    """Pure-ish decision handler (testable). Returns (result, toast).
    result: recorded|duplicate|expired|unknown."""
    from .db import record_decision
    cfg = load_config()
    pick = conn.execute("SELECT * FROM picks WHERE id=?", (pick_id,)).fetchone()
    if pick is None or action not in ("approve", "reject"):
        return "unknown", "Unknown pick"
    existing = conn.execute("SELECT decision FROM decisions WHERE pick_id=?", (pick_id,)).fetchone()
    if existing:
        return "duplicate", f"Already recorded: {existing['decision']}"
    expiry = cfg.scheduler.t(cfg.scheduler.pick_expiry_time)
    if now_ist.date().isoformat() > pick["run_date"] or (
            now_ist.date().isoformat() == pick["run_date"] and now_ist.time() >= expiry):
        record_decision(conn, pick_id, "expired")
        return "expired", "This pick has expired"
    if record_decision(conn, pick_id, action):
        return "recorded", "Approved" if action == "approve" else "Rejected"
    return "duplicate", "Already recorded"


def run_bot() -> None:
    import asyncio

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                              MessageHandler, filters)

    from . import reports
    from .db import connect, kv_set
    from .util import iso_utc, now_ist

    s = load_secrets()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    owner = s.telegram_chat_id
    only_owner = filters.Chat(chat_id=owner)

    def authorized(update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.id == owner)

    async def reply(update: Update, text: str) -> None:
        for ch in split_message(text):
            await update.effective_message.reply_html(ch, disable_web_page_preview=True)

    async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not authorized(update):
            log.warning("ignored callback from unauthorized chat")
            return
        action, _, pid = (q.data or "").partition(":")
        if not pid.isdigit():
            await q.answer()
            return
        conn = connect()
        try:
            result, toast = handle_decision(conn, int(pid), action, now_ist())
            await q.answer(toast)
            if result in ("recorded", "expired"):
                stamp = now_ist().strftime("%H:%M IST")
                label = {"approve": "✅ Approved", "reject": "❌ Rejected"}.get(action) if result == "recorded" \
                    else "⌛ Expired (no decision before cutoff)"
                base = q.message.text_html if q.message and q.message.text else ""
                try:
                    await q.edit_message_text(f"{base}\n\n<b>{label}</b> at {stamp}", parse_mode="HTML",
                                              disable_web_page_preview=True, reply_markup=None)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not edit message: %s", e)
                if result == "recorded" and action == "approve":
                    sym = conn.execute("SELECT symbol FROM picks WHERE id=?", (int(pid),)).fetchone()["symbol"]
                    k = load_config().kite
                    if not k.real_orders:
                        await q.message.reply_html("Noted. Every pick is tracked as a paper trade either way; "
                                                   "the evening summary compares your approved vs rejected picks.")
                        return
                    await q.message.reply_html(
                        f"Logged for paper trading. To order it in Kite, tap below or type e.g.\n"
                        f"<code>buy {esc(sym)} ₹{k.max_order_value:,.0f} @now sl {k.default_sl_pct:g}%</code> · "
                        f"<code>buy {esc(sym)} 2 @&lt;price&gt; sl &lt;price&gt;</code>\n"
                        f"(Bought elsewhere? <code>bought {esc(sym)} &lt;qty&gt; &lt;price&gt;</code>)",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            f"🛒 Buy ₹{k.max_order_value:,.0f} @now · SL {k.default_sl_pct:g}%"
                            + tp_label(k),
                            callback_data=f"ordnew:{pid}")]]))
            elif result == "duplicate":
                try:
                    await q.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            conn.close()

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        text = update.effective_message.text or ""
        if "request_token=" in text:
            await kite_token(update, text)
            return
        from .orders import OrderError, parse_order, parse_sell
        try:
            sell = parse_sell(text)
            req = None if sell else parse_order(text)
        except OrderError as e:
            ex = "sell JPL all @1455" if text.strip().lower().startswith("sell") else "buy HAL ₹2500 @now sl 6%"
            await reply(update, f"Order not understood: {esc(e)}\nExample: <code>{ex}</code>")
            return
        if sell is not None:
            await start_order(update.effective_message, sell)
            return
        if req is not None:
            await start_order(update.effective_message, req)
            return
        parsed = parse_trade(text)
        if not parsed:
            await reply(update, "Didn't understand that. " + HELP)
            return
        side, sym, qty, price = parsed
        conn = connect()
        try:
            pick = conn.execute("SELECT id FROM picks WHERE symbol=? AND status='approved' "
                                "ORDER BY run_date DESC LIMIT 1", (sym,)).fetchone()
            conn.execute("INSERT INTO real_trades(pick_id,symbol,side,qty,price,traded_at) VALUES(?,?,?,?,?,?)",
                         (pick["id"] if pick else None, sym, side, qty, price, iso_utc()))
            conn.commit()
        finally:
            conn.close()
        link = f" (linked to pick #{pick['id']})" if pick else " (no approved pick found; logged anyway)"
        await reply(update, f"Logged real {side}: {esc(sym)} {qty} @ ₹{price:,.2f}{link}")

    async def cmd_help(update: Update, ctx) -> None:
        await reply(update, HELP)

    async def cmd_status(update: Update, ctx) -> None:
        conn = connect()
        try:
            await reply(update, reports.status_text(conn))
        finally:
            conn.close()

    async def cmd_pnl(update: Update, ctx) -> None:
        conn = connect()
        try:
            await reply(update, reports.pnl_text(conn))
        finally:
            conn.close()

    async def cmd_pause(update: Update, ctx) -> None:
        conn = connect()
        kv_set(conn, "paused", "1")
        conn.close()
        await reply(update, "⏸ Paused. No picks will be sent until /resume (evening scoring continues).")

    async def cmd_resume(update: Update, ctx) -> None:
        conn = connect()
        kv_set(conn, "paused", "0")
        conn.close()
        await reply(update, "▶️ Resumed. Picks will be sent from the next pre-market run.")

    async def kite_token(update: Update, text: str) -> None:
        from .kite import KiteError, exchange_token, parse_request_token
        tok = parse_request_token(text)
        if not tok:
            await reply(update, "Couldn't find a request_token in that.")
            return
        conn = connect()
        try:
            who = exchange_token(conn, tok)
            await reply(update, f"✅ Logged in to Kite as {esc(who.get('user_id'))} for today.")
        except KiteError as e:
            await reply(update, f"Kite login failed: {esc(e)}")
        finally:
            conn.close()
        try:  # the token is single-use, but don't leave it lying in the chat
            await update.effective_message.delete()
        except Exception:  # noqa: BLE001
            pass

    async def cmd_kitelogin(update: Update, ctx) -> None:
        from .kite import KiteError, login_url
        try:
            url = login_url()
        except KiteError as e:
            await reply(update, esc(e))
            return
        await reply(update, f'<a href="{esc(url)}">Log in to Kite</a>\nAfter login the browser goes to a '
                            "127.0.0.1 address that won't load on your phone. That's expected: copy that "
                            "address and send it here (or use /token &lt;request_token&gt;).")

    async def cmd_token(update: Update, ctx) -> None:
        await kite_token(update, " ".join(ctx.args or []))

    def with_conn(fn, *args):
        """Run fn(conn, *args) on a worker thread with its own SQLite connection."""
        def run():
            c = connect()
            try:
                return fn(c, *args)
            finally:
                c.close()
        return asyncio.to_thread(run)

    async def cmd_kitetest(update: Update, ctx) -> None:
        from .kite import KiteError, test_gtt
        try:
            steps = await with_conn(test_gtt, "PNB")
            await reply(update, "\n".join(esc(s) for s in steps))
        except KiteError as e:
            await reply(update, f"❌ GTT test failed: {esc(e)}")

    # ---------------- Kite GTT ordering ----------------
    def _draft(conn, req, pick_id):
        from .kite import Kite
        from .orders import (OrderError, SellRequest, build_plan, build_sell_plan, create_draft, draft_text,
                             get_ltp, latest_approved_symbol, ordering_enabled)
        from .orders import PAPER_ONLY_MSG, real_orders_allowed
        if not real_orders_allowed():
            raise OrderError(PAPER_ONLY_MSG)
        if not ordering_enabled(conn):
            raise OrderError("ordering is switched off (/stop). Send /orderson to allow it.")
        kite = Kite(conn)  # raises if not logged in today
        if isinstance(req, SellRequest):
            held, avg, last = kite.holding(req.symbol)
            last = last or get_ltp(req.symbol)
            plan = build_sell_plan(req, held, avg, last, tick=kite.tick_size(req.symbol, last))
            existing = kite.active_sells(req.symbol)
            if existing:
                plan.notes.append("⚠️ already active sell trigger(s) on this stock: " + ", ".join(
                    f"GTT {g} ({q} @ ₹{p:,.2f})" for g, q, p in existing) + ". Delete in Kite if replacing.")
            oid = create_draft(conn, plan, None)
            return oid, draft_text(conn, oid)
        sym = req.symbol
        if sym is None and pick_id is not None:
            r = conn.execute("SELECT symbol FROM picks WHERE id=?", (pick_id,)).fetchone()
            sym = r["symbol"] if r else None
        if sym is None:
            last = latest_approved_symbol(conn, now_ist())
            if not last:
                raise OrderError("which stock? e.g. buy HAL ₹2500 (no approved pick today to default to)")
            sym, pick_id = last
        if pick_id is None:
            r = conn.execute("SELECT id FROM picks WHERE symbol=? AND status='approved' ORDER BY id DESC LIMIT 1",
                             (sym,)).fetchone()
            pick_id = r["id"] if r else None
        ltp = get_ltp(sym)
        from .orders import plan_sl_pct, wants_auto_target
        from .targets import suggest_target
        cfg = load_config()
        sl = plan_sl_pct(req, ltp, cfg)
        if req.no_tp or (cfg.kite.target_mode == "none" and req.tp_pct is None and req.tp_price is None):
            target = None
        elif wants_auto_target(req, cfg):
            target = suggest_target(sym, cfg, sl)
        else:
            fixed = req.tp_pct if req.tp_pct is not None else (
                (req.tp_price / ltp - 1) * 100 if req.tp_price is not None else cfg.kite.default_target_pct)
            target = suggest_target(sym, cfg, sl, fixed_pct=fixed)
        plan = build_plan(req, sym, ltp, cfg, tick=kite.tick_size(sym, ltp), target=target)
        oid = create_draft(conn, plan, pick_id)
        return oid, draft_text(conn, oid)

    async def start_order(msg, req, pick_id=None) -> None:
        from .kite import KiteError
        from .orders import OrderError
        try:
            oid, text = await with_conn(_draft, req, pick_id)
        except (OrderError, KiteError) as e:
            await msg.reply_html(f"Order not prepared: {esc(e)}")
            return
        await msg.reply_html(text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm & send to Kite", callback_data=f"ordok:{oid}"),
            InlineKeyboardButton("✖ Cancel", callback_data=f"ordno:{oid}")]]))

    def _confirm(conn, oid):
        from .kite import Kite
        from .orders import confirm
        return confirm(conn, oid, Kite(conn))

    def _cancel(conn, oid):
        from .kite import Kite
        from .orders import cancel_draft, cancel_order
        o = conn.execute("SELECT status FROM kite_orders WHERE id=?", (oid,)).fetchone()
        if o and o["status"] == "draft":
            return cancel_draft(conn, oid)
        return cancel_order(conn, oid, Kite(conn))

    async def on_order_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from .kite import KiteError
        from .orders import OrderRequest
        q = update.callback_query
        if not authorized(update):
            log.warning("ignored order callback from unauthorized chat")
            return
        action, _, sid = (q.data or "").partition(":")
        num = int(sid)
        if action == "ordnew":
            await q.answer("Preparing…")
            await start_order(q.message, OrderRequest(amount=load_config().kite.max_order_value), pick_id=num)
            return
        await q.answer("Working…" if action == "ordok" else "Cancelling…")
        try:
            result = await with_conn(_confirm if action == "ordok" else _cancel, num)
        except KiteError as e:
            result = f"❌ {e}"
        if re.match(r"Order #\d+ is already", result):  # double tap: keep the first result on screen
            return
        base = q.message.text_html if q.message and q.message.text else ""
        try:
            await q.edit_message_text(f"{base}\n\n{esc(result)}", parse_mode="HTML", reply_markup=None,
                                      disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await q.message.reply_html(esc(result))

    async def cmd_orders(update: Update, ctx) -> None:
        from .kite import Kite, KiteError
        from .orders import orders_text, sync

        def run(conn):
            msgs = []
            try:
                msgs = sync(conn, Kite(conn))
            except KiteError as e:
                msgs = [f"(not synced with Kite: {e})"]
            return msgs, orders_text(conn)
        msgs, text = await with_conn(run)
        await reply(update, "\n".join([esc(m) for m in msgs] + [text]))

    async def cmd_gtt(update: Update, ctx) -> None:
        from .kite import Kite, KiteError
        from .orders import gtt_list_text
        show_all = bool(ctx.args) and ctx.args[0].lower() == "all"

        def run(conn):
            return gtt_list_text(conn, Kite(conn).gtts(), show_all)
        try:
            await reply(update, await with_conn(run))
        except KiteError as e:
            await reply(update, f"❌ {esc(e)}")

    async def cmd_holdings(update: Update, ctx) -> None:
        from .kite import Kite, KiteError
        from .orders import holdings_text

        def run(conn):
            k = Kite(conn)
            return holdings_text(k.holdings(), k.gtts())
        try:
            await reply(update, await with_conn(run))
        except KiteError as e:
            await reply(update, f"❌ {esc(e)}")

    async def watch_loop(application) -> None:
        """Every 5 min: bot-order tracking + alerts for any GTT that fires/fails (works without launchd)."""
        from .orders import sync_tick
        while True:
            await asyncio.sleep(300)
            try:
                msgs: list[str] = []
                await with_conn(lambda c: sync_tick(c, load_config(), now_ist(), msgs.append))
                for m in msgs:
                    await application.bot.send_message(owner, esc(m), parse_mode="HTML",
                                                       disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                log.error("watch loop error: %s", redact(f"{type(e).__name__}: {e}"))

    async def post_init(application) -> None:
        application.create_task(watch_loop(application))

    scan_lock = asyncio.Lock()

    async def cmd_scan(update: Update, ctx) -> None:
        from .scan import run_scan
        if scan_lock.locked():
            await reply(update, "A scan is already running; results will arrive shortly.")
            return
        async with scan_lock:
            await reply(update, "🔎 Scanning prices, news, sector and global cues… (about a minute)")
            try:
                msgs = await with_conn(run_scan, load_config())
            except Exception as e:  # noqa: BLE001
                log.error("scan failed: %s", redact(repr(e)))
                await reply(update, f"❌ Scan failed: {esc(type(e).__name__)}. Details are in logs/bot.log.")
                return
            for text, buttons in msgs:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=cb) for lbl, cb in row]
                                               for row in buttons]) if buttons else None
                for i, ch in enumerate(split_message(text)):
                    last = i == len(split_message(text)) - 1
                    await update.effective_message.reply_html(ch, disable_web_page_preview=True,
                                                              reply_markup=markup if last else None)

    async def on_scan_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from .orders import OrderError, parse_order
        q = update.callback_query
        if not authorized(update):
            log.warning("ignored scan callback from unauthorized chat")
            return
        sym = (q.data or "").partition(":")[2]
        await q.answer("Preparing…")
        k = load_config().kite
        try:
            req = parse_order(f"buy {sym} ₹{k.max_order_value:.0f} @now sl {k.default_sl_pct:g}%")
        except OrderError as e:
            await q.message.reply_html(f"Order not prepared: {esc(e)}")
            return
        await start_order(q.message, req)

    async def cmd_oco(update: Update, ctx) -> None:
        from .kite import Kite, KiteError
        from .orders import PAPER_ONLY_MSG, OrderError, oco_preview, real_orders_allowed
        if not real_orders_allowed():
            await reply(update, esc(PAPER_ONLY_MSG))
            return
        args = ctx.args or []
        if not args or not args[0].isdigit():
            await reply(update, "Usage: <code>/oco 5</code> or <code>/oco 5 20%</code> (order numbers: /orders)")
            return
        pct = None
        if len(args) > 1:
            try:
                pct = float(args[1].rstrip("%"))
            except ValueError:
                await reply(update, "Target must be a percentage, e.g. <code>/oco 5 20%</code>")
                return
        try:
            text, pct = await with_conn(lambda c: oco_preview(c, int(args[0]), Kite(c), pct))
        except (OrderError, KiteError) as e:
            await reply(update, f"Not possible: {esc(e)}")
            return
        await update.effective_message.reply_html(text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"ocook:{int(args[0])}:{pct:g}"),
            InlineKeyboardButton("✖ Cancel", callback_data="ocono:0:0")]]))

    async def on_oco_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from .kite import Kite, KiteError
        from .orders import upgrade_to_oco
        q = update.callback_query
        if not authorized(update):
            log.warning("ignored oco callback from unauthorized chat")
            return
        action, oid, pct = (q.data or "").split(":")
        base = q.message.text_html if q.message and q.message.text else ""
        if action == "ocono":
            await q.answer("Cancelled")
            await q.edit_message_text(f"{base}\n\n✖ Cancelled, nothing changed.", parse_mode="HTML",
                                      reply_markup=None)
            return
        await q.answer("Working…")
        try:
            result = await with_conn(lambda c: upgrade_to_oco(c, int(oid), Kite(c), float(pct)))
        except KiteError as e:
            result = f"❌ {e}"
        await q.edit_message_text(f"{base}\n\n{esc(result)}", parse_mode="HTML", reply_markup=None)

    async def cmd_cancel(update: Update, ctx) -> None:
        from .kite import KiteError
        arg = (ctx.args or [""])[0].lstrip("#")
        if not arg.isdigit():
            await reply(update, "Usage: /cancel &lt;order #&gt; (see /orders)")
            return
        try:
            await reply(update, esc(await with_conn(_cancel, int(arg))))
        except KiteError as e:
            await reply(update, f"❌ {esc(e)}")

    async def cmd_stop(update: Update, ctx) -> None:
        from .orders import set_ordering
        n = await with_conn(set_ordering, False)
        await reply(update, "⛔ Kite ordering switched OFF" + (f" ({n} unconfirmed draft(s) voided)" if n else "")
                    + ". Triggers already in Kite stay there: use /orders and /cancel N to remove them.")

    async def cmd_orderson(update: Update, ctx) -> None:
        from .orders import PAPER_ONLY_MSG, real_orders_allowed, set_ordering
        if not real_orders_allowed():
            await reply(update, esc(PAPER_ONLY_MSG) + " Change it on the Mac to allow real orders again.")
            return
        await with_conn(set_ordering, True)
        await reply(update, "🟢 Kite ordering switched ON. Every order still needs your Confirm tap.")

    async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("bot error: %s", redact(repr(ctx.error)))

    app = Application.builder().token(s.telegram_bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("help", cmd_help, filters=only_owner))
    app.add_handler(CommandHandler("start", cmd_help, filters=only_owner))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=only_owner))
    app.add_handler(CommandHandler("status", cmd_status, filters=only_owner))
    app.add_handler(CommandHandler("pnl", cmd_pnl, filters=only_owner))
    app.add_handler(CommandHandler("pause", cmd_pause, filters=only_owner))
    app.add_handler(CommandHandler("resume", cmd_resume, filters=only_owner))
    app.add_handler(CommandHandler("kitelogin", cmd_kitelogin, filters=only_owner))
    app.add_handler(CommandHandler("token", cmd_token, filters=only_owner))
    app.add_handler(CommandHandler("kitetest", cmd_kitetest, filters=only_owner))
    app.add_handler(CommandHandler("orders", cmd_orders, filters=only_owner))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=only_owner))
    app.add_handler(CommandHandler("oco", cmd_oco, filters=only_owner))
    app.add_handler(CommandHandler("gtt", cmd_gtt, filters=only_owner))
    app.add_handler(CommandHandler("holdings", cmd_holdings, filters=only_owner))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=only_owner))
    app.add_handler(CommandHandler("orderson", cmd_orderson, filters=only_owner))
    app.add_handler(CallbackQueryHandler(on_order_button, pattern=r"^(ordok|ordno|ordnew):\d+$"))
    app.add_handler(CallbackQueryHandler(on_oco_button, pattern=r"^(ocook|ocono):\d+:\d+(\.\d+)?$"))
    app.add_handler(CallbackQueryHandler(on_scan_buy, pattern=r"^ordsym:[A-Z0-9&\-]{1,20}$"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("bot polling started")
    # Pending updates are kept (not dropped) so taps made while the Mac slept get processed.
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
