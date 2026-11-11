from copy import deepcopy
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from state import reconcile_reverse_fills, update_T_from_history
from strategy import execute_reverse_mode


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


def test_reverse_entry_at_t_between_splits_minus_one_and_splits(monkeypatch):
    """T > 분할수-1 (20분할 T=19.5) → 리버스모드 진입, 1일차 MOC 매도."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import 무한매수법_V4

    state = {"T": 19.5, "reverse_mode": {}, "close_prices": [50.0]}
    broker = _make_broker_mock(
        last_price=50.0, position_qty=100, avg_price=100.0, orderable_cash=1000.0
    )

    result = 무한매수법_V4(
        broker, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
        seed=0, T=19.5, state=state,
    )

    assert result["orders"], "리버스 진입 시 주문이 있어야 합니다"
    assert result["orders"][0]["order_type"] == "MOC"
    assert result["T"] == round(19.5 * 0.9, 4)


def test_no_reverse_entry_at_t_equal_to_splits_minus_one(monkeypatch):
    """T == 분할수-1 (20분할 T=19.0) → 일반모드 유지 (MOC 없음, T 유지)."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import 무한매수법_V4

    state = {"T": 19.0, "reverse_mode": {}, "close_prices": [50.0]}
    broker = _make_broker_mock(
        last_price=50.0, position_qty=100, avg_price=100.0, orderable_cash=1000.0
    )

    result = 무한매수법_V4(
        broker, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
        seed=0, T=19.0, state=state,
    )

    assert result["T"] == 19.0
    assert result["orders"], "일반모드에서도 주문은 생성되어야 합니다"
    assert all(order["order_type"] != "MOC" for order in result["orders"])
    assert not any("리버스" in order.get("comment", "") for order in result["orders"])


def test_reverse_mode_advances_one_day_per_execution(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 20.0,
        "reverse_mode": {},
        "close_prices": [100.0],
    }

    first = execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=0.0,
        last_price=118.0,
        state=state,
    )

    assert first["orders"][0]["order_type"] == "MOC"
    assert first["reverse_day"] == 1
    assert state["reverse_mode"] == {}

    second = execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=0.0,
        last_price=118.0,
        state=state,
    )

    assert second["orders"][0]["order_type"] == "MOC"
    assert second["reverse_day"] == 1
    assert state["reverse_mode"] == {}


def test_reverse_mode_does_not_use_unfilled_sell_proceeds_for_buy(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 20.0,
        "reverse_mode": {
            "day_count": 1,
            "cumulative_sell_proceeds": 10000.0,
        },
        "close_prices": [100.0],
    }

    result = execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=0.0,
        last_price=118.0,
        state=state,
    )

    assert len(result["orders"]) == 1
    assert result["orders"][0]["side"] == "SELL"
    assert state["reverse_mode"]["cumulative_sell_proceeds"] == 10000.0


def test_dry_strategy_state_isolated_from_persisted_state(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 20.0,
        "reverse_mode": {},
        "close_prices": [100.0],
    }
    strategy_state = deepcopy(state)

    execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=0.0,
        last_price=118.0,
        state=strategy_state,
    )

    assert state["reverse_mode"] == {}


def test_reverse_sell_fill_updates_t_and_day_once():
    state = {
        "T": 20.0,
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

    assert state["T"] == 18.0
    assert state["reverse_mode"]["day_count"] == 1
    assert state["reverse_mode"]["cumulative_sell_proceeds"] == 3968.0
    assert state["orders_meta"]["RSELL"]["processed_filled_qty"] == 32


def test_reverse_buy_fill_updates_t_without_advancing_day():
    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C2"},
        "orders_meta": {
            "RSELL2": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 2,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C2",
            },
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
        "odno": "RSELL2",
        "ord_datetime_utc": "2026-08-02T20:00:00+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "3968.00",
        "nccs_qty": "0",
    }, {
        "odno": "RBUY",
        "ord_datetime_utc": "2026-08-02T20:00:01+00:00",
        "ft_ccld_qty": "16",
        "ft_ccld_unpr3": "123.00",
        "ft_ccld_amt3": "1968.00",
        "nccs_qty": "16",
    }]

    reconcile_reverse_fills(state, history)

    assert state["T"] == 18.25
    assert state["reverse_mode"]["day_count"] == 2


def test_recent_history_does_not_apply_reverse_sell_as_generic_quarter_sell():
    state = {
        "T": 20.0,
        "last_updated": "2026-07-31T19:00:00+00:00",
        "last_processed_ordno": "OLD",
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
        "ord_dt": "20260801",
        "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "3968.00",
        "nccs_qty": "0",
    }]

    update_T_from_history("SOXL", state, history, balance_qty=293)

    assert state["T"] == 18.0
    assert state["reverse_mode"]["day_count"] == 1


def test_reverse_partial_sell_progress_is_monotonic_and_additive():
    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "cycle_id": "C3", "day_count": 0},
        "orders_meta": {
            "RSELL3": {
                "side": "SELL",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C3",
            }
        },
    }

    def history(qty, amount, remaining):
        return [{
            "odno": "RSELL3",
            "ord_dt": "20260803",
            "ord_datetime_utc": "2026-08-03T20:00:00+00:00",
            "ft_ccld_qty": str(qty),
            "ft_ccld_unpr3": "124.00",
            "ft_ccld_amt3": str(amount),
            "nccs_qty": str(remaining),
        }]

    reconcile_reverse_fills(state, history(10, 1240, 30))
    assert state["T"] == 19.5
    reconcile_reverse_fills(state, history(20, 2480, 20))
    assert state["T"] == 19.0
    reconcile_reverse_fills(state, history(40, 4960, 0))
    assert state["T"] == 18.0
    assert state["reverse_mode"]["day_count"] == 1


def test_missing_previous_reverse_history_blocks_new_plans():
    state = {
        "T": 18.0,
        "reverse_mode": {
            "active": True,
            "cycle_id": "C4",
            "day_count": 1,
        },
        "orders_meta": {
            "RSELL4": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 2,
                "cycle_id": "C4",
                "submitted_at": "20260731191649",
            }
        },
    }

    reconcile_reverse_fills(state, [])

    assert state["reverse_mode"]["reconciliation_only"] is True
    assert state["reverse_mode"]["reconciliation_error"] == "reverse_order_history_missing"


def test_canceled_partial_sell_is_terminal_and_advances_day():
    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "cycle_id": "C5", "day_count": 0},
        "orders_meta": {
            "RSELL5": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C5",
            }
        },
    }
    history = [{
        "odno": "RSELL5",
        "ord_dt": "20260804",
        "ord_datetime_utc": "2026-08-04T20:00:00+00:00",
        "ft_ccld_qty": "16",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "1984.00",
        "nccs_qty": "16",
        "cncl_qty": "16",
        "prcs_stat_name": "취소",
    }]

    reconcile_reverse_fills(state, history)

    assert state["T"] == 19.0
    assert state["reverse_mode"]["day_count"] == 1


def test_reconcile_matches_moc_fill_with_off_by_one_ord_dt():
    """MOC/LOC 주문은 제출 KST일 vs 이력 ord_dt(ET 영업일)이 하루 어긋나도 누락 오판 없이 반영돼야 합니다.

    봇은 KST 04:xx(=전일 ET 15:xx)에 주문하므로 이력 ord_dt는 제출일의 전일입니다.
    이전 버그: _order_for_meta가 ord_dt == submitted_at[:8]로 비교해 미스매치 →
    '리버스 주문 이력 누락' 오판으로 신규 주문을 영구 차단했습니다.
    """
    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "cycle_id": "C6", "day_count": 0},
        "orders_meta": {
            "000004615": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C6",
                "submitted_at": "20260806041650",
                "submitted_session": "2026-08-05",
            }
        },
    }
    history = [{
        "odno": "000004615",
        "ord_dt": "20260805",           # 미국(ET) 영업일 — submitted_at(08-06)과 하루 차이
        "ord_tmd": "041650",            # KST 시각
        "ord_datetime_utc": "2026-08-04T19:16:50+00:00",
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "136.77",
        "ft_ccld_amt3": "4376.64",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }]

    reconcile_reverse_fills(state, history)

    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None
    assert state["T"] == 18.0
    assert state["reverse_mode"]["day_count"] == 1
    assert state["reverse_mode"]["cumulative_sell_proceeds"] == 4376.64


def test_generic_path_skips_reverse_moc_fill_with_off_by_one_ord_dt():
    """제출일과 이력 ord_dt가 하루 어긋난 리버스 매도가 일반 쿼터매도로 오분류되지 않아야 합니다.

    이전 버그: _is_reverse_order의 날짜 가드가 미스매치로 False 반환 → 리버스 매도가
    일반 쿼터매도(×0.75)로 처리되어 T=15로 오반영됐습니다. 수정 후에는 리버스 경로(×0.9)로
    T=18이 되어야 합니다.
    """
    state = {
        "T": 20.0,
        "last_updated": "2026-07-29T19:16:45+00:00",
        "last_processed_ordno": "000004932",
        "reverse_mode": {"active": True, "cycle_id": "C7", "day_count": 0},
        "orders_meta": {
            "000004615": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C7",
                "submitted_at": "20260806041650",
                "submitted_session": "2026-08-05",
            }
        },
    }
    history = [{
        "odno": "000004615",
        "ord_dt": "20260805",
        "ord_tmd": "041650",
        "ord_datetime_utc": "2026-08-04T19:16:50+00:00",
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "136.77",
        "ft_ccld_amt3": "4376.64",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }]

    update_T_from_history("SOXL", state, history, balance_qty=293)

    assert state["T"] == 18.0
    assert state["reverse_mode"]["day_count"] == 1
    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None


# --- 이전 세션 zero-fill 리버스 주문 자동 만료 가정 (데모 전용) ---


def test_auto_expire_prev_session_zero_fill_buy_demo(monkeypatch):
    """데모: 이전 세션 zero-fill 리버스 매수 → 자동 만료 가정, 차단 없음."""
    monkeypatch.setattr("config.BROKER_MODE", "demo")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C8", "day_count": 5},
        "orders_meta": {
            "RBUY1": {
                "side": "BUY",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_t_target": 0.5,
                "cycle_id": "C8",
                "submitted_at": "20260810041612",
                "submitted_session": "2026-08-10",
            }
        },
    }
    history = [{
        "odno": "RBUY1",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "0",
        "ft_ccld_amt3": "0",
        "nccs_qty": "40",
        "prcs_stat_name": "미체결",
    }]

    reconcile_reverse_fills(state, history)

    meta = state["orders_meta"]["RBUY1"]
    assert meta["terminal"] is True
    assert meta["terminal_assumed"] is True
    assert meta["terminal_assumption_reason"] == "auto_expired_demo_day_order_after_session"
    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None
    assert state["T"] == 12.0


def test_auto_expire_prev_session_zero_fill_sell_demo(monkeypatch):
    """데모: 이전 세션 zero-fill 리버스 매도 → 자동 만료 가정, 차단 없음."""
    monkeypatch.setattr("config.BROKER_MODE", "demo")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C9", "day_count": 1},
        "orders_meta": {
            "RSELL1": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 12.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C9",
                "submitted_at": "20260810041612",
                "submitted_session": "2026-08-10",
            }
        },
    }
    history = [{
        "odno": "RSELL1",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": "0",
        "ft_ccld_amt3": "0",
        "nccs_qty": "32",
        "prcs_stat_name": "미체결",
    }]

    reconcile_reverse_fills(state, history)

    meta = state["orders_meta"]["RSELL1"]
    assert meta["terminal"] is True
    assert meta["terminal_assumed"] is True
    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None
    assert state["T"] == 12.0


def test_auto_expire_not_in_real_mode(monkeypatch):
    """실전: 같은 zero-fill 주문이라도 자동 만료하지 않고 차단 유지."""
    monkeypatch.setattr("config.BROKER_MODE", "real")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C10", "day_count": 5},
        "orders_meta": {
            "RBUY1": {
                "side": "BUY",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_t_target": 0.5,
                "cycle_id": "C10",
                "submitted_at": "20260810041612",
                "submitted_session": "2026-08-10",
            }
        },
    }
    history = [{
        "odno": "RBUY1",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "0",
        "ft_ccld_amt3": "0",
        "nccs_qty": "40",
        "prcs_stat_name": "미체결",
    }]

    reconcile_reverse_fills(state, history)

    meta = state["orders_meta"]["RBUY1"]
    assert meta.get("terminal") is not True
    assert state["reverse_mode"]["reconciliation_error"] == "reverse_order_not_terminal"
    assert state["reverse_mode"]["reconciliation_only"] is True


def test_auto_expire_not_for_today_session(monkeypatch):
    """데모: 오늘 세션의 미체결 주문은 만료 가정하지 않음 (다음 RUN에서 판정)."""
    monkeypatch.setattr("config.BROKER_MODE", "demo")
    today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    today_compact = today_session.replace("-", "")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C11", "day_count": 5},
        "orders_meta": {
            "RBUY2": {
                "side": "BUY",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_t_target": 0.5,
                "cycle_id": "C11",
                "submitted_at": today_compact + "041612",
                "submitted_session": today_session,
            }
        },
    }
    history = [{
        "odno": "RBUY2",
        "ord_dt": today_compact,
        "ord_datetime_utc": today_compact + "T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "0",
        "ft_ccld_amt3": "0",
        "nccs_qty": "40",
        "prcs_stat_name": "미체결",
    }]

    reconcile_reverse_fills(state, history)

    meta = state["orders_meta"]["RBUY2"]
    assert meta.get("terminal") is not True
    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None
    assert state["T"] == 12.0


def test_auto_expire_not_for_partial_fill(monkeypatch):
    """데모: 부분체결된 주문은 만료 가정하지 않고 차단 유지 + 체결분은 반영."""
    monkeypatch.setattr("config.BROKER_MODE", "demo")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C12", "day_count": 5},
        "orders_meta": {
            "RBUY3": {
                "side": "BUY",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_t_target": 0.5,
                "cycle_id": "C12",
                "submitted_at": "20260810041612",
                "submitted_session": "2026-08-10",
            }
        },
    }
    history = [{
        "odno": "RBUY3",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "16",
        "ft_ccld_amt3": "792.00",
        "nccs_qty": "24",
        "prcs_stat_name": "부분체결",
    }]

    reconcile_reverse_fills(state, history)

    meta = state["orders_meta"]["RBUY3"]
    assert meta.get("terminal") is not True
    assert state["reverse_mode"]["reconciliation_error"] == "reverse_order_not_terminal"
    assert state["reverse_mode"]["reconciliation_only"] is True
    assert state["T"] == 12.2


def test_auto_expire_late_fill_still_reflected(monkeypatch):
    """자동 만료 가정 후 늦은 체결이 들어오면 다음 RUN에 델타 그대로 반영."""
    monkeypatch.setattr("config.BROKER_MODE", "demo")
    state = {
        "T": 12.0,
        "reverse_mode": {"active": True, "cycle_id": "C13", "day_count": 5},
        "orders_meta": {
            "RBUY4": {
                "side": "BUY",
                "total_qty": 40,
                "processed_filled_qty": 0,
                "reverse_action": "buy",
                "reverse_day": 2,
                "reverse_t_target": 0.5,
                "cycle_id": "C13",
                "submitted_at": "20260810041612",
                "submitted_session": "2026-08-10",
            }
        },
    }
    zero_history = [{
        "odno": "RBUY4",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "0",
        "ft_ccld_amt3": "0",
        "nccs_qty": "40",
        "prcs_stat_name": "미체결",
    }]

    reconcile_reverse_fills(state, zero_history)

    meta = state["orders_meta"]["RBUY4"]
    assert meta["terminal"] is True
    assert meta["terminal_assumed"] is True

    late_history = [{
        "odno": "RBUY4",
        "ord_dt": "20260810",
        "ord_datetime_utc": "2026-08-09T19:16:12+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "16",
        "ft_ccld_amt3": "792.00",
        "nccs_qty": "24",
        "prcs_stat_name": "부분체결",
    }]

    reconcile_reverse_fills(state, late_history)

    assert state["T"] == 12.2
    assert meta["processed_filled_qty"] == 16
    assert meta["terminal"] is True


# --- last_updated 갱신 테스트 ---


def test_reconcile_advances_last_updated_with_reverse_fill():
    """리버스 체결 반영 후 last_updated가 갱신되어야 합니다."""
    state = {
        "T": 20.0,
        "last_updated": "2026-08-04T19:16:50+00:00",
        "last_processed_ordno": "000004615",
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "CL1"},
        "orders_meta": {
            "RSELL_L1": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "CL1",
                "submitted_at": "20260810041650",
            }
        },
    }
    history = [{
        "odno": "RSELL_L1",
        "ord_dt": "20260809",
        "ord_tmd": "041650",
        "ord_datetime_utc": "2026-08-09T19:16:50+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_amt3": "4376.64",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }]

    reconcile_reverse_fills(state, history)

    assert state["last_updated"] == "2026-08-09T19:16:50+00:00"
    assert state["last_processed_ordno"] == "RSELL_L1"


def test_reconcile_does_not_regress_last_updated():
    """리버스 체결이 last_updated보다 과거이면 last_updated를 되돌리지 않아야 합니다."""
    state = {
        "T": 20.0,
        "last_updated": "2026-08-15T19:00:00+00:00",
        "last_processed_ordno": "000009999",
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "CL2"},
        "orders_meta": {
            "RSELL_L2": {
                "side": "SELL",
                "total_qty": 32,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "CL2",
                "submitted_at": "20260810041650",
            }
        },
    }
    history = [{
        "odno": "RSELL_L2",
        "ord_dt": "20260809",
        "ord_tmd": "041650",
        "ord_datetime_utc": "2026-08-09T19:16:50+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_amt3": "4376.64",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }]

    reconcile_reverse_fills(state, history)

    assert state["last_updated"] == "2026-08-15T19:00:00+00:00"
    assert state["last_processed_ordno"] == "000009999"


# --- 리버스모드 소진 기준 테스트 ---


def test_reverse_exhaustion_moc_sell_when_quarter_buy_impossible(monkeypatch):
    """소진 시 쿼터매수 불가 → LOC 매도 대신 MOC 매도만 수행.

    리버스모드 소진 기준: 쿼터매수(잔금의 1/4)로 1개 매수가 불가능해지는 시점.
    이 경우 매수 시도 없이 MOC 매도만 시행합니다.
    """
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 20.0,
        "reverse_mode": {
            "day_count": 1,
            "cumulative_sell_proceeds": 0.0,
        },
        "close_prices": [100.0],
    }

    # orderable_cash=0 → 쿼터매수 불가 (buy_qty = 0)
    result = execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=0.0,
        last_price=118.0,
        state=state,
    )

    assert len(result["orders"]) == 1, "소진 시 매도 주문 1건만 있어야 합니다"
    assert result["orders"][0]["side"] == "SELL"
    assert result["orders"][0]["order_type"] == "MOC", "소진 시 LOC 대신 MOC 매도여야 합니다"
    assert "소진" in result["orders"][0]["comment"]


def test_reverse_non_exhaustion_loc_sell_and_loc_buy(monkeypatch):
    """비소진 시 쿼터매수 가능 → LOC 매도 + LOC 매수 수행.

    쿼터매수(잔금의 1/4)로 1개 매수가 가능하면 별지점 아래에서 매수를 지속합니다.
    """
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 20.0,
        "reverse_mode": {
            "day_count": 1,
            "cumulative_sell_proceeds": 0.0,
        },
        "close_prices": [100.0],
    }

    # orderable_cash=10000 → 쿼터매수 가능 (buy_amount=2500, star≈100 → buy_qty≥1)
    result = execute_reverse_mode(
        broker=None,
        symbol="SOXL",
        exchange_code="NYS",
        splits=20,
        symbol_type="SOXL",
        position_qty=325,
        avg_price=192.149,
        orderable_cash=10000.0,
        last_price=118.0,
        state=state,
    )

    assert len(result["orders"]) == 2, "비소진 시 매도+매수 2건이 있어야 합니다"
    sell_order = next(o for o in result["orders"] if o["side"] == "SELL")
    buy_order = next(o for o in result["orders"] if o["side"] == "BUY")
    assert sell_order["order_type"] == "LOC", "비소진 시 LOC 매도여야 합니다"
    assert buy_order["order_type"] == "LOC", "비소진 시 LOC 매수여야 합니다"
    assert "쿼터매수" in buy_order["comment"]


def test_reverse_marker_missing_with_confirmed_amount_is_held():
    """마커 누락 리버스 주문은 금액이 확정돼도 자동 증분 반영하지 않고 보류한다."""
    state = {
        "T": 20.0,
        "net_invested": 1000.0,
        "last_updated": "2026-07-31T20:00:00+00:00",
        "last_processed_ordno": "OLD9",
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C9"},
        "orders_meta": {
            "RSELL9": {
                "side": "SELL",
                "total_qty": 32,
                # processed_filled_qty 필드 누락 (레거시)
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C9",
            }
        },
    }
    history = [{
        "odno": "RSELL9",
        "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
        "ft_ccld_qty": "32",
        "ft_ccld_unpr3": "124.00",
        "ft_ccld_amt3": "3968.00",
        "nccs_qty": "0",
    }]

    reconcile_reverse_fills(state, history)

    assert state.get("_settlement_hold") is True
    assert "RSELL9" in (state.get("_pending_amount_odnos") or [])
    assert "RSELL9" in (state.get("_pending_marker_odnos") or [])
    assert state["T"] == 20.0
    # 롤백 검증: 순투입금·proceeds·원장·마커·워터마크가 그대로여야 한다.
    assert state["net_invested"] == 1000.0
    assert state["reverse_mode"].get("cumulative_sell_proceeds", 0.0) == 0.0
    assert (state.get("settlement_ledger") or {}).get("entries", []) == []
    assert "processed_filled_qty" not in state["orders_meta"]["RSELL9"]
    assert state.get("_reverse_settlement_deltas", []) == []
    assert state["last_updated"] == "2026-07-31T20:00:00+00:00"
    assert state["last_processed_ordno"] == "OLD9"
    assert state["orders_meta"]["RSELL9"].get("terminal") is not True


def test_reverse_marker_hold_rolls_back_sibling_order():
    """마커 누락 주문과 정상 주문이 함께 있으면 정상 주문의 임시 반영분도 롤백된다."""
    state = {
        "T": 20.0,
        "net_invested": 1000.0,
        "last_updated": "2026-07-31T20:00:00+00:00",
        "last_processed_ordno": "OLD10",
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C10"},
        "orders_meta": {
            "RSELL_BAD": {
                "side": "SELL",
                "total_qty": 32,
                # processed_filled_qty 필드 누락 (레거시)
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C10",
            },
            "RSELL_OK": {
                "side": "SELL",
                "total_qty": 16,
                "processed_filled_qty": 0,
                "reverse_action": "sell",
                "reverse_day": 1,
                "reverse_base_t": 20.0,
                "reverse_t_factor": 0.9,
                "cycle_id": "C10",
            },
        },
    }
    history = [
        {
            "odno": "RSELL_BAD",
            "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
            "ft_ccld_qty": "32",
            "ft_ccld_unpr3": "124.00",
            "ft_ccld_amt3": "3968.00",
            "nccs_qty": "0",
        },
        {
            "odno": "RSELL_OK",
            "ord_datetime_utc": "2026-08-01T20:00:00+00:00",
            "ft_ccld_qty": "16",
            "ft_ccld_unpr3": "124.00",
            "ft_ccld_amt3": "1984.00",
            "nccs_qty": "0",
        },
    ]

    reconcile_reverse_fills(state, history)

    assert state.get("_settlement_hold") is True
    pending = state.get("_pending_amount_odnos") or []
    assert "RSELL_BAD" in pending
    assert "RSELL_BAD" in (state.get("_pending_marker_odnos") or [])
    # 전체 rollback: 정상 주문의 마커·T·순투입금도 그대로여야 한다.
    assert state["T"] == 20.0
    assert state["net_invested"] == 1000.0
    assert state["reverse_mode"].get("cumulative_sell_proceeds", 0.0) == 0.0
    assert (state.get("settlement_ledger") or {}).get("entries", []) == []
    assert state["orders_meta"]["RSELL_OK"]["processed_filled_qty"] == 0
    assert state.get("_reverse_settlement_deltas", []) == []
    assert state["last_updated"] == "2026-07-31T20:00:00+00:00"
    assert state["last_processed_ordno"] == "OLD10"


# ── 진입 첫날 종료 검사 건너뛰기 (2026-11-11 TQQQ 정지 재현) ─────────────────
# 운영 실측: T=19.7734(>19), 보유 144주, 평단 $65.5732, 현재가 $59.26 (> 평단×0.85 = $55.74).
# 종료 검사가 1일차 MOC 매도보다 먼저 돌아 "진입 즉시 종료·주문 0건"이 되고, reverse_mode 가 비워진 채
# T 가 그대로라 다음 날도 같은 판정이 반복되었습니다. 참고 해설 기준(원문 대조 전): 첫날은 무조건 MOC 매도, 종료 판정은 그 이후.

def _tqqq_1111_state():
    return {"T": 19.7734, "reverse_mode": {}, "close_prices": [59.0]}


def _run_reverse(state, last_price=59.26, position_qty=144, avg_price=65.5732, symbol_type="TQQQ"):
    return execute_reverse_mode(
        broker=None, symbol=symbol_type, exchange_code="NAS", splits=20, symbol_type=symbol_type,
        position_qty=position_qty, avg_price=avg_price, orderable_cash=177.5,
        last_price=last_price, state=state,
    )


def test_first_day_sells_moc_even_when_price_above_exit_line(monkeypatch):
    """재현: 진입 첫날 현재가가 종료선 위여도 1일차 MOC 매도를 내야 하고, 종료하지 않는다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    result = _run_reverse(_tqqq_1111_state())

    assert result["exit"] is False
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["side"] == "SELL" and order["order_type"] == "MOC"
    assert order["quantity"] == 14          # 144 // 10 (20분할 1/10, 내림)
    assert result["reverse_day"] == 1
    assert result["new_T"] == round(19.7734 * 0.9, 4)


def test_first_day_never_buys(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    result = _run_reverse(_tqqq_1111_state())
    assert all(o["side"] == "SELL" for o in result["orders"])


def test_second_day_still_exits_when_price_above_exit_line(monkeypatch):
    """2일차 이후에는 기존대로 종료 판정 (종료선 위 → 종료, 주문 없음)."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _tqqq_1111_state()
    state["reverse_mode"] = {"active": True, "day_count": 1}
    result = _run_reverse(state)

    assert result["exit"] is True
    assert result["orders"] == []
    assert result["reverse_day"] == 2


def test_second_day_continues_when_price_below_exit_line(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _tqqq_1111_state()
    state["reverse_mode"] = {"active": True, "day_count": 1}
    result = _run_reverse(state, last_price=50.0)

    assert result["exit"] is False
    assert result["orders"], "종료선 아래면 리버스가 계속되어야 합니다"


def test_first_day_soxl_threshold_also_skipped(monkeypatch):
    """SOXL(−20%)도 첫날은 종료 검사를 하지 않는다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    result = _run_reverse(
        {"T": 19.5, "reverse_mode": {}, "close_prices": [90.0]},
        last_price=95.0, position_qty=100, avg_price=100.0, symbol_type="SOXL",
    )
    assert result["exit"] is False and result["orders"][0]["order_type"] == "MOC"


def test_strategy_entry_day_with_price_above_exit_line_places_moc(monkeypatch):
    """무한매수법_V4 전체 경로: T>19 진입 첫날 종료선 위여도 MOC 매도가 생성되고 reverse_exit 가 아니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import 무한매수법_V4

    state = {"T": 19.7734, "reverse_mode": {}, "close_prices": [59.0], "net_invested": 9518.68,
             "net_invested_status": "valid"}
    broker = _make_broker_mock(last_price=59.26, position_qty=144, avg_price=65.5732, orderable_cash=177.5)
    result = 무한매수법_V4(broker, symbol="TQQQ", exchange_code="NAS", splits=20, symbol_type="TQQQ",
                         seed=9696.18, T=19.7734, state=state)

    assert result["reverse_exit"] is False
    assert [o["order_type"] for o in result["orders"]] == ["MOC"]


# ── 종료일 같은 날 일반모드 복귀 + 미해결 주문 중복 방지 + 체결 연결 ──────────────

def _strategy_call(state, last_price=59.26, position_qty=130, avg_price=65.5732, cash=2000.0, T=None):
    from strategy import 무한매수법_V4
    broker = _make_broker_mock(last_price=last_price, position_qty=position_qty,
                               avg_price=avg_price, orderable_cash=cash)
    return 무한매수법_V4(broker, symbol="TQQQ", exchange_code="NAS", splits=20, symbol_type="TQQQ",
                         seed=9696.18, T=state["T"] if T is None else T, state=state)


def test_exit_day_runs_normal_strategy_same_day(monkeypatch):
    """1일차 매도 체결 후(T=17.7961, 리버스 active, day_count=1): 종료선 위면 같은 날 일반모드 주문을 낸다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 17.7961, "close_prices": [59.0], "net_invested": 8000.0, "net_invested_status": "valid",
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C1"},
        "orders_meta": {"RS": {"side": "SELL", "reverse_action": "sell", "cycle_id": "C1",
                               "terminal": True, "processed_filled_qty": 14, "total_qty": 14}},
    }
    result = _strategy_call(state)

    assert result["reverse_exit"] is True
    assert result["orders"], "종료 판정일에도 일반모드 주문이 생성되어야 합니다"
    assert not any(o.get("reverse_action") for o in result["orders"]), "리버스 주문이 섞이면 안 됩니다"
    assert any(o["side"] == "BUY" for o in result["orders"])
    # 일반 경로는 전달받은 state 를 바꾸지 않는다 (reverse_mode 정리는 호출부가 reverse_exit 로 수행)
    assert state["reverse_mode"] == {"active": True, "day_count": 1, "cycle_id": "C1"}


def test_exit_with_unresolved_reverse_order_does_not_place_normal_orders(monkeypatch):
    """미해결(terminal 아님) 리버스 주문이 남아 있으면 종료만 반환하고 일반 주문은 내지 않는다 (중복 방지)."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 17.7961, "close_prices": [59.0], "net_invested": 8000.0, "net_invested_status": "valid",
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C1"},
        "orders_meta": {"RS": {"side": "SELL", "reverse_action": "sell", "cycle_id": "C1",
                               "processed_filled_qty": 5, "total_qty": 14}},   # 부분체결, terminal 아님
    }
    result = _strategy_call(state)

    # 미종결 리버스 주문이 있으면 종료 판정도, 일반·리버스 신규 주문도 보류한다 (체결 반영 후 재개)
    assert result["reverse_exit"] is False
    assert result["orders"] == []
    assert state["T"] == 17.7961


def test_reconciliation_only_blocks_all_orders(monkeypatch):
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 17.7961, "close_prices": [59.0], "net_invested": 8000.0, "net_invested_status": "valid",
        "reverse_mode": {"active": False, "reconciliation_only": True, "day_count": 1, "cycle_id": "C1"},
    }
    result = _strategy_call(state)
    assert result["orders"] == [] and result["reverse_exit"] is False


def test_exit_with_t_still_exhausted_restarts_reverse_day_one(monkeypatch):
    """종료인데 T 가 여전히 소진 구간이면 리버스 1일차(MOC 매도)로 새로 시작한다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {
        "T": 19.5, "close_prices": [59.0], "net_invested": 9500.0, "net_invested_status": "valid",
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C1"},
        "orders_meta": {"RS": {"side": "SELL", "reverse_action": "sell", "cycle_id": "C1", "terminal": True}},
    }
    result = _strategy_call(state, T=19.5)

    assert result["reverse_exit"] is True
    assert [o["order_type"] for o in result["orders"]] == ["MOC"]
    assert result["orders"][0]["reverse_day"] == 1


def test_chain_day1_sell_fill_then_day2_normal_orders(monkeypatch):
    """연결: 1일차 MOC 매도 계획 → 체결 반영(T/day_count) → 다음 거래일 종료선 위 → 일반 주문 생성."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = {"T": 19.7734, "reverse_mode": {}, "close_prices": [59.0],
             "net_invested": 9518.68, "net_invested_status": "valid", "orders_meta": {}}

    day1 = _strategy_call(state, position_qty=144, cash=177.5)
    assert day1["reverse_exit"] is False
    sell = day1["orders"][0]
    assert sell["order_type"] == "MOC" and sell["quantity"] == 14

    # 주문 등록 (trading_bot 이 하는 일을 최소 재현) + 체결 반영
    state["reverse_mode"].update({"active": True, "cycle_id": "C1"})
    state["orders_meta"]["D1"] = {
        "side": "SELL", "total_qty": 14, "processed_filled_qty": 0, "reverse_action": "sell",
        "reverse_day": 1, "reverse_base_t": sell["reverse_base_t"],
        "reverse_t_factor": sell["reverse_t_factor"], "cycle_id": "C1",
    }
    history = [{"odno": "D1", "ord_datetime_utc": "2026-11-12T09:30:00+00:00", "ft_ccld_qty": "14",
                "ft_ccld_unpr3": "59.26", "ft_ccld_amt3": "829.64", "nccs_qty": "0"}]
    reconcile_reverse_fills(state, history)

    assert state["T"] == round(19.7734 * 0.9, 4)
    assert state["reverse_mode"]["day_count"] == 1
    assert state["orders_meta"]["D1"].get("terminal") is True

    state["reverse_mode"].pop("last_planned_session", None)     # 다음 거래일로 간주
    day2 = _strategy_call(state, position_qty=130, cash=1000.0, T=state["T"])

    assert day2["reverse_exit"] is True
    assert day2["orders"] and not any(o.get("reverse_action") for o in day2["orders"])


def _day1_state(**rev_extra):
    return {"T": 19.7734, "close_prices": [59.0], "net_invested": 9518.68, "net_invested_status": "valid",
            "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C1",
                             "last_planned_session": "2000-01-01", **rev_extra},
            "orders_meta": {}}


def _day1_meta(**kw):
    meta = {"side": "SELL", "total_qty": 14, "processed_filled_qty": 0, "reverse_action": "sell",
            "reverse_day": 1, "reverse_base_t": 19.7734, "reverse_t_factor": 0.9, "cycle_id": "C1"}
    meta.update(kw)
    return meta


def _hist(filled, remaining, status):
    return [{"odno": "D1", "ord_datetime_utc": "2026-11-12T09:30:00+00:00", "ft_ccld_qty": str(filled),
             "ft_ccld_unpr3": "59.26", "ft_ccld_amt3": f"{filled * 59.26:.2f}", "nccs_qty": str(remaining),
             "prcs_stat_name": status}]


def test_rejected_day1_order_retries_day1_not_exit(monkeypatch):
    """거절: 1일차 주문이 접수되지 않아 meta 가 없고 체결도 없다 → 다음 세션도 1일차 MOC (종료 아님), T 불변."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _day1_state()
    result = _strategy_call(state, position_qty=144, cash=177.5)

    assert result["reverse_exit"] is False
    assert [(o["order_type"], o["quantity"]) for o in result["orders"]] == [("MOC", 14)]
    assert state["T"] == 19.7734


def test_rejected_terminal_zero_fill_keeps_day_and_t(monkeypatch):
    """거부 상태로 terminal 확정(체결 0): day_count·T 불변, 다음 세션 1일차 MOC 재시도 (중복 아님)."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _day1_state()
    state["orders_meta"]["D1"] = _day1_meta()
    reconcile_reverse_fills(state, _hist(0, 14, "거부"))

    assert state["orders_meta"]["D1"].get("terminal") is True
    assert state["T"] == 19.7734 and state["reverse_mode"]["day_count"] == 0
    result = _strategy_call(state, position_qty=144, cash=177.5)
    assert [(o["order_type"], o["quantity"]) for o in result["orders"]] == [("MOC", 14)]


def test_unfilled_open_day1_order_blocks_duplicate_day1(monkeypatch):
    """미체결: 전일 1일차 MOC 가 아직 미종결 → 새 1일차 MOC 를 내지 않는다 (중복 매도 방지), T·day_count 불변."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _day1_state()
    state["orders_meta"]["D1"] = _day1_meta()
    reconcile_reverse_fills(state, _hist(0, 14, "미체결"))

    assert not state["orders_meta"]["D1"].get("terminal")
    result = _strategy_call(state, position_qty=144, cash=177.5)
    assert result["orders"] == [] and result["reverse_exit"] is False
    assert state["T"] == 19.7734 and state["reverse_mode"]["day_count"] == 0


def test_partial_fill_open_blocks_orders_and_t_follows_fill(monkeypatch):
    """부분체결(미종결): T 는 체결분 비율로만 감소하고, 종결 전까지 신규·종료 판정 없음."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _day1_state()
    state["orders_meta"]["D1"] = _day1_meta()
    reconcile_reverse_fills(state, _hist(7, 7, "부분체결"))

    assert 17.7961 < state["T"] < 19.7734, "T 는 체결 비율만큼만 줄어야 합니다"
    assert not state["orders_meta"]["D1"].get("terminal")
    result = _strategy_call(state, position_qty=137, cash=500.0, T=state["T"])
    assert result["orders"] == [] and result["reverse_exit"] is False


def test_partial_fill_then_terminal_advances_day_and_returns_to_normal(monkeypatch):
    """부분체결 후 종결(취소) → day_count 증가 → 다음 거래일 종료선 위면 일반 주문."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    state = _day1_state()
    state["orders_meta"]["D1"] = _day1_meta()
    reconcile_reverse_fills(state, _hist(7, 7, "부분체결"))
    reconcile_reverse_fills(state, _hist(7, 7, "취소"))

    assert state["orders_meta"]["D1"].get("terminal") is True
    assert state["reverse_mode"]["day_count"] == 1
    t_after = state["T"]
    result = _strategy_call(state, position_qty=137, cash=1000.0, T=t_after)
    assert result["reverse_exit"] is True
    assert result["orders"] and not any(o.get("reverse_action") for o in result["orders"])
    assert state["T"] == t_after, "전략 호출이 T 를 바꾸면 안 됩니다 (체결 반영만이 T 를 바꿉니다)"


def test_same_session_rerun_creates_no_duplicate(monkeypatch):
    """동일 세션 재실행: 1일차 계획 후 같은 ET 날짜에 다시 실행하면 주문을 만들지 않는다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from datetime import datetime as _dt
    today = _dt.now(ZoneInfo("America/New_York")).date().isoformat()
    state = _day1_state(last_planned_session=today)
    result = _strategy_call(state, position_qty=144, cash=177.5)

    assert result["orders"] == [] and result["reverse_exit"] is False
    assert state["T"] == 19.7734
