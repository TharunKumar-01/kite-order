from datetime import datetime, timedelta

import pytest

from assistant.kite import KiteError
from assistant.orders import (OrderError, OrderRequest, build_plan, cancel_order, confirm, create_draft,
                              draft_text, parse_order, set_ordering, sync)
from assistant.util import now_ist


class FakeKite:
    def __init__(self, held=0):
        self.held, self.day_price = held, None
        self.gtt_state: dict[int, dict] = {}
        self.placed, self.deleted = [], []
        self.next_id = 100
        self.reject_sell = False

    def held_qty(self, symbol):
        return self.held, self.day_price

    def place_gtt(self, o):
        o.validate()
        if o.side == "SELL" and self.reject_sell:
            raise KiteError("InputException: nope")
        self.next_id += 1
        self.placed.append((self.next_id, o.side, o.symbol, o.qty, o.trigger, o.limit))
        self.gtt_state[self.next_id] = {"status": "active", "orders": [{}]}
        return self.next_id

    def place_oco(self, symbol, qty, sl_trigger, sl_limit, tgt_trigger, tgt_limit, last_price, tick=0.05):
        if self.reject_sell:
            raise KiteError("InputException: nope")
        assert sl_trigger < tgt_trigger and sl_limit <= sl_trigger and tgt_limit <= tgt_trigger
        self.next_id += 1
        self.placed.append((self.next_id, "OCO", symbol, qty, (sl_trigger, tgt_trigger), (sl_limit, tgt_limit)))
        self.gtt_state[self.next_id] = {"status": "active", "orders": [{}, {}]}
        return self.next_id

    def holding(self, symbol):
        return self.held, 100.0, 100.0

    def gtt(self, tid):
        return self.gtt_state[tid]

    def delete_gtt(self, tid):
        assert tid in self.gtt_state, "deleted a GTT the bot didn't create"
        self.deleted.append(tid)
        self.gtt_state[tid]["status"] = "deleted"

    def sold_price_today(self, symbol):
        return None


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,exp", [
    ("buy HAL 2 @4210 sl 3950", dict(symbol="HAL", qty=2, price=4210, sl_price=3950)),
    ("Buy hal ₹2500 @now sl 6%", dict(symbol="HAL", amount=2500, price=None, sl_pct=6)),
    ("buy HAL rs 2,500 at 4210", dict(symbol="HAL", amount=2500, price=4210)),
    ("buy HAL 2.5k market", None),                    # amount needs ₹/rs
    ("buy HAL ₹2.5k market", dict(symbol="HAL", amount=2500, price=None)),
    ("buy ₹2500", dict(symbol=None, amount=2500, price=None)),
    ("buy M&M 1 @ 3000 sl none", dict(symbol="M&M", qty=1, price=3000, no_sl=True)),
    ("buy PNB 10 shares @ 120.5", dict(symbol="PNB", qty=10, price=120.5)),
])
def test_parse_order(text, exp):
    if exp is None:
        with pytest.raises(OrderError):
            parse_order(text)
        return
    r = parse_order(text)
    for k, v in exp.items():
        assert getattr(r, k) == v, (k, getattr(r, k))


def test_parse_non_orders_and_errors():
    assert parse_order("bought HAL 1 4210") is None
    assert parse_order("hello") is None
    for bad in ["buy HAL", "buy HAL 2 ₹2500", "buy HAL 2 @abc", "buy HAL 2 sl", "buy HAL 0"]:
        with pytest.raises(OrderError):
            parse_order(bad)


# ---------------------------------------------------------------- planning
def test_plan_now_amount_and_cap(cfg):
    p = build_plan(OrderRequest(amount=2500), "PNB", 126.55, cfg)
    assert p.mode == "now" and p.trigger > 126.55 and p.limit > p.trigger
    assert p.qty == int(2500 // p.limit) and p.value <= cfg.kite.max_order_value
    assert p.sl_trigger == pytest.approx(p.limit * 0.94, abs=0.05) and p.sl_limit < p.sl_trigger
    with pytest.raises(OrderError):
        build_plan(OrderRequest(amount=5000), "PNB", 126.55, cfg)          # above cap
    with pytest.raises(OrderError):
        build_plan(OrderRequest(qty=1), "HAL", 4210, cfg)                   # 1 share > cap
    with pytest.raises(OrderError):
        build_plan(OrderRequest(amount=100), "MRF", 120000, cfg.model_copy(
            update={"kite": cfg.kite.model_copy(update={"max_order_value": 1e9})}))  # < 1 share


def test_plan_limit_prices_and_guards(cfg):
    below = build_plan(OrderRequest(qty=10, price=120), "PNB", 126.55, cfg)
    assert below.mode == "price" and below.trigger == below.limit == 120
    above = build_plan(OrderRequest(qty=10, price=127), "PNB", 126.55, cfg)
    assert above.trigger > 126.55 and above.limit == 127
    with pytest.raises(OrderError):
        build_plan(OrderRequest(qty=10, price=150), "PNB", 126.55, cfg)    # fat finger
    with pytest.raises(OrderError):
        build_plan(OrderRequest(qty=10, price=120, sl_price=121), "PNB", 126.55, cfg)  # SL above buy
    with pytest.raises(OrderError):
        build_plan(OrderRequest(qty=10, price=120, sl_pct=40), "PNB", 126.55, cfg)
    nosl = build_plan(OrderRequest(qty=10, price=120, no_sl=True), "PNB", 126.55, cfg)
    assert nosl.sl_trigger is None


# ---------------------------------------------------------------- confirm / sync
def _draft(conn, cfg, **kw):
    p = build_plan(OrderRequest(**({"qty": 10, "price": 120, "sl_pct": 5} | kw)), "PNB", 126.55, cfg)
    oid = create_draft(conn, p)
    assert "Confirm order" in draft_text(conn, oid, cfg)
    return oid


def test_confirm_places_buy_and_sl_once(conn, cfg):
    k = FakeKite()
    oid = _draft(conn, cfg)
    msg = confirm(conn, oid, k, cfg=cfg)
    assert "Buy trigger placed" in msg and "Stop-loss + target (OCO) placed" in msg
    assert [p[1] for p in k.placed] == ["BUY", "OCO"]
    assert "already" in confirm(conn, oid, k, cfg=cfg)        # double tap
    assert len(k.placed) == 2


def test_confirm_guards(conn, cfg):
    k = FakeKite()
    oid = _draft(conn, cfg)
    assert "expired" in confirm(conn, oid, k, now=now_ist() + timedelta(minutes=10), cfg=cfg)
    oid = _draft(conn, cfg)
    set_ordering(conn, False)
    assert "already cancelled" in confirm(conn, oid, k, cfg=cfg)       # kill switch voids drafts
    oid = _draft(conn, cfg)
    assert "switched off" in confirm(conn, oid, k, cfg=cfg)
    set_ordering(conn, True)
    for _ in range(cfg.kite.max_orders_per_day):
        assert "placed" in confirm(conn, _draft(conn, cfg), k, cfg=cfg)
    assert "Daily limit" in confirm(conn, _draft(conn, cfg), k, cfg=cfg)
    assert not any(p for p in k.placed if p[1] == "BUY" and p[3] * p[5] > cfg.kite.max_order_value)


def test_sl_deferred_when_already_held_then_placed_on_fill(conn, cfg):
    k = FakeKite(held=5)
    oid = _draft(conn, cfg)
    assert "once this buy fills" in confirm(conn, oid, k, cfg=cfg)
    assert [p[1] for p in k.placed] == ["BUY"]
    buy_id = k.placed[0][0]
    k.gtt_state[buy_id] = {"status": "triggered", "updated_at": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                           "orders": [{"result": {"order_result": {"status": "success"}}}]}
    t = now_ist().replace(hour=10, minute=0)
    msgs = sync(conn, k, t)
    assert any("fired" in m for m in msgs)
    k.held, k.day_price = 15, 119.9
    msgs = sync(conn, k, t)
    assert any("bought 10 @ ₹119.90" in m for m in msgs) and any("(OCO) placed" in m for m in msgs)
    assert conn.execute("SELECT side, qty, price FROM real_trades").fetchone()["price"] == 119.9


def test_unfilled_and_rejected_cleanup(conn, cfg):
    k = FakeKite()
    oid = _draft(conn, cfg)
    confirm(conn, oid, k, cfg=cfg)
    buy_id, sl_id = k.placed[0][0], k.placed[1][0]
    day = now_ist().strftime("%Y-%m-%d")
    k.gtt_state[buy_id] = {"status": "triggered", "updated_at": f"{day} 10:00:00",
                           "orders": [{"result": {"order_result": {"status": "success"}}}]}
    sync(conn, k, now_ist().replace(hour=11))
    msgs = sync(conn, k, now_ist().replace(hour=15, minute=40))
    assert any("didn't fill" in m for m in msgs) and sl_id in k.deleted

    oid = _draft(conn, cfg)
    confirm(conn, oid, k, cfg=cfg)
    buy2, sl2 = k.placed[2][0], k.placed[3][0]
    k.gtt_state[buy2] = {"status": "triggered", "updated_at": f"{day} 10:00:00",
                         "orders": [{"result": {"order_result": {"status": "REJECTED",
                                                                  "rejection_reason": "margin"}}}]}
    msgs = sync(conn, k, now_ist().replace(hour=11))
    assert any("rejected: margin" in m for m in msgs) and sl2 in k.deleted


def test_cancel_only_touches_own_triggers(conn, cfg):
    k = FakeKite()
    k.gtt_state[999] = {"status": "active"}                   # the user's own GTT
    oid = _draft(conn, cfg)
    confirm(conn, oid, k, cfg=cfg)
    assert "Cancelled order" in cancel_order(conn, oid, k)
    assert 999 not in k.deleted and len(k.deleted) == 2


def test_stop_loss_failure_is_reported(conn, cfg):
    k = FakeKite()
    k.reject_sell = True
    msg = confirm(conn, _draft(conn, cfg), k, cfg=cfg)
    assert "NOT accepted" in msg


# ---------------------------------------------------------------- target sells
from assistant.orders import SellRequest, build_sell_plan, parse_sell  # noqa: E402


class FakeSellKite(FakeKite):
    def __init__(self, held=12, avg=1119.0, last=1403.0):
        super().__init__(held)
        self.avg, self.last, self.sold = avg, last, (0, None)

    def holding(self, symbol):
        return self.held, self.avg, self.last

    def sold_today(self, symbol):
        return self.sold


def test_parse_sell():
    assert parse_sell("sell JPL all @1455") == SellRequest("JPL", None, 1455.0)
    assert parse_sell("Sell jpl 12 profit ₹4,000") == SellRequest("JPL", 12, profit=4000.0)
    assert parse_sell("sell JPL all target 5%") == SellRequest("JPL", None, pct=5.0)
    assert parse_sell("sell JPL 6 at 1460") == SellRequest("JPL", 6, 1460.0)
    assert parse_sell("sold JPL 1 1400") is None
    for bad in ["sell JPL @1455", "sell JPL all", "sell JPL all @1455 profit 10", "sell 12 @1455"]:
        with pytest.raises(OrderError):
            parse_sell(bad)


def test_sell_plan_profit_includes_charges(cfg):
    p = build_sell_plan(SellRequest("JPL", None, profit=4000), 12, 1119.0, 1403.0, cfg)
    assert p.qty == 12 and p.mode == "target"
    from assistant.costs import leg_costs
    net = lambda px: (px - 1119.0) * 12 - leg_costs(px * 12, "sell", cfg.costs).total  # noqa: E731
    assert net(p.limit) >= 4000 > net(p.limit - 0.05)          # keeps ₹4,000 after charges, lowest such price
    assert p.trigger > p.limit                                  # fires once already above -> fills
    p2 = build_sell_plan(SellRequest("JPL", 12, 1455.0), 12, 1119.0, 1403.0, cfg)
    assert p2.limit == 1455.0
    with pytest.raises(OrderError):
        build_sell_plan(SellRequest("JPL", 13, 1455.0), 12, 1119.0, 1403.0, cfg)   # more than held
    with pytest.raises(OrderError):
        build_sell_plan(SellRequest("JPL", None, 1390.0), 12, 1119.0, 1403.0, cfg)  # below market
    with pytest.raises(OrderError):
        build_sell_plan(SellRequest("JPL", None, 2500.0), 12, 1119.0, 1403.0, cfg)  # typo guard
    with pytest.raises(OrderError):
        build_sell_plan(SellRequest("XYZ", None, 100.0), 0, None, 90.0, cfg)        # not held


def test_sell_confirm_and_fill(conn, cfg):
    k = FakeSellKite()
    oid = create_draft(conn, build_sell_plan(SellRequest("JPL", None, 1455.0), 12, 1119.0, 1403.0, cfg))
    assert "target sell" in draft_text(conn, oid, cfg)
    assert "Target sell placed" in confirm(conn, oid, k, cfg=cfg)
    gid, side = k.placed[0][0], k.placed[0][1]
    assert side == "SELL" and len(k.placed) == 1
    day = now_ist().strftime("%Y-%m-%d")
    k.gtt_state[gid] = {"status": "triggered", "updated_at": f"{day} 10:00:00",
                        "orders": [{"result": {"order_result": {"status": "success"}}}]}
    k.sold = (12, 1455.2)
    msgs = sync(conn, k, now_ist().replace(hour=10, minute=5))
    assert any("sold 12 @ ₹1,455.20" in m for m in msgs)
    assert conn.execute("SELECT side FROM real_trades").fetchone()["side"] == "sell"
    # sells don't use up the daily buy limit
    for _ in range(cfg.kite.max_orders_per_day):
        assert "placed" in confirm(conn, _draft(conn, cfg), FakeKite(), cfg=cfg)


def test_sell_rechecks_holding_at_confirm(conn, cfg):
    k = FakeSellKite(held=5)
    oid = create_draft(conn, build_sell_plan(SellRequest("JPL", None, 1455.0), 12, 1119.0, 1403.0, cfg))
    assert "fewer than 12" in confirm(conn, oid, k, cfg=cfg) and not k.placed


def test_prices_respect_tick_size(cfg):
    from assistant.kite import band_tick, tick_round
    assert [band_tick(p) for p in (126, 600, 1403, 4980, 7000, 15000, 120000)] == [0.01, 0.05, 0.1, 0.1, 0.5, 1, 5]
    assert tick_round(1455.85, 0.1) in (1455.8, 1455.9) and tick_round(126.553, 0.01) == 126.55
    on_grid = lambda x, t: abs(round(x / t) * t - x) < 1e-6  # noqa: E731
    s = build_sell_plan(SellRequest("JPL", None, profit=4000), 12, 1119.0, 1403.0, cfg, tick=0.1)
    assert on_grid(s.limit, 0.1) and on_grid(s.trigger, 0.1) and s.trigger > s.limit
    b = build_plan(OrderRequest(amount=2500, sl_pct=6), "JPL", 1403.0, cfg.model_copy(
        update={"kite": cfg.kite.model_copy(update={"max_order_value": 5000})}), tick=0.1)
    assert all(on_grid(x, 0.1) for x in (b.trigger, b.limit, b.sl_trigger, b.sl_limit))
    m = build_plan(OrderRequest(qty=1, price=118000, no_sl=True), "MRF", 120000.0, cfg.model_copy(
        update={"kite": cfg.kite.model_copy(update={"max_order_value": 200000})}), tick=5)
    assert on_grid(m.trigger, 5) and on_grid(m.limit, 5)


def test_gtt_list_tags_bot_orders(conn, cfg):
    from assistant.orders import gtt_list_text
    k = FakeKite()
    confirm(conn, _draft(conn, cfg), k, cfg=cfg)
    buy_id, sl_id = k.placed[0][0], k.placed[1][0]

    def g(i, side, status="active", t="single"):
        return {"id": i, "type": t, "status": status, "created_at": "2026-11-07 12:00:00",
                "condition": {"tradingsymbol": "PNB", "trigger_values": [120], "last_price": 126.55},
                "orders": [{"transaction_type": side, "quantity": 10, "price": 120}]}
    gtts = [g(buy_id, "BUY"), g(sl_id, "SELL"), g(555, "SELL", t="two-leg"), g(777, "BUY", "triggered")]
    text = gtt_list_text(conn, gtts)
    assert "Active GTTs in Kite: 3" in text and "bot #" in text and "buy" in text and "stop-loss" in text
    assert "👤 yours" in text and "OCO" in text and "777" not in text
    assert "777" in gtt_list_text(conn, gtts, show_all=True)


def test_watch_gtts_reports_user_gtt_changes(conn):
    from assistant.orders import watch_gtts

    def g(i, status, result=None):
        return {"id": i, "status": status, "condition": {"tradingsymbol": "SILVERBEES", "trigger_values": [179]},
                "orders": [{"transaction_type": "SELL", "quantity": 324, "price": 185,
                            "result": result}]}
    assert watch_gtts(conn, [g(1, "active"), g(2, "active")]) == []            # baseline only
    assert watch_gtts(conn, [g(1, "active"), g(2, "active")]) == []            # no change
    msgs = watch_gtts(conn, [g(1, "triggered", {"order_result": {"status": "REJECTED",
                                                                  "rejection_reason": "no CDSL auth"}}),
                             g(3, "active")])
    text = "\n".join(msgs)
    assert "REJECTED: no CDSL auth" in text and "New GTT" in text and "gone from Kite" in text


def test_holdings_text():
    from assistant.orders import holdings_text
    h = [{"tradingsymbol": "JPL", "quantity": 12, "t1_quantity": 0, "average_price": 1119, "last_price": 1403,
          "day_change_percentage": 1.0},
         {"tradingsymbol": "ITC", "quantity": 5, "t1_quantity": 0, "average_price": 300, "last_price": 280,
          "day_change_percentage": -0.5}]
    gt = [{"status": "active", "condition": {"tradingsymbol": "JPL", "trigger_values": [1455.9], "last_price": 1403},
           "orders": [{"transaction_type": "SELL", "quantity": 12}]}]
    t = holdings_text(h, gt)
    assert "Holdings: 2" in t and "+₹3,408" in t and "🎯 12 @ ₹1,455.90" in t and "no sell GTT" in t


# ---------------------------------------------------------------- same-day expiry of buy triggers
def test_buy_expiry_day(cfg):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from assistant.orders import buy_expiry
    ist = ZoneInfo("Asia/Kolkata")
    assert buy_expiry(datetime(2026, 11, 11, 10, 0, tzinfo=ist), cfg) == (date(2026, 11, 11), "15:30")
    assert buy_expiry(datetime(2026, 11, 11, 16, 0, tzinfo=ist), cfg) == (date(2026, 11, 12), "15:30")
    assert buy_expiry(datetime(2026, 11, 13, 18, 0, tzinfo=ist), cfg)[0] == date(2026, 11, 16)  # Fri eve -> Mon
    assert buy_expiry(datetime(2026, 11, 14, 11, 0, tzinfo=ist), cfg)[0] == date(2026, 11, 16)  # Saturday
    assert buy_expiry(datetime(2026, 11, 9, 20, 0, tzinfo=ist), cfg)[0] == date(2026, 11, 11)   # 10 Nov holiday


def test_unfired_buy_and_its_sl_are_deleted_after_cutoff(conn, cfg):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    k = FakeKite()
    oid = _draft(conn, cfg)
    confirm(conn, oid, k, cfg=cfg)
    buy_id, sl_id = k.placed[0][0], k.placed[1][0]
    conn.execute("UPDATE kite_orders SET confirmed_at=? WHERE id=?",
                 (datetime(2026, 11, 11, 10, 0, tzinfo=ist).isoformat(), oid))
    conn.commit()
    assert sync(conn, k, datetime(2026, 11, 11, 15, 0, tzinfo=ist)) == [] and not k.deleted
    msgs = sync(conn, k, datetime(2026, 11, 11, 15, 31, tzinfo=ist))
    assert any("didn't fire by 15:30" in m for m in msgs)
    assert set(k.deleted) == {buy_id, sl_id}
    row = conn.execute("SELECT status, sl_status FROM kite_orders WHERE id=?", (oid,)).fetchone()
    assert (row["status"], row["sl_status"]) == ("expired", "deleted")
    assert sync(conn, k, datetime(2026, 11, 11, 15, 40, tzinfo=ist)) == []     # nothing repeated


def test_filled_buy_keeps_its_stop_loss_after_cutoff(conn, cfg):
    """Expiry only removes buys that never fired. A buy that fired and filled keeps its stop-loss."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    k = FakeKite()
    oid = _draft(conn, cfg)
    confirm(conn, oid, k, cfg=cfg)
    buy_id, sl_id = k.placed[0][0], k.placed[1][0]
    conn.execute("UPDATE kite_orders SET confirmed_at=? WHERE id=?",
                 (datetime(2026, 11, 11, 9, 15, tzinfo=ist).isoformat(), oid))
    conn.commit()
    k.gtt_state[buy_id] = {"status": "triggered", "updated_at": "2026-11-11 09:20:00",
                           "orders": [{"result": {"order_result": {"status": "success"}}}]}
    sync(conn, k, datetime(2026, 11, 11, 9, 25, tzinfo=ist))        # fired
    k.held = 13                                                      # and filled
    sync(conn, k, datetime(2026, 11, 11, 9, 30, tzinfo=ist))
    for t in ((15, 31), (16, 0)):
        sync(conn, k, datetime(2026, 11, 11, *t, tzinfo=ist))
    sync(conn, k, datetime(2026, 11, 12, 10, 0, tzinfo=ist))         # next day too
    row = conn.execute("SELECT status, sl_status FROM kite_orders WHERE id=?", (oid,)).fetchone()
    assert (row["status"], row["sl_status"]) == ("filled", "active")
    assert sl_id not in k.deleted and k.gtt_state[sl_id]["status"] == "active"



# ---------------------------------------------------------------- OCO: stop-loss + target in one GTT
def test_parse_target():
    assert parse_order("buy HAL ₹2500 @now sl 6% tp 20%").tp_pct == 20
    assert parse_order("buy HAL 2 @4210 tp 5000").tp_price == 5000
    assert parse_order("buy HAL ₹2500 tp none").no_tp


def test_plan_target_default_explicit_and_none(cfg):
    cfg = cfg.model_copy(update={"kite": cfg.kite.model_copy(update={"target_mode": "fixed",
                                                                     "default_target_pct": 25.0})})
    p = build_plan(parse_order("buy PNB ₹2500 @120"), "PNB", 132.0, cfg, tick=0.01)
    assert p.tgt_pct == cfg.kite.default_target_pct
    assert p.tgt_limit == pytest.approx(120 * (1 + cfg.kite.default_target_pct / 100), abs=0.01)
    assert p.tgt_trigger > p.tgt_limit                        # fires above the limit, so the sell fills
    p = build_plan(parse_order("buy PNB ₹2500 @120 tp 20%"), "PNB", 132.0, cfg, tick=0.01)
    assert p.tgt_limit == pytest.approx(144.0, abs=0.01)
    with pytest.raises(OrderError):                           # 10% of 120 = 132, not above the market
        build_plan(parse_order("buy PNB ₹2500 @120 tp 10%"), "PNB", 132.0, cfg, tick=0.01)
    p = build_plan(parse_order("buy PNB ₹2500 @120 tp none"), "PNB", 132.0, cfg, tick=0.01)
    assert p.tgt_trigger is None
    with pytest.raises(OrderError):                           # target below the market
        build_plan(parse_order("buy PNB ₹2500 @120 tp 125"), "PNB", 132.0, cfg, tick=0.01)


def test_sl_only_and_target_only_use_single_gtts(conn, cfg):
    k = FakeKite()
    confirm(conn, _draft(conn, cfg, sl_price=114, no_tp=True), k, cfg=cfg)
    confirm(conn, _draft(conn, cfg, no_sl=True, tp_pct=20), k, cfg=cfg)
    assert [p[1] for p in k.placed] == ["BUY", "SELL", "BUY", "SELL"]
    assert k.placed[3][4] > 140                               # the target-only sell is above the market


def test_oco_fire_reports_which_leg(conn, cfg):
    k = FakeKite()
    for leg, status, icon in ((1, "target", "🎯"), (0, "stopped", "🛑")):
        oid = _draft(conn, cfg)
        confirm(conn, oid, k, cfg=cfg)
        buy_id, oco_id = k.placed[-2][0], k.placed[-1][0]
        conn.execute("UPDATE kite_orders SET status='filled', fill_price=120 WHERE id=?", (oid,))
        conn.commit()
        legs = [{}, {}]
        legs[leg] = {"result": {"order_result": {"status": "success"}}}
        k.gtt_state[oco_id] = {"status": "triggered", "orders": legs}
        msgs = sync(conn, k, now_ist().replace(hour=11))
        assert any(m.startswith(icon) for m in msgs), msgs
        assert conn.execute("SELECT status FROM kite_orders WHERE id=?", (oid,)).fetchone()["status"] == status


def test_upgrade_filled_stop_loss_to_oco(conn, cfg, monkeypatch):
    from assistant.orders import oco_preview, upgrade_to_oco
    k = FakeKite()
    oid = _draft(conn, cfg, sl_price=114, no_tp=True)
    confirm(conn, oid, k, cfg=cfg)
    old_sl = k.placed[-1][0]
    conn.execute("UPDATE kite_orders SET status='filled', fill_price=119.5 WHERE id=?", (oid,))
    conn.commit()
    k.held = 10
    import assistant.targets as targets
    stats = targets.TargetStats("PNB", 40, 12.5, 0.5, 0.4, 0.3, 0.3, 5, 1000)
    monkeypatch.setattr(targets, "suggest_target", lambda *a, **kw: stats)
    text, pct = oco_preview(conn, oid, k)
    assert "OCO" in text and pct == 12.5 and "Invested" in text and "you get" in text and "40%" in text
    msg = upgrade_to_oco(conn, oid, k, pct)
    assert "OCO GTT" in msg and old_sl in k.deleted
    assert k.placed[-1][1] == "OCO"                           # new one placed before the old was deleted
    row = conn.execute("SELECT sl_gtt_id, tgt_trigger FROM kite_orders WHERE id=?", (oid,)).fetchone()
    assert row["sl_gtt_id"] == k.placed[-1][0] and row["tgt_trigger"] > 119.5
    with pytest.raises(OrderError):
        oco_preview(conn, oid, k)                             # already has a target


def test_auto_target_uses_stock_history_and_falls_back(cfg):
    from assistant.targets import TargetStats
    stats = TargetStats("PNB", 40, 15.0, 0.5, 0.42, 0.25, 0.33, 5, 1000)
    p = build_plan(parse_order("buy PNB ₹2500 @120"), "PNB", 121.0, cfg, tick=0.01, target=stats)
    assert p.tgt_pct == 15.0 and p.tgt_limit == pytest.approx(138.0, abs=0.01)
    assert "target first 42%" in p.notes[0]
    p = build_plan(parse_order("buy PNB ₹2500 @120"), "PNB", 121.0, cfg, tick=0.01, target=None)
    assert p.tgt_pct == cfg.kite.default_target_pct and "not enough price history" in p.notes[-1]
    p = build_plan(parse_order("buy PNB ₹2500 @120 tp 20%"), "PNB", 121.0, cfg, tick=0.01, target=stats)
    assert p.tgt_pct == 20                                    # typed target wins over the per-stock one


def test_target_outcomes_order_of_hits():
    import numpy as np
    from assistant.targets import outcomes
    close = np.array([100, 100, 100, 100, 100, 100.0])
    high = np.array([100, 111, 100, 100, 100, 100.0])        # day 1: +11%
    low = np.array([100, 100, 100, 93, 100, 100.0])          # day 3: -7%
    reach, tf, sf, ne, n = outcomes(close, high, low, 2, 10, 6)
    # starts 0..3: s0 sees day1 (+) -> target; s1 sees days2,3 -> stop; s2 sees 3,4 -> stop; s3 sees 4,5 -> neither
    assert n == 4 and tf == 0.25 and sf == 0.5 and ne == 0.25 and reach == 0.25


def test_draft_shows_invested_and_sale_values(conn, cfg):
    text = draft_text(conn, _draft(conn, cfg, sl_price=114, tp_pct=20), cfg)
    assert "Invested" in text and text.count("you get") >= 2
