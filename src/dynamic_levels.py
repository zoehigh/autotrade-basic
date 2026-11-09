"""
동적 추가매수 단계(L) 선택 — 후보 실험용 **순수 함수** 모음.

이 모듈은 "추가매수 단계 수(3/5/7/8)"를 어떻게 고를지에 대한 후보 로직만 담습니다.
- I/O 없음: 파일·네트워크·브로커·state 접근 코드 없음
- 주문 실행 없음: 주문 dict를 **가상으로** 만들 뿐 전송하지 않음 (`virtual=True`)
- 가격 공식은 `strategy.build_additional_loc_prices()` 를 그대로 재사용합니다
  (가격 공식 단일 출처 유지 — 이 모듈에서 별도 공식을 만들지 않음)

정책 요약:
    A  : 고정 3단계
    B  : 고정 5단계
    C  : 고정 7 또는 8단계 (`levels` 인자로 지정, C3는 사용하지 않음)
    D  : ATR 비율(r) 버킷 + 히스테리시스 → 3/5/7
    E  : D로 상한을 정한 뒤, **두 개의 독립 예산**을 모두 만족하는 최대 단계로 축소
         ① 전체 주문 예산: normal_amount + Σ추가매수 + (미차감 open_reserved) ≤ B_avail
         ② 추가매수 전용 예산: Σ추가매수 ≤ kappa_add × remaining_seed
         정상매수는 ②의 분자에 포함하지 않습니다. ①을 넘으면 추가매수 단계만 줄입니다
         (정상매수는 건드리지 않음). 0단계(=추가매수 없음)까지 축소를 허용합니다.

지표:
    ATR은 Wilder 14기간 평활 **근사**입니다 (14봉 SMA 시드 + 1회 평활). 확정봉 15개가
    없으면 계산하지 않습니다(추정 금지). 자세한 한계는 `compute_atr14()` docstring 참고.

단계축소와 미체결 주문 (설계 문서 — 이 모듈은 주문을 취소하지 않습니다):
    정책 전환으로 단계가 줄어들어도 **기존 미체결 주문을 자동취소하지 않습니다.**
    - 축소는 "앞으로 만들 신규 주문"에만 적용합니다 (신규 주문만 축소 단계로 생성).
    - 이미 접수된 미체결 주문은 기존 운영 규칙(`trading_bot._cancel_open_bot_orders`,
      취소 → 확정 확인 → 재계획)을 그대로 따릅니다.
    - 이유: 축소 시점에 자동취소를 넣으면 체결과 취소가 경합해 부분체결 상태에서
      중복 매수/누락이 생길 수 있고, 취소 확정 판정(이력 재조회)이 필요한데
      이 모듈은 브로커를 조회하지 않는 순수 함수 계층이기 때문입니다.
"""

import math

# 확정봉 최소 개수 — 14기간 ATR을 만들려면 첫 TR(전일 종가 필요) 이후 14개가 필요합니다.
MIN_ATR_BARS = 15
ATR_PERIOD = 14

# 고정 단계로 허용하는 값 (검증용)
FIXED_LEVEL_CHOICES = (3, 5, 7, 8)

# 정책 C에서 허용하는 단계
POLICY_C_LEVELS = (7, 8)

# 추가매수 전용 예산 비율 기본값 (정책 E) — 후보값·미확정.
# 사다리 전체 합계(Σ추가매수가) 대비 기준입니다. 1회 매수금 대비 비율이 아닙니다.
DEFAULT_KAPPA_ADD = 0.02

__all__ = [
    "MIN_ATR_BARS",
    "ATR_PERIOD",
    "FIXED_LEVEL_CHOICES",
    "POLICY_C_LEVELS",
    "DEFAULT_KAPPA_ADD",
    "compute_atr14",
    "atr_ratio",
    "select_fixed",
    "select_dynamic_d",
    "compute_budget_exact",
    "max_capital_use",
    "select_dynamic_e",
    "decide_levels",
    "build_virtual_additional_orders",
]


def _finite(value):
    """float 변환 + 유한값 검사. 실패하면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _require_finite(value, name):
    """유한한 숫자여야 합니다. NaN/Infinity/숫자 아님은 ValueError."""
    number = _finite(value)
    if number is None:
        raise ValueError(f"{name}가 유효한 숫자가 아닙니다: {value!r}")
    return number


def _require_amount(value, name, default=0.0):
    """
    0 이상의 유한 금액이어야 합니다.

    None 은 `default` 로 해석합니다 (금액 미지정 = 0). 음수·NaN·Infinity 는
    조용히 통과시키지 않고 ValueError 를 냅니다.
    """
    number = _finite(default if value is None else value)
    if number is None:
        raise ValueError(f"{name}가 유효한 숫자가 아닙니다: {value!r}")
    if number < 0:
        raise ValueError(f"{name}는 0 이상이어야 합니다: {value!r}")
    return number


def compute_atr14(bars):
    """
    확정봉 OHLC에서 Wilder 14기간 ATR을 계산합니다.

    Parameters:
        bars (list[dict]): 확정봉 목록, **오래된 순**. 각 봉은 {"o","h","l","c"} 키를 가집니다.
            마지막 15봉만 사용합니다 (최근 구간 기준).

    Returns:
        float | None: ATR. 확정봉이 15개 미만이거나 값이 유효하지 않으면 None.

    계산:
        TR_1 = H1 - L1 (첫봉은 전일 종가가 없으므로 고가-저가)
        TR_i = max(H-L, |H - Cprev|, |L - Cprev|)
        ATR14 = mean(TR_1..TR_14)  → 이후 (prev*13 + TR_i)/14 로 평활

    완전수렴 아님 (중요):
        15봉만 쓰면 첫 ATR 을 14개 TR 의 단순평균(SMA)으로 **시드**하고, 15번째 봉에서
        딱 **1회**만 Wilder 평활을 적용합니다. 실제 Wilder ATR 은 이전 ATR 을 계속
        재귀 평활하므로, 장기간 누적된 완전수렴 값과는 다를 수 있습니다.
        이 모듈은 "같은 입력 → 같은 값"이라는 **내부 일관성**만 보장합니다
        (정책 버킷 판정용 근사 지표이며, 외부 차트의 ATR 과 일치시키는 것이 목적이 아님).

    유효성 (스킵하지 않고 None):
        봉을 조용히 건너뛰면 ATR 기간이 몰래 바뀌므로, 다음 중 하나라도 있으면
        **None을 반환**합니다 — 0/음수 가격, 고가 < 저가, 숫자 아님/무한대, 키 누락.
    """
    if not bars or len(bars) < MIN_ATR_BARS:
        return None

    window = bars[-MIN_ATR_BARS:]
    rows = []
    for bar in window:
        try:
            high = _finite(bar["h"])
            low = _finite(bar["l"])
            close = _finite(bar["c"])
        except (KeyError, TypeError):
            return None
        if high is None or low is None or close is None:
            return None
        if high <= 0 or low <= 0 or close <= 0:
            return None
        if high < low:
            return None
        rows.append((high, low, close))

    trs = [rows[0][0] - rows[0][1]]
    for index in range(1, len(rows)):
        high, low, _close = rows[index]
        prev_close = rows[index - 1][2]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr = sum(trs[:ATR_PERIOD]) / ATR_PERIOD
    for true_range in trs[ATR_PERIOD:]:
        atr = (atr * (ATR_PERIOD - 1) + true_range) / ATR_PERIOD
    return atr


def atr_ratio(atr, close):
    """
    ATR 비율(r) = ATR / 종가.

    Returns:
        float | None: 계산 불가(None/0 이하 종가/음수 ATR/비유한값)면 None.
    """
    atr_value = _finite(atr)
    close_value = _finite(close)
    if atr_value is None or close_value is None:
        return None
    if atr_value < 0 or close_value <= 0:
        return None
    return atr_value / close_value


def select_fixed(levels):
    """
    고정 단계 값 검증용 — 3/5/7/8은 그대로 반환합니다.

    Raises:
        ValueError: 허용 목록에 없는 값.
    """
    try:
        value = int(levels)
    except (TypeError, ValueError):
        raise ValueError(f"고정 단계는 정수여야 합니다: {levels!r}")
    if value not in FIXED_LEVEL_CHOICES:
        raise ValueError(f"고정 단계는 {FIXED_LEVEL_CHOICES} 중 하나여야 합니다: {levels!r}")
    return value


def _validate_buckets(t1, t2, delta):
    """
    버킷 임계값 검증.

    - t1, t2, delta 는 유한한 숫자
    - t1 <= t2
    - 0 <= delta <= t2  (히스테리시스 폭이 상한 버킷 폭을 넘으면 경계가 뒤집힘)
    """
    first = _finite(t1)
    second = _finite(t2)
    margin = _finite(delta) if delta is not None else 0.0
    if first is None or second is None or margin is None:
        raise ValueError(f"임계값이 유효한 숫자가 아닙니다: t1={t1!r}, t2={t2!r}, delta={delta!r}")
    if first > second:
        raise ValueError(f"t1은 t2 이하여야 합니다: t1={t1!r}, t2={t2!r}")
    if margin < 0:
        raise ValueError(f"delta는 0 이상이어야 합니다: {delta!r}")
    if margin > second:
        raise ValueError(
            f"delta는 t2 이하여야 합니다: delta={delta!r}, t2={t2!r}"
        )
    return first, second, margin


def select_dynamic_d(r, t1, t2, prev_level=None, delta=0.0):
    """
    ATR 비율(r)로 추가매수 단계를 3/5/7 중에서 고릅니다 (정책 D).

    버킷 (기본):
        r <= t1          → 3
        t1 < r <= t2     → 5
        r > t2           → 7

    히스테리시스 (경계 왕복 방지):
        prev_level == 7 이고 r > t2 - delta  → 7 유지
        prev_level == 5 이고 t1 - delta < r <= t2 → 5 유지

    Parameters:
        r (float | None): ATR 비율. None/비유한값이면 지표 없음으로 봅니다.
        t1, t2 (float): 버킷 임계값 (t1 <= t2).
        prev_level (int | None): 직전 실행의 단계 (3/5/7). 그 외 값은 무시합니다.
        delta (float): 히스테리시스 폭 (0 이상).

    Returns:
        tuple[int, str]: (단계, 근거 문자열). 근거가 fallback이면 "fallback:"으로 시작합니다.
    """
    first, second, margin = _validate_buckets(t1, t2, delta)

    ratio = _finite(r)
    if ratio is None:
        return 3, "fallback:no-indicator"

    previous = None
    if prev_level is not None:
        previous = _finite(prev_level)

    # 7 유지 → 5 유지 → 일반 버킷 순서
    if previous == 7 and ratio > second - margin:
        return 7, (
            f"hysteresis:prev_level=7 유지 "
            f"(r={ratio:.5f} > t2-delta={second - margin:.5f})"
        )
    if previous == 5 and first - margin < ratio <= second:
        return 5, (
            f"hysteresis:prev_level=5 유지 "
            f"(t1-delta={first - margin:.5f} < r={ratio:.5f} <= t2={second:.5f})"
        )
    if ratio > second:
        return 7, f"bucket:r>t2 (r={ratio:.5f}, t2={second:.5f})"
    if ratio > first:
        return 5, f"bucket:t1<r<=t2 (r={ratio:.5f}, t1={first:.5f}, t2={second:.5f})"
    return 3, f"bucket:r<=t1 (r={ratio:.5f}, t1={first:.5f})"


def compute_budget_exact(prices):
    """
    추가매수 예산 = 실제 생성된 주문가의 **정확한 합계**.

    로그 근사(Σ log)나 반올림 근사를 쓰지 않습니다 — 실제 생성된 가격을 그대로 더합니다.

    Parameters:
        prices (iterable[float]): 주문 가격 목록.

    Returns:
        float: 합계 (빈 목록이면 0.0).

    Raises:
        ValueError: 가격이 숫자가 아니거나 NaN/Infinity 이거나 **음수**인 경우
            (음수 금액이 합계를 조용히 줄이는 것을 막습니다).
    """
    total = 0.0
    for price in prices or []:
        value = _finite(price)
        if value is None:
            raise ValueError(f"가격이 유효한 숫자가 아닙니다: {price!r}")
        if value < 0:
            raise ValueError(f"가격은 0 이상이어야 합니다: {price!r}")
        total += value
    return float(total)


def max_capital_use(normal_amount, additional_prices, open_reserved=0.0):
    """
    한 실행에서 동시에 묶이는 최대 자본 = 정상매수 + 추가매수 합계 + 기존 미체결 예약분.

    Parameters:
        normal_amount (float): 정상매수 금액 (0 이상 유한값, None=0).
        additional_prices (iterable[float]): 추가매수 1주 가격 목록 (0 이상 유한값).
        open_reserved (float): 기존 미체결 주문에 이미 잡혀 있는 금액 (0 이상 유한값, None=0).

    Raises:
        ValueError: 음수·NaN·Infinity 금액/가격.
    """
    normal = _require_amount(normal_amount, "normal_amount")
    reserved = _require_amount(open_reserved, "open_reserved")
    return float(normal + compute_budget_exact(additional_prices) + reserved)


# 부동소수 비교 허용 오차 (금액 단위 USD, 1e-9 = 10억분의 1달러)
_BUDGET_EPS = 1e-9


def select_dynamic_e(
    r,
    t1,
    t2,
    remaining_seed,
    seed,
    normal_amount,
    additional_prices_by_L,
    open_reserved=0.0,
    kappa_add=DEFAULT_KAPPA_ADD,
    prev_level=None,
    delta=0.0,
    budget_available=None,
    reserved_already_deducted=False,
    prev_e=None,
):
    """
    정책 E — D로 상한 단계를 정한 뒤, **두 예산을 모두 만족하는 최대 단계**로 축소합니다.

    예산 (서로 독립):
        ① 전체 주문 예산
           normal_amount + Σ추가매수(L) + 개방예약분 ≤ budget_available(B_avail)
           여기서 개방예약분(open_reserved)은 아래 `reserved_already_deducted` 계약에 따릅니다.
        ② 추가매수 전용 예산
           Σ추가매수(L) ≤ kappa_add × remaining_seed
           **정상매수는 ②의 분자에서 제외**합니다 (정상매수는 전략이 이미 결정한 고정 지출).

    정상매수는 절대 줄이지 않습니다. ①이 빠듯하면 추가매수 단계만 0까지 줄입니다.
    후보 L 은 `0 .. base_level` (D 상한) 이며, `additional_prices_by_L` 에 없는 단계는
    후보에서 건너뜁니다 (L=0 은 항상 후보 — 추가매수 없음).

    Parameters:
        remaining_seed (float): 남은 시드 R. NaN/Infinity 는 ValueError.
            값이 0 이하이면(시드 소진) 추가매수 0단계를 반환합니다.
        seed (float): 최초 시드 (근거 기록용, 계산에는 remaining_seed 사용).
        normal_amount (float): 정상매수 금액 (0 이상 유한값, None=0).
        additional_prices_by_L (dict[int, list[float]]): 단계별 추가매수 1주 가격 목록.
        open_reserved (float): 기존 미체결 주문 예약금 (0 이상 유한값, None=0).
        kappa_add (float): 추가매수 전용 예산 비율 (기본 0.02 — **후보값·미확정**).
            사다리 전체 합계 대비 기준이며, 1회 매수금 대비 비율이 아닙니다.
        prev_level (int | None): **D 상한 히스테리시스**용 직전 D 단계 (E 선택값과 분리).
        delta (float): 히스테리시스 폭 (0 <= delta <= t2).
        budget_available (float | None): 호출자가 넘기는 실제 가용예산 B_avail.
            None 이면 `max(remaining_seed, 0)` 을 사용합니다 (잔여시드=가용예산 기본 가정).
        reserved_already_deducted (bool): open_reserved 이중차감 방지 계약. 기본 False.
            - False (기본): B_avail 에 open_reserved 가 **아직 차감되지 않았다** →
              ①의 분자에 open_reserved 를 더합니다.
            - True: B_avail 이 이미 open_reserved 를 **차감한 값**이다 →
              ①의 분자에서 open_reserved 를 제외합니다 (이중차감 방지).
        prev_e (int | None): 직전 세션 **E 최종 선택값** (진단·근거 기록용).
            D 히스테리시스용 prev_level 과 혼용하지 않습니다.

    Returns:
        tuple[int, str]: (단계 L, 근거). L 은 0..base_level 이며, 예산이 빠듯하면 0까지
        내려갑니다. 근거 문자열은 "budget: L=X (kappa_add/total ...)" 형식입니다.
        `fallback:` 접두사는 쓰지 않습니다 (예산 축소는 fallback 이 아님).
    """
    remaining = _require_finite(remaining_seed, "remaining_seed")
    limit = _finite(kappa_add)
    if limit is None or limit < 0:
        raise ValueError(f"kappa_add는 0 이상의 유효한 숫자여야 합니다: {kappa_add!r}")
    # R <= 0 이어도 금액·예산 입력은 먼저 검증합니다 (음수/NaN 이 조용히 통과하지 않도록).
    normal = _require_amount(normal_amount, "normal_amount")
    reserved = _require_amount(open_reserved, "open_reserved")
    if budget_available is None:
        budget = max(remaining, 0.0)
    else:
        budget = _require_amount(budget_available, "budget_available")
    reserved_in_total = 0.0 if reserved_already_deducted else reserved

    seed_text = "?" if _finite(seed) is None else f"{_finite(seed):.2f}"
    if remaining <= 0:
        return 0, (
            f"budget: L=0 (no-seed: remaining_seed={remaining:.2f}, "
            f"seed={seed_text})"
        )

    base_level, base_reason = select_dynamic_d(
        r, t1, t2, prev_level=prev_level, delta=delta
    )

    prices_by_level = additional_prices_by_L or {}
    candidates = [
        level
        for level in range(0, int(base_level) + 1)
        if level == 0 or level in prices_by_level
    ]

    prev_e_text = "-" if prev_e is None else str(prev_e)
    chosen = None
    chosen_reason = None
    for level in candidates:
        # compute_budget_exact 가 후보 단계의 가격 목록을 검증합니다
        # (음수/NaN/Infinity 가 조용히 통과하지 않도록).
        additional = (
            0.0
            if level == 0
            else compute_budget_exact(prices_by_level.get(level) or [])
        )
        total = normal + additional + reserved_in_total
        kappa_limit = limit * remaining
        if additional > kappa_limit + _BUDGET_EPS:
            continue
        if total > budget + _BUDGET_EPS:
            continue
        chosen = level
        chosen_reason = (
            f"budget: L={level} "
            f"(kappa_add Σ추가={additional:.2f}<={kappa_limit:.2f}, "
            f"total={total:.2f}<={budget:.2f}, "
            f"reserved_already_deducted={reserved_already_deducted}, "
            f"L_D={base_level}, prev_e={prev_e_text}, d_reason={base_reason})"
        )

    if chosen is not None:
        return chosen, chosen_reason

    # L=0 마저 불가능 = 정상매수(+이미 차감되지 않은 예약금)만으로 B_avail 초과.
    # 정상매수는 건드리지 않고 추가매수만 0단계로 둡니다.
    floor_total = normal + reserved_in_total
    return 0, (
        f"budget: L=0 (infeasible: total={floor_total:.2f}>budget={budget:.2f}, "
        f"kappa_add 한도={limit * remaining:.2f}, "
        f"reserved_already_deducted={reserved_already_deducted}, "
        f"L_D={base_level}, prev_e={prev_e_text}, d_reason={base_reason})"
    )


def decide_levels(policy, **kwargs):
    """
    정책 이름으로 추가매수 단계를 결정합니다.

    지원 정책:
        A  → 3 (고정)
        B  → 5 (고정)
        C  → levels 인자로 지정한 7 또는 8 (고정)
        C3 → 사용하지 않음 (ValueError)
        D  → select_dynamic_d()
        E  → select_dynamic_e()

    Parameters (정책별):
        atr, atr_ratio, close: 지표 (atr_ratio 없으면 atr/close로 계산)
        t1, t2, prev_level, delta: 정책 D/E (prev_level = D 상한 히스테리시스용)
        remaining_seed, seed, normal_amount, additional_prices_by_L,
        open_reserved, kappa_add, budget_available, reserved_already_deducted,
        prev_e: 정책 E
            - kappa_add: 추가매수 전용 예산 비율 (기본 0.02, 후보값·미확정).
              구버전 `kappa` 이름도 받아들이지만(kappa_add 우선) 새 코드는 kappa_add 사용.
            - budget_available: 호출자가 넘기는 실제 가용예산 B_avail (None=remaining_seed).
            - reserved_already_deducted: open_reserved 이중차감 방지 계약 (기본 False).
            - prev_e: 직전 세션 E 최종 선택값 (D prev_level 과 분리).
        levels: 정책 C (7 또는 8)

    Returns:
        dict: {"levels": int, "reason": str, "fallback": bool, "atr": float|None,
               "atr_ratio": float|None}

    Note:
        정책 E 의 예산 축소는 `fallback` 이 아닙니다. 예산 때문에 L 이 내려가면
        `reason` 이 "budget: L=..." 로 시작하고 `fallback` 은 False 입니다.
    """
    name = str(policy or "").strip().upper()
    atr = kwargs.get("atr")
    ratio = kwargs.get("atr_ratio")
    if ratio is None and atr is not None and kwargs.get("close") is not None:
        ratio = atr_ratio(atr, kwargs.get("close"))

    if name == "A":
        levels, reason = 3, "fixed:A(3)"
    elif name == "B":
        levels, reason = 5, "fixed:B(5)"
    elif name == "C3":
        raise ValueError("C3 정책은 사용하지 않습니다 — C(=7) 또는 levels=8 을 사용하세요.")
    elif name == "C":
        requested = kwargs.get("levels", 7)
        levels = select_fixed(requested)
        if levels not in POLICY_C_LEVELS:
            raise ValueError(
                f"정책 C는 {POLICY_C_LEVELS} 중 하나여야 합니다: {requested!r}"
            )
        reason = f"fixed:C({levels})"
    elif name == "D":
        levels, reason = select_dynamic_d(
            ratio,
            kwargs.get("t1"),
            kwargs.get("t2"),
            prev_level=kwargs.get("prev_level"),
            delta=kwargs.get("delta", 0.0),
        )
    elif name == "E":
        kappa_add = kwargs.get("kappa_add")
        if kappa_add is None:
            # 구버전 이름 호환 (kappa_add 가 우선). 새 코드는 kappa_add 를 쓰세요.
            kappa_add = kwargs.get("kappa", DEFAULT_KAPPA_ADD)
        levels, reason = select_dynamic_e(
            ratio,
            kwargs.get("t1"),
            kwargs.get("t2"),
            remaining_seed=kwargs.get("remaining_seed"),
            seed=kwargs.get("seed"),
            normal_amount=kwargs.get("normal_amount"),
            additional_prices_by_L=kwargs.get("additional_prices_by_L"),
            open_reserved=kwargs.get("open_reserved", 0.0),
            kappa_add=kappa_add,
            prev_level=kwargs.get("prev_level"),
            delta=kwargs.get("delta", 0.0),
            budget_available=kwargs.get("budget_available"),
            reserved_already_deducted=bool(
                kwargs.get("reserved_already_deducted", False)
            ),
            prev_e=kwargs.get("prev_e"),
        )
    else:
        raise ValueError(f"알 수 없는 정책입니다: {policy!r}")

    return {
        "levels": int(levels),
        "reason": reason,
        "fallback": str(reason).startswith("fallback"),
        "atr": atr,
        "atr_ratio": ratio,
    }


def build_virtual_additional_orders(unit_amount, base_qty, levels, ceiling, policy=None):
    """
    추가매수 주문을 **가상으로만** 생성합니다 (전송·저장 없음).

    가격은 `strategy.build_additional_loc_prices()` 를 그대로 사용합니다
    (가격 공식 단일 출처). 각 주문은 1주 LOC 매수이며 **ΔT=0** 입니다 (t_target=0.0).

    Parameters:
        unit_amount (float): 이번 회차 1회 매수금.
        base_qty (int): 기준 수량 (정상매수 수량).
        levels (int): 추가매수 단계 수.
        ceiling (float): 추가매수가 넘지 못할 상한 가격 (정상매수 보정가).
        policy (str | None): 근거 기록용 정책 이름.

    Returns:
        list[dict]: {side, quantity, price, order_type, t_target, is_additional,
                     virtual, policy, level} 목록. 주문 전송/저장은 하지 않습니다.

    축소 시 미체결 정책 (설계):
        단계가 줄어도 기존 미체결 자동취소 금지 — 신규 주문만 축소 단계로 생성하고,
        기존 미체결은 `_cancel_open_bot_orders`(취소→확정→재계획) 규칙을 따릅니다.
    """
    # 지연 import: 이 모듈 자체는 브로커/설정을 끌어오지 않고, 가격 공식만 빌려옵니다.
    from strategy import build_additional_loc_prices

    steps = build_additional_loc_prices(unit_amount, base_qty, levels, ceiling)
    orders = []
    for level, price in steps:
        orders.append(
            {
                "side": "BUY",
                "quantity": 1,
                "price": float(price),
                "order_type": "LOC",
                "t_target": 0.0,
                "is_additional": True,
                "virtual": True,
                "policy": policy,
                "level": int(level),
            }
        )
    return orders
