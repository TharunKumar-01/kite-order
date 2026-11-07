from datetime import datetime
from zoneinfo import ZoneInfo

from assistant.db import job_mark
from assistant.scheduler import due_jobs

IST = ZoneInfo("Asia/Kolkata")


def jobs(now, conn, cfg):
    return [(d.job, d.run_date.isoformat()) for d in due_jobs(now, conn, cfg)]


def test_premarket_window(conn, cfg):
    assert ("premarket", "2026-11-09") in jobs(datetime(2026, 11, 9, 8, 20, tzinfo=IST), conn, cfg)
    assert not any(j == "premarket" for j, _ in jobs(datetime(2026, 11, 9, 8, 10, tzinfo=IST), conn, cfg))


def test_missed_window_sends_missed_not_picks(conn, cfg):
    js = jobs(datetime(2026, 11, 9, 10, 30, tzinfo=IST), conn, cfg)
    assert ("premarket_missed", "2026-11-09") in js
    assert not any(j == "premarket" for j, _ in js)


def test_done_premarket_not_rerun(conn, cfg):
    job_mark(conn, "premarket", "2026-11-09", "done")
    js = jobs(datetime(2026, 11, 9, 8, 30, tzinfo=IST), conn, cfg)
    assert not any(j.startswith("premarket") for j, _ in js)


def test_timezone_independent(conn, cfg):
    # Same instant expressed in three timezones -> identical decisions.
    instant = datetime(2026, 11, 9, 8, 20, tzinfo=IST)
    a = jobs(instant, conn, cfg)
    b = jobs(instant.astimezone(ZoneInfo("America/Los_Angeles")), conn, cfg)
    c = jobs(instant.astimezone(ZoneInfo("Europe/London")), conn, cfg)
    assert a == b == c and ("premarket", "2026-11-09") in a


def test_evening_and_catch_up(conn, cfg):
    assert ("evening", "2026-11-09") in jobs(datetime(2026, 11, 9, 16, 35, tzinfo=IST), conn, cfg)
    assert not any(j == "evening" for j, _ in jobs(datetime(2026, 11, 9, 16, 0, tzinfo=IST), conn, cfg))
    # 2026-11-10 is a holiday; next morning before pre-market of 11-11 -> catch up 11-09
    assert ("evening", "2026-11-09") in jobs(datetime(2026, 11, 11, 7, 0, tzinfo=IST), conn, cfg)
    # once the next pre-market window opens, no catch-up for the previous day
    assert not any(j == "evening" for j, _ in jobs(datetime(2026, 11, 11, 9, 0, tzinfo=IST), conn, cfg))
    job_mark(conn, "evening", "2026-11-09", "done")
    assert not any(j == "evening" for j, _ in jobs(datetime(2026, 11, 9, 18, 0, tzinfo=IST), conn, cfg))


def test_weekend_and_holiday(conn, cfg):
    sat = jobs(datetime(2026, 11, 14, 9, 0, tzinfo=IST), conn, cfg)
    assert ("weekly", "2026-11-14") in sat and not any(j.startswith("premarket") for j, _ in sat)
    hol = jobs(datetime(2026, 11, 10, 8, 20, tzinfo=IST), conn, cfg)
    assert not any(j.startswith("premarket") for j, _ in hol)


def test_failed_job_retries_are_capped(conn, cfg):
    for _ in range(3):
        job_mark(conn, "premarket", "2026-11-09", "running")
        job_mark(conn, "premarket", "2026-11-09", "failed")
    assert not any(j == "premarket" for j, _ in jobs(datetime(2026, 11, 9, 8, 40, tzinfo=IST), conn, cfg))
