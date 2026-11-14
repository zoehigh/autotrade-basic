"""체결 전용 이력(KIS)에서 미체결로 끝난 이전 세션 리버스 주문이 '이력 누락'으로 보류되지 않는지 검증합니다.

사고(2026-11-12 KIS SOXL 데모): 리버스 2일차 쿼터매수 4주가 체결되지 않아 이력에 행이 없었고
"리버스 주문 이력 누락 → 신규 주문 보류"로 SOXL 이 멈췄습니다. KIS 이력은 CCLD_NCCS_DVSN="01"(체결만)입니다.
"""
from copy import deepcopy
from unittest.mock import patch

import pytest

from state import reconcile_reverse_fills

CYCLE = "C1"


def _sell_row(odno="1001", qty="10", amt="548.40", ord_dt="20261110", ord_tmd="041344"):
    return {
        "odno": odno, "ord_dt": ord_dt, "ord_tmd": ord_tmd,
        "ft_ccld_qty": qty, "ft_ccld_unpr3": "54.84", "ft_ccld_amt3": amt,
        "nccs_qty": "0", "prcs_stat_name": "체결",
    }


def _state():
    return {
        "T": 17.6836, "net_invested": 9000.0,
        "last_updated": "2026-11-09T19:13:43+00:00",
        "reverse_mode": {"active": True, "day_count": 1, "cycle_id": CYCLE},
        "orders_meta": {
            "1001": {  # 1일차 매도 — 체결됨 (이력에 행 있음)
                "side": "SELL", "total_qty": 10, "processed_filled_qty": 10, "processed_filled_amount": 548.4,
                "applied_fill_fraction": 1.0, "terminal": True,
                "reverse_action": "sell", "reverse_day": 1, "reverse_base_t": 19.6484, "reverse_t_factor": 0.9,
                "cycle_id": CYCLE, "submitted_at": "20261111041344", "submitted_session": "2026-11-10",
            },
            "2002": {  # 2일차 쿼터매수 — 미체결, 이력에 행 없음
                "side": "BUY", "total_qty": 4, "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                "fill_marker_version": 1,
                "reverse_action": "buy", "reverse_day": 2, "reverse_t_target": 0.5,
                "cycle_id": CYCLE, "submitted_at": "20261112041106", "submitted_session": "2026-11-11",
            },
        },
    }


# 2일차 세션(2026-11-11)과 같은 날짜의 체결 행이 있음 = 조회 구간이 그 세션을 덮음
HISTORY = [_sell_row(), _sell_row(odno="1003", qty="9", amt="609.39", ord_dt="20261111", ord_tmd="041105")]


def _run(state, history=HISTORY, broker="kis", mode="demo", today="2026-11-12"):
    with patch("config.BROKER", broker), patch("config.BROKER_MODE", mode), \
         patch("state.datetime") as dt:
        import datetime as real
        dt.now.return_value = real.datetime.fromisoformat(f"{today}T12:00:00+00:00")
        dt.fromisoformat.side_effect = real.datetime.fromisoformat
        dt.strptime.side_effect = real.datetime.strptime
        reconcile_reverse_fills(state, deepcopy(history))
    return state


def test_KIS_데모에서_이력에_없는_미체결_리버스_주문은_만료로_종결하고_보류하지_않는다():
    s = _run(_state())
    meta = s["orders_meta"]["2002"]
    assert meta["terminal"] is True and meta["terminal_assumed"] is True
    assert meta["terminal_assumption_reason"] == "missing_from_filled_only_history_after_session"
    assert "reconciliation_only" not in s["reverse_mode"] and "reconciliation_error" not in s["reverse_mode"]
    assert not s.get("_settlement_hold")


def test_이미_보류_표식이_있던_상태도_종결_후_해제된다():
    st = _state()
    st["reverse_mode"].update(reconciliation_only=True, reconciliation_error="reverse_order_history_missing")
    s = _run(st)
    assert "reconciliation_only" not in s["reverse_mode"]


def test_종결된_뒤_다음_실행에서도_다시_보류하지_않는다():
    s = _run(_state())
    s = _run(s)  # 같은 이력(주문 행 없음)으로 한 번 더
    assert "reconciliation_only" not in s["reverse_mode"]
    assert s["orders_meta"]["2002"]["terminal"] is True


def test_체결된_리버스_매도의_T_반영은_그대로():
    st = _state()
    st["orders_meta"]["1003"] = {
        "side": "SELL", "total_qty": 9, "processed_filled_qty": 0, "processed_filled_amount": 0.0,
        "reverse_action": "sell", "reverse_day": 2, "reverse_base_t": 17.6836, "reverse_t_factor": 0.9,
        "cycle_id": CYCLE, "submitted_at": "20261112041105", "submitted_session": "2026-11-11",
    }
    s = _run(st)
    assert s["T"] == 15.9152 and s["reverse_mode"]["day_count"] == 2
    assert s["orders_meta"]["2002"]["terminal"] is True


@pytest.mark.parametrize("broker,mode", [("kiwoom", "demo"), ("nhplug", "demo"), ("ls", "demo"), ("kis", "real")])
def test_다른_브로커나_실전은_기존대로_보류(broker, mode):
    s = _run(_state(), broker=broker, mode=mode)
    assert s["reverse_mode"]["reconciliation_only"] is True
    assert s["reverse_mode"]["reconciliation_error"] == "reverse_order_history_missing"
    assert not s["orders_meta"]["2002"].get("terminal")


def test_이력이_비었으면_보류():
    s = _run(_state(), history=[])
    assert s["reverse_mode"].get("reconciliation_only") is True


def test_조회_구간이_그_세션을_덮지_않으면_보류():
    old_only = [_sell_row()]  # ord_dt=20261110 < 주문 세션 20261111 (이후 행 없음)
    s = _run(_state(), history=old_only)
    assert s["reverse_mode"]["reconciliation_only"] is True


def test_주문이_조회_시작일보다_오래됐으면_이후_행만_있어도_보류():
    """Codex P2: 조회 한도(30일 등)보다 오래된 주문은 이후 행이 있어도 구간 밖일 수 있습니다."""
    st = _state()
    st["orders_meta"]["2002"]["submitted_session"] = "2026-09-01"
    newer_only = [_sell_row(ord_dt="20261110"), _sell_row(odno="1003", ord_dt="20261111")]
    s = _run(st, history=newer_only)
    assert s["reverse_mode"]["reconciliation_only"] is True
    assert not s["orders_meta"]["2002"].get("terminal")


def test_리버스_행이_하나도_없는_이력에서도_마지막_주문을_종결하고_표식을_해제():
    """Codex P2: 이벤트가 없어 조기 return 해도 해제 정리가 실행돼야 합니다."""
    st = _state()
    st["orders_meta"]["1001"]["terminal"] = True
    st["reverse_mode"].update(reconciliation_only=True, reconciliation_error="reverse_order_history_missing")
    non_reverse_only = [_sell_row(odno="9999", ord_dt="20261109"), _sell_row(odno="9998", ord_dt="20261111")]
    s = _run(st, history=non_reverse_only)   # 1001 도 이력에 없음(종결됨), 2002 는 만료 가정
    assert s["orders_meta"]["2002"]["terminal"] is True
    assert "reconciliation_only" not in s["reverse_mode"] and "reconciliation_error" not in s["reverse_mode"]


def test_같은_세션의_주문은_아직_만료가_아니므로_건드리지_않는다():
    s = _run(_state(), today="2026-11-11")  # 오늘 세션 = 주문 세션
    assert not s["orders_meta"]["2002"].get("terminal")
    assert "reconciliation_only" not in s["reverse_mode"]


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(processed_filled_qty=1),
    lambda m: m.update(processed_filled_amount=10.0),
    lambda m: m.update(submitted_session=""),
    lambda m: m.update(processed_filled_qty=0.5),       # 소수 수량은 0 으로 잘리면 안 됨
    lambda m: m.update(processed_filled_qty="0"),       # 문자열 0 불인정
    lambda m: m.update(processed_filled_amount="0.0"),
    lambda m: m.update(processed_filled_qty=None),
    lambda m: m.update(processed_filled_amount=""),
    lambda m: m.update(processed_filled_qty=True),
    lambda m: m.update(processed_filled_qty=float("nan")),
    lambda m: m.update(applied_fill_fraction=0.25),     # 반영 흔적
    lambda m: m.update(applied_fill_fraction="x"),
    lambda m: m.pop("processed_filled_qty"),            # 마커 누락
    lambda m: m.pop("processed_filled_amount"),
])
def test_반영_흔적이나_세션_정보_이상이면_보류(mutate):
    st = _state()
    mutate(st["orders_meta"]["2002"])
    s = _run(st)
    assert s["reverse_mode"].get("reconciliation_only") is True
    assert not s["orders_meta"]["2002"].get("terminal_assumed")


def test_늦게_체결_행이_생기면_델타로_반영된다():
    s = _run(_state())  # 만료 가정
    late = HISTORY + [{
        "odno": "2002", "ord_dt": "20261111", "ord_tmd": "041106", "ft_ccld_qty": "4",
        "ft_ccld_unpr3": "55.0", "ft_ccld_amt3": "220.00", "nccs_qty": "0", "prcs_stat_name": "체결",
    }]
    s = _run(s, history=late)
    assert s["orders_meta"]["2002"]["processed_filled_qty"] == 4
    assert s["net_invested"] > 9000.0 - 1  # 매수 금액 반영


@pytest.mark.parametrize("broker,mode", [("kis", "demo"), ("kis", "real"), ("kiwoom", "demo"), ("nhplug", "demo"), ("ls", "demo")])
def test_이미_종결된_주문은_이력에_없어도_브로커_모드_무관하게_보류하지_않는다(broker, mode):
    """공통 동작: 종결된 주문의 이력 누락은 무시 (KIS 데모 한정 아님)."""
    st = _state()
    st["orders_meta"]["2002"]["terminal"] = True
    s = _run(st, broker=broker, mode=mode)
    assert "reconciliation_only" not in s["reverse_mode"]
    assert s["orders_meta"]["2002"]["terminal"] is True
    assert not s["orders_meta"]["2002"].get("terminal_assumed")


@pytest.mark.parametrize("broker,mode", [("kis", "real"), ("kiwoom", "demo")])
def test_이벤트_없는_실행에서_모두_종결이면_보류_해제는_공통이지만_미종결이면_유지(broker, mode):
    st = _state()
    st["reverse_mode"].update(reconciliation_only=True, reconciliation_error="reverse_order_history_missing")
    # 미종결 주문이 남아 있으면 해제하지 않음
    s = _run(deepcopy(st), history=[_sell_row(odno="9", ord_dt="20261101")], broker=broker, mode=mode)
    assert s["reverse_mode"].get("reconciliation_only") is True
    # 전부 종결이면 해제
    st["orders_meta"]["2002"]["terminal"] = True
    s = _run(st, history=[_sell_row(odno="9", ord_dt="20261101")], broker=broker, mode=mode)
    assert "reconciliation_only" not in s["reverse_mode"]
