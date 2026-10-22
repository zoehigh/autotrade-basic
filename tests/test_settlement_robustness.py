"""
정산·취소 확인 로직 회귀 테스트 (2026-10-22 검토에서 발견된 6건).

① 매도 비율 분모가 이력 구간에 의존 → 오분류
② 잔고 보정이 리버스 체결과 이중 차감
③ 워터마크 뒤로 밀린 부분체결이 반영되지 않음
④ 전체 재추정이 이미 반영한 체결을 빼버림
⑤ 취소 확정 확인이 통과해선 안 되는 조건
⑥ 자동 검증이 T 변화를 검증하지 않음
"""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_bot
from trading_bot import SymbolScopedError, _cancel_open_bot_orders

from state import load_state, update_T_from_history  # noqa: E402


def _row(odno, side="매수", qty=10, ts="2026-10-21T09:00:00+00:00", price=10.0,
         status=None, remaining=None, filled=None):
    return {
        "odno": odno,
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": str(qty if filled is None else filled),
        "ft_ccld_amt3": str(qty * price),
        "ft_ccld_unpr3": str(price),
        "ord_datetime_utc": ts,
        "ord_dt": ts[:10].replace("-", ""),
        "prcs_stat_name": status or ("체결" if qty else "미체결"),
        "nccs_qty": str(remaining if remaining is not None else 0),
    }


def _state(T=13.375, **kw):
    st = {
        "T": T,
        "net_invested": 6108.47,
        "last_updated": "2026-10-20 11:14:03+00:00",
        "last_processed_ordno": "OLD",
        "orders_meta": {},
        "cycle_start_date": "2026-09-23",
        "additional_loc_odno": [],
        "balance_mismatch": {},
        "net_invested_status": "valid",
    }
    st.update(kw)
    return st


# ═══════════════════════════════════════════════════════════════════
# ① 매도 비율 분모 — 보유수량 기준선
# ═══════════════════════════════════════════════════════════════════

def test_sell_ratio_uses_balance_anchor():
    """보유 81주 중 20주(24.7%) 매도 → 쿼터매도(T×0.75)."""
    orders = [
        _row("OLD", "매수", 30, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61),
    ]
    result = update_T_from_history("TQQQ", _state(), orders, balance_qty=61)
    assert abs(result["T"] - 10.0312) < 0.001, f"T={result['T']}"


def test_sell_ratio_uses_stored_position_when_balance_unknown():
    """잔고 조회가 실패해도 저장된 보유수량으로 분류합니다."""
    st = _state(position_qty=81)
    orders = [
        _row("OLD", "매수", 30, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)
    assert abs(result["T"] - 10.0312) < 0.001, f"T={result['T']}"


def test_sell_ratio_without_any_position_basis_keeps_history_and_flags():
    """기준선을 확보하지 못하면 이력값으로 분류하고 신규 주문 보류 신호를 남깁니다."""
    orders = [
        _row("OLD", "매수", 30, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61),
    ]
    result = update_T_from_history("TQQQ", _state(), orders, balance_qty=None)
    # 이력값(30주) 기준 분류가 되지만, 신규 주문은 보류되어야 합니다
    assert result.get("_position_basis_unknown") is True


def test_position_qty_is_saved_for_next_run():
    """보유수량이 state 에 저장되어 다음 실행의 기준선으로 쓰입니다."""
    orders = [_row("B", "매수", 5, "2026-10-21T09:00:00+00:00")]
    result = update_T_from_history("TQQQ", _state(T=1.0), orders, balance_qty=40)
    assert result["position_qty"] == 40


# ═══════════════════════════════════════════════════════════════════
# ② 리버스 체결 이중 차감 제거
# ═══════════════════════════════════════════════════════════════════

def test_reverse_sell_not_double_subtracted_from_anchor():
    """리버스 매도가 기준선(잔고)에 이미 반영되어 다시 차감되지 않습니다."""
    st = _state(
        T=8.0,
        orders_meta={"REV": {"reverse_action": "sell", "total_qty": 60,
                             "t_target": 0.0, "processed_filled_qty": 0}},
    )
    orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00"),
        _row("REV", "매도", 60, "2026-10-21T08:00:00+00:00"),
        _row("S", "매도", 20, "2026-10-21T10:00:00+00:00"),
    ]
    # 실제 잔고 20주(100 매수 - 60 리버스 매도 - 20 일반 매도)
    result = update_T_from_history("TQQQ", st, orders, balance_qty=20)
    # 기준선 = 20 - (-20) = 40주 → 비율 20/40 = 0.5 → 목표매도 → 8 × 0.25 = 2.0
    assert abs(result["T"] - 2.0) < 0.001, f"T={result['T']}"


# ═══════════════════════════════════════════════════════════════════
# ③ 워터마크 뒤로 밀린 부분체결
# ═══════════════════════════════════════════════════════════════════

def test_late_partial_fill_is_applied_by_order_delta():
    """워터마크보다 오래된 주문의 누적 체결 증가는 반영되어야 합니다."""
    st = _state(
        T=1.2,
        last_updated="2026-10-21T10:00:00+00:00",
        last_processed_ordno="LATER",
        net_invested=20.0,
        orders_meta={"EARLY": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                               "is_additional": False, "processed_filled_qty": 2}},
    )
    orders = [_row("EARLY", "매수", 5, "2026-10-21T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=5)
    # 이미 반영된 2주를 제외한 추가 3주가 반영되어야 합니다
    assert abs(result["T"] - 1.5) < 0.001, f"T={result['T']}"
    assert abs(result["net_invested"] - 50.0) < 0.01, f"net_invested={result['net_invested']}"


# ═══════════════════════════════════════════════════════════════════
# ④ 전체 재추정 — 이미 반영한 체결을 빼지 않음
# ═══════════════════════════════════════════════════════════════════

def test_full_reinference_counts_already_processed_fills():
    """전체 재추정은 처음부터 다시 계산하므로 이미 반영된 체결도 포함합니다."""
    st = _state(
        T=1.0,
        last_updated="",
        orders_meta={"B": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                            "is_additional": False, "processed_filled_qty": 10}},
    )
    orders = [_row("B", "매수", 10, "2026-10-21T09:00:00+00:00")]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=10)
    assert abs(result["T"] - 1.0) < 0.001, f"T={result['T']}"


# ═══════════════════════════════════════════════════════════════════
# ⑤ 취소 확정 확인
# ═══════════════════════════════════════════════════════════════════

class _CancelBroker:
    def __init__(self, rows=None, error=False, error_all=False):
        self.rows = rows
        self.error = error
        self.error_all = error_all
        self.history_calls = 0

    def cancel_order(self, odno, symbol=None, exchange=None):
        if self.error:
            raise RuntimeError("취소 타임아웃")

    def get_order_history(self, symbol, exchange, days=30, verbose=False):
        self.history_calls += 1
        if self.error_all:
            raise RuntimeError("이력 조회 실패")
        return list(self.rows or [])


def _crow(status="미체결", remaining="18", filled="0"):
    return {
        "odno": "OD1",
        "ord_dt": trading_bot.get_kst_now().strftime("%Y%m%d"),
        "prcs_stat_name": status,
        "nccs_qty": remaining,
        "ft_ccld_qty": filled,
    }


def test_cancel_blocks_when_history_lookup_fails(monkeypatch):
    """이력 조회가 계속 실패하면 미확정으로 남고 신규 주문을 내지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(error_all=True)
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])

    assert broker.history_calls == 3


def test_cancel_blocks_when_order_row_missing(monkeypatch):
    """이력에서 주문 행이 사라져도 확정으로 보지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])


def test_cancel_blocks_when_fill_happened_during_cancel(monkeypatch):
    """취소 도중 체결이 발생하면 재계획이 필요하므로 중단합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[_crow(status="취소", remaining="0", filled="18")])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])


def test_cancel_confirmed_when_no_new_fill(monkeypatch):
    """체결 증가 없이 취소되면 확정됩니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[_crow(status="취소", remaining="0", filled="0")])
    state = {"orders_meta": {"OD1": {}}}

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])

    assert count == 1
