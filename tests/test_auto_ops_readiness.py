"""
취소 확정 확인 · 리버스 1.19 캡 · 실행 결과 집계 테스트.

배경 (2026-10-20 검토):
- 자동 운영 전제리로 세 가지를 보완합니다.
  ① 취소 요청 접수만으로 취소 완료로 간주하던 문제 → 이력 재조회로 확정 확인
  ② 리버스 쿼터매수에 1.19 상한이 누락되던 문제
  ③ 종목 실패 시에도 exit 0 으로 끝나던 문제 → 결과 집계 + 종료코드
"""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_bot
from trading_bot import SymbolScopedError, _cancel_open_bot_orders


def _row(odno, status="미체결", remaining="18", side="매도", ord_dt=None, filled="0"):
    return {
        "odno": odno,
        "prcs_stat_name": status,
        "nccs_qty": remaining,
        "ft_ccld_qty": filled,
        "sll_buy_dvsn_cd_name": side,
        "ord_dt": ord_dt or trading_bot.get_kst_now().strftime("%Y%m%d"),
    }


class _Broker:
    """취소 후 이력 재조회를 시뮬레이션하는 브로커."""

    def __init__(self, after_cancel_rows=None, cancel_error=None):
        self.calls = []
        self.after_cancel_rows = after_cancel_rows
        self.cancel_error = cancel_error
        self.history_calls = 0

    def cancel_order(self, odno, symbol=None, exchange=None):
        self.calls.append(odno)
        if self.cancel_error:
            raise RuntimeError(self.cancel_error)
        return None

    def get_order_history(self, symbol, exchange, days=30, verbose=False):
        self.history_calls += 1
        if self.after_cancel_rows is not None:
            return list(self.after_cancel_rows)
        return []


# ═══════════════════════════════════════════════════════════════════
# ① 취소 확정 확인
# ═══════════════════════════════════════════════════════════════════


def test_cancel_confirmed_when_history_shows_cancelled(monkeypatch):
    """취소 후 이력에서 취소 상태가 되면 확정한 것으로 봅니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="취소", remaining="0")])
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1")]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 1
    assert broker.calls == ["OD1"]
    assert broker.history_calls >= 1


def test_cancel_confirmed_when_remaining_is_zero(monkeypatch):
    """상태 표기가 남아 있어도 잔량이 0이면 확정입니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="0")])
    state = {"orders_meta": {"OD1": {}}}

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    assert count == 1


def test_cancel_unconfirmed_raises_and_blocks_new_orders(monkeypatch):
    """취소 후에도 잔량이 남아 있으면 신규 주문을 내지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="18")])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])


def test_fill_during_cancel_raises(monkeypatch):
    """취소 도중 체결이 발생하면 포지션이 바뀌므로 신규 주문을 내지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(
        after_cancel_rows=[
            _row("OD1", status="부분체결", remaining="10", filled="8"),
        ]
    )
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])


def test_previous_session_orders_are_not_cancelled(monkeypatch):
    """이전 세션 주문은 만료 대상이므로 취소하지 않습니다 (당일 주문 규칙)."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[])
    yesterday = "20260101"
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", ord_dt=yesterday)]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 0
    assert broker.calls == []
    assert broker.history_calls == 0


def test_cancel_rejection_leaves_order_open_and_blocks(monkeypatch):
    """취소 요청이 거부되고 주문이 여전히 열려 있으면 중단합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="18")],
                     cancel_error="취소 거부")
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    # 실패해도 취소 시도는 이루어집니다
    assert broker.calls == ["OD1"]


def test_retry_confirms_after_history_lag(monkeypatch):
    """이력 반영이 늦으면 재시도로 확인할 수 있어야 합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker()
    state = {"orders_meta": {"OD1": {}}}

    rows = [
        [_row("OD1", status="미체결", remaining="18")],   # 1차: 아직 반영 안 됨
        [_row("OD1", status="미체결", remaining="18")],   # 2차: 아직
        [_row("OD1", status="취소", remaining="0")],      # 3차: 반영됨
    ]
    calls = {"n": 0}

    def _history(_symbol, _exchange, days=30, verbose=False):
        broker.history_calls += 1
        index = min(calls["n"], len(rows) - 1)
        calls["n"] += 1
        return list(rows[index])

    broker.get_order_history = _history

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    assert count == 1
    assert broker.history_calls == 3


# ═══════════════════════════════════════════════════════════════════
# ② 리버스 쿼터매수 1.19 캡
# ═══════════════════════════════════════════════════════════════════


def test_reverse_buy_applies_price_cap(monkeypatch):
    """리버스 쿼터매수도 현재가 ×1.19 상한을 적용합니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import execute_reverse_mode

    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "day_count": 1},
        "close_prices": [100.0, 100.0, 100.0, 100.0, 100.0],
    }

    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL",
        position_qty=100, avg_price=100.0, orderable_cash=1000.0,
        last_price=50.0, state=state,
    )

    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    assert len(buys) == 1
    # 별지점 $100 - 0.01 = $99.99 → 상한 50 × 1.19 = $59.50
    assert buys[0]["price"] == 59.5


def test_reverse_buy_without_cap_keeps_star_price(monkeypatch):
    """상한에 걸리지 않으면 별지점 가격을 그대로 유지합니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import execute_reverse_mode

    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "day_count": 1},
        "close_prices": [50.0, 50.0, 50.0, 50.0, 50.0],
    }

    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL",
        position_qty=100, avg_price=100.0, orderable_cash=1000.0,
        last_price=45.0, state=state,
    )

    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    assert len(buys) == 1
    # 별지점 50 - 0.01 = 49.99, 상한 45 × 1.19 = 53.55 → 캡 없음
    assert buys[0]["price"] == 49.99
