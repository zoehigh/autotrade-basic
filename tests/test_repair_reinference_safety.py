"""REPAIR 재추정(`_repair_reinference`)의 저장 안전성 — 실제 load_state/save_states + 임시 state 파일.

검증하는 것
  1) 정산 보류 표식(`_settlement_hold`, `_pending_amount_odnos`, `_pending_marker_odnos`,
     `_position_basis_unknown`)이 하나라도 있으면 어떤 종목도 저장하지 않고 종료 코드 1.
     원래 last_updated / orders_meta / T / net_invested / settlement_ledger 가 파일에서 그대로.
  2) 다종목은 전부 검증한 뒤 `.state.json` 을 **한 번만(원자적으로)** 저장.
     검증 실패(종료 코드 1)와 파일 저장 실패(종료 코드 2)를 구분하고, 실패 시 원본 파일은 바이트 단위로 그대로.

배경(왜 위험한가): REPAIR 재추정은 `last_updated` 를 비운 **뒤** `update_T_from_history` 를 호출합니다.
보류가 되면 래퍼가 되돌리는 스냅샷이 이미 `last_updated=""` 이므로, 보류 상태를 그대로 저장하면
워터마크가 빈 값으로 바뀌어 다음 실행이 전체 재스캔(이중 가산 위험)으로 들어갑니다.
"""
import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as runtime_config
import state as state_mod
import trading_bot
from broker.base import Balance

WATERMARK = "2026-10-21T19:11:03+00:00"
NEW_WATERMARK = "2026-10-27T00:00:00+00:00"
RAW_A, RAW_B = "0000039445", "0000039446"


def _record(t, net, **extra):
    rec = {
        "T": t, "last_updated": WATERMARK, "cycle_start_date": "2026-09-01", "effective_seed": 0.0,
        "net_invested": net, "net_invested_status": "valid", "last_processed_ordno": "39000",
        "additional_loc_odno": [], "balance_mismatch": {}, "state_version": "v2", "close_prices": [],
        "reverse_mode": {}, "pending_order_intent": None, "pending_order_batch": None,
        "position_qty": 10,
        "orders_meta": {"A1": {"side": "BUY", "total_qty": 5, "processed_filled_qty": 5,
                               "processed_filled_amount": 250.0, "fill_marker_version": 1}},
        "sell_t_effects": {},
        "settlement_ledger": {"start_T": 0.0, "next_seq": 2,
                              "entries": [{"seq": 1, "kind": "buy", "odno": "A1", "delta": 7.0}]},
    }
    rec.update(extra)
    return rec


def _config(symbol, max_t=20.0):
    return {"symbol": symbol, "exchange": "NAS", "splits": 20, "symbol_type": symbol, "seed": 8000.0,
            "additional_loc_levels": 3, "max_t": max_t}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def repair(monkeypatch, capsys):
    """임시 .state.json 에 두 종목(+ 보존 확인용 세 번째 종목)을 쓰고 REPAIR 재추정 환경을 준비합니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "")
    for flag in ("STATE_DIAGNOSTIC_ONLY", "STATE_REPAIR_ONLY", "STATE_CLEAR_FENCE_ONLY",
                 "STATE_REVERSE_AUDIT_ONLY", "STATE_REVERSE_RECONCILE_ONLY",
                 "STATE_NET_INVESTED_REPAIR_ONLY", "STATE_ASSUME_REVERSE_EXPIRY_ONLY",
                 "STATE_SET_CYCLE_START_ONLY"):
        monkeypatch.setattr(trading_bot, flag, False)
    monkeypatch.setattr(trading_bot, "notify", lambda *a, **k: None)

    original = {"TQQQ": _record(7.0, 1234.56), "SOXL": _record(9.0, 4321.09), "QLD": _record(3.0, 99.0)}
    for sym, rec in original.items():
        state_mod.save_state(sym, rec)
    capsys.readouterr()
    broker = MagicMock()
    broker.get_order_history.return_value = []
    broker.get_balance.return_value = Balance(quantity=10, avg_price=50.0)
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config("TQQQ"), _config("SOXL")])

    def impl(plan):
        """_update_T_from_history_impl 대역: 종목별 계획({symbol: callable(state) -> state})을 적용."""
        def _impl(symbol, state, order_history, balance_qty=None):
            return plan[symbol](state)
        monkeypatch.setattr(state_mod, "_update_T_from_history_impl", _impl)

    return {"impl": impl, "broker": broker, "file": state_mod._STATE_FILE, "original": original}


def _ok(t):
    def build(state):
        state.update({"T": t, "last_updated": NEW_WATERMARK})
        return state
    return build


def _hold(kind):
    def build(state):
        # T 는 MAX_T(20) 이내의 유효 값으로 둡니다 — 그래야 거부 사유가 "T 상한"이 아니라 오직 "보류 표식"입니다.
        # (_settlement_hold 인 경우 래퍼가 스냅샷으로 되돌리므로 최종 T 는 원래 값 7.0 입니다)
        state["T"] = 12.0
        if kind == "settlement_hold":
            state["_settlement_hold"] = True
            state["_settlement_hold_reason"] = "적용 순서 근거 부족"
        elif kind == "pending_amount":
            state["_pending_amount_odnos"] = [RAW_A, RAW_B]
        elif kind == "pending_marker":
            state["_pending_marker_odnos"] = [RAW_A]
        elif kind == "position_basis_unknown":
            state["_position_basis_unknown"] = True
            state["_position_basis_unknown_reason"] = "잔고조회실패"
        return state
    return build


def _assert_file_untouched(repair, before_sha):
    assert _sha(repair["file"]) == before_sha, "상태 파일이 바이트 단위로 같아야 합니다"
    loaded = {sym: state_mod.load_state(sym) for sym in ("TQQQ", "SOXL", "QLD")}
    for sym, orig in repair["original"].items():
        for key in ("last_updated", "orders_meta", "T", "net_invested", "settlement_ledger"):
            assert loaded[sym][key] == orig[key], f"{sym}.{key} 가 변경됐습니다"
    leftovers = [p.name for p in Path(repair["file"]).parent.iterdir() if p.name.startswith(".state.") and p.name.endswith(".tmp")]
    assert leftovers == [], f"임시 파일이 남았습니다: {leftovers}"


# ═══════════════════════════════════════════════════════════════════
# 1) 정산 보류 → 저장 거부, 원본 불변
# ═══════════════════════════════════════════════════════════════════
def test_wrapper_hold_restore_leaves_last_updated_blank_which_is_why_saving_is_unsafe(repair):
    """위험의 증명: 보류 후 되돌린 state 는 last_updated 가 빈 값입니다 (저장하면 워터마크가 지워짐)."""
    repair["impl"]({"TQQQ": _hold("settlement_hold")})
    state = state_mod.load_state("TQQQ")
    state["last_updated"] = ""  # _repair_reinference 가 하는 일
    result = state_mod.update_T_from_history("TQQQ", state, [], balance_qty=10)
    assert result["_settlement_hold"] is True
    assert result["last_updated"] == "", "보류 복원 결과의 워터마크가 비어 있습니다"
    assert result["T"] == 7.0, "T 는 스냅샷으로 되돌아가야 합니다"


@pytest.mark.parametrize("kind", ["settlement_hold", "pending_amount", "pending_marker", "position_basis_unknown"])
def test_hold_markers_block_all_saves_and_leave_file_untouched(repair, kind, capsys):
    repair["impl"]({"TQQQ": _hold(kind), "SOXL": _ok(11.0)})
    before = _sha(repair["file"])
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "검증 실패" in out and "어떤 종목도 저장하지 않습니다" in out
    assert RAW_A not in out and RAW_B not in out, "원문 주문번호가 출력됐습니다"
    _assert_file_untouched(repair, before)
    repair["broker"].place_order.assert_not_called()


def test_hold_on_second_symbol_does_not_save_the_healthy_first_symbol(repair):
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _hold("settlement_hold")})
    before = _sha(repair["file"])
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    _assert_file_untouched(repair, before)
    assert state_mod.load_state("TQQQ")["T"] == 7.0, "정상 종목도 먼저 저장되면 안 됩니다"


def test_hold_reasons_helper_reports_all_markers_masked():
    state = {"_settlement_hold": True, "_settlement_hold_reason": "근거 부족",
             "_pending_amount_odnos": [RAW_A], "_pending_marker_odnos": [RAW_B],
             "_position_basis_unknown": True, "_position_basis_unknown_reason": "잔고조회실패"}
    reasons = trading_bot._reinference_hold_reasons("TQQQ", state)
    assert len(reasons) == 4
    joined = "\n".join(reasons)
    assert RAW_A not in joined and RAW_B not in joined and "ord#" in joined
    assert trading_bot._reinference_hold_reasons("TQQQ", {"T": 1.0}) == []


# ═══════════════════════════════════════════════════════════════════
# 2) 다종목: 검증 후 파일 쓰기 1회(원자적), 검증 실패 ≠ 저장 실패
# ═══════════════════════════════════════════════════════════════════
def _count_replace(monkeypatch):
    calls = []
    real_replace = os.replace

    def counting(src, dst):
        calls.append(dst)
        return real_replace(src, dst)
    monkeypatch.setattr(state_mod.os, "replace", counting)
    return calls


def test_success_writes_file_exactly_once_and_preserves_other_symbols(repair, monkeypatch):
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _ok(15.0)})
    replace_calls = _count_replace(monkeypatch)
    trading_bot._dispatch_repair()
    assert len(replace_calls) == 1, "두 종목이어도 파일 교체는 1회여야 합니다"
    assert state_mod.load_state("TQQQ")["T"] == 12.0
    assert state_mod.load_state("SOXL")["T"] == 15.0
    assert state_mod.load_state("QLD") ["T"] == 3.0, "대상이 아닌 종목은 보존돼야 합니다"
    assert state_mod.load_state("QLD")["orders_meta"] == repair["original"]["QLD"]["orders_meta"]


def test_validation_failure_on_second_symbol_never_touches_the_file(repair, monkeypatch, capsys):
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _ok(25.0)})  # SOXL 25 > MAX_T 20
    replace_calls = _count_replace(monkeypatch)
    before = _sha(repair["file"])
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    assert replace_calls == [], "검증 실패 시 파일 교체 시도 자체가 없어야 합니다"
    out = capsys.readouterr().out
    assert "검증 실패" in out and "저장 실패" not in out
    _assert_file_untouched(repair, before)


def test_file_save_failure_exits_2_with_distinct_message_and_keeps_original(repair, monkeypatch, capsys):
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _ok(15.0)})
    before = _sha(repair["file"])

    def broken_replace(src, dst):
        raise OSError("디스크 가득 참(모의)")
    monkeypatch.setattr(state_mod.os, "replace", broken_replace)
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 2, "파일 저장 실패는 검증 실패(1)와 다른 종료 코드여야 합니다"
    out = capsys.readouterr().out
    assert "저장 실패" in out and "변경되지 않았습니다" in out
    assert "검증 실패" not in out
    _assert_file_untouched(repair, before)


def test_failure_in_the_middle_of_writing_keeps_original(repair, monkeypatch, capsys):
    """임시 파일에 쓰다 실패(json.dump 예외)해도 원본은 그대로이고 임시 파일도 정리됩니다."""
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _ok(15.0)})
    before = _sha(repair["file"])

    def broken_dump(*args, **kwargs):
        raise OSError("쓰기 중 오류(모의)")
    monkeypatch.setattr(state_mod.json, "dump", broken_dump)
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 2
    _assert_file_untouched(repair, before)


def test_corrupt_existing_state_file_is_never_overwritten(repair, capsys):
    repair["impl"]({"TQQQ": _ok(12.0), "SOXL": _ok(15.0)})
    Path(repair["file"]).write_text("{ 손상된 json", encoding="utf-8")
    before = _sha(repair["file"])
    with pytest.raises(SystemExit):
        trading_bot._dispatch_repair()
    assert _sha(repair["file"]) == before, "손상된 파일을 덮어쓰면 안 됩니다"


def test_save_states_is_atomic_and_single_write(tmp_path, monkeypatch):
    """state.save_states 단독: 파일 교체 1회, 기존 종목 보존, 실패 시 원본 유지."""
    state_mod.save_state("QLD", _record(3.0, 99.0))
    calls = _count_replace(monkeypatch)
    state_mod.save_states({"tqqq": _record(1.0, 1.0), "SOXL": _record(2.0, 2.0)})
    assert len(calls) == 1
    assert {s: state_mod.load_state(s)["T"] for s in ("TQQQ", "SOXL", "QLD")} == {"TQQQ": 1.0, "SOXL": 2.0, "QLD": 3.0}
