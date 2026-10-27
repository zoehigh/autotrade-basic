"""정산·체결 복구·보류 경로의 주문번호 마스킹 회귀 테스트 (공개 GitHub Actions 로그 보호).

범위(A안): src/state.py, trading_bot.py 의 정산·체결 복구·보류 출력 (stdout / 예외 / 알림).
범위 밖(별도 PR): 일반 주문 접수·취소 로그, 브로커 어댑터 로그 — docs/odno-log-masking-followup.md
Evidence(비공개 암호화 artifact)는 독립 검증을 위해 원문 주문번호를 유지합니다.
"""
import ast
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import trading_bot  # noqa: E402
from state import log_salt_hex, mask_odno  # noqa: E402

RAW_A, RAW_B, RAW_C = "0000039445", "0000039446", "0000039447"


# ── 마스킹 함수 성질 ─────────────────────────────────────────────────
def test_mask_is_stable_within_run_and_hides_raw():
    token = mask_odno(RAW_A)
    assert token == mask_odno(RAW_A)
    assert token.startswith("ord#") and RAW_A not in token
    assert mask_odno([RAW_A, RAW_B]) == f"[{mask_odno(RAW_A)}, {mask_odno(RAW_B)}]"


def test_mask_differs_between_runs():
    """실행(프로세스)마다 salt 가 달라 다른 실행의 로그와 연결·사전 대입이 불가능합니다."""
    code = "import sys; sys.path.insert(0,'src'); from state import mask_odno, log_salt_hex; print(mask_odno('39445'), log_salt_hex())"
    outs = [
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.split()
        for _ in range(2)
    ]
    assert outs[0][0] != outs[1][0]
    assert outs[0][1] != outs[1][1]


def test_salt_is_not_in_mask_output():
    assert log_salt_hex() not in mask_odno(RAW_A)


# ── 보류 경로: stdout / 알림 / 예외 ─────────────────────────────────
def _capture_save_settlement(monkeypatch, state):
    notified = []
    monkeypatch.setattr(trading_bot, "notify", lambda message, urgent=False: notified.append(message))
    return notified


def test_pending_amount_and_marker_hold_masks_everywhere(monkeypatch, capsys):
    notified = _capture_save_settlement(monkeypatch, None)
    state = {
        "_pending_amount_odnos": [RAW_A, RAW_B],
        "_pending_marker_odnos": [RAW_B],
    }
    with pytest.raises(trading_bot.SymbolScopedError) as exc:
        trading_bot._save_settlement_state("TQQQ", state)
    out = capsys.readouterr().out
    for text in (out, str(exc.value), "\n".join(notified)):
        assert RAW_A not in text and RAW_B not in text, text
        assert "ord#" in text


def test_skipped_unverified_log_is_masked(monkeypatch, capsys):
    _capture_save_settlement(monkeypatch, None)
    state = {
        "_unverified_skipped_odnos": [RAW_C],
        "_unverified_skipped_audit": [{"odno": RAW_C, "odno_hash": mask_odno(RAW_C)}],
    }
    assert trading_bot._save_settlement_state("TQQQ", state) is True
    out = capsys.readouterr().out
    assert RAW_C not in out and mask_odno(RAW_C) in out


def test_evidence_keeps_raw_odno_and_salt_for_independent_verification():
    """공개 로그는 마스킹하지만, 비공개 Evidence 는 원문 + salt 를 유지해야 대조할 수 있습니다."""
    state = {
        "T": 1.0, "net_invested": 0.0, "last_updated": "", "orders_meta": {},
        "_unverified_skipped_audit": [
            {"odno": RAW_A, "odno_hash": mask_odno(RAW_A), "filled_qty": 1, "ts": "", "reason": "x"}
        ],
        "_pending_marker_odnos": [RAW_B],
    }
    ev = trading_bot.build_settlement_evidence("TQQQ", deepcopy(state), [], mode="DRY", run_id="r", balance_qty=0)
    assert ev["skipped_unverified"][0]["odno"] == RAW_A
    assert ev["pending_marker_odnos"] == [RAW_B]
    assert ev["log_salt"] == log_salt_hex()
    assert mask_odno(RAW_A, ev["log_salt"]) == ev["skipped_unverified"][0]["odno_hash"]


# ── 정적 가드: 새 출력이 원문 주문번호를 찍지 않도록 ───────────────────
_SINKS = {"print", "notify", "RuntimeError", "SymbolScopedError", "GlobalScopedError", "ValueError", "Exception"}
# 범위 밖(별도 PR) 출력 — 줄 내용의 일부로 식별합니다 (줄 번호는 바뀌므로 쓰지 않음)
_OUT_OF_SCOPE = (
    "[orders_meta 등록]",      # 일반 주문 접수 등록
    "취소 요청 실패",           # 이전 세션 잔여 주문 취소
    "  - {odno} (",            # 취소 대상 목록
)


def _names_odno(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and ("odno" in n.id.lower() or "ordno" in n.id.lower()):
            return True
        if isinstance(n, ast.Attribute) and ("odno" in n.attr.lower() or "ordno" in n.attr.lower()):
            return True
        if isinstance(n, ast.Constant) and n.value == "odno":
            return True
    return False


def _is_wrappable_leak(value):
    if isinstance(value, ast.Call):
        f = value.func
        if getattr(f, "id", "") == "mask_odno":
            return False
        if isinstance(f, ast.Name) and f.id == "len":
            return False
        return (isinstance(f, ast.Attribute) and f.attr == "get") or (isinstance(f, ast.Name) and f.id == "str")
    return isinstance(value, (ast.Name, ast.Attribute, ast.Subscript))


@pytest.mark.parametrize("relpath", ["src/state.py", "trading_bot.py"])
def test_no_raw_odno_in_settlement_outputs(relpath):
    source = (ROOT / relpath).read_text(encoding="utf-8")
    lines = source.splitlines()
    leaks = []

    def check(call_args, lineno):
        for arg in call_args:
            for n in ast.walk(arg):
                if isinstance(n, ast.FormattedValue) and _names_odno(n.value) and _is_wrappable_leak(n.value):
                    text = " ".join(lines[lineno - 1: lineno + 3])
                    if not any(marker in text for marker in _OUT_OF_SCOPE):
                        leaks.append(f"{relpath}:{lineno}: {lines[lineno - 1].strip()[:80]}")
                    return

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in _SINKS:
                check(node.args, node.lineno)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            check(node.exc.args, node.lineno)
    assert not leaks, "원문 주문번호 출력 발견 (mask_odno 사용):\n" + "\n".join(leaks)
