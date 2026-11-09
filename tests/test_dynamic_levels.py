"""
`src/dynamic_levels.py` 단위 테스트 — 네트워크·state·주문 접촉 없음.

검증 범위:
- ATR(Wilder 14기간) 수기 계산 대조, 확정봉 부족/불량봉 처리
- 정책 D 버킷 경계 + 히스테리시스
- 정책 E 시드 사용률(kappa) 축소
- 예산 합계(compute_budget_exact)가 근사식이 아닌 실제 합계인지
- 가상 추가매수 주문의 ΔT=0 (t_target=0.0) · 수량 1
- levels=3 회귀: build_additional_loc_prices() 직접 호출과 동일 결과
"""

import math
import os
import sys
from pathlib import Path

import pytest

# strategy/config 를 import 하려면 시드 환경변수가 필요합니다 (config.py 가 모듈 로드 시 검증).
# 실제 .env 값이 있으면 그 값을 그대로 쓰고, 없을 때만 실험용 기본값을 채웁니다.
os.environ.setdefault("TQQQ_SEED", "40000")
os.environ.setdefault("SOXL_SEED", "20000")

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dynamic_levels import (  # noqa: E402
    ATR_PERIOD,
    DEFAULT_KAPPA_ADD,
    MIN_ATR_BARS,
    atr_ratio,
    build_virtual_additional_orders,
    compute_atr14,
    compute_budget_exact,
    decide_levels,
    max_capital_use,
    select_dynamic_d,
    select_dynamic_e,
    select_fixed,
)
from strategy import build_additional_loc_prices  # noqa: E402

# ── ATR 합성 봉 ───────────────────────────────────────────────────────
# 손으로 계산한 TR (첫봉은 H-L, 이후 max(H-L, |H-Cprev|, |L-Cprev|)):
#   2, 2, 2, 2.5, 2, 2, 2, 1.5, 1.5, 1.6, 1.0, 0.8, 0.8, 0.7  ← 14개 (초기 평균)
#   0.8                                                        ← 15번째 (평활 1회)
# ATR14 = 22.4 / 14 = 1.6 → (1.6 × 13 + 0.8) / 14 = 21.6 / 14 ≈ 1.542857
_ATR_TRS_14 = [2, 2, 2, 2.5, 2, 2, 2, 1.5, 1.5, 1.6, 1.0, 0.8, 0.8, 0.7]
_ATR_TR_15 = 0.8
_EXPECTED_ATR = ((sum(_ATR_TRS_14) / ATR_PERIOD) * (ATR_PERIOD - 1) + _ATR_TR_15) / ATR_PERIOD


def _bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def _synthetic_bars_15():
    """위 TR 목록이 나오도록 만든 합성 확정봉 15개 (오래된 순)."""
    ohlc = [
        (10, 11, 9, 10),
        (10, 12, 10, 11.5),
        (11.5, 12, 10, 10.5),
        (10.5, 13, 10.5, 12.5),
        (12.5, 13, 11, 11.5),
        (11.5, 12, 10, 10.5),
        (10.5, 11.5, 9.5, 10),
        (10, 10.5, 9, 9.5),
        (9.5, 10, 8.5, 9),
        (9, 9.8, 8.2, 8.5),
        (8.5, 9, 8, 8.2),
        (8.2, 8.6, 7.8, 8),
        (8, 8.4, 7.6, 7.9),
        (7.9, 8.2, 7.5, 7.7),
        (7.7, 8.0, 7.2, 7.4),
    ]
    return [_bar(*row) for row in ohlc]


# ── ATR ──────────────────────────────────────────────────────────────


def test_atr14_matches_hand_calculated_wilder_smoothing():
    atr = compute_atr14(_synthetic_bars_15())
    assert atr is not None
    # 1.6 → 평활 1회 → 1.5428571...
    assert atr == pytest.approx(_EXPECTED_ATR, rel=0, abs=1e-12)
    assert atr == pytest.approx(1.5428571428571427, rel=0, abs=1e-12)


@pytest.mark.parametrize("count", [0, 1, 13, 14])
def test_atr14_requires_15_confirmed_bars(count):
    bars = _synthetic_bars_15()[:count] if count else []
    assert compute_atr14(bars) is None


def test_atr14_rejects_empty_and_none():
    assert compute_atr14(None) is None
    assert compute_atr14([]) is None


def test_atr14_uses_last_15_bars_only():
    older = [_bar(100, 200, 1, 150) for _ in range(5)]
    assert compute_atr14(older + _synthetic_bars_15()) == pytest.approx(
        _EXPECTED_ATR, rel=0, abs=1e-12
    )


def test_atr14_returns_none_for_invalid_bars():
    """0/음수 봉·고가<저가·키 누락은 조용히 건너뛰지 않고 None (기간이 몰래 바뀌는 것 방지)."""
    zero_low = _synthetic_bars_15()
    zero_low[-1] = _bar(0, 0, 0, 0)
    assert compute_atr14(zero_low) is None

    negative = _synthetic_bars_15()
    negative[3] = _bar(10, 12, -1, 10)
    assert compute_atr14(negative) is None

    inverted = _synthetic_bars_15()
    inverted[2] = _bar(10, 8, 9, 10)  # 고가 < 저가
    assert compute_atr14(inverted) is None

    missing_key = _synthetic_bars_15()
    missing_key[5] = {"o": 10, "h": 11, "l": 9}
    assert compute_atr14(missing_key) is None


def test_atr14_min_bars_constant_is_15():
    assert MIN_ATR_BARS == 15
    assert ATR_PERIOD == 14


def test_atr_ratio_basic_and_invalid_inputs():
    assert atr_ratio(1.5, 60.0) == pytest.approx(0.025)
    assert atr_ratio(0.0, 60.0) == 0.0
    assert atr_ratio(None, 60.0) is None
    assert atr_ratio(1.5, None) is None
    assert atr_ratio(1.5, 0) is None
    assert atr_ratio(1.5, -10) is None
    assert atr_ratio(-1.5, 60.0) is None
    assert atr_ratio(float("nan"), 60.0) is None


# ── 정책 D ────────────────────────────────────────────────────────────

_T1 = 0.04
_T2 = 0.07
_DELTA = 0.02


def test_d_without_indicator_falls_back_to_3():
    assert select_dynamic_d(None, _T1, _T2) == (3, "fallback:no-indicator")
    levels, reason = select_dynamic_d(float("nan"), _T1, _T2)
    assert levels == 3 and reason.startswith("fallback:")


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, 3),
        (0.01, 3),
        (0.04, 3),  # 경계: r == t1 은 3
        (0.0405, 5),
        (0.07, 5),  # 경계: r == t2 는 5
        (0.0705, 7),
        (0.2, 7),
    ],
)
def test_d_bucket_boundaries(ratio, expected):
    levels, reason = select_dynamic_d(ratio, _T1, _T2)
    assert levels == expected
    assert reason.startswith("bucket:")
    assert "fallback" not in reason


def test_d_hysteresis_keeps_7_until_r_drops_below_t2_minus_delta():
    extended_low = _T2 - _DELTA  # 0.05
    levels, reason = select_dynamic_d(0.06, _T1, _T2, prev_level=7, delta=_DELTA)
    assert levels == 7
    assert reason.startswith("hysteresis:prev_level=7")

    levels, reason = select_dynamic_d(extended_low + 1e-9, _T1, _T2, prev_level=7, delta=_DELTA)
    assert levels == 7

    levels, reason = select_dynamic_d(extended_low - 1e-9, _T1, _T2, prev_level=7, delta=_DELTA)
    assert levels == 5
    assert reason.startswith("bucket:")


def test_d_hysteresis_keeps_5_above_t1_minus_delta():
    extended_low = _T1 - _DELTA  # 0.02
    levels, reason = select_dynamic_d(0.03, _T1, _T2, prev_level=5, delta=_DELTA)
    assert levels == 5
    assert reason.startswith("hysteresis:prev_level=5")

    levels, _reason = select_dynamic_d(extended_low + 1e-9, _T1, _T2, prev_level=5, delta=_DELTA)
    assert levels == 5

    levels, reason = select_dynamic_d(extended_low - 1e-9, _T1, _T2, prev_level=5, delta=_DELTA)
    assert levels == 3
    assert reason.startswith("bucket:r<=t1")


def test_d_previous_level_3_does_not_extend_any_bucket():
    assert select_dynamic_d(0.03, _T1, _T2, prev_level=3, delta=_DELTA)[0] == 3
    assert select_dynamic_d(0.06, _T1, _T2, prev_level=3, delta=_DELTA)[0] == 5


def test_d_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        select_dynamic_d(0.05, 0.07, 0.04)  # t1 > t2
    with pytest.raises(ValueError):
        select_dynamic_d(0.05, _T1, _T2, delta=-0.01)
    with pytest.raises(ValueError):
        select_dynamic_d(0.05, _T1, _T2, delta=_T2 + 0.01)  # delta > t2
    with pytest.raises(ValueError):
        select_dynamic_d(0.05, None, _T2)


# ── 정책 E (예산 2단 구조) ────────────────────────────────────────────

_KAPPA_ADD = DEFAULT_KAPPA_ADD  # 0.02 (후보값·미확정)
_R = 20000.0
_NORMAL = 100.0


def test_e_remaining_seed_nonpositive_returns_zero_levels():
    """R <= 0 이면 추가매수 0단계 (예전처럼 3단계로 되돌리지 않음)."""
    levels, reason = select_dynamic_e(
        0.2, _T1, _T2, 0.0, 40000.0, _NORMAL, {3: []}, kappa_add=_KAPPA_ADD
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")
    assert "no-seed" in reason

    levels, reason = select_dynamic_e(
        -1.0, _T1, _T2, -500.0, 40000.0, _NORMAL, {3: []}, kappa_add=_KAPPA_ADD
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")


def test_e_kappa_add_boundary_includes_exact_equality():
    """Σ추가매수 == kappa_add × R 은 포함(<=), 초과면 제외. 정상매수는 분자에서 제외."""
    # kappa_add=0.02, R=20000 → 한도 400
    by_level = {3: [400.0], 5: [400.0, 100.0]}
    levels, reason = select_dynamic_e(
        0.2, _T1, _T2, _R, 40000.0, _NORMAL, by_level, kappa_add=_KAPPA_ADD
    )
    assert levels == 3  # 400 == 400 (경계 포함), L=5 는 500 > 400
    assert reason.startswith("budget: L=3")
    assert "kappa_add" in reason and "total=" in reason
    assert "L_D=7" in reason

    # 정상매수 500 을 더해도 추가매수 전용 예산에는 영향이 없어야 합니다.
    levels, _reason = select_dynamic_e(
        0.2, _T1, _T2, _R, 40000.0, 500.0, by_level, kappa_add=_KAPPA_ADD
    )
    assert levels == 3


def test_e_kappa_exceeded_shrinks_to_zero_not_three():
    """최소 후보(L=3)도 초과하면 3을 강제하지 않고 0단계까지 내립니다."""
    levels, reason = select_dynamic_e(
        0.2, _T1, _T2, 1000.0, 40000.0, _NORMAL, {3: [500.0]}, kappa_add=_KAPPA_ADD
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")
    assert "kappa_add" in reason


def test_e_cannot_exceed_d_level():
    """D가 3을 고르면 시드가 넉넉해도 5/7로 늘리지 않습니다 (축소 전용)."""
    by_level = {3: [], 5: [], 7: []}
    levels, reason = select_dynamic_e(
        0.01, _T1, _T2, 10_000_000.0, 40000.0, _NORMAL, by_level, kappa_add=_KAPPA_ADD
    )
    assert levels == 3
    assert reason.startswith("budget: L=3")
    assert "L_D=3" in reason


def test_e_shrink_is_step_only_not_price_change():
    """축소는 단계 수만 바꿉니다 — 근거 문자열에 가격/수량 변경 개념이 없어야 합니다."""
    levels, reason = select_dynamic_e(
        0.2,
        _T1,
        _T2,
        15000.0,
        40000.0,
        _NORMAL,
        {3: [], 5: [300.0], 7: [300.0, 200.0]},
        kappa_add=_KAPPA_ADD,
    )
    assert levels == 5  # 한도 300, L=5 300 포함 / L=7 500 초과
    assert reason.startswith("budget: L=5")


def test_e_with_no_candidate_prices_chooses_zero():
    levels, reason = select_dynamic_e(
        0.2, _T1, _T2, 40000.0, 40000.0, _NORMAL, {}, kappa_add=_KAPPA_ADD
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")


def test_e_rejects_invalid_kappa_add():
    with pytest.raises(ValueError):
        select_dynamic_e(
            0.2, _T1, _T2, 40000.0, 40000.0, _NORMAL, {3: []}, kappa_add=-0.01
        )
    with pytest.raises(ValueError):
        select_dynamic_e(
            0.2, _T1, _T2, 40000.0, 40000.0, _NORMAL, {3: []}, kappa_add=float("nan")
        )


# ── 정책 E: 전체 예산(B_avail) vs 추가매수 예산 ───────────────────────


def test_e_total_budget_shrinks_additional_levels_only():
    """전체 예산이 빠듯하면 추가매수 단계만 줄고, 정상매수 금액은 그대로입니다."""
    by_level = {3: [100.0], 5: [100.0, 50.0]}
    # normal 100 + L=3 100 = 200 > B_avail 150 → L=0 (normal 100 은 유지)
    levels, reason = select_dynamic_e(
        0.2,
        _T1,
        _T2,
        _R,
        40000.0,
        _NORMAL,
        by_level,
        kappa_add=_KAPPA_ADD,
        budget_available=150.0,
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")

    # B_avail 250 이면 L=3 (100+100=200 <= 250), L=5 는 250 == 250 (경계 포함)
    levels, reason = select_dynamic_e(
        0.2,
        _T1,
        _T2,
        _R,
        40000.0,
        _NORMAL,
        by_level,
        kappa_add=_KAPPA_ADD,
        budget_available=250.0,
    )
    assert levels == 5
    assert "total=" in reason


def test_e_budget_below_normal_keeps_zero_levels_and_does_not_mutate_inputs():
    """B_avail < normal 이면 추가매수 0단계 — 정상매수는 건드리지 않습니다."""
    by_level = {3: [10.0]}
    snapshot = {3: list(by_level[3])}
    levels, reason = select_dynamic_e(
        0.2,
        _T1,
        _T2,
        _R,
        40000.0,
        _NORMAL,
        by_level,
        kappa_add=_KAPPA_ADD,
        budget_available=_NORMAL - 1.0,
    )
    assert levels == 0
    assert reason.startswith("budget: L=0")
    assert "infeasible" in reason
    assert by_level == snapshot  # 순수 함수 — 입력 불변


def test_e_reserved_already_deducted_avoids_double_counting():
    """open_reserved 가 B_avail 에 이미 차감됐는지에 따라 판정이 달라집니다."""
    kwargs = dict(
        r=0.2,
        t1=_T1,
        t2=_T2,
        remaining_seed=_R,
        seed=40000.0,
        normal_amount=_NORMAL,
        additional_prices_by_L={3: []},
        kappa_add=_KAPPA_ADD,
        open_reserved=50.0,
        budget_available=120.0,
    )
    # 미차감(기본 False): normal 100 + reserved 50 = 150 > 120 → L=0 infeasible
    levels, reason = select_dynamic_e(**kwargs)
    assert levels == 0
    assert "infeasible" in reason
    # 이미 차감됨(True): 100 <= 120 → L=3 가능
    levels, reason = select_dynamic_e(reserved_already_deducted=True, **kwargs)
    assert levels == 3
    assert "total=100.00<=120.00" in reason
    assert "reserved_already_deducted=True" in reason


def test_e_prev_e_is_recorded_separately_from_d_prev_level():
    levels, reason = select_dynamic_e(
        0.2,
        _T1,
        _T2,
        _R,
        40000.0,
        _NORMAL,
        {3: [], 5: [], 7: []},
        kappa_add=_KAPPA_ADD,
        prev_level=7,
        prev_e=5,
    )
    assert levels == 7  # D 상한은 prev_level=7 히스테리시스로 7
    assert "prev_e=5" in reason
    assert "L_D=7" in reason


def test_e_rejects_negative_nan_inf_amounts_and_prices():
    base = dict(
        r=0.2,
        t1=_T1,
        t2=_T2,
        remaining_seed=_R,
        seed=40000.0,
        normal_amount=_NORMAL,
        additional_prices_by_L={3: []},
        kappa_add=_KAPPA_ADD,
    )
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            select_dynamic_e(**{**base, "normal_amount": bad})
        with pytest.raises(ValueError):
            select_dynamic_e(**{**base, "open_reserved": bad})
        with pytest.raises(ValueError):
            select_dynamic_e(**{**base, "budget_available": bad})
        with pytest.raises(ValueError):
            select_dynamic_e(**{**base, "additional_prices_by_L": {3: [bad]}})
    with pytest.raises(ValueError):
        select_dynamic_e(**{**base, "remaining_seed": float("nan")})
    with pytest.raises(ValueError):
        select_dynamic_e(**{**base, "remaining_seed": float("inf")})


def test_e_split_count_comparison_changes_l_without_ranking():
    """동일 κ_add 에서 20분할 vs 40분할(단위금액 차이)로 L 이 달라질 수 있음 (우열 판정 아님)."""
    # 같은 seed·가격 가정에서 20분할은 1회매수금이 2배 → 사다리 합계도 2배.
    seed = 20000.0
    price = 60.0
    kappa_add = 0.01  # 비교용 후보값 (미확정)
    normal_amount = 60.0  # 정상매수 1주 가정 (분할 수와 무관하게 동일)
    levels_seen = {}
    for splits in (20, 40):
        unit_amount = seed / splits
        base_qty = max(int(unit_amount // price), 1)
        prices_by_level = {
            level: [
                float(p)
                for _step, p in build_additional_loc_prices(
                    unit_amount, base_qty, level, price
                )
            ]
            for level in range(0, 8)
        }
        levels, reason = select_dynamic_e(
            0.2, _T1, _T2, seed, seed, normal_amount, prices_by_level,
            kappa_add=kappa_add, budget_available=seed,
        )
        assert reason.startswith("budget: L=")
        levels_seen[splits] = levels
    # 20분할(단위금액 큼)이 40분할보다 같거나 작은 단계 — 우열이 아니라 예산 제약 차이입니다.
    assert levels_seen[20] != levels_seen[40], levels_seen
    assert 0 <= levels_seen[20] <= 7 and 0 <= levels_seen[40] <= 7


# ── 예산 ─────────────────────────────────────────────────────────────


def test_budget_exact_is_plain_sum_not_log_approximation():
    prices = [100.5, 33.333, 0.5, 1e-4]
    total = compute_budget_exact(prices)
    assert total == sum(prices)  # 같은 순서의 단순 합계와 비트 단위 동일
    assert total == pytest.approx(134.3331, abs=1e-12)
    # 로그 근사(Σ log 후 exp)는 다른 값을 내므로, 근사식을 쓰지 않았음을 보여줍니다.
    log_approx = math.exp(sum(math.log(p) for p in prices))
    assert abs(total - log_approx) > 1e-6


def test_budget_exact_empty_and_invalid():
    assert compute_budget_exact([]) == 0.0
    assert compute_budget_exact(None) == 0.0
    for bad in (None, float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            compute_budget_exact([1.0, bad])


def test_max_capital_use_sums_three_parts():
    assert max_capital_use(100.0, [50.0, 25.0], 10.0) == pytest.approx(185.0)
    assert max_capital_use(100.0, [], 0.0) == pytest.approx(100.0)


# ── 고정 단계 검증 ────────────────────────────────────────────────────


@pytest.mark.parametrize("level", [3, 5, 7, 8])
def test_select_fixed_returns_allowed_values(level):
    assert select_fixed(level) == level


@pytest.mark.parametrize("level", [0, 1, 4, 6, 9, "3.5", None])
def test_select_fixed_rejects_other_values(level):
    with pytest.raises(ValueError):
        select_fixed(level)


# ── decide_levels ────────────────────────────────────────────────────


def test_decide_levels_fixed_policies():
    assert decide_levels("A")["levels"] == 3
    assert decide_levels("B")["levels"] == 5
    assert decide_levels("C", levels=7)["levels"] == 7
    assert decide_levels("C", levels=8)["levels"] == 8
    assert decide_levels("A")["fallback"] is False
    assert decide_levels("B")["reason"].startswith("fixed:")
    assert decide_levels("C", levels=8)["reason"].startswith("fixed:C")


def test_decide_levels_c3_is_rejected():
    with pytest.raises(ValueError):
        decide_levels("C3")


def test_decide_levels_c_rejects_other_levels():
    with pytest.raises(ValueError):
        decide_levels("C", levels=5)
    with pytest.raises(ValueError):
        decide_levels("C", levels=3)


def test_decide_levels_unknown_policy_raises():
    with pytest.raises(ValueError):
        decide_levels("Z")


def test_decide_levels_returns_indicator_fields():
    atr = compute_atr14(_synthetic_bars_15())
    result = decide_levels("D", atr=atr, close=60.0, t1=_T1, t2=_T2)
    assert set(result) == {"levels", "reason", "fallback", "atr", "atr_ratio"}
    assert result["atr"] == atr
    assert result["atr_ratio"] == pytest.approx(atr / 60.0)
    assert result["fallback"] is False


def test_decide_levels_marks_fallback_without_indicator():
    result = decide_levels("D", atr=None, atr_ratio=None, t1=_T1, t2=_T2)
    assert result["levels"] == 3
    assert result["fallback"] is True
    assert result["reason"] == "fallback:no-indicator"


def test_decide_levels_e_uses_seed_params():
    result = decide_levels(
        "E",
        atr_ratio=0.2,
        t1=_T1,
        t2=_T2,
        remaining_seed=20000.0,
        seed=40000.0,
        normal_amount=100.0,
        additional_prices_by_L={3: [], 5: [300.0], 7: [300.0, 200.0]},
        kappa_add=_KAPPA_ADD,
        budget_available=20000.0,
    )
    assert result["levels"] == 5
    assert result["reason"].startswith("budget: L=5")
    assert result["fallback"] is False


def test_decide_levels_e_budget_zero_levels_is_not_fallback():
    result = decide_levels(
        "E",
        atr_ratio=0.2,
        t1=_T1,
        t2=_T2,
        remaining_seed=20000.0,
        seed=40000.0,
        normal_amount=100.0,
        additional_prices_by_L={3: [100.0]},
        kappa_add=_KAPPA_ADD,
        budget_available=150.0,
    )
    assert result["levels"] == 0
    assert result["fallback"] is False
    assert result["reason"].startswith("budget: L=0")


def test_decide_levels_e_accepts_legacy_kappa_name():
    result = decide_levels(
        "E",
        atr_ratio=0.2,
        t1=_T1,
        t2=_T2,
        remaining_seed=20000.0,
        seed=40000.0,
        normal_amount=100.0,
        additional_prices_by_L={3: [400.0]},
        kappa=_KAPPA_ADD,
    )
    assert result["levels"] == 3


# ── 가상 주문 (ΔT=0) ─────────────────────────────────────────────────


def test_virtual_orders_are_delta_t_zero_and_single_share():
    orders = build_virtual_additional_orders(1000.0, 16, 5, 62.0, policy="B")
    assert len(orders) == 5
    for order in orders:
        assert order["t_target"] == 0.0
        assert order["quantity"] == 1
        assert order["side"] == "BUY"
        assert order["order_type"] == "LOC"
        assert order["is_additional"] is True
        assert order["virtual"] is True
        assert order["policy"] == "B"


def test_virtual_orders_prices_are_strictly_decreasing_below_ceiling():
    ceiling = 62.0
    orders = build_virtual_additional_orders(1000.0, 16, 8, ceiling, policy="C")
    prices = [order["price"] for order in orders]
    assert len(prices) == 8
    assert all(price < ceiling for price in prices)
    assert all(earlier > later for earlier, later in zip(prices, prices[1:]))
    assert [order["level"] for order in orders] == list(range(1, 9))


def test_virtual_orders_do_not_touch_state_or_bookkeeping():
    orders = build_virtual_additional_orders(1000.0, 16, 3, 62.0)
    for order in orders:
        assert "orders_meta" not in order
        assert "odno" not in order
        assert order["virtual"] is True


# ── 회귀: 기존 가격 공식과 동일 ───────────────────────────────────────


@pytest.mark.parametrize("levels", [3, 5, 7, 8])
def test_virtual_orders_match_build_additional_loc_prices(levels):
    expected = [price for _level, price in build_additional_loc_prices(1000.0, 16, levels, 62.0)]
    orders = build_virtual_additional_orders(1000.0, 16, levels, 62.0)
    assert [order["price"] for order in orders] == expected


def test_levels_3_regression_matches_direct_call():
    """levels=3 은 기존 호출과 완전히 동일해야 합니다 (가격 공식 단일 출처)."""
    direct = build_additional_loc_prices(1000.0, 16, 3, 62.0)
    virtual = build_virtual_additional_orders(1000.0, 16, 3, 62.0)
    assert len(direct) == len(virtual) == 3
    for (level, price), order in zip(direct, virtual):
        assert order["level"] == level
        assert order["price"] == price


# ── ATR: 최근 15봉 근사 vs 전체 유효 기간 Wilder 평활 비교 ──────────────

def _wilder_atr_full(bars, period=14):
    """전체 유효 기간을 사용한 Wilder 14기간 ATR (참조 구현, 테스트 전용).

    모든 봉의 TR 을 계산하고, 처음 period 개 TR 의 단순평균을 초기 ATR 로
    사용한 뒤 (prev*period-1 + TR)/period 로 재귀 평활합니다.
    """
    if not bars or len(bars) < period + 1:
        return None
    rows = []
    for bar in bars:
        high = bar["h"]; low = bar["l"]; close = bar["c"]
        if high <= 0 or low <= 0 or close <= 0 or high < low:
            return None
        rows.append((high, low, close))
    trs = [rows[0][0] - rows[0][1]]
    for i in range(1, len(rows)):
        h, l, c = rows[i]
        pc = rows[i-1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _make_ohlc_bars(n, base_close=50.0, atr_pct=0.08):
    """합성 OHLC 봉 n 개 생성 (오래된 순). ATR 비율이 약 atr_pct 가 되도록."""
    import random
    random.seed(42)
    bars = []
    close = base_close
    for i in range(n):
        tr = close * atr_pct * (0.5 + random.random())
        high = close + tr * 0.6
        low = close - tr * 0.4
        open_price = close + (random.random() - 0.5) * tr * 0.2
        bars.append({"o": open_price, "h": high, "l": low, "c": close})
        close = close * (1 + (random.random() - 0.5) * 0.02)
    return bars


def test_atr_15_bars_vs_full_period_difference():
    """최근 15봉 근사 ATR 과 전체 기간 Wilder ATR 의 차이를 측정.

    39봉(실제 메리츠 확정봉 수)과 15봉(최소 개수)으로 ATR 을 계산하고,
    절대 차이와 상대 차이를 기록합니다. 단계 결정에 영향을 줄 수 있는지
    t1/t2 임계값 근처에서 D 정책 단계도 비교합니다.
    """
    bars_39 = _make_ohlc_bars(39, base_close=52.25, atr_pct=0.085)
    bars_15 = bars_39[-15:]

    atr_15 = compute_atr14(bars_15)
    atr_full = _wilder_atr_full(bars_39)

    assert atr_15 is not None
    assert atr_full is not None

    abs_diff = abs(atr_15 - atr_full)
    rel_diff = abs_diff / atr_full if atr_full else 0.0

    # 기록용 출력 (테스트 통과/실패와 무관)
    print(f"\n  ATR(15봉)={atr_15:.4f}, ATR(전체39봉)={atr_full:.4f}")
    print(f"  절대차이={abs_diff:.4f}, 상대차이={rel_diff:.5f} ({rel_diff*100:.2f}%)")

    # D 정책 단계 결정에 차이가 있는지 확인 (t1=0.04, t2=0.07 후보)
    close = bars_39[-1]["c"]
    ratio_15 = atr_ratio(atr_15, close)
    ratio_full = atr_ratio(atr_full, close)

    level_15 = select_dynamic_d(ratio_15, 0.04, 0.07, prev_level=None, delta=0.02)[0]
    level_full = select_dynamic_d(ratio_full, 0.04, 0.07, prev_level=None, delta=0.02)[0]

    print(f"  r(15봉)={ratio_15:.5f} → D={level_15}, r(전체)={ratio_full:.5f} → D={level_full}")

    # 두 ATR 값이 너무 다르면(>20%) 단계 결정에 영향을 줄 수 있으므로 경고만 기록
    # (단정하지 않음 — 후보값 미확정이므로 우열 판정 아님)
    if rel_diff > 0.20:
        print(f"  ⚠️ ATR 상대차이 20% 초과 — 단계 결정 영향 가능성 있음 (후보값 재검토 필요)")


def test_atr_15_bars_minimum():
    """확정봉 15개 미만이면 compute_atr14 는 None 반환 (추정 금지)."""
    bars_14 = _make_ohlc_bars(14, base_close=50.0, atr_pct=0.05)
    assert compute_atr14(bars_14) is None
    assert compute_atr14([]) is None
