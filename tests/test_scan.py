from datetime import date, datetime

import numpy as np
import pandas as pd

from assistant.config import Config
from assistant.scan import (HoldingCheck, check_holdings, classify, clean_symbol, holdings_review_text,
                            rotation_text, scan_session)

CFG = Config()


def frame(closes):
    idx = pd.bdate_range("2026-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "raw_close": c, "volume": 1e6})


def st(close, fast, s50, s200, rsi_=55, ret5=0.0):
    return {"close": close, "sma_fast": fast, "sma50": s50, "sma200": s200, "rsi": rsi_, "ret5": ret5,
            "sma_fast_raw": fast}


def hc(pct):
    return HoldingCheck("X", 10, 100, 100 * (1 + pct / 100), 10 * pct, pct)


def test_scan_session_uses_completed_bars():
    assert scan_session(datetime(2026, 11, 11, 11, 0)) == date(2026, 11, 11)
    assert scan_session(datetime(2026, 11, 11, 16, 0)) == date(2026, 11, 12)


def test_clean_symbol():
    assert clean_symbol("IDEA-BE") == "IDEA" and clean_symbol("M&M") == "M&M"


def test_profit_and_fading_is_book():
    h = classify(hc(15), st(110, 112, 100, 90), 0, CFG)
    assert h.flag == "book" and "momentum fading" in h.reasons[0]


def test_profit_overbought_is_book():
    assert classify(hc(15), st(115, 110, 100, 90, rsi_=80), 0, CFG).flag == "book"


def test_profit_trend_intact_is_protect_with_stop_hint():
    h = classify(hc(15), st(115, 110, 100, 90), 0, CFG)
    assert h.flag == "protect" and h.stop_hint == 110


def test_downtrend_is_weak():
    assert classify(hc(-5), st(80, 85, 90, 95), 0, CFG).flag == "weak"


def test_sharp_drop_below_50_is_weak_but_dip_in_uptrend_is_hold():
    assert classify(hc(-3), st(95, 98, 100, 90, ret5=-6), 0, CFG).flag == "weak"
    assert classify(hc(-3), st(105, 108, 100, 90, ret5=-6), 0, CFG).flag == "hold"


def test_negative_news_is_weak():
    assert classify(hc(2), st(105, 104, 100, 90), -3, CFG).flag == "weak"


def test_no_history_is_nodata():
    assert classify(hc(2), None, 0, CFG).flag == "nodata"


def test_check_holdings_end_to_end():
    up = frame(np.linspace(100, 200, 250))          # steady uptrend
    down = frame(np.linspace(200, 100, 250))        # steady downtrend
    holdings = [
        {"tradingsymbol": "UP", "quantity": 5, "t1_quantity": 0, "average_price": 150, "last_price": 200, "pnl": 250},
        {"tradingsymbol": "DN", "quantity": 5, "t1_quantity": 0, "average_price": 150, "last_price": 100, "pnl": -250},
        {"tradingsymbol": "ZZ", "quantity": 0, "t1_quantity": 0, "average_price": 1, "last_price": 1},
        {"tradingsymbol": "NEW", "quantity": 2, "t1_quantity": 0, "average_price": 10, "last_price": 11},
    ]
    gtts = [{"status": "active", "condition": {"tradingsymbol": "UP"}, "orders": [{"transaction_type": "SELL"}]}]
    prices = {"UP.NS": up, "DN.NS": down}
    checks = check_holdings(holdings, gtts, prices, {}, CFG, date(2027, 6, 1))
    by = {c.symbol: c for c in checks}
    assert set(by) == {"UP", "DN", "NEW"}
    assert by["DN"].flag == "weak" and by["NEW"].flag == "nodata"
    assert by["UP"].flag in ("protect", "book") and by["UP"].has_sell_gtt
    text = holdings_review_text(checks, CFG)
    assert "DN" in text and "Weak" in text


def test_rotation_skips_already_held_and_sizes_by_cap():
    a = HoldingCheck("A", 10, 100, 300, 2000, 200, flag="book")
    b = HoldingCheck("B", 1, 100, 100, 0, 0, flag="hold")
    t = rotation_text([a, b], ["B", "P1", "P2"], CFG)
    assert "A" in t and "P1" in t and "B," not in t
    assert rotation_text([b], ["P1"], CFG) is None
