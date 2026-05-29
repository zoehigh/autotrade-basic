# 매수/매도 여부를 판단하는 전략 로직 — 무한매수법 V4.0
import math
from trader import (
    get_overseas_stock_price,
    get_overseas_stock_quotation,
    get_overseas_balance,
    get_overseas_purchase_amount,
)

ADDITIONAL_LOC_LEVELS = 10  # 급락 대비 추가 LOC 주문 단계 수


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
    price = float(price)
    if price < 1.0:
        return math.floor(price * 10000) / 10000
    else:
        return math.floor(price * 100) / 100


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


def 무한매수법_V4(symbol, exchange_code, splits, symbol_type, seed=0, T=0.0):
    """
    무한매수법 V4.0 전략을 실행합니다.

    V4.0의 핵심 아이디어:
    - T값(누적 매수 횟수)에 따라 별지점이 자동으로 조정됩니다.
    - 전반전(T < splits/2): 절반은 별지점 LOC, 절반은 평단 LOC로 분산 매수
    - 후반전(T >= splits/2): 전체를 별지점 LOC에 집중 매수
    - 항상 쿼터매도(LOC) + 지정가 최종매도 두 개의 매도 주문을 함께 제출합니다.
        - 최종 익절가는 symbol_type 기준 고정 배율을 사용합니다.
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
        seed (float): 이 종목에 투입할 최대 금액 (달러). 0이면 계좌 전체 사용
        T (float): 현재까지의 누적 매수 횟수 (state.py에서 로드)

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
            - take_profit_price: 최종 익절가 (TQQQ: 평단×1.15, SOXL: 평단×1.20)
            - orders: 예상 주문 목록

    Raises:
        Exception: 잔고 부족 또는 API 호출 실패 시
    """

    # ========================================
    # 1. 시장 정보 조회
    # ========================================

    quotation = get_overseas_stock_quotation(symbol, exchange_code)
    tradable = quotation.get("ordy", "N") == "Y"

    price_detail = get_overseas_stock_price(symbol, exchange_code)
    open_price = float(price_detail.get("open", "0"))
    last_price = float(price_detail.get("last", "0"))

    # ========================================
    # 2. 보유 정보 조회
    # ========================================

    balance = get_overseas_balance(symbol, exchange_code)

    if balance:
        position_qty = int(float(balance.get("quantity", "0")))
        avg_price = float(balance.get("avg_price", "0"))
    else:
        position_qty = 0
        avg_price = 0.0

    # ========================================
    # 3. 주문가능금액 조회 및 시드 적용
    # ========================================

    psamount = get_overseas_purchase_amount(symbol, exchange_code)
    orderable_cash = float(psamount.get("ord_psbl_frcr_amt", "0"))

    remaining_seed = None
    if seed > 0:
        current_use_value = position_qty * avg_price
        remaining_seed = max(seed - current_use_value, 0.0)
        orderable_cash = min(orderable_cash, remaining_seed)
        print(
            f"  시드 적용: ${seed:.2f} (사용한 금액 ${current_use_value:.2f} 차감 후 "
            f"${orderable_cash:.2f} 사용)"
        )

    # ========================================
    # 4. 소진 상태 확인 (T >= splits)
    # ========================================

    if T >= splits:
        print(
            f"[경고] {symbol} T={T} → 분할 수({splits})를 모두 소진했습니다. "
            f"주문을 생성하지 않습니다."
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

    if T == 0 and position_qty == 0:
        # ── 최초 진입 ──────────────────────────────────────────────
        # T=0이고 보유 주식이 없으면 처음 진입하는 상태입니다.
        # 현재가 기준 별% 위 가격에 LOC 매수 주문을 냅니다.
        initial_star_pct = 0.15 if symbol_type == "TQQQ" else 0.20
        entry_price = adjust_price_to_tick(last_price * (1 + initial_star_pct))
        entry_qty = math.floor(unit_amount / last_price)

        if entry_qty == 0:
            raise Exception(
                f"잔고 부족: 주문 가능 금액이 부족합니다. "
                f"현재 잔고: ${orderable_cash:.2f}, 현재가: ${last_price:.2f}"
            )

        orders.append({
            "side": "BUY",
            "quantity": entry_qty,
            "price": entry_price,
            "order_type": "LOC",
            "comment": "초기 진입 (1회 매수)",
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
                })

            if remaining_qty > 0:
                orders.append({
                    "side": "SELL",
                    "quantity": remaining_qty,
                    "price": take_profit_price,
                    "order_type": "LIMIT",
                    "comment": "최종매도 지정가 — 체결 시 T 리셋 후 재진입",
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

                if qty_at_star > 0:
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_star,
                        "price": star_buy_price,
                        "order_type": "LOC",
                        "comment": "전반전 별지점 매수 (절반 매수) — 체결 시 T += 0.5",
                    })

                if qty_at_avg > 0:
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_avg,
                        "price": avg_buy_price,
                        "order_type": "LOC",
                        "comment": "전반전 평단 매수 (절반 매수) — 체결 시 T += 0.5",
                    })

                # 추가매수 LOC: 급락 시 1주씩 추가 매수 (라오어 공식: unit_amount / (base_qty + i))
                if base_qty > 0:
                    for i in range(1, ADDITIONAL_LOC_LEVELS + 1):
                        add_price = adjust_price_to_tick(unit_amount / (base_qty + i))
                        if add_price <= 0:
                            break
                        orders.append({
                            "side": "BUY",
                            "quantity": 1,
                            "price": add_price,
                            "order_type": "LOC",
                            "comment": f"추가매수 {i}단계 (급락 대비) [추가매수]",
                        })

            else:
                # 후반전: 전체 금액을 별지점 LOC 한 곳에
                qty_at_star = math.floor(unit_amount / star_buy_price)

                if qty_at_star > 0:
                    orders.append({
                        "side": "BUY",
                        "quantity": qty_at_star,
                        "price": star_buy_price,
                        "order_type": "LOC",
                        "comment": "후반전 별지점 매수 (1회 매수) — 체결 시 T += 1",
                    })

                # 추가매수 LOC: 급락 시 1주씩 추가 매수 (라오어 공식: unit_amount / (qty_at_star + i))
                if qty_at_star > 0:
                    for i in range(1, ADDITIONAL_LOC_LEVELS + 1):
                        add_price = adjust_price_to_tick(unit_amount / (qty_at_star + i))
                        if add_price <= 0:
                            break
                        orders.append({
                            "side": "BUY",
                            "quantity": 1,
                            "price": add_price,
                            "order_type": "LOC",
                            "comment": f"추가매수 {i}단계 (급락 대비) [추가매수]",
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
    }

