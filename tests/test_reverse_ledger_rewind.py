"""리버스 매도 오분류 정산 되돌리기(REPAIR_ACTION=REVERSE_LEDGER_REWIND) 검증.

사고(2026-11-06 NHPLUG SOXL): 리버스 1일차 매도(odno 2617, 4주 $179.52)가 일반 sell(×0.75)로 정산되어
T 19.25 → 14.4375 (정상 17.325). 복구는 SOXL 만 되돌리고(TQQQ 불변) 다음 정산이 리버스로 1회 반영합니다.
"""
import os
import sys
from copy import deepcopy

import pytest

_repo_root = os.path.join(os.path.dirname(__file__), "..")
for _p in (_repo_root, os.path.join(_repo_root, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import (
    canonical_state_hash,
    load_state,
    reconcile_reverse_fills,
    rewind_misclassified_reverse_sell,
    save_state,
)
from trading_bot import _REPAIR_ACTIONS, _reverse_ledger_rewind

ODNO = "2617"


def _soxl_after_bad_run():
    """오분류 정산 직후 SOXL (실측 구조 재현)."""
    return {
        "T": 14.4375,
        "last_updated": "2026-11-05T19:10:00+00:00",
        "last_processed_ordno": ODNO,
        "cycle_start_date": "2026-09-22",
        "effective_seed": 0.0,
        "net_invested": 4898.69,
        "net_invested_status": "valid",
        "position_qty": 38,
        "balance_mismatch": {},
        "close_prices": [],
        "additional_loc_odno": [],
        "pending_order_intent": None,
        "pending_order_batch": None,
        "reverse_mode": {
            "active": True, "cycle_id": "C1", "last_planned_session": "2026-11-05",
            "reconciliation_error": "reverse_order_history_missing", "reconciliation_only": True,
        },
        "orders_meta": {
            "3048": {"side": "BUY", "total_qty": 1, "t_target": 1.0, "processed_filled_qty": 1},
            ODNO: {
                "side": "SELL", "total_qty": 4, "t_target": 0.0, "is_additional": False,
                "processed_filled_qty": 4, "processed_filled_amount": 179.52, "fill_marker_version": 1,
                "reverse_action": "sell", "reverse_day": 1, "cycle_id": "C1",
                "submitted_at": "20261106052250", "submitted_session": "2026-11-05",
                "reverse_base_t": 19.25, "reverse_t_factor": 0.9,
            },
        },
        "sell_t_effects": {ODNO: {"basis": 42, "factor": 0.75}},
        "settlement_ledger": {
            "start_T": 13.25,
            "entries": [
                {"seq": i, "kind": "buy", "odno": o, "delta": 1.0}
                for i, o in enumerate(["1293", "1080", "1157", "1842", "1516", "3048"], start=1)
            ] + [{"seq": 7, "kind": "sell", "odno": ODNO, "basis": 42, "factor": 0.75, "cumulative_qty": 4}],
            "next_seq": 8,
        },
        "state_version": "v2",
    }


def _tqqq():
    return {
        "T": 24.0, "last_updated": "2026-11-05T19:10:00+00:00", "last_processed_ordno": "2613",
        "cycle_start_date": "2026-09-23", "effective_seed": 0.0, "net_invested": 5589.11,
        "net_invested_status": "valid", "position_qty": 78, "balance_mismatch": {}, "close_prices": [],
        "additional_loc_odno": [], "pending_order_intent": None, "pending_order_batch": None,
        "reverse_mode": {}, "orders_meta": {}, "sell_t_effects": {},
        "settlement_ledger": {"start_T": 17.0, "entries": [{"seq": 1, "kind": "buy", "odno": "2613", "delta": 1.0}], "next_seq": 2},
        "state_version": "v2",
    }


NH_ROW = {
    "odno": ODNO, "ord_dt": "20261105", "ord_tmd": "",
    "ft_ccld_qty": "4", "ft_ccld_unpr3": "44.88", "ft_ccld_amt3": "179.52",
    "nccs_qty": "0", "prcs_stat_name": "체결",
}


# ── 순수 함수 ────────────────────────────────────────────────────

def test_되돌린_뒤_재정산하면_리버스_1회_반영으로_정상값에_수렴():
    s = _soxl_after_bad_run()
    summary = rewind_misclassified_reverse_sell(s, ODNO)
    assert summary["removed_seq"] == 7
    assert s["T"] == 19.25 and s["net_invested"] == 5078.21
    assert s["sell_t_effects"] == {}
    assert s["settlement_ledger"]["next_seq"] == 7 and len(s["settlement_ledger"]["entries"]) == 6
    assert s["orders_meta"][ODNO]["processed_filled_qty"] == 0
    assert s["orders_meta"][ODNO]["processed_filled_amount"] == 0.0
    # 워터마크·포지션·reverse_mode 는 불변
    assert s["last_updated"] == "2026-11-05T19:10:00+00:00" and s["last_processed_ordno"] == ODNO
    assert s["position_qty"] == 38 and s["reverse_mode"]["cycle_id"] == "C1"

    reconcile_reverse_fills(s, [deepcopy(NH_ROW)])
    assert s["T"] == 17.325
    assert s["net_invested"] == 4898.69
    assert s["reverse_mode"]["day_count"] == 1
    assert s["reverse_mode"]["cumulative_sell_proceeds"] == 179.52
    assert "reconciliation_only" not in s["reverse_mode"] and "reconciliation_error" not in s["reverse_mode"]
    assert s["orders_meta"][ODNO]["terminal"] is True
    assert not s.get("_settlement_hold")


def _assert_untouched_on_refusal(mutate, match):
    s = _soxl_after_bad_run()
    mutate(s)
    before = deepcopy(s)
    with pytest.raises(ValueError, match=match):
        rewind_misclassified_reverse_sell(s, ODNO)
    assert s == before  # 거부 시 아무것도 바뀌지 않음


def test_전제_불충족이면_상태를_바꾸지_않고_거부():
    _assert_untouched_on_refusal(lambda s: s["orders_meta"].pop(ODNO), "주문이 없습니다")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(reverse_action="buy"), "리버스 매도")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(cycle_id="OLD"), "cycle")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(terminal=True), "terminal")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(applied_fill_fraction=1.0), "이미 반영")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(processed_filled_qty=0), "마커")
    _assert_untouched_on_refusal(lambda s: s["orders_meta"][ODNO].update(reverse_base_t=18.0), "reverse_base_t")
    _assert_untouched_on_refusal(lambda s: s.update(pending_order_batch={"x": 1}), "fence")
    _assert_untouched_on_refusal(lambda s: s["sell_t_effects"].clear(), "sell_t_effects")
    _assert_untouched_on_refusal(lambda s: s.update(T=14.0), "재생값")


def test_원장_마지막_항목이_아니거나_이미_리버스_항목이면_거부():
    def append_later(s):
        s["settlement_ledger"]["entries"].append({"seq": 8, "kind": "buy", "odno": "9999", "delta": 1.0})
        s["settlement_ledger"]["next_seq"] = 9
        s["T"] = round(14.4375 + 1.0, 4)
    _assert_untouched_on_refusal(append_later, "마지막")

    def already_reverse(s):
        s["settlement_ledger"]["entries"][-1]["kind"] = "reverse_sell"
    _assert_untouched_on_refusal(already_reverse, "정확히 1개")


# ── 핸들러 (CAS · 가드 · 저장 범위) ────────────────────────────────

def _env(monkeypatch, **over):
    base = {
        "REPAIR_REWIND_SYMBOL": "SOXL", "REPAIR_REWIND_ODNO": ODNO,
        "REPAIR_REWIND_EXPECT_HASH": canonical_state_hash(load_state("SOXL")),
        "REPAIR_REWIND_EXPECT_T": "14.4375",
        "REPAIR_REWIND_GUARD_SYMBOL": "TQQQ",
        "REPAIR_REWIND_GUARD_HASH": canonical_state_hash(load_state("TQQQ")),
    }
    base.update(over)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def two_symbols():
    save_state("SOXL", _soxl_after_bad_run())
    save_state("TQQQ", _tqqq())


def test_액션이_허용_목록에_등록돼_있다():
    assert "REVERSE_LEDGER_REWIND" in _REPAIR_ACTIONS


def test_미리보기는_저장하지_않는다(two_symbols, monkeypatch):
    _env(monkeypatch)
    before = canonical_state_hash(load_state("SOXL"))
    _reverse_ledger_rewind()
    assert canonical_state_hash(load_state("SOXL")) == before


def test_적용은_SOXL만_바꾸고_TQQQ는_불변(two_symbols, monkeypatch):
    _env(monkeypatch, REPAIR_REWIND_APPLY="true")
    tqqq_before = canonical_state_hash(load_state("TQQQ"))
    _reverse_ledger_rewind()
    s = load_state("SOXL")
    assert s["T"] == 19.25 and s["net_invested"] == 5078.21
    assert canonical_state_hash(load_state("TQQQ")) == tqqq_before


@pytest.mark.parametrize("name,value,match", [
    ("REPAIR_REWIND_EXPECT_HASH", "0" * 64, "hash 불일치"),
    ("REPAIR_REWIND_EXPECT_T", "14.0", "T 불일치"),
    ("REPAIR_REWIND_GUARD_HASH", "0" * 64, "가드 종목"),
    ("REPAIR_REWIND_GUARD_SYMBOL", "SOXL", "달라야"),
    ("REPAIR_REWIND_ODNO", "9999", "전제 불충족"),
])
def test_CAS_또는_가드_불일치는_저장_없이_중단(two_symbols, monkeypatch, name, value, match):
    _env(monkeypatch, REPAIR_REWIND_APPLY="true", **{name: value})
    soxl_before = canonical_state_hash(load_state("SOXL"))
    with pytest.raises(RuntimeError, match=match):
        _reverse_ledger_rewind()
    assert canonical_state_hash(load_state("SOXL")) == soxl_before


def test_필수_환경변수가_없으면_중단(two_symbols, monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("REPAIR_REWIND_GUARD_HASH")
    with pytest.raises(RuntimeError, match="환경변수가 없습니다"):
        _reverse_ledger_rewind()
