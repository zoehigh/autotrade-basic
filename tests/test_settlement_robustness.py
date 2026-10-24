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
from copy import deepcopy
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
    # 워터마크 기준 보유량 = 현재 잔고 (이번 체결은 워터마크에 포함됨)
    assert result["position_qty"] == 40


def test_stored_position_qty_is_not_reduced_by_batch_again():
    """저장 보유수량은 이번 묶음을 다시 되감지 않습니다 (기준 시점 유지)."""
    st = _state(position_qty=81)
    orders = [
        _row("OLD", "매수", 30, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)
    # 기준선은 저장값 81주 그대로 (61주로 되감으면 안 됨) → 20/81 = 0.25 → 쿼터매도
    assert abs(result["T"] - 10.0312) < 0.001, f"T={result['T']}"
    # 저장값은 새 워터마크(매도 이후) 기준 보유량 = 61주
    assert result["position_qty"] == 61


def test_stored_position_full_sell_detected():
    """저장 보유수량 기준에서도 전량매도가 정확히 판정됩니다."""
    st = _state(position_qty=81)
    orders = [
        _row("OLD", "매수", 81, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 81, "2026-10-21T09:30:16+00:00", price=72.61),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)
    assert result["T"] == 0.0


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
        net_invested=200.0,
        orders_meta={"EARLY": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                               "is_additional": False, "processed_filled_qty": 2,
                               "processed_filled_amount": 200.0}},
    )
    # 2주 @$100 = $200 이미 반영 → 이후 3주 누적 (누적 $530)
    orders = [_row("EARLY", "매수", 5, "2026-10-21T09:00:00+00:00", price=106.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=5)
    # 신규 3주분 $330 만 추가 → $200 + $330 = $530
    assert abs(result["T"] - 1.5) < 0.001, f"T={result['T']}"
    assert abs(result["net_invested"] - 530.0) < 0.01, f"net_invested={result['net_invested']}"


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


def test_cancel_blocks_when_status_unknown(monkeypatch):
    """알 수 없는 주문 상태는 종료로 간주하지 않고 미확정으로 남깁니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[_crow(status="UNKNOWN", remaining="18")])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])


def test_cancel_blocks_when_remaining_unparsable(monkeypatch):
    """잔량 값이 파싱 불가하면 미확정으로 남깁니다 (0 으로 간주하지 않음)."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[_crow(status="미체결", remaining="INVALID")])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()])


def test_cancel_confirmed_with_explicit_terminal_status(monkeypatch):
    """명시적인 종료 상태는 확정으로 인정합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _CancelBroker(rows=[_crow(status="취소", remaining="0", filled="0")])
    state = {"orders_meta": {"OD1": {}}}

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_crow()]) == 1


# ═══════════════════════════════════════════════════════════════════
# ⑥ 불변조건 — 일괄/부분 반영 일치 · 반복 멱등 · 재추정 후 중복 없음
# ═══════════════════════════════════════════════════════════════════

def _sell_row(odno, qty, ts, amount=None, price=10.0):
    return _row(odno, "매도", qty, ts, price=price) if amount is None else _row(
        odno, "매도", qty, ts, price=price
    )


def test_batch_and_partial_sell_give_same_final_t():
    """75주 매도를 한 번에 반영하거나 5주→70주로 나눠 반영해도 최종 T 가 같아야 합니다.

    봇 소유 매도(orders_meta 등록) 기준입니다. 등록되지 않은 수동 매도는
    "워터마크 이전 이력 무시" 계약 때문에 추가 체결분이 반영되지 않습니다.
    """
    old = _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00", price=10.0)
    sell_meta = {"S": {"side": "SELL", "total_qty": 75, "t_target": 0.0,
                       "processed_filled_qty": 0, "processed_filled_amount": 0.0}}

    one_shot = update_T_from_history(
        "TQQQ", _state(T=8.0, position_qty=100, orders_meta=deepcopy(sell_meta)),
        [old, _row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0)],
        balance_qty=25,
    )

    staged = _state(T=8.0, position_qty=100, orders_meta=deepcopy(sell_meta))
    staged = update_T_from_history(
        "TQQQ", staged,
        [old, _row("S", "매도", 5, "2026-10-21T09:00:00+00:00", price=10.0)],
        balance_qty=95,
    )
    staged = update_T_from_history(
        "TQQQ", staged,
        [old, _row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0)],
        balance_qty=25,
    )

    assert abs(one_shot["T"] - staged["T"]) < 0.001, (
        f"일괄 T={one_shot['T']} vs 부분 T={staged['T']}"
    )
    assert abs(one_shot["net_invested"] - staged["net_invested"]) < 0.01


def test_repeated_run_is_idempotent():
    """같은 이력을 반복 정산해도 T·순투입금이 변하지 않아야 합니다."""
    state = _state(
        T=8.0, position_qty=100,
        orders_meta={"S": {"side": "SELL", "total_qty": 75, "t_target": 0.0,
                           "processed_filled_qty": 0}},
    )
    orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00", price=10.0),
        _row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0),
    ]
    first = update_T_from_history("TQQQ", state, orders, balance_qty=25)
    second = update_T_from_history("TQQQ", first, orders, balance_qty=25)
    third = update_T_from_history("TQQQ", second, orders, balance_qty=25)

    assert abs(first["T"] - second["T"]) < 0.0001, f"{first['T']} vs {second['T']}"
    assert abs(second["T"] - third["T"]) < 0.0001
    assert abs(first["net_invested"] - third["net_invested"]) < 0.01


def test_full_reinference_then_incremental_does_not_double_count():
    """전체 재추정 후 같은 이력을增量 정산해도 순투입금이 중복 가산되지 않아야 합니다."""
    from state import _infer_T_from_full_history

    st = _state(
        T=0.2, last_updated="", net_invested=200.0,
        orders_meta={"B": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                           "is_additional": False, "processed_filled_qty": 2,
                           "processed_filled_amount": 200.0}},
    )
    orders = [_row("B", "매수", 5, "2026-10-21T09:00:00+00:00", price=106.0)]
    rebuilt = _infer_T_from_full_history("TQQQ", st, orders)
    assert abs(rebuilt["net_invested"] - 530.0) < 0.01, rebuilt["net_invested"]

    after = update_T_from_history("TQQQ", rebuilt, orders, balance_qty=5)
    assert abs(after["net_invested"] - 530.0) < 0.01, (
        f"재추정 후 증분 정산에서 중복 가산: {after['net_invested']}"
    )


def test_cumulative_amount_correction_is_not_applied_twice():
    """누적체결금액이 역행하면 차감하지 않고 반복 실행해도 값이 변하지 않아야 합니다."""
    st = _state(
        T=1.5, last_updated="2026-10-21T10:00:00+00:00", last_processed_ordno="LATER",
        net_invested=500.0,
        orders_meta={"B": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                           "is_additional": False, "processed_filled_qty": 5,
                           "processed_filled_amount": 500.0}},
    )
    orders = [_row("B", "매수", 5, "2026-10-21T09:00:00+00:00", price=98.0)]
    first = update_T_from_history("TQQQ", st, orders, balance_qty=5)
    second = update_T_from_history("TQQQ", first, orders, balance_qty=5)

    assert abs(first["net_invested"] - 500.0) < 0.01, first["net_invested"]
    assert abs(first["net_invested"] - second["net_invested"]) < 0.01


def test_settlement_is_not_saved_when_basis_unknown(monkeypatch):
    """보유수량 기준이 없으면 정산 결과를 저장하지 않습니다 (reconcile-only 경로 포함)."""
    from state import load_state as real_load_state

    broker = _CancelBroker(error_all=True)
    state = {"orders_meta": {"OD1": {}}}
    with pytest.raises(SymbolScopedError):
        trading_bot._save_settlement_state("TQQQ", {**state, "_position_basis_unknown": True})


# ═══════════════════════════════════════════════════════════════════
# ⑦ P0-2: 금액 미확정 체결 보류 (추정 → 보류)
# ═══════════════════════════════════════════════════════════════════

def _amount_row(odno="F1", side="매수", qty=10, price=10.0, amount=None,
                ts="2026-10-21T09:00:00+00:00"):
    row = {
        "odno": odno,
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": str(qty),
        "ft_ccld_unpr3": str(price),
        "ord_datetime_utc": ts,
        "ord_dt": ts[:10].replace("-", ""),
        "prcs_stat_name": "체결",
        "nccs_qty": "0",
    }
    if amount is not None:
        row["ft_ccld_amt3"] = str(amount)
    return row


def test_fill_amount_returns_confirmed_tuple():
    from state import _fill_amount, _fill_amount_is_available

    assert _fill_amount(_amount_row(amount=100.0)) == (100.0, False)
    assert _fill_amount_is_available(_amount_row(amount=100.0)) is True


def test_fill_amount_missing_or_zero_is_unconfirmed_without_estimation():
    from state import _fill_amount, _fill_amount_is_available

    for row in (_amount_row(amount=None), _amount_row(amount=0)):
        assert _fill_amount(row) == (0.0, True)
        assert _fill_amount_is_available(row) is False


def test_unconfirmed_buy_holds_everything():
    state = _state(T=10.0, net_invested=1000.0)
    state["orders_meta"] = {
        "A": {"total_qty": 10, "t_target": 2.0,
              "processed_filled_qty": 0, "processed_filled_amount": 0.0},
    }
    before = deepcopy(state)
    orders = [_amount_row("A", qty=10, amount=None)]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=70)

    assert result["T"] == pytest.approx(10.0)
    assert result["net_invested"] == pytest.approx(1000.0)
    assert result["last_updated"] == before["last_updated"]
    assert result["last_processed_ordno"] == before["last_processed_ordno"]
    assert result["orders_meta"]["A"] == before["orders_meta"]["A"]
    assert result.get("_settlement_hold") is True
    assert result.get("_pending_amount_odnos") == ["A"]
    assert result.get("_settlement_deltas", []) == []


def test_unconfirmed_late_sell_holds_without_marker_change():
    state = _state(
        T=10.0,
        last_updated="2026-10-21T10:00:00+00:00",
        orders_meta={
            "S": {"total_qty": 20, "t_target": 0.0,
                  "processed_filled_qty": 5, "processed_filled_amount": 50.0},
        },
        position_qty=81,
    )
    orders = [_amount_row("S", side="매도", qty=10, price=10.0, amount=None,
                          ts="2026-10-20T09:00:00+00:00")]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=None)

    assert result["T"] == pytest.approx(10.0)
    assert result["net_invested"] == pytest.approx(6108.47)
    assert result["orders_meta"]["S"]["processed_filled_qty"] == 5
    assert result["orders_meta"]["S"]["processed_filled_amount"] == pytest.approx(50.0)
    assert result.get("_pending_amount_odnos") == ["S"]


def test_unconfirmed_then_confirmed_applies_exactly_once():
    state = _state(T=10.0, net_invested=1000.0)
    state["orders_meta"] = {
        "A": {"total_qty": 10, "t_target": 2.0,
              "processed_filled_qty": 0, "processed_filled_amount": 0.0},
    }
    missing = [_amount_row("A", qty=10, amount=None)]
    update_T_from_history("TQQQ", state, missing, balance_qty=70)
    assert state.get("_pending_amount_odnos") == ["A"]

    confirmed = [_amount_row("A", qty=10, amount=120.0)]
    result = update_T_from_history("TQQQ", state, confirmed, balance_qty=70)
    assert result["T"] == pytest.approx(12.0)
    assert result["net_invested"] == pytest.approx(1120.0)
    assert result.get("_pending_amount_odnos") in (None, [])

    # 같은 이력으로 3회 더 실행해도 중복 반영되지 않습니다.
    for _ in range(3):
        result = update_T_from_history("TQQQ", result, confirmed, balance_qty=70)
        assert result["T"] == pytest.approx(12.0)
        assert result["net_invested"] == pytest.approx(1120.0)


def test_unconfirmed_during_full_reinference_holds(monkeypatch):
    monkeypatch.delenv("FORCE_T_REINFERENCE", raising=False)
    state = _state(T=0.0, net_invested=0.0, last_updated="")
    orders = [_amount_row("A", qty=10, amount=None)]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=70)
    assert result["T"] == pytest.approx(0.0)
    assert result.get("_settlement_hold") is True
    assert result.get("_pending_amount_odnos") == ["A"]


def test_force_reinference_allows_exception_estimation(monkeypatch):
    monkeypatch.setenv("FORCE_T_REINFERENCE", "true")
    state = _state(T=0.0, net_invested=0.0, last_updated="")
    orders = [_amount_row("A", qty=10, price=10.0, amount=None)]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=70)
    assert result.get("_settlement_hold") is not True
    assert result["net_invested"] == pytest.approx(100.0)


def test_reverse_unconfirmed_amount_holds_without_marker_change():
    from state import reconcile_reverse_fills

    state = _state(
        T=10.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1",
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0,
                   "total_qty": 10, "processed_filled_qty": 0,
                   "processed_filled_amount": 0.0, "applied_fill_fraction": 0.0,
                   "submitted_at": "20261021081650", "submitted_session": "2026-10-20"},
        },
    )
    before = deepcopy(state)
    orders = [_amount_row("R1", side="매도", qty=5, amount=None,
                          ts="2026-10-21T08:16:50+00:00")]
    reconcile_reverse_fills(state, orders)
    assert state.get("_settlement_hold") is True
    assert state.get("_pending_amount_odnos") == ["R1"]
    assert state["T"] == before["T"]
    assert state["orders_meta"]["R1"]["applied_fill_fraction"] == 0.0
    assert state.get("settlement_ledger", {}).get("entries", []) == []


def test_has_unconfirmed_amount_fills_only_new_delta():
    from state import _has_unconfirmed_amount_fills

    state = {"orders_meta": {"A": {"processed_filled_qty": 10}}}
    orders = [_amount_row("A", qty=10, amount=None)]
    assert _has_unconfirmed_amount_fills(state, orders) == []
    orders = [_amount_row("A", qty=12, amount=None)]
    assert _has_unconfirmed_amount_fills(state, orders) == ["A"]


def test_evidence_records_unconfirmed_amount_as_zero():
    state = _state(T=10.0)
    orders = [_amount_row("A", qty=10, amount=None)]
    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", state, orders, mode="DRY", run_id="R1",
    )
    fill = evidence["fills"][0]
    assert fill["amount"] == 0.0
    assert fill["amount_confirmed"] is False


def test_evidence_records_marker_hold_odnos_separately():
    """증거 파일에서 마커 누락 주문과 금액 미확정 주문을 구분할 수 있어야 한다."""
    state = _state(T=11.0)
    state["_settlement_hold"] = True
    state["_settlement_hold_reason"] = "마커 누락"
    state["_pending_amount_odnos"] = ["A", "B"]
    state["_pending_marker_odnos"] = ["A"]
    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", state, [], mode="DRY", run_id="R2",
    )
    assert evidence["pending_amount_odnos"] == ["A", "B"]
    assert evidence["pending_marker_odnos"] == ["A"]


# ═══════════════════════════════════════════════════════════════════
# P1-3: 오프라인 전환 매트릭스 + 브로커별 컨벤션
# ═══════════════════════════════════════════════════════════════════
def test_offline_transition_no_ledger_applies_buy():
    state = _state(T=1.0, settlement_ledger=None)
    orders = [_row("B", "매수", 5, "2026-10-21T09:00:00+00:00")]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=40)
    assert result["T"] == pytest.approx(1.5)
    assert result["settlement_ledger"]["entries"]


def test_offline_transition_marker_only_same_class_keeps_t():
    state = _state(
        T=7.5,
        settlement_ledger={"start_T": 10.0, "next_seq": 2,
                           "entries": [{"seq": 1, "kind": "sell", "odno": "S",
                                        "factor": 0.75}]},
        sell_t_effects={"S": {"basis": 80, "factor": 0.75}},
    )
    orders = [_row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61)]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=61)
    assert result.get("_settlement_hold") is not True
    assert result["T"] == pytest.approx(7.5)


def test_offline_transition_position_basis_unknown_blocks_save():
    state = _state(T=10.0)
    orders = [_row("S", "매도", 20, "2026-10-21T09:30:16+00:00", price=72.61)]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=None)
    assert result.get("_position_basis_unknown") is True
    with pytest.raises(SymbolScopedError):
        trading_bot._save_settlement_state("TQQQ", result)


def test_order_us_session_date_broker_conventions():
    from state import _order_us_session_date

    # KIS/LS/키움: ord_dt 자체가 미국(ET) 영업일 — KST 시각은 세션을 바꾸지 않음.
    kis = {"ord_dt": "20261023", "ord_tmd": "013000",
           "ord_datetime_utc": "2026-10-22T16:30:00+00:00"}
    assert _order_us_session_date(kis) == "20261023"

    # TOSS/MERITZ: ord_dt는 KST 날짜 → ET 세션으로 환산.
    toss = {"ord_dt": "20261024", "ord_tmd": "013000", "_ord_dt_is_kst_date": True}
    assert _order_us_session_date(toss) == "20261023"


def test_order_real_kst_date_broker_conventions():
    from state import _order_real_kst_date

    # KIS: ord_dt(미국영업일)+ord_tmd(KST) → 실제 KST 날짜 복원.
    kis = {"ord_dt": "20260731", "ord_tmd": "041046"}
    assert _order_real_kst_date(kis) == "20260801"

    # TOSS/MERITZ: 이미 KST 날짜 — ET round-trip 하지 않음.
    toss = {"ord_dt": "20261024", "ord_tmd": "013000", "_ord_dt_is_kst_date": True}
    assert _order_real_kst_date(toss) == "20261024"


def test_event_evidence_has_execution_metadata():
    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", _state(T=0.0), [], mode="LIVE", run_id="run-9"
    )
    assert evidence["run_id"] == "run-9"
    assert evidence["broker"]
    assert evidence["code_version"] is not None


def test_nhplug_normalizes_to_standard_fields():
    """NHPLUG도 state 표준 필드(ord_dt/ft_ccld_qty/ft_ccld_amt3/odno)를 제공합니다."""
    from broker.nhplug.adapter import NHPlugBroker

    broker = object.__new__(NHPlugBroker)
    row = broker._normalize_order_item({
        "ord_dt": "20261021", "ord_tmd": "041000", "odno": "NH1",
        "sll_buy_dvsn_cd_name": "매수", "ft_ord_qty": "10",
        "ft_ccld_qty": "10", "ft_ccld_unpr3": "10.0",
        "ft_ccld_amt3": "100.0", "nccs_qty": "0",
        "prcs_stat_name": "체결", "prdt_name": "TQQQ",
        "ovrs_excg_cd": "NASD", "tr_crcy_cd": "USD",
    })
    assert row["odno"] == "NH1"
    assert row["ord_dt"] == "20261021"
    assert row["ft_ccld_qty"] == "10"
    assert row["ft_ccld_amt3"] == "100.0"
    assert row["_ord_dt_is_us_trading_date"] is True
    assert row["sll_buy_dvsn_cd_name"] == "매수"


def test_unconfirmed_fill_pushed_before_watermark_is_recovered_once():
    """금액 미확정으로 밀린 체결이 워터마크 이전이어도 확정 후 1회 반영됩니다.

    A(금액불명, 워터마크 이전·미반영) + B(확정, 이미 반영) 상태에서
    A 가 나중에 금액 확정되면 A 만 반영되고 재실행은 멱등이어야 합니다.
    """
    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="B",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "B": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                  "processed_filled_qty": 1, "processed_filled_amount": 20.0},
        },
    )
    before = deepcopy(state)
    missing = [
        _amount_row("A", qty=1, amount=None, ts="2026-10-22T09:00:00+00:00"),
        _amount_row("B", qty=1, amount=20.0, ts="2026-10-22T10:00:00+00:00"),
    ]

    held = update_T_from_history("TQQQ", state, missing, balance_qty=2)
    # 전량 보류: T·순투입금·마커·워터마크 불변
    assert held["T"] == pytest.approx(before["T"])
    assert held["net_invested"] == pytest.approx(before["net_invested"])
    assert held["orders_meta"]["A"] == before["orders_meta"]["A"]
    assert held["orders_meta"]["B"] == before["orders_meta"]["B"]
    assert held["last_updated"] == before["last_updated"]
    assert held["last_processed_ordno"] == before["last_processed_ordno"]
    assert held.get("_settlement_hold") is True
    assert held.get("_pending_amount_odnos") == ["A"]

    confirmed = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T09:00:00+00:00"),
        _amount_row("B", qty=1, amount=20.0, ts="2026-10-22T10:00:00+00:00"),
    ]
    first = update_T_from_history("TQQQ", held, confirmed, balance_qty=2)
    assert first["T"] == pytest.approx(12.0)
    assert first["net_invested"] == pytest.approx(1060.0)
    assert first["orders_meta"]["A"]["processed_filled_qty"] == 1
    assert first["orders_meta"]["A"]["processed_filled_amount"] == pytest.approx(20.0)

    for _ in range(3):
        first = update_T_from_history("TQQQ", first, confirmed, balance_qty=2)
        assert first["T"] == pytest.approx(12.0)
        assert first["net_invested"] == pytest.approx(1060.0)


def test_legacy_meta_missing_processed_qty_marker_is_held_not_recovered():
    """processed_filled_qty 필드가 누락된 레거시 메타는 자동 복구하지 않고 정산을 보류한다.

    필드 누락 상태는 이미 반영된 체결인지 여부를 판단할 수 없으므로,
    명시적 0(미반영)과 구분해 자동 재가산을 방지한다.
    """
    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="B",
        orders_meta={
            # processed_filled_qty 필드 누락 (레거시/수동 편집 state.json)
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0},
            # 명시적 0 (정상 등록 후 미반영) — 복구 대상
            "B": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    before = deepcopy(state)
    # A: 금액 확정 + 마커 누락 → 마커 보류 경로를 검증한다.
    # B: 금액 미확정 → 금액 보류 경로를 검증한다.
    missing = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T09:00:00+00:00"),
        _amount_row("B", qty=1, amount=None, ts="2026-10-22T09:30:00+00:00"),
    ]

    held = update_T_from_history("TQQQ", state, missing, balance_qty=2)
    # A(마커 누락)와 B(금액 미확정) 모두 보류
    assert held.get("_settlement_hold") is True
    pending = held.get("_pending_amount_odnos") or []
    assert "A" in pending, f"A(마커 누락)가 보류 대상에 포함되어야 함: {pending}"
    assert "B" in pending, f"B(금액 미확정)가 보류 대상에 포함되어야 함: {pending}"
    # 마커 누락 사유가 별도 키로 구분되어야 한다.
    assert "A" in (held.get("_pending_marker_odnos") or [])
    assert "B" not in (held.get("_pending_marker_odnos") or [])
    # T·순투입금·마커·워터마크 불변
    assert held["T"] == pytest.approx(before["T"])
    assert held["net_invested"] == pytest.approx(before["net_invested"])
    assert held["orders_meta"]["A"] == before["orders_meta"]["A"]
    assert held["orders_meta"]["B"] == before["orders_meta"]["B"]
    assert held["last_updated"] == before["last_updated"]
    assert held["last_processed_ordno"] == before["last_processed_ordno"]


def test_explicit_zero_processed_qty_is_recovered_after_amount_confirmed():
    """processed_filled_qty가 명시적 0인 주문은 금액 확정 후 1회 복구된다.

    레거시 마커 누락과 구분해 정상 등록 후 미반영 주문은 복구 경로를 유지한다.
    """
    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="B",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    before = deepcopy(state)
    missing = [
        _amount_row("A", qty=1, amount=None, ts="2026-10-22T09:00:00+00:00"),
    ]

    held = update_T_from_history("TQQQ", state, missing, balance_qty=2)
    assert held.get("_settlement_hold") is True
    assert held.get("_pending_amount_odnos") == ["A"]
    assert held["T"] == pytest.approx(before["T"])

    confirmed = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T09:00:00+00:00"),
    ]
    first = update_T_from_history("TQQQ", held, confirmed, balance_qty=2)
    assert first["T"] == pytest.approx(12.0)
    assert first["net_invested"] == pytest.approx(1060.0)
    assert first["orders_meta"]["A"]["processed_filled_qty"] == 1
    assert first["orders_meta"]["A"]["processed_filled_amount"] == pytest.approx(20.0)

    # 재실행 멱등
    for _ in range(3):
        first = update_T_from_history("TQQQ", first, confirmed, balance_qty=2)
        assert first["T"] == pytest.approx(12.0)
        assert first["net_invested"] == pytest.approx(1060.0)


def test_recent_marker_missing_with_confirmed_amount_is_held():
    """워터마크 이후 최근 주문도 마커 누락 시 자동 반영하지 않고 보류한다.

    금액이 확정되어 있어도 processed_filled_qty 마커가 없으면
    전체 체결량을 신규분으로 계산할 수 없으므로 정산을 보류한다.
    """
    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="OLD",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0},
        },
    )
    before = deepcopy(state)
    rows = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T11:00:00+00:00"),
    ]

    held = update_T_from_history("TQQQ", state, rows, balance_qty=2)
    assert held.get("_settlement_hold") is True
    assert "A" in (held.get("_pending_amount_odnos") or [])
    assert "A" in (held.get("_pending_marker_odnos") or [])
    assert held["T"] == pytest.approx(before["T"])
    assert held["net_invested"] == pytest.approx(before["net_invested"])
    assert held["orders_meta"]["A"] == before["orders_meta"]["A"]
    assert held["last_updated"] == before["last_updated"]


def test_invalid_marker_values_are_held_like_missing():
    """None·파싱 불가·음수 마커도 누락과 동일하게 보류한다."""
    for bad in (None, "bad", -1):
        state = _state(
            T=11.0,
            net_invested=1040.0,
            last_updated="2026-10-22T10:00:00+00:00",
            last_processed_ordno="OLD",
            orders_meta={
                "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                      "processed_filled_qty": bad},
            },
        )
        rows = [
            _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T11:00:00+00:00"),
        ]
        held = update_T_from_history("TQQQ", state, rows, balance_qty=2)
        assert held.get("_settlement_hold") is True, f"bad={bad!r}"
        assert "A" in (held.get("_pending_amount_odnos") or []), f"bad={bad!r}"
        assert held["T"] == pytest.approx(11.0)


def test_zero_fill_with_missing_marker_does_not_hold():
    """체결 수량이 0이면 마커가 누락돼도 불필요하게 보류하지 않는다."""
    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="OLD",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0},
        },
    )
    rows = [
        {
            "odno": "A",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "0",
            "ft_ccld_unpr3": "10.0",
            "ord_datetime_utc": "2026-10-22T11:00:00+00:00",
            "ord_dt": "20261022",
            "prcs_stat_name": "미체결",
            "nccs_qty": "1",
        },
    ]
    result = update_T_from_history("TQQQ", state, rows, balance_qty=2)
    assert result.get("_settlement_hold") is not True
    assert "A" not in (result.get("_pending_amount_odnos") or [])
    assert result["T"] == pytest.approx(11.0)


def test_late_fill_delta_marks_legacy_marker_missing_directly():
    """지연 체결 계산 경로 자체가 마커 누락 주문을 보류 대상으로 반환한다."""
    from state import _apply_late_fill_deltas

    state = _state(
        T=11.0,
        last_updated="2026-10-22T10:00:00+00:00",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0},
        },
    )
    history = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T09:00:00+00:00"),
    ]

    pending, net_qty, net_amount, held = _apply_late_fill_deltas(state, history, set())

    assert held == ["A"]
    assert pending == []
    assert net_qty == 0
    assert net_amount == 0.0


def test_fractional_qty_marker_is_invalid_and_held():
    """소수 마커(0.5 등)는 정수 마커로 절삭하지 않고 무효로 보류한다."""
    from state import _parse_processed_qty_marker

    assert _parse_processed_qty_marker({"processed_filled_qty": 0}) == (True, 0)
    assert _parse_processed_qty_marker({"processed_filled_qty": 5}) == (True, 5)
    assert _parse_processed_qty_marker({"processed_filled_qty": "3"}) == (True, 3)
    for bad in (None, "bad", -1, True, 0.5, 1.9):
        valid, qty = _parse_processed_qty_marker({"processed_filled_qty": bad})
        assert valid is False, f"bad={bad!r}"
        assert qty == 0
    assert _parse_processed_qty_marker({}) == (False, 0)
    assert _parse_processed_qty_marker(None) == (False, 0)

    state = _state(
        T=11.0,
        net_invested=1040.0,
        last_updated="2026-10-22T10:00:00+00:00",
        last_processed_ordno="OLD",
        orders_meta={
            "A": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                  "processed_filled_qty": 0.5},
        },
    )
    rows = [
        _amount_row("A", qty=1, amount=20.0, ts="2026-10-22T11:00:00+00:00"),
    ]
    held = update_T_from_history("TQQQ", state, rows, balance_qty=2)
    assert held.get("_settlement_hold") is True
    assert "A" in (held.get("_pending_amount_odnos") or [])
    assert "A" in (held.get("_pending_marker_odnos") or [])
    assert held["T"] == pytest.approx(11.0)
