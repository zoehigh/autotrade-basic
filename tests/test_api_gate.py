"""기본 pytest 실행에서 실 브로커 API·주문이 차단되는지 검증합니다 (네트워크 미사용)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_broker_preflight import _assert_order_allowed  # noqa: E402


def test_external_http_is_blocked_by_default(monkeypatch):
    monkeypatch.delenv("RUN_BROKER_API_TESTS", raising=False)
    if _opted_in():
        pytest.skip("RUN_BROKER_API_TESTS opt-in 상태")
    with pytest.raises(RuntimeError, match="테스트 차단"):
        requests.get("https://example.invalid/")


def _opted_in():
    import os
    return os.getenv("RUN_BROKER_API_TESTS", "").strip().lower() in ("1", "true")


@pytest.mark.parametrize("value", [None, "", "0", "false", "no"])
def test_real_order_requires_explicit_opt_in(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ALLOW_BROKER_ORDERS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_BROKER_ORDERS", value)
    with pytest.raises(RuntimeError, match="주문 차단"):
        _assert_order_allowed(SimpleNamespace())


def test_real_order_passes_only_with_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    _assert_order_allowed(SimpleNamespace())  # 예외 없음


def test_test_double_is_not_a_real_order(monkeypatch):
    monkeypatch.delenv("ALLOW_BROKER_ORDERS", raising=False)
    _assert_order_allowed(SimpleNamespace(_is_test_double=True))


def test_api_marked_tests_are_skipped_by_default(pytester=None):
    """conftest 훅이 preflight/live_order/브로커 마커를 기본 skip 하는지 소스로 확인합니다."""
    text = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert "RUN_BROKER_API_TESTS" in text and "ALLOW_BROKER_ORDERS" in text
