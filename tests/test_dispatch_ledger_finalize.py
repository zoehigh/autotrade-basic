"""진단·복구 dispatch와 원장 finalization 회귀 테스트 (모의 전용, 네트워크·주문 없음).

codex 재리뷰에서 발견된 두 회귀를 고정합니다:
- P1: 전용 모드 처리 후 `main()`이 실거래 파이프라인으로 계속 진행하면 안 됨
      (특히 `TRADE_MODE=LIVE` + `STATE_DIAGNOSTIC_ONLY=true`).
- P2: dispatch 지점의 flush가 once-flag을 세우면 종료 시점 flush가 no-op이 됨.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import log_mask  # noqa: E402
import trading_bot  # noqa: E402


@pytest.fixture()
def _clean_ledger():
    """전역 원장·flush 플래그를 저장/복원합니다 (테스트 간 오염 방지)."""
    saved_ledger = dict(log_mask._LEDGER)
    saved_flag = trading_bot._ODNO_LEDGER_FLUSHED
    log_mask._LEDGER.clear()
    trading_bot._ODNO_LEDGER_FLUSHED = False
    try:
        yield
    finally:
        log_mask._LEDGER.clear()
        log_mask._LEDGER.update(saved_ledger)
        trading_bot._ODNO_LEDGER_FLUSHED = saved_flag


def test_diagnostic_handled_exits_before_broker(monkeypatch, tmp_path, _clean_ledger):
    """진단 모드는 브로커 생성 없이 종료하고, 출력한 토큰의 매핑을 남깁니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")  # 가장 위험한 조합
    monkeypatch.setattr(trading_bot, "STATE_DIAGNOSTIC_ONLY", True)
    monkeypatch.setenv("RUN_ID", "unittest")
    monkeypatch.chdir(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("진단 모드에서 브로커를 생성하면 안 됩니다")

    monkeypatch.setattr(trading_bot, "create_broker", _boom)

    def _fake_diagnostic():
        print(f"[상태 진단] order_meta_ids={trading_bot.mask_odno('0000099999')}")

    monkeypatch.setattr(trading_bot, "_print_state_diagnostic", _fake_diagnostic)

    trading_bot.main()  # raise 없이 반환되어야 함

    ledger_file = tmp_path / "odno-map-unittest.jsonl"
    assert ledger_file.exists(), "진단 모드의 마스킹 토큰 매핑이 flush되지 않았습니다"
    assert '"odno_raw": "0000099999"' in ledger_file.read_text(encoding="utf-8")


def test_normal_path_keeps_final_flush_armed(monkeypatch, tmp_path, _clean_ledger):
    """일반 실행은 dispatch를 통과해 브로커 생성까지 가고, once-flag을 세우지 않습니다."""
    for name in (
        "STATE_DIAGNOSTIC_ONLY",
        "STATE_REPAIR_ONLY",
        "STATE_CLEAR_FENCE_ONLY",
        "STATE_REVERSE_AUDIT_ONLY",
        "STATE_REVERSE_RECONCILE_ONLY",
        "STATE_NET_INVESTED_REPAIR_ONLY",
        "STATE_ASSUME_REVERSE_EXPIRY_ONLY",
        "STATE_SET_CYCLE_START_ONLY",
    ):
        monkeypatch.setattr(trading_bot, name, False)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("RUN_ID", "unittest")
    monkeypatch.chdir(tmp_path)

    class _Sentinel(Exception):
        pass

    def _sentinel_broker(*args, **kwargs):
        raise _Sentinel("브로커 생성 지점 도달")

    monkeypatch.setattr(trading_bot, "create_broker", _sentinel_broker)

    with pytest.raises(_Sentinel):
        trading_bot.main()

    assert trading_bot._ODNO_LEDGER_FLUSHED is False, (
        "dispatch 지점의 flush가 once-flag을 세우면 종료 시점 flush가 no-op이 됩니다"
    )
