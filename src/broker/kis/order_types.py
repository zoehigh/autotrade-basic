"""
KIS 주문 유형 및 TR_ID 레지스트리 — 주문 유형 코드, TR_ID 매핑.

기존 src/trader.py에서 추출. KISBroker가 내부적으로 사용합니다.
"""
from broker.base import OrderError


# ── 주문 유형 코드 매핑 ──────────────────────────────────────────────
# KIS API의 ord_dvsn 코드
ORDER_TYPE_MAP = {
    "LIMIT": "00",  # 지정가
    "LOC": "34",    # 장마감지정가
    "LOO": "32",    # 장개시지정가
    "MOO": "31",    # 장개시시장가
    "MOC": "33",    # 장마감시장가
}

# 모의투자에서 지원하지 않는 주문 유형 — LIMIT으로 자동 변환
DEMO_UNSUPPORTED_ORDER_TYPES = {"LOC", "LOO", "MOO", "MOC"}


def get_ord_dvsn(order_type: str) -> str:
    """
    주문 유형명(LOC, LIMIT 등) → KIS 주문 구분 코드(00, 34 등).

    Raises:
        OrderError: 지원하지 않는 주문 유형인 경우
    """
    if order_type not in ORDER_TYPE_MAP:
        raise OrderError(f"지원하지 않는 주문 유형입니다: {order_type}")
    return ORDER_TYPE_MAP[order_type]


# ── TR_ID 레지스트리 ─────────────────────────────────────────────────
# 실전(real) / 모의(demo) TR_ID를 KIS_MODE에 따라 동적 선택

# 조회 API TR_ID
TR_ID_PRICE_DETAIL = "HHDFS76200200"       # 해외주식 현재가상세
TR_ID_QUOTATION = "HHDFS00000300"          # 해외주식 현재체결가

# 실전/모의 분기 TR_ID (real, demo)
TR_ID_BALANCE = ("TTTS3012R", "VTTS3012R")          # 잔고
TR_ID_PURCHASE_AMOUNT = ("TTTS3007R", "VTTS3007R")  # 매수가능금액
TR_ID_ORDER_HISTORY = ("TTTS3035R", "VTTS3035R")    # 주문체결내역

# 주문 TR_ID (real, demo) — BUY/SELL 구분
TR_ID_BUY_ORDER = ("TTTT1002U", "VTTT1002U")        # 매수
TR_ID_SELL_ORDER = ("TTTT1006U", "VTTT1001U")       # 매도

# 예약주문 TR_ID (real, demo) — usBuy/usSell/asia 구분
TR_ID_RESV_BUY = ("TTTT3014U", "VTTT3014U")         # 예약 매수
TR_ID_RESV_SELL = ("TTTT3016U", "VTTT3016U")        # 예약 매도
TR_ID_RESV_ASIA = ("TTTS3013U", "VTTS3013U")        # 예약 아시아


def select_tr_id(tr_id_pair: tuple[str, str], mode: str) -> str:
    """
    실전/모의 모드에 따라 TR_ID를 선택합니다.

    Parameters:
        tr_id_pair: (real_tr_id, demo_tr_id)
        mode: "real" 또는 "demo"

    Returns:
        mode에 해당하는 TR_ID
    """
    return tr_id_pair[0] if mode == "real" else tr_id_pair[1]


def get_reservation_tr_id(ord_dv: str, mode: str) -> str:
    """
    예약주문 TR_ID를 결정합니다.

    Parameters:
        ord_dv: "usBuy", "usSell", "asia" 중 하나
        mode: "real" 또는 "demo"

    Raises:
        OrderError: 잘못된 ord_dv인 경우
    """
    if ord_dv == "usBuy":
        return select_tr_id(TR_ID_RESV_BUY, mode)
    elif ord_dv == "usSell":
        return select_tr_id(TR_ID_RESV_SELL, mode)
    elif ord_dv == "asia":
        return select_tr_id(TR_ID_RESV_ASIA, mode)
    raise OrderError(f"ord_dv는 'usBuy', 'usSell', 'asia' 중 하나여야 합니다: {ord_dv}")
