"""
메리츠증권 거래소 코드 매핑 — 시세용 코드와 주문 벤더코드를 분리 관리합니다.

메리츠증권은 같은 거래소에 대해 **두 가지 서로 다른 코드 체계**를 씁니다:
- 시세 조회용 mrkt_cls_code(2자리): /market/v1/overseas/* API에서 사용
  (예: NAS → "OQ", NYS → "NY", AMS → "AX")
- 주문 벤더코드 ovrs_exch_vndr_idno(4자리): /trading/v1/overseas/orders/* API에서 사용
  (예: NASDAQ "0537", NYSE "0321", AMEX "0066")

사용자 코드(NAS/NYS/AMS)는 broker.exchange_code() → 시세 코드(2자리)로 변환되며,
주문 API는 그 값을 다시 주문 벤더코드로 변환합니다(get_order_vendor_code는
사용자 코드/시세 코드/벤더코드 중 무엇이든 입력받아 벤더코드를 반환).

SOXL은 사용자 코드 AMS(아멕스)로 운용합니다 (키움처럼 NYS로 바꾸지 않음).

구형 코드(OQNAS/OWNYS/OXAMS, 0538/0539)는 openapi.json(2026-10-02)에 없어 삭제됨.

⚠️ 벤더코드(0537/0321/0066)는 공식 스키마 기준 값이나 실 계좌 preflight에서
1회 검증이 필요합니다.
"""

# 사용자 거래소 코드 → (시세 mrkt_cls_code, 주문 벤더코드, 통화)
_EXCHANGE_MAP = {
    "NAS": ("OQ", "0537", "USD"),  # NASDAQ
    "NYS": ("NY", "0321", "USD"),  # NYSE
    "AMS": ("AX", "0066", "USD"),  # AMEX / NYSE American
}

_QUOTE_CODE_MAP = {user: quote for user, (quote, _vendor, _ccy) in _EXCHANGE_MAP.items()}
_QUOTE_CODE_SET = {entry[0] for entry in _EXCHANGE_MAP.values()}
_VENDOR_CODE_SET = {entry[1] for entry in _EXCHANGE_MAP.values()}
_CURRENCY_MAP = {user: ccy for user, (_q, _v, ccy) in _EXCHANGE_MAP.items()}


def _normalize(user_code: str) -> str:
    """사용자 거래소 코드를 대문자로 정규화합니다."""
    code = str(user_code or "").strip().upper()
    if not code:
        raise ValueError("거래소 코드가 비어있습니다")
    return code


def get_quote_exchange_code(user_code: str) -> str:
    """
    거래소 코드 → 시세 조회용 mrkt_cls_code (broker.exchange_code()가 사용).

    사용자 코드(NAS/NYS/AMS)도, 이미 시세 코드(OQ/NY/AX)인 값도 받습니다.

    예: 'NAS' → 'OQ', 'OQ' → 'OQ'
    """
    code = _normalize(user_code)
    if code in _QUOTE_CODE_MAP:
        return _QUOTE_CODE_MAP[code]
    if code in _QUOTE_CODE_SET:
        return code
    raise ValueError(
        f"지원하지 않는 거래소 코드: {user_code} "
        f"(지원: {', '.join(sorted(_EXCHANGE_MAP))})"
    )


def get_order_vendor_code(exchange: str) -> str:
    """
    거래소 코드 → 주문 벤더코드(ovrs_exch_vndr_idno).

    broker.exchange_code()로 변환된 시세 코드(OQ/NY/AX), 사용자 코드(NAS/NYS/AMS),
    이미 벤더코드인 값을 모두 입력받을 수 있습니다 (place_order는
    호출 측에서 이미 변환된 시세 코드를 받습니다).

    예: 'OQ' → '0537', 'NAS' → '0537', '0537' → '0537'
    """
    code = _normalize(exchange)
    if code in _QUOTE_CODE_MAP:          # 사용자 코드 (NAS/NYS/AMS)
        return _EXCHANGE_MAP[code][1]
    if code in _QUOTE_CODE_SET:          # 이미 변환된 시세 코드 (OQ/NY/AX)
        user = next(k for k, v in _QUOTE_CODE_MAP.items() if v == code)
        return _EXCHANGE_MAP[user][1]
    if code in _VENDOR_CODE_SET:         # 이미 벤더코드 (0537/0321/0066)
        return code
    raise ValueError(
        f"주문 벤더코드로 변환할 수 없는 거래소 코드: {exchange} "
        f"(지원: {', '.join(sorted(_EXCHANGE_MAP))})"
    )


def get_currency(user_code: str) -> str:
    """사용자 거래소 코드 → 통화 코드 (해외주식 = USD)."""
    code = _normalize(user_code)
    if code in _CURRENCY_MAP:
        return _CURRENCY_MAP[code]
    if code in _QUOTE_CODE_MAP:
        return _EXCHANGE_MAP[code][2]
    if code in _QUOTE_CODE_SET:
        user = next(k for k, v in _QUOTE_CODE_MAP.items() if v == code)
        return _EXCHANGE_MAP[user][2]
    return "USD"
