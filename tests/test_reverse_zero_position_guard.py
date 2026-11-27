"""리버스모드에서 보유 수량이 0 일 때의 동작 고정 (분석용 특성화 테스트 — 전략 로직은 바꾸지 않음).

`execute_reverse_mode()` 안의 `day_count > 5 and position_qty <= 0` 종료 조건은 운영 경로에서는 도달하지 않습니다:
  - `무한매수법_V4()` 는 보유 수량이 0 이면 `execute_reverse_mode()` 를 호출하지 않고 주문 없이 돌려줍니다.
  - 보유 0 + T>0 은 호출부(pipeline)가 전략 실행 전에 사이클 종료로 감지해 state 를 초기화합니다.
"""
import pytest

import strategy
from strategy import 무한매수법_V4
from test_reverse_mode import _make_broker_mock


@pytest.mark.parametrize("prev_day_count", [0, 2, 5, 8])
def test_리버스_활성_상태에서_보유가_0이면_일차와_무관하게_주문도_종료도_없다(monkeypatch, prev_day_count):
    monkeypatch.setattr(strategy, "get_finnhub_ma5", lambda symbol: None)
    state = {"T": 19.5, "net_invested": 0.0, "close_prices": [50.0] * 5,
             "reverse_mode": {"active": True, "day_count": prev_day_count, "cycle_id": "C1"}}
    broker = _make_broker_mock(last_price=50.0, position_qty=0, avg_price=0.0, orderable_cash=1000.0)

    result = 무한매수법_V4(broker, symbol="SOXL", exchange_code="NYS", splits=20, symbol_type="SOXL",
                         seed=0, T=19.5, state=state)

    assert result["orders"] == []
    assert result["reverse_exit"] is False
    assert state["reverse_mode"]["active"] is True          # 전략은 state 를 바꾸지 않음 (사이클 종료 초기화는 호출부 몫)
