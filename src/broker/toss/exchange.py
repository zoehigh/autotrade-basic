"""
토스증권 거래소 코드 매핑.

토스증권은 미국 주식의 경우 심볼(TQQQ, SOXL)을 그대로 사용합니다.
별도의 거래소 코드 변환이 필요하지만, API 응답에는 거래소 이름이 포함됩니다.

사용자 코드: NAS, NYS, AMS
토스 API 거래소: NASDAQ, NYSE, AMEX (MarketCountry enum)
"""

# 사용자 거래소 코드 → (토스 거래소 이름, 통화 코드)
_EXCHANGE_MAP = {
    "NAS": ("NASDAQ", "USD"),
    "NYS": ("NYSE", "USD"),
    "AMS": ("AMEX", "USD"),
}


def get_api_exchange_code(user_code: str) -> str:
    """
    사용자 거래소 코드(NAS/NYS/AMS) → 토스 API 거래소 이름(NASDAQ/NYSE/AMEX).
    """
    mapping = _EXCHANGE_MAP.get(user_code)
    if mapping is None:
        raise ValueError(f"지원하지 않는 거래소 코드: {user_code}")
    return mapping[0]


def get_currency(user_code: str) -> str:
    """사용자 거래소 코드 → 통화 코드(USD)."""
    mapping = _EXCHANGE_MAP.get(user_code)
    if mapping is None:
        raise ValueError(f"지원하지 않는 거래소 코드: {user_code}")
    return mapping[1]
