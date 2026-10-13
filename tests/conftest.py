"""
pytest 공유 fixture — 멀티 브로커 테스트 지원.

Broker 계약 테스트, MockBroker, 응답 fixture를 제공합니다.

브로커 실 API 통합 테스트는 pytest 마커(kis/kiwoom/ls/toss)로 구분합니다.
tests/conftest.py가 .env의 자격증명 유무만으로 skip을 판단하므로,
BROKER 환경변수 값과 무관하게 해당 브로커 키가 설정돼 있으면 테스트가 실행됩니다.
"""
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

from dotenv import load_dotenv

import pytest

# .env 로드 — 자격증명 skip 판단 전에 환경변수를 읽어야 합니다.
load_dotenv()

# src 디렉터리를 Python 경로에 추가 (broker.base 임포트 전에 필요)
_src_path = str(Path(__file__).parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from broker.base import StockPrice, StockQuotation, Balance, PurchaseAmount

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# 브로커 마커 → 필수 환경변수 매핑.
# 마커가 붙은 테스트는 이 키가 하나라도 없으면 skip됩니다.
# KIS/NHPLUG는 계좌번호 없이 API 호출이 금지되므로 게이트에 포함합니다.
_REQUIRED_KEYS = {
    "kis": ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"),
    "kiwoom": ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"),
    "ls": ("LS_APP_KEY", "LS_APP_SECRET"),
    "toss": ("TOSS_APP_KEY", "TOSS_APP_SECRET"),
    "nhplug": ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_ACCT_NO"),
    # 메리츠: 계좌번호 불필요, MAC은 선택 override라 게이트 제외
    "meritz": ("MERITZ_APP_KEY", "MERITZ_APP_SECRET"),
}


def _get_marked_brokers(item) -> set:
    """테스트에 지정된 브로커 마커 집합을 반환합니다."""
    return {m.name for m in item.iter_markers() if m.name in _REQUIRED_KEYS}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """
    자격증명 기반 자동 skip.

    브로커 마커(kis/kiwoom/ls/toss)를 가진 테스트는 BROKER 환경변수와 무관하게
    해당 브로커의 필수 키가 설정돼 있을 때만 실행됩니다.
    마커가 없는 유닛/모의 테스트는 항상 실행됩니다.
    """
    for item in items:
        marked = _get_marked_brokers(item)
        if not marked:
            continue
        broker = sorted(marked)[0]
        missing = [key for key in _REQUIRED_KEYS[broker] if not os.getenv(key)]
        if missing:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"{broker} 자격증명 미설정: {', '.join(missing)}"
                )
            )


@pytest.fixture(scope="module", autouse=True)
def _force_broker_env(request):
    """
    브로커 마커가 있는 모듈은 자신의 BROKER 환경변수를 강제합니다.

    KIWOOM/TOSS 어댑터가 BROKER_CONFIG(BROKER env 기반)를 읽으므로,
    .env의 BROKER 값과 무관하게 해당 브로커 설정으로 config를 reload해 실행합니다.
    마커가 없는 유닛 테스트 모듈은 건드리지 않습니다.
    """
    broker = next((m for m in _get_marked_brokers(request.node)), None)
    if broker is None:
        yield
        return

    import config

    saved = {key: os.environ.get(key) for key in ("BROKER", "BROKER_MODE")}
    os.environ["BROKER"] = broker
    importlib.reload(config)

    yield

    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    importlib.reload(config)


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
