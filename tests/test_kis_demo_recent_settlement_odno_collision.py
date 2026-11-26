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


# ───────── 보류 해소·오탐 경계 (리뷰 반영) ─────────

def test_사이클_종료로_매도_효과가_비워진_뒤_번호가_재사용돼도_보류하지_않는다():
    st = _state(_sell_meta(0), last_updated=MID_WM)
    st["sell_t_effects"] = {}                                 # 전량매도 사이클 종료 시 비워짐
    assert state_module._ambiguous_reused_odnos(st, [(None, N, _sell_row(5))]) == {}


def test_매도_효과_기록을_정리하면_같은_행이_정상_정산으로_복귀한다():
    """보류는 매 실행 같은 조건으로 반복됩니다. 아래 기록 정리는 **로직 동작을 보여 주는 단위 수준 시뮬레이션**일 뿐
    승인된 운영 복구 절차가 아닙니다(전용 복구 도구 없음 — 실제 복구는 별도 분석·백업·사용자 승인 후)."""
    st = _state(_sell_meta(0), last_updated=MID_WM)
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}
    for _ in range(2):                                        # 같은 입력은 매 실행 같은 보류 (워터마크가 전진하지 않음)
        held = _settle(deepcopy(st), [_sell_row(5)])
        assert held.get("_settlement_hold") is True
        assert held["_pending_amount_odnos"] == [N]

    st["sell_t_effects"] = {}                                 # 같은 번호의 과거 주문 기록 정리 후
    recovered = _settle(deepcopy(st), [_sell_row(5)])
    assert not recovered.get("_settlement_hold")
    assert recovered["orders_meta"][N]["processed_filled_qty"] == 5


def test_두_행_중_하나만_메타와_날짜가_맞아도_번호_키_충돌이므로_보류하고_해당_번호를_알린다():
    st = _state(_meta(0, 0.0), last_updated=EARLY_WM)
    result = _settle(st, [_old_row(10, 50.0), _new_row(5, 10.0)])
    assert result["_pending_amount_odnos"] == [N]
    reason = result["_settlement_hold_reason"]
    # 지원되지 않는 복구 절차를 안내하지 않고, 수동 확인·전용 도구 없음·승인 필요를 알린다.
    assert "브로커 앱" in reason and "전용 복구 도구는 아직 없습니다" in reason and "사용자 승인" in reason
    assert "REPAIR 절차" not in reason and "docs/agents/state-repair.md" not in reason
    assert "권장하지 않습니다" in reason           # FORCE_T_REINFERENCE·sell_t_effects 삭제·워터마크 수동 변경은 안내하지 않음


def test_메타가_없는_매도_행은_효과_기록이_있어도_기존_동작이다():
    """미등록 매도 행의 번호가 sell_t_effects 에 있는 경우는 이 가드의 대상이 아님 (기존 동작, 잔여 위험으로 문서화)."""
    st = _state(None, last_updated=MID_WM)
    st["orders_meta"] = {}
    st["sell_t_effects"] = {N: {"basis": 20, "factor": 0.75}}
    assert state_module._ambiguous_reused_odnos(st, [(None, N, _sell_row(5))]) == {}


@pytest.mark.xfail(strict=True, reason=(
    "알려진 한계(후속 과제): additional_loc_odno 는 번호만으로 추가매수를 판정하고 정리되지 않음 — "
    "같은 번호가 재사용된 신규 일반 매수가 날짜 확인 없이 추가매수(T 효과 없음)로 분류될 수 있음. 상태 스키마 변경이 필요해 이번 범위 밖"))
def test_additional_loc_odno_에_남은_번호가_재사용된_신규_일반_매수는_추가매수로_분류되지_않는다():
    meta = _meta(0, 0.0)                                       # 새 일반 매수 (is_additional=False)
    st = _state(meta, last_updated=MID_WM)
    st["additional_loc_odno"] = [N]                            # 과거 미등록 추가매수가 같은 번호
    result = _settle(st, [_new_row(5, 10.0)])
    assert result["T"] == pytest.approx(5.5, abs=1e-3)         # 일반 매수이므로 T 가 올라야 함
