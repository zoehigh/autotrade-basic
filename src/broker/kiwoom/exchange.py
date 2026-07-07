"""
키움증권 거래소 코드 매핑.

키움의 거래소 코드 체계는 KIS와 다릅니다:
  NA → AMEX, ND → NASDAQ, NY → NYSE
"""

# 사용자 코드 → (키움 거래소 코드, 통화)
_EXCHANGE_MAP = {
    "NAS": ("ND", "USD"),   # NASDAQ
    "NYS": ("NY", "USD"),   # NYSE
    "AMS": ("NA", "USD"),   # AMEX
}


def get_api_exchange_code(user_code: str) -> str:
    """
    사용자 거래소 코드(NAS/NYS/AMS) → 키움 API 거래소 코드(ND/NY/NA).
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
