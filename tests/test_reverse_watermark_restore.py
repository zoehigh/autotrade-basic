"""REVERSE_WATERMARK_RESTORE — 되돌리기 직후 미정산 리버스 매도의 워터마크 복원 검증.

배경(2026-11-09 NHPlug SOXL DRY): 원장·마커를 되돌려도 워터마크가 2026-11-05T19:10 으로 올라가 있으면
독립 검증기가 정상 정산을 '과다 반영'으로 판정합니다. 오분류 직전 워터마크(11-04T19:10 / 3048)로 되돌립니다.
"""
import json
import os
import shutil
import sys
from copy import deepcopy

import pytest

_repo_root = os.path.join(os.path.dirname(__file__), "..")
for _p in (_repo_root, os.path.join(_repo_root, "src"), os.path.join(_repo_root, "ops")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import state as state_mod
from state import (
    canonical_state_hash,
    load_state,
    reconcile_reverse_fills,
    restore_watermark_for_unapplied_reverse_order,
    rewind_misclassified_reverse_sell,
    save_state,
)
from trading_bot import _REPAIR_ACTIONS, _reverse_watermark_restore
import verify_repair_rewind as vr
from test_reverse_ledger_rewind import NH_ROW, ODNO, _soxl_after_bad_run, _tqqq

TARGET_UPDATED = "2026-11-04T19:10:00+00:00"
TARGET_ORDNO = "3048"


def _soxl_after_rewind():
    """REVERSE_LEDGER_REWIND 적용 직후 상태 (미정산 리버스 매도 1개, 워터마크는 아직 오분류 실행 값)."""
    s = _soxl_after_bad_run()
    rewind_misclassified_reverse_sell(s, ODNO)
    return s


def test_액션이_허용_목록에_등록돼_있다():
    assert "REVERSE_WATERMARK_RESTORE" in _REPAIR_ACTIONS


def test_워터마크만_바뀌고_이후_재정산도_정상():
    s = _soxl_after_rewind()
    before = deepcopy(s)
    summary = restore_watermark_for_unapplied_reverse_order(s, TARGET_UPDATED)  # ordno 생략 → 원장 마지막 항목(3048)
    assert s["last_updated"] == TARGET_UPDATED and s["last_processed_ordno"] == TARGET_ORDNO
    assert summary["last_updated"] == ("2026-11-05T19:10:00+00:00", TARGET_UPDATED)
    for key in set(before) | set(s):
        if key not in ("last_updated", "last_processed_ordno"):
            assert s[key] == before[key], key
    reconcile_reverse_fills(s, [deepcopy(NH_ROW)])
    assert s["T"] == 17.325 and s["net_invested"] == 4898.69 and s["reverse_mode"]["day_count"] == 1


def test_ordno_생략시_원장_마지막_항목을_쓰고_명시값도_허용():
    s = _soxl_after_rewind()
    restore_watermark_for_unapplied_reverse_order(s, TARGET_UPDATED)
    assert s["last_processed_ordno"] == TARGET_ORDNO == "3048"
    s2 = _soxl_after_rewind()
    restore_watermark_for_unapplied_reverse_order(s2, TARGET_UPDATED, "1516")
    assert s2["last_processed_ordno"] == "1516"


def test_원장_마지막_주문이_메타에_없으면_자동_도출_거부():
    s = _soxl_after_rewind()
    del s["orders_meta"]["3048"]
    before = deepcopy(s)
    with pytest.raises(ValueError, match="목표 last_processed_ordno"):
        restore_watermark_for_unapplied_reverse_order(s, TARGET_UPDATED)
    assert s == before


def _refuses(mutate, args, match):
    s = _soxl_after_rewind()
    mutate(s)
    before = deepcopy(s)
    with pytest.raises(ValueError, match=match):
        restore_watermark_for_unapplied_reverse_order(s, *args)
    assert s == before


def test_전제_불충족이면_상태를_바꾸지_않고_거부():
    ok = (TARGET_UPDATED, TARGET_ORDNO)
    _refuses(lambda s: None, ("2026-11-06T00:00:00+00:00", TARGET_ORDNO), "과거")        # 앞으로 감기
    _refuses(lambda s: None, ("2026-11-05T19:10:00+00:00", TARGET_ORDNO), "과거")        # 같은 값
    _refuses(lambda s: None, ("not-a-time", TARGET_ORDNO), "ISO")
    _refuses(lambda s: None, ("2026-11-04T19:10:00", TARGET_ORDNO), "시간대")
    _refuses(lambda s: s["settlement_ledger"].update(entries=[]), (TARGET_UPDATED,), "원장이 비어")
    _refuses(lambda s: s["orders_meta"][ODNO].update(processed_filled_qty=4), ok, "후보 0개")      # 이미 반영됨
    _refuses(lambda s: s["orders_meta"][ODNO].update(terminal=True), ok, "후보 0개")
    _refuses(lambda s: s["orders_meta"][ODNO].update(applied_fill_fraction=1.0), ok, "후보 0개")
    _refuses(lambda s: s["settlement_ledger"]["entries"].append({"seq": 7, "kind": "reverse_sell", "odno": ODNO, "delta": -1.9}), ok, "후보 0개")
    _refuses(lambda s: s.update(pending_order_batch={"x": 1}), ok, "fence")
    _refuses(lambda s: s["reverse_mode"].clear(), ok, "cycle")
    _refuses(lambda s: s.update(T=1.0), ok, "재생값")
    def second(s):
        s["orders_meta"]["2618"] = deepcopy(s["orders_meta"][ODNO])
    _refuses(second, ok, "후보 2개")


@pytest.mark.parametrize("field,value", [
    ("processed_filled_qty", 0.5),                 # 정수 변환하면 0 이 되는 소수
    ("processed_filled_qty", None),
    ("processed_filled_qty", "0"),
    ("processed_filled_qty", float("nan")),
    ("processed_filled_qty", False),
    ("processed_filled_amount", None),
    ("processed_filled_amount", "0.0"),
    ("processed_filled_amount", float("nan")),
    ("processed_filled_amount", float("inf")),
    ("applied_fill_fraction", None),
    ("applied_fill_fraction", "0"),
    ("applied_fill_fraction", float("nan")),
])
def test_잘못된_마커_값은_미정산으로_인정하지_않는다(field, value):
    _refuses(lambda s: s["orders_meta"][ODNO].update({field: value}), (TARGET_UPDATED,), "후보 0개")


@pytest.mark.parametrize("missing", ["processed_filled_qty", "processed_filled_amount"])
def test_마커_누락은_미정산_증거가_아니다(missing):
    _refuses(lambda s: s["orders_meta"][ODNO].pop(missing), (TARGET_UPDATED,), "후보 0개")


def test_반영비율_키가_없으면_0으로_보고_정상_통과():
    s = _soxl_after_rewind()
    assert "applied_fill_fraction" not in s["orders_meta"][ODNO]
    restore_watermark_for_unapplied_reverse_order(s, TARGET_UPDATED)
    s2 = _soxl_after_rewind()
    s2["orders_meta"][ODNO]["applied_fill_fraction"] = 0.0  # 명시된 유효한 0 도 허용
    restore_watermark_for_unapplied_reverse_order(s2, TARGET_UPDATED)


def test_오분류_상태_그대로는_거부(monkeypatch):
    """아직 되돌리기 전(마커 4/179.52, 원장에 sell 항목)에는 이 액션을 쓸 수 없습니다."""
    s = _soxl_after_bad_run()
    before = deepcopy(s)
    with pytest.raises(ValueError, match="후보 0개"):
        restore_watermark_for_unapplied_reverse_order(s, TARGET_UPDATED, TARGET_ORDNO)
    assert s == before


# ── 핸들러 ────────────────────────────────────────────────────────

@pytest.fixture
def two_symbols():
    save_state("SOXL", _soxl_after_rewind())
    save_state("TQQQ", _tqqq())


def _env(monkeypatch, **over):
    base = {
        "REPAIR_REWIND_SYMBOL": "SOXL",
        "REPAIR_REWIND_EXPECT_HASH": canonical_state_hash(load_state("SOXL")),
        "REPAIR_REWIND_EXPECT_T": "19.25",
        "REPAIR_REWIND_GUARD_SYMBOL": "TQQQ",
        "REPAIR_REWIND_GUARD_HASH": canonical_state_hash(load_state("TQQQ")),
        "REPAIR_REWIND_TARGET_LAST_UPDATED": TARGET_UPDATED,
    }
    base.update(over)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_미리보기는_저장하지_않는다(two_symbols, monkeypatch):
    _env(monkeypatch)
    before = canonical_state_hash(load_state("SOXL"))
    _reverse_watermark_restore()
    assert canonical_state_hash(load_state("SOXL")) == before


def test_적용은_SOXL_워터마크만_바꾸고_TQQQ_불변(two_symbols, monkeypatch):
    _env(monkeypatch, REPAIR_REWIND_APPLY="true")
    tqqq = canonical_state_hash(load_state("TQQQ"))
    _reverse_watermark_restore()
    s = load_state("SOXL")
    assert s["last_updated"] == TARGET_UPDATED and s["last_processed_ordno"] == TARGET_ORDNO
    assert s["T"] == 19.25
    assert canonical_state_hash(load_state("TQQQ")) == tqqq


@pytest.mark.parametrize("name,value,match", [
    ("REPAIR_REWIND_EXPECT_HASH", "0" * 64, "hash 불일치"),
    ("REPAIR_REWIND_EXPECT_T", "14.0", "T 불일치"),
    ("REPAIR_REWIND_GUARD_HASH", "0" * 64, "가드 종목"),
    ("REPAIR_REWIND_GUARD_SYMBOL", "SOXL", "달라야"),
    ("REPAIR_REWIND_TARGET_LAST_UPDATED", "2026-11-06T00:00:00+00:00", "전제 불충족"),
])
def test_CAS_가드_전제_불일치는_저장_없이_중단(two_symbols, monkeypatch, name, value, match):
    _env(monkeypatch, REPAIR_REWIND_APPLY="true", **{name: value})
    before = canonical_state_hash(load_state("SOXL"))
    with pytest.raises(RuntimeError, match=match):
        _reverse_watermark_restore()
    assert canonical_state_hash(load_state("SOXL")) == before


def test_필수_환경변수_없으면_중단(two_symbols, monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("REPAIR_REWIND_TARGET_LAST_UPDATED")
    with pytest.raises(RuntimeError, match="환경변수가 없습니다"):
        _reverse_watermark_restore()


# ── 독립 검증기 (watermark 모드) ───────────────────────────────────

@pytest.fixture
def before_after(tmp_path, monkeypatch, two_symbols):
    shutil.copy(state_mod._STATE_FILE, tmp_path / "before.json")
    _env(monkeypatch, REPAIR_REWIND_APPLY="true")
    _reverse_watermark_restore()
    shutil.copy(state_mod._STATE_FILE, tmp_path / "after.json")
    return json.load(open(tmp_path / "before.json")), json.load(open(tmp_path / "after.json"))


def test_검증기_실제_결과_통과(before_after):
    before, after = before_after
    assert vr.verify_watermark(before, after, "SOXL", TARGET_UPDATED) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda a: a["TQQQ"].update(T=26.0), "TQQQ"),
    (lambda a: a["SOXL"].update(T=17.0), "허용되지 않은 필드"),
    (lambda a: a["SOXL"].update(position_qty=1), "position_qty"),
    (lambda a: a["SOXL"].update(last_processed_ordno="1"), "목표값"),
    (lambda a: a["SOXL"]["settlement_ledger"].update(entries=[]), "허용되지 않은 필드"),
    (lambda a: a["SOXL"].update(last_updated="2026-11-03T00:00:00+00:00"), "목표값"),
])
def test_검증기_변조_탐지(before_after, mutate, needle):
    before, after = before_after
    after = deepcopy(after)
    mutate(after)
    assert any(needle in p for p in vr.verify_watermark(before, after, "SOXL", TARGET_UPDATED))


def test_검증기_변경_없음과_앞으로_감기는_실패(before_after):
    before, _ = before_after
    assert vr.verify_watermark(before, deepcopy(before), "SOXL", before["SOXL"]["last_updated"], before["SOXL"]["last_processed_ordno"]) != []
    fwd = deepcopy(before)
    fwd["SOXL"]["last_updated"] = "2026-11-09T00:00:00+00:00"
    assert any("과거" in p for p in vr.verify_watermark(before, fwd, "SOXL", "2026-11-09T00:00:00+00:00", before["SOXL"]["last_processed_ordno"]))


def test_main_watermark_모드_종료코드(before_after, tmp_path):
    base = [str(tmp_path / "before.json"), str(tmp_path / "after.json"), "--symbol", "SOXL", "--mode", "watermark"]
    assert vr.main(base + ["--target-last-updated", TARGET_UPDATED]) == 0
    assert vr.main(base + ["--target-last-updated", TARGET_UPDATED, "--target-last-ordno", "1"]) == 1
