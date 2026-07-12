"""
토스증권 해외주식 잔고(/api/v1/holdings) 및 매수가능금액(/api/v1/buying-power) 조회 테스트.

목적:
- GET /api/v1/holdings?symbol={ticker} 응답 구조 확인
- GET /api/v1/buying-power?currency=USD 응답 구조 확인
- 잔고 없을 때 응답 패턴 확인
- raw JSON 응답을 tests/output/ 에 덤프하여 분석
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.toss.adapter import TossBroker
from tests.brokers.base import dump_json, print_separator


def _raw_holdings_request(broker: TossBroker, symbol: str) -> dict:
    """토스 보유잔고 API를 직접 호출하여 raw 응답을 반환합니다."""
    token = broker._get_token()
    resp = broker._session.get(
        "/api/v1/holdings",
        token=token,
        params={"symbol": symbol.upper()},
    )
    return resp.json()


def _raw_buying_power_request(broker: TossBroker) -> dict:
    """토스 매수가능금액 API를 직접 호출하여 raw 응답을 반환합니다."""
    token = broker._get_token()
    resp = broker._session.get(
        "/api/v1/buying-power",
        token=token,
        params={"currency": "USD"},
    )
    return resp.json()


def _print_holdings_result(label: str, raw: dict, path: Path):
    print(f"\n--- {label} ---")
    print(f"  status:  {raw.get('status', '')}")
    print(f"  error:   {raw.get('error', '')}")
    result = raw.get("result", {})
    items = result.get("items", [])
    if items:
        print(f"  items count: {len(items)}")
        for i, item in enumerate(items):
            print(f"  [{i}] symbol={item.get('symbol')}, quantity={item.get('quantity')}, "
                  f"avgPurchasePrice={item.get('averagePurchasePrice')}")
    else:
        print("  (보유 종목 없음)")
    print(f"  → raw JSON saved: {path}")


def _print_buying_power_result(label: str, raw: dict, path: Path):
    print(f"\n--- {label} ---")
    print(f"  status:  {raw.get('status', '')}")
    print(f"  error:   {raw.get('error', '')}")
    result = raw.get("result", {})
    if result:
        print(f"  cashBuyingPower:   {result.get('cashBuyingPower')}")
        print(f"  settledCash:       {result.get('settledCash')}")
        print(f"  totalBuyingPower:  {result.get('totalBuyingPower')}")
        print(f"  currency:          {result.get('currency')}")
    else:
        print("  (result 없음)")
    print(f"  → raw JSON saved: {path}")


def test_toss_holdings_tqqq():
    print_separator("토스증권 TQQQ(NASDAQ) 보유잔고 조회")
    broker = TossBroker()
    try:
        raw = _raw_holdings_request(broker, "TQQQ")
        path = dump_json(raw, "toss_holdings_tqqq")
        _print_holdings_result("TQQQ NASDAQ", raw, path)
    except Exception as e:
        print(f"  ❌ TQQQ 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_holdings_soxl():
    print_separator("토스증권 SOXL(AMEX) 보유잔고 조회")
    broker = TossBroker()
    try:
        raw = _raw_holdings_request(broker, "SOXL")
        path = dump_json(raw, "toss_holdings_soxl")
        _print_holdings_result("SOXL AMEX", raw, path)
    except Exception as e:
        print(f"  ❌ SOXL 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_buying_power():
    print_separator("토스증권 매수가능금액 조회")
    broker = TossBroker()
    try:
        raw = _raw_buying_power_request(broker)
        path = dump_json(raw, "toss_buying_power")
        _print_buying_power_result("USD 매수가능금액", raw, path)
    except Exception as e:
        print(f"  ❌ buying-power 실패: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    from config import BROKER_MODE
    print(f" 현재 브로커 모드: {BROKER_MODE}")
    print(" (토스는 모의투자를 지원하지 않습니다)")

    test_toss_holdings_tqqq()
    test_toss_holdings_soxl()
    test_toss_buying_power()
    print_separator("완료")
