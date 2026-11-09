"""
동적 추가매수 단계 — **DRY 전용 통합 미리보기**(순수 계산).

이 모듈은 "고정 3단계"와 "동적 정책(D/E)"의 주문 구성을 **나란히 비교**해서
DRY 실행에서 로그로 보여주기 위한 계산 계층입니다.

경계 (매우 중요):
    - 금지: state·broker·주문 전송·파일 쓰기·환경변수 접근. 이 모듈은 **값만** 받습니다.
      (T·net_invested·orders_meta 를 바꾸는 코드가 원천적으로 없습니다)
    - 주문 dict 는 `virtual=True` 인 **가상 주문**입니다 (전송 없음).
    - 가격 공식은 `strategy.build_additional_loc_prices()` / `dynamic_levels` 를 그대로
      재사용합니다 (공식 단일 출처 유지).
    - 기본 비활성: 호출자가 명시적으로 부를 때만 동작합니다 (`trading_bot` 후크는
      `DYNAMIC_LOC_PREVIEW=1|true` + DRY 일 때만 호출).

세션 prev 상태 (히스테리시스):
    - 모듈 메모리에만 존재합니다: `_PREV_LEVELS[(symbol, YYYYMMDD)] = {"D": L, "E": L}`.
    - 운영 state 스키마는 건드리지 않습니다 (재시작하면 prev 는 초기화 = 안전한 기본값).
    - prev 는 **이전 세션 날짜의 값만** 사용합니다 → 같은 세션에서 같은 입력으로 몇 번을
      호출해도 출력이 완전히 동일합니다(멱등).

예약금(open_reserved) 계약:
    - `open_reserved=None` 은 "미확정"입니다. 0 으로 간주하지 않고 **동적 정책을 강제
      fallback 3** 으로 내립니다 (reason 에 'reservation-undetermined').
    - `0.0` 은 "측정된 0" 일 때만 넘깁니다.
    - 자세한 계약은 `docs/dynamic-budget-contract.md`.

Fail-closed 원칙:
    데이터/예산/예약금 중 하나라도 확정할 수 없으면 **동적을 켜지 않고 고정 3** 을
    보여줍니다. 미확정 값을 0 이나 추정값으로 채우지 않습니다.
"""

import math

from dynamic_levels import (
    DEFAULT_KAPPA_ADD,
    FIXED_LEVEL_CHOICES,
    atr_ratio,
    build_virtual_additional_orders,
    compute_atr14,
    compute_budget_exact,
    decide_levels,
)

# 비교 기준이 되는 고정 단계
PREVIEW_FIXED_LEVELS = 3

# 버킷 임계값 후보 (ATR 비율 기준) — **후보값·미확정** 입니다.
DEFAULT_T1 = 0.03
DEFAULT_T2 = 0.06
DEFAULT_DELTA = 0.005

# 추가매수 단계 최대 후보 (고정 8단계까지)
MAX_LEVEL_CANDIDATE = max(FIXED_LEVEL_CHOICES)

# fallback 사유 토큰 (테스트·로그에서 부분 문자열로 확인)
REASON_RESERVATION_UNDETERMINED = "reservation-undetermined"
REASON_BUDGET_UNDETERMINED = "budget-undetermined"
REASON_API_ERROR = "api-error"
REASON_OHLC_UNAVAILABLE = "ohlc-unavailable"
REASON_SESSION_UNDETERMINED = "session-date-undetermined"

# DRY 메모리 전용 prev 상태: {(symbol, "YYYYMMDD"): {"D": int|None, "E": int|None}}
_PREV_LEVELS = {}

# 종목별로 보관하는 세션 수 상한 (장기 실행 프로세스에서도 메모리 증가 방지)
_MAX_SESSIONS_PER_SYMBOL = 32

__all__ = [
    "PREVIEW_FIXED_LEVELS",
    "DEFAULT_T1",
    "DEFAULT_T2",
    "DEFAULT_DELTA",
    "MAX_LEVEL_CANDIDATE",
    "REASON_RESERVATION_UNDETERMINED",
    "REASON_BUDGET_UNDETERMINED",
    "REASON_API_ERROR",
    "REASON_OHLC_UNAVAILABLE",
    "REASON_SESSION_UNDETERMINED",
    "compare_dynamic_preview",
    "compute_open_reserved_amount",
    "format_preview_report",
    "get_prev",
    "get_prev_levels",
    "reset_preview_state",
]


# ── 세션 prev 상태 (DRY 메모리 전용) ─────────────────────────────────────


def _normalize_session_date(value):
    """'2026-11-06' / '20261106' / 20261106 → '20261106'. 실패하면 None."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) != 8:
        return None
    return digits


def _resolve_session_date(session_date, bars):
    """
    세션 날짜 확정: 명시값 → 마지막 확정봉 date. **둘 다 없으면 None**(추정 금지).

    None 이면 호출부에서 `session-date-undetermined` fallback 으로 처리합니다
    (로컬 시계·UTC 로 조용히 대체하지 않습니다).
    """
    normalized = _normalize_session_date(session_date)
    if normalized:
        return normalized
    if bars:
        last = bars[-1]
        if isinstance(last, dict):
            normalized = _normalize_session_date(last.get("date"))
            if normalized:
                return normalized
    return None


def _prev_for(symbol, session_date):
    """**이전 세션**의 prev 만 반환합니다 (같은 세션 값은 히스테리시스에 쓰지 않음)."""
    candidates = [
        (date, levels)
        for (stored_symbol, date), levels in _PREV_LEVELS.items()
        if stored_symbol == symbol and date < session_date
    ]
    if not candidates:
        return {"D": None, "E": None}
    _, levels = max(candidates, key=lambda item: item[0])
    return {"D": levels.get("D"), "E": levels.get("E")}


def _store_levels(symbol, session_date, d_level, e_level):
    """이번 세션 결과를 기록하고, 종목별 오래된 세션은 버립니다."""
    _PREV_LEVELS[(symbol, session_date)] = {"D": d_level, "E": e_level}
    dates = sorted(
        date for (stored_symbol, date) in _PREV_LEVELS if stored_symbol == symbol
    )
    for date in dates[:-_MAX_SESSIONS_PER_SYMBOL]:
        _PREV_LEVELS.pop((symbol, date), None)


def get_prev_levels(symbol, session_date=None):
    """
    저장된 prev 단계를 조회합니다 (진단용).

    session_date 를 주면 그 세션의 기록을, 없으면 해당 종목의 최신 기록을 돌려줍니다.
    """
    normalized = _normalize_session_date(session_date)
    if normalized:
        return dict(_PREV_LEVELS.get((symbol, normalized), {"D": None, "E": None}))
    dates = [date for (stored_symbol, date) in _PREV_LEVELS if stored_symbol == symbol]
    if not dates:
        return {"D": None, "E": None}
    return dict(_PREV_LEVELS.get((symbol, max(dates)), {"D": None, "E": None}))


def reset_preview_state(symbol=None):
    """미리보기 메모리 상태를 초기화합니다 (테스트·DRY 재시작용, state 파일과 무관)."""
    if symbol is None:
        _PREV_LEVELS.clear()
        return
    for key in [key for key in _PREV_LEVELS if key[0] == symbol]:
        _PREV_LEVELS.pop(key, None)


# 짧은 별칭 (문서·로그에서 get_prev 로 부를 때)
get_prev = get_prev_levels


# ── 예약금 계산식 ────────────────────────────────────────────────────────


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def compute_open_reserved_amount(open_orders):
    """
    미체결(부분체결 포함) **매수** 주문의 예약금 = Σ 잔량 × 주문가.

    Parameters:
        open_orders (iterable[dict] | None): 살아 있는 주문 행. 각 행은
            - price: 실제 전송한 주문가 (호가 보정 후)
            - quantity: 주문 수량
            - filled_qty: 누적 체결 수량 (없으면 0)
            - side: "BUY"/"SELL" (없으면 BUY 로 봅니다)

    Returns:
        tuple[float | None, dict]: (예약금 또는 None=미확정, 상세)

    None 을 돌려주는 경우 (0 으로 간주하면 예산이 부풀려지므로 fail-closed):
        - 목록 자체가 None (조회 실패/미수행)
        - 주문가·수량이 숫자가 아니거나 음수/비유한값
        - 체결수량이 수량보다 큼 (데이터 불일치)

    매도 주문은 현금 예약이 아니므로 제외합니다 (주식 수량만 예약).
    """
    if open_orders is None:
        return None, {"state": "undetermined", "reason": "no-rows", "count": 0}

    total = 0.0
    buy_count = 0
    sell_count = 0
    for row in open_orders:
        if not isinstance(row, dict):
            return None, {"state": "undetermined", "reason": "invalid-row", "count": 0}
        side = str(row.get("side") or "BUY").strip().upper()
        if side != "BUY":
            sell_count += 1
            continue
        buy_count += 1
        price = _finite(row.get("price"))
        quantity = _finite(row.get("quantity"))
        filled = _finite(row.get("filled_qty", 0.0) or 0.0)
        if price is None or quantity is None or filled is None:
            return None, {"state": "undetermined", "reason": "unparsable", "count": buy_count}
        if price < 0 or quantity < 0 or filled < 0 or filled > quantity:
            return None, {"state": "undetermined", "reason": "inconsistent", "count": buy_count}
        total += price * (quantity - filled)

    return float(total), {
        "state": "measured",
        "reason": "",
        "count": buy_count,
        "sell_count": sell_count,
    }


# ── 비교 계산 ────────────────────────────────────────────────────────────


def _indicator_section(bars, last_close):
    """ATR / ATR 비율 — 계산 불가면 값은 None (추정하지 않음)."""
    count = len(bars) if isinstance(bars, (list, tuple)) else 0
    atr = compute_atr14(bars) if count else None
    close = _finite(last_close)
    if close is None and count:
        last = bars[-1]
        if isinstance(last, dict):
            close = _finite(last.get("c"))
    ratio = atr_ratio(atr, close) if atr is not None and close is not None else None
    return {
        "bars": count,
        "atr": atr,
        "close": close,
        "atr_ratio": ratio,
        "available": ratio is not None,
    }


def _additional_prices(unit_amount, base_qty, levels, ceiling):
    """`strategy.build_additional_loc_prices()` 결과의 가격 목록 (공식 단일 출처)."""
    from strategy import build_additional_loc_prices

    return [float(price) for _level, price in build_additional_loc_prices(
        unit_amount, base_qty, levels, ceiling
    )]


def _build_side(policy, levels, unit_amount, base_qty, ceiling, reason, fallback):
    """가상 주문 목록 + 합계를 만듭니다 (전송·저장 없음)."""
    orders = []
    prices = []
    try:
        orders = build_virtual_additional_orders(
            unit_amount, base_qty, levels, ceiling, policy
        )
        prices = [float(order["price"]) for order in orders]
        additional_total = compute_budget_exact(prices)
        error = ""
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        orders, prices, additional_total = [], [], 0.0
        error = f"order-build-failed:{type(exc).__name__}"
    return {
        "policy": policy,
        "levels": int(levels),
        "orders": orders,
        "prices": prices,
        "order_count": len(orders),
        "additional_total": additional_total,
        "reason": reason if not error else f"{reason}|{error}",
        "fallback": bool(fallback),
    }


def compare_dynamic_preview(
    symbol,
    *,
    bars=None,
    last_close=None,
    unit_amount=None,
    base_qty=None,
    ceiling=None,
    remaining_seed=None,
    budget_available=None,
    open_reserved=None,
    reserved_already_deducted=False,
    reservation_note="",
    policy="E",
    t1=None,
    t2=None,
    delta=None,
    kappa_add=DEFAULT_KAPPA_ADD,
    normal_amount=None,
    session_date=None,
    prev_levels=None,
    data_errors=None,
):
    """
    고정 3단계와 동적 정책(D/E) 결과를 나란히 계산합니다 (DRY 전용, 주문 없음).

    Parameters:
        symbol (str): 종목 코드.
        bars (list[dict] | None): 확정봉 OHLC, **오래된 순**. ATR 계산에만 사용.
        last_close (float | None): 직전 종가 (ATR 비율 분모). 없으면 마지막 봉 종가.
        unit_amount (float): 이번 회차 1회 매수금.
        base_qty (int): 기준 수량 (추가매수 계단의 분모).
        ceiling (float | None): 추가매수가 넘지 못하는 상한 가격 (정상매수 보정가).
        remaining_seed (float | None): R = seed − net_invested (호출자가 확정).
        budget_available (float | None): B_avail (호출자가 확정한 가용예산).
        open_reserved (float | None): 기존 미체결 **매수** 예약금.
            None = 미확정 → 동적 fallback 3 (0 으로 간주하지 않음).
            0.0 = 측정된 0.
        reserved_already_deducted (bool): B_avail 이 이미 예약금을 차감한 값이면 True
            (이중차감 방지). 기본 False.
        reservation_note (str): 미확정 사유 표기 (예: "open-partial-fill").
        policy (str): 동적 정책 ("D"/"E" 우선, A/B/C도 비교용으로 허용).
        t1, t2, delta (float | None): ATR 비율 버킷 임계값·히스테리시스 폭
            (None 이면 DEFAULT_T1/DEFAULT_T2/DEFAULT_DELTA 후보값).
        kappa_add (float): 추가매수 전용 예산 비율 (정책 E, 후보값).
        normal_amount (float | None): 정상매수 금액 (None 이면 unit_amount).
        session_date (str | None): 세션 날짜 (None 이면 마지막 봉 date).
            **추정하지 않습니다** — 봉 date 도 없으면 `session-date-undetermined`
            fallback 으로 처리합니다 (UTC/로컬 날짜 대체 금지).
        prev_levels (dict | None): prev 상태 override {"D":..,"E":..}. 없으면 메모리 기록.
        data_errors (list[str] | None): 호출자가 실패한 조회를 표시
            (예: ["ohlc-query-failed:BrokerError"], ["ohlc-unavailable:daily_candles"])
            → 동적 fallback 3.

    Returns:
        dict: 고정/동적 결과, 지표, 예산, 비교, 사유. 항상 `virtual=True`.
        같은 입력·같은 세션이면 완전히 같은 dict 를 돌려줍니다(prev 는 이전 세션만 참조).

    Raises:
        이 함수는 계산 실패를 예외로 던지지 않습니다 (DRY 미리보기가 봇을 멈추면 안 됨).
        실패는 `dynamic.reason` / `reasons` 에 기록하고 고정 3 으로 fallback 합니다.
    """
    symbol_text = str(symbol)
    errors = [str(item) for item in (data_errors or []) if str(item).strip()]
    session = _resolve_session_date(session_date, bars)

    # ── 입력 정규화 (실패는 None 으로 두고 fallback 사유로 남깁니다) ──
    unit = _finite(unit_amount)
    qty = _finite(base_qty)
    base_qty_int = int(qty) if qty is not None and qty >= 0 else 0
    ceiling_value = _finite(ceiling)
    if ceiling_value is not None and ceiling_value <= 0:
        ceiling_value = None
    normal = _finite(normal_amount)
    if normal is None:
        normal = unit if unit is not None else 0.0

    remaining = _finite(remaining_seed)
    budget = _finite(budget_available)
    reserved_measured = _finite(open_reserved)
    reserved_undetermined = open_reserved is None or (
        open_reserved is not None and reserved_measured is None
    )

    # ── 고정 3 결과 ──
    fixed = _build_side(
        "A",
        PREVIEW_FIXED_LEVELS,
        unit,
        base_qty_int,
        ceiling_value,
        "fixed:A(3)",
        False,
    )

    reasons = []
    prev = {"D": None, "E": None}
    if isinstance(prev_levels, dict):
        prev = {
            "D": prev_levels.get("D"),
            "E": prev_levels.get("E"),
        }
    elif session is not None:
        # 세션 날짜를 확정할 수 없으면 prev(히스테리시스)는 쓰지 않습니다.
        prev = _prev_for(symbol_text, session)

    policy_name = str(policy or "E").strip().upper()
    dynamic = None
    decision = None

    # ── fallback 판정 (API/OHLC 오류 → 세션일 → 예약금 → 예산) ──
    fallback_reason = ""
    if errors:
        tokens = []
        for error in errors:
            token = (
                REASON_OHLC_UNAVAILABLE
                if error.startswith(REASON_OHLC_UNAVAILABLE)
                else REASON_API_ERROR
            )
            if token not in tokens:
                tokens.append(token)
        fallback_reason = f"fallback:{tokens[0]}({','.join(errors)})"
        reasons.extend(tokens + errors)
    elif session is None:
        # 조용한 날짜 대체 금지 — 세션이 확정되지 않으면 동적을 켜지 않습니다.
        fallback_reason = f"fallback:{REASON_SESSION_UNDETERMINED}"
        reasons.append(REASON_SESSION_UNDETERMINED)
    elif reserved_undetermined:
        note = f"({reservation_note})" if str(reservation_note).strip() else ""
        # E 정책만 예산 미확정으로 fallback. D는 ATR 기반 이론적 단계는 별도로 계산.
        e_budget_fallback_reason = f"fallback:{REASON_RESERVATION_UNDETERMINED}{note}"
        fallback_reason = e_budget_fallback_reason
        reasons.append(REASON_RESERVATION_UNDETERMINED)
    elif remaining is None or remaining < 0:
        fallback_reason = f"fallback:{REASON_BUDGET_UNDETERMINED}(remaining_seed)"
        reasons.append(REASON_BUDGET_UNDETERMINED)
    elif budget is None:
        fallback_reason = f"fallback:{REASON_BUDGET_UNDETERMINED}(budget_available)"
        reasons.append(REASON_BUDGET_UNDETERMINED)
    elif unit is None or unit <= 0 or base_qty_int <= 0 or ceiling_value is None:
        fallback_reason = "fallback:no-additional-capacity"
        reasons.append("no-additional-capacity")

    indicator = _indicator_section(bars, last_close)

    # D 정책: ATR 기반 이론적 단계 (예약금과 무관). reserved_undetermined でも 계산.
    d_decision = None
    d_threshold1 = DEFAULT_T1 if t1 is None else t1
    d_threshold2 = DEFAULT_T2 if t2 is None else t2
    d_hysteresis = DEFAULT_DELTA if delta is None else delta
    try:
        d_prices_by_level = {
            level: _additional_prices(
                unit, base_qty_int, level, ceiling_value
            )
            for level in range(0, MAX_LEVEL_CANDIDATE + 1)
        }
        d_decision = decide_levels(
            "D",
            atr_ratio=indicator["atr_ratio"],
            t1=d_threshold1,
            t2=d_threshold2,
            prev_level=prev.get("D"),
            delta=d_hysteresis,
        )
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        d_decision = None
        reasons.append(f"d-invalid-input({type(exc).__name__})")

    # E 정책: fallback_reason이 reserved_undetermined 전용이면 3단계 fallback,
    # 다른 사유(오류·세션 미확정 등)로 전체 동적 비활성.
    e_only_fallback = bool(
        fallback_reason
        and fallback_reason.startswith(f"fallback:{REASON_RESERVATION_UNDETERMINED}")
    )

    if fallback_reason and not e_only_fallback:
        # ATR·세션·예산(remaining/budget)·가격 능력 부족 등: 동적 전체 비활성
        dynamic = _build_side(
            "A",
            PREVIEW_FIXED_LEVELS,
            unit,
            base_qty_int,
            ceiling_value,
            fallback_reason,
            True,
        )
        decision = None
    elif e_only_fallback:
        # 예약금 미확정: E만 3단계 fallback, D는 ATR 기반 단계 유지
        dynamic = _build_side(
            "A",
            PREVIEW_FIXED_LEVELS,
            unit,
            base_qty_int,
            ceiling_value,
            e_budget_fallback_reason,
            True,
        )
        decision = None
    else:
        # 정상 계산: D와 E 모두 계산
        extra_kappa = DEFAULT_KAPPA_ADD if kappa_add is None else kappa_add
        try:
            prices_by_level = {
                level: _additional_prices(
                    unit, base_qty_int, level, ceiling_value
                )
                for level in range(0, MAX_LEVEL_CANDIDATE + 1)
            }
            decision = decide_levels(
                policy_name if policy_name in ("A", "B", "C", "D", "E") else "E",
                atr_ratio=indicator["atr_ratio"],
                t1=d_threshold1,
                t2=d_threshold2,
                prev_level=prev.get("D"),
                prev_e=prev.get("E"),
                delta=d_hysteresis,
                remaining_seed=remaining,
                seed=remaining,
                normal_amount=normal,
                additional_prices_by_L=prices_by_level,
                open_reserved=0.0 if reserved_already_deducted else reserved_measured,
                kappa_add=extra_kappa,
                budget_available=budget,
                reserved_already_deducted=reserved_already_deducted,
                levels=7,
            )
        except (ValueError, TypeError, ZeroDivisionError) as exc:
            decision = None
            fallback_reason = f"fallback:invalid-input({type(exc).__name__})"
            reasons.append("invalid-input")

        if decision is None:
            dynamic = _build_side(
                "A",
                PREVIEW_FIXED_LEVELS,
                unit,
                base_qty_int,
                ceiling_value,
                fallback_reason,
                True,
            )
        elif d_decision and d_decision.get("fallback") and policy_name in ("D", "E"):
            # 지표(ATR)가 없으면 동적 정책을 켜지 않습니다 (fail-closed → 고정 3).
            # 정책 E 는 D 단계를 상한으로 쓰므로 D fallback 도 동적 비활성 사유입니다.
            reasons.append("atr-insufficient")
            dynamic = _build_side(
                "A",
                PREVIEW_FIXED_LEVELS,
                unit,
                base_qty_int,
                ceiling_value,
                f"fallback:no-indicator ({decision['reason'] if decision else 'unknown'})",
                True,
            )
        else:
            if decision and decision["fallback"]:
                reasons.append("dynamic-fallback")
            dynamic = _build_side(
                policy_name,
                decision["levels"],
                unit,
                base_qty_int,
                ceiling_value,
                decision["reason"],
                decision["fallback"],
            )
            # fallback 이 아닌 실제 결정만 세션 prev 로 기록합니다
            # (데이터 결손으로 내려간 값을 다음 세션 히스테리시스에 물려주지 않음).
            if decision and not decision["fallback"]:
                _store_levels(
                    symbol_text,
                    session,
                    d_decision["levels"] if d_decision else None,
                    dynamic["levels"],
                )

    # ── 예약금 반영 (이중차감 방지 계약) ──
    if reserved_undetermined:
        reserved_applied = None
    elif reserved_already_deducted:
        reserved_applied = 0.0
    else:
        reserved_applied = reserved_measured

    def _total(side):
        if reserved_applied is None:
            return None
        return float(normal + side["additional_total"] + reserved_applied)

    fixed["normal_amount"] = normal
    fixed["total"] = _total(fixed)
    dynamic["normal_amount"] = normal
    dynamic["total"] = _total(dynamic)

    shared = (
        min(fixed["order_count"], dynamic["order_count"]) > 0
        and dynamic["prices"][: fixed["order_count"]] == fixed["prices"]
    )

    return {
        "symbol": symbol_text,
        "session_date": session,
        "virtual": True,
        "policy": policy_name,
        "indicator": indicator,
        "budget": {
            "remaining_seed": remaining,
            "budget_available": budget,
            "normal_amount": normal,
            "open_reserved": reserved_measured,
            "open_reserved_state": "undetermined" if reserved_undetermined else "measured",
            "reserved_already_deducted": bool(reserved_already_deducted),
            "reserved_applied": reserved_applied,
            "reservation_note": str(reservation_note),
        },
        "fixed": fixed,
        "dynamic": dynamic,
        "comparison": {
            "fixed_levels": fixed["levels"],
            "dynamic_levels": dynamic["levels"],
            "level_delta": dynamic["levels"] - fixed["levels"],
            "fixed_order_count": fixed["order_count"],
            "dynamic_order_count": dynamic["order_count"],
            "fixed_additional_total": fixed["additional_total"],
            "dynamic_additional_total": dynamic["additional_total"],
            "additional_total_delta": dynamic["additional_total"]
            - fixed["additional_total"],
            "fixed_prices": list(fixed["prices"]),
            "dynamic_prices": list(dynamic["prices"]),
            "shared_prefix_match": shared,
            "budget_delta": (
                None
                if fixed["total"] is None or dynamic["total"] is None
                else dynamic["total"] - fixed["total"]
            ),
            # D 정책: ATR 기반 이론적 단계 (예약금 미확정에도 계산 가능)
            "d_levels": d_decision["levels"] if d_decision else None,
            "d_reason": d_decision["reason"] if d_decision else "atr-insufficient",
            "d_fallback": d_decision["fallback"] if d_decision else True,
        },
        "reasons": reasons,
        "prev": dict(prev),
    }


def format_preview_report(preview):
    """
    미리보기 dict → 로그용 문자열 (여러 줄).

    주문번호·계좌 등 민감 정보는 포함하지 않습니다 (가격·수량만).
    """
    if not isinstance(preview, dict):
        return "[동적 LOC 미리보기] 비교 결과가 없습니다."

    indicator = preview.get("indicator") or {}
    fixed = preview.get("fixed") or {}
    dynamic = preview.get("dynamic") or {}
    budget = preview.get("budget") or {}

    lines = []
    session_text = preview.get("session_date") or "미확정"
    lines.append(
        f"[동적 LOC 미리보기] {preview.get('symbol')} 세션 {session_text}"
        " (DRY 전용 · 주문 전송 없음)"
    )
    if indicator.get("available"):
        lines.append(
            f"  지표: ATR={indicator.get('atr'):.4f}, r={indicator.get('atr_ratio'):.5f} "
            f"(확정봉 {indicator.get('bars')}개)"
        )
    else:
        lines.append(
            f"  지표: ATR 계산 불가 (확정봉 {indicator.get('bars')}개) → 고정 3 유지"
        )

    def _side_text(label, side):
        prices = ", ".join(f"${price:.2f}" for price in (side.get("prices") or []))
        total = side.get("total")
        total_text = "-" if total is None else f"${total:.2f}"
        return (
            f"  {label}: 단계 {side.get('levels')} · 주문 {side.get('order_count')}건 "
            f"{prices or '(없음)'} · 추가합계 ${side.get('additional_total', 0.0):.2f} "
            f"· 최대자본 {total_text}"
        )

    lines.append(_side_text("고정 3", fixed))
    lines.append(_side_text(f"동적 {dynamic.get('policy')}", dynamic))
    lines.append(f"  근거: {dynamic.get('reason')}")

    # D 정책: ATR 기반 이론적 단계 (예약금 미확정에도 계산 가능)
    comparison = preview.get("comparison") or {}
    d_levels = comparison.get("d_levels")
    d_reason = comparison.get("d_reason")
    if d_levels is not None:
        lines.append(f"  D(ATR 이론): 단계 {d_levels} · {d_reason}")
    else:
        lines.append("  D(ATR 이론): 계산 불가 (ATR 부족)")

    reserved_state = budget.get("open_reserved_state")
    if reserved_state == "undetermined":
        note = budget.get("reservation_note") or ""
        suffix = f" ({note})" if note else ""
        lines.append(
            f"  예약금: 미확정{suffix} → E는 예산 미확정(고정 3), D는 ATR 이론 단계 표시"
        )
    else:
        lines.append(f"  예약금: ${(budget.get('open_reserved') or 0.0):.2f} (측정값)")

    if preview.get("reasons"):
        lines.append(f"  사유: {', '.join(preview['reasons'])}")
    return "\n".join(lines)
