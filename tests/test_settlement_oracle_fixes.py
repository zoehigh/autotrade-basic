"""
정산·검증 오라클 재검토(2026-10-23) 8건 수정 회귀 테스트.

① load_state가 sell_t_effects를 반환 (재시작 후 원장 소실 방지)
② 전체 재추정이 낡은 매도 원장을 리셋
③ 부분 매도 사이 매수 T가 재조정에 함께 스케일되지 않음 (t_before 기반)
④ 기준선 없는 지연 매도가 _position_basis_unknown을 남김
⑤ reverse_base_t 누락 시 경고 + 폴백
⑥ 검증기가 T/순투입금을 독립 재계산 (t_delta 합산이 아님)
⑦ evidence에 late/추가/미등록 체결이 모두 포함
⑧ 취소 대상 선정이 실제 KST 주문일 기준
"""
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import state as state_mod
from state import (
    _apply_sell_t_effect,
    _classify_sell_ratio,
    load_state,
    save_state,
    update_T_from_history,
)

sys.path.append(str(Path(__file__).resolve().parents[1] / "ops"))
import verify_run


def _row(odno, side="매수", qty=10, ts="2026-10-21T09:00:00+00:00", price=10.0,
         status=None, ord_dt=None, ord_tmd="040000"):
    return {
        "odno": odno,
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": str(qty),
        "ft_ccld_amt3": str(qty * price),
        "ft_ccld_unpr3": str(price),
        "ord_datetime_utc": ts,
        "ord_dt": ord_dt or ts[:10].replace("-", ""),
        "ord_tmd": ord_tmd,
        "prcs_stat_name": status or "체결",
        "nccs_qty": "0",
    }


def _state(T=10.0, **kw):
    st = {
        "T": T,
        "net_invested": 1000.0,
        "last_updated": "2026-10-20T11:14:03+00:00",
        "last_processed_ordno": "OLD",
        "orders_meta": {},
        "cycle_start_date": "2026-09-23",
        "additional_loc_odno": [],
        "balance_mismatch": {},
        "net_invested_status": "valid",
    }
    st.update(kw)
    return st


# ── ① ──────────────────────────────────────────────────────────────
def test_load_state_returns_sell_t_effects(tmp_path, monkeypatch):
    f = tmp_path / ".state.json"
    f.write_text('{"TQQQ": {"T": 5.0, "sell_t_effects": {"S1": {"basis": 80, "factor": 0.75, "t_before": 5.0}}}}')
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(f))
    st = load_state("TQQQ")
    assert st["sell_t_effects"] == {"S1": {"basis": 80, "factor": 0.75, "t_before": 5.0}}

    st["T"] = 6.0
    save_state("TQQQ", st)
    st2 = load_state("TQQQ")
    assert st2["T"] == 6.0
    assert st2["sell_t_effects"]["S1"]["factor"] == 0.75


# ── ② ──────────────────────────────────────────────────────────────
def test_full_reinference_resets_stale_ledger():
    st = _state(T=0.0, last_updated="", sell_t_effects={"OLD": {"basis": 10, "factor": 0.25, "t_before": 3.0}})
    orders = [_row("B1", "매수", 10, "2026-10-21T09:00:00+00:00")]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)
    assert "OLD" not in result.get("sell_t_effects", {})


def test_full_reinference_marks_additional_fills_as_processed():
    st = _state(
        T=0.0,
        last_updated="",
        orders_meta={"A1": {"is_additional": True, "total_qty": 10,
                            "processed_filled_qty": 1, "processed_filled_amount": 10.0}},
    )
    orders = [_row("A1", "매수", 10, "2026-10-21T09:00:00+00:00")]
    update_T_from_history("TQQQ", st, orders)
    assert st["orders_meta"]["A1"]["processed_filled_qty"] == 10
    assert st["orders_meta"]["A1"]["processed_filled_amount"] == pytest.approx(100.0)
    money = st["net_invested"]
    update_T_from_history("TQQQ", st, orders)
    assert st["net_invested"] == pytest.approx(money)


# ── ③ ──────────────────────────────────────────────────────────────
def test_sell_rescale_does_not_scale_intervening_buy():
    st = {}
    T = 10.0
    # 80주 보유 중 20주 매도 → 쿼터매도(×0.75)
    T, _ = _apply_sell_t_effect(st, T, 80, "S1", 20)
    assert T == pytest.approx(7.5)
    # 사이에 매수 +2
    T += 2.0
    # 누적 60주 → 목표매도(×0.25). 매도 기여분만 재조정되어야 함
    T, _ = _apply_sell_t_effect(st, T, 80, "S1", 60)
    assert T == pytest.approx(9.5 - 10.0 * 0.75 + 10.0 * 0.25)  # 4.5
    # 일괄 반영(60주 한 번, T=10 기준)과 비교: 매도 기여분만 동일하게 조정됨
    st2 = {}
    T2, _ = _apply_sell_t_effect(st2, 10.0, 80, "S1", 60)
    assert T2 == pytest.approx(10.0 * 0.25)  # 2.5
    assert T == pytest.approx(T2 + 2.0)  # 사이에 낀 매수 +2는 그대로 유지


def test_late_partial_sell_reclassification_accounts_for_later_sell():
    st = {}
    T, _ = _apply_sell_t_effect(st, 20.0, 100, "S1", 5)
    T, _ = _apply_sell_t_effect(st, T, 95, "S2", 48)
    assert T == pytest.approx(3.75)
    T, _ = _apply_sell_t_effect(st, T, 100, "S1", 50)
    assert T == pytest.approx(1.25)


# ── ④ ──────────────────────────────────────────────────────────────
def test_late_sell_without_basis_sets_unknown_flag():
    st = _state(
        T=10.0,
        last_updated="2026-10-21T10:00:00+00:00",
        orders_meta={
            "S1": {"processed_filled_qty": 5, "processed_filled_amount": 50.0,
                   "total_qty": 20, "t_target": 0.0},
        },
    )
    # 워터마크 이전 주문의 체결이 뒤늦게 증가 (기준선 없음: 잔고 None + 저장값 없음)
    orders = [_row("S1", "매도", 10, "2026-10-20T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)
    assert result.get("_position_basis_unknown") is True


# ── ⑤ ──────────────────────────────────────────────────────────────
def test_reverse_reconcile_warns_on_missing_base_t(capsys):
    st = _state(
        T=10.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1",
                   "reverse_t_factor": 0.9, "total_qty": 10,
                   "submitted_at": "20261022041650", "submitted_session": "2026-10-20",
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
        },
    )
    orders = [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="부분체결", ord_dt="20261021", ord_tmd="041650")]
    orders[0]["ord_datetime_utc"] = "2026-10-21T08:16:50+00:00"
    from state import reconcile_reverse_fills
    reconcile_reverse_fills(st, orders)
    out = capsys.readouterr().out
    assert "reverse_base_t" in out


# ── ⑥⑦ ────────────────────────────────────────────────────────────
def _evidence():
    return {
        "T_before": 10.0,
        "net_invested_before": 1000.0,
        "balance_qty": 100,
        "T_after": 8.25,
        "net_invested_after": 950.0,
        "orders": [
            {"odno": "B1", "side": "매수", "qty_delta": 10, "amount_delta": 100.0,
             "t_target": 1.0, "total_qty": 10, "is_additional": False, "t_delta": 1.0},
            {"odno": "S1", "side": "매도", "qty_delta": 25, "amount_delta": 150.0,
             "cumulative_qty": 25, "prev_cumulative_qty": 0,
              "basis": 125, "factor": 0.75,
             "factor_before": 1.0, "t_before": 11.0, "t_delta": -2.75},
        ],
        "fills": [
            {"odno": "B1", "qty": 10, "amount": 100.0},
            {"odno": "S1", "qty": 25, "amount": 150.0},
        ],
    }


def test_verifier_independently_recomputes_sell_t():
    problems, t, money = verify_run._recompute_expected(_evidence())
    assert problems == [], problems
    assert t == pytest.approx(8.25)  # 10 + 1 − 11×1 + 11×0.75
    assert money == pytest.approx(950.0)


def test_verifier_catches_wrong_sell_factor():
    ev = _evidence()
    ev["orders"][1] = dict(ev["orders"][1], factor=0.25, t_delta=-8.25)
    ev["T_after"] = 2.75
    ev["net_invested_after"] = 950.0
    problems, t, money = verify_run._recompute_expected(ev)
    assert any("매도 분류 불일치" in p for p in problems), problems


def test_verifier_catches_tampered_t_delta():
    ev = _evidence()
    ev["orders"][1] = dict(ev["orders"][1], t_delta=-1.0)
    problems, t, money = verify_run._recompute_expected(ev)
    assert any("T 델타 불일치" in p for p in problems), problems


def test_verifier_does_not_trust_first_sell_t_before():
    ev = _evidence()
    ev["orders"][1] = dict(ev["orders"][1], t_before=4.0, t_delta=-1.0)
    ev["T_after"] = 9.0
    problems = verify_run._verify_evidence(ev)
    assert any("T 불일치" in p for p in problems), problems


# ── P0-3: 미등록 매수 T 배분 독립 검증 ──────────────────────────────
def _unregistered_evidence(share_a=0.5, share_b=0.5):
    return {
        "T_before": 5.0,
        "net_invested_before": 500.0,
        "balance_qty": 120,
        "T_after": 6.0,
        "net_invested_after": 700.0,
        "orders": [
            {"odno": "B1", "side": "매수", "qty_delta": 10, "amount_delta": 100.0,
             "t_target": 1.0, "total_qty": 20, "unregistered": True,
             "ord_dt": "20261021", "t_delta": share_a},
            {"odno": "B2", "side": "매수", "qty_delta": 10, "amount_delta": 100.0,
             "t_target": 1.0, "total_qty": 20, "unregistered": True,
             "ord_dt": "20261021", "t_delta": share_b},
        ],
        "fills": [
            {"odno": "B1", "qty": 10, "amount": 100.0, "ts": "2026-10-21T09:00:00+00:00"},
            {"odno": "B2", "qty": 10, "amount": 100.0, "ts": "2026-10-21T10:00:00+00:00"},
        ],
    }


def test_verifier_recomputes_unregistered_buy_count_fallback():
    problems, t, money = verify_run._recompute_expected(_unregistered_evidence())
    assert problems == [], problems
    assert t == pytest.approx(6.0)
    assert money == pytest.approx(700.0)

    # 같은 총합(1.0)이라도 배분이 틀리면 실패해야 한다.
    problems, _, _ = verify_run._recompute_expected(
        _unregistered_evidence(share_a=0.8, share_b=0.2)
    )
    assert any("미등록매수 T 배분 불일치" in p for p in problems), problems


# ── P1-4: 체결 마커 무결성 ───────────────────────────────────────────
def _marker_evidence():
    return {
        "orders_meta_before": {
            "B1": {"processed_filled_qty": 5, "processed_filled_amount": 50.0},
        },
        "orders_meta_after": {
            "B1": {"processed_filled_qty": 10, "processed_filled_amount": 100.0},
        },
        "orders": [{"odno": "B1"}],
        "fills": [{"odno": "B1", "qty": 10, "amount": 100.0}],
    }


def test_fill_marker_integrity_passes_clean_advance():
    assert verify_run._verify_fill_markers(_marker_evidence()) == []


def test_fill_marker_integrity_detects_double_add():
    ev = _marker_evidence()
    ev["orders_meta_after"]["B1"]["processed_filled_qty"] = 15
    problems = verify_run._verify_fill_markers(ev)
    assert any("이중 가산" in p or "초과" in p for p in problems), problems


def test_fill_marker_integrity_detects_amount_regression():
    ev = _marker_evidence()
    ev["orders_meta_after"]["B1"]["processed_filled_amount"] = 40.0
    problems = verify_run._verify_fill_markers(ev)
    assert any("역행" in p for p in problems), problems


def test_fill_marker_integrity_detects_unapplied_marker_change():
    ev = deepcopy(_marker_evidence())
    ev["orders"] = []
    problems = verify_run._verify_fill_markers(ev)
    assert any("적용되지 않은 주문" in p for p in problems), problems
