"""검증기의 원장 start_T 검증 회귀 테스트 (ops/verify_run.py `_recompute_expected_ordered`).

- 정산 전 원장이 완전히 비어 있고 start_T 가 없는 정상 초기 상태 → start_T 문제 없음
- start_T 가 있고 정산 전후 값이 달라짐 → 실패
- start_T 는 없는데 entries 가 있는 비정상 원장 → 실패
- full_reinference 는 start_T=0 요구를 그대로 유지
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(_ROOT))
sys.path.append(str(_ROOT / "ops"))

import verify_run  # noqa: E402

WATERMARK = "2026-11-10T09:30:00+00:00"


def _evidence(**overrides):
    evidence = {
        "symbol": "TQQQ", "run_id": "run-1", "mode": "LIVE", "broker": "kis", "broker_mode": "demo",
        "code_version": "abc123", "evidence_version": 2, "settlement_mode": "incremental",
        "balance_qty": 0, "basis_unknown": False, "pending_marker_odnos": [],
        "T_before": 5.0, "net_invested_before": 500.0, "T_after": 5.0, "net_invested_after": 500.0,
        "watermark_before": WATERMARK, "last_processed_ordno_before": "OLD",
        "orders_meta_before": {}, "orders_meta_after": {}, "sell_t_effects_before": {},
        "ledger_before": {"start_T": 5.0, "entries": []},
        "ledger_after": {"start_T": 5.0, "next_seq": 1, "entries": []},
        "orders": [], "fills": [], "skipped_fills": [],
    }
    evidence.update(overrides)
    return evidence


def _start_t_problems(evidence):
    problems, _ledger, _money = verify_run._recompute_expected_ordered(evidence)
    return [p for p in problems if "start_T" in p]


def test_정산_전_원장이_완전히_비고_start_T가_없는_초기_상태는_통과():
    evidence = _evidence(
        ledger_before={"entries": [], "next_seq": 1},
        ledger_after={"start_T": 5.0, "entries": [], "next_seq": 1},   # 이번 정산이 처음 기준을 확정
    )
    assert _start_t_problems(evidence) == []


def test_start_T가_있고_정산_전후_값이_달라지면_실패():
    evidence = _evidence(
        ledger_before={"start_T": 5.0, "entries": []},
        ledger_after={"start_T": 6.0, "entries": [], "next_seq": 1},
    )
    assert any("정산 중 변경" in p for p in _start_t_problems(evidence))


def test_start_T가_없고_entries가_있는_비정상_원장은_실패():
    entry = {"seq": 1, "kind": "buy", "delta": 1.0}
    evidence = _evidence(
        T_after=6.0,
        ledger_before={"entries": [entry], "next_seq": 2},
        ledger_after={"start_T": 5.0, "entries": [entry], "next_seq": 2},
    )
    assert _start_t_problems(evidence), "entries 가 있는 원장에 start_T 가 없으면 통과하면 안 됩니다"


def test_정산_전후_start_T가_같으면_통과():
    assert _start_t_problems(_evidence()) == []


def test_full_reinference_는_start_T_0을_계속_요구한다():
    evidence = _evidence(
        settlement_mode="full_reinference",
        ledger_before={},
        ledger_after={"start_T": 5.0, "entries": [], "next_seq": 1},
    )
    assert any("start_T 가 0이 아닙니다" in p for p in _start_t_problems(evidence))
