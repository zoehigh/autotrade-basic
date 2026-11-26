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


# ── ⑨ 검증기 보유 궤적에 T-효과 없는 추가매수 반영 (KIS 실측 회귀) ───────────
# KIS DRY full_reinference 실측: 원장 매도 3건의 기준선이 69/74/109로 기록됐으나
# 검증기가 66/70/98을 계산해 보류. 차이 3/4/11은 각 매도 이전의 누적 추가매수량
# (ledger_seq 없음, is_additional=True, t_delta=0)과 정확히 일치했다.
# 검증기는 _required_settlement_fills가 시간순으로 계산한 보유 궤적(추가매수 포함)의
# 기준선을 사용해야 한다. 아래 fixture는 실제 KIS Evidence의 최소 익명 구조다.


def _ordered_evidence_with_additional():
    """정상매수 → 추가매수(3주) → 매도. 매도 기준선은 13이어야 한다 (10+3)."""
    return {
        "settlement_mode": "full_reinference",
        "T_before": 0.0,
        "net_invested_before": 0.0,
        "balance_qty": 10,  # 매도 후 잔고
        "T_after": 0.0,
        "net_invested_after": 0.0,
        "orders_meta_before": {},
        "ledger_before": {"entries": []},
        "ledger_after": {
            "start_T": 0.0,
            "entries": [
                {"seq": 1, "kind": "buy", "odno": "B1", "delta": 1.0},
                # 추가매수는 T 효과 없어 원장 항목 없음 (ledger_seq=None)
                {"seq": 2, "kind": "sell", "odno": "S1", "basis": 13, "factor": 0.75,
                 "cumulative_qty": 3},
            ],
        },
        "orders": [
            {"odno": "B1", "side": "매수", "qty_delta": 10, "amount_delta": 100.0,
             "t_target": 1.0, "total_qty": 10, "is_additional": False,
             "ledger_seq": 1, "t_delta": 1.0},
            {"odno": "A1", "side": "매수", "qty_delta": 1, "amount_delta": 10.0,
             "is_additional": True, "ledger_seq": None, "t_delta": 0.0},
            {"odno": "A2", "side": "매수", "qty_delta": 1, "amount_delta": 10.0,
             "is_additional": True, "ledger_seq": None, "t_delta": 0.0},
            {"odno": "A3", "side": "매수", "qty_delta": 1, "amount_delta": 10.0,
             "is_additional": True, "ledger_seq": None, "t_delta": 0.0},
            {"odno": "S1", "side": "매도", "qty_delta": 3, "amount_delta": 30.0,
             "cumulative_qty": 3, "basis": 13, "factor": 0.75,
             "ledger_seq": 2, "t_delta": -0.25},
        ],
        "fills": [
            {"odno": "B1", "side": "매수", "qty": 10, "amount": 100.0,
             "ts": "2026-10-14T09:00:00+00:00", "is_additional": False},
            {"odno": "A1", "side": "매수", "qty": 1, "amount": 10.0,
             "ts": "2026-10-15T09:00:00+00:00", "is_additional": True},
            {"odno": "A2", "side": "매수", "qty": 1, "amount": 10.0,
             "ts": "2026-10-16T09:00:00+00:00", "is_additional": True},
            {"odno": "A3", "side": "매수", "qty": 1, "amount": 10.0,
             "ts": "2026-10-17T09:00:00+00:00", "is_additional": True},
            {"odno": "S1", "side": "매도", "qty": 3, "amount": 30.0,
             "ts": "2026-10-21T09:00:00+00:00", "is_additional": False},
        ],
    }


def test_ordered_verifier_includes_additional_buys_in_sell_basis():
    """정상매수+추가매수 이후 매도의 기준선에 추가매수 수량이 포함된다."""
    ev = _ordered_evidence_with_additional()
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    assert sell["basis"] == 13, f"추가매수 3주 누락: {sell['basis']}"
    assert not any("기준선 불일치" in p for p in problems), problems


def test_ordered_verifier_additional_buy_after_sell_not_counted():
    """매도 이후의 추가매수는 이전 매도 기준선에 반영되지 않는다."""
    ev = _ordered_evidence_with_additional()
    # A3을 매도(S1) 이후로 이동
    a3 = next(f for f in ev["fills"] if f["odno"] == "A3")
    a3["ts"] = "2026-10-25T09:00:00+00:00"
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    assert sell["basis"] == 12, f"매도 후 추가매수가 기준선에 잘못 반영: {sell['basis']}"


def test_ordered_verifier_detects_tampered_sell_basis():
    """매도 basis를 고의 변조하면 여전히 검증 실패 (게이트 유지)."""
    ev = _ordered_evidence_with_additional()
    # 실제 원장 basis를 10으로 변조 (정답 13)
    sell_actual = next(e for e in ev["ledger_after"]["entries"] if e.get("kind") == "sell")
    sell_actual["basis"] = 10
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    ledger_problems = verify_run._expected_ledger_problems(expected_ledger, ev["ledger_after"])
    assert any("기준선 불일치" in p for p in ledger_problems), ledger_problems


def _kis_like_evidence(additional_before_sell=3, recorded_basis=69):
    """KIS 실측 최소 익명 fixture: 정상매수 66주 + 추가매수 N주 → 매도 17주.

    실제 KIS 원장 #46은 기록 basis=69, 검증기 구계산=66 (추가매수 3주 누락).
    """
    sell_qty = 17
    basis = recorded_basis
    # 정상매수 = 기준선 - 추가매수 (실제 KIS: 매도 직전 보유 = 정상+추가)
    normal_qty = basis - additional_before_sell
    return {
        "settlement_mode": "full_reinference",
        "T_before": 0.0,
        "net_invested_before": 0.0,
        "balance_qty": normal_qty + additional_before_sell - sell_qty,
        "T_after": 0.0,
        "net_invested_after": 0.0,
        "orders_meta_before": {},
        "ledger_before": {"entries": []},
        "ledger_after": {
            "start_T": 0.0,
            "entries": [
                {"seq": 1, "kind": "buy", "odno": "B1", "delta": 1.0},
                {"seq": 2, "kind": "sell", "odno": "S1", "basis": basis,
                 "factor": 0.75, "cumulative_qty": sell_qty},
            ],
        },
        "orders": [
            {"odno": "B1", "side": "매수", "qty_delta": normal_qty, "amount_delta": 1000.0,
             "t_target": 1.0, "total_qty": normal_qty, "is_additional": False,
             "ledger_seq": 1, "t_delta": 1.0},
            *[
                {"odno": f"A{i}", "side": "매수", "qty_delta": 1, "amount_delta": 10.0,
                 "is_additional": True, "ledger_seq": None, "t_delta": 0.0}
                for i in range(additional_before_sell)
            ],
            {"odno": "S1", "side": "매도", "qty_delta": sell_qty, "amount_delta": 200.0,
             "cumulative_qty": sell_qty, "basis": basis, "factor": 0.75,
             "ledger_seq": 2, "t_delta": -0.25},
        ],
        "fills": [
            {"odno": "B1", "side": "매수", "qty": normal_qty, "amount": 1000.0,
             "ts": "2026-10-01T09:00:00+00:00", "is_additional": False},
            *[
                {"odno": f"A{i}", "side": "매수", "qty": 1, "amount": 10.0,
                 "ts": f"2026-10-{10+i:02d}T09:00:00+00:00", "is_additional": True}
                for i in range(additional_before_sell)
            ],
            {"odno": "S1", "side": "매도", "qty": sell_qty, "amount": 200.0,
             "ts": "2026-10-21T09:00:00+00:00", "is_additional": False},
        ],
    }


@pytest.mark.parametrize("add_qty,recorded_basis", [(3, 69), (4, 74), (11, 109)])
def test_kis_real_three_sell_basis_matches_after_fix(add_qty, recorded_basis):
    """KIS 실측 3건 (원장 #46/#52/#62): 추가매수 누락분 반영 후 기준선 일치."""
    ev = _kis_like_evidence(additional_before_sell=add_qty, recorded_basis=recorded_basis)
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    assert sell["basis"] == recorded_basis, (
        f"추가매수 {add_qty}주 반영 실패: 계산 {sell['basis']} vs 기록 {recorded_basis}"
    )
    ledger_problems = verify_run._expected_ledger_problems(expected_ledger, ev["ledger_after"])
    assert not any("기준선 불일치" in p for p in ledger_problems), ledger_problems


def test_full_reinference_same_day_buy_before_sell_uses_sell_first_basis():
    """같은 날 매수가 매도보다 앞서도 기준선은 '그날 매수 반영 전'이어야 한다 (Codex P2).

    정산 코드는 날짜별로 매도를 먼저 처리한다. 66주 보유 + 같은 날 3주 매수 + 17주 매도 시
    정산 기준선은 66이지 69가 아니다. 시간순 basis를 쓰면 69로 잘못 검증한다.
    """
    # 정상매수 63주(전일) + 같은 날 추가매수 3주 + 같은 날 매도 17주.
    # recorded_basis=66은 시간순(63+3) 값이고, 정산의 매도-우선 기준선은 63이어야 한다.
    ev = _kis_like_evidence(additional_before_sell=3, recorded_basis=66)
    for i in range(3):
        f = next(f for f in ev["fills"] if f["odno"] == f"A{i}")
        f["ts"] = "2026-10-21T08:00:0%d+00:00" % i
        f["ord_dt"] = "20261021"
    next(f for f in ev["fills"] if f["odno"] == "S1")["ord_dt"] = "20261021"
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    # 같은 날 매수는 매도 기준선에 미반영 → 63 (시간순 66 아님)
    assert sell["basis"] == 63, f"같은 날 매수가 기준선에 잘못 반영: {sell['basis']}"


def test_full_reinference_ignores_stale_sell_marker_basis():
    """full_reinference는 낡은 sell_t_effects_before 마커를 무시하고 재구성해야 한다 (Codex P2)."""
    ev = _kis_like_evidence(additional_before_sell=3, recorded_basis=69)
    # 낡은 마커에 엉뚱한 basis 기록
    ev["sell_t_effects_before"] = {"S1": {"basis": 999, "factor": 0.75}}
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    assert sell["basis"] == 69, f"낡은 마커 basis가 재구성에 침투: {sell['basis']}"


def test_full_reinference_reverse_sell_reduces_normal_sell_basis():
    """리버스 매도도 보유량 궤적에 반영되어 일반 매도 기준선을 낮춘다 (Codex P2).

    100주 보유 + 25주 리버스 매도 + 17주 일반 매도 → 일반 매도 기준선은 75.
    리버스 체결을 제외하면 100으로 잘못 계산된다. 리버스에는 basis 항목을 만들지 않는다.
    """
    ev = {
        "settlement_mode": "full_reinference",
        "T_before": 0.0, "net_invested_before": 0.0,
        "balance_qty": 58, "T_after": 0.0, "net_invested_after": 0.0,
        "orders_meta_before": {}, "ledger_before": {"entries": []},
        "ledger_after": {"start_T": 0.0, "entries": [
            {"seq": 1, "kind": "buy", "odno": "B1", "delta": 1.0},
            {"seq": 2, "kind": "sell", "odno": "S1", "basis": 75, "factor": 0.75,
             "cumulative_qty": 17},
        ]},
        "orders": [
            {"odno": "B1", "side": "매수", "qty_delta": 100, "amount_delta": 1000.0,
             "t_target": 1.0, "total_qty": 100, "is_additional": False,
             "ledger_seq": 1, "t_delta": 1.0},
            {"odno": "RS", "side": "매도", "qty_delta": 25, "amount_delta": 250.0,
             "reverse_action": "sell", "ledger_seq": None, "t_delta": 0.0},
            {"odno": "S1", "side": "매도", "qty_delta": 17, "amount_delta": 170.0,
             "cumulative_qty": 17, "basis": 75, "factor": 0.75,
             "ledger_seq": 2, "t_delta": -0.25},
        ],
        "fills": [
            {"odno": "B1", "side": "매수", "qty": 100, "amount": 1000.0,
             "ts": "2026-10-01T09:00:00+00:00", "ord_dt": "20261001"},
            {"odno": "RS", "side": "매도", "qty": 25, "amount": 250.0,
             "ts": "2026-10-20T08:00:00+00:00", "ord_dt": "20261020",
             "reverse_action": "sell"},
            {"odno": "S1", "side": "매도", "qty": 17, "amount": 170.0,
             "ts": "2026-10-21T09:00:00+00:00", "ord_dt": "20261021"},
        ],
    }
    problems, expected_ledger, _ = verify_run._independent_expected_ordered(ev)
    sell = next(e for e in expected_ledger["entries"] if e.get("kind") == "sell")
    assert sell["basis"] == 75, f"리버스 매도 25주 미반영: {sell['basis']}"
    # 리버스 주문은 일반 매도 basis 항목을 만들지 않아야 함
    assert not any(e.get("odno") == "RS" for e in expected_ledger["entries"])
