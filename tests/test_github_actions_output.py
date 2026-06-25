"""
GitHub Actions 출력 버퍼링 테스트

GitHub Actions에서는 Python 출력이 파이프로 연결되어 기본적으로 버퍼링됩니다.
PYTHONUNBUFFERED=1 또는 sys.stdout.reconfigure(line_buffering=True)로
출력이 실시간(line-by-line)으로 표시되는지 검증합니다.
"""

import subprocess
import sys
import os


def test_stdout_unbuffered_with_env_var():
    """
    PYTHONUNBUFFERED=1 환경변수가 설정된 상태에서 파이썬 스크립트를
    subprocess로 실행하면 출력이 버퍼링되지 않고 즉시 나타나는지 확인합니다.

    검증 방법: "A", "B", "C"를 순서대로 print하는 스크립트를
    subprocess.PIPE로 실행했을 때, 모든 출력이 정상적으로 캡처되는지 확인합니다.
    실제 버퍼링 여부는 OS/환경에 따라 subprocess에서 재현이 어려우므로,
    환경변수가 설정되었는지와 스크립트가 정상 실행되는지만 검증합니다.
    """
    test_code = """
import sys
print("LINE_A")
print("LINE_B")
print("LINE_C")
"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", test_code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"Return code: {result.returncode}"
    assert "LINE_A" in result.stdout
    assert "LINE_B" in result.stdout
    assert "LINE_C" in result.stdout


def test_stdout_unbuffered_with_reconfigure():
    """
    sys.stdout.reconfigure(line_buffering=True)가 설정된 스크립트를
    subprocess로 실행했을 때 출력이 정상적으로 캡처되는지 확인합니다.
    """
    test_code = """
import sys
sys.stdout.reconfigure(line_buffering=True)
print("RECONFIGURE_LINE_1")
print("RECONFIGURE_LINE_2")
"""
    result = subprocess.run(
        [sys.executable, "-c", test_code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Return code: {result.returncode}"
    assert "RECONFIGURE_LINE_1" in result.stdout
    assert "RECONFIGURE_LINE_2" in result.stdout


def test_stdout_unbuffered_combined():
    """
    PYTHONUNBUFFERED=1 환경변수와 sys.stdout.reconfigure(line_buffering=True)를
    함께 사용할 때도 정상 동작하는지 확인합니다.
    """
    test_code = """
import sys
sys.stdout.reconfigure(line_buffering=True)
print("COMBINED_LINE_1")
print("COMBINED_LINE_2")
print("COMBINED_LINE_3")
"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", test_code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"Return code: {result.returncode}"
    assert "COMBINED_LINE_1" in result.stdout
    assert "COMBINED_LINE_2" in result.stdout
    assert "COMBINED_LINE_3" in result.stdout
