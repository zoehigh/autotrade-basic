"""
LS증권 주문 유형 및 TR_ID 레지스트리 — 주문 유형 코드, TR_ID 매핑.

LS증권 OPEN API 호가유형코드 (OrdprcPtnCode):
  매수: 00(지정가), M1(LOO), M2(LOC)
  매도: 00(지정가), 03(시장가), M1(LOO), M2(LOC), M3(MOO), M4(MOC)
"""
from broker.base import OrderError


# ── 주문 유형 코드 매핑 ──────────────────────────────────────────────
# 매수 가능 유형
ORDER_TYPE_MAP = {
    "LIMIT": "00",   # 지정가
    "LOO": "M1",     # 장개시지정가
    "LOC": "M2",     # 장마감지정가
}

# 매도 추가 유형 (매수 불가 유형 포함)
SELL_ORDER_TYPE_MAP = {
    "LIMIT": "00",   # 지정가
    "MARKET": "03",  # 시장가
    "LOO": "M1",     # 장개시지정가
    "LOC": "M2",     # 장마감지정가
    "MOO": "M3",     # 장개시시장가
    "MOC": "M4",     # 장마감시장가
}

# 모의투자에서 지원하지 않을 수 있는 주문 유형 — 테스트 후 필요시 활성화
DEMO_UNSUPPORTED_ORDER_TYPES: set[str] = set()


def get_ord_dvsn(order_type: str, side: str = "BUY") -> str:
    """
    주문 유형명(LOC, LIMIT 등) → LS 호가유형코드(00, M2 등).

    매도(side="SELL")는 MARKET/MOO/MOC를 추가 지원합니다.
    매수는 LIMIT/LOO/LOC만 지원합니다.

    Parameters:
        order_type (str): 주문 유형 (LIMIT, LOC, LOO, MARKET, MOO, MOC)
        side (str): "BUY" 또는 "SELL"

    Returns:
        str: LS API 호가유형코드

    Raises:
        OrderError: 지원하지 않는 주문 유형인 경우
    """
    if side == "SELL" and order_type in SELL_ORDER_TYPE_MAP:
        return SELL_ORDER_TYPE_MAP[order_type]
    if order_type in ORDER_TYPE_MAP:
        return ORDER_TYPE_MAP[order_type]
    raise OrderError(
        f"지원하지 않는 주문 유형입니다: {order_type} (side={side}). "
        f"매수: {list(ORDER_TYPE_MAP.keys())}, "
        f"매도: {list(SELL_ORDER_TYPE_MAP.keys())}"
    )


# ── TR_ID 레지스트리 ─────────────────────────────────────────────────
# LS증권은 실전/모두 동일한 TR_ID 사용 (AppKey로 환경 구분)
#
#   TR               | 용도                          | OutBlock(잔고)
#   -----------------|-------------------------------|------------------
#   COSOQ00201 ★     | 해외주식 종합잔고평가 ★       | Block4(종목)
#                    |                               | Block3(통화별)
#   COSAQ01400       | 예약주문 처리결과 조회         | Block2(주문내역)
#                    | (잔고 TR 아님!)                |
#   COSOQ02701       | 해외주식 예수금 조회           |
#
# ★ COSOQ00201 = 유일한 잔고/매수가능금액 TR.
#   - 실전 정상 (rsp_cd=02679: 조회내역없음)
#   - OutBlock4: ShtnIsuNo, AstkBalQty, FcstckUprc (종목별 수량/평단가)
#   - OutBlock3: FcurrOrdAbleAmt (매수가능금액)
#   - 모의투자 IGW40014 버그 (서버 고정폭 변환 오류)
#   - 확인 출처: https://github.com/whitehwarang/LSREST/blob/master/FrgStock/Account.py
#
# ⚠ COSAQ01400 오용 경고: 이 TR은 "예약주문 처리결과 조회" 전용.
#   동일 엔드포인트(/overseas-stock/accno)를 쓰고 OutBlock2에
#   ShtnIsuNo/AstkBalQty/FcstckUprc 필드가 있어 잔고 TR로 오인했으나
#   공식 카탈로그 명칭은 "예약주문 처리결과 조회"이다.
#   (dcff6dd 커밋에서 TR_ID_BALANCE로 잘못 사용함 — 수정 완료)

TR_ID_PRICE = "g3101"                # 해외주식 현재가 조회
TR_ID_ORDERBOOK = "g3106"            # 해외주식 현재가호가 조회 (10단위 호가 + 현재가 스냅샷)
TR_ID_BALANCE = "COSOQ00201"         # 해외주식 종합잔고평가 (실전 정상, 모의투자 IGW40014)
TR_ID_ORDER_HISTORY = "COSAQ00102"   # 해외주식 계좌주문체결내역조회 (모의투자 01900 미지원)
TR_ID_ORDER = "COSAT00301"           # 해외주식 신규주문


def select_tr_id(tr_id: str, mode: str = "demo") -> str:
    """
    실전/모의 모드에 따라 TR_ID를 선택합니다.
    LS는 동일한 TR_ID를 사용하므로 tr_id를 그대로 반환합니다.
    mode 파라미터는 Broker 인터페이스 통일성을 위해 유지합니다.
    """
    return tr_id
