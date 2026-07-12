"""
토스증권 해외주식 주문 체결내역(/api/v1/orders) 조회 테스트.

목적:
- GET /api/v1/orders?status=CLOSED&symbol={ticker} 응답 구조 확인
- 표준 필드(ord_dt, ord_tmd, ft_ccld_qty, ft_ccld_unpr3 등) 정상 변환 확인
- 커서 기반 페이지네이션 응답 구조 확인
- raw JSON 응답을 tests/output/ 에 덤프하여 분석
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.toss.adapter import TossBroker
from tests.brokers.base import dump_json, print_separator


def _raw_order_history_request(
    broker: TossBroker,
    symbol: str,
    days: int = 30,
    limit: int = 20,
) -> dict:
    """토스 주문체결내역 API를 직접 호출하여 raw 응답을 반환합니다."""
    from datetime import datetime, timedelta
    from broker.market_utils import get_kst_now

    token = broker._get_token()
    now_kst = get_kst_now()
    start_date = now_kst - timedelta(days=days)
    from_date = start_date.strftime("%Y-%m-%d")
    to_date = now_kst.strftime("%Y-%m-%d")

    resp = broker._session.get(
        "/api/v1/orders",
        token=token,
        params={
            "status": "CLOSED",
            "symbol": symbol.upper(),
            "from": from_date,
            "to": to_date,
            "limit": limit,
        },
    )
    return resp.json()


def _print_result(label: str, raw: dict, path: Path, broker: TossBroker | None = None):
    print(f"\n--- {label} ---")
    print(f"  status:  {raw.get('status', '')}")
    print(f"  error:   {raw.get('error', '')}")
    result = raw.get("result", {})
    orders = result.get("orders", [])
    next_cursor = result.get("nextCursor")
    print(f"  orders count: {len(orders)}")
    print(f"  nextCursor:   {next_cursor}")
    if orders and broker:
        print(f"\n  [정규화된 주문 데이터 (최대 5건)]")
        for i, item in enumerate(orders[:5]):
            normalized = broker._normalize_order_item(item)
            print(f"\n  --- 주문 [{i}] ---")
            for k in ("ord_dt", "ord_tmd", "sll_buy_dvsn_cd_name",
                      "ft_ord_qty", "ft_ccld_qty", "ft_ccld_unpr3",
                      "ft_ccld_amt3", "prcs_stat_name", "odno"):
                print(f"    {k}: {normalized.get(k, '')}")
    print(f"  → raw JSON saved: {path}")


def test_toss_order_history_tqqq():
    print_separator("토스증권 TQQQ(NASDAQ) 주문 체결내역 조회")
    broker = TossBroker()
    try:
        raw = _raw_order_history_request(broker, "TQQQ", days=30, limit=20)
        path = dump_json(raw, "toss_order_history_tqqq")
        _print_result("TQQQ NASDAQ", raw, path, broker)
    except Exception as e:
        print(f"  ❌ TQQQ 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_order_history_soxl():
    print_separator("토스증권 SOXL(AMEX) 주문 체결내역 조회")
    broker = TossBroker()
    try:
        raw = _raw_order_history_request(broker, "SOXL", days=30, limit=20)
        path = dump_json(raw, "toss_order_history_soxl")
        _print_result("SOXL AMEX", raw, path, broker)
    except Exception as e:
        print(f"  ❌ SOXL 실패: {e}")
        import traceback
        traceback.print_exc()


def test_toss_order_history_all():
    """심볼 없이 전체 주문 체결내역 조회."""
    print_separator("토스증권 전체 주문 체결내역 조회 (심볼 미지정)")
    broker = TossBroker()
    token = broker._get_token()
    from datetime import datetime, timedelta
    from broker.market_utils import get_kst_now
    now_kst = get_kst_now()
    start_date = now_kst - timedelta(days=30)
    try:
        resp = broker._session.get(
            "/api/v1/orders",
            token=token,
            params={
                "status": "CLOSED",
                "from": start_date.strftime("%Y-%m-%d"),
                "to": now_kst.strftime("%Y-%m-%d"),
                "limit": 20,
            },
        )
        raw = resp.json()
        path = dump_json(raw, "toss_order_history_all")
        _print_result("All symbols", raw, path, broker)
    except Exception as e:
        print(f"  ❌ 전체 조회 실패: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    from config import BROKER_MODE
    print(f" 현재 브로커 모드: {BROKER_MODE}")
    print(" (토스는 모의투자를 지원하지 않습니다)")

    test_toss_order_history_tqqq()
    test_toss_order_history_soxl()
    test_toss_order_history_all()
    print_separator("완료")
