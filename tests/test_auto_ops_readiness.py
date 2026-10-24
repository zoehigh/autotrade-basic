"""
취소 확정 확인 · 리버스 1.19 캡 · 실행 결과 집계 테스트.

배경 (2026-10-20 검토):
- 자동 운영 전제리로 세 가지를 보완합니다.
  ① 취소 요청 접수만으로 취소 완료로 간주하던 문제 → 이력 재조회로 확정 확인
  ② 리버스 쿼터매수에 1.19 상한이 누락되던 문제
  ③ 종목 실패 시에도 exit 0 으로 끝나던 문제 → 결과 집계 + 종료코드
"""
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_bot
from trading_bot import SymbolScopedError, _cancel_open_bot_orders


def _row(odno, status="미체결", remaining="18", side="매도", ord_dt=None, filled="0"):
    return {
        "odno": odno,
        "prcs_stat_name": status,
        "nccs_qty": remaining,
        "ft_ccld_qty": filled,
        "sll_buy_dvsn_cd_name": side,
        "ord_dt": ord_dt or trading_bot.get_kst_now().strftime("%Y%m%d"),
    }


class _Broker:
    """취소 후 이력 재조회를 시뮬레이션하는 브로커."""

    def __init__(self, after_cancel_rows=None, cancel_error=None):
        self.calls = []
        self.after_cancel_rows = after_cancel_rows
        self.cancel_error = cancel_error
        self.history_calls = 0

    def cancel_order(self, odno, symbol=None, exchange=None):
        self.calls.append(odno)
        if self.cancel_error:
            raise RuntimeError(self.cancel_error)
        return None

    def get_order_history(self, symbol, exchange, days=30, verbose=False):
        self.history_calls += 1
        if self.after_cancel_rows is not None:
            return list(self.after_cancel_rows)
        return []


# ═══════════════════════════════════════════════════════════════════
# ① 취소 확정 확인
# ═══════════════════════════════════════════════════════════════════


def test_cancel_confirmed_when_history_shows_cancelled(monkeypatch):
    """취소 후 이력에서 취소 상태가 되면 확정한 것으로 봅니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="취소", remaining="0")])
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1")]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 1
    assert broker.calls == ["OD1"]
    assert broker.history_calls >= 1


def test_cancel_confirmed_when_remaining_is_zero(monkeypatch):
    """상태 표기가 남아 있어도 잔량이 0이면 확정입니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="0")])
    state = {"orders_meta": {"OD1": {}}}

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    assert count == 1


def test_cancel_unconfirmed_raises_and_blocks_new_orders(monkeypatch):
    """취소 후에도 잔량이 남아 있으면 신규 주문을 내지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="18")])
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])


def test_fill_during_cancel_raises(monkeypatch):
    """취소 도중 체결이 발생하면 포지션이 바뀌므로 신규 주문을 내지 않습니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(
        after_cancel_rows=[
            _row("OD1", status="부분체결", remaining="10", filled="8"),
        ]
    )
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])


def test_previous_session_orders_are_not_cancelled(monkeypatch):
    """이전 세션 주문은 만료 대상이므로 취소하지 않습니다 (당일 주문 규칙)."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[])
    yesterday = "20260101"
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", ord_dt=yesterday)]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 0
    assert broker.calls == []
    assert broker.history_calls == 0


def test_cancel_rejection_leaves_order_open_and_blocks(monkeypatch):
    """취소 요청이 거부되고 주문이 여전히 열려 있으면 중단합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker(after_cancel_rows=[_row("OD1", status="미체결", remaining="18")],
                     cancel_error="취소 거부")
    state = {"orders_meta": {"OD1": {}}}

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    # 실패해도 취소 시도는 이루어집니다
    assert broker.calls == ["OD1"]


def test_retry_confirms_after_history_lag(monkeypatch):
    """이력 반영이 늦으면 재시도로 확인할 수 있어야 합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _Broker()
    state = {"orders_meta": {"OD1": {}}}

    rows = [
        [_row("OD1", status="미체결", remaining="18")],   # 1차: 아직 반영 안 됨
        [_row("OD1", status="미체결", remaining="18")],   # 2차: 아직
        [_row("OD1", status="취소", remaining="0")],      # 3차: 반영됨
    ]
    calls = {"n": 0}

    def _history(_symbol, _exchange, days=30, verbose=False):
        broker.history_calls += 1
        index = min(calls["n"], len(rows) - 1)
        calls["n"] += 1
        return list(rows[index])

    broker.get_order_history = _history

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])

    assert count == 1
    assert broker.history_calls == 3


# ═══════════════════════════════════════════════════════════════════
# ② 리버스 쿼터매수 1.19 캡
# ═══════════════════════════════════════════════════════════════════


def test_reverse_buy_applies_price_cap(monkeypatch):
    """리버스 쿼터매수도 현재가 ×1.19 상한을 적용합니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import execute_reverse_mode

    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "day_count": 1},
        "close_prices": [100.0, 100.0, 100.0, 100.0, 100.0],
    }

    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL",
        position_qty=100, avg_price=100.0, orderable_cash=1000.0,
        last_price=50.0, state=state,
    )

    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    assert len(buys) == 1
    # 별지점 $100 - 0.01 = $99.99 → 상한 50 × 1.19 = $59.50
    assert buys[0]["price"] == 59.5


def test_reverse_buy_without_cap_keeps_star_price(monkeypatch):
    """상한에 걸리지 않으면 별지점 가격을 그대로 유지합니다."""
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    from strategy import execute_reverse_mode

    state = {
        "T": 20.0,
        "reverse_mode": {"active": True, "day_count": 1},
        "close_prices": [50.0, 50.0, 50.0, 50.0, 50.0],
    }

    result = execute_reverse_mode(
        None, "SOXL", "AMS", 20, "SOXL",
        position_qty=100, avg_price=100.0, orderable_cash=1000.0,
        last_price=45.0, state=state,
    )

    buys = [o for o in result["orders"] if o["side"] == "BUY"]
    assert len(buys) == 1
    # 별지점 50 - 0.01 = 49.99, 상한 45 × 1.19 = 53.55 → 캡 없음
    assert buys[0]["price"] == 49.99


# ═══════════════════════════════════════════════════════════════════
# P1-2: evidence 파일 검증 단일 진입점 (계좌 API 호출 없음)
# ═══════════════════════════════════════════════════════════════════
import json
import sys as _sys
from pathlib import Path as _Path

_sys.path.append(str(_Path(__file__).resolve().parents[1] / "ops"))
import verify_run  # noqa: E402


def _evidence(**overrides):
    evidence = {
        "symbol": "TQQQ",
        "run_id": "run-1",
        "retry_num": "0",
        "mode": "LIVE",
        "broker": "meritz",
        "broker_mode": "real",
        "code_version": "abc123",
        "max_t": None,
        "evidence_version": 2,
        "settlement_mode": "full_reinference",
        "balance_qty": 0,
        "basis_unknown": False,
        "hold_reason": "",
        "T_before": 0.0,
        "net_invested_before": 0.0,
        "T_after": 0.0,
        "net_invested_after": 0.0,
        "watermark_before": "",
        "ledger_before": {},
        "ledger_after": {"start_T": 0.0, "next_seq": 1, "entries": []},
        "orders_meta_before": {},
        "orders_meta_after": {},
        "orders": [],
        "fills": [],
        "skipped_fills": [],
        "last_updated": "",
        "last_processed_ordno_before": "",
        "sell_t_effects": {},
        "sell_t_effects_before": {},
    }
    evidence.update(overrides)
    return evidence


def _write(tmp_path, evidence, name="settlement-TQQQ-test.json"):
    path = tmp_path / name
    path.write_text(json.dumps(evidence))
    return path


def test_verify_evidence_file_passes_clean_evidence(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "meritz", "LIVE", run_id="run-1"
    )
    assert ok is True, problems
    assert problems == []


def test_verify_evidence_file_fails_when_missing(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        tmp_path / "missing.json", "meritz", "LIVE"
    )
    assert ok is False
    assert any("없음" in p for p in problems)


def test_verify_evidence_file_detects_tamper(tmp_path):
    evidence = _evidence(T_after=1.0)
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, evidence), "meritz", "LIVE"
    )
    assert ok is False
    assert any("T 불일치" in p for p in problems)


def test_verify_evidence_file_detects_run_id_mismatch(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "meritz", "LIVE", run_id="run-2"
    )
    assert ok is False
    assert any("run_id 불일치" in p for p in problems)


def test_verify_evidence_file_detects_broker_or_mode_mismatch(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "kis", "DRY"
    )
    assert ok is False
    assert any("broker 불일치" in p for p in problems)
    assert any("mode 불일치" in p for p in problems)


def test_verify_evidence_file_makes_no_network_calls(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("검증기가 브로커/네트워크를 호출했습니다")

    monkeypatch.setattr(verify_run, "MeritzBroker", _boom)
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "meritz", "LIVE"
    )
    assert ok is True, problems


def test_hold_evidence_has_reason_and_is_not_saved(monkeypatch):
    import trading_bot
    from state import _mark_settlement_hold

    state = {"T": 10.0, "net_invested": 1000.0, "orders_meta": {}}
    _mark_settlement_hold(state, "금액 미확정")
    state["pending_order_intent"] = {"id": "I"}
    state["pending_order_batch"] = {"id": "B"}
    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", state, [], mode="LIVE", run_id="run-1"
    )
    assert evidence["basis_unknown"] is True
    assert evidence["hold_reason"] == "금액 미확정"

    monkeypatch.setattr(trading_bot, "save_state", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("hold 상태에서 save_state 호출")
    ))
    with pytest.raises(SymbolScopedError):
        trading_bot._save_settlement_state("TQQQ", state)
    # fence 는 보존됩니다 (자동 재제출 금지).
    assert state["pending_order_intent"] == {"id": "I"}
    assert state["pending_order_batch"] == {"id": "B"}


def test_verify_evidence_file_fails_on_missing_metadata(tmp_path):
    evidence = _evidence()
    evidence.pop("broker")
    evidence.pop("mode")
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, evidence), "meritz", "LIVE"
    )
    assert ok is False
    assert any("broker 필드" in p for p in problems)
    assert any("mode 필드" in p for p in problems)


# ── P1-2 보완: 실제 모드/실패 후 검증/최종값 대조 ────────────────────
def test_verify_evidence_file_detects_missing_code_version(tmp_path):
    evidence = _evidence()
    evidence.pop("code_version")
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, evidence), "meritz", "LIVE",
        code_version="abc123",
    )
    assert ok is False
    assert any("code_version" in p for p in problems)


def test_verify_evidence_file_detects_broker_mode_and_retry_mismatch(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "meritz", "LIVE",
        broker_mode="demo", retry_num="1", code_version="abc123",
    )
    assert ok is False
    assert any("broker_mode 불일치" in p for p in problems)
    assert any("retry_num 불일치" in p for p in problems)


def test_verify_evidence_file_compares_final_state(tmp_path):
    evidence = _evidence(T_after=2.0, net_invested_after=200.0)
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, evidence), "meritz", "LIVE",
        final_state={"T": 1.0, "net_invested": 200.0},
    )
    assert ok is False
    assert any("최종 저장 T 불일치" in p for p in problems)


def test_verify_evidence_file_allows_max_t_correction(tmp_path):
    evidence = _evidence(
        T_before=25.0, net_invested_before=200.0,
        T_after=25.0, net_invested_after=200.0, max_t=20.0,
        settlement_mode="incremental",
        ledger_before={},
        ledger_after={"start_T": 25.0, "next_seq": 1, "entries": []},
    )
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, evidence), "meritz", "LIVE",
        final_state={"T": 20.0, "net_invested": 200.0},
    )
    assert ok is True, problems


def test_write_final_snapshot_and_dir_completeness(tmp_path):
    state_file = tmp_path / ".state.json"
    state_file.write_text(json.dumps({
        "TQQQ": {"T": 1.0, "net_invested": 100.0, "last_updated": "u1",
                 "cycle_start_date": "2026-09-01", "position_qty": 10},
        "SOXL": {"T": 0.0, "net_invested": 0.0, "last_updated": "u2",
                 "cycle_start_date": "", "position_qty": 0},
    }))
    snap_path = tmp_path / "final-settlement-run-1.json"
    verify_run.write_final_snapshot(
        snap_path, state_file, broker="meritz", broker_mode="real",
        trade_mode="LIVE", run_id="run-1", retry_num="0",
        code_version="abc123", symbols=["TQQQ:NAS", "SOXL:AMS"],
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _write(evidence_dir, _evidence())  # TQQQ 만 있음

    ok, problems = verify_run.verify_evidence_dir(
        evidence_dir, "meritz", "LIVE", run_id="run-1",
        retry_num="0", code_version="abc123", broker_mode="real",
        snapshot_path=snap_path,
    )
    assert ok is False
    assert any("종목별 evidence 누락" in p and "SOXL" in p for p in problems)


def test_verify_evidence_dir_passes_with_snapshot_and_skips_dry_final_state(tmp_path):
    state_file = tmp_path / ".state.json"
    state_file.write_text(json.dumps({
        "TQQQ": {"T": 99.0, "net_invested": 999.0},
    }))
    snap_path = tmp_path / "final-settlement-run-1.json"
    verify_run.write_final_snapshot(
        snap_path, state_file, broker="meritz", broker_mode="demo",
        trade_mode="DRY", run_id="run-1", retry_num="0",
        code_version="abc123", symbols=["TQQQ"],
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _write(evidence_dir, _evidence(mode="DRY", broker_mode="demo"))

    ok, problems = verify_run.verify_evidence_dir(
        evidence_dir, "meritz", "DRY", run_id="run-1",
        retry_num="0", code_version="abc123", broker_mode="demo",
        snapshot_path=snap_path,
    )
    assert ok is True, problems


def test_verify_evidence_dir_fails_without_snapshot(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _write(evidence_dir, _evidence())
    ok, problems = verify_run.verify_evidence_dir(
        evidence_dir, "meritz", "LIVE", run_id="run-1"
    )
    assert ok is False
    assert any("최종 저장 스냅샷이 없습니다" in p for p in problems)


def test_verify_evidence_file_fails_without_actual_mode(tmp_path):
    ok, problems = verify_run.verify_evidence_file(
        _write(tmp_path, _evidence()), "meritz", ""
    )
    assert ok is False
    assert any("실행 모드가 전달되지 않았습니다" in p for p in problems)


def test_verify_evidence_dir_uses_latest_retry_file_per_symbol(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    # 이전 회차는 일부러 최종값과 불일치 — 최신 파일만 대조되는지 확인합니다.
    _write(
        evidence_dir,
        _evidence(T_after=99.0),
        name="settlement-TQQQ-20260101T000000Z.json",
    )
    _write(
        evidence_dir,
        _evidence(T_after=0.0),
        name="settlement-TQQQ-20260102T000000Z.json",
    )
    state_file = tmp_path / ".state.json"
    state_file.write_text(json.dumps({"TQQQ": {"T": 0.0, "net_invested": 0.0}}))
    verify_run.write_final_snapshot(
        evidence_dir / "final-settlement-run-1.json",
        state_file, broker="meritz", broker_mode="real", trade_mode="LIVE",
        run_id="run-1", retry_num="0", code_version="abc123", symbols=["TQQQ"],
    )
    ok, problems = verify_run.verify_evidence_dir(
        evidence_dir, "meritz", "LIVE", run_id="run-1",
        retry_num="0", code_version="abc123", broker_mode="real",
    )
    assert ok is True, problems


def test_verify_evidence_dir_fails_without_any_evidence(tmp_path):
    ok, problems = verify_run.verify_evidence_dir(
        tmp_path, "meritz", "LIVE", run_id="run-1"
    )
    assert ok is False
    assert any("정산 근거 파일이 없습니다" in p for p in problems)
