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
    "/status – open paper positions + today's picks\n"
    "/pnl – paper P&amp;L vs Nifty, hit rates\n"
    "/pause – stop sending picks\n"
    "/resume – resume picks\n"
    "/help – this message\n"
    "/kitelogin – daily Kite login link\n"
    "/kitetest – place + delete a harmless test GTT\n\n"
    "Log a real fill: <code>bought HAL 1 4210.5</code> or <code>sold HAL 1 4400</code>"
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
    from telegram import Update
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
                    await q.message.reply_html(
                        f"Logged for paper trading. If you buy for real, reply:\n"
                        f"<code>bought {esc(sym)} &lt;qty&gt; &lt;price&gt;</code>")
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

    async def cmd_kitetest(update: Update, ctx) -> None:
        import asyncio

        from .kite import KiteError, test_gtt
        conn = connect()
        try:
            steps = await asyncio.to_thread(test_gtt, conn, "PNB")
            await reply(update, "\n".join(esc(s) for s in steps))
        except KiteError as e:
            await reply(update, f"❌ GTT test failed: {esc(e)}")
        finally:
            conn.close()

    async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("bot error: %s", redact(repr(ctx.error)))

    app = Application.builder().token(s.telegram_bot_token).build()
    app.add_handler(CommandHandler("help", cmd_help, filters=only_owner))
    app.add_handler(CommandHandler("start", cmd_help, filters=only_owner))
    app.add_handler(CommandHandler("status", cmd_status, filters=only_owner))
    app.add_handler(CommandHandler("pnl", cmd_pnl, filters=only_owner))
    app.add_handler(CommandHandler("pause", cmd_pause, filters=only_owner))
    app.add_handler(CommandHandler("resume", cmd_resume, filters=only_owner))
    app.add_handler(CommandHandler("kitelogin", cmd_kitelogin, filters=only_owner))
    app.add_handler(CommandHandler("token", cmd_token, filters=only_owner))
    app.add_handler(CommandHandler("kitetest", cmd_kitetest, filters=only_owner))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("bot polling started")
    # Pending updates are kept (not dropped) so taps made while the Mac slept get processed.
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
