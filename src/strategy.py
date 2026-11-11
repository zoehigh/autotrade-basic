# 매수/매도 여부를 판단하는 전략 로직 — 무한매수법 V4.0
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from config import TRADE_MODE, LS_DEMO_BYPASS_BUGS
from broker.base import Broker
from broker.market_utils import normalize_order_price, us_session_last_closed_date
from market_data import get_finnhub_ma5

# BUY LOC 가격 상한 — 브로커가 현재가 대비 +20% 초과 주문을 거부하므로 미리 낮춥니다.
BUY_LOC_PRICE_CAP_RATIO = 1.19

# 추가매수 주문 식별용 (정상매수와 구분)
_ADDITIONAL_MARK = "[추가매수]"


def cap_buy_loc_price(price, last_price):
    """
    BUY LOC 가격을 현재가 ×1.19 상한으로 보정합니다.

    Returns:
        tuple: (보정 후 가격, 보정 여부)
    """
    price = float(price or 0)
    if price <= 0 or not last_price or last_price <= 0:
        return price, False
    max_allowed = adjust_price_to_tick(last_price * BUY_LOC_PRICE_CAP_RATIO)
    if price > max_allowed:
        return max_allowed, True
    return price, False


def build_additional_loc_prices(unit_amount, base_qty, levels, ceiling):
    """
    추가매수 LOC 가격을 계단식으로 만듭니다.

    - 후보 가격: unit_amount / (base_qty + i)
    - ceiling(정상매수 보정가) 이상인 후보는 건너뜁니다
      (1.19 상한에 걸리면 여러 후보가 같은 가격으로 뭉칠 수 있기 때문)
    - 앞 단계보다 엄격히 낮은 가격만 유지합니다

    Parameters:
        unit_amount (float): 이번 회차 1회 매수금
        base_qty (int): 기준 수량 (정상매수 수량)
        levels (int): 필요한 추가매수 단계 수
        ceiling (float): 추가매수가 넘지 못할 상한 가격 (정상매수 보정가)

    Returns:
        list: [(단계 번호, 가격), ...]
    """
    result: list[tuple[int, float]] = []
    previous = float(ceiling)
    index = 1
    max_iterations = max(levels, 0) * 3 + 5
    while len(result) < levels and index <= max_iterations:
        price = adjust_price_to_tick(unit_amount / (base_qty + index))
        if price <= 0:
            break
        if price < previous:
            result.append((len(result) + 1, price))
            previous = price
        index += 1
    return result

def adjust_price_to_tick(price):
    """
    미국 주식 거래소의 호가 단위 규칙에 맞춰 가격을 조정합니다.
    
    호가 단위 규칙:
    - 가격이 $1.00 미만: 소수점 4자리까지 ($0.0001 단위)
    - 가격이 $1.00 이상: 소수점 2자리까지 ($0.01 단위)
    
    모든 가격은 버림(floor) 처리합니다.
    
    Examples:
        >>> adjust_price_to_tick(56.378)
        56.37
        >>> adjust_price_to_tick(0.98769)
        0.9876
    """
    return normalize_order_price(price)


def calculate_star_point(avg_price, T, splits, symbol_type):
    """
    별지점(목표 매도가이자 매수 기준 가격)을 계산합니다.

    별지점은 "여기서 팔면 이익, 여기서 사면 평단이 낮아지는" 가격입니다.
    T가 높을수록 별지점은 평단에 가까워집니다.

    공식:
        TQQQ 20분할: 별% = (15 - 1.5 × T)%
        TQQQ 40분할: 별% = (15 - 0.75 × T)%
        SOXL 20분할: 별% = (20 - 2 × T)%
        SOXL 40분할: 별% = (20 - T)%
        별지점 = 평단 × (1 + 별%)

    Parameters:
        avg_price (float): 평균 매수가 (평단)
        T (float): 현재 누적 매수 횟수
        splits (int): 분할 수 (20 또는 40)
        symbol_type (str): 종목 타입 ("TQQQ" 또는 "SOXL")

    Returns:
        float: 별지점 가격

    Examples:
        >>> calculate_star_point(38.30, 8.6, 20, "SOXL")
        39.37...  # (20 - 2*8.6)/100 = 2.8% 위
    """
    if symbol_type == "TQQQ":
        if splits == 20:
            star_pct = (15 - 1.5 * T) / 100
        else:
            star_pct = (15 - 0.75 * T) / 100
    else:
        # SOXL 및 기타 종목
        if splits == 20:
            star_pct = (20 - 2 * T) / 100
        else:
            star_pct = (20 - T) / 100

    return avg_price * (1 + star_pct)


def calculate_unit_amount(remaining_cash, T, splits):
    """
    이번 회차에 투입할 1회 매수금을 계산합니다.

    남은 슬롯(splits - T)으로 현재 가용 자금을 균등 분할합니다.

    공식:
        1회 매수금 = 남은 자금 / (splits - T)

    Parameters:
        remaining_cash (float): 현재 주문 가능 자금 (달러)
        T (float): 현재 누적 매수 횟수
        splits (int): 분할 수

    Returns:
        float: 이번에 투입할 금액 (달러). 슬롯이 없으면 0.0 반환

    Examples:
        >>> calculate_unit_amount(19522, 1, 40)
        500.56...  # 19522 / 39
    """
    remaining_slots = splits - T
    if remaining_slots <= 0:
        return 0.0
    return remaining_cash / remaining_slots


REVERSE_EXIT_THRESHOLD = {
    "TQQQ": 0.85,
    "SOXL": 0.80,
}

_REVERSE_DIVISOR = {
    20: 10,
    40: 20,
}

_REVERSE_T_SELL_FACTOR = {
    20: 0.9,
    40: 0.95,
}


def _get_reverse_star_point(symbol, last_price, close_prices):
    ma5 = get_finnhub_ma5(symbol)
    if ma5 and ma5 > 0:
        return ma5
    if close_prices:
        valid = [p for p in close_prices if isinstance(p, (int, float)) and p > 0]
        if valid:
            return sum(valid[-5:]) / len(valid[-5:])
    print(
        f"[경고] {symbol} 별지점(직전 5거래일 종가 평균) 계산 불가 — "
        f"현재가 ${last_price:.2f}로 대체합니다 (Finnhub 키/close_prices 없음)"
    )
    return last_price


def _has_unresolved_reverse_orders(state):
    """현재 리버스 cycle 에 아직 terminal 확정되지 않은 주문이 있는지 (trading_bot 의 같은 이름 함수와 동일 규칙)."""
    cycle_id = state.get("reverse_mode", {}).get("cycle_id", "")
    return any(
        meta.get("reverse_action")
        and meta.get("cycle_id") == cycle_id
        and not meta.get("terminal")
        for meta in state.get("orders_meta", {}).values()
    )


def _confirmed_prev_close(broker, symbol, exchange_code, now_utc=None):
    """확정된 직전 정규장 종가를 (종가, 거래일 YYYY-MM-DD) 로 반환합니다. 확보하지 못하면 None.

    리버스 종료 판정은 '종가' 기준이므로 프리마켓·실시간 시세(last_price)를 쓰지 않습니다.
    확정 종가의 조건: XNYS 캘린더상 이미 마감된 가장 최근 세션(조기마감·휴장 반영)의 일봉이
    브로커 일봉에 같은 날짜로 존재하고 종가가 양수일 것. 브로커가 날짜를 주지 않거나(date=None)
    최신 일봉이 그 세션보다 오래됐거나 조회가 실패하면 '미확보'입니다.
    """
    if broker is None or not hasattr(broker, "get_daily_candles"):
        return None
    if now_utc is None:
        now_utc = datetime.now(ZoneInfo("UTC"))   # 전략의 시계를 그대로 사용 (섀도 원장 등 세션 시뮬레이션과 일치)
    expected = us_session_last_closed_date(now_utc)
    if not expected:
        return None
    try:
        candles = broker.get_daily_candles(symbol, exchange_code, days=5)
    except Exception as error:  # 조회 실패는 '미확보'로 처리 (종료 판정을 하지 않음)
        print(f"[경고] {symbol} 일봉 조회 실패 — 확정 종가 미확보: {error}")
        return None
    if not isinstance(candles, list):
        return None
    for candle in reversed(candles):
        if not isinstance(candle, dict) or candle.get("date") != expected:
            continue
        close = candle.get("close")
        if isinstance(close, (int, float)) and not isinstance(close, bool) and close > 0:
            return float(close), expected
    return None


class _SnapshotBroker:
    """종료일 일반모드 복귀 시 재귀 호출이 시세·잔고를 다시 조회하지 않도록 첫 조회 결과를 고정해 넘기는 래퍼.

    첫 판정(종료 판단)과 복귀 주문 계산이 서로 다른 시점의 시세·잔고·주문가능금액을 쓰면 판단과 주문이 어긋날 수 있습니다.
    """

    def __init__(self, broker, quotation, price_detail, balance, purchase_amount):
        self._broker = broker
        self._snap = {
            "get_stock_quotation": quotation,
            "get_stock_price": price_detail,
            "get_balance": balance,
            "get_purchase_amount": purchase_amount,
        }

    def __getattr__(self, name):
        if name in self._snap:
            value = self._snap[name]
            return lambda *args, **kwargs: value
        return getattr(self._broker, name)


def execute_reverse_mode(broker, symbol, exchange_code, splits, symbol_type,
                         position_qty, avg_price, orderable_cash, last_price,
                         state):
    """리버스모드 1회 실행의 주문을 계산합니다 (상태는 읽기만 합니다 — T·day_count 는 체결 반영으로만 바뀝니다).

    [원문(라오어 V4 두 문서)에서 확인된 규칙]
    - 진입: 20분할 T>19 / 40분할 T>39. 첫날은 매수 없이 MOC 매도만(보유량 1/10, 40분할 1/20, 내림).
    - 둘째 날부터: 직전 보유량을 같은 비율로 나눠 별지점(직전 5거래일 종가 평균)에 LOC 매도 + 총 잔금 1/4 로 별지점 아래 쿼터매수.
    - 리버스 중 소진 판단은 T 가 아니라 '잔금 1/4 로 1주 매수 가능 여부'(불가하면 매수 없이 MOC 매도만).
    - T: 20분할 매도 ×0.9 / 쿼터매수 T+(20−T)×0.25, 40분할 ×0.95 / T+(40−T)×0.25. 일반모드 복귀 시 그대로 승계.
    - 종료: 종가가 평단 대비 TQQQ −15%, SOXL −20% 위로 회복했음을 확인하면 일반모드 복귀(하루 만에 종료될 수도 있음).
      복귀 후 매수금은 잔금÷(분할수−T), T 가 소진이면 다시 리버스.

    [원문에 없어 구현이 정한 정책 — 변경 시 별도 검토]
    - 종료 판정 시작 시점: 진입 첫날은 종료 판정을 하지 않고 2일차부터(보수적 해석).
    - 종료 판정 가격: 확정된 직전 정규장 종가(+ XNYS 직전 마감 세션 날짜 대조). 현재가(프리마켓)로는 종료하지 않고,
      확정 종가를 얻지 못하면 종료도 계속도 판단하지 않고 그 실행은 주문 없이 보류.
    - 미체결·부분체결: terminal 확정 전에는 신규 주문·종료 판정을 모두 보류(중복 매도 방지).
      체결>0 + 종결(취소·만료, 거부 제외)이면 그날을 리버스 1일차 완료로 보는 것은 이 PR 이전부터의 동작(state.py).
    - 종료일 같은 날 일반모드 복귀(원문은 '확인한 다음 날부터'로 읽힐 수 있음 — 이 구현은 종료 판정을 한 그 실행에서 이어서 주문).
    - 쿼터매수 잔금은 broker 주문가능금액만 사용(원문은 매도 회수금 포함 — 의도적 보수 설계, AGENTS.md 참고).
    """
    rev = state.get("reverse_mode", {})
    if rev.get("reconciliation_error") or rev.get("reconciliation_only"):
        print(f"[리버스모드] {symbol} 상태 reconciliation 확인 필요 → 신규 주문을 생성하지 않습니다.")
        return {"orders": [], "new_T": float(state.get("T", 0.0)), "exit": False}
    # 아직 terminal 확정되지 않은 리버스 주문(미체결·부분체결·이력 미확인)이 있으면 새 리버스 주문을 만들지 않습니다.
    # day_count 는 체결이 반영돼야 증가하므로, 가드가 없으면 전일 1일차 MOC 가 미종결인 상태에서 1일차 MOC 를 또 내 중복 매도합니다.
    if _has_unresolved_reverse_orders(state):
        print(f"[리버스모드] {symbol} 미종결 리버스 주문이 있어 신규 주문을 생성하지 않습니다 (체결 반영·이력 확인 후 재개).")
        return {"orders": [], "new_T": float(state.get("T", 0.0)), "exit": False}
    session_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if rev.get("last_planned_session") == session_date:
        print(f"[리버스모드] {symbol} 이미 {session_date} 실행됨 → 중복 주문을 생성하지 않습니다.")
        return {"orders": [], "new_T": float(state.get("T", 0.0)), "exit": False}
    day_count = rev.get("day_count", 0) + 1
    close_prices = state.get("close_prices", [])
    T = float(state.get("T", 0.0))

    divisor = _REVERSE_DIVISOR.get(splits, 20)
    sell_factor = _REVERSE_T_SELL_FACTOR.get(splits, 0.95)

    # 진입 첫날은 종료 판정을 하지 않고 무조건 MOC 매도를 먼저 냅니다 ("첫날 — 무조건 매도").
    # 종료 검사를 먼저 하면 T>분할수-1 인데 종가가 이미 종료선 위인 종목은 진입 즉시 종료되어 매도 없이
    # T 가 그대로 남고, 다음 날도 같은 판정이 반복되어 영구 정지합니다 (2026-11-11 TQQQ 실측).
    # 종료 판정은 2일차부터 합니다.
    #
    # 종료 판정 가격은 '확정된 직전 정규장 종가'입니다 (프리마켓·실시간 last_price 로 종료하지 않음).
    # 확정 종가를 확보하지 못하면(일봉 조회 실패·날짜 불일치·날짜 미제공) 종료도 계속도 판단하지 않고
    # 이번 실행은 신규 주문 없이 보류합니다 (오종료로 일반 매수가 나가거나 리버스 매도가 어긋나는 것보다 하루 보류가 안전).
    is_first_day = (day_count == 1)
    threshold = REVERSE_EXIT_THRESHOLD.get(symbol_type, 0.85)
    exit_price = avg_price * threshold
    if not is_first_day:
        confirmed = _confirmed_prev_close(broker, symbol, exchange_code)
        if confirmed is None:
            print(
                f"[리버스모드] {symbol} 확정된 직전 정규장 종가를 확인하지 못해 종료 판정을 보류합니다 "
                f"→ 이번 실행은 신규 주문을 생성하지 않습니다 (현재가 ${last_price:.2f}는 판정에 쓰지 않음)."
            )
            return {"orders": [], "new_T": float(state.get("T", 0.0)), "exit": False, "exit_unconfirmed": True}
        close_price, close_date = confirmed
        if close_price > exit_price:
            print(
                f"[리버스모드] {symbol} 종료 조건 충족 (확정 종가 ${close_price:.2f}[{close_date}] > ${exit_price:.2f}; "
                f"현재가 ${last_price:.2f}는 판정에 쓰지 않음)"
            )
            return {"orders": [], "new_T": T, "exit": True, "reverse_day": day_count}
        if last_price > exit_price:
            print(
                f"[리버스모드] {symbol} 현재가 ${last_price:.2f}는 종료선 ${exit_price:.2f} 위지만 확정 종가 "
                f"${close_price:.2f}[{close_date}]는 이하 → 종료하지 않고 리버스를 유지합니다 (프리마켓 가격 단독 종료 방지)."
            )

    print(f"[리버스모드] {symbol} 진입 {day_count}일차 (T={T})")
    orders = []

    if is_first_day:
        sell_qty = max(1, position_qty // divisor)
        if sell_qty > 0 and position_qty > 0:
            orders.append({
                "side": "SELL",
                "quantity": sell_qty,
                "price": adjust_price_to_tick(last_price),
                "order_type": "MOC",
                "comment": f"리버스모드 {day_count}일차 MOC 매도 ({splits}분할 1/{divisor})",
                "t_target": 0.0,
                "reverse_action": "sell",
                "reverse_day": day_count,
                "reverse_base_t": T,
                "reverse_t_factor": sell_factor,
            })
        new_T = round(T * sell_factor, 4)
        print(f"  → MOC 매도 {sell_qty}주, T: {T} → {new_T}")
    else:
        star = _get_reverse_star_point(symbol, last_price, close_prices)
        adjusted_star = adjust_price_to_tick(star)

        # 쿼터매수 가능 여부 확인 (잔금의 1/4로 1개 매수가 가능한지)
        # 소진 기준: 쿼터매수로 1개 매수가 불가능해지는 시점
        total_cash = max(orderable_cash, 0)
        buy_amount = total_cash / 4
        buy_qty = math.floor(buy_amount / adjusted_star) if adjusted_star > 0 else 0

        sell_qty = max(1, position_qty // divisor)

        if buy_qty > 0:
            # 별지점 아래에서 매수 지속: LOC 매도 + LOC 매수
            if sell_qty > 0 and position_qty > 0:
                orders.append({
                    "side": "SELL",
                    "quantity": sell_qty,
                    "price": adjusted_star,
                    "order_type": "LOC",
                    "comment": f"리버스모드 {day_count}일차 LOC 매도 @별지점",
                    "t_target": 0.0,
                    "reverse_action": "sell",
                    "reverse_day": day_count,
                    "reverse_base_t": T,
                    "reverse_t_factor": sell_factor,
                })
            new_T_sell = round(T * sell_factor, 4)

            # 리버스 쿼터매수도 현재가 ×1.19 상한을 적용합니다.
            # (브로커가 +20% 초과 주문을 거부합니다 — 일반매수와 동일 규칙)
            original_buy_price = adjust_price_to_tick(adjusted_star - 0.01)
            buy_price, buy_capped = cap_buy_loc_price(original_buy_price, last_price)
            if buy_capped:
                print(
                    f"  ⚠️ LOC 가격 보정: ${original_buy_price:.2f} → ${buy_price:.2f} "
                    f"(현재가 ${last_price:.2f} × {BUY_LOC_PRICE_CAP_RATIO} 기준)"
                )
            orders.append({
                "side": "BUY",
                "quantity": buy_qty,
                "price": buy_price,
                "order_type": "LOC",
                "comment": f"리버스모드 {day_count}일차 쿼터매수 (잔금/4)",
                "t_target": 0.0,
                "reverse_action": "buy",
                "reverse_day": day_count,
                "reverse_base_t": T,
                "reverse_t_target": round((splits - new_T_sell) * 0.25, 4),
            })
            new_T_buy = round(new_T_sell + (splits - new_T_sell) * 0.25, 4)
            new_T = new_T_buy
            print(f"  → LOC 매도 {sell_qty}주 @${adjusted_star}, LOC 매수 {buy_qty}주 @${buy_price}")
            print(f"  → T: {T} → (매도{new_T_sell}) → (매수{new_T_buy}) → T={new_T}")
        else:
            # 소진: 쿼터매수로 1개 매수가 불가능 → 매수 시도 없이 MOC 매도만
            if sell_qty > 0 and position_qty > 0:
                orders.append({
                    "side": "SELL",
                    "quantity": sell_qty,
                    "price": adjust_price_to_tick(last_price),
                    "order_type": "MOC",
                    "comment": f"리버스모드 {day_count}일차 소진 MOC 매도 (쿼터매수 불가)",
                    "t_target": 0.0,
                    "reverse_action": "sell",
                    "reverse_day": day_count,
                    "reverse_base_t": T,
                    "reverse_t_factor": sell_factor,
                })
            new_T = round(T * sell_factor, 4)
            print(f"  → 소진 MOC 매도 {sell_qty}주 (쿼터매수 불가, 잔금 ${total_cash:.2f})")
            print(f"  → T: {T} → {new_T}")

        if day_count > 5 and position_qty <= 0:
            print(f"[리버스모드] {symbol} 전량 매도 완료 → 리버스모드 종료")
            return {"orders": orders, "new_T": new_T, "exit": True}

    return {"orders": orders, "new_T": new_T, "exit": False, "reverse_day": day_count}


def 무한매수법_V4(broker: Broker, symbol, exchange_code, splits, symbol_type, seed=0, T=0.0, additional_loc_levels=3, state=None):
    """
    무한매수법 V4.0 전략을 실행합니다.

    V4.0의 핵심 아이디어:
    - T값(누적 매수 횟수)에 따라 별지점이 자동으로 조정됩니다.
    - 전반전(T < splits/2): 절반은 별지점 LOC, 절반은 평단 LOC로 분산 매수
    - 후반전(T >= splits/2): 전체를 별지점 LOC에 집중 매수
    - 항상 쿼터매도(LOC) + 지정가 목표매도 두 개의 매도 주문을 함께 제출합니다.
        - 목표 익절가는 symbol_type 기준 고정 배율을 사용합니다.
            (TQQQ: 평단 x 1.15, SOXL: 평단 x 1.20)

        왜 고정 배율을 쓰나요?
        - V4 학습용 로직은 종목별 익절 규칙을 단순하게 유지하는 것이 핵심입니다.
        - 환경변수 후보를 많이 두면 설정 실수로 실제 전략과 다른 기대를 만들 수 있어,
            전략 내부의 명시값으로 동작을 고정했습니다.

    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ")
        exchange_code (str): 거래소 코드 (예: "NAS")
        splits (int): 분할 수 (20 또는 40)
        symbol_type (str): 종목 타입 ("TQQQ" 또는 "SOXL") — 별지점 공식 결정용
        seed (float): 이 종목에 투입할 최대 금액 (달러). 필수.
        T (float): 현재까지의 누적 매수 횟수 (state.py에서 로드)
        additional_loc_levels (int): 급락 대비 추가 LOC 주문 단계 수 (기본값 3)

    Returns:
        dict: 전략 결과
            - symbol, exchange, tradable
            - open_price, last_price
            - position_qty, avg_price
            - orderable_cash, seed, remaining_seed
            - T: 전달받은 T값 (참고용)
            - unit_amount: 이번 회차 1회 매수금
            - unit_qty: 1회 매수 수량 (unit_amount / last_price)
            - star_point: 별지점 가격
            - star_buy_price: 매수점 (별지점 - 0.01)
            - take_profit_price: 목표 익절가 (TQQQ: 평단×1.15, SOXL: 평단×1.20)
            - orders: 예상 주문 목록

    Raises:
        Exception: 잔고 부족 또는 API 호출 실패 시
    """

    # ========================================
    # 1. 시장 정보 조회
    # ========================================

    quotation = broker.get_stock_quotation(symbol, exchange_code)
    tradable = quotation.tradable

    price_detail = broker.get_stock_price(symbol, exchange_code)
    open_price = price_detail.open
    last_price = price_detail.last

    if last_price <= 0:
        print(f"[경고] {symbol} 현재가({last_price})를 조회할 수 없습니다. 주문을 생성하지 않습니다.")
        return {
            "symbol": symbol,
            "exchange": exchange_code,
            "tradable": tradable,
            "open_price": open_price,
            "last_price": last_price,
            "position_qty": 0,
            "avg_price": 0.0,
            "orderable_cash": 0.0,
            "seed": seed,
            "remaining_seed": None,
            "T": T,
            "unit_amount": 0.0,
            "unit_qty": 0,
            "star_point": None,
            "star_buy_price": None,
            "take_profit_price": None,
            "orders": [],
        }

    # ========================================
    # 1.5 net_invested 신뢰성 게이트
    # ========================================
    # 기존 상태에 status가 없더라도 양수 net_invested가 있으면 기존 운용을 유지합니다.
    # 0 이하인 unresolved 상태만 시드 캡이 풀린 손상 상태로 보고 신규 주문을 차단합니다.
    # (SOXL net_invested=0.00 손상 사고 — 시드 캡 왜곡으로 과매수 방지용.)
    net_invested = float(state.get("net_invested", 0.0) or 0.0) if state else 0.0
    status_unresolved = state is not None and (state.get("net_invested_status") or "unresolved") != "valid"
    if seed > 0 and status_unresolved and net_invested <= 0:
        status = state.get("net_invested_status", "unresolved") if state else "unresolved"
        print(
            f"[상태] {symbol} net_invested_status={status} → "
            f"순투입 미확정 및 금액 0 이하로 신규 전략 주문을 보류합니다. (audit/reconcile로 상태 확인 필요)"
        )
        return {
            "symbol": symbol,
            "exchange": exchange_code,
            "tradable": tradable,
            "open_price": open_price,
            "last_price": last_price,
            "position_qty": 0,
            "avg_price": 0.0,
            "orderable_cash": 0.0,
            "seed": seed,
            "remaining_seed": None,
            "T": T,
            "unit_amount": 0.0,
            "unit_qty": 0,
            "star_point": None,
            "star_buy_price": None,
            "take_profit_price": None,
            "orders": [],
        }

    # ========================================
    # 2. 보유 정보 조회
    # ========================================

    try:
        balance = broker.get_balance(symbol, exchange_code)
    except Exception as e:
        if LS_DEMO_BYPASS_BUGS:
            print(f"[전략] 잔고 조회 실패 — LS 모의투자 버그 우회: {e}")
            balance = None
        else:
            raise

    if balance:
        position_qty = balance.quantity
        avg_price = balance.avg_price
    else:
        position_qty = 0
        avg_price = 0.0

    # 보유 수량이 0이고 T==0(새 사이클로 판단)인 경우, 초회 진입을 허용하기 위해
    # 평단가가 없다면 현재가로 대체합니다. (옵션: 학습용 간편 처리)
    if position_qty == 0 and avg_price == 0.0 and T == 0:
        if last_price > 0:
            print(f"  ※ 보유 없음(초기 사이클) — 평단가가 없어 현재가 ${last_price}로 대체하여 진행합니다")
            avg_price = last_price

    # ========================================
    # 3. 주문가능금액 조회 및 시드 적용
    # ========================================

    psamount = broker.get_purchase_amount(symbol, exchange_code)
    orderable_cash = psamount.orderable_cash

    remaining_seed = None
    if seed > 0:
        # 시드 캡은 현재 포지션 원가(평단×수량)가 아니라 실제로 투입된 순투입 금액
        # (Σ 매수체결금액 − Σ 매도체결금액)을 기준으로 합니다.
        # 평단 이하 매도(손절) 시 원가 기준으로는 시드 여유가 부풀려져
        # 누적 투입이 시드를 초과할 수 있어, state가 계산한 net_invested를 사용합니다.
        net_invested = float((state or {}).get("net_invested", 0.0) or 0.0)
        remaining_seed = max(seed - net_invested, 0.0)
        orderable_cash = min(orderable_cash, remaining_seed)
        print(
            f"  시드 적용: ${seed:.2f} (순투입 ${net_invested:.2f} 차감 후 "
            f"${orderable_cash:.2f} 사용)"
        )

    # ========================================
    # 4. 소진 상태 확인 (T > splits - 1)
    #    원문 규칙: 20분할 T>19, 40분할 T>39 — 마지막 1회 매수도 불가능한 상태
    # ========================================

    reverse_state = (state or {}).get("reverse_mode", {})
    reverse_active = bool(
        reverse_state.get("active", False)
        or reverse_state.get("reconciliation_only", False)
    )
    if T > splits - 1 or reverse_active:
        if state is not None and position_qty > 0:
            rev_result = execute_reverse_mode(
                broker, symbol, exchange_code, splits, symbol_type,
                position_qty, avg_price, orderable_cash, last_price,
                state,
            )
            new_T = rev_result["new_T"]
            rev_orders = rev_result["orders"]
            if rev_result.get("exit") and not _has_unresolved_reverse_orders(state):
                # 리버스 종료일: 같은 날 일반모드 전략까지 이어서 실행합니다 (종료 다음 날이 아니라 종료 판정일부터 복귀).
                # 아직 terminal 확정되지 않은 리버스 주문이 있으면 이어가지 않습니다 (미체결·부분체결 중 중복 주문 방지) —
                # 그 경우는 아래 기존 경로로 종료만 반환하고, 호출부가 reconciliation_only 로 전환합니다.
                # 일반 경로는 state 를 읽기만 하므로(net_invested·reverse_mode) reverse_mode 를 비운 얕은 복사본을 씁니다.
                # T 가 아직 소진 구간이면 리버스 1일차(MOC 매도)로 새로 시작합니다 — "복귀 후 T 가 1회분도 안 남으면 다시 리버스".
                print(f"[리버스모드] {symbol} 종료 → 같은 날 일반모드로 복귀합니다 (T={new_T})")
                normal_state = {**state, "reverse_mode": {}}
                normal_result = 무한매수법_V4(
                    _SnapshotBroker(broker, quotation, price_detail, balance, psamount),
                    symbol, exchange_code, splits, symbol_type,
                    seed=seed, T=new_T, additional_loc_levels=additional_loc_levels,
                    state=normal_state,
                )
                normal_result["reverse_exit"] = True
                return normal_result
            print(f"[리버스모드] {symbol} 주문 {len(rev_orders)}건 생성")
            return {
                "symbol": symbol,
                "exchange": exchange_code,
                "tradable": tradable,
                "open_price": open_price,
                "last_price": last_price,
                "position_qty": position_qty,
                "avg_price": avg_price,
                "orderable_cash": orderable_cash,
                "seed": seed,
                "remaining_seed": remaining_seed,
                "T": new_T,
                "reverse_exit": bool(rev_result.get("exit")),
                "unit_amount": 0.0,
                "unit_qty": 0,
                "star_point": None,
                "star_buy_price": None,
                "take_profit_price": None,
                "orders": rev_orders,
            }
        print(
            f"[경고] {symbol} T={T} → 소진 구간(T > {splits - 1})에 진입했지만 "
            f"보유수량이 없어 주문을 생성하지 않습니다."
        )
        return {
            "symbol": symbol,
            "exchange": exchange_code,
            "tradable": tradable,
            "open_price": open_price,
            "last_price": last_price,
            "position_qty": position_qty,
            "avg_price": avg_price,
            "orderable_cash": orderable_cash,
            "seed": seed,
            "remaining_seed": remaining_seed,
            "T": T,
            "reverse_exit": False,
            "unit_amount": 0.0,
            "unit_qty": 0,
            "star_point": None,
            "star_buy_price": None,
            "take_profit_price": None,
            "orders": [],
        }

    # ========================================
    # 5. 핵심 수치 계산
    # ========================================

    # 1회 매수금 계산
    unit_amount = calculate_unit_amount(orderable_cash, T, splits)

    # 별지점 계산 (포지션이 있을 때만 의미 있음)
    star_point = None
    star_buy_price = None
    if avg_price > 0:
        star_point = calculate_star_point(avg_price, T, splits, symbol_type)
        star_buy_price = adjust_price_to_tick(star_point - 0.01)

    # 익절가 계산: V4 고정 규칙 (환경변수 TAKE_PROFIT를 사용하지 않음)
    take_profit_multiplier = 1.15 if symbol_type == "TQQQ" else 1.20
    take_profit_price = None
    if avg_price > 0:
        take_profit_price = adjust_price_to_tick(avg_price * take_profit_multiplier)

    # 참고용: unit_amount로 살 수 있는 수량
    unit_qty = math.floor(unit_amount / last_price) if last_price > 0 else 0

    # ========================================
    # 6. 주문 생성
    # ========================================

    orders = []
    cap_notes: list[dict] = []

    if T == 0 and position_qty == 0:
        # ── 최초 진입 ──────────────────────────────────────────────
        # T=0이고 보유 주식이 없으면 처음 진입하는 상태입니다.
        # 현재가 기준 별% 위 가격에 LOC 매수 주문을 냅니다.
        initial_star_pct = 0.15 if symbol_type == "TQQQ" else 0.20
        entry_price = adjust_price_to_tick(last_price * (1 + initial_star_pct))
        entry_qty = math.floor(unit_amount / last_price)

        if entry_qty == 0:
            msg = (
                f"잔고 부족: 주문 가능 금액이 부족합니다. "
                f"현재 잔고: ${orderable_cash:.2f}, 현재가: ${last_price:.2f}"
            )
            if TRADE_MODE == "DRY":
                sim_cash = remaining_seed if remaining_seed is not None else seed
                if sim_cash > 0 and last_price > 0:
                    sim_unit_amount = calculate_unit_amount(sim_cash, T, splits)
                    sim_qty = math.floor(sim_unit_amount / last_price)
                    if sim_qty > 0:
                        print(f"  [DRY 시뮬레이션] {msg}")
                        print(f"    시드 ${sim_cash:.0f} 기준으로 시뮬레이션합니다: "
                              f"unit_amount ${unit_amount:.2f} → ${sim_unit_amount:.2f}, "
                              f"수량 {entry_qty}주 → {sim_qty}주")
                        orderable_cash = sim_cash
                        unit_amount = sim_unit_amount
                        unit_qty = sim_qty
                        entry_qty = sim_qty
                    else:
                        print(f"  [주의] {msg}")
                        print(f"  시드 ${sim_cash:.0f}(으)로는 1주도 매수할 수 없습니다. 시드를 늘려주세요.")
                        return {
                            "symbol": symbol, "exchange": exchange_code,
                            "tradable": tradable, "open_price": open_price,
                            "last_price": last_price, "position_qty": position_qty,
                            "avg_price": avg_price, "orderable_cash": orderable_cash,
                            "seed": seed, "remaining_seed": remaining_seed,
                            "T": T, "unit_amount": unit_amount, "unit_qty": unit_qty,
                            "star_point": star_point, "star_buy_price": star_buy_price,
                            "take_profit_price": take_profit_price, "orders": [],
                        }
                else:
                    print(f"  [주의] {msg}")
                    return {
                        "symbol": symbol, "exchange": exchange_code,
                        "tradable": tradable, "open_price": open_price,
                        "last_price": last_price, "position_qty": position_qty,
                        "avg_price": avg_price, "orderable_cash": orderable_cash,
                        "seed": seed, "remaining_seed": remaining_seed,
                        "T": T, "unit_amount": unit_amount, "unit_qty": unit_qty,
                        "star_point": star_point, "star_buy_price": star_buy_price,
                        "take_profit_price": take_profit_price, "orders": [],
                    }
            else:
                raise Exception(msg)

        original_entry_price = entry_price
        entry_price, entry_capped = cap_buy_loc_price(entry_price, last_price)
        if entry_capped:
            cap_notes.append({
                "comment": "초기 진입 (1회 매수)",
                "from": original_entry_price,
                "to": entry_price,
            })
            print(
                f"  ⚠️ LOC 가격 보정: ${original_entry_price:.2f} → ${entry_price:.2f} "
                f"(현재가 ${last_price:.2f} × {BUY_LOC_PRICE_CAP_RATIO} 기준)"
            )

        orders.append({
            "side": "BUY",
            "quantity": entry_qty,
            "price": entry_price,
            "order_type": "LOC",
            "comment": "초기 진입 (1회 매수)",
            "t_target": 1.0,
            # 1.19 상한으로 낮춘 경우에만 원래 전략 매수가를 남깁니다 (사후 재조정 도구용)
            "original_price": original_entry_price if entry_capped else None,
        })

        # 추가매수 LOC: 급락 시 1주씩 추가 매수 (라오어 공식: unit_amount / (entry_qty + i))
        # 1.19 상한에 걸린 후보는 건너뛰고 진입가보다 낮은 단계부터 배치합니다.
        for level, add_price in build_additional_loc_prices(
            unit_amount, entry_qty, additional_loc_levels, entry_price
        ):
            orders.append({
                "side": "BUY",
                "quantity": 1,
                "price": add_price,
                "order_type": "LOC",
                "comment": f"초기진입 추가매수 {level}단계 (급락 대비) {_ADDITIONAL_MARK}",
                "t_target": 0.0,
            })

    else:
        # ── 포지션 있음: 매도 주문 ──────────────────────────────────
        if position_qty > 0 and star_point and take_profit_price:
            quarter_qty = position_qty // 4
            remaining_qty = position_qty - quarter_qty
            sell_star_price = adjust_price_to_tick(star_point)

            if quarter_qty > 0:
                orders.append({
                    "side": "SELL",
                    "quantity": quarter_qty,
                    "price": sell_star_price,
                    "order_type": "LOC",
                    "comment": "쿼터매도 (별지점 LOC) — 체결 시 T = T × 0.75",
                    "t_target": 0.0,
                })

            if remaining_qty > 0:
                orders.append({
                    "side": "SELL",
                    "quantity": remaining_qty,
                    "price": take_profit_price,
                    "order_type": "LIMIT",
                    "comment": "목표매도 지정가 — 체결 시 T × 0.25 (보유수량 0이면 사이클 종료)",
                    "t_target": 0.0,
                })

        # ── 매수 주문 (전반전 / 후반전 구분) ─────────────────────────
        if unit_qty == 0:
            print(f"  [주의] {symbol} 1회 매수금 부족으로 매수 주문을 생략합니다.")
        elif avg_price > 0 and star_buy_price:
            if T < splits / 2:
                # 전반전: 절반은 별지점 LOC, 절반은 평단 LOC
                avg_buy_price = adjust_price_to_tick(avg_price)
                base_qty = math.floor(unit_amount / avg_buy_price)
                qty_at_star = math.floor(unit_amount / 2 / star_buy_price)
                qty_at_avg = max(base_qty - qty_at_star, 0)

                # 소액 시드로 1주만 가능하면 그 1주가 1회분 전액 → t_target=1.0
                active_round_orders = (1 if qty_at_star > 0 else 0) + (1 if qty_at_avg > 0 else 0)
                t_target_each = 1.0 if active_round_orders == 1 else 0.5
                t_target_label = "1.0" if active_round_orders == 1 else "0.5"

                normal_buy_prices: list[float] = []

                if qty_at_star > 0:
                    buy_price, capped = cap_buy_loc_price(star_buy_price, last_price)
                    if capped:
                        cap_notes.append({
                            "comment": "전반전 별지점 매수",
                            "from": star_buy_price,
                            "to": buy_price,
                        })
                        print(
                            f"  ⚠️ LOC 가격 보정: ${star_buy_price:.2f} → ${buy_price:.2f} "
                            f"(현재가 ${last_price:.2f} × {BUY_LOC_PRICE_CAP_RATIO} 기준)"
                        )
                    normal_buy_prices.append(buy_price)
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_star,
                        "price": buy_price,
                        "order_type": "LOC",
                        "comment": f"전반전 별지점 매수 — 체결 시 T += {t_target_label}",
                        "t_target": t_target_each,
                        "original_price": star_buy_price if capped else None,
                    })

                if qty_at_avg > 0:
                    buy_price, capped = cap_buy_loc_price(avg_buy_price, last_price)
                    if capped:
                        cap_notes.append({
                            "comment": "전반전 평단 매수",
                            "from": avg_buy_price,
                            "to": buy_price,
                        })
                        print(
                            f"  ⚠️ LOC 가격 보정: ${avg_buy_price:.2f} → ${buy_price:.2f} "
                            f"(현재가 ${last_price:.2f} × {BUY_LOC_PRICE_CAP_RATIO} 기준)"
                        )
                    normal_buy_prices.append(buy_price)
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_avg,
                        "price": buy_price,
                        "order_type": "LOC",
                        "comment": f"전반전 평단 매수 — 체결 시 T += {t_target_label}",
                        "t_target": t_target_each,
                        "original_price": avg_buy_price if capped else None,
                    })

                # 추가매수 LOC: 급락 시 1주씩 추가 매수 (라오어 공식: unit_amount / (base_qty + i))
                # 1.19 상한에 걸린 후보는 건너뛰고 정상매수보다 낮은 단계부터 배치합니다.
                if base_qty > 0:
                    ceiling = min(normal_buy_prices) if normal_buy_prices else float("inf")
                    for level, add_price in build_additional_loc_prices(
                        unit_amount, base_qty, additional_loc_levels, ceiling
                    ):
                        orders.append({
                            "side": "BUY",
                            "quantity": 1,
                            "price": add_price,
                            "order_type": "LOC",
                            "comment": f"추가매수 {level}단계 (급락 대비) {_ADDITIONAL_MARK}",
                            "t_target": 0.0,
                        })

            else:
                # 후반전: 전체 금액을 별지점 LOC 한 곳에
                qty_at_star = math.floor(unit_amount / star_buy_price)

                normal_buy_prices: list[float] = []

                if qty_at_star > 0:
                    buy_price, capped = cap_buy_loc_price(star_buy_price, last_price)
                    if capped:
                        cap_notes.append({
                            "comment": "후반전 별지점 매수",
                            "from": star_buy_price,
                            "to": buy_price,
                        })
                        print(
                            f"  ⚠️ LOC 가격 보정: ${star_buy_price:.2f} → ${buy_price:.2f} "
                            f"(현재가 ${last_price:.2f} × {BUY_LOC_PRICE_CAP_RATIO} 기준)"
                        )
                    normal_buy_prices.append(buy_price)
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_star,
                        "price": buy_price,
                        "order_type": "LOC",
                        "comment": "후반전 별지점 매수 (1회 매수) — 체결 시 T += 1",
                        "t_target": 1.0,
                        "original_price": star_buy_price if capped else None,
                    })

                # 추가매수 LOC: 급락 시 1주씩 추가 매수 (라오어 공식: unit_amount / (qty_at_star + i))
                # 1.19 상한에 걸린 후보는 건너뛰고 정상매수보다 낮은 단계부터 배치합니다.
                if qty_at_star > 0:
                    ceiling = min(normal_buy_prices) if normal_buy_prices else float("inf")
                    for level, add_price in build_additional_loc_prices(
                        unit_amount, qty_at_star, additional_loc_levels, ceiling
                    ):
                        orders.append({
                            "side": "BUY",
                            "quantity": 1,
                            "price": add_price,
                            "order_type": "LOC",
                            "comment": f"추가매수 {level}단계 (급락 대비) {_ADDITIONAL_MARK}",
                            "t_target": 0.0,
                        })
        else:
            # avg_price가 없는데 T > 0인 경우 (상태 불일치): 별지점 계산 불가
            print(
                f"  [주의] {symbol} 평단가가 없어 매수 주문을 생략합니다. "
                f"(T={T}이지만 보유 잔고가 없습니다)"
            )

    # ========================================
    # 7. 결과 반환
    # ========================================

    return {
        "symbol": symbol,
        "exchange": exchange_code,
        "tradable": tradable,
        "open_price": open_price,
        "last_price": last_price,
        "position_qty": position_qty,
        "avg_price": avg_price,
        "orderable_cash": orderable_cash,
        "seed": seed,
        "remaining_seed": remaining_seed,
        "T": T,
        "unit_amount": unit_amount,
        "unit_qty": unit_qty,
        "star_point": star_point,
        "star_buy_price": star_buy_price,
        "take_profit_price": take_profit_price,
        "orders": orders,
        "price_cap_notes": cap_notes,
    }
