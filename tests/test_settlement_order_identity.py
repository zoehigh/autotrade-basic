"""
주문번호 재사용에 따른 독립 정산 검증 회귀 테스트.

NHPlug SOXL 3357 사고(11-12 매도 4주/$260.76 + 11-24 매수 1주/$32.53)를
합성 fixture 로 재현해 **다른 주문의 마커를 차용하지 않는지** 검증합니다.

검증기는 정산 처리기(state.py)를 재사용하지 않고 독립 구현을 유지합니다.
실제 Evidence·실제 계좌 데이터는 사용하지 않습니다.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "ops"))

import verify_run  # noqa: E402

WM = "2026-11-24T19:10:00+00:00"


def _fill(odno, side, qty, amount, ts, **extra):
    row = {
        "odno": str(odno),
        "side": side,
        "qty": qty,
        "amount": amount,
        "ts": ts,
        "amount_confirmed": True,
        "processed_filled_qty": 0,
        "processed_filled_amount": 0.0,
    }
    row.update(extra)
    return row


def _meta(session, qty=0, amount=0.0, side="BUY", **extra):
    row = {
        "side": side,
        "submitted_session": session,
        "submitted_at": session.replace("-", "") + "052359",
        "processed_filled_qty": qty,
        "processed_filled_amount": amount,
        "fill_marker_version": 1,
    }
    row.update(extra)
    return row


def _ev(**overrides):
    ev = {
        "symbol": "SOXL",
        "run_id": "synthetic",
        "retry_num": "0",
        "mode": "DRY",
        "broker": "nhplug",
        "broker_mode": "demo",
        "evidence_version": 2,
        "settlement_mode": "incremental",
        "balance_qty": 49,
        "basis_unknown": False,
        "pending_marker_odnos": [],
        "T_before": 19.6562,
        "net_invested_before": 5249.61,
        "T_after": 19.7422,
        "net_invested_after": 5280.88,
        "watermark_before": WM,
        "last_processed_ordno_before": "3365",
        "orders_meta_before": {},
        "orders_meta_after": {},
        "sell_t_effects_before": {},
        "ledger_before": {"start_T": 13.25, "entries": []},
        "ledger_after": {"start_T": 13.25, "next_seq": 20, "entries": []},
        "orders": [],
        "fills": [],
        "skipped_unverified": [],
        "log_salt": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    }
    ev.update(overrides)
    return ev


# ───────────────────────────────────────────────────────────────────
# A. 주문 인스턴스 식별
# ───────────────────────────────────────────────────────────────────
def test_nhplug_3357_reuse_no_phantom_fill():
    """11-12 매도 마커에 11-24 매수 마커를 차용해 3주/$228.23을 만들지 않습니다."""
    ev = _ev(
        fills=[
            _fill("3357", "매도", 4, 260.76, "2026-11-12T19:10:00+00:00"),
            _fill("3357", "매수", 1, 32.53, "2026-11-24T19:10:00+00:00"),
            _fill("4822", "매수", 1, 31.27, "2026-11-25T19:10:00+00:00", is_additional=True),
        ],
        orders_meta_before={
            "3357": _meta("2026-11-24", qty=1, amount=32.53, reverse_action="buy"),
            "4822": _meta("2026-11-25", reverse_action="buy"),
        },
        orders=[{"odno": "4822", "side": "매수", "qty_delta": 1, "amount_delta": 31.27}],
    )
    problems, required, money = verify_run._required_settlement_fills(ev)
    rows = [r for r in required if r["odno"] == "3357"]
    assert not any(r["req_qty"] == 3 and abs(r["req_amount"] - 228.23) < 0.01 for r in rows)
    sell = next(r for r in rows if r["side"] == "매도")
    buy = next(r for r in rows if r["side"] == "매수")
    assert sell["req_qty"] == 0  # 워터마크 이전 — 이미 반영
    assert buy["req_qty"] == 0  # 11-24 매수 마커로 이미 반영
    new = next(r for r in required if r["odno"] == "4822")
    assert new["req_qty"] == 1 and abs(new["req_amount"] - 31.27) < 0.01
    assert abs(money - 5280.88) < 0.05
    assert not problems


def test_same_odno_same_side_different_dates():
    """같은 방향·같은 번호라도 다른 거래일이면 마커를 차용하지 않습니다."""
    ev = _ev(
        fills=[
            _fill("7000", "매수", 1, 10.0, "2026-11-24T19:10:00+00:00"),
            _fill("7000", "매수", 1, 12.0, "2026-11-25T19:10:00+00:00"),
        ],
        orders_meta_before={"7000": _meta("2026-11-24", qty=1, amount=10.0)},
    )
    _problems, required, _money = verify_run._required_settlement_fills(ev)
    rows = sorted(required, key=lambda r: str(r.get("ts") or ""))
    assert rows[0]["req_qty"] == 0  # 11-24 — 마커 일치
    assert rows[1]["req_qty"] == 1 and abs(rows[1]["req_amount"] - 12.0) < 0.01


def test_normal_partial_fill_delta_preserved():
    """같은 주문의 누적 부분체결 증가는 기존 델타 규칙을 그대로 유지합니다."""
    ev = _ev(
        fills=[_fill("7100", "매수", 4, 40.0, "2026-11-24T19:10:00+00:00")],
        orders_meta_before={
            "7100": _meta("2026-11-24", qty=1, amount=10.0, total_qty=4)
        },
    )
    _problems, required, _money = verify_run._required_settlement_fills(ev)
    row = required[0]
    assert row["req_qty"] == 3 and abs(row["req_amount"] - 30.0) < 0.01


def test_past_applied_and_new_unapplied_reuse():
    """과거 반영분과 신규 미반영분이 같은 번호여도 각각 판정합니다."""
    ev = _ev(
        watermark_before="2026-11-23T19:10:00+00:00",
        fills=[
            _fill("7200", "매도", 2, 100.0, "2026-11-20T19:10:00+00:00"),
            _fill("7200", "매도", 3, 150.0, "2026-11-25T19:10:00+00:00"),
        ],
        orders_meta_before={
            "7200": _meta("2026-11-20", qty=2, amount=100.0, side="SELL")
        },
    )
    _problems, required, _money = verify_run._required_settlement_fills(ev)
    rows = sorted(required, key=lambda r: str(r.get("ts") or ""))
    assert rows[0]["req_qty"] == 0
    assert rows[1]["req_qty"] == 3 and abs(rows[1]["req_amount"] - 150.0) < 0.01


def test_indeterminate_instance_fail_closed():
    """대응 인스턴스를 확정할 수 없으면 마커를 대조하지 않고 검증 불가로 막습니다."""
    fill_index = verify_run._fill_index_by_odno(
        [
            _fill("7300", "매도", 4, 260.76, "2026-11-12T19:10:00+00:00"),
            _fill("7300", "매수", 1, 32.53, "2026-11-24T19:10:00+00:00"),
        ]
    )
    meta = _meta("2026-11-23")  # 두 행 모두와 불일치
    assert verify_run._match_fill_for_meta("7300", meta, fill_index) is None
    ev = _ev(
        fills=fill_index["7300"],
        orders_meta_before={"7300": meta},
        orders_meta_after={"7300": _meta("2026-11-23", qty=9, amount=999.0)},
        orders=[{"odno": "7300", "side": "매수", "qty_delta": 1, "amount_delta": 32.53}],
    )
    problems = verify_run._verify_fill_markers(ev)
    assert any("주문 인스턴스를 확정할 수 없어" in p for p in problems)


def test_legacy_evidence_without_session_keeps_last_wins():
    """제출일 정보가 없는 레거시 근거는 기존 last-wins 동작을 유지합니다."""
    fills = [
        _fill("7400", "매수", 1, 10.0, "2026-11-12T19:10:00+00:00"),
        _fill("7400", "매수", 2, 20.0, "2026-11-24T19:10:00+00:00"),
    ]
    legacy_meta = {"side": "BUY", "processed_filled_qty": 2, "processed_filled_amount": 20.0}
    idx = verify_run._fill_index_by_odno(fills)
    assert verify_run._match_fill_for_meta("7400", legacy_meta, idx) is fills[-1]


def test_kst_and_us_session_conventions():
    """KST submitted_at·메리츠 KST 행·NHPlug 미국 영업일 행을 모두 구분합니다."""
    assert verify_run._verifier_meta_session(_meta("2026-11-24")) == "20261124"
    assert (
        verify_run._verifier_meta_session({"submitted_at": "20261125052359"})
        == "20261124"
    )
    us_row = _fill("1", "매수", 1, 1.0, "2026-11-24T19:10:00+00:00", ord_dt="20261124")
    assert verify_run._verifier_fill_session(us_row) == "20261124"
    kst_row = {
        "odno": "1",
        "side": "매수",
        "ord_dt": "20261125",
        "ord_tmd": "052359",
        "_ord_dt_is_kst_date": True,
    }
    assert verify_run._verifier_fill_session(kst_row) == "20261124"
    assert verify_run._order_instance_matches(kst_row, {"submitted_at": "20261125052359"}) is True


# ───────────────────────────────────────────────────────────────────
# B. 기존 탐지 정책 유지
# ───────────────────────────────────────────────────────────────────
def test_tampering_and_duplicate_still_detected():
    """마커 변조·과다 적용은 여전히 실패합니다."""
    ev = _ev(
        fills=[_fill("7500", "매수", 1, 10.0, "2026-11-24T19:10:00+00:00")],
        orders_meta_before={"7500": _meta("2026-11-24")},
        orders_meta_after={"7500": _meta("2026-11-24", qty=5, amount=50.0)},
        orders=[{"odno": "7500", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}],
    )
    problems = verify_run._verify_fill_markers(ev)
    assert any("초과" in p or "불일치" in p for p in problems)


def test_watermark_and_legacy_zero_marker_preserved():
    """워터마크 이전 레거시 0 마커는 건너뜀으로 유지됩니다."""
    ev = _ev(
        fills=[_fill("7600", "매수", 1, 10.0, "2026-10-01T19:10:00+00:00")],
        orders_meta_before={
            "7600": {
                "side": "BUY",
                "processed_filled_qty": 0,
                "processed_filled_amount": 0.0,
            }
        },
        orders_meta_after={
            "7600": {
                "side": "BUY",
                "processed_filled_qty": 0,
                "processed_filled_amount": 0.0,
            }
        },
        skipped_unverified=[
            {
                "odno": "7600",
                "odno_hash": verify_run._independent_odno_hash(
                    "7600", "a1b2c3d4e5f60718293a4b5c6d7e8f90"
                ),
                "filled_qty": 1,
                "ts": "2026-10-01T19:10:00+00:00",
                "reason": "legacy_zero_marker_unproven",
            }
        ],
    )
    assert verify_run._verify_skipped_unverified(ev) == []


# ───────────────────────────────────────────────────────────────────
# C. 실패 Evidence 보존 · D. 마스킹
# ───────────────────────────────────────────────────────────────────
def test_failed_evidence_is_committed_false_and_masked(tmp_path, monkeypatch):
    """검증 실패 근거는 committed=false 로 남고 메시지의 원문 번호는 마스킹됩니다."""
    monkeypatch.setenv("TQQQ_SEED", "10000")
    monkeypatch.setenv("SOXL_SEED", "5000")
    monkeypatch.setenv("BROKER", "nhplug")
    monkeypatch.setenv("BROKER_MODE", "demo")
    import pipeline

    monkeypatch.chdir(tmp_path)
    ev = _ev(
        scope="settlement_detail",
        fills=[_fill("3357", "매도", 4, 260.76, "2026-11-12T19:10:00+00:00")],
        orders_meta_before={"3357": _meta("2026-11-24")},
        orders_meta_after={"3357": _meta("2026-11-24", qty=9, amount=999.0)},
        orders=[{"odno": "3357", "side": "매도", "qty_delta": 1, "amount_delta": 10.0}],
    )
    path = pipeline._write_verification_failed_evidence("SOXL", ev, ["3357 검증 실패"])
    assert path and Path(path).exists()
    saved = json.loads(Path(path).read_text())
    assert saved["committed"] is False
    assert saved["scope"] == "settlement_detail"
    assert "verification_failed" in saved["failure_reason"]
    assert pipeline.evidence_file_for_run("SOXL", "synthetic") == ""

    masked = verify_run.verify_evidence_masked(ev)
    assert masked
    assert all("3357" not in message for message in masked)
    assert any("ord#" in message for message in masked)


def test_e2e_reconstructed_failed_dry():
    """재구성 실패 DRY 입력의 정산 결과와 중복 방지 정책을 확인합니다."""
    import copy

    ev = _ev(
        fills=[
            _fill("3357", "매도", 4, 260.76, "2026-11-12T19:10:00+00:00"),
            _fill("3357", "매수", 1, 32.53, "2026-11-24T19:10:00+00:00"),
            _fill("4822", "매수", 1, 31.27, "2026-11-25T19:10:00+00:00", is_additional=True),
        ],
        orders_meta_before={
            "3357": _meta("2026-11-24", qty=1, amount=32.53, reverse_action="buy"),
            "4822": _meta("2026-11-25", reverse_action="buy"),
        },
        orders=[{"odno": "4822", "side": "매수", "qty_delta": 1, "amount_delta": 31.27}],
    )
    problems, required, money = verify_run._required_settlement_fills(ev)
    assert not problems
    assert not any(
        r["odno"] == "3357" and r["req_qty"] == 3 and abs(r["req_amount"] - 228.23) < 0.01
        for r in required
    )
    new = next(r for r in required if r["odno"] == "4822")
    assert new["req_qty"] == 1 and abs(new["req_amount"] - 31.27) < 0.01
    assert abs(money - 5280.88) < 0.05
    # 장애 재현 기대값 (DRY 재구성 결과 — 영속 state 를 보정하지 않습니다)
    assert ev["T_before"] == 19.6562 and ev["T_after"] == 19.7422
    assert abs(ev["net_invested_after"] - 5280.88) < 0.01
    assert ev["balance_qty"] == 49

    # 같은 이력 재처리 — 4822 마커가 전진하면 신규 요구가 사라집니다 (중복 반영 없음)
    replay = copy.deepcopy(ev)
    replay["orders_meta_before"]["4822"]["processed_filled_qty"] = 1
    replay["orders_meta_before"]["4822"]["processed_filled_amount"] = 31.27
    _p2, req2, money2 = verify_run._required_settlement_fills(replay)
    assert all(r["req_qty"] == 0 for r in req2 if r["odno"] == "4822")
    assert abs(money2 - 5249.61) < 0.05
