"""premarket_job, evening_job, weekly_job, backtest_job."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import market_data as md
from . import news as newsmod
from . import reports
from .config import REPORT_DIR, Config
from .db import job_mark, kv_get
from .paper import Entry, horizon_returns, max_drawdown, save_simulation, simulate
from .signals import (Candidate, apply_tailwind, build_candidates, indicator_frame, sector_moves, shortlist)
from .summarizer import get_summarizer
from .telegram_bot import FOOTER, TelegramClient, esc
from .universe import Stock, is_trading_day, load_universe, previous_trading_day, yf_ticker
from .util import iso_utc, now_ist, now_utc

log = logging.getLogger(__name__)


# =====================================================================================
# Pre-market
# =====================================================================================
def _sector_tickers(cfg: Config) -> list[str]:
    return sorted(set(cfg.market.sector_index.values()))


def compute_picks(conn: sqlite3.Connection, cfg: Config, today: date) -> dict:
    """Everything up to (not including) sending/storing. Returns a dict for formatting."""
    t0 = time.time()
    universe = load_universe()
    bench_t = cfg.market.benchmark
    tickers = [s.yf for s in universe] + [bench_t] + _sector_tickers(cfg)
    prices = md.get_prices(conn, tickers, cfg.market.history_days)
    missing = []
    bench = prices.get(bench_t)
    asof = pd.Timestamp(today)
    cands = build_candidates(universe, prices, bench, cfg, asof)
    log.info("evaluated %d/%d stocks in %.1fs", len(cands), len(universe), time.time() - t0)

    cues = md.global_cues(cfg)
    if not cues:
        missing.append("global cues unavailable")
    smoves = sector_moves(cands, prices, cfg, asof)
    apply_tailwind(cands, smoves, cues, cfg)

    latest = max((c.signal_date for c in cands), default=None)
    stale_n = sum(1 for c in cands if c.signal_date != latest)
    if stale_n:
        missing.append(f"{stale_n} stocks skipped: their latest daily bar is older than {latest.date()}")
    eligible = [c for c in cands if c.qualifies and c.liquid and c.last_close <= cfg.pick_price_cap
                and c.signal_date == latest]
    eligible.sort(key=lambda c: -(c.price_score + cfg.weights.sector_tailwind * np.clip(c.tailwind, -2, 2)))
    pool = eligible[: cfg.signals.news_candidates]

    # ---- news ----
    tags = {t.lower() for c in pool for t in c.stock.tags}
    sector_q = {f"sector:{k}": q for k, q in cfg.news.sector_queries.items() if k.lower() in tags}
    stock_q = {f"stock:{c.stock.symbol}": f'"{c.stock.name}" stock NSE' for c in pool}
    fetched = newsmod.fetch_many({**sector_q, **stock_q}, cfg) if (sector_q or stock_q) else {}
    if (sector_q or stock_q) and not any(fetched.values()):
        missing.append("news unavailable")
    sector_scores = {k.split(":", 1)[1].lower(): newsmod.net_score(v, cfg)
                     for k, v in fetched.items() if k.startswith("sector:")}
    sector_heads = [h for k, v in fetched.items() if k.startswith("sector:") for h in v]
    for c in pool:
        own = fetched.get(f"stock:{c.stock.symbol}", [])
        st = c.stock
        own = [h for h in own if newsmod.mentions(h, st.name, st.symbol, st.aliases)]
        extra = [h for h in sector_heads if newsmod.mentions(h, st.name, st.symbol, st.aliases)]
        c.headlines = newsmod.dedupe(own + extra)
        c.news_score = float(newsmod.net_score(c.headlines, cfg))
        sv = [sector_scores[t.lower()] for t in c.stock.tags if t.lower() in sector_scores]
        c.sector_news = float(np.mean(sv)) if sv else 0.0

    # ---- public Telegram channels (cached by the tick; refreshed here with a short budget) ----
    channel_hits: dict[str, list] = {}
    if cfg.channels.enabled and cfg.channels.names:
        from . import channel as chmod
        try:
            chmod.poll(conn, cfg, budget_s=60)
        except Exception as e:  # noqa: BLE001
            log.warning("channel refresh failed: %s", e)
        posts = chmod.cached_posts(conn, now_utc() - timedelta(hours=cfg.channels.lookback_hours))
        if not posts:
            missing.append("no recent posts from " + ", ".join("@" + n for n in cfg.channels.names))
        channel_hits = chmod.stock_mentions(posts, universe)
        for c in pool:
            extra = channel_hits.get(c.stock.symbol, [])
            if extra:
                c.headlines = newsmod.dedupe(c.headlines + extra)
                c.news_score = float(newsmod.net_score(c.headlines, cfg))

    picks = shortlist(pool, cfg)
    summ = get_summarizer(cfg)
    for c in picks:
        c.summary = summ.summarize(c.stock, c.headlines[: cfg.news.max_headlines_per_stock], c.components)
        c.summarizer = getattr(summ, "last_used", summ.name)

    stale = None
    if bench is not None and not bench.empty:
        last_bar = bench[bench.index < asof].index[-1].date()
        expected = previous_trading_day(today, conn)
        if last_bar < expected:
            stale = f"latest Nifty bar is {last_bar}, expected {expected}; data may be stale"
            missing.append(stale)
    log.info("premarket compute finished in %.1fs: %d picks", time.time() - t0, len(picks))
    return {"picks": picks, "cues": cues, "bench": bench, "missing": missing, "asof": asof,
            "n_eval": len(cands), "n_eligible": len(eligible), "cands": cands, "channel_hits": channel_hits}


def header_text(result: dict, today: date) -> str:
    lines = [f"<b>📈 Pre-market screen – {today.strftime('%a %d %b %Y')}</b>"]
    bench = result["bench"]
    if bench is not None and len(bench[bench.index < result["asof"]]) >= 2:
        b = bench[bench.index < result["asof"]]
        chg = (b["close"].iloc[-1] / b["close"].iloc[-2] - 1) * 100
        lines.append(f"Nifty prior close {b['raw_close'].iloc[-1]:,.0f} ({chg:+.2f}%)")
    if result["cues"]:
        lines.append("Overnight: " + " | ".join(f"{esc(c['name'])} {c['change_pct']:+.2f}%" for c in result["cues"]))
    for m in result["missing"]:
        lines.append(f"⚠️ {esc(m)}")
    lines.append(f"Screened {result['n_eval']} stocks; {result['n_eligible']} passed rules/affordability/liquidity.")
    hits = result.get("channel_hits") or {}
    if hits:
        cfg = _cfg()
        tags = []
        for sym, hs in sorted(hits.items(), key=lambda kv: -len(kv[1]))[:12]:
            sc = newsmod.net_score(hs, cfg)
            tags.append(f"{esc(sym)}{'🟢' if sc > 0 else '🔴' if sc < 0 else ''}")
        lines.append("📣 In the Telegram channel(s) since yesterday: " + ", ".join(tags)
                     + " <i>(🟢/🔴 = positive/negative words; untrusted, not checked)</i>")
    return "\n".join(lines)


def pick_text(i: int, c: Candidate, cfg: Config) -> str:
    comp = c.components
    tick = lambda k: "✓" if comp.get(k) == 1 else "✗"  # noqa: E731
    rs = comp.get("rel_strength")
    extra = [t for t in c.stock.themes if t.lower() not in ("watchlist", c.stock.sector.lower())]
    theme = c.stock.sector + (f", {', '.join(extra)}" if extra else "")
    wl = " [watchlist]" if c.stock.group == "watchlist" else ""
    lines = [
        f"<b>{i}) {esc(c.stock.symbol)}</b>  ({esc(theme)}){wl}   ₹{c.last_close:,.2f} close",
        f"Score {c.total:.1f} | Trend {tick('trend')} Breakout {tick('breakout')} Vol {tick('volume')} "
        f"RSI {comp.get('rsi') or 0:.0f} | RS {rs:+.1f}% vs Nifty" if rs is not None else
        f"Score {c.total:.1f}",
        f"News {c.news_score:+.0f} | Sector/cues {comp.get('sector_tailwind', 0):+.2f}%",
        f"Why: {esc(getattr(c, 'summary', ''))}",
    ]
    hl = c.headlines[: cfg.news.max_headlines_per_stock]
    if hl:
        lines.append("Headlines:")
        for h in hl:
            link = f' <a href="{esc(h.link)}">link</a>' if h.link else ""
            lines.append(f"• {esc(h.title)} – <i>{esc(h.source)}</i>{link}")
    lines.append(f"Entry plan: limit order near 09:45; paper stop -{cfg.paper.stop_loss_pct:g}%; "
                 f"{cfg.paper.hold_days}-day hold")
    return "\n".join(lines)


def premarket_job(conn: sqlite3.Connection, cfg: Config, dry_run: bool = False, force: bool = False,
                  now: datetime | None = None) -> str:
    now = now or now_ist()
    today = now.date()
    if not force and not is_trading_day(today, conn):
        log.info("not a trading day: %s", today)
        return "skipped"
    if not dry_run and kv_get(conn, "paused") == "1":
        log.info("paused; not sending picks")
        return "paused"
    if not dry_run and conn.execute("SELECT 1 FROM picks WHERE run_date=?", (today.isoformat(),)).fetchone():
        log.info("picks already stored for %s", today)
        return "done"
    tg = TelegramClient(dry_run=dry_run)
    result = compute_picks(conn, cfg, today)
    cutoff = cfg.scheduler.t(cfg.scheduler.premarket_cutoff)
    if not dry_run and not force and now_ist().time() >= cutoff:
        tg.send(f"⏰ Missed today's pre-market window (picks were ready after {cfg.scheduler.premarket_cutoff} IST). "
                "No picks today.")
        return "missed"

    tg.send(header_text(result, today))
    picks: list[Candidate] = result["picks"]
    if not picks:
        tg.send("No picks today: nothing passed the rules, affordability, liquidity and score threshold.\n" + FOOTER)
        return "done"
    for i, c in enumerate(picks, 1):
        pick_id = None if dry_run else store_pick(conn, today, i, c)
        buttons = [[("Approve", f"approve:{pick_id or 0}"), ("Reject", f"reject:{pick_id or 0}")]]
        mid = tg.send(pick_text(i, c, cfg), buttons)
        if pick_id and mid:
            conn.execute("UPDATE picks SET chat_id=?, message_id=? WHERE id=?", (tg.chat_id, mid, pick_id))
            conn.commit()
    tg.send(FOOTER)
    return "done"


def store_pick(conn: sqlite3.Connection, today: date, rank: int, c: Candidate) -> int:
    cur = conn.execute(
        """INSERT INTO picks(run_date,signal_date,symbol,name,sector,themes,rank,total_score,last_close,summary,
           summarizer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (today.isoformat(), c.signal_date.date().isoformat(), c.stock.symbol, c.stock.name, c.stock.sector,
         ";".join(c.stock.themes), rank, c.total, c.last_close, getattr(c, "summary", ""),
         getattr(c, "summarizer", "keyword"), iso_utc()))
    pid = cur.lastrowid
    conn.executemany("INSERT INTO pick_scores(pick_id,component,value) VALUES(?,?,?)",
                     [(pid, k, v) for k, v in c.components.items()])
    for h in c.headlines:
        conn.execute("INSERT INTO headlines(pick_id,title,source,link,published_at,score) VALUES(?,?,?,?,?,?)",
                     (pid, h.title, h.source, h.link, h.published.isoformat(),
                      newsmod.headline_score(h.title, _cfg())))
    conn.commit()
    return pid


def _cfg() -> Config:
    from .config import load_config
    return load_config()


def missed_notice(cfg: Config) -> None:
    TelegramClient().send("⏰ Missed today's pre-market run (the Mac was asleep or off). No picks today; "
                          "stale picks are never sent.")


def expire_pending(conn: sqlite3.Connection, run_date: date) -> int:
    from .db import record_decision
    rows = conn.execute("SELECT id FROM picks WHERE run_date<=? AND status='pending'",
                        (run_date.isoformat(),)).fetchall()
    return sum(record_decision(conn, r["id"], "expired") for r in rows)


# =====================================================================================
# Evening
# =====================================================================================
def evening_job(conn: sqlite3.Connection, cfg: Config, run_date: date, dry_run: bool = False,
                retry_delays: tuple[int, ...] = (0, 30, 90)) -> str:
    if not is_trading_day(run_date, conn):
        return "skipped"
    bench_t = cfg.market.benchmark
    since = run_date - timedelta(days=max(45, int(cfg.paper.hold_days * 1.6) + 10))  # covers the tracker window
    pick_rows = conn.execute("SELECT * FROM picks WHERE run_date>=? AND run_date<=? ORDER BY run_date, rank",
                             (since.isoformat(), run_date.isoformat())).fetchall()
    all_approved = conn.execute("SELECT * FROM picks WHERE status='approved' AND run_date<=?",
                                (run_date.isoformat(),)).fetchall()
    syms = {yf_ticker(r["symbol"]) for r in [*pick_rows, *all_approved]}
    tickers = sorted(syms | {bench_t} | set(_sector_tickers(cfg)))

    have = False
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        md.update_cache(conn, tickers, cfg.market.history_days)
        b = md.load_prices(conn, [bench_t], run_date - timedelta(days=10)).get(bench_t)
        if b is not None and pd.Timestamp(run_date) in b.index:
            have = True
            break
    if not have:
        return "waiting"

    first = min([date.fromisoformat(r["run_date"]) for r in all_approved] + [since]) - timedelta(days=10)
    prices = md.load_prices(conn, tickers, first)
    bench = prices[bench_t]
    bench = bench[bench.index <= pd.Timestamp(run_date)]
    prices = {k: v[v.index <= pd.Timestamp(run_date)] for k, v in prices.items()}
    D = pd.Timestamp(run_date)

    # today's picks: prev close, open, 09:45, close
    todays = [r for r in pick_rows if r["run_date"] == run_date.isoformat()]
    intraday = md.intraday_15m([yf_ticker(r["symbol"]) for r in todays]) if todays else {}
    for r in todays:
        df = prices.get(yf_ticker(r["symbol"]))
        if df is None or D not in df.index:
            continue
        f = df.at[D, "close"] / df.at[D, "raw_close"] if df.at[D, "raw_close"] else 1.0
        prev = df[df.index < D]
        p0945 = md.price_at(intraday.get(yf_ticker(r["symbol"])), run_date, "09:45", cfg.tz)
        conn.execute("UPDATE picks SET prev_close=?, open_price=?, price_0945=?, close_d0=? WHERE id=?",
                     (float(prev["raw_close"].iloc[-1]) if len(prev) else None, float(df.at[D, "open"] / f),
                      p0945, float(df.at[D, "raw_close"]), r["id"]))
    conn.commit()

    # outcomes for every pick (signal quality)
    sector_idx = cfg.market.sector_index
    for r in conn.execute("SELECT * FROM picks WHERE run_date>=? AND run_date<=?",
                          (since.isoformat(), run_date.isoformat())).fetchall():
        df = prices.get(yf_ticker(r["symbol"]))
        sec = prices.get(sector_idx.get(r["sector"], ""), None)
        hr = horizon_returns(df, pd.Timestamp(r["run_date"]), bench, sec, r["price_0945"])
        for h, v in hr.items():
            conn.execute(
                """INSERT INTO pick_outcomes(pick_id,horizon,end_date,ret,nifty_ret,sector_ret,excess_nifty,
                   excess_sector,ret_from_0945) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pick_id,horizon) DO UPDATE SET end_date=excluded.end_date, ret=excluded.ret,
                   nifty_ret=excluded.nifty_ret, sector_ret=excluded.sector_ret, excess_nifty=excluded.excess_nifty,
                   excess_sector=excluded.excess_sector, ret_from_0945=excluded.ret_from_0945""",
                (r["id"], h, v["end_date"].date().isoformat(), v["ret"], v["nifty_ret"], v["sector_ret"],
                 v["excess_nifty"], v["excess_sector"], v["ret_from_0945"]))
    conn.commit()

    # paper portfolio (recomputed from scratch, idempotent)
    entries = [Entry(yf_ticker(r["symbol"]), pd.Timestamp(r["run_date"]), r["rank"], r["id"], r["sector"])
               for r in all_approved]
    sim = simulate(entries, prices, bench.index, cfg, bench)
    save_simulation(conn, sim)

    nifty_ret = float((bench["close"].iloc[-1] / bench["close"].iloc[-2] - 1) * 100) if len(bench) >= 2 else None
    tracker_block = None
    if cfg.tracker.enabled:
        from . import tracker
        tracked = tracker.track(conn, cfg, prices, bench, since, run_date)
        tracker_block = tracker.tracker_text(tracked, cfg, run_date,
                                             tracker.negative_news(tracked, cfg) if not dry_run else None,
                                             cfg.tracker.detail_limit)
    text = evening_text(conn, cfg, run_date, todays, nifty_ret, sim, tracker_block)
    paper_eq = float(sim.equity.iloc[-1]) if not sim.equity.empty else cfg.capital_total
    nifty_eq = float(sim.nifty_equity.iloc[-1]) if not sim.nifty_equity.empty else None
    conn.execute("""INSERT INTO daily_summary(date,text,nifty_ret,paper_equity,nifty_equity,created_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET text=excluded.text,
                    nifty_ret=excluded.nifty_ret, paper_equity=excluded.paper_equity,
                    nifty_equity=excluded.nifty_equity, created_at=excluded.created_at""",
                 (run_date.isoformat(), text, nifty_ret, paper_eq, nifty_eq, iso_utc()))
    conn.commit()
    TelegramClient(dry_run=dry_run).send(text)
    return "done"


def evening_text(conn, cfg: Config, run_date: date, todays, nifty_ret, sim, tracker_block: str | None = None) -> str:
    lines = [f"<b>🌆 Evening summary – {run_date.strftime('%a %d %b %Y')}</b>",
             f"Nifty today: {reports.fmt_pct(nifty_ret)}"]
    if tracker_block:
        lines += ["", tracker_block]
    if todays:
        lines.append("")
        lines.append("<b>Today's picks</b> (gap = prev close→open, post-open = open→close)")
        for r in conn.execute("SELECT * FROM picks WHERE run_date=? ORDER BY rank", (run_date.isoformat(),)):
            if not r["open_price"] or not r["prev_close"]:
                lines.append(f"  {esc(r['symbol'])}: prices unavailable")
                continue
            gap = (r["open_price"] / r["prev_close"] - 1) * 100
            post = (r["close_d0"] / r["open_price"] - 1) * 100
            day = (r["close_d0"] / r["prev_close"] - 1) * 100
            vs = f" ({day - nifty_ret:+.2f} vs Nifty)" if nifty_ret is not None else ""
            p945 = f", 09:45 ₹{r['price_0945']:,.2f}" if r["price_0945"] else ""
            lines.append(f"  {esc(r['symbol'])} [{r['status']}]: gap {gap:+.2f}%, post-open {post:+.2f}%, "
                         f"day {day:+.2f}%{vs}{p945}")
    else:
        lines.append("No picks today.")
    if tracker_block:   # the tracker is the paper analysis; the old approved-only ₹5,000 portfolio is hidden
        lines += ["", "<b>Running hit rate, all picks</b> (beat Nifty, entry at open)",
                  reports.horizon_lines(reports.horizon_stats(reports.outcome_frame(conn))), "", FOOTER]
        return "\n".join(lines)
    closed_today = [p for p in sim.positions if p.get("exit_date") is not None
                    and p["exit_date"].date() == run_date]
    opened_today = [p for p in sim.positions if p["entry_date"].date() == run_date]
    if opened_today or closed_today:
        lines.append("")
        for p in opened_today:
            lines.append(f"  📥 paper BUY {esc(p['symbol'].removesuffix('.NS'))} x{p['qty']} @ ₹{p['entry_price']:,.2f}")
        for p in closed_today:
            lines.append(f"  📤 paper SELL {esc(p['symbol'].removesuffix('.NS'))} ({p['exit_reason']}) "
                         f"net ₹{p['net_pnl']:+,.2f}")
    skipped = [s for s in sim.skipped if pd.Timestamp(s["date"]).date() == run_date]
    for s in skipped:
        lines.append(f"  (paper skipped {esc(s['symbol'].removesuffix('.NS'))}: {s['reason']})")
    lines += ["", reports.paper_lines(reports.paper_snapshot(conn))]
    if not sim.equity.empty and not sim.nifty_equity.empty:
        lines[-1] = lines[-1].split("\nSame capital")[0]
        lines.append(f"Same capital in Nifty 50 since first paper trade: ₹{sim.nifty_equity.iloc[-1]:,.0f}")
    lines += ["", "<b>Running hit rate, all picks</b> (beat Nifty, entry at open)",
              reports.horizon_lines(reports.horizon_stats(reports.outcome_frame(conn))), "", FOOTER]
    return "\n".join(lines)


def weekly_job(conn: sqlite3.Connection, cfg: Config, dry_run: bool = False) -> str:
    TelegramClient(dry_run=dry_run).send(reports.weekly_text(conn) + "\n\n" + FOOTER)
    return "done"


# =====================================================================================
# Backtest (price rules only)
# =====================================================================================
def backtest_job(conn: sqlite3.Connection, cfg: Config, years: int = 5, refresh: bool = True) -> str:
    from tabulate import tabulate

    universe = load_universe()
    bench_t = cfg.market.benchmark
    sector_idx = cfg.market.sector_index
    tickers = [s.yf for s in universe] + [bench_t] + _sector_tickers(cfg)
    hist_days = years * 252 + cfg.signals.sma_slow + 60
    if refresh:
        md.update_cache(conn, tickers, hist_days)
    start = date.today() - timedelta(days=int(hist_days * 1.5) + 10)
    prices = md.load_prices(conn, tickers, start)
    bench = prices.get(bench_t)
    if bench is None:
        raise RuntimeError("no Nifty data; cannot backtest")
    cal = bench.index
    test_start = cal[-1] - pd.DateOffset(years=years)

    # vectorized signals; row t only uses bars <= t
    panel = []
    by_sym: dict[str, Stock] = {s.yf: s for s in universe}
    for s in universe:
        df = prices.get(s.yf)
        if df is None or len(df) < cfg.signals.sma_slow + 30:
            continue
        f = indicator_frame(df, bench["close"], cfg)
        ok = (f["qualifies"] & (f["raw_close"] <= cfg.max_position_value)
              & (f["avg_traded_value_20d"] >= cfg.min_avg_traded_value_20d)
              & (f["price_score"] >= cfg.signals.min_total_score) & (f.index >= test_start))
        sel = f[ok]
        for d, r in sel.iterrows():
            panel.append({"date": d, "symbol": s.yf, "sector": s.sector, "score": r["price_score"],
                          **{k: r[k] for k in ("trend", "breakout", "volume", "not_overextended", "rel_strength")}})
    if not panel:
        raise RuntimeError("no signals generated")
    sig = pd.DataFrame(panel).sort_values(["date", "score", "rel_strength"], ascending=[True, False, False])
    sig = sig.groupby("date").head(cfg.shortlist_size).copy()
    sig["rank"] = sig.groupby("date").cumcount() + 1

    # entry at the next session's open
    nxt = pd.Series(cal[1:], index=cal[:-1])
    sig["entry_date"] = sig["date"].map(nxt)
    sig = sig.dropna(subset=["entry_date"])
    rows = []
    for r in sig.itertuples():
        hr = horizon_returns(prices[r.symbol], r.entry_date, bench, prices.get(sector_idx.get(r.sector, "")))
        row = {"date": r.date, "entry_date": r.entry_date, "symbol": r.symbol, "sector": r.sector, "rank": r.rank,
               "breakout": r.breakout, "volume": r.volume}
        for h, v in hr.items():
            row[f"ret_{h}"], row[f"ex_{h}"] = v["ret"], v["excess_nifty"]
        rows.append(row)
    res = pd.DataFrame(rows)

    entries = [Entry(r.symbol, r.entry_date, r.rank, None, r.sector) for r in sig.itertuples()]
    sim = simulate(entries, prices, cal, cfg, bench)
    closed = [p for p in sim.positions if p["status"] == "closed"]
    net = sum(p["net_pnl"] for p in sim.positions)
    gross = sum(p["gross_pnl"] for p in sim.positions)
    costs = sum(p["entry_costs"] + (p.get("exit_costs") or 0) for p in sim.positions)

    def hstats(d: pd.DataFrame) -> list:
        out = []
        for h in (1, 3, 5):
            r_, e_ = d.get(f"ret_{h}", pd.Series(dtype=float)).dropna(), d.get(f"ex_{h}", pd.Series(dtype=float)).dropna()
            out.append([f"{h}d", len(r_), f"{(r_ > 0).mean() * 100:.1f}%" if len(r_) else "–",
                        f"{(e_ > 0).mean() * 100:.1f}%" if len(e_) else "–",
                        f"{r_.mean():+.2f}%" if len(r_) else "–", f"{r_.median():+.2f}%" if len(r_) else "–",
                        f"{e_.mean():+.2f}%" if len(e_) else "–"])
        return out

    hdr = ["horizon", "n", "hit (ret>0)", "beat Nifty", "avg ret", "median ret", "avg excess"]
    sector_rows = []
    for sec, d in res.groupby("sector"):
        e5 = d.get("ex_5", pd.Series(dtype=float)).dropna()
        sector_rows.append([sec, len(d), f"{e5.mean():+.2f}%" if len(e5) else "–",
                            f"{(e5 > 0).mean() * 100:.0f}%" if len(e5) else "–"])
    sector_rows.sort(key=lambda x: -len(x))
    eq, neq = sim.equity, sim.nifty_equity
    trades = len(closed)
    wins = sum(1 for p in closed if p["net_pnl"] > 0)
    stops = sum(1 for p in closed if p["exit_reason"] == "stop")
    md_lines = [
        f"# Backtest – price rules only ({years}y, run {date.today()})",
        "",
        "> **Survivorship bias:** the universe is *today's* list (Nifty 100 + watchlist), so stocks that were "
        "dropped or delisted are missing and results are flattered. **No news or sector/global-cue scores** "
        "(historical news isn't available); only the price rules are replayed. Signals use bars up to the "
        "prior close; entry is at the next session's open. Learning tool, not advice.",
        "",
        f"Period: {cal[cal >= test_start][0].date()} → {cal[-1].date()} | universe {len(universe)} | "
        f"signals (top {cfg.shortlist_size}/day): {len(res)} on {res['date'].nunique()} days",
        "",
        "## Signal quality (entry at next open, all shortlisted signals)",
        "",
        tabulate(hstats(res), hdr, tablefmt="github"),
        "",
        f"## Simulated ₹{cfg.capital_total:,.0f} portfolio (every signal treated as approved; "
        f"max {cfg.max_open_positions} positions, ₹{cfg.max_position_value:,.0f} each, "
        f"{cfg.paper.hold_days}-day hold, -{cfg.paper.stop_loss_pct:g}% stop, "
        f"{cfg.paper.slippage_pct:g}% slippage, Zerodha delivery costs)",
        "",
        tabulate([
            ["trades closed", trades], ["win rate (net)", f"{wins / trades * 100:.1f}%" if trades else "–"],
            ["stopped out", stops], ["gross P&L", f"₹{gross:,.0f}"], ["total costs", f"₹{costs:,.0f}"],
            ["net P&L", f"₹{net:,.0f}"],
            ["final equity", f"₹{eq.iloc[-1]:,.0f}" if not eq.empty else "–"],
            ["max drawdown", f"{max_drawdown(eq):.1f}%"],
            ["Nifty buy-and-hold (same start)", f"₹{neq.iloc[-1]:,.0f}" if not neq.empty else "–"],
            ["Nifty max drawdown", f"{max_drawdown(neq.dropna()):.1f}%" if not neq.empty else "–"],
        ], tablefmt="github"),
        "",
        "## Per-sector (5-day excess vs Nifty)",
        "",
        tabulate(sorted(sector_rows, key=lambda x: -x[1]), ["sector", "signals", "avg 5d excess", "beat Nifty"],
                 tablefmt="github"),
        "",
        "## By rule (5-day excess when the rule fired)",
        "",
        tabulate([[k, int((res[k] == 1).sum()), f"{res.loc[res[k] == 1, 'ex_5'].mean():+.2f}%"]
                  for k in ("breakout", "volume") if "ex_5" in res],
                 ["rule", "signals", "avg 5d excess"], tablefmt="github"),
    ]
    text = "\n".join(md_lines)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"backtest_{date.today().isoformat()}.md"
    path.write_text(text)
    print(text)
    print(f"\nReport written to {path}")
    return "done"
