"""리버스모드 2일차 이후 쿼터매수 아래 계단식 추가매수(원문 주문표) 검증.

원문 PDF 는 이 저장소에 없어 사용자 제공 요약(쿼터매수 11주·$544.47 → 첫 추가매수 $544.47÷12 = $45.37)을
예시값으로만 사용합니다. 정책 단계 수(원문 8단계 vs 기본 3단계)는 additional_loc_levels 설정입니다.
"""
import pytest

from strategy import _build_reverse_additional_buys, execute_reverse_mode
from state import reconcile_reverse_fills

_MARK = "[추가매수]"


def _patch_star(monkeypatch, star=50.0):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    monkeypatch.setattr("strategy._confirmed_prev_close", lambda *a, **k: (star, "2026-11-10"))


def _day2_state(T=19.9):
    return {"T": T, "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C1"},
            "close_prices": [50.0]}


def test_helper_matches_original_order_table_example():
    """원문 예시: 쿼터매수 $544.47, 기본 11주 → 첫 추가매수 $45.37 (÷12)."""
    extra = _build_reverse_additional_buys(544.47, 11, 49.99, 10_000.0, 8, 2, 20.0)
    assert len(extra) == 8
    assert extra[0]["price"] == 45.37
    prices = [o["price"] for o in extra]
    assert prices == sorted(prices, reverse=True) and len(set(prices)) == 8
    assert all(p < 49.99 for p in prices)
    assert all(o["quantity"] == 1 and o["order_type"] == "LOC" and o["side"] == "BUY" for o in extra)


def test_helper_orders_do_not_move_t_but_are_tracked_as_reverse_buys():
    (o,) = _build_reverse_additional_buys(250.0, 5, 49.99, 1000.0, 1, 2, 19.9)
    assert _MARK in o["comment"]
    assert o["reverse_action"] == "buy" and o["reverse_t_target"] == 0.0 and o["t_target"] == 0.0
    assert o["reverse_day"] == 2 and o["reverse_base_t"] == 19.9


def test_helper_respects_cash_limit_and_zero_levels():
    # 쿼터매수 2주×49.99 = 99.98 → 추가 33.33 을 더하면 120 초과 → 건너뜀
    assert _build_reverse_additional_buys(100.0, 2, 49.99, 120.0, 3, 2, 20.0) == []
    assert _build_reverse_additional_buys(100.0, 2, 49.99, 1000.0, 0, 2, 20.0) == []
    assert _build_reverse_additional_buys(100.0, 0, 49.99, 1000.0, 3, 2, 20.0) == []


def test_day2_creates_sell_quarter_buy_and_ladder(monkeypatch):
    _patch_star(monkeypatch)
    state = _day2_state()
    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL", position_qty=100, avg_price=100.0,
        orderable_cash=1000.0, last_price=50.0, state=state, additional_loc_levels=3,
    )
    orders = result["orders"]
    sells = [o for o in orders if o["side"] == "SELL"]
    quarter = [o for o in orders if o["side"] == "BUY" and _MARK not in o["comment"]]
    extra = [o for o in orders if o["side"] == "BUY" and _MARK in o["comment"]]
    assert len(sells) == 1 and len(quarter) == 1 and len(extra) == 3
    assert quarter[0]["quantity"] == 5 and quarter[0]["price"] == 49.99
    assert [o["price"] for o in extra] == [41.66, 35.71, 31.25]   # 250 ÷ (5+i)
    # 추가매수는 T 계산(new_T)에 영향을 주지 않는다: 매도 ×0.9 → 쿼터매수 +(20−T)×0.25 만 반영
    t_sell = round(19.9 * 0.9, 4)
    assert result["new_T"] == round(t_sell + (20 - t_sell) * 0.25, 4)


def test_exhausted_day_has_no_ladder(monkeypatch):
    _patch_star(monkeypatch)
    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL", position_qty=100, avg_price=100.0,
        orderable_cash=100.0, last_price=50.0, state=_day2_state(), additional_loc_levels=3,
    )
    assert [(o["side"], o["order_type"]) for o in result["orders"]] == [("SELL", "MOC")]


def test_first_day_has_no_buy_at_all(monkeypatch):
    _patch_star(monkeypatch)
    state = {"T": 19.9, "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C1"},
             "close_prices": [50.0]}
    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL", position_qty=100, avg_price=100.0,
        orderable_cash=1000.0, last_price=50.0, state=state, additional_loc_levels=3,
    )
    assert [o["side"] for o in result["orders"]] == ["SELL"]


def test_levels_zero_restores_previous_behavior(monkeypatch):
    _patch_star(monkeypatch)
    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL", position_qty=100, avg_price=100.0,
        orderable_cash=1000.0, last_price=50.0, state=_day2_state(), additional_loc_levels=0,
    )
    assert sorted(o["side"] for o in result["orders"]) == ["BUY", "SELL"]


def test_additional_buy_fill_adds_net_invested_but_not_t():
    state = {
        "T": 18.0, "net_invested": 1000.0,
        "reverse_mode": {"active": True, "day_count": 2, "cycle_id": "C2"},
        "orders_meta": {
            "RADD": {
                "side": "BUY", "total_qty": 1, "processed_filled_qty": 0,
                "processed_filled_amount": 0.0, "fill_marker_version": 1,
                "reverse_action": "buy", "reverse_day": 2, "reverse_base_t": 18.0,
                "reverse_t_target": 0.0, "is_additional": True, "t_target": 0.0,
                "cycle_id": "C2",
            },
        },
    }
    history = [{
        "odno": "RADD", "ord_datetime_utc": "2026-08-02T20:00:01+00:00",
        "ft_ccld_qty": "1", "ft_ccld_unpr3": "41.66", "ft_ccld_amt3": "41.66", "nccs_qty": "0",
    }]
    reconcile_reverse_fills(state, history)
    assert state["T"] == 18.0, "추가매수 체결은 T 를 올리지 않는다"
    assert state["net_invested"] == pytest.approx(1041.66)
    assert state["reverse_mode"]["day_count"] == 2
    assert state["orders_meta"]["RADD"]["terminal"] is True


def test_default_reverse_levels_is_eight(monkeypatch):
    """원문 주문표 기준 기본 8단계 (일반모드 기본 3단계와 별개)."""
    _patch_star(monkeypatch)
    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL", position_qty=100, avg_price=100.0,
        orderable_cash=10_000.0, last_price=50.0, state=_day2_state(),
    )
    extra = [o for o in result["orders"] if _MARK in o["comment"]]
    assert len(extra) == 8


def test_config_additional_levels_is_shared_and_defaults_to_eight(monkeypatch):
    """일반모드와 리버스모드가 같은 {SYMBOL}_ADDITIONAL_LOC_LEVELS 를 쓰고 기본값은 8."""
    import config
    monkeypatch.setenv("TQQQ_SEED", "1000"); monkeypatch.setenv("SOXL_SEED", "1000")
    monkeypatch.delenv("SOXL_ADDITIONAL_LOC_LEVELS", raising=False)
    monkeypatch.delenv("ADDITIONAL_LOC_LEVELS", raising=False)
    cfg = {c["symbol"]: c for c in config._parse_symbols()}
    assert cfg["SOXL"]["additional_loc_levels"] == 8
    assert "reverse_additional_loc_levels" not in cfg["SOXL"]
    monkeypatch.setenv("SOXL_ADDITIONAL_LOC_LEVELS", "0")
    cfg = {c["symbol"]: c for c in config._parse_symbols()}
    assert cfg["SOXL"]["additional_loc_levels"] == 0
