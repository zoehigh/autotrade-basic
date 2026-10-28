"""마스킹이 **동작(주문/정산/state 저장)** 을 바꾸지 않았음을 고정하는 회귀 테스트 (mock 전용).

마스킹은 공개 로그 **표시만** 바꿉니다. 저장 키·정산 근거·state 저장 여부는 그대로여야 합니다.
DRY/LIVE 주문·정산·state 저장 경로의 전체 동작은 기존 스위트가 이미 덮고 있습니다:
  - `tests/test_dry_mismatch.py`, `tests/test_dryrun.py` — DRY 미리보기/불일치 처리
  - `tests/test_settlement_*.py` (ordered_ledger, cycle_basis, robustness, oracle_fixes)
  - `tests/test_cache_save_guard.py` — 모드별 state 캐시 저장 게이팅
  - `tests/test_repair_mode.py`, `tests/test_force_t_mode_guard.py` — REPAIR/FORCE_T 게이트
여기서는 **마스킹이 끼어든 지점의 불변식**만 추가로 확인합니다.
"""
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import log_mask  # noqa: E402
import notifier  # noqa: E402
import state as state_module  # noqa: E402
import trading_bot  # noqa: E402

RAW = "0000039445"


def _clean_state():
    return {
        "T": 1.0,
        "net_invested": 100.0,
        "net_invested_status": "valid",
        "last_updated": "",
        "orders_meta": {},
        "_unverified_skipped_audit": [
            {"odno": RAW, "odno_hash": log_mask.mask_odno(RAW), "filled_qty": 1, "ts": "", "reason": "x"}
        ],
    }


def test_register_order_meta_keeps_raw_state_key(capsys):
    """마스킹은 출력만 바꿉니다 — `orders_meta` 키는 원문 주문번호여야 합니다."""
    state = _clean_state()
    state_module.register_order_meta_in_state(
        state, RAW, {"side": "BUY", "total_qty": 1, "t_target": 0.5, "is_additional": False}
    )
    out = capsys.readouterr().out

    assert RAW in state["orders_meta"], "state 저장 키가 마스킹되면 정산·취소가 깨집니다"
    assert log_mask.mask_odno(RAW) not in state["orders_meta"]
    assert RAW not in out and log_mask.mask_odno(RAW) in out


@pytest.mark.parametrize("mode", ["DRY", "LIVE"])
def test_settlement_evidence_keeps_raw_odno_and_salt(mode):
    """DRY/LIVE 모두 Evidence(비공개)에는 원문·salt 가 남아 독립 검증이 가능합니다."""
    state = _clean_state()
    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", deepcopy(state), [], mode=mode, run_id="r1", balance_qty=0
    )
    assert evidence["mode"] == mode
    assert evidence["skipped_unverified"][0]["odno"] == RAW
    assert evidence["log_salt"] == log_mask.log_salt_hex()


def test_dry_and_live_evidence_differ_only_in_mode():
    """모드가 정산 근거의 주문번호·salt 를 바꾸지 않습니다 (표시/게이팅만 다름)."""
    state = _clean_state()
    dry = trading_bot.build_settlement_evidence("TQQQ", deepcopy(state), [], mode="DRY", run_id="r1")
    live = trading_bot.build_settlement_evidence("TQQQ", deepcopy(state), [], mode="LIVE", run_id="r1")
    assert {key for key in dry if dry[key] != live[key]} <= {"mode"}


def test_masking_helpers_are_pure(monkeypatch):
    """마스킹 도우미는 state·환경변수를 건드리지 않습니다 (부작용 없음)."""
    monkeypatch.setenv("TRADE_MODE", "DRY")
    state = _clean_state()
    before = deepcopy(state)

    trading_bot._mask_odno_references(f"odno={RAW} 주문번호: {RAW}")
    trading_bot._safe_error_text(RuntimeError(f"실패 odno={RAW}"))
    trading_bot._mask_token_in(f"주문번호: {RAW}", RAW)

    assert state == before
    assert os.environ["TRADE_MODE"] == "DRY"
    assert RAW not in trading_bot._mask_odno_references(f"odno={RAW}")


def test_masking_does_not_touch_state_file(monkeypatch, tmp_path):
    """마스킹 호출이 state 저장을 유발하지 않습니다 (저장은 모드 게이트가 결정)."""
    target = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "_STATE_FILE", str(target))

    trading_bot._mask_odno_references(f"odno={RAW}")
    log_mask.mask_odno(RAW)

    assert not target.exists(), "마스킹이 state 파일을 만들었습니다"


@pytest.mark.parametrize("trade_mode", ["DRY", "LIVE"])
def test_personal_channel_choice_is_mode_independent(monkeypatch, trade_mode):
    """알림 채널 선택은 TRADE_MODE 와 무관합니다 (개인 채팅 = 원문, 그 외 = 마스킹).

    DRY/LIVE 어느 쪽이든 같은 규칙이어야 실행 모드에 따라 주문번호 노출이 달라지지 않습니다.
    """
    captured = []
    monkeypatch.setattr(notifier, "send_telegram", lambda text: captured.append(text) or True)
    monkeypatch.setenv("TRADE_MODE", trade_mode)
    monkeypatch.setenv("QUIET_HOURS", "false")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", "777")

    message = f"주문번호: {log_mask.mask_odno(RAW)}"
    telegram_message = f"주문번호: {RAW}"
    notifier.notify(message, telegram_message=telegram_message)

    assert captured == [telegram_message]
    assert trading_bot.notify is notifier.notify, (
        "trading_bot 은 notifier.notify 를 그대로 써야 채널 규칙이 한 곳에 유지됩니다"
    )
