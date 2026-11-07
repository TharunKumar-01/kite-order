"""Universe loader and NSE trading calendar."""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from .config import HOLIDAYS_CSV, UNIVERSE_CSV


@dataclass(frozen=True)
class Stock:
    symbol: str
    name: str
    sector: str
    themes: tuple[str, ...] = field(default_factory=tuple)
    group: str = "nifty100"
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def yf(self) -> str:
        return yf_ticker(self.symbol)

    @property
    def tags(self) -> tuple[str, ...]:
        """Sector plus themes, for cue / query matching."""
        return (self.sector, *self.themes)


def yf_ticker(symbol: str) -> str:
    return symbol if symbol.startswith("^") or "=" in symbol or "." in symbol else f"{symbol}.NS"


def load_universe(path: Path | str | None = None) -> list[Stock]:
    p = Path(path) if path else UNIVERSE_CSV
    out: list[Stock] = []
    seen: set[str] = set()
    with open(p, newline="") as f:
        for row in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            sym = (row.get("symbol") or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            themes = tuple(t.strip() for t in (row.get("themes") or "").split(";") if t.strip())
            aliases = tuple(a.strip() for a in (row.get("aliases") or "").split(";") if a.strip())
            out.append(Stock(sym, (row.get("name") or sym).strip(), (row.get("sector") or "Other").strip(),
                             themes, (row.get("group") or "nifty100").strip() or "nifty100", aliases))
    return out


# ---------------- trading calendar ----------------
@lru_cache(maxsize=4)
def load_holidays(path: str | None = None) -> frozenset[date]:
    p = Path(path) if path else HOLIDAYS_CSV
    if not p.exists():
        return frozenset()
    days = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("date"):
            continue
        try:
            days.add(date.fromisoformat(line.split(",")[0].strip()))
        except ValueError:
            continue
    return frozenset(days)


def inferred_holidays(conn: sqlite3.Connection | None) -> set[date]:
    if conn is None:
        return set()
    rows = conn.execute("SELECT run_date FROM job_runs WHERE job='holiday'").fetchall()
    return {date.fromisoformat(r[0]) for r in rows}


def is_trading_day(d: date, conn: sqlite3.Connection | None = None,
                   holidays: frozenset[date] | set[date] | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    hol = load_holidays() if holidays is None else holidays
    return d not in hol and d not in inferred_holidays(conn)


def previous_trading_day(d: date, conn: sqlite3.Connection | None = None,
                         holidays: frozenset[date] | set[date] | None = None) -> date:
    x = d - timedelta(days=1)
    while not is_trading_day(x, conn, holidays):
        x -= timedelta(days=1)
    return x
