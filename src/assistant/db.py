"""SQLite schema, migrations and small helpers. All timestamps stored as UTC ISO strings;
trading dates are stored as IST calendar dates (YYYY-MM-DD)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import DB_PATH
from .util import iso_utc

MIGRATIONS: list[str] = [
    # v1: initial schema
    """
    CREATE TABLE picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,            -- IST session date the pick is for
        signal_date TEXT NOT NULL,         -- last daily bar used (prior close)
        symbol TEXT NOT NULL,
        name TEXT, sector TEXT, themes TEXT,
        rank INTEGER NOT NULL,
        total_score REAL NOT NULL,
        last_close REAL NOT NULL,
        summary TEXT,
        summarizer TEXT,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected/expired
        chat_id INTEGER, message_id INTEGER,
        prev_close REAL, open_price REAL, price_0945 REAL, close_d0 REAL,
        created_at TEXT NOT NULL,
        UNIQUE(run_date, symbol)
    );
    CREATE TABLE pick_scores (
        pick_id INTEGER NOT NULL REFERENCES picks(id),
        component TEXT NOT NULL,
        value REAL,
        PRIMARY KEY (pick_id, component)
    );
    CREATE TABLE headlines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id INTEGER NOT NULL REFERENCES picks(id),
        title TEXT, source TEXT, link TEXT, published_at TEXT, score INTEGER
    );
    CREATE TABLE decisions (
        pick_id INTEGER PRIMARY KEY REFERENCES picks(id),
        decision TEXT NOT NULL,            -- approve/reject/expired
        decided_at TEXT NOT NULL
    );
    CREATE TABLE paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id INTEGER, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
        entry_date TEXT NOT NULL, entry_price REAL NOT NULL, entry_costs REAL NOT NULL,
        status TEXT NOT NULL,              -- open/closed
        exit_date TEXT, exit_price REAL, exit_costs REAL, exit_reason TEXT,
        last_close REAL, gross_pnl REAL, net_pnl REAL
    );
    CREATE TABLE paper_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER NOT NULL, side TEXT NOT NULL, date TEXT NOT NULL,
        ref_price REAL, fill_price REAL NOT NULL, qty INTEGER NOT NULL, costs REAL NOT NULL,
        cost_detail TEXT
    );
    CREATE TABLE pick_outcomes (
        pick_id INTEGER NOT NULL REFERENCES picks(id),
        horizon INTEGER NOT NULL,          -- trading days, entry day = 1
        end_date TEXT,
        ret REAL, nifty_ret REAL, sector_ret REAL,
        excess_nifty REAL, excess_sector REAL,
        ret_from_0945 REAL,
        PRIMARY KEY (pick_id, horizon)
    );
    CREATE TABLE real_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id INTEGER, symbol TEXT NOT NULL, side TEXT NOT NULL,
        qty INTEGER NOT NULL, price REAL NOT NULL, traded_at TEXT NOT NULL, note TEXT
    );
    CREATE TABLE daily_summary (
        date TEXT PRIMARY KEY, text TEXT, nifty_ret REAL,
        paper_equity REAL, nifty_equity REAL, created_at TEXT
    );
    CREATE TABLE job_runs (
        job TEXT NOT NULL, run_date TEXT NOT NULL,
        status TEXT NOT NULL,              -- running/done/missed/skipped/failed/paused/waiting
        attempts INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, finished_at TEXT, detail TEXT,
        PRIMARY KEY (job, run_date)
    );
    CREATE TABLE price_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
        PRIMARY KEY (symbol, date)
    );
    CREATE TABLE price_meta (
        symbol TEXT PRIMARY KEY, last_full_refresh TEXT, last_fetch TEXT
    );
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """,
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (0)")
    for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript("BEGIN;" + sql + f"; UPDATE schema_version SET version={i}; COMMIT;")
    conn.commit()
    return len(MIGRATIONS)


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---- key/value (pause flag etc.) ----
def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


# ---- job runs ----
def job_status(conn: sqlite3.Connection, job: str, run_date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM job_runs WHERE job=? AND run_date=?", (job, run_date)).fetchone()


def job_mark(conn: sqlite3.Connection, job: str, run_date: str, status: str, detail: str | None = None) -> None:
    now = iso_utc()
    conn.execute(
        """INSERT INTO job_runs(job, run_date, status, attempts, started_at, finished_at, detail)
           VALUES(?,?,?,1,?,?,?)
           ON CONFLICT(job, run_date) DO UPDATE SET status=excluded.status,
             attempts=job_runs.attempts + (CASE WHEN excluded.status='running' THEN 1 ELSE 0 END),
             started_at=CASE WHEN excluded.status='running' THEN excluded.started_at ELSE job_runs.started_at END,
             finished_at=CASE WHEN excluded.status='running' THEN NULL ELSE excluded.finished_at END,
             detail=COALESCE(excluded.detail, job_runs.detail)""",
        (job, run_date, status, now, None if status == "running" else now, detail),
    )
    conn.commit()


FINAL_STATUSES = ("done", "missed", "skipped", "paused")


def job_finished(conn: sqlite3.Connection, job: str, run_date: str) -> bool:
    r = job_status(conn, job, run_date)
    return bool(r and r["status"] in FINAL_STATUSES)


# ---- decisions ----
def record_decision(conn: sqlite3.Connection, pick_id: int, decision: str) -> bool:
    """Record a decision exactly once. Returns True if this call recorded it."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO decisions(pick_id, decision, decided_at) VALUES(?,?,?)",
        (pick_id, decision, iso_utc()),
    )
    if cur.rowcount == 1:
        status = {"approve": "approved", "reject": "rejected", "expired": "expired"}[decision]
        conn.execute("UPDATE picks SET status=? WHERE id=?", (status, pick_id))
    conn.commit()
    return cur.rowcount == 1
