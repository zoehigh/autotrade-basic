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


# ── 소켓 계층 차단: requests 외 경로(urllib/http.client)와 수집 단계 보호 ──
def test_socket_layer_blocks_urllib_and_raw_sockets():
    if _opted_in():
        pytest.skip("RUN_BROKER_API_TESTS opt-in 상태")
    import socket
    import urllib.request

    with pytest.raises(RuntimeError, match="테스트 차단"):
        urllib.request.urlopen("http://example.invalid/", timeout=1)
    with pytest.raises(RuntimeError, match="테스트 차단"):
        socket.create_connection(("203.0.113.1", 443), timeout=1)
    with pytest.raises(RuntimeError, match="테스트 차단"):
        socket.getaddrinfo("api.example.invalid", 443)


def test_socket_guard_allows_loopback():
    if _opted_in():
        pytest.skip("RUN_BROKER_API_TESTS opt-in 상태")
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=1)
    client.close()
    server.close()


def test_conftest_guard_is_installed_at_import_time():
    """conftest 를 import 하는 것만으로(= 수집 전) 소켓 차단이 설치되는지 새 프로세스에서 확인합니다."""
    if _opted_in():
        pytest.skip("RUN_BROKER_API_TESTS opt-in 상태")
    import os
    import subprocess

    tests_dir = Path(__file__).resolve().parent
    code = (
        "import conftest, urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.invalid/', timeout=1)\n"
        "except RuntimeError as e:\n"
        "    print('BLOCKED' if '테스트 차단' in str(e) else 'OTHER:' + str(e))\n"
        "else:\n"
        "    print('OPEN')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "RUN_BROKER_API_TESTS"}
    env["PYTHONPATH"] = str(tests_dir)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tests_dir, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.strip().endswith("BLOCKED"), result.stdout + result.stderr[-300:]


# ── 저장소 .state.json 보호: 테스트는 기본적으로 임시 state 파일만 씁니다 ──
def test_state_file_is_isolated_from_repo_by_default(tmp_path):
    import state as state_module

    repo_state = (Path(__file__).resolve().parent.parent / ".state.json").resolve()
    assert Path(state_module._STATE_FILE).resolve() != repo_state, "테스트가 저장소 .state.json 을 가리킵니다"
    assert Path(state_module._STATE_FILE).resolve().parent == tmp_path.resolve()


def test_unmocked_save_state_and_save_states_never_touch_repo_state_file():
    import hashlib

    import state as state_module

    repo_state = Path(__file__).resolve().parent.parent / ".state.json"
    before = hashlib.sha256(repo_state.read_bytes()).hexdigest() if repo_state.exists() else None
    record = {"T": 1.0, "last_updated": "", "orders_meta": {}}
    state_module.save_state("TQQQ", record)
    state_module.save_states({"TQQQ": record, "SOXL": record})
    after = hashlib.sha256(repo_state.read_bytes()).hexdigest() if repo_state.exists() else None
    assert before == after


def test_session_fails_when_a_test_writes_the_repo_state_file(tmp_path):
    """conftest 감시가 실제로 종료 코드를 바꾸는지 임시 프로젝트에서 끝까지 확인합니다 (실제 저장소는 건드리지 않음)."""
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parent.parent
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    os.symlink(root / "src", proj / "src")
    shutil.copy(root / "tests" / "conftest.py", proj / "tests" / "conftest.py")
    shutil.copy(root / "pyproject.toml", proj / "pyproject.toml")
    (proj / "tests" / "test_dirty.py").write_text(
        "from pathlib import Path\n"
        "def test_writes_repo_state():\n"
        "    (Path(__file__).resolve().parent.parent / '.state.json').write_text('{}')\n"
    )
    (proj / "tests" / "test_clean.py").write_text("def test_clean():\n    assert True\n")
    env = {k: v for k, v in os.environ.items() if k != "RUN_BROKER_API_TESTS"}

    def run(name):
        return subprocess.run(
            [sys.executable, "-m", "pytest", f"tests/{name}", "-q", "-p", "no:cacheprovider"],
            cwd=proj, env=env, capture_output=True, text=True, timeout=120,
        )

    dirty = run("test_dirty.py")
    assert "1 passed" in dirty.stdout and "테스트 사고" in dirty.stdout
    assert dirty.returncode == 1, "테스트가 통과해도 저장소 state 파일을 건드렸다면 세션은 실패해야 합니다"
    (proj / ".state.json").unlink()
    clean = run("test_clean.py")
    assert clean.returncode == 0 and "테스트 사고" not in clean.stdout
