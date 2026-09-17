"""가상 브로커 (VirtualBroker) — 섀도우 원장 전용.

실제 브로커를 감싸되, 전략이 읽는 잔고/주문가능금액만 가상 스냅샷에서
제공합니다. 그 외 시세/이력/거래일 등은 실제 브로커에 그대로 위임합니다.

⚠️  place_order/cancel_order는 항상 RuntimeError — 실제 주문은 절대 발생하지 않습니다.
    (shadow는 가상 체결만 사용)

읽기는 생성 시점의 스냅샷 dict 객체를 그대로 참조합니다 (복사본 아님).
run_shadow_symbol이 스냅샷을 변경하면 다음 읽기에 즉시 반영됩니다.
"""
from broker.base import Balance, PurchaseAmount


class VirtualBroker:
    """실제 브로커 + 가상 스냅샷을 결합한 읽기 전용 가상 브로커."""

    def __init__(self, real, snapshot):
        self._real = real
        self._snapshot = snapshot  # live 참조 — run_shadow_symbol이 변경하는 그 객체

    @property
    def name(self) -> str:
        return self._real.name

    # ── 가상 잔고/주문가능금액: 스냅샷 기준 ──
    def get_balance(self, symbol: str, exchange: str):
        """가상 보유 수량이 0 이하면 None (실제 브로커와 동일 규약)."""
        holdings = int(self._snapshot.get("holdings", 0))
        if holdings <= 0:
            return None
        return Balance(
            quantity=holdings,
            avg_price=float(self._snapshot.get("avg_price", 0.0)),
        )

    def get_purchase_amount(self, symbol: str, exchange: str):
        """가상 주문가능금액 = 스냅샷 현금 잔액."""
        return PurchaseAmount(
            orderable_cash=float(self._snapshot.get("cash_usd", 0.0)),
        )

    # ── 주문: 절대 실제 주문 금지 ──
    def place_order(self, *args, **kwargs):
        raise RuntimeError("shadow는 가상 체결만 사용")

    def cancel_order(self, *args, **kwargs):
        raise RuntimeError("shadow는 가상 체결만 사용")

    # ── 시장 정보: 실제 브로커에 위임 ──
    def is_trading_day(self):
        return self._real.is_trading_day()

    # ── 조회 API: 실제 브로커에 위임 ──
    def get_stock_price(self, symbol: str, exchange: str):
        return self._real.get_stock_price(symbol, exchange)

    def get_stock_quotation(self, symbol: str, exchange: str):
        return self._real.get_stock_quotation(symbol, exchange)

    def get_order_history(
        self, symbol: str, exchange: str,
        days: int = 30, verbose: bool = False, limit: int = 100,
    ):
        return self._real.get_order_history(symbol, exchange, days, verbose, limit)

    # ── 일봉 종가: 실제 브로커에 위임 ──
    def get_daily_closes(self, symbol: str, exchange: str, days: int = 5):
        return self._real.get_daily_closes(symbol, exchange, days)

    # ── 유틸리티/라이프사이클: 실제 브로커에 위임 ──
    def exchange_code(self, user_code: str):
        return self._real.exchange_code(user_code)

    def close(self):
        return self._real.close()

    # ── 미정의 속성/메서드는 실제 브로커로 fallback ──
    def __getattr__(self, item):
        real = self.__dict__.get("_real")
        if real is None:
            raise AttributeError(item)
        return getattr(real, item)