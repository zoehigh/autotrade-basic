"""
net_invested(순투입) 시드 캡 유닛 테스트

배경:
  - 기존 시드 캡(remaining_seed = seed - position_qty*avg_price)은 평단 이하 매도(손절) 시
    매도 회수액이 원가보다 작아 원가 기준으로 시드 여유가 부풀려지는 문제가 있었습니다.
  - 이를 실제 투입된 순투입 금액(net_invested = Σ 매수체결금액 - Σ 매도체결금액)으로
    대체해, 누적 투입이 시드를 초과하지 못하도록 합니다.

테스트 대상:
  - _compute_non_reverse_net_invested 재계산 (손절/전량매도/리버스 제외/cutoff)
  - _apply_recent_history_dt 증분 누적 (일반모드 매수/매도/전량매도)
  - _infer_T_from_full_history 재계산 (초기모드)
  - net_invested 마이그레이션 백필 (이중 가산 없음)
  - reconcile_reverse_fills 델타 반영 (리버스 매도/매수)
  - strategy 무한매수법_V4 시드 캡 적용
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from copy import deepcopy
from unittest.mock import MagicMock

from state import (
    reconcile_reverse_fills,
    update_T_from_history,
    _compute_non_reverse_net_invested,
)


# ─────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────

def _make_state(T=0.0, last_updated="", net_invested=0.0, orders_meta=None):
    return {
        "T": T,
        "last_updated": last_updated,
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "net_invested": net_invested,
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": orders_meta or {},
        "balance_mismatch": {},
        "reverse_mode": {},
        "state_version": "v2",
    }


def _make_buy_order(odno, ord_dt, qty, amt, utc_dt=None):
    """체결 완료된 매수 주문 (ft_ccld_amt3 포함)."""
    if utc_dt is None:
        utc_dt = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T10:00:00+00:00"
    return {
        "odno": odno,
        "ord_dt": ord_dt,
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": str(qty),
        "ft_ccld_amt3": f"{amt:.2f}",
        "ord_datetime_utc": utc_dt,
    }


def _make_sell_order(odno, ord_dt, qty, amt, utc_dt=None):
    """체결 완료된 매도 주문 (ft_ccld_amt3 포함)."""
    if utc_dt is None:
        utc_dt = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T15:00:00+00:00"
    return {
        "odno": odno,
        "ord_dt": ord_dt,
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": str(qty),
        "ft_ccld_amt3": f"{amt:.2f}",
        "ord_datetime_utc": utc_dt,
    }


# ─────────────────────────────────────────────────────────
# _compute_non_reverse_net_invested
# ─────────────────────────────────────────────────────────

def test_compute_net_invested_basic_buys_minus_sells():
    state = _make_state()
    orders = [
        _make_buy_order("B1", "20260520", 10, 5000.0),
        _make_buy_order("B2", "20260521", 10, 5000.0),
        _make_sell_order("S1", "20260522", 4, 2400.0),   # 쿼터매도
    ]
    # 순투입 = 10000 - 2400 = 7600
    assert _compute_non_reverse_net_invested(state, orders) == 7600.0


def test_compute_net_invested_loss_sell_keeps_invested():
    """손절(평단 이하 매도) 시 순투입이 원가보다 크게 유지됨을 검증.

    100주 @ $50 매수($5000) 후 50주 @ $30 매도($1500, 손절) → 순투입 $3500.
    원가 기준(position_qty*avg)으로는 남은 포지션이 $2333로 집계되어
    시드 여유가 부풀어지지만, 순투입 $3500으로 정확히 집계됩니다.
    """
    state = _make_state()
    orders = [
        _make_buy_order("B1", "20260520", 100, 5000.0),
        _make_sell_order("S1", "20260522", 50, 1500.0),  # 손절
    ]
    assert _compute_non_reverse_net_invested(state, orders) == 3500.0


def test_compute_net_invested_full_sell_resets_to_zero():
    state = _make_state()
    orders = [
        _make_buy_order("B1", "20260520", 100, 5000.0),
        _make_buy_order("B2", "20260521", 50, 2500.0),
        _make_sell_order("S1", "20260522", 150, 6000.0),  # 전량매도(사이클 종료)
        _make_buy_order("B3", "20260525", 20, 1000.0),    # 새 사이클
    ]
    assert _compute_non_reverse_net_invested(state, orders) == 1000.0


def test_compute_net_invested_skips_reverse_orders():
    state = _make_state(orders_meta={
        "RSELL": {"reverse_action": "sell", "submitted_at": "20260806041650"},
    })
    orders = [
        _make_buy_order("B1", "20260520", 100, 5000.0),
        {
            "odno": "RSELL",
            "ord_dt": "20260805",   # 미국 영업일 (KST 8/6 04:16 제출 → ET 8/5), 시각이 있는 실제 이력 형태
            "ord_tmd": "041650",
            "sll_buy_dvsn_cd_name": "매도",
            "ft_ccld_qty": "32",
            "ft_ccld_amt3": "3968.00",
            "ord_datetime_utc": "2026-08-06T20:00:00+00:00",
        },
    ]
    # 리버스 매도는 reconcile_reverse_fills가 반영하므로 제외 → 5000 유지
    assert _compute_non_reverse_net_invested(state, orders) == 5000.0


def test_compute_net_invested_empty_returns_none():
    state = _make_state()
    assert _compute_non_reverse_net_invested(state, []) is None


def test_compute_net_invested_cutoff_excludes_recent():
    state = _make_state()
    orders = [
        _make_buy_order("B1", "20260526", 10, 10000.0, utc_dt="2026-05-26T10:00:00+00:00"),
        _make_buy_order("B2", "20260528", 10, 5000.0, utc_dt="2026-05-28T10:00:00+00:00"),
    ]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cutoff = datetime(2026, 5, 26, 15, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert _compute_non_reverse_net_invested(state, orders, cutoff_dt=cutoff) == 10000.0


# ─────────────────────────────────────────────────────────
# update_T_from_history 일반모드 (_apply 증분 누적)
# ─────────────────────────────────────────────────────────

def test_apply_recent_buy_and_sell_updates_net_invested():
    state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00", net_invested=10000.0)
    orders = [
        _make_buy_order("B1", "20260528", 10, 5000.0),
        _make_sell_order("S1", "20260529", 2, 600.0),  # 쿼터매도(보유수량 불명 분기)
    ]
    result = update_T_from_history("SOXL", state, orders)
    # 10000 + 5000 - 600 = 14400
    assert result["net_invested"] == 14400.0


def test_apply_recent_full_sell_resets_net_invested():
    """이력(net_qty) 기준 전량매도 → 순투입 0 리셋."""
    state = _make_state(T=4.0, last_updated="2026-05-27 00:00:00", net_invested=10000.0)
    orders = [
        _make_buy_order("B1", "20260520", 10, 5000.0),  # last_updated 이전 (net_qty용)
        _make_buy_order("B2", "20260521", 10, 5000.0),  # last_updated 이전
        _make_sell_order("S1", "20260528", 20, 12000.0),  # 비율 20/20 → 전량매도
    ]
    result = update_T_from_history("SOXL", state, orders)
    assert result["net_invested"] == 0.0
    assert result["T"] == 0.0


# ─────────────────────────────────────────────────────────
# 마이그레이션 백필 (이중 가산 없음)
# ─────────────────────────────────────────────────────────

def test_migration_backfill_no_double_count_general_mode():
    """last_updated 이전 체결은 백필, 이후 체결은 _apply가 누적 → 이중 가산 없음."""
    state = _make_state(T=2.0, last_updated="2026-05-27 00:00:00", net_invested=0.0)
    state["_net_invested_missing"] = True
    orders = [
        _make_buy_order("B1", "20260526", 10, 10000.0, utc_dt="2026-05-26T10:00:00+00:00"),
        _make_buy_order("B2", "20260528", 10, 5000.0, utc_dt="2026-05-28T10:00:00+00:00"),
    ]
    result = update_T_from_history("SOXL", state, orders)
    # 백필(10000) + 증분(5000) = 15000
    assert result["net_invested"] == 15000.0
    assert "_net_invested_missing" not in result


def test_migration_backfill_initial_mode_recomputes():
    """초기모드(last_updated 없음)는 백필 전체값과 _infer 재계산값이 일치."""
    state = _make_state(T=2.0, last_updated="", net_invested=0.0)
    state["_net_invested_missing"] = True
    orders = [
        _make_buy_order("B1", "20260520", 10, 5000.0),
        _make_buy_order("B2", "20260521", 10, 5000.0),
        _make_sell_order("S1", "20260522", 4, 2400.0),
    ]
    result = update_T_from_history("SOXL", state, orders)
    assert result["net_invested"] == 7600.0
    assert "_net_invested_missing" not in result


# ─────────────────────────────────────────────────────────
# reconcile_reverse_fills 델타 반영
# ─────────────────────────────────────────────────────────

def test_reconcile_reverse_sell_decreases_net_invested():
    state = {
        "T": 20.0,
        "net_invested": 50000.0,
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C1"},
        "orders_meta": {
            "RSELL": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C1",
            }
        },
    }
    history = [{
        "odno": "RSELL",
        "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "3968.00",
        "nccs_qty": "0",
    }]
    reconcile_reverse_fills(state, history)
    # 50000 - 3968 = 46032
    assert state["net_invested"] == 46032.0


def test_reconcile_reverse_buy_increases_net_invested():
    state = {
        "T": 20.0,
        "net_invested": 46032.0,
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C2"},
        "orders_meta": {
            "RBUY": {
                "side": "BUY",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_base_t": 18.0,
                "reverse_t_target": 0.5,
                "cycle_id": "C2",
            }
        },
    }
    history = [{
        "odno": "RBUY",
        "ord_datetime_utc": "2026-08-02T20:00:00+00:00",
        "ft_ccld_qty": "16",
        "ft_ccld_unpr3": "123.00",
        "ft_ccld_amt3": "1968.00",
        "nccs_qty": "16",
    }]
    reconcile_reverse_fills(state, history)
    # 46032 + 1968 = 48000
    assert state["net_invested"] == 48000.0


def test_reconcile_does_not_double_count_processed_amount():
    """이미 반영한 체결금액은 델타만 반영되어 재호출해도 중복 차감되지 않습니다."""
    state = {
        "T": 20.0,
        "net_invested": 50000.0,
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C1"},
        "orders_meta": {
            "RSELL": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C1",
            }
        },
    }
    history = [{
        "odno": "RSELL",
        "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "3968.00",
        "nccs_qty": "0",
    }]
    reconcile_reverse_fills(state, history)
    reconcile_reverse_fills(state, history)
    assert state["net_invested"] == 46032.0


# ─────────────────────────────────────────────────────────
# strategy 시드 캡 적용
# ─────────────────────────────────────────────────────────

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


def test_strategy_seed_cap_blocks_when_net_invested_exceeds_seed(monkeypatch):
    """손절로 순투입이 시드를 초과($56,299.66 vs $50,000) → 추가 매수 금액 0."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import 무한매수법_V4

    state = {
        "T": 5.0,
        "net_invested": 56299.66,
        "net_invested_status": "valid",
        "reverse_mode": {},
        "close_prices": [50.0],
    }
    broker = _make_broker_mock(
        last_price=50.0, position_qty=100, avg_price=45.0, orderable_cash=20000.0
    )

    result = 무한매수법_V4(
        broker, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
        seed=50000, T=5.0, state=state,
    )

    assert result["remaining_seed"] == 0.0
    assert result["orderable_cash"] == 0.0
    assert result["unit_amount"] == 0.0


def test_strategy_seed_cap_allows_within_seed(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import 무한매수법_V4

    state = {
        "T": 5.0,
        "net_invested": 30000.0,
        "net_invested_status": "valid",
        "reverse_mode": {},
        "close_prices": [50.0],
    }
    broker = _make_broker_mock(
        last_price=50.0, position_qty=100, avg_price=45.0, orderable_cash=20000.0
    )

    result = 무한매수법_V4(
        broker, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
        seed=50000, T=5.0, state=state,
    )

    # 남은 시드 $20,000 > 주문가능금액 $20,000 → 그대로 사용
    assert result["remaining_seed"] == 20000.0
    assert result["orderable_cash"] == 20000.0
    assert result["unit_amount"] > 0.0
