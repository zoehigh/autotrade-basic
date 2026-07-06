"""
pytest 공유 fixture — 멀티 브로커 테스트 지원.

Broker 계약 테스트, MockBroker, 응답 fixture를 제공합니다.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# src 디렉터리를 Python 경로에 추가 (broker.base 임포트 전에 필요)
_src_path = str(Path(__file__).parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from broker.base import StockPrice, StockQuotation, Balance, PurchaseAmount

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    """응답 fixture 디렉터리 경로."""
    return FIXTURES_DIR


@pytest.fixture
def mock_broker():
    """
    완전 mock 브로커 — strategy.py/state.py 테스트용.
    실제 API 호출 없이 고정된 값을 반환합니다.

    StockPrice(open, last)
    StockQuotation(tradable, last)
    Balance(quantity, avg_price)
    PurchaseAmount(orderable_cash)
    """
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_stock_price.return_value = StockPrice(open=50.0, last=52.0)
    broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=52.0)
    broker.get_balance.return_value = Balance(quantity=10, avg_price=48.0)
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=5000.0)
    broker.get_order_history.return_value = []
    broker.exchange_code.return_value = "NASD"
    broker.close = MagicMock()
    return broker


@pytest.fixture
def load_fixture_json():
    """
    fixture JSON 파일을 읽어 dict로 반환하는 팩토리 fixture.

    사용법:
        def test_something(load_fixture_json):
            data = load_fixture_json("kis_stock_price.json")
            assert data["rt_cd"] == "0"
    """
    def _load(filename: str) -> dict:
        path = FIXTURES_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"fixture 파일이 없습니다: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return _load


@pytest.fixture
def mock_response_factory():
    """
    requests.Response를 흉내내는 MockResponse 객체를 생성하는 팩토리.

    KISBroker 계약 테스트에서 HTTP 응답을 mock할 때 사용합니다.
    mock_session.request()가 이 객체를 반환하도록 설정하세요.

    사용법:
        mock_session.request.return_value = mock_response_factory(
            {"rt_cd": "0", "output": {"open": "50.0", "last": "52.0"}}
        )
    """
    def _create(
        json_data: dict,
        status_code: int = 200,
        headers: dict | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = json_data
        resp.status_code = status_code
        resp.ok = 200 <= status_code < 300
        resp.headers = headers or {"tr_cont": ""}
        # raise_for_status: ok면 no-op, 아니면 예외
        if not resp.ok:
            resp.raise_for_status.side_effect = ConnectionError(
                f"HTTP {status_code}"
            )
        else:
            resp.raise_for_status = MagicMock()
        return resp
    return _create
