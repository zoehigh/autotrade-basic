"""KIS 모의 주문번호 재사용 — 일반(비리버스) 주문의 지연 체결·정산 경로 회귀 테스트.

KIS 모의 주문번호는 날짜 간 재사용됩니다. 같은 번호의 **다른 날짜** 이력 행을 새 주문의 체결로 반영하면 안 되고,
정상 지연 체결(부분체결 증가)은 날짜가 맞는 행으로 반영해야 합니다. 적용 범위는 KIS 모의뿐이며
KIS 실전·다른 브로커는 기존 동작(같은 번호면 마지막 행)을 유지합니다.
"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import state as state_module
from state import update_T_from_history

N = "777"                       # 재사용되는 주문번호
SYMBOL = "TQQQ"
NEW_AT, NEW_SESSION = "20261125041136", "2026-11-24"      # 새 주문: 11/24 세션, 접수 KST 11/25 04:11:36


@pytest.fixture(autouse=True)
def _kis_demo(monkeypatch):
    monkeypatch.setattr("config.BROKER", "kis")
    monkeypatch.setattr("config.BROKER_MODE", "demo")


def _krow(odno, ord_dt, ord_tmd, filled, price, total, side="매수"):
    kst = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return {
        "odno": odno, "ord_dt": ord_dt, "ord_tmd": ord_tmd,
        "ord_datetime_utc": kst.astimezone(ZoneInfo("UTC")).isoformat(),
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": str(filled), "ft_ccld_amt3": str(round(filled * price, 2)), "ft_ccld_unpr3": str(price),
        "nccs_qty": str(total - filled),
        "prcs_stat_name": "체결" if filled == total else ("부분체결" if filled else "미체결"),
    }


def _old_row(filled=10, price=50.0):
    """11/18 에 체결된 다른 주문 (같은 번호 N)."""
    return _krow(N, "20261118", "041000", filled, price, 10)


def _new_row(filled, price=10.0, total=10):
    """11/24 세션의 새 주문 (같은 번호 N)."""
    return _krow(N, "20261124", "041136", filled, price, total)


def _meta(processed_qty=2, processed_amount=20.0, **extra):
    meta = {"side": "BUY", "total_qty": 10, "t_target": 1.0, "is_additional": False,
            "fill_marker_version": 1, "processed_filled_qty": processed_qty,
            "processed_filled_amount": processed_amount,
            "submitted_session": NEW_SESSION}          # 운영 일반 주문 메타 형태: submitted_at 없이 submitted_session 만
    meta.update(extra)
    return meta


def _meta_with_at(**extra):
    """submitted_at 까지 있는 메타 (리버스 주문 형태)."""
    return _meta(submitted_at=NEW_AT, **extra)


def _state(meta=None, **kw):
    st = {"T": 5.0, "net_invested": 520.0, "last_updated": "2026-11-24T10:00:00+00:00",
          "last_processed_ordno": "ZZZ", "orders_meta": {N: meta if meta is not None else _meta()},
          "cycle_start_date": "2026-11-01", "additional_loc_odno": [], "balance_mismatch": {},
          "net_invested_status": "valid", "reverse_mode": {}}
    st.update(kw)
    return st


def _settle(st, history, qty=5):
    return update_T_from_history(SYMBOL, st, history, balance_qty=qty)


def _clean(result):
    assert not result.get("_settlement_hold"), result.get("_settlement_hold_reason")
    assert not result.get("_position_basis_unknown")


def test_동일_번호의_과거_체결과_신규_미체결은_신규_주문에_반영되지_않는다():
    st = _state()
    result = _settle(st, [_old_row(), _new_row(0)])
    _clean(result)
    assert result["T"] == pytest.approx(5.0)
    assert result["net_invested"] == pytest.approx(520.0)
    assert result["orders_meta"][N]["processed_filled_qty"] == 2
    assert result["orders_meta"][N]["processed_filled_amount"] == pytest.approx(20.0)


@pytest.mark.parametrize("old_first", [True, False])
def test_동일_번호의_서로_다른_날짜_체결은_행_순서와_무관하게_제출일이_맞는_행만_반영한다(old_first):
    history = [_old_row(), _new_row(5)] if old_first else [_new_row(5), _old_row()]
    result = _settle(_state(), history)
    _clean(result)
    # 새 주문 누적 5주($50) 중 이미 2주($20) 반영 → 3주 $30 만 추가: T +0.3, net +30
    assert result["T"] == pytest.approx(5.3, abs=1e-3)
    assert result["net_invested"] == pytest.approx(550.0, abs=0.01)
    assert result["orders_meta"][N]["processed_filled_qty"] == 5
    assert result["orders_meta"][N]["processed_filled_amount"] == pytest.approx(50.0)


def test_신규_주문의_부분체결과_지연_체결은_정상_반영되고_반복_정산에도_멱등이다():
    st = _state()
    history_1 = [_old_row(), _new_row(5)]
    first = _settle(st, history_1)
    _clean(first)
    assert first["T"] == pytest.approx(5.3, abs=1e-3)

    # 같은 이력으로 반복 → 변화 없음
    again = _settle(deepcopy(first), history_1)
    assert (again["T"], again["net_invested"]) == (first["T"], first["net_invested"])
    assert again["orders_meta"][N]["processed_filled_qty"] == 5

    # 이후 지연 체결로 누적 10주 → 나머지 5주($50)만 추가
    history_2 = [_old_row(), _new_row(10)]
    second = _settle(deepcopy(first), history_2, qty=10)
    _clean(second)
    assert second["T"] == pytest.approx(5.8, abs=1e-3)
    assert second["net_invested"] == pytest.approx(600.0, abs=0.01)
    assert second["orders_meta"][N]["processed_filled_qty"] == 10
    final = _settle(deepcopy(second), history_2, qty=10)
    assert (final["T"], final["net_invested"]) == (second["T"], second["net_invested"])


def test_제출일이_맞는_행이_없으면_다른_날짜_행을_쓰지_않고_보류도_만들지_않는다():
    st = _state()
    result = _settle(st, [_old_row()])               # 이력에 새 주문 행이 아직 없음 (이전 날짜의 같은 번호 행뿐)
    _clean(result)
    assert result["T"] == pytest.approx(5.0)
    assert result["orders_meta"][N]["processed_filled_qty"] == 2


def test_날짜_정보가_없는_레거시_메타는_후보가_한_날짜뿐이면_기존처럼_반영한다():
    meta = _meta()
    del meta["submitted_session"]
    result = _settle(_state(meta), [_new_row(5)])
    _clean(result)
    assert result["T"] == pytest.approx(5.3, abs=1e-3)
    assert result["orders_meta"][N]["processed_filled_qty"] == 5


def test_날짜_정보가_없는_레거시_메타는_대조할_근거가_없어_기존_조회표와_같이_체결_행을_우선한다():
    """날짜가 없으면 같은 번호의 어느 날짜 행인지 구분할 수 없습니다. 전체("00")·체결 전용("01") 조회가 같은
    결과가 되도록(#28 불변식) 기존 동작(체결 행 우선)을 유지합니다 — 알려진 한계."""
    meta = _meta()
    del meta["submitted_session"]
    index = state_module._index_history_by_odno([_old_row(), _new_row(0)])
    assert state_module._history_row_for_meta(index, N, meta)["ord_dt"] == "20261118"
    index_01 = state_module._index_history_by_odno([_old_row()])
    assert state_module._history_row_for_meta(index_01, N, meta)["ord_dt"] == "20261118"


@pytest.mark.parametrize("meta_factory", [_meta, _meta_with_at])
def test_일반_메타는_submitted_session_만_있어도_submitted_at_이_있어도_같은_결과다(meta_factory):
    result = _settle(_state(meta_factory()), [_old_row(), _new_row(5)])
    _clean(result)
    assert result["T"] == pytest.approx(5.3, abs=1e-3)
    assert result["orders_meta"][N]["processed_filled_qty"] == 5
    untouched = _settle(_state(meta_factory()), [_old_row(), _new_row(0)])
    assert untouched["T"] == pytest.approx(5.0)


# ───────── 선택 함수 단위 동작 ─────────

def test_행_선택은_전체_후보에서_제출일이_맞는_행을_먼저_고른_뒤_체결_행을_결정한다():
    index = state_module._index_history_by_odno([_old_row(), _new_row(0)])
    meta = _meta()
    picked = state_module._history_row_for_meta(index, N, meta)
    assert picked["ord_dt"] == "20261124" and picked["ft_ccld_qty"] == "0"       # 과거 체결 행이 아님
    assert index.get(N)["ord_dt"] == "20261118"                                  # 번호만의 조회표는 기존 그대로(체결 우선)


@pytest.mark.parametrize("broker,mode", [("kis", "real"), ("kiwoom", "demo"), ("nhplug", "demo"), ("ls", "real")])
def test_KIS_모의가_아니면_기존_동작_마지막_행이_이긴다(monkeypatch, broker, mode):
    monkeypatch.setattr("config.BROKER", broker)
    monkeypatch.setattr("config.BROKER_MODE", mode)
    history = [_new_row(0), _old_row()]               # 마지막 행 = 과거 체결 행
    index = state_module._index_history_by_odno(history)
    assert index.get(N)["ord_dt"] == "20261118"
    assert state_module._history_row_for_meta(index, N, _meta())["ord_dt"] == "20261118"
    history = [_old_row(), _new_row(0)]
    index = state_module._index_history_by_odno(history)
    assert state_module._history_row_for_meta(index, N, _meta())["ord_dt"] == "20261124"
    # 날짜가 맞지 않아도 번호로 바로 찾음 (기존 동작)
    assert state_module._history_row_for_meta(
        state_module._index_history_by_odno([_old_row()]), N, _meta())["ord_dt"] == "20261118"


def test_승인_대조의_새_체결_목록도_같은_번호의_과거_체결_행을_새_주문의_체결로_보지_않는다():
    """pipeline 의 승인 대조(`_history_new_fill_odnos`)가 정산과 같은 선택 규칙을 써야 감사가 어긋나지 않습니다."""
    import pipeline
    st = _state()
    assert pipeline._history_new_fill_odnos(st, [_old_row(), _new_row(0)]) == set()
    assert pipeline._history_new_fill_odnos(st, [_old_row(), _new_row(5)]) == {N}      # 정상 지연 체결은 포함
