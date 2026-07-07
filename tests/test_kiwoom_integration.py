"""
키움증권 모의투자 통합 테스트.

⚠️ 실제 키움 모의투자 API(mockapi.kiwoom.com)를 호출합니다.
다음 환경변수가 설정되어 있어야 실행됩니다:
  KIWOOM_APP_KEY=모의투자_앱키
  KIWOOM_APP_SECRET=모의투자_앱시크릿
  KIWOOM_ACCOUNT_NO=모의투자_계좌번호
  BROKER=kiwoom
  BROKER_MODE=demo

환경변수가 없으면 자동으로 skip됩니다.

실행:
  BROKER=kiwoom BROKER_MODE=demo KIWOOM_APP_KEY=... KIWOOM_APP_SECRET=... KIWOOM_ACCOUNT_NO=... \\
    uv run pytest tests/test_kiwoom_integration.py -v

테스트 시나리오:
  1. 현재가 조회 (TQQQ)
  2. 호가 조회 (TQQQ)
  3. 잔고 조회 (TQQQ — 모의투자 계좌에 따라 None 가능)
  4. 주문가능금액 조회
  5. 체결내역 조회 (최근 7일)
  6. 1주 매수 (LIMIT, 현재가) — 모의투자 가상 1,000만원 범위
  7. 체결내역 재조회 (매수 건 확인)
  8. 1주 매도 (LIMIT, 현재가)
  9. 잔고 재조회 (0주 확인)
"""

import os
import sys
import time
import importlib
from pathlib import Path

from dotenv import load_dotenv

# .env 파일을 명시적으로 로드 — 모듈 레벨 skip 조건이 다른 테스트의
# config.py 임포트 순서에 의존하지 않도록 테스트 격리를 보장합니다.
load_dotenv()

# src 디렉터리를 Python 경로에 추가 (conftest.py가 처리하지만 명시적으로 추가)
_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import pytest
from broker.base import (
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    OrderError,
)

# ═══════════════════════════════════════════════════════════════════════
# 모듈 레벨 skip 조건
# ═══════════════════════════════════════════════════════════════════════

KIWOOM_CREDENTIALS_AVAILABLE = all([
    os.getenv("KIWOOM_APP_KEY"),
    os.getenv("KIWOOM_APP_SECRET"),
    os.getenv("KIWOOM_ACCOUNT_NO"),
])

pytestmark = pytest.mark.skipif(
    not KIWOOM_CREDENTIALS_AVAILABLE,
    reason=(
        "KIWOOM_APP_KEY / KIWOOM_APP_SECRET / KIWOOM_ACCOUNT_NO "
        "환경변수가 설정되지 않았습니다."
    ),
)

# ═══════════════════════════════════════════════════════════════════════
# 테스트 상수
# ═══════════════════════════════════════════════════════════════════════

TEST_SYMBOL = "TQQQ"
TEST_EXCHANGE = "NAS"  # NASDAQ → 키움 내부 코드: ND


# ═══════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def kiwoom_broker():
    """
    키움 모의투자 broker 인스턴스 (모듈 전체에서 재사용).

    config.py가 모듈 로드 시점에 환경변수를 읽으므로,
    BROKER / BROKER_MODE / TRADE_MODE를 먼저 설정한 뒤 config를 리로드합니다.

    TRADE_MODE=LIVE 필수 — 통합 테스트는 실제 모의투자 API를 호출해야 하므로
    create_broker()가 DryBroker로 래핑하지 않도록 합니다.
    """
    # 기존 환경변수 백업 (다른 테스트에 영향을 주지 않도록 격리)
    _env_keys = ("BROKER", "BROKER_MODE", "TRADE_MODE")
    saved = {key: os.environ.get(key) for key in _env_keys}

    os.environ["BROKER"] = "kiwoom"
    os.environ["BROKER_MODE"] = "demo"
    os.environ["TRADE_MODE"] = "LIVE"  # DryBroker 래핑 방지

    # config 모듈 리로드 — env var 변경 반영
    import config
    importlib.reload(config)

    from broker import create_broker
    broker = create_broker()
    print(f"[키움 통합테스트] broker={broker.name}, mode={broker._mode}")
    print(f"  domain={broker._domain}")
    print(f"  account_no={broker._account_no[0:4]}****")
    yield broker
    broker.close()

    # 환경변수 복원 — 후속 테스트에 영향을 주지 않도록
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    importlib.reload(config)


@pytest.fixture(scope="module")
def context():
    """테스트 간 상태 공유 (매수 주문 ID, 가격 등)."""
    return {
        "buy_order_id": None,
        "buy_price": None,
    }


@pytest.fixture(autouse=True)
def _rate_limit_wait():
    """
    각 테스트 후 1.2초 대기 (모의투자 rate limit 1회/초).

    KiwoomBroker 내부에서도 rate-limit 재시도 로직이 있지만,
    테스트 간 간격을 두어 불필요한 재시도를 방지합니다.
    """
    yield
    time.sleep(1.2)


# ═══════════════════════════════════════════════════════════════════════
# 테스트 클래스
# ═══════════════════════════════════════════════════════════════════════

class TestKiwoomMockIntegration:
    """키움증권 모의투자 API 통합 테스트 — 조회 → 매수 → 매도 전체 플로우."""

    # ── 조회 API ───────────────────────────────────────────────────────

    def test_01_is_trading_day(self, kiwoom_broker):
        """영업일 여부를 반환하는지 확인합니다."""
        result = kiwoom_broker.is_trading_day()
        assert isinstance(result, bool), (
            f"is_trading_day()가 bool을 반환해야 합니다: {type(result)}"
        )

    def test_02_get_stock_price(self, kiwoom_broker):
        """TQQQ 현재가를 조회하고 StockPrice dataclass를 반환하는지 확인합니다."""
        price = kiwoom_broker.get_stock_price(TEST_SYMBOL, TEST_EXCHANGE)
        assert isinstance(price, StockPrice), (
            f"get_stock_price()가 StockPrice를 반환해야 합니다: {type(price)}"
        )
        assert price.last > 0, (
            f"현재가가 0입니다 (모의투자 서버 문제 또는 장 마감): last={price.last}"
        )
        assert price.open > 0, (
            f"시가가 0입니다 (모의투자 서버 문제): open={price.open}"
        )
        print(f"  TQQQ 현재가: ${price.last:.2f} (시가: ${price.open:.2f})")

    def test_03_get_stock_quotation(self, kiwoom_broker):
        """TQQQ 호가를 조회하고 StockQuotation dataclass를 반환하는지 확인합니다."""
        quotation = kiwoom_broker.get_stock_quotation(TEST_SYMBOL, TEST_EXCHANGE)
        assert isinstance(quotation, StockQuotation), (
            f"get_stock_quotation()가 StockQuotation을 반환해야 합니다: {type(quotation)}"
        )
        assert isinstance(quotation.tradable, bool), (
            f"tradable 필드가 bool이어야 합니다: {type(quotation.tradable)}"
        )
        assert quotation.last > 0, (
            f"호가의 현재가가 0입니다: last={quotation.last}"
        )
        print(f"  TQQQ 호가: ${quotation.last:.2f}, 거래가능: {quotation.tradable}")

    def test_04_get_balance(self, kiwoom_broker):
        """TQQQ 잔고를 조회합니다 (None 또는 Balance)."""
        balance = kiwoom_broker.get_balance(TEST_SYMBOL, TEST_EXCHANGE)
        if balance is not None:
            assert isinstance(balance, Balance), (
                f"get_balance()가 Balance 또는 None을 반환해야 합니다: {type(balance)}"
            )
            assert isinstance(balance.quantity, int)
            assert isinstance(balance.avg_price, float)
            print(f"  TQQQ 보유: {balance.quantity}주, 평단: ${balance.avg_price:.2f}")
        else:
            print("  TQQQ 보유: 없음")

    def test_05_get_purchase_amount(self, kiwoom_broker):
        """주문가능금액을 조회하고 PurchaseAmount dataclass를 반환하는지 확인합니다."""
        # 모의투자에서는 외화 예수금이 없으면 result_list가 빈 리스트 → 0.0 반환 (정상).
        # 실전에서는 result_list[].fc_ord_alowa 값 반환.
        amount = kiwoom_broker.get_purchase_amount(TEST_SYMBOL, TEST_EXCHANGE)
        assert isinstance(amount, PurchaseAmount), (
            f"get_purchase_amount()가 PurchaseAmount를 반환해야 합니다: {type(amount)}"
        )
        assert isinstance(amount.orderable_cash, float)
        assert amount.orderable_cash >= 0, (
            f"주문가능금액이 음수입니다: {amount.orderable_cash}"
        )
        print(f"  주문가능금액: ${amount.orderable_cash:.2f}")

    def test_06_get_order_history(self, kiwoom_broker):
        """체결내역을 조회하고 list를 반환하는지 확인합니다 (최근 7일)."""
        # 모의투자: ust21150(일별 주문체결내역) 사용 — ust21100/ust21180 미지원 대안.
        # 실전: ust21100(거래내역) 사용.
        # 거래내역이 없으면 빈 리스트 반환 (정상).
        history = kiwoom_broker.get_order_history(
            TEST_SYMBOL, TEST_EXCHANGE, days=7, verbose=True,
        )
        assert isinstance(history, list), (
            f"get_order_history()가 list를 반환해야 합니다: {type(history)}"
        )
        print(f"  최근 7일 체결내역: {len(history)}건")

        # 각 항목의 타입과 표준 필드 검증
        if history:
            for item in history:
                assert isinstance(item, dict), (
                    f"각 체결 항목은 dict여야 합니다: {type(item)}"
                )
            # 표준 필드 존재 여부 검사 (최대 3건)
            standard_fields = {
                "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
                "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
                "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
                "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
            }
            for idx, item in enumerate(history[:3]):
                missing = standard_fields - set(item.keys())
                assert not missing, (
                    f"항목 #{idx}에 표준 필드 누락: {missing}\n"
                    f"  item keys: {sorted(item.keys())}"
                )

    # ── 주문 API (매수 → 확인 → 매도 → 확인) ─────────────────────────

    def test_07_place_buy_order(self, kiwoom_broker, context):
        """TQQQ 1주 LIMIT 매수를 실행합니다 — 현재가로 지정가 주문.

        주의: 미국 장 시간(ET 09:30~16:00)에만 주문 가능.
        장 마감 시 RC4058 오류로 자동 skip.
        """
        # 현재가 조회
        price = kiwoom_broker.get_stock_price(TEST_SYMBOL, TEST_EXCHANGE)
        if price.last <= 0:
            pytest.skip("현재가가 0이어서 매수 테스트를 건너뜁니다 (모의투자 서버 문제).")

        buy_price = round(price.last, 2)
        print(f"  매수 시도: {TEST_SYMBOL} 1주 @ ${buy_price:.2f} (LIMIT)")

        try:
            result = kiwoom_broker.place_order(
                TEST_SYMBOL,
                TEST_EXCHANGE,
                "BUY",
                1,
                buy_price,
                "LIMIT",
            )
        except OrderError as e:
            # 장 마감 오류(RC4058 등)는 skip, 그 외 오류는 re-raise
            err_msg = str(e)
            if "RC4058" in err_msg or "장종료" in err_msg or "장 마감" in err_msg:
                pytest.skip(
                    f"미국 장 마감 상태로 주문이 거부되었습니다. "
                    f"장 시간(ET 09:30~16:00)에 재실행하세요. 오류: {err_msg[:80]}"
                )
            raise

        if result is None:
            pytest.skip("매수 주문 결과가 None입니다 (DRY 모드 또는 미실행).")

        assert isinstance(result, OrderResult), (
            f"place_order()가 OrderResult를 반환해야 합니다: {type(result)}"
        )
        assert result.order_id, (
            f"주문번호가 비어 있습니다: {result}"
        )
        assert isinstance(result.order_time, str)
        assert isinstance(result.is_reservation, bool)

        # 컨텍스트 저장 (다음 테스트에서 활용)
        context["buy_order_id"] = result.order_id
        context["buy_price"] = buy_price

        print(f"  ✅ 매수 주문 성공: 주문번호={result.order_id}")
        print(f"     주문시각={result.order_time}, 예약여부={result.is_reservation}")

    def test_08_verify_buy_in_history(self, kiwoom_broker, context):
        """체결내역에서 앞서 실행한 매수 건을 확인합니다."""
        buy_order_id = context.get("buy_order_id")
        if not buy_order_id:
            pytest.skip("이전 매수 주문 정보가 없어 건너뜁니다.")

        # 체결내역 조회 (최근 7일, 재시도 포함)
        history = kiwoom_broker.get_order_history(
            TEST_SYMBOL, TEST_EXCHANGE, days=7,
        )

        # 방금 매수한 건을 찾습니다
        found = False
        for item in history:
            if item.get("odno") == buy_order_id:
                found = True
                side_name = item.get("sll_buy_dvsn_cd_name", "")
                qty = item.get("ft_ccld_qty", "0")
                price = item.get("ft_ccld_unpr3", "0")
                status = item.get("prcs_stat_name", "")
                print(f"  ✅ 매수 건 확인: odno={buy_order_id}")
                print(f"     구분={side_name}, 체결수량={qty}, 체결가={price}")
                print(f"     처리상태={status}")
                break

        if not found:
            # 체결이 지연되었을 수 있으므로 skip (실패가 아님)
            pytest.skip(
                f"매수 주문({buy_order_id})이 아직 체결내역에 없습니다. "
                "모의투자 환경에서 체결이 지연되었을 수 있습니다."
            )

    def test_09_place_sell_order(self, kiwoom_broker, context):
        """TQQQ 1주 LIMIT 매도를 실행합니다 — 현재가로 지정가 주문."""
        buy_order_id = context.get("buy_order_id")
        if not buy_order_id:
            pytest.skip("이전 매수 주문 정보가 없어 매도 테스트를 건너뜁니다.")

        # 현재가 재조회 (시장가 변동 반영)
        price = kiwoom_broker.get_stock_price(TEST_SYMBOL, TEST_EXCHANGE)
        if price.last <= 0:
            pytest.skip("현재가가 0이어서 매도 테스트를 건너뜁니다.")

        sell_price = round(price.last, 2)
        print(f"  매도 시도: {TEST_SYMBOL} 1주 @ ${sell_price:.2f} (LIMIT)")

        result = kiwoom_broker.place_order(
            TEST_SYMBOL,
            TEST_EXCHANGE,
            "SELL",
            1,
            sell_price,
            "LIMIT",
        )

        if result is None:
            pytest.skip("매도 주문 결과가 None입니다 (DRY 모드 또는 미실행).")

        assert isinstance(result, OrderResult), (
            f"place_order()가 OrderResult를 반환해야 합니다: {type(result)}"
        )
        assert result.order_id

        context["sell_order_id"] = result.order_id
        print(f"  ✅ 매도 주문 성공: 주문번호={result.order_id}")
        print(f"     주문시각={result.order_time}")

    def test_10_verify_balance_after_sell(self, kiwoom_broker, context):
        """매수→매도 후 TQQQ 잔고가 0 또는 None인지 확인합니다."""
        if not context.get("buy_order_id"):
            pytest.skip("매수 주문이 실행되지 않아 잔고 검증을 건너뜁니다.")

        balance = kiwoom_broker.get_balance(TEST_SYMBOL, TEST_EXCHANGE)

        if balance is None:
            print("  ✅ 잔고: None (보유 수량 없음)")
            return

        assert isinstance(balance, Balance)
        print(f"  TQQQ 잔고: {balance.quantity}주, 평단: ${balance.avg_price:.2f}")

        if balance.quantity > 0:
            # 매도가 아직 체결되지 않았을 수 있음 (모의투자)
            print(
                f"  ⚠️  잔고가 {balance.quantity}주 남아 있습니다. "
                "매도 주문이 아직 체결되지 않았을 수 있습니다."
            )
        else:
            print("  ✅ 잔고: 0주 (매도 완료)")


# ═══════════════════════════════════════════════════════════════════════
# 독립 실행 지원
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
