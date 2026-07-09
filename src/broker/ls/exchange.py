"""
LS증권 거래소 코드 매핑 — 사용자 코드 ↔ LS API 코드/통화 코드.

사용자 입력: NAS, NYS, AMS
LS API 코드: 82(나스닥), 81(뉴욕), 83(아멕스) — 2자리 숫자
"""
from broker.base import BrokerError


# 사용자 거래소 코드 → (LS 거래소 코드 2자리, 통화 코드)
_LS_EXCHANGE_MAP = {
    "NAS": ("82", "USD"),   # NASDAQ
    "NYS": ("81", "USD"),   # NYSE
    "AMS": ("83", "USD"),   # AMEX (American Stock Exchange)
}


def convert_exchange_code(exchange_code: str) -> tuple[str, str]:
    """
    API 호출에 사용되는 거래소 코드와 통화 코드로 변환합니다.

    Parameters:
        exchange_code (str): 사용자 입력 거래소 코드 (NAS, NYS, AMS)

    Returns:
        tuple: (LS API 요청용 거래소 코드 2자리, 통화 코드)

    Raises:
        BrokerError: 지원하지 않는 거래소 코드인 경우
    """
    if exchange_code in _LS_EXCHANGE_MAP:
        return _LS_EXCHANGE_MAP[exchange_code]
    raise BrokerError(f"지원하지 않는 거래소 코드입니다: {exchange_code}")


def get_api_exchange_code(exchange_code: str) -> str:
    """사용자 거래소 코드 → LS API 거래소 코드 (예: 'NAS' → '82')."""
    return convert_exchange_code(exchange_code)[0]


def build_symbol(symbol: str, ls_exchange_code: str) -> str:
    """
    LS API용 종목키를 생성합니다.

    LS API는 '거래소코드(2자리) + 종목심볼' 형식을 사용합니다.
    (예: 'TSLA', '82' → '82TSLA')

    Parameters:
        symbol (str): 종목 심볼 (예: 'TSLA', 'TQQQ')
        ls_exchange_code (str): LS API 거래소 코드 (get_api_exchange_code()의 반환값)

    Returns:
        str: LS API 호환 종목키 (예: '82TSLA')
    """
    return f"{ls_exchange_code}{symbol.upper()}"
