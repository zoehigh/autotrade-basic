"""리버스 쿼터매수 T 효과 = 원문 규칙(직전 T 기준) 회귀 테스트.

원문(무한매수법 V4.0 4~5쪽): 매도 T = 직전 T × 0.9, 매수 T = 직전 T + (20 − 직전 T) × 0.25.
주문 생성 때 '같은 날 매도가 전량 체결된다'고 가정해 고정하던 목표값(2026-11-17 SOXL 실측 +0.747,
매도 미체결이면 +0.274)을, 신규 주문(reverse_t_rule=2)은 첫 체결 반영 시점의 실제 T로 확정한다.
"""
import os
import sys
from copy import deepcopy

import pytest

from state import reconcile_reverse_fills

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(_repo_root, "ops"))

SPLITS = 20


def _state(t=18.9022, rule=True, planned=None):
    base_t = t
    planned_target = planned if planned is not None else round((SPLITS - round(base_t * 0.9, 4)) * 0.25, 4)
    buy_meta = {
        "side": "BUY", "total_qty": 10, "processed_filled_qty": 0,
        "processed_filled_amount": 0.0, "fill_marker_version": 1,
        "reverse_action": "buy", "reverse_day": 2, "reverse_base_t": base_t,
        "reverse_t_target": planned_target, "cycle_id": "C2",
    }
    if rule:
        buy_meta.update({"reverse_t_rule": 2, "reverse_splits": SPLITS})
    return {
        "T": t,
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": "C2"},
        "orders_meta": {
            "RSELL": {
                "side": "SELL", "total_qty": 10, "processed_filled_qty": 0,
                "processed_filled_amount": 0.0, "fill_marker_version": 1,
                "reverse_action": "sell", "reverse_day": 2, "reverse_base_t": base_t,
                "reverse_t_factor": 0.9, "cycle_id": "C2",
            },
            "RBUY": buy_meta,
        },
    }


def _row(odno, ts, qty, total, amount):
    return {
        "odno": odno, "ord_datetime_utc": ts,
        "ft_ccld_qty": str(qty), "ft_ccld_unpr3": "50.00",
        "ft_ccld_amt3": f"{amount:.2f}", "nccs_qty": str(total - qty),
    }


def _history(sell_qty, buy_qty, ts_sell="2026-11-17T09:30:22+00:00", ts_buy="2026-11-17T09:30:22+00:00"):
    return [
        _row("RSELL", ts_sell, sell_qty, 10, sell_qty * 50.0),
        _row("RBUY", ts_buy, buy_qty, 10, buy_qty * 50.0),
    ]


def test_sell_unfilled_buy_filled_uses_prior_t_not_post_sell_t():
    """11-17 SOXL 실측 재현: 매도 미체결 + 매수 전량 → T + (20−T)×0.25 (+0.2745), 계획값 +0.747 이 아니다."""
    state = _state(18.9022)
    reconcile_reverse_fills(state, _history(0, 10))
    assert state["T"] == pytest.approx(18.9022 + (20 - 18.9022) * 0.25, abs=1e-4)
    meta = state["orders_meta"]["RBUY"]
    assert meta["reverse_t_target_settled"] is True
    assert meta["reverse_t_target_planned"] == pytest.approx(0.747, abs=1e-3)
    assert meta["reverse_t_target_basis_t"] == pytest.approx(18.9022)


def test_sell_full_buy_full_matches_original_sequence():
    """매도·매수 모두 체결: T×0.9 → +(20−T×0.9)×0.25 (기존 규칙과 같은 결과)."""
    state = _state(18.9022)
    reconcile_reverse_fills(state, _history(10, 10))
    after_sell = round(18.9022 * 0.9, 4)
    assert state["T"] == pytest.approx(after_sell + (20 - after_sell) * 0.25, abs=1e-3)


def test_sell_partial_buy_full_uses_partially_reduced_t():
    state = _state(18.0)
    reconcile_reverse_fills(state, _history(5, 10))
    t_after_sell = 18.0 - 18.0 * 0.1 * 0.5      # 매도 50% 체결분만 반영
    assert state["T"] == pytest.approx(t_after_sell + (20 - t_after_sell) * 0.25, abs=1e-3)


def test_tie_timestamp_buy_sorted_after_sell_even_if_odno_sorts_first():
    """같은 초에 접수돼도 매도를 먼저 정산한다 (odno 문자열 정렬이 매수를 앞세워도)."""
    state = _state(18.0)
    state["orders_meta"]["AAA_BUY"] = state["orders_meta"].pop("RBUY")
    state["orders_meta"]["ZZZ_SELL"] = state["orders_meta"].pop("RSELL")
    history = [
        _row("AAA_BUY", "2026-11-17T09:30:22+00:00", 10, 10, 500.0),
        _row("ZZZ_SELL", "2026-11-17T09:30:22+00:00", 10, 10, 500.0),
    ]
    reconcile_reverse_fills(state, history)
    after_sell = round(18.0 * 0.9, 4)
    assert state["T"] == pytest.approx(after_sell + (20 - after_sell) * 0.25, abs=1e-3)


def test_partial_buy_fills_split_same_total_as_single_fill():
    """매수를 50%+50%로 나눠 체결해도 한 번에 100% 체결과 같은 T (기준 T 를 첫 체결에서 확정)."""
    once = _state(18.0)
    reconcile_reverse_fills(once, _history(0, 10))

    split = _state(18.0)
    reconcile_reverse_fills(split, _history(0, 5, ts_sell="2026-11-17T09:30:22+00:00", ts_buy="2026-11-17T09:30:22+00:00"))
    first_t = split["T"]
    reconcile_reverse_fills(split, _history(0, 10))
    assert first_t == pytest.approx(18.0 + 2.0 * 0.25 * 0.5, abs=1e-3)
    assert split["T"] == pytest.approx(once["T"], abs=1e-3)
    assert split["T"] == pytest.approx(18.5, abs=1e-3)


def test_late_buy_fill_keeps_settled_target_and_is_not_resettled():
    """확정 후 늦은 체결은 확정된 목표값에 누적 체결비율만 적용한다 (기준 T 재계산 없음, 중복 확정 없음)."""
    state = _state(18.0)
    reconcile_reverse_fills(state, _history(0, 5))
    meta = state["orders_meta"]["RBUY"]
    settled = (meta["reverse_t_target"], meta["reverse_t_target_basis_t"], meta["reverse_t_target_planned"])
    reconcile_reverse_fills(state, _history(0, 10))
    assert (meta["reverse_t_target"], meta["reverse_t_target_basis_t"], meta["reverse_t_target_planned"]) == settled
    assert state["T"] == pytest.approx(18.0 + settled[0], abs=1e-3)


def test_unfilled_buy_does_not_settle_or_change_t():
    state = _state(18.0)
    reconcile_reverse_fills(state, _history(0, 0))
    assert state["T"] == 18.0
    assert "reverse_t_target_settled" not in state["orders_meta"]["RBUY"]


def test_legacy_order_without_rule_keeps_planned_target():
    """기존 주문(표식 없음)은 계획 목표값 그대로 (기존 동작 호환)."""
    state = _state(18.9022, rule=False)
    reconcile_reverse_fills(state, _history(0, 10))
    assert state["T"] == pytest.approx(18.9022 + 0.747, abs=1e-3)
    assert "reverse_t_target_settled" not in state["orders_meta"]["RBUY"]


def test_invalid_splits_holds_instead_of_guessing():
    state = _state(18.0)
    state["orders_meta"]["RBUY"]["reverse_splits"] = "x"
    reconcile_reverse_fills(state, _history(0, 10))
    assert state["T"] == 18.0
    assert state.get("_settlement_hold")


def test_forty_split_uses_its_own_splits():
    state = _state(38.0)
    state["orders_meta"]["RBUY"]["reverse_splits"] = 40
    state["orders_meta"]["RBUY"]["reverse_t_target"] = 1.0
    reconcile_reverse_fills(state, _history(0, 10))
    assert state["T"] == pytest.approx(38.0 + (40 - 38.0) * 0.25, abs=1e-3)


def test_additional_buy_orders_never_raise_t():
    """추가매수(reverse_t_target=0)는 T 를 올리지 않는다 (규칙 표식 없음)."""
    state = _state(18.0)
    state["orders_meta"]["RADD"] = {
        "side": "BUY", "total_qty": 1, "processed_filled_qty": 0,
        "processed_filled_amount": 0.0, "fill_marker_version": 1,
        "reverse_action": "buy", "reverse_day": 2, "reverse_base_t": 18.0,
        "reverse_t_target": 0.0, "cycle_id": "C2", "is_additional": True,
    }
    history = _history(0, 0) + [_row("RADD", "2026-11-17T09:30:23+00:00", 1, 1, 40.0)]
    reconcile_reverse_fills(state, history)
    assert state["T"] == 18.0


def test_strategy_buy_order_carries_rule_markers(monkeypatch):
    """주문 생성 단계: 쿼터매수에는 규칙 표식과 분할수, 추가매수에는 표식이 없다."""
    from strategy import execute_reverse_mode

    monkeypatch.setattr("strategy._confirmed_prev_close", lambda *a, **k: (1.0, "2026-11-10"))
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    monkeypatch.setattr("strategy._reverse_ladder_supported", lambda: True)
    state = {"T": 18.9022, "reverse_mode": {"day_count": 1, "cumulative_sell_proceeds": 0.0},
             "close_prices": [100.0]}
    result = execute_reverse_mode(
        broker=None, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
        position_qty=325, avg_price=192.149, orderable_cash=10000.0, last_price=118.0, state=state,
    )
    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    quarter = next(o for o in buys if "[추가매수]" not in o["comment"])
    assert quarter["reverse_t_rule"] == 2 and quarter["reverse_splits"] == 20
    assert all("reverse_t_rule" not in o for o in buys if "[추가매수]" in o["comment"])


def test_verifier_independently_recomputes_settled_target():
    """독립 검증기: 기록된 목표값이 직전 T 기준 재계산과 다르면 실패, 같으면 통과."""
    import verify_run

    def evidence(recorded_target):
        return {
            "T_before": 18.9022, "net_invested_before": 0.0, "orders": [{
                "odno": "RBUY", "side": "매수", "qty_delta": 10, "amount_delta": 500.0,
                "reverse": True, "reverse_t_target": recorded_target, "reverse_t_rule": 2,
                "reverse_splits": 20, "fill_fraction": 1.0, "fill_fraction_delta": 1.0,
            }],
            "fills": [{"odno": "RBUY", "side": "매수", "qty": 10, "amount": 500.0, "reverse_action": "buy"}],
        }

    good_problems, good_t, _ = verify_run._recompute_expected(evidence(round((20 - 18.9022) * 0.25, 4)))
    assert good_t == pytest.approx(18.9022 + 0.2745, abs=1e-3)
    assert not [p for p in good_problems if "쿼터매수 T 효과" in p]

    bad_problems, _, _ = verify_run._recompute_expected(evidence(0.747))   # 매도 체결 가정값
    assert any("쿼터매수 T 효과 불일치" in p for p in bad_problems)


def _evidence_for(sell_qty, buy_qty):
    from trading_bot import build_settlement_evidence
    state = _state(18.0)
    state["net_invested"] = 1000.0
    state["last_updated"] = "2026-11-16T09:00:00+00:00"
    state["settlement_ledger"] = {"start_T": 18.0, "entries": []}
    before = deepcopy(state)
    history = _history(sell_qty, buy_qty)
    for row in history:
        row["sll_buy_dvsn_cd_name"] = "매수" if row["odno"] == "RBUY" else "매도"
    reconcile_reverse_fills(state, history)
    state["_settlement_deltas"] = state.pop("_reverse_settlement_deltas", [])
    return build_settlement_evidence("SOXL", state, history, mode="LIVE", before=before, balance_qty=110)


def test_verifier_detects_omitted_quarter_buy_t_effect():
    """Codex P2: 쿼터매수 T 효과 원장 항목을 통째로 빼고 T 를 그대로 보고해도 통과하면 안 된다."""
    import verify_run
    evidence = _evidence_for(0, 10)
    assert verify_run._verify_evidence_ordered(evidence) == []
    evidence["ledger_after"] = {"start_T": 18.0, "entries": []}
    evidence["T_after"] = 18.0
    evidence["applied_seq"] = []
    for order in evidence["orders"]:
        order["ledger_seq"] = None
        order["t_delta"] = 0.0
        order["ledger_delta"] = 0.0
    problems = verify_run._verify_evidence_ordered(evidence)
    assert problems, "누락된 T 효과는 검증 실패여야 합니다"
