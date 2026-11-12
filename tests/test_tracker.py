from datetime import date

import pandas as pd

from assistant import tracker
from conftest import make_bars


def _pick(conn, run_date, sym="ABC", sector="Banks", status="pending", rsi=60.0):
    cur = conn.execute(
        "INSERT INTO picks(run_date,signal_date,symbol,name,sector,rank,total_score,last_close,status,created_at)"
        " VALUES(?,?,?,?,?,1,5,100,?, 'x')", (run_date, run_date, sym, sym + " Ltd", sector, status))
    conn.execute("INSERT INTO pick_scores VALUES(?,?,?)", (cur.lastrowid, "rsi", rsi))
    conn.commit()
    return cur.lastrowid


def test_every_pick_is_followed_with_stop_and_reasons(conn, cfg):
    # stock: opens day 3 at 100, drifts down to 93 (a 7% fall: stop at 6%)
    closes = [100, 100, 100, 99, 97, 95, 93, 92]
    stock = make_bars(closes)
    nifty = make_bars([100] * 8)                       # flat market
    bank = make_bars([100] * 8)                        # flat sector
    d = stock.index[2]
    _pick(conn, d.date().isoformat(), status="rejected", rsi=72)
    prices = {"ABC.NS": stock, "^NSEBANK": bank}
    tr = tracker.track(conn, cfg, prices, nifty, date(2023, 12, 1), date(2024, 2, 1))
    assert len(tr) == 1
    t = tr[0]
    assert t["closed"] == "stop" and t["ret"] < -6 and t["status"] == "rejected"
    keys = [k for k, _ in tracker.diagnose(t)]
    assert keys[0] == "stock" and "stretched" in keys and "market" not in keys
    text = tracker.tracker_text(tr, cfg, date(2024, 1, 10), {"ABC": [(-2, "ABC <b>probe</b> fined")]})
    assert "ABC" in text and "stopped" in text and "stock-specific" in text
    assert "&lt;b&gt;probe" in text                    # headline text is escaped (untrusted)


def test_market_fall_is_blamed_on_the_market(conn, cfg):
    stock = make_bars([100, 100, 98, 97, 96.5])
    nifty = make_bars([100, 100, 98, 97, 96.6])
    _pick(conn, stock.index[1].date().isoformat(), sector="Unknown")
    tr = tracker.track(conn, cfg, {"ABC.NS": stock}, nifty, date(2023, 12, 1), date(2024, 2, 1))
    keys = [k for k, _ in tracker.diagnose(tr[0])]
    assert keys[0] == "market" and "stock" not in keys


def test_scorecard_compares_your_calls(conn, cfg):
    up = make_bars([100, 100, 102, 104, 106])
    down = make_bars([100, 100, 99, 98, 98.5])
    nifty = make_bars([100] * 5)
    day = up.index[1].date().isoformat()
    _pick(conn, day, "UPP", status="approved")
    _pick(conn, day, "DWN", status="rejected")
    tr = tracker.track(conn, cfg, {"UPP.NS": up, "DWN.NS": down}, nifty, date(2023, 12, 1), date(2024, 2, 1))
    text = tracker.tracker_text(tr, cfg, date(2024, 1, 5))
    assert "2 picks" in text and "1 up" in text and "approved 1" in text and "rejected 1" in text


def test_empty_tracker_message(cfg):
    assert "No picks tracked yet" in tracker.tracker_text([], cfg, date(2024, 1, 5))
