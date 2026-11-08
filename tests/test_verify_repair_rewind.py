"""ops/verify_repair_rewind.py — 복구 결과 독립 검증기 테스트 (실제 복구 핸들러 출력과 대조)."""
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
from state import canonical_state_hash, load_state, save_state
from trading_bot import _reverse_ledger_rewind
import verify_repair_rewind as vr
from test_reverse_ledger_rewind import ODNO, _soxl_after_bad_run, _tqqq


def _snapshot(path):
    shutil.copy(state_mod._STATE_FILE, path)
    return json.load(open(path))


@pytest.fixture
def before_after(tmp_path, monkeypatch):
    save_state("SOXL", _soxl_after_bad_run())
    save_state("TQQQ", _tqqq())
    before = _snapshot(tmp_path / "before.json")
    env = {
        "REPAIR_REWIND_SYMBOL": "SOXL", "REPAIR_REWIND_ODNO": ODNO,
        "REPAIR_REWIND_EXPECT_HASH": canonical_state_hash(load_state("SOXL")),
        "REPAIR_REWIND_EXPECT_T": "14.4375", "REPAIR_REWIND_GUARD_SYMBOL": "TQQQ",
        "REPAIR_REWIND_GUARD_HASH": canonical_state_hash(load_state("TQQQ")),
        "REPAIR_REWIND_APPLY": "true",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _reverse_ledger_rewind()
    after = _snapshot(tmp_path / "after.json")
    return before, after


def test_실제_복구_결과는_검증을_통과한다(before_after):
    before, after = before_after
    assert vr.verify(before, after, "SOXL", ODNO) == []


def test_TQQQ가_바뀌면_실패(before_after):
    before, after = before_after
    after["TQQQ"]["T"] = 25.0
    assert any("TQQQ" in p for p in vr.verify(before, after, "SOXL", ODNO))


def test_허용되지_않은_SOXL_필드_변경은_실패(before_after):
    before, after = before_after
    after["SOXL"]["position_qty"] = 99
    assert any("position_qty" in p for p in vr.verify(before, after, "SOXL", ODNO))
    after["SOXL"]["position_qty"] = before["SOXL"]["position_qty"]
    after["SOXL"]["last_updated"] = "2026-11-06T00:00:00+00:00"
    assert any("last_updated" in p for p in vr.verify(before, after, "SOXL", ODNO))


@pytest.mark.parametrize("mutate,needle", [
    (lambda a: a["SOXL"].update(T=17.325), "reverse_base_t"),
    (lambda a: a["SOXL"].update(net_invested=4898.69), "net_invested"),
    (lambda a: a["SOXL"]["sell_t_effects"].update({"x": 1}), "sell_t_effects"),
    (lambda a: a["SOXL"]["settlement_ledger"].update(next_seq=8), "next_seq"),
    (lambda a: a["SOXL"]["settlement_ledger"]["entries"].pop(0), "원장 항목"),
    (lambda a: a["SOXL"]["orders_meta"]["3048"].update(processed_filled_qty=0), "다른 주문"),
    (lambda a: a["SOXL"]["orders_meta"][ODNO].update(processed_filled_qty=4), "마커"),
    (lambda a: a["SOXL"]["orders_meta"][ODNO].update(terminal=True), "허용되지 않은 필드"),
])
def test_기대와_다른_결과는_실패(before_after, mutate, needle):
    before, after = before_after
    after = deepcopy(after)
    mutate(after)
    assert any(needle in p for p in vr.verify(before, after, "SOXL", ODNO))


def test_변경_없이_그대로면_실패(before_after):
    """복구가 실제로 일어나지 않았는데 통과하면 안 됩니다."""
    before, _ = before_after
    assert vr.verify(before, deepcopy(before), "SOXL", ODNO) != []


def test_main_종료코드(before_after, tmp_path):
    before, after = before_after
    assert vr.main([str(tmp_path / "before.json"), str(tmp_path / "after.json"), "--symbol", "SOXL", "--odno", ODNO]) == 0
    assert vr.main([str(tmp_path / "before.json"), str(tmp_path / "nope.json"), "--symbol", "SOXL", "--odno", ODNO]) == 2
    bad = tmp_path / "bad.json"
    after["TQQQ"]["T"] = 1.0
    bad.write_text(json.dumps(after))
    assert vr.main([str(tmp_path / "before.json"), str(bad), "--symbol", "SOXL", "--odno", ODNO]) == 1


def test_odno_없이도_복구_전_상태에서_대상을_특정해_검증(before_after):
    before, after = before_after
    assert vr.verify(before, after, "SOXL") == []
    assert vr.verify(before, deepcopy(before), "SOXL") != []
