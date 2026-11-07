"""Price data via yfinance with a SQLite cache, plus best-effort global cues."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

from .config import Config
from .util import iso_utc, now_utc, redact, retry

log = logging.getLogger(__name__)
FULL_REFRESH_DAYS = 7      # re-download full history weekly so split/dividend adjustments stay right
BATCH = 40


def _yf():
    import yfinance as yf  # imported lazily: slow import, and keeps tests offline
    return yf


def _split_download(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    for t in tickers:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                lvl0 = raw.columns.get_level_values(0)
                sub = raw[t] if t in lvl0 else raw.xs(t, axis=1, level=1)
            else:
                sub = raw
        except KeyError:
            continue
        sub = sub.rename(columns=lambda c: str(c).lower().replace(" ", "_"))
        if "close" not in sub:
            continue
        sub = sub.dropna(subset=["close", "open"], how="all")
        if sub.empty:
            continue
        if "adj_close" not in sub:
            sub["adj_close"] = sub["close"]
        out[t] = sub[["open", "high", "low", "close", "adj_close", "volume"]].copy()
    return out


def download_daily(tickers: list[str], start: date | None = None, period: str | None = None,
                   interval: str = "1d") -> dict[str, pd.DataFrame]:
    """Batched yfinance download. Missing/failed tickers are simply absent."""
    yf = _yf()
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        kw = dict(auto_adjust=False, group_by="ticker", progress=False, threads=True,
                  interval=interval, timeout=20)
        if start is not None:
            kw["start"] = start.isoformat()
        else:
            kw["period"] = period or "1y"
        try:
            raw = retry(lambda: yf.download(chunk, **kw), tries=3, what=f"yfinance {len(chunk)} tickers")
        except Exception as e:  # noqa: BLE001
            log.error("price download failed for %s..: %s", chunk[:3], redact(str(e)))
            continue
        out.update(_split_download(raw, chunk))
    if interval == "1d":
        _patch_missing_closes(out)
    return out


def _patch_missing_closes(data: dict[str, pd.DataFrame]) -> None:
    """Yahoo sometimes returns the latest daily bar with a NaN close (open/volume present).
    Rebuild such recent bars from hourly bars; drop any row that still lacks a close."""
    holes = {t: df.index[-7:][df["close"].iloc[-7:].isna()] for t, df in data.items()}
    holes = {t: idx for t, idx in holes.items() if len(idx)}
    if holes:
        log.info("patching %d tickers with missing daily closes from hourly bars", len(holes))
        hourly = download_daily(list(holes), period="7d", interval="60m")
        for t, idx in holes.items():
            h = hourly.get(t)
            if h is None:
                continue
            hidx = h.index.tz_convert("Asia/Kolkata") if h.index.tz is not None else h.index
            for d in idx:
                day = h[hidx.date == pd.Timestamp(d).date()]
                if day.empty:
                    continue
                df = data[t]
                df.at[d, "close"] = df.at[d, "adj_close"] = float(day["close"].iloc[-1])
                df.at[d, "high"] = float(day["high"].max())
                df.at[d, "low"] = float(day["low"].min())
                if pd.isna(df.at[d, "open"]):
                    df.at[d, "open"] = float(day["open"].iloc[0])
                if pd.isna(df.at[d, "volume"]) or not df.at[d, "volume"]:
                    df.at[d, "volume"] = float(day["volume"].sum())
    for t in list(data):
        data[t] = data[t].dropna(subset=["close"])
        if data[t].empty:
            del data[t]


# ---------------- cache ----------------
def _upsert(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> None:
    rows = [
        (ticker, pd.Timestamp(idx).date().isoformat(), _f(r.open), _f(r.high), _f(r.low), _f(r.close),
         _f(r.adj_close), _f(r.volume))
        for idx, r in df.iterrows()
    ]
    conn.executemany(
        """INSERT INTO price_cache(symbol,date,open,high,low,close,adj_close,volume) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
           close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume""",
        rows,
    )


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


def update_cache(conn: sqlite3.Connection, tickers: list[str], history_days: int,
                 today: date | None = None) -> None:
    today = today or now_utc().date()
    full_start = today - timedelta(days=int(history_days * 1.5) + 10)  # calendar days for N trading days
    meta = {r["symbol"]: r for r in conn.execute("SELECT * FROM price_meta").fetchall()}
    first = {r[0]: date.fromisoformat(r[1]) for r in
             conn.execute("SELECT symbol, MIN(date) FROM price_cache GROUP BY symbol").fetchall()}
    last = {r[0]: date.fromisoformat(r[1]) for r in
            conn.execute("SELECT symbol, MAX(date) FROM price_cache GROUP BY symbol").fetchall()}
    need_full, incr = [], []
    for t in tickers:
        m = meta.get(t)
        stale = (m is None or not m["last_full_refresh"]
                 or datetime.fromisoformat(m["last_full_refresh"]) < now_utc() - timedelta(days=FULL_REFRESH_DAYS))
        if t not in last or stale or first.get(t, today) > full_start + timedelta(days=10):
            need_full.append(t)
        else:
            incr.append(t)
    now = iso_utc()
    if need_full:
        log.info("full price download for %d tickers from %s", len(need_full), full_start)
        for t, df in download_daily(need_full, start=full_start).items():
            conn.execute("DELETE FROM price_cache WHERE symbol=?", (t,))
            _upsert(conn, t, df)
            conn.execute("INSERT INTO price_meta(symbol,last_full_refresh,last_fetch) VALUES(?,?,?) "
                         "ON CONFLICT(symbol) DO UPDATE SET last_full_refresh=excluded.last_full_refresh, "
                         "last_fetch=excluded.last_fetch", (t, now, now))
        conn.commit()
    if incr:
        start = min(last[t] for t in incr) - timedelta(days=7)
        log.info("incremental price download for %d tickers from %s", len(incr), start)
        for t, df in download_daily(incr, start=start).items():
            _upsert(conn, t, df)
            conn.execute("UPDATE price_meta SET last_fetch=? WHERE symbol=?", (now, t))
        conn.commit()


def load_prices(conn: sqlite3.Connection, tickers: list[str], start: date | None = None) -> dict[str, pd.DataFrame]:
    """Returns split/dividend-adjusted OHLC (+ raw_close, volume) per ticker, indexed by Timestamp."""
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        q = "SELECT date, open, high, low, close, adj_close, volume FROM price_cache WHERE symbol=?"
        args: list = [t]
        if start:
            q += " AND date>=?"
            args.append(start.isoformat())
        df = pd.read_sql_query(q + " ORDER BY date", conn, params=args)
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        out[t] = adjust(df)
    return out


def adjust(df: pd.DataFrame) -> pd.DataFrame:
    factor = (df["adj_close"] / df["close"]).fillna(1.0)
    res = pd.DataFrame(index=df.index)
    for c in ("open", "high", "low", "close"):
        res[c] = df[c] * factor
    res["raw_close"] = df["close"]
    res["volume"] = df["volume"].fillna(0)
    return res


def get_prices(conn: sqlite3.Connection, tickers: list[str], history_days: int,
               refresh: bool = True) -> dict[str, pd.DataFrame]:
    if refresh:
        update_cache(conn, tickers, history_days)
    start = now_utc().date() - timedelta(days=int(history_days * 1.5) + 10)
    return load_prices(conn, tickers, start)


# ---------------- global cues ----------------
def global_cues(cfg: Config) -> list[dict]:
    """Latest move per cue ticker (best-effort). Each: name, ticker, last, change_pct, date."""
    cues = dict(cfg.market.global_cues)
    if cfg.market.gift_nifty_ticker:
        cues["GIFT Nifty"] = cfg.market.gift_nifty_ticker
    try:
        data = download_daily(list(cues.values()), period="10d")
    except Exception as e:  # noqa: BLE001
        log.warning("global cues unavailable: %s", e)
        return []
    out = []
    for name, t in cues.items():
        df = data.get(t)
        if df is None or len(df) < 2:
            continue
        last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        out.append({"name": name, "ticker": t, "last": last,
                    "change_pct": (last / prev - 1) * 100 if prev else 0.0,
                    "date": pd.Timestamp(df.index[-1]).date().isoformat()})
    return out


def intraday_15m(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Last ~5 sessions of 15-minute bars (yfinance keeps ~60 days of these)."""
    return download_daily(tickers, period="5d", interval="15m")


def price_at(df15: pd.DataFrame, day: date, hhmm: str, tz) -> float | None:
    """Open of the 15m bar starting at hh:mm IST on `day` (e.g. 09:45)."""
    if df15 is None or df15.empty:
        return None
    idx = df15.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    local = idx.tz_convert(tz)
    h, m = map(int, hhmm.split(":"))
    mask = (local.date == day) & (local.hour == h) & (local.minute == m)
    sel = df15[mask]
    if sel.empty:
        return None
    return float(sel["open"].iloc[0])
