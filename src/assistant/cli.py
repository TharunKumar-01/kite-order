"""Entry points: python -m assistant.cli <command>"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

from .config import DB_PATH, ROOT, load_config, load_secrets
from .db import connect, job_mark
from .util import now_ist, setup_logging


def cmd_init_db(args) -> int:
    conn = connect()
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    print(f"Database ready at {DB_PATH} (schema v{v})")
    return 0


def cmd_premarket(args) -> int:
    from .jobs import premarket_job
    cfg, conn = load_config(), connect()
    t0 = time.time()
    today = now_ist().date().isoformat()
    if not args.dry_run:
        job_mark(conn, "premarket", today, "running")
    status = premarket_job(conn, cfg, dry_run=args.dry_run, force=args.force or args.dry_run)
    if not args.dry_run:
        job_mark(conn, "premarket", today, status)
    print(f"premarket: {status} in {time.time() - t0:.1f}s")
    return 0


def cmd_scan(args) -> int:
    from .scan import run_scan
    from .telegram_bot import TelegramClient, strip_html
    msgs = run_scan(connect(), load_config())
    tg = None if args.print else TelegramClient()
    for text, buttons in msgs:
        if tg:
            tg.send(text, buttons)
        else:
            print(strip_html(text), end="\n\n")
    return 0


def cmd_evening(args) -> int:
    from .jobs import evening_job
    cfg, conn = load_config(), connect()
    d = date.fromisoformat(args.date) if args.date else now_ist().date()
    status = evening_job(conn, cfg, d, dry_run=args.dry_run, retry_delays=(0,))
    if not args.dry_run and status in ("done", "skipped"):
        job_mark(conn, "evening", d.isoformat(), status)
    print(f"evening {d}: {status}")
    return 0


def cmd_tick(args) -> int:
    from .scheduler import run_tick
    res = run_tick(connect(), load_config())
    if res:
        print(res)
    return 0


def cmd_bot(args) -> int:
    from .telegram_bot import run_bot
    run_bot()
    return 0


def cmd_backtest(args) -> int:
    from .jobs import backtest_job
    backtest_job(connect(), load_config(), years=args.years, refresh=not args.no_refresh)
    return 0


def cmd_report(args) -> int:
    from . import reports
    from .telegram_bot import strip_html
    conn = connect()
    text = reports.weekly_text(conn) if args.weekly else reports.pnl_text(conn) + "\n\n" + reports.status_text(conn)
    print(strip_html(text))
    return 0


def cmd_kite_login(args) -> int:
    """Open the Kite login page and capture the redirect on 127.0.0.1 (or paste the URL)."""
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from .kite import exchange_token, login_url, parse_request_token
    conn, cfg = connect(), load_config()
    url = login_url()
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            tok = parse_request_token(self.path)
            if tok:
                got["token"] = tok
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Kite login received. You can close this tab." if tok else "No request_token").encode())

        def log_message(self, *a):  # silence (URL contains the request token)
            pass

    srv = HTTPServer(("127.0.0.1", cfg.kite.login_port), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    print(f"Opening Kite login. Redirect URL must be http://127.0.0.1:{cfg.kite.login_port}/kite")
    print("If the browser doesn't open, visit:", url)
    webbrowser.open(url)
    for _ in range(300):
        if "token" in got:
            break
        time.sleep(1)
    srv.shutdown()
    tok = got.get("token") or parse_request_token(input("Paste the redirect URL (or press Enter to quit): "))
    if not tok:
        print("No token; not logged in.")
        return 1
    who = exchange_token(conn, tok)
    print(f"Logged in to Kite as {who.get('user_id')} for today.")
    return 0


def cmd_kite_test_gtt(args) -> int:
    from .kite import KiteError, test_gtt
    try:
        for s in test_gtt(connect(), args.symbol.upper()):
            print(s)
        return 0
    except KiteError as e:
        print(f"GTT test FAILED: {e}")
        if "ip" in str(e).lower():
            print("Zerodha rejected the request because of the IP address: GTT also needs a static IP.")
        return 1


def cmd_selftest(args) -> int:
    """Checks config, DB, universe, data sources and Telegram. Sends nothing."""
    from tabulate import tabulate
    rows = []

    def check(name, fn):
        try:
            rows.append([name, "OK", fn() or ""])
        except Exception as e:  # noqa: BLE001
            from .util import redact
            rows.append([name, "FAIL", redact(f"{type(e).__name__}: {e}")[:120]])

    cfg = load_config()
    check("config.yaml", lambda: f"tz={cfg.timezone}, capital ₹{cfg.capital_total:,.0f}")
    check("IST clock", lambda: now_ist().strftime("%Y-%m-%d %H:%M %Z"))
    check("database", lambda: f"{DB_PATH.relative_to(ROOT)} v{connect().execute('SELECT version FROM schema_version').fetchone()[0]}")

    def uni():
        from .universe import load_universe
        u = load_universe()
        wl = sum(1 for s in u if s.group == "watchlist")
        return f"{len(u)} stocks ({wl} watchlist)"
    check("universe.csv", uni)

    def hol():
        from .universe import is_trading_day, load_holidays
        h = load_holidays()
        return f"{len(h)} holidays; today trading day: {is_trading_day(now_ist().date())}"
    check("holidays.csv", hol)

    def secrets():
        s = load_secrets()
        miss = [k for k, v in (("TELEGRAM_BOT_TOKEN", s.telegram_bot_token), ("TELEGRAM_CHAT_ID", s.telegram_chat_id)) if not v]
        if miss:
            raise RuntimeError("missing in .env: " + ", ".join(miss))
        return "telegram secrets present"
    check(".env", secrets)

    def yf():
        from .market_data import download_daily
        d = download_daily([cfg.market.benchmark, "HAL.NS"], period="5d")
        if cfg.market.benchmark not in d:
            raise RuntimeError("no Nifty data")
        b = d[cfg.market.benchmark]
        return f"Nifty last bar {b.index[-1].date()} close {b['close'].iloc[-1]:,.0f}"
    check("yfinance", yf)

    def news():
        from datetime import timedelta
        from .news import fetch_query
        from .util import now_utc
        n = fetch_query("Nifty", now_utc() - timedelta(hours=48))
        if not n:
            raise RuntimeError("no headlines returned")
        return f"{len(n)} headlines"
    check("Google News RSS", news)

    def tg():
        from .telegram_bot import TelegramClient
        me = TelegramClient().get_me()
        if not me:
            raise RuntimeError("getMe failed (check token / network)")
        return f"bot @{me.get('username')}"
    check("Telegram", tg)

    def no_orders():
        import pathlib
        bad = [p.name for p in pathlib.Path(__file__).parent.glob("*.py")
               if any(k in p.read_text() for k in ("kiteconnect", "place_order", "orders/regular"))
               and p.name != "cli.py"]
        if bad:
            raise RuntimeError(f"regular-order code found in {bad}")
        return "no regular-order code (GTT only, capped)"
    check("safety", no_orders)

    def llm():
        if not cfg.llm.enabled:
            return "disabled (keyword summarizer)"
        return "enabled, key present" if load_secrets().anthropic_api_key else "enabled but ANTHROPIC_API_KEY missing"
    check("LLM summarizer", llm)

    print(tabulate(rows, ["check", "result", "detail"], tablefmt="simple"))
    return 1 if any(r[1] == "FAIL" for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="assistant", description="Trading research assistant (learning tool, not advice)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("premarket", help="screen + send picks")
    s.add_argument("--dry-run", action="store_true", help="print instead of sending; store nothing")
    s.add_argument("--force", action="store_true", help="ignore trading-day and cutoff checks")
    s.set_defaults(fn=cmd_premarket)
    s = sub.add_parser("scan", help="run the screen now + check Kite holdings; sends to Telegram")
    s.add_argument("--print", action="store_true", help="print to the console instead of sending")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("evening", help="score picks, update paper portfolio, send summary")
    s.add_argument("--date", help="session date YYYY-MM-DD (default today IST)")
    s.add_argument("--dry-run", action="store_true", help="print the summary instead of sending (DB is still updated)")
    s.set_defaults(fn=cmd_evening)
    sub.add_parser("tick", help="run due jobs (called by launchd every 5 min)").set_defaults(fn=cmd_tick)
    sub.add_parser("bot", help="Telegram approval listener").set_defaults(fn=cmd_bot)
    s = sub.add_parser("backtest", help="replay price rules over history")
    s.add_argument("--years", type=int, default=5)
    s.add_argument("--no-refresh", action="store_true", help="use cached prices only")
    s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("report", help="print paper P&L / status (or --weekly)")
    s.add_argument("--weekly", action="store_true")
    s.set_defaults(fn=cmd_report)
    sub.add_parser("init-db", help="create/migrate the database").set_defaults(fn=cmd_init_db)
    sub.add_parser("kite-login", help="daily Kite Connect login").set_defaults(fn=cmd_kite_login)
    s = sub.add_parser("kite-test-gtt", help="place + delete a harmless 1-share GTT far below market")
    s.add_argument("--symbol", default="PNB")
    s.set_defaults(fn=cmd_kite_test_gtt)
    sub.add_parser("selftest", help="check config, data sources and Telegram").set_defaults(fn=cmd_selftest)
    args = p.parse_args(argv)
    setup_logging({"tick": "tick", "bot": "bot"}.get(args.cmd, "assistant"), args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
