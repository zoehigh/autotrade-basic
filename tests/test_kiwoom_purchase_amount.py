"""
키움증권 모의투자 주문가능금액(get_purchase_amount) 심층 테스트.

목적:
  - fc_ord_alowa 필드의 실제 의미와 정확성 확인
  - get_balance() 보유 포지션과의 일관성 검증
  - 종목별(TQQQ/SOXL) 동일 값 반환 확인 (account-level API)
  - 1주 매수 전후 orderable_cash 변화 관찰

⚠️ 실제 키움 모의투자 API(mockapi.kiwoom.com)를 호출합니다.
다음 환경변수가 설정되어 있어야 실행됩니다:
  KIWOOM_APP_KEY=모의투자_앱키
  KIWOOM_APP_SECRET=모의투자_앱시크릿
  BROKER=kiwoom
  BROKER_MODE=demo

실행:
  BROKER=kiwoom BROKER_MODE=demo KIWOOM_APP_KEY=... KIWOOM_APP_SECRET=... \\
    uv run pytest tests/test_kiwoom_purchase_amount.py -v -s
"""

import importlib
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import pytest
from broker.base import PurchaseAmount, OrderError
from broker.kiwoom.tr_registry import TR_DEPOSIT

# ═══════════════════════════════════════════════════════════════════════
# 모듈 레벨 skip 조건
# ═══════════════════════════════════════════════════════════════════════

KIWOOM_CREDENTIALS_AVAILABLE = all([
    os.getenv("KIWOOM_APP_KEY"),
    os.getenv("KIWOOM_APP_SECRET"),
])

pytestmark = pytest.mark.skipif(
    not KIWOOM_CREDENTIALS_AVAILABLE,
    reason="KIWOOM_APP_KEY / KIWOOM_APP_SECRET 환경변수가 설정되지 않았습니다.",
)


# ═══════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def kiwoom_broker():
    """키움 모의투자 broker 인스턴스."""
    _env_keys = ("BROKER", "BROKER_MODE", "TRADE_MODE")
    saved = {key: os.environ.get(key) for key in _env_keys}

    os.environ["BROKER"] = "kiwoom"
    os.environ["BROKER_MODE"] = "demo"
    os.environ["TRADE_MODE"] = "LIVE"

    import config
    importlib.reload(config)

    from broker import create_broker
    broker = create_broker()
    print(f"[키움 purchase_amount 테스트] broker={broker.name}, mode={broker._mode}")
    yield broker
    broker.close()

    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    importlib.reload(config)


@pytest.fixture(autouse=True)
def _rate_limit_wait():
    """각 테스트 후 1.2초 대기 (모의투자 rate limit 1회/초)."""
    yield
    time.sleep(1.2)


# ═══════════════════════════════════════════════════════════════════════
# 테스트 클래스
# ═══════════════════════════════════════════════════════════════════════

class TestKiwoomPurchaseAmount:
    """키움 모의투자 get_purchase_amount() 심층 테스트."""

    def test_01_returns_valid_type(self, kiwoom_broker):
        """PurchaseAmount 타입과 값이 유효한지 확인합니다."""
        amount = kiwoom_broker.get_purchase_amount("TQQQ", "NAS")

        assert isinstance(amount, PurchaseAmount), (
            f"PurchaseAmount여야 합니다: {type(amount)}"
        )
        assert isinstance(amount.orderable_cash, float), (
            f"orderable_cash가 float이어야 합니다: {type(amount.orderable_cash)}"
        )
        assert amount.orderable_cash >= 0, (
            f"주문가능금액이 음수입니다: {amount.orderable_cash}"
        )

        print(f"\n  ✅ TQQQ 주문가능금액: ${amount.orderable_cash:,.2f}")

    def test_02_raw_api_response(self, kiwoom_broker):
        """
        원시 API 응답을 확인하여 fc_ord_alowa 필드의 의미를 파악합니다.

        fc_ord_alowa: 외화 주문가능금액 (Foreign Currency Order Allowance)
        """
        token = kiwoom_broker._get_token()

        body = {}
        data = kiwoom_broker._request_with_rate_retry(TR_DEPOSIT, body, token)

        print("\n  === 원시 API 응답 (ust21110 - 예수금) ===")
        print(f"  return_code: {data.get('return_code')}")
        print(f"  return_msg: {data.get('return_msg')}")

        result_list = data.get("result_list", [])
        print(f"  result_list 타입: {type(result_list)}")
        print(f"  result_list 길이: {len(result_list) if isinstance(result_list, list) else 'N/A'}")

        if result_list and isinstance(result_list, list):
            for idx, item in enumerate(result_list):
                print(f"\n  [항목 {idx}]")
                if isinstance(item, dict):
                    for key, value in item.items():
                        print(f"    {key}: {value}")
                else:
                    print(f"    값: {item}")

            first_item = result_list[0] if isinstance(result_list, list) else result_list
            if isinstance(first_item, dict):
                fc_ord_alowa = first_item.get("fc_ord_alowa")
                print(f"\n  === 핵심 필드 분석 ===")
                print(f"  fc_ord_alowa (원시): {fc_ord_alowa}")

                from broker.kiwoom.adapter import _parse_price
                parsed = _parse_price(fc_ord_alowa)
                print(f"  fc_ord_alowa (파싱): ${parsed:,.2f}")
        else:
            print("\n  ⚠️ result_list가 비어 있습니다.")
            print("  → 모의투자에서 외화 예수금 데이터가 없을 수 있습니다.")

        assert data.get("return_code") == 0 or result_list is not None

    def test_03_balance_consistency(self, kiwoom_broker):
        """
        get_purchase_amount()와 get_balance()를 비교하여
        보고된 주문가능금액과 보유 포지션의 관계를 확인합니다.
        """
        symbols = [
            ("TQQQ", "NAS"),
            ("SOXL", "NYS"),
        ]

        total_position_value = 0.0
        purchase_amounts = {}

        for symbol, exchange in symbols:
            amount = kiwoom_broker.get_purchase_amount(symbol, exchange)
            balance = kiwoom_broker.get_balance(symbol, exchange)

            position_value = 0.0
            quantity = 0
            avg_price = 0.0

            if balance is not None:
                quantity = balance.quantity
                avg_price = balance.avg_price
                position_value = quantity * avg_price
                total_position_value += position_value

            purchase_amounts[symbol] = amount.orderable_cash

            print(f"\n  {symbol} ({exchange}):")
            print(f"    주문가능금액:    ${amount.orderable_cash:>12,.2f}")
            print(f"    보유 수량:       {quantity:>8}주")
            print(f"    평단가:          ${avg_price:>12,.2f}")
            print(f"    포지션 가치:     ${position_value:>12,.2f}")

        values = list(purchase_amounts.values())
        if len(values) >= 2:
            same_value = all(abs(v - values[0]) < 0.01 for v in values)
            print(f"\n  === 일관성 분석 ===")
            print(f"  TQQQ/SOXL 주문가능금액 동일 여부: {same_value}")
            print(f"  (account-level API이므로 동일해야 정상)")

            if not same_value:
                print(f"  ⚠️ 주문가능금액이 다릅니다!")
                for sym, val in purchase_amounts.items():
                    print(f"    {sym}: ${val:,.2f}")

        print(f"\n  총 포지션 가치: ${total_position_value:,.2f}")

    def test_04_per_symbol_same_value(self, kiwoom_broker):
        """
        TQQQ와 SOXL을 각각 조회했을 때 동일한 주문가능금액을 반환하는지 확인합니다.
        """
        amount_tqqq = kiwoom_broker.get_purchase_amount("TQQQ", "NAS")
        time.sleep(1.2)
        amount_soxl = kiwoom_broker.get_purchase_amount("SOXL", "NYS")

        print(f"\n  TQQQ 주문가능금액: ${amount_tqqq.orderable_cash:,.2f}")
        print(f"  SOXL 주문가능금액: ${amount_soxl.orderable_cash:,.2f}")

        diff = abs(amount_tqqq.orderable_cash - amount_soxl.orderable_cash)
        print(f"  차이: ${diff:,.2f}")

        assert diff < 0.01, (
            f"TQQQ/SOXL 주문가능금액이 다릅니다: "
            f"TQQQ=${amount_tqqq.orderable_cash:,.2f}, "
            f"SOXL=${amount_soxl.orderable_cash:,.2f}"
        )

        print(f"  ✅ 두 종목의 주문가능금액이 동일합니다 (account-level API 확인)")

    def test_05_pre_post_order_comparison(self, kiwoom_broker):
        """
        1주 매수 전후의 orderable_cash 변화를 관찰합니다.
        """
        symbol = "TQQQ"
        exchange = "NAS"

        amount_before = kiwoom_broker.get_purchase_amount(symbol, exchange)
        cash_before = amount_before.orderable_cash
        print(f"\n  매수 전 주문가능금액: ${cash_before:,.2f}")

        price = kiwoom_broker.get_stock_price(symbol, exchange)
        if price.last <= 0:
            pytest.skip("현재가가 0이어서 매수 테스트를 건너뜁니다.")

        buy_price = round(price.last, 2)
        print(f"  매수 시도: {symbol} 1주 @ ${buy_price:.2f} (LIMIT)")

        try:
            result = kiwoom_broker.place_order(
                symbol,
                kiwoom_broker.exchange_code(exchange),
                "BUY",
                1,
                buy_price,
                "LIMIT",
            )
        except OrderError as e:
            err_msg = str(e)
            market_closed = any(code in err_msg for code in [
                "RC4057", "RC4058", "장시작전", "장종료", "장 마감"
            ])
            if market_closed:
                pytest.skip(f"미국 장 시간 외로 주문 거부: {err_msg[:80]}")
            if "RC4025" in err_msg:
                print(f"  ⚠️ 주문가능금액 부족으로 매수 실패: {err_msg[:80]}")
                print(f"  → 현재 주문가능금액(${cash_before:,.2f})으로는 매수 불가")
                return
            raise

        if result is None:
            pytest.skip("매수 결과가 None입니다.")

        print(f"  ✅ 매수 성공: 주문번호={result.order_id}")

        time.sleep(1.2)
        amount_after = kiwoom_broker.get_purchase_amount(symbol, exchange)
        cash_after = amount_after.orderable_cash

        print(f"  매수 후 주문가능금액: ${cash_after:,.2f}")
        print(f"  차이: ${cash_before - cash_after:,.2f}")

        if cash_after < cash_before:
            print(f"  ✅ 매수 후 주문가능금액이 감소했습니다 (정상)")
        else:
            print(f"  ℹ️  주문가능금액에 변화가 없습니다 (미체결 또는 반영 지연)")

        time.sleep(1.2)
        sell_result = kiwoom_broker.place_order(
            symbol,
            kiwoom_broker.exchange_code(exchange),
            "SELL",
            1,
            buy_price,
            "LIMIT",
        )
        if sell_result:
            print(f"  ↩️ 매도 복원: 주문번호={sell_result.order_id}")


# ═══════════════════════════════════════════════════════════════════════
# 독립 실행 지원
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
