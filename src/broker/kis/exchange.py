"""
KIS 거래소 코드 매핑 — 사용자 코드 ↔ API 코드/통화 코드.

기존 src/trader.py의 _convert_exchange_code()에서 추출.
"""
from broker.base import BrokerError


# 사용자 거래소 코드 → (API 거래소 코드, 통화 코드)
_EXCHANGE_MAP = {
    "NAS": ("NASD", "USD"),      # 나스닥
    "NYS": ("NYSE", "USD"),      # 뉴욕
    "AMS": ("AMEX", "USD"),      # 아멕스
    "HKS": ("SEHK", "HKD"),      # 홍콩
    "TSE": ("TKSE", "JPY"),      # 도쿄
    "SHS": ("SHAA", "CNY"),      # 상해
    "SZS": ("SZAA", "CNY"),      # 심천
    "HSX": ("HASE", "VND"),      # 베트남 하노이
    "HNX": ("VNSE", "VND"),      # 베트남 호치민
}


def convert_exchange_code(exchange_code: str) -> tuple[str, str]:
    """
    API 호출에 사용되는 거래소 코드와 통화 코드로 변환합니다.

    Parameters:
        exchange_code (str): 사용자 입력 거래소 코드 (NAS, NYS, AMS 등)

    Returns:
        tuple: (API 요청용 거래소 코드, 통화 코드)

    Raises:
        BrokerError: 지원하지 않는 거래소 코드인 경우
    """
    if exchange_code in _EXCHANGE_MAP:
        return _EXCHANGE_MAP[exchange_code]
    raise BrokerError(f"지원하지 않는 거래소 코드입니다: {exchange_code}")


def get_api_exchange_code(exchange_code: str) -> str:
    """사용자 거래소 코드 → API 거래소 코드만 반환 (예: 'NAS' → 'NASD')."""
    return convert_exchange_code(exchange_code)[0]
