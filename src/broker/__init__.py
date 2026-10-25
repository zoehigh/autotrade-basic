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
    ReadOnlyBroker,
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    BrokerError,
    AuthError,
    OrderError,
)

# RUN_PHASE 별 허용 TRADE_MODE — 브로커 생성 전에 fail-closed 로 검증합니다.
_RUN_PHASE_MATRIX = {
    "SETTLE": {"DRY", "LIVE"},
    "PREVIEW": {"DRY", "LIVE"},
    "APPROVE": {"LIVE"},
    "EXECUTE": {"LIVE"},
    "VERIFY": {"DRY", "LIVE"},
}


def validate_run_phase(run_phase=None, trade_mode=None) -> str:
    """RUN_PHASE / TRADE_MODE 조합을 검증합니다 (브로커 생성 전).

    유효하지 않은 조합이면 broker 를 만들기 전에 SystemExit(1) 로 중단합니다.
    RUN_PHASE 미설정("")은 기존 통합 실행(레거시)으로 간주합니다.
    """
    import os

    phase = (
        run_phase if run_phase is not None else os.getenv("RUN_PHASE", "")
    ).strip().upper()
    mode = (
        trade_mode if trade_mode is not None else os.getenv("TRADE_MODE", "DRY")
    ).strip().upper()

    if not phase:
        # 레거시 통합 실행은 DRY 프리뷰만 허용합니다.
        # LIVE 주문은 반드시 RUN_PHASE(APPROVE/EXECUTE) 승인 게이트를 통과해야 합니다.
        if mode == "LIVE":
            print(
                "[RUN_PHASE] LIVE 에는 RUN_PHASE 가 필요합니다 "
                "(SETTLE → PREVIEW → APPROVE → EXECUTE). 레거시 LIVE 실행은 차단됩니다.",
                file=__import__("sys").stderr,
            )
            raise SystemExit(1)
        return ""

    allowed = _RUN_PHASE_MATRIX.get(phase)
    if allowed is None:
        print(
            f"[RUN_PHASE] 알 수 없는 단계입니다: '{phase}'. "
            f"지원: {', '.join(sorted(_RUN_PHASE_MATRIX))}",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    if mode not in allowed:
        print(
            f"[RUN_PHASE] 허용되지 않는 조합입니다: RUN_PHASE={phase}, TRADE_MODE={mode}. "
            f"허용 TRADE_MODE: {', '.join(sorted(allowed))}",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    return phase


def create_broker() -> Broker:
    """
    config 기반으로 Broker 인스턴스를 생성합니다.

    - config.BROKER 값으로 증권사 어댑터를 선택합니다 (기본값: "kis")
    - config.TRADE_MODE == "DRY"이면 DryBroker로 래핑합니다

    Returns:
        Broker: DRY 모드면 DryBroker, LIVE 모드면 실제 브로커
    """
    from config import BROKER, TRADE_MODE

    # 잘못된 RUN_PHASE/TRADE_MODE 조합은 어댑터 생성 전에 fail-closed.
    validate_run_phase()

    if BROKER == "kis":
        from broker.kis.adapter import KISBroker
        broker = KISBroker()
    elif BROKER == "kiwoom":
        from broker.kiwoom.adapter import KiwoomBroker
        broker = KiwoomBroker()
    elif BROKER == "ls":
        from broker.ls.adapter import LSBroker
        broker = LSBroker()
    elif BROKER == "toss":
        from broker.toss.adapter import TossBroker
        broker = TossBroker()
    elif BROKER == "nhplug":
        from broker.nhplug.adapter import NHPlugBroker
        broker = NHPlugBroker()
    elif BROKER == "meritz":
        from broker.meritz.adapter import MeritzBroker
        broker = MeritzBroker()
    else:
        raise ValueError(
            f"알 수 없는 증권사입니다: BROKER={BROKER}. "
            f"지원: kis, kiwoom, ls, toss, nhplug, meritz"
        )

    if TRADE_MODE == "DRY":
        return DryBroker(broker)
    return broker
