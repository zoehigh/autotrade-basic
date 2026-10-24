"""
정산 순서 원장(ordered ledger) 계약 테스트.

T 의 덧셈(매수)과 곱셈(매도)은 순서가 바뀌면 결과가 달라집니다.
기존 `t_before + downstream_factor` 누적 방식 대신, 적용 순서(seq)를 가진
작은 원장을 재생(replay)해 T 를 계산합니다.

정답 예시: 시작 T=20, S1 최종 ×0.25, 중간 매수 +2, S2 최종 ×0.25 → 최종 T=1.75
"""
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import state as state_mod
import trading_bot
from state import (
    _apply_buy_t_effect,
    _apply_sell_t_effect,
    _classify_sell_ratio,
    _order_us_session_date,
    canonical_state_hash,
    load_state,
    save_state,
    update_T_from_history,
)

sys.path.append(str(Path(__file__).resolve().parents[1] / "ops"))
import verify_run


# ── 헬퍼 ────────────────────────────────────────────────────────────
def _row(odno, side="매수", qty=10, ts="2026-10-21T09:00:00+00:00", price=10.0,
         status=None, ord_dt=None, ord_tmd="040000", reverse=False):
    row = {
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
    if reverse:
        row["_ord_dt_is_kst_date"] = True
    return row


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
        "position_qty": None,
        "sell_t_effects": {},
    }
    st.update(kw)
    return st


def _ledger_seqs(state):
    return [e["seq"] for e in state["settlement_ledger"]["entries"]]


# ── 1) 복합 부분체결 — 순서 원장 1.75 ───────────────────────────────
def test_ordered_ledger_complex_partial_fills_gives_1_75():
    st = {"T": 20.0}
    T = 20.0
    # S1 5/100 (×0.75)
    T, _ = _apply_sell_t_effect(st, T, 100, "S1", 5)
    assert T == pytest.approx(15.0)
    # 중간 매수 +2 (매수/매도 순서가 뒤바뀌면 결과가 달라진다)
    T, _ = _apply_buy_t_effect(st, T, 2.0, "B1")
    assert T == pytest.approx(17.0)
    # S2 48/95 (×0.25)
    T, _ = _apply_sell_t_effect(st, T, 95, "S2", 48)
    assert T == pytest.approx(4.25)
    # S1 재분류 75/100 (최종 ×0.25) → 시작 T부터 순서대로 재생
    T, _ = _apply_sell_t_effect(st, T, 100, "S1", 75)
    assert T == pytest.approx(1.75)


# ── 2) 매도 누적 증가 — 일괄/분할 일치 ──────────────────────────────
def test_sell_batch_and_staged_are_equal():
    batch = {"T": 20.0}
    one, _ = _apply_sell_t_effect(batch, 20.0, 100, "S", 75)

    staged = {"T": 20.0}
    T, _ = _apply_sell_t_effect(staged, 20.0, 100, "S", 5)
    T, _ = _apply_sell_t_effect(staged, T, 100, "S", 75)

    assert one == pytest.approx(5.0)
    assert staged["T"] == pytest.approx(one)


# ── 3) 매도 2개 번갈아 증가 — 재분류 순서 재생 ──────────────────────
def test_two_sells_interleaved_increases_replay_in_order():
    st = {"T": 20.0}
    T, _ = _apply_sell_t_effect(st, 20.0, 100, "S1", 25)   # ×0.75
    T, _ = _apply_sell_t_effect(st, T, 90, "S2", 40)        # ×0.75
    T, _ = _apply_sell_t_effect(st, T, 100, "S1", 60)       # ×0.25 (재분류)
    assert T == pytest.approx(20.0 * 0.25 * 0.75)
    T, _ = _apply_sell_t_effect(st, T, 90, "S2", 70)        # ×0.25 (재분류)
    assert T == pytest.approx(20.0 * 0.25 * 0.25)


# ── 4) 동일 이력 반복 불변 ──────────────────────────────────────────
def test_repeated_identical_history_is_idempotent():
    st = _state(
        T=8.0, position_qty=100,
        orders_meta={"S": {"side": "SELL", "total_qty": 75, "t_target": 0.0,
                           "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 75, "2026-10-21T09:00:00+00:00"),
    ]
    first = update_T_from_history("TQQQ", st, orders, balance_qty=25)
    second = update_T_from_history("TQQQ", first, orders, balance_qty=25)
    third = update_T_from_history("TQQQ", second, orders, balance_qty=25)

    assert first["T"] == pytest.approx(2.0)
    assert second["T"] == pytest.approx(first["T"])
    assert third["T"] == pytest.approx(first["T"])
    assert third["net_invested"] == pytest.approx(first["net_invested"])
    assert _ledger_seqs(third) == _ledger_seqs(first)


# ── 5) 저장·로드 후 다음 체결 일치 ──────────────────────────────────
def test_save_load_ledger_continues_same_result(tmp_path, monkeypatch):
    f = tmp_path / ".state.json"
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(f))

    def _initial():
        return _state(
            T=20.0, position_qty=100, net_invested=1000.0,
            orders_meta={"S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                               "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
        )

    first_orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 5, "2026-10-21T09:00:00+00:00"),
    ]
    second_orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 75, "2026-10-22T09:00:00+00:00"),
    ]

    # 연속 실행 (메모리)
    continuous = update_T_from_history("TQQQ", _initial(), first_orders, balance_qty=95)
    continuous = update_T_from_history("TQQQ", continuous, second_orders, balance_qty=25)

    # 저장 → 로드 → 다음 체결
    stored = update_T_from_history("TQQQ", _initial(), first_orders, balance_qty=95)
    save_state("TQQQ", stored)
    loaded = load_state("TQQQ")
    loaded = update_T_from_history("TQQQ", loaded, second_orders, balance_qty=25)

    assert loaded["T"] == pytest.approx(continuous["T"])
    assert loaded["T"] == pytest.approx(5.0)  # 20 × 0.25
    assert loaded["net_invested"] == pytest.approx(continuous["net_invested"])
    assert loaded["settlement_ledger"]["entries"] == continuous["settlement_ledger"]["entries"]


# ── 5-2) 원장 저장/로드/해시 일관성 ─────────────────────────────────
def test_save_load_ledger_and_canonical_hash_consistent(tmp_path, monkeypatch):
    f = tmp_path / ".state.json"
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(f))

    st = _state(T=8.0, position_qty=100)
    st["settlement_ledger"] = {
        "start_T": 20.0,
        "next_seq": 3,
        "entries": [
            {"seq": 1, "kind": "sell", "odno": "S1", "basis": 100,
             "factor": 0.75, "cumulative_qty": 25},
            {"seq": 2, "kind": "buy", "odno": "B1", "delta": 2.0},
        ],
    }
    st["sell_t_effects"] = {"S1": {"basis": 100, "factor": 0.75}}
    save_state("TQQQ", st)
    loaded = load_state("TQQQ")

    assert loaded["settlement_ledger"] == st["settlement_ledger"]
    assert canonical_state_hash(loaded) == canonical_state_hash(st)


# ── 6) 첫 매도 evidence 변조 → 검증 실패 ────────────────────────────
def _settle_evidence():
    """실제 정산을 수행해 trading_bot 과 같은 근거를 만든다."""
    st = _state(
        T=10.0, net_invested=1000.0, position_qty=115,
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "S1": {"side": "SELL", "total_qty": 25, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [
        _row("B1", "매수", 10, "2026-10-21T09:00:00+00:00", price=100.0),
        _row("S1", "매도", 25, "2026-10-21T10:00:00+00:00", price=150.0),
    ]
    T_before = st["T"]
    net_before = st["net_invested"]
    ledger_before = deepcopy(st.get("settlement_ledger", {}))
    effects_before = deepcopy(st.get("sell_t_effects", {}))
    result = update_T_from_history("TQQQ", st, orders, balance_qty=100)
    ev = {
        "symbol": "TQQQ",
        "run_id": "TEST",
        "mode": "incremental",
        "evidence_version": 2,
        "T_before": T_before,
        "net_invested_before": net_before,
        "T_after": result["T"],
        "net_invested_after": result["net_invested"],
        "balance_qty": 100,
        "position_basis": result.get("position_qty"),
        "basis_unknown": bool(result.get("_position_basis_unknown")),
        "ledger_before": ledger_before,
        "ledger_after": deepcopy(result["settlement_ledger"]),
        "sell_t_effects_before": effects_before,
        "sell_t_effects": result.get("sell_t_effects", {}),
        "applied_seq": [o.get("ledger_seq") for o in result.get("_settlement_deltas", [])],
        "orders": deepcopy(result.get("_settlement_deltas", [])),
        "fills": [
            {"odno": "B1", "qty": 10, "amount": 1000.0},
            {"odno": "S1", "qty": 25, "amount": 3750.0},
        ],
    }
    return ev


def test_first_sell_evidence_tamper_fails_verification():
    ev = _settle_evidence()
    assert verify_run._verify_evidence(ev) == []

    # 첫 매도 배율 변조 — 원장/근거가 실제 분류와 달라져야 한다
    tampered = deepcopy(ev)
    tampered["ledger_after"]["entries"][-1]["factor"] = 0.25
    problems = verify_run._verify_evidence(tampered)
    assert any("매도" in p for p in problems), problems

    # 첫 매도 T효과(t_delta) 변조
    tampered2 = deepcopy(ev)
    for o in tampered2["orders"]:
        if o["odno"] == "S1":
            o["t_delta"] = 0.0
    problems2 = verify_run._verify_evidence(tampered2)
    assert any("T 델타 불일치" in p for p in problems2) or any("T 불일치" in p for p in problems2)


# ── 7) 리버스 50% 체결 T=10→9.5 및 재실행 유지 ──────────────────────
def test_reverse_half_fill_moves_t_to_9_5_and_stays():
    from state import reconcile_reverse_fills

    st = _state(
        T=10.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
        },
    )
    orders = [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="부분체결", reverse=True)]

    reconcile_reverse_fills(st, orders)
    assert st["T"] == pytest.approx(9.5)

    reconcile_reverse_fills(st, orders)
    assert st["T"] == pytest.approx(9.5)
    # 50% 증가분만 한 번 반영됐는지 (원장 재생 == 실제 T)
    assert st["settlement_ledger"]["entries"][-1]["delta"] == pytest.approx(-0.5)


# ── 8) 리버스매수 후 일반매도 기준선 1회 반영 ────────────────────────
def test_reverse_buy_then_normal_sell_basis_once():
    from state import reconcile_reverse_fills

    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    st = _state(
        T=8.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "RB": {"reverse_action": "buy", "cycle_id": "C1", "reverse_t_target": 0.0,
                   "total_qty": 10, "submitted_at": "", "submitted_session": today,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
            "S": {"side": "SELL", "total_qty": 20, "t_target": 0.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [
        _row("RB", "매수", 10, "2026-10-21T08:00:00+00:00", reverse=True),
        _row("S", "매도", 20, "2026-10-21T10:00:00+00:00"),
    ]
    # 잔고 20주: 100 보유 + 리버스매수 10 - 일반매도 20 = 90? 시나리오를 단순화해
    # 매도 직전 보유 40주(리버스매수 10 포함)로 본다.
    result = update_T_from_history("TQQQ", st, orders, balance_qty=20)
    assert result["sell_t_effects"]["S"]["basis"] == 40
    assert result["T"] == pytest.approx(2.0)  # 8 × 0.25


# ── 9) 전체 재추정 → 검증 → 증분 연결 ───────────────────────────────
def test_full_reinference_verify_then_incremental():
    st = _state(
        T=0.0, last_updated="", net_invested=0.0, position_qty=None,
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 100, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "S1": {"side": "SELL", "total_qty": 20, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [
        _row("B1", "매수", 100, "2026-10-20T09:00:00+00:00", price=10.0),
        _row("S1", "매도", 20, "2026-10-21T10:00:00+00:00", price=10.0),
    ]
    T_before = st["T"]
    ledger_before = deepcopy(st.get("settlement_ledger", {}))
    inferred = update_T_from_history("TQQQ", st, orders, balance_qty=80)
    assert inferred["T"] == pytest.approx(0.75)

    ev = {
        "mode": "full_reinference",
        "evidence_version": 2,
        "T_before": T_before,
        "net_invested_before": 0.0,
        "T_after": inferred["T"],
        "net_invested_after": inferred["net_invested"],
        "balance_qty": 80,
        "ledger_before": ledger_before,
        "ledger_after": deepcopy(inferred["settlement_ledger"]),
        "applied_seq": [o.get("ledger_seq") for o in inferred.get("_settlement_deltas", [])],
        "orders": deepcopy(inferred.get("_settlement_deltas", [])),
        "fills": [
            {"odno": "B1", "qty": 100, "amount": 1000.0},
            {"odno": "S1", "qty": 20, "amount": 200.0},
        ],
    }
    assert verify_run._verify_evidence(ev) == []

    again = update_T_from_history("TQQQ", inferred, orders, balance_qty=80)
    assert again["T"] == pytest.approx(0.75)
    assert again["net_invested"] == pytest.approx(inferred["net_invested"])


# ── 10) 전량매도 $100 후 새매수 $200 → $200 ─────────────────────────
def test_full_sell_resets_then_new_buy_net_invested():
    st = _state(
        T=5.0, net_invested=100.0, position_qty=10,
        orders_meta={"S": {"side": "SELL", "total_qty": 10, "t_target": 0.0,
                           "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    sell_history = [
        _row("OLD", "매수", 10, "2026-10-19T08:00:00+00:00", price=10.0),
        _row("S", "매도", 10, "2026-10-21T09:00:00+00:00", price=10.0),
    ]
    after_sell = update_T_from_history("TQQQ", st, sell_history, balance_qty=0)
    assert after_sell["T"] == 0.0
    assert after_sell["net_invested"] == pytest.approx(0.0)

    after_sell["orders_meta"]["B2"] = {
        "side": "BUY", "total_qty": 20, "t_target": 1.0,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    buy_history = sell_history + [
        _row("B2", "매수", 20, "2026-10-22T09:00:00+00:00", price=10.0),
    ]
    after_buy = update_T_from_history("TQQQ", after_sell, buy_history, balance_qty=20)
    assert after_buy["T"] == pytest.approx(1.0)
    assert after_buy["net_invested"] == pytest.approx(200.0)


# ── 11) 근거 없는 레거시 최초기준 역산 금지(보류) ───────────────────
def test_legacy_late_sell_without_basis_is_held_not_reverse_inferred():
    st = _state(
        T=10.0,
        last_updated="2026-10-21T10:00:00+00:00",
        last_processed_ordno="LATER",
        position_qty=None,
        orders_meta={
            "S": {"side": "SELL", "total_qty": 20, "t_target": 0.0,
                  "processed_filled_qty": 5, "processed_filled_amount": 50.0},
        },
    )
    orders = [_row("S", "매도", 10, "2026-10-20T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=None)

    assert result.get("_position_basis_unknown") is True
    # 기존 T를 되돌려 최초 기준을 역산하지 않는다 (T를 그대로 두고 보류)
    assert result["T"] == pytest.approx(10.0)
    assert "S" not in (result.get("sell_t_effects") or {})


# ── 12) KIS 앞선 UTC 무시 · 미국영업일 판정 ─────────────────────────
def test_kis_us_session_date_ignores_earlier_utc_timestamp():
    row = {
        "odno": "K1",
        "ord_dt": "20261023",          # 미국(ET) 영업일
        "ord_tmd": "013000",           # KST 시각
        "ord_datetime_utc": "2026-10-22T16:30:00+00:00",  # 하루 앞선 호환값
    }
    assert _order_us_session_date(row) == "20261023"


def test_kis_same_us_session_is_cancel_target(monkeypatch):
    real_datetime = datetime

    class FixedDateTime:
        fromisoformat = staticmethod(real_datetime.fromisoformat)

        @staticmethod
        def now(tz=None):
            return real_datetime(2026, 10, 23, 12, 30, tzinfo=ZoneInfo("America/New_York"))

    monkeypatch.setattr(trading_bot, "datetime", FixedDateTime)
    monkeypatch.setattr(
        trading_bot, "get_kst_now",
        lambda: real_datetime(2026, 10, 24, 1, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    calls = []

    class _Broker:
        def cancel_order(self, odno, symbol=None, exchange=None):
            calls.append(odno)

        def get_order_history(self, symbol, exchange, days=30, verbose=False):
            return [{**_row(odno, "매도", 0, "2026-10-23T12:30:00+00:00"),
                     "prcs_stat_name": "취소", "nccs_qty": "0", "ft_ccld_qty": "0"}
                    for odno in calls]

    row = {**_row("OD1", "매도", 0, "2026-10-23T12:30:00+00:00"),
           "prcs_stat_name": "미체결", "nccs_qty": "18", "ft_ccld_qty": "0",
           "ord_dt": "20261023", "ord_tmd": "013000",
           "ord_datetime_utc": "2026-10-22T16:30:00+00:00"}
    count = trading_bot._cancel_open_bot_orders(
        _Broker(), "TQQQ", "NAS", {"orders_meta": {"OD1": {}}}, [row]
    )
    assert count == 1
    assert calls == ["OD1"]


# ── 검증기 오류 주입 (배율/기준선/T효과 0/중복/누락/순서/결과값) ─────
def test_verifier_injected_wrong_basis_fails():
    ev = _settle_evidence()
    tampered = deepcopy(ev)
    for o in tampered["orders"]:
        if o["odno"] == "S1":
            o["basis"] = 50
    for e in tampered["ledger_after"]["entries"]:
        if e.get("odno") == "S1":
            e["basis"] = 50
    problems = verify_run._verify_evidence(tampered)
    assert any("분류 불일치" in p for p in problems), problems


def test_verifier_injected_duplicate_seq_fails():
    ev = _settle_evidence()
    ev["orders"].append(deepcopy(ev["orders"][-1]))
    problems = verify_run._verify_evidence(ev)
    assert any("중복" in p or "순서" in p for p in problems), problems


def test_verifier_injected_missing_ledger_entry_fails():
    ev = _settle_evidence()
    ev["ledger_after"]["entries"] = ev["ledger_after"]["entries"][:-1]
    problems = verify_run._verify_evidence(ev)
    assert any("원장" in p or "누락" in p for p in problems), problems


def test_verifier_injected_order_reorder_fails():
    ev = _settle_evidence()
    ev["orders"] = list(reversed(ev["orders"]))
    ev["applied_seq"] = [o.get("ledger_seq") for o in ev["orders"]]
    problems = verify_run._verify_evidence(ev)
    assert any("순서" in p or "T 불일치" in p or "T 델타" in p for p in problems), problems


def test_verifier_injected_result_tamper_fails():
    ev = _settle_evidence()
    ev["T_after"] = float(ev["T_after"]) + 0.5
    problems = verify_run._verify_evidence(ev)
    assert any("T 불일치" in p for p in problems), problems


def test_verifier_injected_t_effect_zero_fails():
    ev = _settle_evidence()
    for o in ev["orders"]:
        if o["odno"] == "S1":
            o["t_delta"] = 0.0
    problems = verify_run._verify_evidence(ev)
    assert any("T 델타 불일치" in p or "T 불일치" in p for p in problems), problems


# ── 신규 계약 (a)~(h) ───────────────────────────────────────────────
def test_a_legacy_reflected_sell_same_class_keeps_t_without_hold():
    """기존 정상매도 반영(T=7.5, 누적 5) + 같은 분류 10주 증가 → T 유지."""
    st = {"T": 7.5, "sell_t_effects": {"S": {"basis": 100, "factor": 0.75}}}
    T, applied = _apply_sell_t_effect(st, 7.5, 100, "S", 15)
    assert T == pytest.approx(7.5)
    assert applied is False
    assert not st.get("_settlement_hold")
    assert st["sell_t_effects"]["S"]["factor"] == 0.75


def test_b_reverse_half_already_reflected_replay_stays_9_5():
    from state import reconcile_reverse_fills

    st = _state(
        T=9.5,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 5, "processed_filled_amount": 50.0,
                   "applied_fill_fraction": 0.5, "terminal": True},
        },
    )
    orders = [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="부분체결", reverse=True)]
    reconcile_reverse_fills(st, orders)
    assert st["T"] == pytest.approx(9.5)
    assert not st.get("_settlement_hold")


def test_c_reverse_increase_from_50_to_100_moves_to_9_0():
    from state import reconcile_reverse_fills

    st = _state(
        T=9.5,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 5, "processed_filled_amount": 50.0,
                   "applied_fill_fraction": 0.5, "terminal": True},
        },
    )
    orders = [_row("R1", "매도", 10, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="체결", reverse=True)]
    reconcile_reverse_fills(st, orders)
    assert st["T"] == pytest.approx(9.0)


def test_d_late_buy_increase_after_sell_is_not_retroactive():
    st = {"T": 10.0}
    T, _ = _apply_buy_t_effect(st, 10.0, 0.2, "B1")
    T, _ = _apply_sell_t_effect(st, T, 20, "S1", 5)   # ×0.75 → 7.65
    assert T == pytest.approx(7.65)
    T, _ = _apply_buy_t_effect(st, T, 0.3, "B1")      # 매도 뒤 새 증분
    assert T == pytest.approx(7.95)


def test_e_reverse_lower_response_keeps_t_and_marker():
    from state import reconcile_reverse_fills

    st = _state(
        T=10.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
        },
    )
    reconcile_reverse_fills(
        st, [_row("R1", "매도", 10, "2026-10-21T08:16:50+00:00", price=10.0,
                  status="체결", reverse=True)]
    )
    assert st["T"] == pytest.approx(9.0)
    assert st["orders_meta"]["R1"]["applied_fill_fraction"] == pytest.approx(1.0)

    reconcile_reverse_fills(
        st, [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                  status="부분체결", reverse=True)]
    )
    assert st["T"] == pytest.approx(9.0)
    assert st["orders_meta"]["R1"]["applied_fill_fraction"] == pytest.approx(1.0)


def test_f_second_sell_basis_uses_holdings_after_first_sell():
    st = _state(
        T=10.0, position_qty=100, net_invested=1000.0,
        orders_meta={
            "S1": {"side": "SELL", "total_qty": 25, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "S2": {"side": "SELL", "total_qty": 50, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [
        _row("S1", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0),
        _row("S2", "매도", 50, "2026-10-21T10:00:00+00:00", price=10.0),
    ]
    before = {
        "T": 10.0, "net_invested": 1000.0, "last_updated": st["last_updated"],
        "position_qty": 100, "sell_t_effects": {},
        "settlement_ledger": {}, "orders_meta": deepcopy(st["orders_meta"]),
    }
    result = update_T_from_history("TQQQ", st, orders, balance_qty=25)
    assert result["sell_t_effects"]["S2"]["basis"] == 75
    assert result["T"] == pytest.approx(1.875)

    ev = trading_bot.build_settlement_evidence(
        "TQQQ", result, orders, mode="DRY", balance_qty=25, before=before,
    )
    assert verify_run._verify_evidence(ev) == []


def test_g_full_reinference_money_starts_from_zero():
    st = _state(
        T=0.0, last_updated="", net_invested=500.0, position_qty=None,
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [_row("B1", "매수", 10, "2026-10-20T09:00:00+00:00", price=10.0)]
    before = {
        "T": 0.0, "net_invested": 500.0, "last_updated": "",
        "position_qty": None, "sell_t_effects": {},
        "settlement_ledger": {}, "orders_meta": deepcopy(st["orders_meta"]),
    }
    inferred = update_T_from_history("TQQQ", st, orders, balance_qty=10)
    assert inferred["net_invested"] == pytest.approx(100.0)

    ev = trading_bot.build_settlement_evidence(
        "TQQQ", inferred, orders, mode="DRY", balance_qty=10, before=before,
    )
    assert ev["settlement_mode"] == "full_reinference"
    assert verify_run._verify_evidence(ev) == []


def test_h_repeated_results_use_independent_snapshots():
    st = _state(
        T=8.0, position_qty=100,
        orders_meta={"S": {"side": "SELL", "total_qty": 75, "t_target": 0.0,
                           "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    orders = [
        _row("OLD", "매수", 100, "2026-10-19T08:00:00+00:00"),
        _row("S", "매도", 75, "2026-10-21T09:00:00+00:00"),
    ]
    first = deepcopy(update_T_from_history("TQQQ", st, orders, balance_qty=25))
    first_T = first["T"]
    second = deepcopy(update_T_from_history("TQQQ", first, orders, balance_qty=25))
    assert first["T"] == pytest.approx(first_T)
    assert second["T"] == pytest.approx(first_T)


# ── 추가 오류 주입: 원장/마커 변조는 모두 검증 실패 ─────────────────
def test_verifier_injected_buy_increase_tamper_fails():
    ev = _settle_evidence()
    for e in ev["ledger_after"]["entries"]:
        if e.get("kind") == "buy":
            e["delta"] = float(e.get("delta", 0.0)) + 100.0
    problems = verify_run._verify_evidence(ev)
    assert any("원장" in p or "T 불일치" in p for p in problems), problems


def test_verifier_injected_reverse_delta_tamper_fails():
    st = _state(
        T=10.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
        },
    )
    orders = [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="부분체결", reverse=True)]
    before = {
        "T": 10.0, "net_invested": 1000.0, "last_updated": st["last_updated"],
        "position_qty": None, "sell_t_effects": {},
        "settlement_ledger": {}, "orders_meta": deepcopy(st["orders_meta"]),
    }
    result = update_T_from_history("TQQQ", st, orders, balance_qty=5)
    ev = trading_bot.build_settlement_evidence(
        "TQQQ", result, orders, mode="DRY", balance_qty=5, before=before,
    )
    assert verify_run._verify_evidence(ev) == []
    tampered = deepcopy(ev)
    for e in tampered["ledger_after"]["entries"]:
        if e.get("kind") == "reverse_sell":
            e["delta"] = -5.0
    problems = verify_run._verify_evidence(tampered)
    assert any("원장" in p or "T 불일치" in p for p in problems), problems


def test_verifier_injected_adjust_without_basis_fails():
    ev = _settle_evidence()
    ev["ledger_after"]["entries"].append(
        {"seq": 999, "kind": "adjust", "odno": "", "delta": 1.0}
    )
    problems = verify_run._verify_evidence(ev)
    assert any("adjust" in p for p in problems), problems


def test_verifier_injected_basis_unknown_fails():
    ev = _settle_evidence()
    ev["basis_unknown"] = True
    problems = verify_run._verify_evidence(ev)
    assert any("보류" in p or "검증 불가" in p for p in problems), problems


def test_verifier_injected_money_tamper_fails():
    ev = _settle_evidence()
    ev["net_invested_after"] = float(ev["net_invested_after"]) + 50.0
    problems = verify_run._verify_evidence(ev)
    assert any("순투입금 불일치" in p for p in problems), problems


def test_verifier_injected_marker_advance_fails():
    ev = _settle_evidence()
    ev["orders_meta_before"] = {
        "B1": {"processed_filled_qty": 5, "processed_filled_amount": 500.0,
               "total_qty": 10, "t_target": 1.0},
        "S1": {"processed_filled_qty": 0, "processed_filled_amount": 0.0,
               "total_qty": 25, "t_target": 0.0},
    }
    problems = verify_run._verify_evidence(ev)
    assert any("원장" in p or "T 불일치" in p or "T 델타" in p for p in problems), problems


def test_reverse_marker_ledger_mismatch_holds_without_mutation():
    from state import reconcile_reverse_fills

    st = _state(
        T=9.5,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 5, "processed_filled_amount": 50.0,
                   "applied_fill_fraction": 0.5, "terminal": True},
        },
        settlement_ledger={
            "start_T": 10.0,
            "next_seq": 2,
            "entries": [{"seq": 1, "kind": "reverse_sell", "odno": "R1",
                         "applied_before": 0.0, "fill_fraction": 0.5,
                         "delta": -1.0, "base_t": 10.0, "factor": 0.9}],
        },
    )
    before = deepcopy(st)
    orders = [_row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
                   status="부분체결", reverse=True)]
    reconcile_reverse_fills(st, orders)
    assert st.get("_settlement_hold") is True
    assert st["T"] == before["T"]
    assert st["orders_meta"] == before["orders_meta"]
    assert st["settlement_ledger"] == before["settlement_ledger"]


def test_full_reinference_ignores_previous_ledger_and_money_basis():
    st = _state(
        T=7.5, last_updated="", net_invested=500.0, position_qty=50,
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "OLD",
                         "basis": 50, "factor": 0.75, "cumulative_qty": 50}],
        },
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    orders = [_row("B1", "매수", 10, "2026-10-20T09:00:00+00:00", price=10.0)]
    before = {
        "T": 7.5, "net_invested": 500.0, "last_updated": "",
        "position_qty": 50, "sell_t_effects": {"OLD": {"basis": 50, "factor": 0.75}},
        "settlement_ledger": deepcopy(st["settlement_ledger"]),
        "orders_meta": deepcopy(st["orders_meta"]),
    }
    inferred = update_T_from_history("TQQQ", st, orders, balance_qty=10)
    ev = trading_bot.build_settlement_evidence(
        "TQQQ", inferred, orders, mode="DRY", balance_qty=10, before=before,
    )
    assert ev["settlement_mode"] == "full_reinference"
    assert inferred["net_invested"] == pytest.approx(100.0)
    assert verify_run._verify_evidence(ev) == []


def test_legacy_sell_reclassification_hold_restores_state():
    st = _state(
        T=7.5, position_qty=100,
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        orders_meta={"S": {"side": "SELL", "total_qty": 75, "t_target": 0.0,
                           "processed_filled_qty": 25, "processed_filled_amount": 250.0}},
    )
    before = deepcopy(st)
    orders = [_row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=25)

    assert result.get("_settlement_hold") is True
    assert result["T"] == before["T"]
    assert result["last_updated"] == before["last_updated"]
    assert result["sell_t_effects"] == before["sell_t_effects"]
    assert result["orders_meta"] == before["orders_meta"]
    assert result.get("settlement_ledger", {}) == before.get("settlement_ledger", {})


# ── 2차 계약: 배율 0 vs 실제 청산, T-only 복구, 필수 체결 집합 ──────
def _snapshot(state):
    return {
        "T": float(state.get("T", 0.0)),
        "net_invested": float(state.get("net_invested", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "last_processed_ordno": state.get("last_processed_ordno", ""),
        "position_qty": state.get("position_qty"),
        "sell_t_effects": deepcopy(state.get("sell_t_effects", {})),
        "settlement_ledger": deepcopy(state.get("settlement_ledger", {})),
        "orders_meta": deepcopy(state.get("orders_meta", {})),
    }


def _verify_after_settle(symbol, result, orders, before, balance_qty):
    ev = trading_bot.build_settlement_evidence(
        symbol, result, orders, mode="DRY",
        balance_qty=balance_qty, before=before,
    )
    return ev, verify_run._verify_evidence(ev)


def test_cycle_not_ended_when_zero_factor_but_position_remains(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(tmp_path / ".state.json"))
    st = _state(
        T=8.5, net_invested=950.0, position_qty=95,
        last_updated="2026-10-22T09:00:00+00:00",
        last_processed_ordno="B1",
        cycle_start_date="2026-10-20",
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        settlement_ledger={
            "start_T": 10.0, "next_seq": 3,
            "entries": [
                {"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
                 "factor": 0.75, "cumulative_qty": 25},
                {"seq": 2, "kind": "buy", "odno": "B1", "delta": 1.0},
            ],
        },
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 20, "t_target": 1.0,
                   "processed_filled_qty": 20, "processed_filled_amount": 200.0},
            "S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                  "processed_filled_qty": 25, "processed_filled_amount": 250.0},
        },
    )
    before = _snapshot(st)
    orders = [
        _row("B1", "매수", 20, "2026-10-22T09:00:00+00:00", price=10.0),
        _row("S", "매도", 100, "2026-10-23T09:00:00+00:00", price=10.0),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=20)

    assert result["net_invested"] == pytest.approx(200.0)
    assert result["cycle_start_date"] == "2026-10-20"
    assert result.get("position_qty") == 20
    assert not result.get("_completed_cycle_start")

    ev, problems = _verify_after_settle("TQQQ", result, orders, before, 20)
    assert problems == []
    assert ev["orders"][-1]["factor"] == pytest.approx(0.0)
    assert ev["net_invested_after"] == pytest.approx(200.0)

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")
    next_before = _snapshot(loaded)
    next_orders = orders + [
        _row("B2", "매수", 10, "2026-10-24T09:00:00+00:00", price=10.0),
    ]
    loaded["orders_meta"]["B2"] = {
        "side": "BUY", "total_qty": 10, "t_target": 1.0,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=30)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 30
    )
    assert next_problems == []
    assert after_next["net_invested"] == pytest.approx(300.0)


def test_t_only_recovery_without_explicit_adjust_holds():
    st = _state(
        T=9.0, net_invested=1000.0, position_qty=100,
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
                         "factor": 0.75, "cumulative_qty": 25}],
        },
        orders_meta={"B1": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                            "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    before = _snapshot(st)
    orders = [_row("B1", "매수", 1, "2026-10-21T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert result["T"] == before["T"]
    assert result["net_invested"] == before["net_invested"]


def test_t_only_recovery_with_explicit_adjust_reaches_ten(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(tmp_path / ".state.json"))
    from state import record_settlement_t_adjustment

    st = _state(
        T=9.0, net_invested=1000.0, position_qty=100,
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
                         "factor": 0.75, "cumulative_qty": 25}],
        },
        orders_meta={"B1": {"side": "BUY", "total_qty": 1, "t_target": 1.0,
                            "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    record_settlement_t_adjustment(
        st, 9.0, source="STATE_REPAIR_TARGET_T",
        at="2026-10-20T12:00:00+00:00",
    )
    before = _snapshot(st)
    orders = [_row("B1", "매수", 1, "2026-10-21T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(10.0)
    _, problems = _verify_after_settle("TQQQ", result, orders, before, 101)
    assert problems == []

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")
    next_before = _snapshot(loaded)
    next_orders = orders + [
        _row("B2", "매수", 10, "2026-10-22T09:00:00+00:00", price=10.0),
    ]
    loaded["orders_meta"]["B2"] = {
        "side": "BUY", "total_qty": 10, "t_target": 1.0,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=111)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 111
    )
    assert next_problems == []
    assert after_next["T"] == pytest.approx(11.0)


def _buy_settled_evidence():
    st = _state(
        T=10.0, net_invested=1000.0, position_qty=100,
        orders_meta={"B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                            "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    before = _snapshot(st)
    orders = [_row("B1", "매수", 10, "2026-10-21T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=110)
    ev, problems = _verify_after_settle("TQQQ", result, orders, before, 110)
    assert problems == []
    return ev


def test_verifier_requires_all_new_fills_even_without_ledger():
    ev = _buy_settled_evidence()
    tampered = deepcopy(ev)
    tampered["orders"] = []
    tampered["applied_seq"] = []
    tampered["ledger_after"] = {"start_T": ev["ledger_before"].get("start_T", 10.0),
                                "next_seq": 1, "entries": []}
    tampered["T_after"] = 10.0
    tampered["net_invested_after"] = 1000.0
    problems = verify_run._verify_evidence(tampered)
    assert any("체결" in p or "누락" in p or "원장" in p for p in problems), problems


def test_verifier_rejects_half_reflected_new_fill():
    ev = _buy_settled_evidence()
    tampered = deepcopy(ev)
    for order in tampered["orders"]:
        order["qty_delta"] = 5
        order["amount_delta"] = 50.0
        order["t_delta"] = 0.5
    for entry in tampered["ledger_after"]["entries"]:
        if entry.get("kind") == "buy":
            entry["delta"] = 0.5
    tampered["T_after"] = 10.5
    tampered["net_invested_after"] = 1050.0
    problems = verify_run._verify_evidence(tampered)
    assert problems, problems


def test_verifier_includes_t_less_additional_buy_in_money(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(tmp_path / ".state.json"))
    st = _state(
        T=5.0, net_invested=1000.0, position_qty=50,
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "OLD", "basis": 100,
                         "factor": 0.5, "cumulative_qty": 50}],
        },
        orders_meta={"A1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                            "is_additional": True,
                            "processed_filled_qty": 0, "processed_filled_amount": 0.0}},
    )
    before = _snapshot(st)
    orders = [_row("A1", "매수", 10, "2026-10-21T09:00:00+00:00", price=1.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=60)

    assert result["T"] == pytest.approx(5.0)
    assert result["net_invested"] == pytest.approx(1010.0)
    _, problems = _verify_after_settle("TQQQ", result, orders, before, 60)
    assert problems == []

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")
    next_before = _snapshot(loaded)
    next_orders = orders + [
        _row("A2", "매수", 5, "2026-10-22T09:00:00+00:00", price=1.0),
    ]
    loaded["orders_meta"]["A2"] = {
        "side": "BUY", "total_qty": 5, "t_target": 1.0, "is_additional": True,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=65)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 65
    )
    assert next_problems == []
    assert after_next["net_invested"] == pytest.approx(1015.0)


def test_full_reinference_keeps_sell_factor_zero_and_connects_next_cycle(tmp_path, monkeypatch):
    f = tmp_path / ".state.json"
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(f))

    st = _state(
        T=7.5, last_updated="", net_invested=500.0, position_qty=50,
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "OLD", "basis": 50,
                         "factor": 0.75, "cumulative_qty": 50}],
        },
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 100, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "S1": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "B2": {"side": "BUY", "total_qty": 20, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "B3": {"side": "BUY", "total_qty": 5, "t_target": 1.0,
                   "is_additional": True,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    before = _snapshot(st)
    orders = [
        _row("B1", "매수", 100, "2026-10-20T09:00:00+00:00", price=10.0),
        _row("S1", "매도", 100, "2026-10-21T09:00:00+00:00", price=10.0),
        _row("B2", "매수", 20, "2026-10-22T09:00:00+00:00", price=10.0),
        _row("B3", "매수", 5, "2026-10-22T10:00:00+00:00", price=10.0),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=25)
    assert result["T"] == pytest.approx(1.0)
    assert result["net_invested"] == pytest.approx(250.0)

    ev, problems = _verify_after_settle("TQQQ", result, orders, before, 25)
    assert problems == []
    sell_effects = [o for o in ev["orders"] if o.get("side") == "매도"]
    assert sell_effects and sell_effects[0]["factor"] == pytest.approx(0.0)

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")

    next_before = _snapshot(loaded)
    next_orders = orders + [
        _row("B4", "매수", 10, "2026-10-23T09:00:00+00:00", price=10.0),
    ]
    loaded["orders_meta"]["B4"] = {
        "side": "BUY", "total_qty": 10, "t_target": 1.0,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=35)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 35
    )
    assert next_problems == []
    assert after_next["net_invested"] == pytest.approx(350.0)


def test_legacy_same_class_sell_without_new_ledger_verifies():
    st = _state(
        T=7.5, net_invested=1000.0, position_qty=100,
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        orders_meta={"S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                           "processed_filled_qty": 25, "processed_filled_amount": 250.0}},
        last_updated="2026-10-21T08:00:00+00:00",
        last_processed_ordno="OLD",
    )
    before = _snapshot(st)
    orders = [_row("S", "매도", 35, "2026-10-22T09:00:00+00:00", price=10.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=65)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(7.5)
    _, problems = _verify_after_settle("TQQQ", result, orders, before, 65)
    assert problems == []


def test_cycle_same_day_uses_chronological_holdings():
    st = _state(
        T=7.5, net_invested=750.0, position_qty=75,
        last_updated="2026-10-21T08:00:00+00:00",
        last_processed_ordno="OLD",
        cycle_start_date="2026-10-20",
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
                         "factor": 0.75, "cumulative_qty": 25}],
        },
        orders_meta={
            "S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                  "processed_filled_qty": 25, "processed_filled_amount": 250.0},
            "B": {"side": "BUY", "total_qty": 20, "t_target": 1.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    before = _snapshot(st)
    orders = [
        _row("S", "매도", 100, "2026-10-22T10:00:00+00:00", price=10.0),
        _row("B", "매수", 20, "2026-10-22T09:00:00+00:00", price=10.0),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=20)

    assert result["net_invested"] == pytest.approx(200.0)
    assert result["cycle_start_date"] == "2026-10-20"
    assert result.get("position_qty") == 20
    assert not result.get("_completed_cycle_start")
    _, problems = _verify_after_settle("TQQQ", result, orders, before, 20)
    assert problems == []


def test_amount_only_correction_is_counted_without_t_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(tmp_path / ".state.json"))
    st = _state(
        T=11.0, net_invested=1000.0, position_qty=110,
        last_updated="2026-10-22T09:00:00+00:00",
        last_processed_ordno="B1",
        orders_meta={"B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                            "processed_filled_qty": 10,
                            "processed_filled_amount": 100.0}},
        settlement_ledger={
            "start_T": 10.0, "next_seq": 2,
            "entries": [{"seq": 1, "kind": "buy", "odno": "B1", "delta": 1.0}],
        },
    )
    before = _snapshot(st)
    orders = [_row("B1", "매수", 10, "2026-10-21T09:00:00+00:00", price=11.0)]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=110)

    assert result["T"] == pytest.approx(11.0)
    assert result["net_invested"] == pytest.approx(1010.0)
    ev, problems = _verify_after_settle("TQQQ", result, orders, before, 110)
    assert problems == []
    assert ev["orders"][0]["qty_delta"] == 0
    assert ev["orders"][0]["amount_delta"] == pytest.approx(10.0)

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")
    next_before = _snapshot(loaded)
    next_orders = orders + [
        _row("B2", "매수", 5, "2026-10-23T09:00:00+00:00", price=10.0),
    ]
    loaded["orders_meta"]["B2"] = {
        "side": "BUY", "total_qty": 5, "t_target": 1.0,
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
    }
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=115)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 115
    )
    assert next_problems == []


# ═══════════════════════════════════════════════════════════════════
# 결함 1 — 빈 원장의 시작점 불일치 검사
# ═══════════════════════════════════════════════════════════════════
def _buy_meta(qty=1, t_target=1.0):
    return {
        "side": "BUY",
        "total_qty": qty,
        "t_target": t_target,
        "processed_filled_qty": 0,
        "processed_filled_amount": 0.0,
    }


def _buy_only_state(T, ledger_marker=None, **kw):
    """정상모드에서 매수 +1 을 반영할 수 있는 최소 상태."""
    st = _state(
        T=T,
        net_invested=1000.0,
        position_qty=100,
        orders_meta={"B1": _buy_meta(1, 1.0)},
        **kw,
    )
    if ledger_marker is not None:
        st["settlement_ledger"] = deepcopy(ledger_marker)
    return st


def _buy_order():
    return _row("B1", "매수", 1, "2026-10-21T09:00:00+00:00", price=10.0)


def _state_snapshot_for_hold(state):
    return {
        "T": float(state.get("T", 0.0)),
        "net_invested": float(state.get("net_invested", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "last_processed_ordno": state.get("last_processed_ordno", ""),
        "position_qty": state.get("position_qty"),
        "sell_t_effects": deepcopy(state.get("sell_t_effects", {})),
        "settlement_ledger": deepcopy(state.get("settlement_ledger", {})),
        "orders_meta": deepcopy(state.get("orders_meta", {})),
    }


def test_ledger_absent_keeps_legacy_transition_and_buys():
    """원장 없음 → 기존 전환 규칙(마커/부트스트랩)으로 정상 반영."""
    st = _buy_only_state(T=7.5)
    assert "settlement_ledger" not in st
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(8.5)
    assert result["settlement_ledger"]["entries"][-1]["kind"] == "buy"


def test_empty_ledger_matching_basis_applies_buy():
    """빈 원장 + 기준 T=7.5 와 현재 T=7.5 일치 → 매수 +1 = 8.5."""
    ledger = {"start_T": 7.5, "next_seq": 1, "entries": []}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(8.5)
    assert result["settlement_ledger"]["start_T"] == pytest.approx(7.5)
    assert [e["kind"] for e in result["settlement_ledger"]["entries"]] == ["buy"]


def test_empty_ledger_mismatched_basis_holds_and_preserves_everything():
    """빈 원장 기준 7.5 vs 현재 T 9.0 → 보류, 상태 전체(금액·마커·워터마크·원장) 보존."""
    ledger = {"start_T": 7.5, "next_seq": 1, "entries": []}
    st = _buy_only_state(T=9.0, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert "원장" in result.get("_settlement_hold_reason", "") or "기준" in result.get("_settlement_hold_reason", "")
    assert _state_snapshot_for_hold(result) == before
    # 불일치 시 자동 adjust 생성 금지
    assert all(e.get("kind") != "adjust" for e in result["settlement_ledger"]["entries"])


def test_ledger_entries_replay_mismatch_holds():
    """항목 원장 재생값 7.5 vs 현재 T 9.0 → 보류."""
    ledger = {
        "start_T": 10.0,
        "next_seq": 2,
        "entries": [
            {"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
             "factor": 0.75, "cumulative_qty": 25},
        ],
    }
    st = _buy_only_state(T=9.0, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert _state_snapshot_for_hold(result) == before


def test_zero_basis_is_valid_and_new_buy_moves_t():
    """기준 T=0 은 정상값 — 필드 없음/None/잘못된 값과 구분한다."""
    ledger = {"start_T": 0.0, "next_seq": 1, "entries": []}
    st = _buy_only_state(T=0.0, ledger_marker=ledger)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(1.0)
    assert result["settlement_ledger"]["start_T"] == pytest.approx(0.0)


def test_empty_ledger_without_basis_starts_from_current_t():
    """기준 없음(None) + 항목 없음 → 현재 T 를 새 시작점으로 초기화 후 정상 반영."""
    ledger = {"start_T": None, "next_seq": 1, "entries": []}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["settlement_ledger"]["start_T"] == pytest.approx(7.5)
    assert result["T"] == pytest.approx(8.5)


def test_ledger_entries_without_basis_holds_even_if_t_matches_replay():
    """항목이 있지만 기준 T 가 없으면 재생값이 우연히 T 와 같아도 보류."""
    ledger = {
        "next_seq": 2,
        "entries": [{"seq": 1, "kind": "buy", "odno": "OLD", "delta": 1.0}],
    }
    st = _buy_only_state(T=1.0, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert "기준" in result.get("_settlement_hold_reason", "")
    assert _state_snapshot_for_hold(result) == before


def test_invalid_basis_format_holds_without_coercing_to_zero():
    """start_T 형식 오류(문자열) → 0 으로 대체하지 않고 보류."""
    ledger = {"start_T": "not-a-number", "next_seq": 1, "entries": []}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert _state_snapshot_for_hold(result) == before
    assert result["settlement_ledger"]["start_T"] == "not-a-number"


def test_invalid_entries_format_holds_instead_of_crashing_or_zeroing():
    """entries 가 리스트가 아니면 형식 오류로 보류 (0 대체/예외 금지)."""
    ledger = {"start_T": 7.5, "next_seq": 1, "entries": "broken"}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert _state_snapshot_for_hold(result) == before


def test_non_dict_ledger_entry_holds():
    ledger = {"start_T": 7.5, "next_seq": 2, "entries": ["broken"]}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert result.get("_settlement_hold") is True
    assert _state_snapshot_for_hold(result) == before


def test_empty_ledger_basis_tolerance_boundary():
    """허용 오차는 기존 정밀도(0.005) — 경계값 포함, 초과 시 보류."""
    within = _buy_only_state(
        T=7.505, ledger_marker={"start_T": 7.5, "next_seq": 1, "entries": []}
    )
    result = update_T_from_history("TQQQ", within, [_buy_order()], balance_qty=101)
    assert not result.get("_settlement_hold")
    # 허용 오차 안이면 원장 기준 T(7.5)가 정본이므로 매수 후 8.5.
    assert result["T"] == pytest.approx(8.5)

    beyond = _buy_only_state(
        T=7.5051, ledger_marker={"start_T": 7.5, "next_seq": 1, "entries": []}
    )
    before = _state_snapshot_for_hold(beyond)
    held = update_T_from_history("TQQQ", beyond, [_buy_order()], balance_qty=101)
    assert held.get("_settlement_hold") is True
    assert _state_snapshot_for_hold(held) == before


def test_explicit_t_adjustment_then_buy_reaches_ten_and_persists():
    """명시적 보정 7.5→9.0(adjust 원장) 후 매수 +1 → T=10.0."""
    from state import record_settlement_t_adjustment

    ledger = {"start_T": 7.5, "next_seq": 1, "entries": []}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    record_settlement_t_adjustment(
        st, 9.0, source="STATE_REPAIR_TARGET_T", at="2026-10-20T12:00:00+00:00",
    )
    assert st["T"] == pytest.approx(9.0)
    before = _state_snapshot_for_hold(st)

    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(10.0)
    assert result["settlement_ledger"]["entries"][-1]["kind"] == "buy"
    from state import _replay_settlement_ledger
    assert _replay_settlement_ledger(result) == pytest.approx(10.0)
    # 정산 직전 스냅샷은 보정 완료 상태를 그대로 담는다 (adjust 보존)
    assert before["T"] == pytest.approx(9.0)
    assert any(e.get("kind") == "adjust" for e in before["settlement_ledger"]["entries"])


def test_empty_ledger_without_start_key_starts_from_current_t():
    """start_T 필드 자체가 없고 항목도 없으면 현재 T 로 새 원장을 시작한다."""
    ledger = {"next_seq": 1, "entries": []}
    st = _buy_only_state(T=7.5, ledger_marker=ledger)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["settlement_ledger"]["start_T"] == pytest.approx(7.5)
    assert result["T"] == pytest.approx(8.5)


def test_ledger_with_negative_delta_entries_is_valid():
    """reverse_sell/adjust 의 음수 delta 는 정상 — 형식 오류로 보류하면 안 된다."""
    ledger = {
        "start_T": 10.0,
        "next_seq": 3,
        "entries": [
            {"seq": 1, "kind": "reverse_sell", "odno": "R1",
             "delta": -0.5, "base_t": 10.0, "factor": 0.9},
            {"seq": 2, "kind": "adjust", "odno": "", "delta": -0.5,
             "target_T": 9.0, "replay_T": 9.5},
        ],
    }
    st = _buy_only_state(T=9.0, ledger_marker=ledger)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(10.0)
    assert result["settlement_ledger"]["entries"][-1]["kind"] == "buy"


def test_intentional_full_reinference_is_not_blocked_by_stale_ledger():
    """T 오추정 복구(T-estimation-suspected-low)는 의도적 재구성 — 원장 점검 제외."""
    st = _state(
        T=0.0,
        net_invested=500.0,
        balance_mismatch={"note": "T-estimation-suspected-low"},
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 100, "t_target": 1.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
            "S1": {"side": "SELL", "total_qty": 20, "t_target": 0.0,
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
        settlement_ledger={
            "start_T": 10.0,
            "next_seq": 2,
            "entries": [{"seq": 1, "kind": "sell", "odno": "OLD", "basis": 50,
                         "factor": 0.75, "cumulative_qty": 50}],
        },
    )
    orders = [
        _row("B1", "매수", 100, "2026-10-20T09:00:00+00:00", price=10.0),
        _row("S1", "매도", 20, "2026-10-21T10:00:00+00:00", price=10.0),
    ]
    result = update_T_from_history("TQQQ", st, orders, balance_qty=80)

    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(0.75)
    assert result["settlement_ledger"]["start_T"] == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════
# 결함 2 — 로그 T 전이 검증기 (현재 매도 로그 형식)
# ═══════════════════════════════════════════════════════════════════
def _log_section(symbol, t_before, watermark, output):
    return (
        f"\n{'=' * 60}\n"
        f"종목 처리 시작: {symbol} (NAS)\n"
        f"{'=' * 60}\n"
        f"[상태] {symbol} 상태 로드 완료 → T={t_before}, 마지막 갱신: {watermark}\n"
        f"{output}"
    )


def _settle_and_log(capsys, symbol, st, orders, balance_qty):
    t_before = st["T"]
    watermark = st["last_updated"]
    result = update_T_from_history(symbol, st, orders, balance_qty=balance_qty)
    output = capsys.readouterr().out
    return _log_section(symbol, t_before, watermark, output), result


def _sell_state(T, position_qty, qty, meta_total=None, **kw):
    return _state(
        T=T,
        position_qty=position_qty,
        net_invested=1000.0,
        orders_meta={
            "S": {
                "side": "SELL", "total_qty": meta_total or position_qty,
                "t_target": 0.0, "processed_filled_qty": 0,
                "processed_filled_amount": 0.0,
            }
        },
        **kw,
    )


def test_log_verifier_accepts_current_quarter_sell(capsys):
    st = _sell_state(10.0, 100, 25)
    log, result = _settle_and_log(
        capsys, "TQQQ", st,
        [_row("S", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0)],
        75,
    )
    assert "쿼터매도 (비율=0.25, 기준 100주, 누적 25주, 보유 75주)" in log
    assert result["T"] == pytest.approx(7.5)
    assert verify_run._verify_t_transitions(log, "TQQQ", [], 75) == []


def test_real_settlement_log_and_evidence_both_pass(capsys):
    """실제 정산 출력(로그)과 근거 파일 검증이 같은 실행에서 모두 통과한다."""
    st = _sell_state(10.0, 100, 25)
    before = _snapshot(st)
    orders = [_row("S", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0)]
    log, result = _settle_and_log(capsys, "TQQQ", st, orders, 75)

    evidence = trading_bot.build_settlement_evidence(
        "TQQQ", result, orders, mode="DRY", balance_qty=75, before=before,
    )
    assert verify_run._verify_evidence(evidence) == []
    assert verify_run._verify_t_transitions(log, "TQQQ", [], 75) == []


def test_log_verifier_accepts_current_target_and_full_sell(capsys):
    target_state = _sell_state(10.0, 100, 75)
    target_log, target = _settle_and_log(
        capsys, "TQQQ", target_state,
        [_row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0)],
        25,
    )
    assert "목표매도 (비율=0.75, 기준 100주, 누적 75주, 보유 25주)" in target_log
    assert target["T"] == pytest.approx(2.5)
    assert verify_run._verify_t_transitions(target_log, "TQQQ", [], 25) == []

    full_state = _sell_state(10.0, 100, 100)
    full_log, full = _settle_and_log(
        capsys, "TQQQ", full_state,
        [_row("S", "매도", 100, "2026-10-21T09:00:00+00:00", price=10.0)],
        0,
    )
    assert "전량매도 (비율=1.00, 기준 100주, 누적 100주, 보유 0주)" in full_log
    assert full["T"] == pytest.approx(0.0)
    assert verify_run._verify_t_transitions(full_log, "TQQQ", [], 0) == []


def test_log_verifier_accepts_mixed_buy_and_sell(capsys):
    st = _state(
        T=10.0, position_qty=100, net_invested=1000.0,
        orders_meta={
            "B1": _buy_meta(10, 1.0),
            "S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                  "processed_filled_qty": 0, "processed_filled_amount": 0.0},
        },
    )
    # 매도(09:00) 후 매수(10:00) — 같은 묶음에서도 순서가 분명한 시나리오.
    orders = [
        _row("S", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0),
        _row("B1", "매수", 10, "2026-10-21T10:00:00+00:00", price=10.0),
    ]
    log, result = _settle_and_log(capsys, "TQQQ", st, orders, 85)
    # 매도 정산 후 매수가 뒤따릅니다 (기존 계약).
    assert result["T"] == pytest.approx(8.5)
    assert "쿼터매도 (비율=0.25, 기준 100주, 누적 25주, 보유 75주) → T=7.5" in log
    assert "→ ΔT=1.0 → T=8.5" in log
    assert verify_run._verify_t_transitions(log, "TQQQ", [], 85) == []


def test_log_verifier_accepts_legacy_simple_sell_format():
    log = _log_section(
        "TQQQ", 10.0, "2026-10-21T09:00:00+00:00",
        "  → 매도 체결 (20261021): 쿼터매도 (비율=0.25) → T=7.5\n"
        "[상태] TQQQ T값 업데이트 완료 → T=7.5, 순투입 $750.00\n",
    )
    assert verify_run._verify_t_transitions(log, "TQQQ", [], 75) == []

    legacy_with_holding = _log_section(
        "TQQQ", 10.0, "2026-10-21T09:00:00+00:00",
        "  → 매도 체결 (20261021): 쿼터매도 (비율=0.25, 보유 75주) → T=7.5\n"
        "[상태] TQQQ T값 업데이트 완료 → T=7.5, 순투입 $750.00\n",
    )
    assert verify_run._verify_t_transitions(
        legacy_with_holding, "TQQQ", [], 75
    ) == []


def test_log_verifier_accepts_late_buy_and_sell(capsys):
    late_buy_state = _state(
        T=10.0, position_qty=100, net_invested=1000.0,
        last_updated="2026-10-21T09:00:00+00:00",
        orders_meta={
            "B1": {"side": "BUY", "total_qty": 10, "t_target": 1.0,
                   "processed_filled_qty": 5, "processed_filled_amount": 50.0},
        },
    )
    late_buy_log, late_buy = _settle_and_log(
        capsys, "TQQQ", late_buy_state,
        [_row("B1", "매수", 10, "2026-10-20T09:00:00+00:00", price=10.0)],
        105,
    )
    assert "지연 체결 반영" in late_buy_log
    assert late_buy["T"] == pytest.approx(10.5)
    assert verify_run._verify_t_transitions(late_buy_log, "TQQQ", [], 105) == []

    late_sell_state = _state(
        T=7.5, position_qty=100, net_invested=1000.0,
        last_updated="2026-10-21T09:00:00+00:00",
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        orders_meta={
            "S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                  "processed_filled_qty": 5, "processed_filled_amount": 50.0},
        },
    )
    late_sell_log, late_sell = _settle_and_log(
        capsys, "TQQQ", late_sell_state,
        [_row("S", "매도", 25, "2026-10-20T09:00:00+00:00", price=10.0)],
        75,
    )
    assert "지연 체결 반영: odno=S 매도 20주 추가 (누적 25주) → T=7.5" in late_sell_log
    assert late_sell["T"] == pytest.approx(7.5)
    assert verify_run._verify_t_transitions(late_sell_log, "TQQQ", [], 75) == []


def test_log_verifier_accepts_reverse_lines_without_false_alarm(capsys):
    st = _state(
        T=10.0, position_qty=100, net_invested=1000.0,
        reverse_mode={"active": True, "cycle_id": "C1"},
        orders_meta={
            "R1": {"reverse_action": "sell", "cycle_id": "C1", "reverse_day": 1,
                   "reverse_t_factor": 0.9, "reverse_base_t": 10.0, "total_qty": 10,
                   "submitted_at": "", "submitted_session": "",
                   "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                   "applied_fill_fraction": 0.0},
            "B1": _buy_meta(1, 1.0),
        },
    )
    orders = [
        _row("R1", "매도", 5, "2026-10-21T08:16:50+00:00", price=10.0,
             status="부분체결", reverse=True),
        _row("B1", "매수", 1, "2026-10-21T09:00:00+00:00", price=10.0),
    ]
    log, result = _settle_and_log(capsys, "TQQQ", st, orders, 96)
    assert "→ 리버스 매도 체결 반영:" in log
    assert result["T"] == pytest.approx(10.5)  # 10→9.5 (리버스) →10.5 (매수)
    assert verify_run._verify_t_transitions(log, "TQQQ", [], 96) == []


def test_log_verifier_separates_symbols(capsys):
    tqqq = _sell_state(10.0, 100, 25)
    tqqq_log, _ = _settle_and_log(
        capsys, "TQQQ", tqqq,
        [_row("S", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0)], 75,
    )
    soxl = _sell_state(10.0, 100, 75)
    soxl_log, _ = _settle_and_log(
        capsys, "SOXL", soxl,
        [_row("S", "매도", 75, "2026-10-21T09:00:00+00:00", price=10.0)], 25,
    )
    combined = tqqq_log + soxl_log
    assert verify_run._verify_t_transitions(combined, "TQQQ", [], 75) == []
    assert verify_run._verify_t_transitions(combined, "SOXL", [], 25) == []

    # SOXL 구간만 변조 — TQQQ 검증은 영향을 받지 않아야 한다.
    broken_soxl = combined.replace(
        "[상태] SOXL T값 업데이트 완료 → T=2.5",
        "[상태] SOXL T값 업데이트 완료 → T=3.0",
    )
    assert verify_run._verify_t_transitions(broken_soxl, "TQQQ", [], 75) == []
    assert verify_run._verify_t_transitions(broken_soxl, "SOXL", [], 25) != []


def test_log_verifier_detects_tampered_final_t(capsys):
    st = _sell_state(10.0, 100, 25)
    log, _ = _settle_and_log(
        capsys, "TQQQ", st,
        [_row("S", "매도", 25, "2026-10-21T09:00:00+00:00", price=10.0)], 75,
    )
    tampered = log.replace(
        "[상태] TQQQ T값 업데이트 완료 → T=7.5",
        "[상태] TQQQ T값 업데이트 완료 → T=8.0",
    )
    problems = verify_run._verify_t_transitions(tampered, "TQQQ", [], 75)
    assert any("최종 T" in p or "전이" in p for p in problems), problems


def test_log_verifier_detects_transition_and_completion_mismatch(capsys):
    st = _state(
        T=10.0, position_qty=100, net_invested=1000.0,
        orders_meta={"B1": _buy_meta(10, 1.0)},
    )
    log, _ = _settle_and_log(
        capsys, "TQQQ", st,
        [_row("B1", "매수", 10, "2026-10-21T09:00:00+00:00", price=10.0)], 110,
    )
    tampered = log.replace("→ ΔT=1.0 → T=11.0", "→ ΔT=1.0 → T=12.0")
    problems = verify_run._verify_t_transitions(tampered, "TQQQ", [], 110)
    assert any("전이" in p or "ΔT" in p for p in problems), problems


def test_log_verifier_marks_unparseable_sell_instead_of_passing():
    """T 가 그대로여도 해석 불가 형식은 '로그 검증 불가'로 실패해야 한다."""
    log = _log_section(
        "TQQQ", 10.0, "2026-10-21T09:00:00+00:00",
        "  → 매도 체결 (20261021): 알수없는형식 → T=10.0\n"
        "[상태] TQQQ T값 업데이트 완료 → T=10.0, 순투입 $1000.00\n",
    )
    problems = verify_run._verify_t_transitions(log, "TQQQ", [], 100)
    assert any("로그 검증 불가" in p for p in problems), problems


# ═══════════════════════════════════════════════════════════════════
# 3차 계약: 지연 매도 실제 청산 판정 / 레거시 → 복구 전이
# ═══════════════════════════════════════════════════════════════════
def _late_sell_state(T=7.5):
    """워터마크 이전 매도(S)가 25/100 만 반영된 상태.

    주문 시각은 워터마크보다 이전으로 고정되어 있고, 이번 실행에서는 누적 체결만
    증가합니다 (지연 체결 경로).
    """
    return _state(
        T=T,
        net_invested=1000.0,
        position_qty=100,
        last_updated="2026-10-22T09:00:00+00:00",
        last_processed_ordno="S",
        cycle_start_date="2026-10-20",
        sell_t_effects={"S": {"basis": 100, "factor": 0.75}},
        settlement_ledger={
            "start_T": 10.0,
            "next_seq": 2,
            "entries": [
                {"seq": 1, "kind": "sell", "odno": "S", "basis": 100,
                 "factor": 0.75, "cumulative_qty": 25},
            ],
        },
        orders_meta={
            "S": {"side": "SELL", "total_qty": 100, "t_target": 0.0,
                  "processed_filled_qty": 25, "processed_filled_amount": 250.0},
        },
    )


def _late_sell_orders():
    return [
        _row("OLD", "매수", 100, "2026-10-19T09:00:00+00:00", price=10.0),
        _row("S", "매도", 100, "2026-10-21T09:00:00+00:00", price=10.0),
    ]


def test_late_sell_liquidates_only_when_actual_position_is_zero():
    """누적 100% 지연 매도라도 실제 보유가 0 일 때만 사이클을 종료합니다."""
    orders = _late_sell_orders()
    st = _late_sell_state()
    before = _snapshot(st)
    result = update_T_from_history("TQQQ", st, orders, balance_qty=0)

    assert result["T"] == pytest.approx(0.0)
    assert result["net_invested"] == pytest.approx(0.0)
    assert result["cycle_start_date"] == ""
    assert result.get("_completed_cycle_start") == "2026-10-20"
    assert result.get("sell_t_effects") == {}
    assert result.get("position_qty") == 0

    _, problems = _verify_after_settle("TQQQ", result, orders, before, 0)
    assert problems == [], problems


def test_late_sell_zero_factor_with_remaining_position_does_not_end_cycle():
    """누적 100%라도 보유가 남으면 T 효과만 반영하고 순투입금만 차감합니다."""
    orders = _late_sell_orders()
    st = _late_sell_state()
    before = _snapshot(st)
    result = update_T_from_history("TQQQ", st, orders, balance_qty=10)

    assert result["T"] == pytest.approx(0.0)
    assert result["net_invested"] == pytest.approx(250.0)
    assert result["cycle_start_date"] == "2026-10-20"
    assert not result.get("_completed_cycle_start")
    assert result["sell_t_effects"]["S"]["factor"] == pytest.approx(0.0)
    assert result.get("position_qty") == 10

    _, problems = _verify_after_settle("TQQQ", result, orders, before, 10)
    assert problems == [], problems


def test_legacy_t_adjustment_without_ledger_then_next_settlement_proceeds(
    tmp_path, monkeypatch,
):
    """원장이 없던 레거시 상태의 T-only 복구도 다음 정산을 보류시키지 않습니다."""
    monkeypatch.setattr(state_mod, "_STATE_FILE", str(tmp_path / ".state.json"))
    from state import record_settlement_t_adjustment

    # (A) 원장 자체가 없는 레거시 상태
    st = _buy_only_state(T=7.5)
    assert "settlement_ledger" not in st
    record_settlement_t_adjustment(
        st, 9.0, source="STATE_REPAIR_TARGET_T",
        at="2026-10-20T12:00:00+00:00",
    )
    ledger = st["settlement_ledger"]
    assert ledger["start_T"] == pytest.approx(7.5)
    assert ledger["entries"][0]["kind"] == "adjust"
    assert ledger["entries"][0]["delta"] == pytest.approx(1.5)
    assert st["T"] == pytest.approx(9.0)

    before = _snapshot(st)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)
    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(10.0)
    _, problems = _verify_after_settle("TQQQ", result, [_buy_order()], before, 101)
    assert problems == [], problems

    save_state("TQQQ", result)
    loaded = load_state("TQQQ")
    next_before = _snapshot(loaded)
    next_orders = [
        _buy_order(),
        _row("B2", "매수", 10, "2026-10-22T09:00:00+00:00", price=10.0),
    ]
    loaded["orders_meta"]["B2"] = _buy_meta(10, 1.0)
    after_next = update_T_from_history("TQQQ", loaded, next_orders, balance_qty=111)
    assert not after_next.get("_settlement_hold")
    assert after_next["T"] == pytest.approx(11.0)
    _, next_problems = _verify_after_settle(
        "TQQQ", after_next, next_orders, next_before, 111
    )
    assert next_problems == [], next_problems


def test_legacy_ledger_entries_without_start_t_are_rebased(monkeypatch):
    """항목만 있고 start_T 가 없는 원장도 현재 T 를 기준으로 보정합니다."""
    from state import record_settlement_t_adjustment

    legacy_ledger = {
        "next_seq": 2,
        "entries": [{"seq": 1, "kind": "buy", "odno": "OLD", "delta": 0.5}],
    }
    st = _buy_only_state(T=7.5, ledger_marker=legacy_ledger)
    record_settlement_t_adjustment(
        st, 9.0, source="STATE_REPAIR_TARGET_T",
        at="2026-10-20T12:00:00+00:00",
    )

    ledger = st["settlement_ledger"]
    assert ledger["start_T"] == pytest.approx(7.5)
    assert st["T"] == pytest.approx(9.0)

    before = _snapshot(st)
    result = update_T_from_history("TQQQ", st, [_buy_order()], balance_qty=101)
    assert not result.get("_settlement_hold")
    assert result["T"] == pytest.approx(10.0)
    _, problems = _verify_after_settle("TQQQ", result, [_buy_order()], before, 101)
    assert problems == [], problems

