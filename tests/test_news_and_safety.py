import pathlib
import re
from datetime import datetime, timezone

from assistant.news import Headline, dedupe, headline_score, mentions, net_score, sanitize

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "assistant"


def H(t):
    return Headline(t, "src", "https://x", datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_keyword_scoring(cfg):
    assert headline_score("HAL bags order win worth 5,000 crore, record quarter", cfg) == 3  # bags order, order win, record
    assert headline_score("SEBI probe into fraud allegations; rating downgrade", cfg) == -3
    assert net_score([H("approval and partnership and expansion and record contract")] * 2, cfg) == 3
    assert net_score([H("fraud probe ban penalty default")], cfg) == -3


def test_mentions():
    assert mentions(H("HAL shares jump on order"), "Hindustan Aeronautics", "HAL")
    assert not mentions(H("Halving of tax"), "Hindustan Aeronautics", "HAL")
    assert mentions(H("Kotak Bank rallies"), "Kotak Mahindra Bank", "KOTAKBANK", ("Kotak",))
    assert not mentions(H("Adroit Industries share price"), "Samvardhana Motherson", "MOTHERSON", ("Motherson",))


def test_sanitize_and_dedupe():
    assert sanitize("a\x00b​  c" + "x" * 500, 20) == "ab c" + "x" * 16
    assert len(dedupe([H("Same title!"), H("same   title")])) == 1


def test_no_regular_order_code():
    """No regular-order API anywhere. The only write path to Kite is GTT, in kite.py only."""
    forbidden = re.compile(r"kiteconnect|place_order|modify_order|orders/regular|orders/amo|orders/co", re.I)
    for p in SRC.glob("*.py"):
        text = p.read_text()
        if p.name == "cli.py":  # selftest contains the search strings themselves
            text = text.replace('("kiteconnect", "place_order", "orders/regular")', "")
        assert not forbidden.search(text), f"order-placement reference in {p.name}"
        if p.name != "kite.py":
            assert "api.kite.trade" not in text, f"Kite API called outside kite.py: {p.name}"
    kite = (SRC / "kite.py").read_text()
    assert not re.search(r"/orders\b", kite), "Kite /orders endpoint referenced"
    # the ordering logic may only use the Kite client, never raw HTTP or the private request method
    orders = (SRC / "orders.py").read_text()
    assert "requests" not in orders and "_req(" not in orders
    writes = re.findall(r'_req\("(POST|PUT|DELETE)", f?"([^"]+)"', kite)
    assert {path for _, path in writes} <= {"/gtt/triggers", "/gtt/triggers/{int(trigger_id)}"}, writes
    assert '"product": "CNC"' in kite and '"order_type": "LIMIT"' in kite


def test_gtt_value_cap_and_parsing():
    import pytest
    from assistant.kite import GTTOrder, KiteError, parse_request_token, tick_round
    with pytest.raises(KiteError):
        GTTOrder("HAL", "BUY", 2, 4000, 4000, 4100).validate()  # 8000 > cap
    GTTOrder("PNB", "BUY", 1, 100, 100, 126).validate()
    with pytest.raises(KiteError):
        GTTOrder("PNB; DROP", "BUY", 1, 100, 100, 126).validate()
    assert tick_round(101.23) == 101.25
    assert parse_request_token("http://127.0.0.1:8765/kite?action=login&type=login&status=success"
                               "&request_token=AbC123xyzAbC123xyzAbC12") == "AbC123xyzAbC123xyzAbC12"
    assert parse_request_token("hello") is None
