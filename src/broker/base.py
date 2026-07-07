"""
증권사 공통 인터페이스 — Broker 추상 클래스 + 공통 타입 + DryBroker

모든 증권사 어댑터는 Broker 클래스를 상속받아 구현합니다.
strategy.py와 trading_bot.py는 이 인터페이스만 의존하며,
구체적인 증권사 구현체(KISBroker, KiwoomBroker 등)를 직접 import하지 않습니다.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════
# 반환 타입 (dataclass)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class StockPrice:
    """현재가 정보"""
    open: float
    last: float


@dataclass
class StockQuotation:
    """주문 가능 여부 + 현재가"""
    tradable: bool
    last: float


@dataclass
class Balance:
    """보유 잔고"""
    quantity: int
    avg_price: float


@dataclass
class PurchaseAmount:
    """주문 가능 금액"""
    orderable_cash: float


@dataclass
class OrderResult:
    """주문 실행 결과"""
    order_id: str            # 주문번호
    order_time: str          # 주문시각 또는 예약접수일자
    is_reservation: bool     # 예약주문 여부


# ═══════════════════════════════════════════════════════════════════════
# 예외 계층
# ═══════════════════════════════════════════════════════════════════════

class BrokerError(Exception):
    """브로커 API 오류 기본 클래스"""
    pass


class AuthError(BrokerError):
    """인증 오류"""
    pass


class OrderError(BrokerError):
    """주문 실행 오류"""
    pass


# ═══════════════════════════════════════════════════════════════════════
# Broker 추상 클래스
# ═══════════════════════════════════════════════════════════════════════

class Broker(ABC):
    """
    증권사 공통 인터페이스.

    새 증권사 추가 시 이 클래스를 상속받아 모든 추상 메서드를 구현하세요.
    구현 누락 방지를 위해 ABC를 사용합니다 — 상속 시점에 미구현 메서드가 있으면
    즉시 TypeError가 발생합니다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """증권사 식별자 (예: 'kis', 'kiwoom', 'ls')"""
        pass

    # ── 시장 정보 ──────────────────────────────────────────────────────

    @abstractmethod
    def is_trading_day(self) -> bool:
        """오늘이 미국 증시 영업일인지 확인 (NYSE 기준)."""
        pass

    # ── 조회 API (strategy.py가 호출) ──────────────────────────────────

    @abstractmethod
    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        """현재가 조회 → StockPrice(open, last)."""
        pass

    @abstractmethod
    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """주문 가능 여부 + 현재가 → StockQuotation(tradable, last)."""
        pass

    @abstractmethod
    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """보유 잔고 조회 → Balance(quantity, avg_price). 없으면 None."""
        pass

    @abstractmethod
    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """주문 가능 금액 → PurchaseAmount(orderable_cash)."""
        pass

    @abstractmethod
    def get_order_history(
        self,
        symbol: str,
        exchange: str,
        days: int = 30,
        verbose: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """
        주문 체결 내역 조회 → list[dict].

        각 dict는 다음 표준 필드를 포함해야 합니다 (state.py 호환):
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        pass

    # ── 주문 API (trading_bot.py가 호출) ───────────────────────────────

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        exchange: str,
        side: str,            # "BUY" | "SELL"
        quantity: int,
        price: float,
        order_type: str,      # "LOC" | "LIMIT" | "MOC" | "MOO" | "LOO"
    ) -> Optional[OrderResult]:
        """
        주문 실행. 예약주문 필요시 어댑터 내부에서 처리.

        Returns:
            OrderResult: 주문 성공 시 (주문번호, 시각, 예약여부)
            None: DRY 모드이거나 주문이 실행되지 않은 경우
        """
        pass

    # ── 유틸리티 ───────────────────────────────────────────────────────

    @abstractmethod
    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → 'NASD')."""
        pass

    # ── 라이프사이클 ───────────────────────────────────────────────────

    def close(self):
        """HTTP 세션 등 리소스를 정리합니다. 기본 구현은 no-op."""
        pass


# ═══════════════════════════════════════════════════════════════════════
# DryBroker (Decorator)
# ═══════════════════════════════════════════════════════════════════════

class DryBroker(Broker):
    """
    DRY 모드 데코레이터.

    조회 API는 실제 브로커에게 위임하고,
    주문 API(place_order)는 로그만 출력하고 None을 반환합니다.

    사용법:
        real_broker = KISBroker(...)
        broker = DryBroker(real_broker)   # TRADE_MODE == "DRY"일 때
    """

    def __init__(self, real: Broker):
        self._real = real

    @property
    def name(self) -> str:
        return self._real.name

    # ── 시장 정보: 실제 브로커에 위임 ──
    def is_trading_day(self) -> bool:
        return self._real.is_trading_day()

    # ── 조회 API: 실제 브로커에 위임 ──
    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        return self._real.get_stock_price(symbol, exchange)

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        return self._real.get_stock_quotation(symbol, exchange)

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        return self._real.get_balance(symbol, exchange)

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        return self._real.get_purchase_amount(symbol, exchange)

    def get_order_history(
        self, symbol: str, exchange: str,
        days: int = 30, verbose: bool = False, limit: int = 100,
    ) -> list[dict]:
        return self._real.get_order_history(symbol, exchange, days, verbose, limit)

    # ── 주문 API: 로그만 출력 ──
    def place_order(
        self, symbol: str, exchange: str,
        side: str, quantity: int, price: float, order_type: str,
    ) -> Optional[OrderResult]:
        print("\n========== [DRY 모드] 주문 정보 ==========")
        print(f"종목 코드: {symbol}")
        print(f"거래소: {exchange}")
        print(f"매수/매도: {side}")
        print(f"주문 유형: {order_type}")
        print(f"주문 수량: {quantity}주")
        print(f"주문 가격: ${price}")
        print("실제 주문은 실행되지 않았습니다.")
        print("=========================================\n")
        return None

    # ── 유틸리티: 실제 브로커에 위임 ──
    def exchange_code(self, user_code: str) -> str:
        return self._real.exchange_code(user_code)

    def close(self):
        self._real.close()
