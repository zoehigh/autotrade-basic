"""
증권사 브로커 패키지 — 다중 증권사 지원

사용법:
    from broker import create_broker

    broker = create_broker()   # config.BROKER + config.TRADE_MODE 기반 자동 생성
    broker.get_balance("TQQQ", "NAS")
    broker.place_order("TQQQ", "NASD", "BUY", 10, 50.0, "LOC")

새 증권사 추가:
    1. src/broker/{name}/ 패키지 생성
    2. adapter.py에 {Name}Broker(Broker) 구현
    3. 이 파일의 create_broker()에 elif 분기 추가
    4. config.py에 {NAME}_APP_KEY 등 env var 추가
"""
from broker.base import (
    Broker,
    DryBroker,
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    BrokerError,
    AuthError,
    OrderError,
)


def create_broker() -> Broker:
    """
    config 기반으로 Broker 인스턴스를 생성합니다.

    - config.BROKER 값으로 증권사 어댑터를 선택합니다 (기본값: "kis")
    - config.TRADE_MODE == "DRY"이면 DryBroker로 래핑합니다

    Returns:
        Broker: DRY 모드면 DryBroker, LIVE 모드면 실제 브로커
    """
    from config import BROKER, TRADE_MODE

    if BROKER == "kis":
        from broker.kis.adapter import KISBroker
        broker = KISBroker()
    elif BROKER == "kiwoom":
        from broker.kiwoom.adapter import KiwoomBroker
        broker = KiwoomBroker()
    elif BROKER == "ls":
        # from broker.ls.adapter import LSBroker
        # broker = LSBroker()
        raise NotImplementedError("LS증권 브로커는 아직 구현되지 않았습니다.")
    elif BROKER == "toss":
        # from broker.toss.adapter import TossBroker
        # broker = TossBroker()
        raise NotImplementedError("토스증권 브로커는 아직 구현되지 않았습니다.")
    else:
        raise ValueError(
            f"알 수 없는 증권사입니다: BROKER={BROKER}. "
            f"지원: kis, kiwoom, ls, toss"
        )

    if TRADE_MODE == "DRY":
        return DryBroker(broker)
    return broker
