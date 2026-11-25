"""KIS 모의 주문이력 조회를 CCLD_NCCS_DVSN="00"(전체)로 바꾼 뒤의 동작을 검증합니다.

사고(2026-11-20~24 KIS TQQQ 모의): 리버스 1일차 매도 1건(지정가 $43.22, 12주)이 끝내 체결되지 않았는데,
이력 조회가 "01"(체결만)이라 그 주문 행이 응답에 없었고 같은 구간의 다른 행은 모두 세션 날짜보다 이전이라
"이력 누락 → 신규 주문 보류"가 풀리지 않았습니다. 실측으로 "00" 응답에는 그 주문이 체결 0·미체결 12 로 들어 있었습니다.

외부 호출·실주문 없음 (브로커는 모의 객체).
"""
import os
import sys
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

_repo_root = os.path.join(os.path.dirname(__file__), "..")
for _p in (_repo_root, os.path.join(_repo_root, "src"), os.path.join(_repo_root, "ops")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import trading_bot
import state as state_module
from broker.kis.adapter import KISBroker
from broker.base import OrderResult
from state import reconcile_reverse_fills, update_T_from_history

# 실제 파이프라인 하네스(고정 시계·모의 브로커)를 재사용합니다.
from test_reverse_exit_pipeline import (  # noqa: F401  (env 는 pytest fixture)
    _CLOCK, _DAY3_NOW_UTC, _broker, _config, _day1, _next_session, _pipeline_env, _placed, _run_live, _same_session_later, env,
)

CYCLE = "C1"


# ───────────────────────── 어댑터: 요청 파라미터 / 정규화 / 페이지네이션 ─────────────────────────

def _response(rows, tr_cont="", nk="", fk=""):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"tr_cont": tr_cont}
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"rt_cd": "0", "output": rows, "ctx_area_nk200": nk, "ctx_area_fk200": fk}
    return resp


def _kis(mode, pages):
    """네트워크 없이 KISBroker 를 만들고 _request_with_rate_retry 를 pages 순서대로 응답하게 합니다."""
    broker = KISBroker.__new__(KISBroker)
    broker._session = MagicMock()
    broker._mode = mode
    broker._account_no = "12345678"
    broker._acnt_prdt_cd = "01"
    broker._get_token = lambda: "token"
    calls = []

    def _request(method, path, headers=None, params=None, **kw):
        calls.append({"headers": dict(headers), "params": dict(params)})
        return pages[len(calls) - 1]

    broker._request_with_rate_retry = _request
    return broker, calls


def _item(odno, filled, ordered, ord_dt="20261120", side="매도", price="43.22", status=""):
    return {
        "ord_dt": ord_dt, "ord_tmd": "041049", "prdt_name": "TQQQ", "sll_buy_dvsn_cd_name": side,
        "ft_ord_qty": str(ordered), "ft_ccld_qty": str(filled), "ft_ccld_unpr3": price if filled else "0.00000000",
        "ft_ccld_amt3": f"{filled * float(price):.2f}" if filled else "0.00000000",
        "nccs_qty": str(ordered - filled), "prcs_stat_name": status, "tr_mket_name": "", "tr_crcy_cd": "USD",
        "odno": odno, "ovrs_excg_cd": "NASD",
    }


def test_모의는_전체_조회_00_실전은_기존_01_유지():
    for mode, expected in (("demo", "00"), ("real", "01")):
        broker, calls = _kis(mode, [_response([])])
        broker.get_order_history("TQQQ", "NAS", days=8)
        params = calls[0]["params"]
        assert params["CCLD_NCCS_DVSN"] == expected, mode
        # 이번 변경 범위 밖 파라미터는 그대로
        assert params["PDNO"] == "TQQQ" and params["SORT_SQN"] == "AS" and params["SLL_BUY_DVSN"] == "00"
        assert params["OVRS_EXCG_CD"] == "NASD" and params["ODNO"] == "" and params["ORD_DT"] == ""
        assert len(params["ORD_STRT_DT"]) == 8 and len(params["ORD_END_DT"]) == 8


def test_모의_응답의_미체결_행도_정규화에서_사라지지_않는다():
    rows = [_item("0000043210", 0, 12), _item("0000043211", 5, 12, side="매수", price="40.00")]
    broker, _ = _kis("demo", [_response(rows)])
    history = broker.get_order_history("TQQQ", "NAS", days=8)
    assert [h["odno"] for h in history] == ["43210", "43211"]
    unfilled = history[0]
    assert unfilled["ft_ccld_qty"] == "0" and unfilled["nccs_qty"] == "12" and unfilled["ord_dt"] == "20261120"


def test_페이지네이션은_누락도_중복도_없다():
    page1 = _response([_item(str(i), 0, 3, ord_dt="20261119") for i in range(1, 16)], tr_cont="F", nk="NK1", fk="FK1")
    page2 = _response([_item(str(i), 0, 3, ord_dt="20261119") for i in range(16, 31)], tr_cont="M", nk="NK2", fk="FK2")
    page3 = _response([_item(str(i), 0, 3, ord_dt="20261120") for i in range(31, 41)], tr_cont="D")
    broker, calls = _kis("demo", [page1, page2, page3])
    history = broker.get_order_history("TQQQ", "NAS", days=8)
    assert len(calls) == 3
    assert "tr_cont" not in calls[0]["headers"] and calls[0]["params"]["CTX_AREA_NK200"] == ""
    assert calls[1]["headers"]["tr_cont"] == "N" and calls[1]["params"]["CTX_AREA_NK200"] == "NK1"
    assert calls[1]["params"]["CTX_AREA_FK200"] == "FK1"
    assert calls[2]["params"]["CTX_AREA_NK200"] == "NK2"
    odnos = [h["odno"] for h in history]
    assert odnos == [str(i) for i in range(1, 41)] and len(set(odnos)) == 40


# ───────────────────────── 상태: 보류 재현·해소, 체결 원장 영향 없음 ─────────────────────────

def _incident_state():
    """11/20 TQQQ 리버스 1일차 매도 12주가 미체결로 남은 상태 (실측 사고 형태)."""
    return {
        "T": 19.2773, "net_invested": 9000.0, "position_qty": 126,
        "last_updated": "2026-11-19T19:10:00+00:00",
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": CYCLE},
        "orders_meta": {
            "43210": {
                "side": "SELL", "total_qty": 12, "processed_filled_qty": 0, "processed_filled_amount": 0.0,
                "applied_fill_fraction": 0.0, "fill_marker_version": 1,
                "reverse_action": "sell", "reverse_day": 1, "reverse_base_t": 19.2773, "reverse_t_factor": 0.9,
                "cycle_id": CYCLE, "submitted_at": "20261121041049", "submitted_session": "2026-11-20",
            },
        },
    }


def _buy_rows():
    """체결된 매수 (11/16~11/19) — 두 응답(01/00)에 공통."""
    return [_item(str(100 + i), 3, 3, ord_dt=d, side="매수", price="50.00")
            for i, d in enumerate(("20261116", "20261117", "20261118", "20261119"))]


def _history_01():
    return _normalize_via_adapter(_buy_rows())


def _history_00():
    return _normalize_via_adapter(_buy_rows() + [_item("43210", 0, 12)])


def _normalize_via_adapter(rows):
    broker, _ = _kis("demo", [_response(rows)])
    return broker.get_order_history("TQQQ", "NAS", days=8)


def _reconcile(state, history, broker="kis", mode="demo"):
    with patch("config.BROKER", broker), patch("config.BROKER_MODE", mode):
        reconcile_reverse_fills(state, deepcopy(history))
    return state


def test_01_응답이면_사고_그대로_보류되고_00_응답이면_해소된다():
    held = _reconcile(_incident_state(), _history_01())
    assert held["reverse_mode"].get("reconciliation_only") is True
    assert held["reverse_mode"].get("reconciliation_error") == "reverse_order_history_missing"

    resolved = _reconcile(_incident_state(), _history_00())
    meta = resolved["orders_meta"]["43210"]
    assert meta["terminal"] is True and meta["terminal_assumed"] is True
    assert meta["terminal_assumption_reason"] == "auto_expired_demo_day_order_after_session"
    assert "reconciliation_only" not in resolved["reverse_mode"]
    assert "reconciliation_error" not in resolved["reverse_mode"]


def test_미체결_행은_체결_원장과_T를_바꾸지_않는다():
    state = _reconcile(_incident_state(), _history_00())
    meta = state["orders_meta"]["43210"]
    assert state["T"] == 19.2773 and state["net_invested"] == 9000.0
    assert meta["processed_filled_qty"] == 0 and meta["processed_filled_amount"] == 0.0
    assert not (state.get("settlement_ledger") or {}).get("entries")
    assert state["reverse_mode"].get("day_count") == 0


def test_01과_00_응답의_체결_행은_동일하다():
    def filled(history):
        return sorted((h["odno"], h["ft_ccld_qty"], h["ft_ccld_amt3"], h["ord_dt"])
                      for h in history if float(h["ft_ccld_qty"]) > 0)
    assert filled(_history_01()) == filled(_history_00()) and len(filled(_history_00())) == 4
    extra = [h for h in _history_00() if h["odno"] not in {x["odno"] for x in _history_01()}]
    assert [h["odno"] for h in extra] == ["43210"] and extra[0]["nccs_qty"] == "12"


def test_일반_T갱신도_미체결_행을_체결로_세지_않는다():
    base = {"T": 5.0, "position_qty": 12, "last_updated": "2026-11-15T00:00:00+00:00", "last_processed_ordno": "",
            "orders_meta": {}, "reverse_mode": {}, "net_invested": 600.0, "net_invested_status": "valid"}
    with patch("config.BROKER", "kis"), patch("config.BROKER_MODE", "demo"):
        a = update_T_from_history("TQQQ", deepcopy(base), deepcopy(_history_01()), balance_qty=12)
        b = update_T_from_history("TQQQ", deepcopy(base), deepcopy(_history_00()), balance_qty=12)
    assert a["T"] == b["T"] and a.get("net_invested") == b.get("net_invested")
    assert a.get("position_qty") == b.get("position_qty")


def test_부분체결은_실제_체결량만_반영하고_자동_종결하지_않는다():
    history = _buy_rows() + [_item("43210", 5, 12, price="43.22")]
    history = _normalize_via_adapter(history)
    state = _reconcile(_incident_state(), history)
    meta = state["orders_meta"]["43210"]
    assert meta["processed_filled_qty"] == 5
    assert meta.get("terminal_assumed") is not True      # 부분체결은 수동 확인 대상 (기존 규칙 유지)
    assert meta.get("terminal") is not True


def test_반복_실행해도_정산이_중복되지_않는다():
    once = _reconcile(_incident_state(), _history_00())
    twice = _reconcile(deepcopy(once), _history_00())
    for key in ("T", "net_invested", "position_qty"):
        assert once[key] == twice[key]
    assert once["orders_meta"]["43210"]["processed_filled_qty"] == twice["orders_meta"]["43210"]["processed_filled_qty"] == 0
    assert twice["orders_meta"]["43210"]["terminal"] is True
    assert "reconciliation_only" not in twice["reverse_mode"]


def test_실전이나_다른_브로커는_이전과_같이_보류한다():
    for broker, mode in (("kis", "real"), ("kiwoom", "real"), ("nhplug", "real")):
        state = _reconcile(_incident_state(), _history_00(), broker=broker, mode=mode)
        meta = state["orders_meta"]["43210"]
        assert meta.get("terminal_assumed") is not True, (broker, mode)
        assert state["reverse_mode"].get("reconciliation_only") is True, (broker, mode)


# ───────────────────────── 파이프라인(run_one_symbol) + verify_run ─────────────────────────

def _kis_demo(monkeypatch):
    monkeypatch.setattr("config.BROKER", "kis")
    monkeypatch.setattr("config.BROKER_MODE", "demo")


def _kis_day2_history(odno, filled, nccs_status_text=""):
    """KIS 모의 형태: prcs_stat_name 빈 문자열, 상태는 nccs_qty 로만 판단."""
    rows = [{**_item("1", 144, 144, ord_dt="20261105", side="매수", price="65.5732"),
             "ord_datetime_utc": "2026-11-05T09:30:00+00:00", "ord_datetime_kst": "2026-11-05T18:30:00+09:00",
             "_ord_dt_is_kst_date": True}]
    sell = _item(odno, filled, 14, ord_dt="20261112", side="매도", price="59.26", status=nccs_status_text)
    sell["ord_datetime_utc"] = "2026-11-12T09:30:30+00:00"
    sell["ord_datetime_kst"] = "2026-11-12T18:30:30+09:00"
    sell["_ord_dt_is_kst_date"] = True
    return rows + [sell]


def test_파이프라인_이전_세션_미체결_리버스_매도는_만료_가정_후_주문_평가가_재개된다(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    holder, saved = env
    _kis_demo(monkeypatch)
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_kis_day2_history(odno, 0), qty=144, close=59.26, cash=2000.0)

    _run_live(broker, _config())

    final = saved[-1]
    meta = final["orders_meta"][odno]
    assert meta["terminal"] is True and meta["terminal_assumed"] is True       # 실제 만료 확인이 아니므로 구분 유지
    assert "reconciliation_only" not in final["reverse_mode"]
    assert final["T"] == 19.7734, "정산 단계에서 T 가 바뀌면 안 됩니다"
    assert meta["processed_filled_qty"] == 0 and meta["processed_filled_amount"] == 0.0
    assert broker.place_order.call_count > 0, "보류가 풀려 주문 평가가 재개돼야 합니다"


def test_파이프라인_같은_세션의_미체결_주문은_자동_종결하지_않는다(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    holder, saved = env
    _kis_demo(monkeypatch)
    state, odno = _day1(env)
    holder["state"] = deepcopy(state)
    _same_session_later()
    broker = _broker(_kis_day2_history(odno, 0), qty=144, close=59.26)

    _run_live(broker, _config())

    final = saved[-1] if saved else holder["state"]
    meta = final["orders_meta"][odno]
    assert meta.get("terminal") is not True and meta.get("terminal_assumed") is not True
    assert final["T"] == 19.7734


def test_파이프라인_해소_후_반복_실행해도_T와_마커가_변하지_않고_verify_run_통과(env, monkeypatch, tmp_path):
    import glob
    import json
    import verify_run
    monkeypatch.chdir(tmp_path)
    holder, saved = env
    _kis_demo(monkeypatch)
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_kis_day2_history(odno, 0), qty=144, close=59.26, cash=2000.0)
    _run_live(broker, _config())
    first = deepcopy(saved[-1])

    evid = sorted(glob.glob(str(tmp_path / "settlement-TQQQ-*.json")))
    assert evid, "정산 근거 파일이 기록돼야 합니다"
    with open(evid[-1], encoding="utf-8") as fh:
        label = json.loads(fh.read()).get("broker")
    ok, problems = verify_run.verify_evidence_file(evid[-1], label, "LIVE")
    assert ok is True, problems

    # 같은 세션 재실행 — 정산 중복·T 변경 없음
    _same_session_later()
    holder["state"] = deepcopy(first)
    placed_before = broker.place_order.call_count
    broker2 = _broker(_kis_day2_history(odno, 0), qty=144, close=59.26, cash=2000.0)
    _run_live(broker2, _config())
    second = saved[-1]
    assert second["T"] == first["T"]
    assert second["orders_meta"][odno]["processed_filled_qty"] == 0
    assert second["orders_meta"][odno]["terminal_assumed"] is True
    assert placed_before > 0


# ───────────────────────── 모의 주문번호 재사용(날짜가 달라도 같은 번호) ─────────────────────────

def test_어댑터는_번호가_같은_행도_모두_돌려준다():
    rows = [_item("14455", 7, 7, ord_dt="20260921", side="매도", price="60.00"),
            _item("14455", 0, 3, ord_dt="20260929", side="매수", price="55.00")]
    history = _normalize_via_adapter(rows)
    assert [(h["odno"], h["ord_dt"], h["ft_ccld_qty"]) for h in history] == [
        ("14455", "20260921", "7"), ("14455", "20260929", "0")]


def test_체결_정산_조회표는_같은_번호의_체결_행을_우선한다():
    rows = _normalize_via_adapter([
        _item("14455", 7, 7, ord_dt="20260921"), _item("14455", 0, 3, ord_dt="20260929"),      # 체결 → 미체결
        _item("500", 0, 3, ord_dt="20261001"), _item("500", 4, 4, ord_dt="20261002"),          # 미체결 → 체결
        _item("888", 2, 2, ord_dt="20261106"), _item("888", 3, 3, ord_dt="20261107"),          # 체결 둘 → 마지막
        _item("777", 0, 5, ord_dt="20261105"), _item("777", 0, 6, ord_dt="20261106"),          # 미체결 둘 → 마지막
    ])
    with patch("config.BROKER", "kis"), patch("config.BROKER_MODE", "demo"):
        index = state_module._index_history_by_odno(rows)
    assert (index["14455"]["ord_dt"], index["14455"]["ft_ccld_qty"]) == ("20260921", "7")
    assert (index["500"]["ord_dt"], index["500"]["ft_ccld_qty"]) == ("20261002", "4")
    assert (index["888"]["ord_dt"], index["888"]["ft_ccld_qty"]) == ("20261107", "3")
    assert (index["777"]["ord_dt"], index["777"]["ft_ccld_qty"]) == ("20261106", "0")


def test_KIS_실전과_다른_브로커는_같은_번호면_기존처럼_마지막_행이_이긴다():
    rows = _normalize_via_adapter([_item("14455", 7, 7, ord_dt="20260921"), _item("14455", 0, 3, ord_dt="20260929")])
    for broker, mode in (("kis", "real"), ("kiwoom", "demo"), ("nhplug", "demo"), ("meritz", "real")):
        with patch("config.BROKER", broker), patch("config.BROKER_MODE", mode):
            index = state_module._index_history_by_odno(rows)
        assert index["14455"]["ord_dt"] == "20260929" and index["14455"]["ft_ccld_qty"] == "0", (broker, mode)


def test_번호_재사용에도_지연_체결의_건너뜀_감사_목록이_01과_같다():
    rows = [_item("14455", 7, 7, ord_dt="20260921", side="매도", price="60.00"),
            _item("14455", 0, 3, ord_dt="20260929", side="매수", price="55.00")]
    state = {"orders_meta": {"14455": {"side": "SELL", "total_qty": 7, "processed_filled_qty": 0,
                                       "processed_filled_amount": 0.0}}}
    results = {}
    patcher = patch("config.BROKER", "kis"), patch("config.BROKER_MODE", "demo")
    patcher[0].start(); patcher[1].start()
    for label, history in (("00", _normalize_via_adapter(rows)), ("01", _normalize_via_adapter(rows[:1]))):
        skipped = []
        pending, *_ = state_module._apply_late_fill_deltas(deepcopy(state), history, set(), skipped)
        results[label] = ([p[0] for p in pending], [e["odno"] for e in skipped])
    patcher[0].stop(); patcher[1].stop()
    assert results["00"] == results["01"] == ([], ["14455"])


def test_과거_체결_주문과_번호가_같은_현재_리버스_미체결_주문도_종결된다():
    """이전 필터 방식의 결함: 번호가 같은 과거 체결 행이 있으면 현재 리버스 미체결 행이 제거돼 보류가 재발했습니다."""
    past = _item("43210", 7, 7, ord_dt="20260921", side="매수", price="60.00")      # 과거에 체결된 같은 번호
    history = _normalize_via_adapter(_buy_rows() + [past, _item("43210", 0, 12)])
    state = _reconcile(_incident_state(), history)
    meta = state["orders_meta"]["43210"]
    assert meta["terminal"] is True and meta["terminal_assumed"] is True
    assert meta["processed_filled_qty"] == 0 and state["T"] == 19.2773
    assert "reconciliation_only" not in state["reverse_mode"]


def test_번호가_같은_서로_다른_날짜의_미체결_주문은_제출일로_구분한다():
    other = _item("43210", 0, 3, ord_dt="20261118", side="매수", price="40.00")     # 다른 날, 같은 번호, 미체결
    history = _normalize_via_adapter(_buy_rows() + [other, _item("43210", 5, 12, price="43.22")])
    state = _reconcile(_incident_state(), history)
    meta = state["orders_meta"]["43210"]
    assert meta["processed_filled_qty"] == 5          # 11/20 주문의 부분체결 5주만 반영 (11/18 행과 섞이지 않음)
    assert meta.get("terminal") is not True


# ───────────────────────── 정산 기준 시각(워터마크) ─────────────────────────

def test_0체결_리버스_행은_KIS_모의에서_정산_기준_시각을_옮기지_않는다():
    state = _incident_state()
    state["last_processed_ordno"] = "9"
    before = state["last_updated"]
    state = _reconcile(state, _history_00())
    assert state["orders_meta"]["43210"]["terminal_assumed"] is True
    assert state["last_updated"] == before and state["last_processed_ordno"] == "9"


def test_체결된_리버스_행은_기존처럼_정산_기준_시각을_옮긴다():
    history = _normalize_via_adapter(_buy_rows() + [_item("43210", 12, 12, price="43.22")])
    state = _reconcile(_incident_state(), history)
    assert state["last_updated"] > "2026-11-19T19:10:00+00:00"
    assert state["last_processed_ordno"] == "43210"


def test_다른_브로커나_실전의_정산_기준_시각_동작은_그대로다():
    for broker, mode in (("nhplug", "demo"), ("kis", "real")):
        state = _reconcile(_incident_state(), _history_00(), broker=broker, mode=mode)
        # 이전 동작: 0체결 행도 이벤트로 기준 시각을 옮김 (이 PR 은 KIS 모의만 바꿈)
        assert state["last_updated"] > "2026-11-19T19:10:00+00:00", (broker, mode)
