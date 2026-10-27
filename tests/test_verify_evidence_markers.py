"""
검증기(ops/verify_run.py) 강화 회귀 테스트 — 마커 보류/명시성/종목별 접수 판정.

다루는 항목
  B1) `pending_marker_odnos`(마커 누락·무효 보류) 주문을 **필수 체결 기대에서 제외**
      하되, 보류 자체는 여전히 "검증 불가"로 실패하고 보류+적용 동시 존재도 실패
  B2) 적용된 주문의 마커가 누락·무효면 실패 (누락을 0 으로 오인해 통과시키지 않음)
  B3) `[실행 결과]` 종목별 라인에서 성공 건수를 읽어 접수 여부를 **종목별**로 판정

검증기는 정산 코드(state.py)를 재사용하지 않습니다 — 독립 재구현 철학 유지.
"""
import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(_ROOT))
sys.path.append(str(_ROOT / "src"))
sys.path.append(str(_ROOT / "ops"))

import verify_run  # noqa: E402


WATERMARK = "2026-10-20T00:00:00+00:00"
AFTER_WATERMARK = "2026-10-21T09:00:00+00:00"


# ───────────────────────────────────────────────────────────────────
# 공통 근거(fixture) — 파일로 쓰지 않고 dict 로 직접 사용합니다
# ───────────────────────────────────────────────────────────────────
def _evidence(**overrides):
    evidence = {
        "symbol": "TQQQ",
        "run_id": "run-1",
        "mode": "LIVE",
        "broker": "meritz",
        "broker_mode": "real",
        "code_version": "abc123",
        "evidence_version": 2,
        "settlement_mode": "incremental",
        "balance_qty": 0,
        "basis_unknown": False,
        "pending_marker_odnos": [],
        "T_before": 5.0,
        "net_invested_before": 500.0,
        "T_after": 5.0,
        "net_invested_after": 500.0,
        "watermark_before": WATERMARK,
        "last_processed_ordno_before": "OLD",
        "orders_meta_before": {},
        "orders_meta_after": {},
        "sell_t_effects_before": {},
        "ledger_before": {"start_T": 5.0, "entries": []},
        "ledger_after": {"start_T": 5.0, "next_seq": 1, "entries": []},
        "orders": [],
        "fills": [],
        "skipped_fills": [],
    }
    evidence.update(overrides)
    return evidence


def _pending_marker_evidence(**overrides):
    """마커 누락 레거시 메타로 정산이 **보류된** 실행의 근거 (orders 비어 있음)."""
    evidence = _evidence(
        pending_marker_odnos=["PF"],
        orders_meta_before={"PF": {"side": "BUY", "total_qty": 10, "t_target": 1.0}},
        fills=[{"odno": "PF", "qty": 10, "amount": 100.0, "ts": AFTER_WATERMARK}],
    )
    evidence.update(overrides)
    return evidence


def _write_evidence(tmp_path, evidence):
    # .gitignore 대상(settlement-*.json)이 아닌 이름을 씁니다.
    path = tmp_path / "evidence_TQQQ_test.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False))
    return path


# ═══════════════════════════════════════════════════════════════════
# B1 — 보류(marker hold) 주문을 필수 체결 기대에서 제외
# ═══════════════════════════════════════════════════════════════════
def test_required_fills_neutralize_pending_marker_odno():
    problems, required, _money = verify_run._required_settlement_fills(
        _pending_marker_evidence()
    )

    # 보류 주문은 "반드시 처리해야 할 체결"에서 빠지므로 불일치 문제가 없어야 합니다.
    assert not any("필수 체결 불일치" in p for p in problems), problems
    item = next(i for i in required if i["odno"] == "PF")
    assert item["req_qty"] == 0
    assert item["req_amount"] == 0.0
    assert item["expects_entry"] is False
    assert item["pending_marker"] is True
    # 항목 자체는 남아 있어야 합니다 (상단 필터링으로 집합을 줄이지 않음).
    assert len(required) == 1


def test_pending_marker_hold_is_still_verification_failure():
    problems = verify_run._verify_evidence(_pending_marker_evidence())

    assert any("보류" in p and "검증 불가" in p for p in problems), problems
    # 제외는 "통과"가 아닙니다 — 다른 불일치로 오인되지도 않아야 합니다.
    assert not any("필수 체결 불일치" in p for p in problems), problems


def test_pending_marker_file_verification_fails(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write_evidence(tmp_path, _pending_marker_evidence()),
        "meritz",
        "LIVE",
        run_id="run-1",
    )
    assert ok is False
    assert any("보류" in p for p in problems), problems


def test_pending_marker_with_applied_delta_is_flagged():
    evidence = _pending_marker_evidence(
        orders=[
            {
                "odno": "PF",
                "side": "매수",
                "qty_delta": 10,
                "amount_delta": 100.0,
                "t_target": 1.0,
                "total_qty": 10,
                "ledger_seq": 1,
                "t_delta": 1.0,
            }
        ],
    )
    problems, _required, _money = verify_run._required_settlement_fills(evidence)

    assert any("보류와 적용이 동시에 존재" in p for p in problems), problems


# ═══════════════════════════════════════════════════════════════════
# B2 — 적용된 주문의 마커 누락·무효는 실패
# ═══════════════════════════════════════════════════════════════════
def _marker_only_evidence(before_meta, after_meta, order=None, fill=None):
    return {
        "orders_meta_before": before_meta,
        "orders_meta_after": after_meta,
        "orders": [order if order is not None else {"odno": "B1"}],
        "fills": [
            fill
            if fill is not None
            else {"odno": "B1", "qty": 10, "amount": 100.0, "ts": AFTER_WATERMARK}
        ],
    }


def test_applied_order_without_marker_fails():
    problems = verify_run._verify_fill_markers(
        _marker_only_evidence(
            {"B1": {"total_qty": 10, "t_target": 1.0}},  # 마커 필드 없음
            {"B1": {"processed_filled_qty": 10, "processed_filled_amount": 100.0}},
        )
    )
    assert any("누락·무효" in p for p in problems), problems


@pytest.mark.parametrize("bad", [None, -1, 2.5, True, "abc", ""])
def test_applied_order_with_invalid_marker_fails(bad):
    problems = verify_run._verify_fill_markers(
        _marker_only_evidence(
            {"B1": {"processed_filled_qty": bad, "processed_filled_amount": 100.0}},
            {"B1": {"processed_filled_qty": 10, "processed_filled_amount": 100.0}},
        )
    )
    assert any("누락·무효" in p for p in problems), problems


def test_applied_order_with_explicit_zero_marker_passes():
    # 명시적 0 은 "아직 미반영"의 정상 상태입니다 (등록 직후 신규 주문).
    problems = verify_run._verify_fill_markers(
        _marker_only_evidence(
            {"B1": {"processed_filled_qty": 0, "processed_filled_amount": 0.0}},
            {"B1": {"processed_filled_qty": 10, "processed_filled_amount": 100.0}},
        )
    )
    assert problems == [], problems


def test_unregistered_buy_is_not_required_to_have_marker():
    # 미등록 매수는 orders_meta 자체가 없습니다 — 오탐하면 안 됩니다.
    problems = verify_run._verify_fill_markers(
        _marker_only_evidence(
            {},
            {"U1": {"processed_filled_qty": 10, "processed_filled_amount": 100.0}},
            order={"odno": "U1", "unregistered": True},
            fill={"odno": "U1", "qty": 10, "amount": 100.0, "ts": AFTER_WATERMARK},
        )
    )
    assert problems == [], problems


def _single_buy_evidence(pre_meta):
    """워터마크 이후 매수 1건 — 마커가 유효하면 통과하는 기준 근거."""
    return _evidence(
        balance_qty=10,
        T_after=6.0,
        net_invested_after=600.0,
        orders_meta_before=pre_meta,
        orders_meta_after={
            "B1": {
                "side": "BUY",
                "total_qty": 10,
                "t_target": 1.0,
                "processed_filled_qty": 10,
                "processed_filled_amount": 100.0,
            }
        },
        orders=[
            {
                "odno": "B1",
                "side": "매수",
                "qty_delta": 10,
                "amount_delta": 100.0,
                "t_target": 1.0,
                "total_qty": 10,
                "ledger_seq": 1,
                "t_delta": 1.0,
            }
        ],
        fills=[{"odno": "B1", "qty": 10, "amount": 100.0, "ts": AFTER_WATERMARK}],
        ledger_after={
            "start_T": 5.0,
            "next_seq": 2,
            "entries": [{"seq": 1, "kind": "buy", "odno": "B1", "delta": 1.0}],
        },
        applied_seq=[1],
    )


def test_clean_post_watermark_buy_still_passes():
    evidence = _single_buy_evidence(
        {"B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                "processed_filled_qty": 0, "processed_filled_amount": 0.0}}
    )
    assert verify_run._verify_evidence(evidence) == []


def test_post_watermark_buy_with_missing_marker_now_fails():
    # 마커 누락을 0 으로 읽던 시절에는 이 근거가 통과했습니다 (이중 가산 통과).
    evidence = _single_buy_evidence(
        {"B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0}}
    )
    problems = verify_run._verify_evidence(evidence)
    assert any("누락·무효" in p for p in problems), problems


def test_pre_watermark_buy_with_missing_marker_still_fails():
    # 워터마크 이전 가산은 기존에도 필수 체결 불일치로 실패했습니다 — 유지 확인.
    evidence = _single_buy_evidence(
        {"B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0}}
    )
    evidence["fills"][0]["ts"] = "2026-10-19T09:00:00+00:00"
    problems = verify_run._verify_evidence(evidence)
    assert problems, "검증 불가/불일치로 실패해야 합니다"
    assert any("필수 체결 불일치" in p or "누락·무효" in p for p in problems), problems


# ═══════════════════════════════════════════════════════════════════
# B3 — 종목별 접수 건수 파싱
# ═══════════════════════════════════════════════════════════════════
RESULT_LOG = """[launcher] start mode=LIVE
  TQQQ: 성공 0건 / 예약 5건 / 실패 0건
  SOXL: 성공 2건 / 예약 0건 / 실패 0건
  합계: 성공 2건 / 예약 5건 / 실패 0건
[launcher] exit=0
"""


def test_symbol_executed_count_reads_exact_symbol_line():
    assert verify_run._symbol_executed_count(RESULT_LOG, "TQQQ") == (True, 0)
    assert verify_run._symbol_executed_count(RESULT_LOG, "SOXL") == (True, 2)


def test_symbol_executed_count_does_not_partially_match():
    # 접두사만 같은 심볼(TQQQ2)이나 합계 라인을 TQQQ 로 오인하면 안 됩니다.
    assert verify_run._symbol_executed_count(RESULT_LOG, "TQQ") == (False, 0)
    text = "  TQQQ2: 성공 3건 / 예약 0건 / 실패 0건\n" + RESULT_LOG
    assert verify_run._symbol_executed_count(text, "TQQQ") == (True, 0)
    only_total = "  합계: 성공 7건 / 예약 0건 / 실패 0건\n"
    assert verify_run._symbol_executed_count(only_total, "TQQQ") == (False, 0)


def _run_main(monkeypatch, tmp_path, log_text, symbols=("TQQQ", "SOXL")):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "run-20260101-000000-mode=LIVE.log").write_text(log_text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        verify_run, "MeritzBroker",
        lambda *a, **k: types.SimpleNamespace(
            get_balance=lambda sym, exch: types.SimpleNamespace(quantity=0),
            get_order_history=lambda sym, exch, days=5: [],
        ),
    )
    monkeypatch.setattr(
        verify_run, "load_state",
        lambda sym: {"T": 0.0, "net_invested": 0.0,
                     "net_invested_status": "valid", "orders_meta": {}},
    )
    monkeypatch.setattr(sys, "argv", ["verify_run.py", "20260101"])
    with pytest.raises(SystemExit):
        verify_run.main()


def test_main_judges_order_acceptance_per_symbol(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, RESULT_LOG)
    out = capsys.readouterr().out

    # 전체 합계(2건)로 판정하면 TQQQ 도 실패로 잡힙니다 — 종목별이어야 합니다.
    assert "SOXL 접수된 주문이 이력에 없습니다" in out
    assert "TQQQ 접수된 주문이 이력에 없습니다" not in out


def test_main_notes_missing_symbol_line_and_treats_as_zero(
    monkeypatch, tmp_path, capsys
):
    log_text = (
        "[launcher] start mode=LIVE\n"
        "  SOXL: 성공 1건 / 예약 0건 / 실패 0건\n"
        "  합계: 성공 1건 / 예약 0건 / 실패 0건\n"
        "[launcher] exit=0\n"
    )
    _run_main(monkeypatch, tmp_path, log_text)
    out = capsys.readouterr().out

    assert "TQQQ 종목별 실행 결과 라인이 없습니다" in out
    assert "TQQQ 접수된 주문이 이력에 없습니다" not in out
    assert "SOXL 접수된 주문이 이력에 없습니다" in out
