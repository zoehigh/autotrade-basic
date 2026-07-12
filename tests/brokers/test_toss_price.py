"""
토스증권 해외주식 현재가(/api/v1/prices) 조회 테스트.

목적:
- GET /api/v1/prices?symbols={ticker} 응답 구조 확인
- TQQQ(NAS), SOXL(AMS) 현재가 조회 정상 동작 확인
- raw JSON 응답을 tests/output/ 에 덤프하여 분석
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.toss.adapter import TossBroker
from tests.brokers.base import dump_json, print_separator


def _raw_price_request(broker: TossBroker, symbol: str) -> dict:
    """토스 현재가 API를 직접 호출하여 raw 응답을 반환합니다."""
    token = broker._get_token()
    resp = broker._session.get(
        "/api/v1/prices",
        token=token,
        params={"symbols": symbol.upper()},
    )
    return resp.json()


def _print_result(label: str, raw: dict, path: Path):
    print(f"\n--- {label} ---")
    print(f"  status:  {raw.get('status', '')}")
    print(f"  error:   {raw.get('error', '')}")
    result = raw.get("result", [])
    if isinstance(result, list) and result:
        item = result[0]
        print(f"  symbol:    {item.get('symbol')}")
        print(f"  lastPrice: {item.get('lastPrice')}")
        print(f"  change:    {item.get('change')}")
        print(f"  changeRate: {item.get('changeRate')}")
        print(f"  highPrice: {item.get('highPrice')}")
        print(f"  lowPrice:  {item.get('lowPrice')}")
    elif isinstance(result, dict):
        print(f"  (dict): {result}")
    else:
        print(f"  (empty)")
    print(f"  → raw JSON saved: {path}")


def test_toss_price_tqqq():
    print_separator("토스증권 TQQQ(NASDAQ) 현재가 조회")
    broker = TossBroker()
    try:
        raw = _raw_price_request(broker, "TQQQ")
        path = dump_json(raw, "toss_tqqq_price")
        _print_result("TQQQ NASDAQ", raw, path)
    except Exception as e:
        print(f"  ❌ TQQQ 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_price_soxl():
    print_separator("토스증권 SOXL(AMEX) 현재가 조회")
    broker = TossBroker()
    try:
        raw = _raw_price_request(broker, "SOXL")
        path = dump_json(raw, "toss_soxl_price")
        _print_result("SOXL AMEX", raw, path)
    except Exception as e:
        print(f"  ❌ SOXL 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_price_invalid_symbol():
    """존재하지 않는 심볼로 404/에러 응답 구조 확인."""
    print_separator("토스증권 존재하지 않는 심볼 조회")
    broker = TossBroker()
    try:
        raw = _raw_price_request(broker, "INVALID1234")
        path = dump_json(raw, "toss_invalid_price")
        _print_result("INVALID1234", raw, path)
    except Exception as e:
        print(f"  ❌ INVALID1234 실패: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    from config import BROKER_MODE
    print(f" 현재 브로커 모드: {BROKER_MODE}")
    print(" (토스는 모의투자를 지원하지 않습니다)")

    test_toss_price_tqqq()
    test_toss_price_soxl()
    test_toss_price_invalid_symbol()
    print_separator("완료")
