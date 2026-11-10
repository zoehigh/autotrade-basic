"""거부된 정정 행의 원주문 상태 표시 (어댑터) + 봇 잔여 주문 취소와의 연결 검증.

배경: 어댑터는 정정 행(rctf=1)이면 거부 여부와 무관하게 원주문을 '정정됨·잔량 0'으로 표시했습니다.
거부된 정정 뒤에도 원주문은 살아 있을 수 있는데, 봇의 잔여 주문 취소는 '정정됨'을 종료 상태로 보고 건너뛰어
살아 있는 원주문이 신규 주문과 중복될 수 있었습니다 (2026-10-16 SOXL 실데이터에서 거부된 정정 행 확인).
수정: 거부된 정정은 원주문을 대체하지 않고(자기 상태 유지) `_amend_rejected` 표식만 붙입니다.
원주문 생존 여부를 확정할 수 없으므로 봇이 취소 대상으로 삼아 확인하고, 취소가 거부되면 기존대로
'미확정'으로 해당 종목 신규 주문을 보류합니다 (보류 유지).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trading_bot
from broker.market_utils import get_kst_now
from test_meritz_broker import PERIOD_ROW, make_broker, ok  # noqa: F401  (fixture 재사용)
from test_stale_order_cleanup import _FakeBroker, _no_sleep  # noqa: F401


def _today_rows():
    now = get_kst_now()
    day, clock = now.strftime("%Y%m%d"), now.strftime("%H:%M:%S")
    original = dict(PERIOD_ROW, der_date=day, der_no="9001", orgl_oder_no="", cntg_dqty="0", cntg_camt="0",
                    der_dqty="10", der_exec_ymdh=f"{day} {clock}", cntg_ymdh="", aspr_type_code="5")
    amend = dict(original, der_no="9002", orgl_oder_no="9001", der_rctf_cncl_cls_code="1", rctf_cncl_cls_name="정정",
                 ovrs_stck_oder_prc="73.73")
    return day, original, amend


def _history(make_broker_fn, rows):
    broker, _ = make_broker_fn([ok(rsp_cd="5766", data=rows), ok(rsp_cd="5766", data=[])])
    return broker.get_order_history("TQQQ", "NAS")


def test_rejected_amend_does_not_supersede_original(make_broker):
    _day, original, amend = _today_rows()
    amend["ovrs_stck_oder_stat_cls_code"] = "1"          # 0 이 아니면 거부
    rows = _history(make_broker, [original, amend])
    old = next(r for r in rows if r["_plain_oder_no"] == "9001")
    rejected = next(r for r in rows if r["_plain_oder_no"] == "9002")
    assert rejected["prcs_stat_name"] == "거부"
    assert old["prcs_stat_name"] == "미체결" and old["nccs_qty"] == "10" and old["_remaining"] == 10
    assert old["_amend_rejected"] is True and not old.get("_is_superseded")


def test_accepted_amend_still_supersedes_original(make_broker):
    _day, original, amend = _today_rows()
    rows = _history(make_broker, [original, amend])
    old = next(r for r in rows if r["_plain_oder_no"] == "9001")
    assert old["prcs_stat_name"] == "정정됨" and old["nccs_qty"] == "0" and old["_is_superseded"] is True
    assert not old.get("_amend_rejected")


@pytest.mark.parametrize("order", ["rejected_first", "accepted_first"])
def test_rejected_then_accepted_amend_of_same_original_ends_superseded(make_broker, order):
    _day, original, rejected_amend = _today_rows()
    rejected_amend["ovrs_stck_oder_stat_cls_code"] = "1"
    accepted = dict(rejected_amend, der_no="9003", ovrs_stck_oder_stat_cls_code="0")
    extra = [rejected_amend, accepted] if order == "rejected_first" else [accepted, rejected_amend]
    rows = _history(make_broker, [original] + extra)
    old = next(r for r in rows if r["_plain_oder_no"] == "9001")
    assert old["prcs_stat_name"] == "정정됨" and old["_remaining"] == 0     # 접수된 정정이 있으면 대체됨


def test_cancel_row_behavior_unchanged(make_broker):
    _day, original, _amend = _today_rows()
    cancel = dict(original, der_no="9004", orgl_oder_no="9001", der_rctf_cncl_cls_code="2", rctf_cncl_cls_name="취소")
    rows = _history(make_broker, [original, cancel])
    assert next(r for r in rows if r["_plain_oder_no"] == "9001")["prcs_stat_name"] == "취소"


# ── 봇의 실제 잔여 주문 취소 함수와 연결 ─────────────────────────────────

def _cancel_calls(make_broker_fn, rows):
    history = _history(make_broker_fn, rows)
    old = next(r for r in history if r["_plain_oder_no"] == "9001")
    state = {"orders_meta": {old["odno"]: {"side": "BUY", "total_qty": 10, "t_target": 1.0}}}
    fake = _FakeBroker()
    count = trading_bot._cancel_open_bot_orders(fake, "TQQQ", "NAS", state, history)
    return old["odno"], fake.calls, count


def test_bot_now_attempts_to_cancel_original_after_rejected_amend(make_broker):
    _day, original, amend = _today_rows()
    amend["ovrs_stck_oder_stat_cls_code"] = "1"
    odno, calls, count = _cancel_calls(make_broker, [original, amend])
    assert [c[0] for c in calls] == [odno] and count == 1           # 살아 있을 수 있는 원주문을 취소 시도 → 확인


def test_bot_still_skips_original_after_accepted_amend(make_broker):
    _day, original, amend = _today_rows()
    odno, calls, count = _cancel_calls(make_broker, [original, amend])
    assert calls == [] and count == 0                               # 대체된 원주문은 취소 대상 아님 (기존 동작)


def test_bot_holds_symbol_when_original_cannot_be_cancelled(make_broker):
    """원주문이 이미 죽어 있어 취소가 거부되면 '미확정'으로 신규 주문을 보류한다 — 생존 불명이면 보류 유지."""
    _day, original, amend = _today_rows()
    amend["ovrs_stck_oder_stat_cls_code"] = "1"
    history = _history(make_broker, [original, amend])
    old = next(r for r in history if r["_plain_oder_no"] == "9001")
    state = {"orders_meta": {old["odno"]: {"side": "BUY", "total_qty": 10, "t_target": 1.0}}}
    fake = _FakeBroker(fail_on=old["odno"], after_cancel_rows=history)
    with pytest.raises(trading_bot.SymbolScopedError):
        trading_bot._cancel_open_bot_orders(fake, "TQQQ", "NAS", state, history)
