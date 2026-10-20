"""shadow_runner 휴장일 조기 종료 테스트 (원장·스냅샷 기록 없음 보장)."""
import importlib.util
import os
import sys

_repo_root = os.path.join(os.path.dirname(__file__), "..")
_runner_path = os.path.join(_repo_root, "scripts", "shadow_runner.py")

_spec = importlib.util.spec_from_file_location("shadow_runner", _runner_path)
shadow_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow_runner)


class _FakeBroker:
    def __init__(self, trading_day):
        self._trading_day = trading_day
        self.closed = False

    def is_trading_day(self):
        return self._trading_day

    def close(self):
        self.closed = True


def test_holiday_exits_without_writes(monkeypatch, tmp_path, capsys):
    """휴장일이면 run_shadow_symbol 호출 없이 exit 0, 파일 생성 없음."""
    fake = _FakeBroker(False)
    monkeypatch.setattr(shadow_runner, "create_broker", lambda: fake)
    monkeypatch.setattr(
        sys, "argv",
        ["shadow_runner.py", "--symbol", "TQQQ", "--exchange", "NAS",
         "--snapshot-dir", str(tmp_path)],
    )
    assert shadow_runner.main() == 0
    assert list(tmp_path.iterdir()) == []
    assert "휴장일" in capsys.readouterr().out
    assert fake.closed


def test_settle_phase_no_pendings_exits_zero(monkeypatch, tmp_path, capsys):
    """settle 단계: 대기 의도 없으면 로그 + exit 0 (휴장일 대응).

    settle은 거래일 체크를 하지 않으므로 is_trading_day=False여도 정상 종료합니다.
    """
    fake = _FakeBroker(False)
    fake.get_daily_closes = lambda *a, **k: []
    monkeypatch.setattr(shadow_runner, "create_broker", lambda: fake)
    monkeypatch.setattr(
        sys, "argv",
        ["shadow_runner.py", "--phase", "settle", "--symbol", "TQQQ",
         "--exchange", "NAS", "--snapshot-dir", str(tmp_path)],
    )
    assert shadow_runner.main() == 0
    assert "대기 의도 없음" in capsys.readouterr().out
    assert fake.closed


def _runner_argv(tmp_path, phase="generate"):
    return [
        "shadow_runner.py", "--phase", phase,
        "--symbol", "TQQQ", "--exchange", "NAS",
        "--snapshot-dir", str(tmp_path),
    ]


def _fake_with_raise(monkeypatch, exc):
    """create_broker를 fake로, 해당 phase 실행 함수를 예외 발생으로 교체합니다."""
    fake = _FakeBroker(True)
    monkeypatch.setattr(shadow_runner, "create_broker", lambda: fake)

    def _raise(*args, **kwargs):
        raise exc

    return fake, _raise


def test_version_mismatch_exits_two(monkeypatch, tmp_path, capsys):
    """버전 불일치는 traceback 대신 안내 메시지 + exit 2."""
    monkeypatch.setenv("TQQQ_SEED", "8000")
    fake, raise_func = _fake_with_raise(
        monkeypatch,
        shadow_runner.ShadowVersionMismatchError(
            "저장='v1.3', 현재='v1.4'"
        ),
    )
    monkeypatch.setattr(shadow_runner, "generate_symbol", raise_func)
    monkeypatch.setattr(sys, "argv", _runner_argv(tmp_path))

    assert shadow_runner.main() == 2
    out = capsys.readouterr().out
    assert "가정 버전" in out
    assert "자동 마이그레이션" in out
    assert "archive" in out
    assert fake.closed


def test_corrupt_snapshot_exits_two(monkeypatch, tmp_path, capsys):
    """손상 스냅샷도 안내 메시지 + exit 2 (settle 단계)."""
    monkeypatch.setenv("TQQQ_SEED", "8000")
    fake, raise_func = _fake_with_raise(
        monkeypatch,
        shadow_runner.ShadowSnapshotCorruptError(
            "스냅샷을 읽을 수 없습니다. SHADOW_RESET_ON_CORRUPT=true로 재시작 가능"
        ),
    )
    monkeypatch.setattr(shadow_runner, "settle_symbol", raise_func)
    monkeypatch.setattr(sys, "argv", _runner_argv(tmp_path, phase="settle"))

    assert shadow_runner.main() == 2
    out = capsys.readouterr().out
    assert "스냅샷을 읽을 수 없" in out
    assert "SHADOW_RESET_ON_CORRUPT" in out
    assert fake.closed
