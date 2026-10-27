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


# 실 브로커 API를 호출할 수 있는 테스트 마커 — 기본 실행에서는 전부 skip 됩니다.
_API_MARKERS = set(_REQUIRED_KEYS) | {"preflight", "live_order"}


def _env_flag(name: str) -> bool:
    """opt-in 플래그: 정확히 '1'/'true' 일 때만 켜집니다 (.env 값도 동일 규칙)."""
    return os.getenv(name, "").strip().lower() in ("1", "true")


# ── 소켓 계층 차단 (수집·import 단계 포함) ─────────────────────────────
# autouse fixture 는 테스트 함수 실행 시점에만 적용되고 `requests.Session.request` 만 막습니다.
# 수집(collection)·모듈 import 중 호출이나 urllib/http.client/httpx 등 다른 경로는
# 소켓 연결·DNS 조회 단계에서 막아야 합니다. conftest import 시점(= 수집 전)에 설치합니다.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _is_local_address(address) -> bool:
    if isinstance(address, (str, bytes, os.PathLike)):  # AF_UNIX
        return True
    host = address[0] if address else ""
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    return host in _LOCAL_HOSTS or str(host).startswith("127.")


def _install_socket_guard():
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _deny(what, target):
        raise RuntimeError(
            f"[테스트 차단] 외부 네트워크 접근 금지({what}): {target} "
            f"(실 API 테스트는 RUN_BROKER_API_TESTS=1 로 opt-in)"
        )

    def guarded_connect(self, address):
        if not _is_local_address(address):
            _deny("connect", address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_local_address(address):
            _deny("connect_ex", address)
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS and not str(host).startswith("127."):
            _deny("DNS", host)
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo


if not _env_flag("RUN_BROKER_API_TESTS"):
    _install_socket_guard()


# ── 저장소 .state.json 보호 ───────────────────────────────────────────
# `src/state.py` 의 `_STATE_FILE` 은 저장소 루트의 `.state.json` 을 가리킵니다. 테스트가 `save_state`/
# `save_states` 를 mock 하지 않고 실제 구현에 닿으면 이 로컬 state 를 덮어쓰게 됩니다
# (실제로 일어난 사고: 2026-10-27, save_state 만 mock 한 테스트가 새 save_states 경로로 실제 파일을 씀).
# 1) 모든 테스트는 기본적으로 임시 경로를 쓰도록 강제하고 (개별 테스트가 따로 지정하면 그것이 우선),
# 2) 세션 전후 해시를 비교해 저장소 파일이 바뀌었다면 세션을 실패시킵니다.
_REPO_STATE_FILE = Path(__file__).resolve().parent.parent / ".state.json"


def _repo_state_digest():
    import hashlib

    try:
        return hashlib.sha256(_REPO_STATE_FILE.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


_REPO_STATE_DIGEST_AT_START = _repo_state_digest()


@pytest.fixture(autouse=True)
def _isolate_state_file(monkeypatch, tmp_path):
    import state as _state_module

    monkeypatch.setattr(_state_module, "_STATE_FILE", str(tmp_path / ".state.json"))


def pytest_sessionfinish(session, exitstatus):
    if _repo_state_digest() != _REPO_STATE_DIGEST_AT_START:
        print(
            f"\n[테스트 사고] 저장소의 {_REPO_STATE_FILE.name} 이 테스트 도중 변경되었습니다 — "
            "테스트가 실제 state 파일에 썼습니다. 세션을 실패 처리합니다."
        )
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _block_external_http(monkeypatch):
    """기본 실행에서는 외부 HTTP를 원천 차단합니다 (마커 누락 테스트가 실 API를 쳐도 막힘).

    RUN_BROKER_API_TESTS=1 일 때만 해제합니다. 개별 테스트가 requests를 mock 하면
    이 차단보다 나중에 적용되므로 그대로 동작합니다.
    """
    if _env_flag("RUN_BROKER_API_TESTS"):
        return
    import requests

    def _blocked(self, method, url, *args, **kwargs):
        raise RuntimeError(
            f"[테스트 차단] 외부 HTTP 호출 금지: {method} {url} "
            f"(실 API 테스트는 RUN_BROKER_API_TESTS=1 로 opt-in)"
        )

    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)


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
    api_enabled = _env_flag("RUN_BROKER_API_TESTS")
    orders_enabled = api_enabled and _env_flag("ALLOW_BROKER_ORDERS")
    for item in items:
        marker_names = {m.name for m in item.iter_markers()}
        if marker_names & _API_MARKERS and not api_enabled:
            item.add_marker(pytest.mark.skip(
                reason="실 브로커 API 테스트는 RUN_BROKER_API_TESTS=1 일 때만 실행됩니다"
            ))
            continue
        if "live_order" in marker_names and not orders_enabled:
            item.add_marker(pytest.mark.skip(
                reason="주문이 발생하는 테스트는 ALLOW_BROKER_ORDERS=1 이 추가로 필요합니다"
            ))
            continue
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


# ── 실제 백업 재생 테스트 결과 기록 ─────────────────────────────────────
# 외부 백업(STATE_AUDIT_DIR)이 없으면 test_real_state_replay 는 skip 되어 "통과"처럼 보일 수 있습니다.
# 실행 여부·입력 파일 해시·테스트별 결과를 터미널 요약에 항상 남기고,
# REPLAY_REPORT_PATH 가 있으면 JSON 으로도 저장합니다 (파일 내용은 담지 않고 이름+sha256 만 기록).
_REPLAY_MODULE = "test_real_state_replay"
_replay_outcomes: dict = {}


def pytest_runtest_logreport(report):
    if _REPLAY_MODULE not in report.nodeid:
        return
    name = report.nodeid.split("::")[-1]
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        _replay_outcomes[name] = report.outcome


def _replay_input_hashes() -> list:
    import hashlib

    audit = Path(os.getenv("STATE_AUDIT_DIR", str(Path.home() / "state-audit")))
    names = [f"{b}-before.json" for b in ("kis", "kiwoom", "nhplug")]
    names += sorted(p.name for p in audit.glob("settlement-evidence-*.zip")) if audit.is_dir() else []
    rows = []
    for name in names:
        path = audit / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        rows.append({"file": name, "sha256": digest})
    return rows


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _replay_outcomes:
        return  # 이번 실행에 재생 테스트가 선택되지 않음
    executed = any(v == "passed" or v == "failed" for v in _replay_outcomes.values())
    inputs = _replay_input_hashes()
    terminalreporter.write_sep("-", "실제 백업 재생 검증 기록")
    if executed:
        terminalreporter.write_line(
            "실행됨: " + ", ".join(f"{k}={v}" for k, v in sorted(_replay_outcomes.items()))
        )
    else:
        terminalreporter.write_line(
            "⚠️ 건너뜀 — 실제 백업/Evidence 가 없어 72건 차단·16건 반영 재생이 검증되지 않았습니다 "
            "(STATE_AUDIT_DIR 확인). 이 실행의 통과 결과는 재생 검증을 포함하지 않습니다."
        )
    for row in inputs:
        terminalreporter.write_line(f"  입력 {row['file']}: {row['sha256'] or '없음'}")
    report_path = os.getenv("REPLAY_REPORT_PATH", "").strip()
    if report_path:
        import json
        import platform
        import subprocess
        from datetime import datetime, timezone

        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent.parent,
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        record = {
            "executed": executed,
            "outcomes": dict(sorted(_replay_outcomes.items())),
            "inputs": inputs,
            "git_head": head,
            "python": platform.python_version(),
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        Path(report_path).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        terminalreporter.write_line(f"기록 저장: {report_path}")
