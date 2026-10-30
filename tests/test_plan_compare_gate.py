"""fail-closed 계획 비교 게이트 회귀 테스트.

고정하려는 회귀(외부 리뷰 P1):
1. 승인(APPROVE) → 실행(EXECUTE) → 검증(VERIFY) 이 **같은 기준**으로 계획을 대조해야
   합니다. 기준은 `pipeline.compare_plan_orders()` / `compare_state_patches()` 한 곳에
   있고, 허용오차는 **수량·가격·금액에만** 적용합니다.
2. 리버스 단계/배율/T 기준값(`reverse_action`/`reverse_day`/`reverse_base_t`/
   `reverse_t_factor`/`reverse_t_target`)과 `t_target` 은 시세 드리프트 대상이 아니라
   **정합성 대상**입니다 — 허용오차 없이 정확히 일치해야 합니다.
3. `reverse_mode` 상태 변경은 승인에 없으면 실행에서 추가할 수 없고, 승인에 있으면
   제거할 수 없습니다 (전략 상태머신·사이클·리버스 단계가 승인 없이 바뀌는 경로 차단).
4. `REVIEWED_PLAN_HASH` 는 사람이 검토한 plan_hash 를 APPROVE(승인 파일 쓰기 **전**)와
   EXECUTE(주문 실행 **전**)에서 대조합니다 — 불일치는 중단, 빈 값은 기존 동작(no-op).

실제 브로커 API·주문·운영 state 파일은 사용하지 않습니다 (conftest 가 외부 HTTP/소켓과
`state._STATE_FILE` 을 격리합니다).
"""
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
for path in (_repo_root, _src_path):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pipeline
import trading_bot
from broker.base import Balance, PurchaseAmount
from state import canonical_state_hash


# ── 공통 헬퍼 ─────────────────────────────────────────────────────


def _policy(price_pct=0.01, price_abs=0.05, qty=0):
    """승인 시점에 고정되는 허용오차 정책 (테스트 기본값)."""
    return {"price_pct": price_pct, "price_abs": price_abs, "qty": qty}


def _loc_buy(qty=3, price=52.0, t_target=0.5):
    """일반모드 LOC 매수 주문 (strategy.py 가 만드는 실제 필드 형태)."""
    return {
        "side": "BUY",
        "quantity": qty,
        "price": price,
        "order_type": "LOC",
        "comment": "전반전 별지점 매수 — 체결 시 T += 0.5",
        "t_target": t_target,
    }


def _reverse_sell(day=1, qty=8, price=50.0, base_t=19.0, factor=0.9):
    """리버스 1일차 MOC 매도 (strategy.py execute_reverse_mode())."""
    return {
        "side": "SELL",
        "quantity": qty,
        "price": price,
        "order_type": "MOC",
        "comment": f"리버스모드 {day}일차 MOC 매도 (20분할 1/10)",
        "t_target": 0.0,
        "reverse_action": "sell",
        "reverse_day": day,
        "reverse_base_t": base_t,
        "reverse_t_factor": factor,
    }


def _reverse_buy(day=2, qty=2, price=45.5, base_t=17.1, reverse_t_target=0.725):
    """리버스 2일차+ 쿼터매수 (strategy.py execute_reverse_mode())."""
    return {
        "side": "BUY",
        "quantity": qty,
        "price": price,
        "order_type": "LOC",
        "comment": f"리버스모드 {day}일차 쿼터매수 (잔금/4)",
        "t_target": 0.0,
        "reverse_action": "buy",
        "reverse_day": day,
        "reverse_base_t": base_t,
        "reverse_t_target": reverse_t_target,
    }


def _state_patch(**overrides):
    """승인/재계산 state_patch 예시."""
    patch = {
        "T": 18.0,
        "reverse_mode": {"active": True, "day_count": 2, "cycle_id": "c1"},
        "close_prices": [50.0, 50.5, 51.0, 50.25, 52.0],
    }
    patch.update(overrides)
    return patch


def _joined(problems):
    return " | ".join(problems)


# ── strict_equal ─────────────────────────────────────────────────


class TestStrictEqual:
    def test_int_and_float_are_equal(self):
        # given: 같은 값의 int / float
        # when: 허용오차 없는 비교
        # then: 숫자 표현 차이는 같게 봅니다 (JSON 왕복 후에도 유지).
        assert pipeline.strict_equal(18, 18.0) is True
        assert pipeline.strict_equal(0.5, 0.5) is True

    def test_bool_is_not_a_number(self):
        # given: True 와 1 (파이썬에선 동등하지만 의미가 다름)
        # when / then: bool 은 값 타입까지 구분합니다.
        assert pipeline.strict_equal(True, 1) is False
        assert pipeline.strict_equal(False, 0) is False
        assert pipeline.strict_equal(True, True) is True

    def test_dict_ignores_key_order_but_not_key_set(self):
        # given: 키 순서만 다른 동등한 딕셔너리 / 키가 하나 빠진 딕셔너리
        left = {"a": 1, "b": [1, 2]}
        same_values_other_order = {"b": [1, 2], "a": 1}
        missing_key = {"a": 1}
        # when / then
        assert pipeline.strict_equal(left, same_values_other_order) is True
        assert pipeline.strict_equal(left, missing_key) is False

    def test_list_length_and_order_matter(self):
        # given: 같은 값이지만 순서/길이가 다른 close_prices
        assert pipeline.strict_equal([50, 51], [51, 50]) is False
        assert pipeline.strict_equal([50, 51], [50, 51, 52]) is False
        assert pipeline.strict_equal([50, 51], [50.0, 51.0]) is True

    def test_nested_structure(self):
        # given: reverse_mode 중첩 딕셔너리
        left = {"active": True, "cycle": {"id": "c1", "day": 2}}
        right = {"active": True, "cycle": {"id": "c1", "day": 2}}
        drifted = {"active": True, "cycle": {"id": "c1", "day": 3}}
        # when / then
        assert pipeline.strict_equal(left, right) is True
        assert pipeline.strict_equal(left, drifted) is False

    def test_missing_key_is_detected_at_nested_level(self):
        # given: 중첩 딕셔너리에서 리버스 필드가 누락된 경우
        # when / then: 키 집합 비교로 누락을 잡습니다.
        assert pipeline.strict_equal(
            {"reverse": {"active": True}}, {"reverse": {}}
        ) is False


# ── _exact_field_problem (필드 존재/값 비교) ──────────────────────


class TestExactFieldProblem:
    def test_both_missing_is_not_a_problem(self):
        # given: 일반 주문 — 양쪽 모두 리버스 필드가 없음
        # when: 허용오차 없는 필드 비교
        # then: 문제 없음 (일반 주문은 리버스 필드가 없는 게 정상).
        assert pipeline._exact_field_problem(
            1, {"side": "BUY"}, {"side": "BUY"}, "reverse_day", "",
            "승인", "재계산",
        ) is None

    def test_none_is_treated_as_missing(self):
        # given: 한쪽은 `None`(값 없음), 다른 쪽은 키 부재
        # when / then: `_compare_field` 가 둘 다 "값 없음"으로 정규화합니다.
        assert pipeline._exact_field_problem(
            1, {"reverse_day": None}, {}, "reverse_day", "", "승인", "재계산",
        ) is None

    def test_one_side_missing_is_a_problem(self):
        # given: 승인에는 리버스 필드가 있고 재계산에는 없음
        # when: 허용오차 없는 필드 비교
        # then: "존재 불일치"로 차단됩니다 (허용오차 대상이 아님).
        problem = pipeline._exact_field_problem(
            2,
            {"reverse_action": "sell"},
            {"side": "SELL"},
            "reverse_action",
            "",
            "승인",
            "재계산",
        )
        assert problem is not None
        assert "reverse_action 필드 존재 불일치" in problem
        assert "드리프트 대상이 아닙니다" in problem

    def test_different_value_is_a_problem_without_tolerance(self):
        # given: 리버스 일차가 1 → 2 로 바뀐 계획
        # when / then: 허용오차 없이 차단됩니다.
        problem = pipeline._exact_field_problem(
            1, {"reverse_day": 1}, {"reverse_day": 2}, "reverse_day", "",
            "승인", "재계산",
        )
        assert problem is not None
        assert "reverse_day 불일치" in problem
        assert "허용오차 없음" in problem

    def test_t_target_message_names_t_basis(self):
        # given: T 가산 기준값(t_target)만 다른 계획
        # when / then: 메시지가 "T 가산 기준값"이라고 알려 줍니다.
        problem = pipeline._exact_field_problem(
            1, {"t_target": 0.5}, {"t_target": 1.0}, "t_target", "",
            "승인", "재계산",
        )
        assert problem is not None
        assert "T 가산 기준값" in problem

    def test_int_float_difference_is_allowed_for_exact_numeric(self):
        # given: 같은 값의 int/float (JSON 왕복에서 생길 수 있는 차이)
        # when / then: 오탐이 아닙니다.
        assert pipeline._exact_field_problem(
            1, {"t_target": 1}, {"t_target": 1.0}, "t_target", "",
            "승인", "재계산",
        ) is None


# ── compare_plan_orders ──────────────────────────────────────────


class TestComparePlanOrders:
    def test_normal_drift_within_tolerance_passes(self):
        # given: 승인 후 시세가 허용오차 안에서 움직인 일반 주문 (가격 +0.3)
        approved = [_loc_buy(price=52.0)]
        rebuilt = [_loc_buy(price=52.3)]
        # when: 승인 vs 재계산 비교 (가격 허용 ±0.52, 금액 허용 ±1.56)
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 정상 드리프트는 통과합니다.
        assert problems == []

    def test_order_count_mismatch_is_blocked(self):
        # given: 승인 1건 / 재계산 2건
        approved = [_loc_buy()]
        rebuilt = [_loc_buy(), _loc_buy()]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 개수 불일치는 즉시 차단 (주문별 비교는 하지 않음).
        assert len(problems) == 1
        assert "주문 개수 불일치" in problems[0]

    def test_side_type_comment_are_exact(self):
        # given: side/order_type/comment 가 달라진 계획
        approved = [_loc_buy()]
        rebuilt = [_loc_buy()]
        rebuilt[0]["side"] = "SELL"
        rebuilt[0]["order_type"] = "MOC"
        rebuilt[0]["comment"] = "다른 주문"
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 세 필드 모두 정확 일치가 필요합니다.
        text = _joined(problems)
        assert "side 불일치" in text
        assert "order_type 불일치" in text
        assert "comment 불일치" in text

    def test_quantity_within_tolerance_is_not_reported(self):
        # given: 수량 허용오차 1주, 실제 차이 1주
        approved = [_loc_buy(qty=3)]
        rebuilt = [_loc_buy(qty=4)]
        # when: 승인 vs 재계산 비교
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy(qty=1))
        # then: 수량 자체는 허용 범위라 "수량 불일치"가 나오지 않습니다.
        # (금액 검사는 가격×수량이므로 수량이 늘면 금액도 커집니다 — 별도 항목)
        assert not any("수량 불일치" in problem for problem in problems)

    def test_quantity_over_tolerance_is_blocked(self):
        # given: 수량 허용오차 0주인데 3 → 4
        approved = [_loc_buy(qty=3)]
        rebuilt = [_loc_buy(qty=4)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then
        assert any("수량 불일치" in problem for problem in problems)

    def test_price_over_tolerance_is_blocked(self):
        # given: 허용 ±0.52 인데 가격이 +1.0 움직임
        approved = [_loc_buy(price=52.0)]
        rebuilt = [_loc_buy(price=53.0)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 가격(과 금액) 불일치로 차단합니다.
        assert any("가격 불일치" in problem for problem in problems)

    def test_reverse_day_change_is_blocked(self):
        # given: 리버스 1일차 → 2일차로 바뀐 계획 (가격/수량/주문유형은 동일)
        approved = [_reverse_sell(day=1)]
        rebuilt = [_reverse_sell(day=2)]
        # when: 리버스 일차만 다름
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 리버스 단계는 허용오차 없이 차단됩니다.
        assert any("reverse_day 불일치" in problem for problem in problems)

    def test_reverse_t_factor_change_is_blocked(self):
        # given: 매도 배율 0.9 → 0.95 (T 배율이 달라지면 정산이 달라짐)
        approved = [_reverse_sell(factor=0.9)]
        rebuilt = [_reverse_sell(factor=0.95)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then
        assert any("reverse_t_factor 불일치" in problem for problem in problems)

    def test_reverse_base_t_change_is_blocked(self):
        # given: 배율 적용 기준 T 19.0 → 17.1
        approved = [_reverse_sell(base_t=19.0)]
        rebuilt = [_reverse_sell(base_t=17.1)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then
        assert any("reverse_base_t 불일치" in problem for problem in problems)

    def test_reverse_field_missing_in_rebuilt_is_blocked(self):
        # given: 승인은 리버스 매도, 재계산은 리버스 필드가 빠진 같은 주문
        approved = [_reverse_sell()]
        rebuilt = [_reverse_sell()]
        for key in ("reverse_action", "reverse_day", "reverse_base_t", "reverse_t_factor"):
            rebuilt[0].pop(key)
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 한쪽에만 있는 리버스 필드는 존재 불일치로 차단됩니다.
        text = _joined(problems)
        for key in ("reverse_action", "reverse_day", "reverse_base_t", "reverse_t_factor"):
            assert f"{key} 필드 존재 불일치" in text

    def test_reverse_field_added_in_rebuilt_is_blocked(self):
        # given: 승인은 일반 매수인데 재계산은 리버스 매수 필드까지 붙은 경우
        approved = [_loc_buy()]
        rebuilt = [_loc_buy()]
        rebuilt[0].update(
            {"reverse_action": "buy", "reverse_day": 2, "reverse_base_t": 17.1}
        )
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 승인에 없던 리버스 필드 추가도 차단됩니다.
        text = _joined(problems)
        assert "reverse_action 필드 존재 불일치" in text
        assert "reverse_day 필드 존재 불일치" in text
        assert "reverse_base_t 필드 존재 불일치" in text

    def test_t_target_difference_is_blocked(self):
        # given: T 가산 기준값 0.5 → 1.0 (주문별 T 반영 근거가 달라짐)
        approved = [_loc_buy(t_target=0.5)]
        rebuilt = [_loc_buy(t_target=1.0)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then: 허용오차 없이 차단됩니다.
        assert any("t_target 불일치" in problem for problem in problems)
        assert any("T 가산 기준값" in problem for problem in problems)

    def test_reverse_t_target_difference_is_blocked(self):
        # given: 리버스 쿼터매수 T 증가분 0.725 → 0.9
        approved = [_reverse_buy(reverse_t_target=0.725)]
        rebuilt = [_reverse_buy(reverse_t_target=0.9)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then
        assert any("reverse_t_target 불일치" in problem for problem in problems)

    def test_policy_missing_only_checks_count(self):
        # given: 허용오차 정책이 없는 비교 (호출 측이 이미 fail-closed 처리)
        approved = [_loc_buy(price=52.0)]
        rebuilt = [_loc_buy(price=99.0)]
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, None)
        # then: 정책이 없으면 개수만 확인합니다 (기존 동작 유지).
        assert problems == []

    def test_prefix_and_labels_are_used_in_messages(self):
        # given: VERIFY(승인 vs 전송) 경로의 라벨
        approved = [_reverse_sell(factor=0.9)]
        rebuilt = [_reverse_sell(factor=0.95)]
        # when
        problems = pipeline.compare_plan_orders(
            approved, rebuilt, _policy(),
            prefix="전송 ", approved_label="승인", rebuilt_label="전송",
        )
        # then: 메시지가 어느 단계 비교인지 알려 줍니다.
        text = _joined(problems)
        assert text.startswith("전송 주문 1")
        assert "승인=" in text and "전송=" in text


# ── compare_state_patches ────────────────────────────────────────


class TestCompareStatePatches:
    def test_equivalent_patch_with_different_key_order_passes(self):
        # given: 키 순서만 다른 동등 패치 (dict 는 순서 무관)
        approved = _state_patch()
        rebuilt = {
            "close_prices": list(approved["close_prices"]),
            "reverse_mode": deepcopy(approved["reverse_mode"]),
            "T": 18,
        }
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 순서는 무시하고 값으로만 비교합니다 (T: int/float 허용).
        assert problems == []

    def test_reverse_mode_value_change_is_blocked(self):
        # given: 리버스 2일차 → 3일차 (전략 상태머신이 바뀜)
        approved = _state_patch()
        rebuilt = _state_patch(
            reverse_mode={"active": True, "day_count": 3, "cycle_id": "c1"}
        )
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 허용오차 없이 차단됩니다.
        assert any("reverse_mode 불일치" in problem for problem in problems)

    def test_reverse_mode_bool_is_strict(self):
        # given: active True vs 1 (파이썬 동등이지만 의미가 다름)
        approved = _state_patch(reverse_mode={"active": True})
        rebuilt = _state_patch(reverse_mode={"active": 1})
        # when / then
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        assert any("reverse_mode 불일치" in problem for problem in problems)

    def test_reverse_mode_added_without_approval_is_blocked(self):
        # given: 승인 패치에는 reverse_mode 가 없는데 재계산은 리버스를 켬
        approved = {"T": 18.0}
        rebuilt = {"T": 18.0, "reverse_mode": {"active": True, "day_count": 1}}
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 승인에 없는 전략 상태 변경은 추가할 수 없습니다.
        text = _joined(problems)
        assert "상태 변경 reverse_mode 추가" in text
        assert "전략 상태를 바꾸는 변경은 승인에 없으면 추가할 수 없습니다" in text

    def test_reverse_mode_removed_in_rebuilt_is_blocked(self):
        # given: 승인에는 리버스 상태가 있는데 재계산에서 빠짐
        approved = _state_patch()
        rebuilt = {"T": 18.0, "close_prices": list(approved["close_prices"])}
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 승인된 상태 변경은 실행에서 제거할 수 없습니다.
        text = _joined(problems)
        assert "상태 변경 reverse_mode 누락" in text
        assert "승인된 상태 변경은 실행에서 제거할 수 없습니다" in text

    def test_other_key_added_without_approval_is_blocked(self):
        # given: 승인에 없던 position_qty 변경이 재계산에만 있음
        approved = _state_patch()
        rebuilt = _state_patch(position_qty=81)
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then
        assert any("상태 변경 position_qty 추가" in problem for problem in problems)

    def test_scalar_change_is_blocked(self):
        # given: T 18.0 → 18.5 (주문 없이 T 만 바뀌는 경로)
        approved = _state_patch()
        rebuilt = _state_patch(T=18.5)
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then
        assert any("상태 변경 T 불일치" in problem for problem in problems)

    def test_close_prices_last_price_drift_within_tolerance_passes(self):
        # given: 마지막 원소 = 이번 실행의 현재가 (허용 ±0.52)
        approved = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, 52.0])
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, 52.3])
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 정상 드리프트는 통과합니다.
        assert problems == []

    def test_close_prices_last_price_over_tolerance_is_blocked(self):
        # given: 마지막 원소가 +1.0 움직임 (허용 ±0.52)
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, 53.0])
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then
        text = _joined(problems)
        assert "close_prices 불일치" in text
        assert "현재가 불일치" in text

    def test_close_prices_history_difference_is_blocked(self):
        # given: 마지막이 아닌 이력 종가가 바뀜 (state 가 잠겨 있으므로 불가능해야 함)
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.5, 50.25, 52.0])
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then
        text = _joined(problems)
        assert "이력 종가 불일치" in text
        assert "이력은 드리프트 대상이 아닙니다" in text

    def test_close_prices_length_difference_is_blocked(self):
        # given: 승인 5개 / 재계산 4개
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25])
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then
        assert any("길이 불일치" in problem for problem in problems)

    def test_close_prices_not_bound_in_approval_is_allowed(self):
        # given: 구버전 계획 — 승인 패치에 close_prices 키가 없음
        approved = {"T": 18.0, "reverse_mode": {"active": True}}
        rebuilt = {"T": 18.0, "reverse_mode": {"active": True},
                   "close_prices": [50.0, 52.0]}
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 바인딩되지 않은 키의 추가는 허용합니다 (승인 내용은 plan_hash 고정).
        assert problems == []

    def test_none_valued_keys_are_ignored(self):
        # given: 값이 None 인 키는 "변경 없음"입니다 (apply_state_patch 규칙).
        approved = {"T": 18.0, "position_qty": None}
        rebuilt = {"T": 18.0}
        # when / then
        assert pipeline.compare_state_patches(approved, rebuilt, _policy()) == []

    def test_non_dict_patch_is_reported(self):
        # given: 패치가 dict 가 아님 (형식 오류)
        # when
        problems = pipeline.compare_state_patches(
            ["T"], {"T": 18.0}, _policy(), rebuilt_label="재계산"
        )
        # then: 비교 불가를 문제로 보고합니다 (조용히 통과 금지).
        assert any("형식이 올바르지 않습니다" in problem for problem in problems)

    def test_both_patches_empty_passes(self):
        # given: 양쪽 모두 변경 없음
        # when / then
        assert pipeline.compare_state_patches(None, {}, _policy()) == []


# ── t_target 실증 (전략 산출값이 양쪽에서 비트 동일) ──────────────


def _reverse_quarter_t_target(T, splits, sell_factor):
    """strategy.py execute_reverse_mode() 의 리버스 쿼터매수 T 증가분 계산식.

    `reverse_t_target = round((splits - new_T_sell) * 0.25, 4)` 와 동일한 표현입니다.
    (strategy.py:293 — 값이 소수 4자리로 고정되므로 JSON 왕복 후에도 비트 동일)
    """
    new_t_sell = round(T * sell_factor, 4)
    return round((splits - new_t_sell) * 0.25, 4)


class TestTTargetBitIdentity:
    def test_approved_and_rebuilt_compute_bit_identical_value(self):
        # given: 승인 계획(파일에 JSON 으로 저장·복사)과 실행 직전 재계산 계획
        approved_value = _reverse_quarter_t_target(19.0, 20, 0.9)
        rebuilt_value = _reverse_quarter_t_target(19.0, 20, 0.9)
        # when: 승인 값은 계획 파일을 거칩니다 (float → JSON → float)
        round_tripped = json.loads(json.dumps({"t": approved_value}))["t"]
        # then: 두 값은 비트 단위로 같습니다 (허용오차 없이 비교 가능).
        assert round_tripped.hex() == rebuilt_value.hex()
        assert pipeline.compare_plan_orders(
            [_reverse_buy(reverse_t_target=round_tripped)],
            [_reverse_buy(reverse_t_target=rebuilt_value)],
            _policy(),
        ) == []

    @pytest.mark.parametrize("literal", [0.0, 0.5, 1.0])
    def test_literal_t_targets_survive_json_round_trip(self, literal):
        # given: strategy.py 가 리터럴로 넣는 t_target 값들 (0.0 / 0.5 / 1.0)
        round_tripped = json.loads(json.dumps({"t": literal}))["t"]
        # when / then: JSON 왕복이 값을 바꾸지 않아 정확 비교를 통과합니다.
        assert round_tripped.hex() == float(literal).hex()
        assert pipeline.compare_plan_orders(
            [_loc_buy(t_target=round_tripped)],
            [_loc_buy(t_target=literal)],
            _policy(),
        ) == []

    @pytest.mark.parametrize("literal", [0.0, 0.5, 1.0])
    def test_missing_t_target_on_one_side_fails(self, literal):
        # given: 재계산 주문에서 t_target 필드가 빠짐 (등록 시 T 가산 근거 소실)
        approved_order = _loc_buy(t_target=literal)
        rebuilt_order = _loc_buy(t_target=literal)
        rebuilt_order.pop("t_target")
        # when
        problems = pipeline.compare_plan_orders(
            [approved_order], [rebuilt_order], _policy()
        )
        # then: 필드 존재 불일치로 차단됩니다 (T 가산 근거가 사라진 주문은 실행 금지).
        assert any("t_target 필드 존재 불일치" in problem for problem in problems)

    def test_t_target_zero_is_not_treated_as_missing(self):
        # given: 리버스 매도 주문의 t_target 은 0.0 (T 가산 없음) — "값 없음"이 아님
        # when: 양쪽 모두 0.0 인 비교
        # then: 통과합니다 (0.0 을 필드 누락으로 오판하지 않음).
        assert pipeline.compare_plan_orders(
            [_reverse_sell()], [_reverse_sell()], _policy()
        ) == []
        # and: 0.0 → 0.5 로 바뀌면 값 불일치로 차단됩니다.
        rebuilt = [_reverse_buy()]
        rebuilt[0]["t_target"] = 0.5
        assert any(
            "t_target 불일치" in problem
            for problem in pipeline.compare_plan_orders(
                [_reverse_buy()], rebuilt, _policy()
            )
        )


# ── REVIEWED_PLAN_HASH 바인딩 (_assert_reviewed_plan_hash_matches) ──


class TestReviewedPlanHashBinding:
    def test_env_value_is_normalized(self, monkeypatch):
        # given: 대소문자/공백이 섞인 env 값
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "  AbC123  ")
        # when / then: strip + lower 로 정규화합니다.
        assert trading_bot._reviewed_plan_hash() == "abc123"

    def test_env_missing_is_empty(self, monkeypatch):
        # given: REVIEWED_PLAN_HASH 미설정 (자동 경로)
        monkeypatch.delenv("REVIEWED_PLAN_HASH", raising=False)
        # when / then
        assert trading_bot._reviewed_plan_hash() == ""

    def test_matching_hash_passes(self, monkeypatch, capsys):
        # given: 사람이 검토한 값과 계획 파일 값이 같음 (대소문자만 다름)
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "ABCDEF012345")
        # when: APPROVE/EXECUTE 직전 대조
        trading_bot._assert_reviewed_plan_hash_matches(
            "abcdef012345", "[APPROVE] TQQQ-plan"
        )
        # then: 예외 없이 통과하고 확인 로그를 남깁니다.
        assert "plan_hash 일치 확인" in capsys.readouterr().out

    def test_mismatched_hash_aborts(self, monkeypatch):
        # given: 사람이 본 해시와 계획 파일 해시가 다름 (가격·수량이 바뀐 계획)
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "aaaa1111")
        # when / then: 예외로 중단합니다 (호출 측이 1 로 끝냄).
        with pytest.raises(RuntimeError) as error:
            trading_bot._assert_reviewed_plan_hash_matches(
                "bbbb2222", "[EXECUTE] TQQQ-plan"
            )
        assert "plan_hash 불일치" in str(error.value)
        assert "[EXECUTE] TQQQ-plan" in str(error.value)

    def test_empty_env_is_noop_for_backward_compatibility(self, monkeypatch):
        # given: REVIEWED_PLAN_HASH 가 비어 있음 (기존/자동 경로)
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "")
        # when: 계획 해시가 없어도
        # then: 아무것도 하지 않습니다 (하위호환 — 워크플로가 필수 여부를 강제).
        assert trading_bot._assert_reviewed_plan_hash_matches(
            "", "[APPROVE] legacy"
        ) is None
        assert trading_bot._assert_reviewed_plan_hash_matches(
            "anything", "[APPROVE] legacy"
        ) is None

    def test_expected_but_actual_missing_aborts(self, monkeypatch):
        # given: 사람은 검토했는데 계획/승인 파일에 plan_hash 가 없음
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "aaaa1111")
        # when / then: 대조 불가 = 중단 (조용히 통과하지 않음).
        with pytest.raises(RuntimeError) as error:
            trading_bot._assert_reviewed_plan_hash_matches(None, "[EXECUTE] x")
        assert "plan_hash 가 없어" in str(error.value)

    def test_uses_constant_time_compare(self, monkeypatch):
        # given: hmac.compare_digest 를 spy 로 교체
        calls = []
        real = trading_bot.hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        monkeypatch.setattr(
            trading_bot, "hmac", SimpleNamespace(compare_digest=spy)
        )
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "DEADBEEF")
        # when
        trading_bot._assert_reviewed_plan_hash_matches("deadbeef", "[APPROVE] x")
        # then: `==` 가 아니라 상수시간 비교를 정규화된 값으로 호출합니다.
        assert calls == [("deadbeef", "deadbeef")]


# ── APPROVE / EXECUTE 단계의 해시 게이트 (fail-closed 순서) ───────


def _base_state(**overrides):
    state = {
        "T": 0.0,
        "last_updated": "2026-10-20T11:14:03+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
        "position_qty": None,
        "sell_t_effects": {},
        "last_settlement_id": "run-test",
        "last_settlement_evidence": "",
        "settled_session": "2026-10-23",
        "execution_session": "2026-10-26",
    }
    state.update(overrides)
    return state


def _fixed_now():
    # 2026-10-26(월) 09:30 UTC = 05:30 ET (프리장, 실행 세션 당일).
    return datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)


def _make_plan(state, orders):
    plan = pipeline.OrderPlan(
        plan_id="",
        symbol="TQQQ",
        exchange="NAS",
        session="",
        orders=orders,
        state_hash=canonical_state_hash(state),
        plan_hash="",
        settlement_run_id="run-test",
        created_at_utc="",
        expires_at_utc="",
    )
    pipeline._prepare_plan_identity(
        plan, now=_fixed_now(), ttl_seconds=32400
    )
    return plan


def _make_broker():
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = []
    broker.get_balance.return_value = Balance(quantity=0, avg_price=0.0)
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=10000.0)
    broker.exchange_code.return_value = "NASD"
    broker.supports_open_orders_query = False
    broker.close = MagicMock()
    return broker


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """APPROVE/EXECUTE 디스패처 실행에 필요한 경로·state·브로커 격리."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
    monkeypatch.delenv("RUN_LOCK_FD", raising=False)
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.delenv("PLAN_FILE", raising=False)
    monkeypatch.delenv("APPROVAL_FILE", raising=False)
    monkeypatch.setattr(trading_bot, "BROKER", "mockbroker")
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")

    state = _base_state()
    monkeypatch.setattr(
        trading_bot, "load_state", lambda symbol: deepcopy(state)
    )
    return SimpleNamespace(root=tmp_path, state=state)


class TestApprovePhaseHashGate:
    def test_mismatched_hash_blocks_before_approval_file(self, gate_env, monkeypatch):
        # given: PREVIEW 가 만든 유효한 계획 파일 + 사람이 본 것과 다른 해시
        plan = _make_plan(gate_env.state, [_loc_buy()])
        pipeline.write_order_plan(plan, plans_dir=str(gate_env.root / "plans"))
        monkeypatch.setenv("PLAN_ID", plan.plan_id)
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "0000000000")
        # when: APPROVE 단계 실행
        result = trading_bot._dispatch_pipeline_phase("APPROVE", _make_broker())
        # then: 중단(1)되고 승인 파일이 **하나도** 생기지 않습니다.
        assert result == 1
        approvals = gate_env.root / "approvals"
        assert list(approvals.glob("*.json")) == []

    def test_matching_hash_writes_approval(self, gate_env, monkeypatch):
        # given: 사람이 검토한 plan_hash 와 계획 파일 해시가 일치
        plan = _make_plan(gate_env.state, [_loc_buy()])
        pipeline.write_order_plan(plan, plans_dir=str(gate_env.root / "plans"))
        monkeypatch.setenv("PLAN_ID", plan.plan_id)
        monkeypatch.setenv("REVIEWED_PLAN_HASH", plan.plan_hash.upper())
        # when
        result = trading_bot._dispatch_pipeline_phase("APPROVE", _make_broker())
        # then: 승인이 정상 기록되고 그 plan_hash 가 그대로 복사됩니다.
        assert result == 0
        approvals = list((gate_env.root / "approvals").glob("*.json"))
        assert len(approvals) == 1
        approval = json.loads(approvals[0].read_text(encoding="utf-8"))
        assert approval["plan_hash"] == plan.plan_hash
        assert approval["consumed"] is False

    def test_missing_env_keeps_legacy_behavior(self, gate_env, monkeypatch):
        # given: REVIEWED_PLAN_HASH 미설정 (하위호환 경로)
        plan = _make_plan(gate_env.state, [_loc_buy()])
        pipeline.write_order_plan(plan, plans_dir=str(gate_env.root / "plans"))
        monkeypatch.setenv("PLAN_ID", plan.plan_id)
        monkeypatch.delenv("REVIEWED_PLAN_HASH", raising=False)
        # when
        result = trading_bot._dispatch_pipeline_phase("APPROVE", _make_broker())
        # then: 기존처럼 승인됩니다 (게이트는 값이 있을 때만 작동).
        assert result == 0
        assert list((gate_env.root / "approvals").glob("*.json"))


class TestExecutePhaseHashGate:
    def _write_approval(self, root, plan_hash):
        path = root / "approval.json"
        path.write_text(
            json.dumps({"plan_id": "TQQQ-plan", "plan_hash": plan_hash}),
            encoding="utf-8",
        )
        return path

    def test_mismatched_hash_blocks_before_execute(self, gate_env, monkeypatch):
        # given: 승인 파일의 plan_hash 와 사람이 검토한 값이 다름
        approval_path = self._write_approval(gate_env.root, "bbbb2222")
        monkeypatch.setenv("APPROVAL_FILE", str(approval_path))
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "aaaa1111")
        executed = []
        monkeypatch.setattr(
            trading_bot,
            "execute_approved_plan",
            lambda broker, ref: executed.append(ref) or {"failed": 0},
        )
        # when: EXECUTE 단계 실행
        result = trading_bot._dispatch_pipeline_phase("EXECUTE", _make_broker())
        # then: 주문 실행 함수에 **도달하지 않고** 실패로 끝납니다.
        assert executed == []
        assert result == 1

    def test_matching_hash_reaches_execute(self, gate_env, monkeypatch):
        # given: 승인 파일 해시와 검토 값이 일치
        approval_path = self._write_approval(gate_env.root, "aaaa1111")
        monkeypatch.setenv("APPROVAL_FILE", str(approval_path))
        monkeypatch.setenv("REVIEWED_PLAN_HASH", "AAAA1111")
        executed = []
        monkeypatch.setattr(
            trading_bot,
            "execute_approved_plan",
            lambda broker, ref: executed.append(ref) or {"failed": 0},
        )
        # when
        result = trading_bot._dispatch_pipeline_phase("EXECUTE", _make_broker())
        # then: 게이트를 통과해 실행 단계로 진행합니다.
        assert result == 0
        assert len(executed) == 1

    def test_missing_env_keeps_legacy_behavior(self, gate_env, monkeypatch):
        # given: REVIEWED_PLAN_HASH 미설정
        approval_path = self._write_approval(gate_env.root, "cccc3333")
        monkeypatch.setenv("APPROVAL_FILE", str(approval_path))
        monkeypatch.delenv("REVIEWED_PLAN_HASH", raising=False)
        executed = []
        monkeypatch.setattr(
            trading_bot,
            "execute_approved_plan",
            lambda broker, ref: executed.append(ref) or {"failed": 0},
        )
        # when
        result = trading_bot._dispatch_pipeline_phase("EXECUTE", _make_broker())
        # then
        assert result == 0
        assert len(executed) == 1


# ── P1(리뷰): 비유한(NaN/Infinity) 가격·금액 fail-closed ─────────────
#
# 회귀: `abs(a - b) > tolerance` 는 NaN 에서 항상 False 입니다. 그래서 승인·재계산
# 주문 가격이 NaN 이면 **허용오차 비교를 그대로 통과**했습니다 (검증 우회).
# `float()` 파싱 실패도 비교 대신 예외로 터져 "문제 보고"가 아니었습니다.
# 지금은 비교 입력(가격·수량·금액·close_prices 현재가)을 **유한한 실수**로
# 제한하고, 아니면 문제로 보고합니다 (fail-closed).


class TestNonFiniteNumbersFailClosed:
    def test_nan_price_used_to_pass_and_is_now_blocked(self):
        # given: 승인 가격이 NaN, 재계산 가격은 정상 (수정 전에는 통과)
        approved = [_loc_buy(price=float("nan"))]
        rebuilt = [_loc_buy(price=52.0)]
        policy = _policy()
        # 회귀 재현: 수정 전 비교식은 NaN 을 통과시켰습니다 (False = 초과 아님).
        tolerance = pipeline.price_tolerance(52.0, policy)
        assert not (abs(float("nan") - 52.0) > tolerance)
        # when
        problems = pipeline.compare_plan_orders(approved, rebuilt, policy)
        # then: 가격을 숫자로 확정할 수 없으므로 차단합니다.
        assert any("가격이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_nan_price_on_both_sides_is_blocked(self):
        # given: 양쪽 모두 NaN — 수정 전에는 "차이 없음"으로 보였습니다
        assert not (abs(float("nan") - float("nan")) > 0.05)
        approved = [_loc_buy(price=float("nan"))]
        rebuilt = [_loc_buy(price=float("nan"))]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("가격이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_infinite_price_is_blocked(self):
        # given: 무한대 가격 (브로커가 받아줄 수 없는 값)
        approved = [_loc_buy(price=float("inf"))]
        rebuilt = [_loc_buy(price=52.0)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("가격이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_non_numeric_price_is_reported_not_raised(self):
        # given: 숫자로 읽을 수 없는 가격 문자열
        approved = [_loc_buy(price="52.0abc")]
        rebuilt = [_loc_buy(price=52.0)]
        # when: 예외가 아니라 문제 보고여야 합니다 (조용한 통과·크래시 금지)
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        # then
        assert any("가격이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_numeric_string_price_is_accepted(self):
        # given: JSON 왕복 등으로 문자열이 된 숫자 가격 (과차단 금지)
        approved = [_loc_buy(price="52.34")]
        rebuilt = [_loc_buy(price=52.35)]
        # when / then: 허용오차 안이면 통과합니다.
        assert pipeline.compare_plan_orders(approved, rebuilt, _policy()) == []

    def test_nan_quantity_is_blocked(self):
        # given: 수량이 NaN (금액 = 가격×수량도 NaN 이 되어 비교가 무력화됨)
        approved = [_loc_buy(qty=float("nan"))]
        rebuilt = [_loc_buy(qty=3)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("수량이 유한한 숫자가 아닙니다" in p for p in problems), problems
        assert any("금액이 유한한 숫자가 아닙니다" in p or "수량이 유한한" in p
                   for p in problems)

    def test_infinite_quantity_is_blocked(self):
        # given: 수량 무한대 (int() 변환에서 OverflowError 가 나던 값)
        approved = [_loc_buy(qty=float("inf"))]
        rebuilt = [_loc_buy(qty=3)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("수량이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_negative_price_is_blocked(self):
        # given: 음수 가격 (허용 범위 밖)
        approved = [_loc_buy(price=-52.0)]
        rebuilt = [_loc_buy(price=52.0)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("가격이 음수입니다" in p for p in problems), problems

    def test_negative_quantity_is_blocked(self):
        # given: 음수 수량
        approved = [_loc_buy(qty=-3)]
        rebuilt = [_loc_buy(qty=3)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("수량이 음수입니다" in p for p in problems), problems

    def test_zero_price_market_order_is_not_overblocked(self):
        # given: 가격 0 은 "시장가/미지정" 표기입니다 (build_order_plan 출력 규칙).
        approved = [_loc_buy(price=0)]
        rebuilt = [_loc_buy(price=0.0)]
        # when / then: 정상 케이스를 과차단하지 않습니다.
        assert pipeline.compare_plan_orders(approved, rebuilt, _policy()) == []

    def test_zero_price_does_not_swallow_a_real_price(self):
        # given: 가격 0 → 52 (시장가가 지정가로 바뀐 드리프트는 차단)
        approved = [_loc_buy(price=0)]
        rebuilt = [_loc_buy(price=52.0)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("가격 불일치" in p for p in problems), problems

    def test_decimal_prices_within_tolerance_pass(self):
        # given: 정상 소수 가격의 미세 드리프트 ($0.01)
        approved = [_loc_buy(price=52.34)]
        rebuilt = [_loc_buy(price=52.35)]
        # when / then
        assert pipeline.compare_plan_orders(approved, rebuilt, _policy()) == []

    def test_overflowing_amount_is_blocked(self):
        # given: 유한한 가격·수량이지만 금액이 무한대로 넘칩니다
        approved = [_loc_buy(qty=3, price=1e308)]
        rebuilt = [_loc_buy(qty=3, price=1e308)]
        # when / then
        problems = pipeline.compare_plan_orders(approved, rebuilt, _policy())
        assert any("금액이 유한한 숫자가 아닙니다" in p for p in problems), problems

    def test_nan_current_close_price_is_blocked(self):
        # given: close_prices 마지막 원소(이번 실행 현재가)가 NaN
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, float("nan")])
        # when
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        # then: 허용오차 비교를 통과하지 못하고 차단됩니다.
        text = _joined(problems)
        assert "현재가를 유한한 숫자로 읽을 수 없습니다" in text, problems

    def test_infinite_current_close_price_is_blocked(self):
        # given: 현재가가 무한대
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, float("inf")])
        # when / then
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        assert any(
            "현재가를 유한한 숫자로 읽을 수 없습니다" in p for p in problems
        ), problems

    def test_nan_history_close_price_is_blocked(self):
        # given: 이력 종가에 NaN (정확 일치 대상)
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, float("nan"), 51.0, 50.25, 52.0])
        # when / then: NaN 은 자기 자신과도 같지 않으므로 이력 불일치로 차단됩니다.
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        assert any("이력 종가 불일치" in p for p in problems), problems

    def test_negative_current_close_price_is_blocked(self):
        # given: 음수 현재가
        approved = _state_patch()
        rebuilt = _state_patch(close_prices=[50.0, 50.5, 51.0, 50.25, -52.0])
        # when / then
        problems = pipeline.compare_state_patches(approved, rebuilt, _policy())
        assert any("현재가가 음수입니다" in p for p in problems), problems


class TestFiniteNumberHelper:
    """비교 입력 정규화 헬퍼의 경계값 (과차단/과통과 동시 방지)."""

    def test_missing_values_become_default(self):
        # given: "값 없음" 표기들 (시장가 price 0, 수량 미지정)
        # when / then: 기존처럼 0 으로 봅니다.
        assert pipeline._finite_number(None) == 0.0
        assert pipeline._finite_number("") == 0.0
        assert pipeline._finite_number("   ") == 0.0
        assert pipeline._finite_number(None, 1.5) == 1.5

    def test_finite_values_are_returned(self):
        assert pipeline._finite_number(52) == 52.0
        assert pipeline._finite_number("52.34") == 52.34
        assert pipeline._finite_number(0) == 0.0

    def test_non_finite_and_bool_are_rejected(self):
        # given: 비유한 값/불리언/파싱 불가
        # when / then: 모두 None (fail-closed 신호)
        assert pipeline._finite_number(float("nan")) is None
        assert pipeline._finite_number(float("inf")) is None
        assert pipeline._finite_number("abc") is None
        assert pipeline._finite_number(True) is None
        assert pipeline._finite_number(False) is None
