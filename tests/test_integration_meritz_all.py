"""#12(hotfix) + #15(어댑터) + #14(감시기) + #13(합산) 통합 호환성 테스트.

각 기능을 따로 검증한 테스트가 서로 충돌하지 않는지, 한 브랜치에 함께 적용했을 때의 접점만 봅니다.
  - 기본값(플래그 OFF)에서 #13 이 기존 주문표를 바꾸지 않는다
  - #13 합산 주문은 #14 감시기(APPLY)도 정정하지 않는다
  - #15 어댑터가 만든 이력으로 #12 계획이 거부된 정정 행이 있는 원주문을 정정하지 않는다
  - 위 주문이 생기면 #12 보류 원칙(표식·봇 보류)과 #14 중단이 함께 동작한다
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meritz_reprice as rp
import run_lock
import state as st
import strategy
from test_buy_price_cap import _soxl_late_half_orders
from test_meritz_broker import make_broker  # noqa: F401  (fixture 재사용)
from test_meritz_rejected_amend_status import _history, _today_rows
from test_meritz_reprice import OLD, _state
from test_meritz_reprice_watch import APPLY_ENV, CONFIG_LIVE, run, world  # noqa: F401


def test_flag_off_is_default_and_order_table_is_unchanged(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", False)
    result = _soxl_late_half_orders(monkeypatch)
    normal = [o for o in result["orders"] if o["side"] == "BUY" and "[추가매수]" not in o["comment"]][0]
    additional = [o["price"] for o in result["orders"] if "[추가매수]" in o["comment"]]
    assert (normal["quantity"], normal["price"]) == (4, 111.51) and additional == [96.53, 82.74, 72.40]
    assert "merged_additional_qty" not in normal and normal.get("original_price") == 118.10   # #12 메타는 유지


def test_watcher_apply_never_amends_a_merged_order(world):
    st.save_state("SOXL", _state(merged_additional_qty=1, normal_qty=6))
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 0 and world.broker.calls == []                    # 정정 호출 없음
    assert not os.path.exists(run_lock.journal_path())


def test_adapter_rejected_amend_original_is_held_by_reprice_plan(make_broker):
    _day, original, amend = _today_rows()
    amend["ovrs_stck_oder_stat_cls_code"] = "1"                       # 거부된 정정 행
    rows = _history(make_broker, [original, amend])
    old = next(r for r in rows if r["_plain_oder_no"] == "9001")
    assert old["prcs_stat_name"] == "미체결" and old["_amend_rejected"] is True      # #15
    session = rp._order_us_session_date(old)
    state = {"orders_meta": {str(old["odno"]): {
        "side": "BUY", "total_qty": 10, "t_target": 1.0, "is_additional": False, "processed_filled_qty": 0,
        "processed_filled_amount": 0.0, "fill_marker_version": 1, "original_price": 80.0, "order_price": 73.69}},
        "T": 5.0, "net_invested": 1000.0}
    plan = rp.build_plan(state, rows, 65.0, {}, session)
    assert not [p for p in plan if p["action"] == rp.ACTION_AMEND]   # #12: 거부 정정 이력이 있으면 재정정하지 않음


def test_rejected_late_hold_and_watcher_halt_work_together(world):
    world.broker.new_row_rejected = True
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and len(world.broker.calls) == 1               # 1회 시도 후 즉시 중단 (#14)
    entries = rp.read_journal(run_lock.journal_path())
    assert len(entries) == 1 and entries[0]["review_required"] == "REJECTED_LATE"   # #12 표식
    import trading_bot
    assert trading_bot._pending_amend_journal("SOXL")               # 봇 보류
    calls = len(world.broker.calls)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)         # 다음 실행은 자동 복구·재정정 없음
    assert code == 1 and len(world.broker.calls) == calls
