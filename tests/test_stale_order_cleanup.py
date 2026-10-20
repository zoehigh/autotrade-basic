"""
봇 소유 미체결 주문 정리 테스트 (신규 주문 제출 전 중복 방지).

배경: 매 세션마다 새 주문 전체를 새로 냅니다. 이전 세션의 주문이 살아 있으면
(특히 목표매도 LIMIT) 새 주문과 중복되어 매도 수량이 보유량을 초과할 수 있습니다.
"""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_bot
from trading_bot import SymbolScopedError, _cancel_open_bot_orders


def _today() -> str:
    return trading_bot.get_kst_now().strftime("%Y%m%d")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """취소 확정 확인의 재시도 대기를 빠르게 합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)


class _FakeBroker:
    """취소 후 이력 재조회까지 시뮬레이션하는 브로커."""

    def __init__(self, fail_on=None, after_cancel_rows=None):
        self.calls = []
        self.fail_on = fail_on
        self.after_cancel_rows = after_cancel_rows

    def cancel_order(self, odno, symbol=None, exchange=None):
        self.calls.append((odno, symbol, exchange))
        if self.fail_on and self.fail_on == odno:
            raise RuntimeError("취소 거부")
        return None

    def get_order_history(self, symbol, exchange, days=30, verbose=False):
        if self.after_cancel_rows is not None:
            return list(self.after_cancel_rows)
        # 기본: 취소된 상태(잔량 0)로 반환
        return [_row(odno, status="취소", remaining="0")
                for odno, _s, _e in self.calls]


class _BrokerWithoutCancel:
    """취소를 지원하지 않는 브로커 (KIS/LS 등) 흉내."""
    def __init__(self):
        self.calls = []


def _row(odno, status="미체결", remaining="18", side="매도", ord_dt=None, filled="0"):
    return {
        "odno": odno,
        "prcs_stat_name": status,
        "nccs_qty": remaining,
        "ft_ccld_qty": filled,
        "sll_buy_dvsn_cd_name": side,
        "ord_dt": ord_dt or _today(),
    }


def test_cancels_open_bot_orders():
    broker = _FakeBroker()
    state = {
        "orders_meta": {
            "OD1": {"side": "SELL", "total_qty": 18, "t_target": 0.0},
            "OD2": {"side": "BUY", "total_qty": 7, "t_target": 1.0},
        }
    }
    history = [_row("OD1"), _row("OD2", side="매수")]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 2
    assert [c[0] for c in broker.calls] == ["OD1", "OD2"]
    assert broker.calls[0][1] == "TQQQ"


def test_skips_closed_orders():
    """체결/취소/정정됨 주문은 취소 대상이 아닙니다."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}, "OD2": {}, "OD3": {}}}
    history = [
        _row("OD1", status="체결", remaining="0"),
        _row("OD2", status="취소", remaining="0"),
        _row("OD3", status="정정됨", remaining="0"),
    ]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_skips_orders_without_remaining():
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", status="부분체결", remaining="0")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_skips_manual_orders_not_in_orders_meta():
    """봇이 낸 주문만 정리합니다 (수동 주문은 건드리지 않음)."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1"), _row("MANUAL1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_skips_reverse_orders():
    """리버스 주문은 기간 관리 대상이므로 건드리지 않습니다."""
    broker = _FakeBroker()
    state = {
        "orders_meta": {
            "REV1": {"reverse_action": "sell"},
            "OD1": {},
        }
    }
    history = [_row("REV1"), _row("OD1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_skips_entirely_when_reverse_mode_active():
    broker = _FakeBroker()
    state = {
        "orders_meta": {"OD1": {}},
        "reverse_mode": {"active": True},
    }
    history = [_row("OD1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_broker_without_cancel_support_is_ignored():
    """취소를 지원하지 않는 브로커(KIS 등)는 오류 없이 건너뜁니다."""
    broker = _BrokerWithoutCancel()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_cancel_failure_raises_symbol_scoped_error():
    """취소 실패 시 신규 주문을 내지 않도록 종목만 중단합니다."""
    broker = _FakeBroker(
        fail_on="OD1",
        after_cancel_rows=[_row("OD1", status="미체결", remaining="18"),
                           _row("OD2", status="미체결", remaining="18")],
    )
    state = {"orders_meta": {"OD1": {}, "OD2": {}}}
    history = [_row("OD1"), _row("OD2")]

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    # 실패가 있어도 나머지 취소는 모두 시도합니다 (부분 취소 상태 방지)
    assert [c[0] for c in broker.calls] == ["OD1", "OD2"]
