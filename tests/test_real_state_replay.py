"""실제 백업 state + Evidence 재생 회귀 테스트 (2026-10-26 모의 3개 증권사 실행).

STATE_AUDIT_DIR (기본 ~/state-audit) 에 아래가 있을 때만 실행됩니다 (없으면 skip):
  {kis,kiwoom,nhplug}-before.json, settlement-evidence-<run_id>.zip
순수 계산만 합니다 — 네트워크/주문 없음.
기대: 과거 72건 재반영 0건, 신규 16건만 반영, T == Evidence 의 T_after,
순투입금 == 백업 net_invested + 신규 체결 순액 (6개 값 고정 — 실패 Evidence 의
net_invested_after 는 오류가 섞인 임시 계산이므로 기대값으로 쓰지 않습니다).
"""
import contextlib
import re
import copy
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from state import update_T_from_history  # noqa: E402

AUDIT = Path(os.getenv("STATE_AUDIT_DIR", str(Path.home() / "state-audit")))
RUNS = {"40384302200": "kis", "40385059348": "kiwoom", "40385826091": "nhplug"}


def _cases():
    cases = []
    for run, broker in RUNS.items():
        zpath = AUDIT / f"settlement-evidence-{run}.zip"
        spath = AUDIT / f"{broker}-before.json"
        if not (zpath.exists() and spath.exists()):
            continue
        base = json.loads(spath.read_text())
        with zipfile.ZipFile(zpath) as zf:
            for name in sorted(zf.namelist()):
                if name.rsplit("/", 1)[-1].startswith("settlement-"):
                    cases.append((broker, json.loads(zf.read(name)), base))
    return cases


CASES = _cases()


def _rows(fills):
    rows = []
    for fl in fills:
        q, a = int(fl["qty"]), float(fl["amount"])
        rows.append({
            "odno": str(fl["odno"]), "sll_buy_dvsn_cd_name": fl["side"],
            "ft_ccld_qty": str(q), "ft_ccld_amt3": str(a),
            "ft_ccld_unpr3": str(a / q if q else 0),
            "ord_datetime_utc": fl["ts"], "ord_dt": fl["ts"][:10].replace("-", ""),
            "prcs_stat_name": "체결", "nccs_qty": "0", "ft_ord_qty": str(q),
        })
    return rows


# 백업 net_invested_before + Evidence 신규 매수 16건 순액 (브로커 원장 독립 확정값 아님 — 반올림 오차 허용)
EXPECTED_NET = {
    ("kis", "TQQQ"): 5105.94, ("kis", "SOXL"): 6804.12,
    ("kiwoom", "TQQQ"): 22947.92, ("kiwoom", "SOXL"): 38350.42,
    ("nhplug", "TQQQ"): 4078.89, ("nhplug", "SOXL"): 3962.14,
}

needs_audit = pytest.mark.skipif(not CASES, reason="실제 백업/Evidence 없음 (STATE_AUDIT_DIR)")


def _replay(ev, base, state=None):
    rows = _rows(ev["fills"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = update_T_from_history(
            ev["symbol"], copy.deepcopy(state if state is not None else base[ev["symbol"]]),
            rows, balance_qty=ev["balance_qty"],
        )
    return res, rows, buf.getvalue()


@needs_audit
def test_real_replay_blocks_72_past_and_applies_16_new():
    past_applied = new_applied = 0
    for broker, ev, base in CASES:
        sym, wm = ev["symbol"], ev["watermark_before"]
        res, _rows_, _out = _replay(ev, base)
        assert res.get("_settlement_hold") is not True, f"{broker}/{sym} 보류"
        ts = {str(f["odno"]): f["ts"] for f in ev["fills"]}
        for d in res["_settlement_deltas"]:
            if not d.get("qty_delta"):
                continue
            if ts[str(d["odno"])] <= wm:
                past_applied += 1
            else:
                new_applied += 1
        assert res["T"] == pytest.approx(ev["T_after"]), f"{broker}/{sym} T"
    assert past_applied == 0
    assert new_applied == 16


@needs_audit
def test_real_replay_net_invested_matches_independent_expectation():
    for broker, ev, base in CASES:
        sym = ev["symbol"]
        res, _r, _o = _replay(ev, base)
        assert res["net_invested"] == pytest.approx(EXPECTED_NET[(broker, sym)], abs=0.02), \
            f"{broker}/{sym} net_invested={res['net_invested']}"
        # 실패 Evidence 의 net_invested_after 는 오류가 섞인 값 — 일치하면 안 됩니다.
        assert abs(res["net_invested"] - ev["net_invested_after"]) > 1.0


@needs_audit
def test_real_replay_independent_net_matches_backup_plus_new_fills():
    """표 값에 의존하지 않고 백업 + Evidence 신규 체결로 직접 재계산해도 같아야 합니다."""
    for broker, ev, base in CASES:
        sym, wm = ev["symbol"], ev["watermark_before"]
        new = [f for f in ev["fills"] if f["ts"] > wm]
        net = sum(f["amount"] if f["side"] == "매수" else -f["amount"] for f in new)
        res, _r, _o = _replay(ev, base)
        assert res["net_invested"] == pytest.approx(base[sym]["net_invested"] + net, abs=0.02)


@needs_audit
def test_real_replay_qty_watermark_and_markers():
    for broker, ev, base in CASES:
        sym, wm = ev["symbol"], ev["watermark_before"]
        res, _r, _o = _replay(ev, base)
        # 수량: 정산 후 보유 기준은 Evidence 의 position_basis 와 같습니다
        assert res.get("position_qty") == ev["position_basis"], f"{broker}/{sym} position_qty"
        # 워터마크: 마지막 신규 체결 시각으로만 전진합니다
        assert res["last_updated"] == ev["last_updated"], f"{broker}/{sym} watermark"
        fills = {str(f["odno"]): f for f in ev["fills"]}
        for odno, f in fills.items():
            before = base[sym]["orders_meta"].get(odno)
            after = res["orders_meta"].get(odno)
            if f["ts"] > wm:
                # 신규: 마커가 실제 체결 수량·금액까지 전진
                assert after["processed_filled_qty"] == f["qty"], f"{broker}/{sym} {odno}"
                assert after["processed_filled_amount"] == pytest.approx(f["amount"], abs=0.01)
            else:
                # 과거 건너뜀: 메타가 한 글자도 바뀌지 않음 (마커 진행·변조 없음)
                assert after == before, f"{broker}/{sym} {odno} 과거 메타 변경"


@needs_audit
def test_real_replay_is_idempotent():
    """같은 체결 이력을 두 번 처리해도 T·순투입금·워터마크·마커가 다시 변하지 않습니다."""
    for broker, ev, base in CASES:
        sym = ev["symbol"]
        first, rows, _o = _replay(ev, base)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            second = update_T_from_history(
                sym, copy.deepcopy(first), rows, balance_qty=ev["balance_qty"]
            )
        assert second["T"] == pytest.approx(first["T"]), f"{broker}/{sym} T"
        assert second["net_invested"] == pytest.approx(first["net_invested"]), f"{broker}/{sym} net"
        assert second["last_updated"] == first["last_updated"]
        assert second["orders_meta"] == first["orders_meta"]
        assert [d for d in second["_settlement_deltas"] if d.get("qty_delta")] == [], \
            f"{broker}/{sym} 재처리에서 추가 반영 발생"
        assert second.get("_settlement_hold") is not True


@needs_audit
def test_real_replay_skipped_past_orders_are_audited_and_masked():
    total = 0
    for broker, ev, base in CASES:
        sym, wm = ev["symbol"], ev["watermark_before"]
        res, _r, out = _replay(ev, base)
        audit = res.get("_unverified_skipped_audit") or []
        past = [f for f in ev["fills"] if f["ts"] <= wm]
        past_odnos = {str(f["odno"]) for f in past}
        skipped = {e["odno"] for e in audit}
        # 건너뜀 대상은 과거 체결 중 "0 마커 + 버전 표식 없음" 인 주문뿐입니다
        assert skipped <= past_odnos
        for odno in skipped:
            meta = base[sym]["orders_meta"][odno]
            assert meta.get("processed_filled_qty") == 0 and "fill_marker_version" not in meta
        for e in audit:
            assert re.fullmatch(r"ord#[0-9a-f]{10}", e["odno_hash"])
            assert e["reason"] == "legacy_zero_marker_unproven"
        total += len(audit)
        # 공개 로그(stdout)의 건너뜀 경고에는 실제 주문번호가 없어야 합니다
        for line in out.splitlines():
            if "증명할 마커" in line:
                assert "ord#" in line
        # 정산 전체 stdout 에 이력·메타의 어떤 원문 주문번호도 나오지 않아야 합니다
        all_odnos = {str(f["odno"]) for f in ev["fills"]} | set(base[sym]["orders_meta"])
        for odno in all_odnos:
            assert not re.search(rf"(?<![\w.]){re.escape(odno)}(?![\w.])", out), \
                f"{broker}/{sym}: 공개 로그에 원문 주문번호 노출"
    assert total == 72


@needs_audit
def test_real_replay_passes_independent_verifier():
    """운영 Evidence 생성기 + 독립 검증기가 새 코드 결과를 문제없이 통과시킵니다."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ops.verify_run import _verify_evidence  # noqa: E402
    import trading_bot  # noqa: E402

    for broker, ev, base in CASES:
        sym = ev["symbol"]
        before = copy.deepcopy(base[sym])
        res, rows, _o = _replay(ev, base)
        evidence = trading_bot.build_settlement_evidence(
            sym, res, rows, mode="LIVE", run_id="replay",
            balance_qty=ev["balance_qty"], before=before,
        )
        problems = _verify_evidence(evidence)
        assert problems == [], f"{broker}/{sym}: {problems[:3]}"
        assert len(evidence["skipped_unverified"]) == len(res["_unverified_skipped_audit"])
