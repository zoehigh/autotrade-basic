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
