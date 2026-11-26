"""KIS 모의 주문번호 재사용 — 최근 체결 정산(`_apply_recent_history_dt`)의 번호 충돌 회귀 테스트.

수량·금액 델타(deltas)와 매도 효과(sell_t_effects)는 주문번호만으로 저장·조회됩니다(상태 스키마 변경 제외).
따라서 한 정산 묶음에서 같은 번호의 서로 다른 날짜 체결을 구분할 수 없으면 **잘못 반영하지 않고 명시적으로 보류**하고,
구분할 수 있는 경우(한 날짜만 묶음에 있음 / 날짜가 맞지 않는 행은 미등록 주문)는 정상 반영합니다.
KIS 모의에만 적용하며 KIS 실전·다른 브로커의 기존 동작은 유지합니다.
"""
from copy import deepcopy

import pytest

import state as state_module
from state import update_T_from_history

from test_kis_demo_odno_reuse_general_orders import (  # noqa: F401  (autouse 픽스처 _kis_demo 포함)
    N, _kis_demo, _meta, _meta_with_at, _new_row, _old_row, _state,
)

EARLY_WM = "2026-11-10T10:00:00+00:00"        # 두 행(11/17·11/23 UTC)보다 이른 워터마크 → 둘 다 최근 후보
MID_WM = "2026-11-20T10:00:00+00:00"          # 과거 행(11/17 UTC)은 워터마크 이전, 새 행(11/23 UTC)만 최근 후보


def _settle(st, history, qty=15):
    return update_T_from_history("TQQQ", st, history, balance_qty=qty)


def _unchanged(result, before):
    assert result["T"] == before["T"] and result["net_invested"] == before["net_invested"]
    assert result["orders_meta"] == before["orders_meta"]
    assert not result.get("_settlement_deltas")


@pytest.mark.parametrize("old_first", [True, False])
@pytest.mark.parametrize("with_at", [False, True], ids=["session만", "submitted_at포함"])
def test_같은_번호의_다른_날짜_체결이_한_묶음에_있으면_잘못_반영하지_않고_보류한다(old_first, with_at):
    meta = _meta_with_at(processed_filled_qty=0, processed_filled_amount=0.0) if with_at else _meta(0, 0.0)
    st = _state(meta, last_updated=EARLY_WM)
    before = deepcopy(st)
    history = [_old_row(10, 50.0), _new_row(5, 10.0)]
    if not old_first:
        history.reverse()

    result = _settle(st, history)

    assert result.get("_settlement_hold") is True
    assert "구분할 수 없" in result["_settlement_hold_reason"]
    assert result["_pending_amount_odnos"] == [N]
    _unchanged(result, before)                      # 수량·금액·T·마커·원장 모두 반영 없음


def test_구분_가능한_경우_과거_행은_워터마크_이전이므로_새_주문만_정상_반영한다():
    st = _state(_meta(0, 0.0), last_updated=MID_WM)
    result = _settle(st, [_old_row(10, 50.0), _new_row(5, 10.0)])

    assert not result.get("_settlement_hold")
    assert result["orders_meta"][N]["processed_filled_qty"] == 5
    assert result["orders_meta"][N]["processed_filled_amount"] == pytest.approx(50.0)
    assert result["net_invested"] == pytest.approx(520.0 + 50.0)
    assert result["T"] == pytest.approx(5.5, abs=1e-3)                 # 5/10 × t_target 1.0
    assert [d["odno"] for d in result["_settlement_deltas"]] == [N]


def test_날짜가_다른_과거_체결_행은_미등록_주문으로_반영하고_새_주문_메타는_건드리지_않는다():
    st = _state(_meta(0, 0.0), last_updated=EARLY_WM)
    result = _settle(st, [_old_row(10, 50.0), _new_row(0, 0.0)])         # 새 주문은 아직 미체결

    assert not result.get("_settlement_hold")
    assert result["orders_meta"][N]["processed_filled_qty"] == 0          # 새 주문 마커에 과거 체결이 섞이지 않음
    assert result["orders_meta"][N]["processed_filled_amount"] == 0.0
    assert result["net_invested"] == pytest.approx(520.0 + 500.0)         # 과거 주문의 10주 $500 은 미등록 체결로 반영
    assert [(d["odno"], d["qty_delta"], d["amount_delta"]) for d in result["_settlement_deltas"]] == [(N, 10, 500.0)]


def test_다른_날짜_메타의_마커_누락이_정상_정산을_보류시키지_않는다():
    meta = _meta()
    del meta["processed_filled_qty"], meta["processed_filled_amount"], meta["fill_marker_version"]   # 마커 없는 새 주문 메타
    st = _state(meta, last_updated=EARLY_WM)
    result = _settle(st, [_old_row(10, 50.0), _new_row(0, 0.0)])

    assert not result.get("_settlement_hold")             # 과거 행은 이 메타의 주문이 아님
    assert result["net_invested"] == pytest.approx(520.0 + 500.0)


def test_마커_누락_메타의_실제_체결은_여전히_보류한다():
    meta = _meta()
    del meta["processed_filled_qty"], meta["processed_filled_amount"], meta["fill_marker_version"]
    st = _state(meta, last_updated=MID_WM)
    result = _settle(st, [_new_row(5, 10.0)])

    assert result.get("_settlement_hold") is True
    assert N in result["_pending_marker_odnos"]


# ───────── 매도 효과(sell_t_effects)의 번호 충돌 ─────────

def _sell_meta(processed_qty):
    return _meta(processed_qty, 0.0 if processed_qty == 0 else processed_qty * 10.0, side="SELL")


def _sell_row(filled):
    row = _new_row(filled, 10.0)
    row["sll_buy_dvsn_cd_name"] = "매도"
    return row


def test_번호가_재사용된_새_매도의_첫_체결은_기존_매도_효과_기록과_구분할_수_없어_보류한다():
    st = _state(_sell_meta(0), last_updated=MID_WM)
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}        # 같은 번호의 과거 주문 기록
    before = deepcopy(st)

    result = _settle(st, [_sell_row(5)])

    assert result.get("_settlement_hold") is True
    assert result["_pending_amount_odnos"] == [N]
    _unchanged(result, before)


def test_이미_반영된_같은_주문의_이어지는_매도_체결은_보류하지_않는다():
    st = _state(_sell_meta(2), last_updated=MID_WM)
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}
    ambiguous = state_module._ambiguous_reused_odnos(
        st, [(None, N, _sell_row(5))])
    assert ambiguous == {}


def test_다른_날짜_매도_행은_효과_기록이_있으면_구분_불가로_본다():
    st = _state(_sell_meta(2), last_updated=MID_WM)
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}
    old_sell = _old_row(5, 50.0)
    old_sell["sll_buy_dvsn_cd_name"] = "매도"
    assert list(state_module._ambiguous_reused_odnos(st, [(None, N, old_sell)])) == [N]


# ───────── 날짜 불명 행의 우선순위 ─────────

def _unknown_row(filled=0):
    row = _new_row(filled, 10.0)
    row["ord_dt"] = ""                                   # 날짜를 알 수 없는 행 (세션 판정 불가)
    row.pop("ord_tmd")
    row["ord_datetime_utc"] = ""
    return row


def test_날짜가_일치하는_행이_있으면_날짜_불명_행보다_우선한다():
    index = state_module._index_history_by_odno([_unknown_row(9), _new_row(5)])
    picked = state_module._history_row_for_meta(index, N, _meta())
    assert picked["ft_ccld_qty"] == "5" and picked["ord_dt"] == "20261124"


def test_다른_날짜_행이_보이는_번호의_날짜_불명_행은_근거가_부족해_쓰지_않는다():
    index = state_module._index_history_by_odno([_old_row(10), _unknown_row(9)])
    assert state_module._history_row_for_meta(index, N, _meta()) is None


def test_날짜_불명_행뿐이면_충돌_근거가_없으므로_기존처럼_쓴다():
    index = state_module._index_history_by_odno([_unknown_row(9)])
    assert state_module._history_row_for_meta(index, N, _meta())["ft_ccld_qty"] == "9"


def test_날짜_없는_레거시_메타는_기존_호환_정책_체결_행_우선을_유지한다():
    meta = _meta()
    del meta["submitted_session"]
    index = state_module._index_history_by_odno([_old_row(10), _new_row(0)])
    assert state_module._history_row_for_meta(index, N, meta)["ord_dt"] == "20261118"


# ───────── KIS 모의 외 브로커·모드: 기존 동작 ─────────

@pytest.mark.parametrize("broker,mode", [("kis", "real"), ("kiwoom", "demo"), ("nhplug", "demo"), ("ls", "real")])
def test_KIS_모의가_아니면_구분_불가_보류를_만들지_않는다(monkeypatch, broker, mode):
    monkeypatch.setattr("config.BROKER", broker)
    monkeypatch.setattr("config.BROKER_MODE", mode)
    st = _state(_sell_meta(0), last_updated=EARLY_WM)
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}
    candidates = [(None, N, _old_row(10, 50.0)), (None, N, _sell_row(5))]
    assert state_module._ambiguous_reused_odnos(st, candidates) == {}
    assert state_module._row_date_relation(_old_row(), _meta()) == "match"
