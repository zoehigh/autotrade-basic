"""
토스증권 해외주식 주문 실행(POST /api/v1/orders) 테스트.

목적:
- POST /api/v1/orders 요청/응답 구조 확인
- LIMIT/지정가 주문 정상 동작 확인
- 토스 error envelope 응답 패턴 확인 (유효하지 않은 심볼 등)

⚠️ 중요: 이 테스트는 DRY 모드에서도 실제 API 요청을 보냅니다.
   단, DRY 모드에서는 order_type을 강제로 LIMIT으로 변환하거나
   실제로는 주문이 실행되지 않도록 구성할 수 있습니다.

   기본적으로는 실제 주문이 실행되므로 주의하세요.
   실제 자금이 사용될 수 있음을 인지하고 실행하세요.
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.toss.adapter import TossBroker
from tests.brokers.base import dump_json, print_separator

# DRY 모드에서는 주문 요청 본문만 출력하고 실제 POST는 건너뜀
_DRY = os.environ.get("TRADE_MODE", "").upper() == "DRY"


def _raw_order_request(
    broker: TossBroker,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    order_type: str = "LIMIT",
) -> dict | None:
    """토스 주문 API에 POST 요청을 보냅니다.
    
    DRY 모드에서는 요청 body만 출력하고 None을 반환합니다.
    """
    from config import BROKER_MODE

    token = broker._get_token()

    order_body = {
        "symbol": symbol.upper(),
        "side": side,
        "orderType": order_type,
        "timeInForce": "DAY",
        "quantity": quantity,
        "price": price,
    }

    if _DRY:
        print(f"  [DRY] 요청 body: {order_body}")
        print(f"  [DRY] 실제 주문을 건너뜁니다.")
        return None

    resp = broker._session.post(
        "/api/v1/orders",
        token=token,
        json_body=order_body,
    )
    return resp.json()


def _print_result(label: str, raw: dict | None, path: Path | None):
    if raw is None:
        print(f"\n--- {label} ---")
        print("  (DRY 모드 — 실제 요청 없음)")
        return

    print(f"\n--- {label} ---")
    print(f"  status:  {raw.get('status', '')}")
    print(f"  error:   {raw.get('error', '')}")

    if "error" in raw:
        error = raw["error"]
        print(f"  error.code:    {error.get('code', '')}")
        print(f"  error.message: {error.get('message', '')}")
    else:
        result = raw.get("result", {})
        print(f"  orderId: {result.get('orderId')}")
        print(f"  status:  {result.get('status')}")
        print(f"  side:    {result.get('side')}")
        print(f"  symbol:  {result.get('symbol')}")
        print(f"  quantity: {result.get('quantity')}")
        print(f"  price:   {result.get('price')}")

    if path:
        print(f"  → raw JSON saved: {path}")


def test_toss_order_validate_body():
    """
    주문 요청 본문 검증 — API 호출 없이 body 구조만 확인.

    실제 주문을 보내지 않고 요청이 어떻게 구성되는지 확인합니다.
    """
    print_separator("토스증권 주문 요청 body 검증 (API 미호출)")
    broker = TossBroker()

    # LIMIT 지정가 매수
    body_limit = {
        "symbol": "TQQQ",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "quantity": 1,
        "price": 10.0,
    }
    print(f"\n  LIMIT 매수 body: {body_limit}")

    # LOC 장마감지정가 매수
    from broker.toss.adapter import _TOSS_ORDER_TYPE_MAP
    loc_order_type, loc_time_in_force = _TOSS_ORDER_TYPE_MAP["LOC"]
    body_loc = {
        "symbol": "TQQQ",
        "side": "BUY",
        "orderType": loc_order_type,
        "timeInForce": loc_time_in_force,
        "quantity": 1,
        "price": 10.0,
    }
    print(f"  LOC 매수 body:   {body_loc}")

    print("\n  ✅ 요청 body 검증 완료 (실제 주문 없음)")


def test_toss_order_live_buy_1_share():
    """
    TQQQ 1주 LIMIT 지정가 매수 — 실제 API 호출.

    ⚠️ TRADE_MODE != DRY 이면 실제 매수 주문이 실행됩니다.
    가격은 0.01 USD로 설정하여 체결되지 않도록 합니다.
    """
    print_separator("토스증권 TQQQ LIMIT 지정가 매수 (1주 @ $0.01)")
    broker = TossBroker()

    raw = _raw_order_request(broker, "TQQQ", "BUY", 1, 0.01)
    path = dump_json(raw, "toss_order_buy") if raw is not None else None
    _print_result("TQQQ BUY 1@0.01", raw, path)


if __name__ == "__main__":
    from config import BROKER_MODE
    print(f" 현재 브로커 모드: {BROKER_MODE}")
    print(f" TRADE_MODE:       {'DRY' if _DRY else 'LIVE/기타'}")
    if _DRY:
        print(" ⚠️  DRY 모드: 실제 주문을 실행하지 않습니다.")
    else:
        print(" ⚠️  실제 주문이 실행될 수 있습니다! 주의하세요!")

    test_toss_order_validate_body()
    test_toss_order_live_buy_1_share()
    print_separator("완료")
