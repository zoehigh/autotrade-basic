"""KIS 모의: 만료 가정(terminal_assumed)된 리버스 주문의 뒤늦은 체결 반영 회귀 테스트.

배경(2026-11 KIS TQQQ 실측):
  - 11/20 에 낸 리버스 1일차 매도 A(12주)는 체결되지 않아 11/24 정산에서 `terminal_assumed` 로 종결됐고,
    같은 실행에서 새 1일차 매도 B(12주)가 접수돼 체결됐습니다.
  - A 가 나중에 체결돼도 상태가 소실되거나 중복 반영되지 않는지(주문번호 단위 누적 체결 델타),
    KIS 모의의 주문번호 재사용(날짜가 다르면 다른 주문)을 날짜로 구분하는지 고정합니다.

이 파일은 정산 로직을 바꾸지 않고 **현재 동작을 고정**합니다. 결함으로 확인된 항목은 xfail(strict) 로 표시했습니다.
"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import state as state_module
from state import reconcile_reverse_fills

A_ODNO = "39445"       # 11/20 세션 1일차 매도 (미체결 → 만료 가정)
B_ODNO = "39233"       # 11/24 세션 1일차 매도 (체결) — 번호가 더 작음: 날짜 간 번호가 시간순이 아님
BASE_T = 19.2773
FACTOR = 0.9


@pytest.fixture(autouse=True)
def _kis_demo(monkeypatch):
    monkeypatch.setattr("config.BROKER", "kis")
    monkeypatch.setattr("config.BROKER_MODE", "demo")


def _utc(ord_dt, ord_tmd):
    """KIS 이력 규칙(ord_dt=미국 영업일, ord_tmd=KST 시각)을 어댑터와 같이 KST 로 취급해 UTC 로 바꿉니다."""
    kst = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return kst.astimezone(ZoneInfo("UTC")).isoformat()


def _row(odno, ord_dt, ord_tmd, filled, amount, total=12):
    return {
        "odno": odno, "ord_dt": ord_dt, "ord_tmd": ord_tmd, "ord_datetime_utc": _utc(ord_dt, ord_tmd),
        "ft_ccld_qty": str(filled), "ft_ccld_amt3": str(amount), "ft_ccld_unpr3": "46.17",
        "nccs_qty": str(total - filled), "sll_buy_dvsn_cd_name": "매도",
        "prcs_stat_name": "체결" if filled == total else ("부분체결" if filled else "미체결"),
    }


def _a_row(filled=0, amount=0.0):
    return _row(A_ODNO, "20261120", "041049", filled, amount)


def _b_row(filled=12, amount=554.09):
    return _row(B_ODNO, "20261124", "041136", filled, amount)


def _meta(submitted_at, submitted_session, **extra):
    meta = dict(
        side="SELL", total_qty=12, reverse_action="sell", reverse_day=1, cycle_id="C1",
        reverse_base_t=BASE_T, reverse_t_factor=FACTOR, fill_marker_version=1,
        processed_filled_qty=0, processed_filled_amount=0.0, applied_fill_fraction=0.0,
        submitted_at=submitted_at, submitted_session=submitted_session,
    )
    meta.update(extra)
    return meta


def _state():
    """A(만료 가정 종결, 미반영) + B(12/12 반영 완료) 이후의 상태 (T = 19.2773 - 1.9277)."""
    st = {
        "T": 17.3496, "net_invested": 7420.75, "last_updated": "2026-11-23T19:11:36+00:00",
        "last_processed_ordno": B_ODNO,
        "reverse_mode": {"active": True, "cycle_id": "C1", "day_count": 1},
        "orders_meta": {
            A_ODNO: _meta("20261121041049", "2026-11-20", terminal=True, terminal_assumed=True),
            B_ODNO: _meta("20261125041136", "2026-11-24", processed_filled_qty=12,
                          processed_filled_amount=554.09, applied_fill_fraction=1.0, terminal=True),
        },
    }
    state_module._bootstrap_settlement_start(st, BASE_T)
    entry = state_module._append_settlement_entry(st, "reverse_sell", B_ODNO, delta=-1.9277)
    entry.update(base_t=BASE_T, factor=FACTOR, applied_before=0.0, fill_fraction=1.0)
    st["T"] = state_module._replay_settlement_ledger(st)
    assert st["T"] == pytest.approx(17.3496)
    return st


def _ledger_entries(st, odno):
    return [e for e in st["settlement_ledger"]["entries"] if str(e.get("odno")) == str(odno)]


def test_늦게_체결된_만료_가정_주문이_반영되고_표식은_유지된다():
    st = _state()
    before_wm = st["last_updated"]

    reconcile_reverse_fills(st, [_a_row(12, 554.0), _b_row()])

    meta = st["orders_meta"][A_ODNO]
    assert meta["processed_filled_qty"] == 12
    assert meta["processed_filled_amount"] == pytest.approx(554.0)
    assert meta["applied_fill_fraction"] == pytest.approx(1.0)
    assert meta["terminal"] is True and meta["terminal_assumed"] is True       # 실제 만료 확인이 아니라는 구분 유지
    # T: 주문별 기준선(base_t) 기준 가산 — 두 주문 모두 실제 매도이므로 각각 -1.9277
    assert st["T"] == pytest.approx(17.3496 - 1.9277, abs=1e-4)
    assert st["net_invested"] == pytest.approx(7420.75 - 554.0)
    assert not st.get("_settlement_hold")
    assert st["reverse_mode"]["day_count"] == 1                                  # 늦은 체결은 일차를 올리지 않음
    assert st["last_updated"] >= before_wm                                       # 워터마크 역행 없음
    entries = _ledger_entries(st, A_ODNO)
    assert len(entries) == 1 and entries[0]["kind"] == "reverse_sell"
    assert entries[0]["applied_before"] == 0.0 and entries[0]["fill_fraction"] == pytest.approx(1.0)
    # 신규 주문 B 의 기존 반영은 그대로
    assert st["orders_meta"][B_ODNO]["processed_filled_qty"] == 12
    assert len(_ledger_entries(st, B_ODNO)) == 1


def test_같은_이력으로_반복_정산해도_중복_반영되지_않는다():
    st = _state()
    history = [_a_row(12, 554.0), _b_row()]
    reconcile_reverse_fills(st, history)
    snapshot = deepcopy(st)

    for _ in range(3):
        reconcile_reverse_fills(st, history)

    assert st["T"] == snapshot["T"]
    assert st["net_invested"] == snapshot["net_invested"]
    assert st["orders_meta"] == snapshot["orders_meta"]
    assert len(st["settlement_ledger"]["entries"]) == len(snapshot["settlement_ledger"]["entries"])


def test_부분체결이_나중에_늘어도_누적_델타만_반영되어_일괄_반영과_같다():
    split = _state()
    reconcile_reverse_fills(split, [_a_row(5, 230.0), _b_row()])
    assert split["orders_meta"][A_ODNO]["processed_filled_qty"] == 5
    assert split["T"] == pytest.approx(17.3496 - 1.9277 * 5 / 12, abs=1e-3)
    reconcile_reverse_fills(split, [_a_row(12, 554.0), _b_row()])

    whole = _state()
    reconcile_reverse_fills(whole, [_a_row(12, 554.0), _b_row()])

    assert split["T"] == pytest.approx(whole["T"], abs=1e-3)
    assert split["net_invested"] == pytest.approx(whole["net_invested"])
    assert split["orders_meta"][A_ODNO]["processed_filled_qty"] == 12
    fractions = [e["fill_fraction"] for e in _ledger_entries(split, A_ODNO)]
    assert fractions == sorted(fractions) and fractions[-1] == pytest.approx(1.0)


def test_이력에서_체결_수량이_줄어든_응답은_이미_반영한_T를_되돌리지_않는다():
    st = _state()
    reconcile_reverse_fills(st, [_a_row(12, 554.0), _b_row()])
    applied = (st["T"], st["net_invested"])

    reconcile_reverse_fills(st, [_a_row(0, 0.0), _b_row()])     # 일시적으로 0체결로 보이는 응답

    assert (st["T"], st["net_invested"]) == applied
    assert st["orders_meta"][A_ODNO]["processed_filled_qty"] == 12


def test_번호가_같은_다른_날짜의_체결_행은_만료_가정_주문에_반영되지_않는다():
    """KIS 모의는 주문번호가 날짜 간 재사용됩니다. A(11/20, 0체결)와 같은 번호의 11/24 체결 행이 와도 A 에 반영 금지."""
    st = _state()
    other_day_filled = _row(A_ODNO, "20261124", "051500", 12, 560.0)    # 같은 번호, 다른 날짜의 다른 주문
    before = (st["T"], st["net_invested"])

    reconcile_reverse_fills(st, [_a_row(0, 0.0), other_day_filled, _b_row()])

    assert st["orders_meta"][A_ODNO]["processed_filled_qty"] == 0
    assert (st["T"], st["net_invested"]) == before
    assert _ledger_entries(st, A_ODNO) == []


def test_번호가_같은_이전_날짜의_체결_행은_새_주문에_반영되지_않는다():
    """새 주문(11/24)의 번호가 예전(11/20) 체결 주문 번호와 같아도, 날짜가 다르면 새 주문에 반영하지 않습니다."""
    st = _state()
    st["orders_meta"][B_ODNO].update(processed_filled_qty=0, processed_filled_amount=0.0,
                                      applied_fill_fraction=0.0, terminal=False)
    st["settlement_ledger"]["entries"].clear()
    st["T"] = BASE_T
    st["net_invested"] = 7974.84
    st["settlement_ledger"]["start_T"] = BASE_T
    old_day_filled = _row(B_ODNO, "20261120", "030000", 12, 560.0)       # 같은 번호의 11/20 체결 주문
    open_row = _b_row(0, 0.0)                                            # 11/24 새 주문은 미체결
    before = (st["T"], st["net_invested"])

    reconcile_reverse_fills(st, [old_day_filled, open_row])

    assert st["orders_meta"][B_ODNO]["processed_filled_qty"] == 0
    assert (st["T"], st["net_invested"]) == before


# ───────── 일반(비리버스) 주문 경로: 같은 번호의 이전 날짜 체결 행 ─────────

def _normal_meta(**extra):
    meta = dict(side="BUY", total_qty=5, t_target=1.0, fill_marker_version=1,
                processed_filled_qty=0, processed_filled_amount=0.0,
                submitted_at="20261125041136", submitted_session="2026-11-24")
    meta.update(extra)
    return meta


@pytest.mark.xfail(strict=True, reason=(
    "결함 후보: 일반 주문의 지연 체결 복구(_apply_late_fill_deltas)는 주문번호만으로 이력 행을 찾고 "
    "제출 날짜를 대조하지 않음 — KIS 모의의 번호 재사용 시 이전 날짜의 체결 행이 새 미체결 주문에 반영될 수 있음 "
    "(리버스 경로 reconcile_reverse_fills 는 _order_matches_submitted_date 로 막고 있음)"))
def test_일반_주문_번호가_같은_이전_날짜_체결_행은_새_주문의_지연_체결로_반영되지_않는다():
    st = {"T": 5.0, "net_invested": 500.0, "last_updated": "2026-11-23T19:00:00+00:00",
          "last_processed_ordno": "1", "reverse_mode": {}, "orders_meta": {"777": _normal_meta()}}
    old_day_filled = {**_row("777", "20261118", "041000", 5, 250.0, total=5), "sll_buy_dvsn_cd_name": "매수"}
    new_open = {**_row("777", "20261124", "041136", 0, 0.0, total=5), "sll_buy_dvsn_cd_name": "매수"}

    pending, net_qty, net_amount, held = state_module._apply_late_fill_deltas(
        st, [old_day_filled, new_open], skip_odnos=set())

    assert pending == [] and net_qty == 0 and net_amount == 0.0
