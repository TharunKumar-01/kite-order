"""Minimal Kite Connect v3 client (raw HTTPS, no SDK) for login + GTT triggers + read-only calls.

Deliberately NOT implemented: regular (non-GTT) order placement. Since 1 Apr 2026
Zerodha only accepts those from a registered static IP. Only GTT endpoints write anything, and
every GTT is CNC + LIMIT on NSE with a hard value cap (enforced here, not just in the UI)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

import requests

from .config import load_config, load_secrets
from .db import kv_get, kv_set
from .util import iso_utc, now_ist, redact

log = logging.getLogger(__name__)
API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
TOKEN_RE = re.compile(r"request_token=([A-Za-z0-9]+)")


class KiteError(RuntimeError):
    pass


def tick_round(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def login_url() -> str:
    s = load_secrets()
    if not s.kite_api_key:
        raise KiteError("KITE_API_KEY missing in .env")
    return LOGIN.format(api_key=s.kite_api_key)


def parse_request_token(text: str) -> str | None:
    """Accepts the full redirect URL or the bare token."""
    m = TOKEN_RE.search(text or "")
    if m:
        return m.group(1)
    t = (text or "").strip()
    return t if re.fullmatch(r"[A-Za-z0-9]{20,64}", t) else None


def exchange_token(conn: sqlite3.Connection, request_token: str) -> dict:
    s = load_secrets()
    if not (s.kite_api_key and s.kite_api_secret):
        raise KiteError("KITE_API_KEY / KITE_API_SECRET missing in .env")
    checksum = hashlib.sha256((s.kite_api_key + request_token + s.kite_api_secret).encode()).hexdigest()
    r = requests.post(f"{API}/session/token", timeout=15, headers={"X-Kite-Version": "3"},
                      data={"api_key": s.kite_api_key, "request_token": request_token, "checksum": checksum})
    data = _unwrap(r)
    kv_set(conn, "kite_access_token", data["access_token"])
    kv_set(conn, "kite_token_date", now_ist().date().isoformat())
    kv_set(conn, "kite_login_at", iso_utc())
    return {"user_id": data.get("user_id"), "user_name": data.get("user_name")}


def _unwrap(r: requests.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise KiteError(f"HTTP {r.status_code} (non-JSON response)") from None
    if body.get("status") != "success":
        raise KiteError(f"{body.get('error_type', 'Error')}: {body.get('message', r.status_code)}")
    return body.get("data") or {}


@dataclass
class GTTOrder:
    symbol: str
    side: str          # BUY / SELL
    qty: int
    trigger: float
    limit: float
    last_price: float

    def validate(self) -> None:
        cfg = load_config()
        if self.side not in ("BUY", "SELL"):
            raise KiteError("side must be BUY or SELL")
        if self.qty < 1 or self.trigger <= 0 or self.limit <= 0 or self.last_price <= 0:
            raise KiteError("quantity and prices must be positive")
        if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", self.symbol):
            raise KiteError("bad symbol")
        cap = cfg.kite.max_order_value
        if self.side == "BUY" and self.qty * self.limit > cap:
            raise KiteError(f"order value ₹{self.qty * self.limit:,.0f} exceeds the cap ₹{cap:,.0f} (config kite.max_order_value)")


class Kite:
    def __init__(self, conn: sqlite3.Connection):
        s = load_secrets()
        if not s.kite_api_key:
            raise KiteError("KITE_API_KEY missing in .env")
        token = kv_get(conn, "kite_access_token")
        if not token or kv_get(conn, "kite_token_date") != now_ist().date().isoformat():
            raise KiteError("Not logged in to Kite today. Send /kitelogin in Telegram or run `cli kite-login`.")
        self.auth = f"token {s.kite_api_key}:{token}"

    def _req(self, method: str, path: str, data: dict | None = None) -> dict:
        try:
            r = requests.request(method, API + path, data=data, timeout=15,
                                 headers={"X-Kite-Version": "3", "Authorization": self.auth})
        except requests.RequestException as e:
            raise KiteError(redact(f"network error: {type(e).__name__}")) from None
        return _unwrap(r)

    # ---- read-only ----
    def profile(self) -> dict:
        return self._req("GET", "/user/profile")

    def holdings(self) -> list[dict]:
        return self._req("GET", "/portfolio/holdings")

    def gtts(self) -> list[dict]:
        return self._req("GET", "/gtt/triggers")

    # ---- GTT (the only write path) ----
    def place_gtt(self, o: GTTOrder) -> int:
        o.validate()
        condition = {"exchange": "NSE", "tradingsymbol": o.symbol, "trigger_values": [tick_round(o.trigger)],
                     "last_price": o.last_price}
        orders = [{"exchange": "NSE", "tradingsymbol": o.symbol, "transaction_type": o.side, "quantity": o.qty,
                   "order_type": "LIMIT", "product": "CNC", "price": tick_round(o.limit)}]
        data = self._req("POST", "/gtt/triggers", {"type": "single", "condition": json.dumps(condition),
                                                    "orders": json.dumps(orders)})
        log.info("GTT placed: %s %s x%d trigger %.2f limit %.2f -> id %s", o.side, o.symbol, o.qty, o.trigger,
                 o.limit, data.get("trigger_id"))
        return int(data["trigger_id"])

    def delete_gtt(self, trigger_id: int) -> None:
        self._req("DELETE", f"/gtt/triggers/{int(trigger_id)}")
        log.info("GTT %s deleted", trigger_id)


def test_gtt(conn: sqlite3.Connection, symbol: str = "PNB") -> list[str]:
    """Place a 1-share BUY GTT ~20% below market (cannot trigger), then delete it.
    Proves whether Zerodha accepts GTT writes from this machine without a static IP."""
    from .market_data import download_daily
    steps = []
    k = Kite(conn)
    prof = k.profile()
    steps.append(f"logged in as {prof.get('user_id')}")
    d = download_daily([f"{symbol}.NS"], period="5d").get(f"{symbol}.NS")
    if d is None or d.empty:
        raise KiteError(f"no price for {symbol}")
    ltp = float(d["close"].iloc[-1])
    trig = tick_round(ltp * 0.80)
    steps.append(f"{symbol} last ₹{ltp:,.2f}; test BUY 1 trigger/limit ₹{trig:,.2f} (20% below, won't fire)")
    tid = k.place_gtt(GTTOrder(symbol, "BUY", 1, trig, trig, round(ltp, 2)))
    steps.append(f"GTT accepted, id {tid}  ✅ (no static IP needed for GTT)")
    try:
        k.delete_gtt(tid)
        still = [g for g in k.gtts() if int(g.get("id", 0)) == tid and g.get("status") == "active"]
        steps.append("GTT deleted ✅" if not still else f"⚠️ GTT {tid} still active, delete it in Kite > Orders > GTT")
    except KiteError as e:
        steps.append(f"⚠️ delete failed ({e}); delete GTT {tid} manually in Kite > Orders > GTT")
    return steps
