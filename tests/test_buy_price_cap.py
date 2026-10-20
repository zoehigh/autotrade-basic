"""
BUY LOC 1.19 상한 보정 + 추가매수 계단식 재배치 테스트.

실측 배경 (2026-10-20): SOXL 후반전에서 정상매수 $118.10 과 추가매수 1단계 $115.84 가
현재가×1.19 상한($111.51)으로 함께 낮춰지면서 두 주문이 같은 가격에 뭉쳤습니다.
"""
from unittest.mock import MagicMock

import pytest

from strategy import (
    BUY_LOC_PRICE_CAP_RATIO,
    build_additional_loc_prices,
    cap_buy_loc_price,
    무한매수법_V4,
)


class _FakeQuotation:
    tradable = True


class _FakePrice:
    def __init__(self, last):
        self.open = last
        self.last = last


class _FakeBalance:
    def __init__(self, qty, avg):
        self.quantity = qty
        self.avg_price = avg


class _FakePurchaseAmount:
    def __init__(self, cash):
        self.orderable_cash = cash


def _make_broker_mock(last_price, position_qty, avg_price, orderable_cash):
    broker = MagicMock()
    broker.get_stock_quotation.return_value = _FakeQuotation()
    broker.get_stock_price.return_value = _FakePrice(last_price)
    broker.get_balance.return_value = _FakeBalance(position_qty, avg_price)
    broker.get_purchase_amount.return_value = _FakePurchaseAmount(orderable_cash)
    return broker


# ─────────────────────────────────────────────────────────
# cap_buy_loc_price
# ─────────────────────────────────────────────────────────

def test_cap_buy_loc_price_uses_last_price_ratio():
    price, capped = cap_buy_loc_price(118.10, 93.71)
    assert capped is True
    assert price == 111.51  # 93.71 × 1.19 = 111.5149 → 버림


def test_cap_buy_loc_price_keeps_price_below_cap():
    price, capped = cap_buy_loc_price(73.73, 70.39)
    assert capped is False
    assert price == 73.73


def test_cap_buy_loc_price_without_last_price_is_noop():
    price, capped = cap_buy_loc_price(118.10, 0)
    assert capped is False
    assert price == 118.10


# ─────────────────────────────────────────────────────────
# build_additional_loc_prices
# ─────────────────────────────────────────────────────────

def test_additional_prices_skip_candidates_above_ceiling():
    """상한(111.51) 이상인 후보는 건너뛰고 그 아래부터 계단식으로 배치합니다."""
    unit_amount = 579.200141616057
    result = build_additional_loc_prices(unit_amount, 4, 3, 111.51)

    assert [price for _level, price in result] == [96.53, 82.74, 72.40]
    assert [level for level, _price in result] == [1, 2, 3]


def test_additional_prices_are_strictly_decreasing():
    result = build_additional_loc_prices(579.20, 4, 3, 111.51)
    prices = [price for _level, price in result]
    assert prices == sorted(prices, reverse=True)
    assert len(set(prices)) == len(prices)


def test_additional_prices_without_cap_keep_original_levels():
    """캡이 걸리지 않으면 기존 i=1,2,3 배치가 그대로 유지됩니다."""
    unit_amount = 579.200141616057
    result = build_additional_loc_prices(unit_amount, 4, 3, 118.10)
    assert [price for _level, price in result] == [115.84, 96.53, 82.74]


def test_additional_prices_respects_requested_level_count():
    result = build_additional_loc_prices(579.20, 4, 2, 111.51)
    assert len(result) == 2


# ─────────────────────────────────────────────────────────
# 전략 통합 — SOXL 후반전 실제 사례
# ─────────────────────────────────────────────────────────

def _soxl_late_half_orders(monkeypatch, last_price=93.71):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 12.6562,
        "net_invested": 5373.93,
        "net_invested_status": "valid",
        "reverse_mode": {},
        "close_prices": [128.4, 127.57, 110.54, 107.35, 93.67],
    }
    broker = _make_broker_mock(
        last_price=last_price, position_qty=44, avg_price=124.7454,
        orderable_cash=4253.53,
    )
    return 무한매수법_V4(
        broker, symbol="SOXL", exchange_code="AMS", splits=20, symbol_type="SOXL",
        seed=9627.46, T=12.6562, state=state,
    )


def test_soxl_late_half_normal_buy_is_capped(monkeypatch):
    result = _soxl_late_half_orders(monkeypatch)
    normal = [o for o in result["orders"]
              if o["side"] == "BUY" and "[추가매수]" not in o["comment"]]

    assert len(normal) == 1
    assert normal[0]["price"] == 111.51
    assert normal[0]["quantity"] == 4
    assert result["price_cap_notes"], "가격 보정 기록이 있어야 합니다"


def test_soxl_late_half_additional_ladder_has_no_duplicate(monkeypatch):
    result = _soxl_late_half_orders(monkeypatch)
    additional = [o for o in result["orders"] if "[추가매수]" in o["comment"]]

    assert [o["price"] for o in additional] == [96.53, 82.74, 72.40]
    assert all(o["quantity"] == 1 for o in additional)
    assert all(o["t_target"] == 0.0 for o in additional)


def test_soxl_late_half_additional_is_below_normal_buy(monkeypatch):
    result = _soxl_late_half_orders(monkeypatch)
    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    prices = [o["price"] for o in buys]

    assert len(set(prices)) == len(prices), "가격이 중복되면 안 됩니다"
    assert prices == sorted(prices, reverse=True), "가격이 계단식으로 내려가야 합니다"


def test_soxl_late_half_sell_orders_unchanged(monkeypatch):
    result = _soxl_late_half_orders(monkeypatch)
    sells = [o for o in result["orders"] if o["side"] == "SELL"]

    assert [(o["quantity"], o["price"]) for o in sells] == [
        (11, 118.11),
        (33, 149.69),
    ]


def test_orders_without_cap_are_unchanged(monkeypatch):
    """캡이 걸리지 않는 가격에서는 주문이 이전과 동일합니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 12.375,
        "net_invested": 5615.60,
        "net_invested_status": "valid",
        "reverse_mode": {},
        "close_prices": [79.61, 78.81, 74.12, 73.82, 68.21],
    }
    broker = _make_broker_mock(
        last_price=70.39, position_qty=74, avg_price=76.4687,
        orderable_cash=4080.58,
    )
    result = 무한매수법_V4(
        broker, symbol="TQQQ", exchange_code="NAS", splits=20, symbol_type="TQQQ",
        seed=9696.18, T=12.375, state=state,
    )

    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    assert [o["price"] for o in buys] == [73.73, 66.89, 59.46, 53.51]
    assert result["price_cap_notes"] == []
