from datetime import datetime
from zoneinfo import ZoneInfo

from assistant.db import record_decision
from assistant.telegram_bot import handle_decision, parse_trade, split_message

IST = ZoneInfo("Asia/Kolkata")


def _pick(conn, run_date="2026-11-09"):
    cur = conn.execute("INSERT INTO picks(run_date,signal_date,symbol,rank,total_score,last_close,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (run_date, "2026-11-06", "HAL", 1, 7.0, 1000, "x"))
    conn.commit()
    return cur.lastrowid


def test_migrations_create_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("picks", "pick_scores", "headlines", "decisions", "paper_positions", "paper_fills", "pick_outcomes",
              "real_trades", "daily_summary", "job_runs", "price_cache", "schema_version"):
        assert t in names


def test_decision_recorded_exactly_once(conn):
    pid = _pick(conn)
    now = datetime(2026, 11, 9, 8, 30, tzinfo=IST)
    assert handle_decision(conn, pid, "approve", now)[0] == "recorded"
    assert handle_decision(conn, pid, "approve", now)[0] == "duplicate"
    assert handle_decision(conn, pid, "reject", now)[0] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE pick_id=?", (pid,)).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM picks WHERE id=?", (pid,)).fetchone()[0] == "approved"


def test_late_tap_expires(conn):
    pid = _pick(conn)
    assert handle_decision(conn, pid, "approve", datetime(2026, 11, 9, 11, 5, tzinfo=IST))[0] == "expired"
    assert conn.execute("SELECT status FROM picks WHERE id=?", (pid,)).fetchone()[0] == "expired"


def test_unknown_pick(conn):
    assert handle_decision(conn, 999, "approve", datetime(2026, 11, 9, 9, 0, tzinfo=IST))[0] == "unknown"


def test_record_decision_idempotent(conn):
    pid = _pick(conn)
    assert record_decision(conn, pid, "reject") is True
    assert record_decision(conn, pid, "approve") is False


def test_parse_trade():
    assert parse_trade("bought HAL 2 4210.5") == ("buy", "HAL", 2, 4210.5)
    assert parse_trade("Sold m&m 1 3000") == ("sell", "M&M", 1, 3000.0)
    assert parse_trade("bought HAL two 4210") is None
    assert parse_trade("bought HAL 0 4210") is None


def test_split_message():
    parts = split_message("\n".join(["x" * 100] * 100), 1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 1
