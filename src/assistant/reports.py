"""Stats and text for /status, /pnl, evening summaries, weekly report and `report` CLI.
All text is Telegram HTML (use telegram_bot.strip_html for console)."""
from __future__ import annotations

import sqlite3

import pandas as pd

from .config import load_config
from .paper import HORIZONS
from .telegram_bot import esc
from .util import today_ist


def outcome_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT o.*, p.symbol, p.sector, p.status, p.run_date FROM pick_outcomes o
           JOIN picks p ON p.id=o.pick_id""", conn)


def horizon_stats(df: pd.DataFrame) -> list[dict]:
    out = []
    for h in HORIZONS:
        d = df[df["horizon"] == h] if not df.empty else df
        ex = d["excess_nifty"].dropna() if not d.empty else pd.Series(dtype=float)
        out.append({"h": h, "n": int(len(d)),
                    "hit": float((ex > 0).mean() * 100) if len(ex) else None,
                    "avg_ret": float(d["ret"].mean()) if len(d) else None,
                    "avg_excess": float(ex.mean()) if len(ex) else None})
    return out


def fmt_pct(x, signed=True) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "–"
    return f"{x:+.2f}%" if signed else f"{x:.0f}%"


def horizon_lines(stats: list[dict]) -> str:
    return "\n".join(f"  {s['h']}d: n={s['n']}  beat Nifty {fmt_pct(s['hit'], False)}  "
                     f"avg {fmt_pct(s['avg_ret'])}  excess {fmt_pct(s['avg_excess'])}" for s in stats)


def paper_snapshot(conn: sqlite3.Connection) -> dict:
    cfg = load_config()
    pos = pd.read_sql_query("SELECT * FROM paper_positions ORDER BY id", conn)
    realized = float(pos.loc[pos["status"] == "closed", "net_pnl"].sum()) if not pos.empty else 0.0
    unreal = float(pos.loc[pos["status"] == "open", "net_pnl"].sum()) if not pos.empty else 0.0
    gross = float(pos["gross_pnl"].sum()) if not pos.empty else 0.0
    last = conn.execute("SELECT * FROM daily_summary ORDER BY date DESC LIMIT 1").fetchone()
    return {"positions": pos, "realized": realized, "unrealized": unreal, "gross": gross,
            "equity": cfg.capital_total + realized + unreal,
            "nifty_equity": last["nifty_equity"] if last and last["nifty_equity"] else None}


def paper_lines(snap: dict) -> str:
    cfg = load_config()
    lines = [f"Paper equity ₹{snap['equity']:,.0f} (start ₹{cfg.capital_total:,.0f}) | "
             f"realized net ₹{snap['realized']:+,.0f}, open net ₹{snap['unrealized']:+,.0f}, "
             f"gross ₹{snap['gross']:+,.0f}"]
    if snap["nifty_equity"]:
        lines.append(f"Same capital in Nifty 50: ₹{snap['nifty_equity']:,.0f}")
    pos = snap["positions"]
    if not pos.empty:
        for _, p in pos[pos["status"] == "open"].iterrows():
            lines.append(f"  OPEN {esc(p['symbol'])} x{p['qty']} @ ₹{p['entry_price']:,.2f} "
                         f"(since {p['entry_date']}) last ₹{(p['last_close'] or 0):,.2f} net ₹{p['net_pnl']:+,.0f}")
    return "\n".join(lines)


def status_text(conn: sqlite3.Connection) -> str:
    today = today_ist().isoformat()
    picks = conn.execute("SELECT * FROM picks WHERE run_date=? ORDER BY rank", (today,)).fetchall()
    paused = conn.execute("SELECT value FROM kv WHERE key='paused'").fetchone()
    lines = [f"<b>Status {today}</b>" + (" (⏸ paused)" if paused and paused[0] == "1" else "")]
    if picks:
        lines.append("Today's picks:")
        lines += [f"  {p['rank']}) {esc(p['symbol'])} score {p['total_score']:.1f} – {p['status']}" for p in picks]
    else:
        lines.append("No picks today.")
    lines.append(paper_lines(paper_snapshot(conn)))
    return "\n".join(lines)


def pnl_text(conn: sqlite3.Connection) -> str:
    df = outcome_frame(conn)
    lines = ["<b>Paper P&amp;L</b>", paper_lines(paper_snapshot(conn)),
             "", "<b>All picks vs Nifty</b> (entry at open)", horizon_lines(horizon_stats(df))]
    real = conn.execute("SELECT side, COUNT(*) n, SUM(qty*price) v FROM real_trades GROUP BY side").fetchall()
    if real:
        lines.append("")
        lines.append("Real trades logged: " + ", ".join(f"{r['side']} {r['n']} (₹{r['v']:,.0f})" for r in real))
    return "\n".join(lines)


def weekly_text(conn: sqlite3.Connection) -> str:
    df = outcome_frame(conn)
    lines = ["<b>📊 Weekly report</b>", "", "<b>All picks</b>", horizon_lines(horizon_stats(df))]
    d5 = df[df["horizon"] == 5] if not df.empty else df
    if not d5.empty:
        lines += ["", "<b>By sector (5d, excess vs Nifty)</b>"]
        g = d5.groupby("sector")["excess_nifty"].agg(["count", "mean", lambda s: (s > 0).mean() * 100])
        for sector, r in g.sort_values("mean", ascending=False).iterrows():
            lines.append(f"  {esc(sector)}: n={int(r['count'])} avg {r['mean']:+.2f}% beat {r.iloc[2]:.0f}%")
        scores = pd.read_sql_query("SELECT pick_id, component, value FROM pick_scores", conn)
        lines += ["", "<b>By rule fired (5d excess)</b>"]
        for rule in ("trend", "breakout", "volume", "not_overextended"):
            ids = scores[(scores.component == rule) & (scores.value == 1)]["pick_id"]
            sub = d5[d5.pick_id.isin(ids)]["excess_nifty"].dropna()
            if len(sub):
                lines.append(f"  {rule}: n={len(sub)} avg {sub.mean():+.2f}% beat {(sub > 0).mean() * 100:.0f}%")
        news_ids = scores[(scores.component == "news") & (scores.value > 0)]["pick_id"]
        sub = d5[d5.pick_id.isin(news_ids)]["excess_nifty"].dropna()
        if len(sub):
            lines.append(f"  positive news: n={len(sub)} avg {sub.mean():+.2f}%")
        lines += ["", "<b>Your decisions (5d excess)</b>"]
        for st in ("approved", "rejected", "expired", "pending"):
            sub = d5[d5.status == st]["excess_nifty"].dropna()
            if len(sub):
                lines.append(f"  {st}: n={len(sub)} avg {sub.mean():+.2f}% beat {(sub > 0).mean() * 100:.0f}%")
    counts = conn.execute("SELECT status, COUNT(*) n FROM picks GROUP BY status").fetchall()
    if counts:
        lines.append("Pick counts: " + ", ".join(f"{r['status']} {r['n']}" for r in counts))
    lines += ["", paper_lines(paper_snapshot(conn))]
    return "\n".join(lines)
