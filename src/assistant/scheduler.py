"""Tick-based scheduler. launchd runs `cli tick` every 5 minutes; each tick converts "now"
to IST and runs whatever is due and not yet finished today. Never depends on the Mac's
local timezone."""
from __future__ import annotations

import fcntl
import logging
import sqlite3
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import DATA_DIR, Config
from .db import job_finished, job_mark, job_status, kv_get
from .universe import is_trading_day, previous_trading_day

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
HOLIDAY_INFER_AFTER = time(21, 0)


@dataclass(frozen=True)
class Due:
    job: str          # premarket | premarket_missed | expire | evening | weekly
    run_date: date


def due_jobs(now: datetime, conn: sqlite3.Connection, cfg: Config) -> list[Due]:
    """Pure decision logic. `now` may be in any timezone; it is converted to IST."""
    now = now.astimezone(cfg.tz)
    today, t = now.date(), now.time()
    sch = cfg.scheduler
    pm_start, pm_cut = sch.t(sch.premarket_time), sch.t(sch.premarket_cutoff)
    ev, expiry = sch.t(sch.evening_time), sch.t(sch.pick_expiry_time)
    out: list[Due] = []

    def retryable(job: str, d: date) -> bool:
        r = job_status(conn, job, d.isoformat())
        if r is None:
            return True
        if r["status"] in ("done", "missed", "skipped", "paused"):
            return False
        return r["attempts"] < MAX_ATTEMPTS or r["status"] == "waiting"

    trading_today = is_trading_day(today, conn)
    if trading_today and retryable("premarket", today):
        if pm_start <= t < pm_cut:
            out.append(Due("premarket", today))
        elif t >= pm_cut:
            out.append(Due("premarket_missed", today))
    if trading_today and t >= expiry:
        out.append(Due("expire", today))

    # evening for today, or catch-up for the previous trading day until the next pre-market
    if trading_today and t >= ev:
        ev_day = today
    elif not trading_today or t < pm_start:
        ev_day = previous_trading_day(today, conn)
    else:
        ev_day = None
    if ev_day and retryable("evening", ev_day):
        out.append(Due("evening", ev_day))

    # weekly report Saturday morning (catch-up Sunday)
    if today.weekday() in (5, 6):
        sat = today - timedelta(days=today.weekday() - 5)
        if (today > sat or t >= sch.t(sch.weekly_report_time)) and not job_finished(conn, "weekly", sat.isoformat()) \
                and retryable("weekly", sat):
            out.append(Due("weekly", sat))
    return out


def run_tick(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[tuple[str, str]]:
    from . import jobs
    from .telegram_bot import alert, esc
    from .util import now_ist, redact

    now = now or now_ist()
    lock_f = open(DATA_DIR / "tick.lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another tick is running; exiting")
        return []
    results = []
    try:
        for d in due_jobs(now, conn, cfg):
            rd = d.run_date.isoformat()
            if d.job == "expire":
                n = jobs.expire_pending(conn, d.run_date)
                if n:
                    log.info("expired %d pending picks", n)
                continue
            if d.job == "premarket_missed":
                if kv_get(conn, "paused") == "1":
                    job_mark(conn, "premarket", rd, "paused")
                else:
                    jobs.missed_notice(cfg)
                    job_mark(conn, "premarket", rd, "missed", "window missed")
                results.append(("premarket", "missed"))
                continue
            job_mark(conn, d.job, rd, "running")
            try:
                if d.job == "premarket":
                    status = jobs.premarket_job(conn, cfg, now=now)
                elif d.job == "evening":
                    status = jobs.evening_job(conn, cfg, d.run_date)
                    if status == "waiting":
                        status = _handle_waiting(conn, d.run_date, now)
                elif d.job == "weekly":
                    status = jobs.weekly_job(conn, cfg)
                else:
                    continue
                job_mark(conn, d.job, rd, status)
                results.append((d.job, status))
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()
                log.error("%s failed: %s", d.job, redact(tb))
                job_mark(conn, d.job, rd, "failed", redact(f"{type(e).__name__}: {e}")[:500])
                last = redact(tb.strip().splitlines()[-1])[:300]
                where = [ln.strip() for ln in tb.splitlines() if ln.strip().startswith("File")][-1:]
                alert(f"⚠️ <b>{esc(d.job)} job failed</b> ({rd})\n<code>{esc(last)}</code>\n"
                      f"{esc(redact(where[0]) if where else '')}\nSee logs/tick.log")
                results.append((d.job, "failed"))
        try:  # cache posts from public Telegram channels (extra news source for the morning screen)
            from .channel import maybe_poll
            n = maybe_poll(conn, cfg, now)
            if n:
                log.info("channel poll: %d new posts", n)
        except Exception as e:  # noqa: BLE001
            log.warning("channel poll failed: %s", redact(f"{type(e).__name__}: {e}"))
        try:  # track Kite triggers placed from Telegram (read-only unless a stop-loss is due/cleanup)
            from .orders import sync_tick
            sync_tick(conn, cfg, now, lambda m: alert(esc(m)))
        except Exception as e:  # noqa: BLE001
            log.error("order sync failed: %s", redact(f"{type(e).__name__}: {e}"))
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()
    return results


def _handle_waiting(conn: sqlite3.Connection, run_date: date, now: datetime) -> str:
    """Today's bars not available. Notify once; infer a holiday if data never shows up."""
    from .telegram_bot import alert

    r = job_status(conn, "evening", run_date.isoformat())
    attempts = r["attempts"] if r else 1
    local = now.astimezone(now.tzinfo)
    if local.date() > run_date or local.time() >= HOLIDAY_INFER_AFTER:
        job_mark(conn, "holiday", run_date.isoformat(), "done", "inferred from missing data")
        alert(f"ℹ️ No market data for {run_date}; treating it as a market holiday. "
              "Add it to data/holidays.csv if correct.")
        return "skipped"
    if attempts == 2:
        alert(f"ℹ️ Today's ({run_date}) closing data isn't available yet. I'll keep retrying every few minutes.")
    return "waiting"
