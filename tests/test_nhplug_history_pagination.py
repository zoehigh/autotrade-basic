"""NHPlug 주문체결내역(GSB10030) 연속조회 회귀 테스트 (외부 호출 없음).

배경: 2026-11-18 NHPlug 모의 실행이 `20261117` 이력 조회에서 XA109("모의투자 조회가 계속 됩니다")를 받았는데
어댑터가 연속조회를 처리하지 않고 날짜를 "건너뜀"으로 넘겨 이력이 비었고, 포지션 불일치(TQQQ 108↔113)로 중단됐습니다.

연속조회 규약(공식 llms.txt·SDK): 응답 헤더의 cts·cts_flag(Y=다음, N=끝)를 다음 요청 헤더에 그대로 실어 보냅니다.
헤더 없이 응답코드(XA109 등)로만 계속을 알리는 경우도 있습니다. 연속조회를 끝내지 못하면 Fail-Closed 입니다.
"""
import pytest

import broker.nhplug.adapter as adapter_mod
from broker.base import BrokerError
from broker.nhplug.adapter import NHPlugBroker, NHPlugPagingError


@pytest.fixture(autouse=True)
def _probe_off_by_default(monkeypatch):
    """진단용 비교 조회는 DRY 에서만 실행됩니다. 기본 테스트 환경의 TRADE_MODE 는 DRY 이므로, 연속조회 로직 테스트에는
    비교 조회가 응답을 소비하지 않게 LIVE 로 고정합니다 (비교 조회 테스트는 스스로 DRY 로 바꿉니다)."""
    import config
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")


class _Resp:
    def __init__(self, data, headers=None, status=200):
        self._data, self.headers, self.status_code = data, headers or {}, status
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeSession:
    """요청을 기록하고 미리 정한 응답을 순서대로 돌려줍니다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []   # (path, json_body, extra_headers)

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        self.calls.append((path, json_body, extra_headers))
        if not self._responses:
            raise AssertionError("준비된 응답보다 많이 호출되었습니다")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _broker(responses):
    broker = NHPlugBroker.__new__(NHPlugBroker)   # __init__ (설정·세션 생성) 없이 필요한 속성만 지정
    broker._rate_limit_wait = 0
    broker._acct_type = "03"
    broker._account_no = "12345678901"
    broker._get_token = lambda: "token"
    broker._session = _FakeSession(responses)
    return broker


def _row(odno, dt="20261117", cns="0"):
    return {"orr_no": odno, "orr_dt": dt, "iem_nm": "TQQQ", "sby_dit_nm": "2", "orr_qty": "1", "cns_qty": cns,
            "cns_pr": "50.0", "ny_cns_orr_qty": "1"}


def _page(rows, cts=None, flag=None, code=None, code_in="message"):
    headers = {}
    if cts is not None:
        headers["CTS"] = cts           # 대소문자 무시 확인용 (응답 헤더는 소문자로 정규화)
    if flag is not None:
        headers["Cts_Flag"] = flag
    data = {"Output_0": rows}
    if code and code_in == "message":
        data["message"] = {"msg_code": code, "usr_msg": "모의투자 조회가 계속 됩니다. 다음 버튼을 누르세요"}
    elif code:
        data["rsp_cd"], data["rsp_msg"] = code, "모의투자 조회가 계속 됩니다. 다음 버튼을 누르세요"
    return _Resp(data, headers)


# ── _request_with_rate_retry: 연속조회 신호 처리 ──────────────────────────────

def test_continue_code_is_not_an_error_only_when_meta_is_passed():
    """meta 를 넘긴 연속조회 호출에서만 XA109 가 '계속'입니다. 넘기지 않는 기존 호출은 그대로 오류입니다 (회귀)."""
    broker = _broker([_page([], code="XA109"), _page([], code="XA109")])
    meta = {}
    data = broker._request_with_rate_retry("/x", {}, "t", meta=meta)
    assert data["Output_0"] == [] and meta["continue_code"] == "XA109"
    with pytest.raises(BrokerError, match="XA109"):
        broker._request_with_rate_retry("/x", {}, "t")


@pytest.mark.parametrize("code_in", ["message", "rsp_cd"])
def test_continue_code_in_either_envelope(code_in):
    broker = _broker([_page([], code="XA109", code_in=code_in)])
    meta = {}
    broker._request_with_rate_retry("/x", {}, "t", meta=meta)
    assert meta["continue_code"] == "XA109"


def test_other_error_codes_still_raise_even_with_meta():
    """연속조회 코드가 아닌 오류는 meta 가 있어도 오류입니다."""
    resp = _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
    broker = _broker([resp])
    with pytest.raises(BrokerError, match="14580"):
        broker._request_with_rate_retry("/x", {}, "t", meta={})


def test_request_headers_forwarded_and_response_headers_lowercased():
    broker = _broker([_page([], cts="K1", flag="Y")])
    meta = {}
    broker._request_with_rate_retry("/x", {}, "t", extra_headers={"cts": "K0", "cts_flag": "Y"}, meta=meta)
    assert broker._session.calls[0][2] == {"cts": "K0", "cts_flag": "Y"}
    assert meta["headers"] == {"cts": "K1", "cts_flag": "Y"}


def test_no_extra_headers_keeps_original_session_call():
    """연속조회가 아닌 호출은 extra_headers 인자 없이 세션을 부릅니다 (기존 동작·기존 테스트 보존)."""
    broker = _broker([_Resp({"Output_0": []})])
    broker._request_with_rate_retry("/x", {}, "t")
    assert broker._session.calls[0][2] is None


# ── _fetch_history_day: 연속조회 루프 ────────────────────────────────────────

def test_single_page_without_continuation_headers():
    broker = _broker([_page([_row("1"), _row("2")])])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert [r["orr_no"] for r in rows] == ["1", "2"]
    assert len(broker._session.calls) == 1
    assert broker._session.calls[0][2] is None            # 첫 요청에는 cts 헤더가 없습니다


def test_single_page_flag_N_stops():
    broker = _broker([_page([_row("1")], cts="K", flag="N")])
    assert len(broker._fetch_history_day("TQQQ", "20261117", "t")) == 1
    assert len(broker._session.calls) == 1


def test_flag_N_overrides_continue_code():
    """cts_flag=N 이 명시되면 XA109 코드가 같이 와도 끝입니다 (SDK has_next 규칙과 동일)."""
    broker = _broker([_page([_row("1")], cts="K", flag="N", code="XA109")])
    assert len(broker._fetch_history_day("TQQQ", "20261117", "t")) == 1
    assert len(broker._session.calls) == 1


def test_multi_page_collects_all_pages_and_forwards_keys():
    broker = _broker([
        _page([_row("1"), _row("2")], cts="K1", flag="Y"),
        _page([_row("3")], cts="K2", flag="Y"),
        _page([_row("4")], cts="K2", flag="N"),            # 마지막 페이지 (flag=N 이면 cts 는 무시)
    ])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert [r["orr_no"] for r in rows] == ["1", "2", "3", "4"]
    sent = [c[2] for c in broker._session.calls]
    assert sent == [None, {"cts": "K1", "cts_flag": "Y"}, {"cts": "K2", "cts_flag": "Y"}]


def test_xa109_code_only_continuation():
    """실제 장애 형태: 응답코드 XA109(+cts 헤더)로 다음 페이지를 알립니다. 마지막 페이지에는 코드가 없습니다."""
    broker = _broker([
        _page([_row("1"), _row("2")], cts="K1", code="XA109"),
        _page([_row("3")]),
    ])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert [r["orr_no"] for r in rows] == ["1", "2", "3"]
    assert broker._session.calls[1][2] == {"cts": "K1"}


def test_missing_key_fails_closed():
    """계속해야 하는데 cts 가 없으면 일부 이력으로 진행하지 않고 실패합니다 (SDK 는 조용히 멈춥니다)."""
    for first in (_page([_row("1")], code="XA109"), _page([_row("1")], flag="Y"),
                  _page([_row("1")], cts="  ", flag="Y")):
        broker = _broker([first])
        with pytest.raises(NHPlugPagingError, match="cts"):
            broker._fetch_history_day("TQQQ", "20261117", "t")


def test_error_on_continuation_page_fails_closed_not_skipped(monkeypatch):
    """(Codex P1) 1페이지는 받았는데 2페이지 조회가 오류면, 날짜 전체를 '건너뜀'으로 넘기지 않고 실패합니다."""
    err = _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
    broker = _broker([_page([_row("1")], cts="K1", flag="Y"), err])
    with pytest.raises(NHPlugPagingError, match="2페이지"):
        broker._fetch_history_day("TQQQ", "20261117", "t")
    # 호출부(get_order_history)에서도 '건너뜀'으로 삼켜지지 않습니다
    broker = _broker([_page([_row("1")], cts="K1", flag="Y"), err])
    with pytest.raises(NHPlugPagingError):
        _history(broker, monkeypatch)


def test_error_on_first_page_is_still_skippable_as_before(monkeypatch):
    """첫 페이지 오류는 기존대로 그 날짜만 건너뜁니다(연속조회와 무관 — 최소 변경)."""
    err = _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
    broker = _broker([err, _page([_row("7", dt="20261118")])])
    assert [h["odno"] for h in _history(broker, monkeypatch)] == ["7"]


def test_repeated_key_fails_closed():
    broker = _broker([_page([_row("1")], cts="K1", flag="Y"), _page([_row("2")], cts="K1", flag="Y")])
    with pytest.raises(NHPlugPagingError, match="반복"):
        broker._fetch_history_day("TQQQ", "20261117", "t")
    # 이전에 보냈던 키로 되돌아가는 순환도 막습니다
    broker = _broker([_page([_row("1")], cts="A", flag="Y"), _page([_row("2")], cts="B", flag="Y"),
                      _page([_row("3")], cts="A", flag="Y")])
    with pytest.raises(NHPlugPagingError, match="반복"):
        broker._fetch_history_day("TQQQ", "20261117", "t")


def test_page_cap_fails_closed(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_MAX_HISTORY_PAGES", 3)
    broker = _broker([_page([_row(str(i))], cts=f"K{i}", flag="Y") for i in range(1, 10)])
    with pytest.raises(NHPlugPagingError, match="3페이지"):
        broker._fetch_history_day("TQQQ", "20261117", "t")
    assert len(broker._session.calls) == 3


def test_exactly_at_page_cap_succeeds(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_MAX_HISTORY_PAGES", 3)
    broker = _broker([_page([_row("1")], cts="K1", flag="Y"), _page([_row("2")], cts="K2", flag="Y"),
                      _page([_row("3")], flag="N")])
    assert len(broker._fetch_history_day("TQQQ", "20261117", "t")) == 3


# ── get_order_history: 호출부 ────────────────────────────────────────────────

def _history(broker, monkeypatch, days=1):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(adapter_mod, "get_kst_now", lambda: datetime(2026, 11, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")))
    return broker.get_order_history("TQQQ", "NAS", days=days)


def test_get_order_history_dedups_rows_repeated_across_pages(monkeypatch):
    """페이지 경계에서 같은 주문이 다시 내려와도 1건으로 셉니다 (중복 가산 방지)."""
    broker = _broker([
        _page([_row("1"), _row("2")], cts="K1", flag="Y"),     # 20261117 1페이지
        _page([_row("2"), _row("3")], flag="N"),               # 20261117 2페이지 (2번 중복)
        _page([]),                                             # 20261118
    ])
    history = _history(broker, monkeypatch)
    assert sorted(h["odno"] for h in history) == ["1", "2", "3"]


def test_get_order_history_paging_failure_is_not_skipped(monkeypatch):
    """연속조회 실패는 '건너뜀'으로 삼키지 않고 호출자에게 올립니다 — 이력 누락으로 포지션 불일치가 되기 전에 멈춥니다."""
    broker = _broker([_page([_row("1")], code="XA109")])       # 계속 신호는 있는데 cts 없음
    with pytest.raises(NHPlugPagingError):
        _history(broker, monkeypatch)


def test_get_order_history_other_broker_errors_still_skipped(monkeypatch):
    """연속조회와 무관한 날짜별 오류는 기존대로 건너뜁니다 (최소 변경)."""
    err = _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
    broker = _broker([err, _page([_row("9", dt="20261118")])])
    history = _history(broker, monkeypatch)
    assert [h["odno"] for h in history] == ["9"]


def test_get_order_history_xa109_full_scenario(monkeypatch):
    """11-18 장애 재현: 한 날짜가 XA109 로 이어지는 두 페이지. 수정 후에는 모든 행이 모입니다."""
    broker = _broker([
        _page([_row(str(i)) for i in range(1, 12)], cts="CTS-1", code="XA109"),
        _page([_row("12"), _row("13")]),
        _page([]),
    ])
    history = _history(broker, monkeypatch)
    assert len(history) == 13


# ── 진단 출력 (페이지별 건수·cts_flag·신호, 전체/고유 구분, 비교 조회) ───────────────

def test_diag_per_page_lines_and_totals_without_secrets(capsys):
    """페이지별 응답건수·cts_flag·신호·cts 응답/전달 여부와 전체/고유 건수를 출력하되 cts·주문번호 원문은 숨깁니다."""
    broker = _broker([
        _page([_row("A1"), _row("A2")], cts="SECRET-CTS-1", flag="Y"),
        _page([_row("A2"), _row("A3")], cts="SECRET-CTS-2", flag="N"),   # A2 가 페이지 경계에서 중복
    ])
    broker._fetch_history_day("TQQQ", "20261117", "t")
    out = capsys.readouterr().out
    assert "페이지=1 응답건수=2 고유주문=2 cts_flag=Y 신호=없음 cts응답=있음 cts전달=아니오" in out
    assert "페이지=2 응답건수=2 고유주문=2 cts_flag=N 신호=없음 cts응답=있음 cts전달=예" in out
    assert "수집 완료: 페이지=2 전체수집=4건 고유주문=3건 종료사유=cts_flag=N" in out
    assert "SECRET-CTS" not in out and "A1" not in out and "A3" not in out


def test_diag_xa109_signal_shown(capsys):
    broker = _broker([_page([_row("1")], cts="K", code="XA109"), _page([])])
    broker._fetch_history_day("SOXL", "20261117", "t")
    out = capsys.readouterr().out
    assert "신호=XA109" in out and "종료사유=계속 신호 없음" in out


def test_probe_runs_only_in_dry_and_never_changes_result(monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "TRADE_MODE", "DRY")
    # 본 조회 2페이지 + 비교 조회 4번 (비교 응답은 각기 다른 형태)
    broker = _broker([
        _page([_row("1"), _row("2")], cts="K1", code="XA109"),             # 1페이지
        _page([_row("1"), _row("9")], cts="K1-b", flag="Y"),               # 비교1: 명세본문+헤더cts
        _page([]),                                                         # 비교2
        _Resp({"message": {"msg_code": "14580", "usr_msg": "장종료"}}),     # 비교3 (오류 — 삼켜져야 함)
        _page([_row("1"), _row("2")]),                                     # 비교4: 1페이지 재조회
        _page([_row("3")]),                                                # 본 조회 2페이지
    ])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert [r["orr_no"] for r in rows] == ["1", "2", "3"]                  # 비교 조회가 결과에 섞이지 않음
    out = capsys.readouterr().out
    assert "명세본문(Input_0만)+헤더cts: 응답건수=2 고유주문=2 1페이지와겹침=1 새주문=1 cts_flag=Y" in out
    assert "본문tr_cont=Y, 헤더cts없음: 조회 오류" in out
    calls = broker._session.calls
    assert calls[1][1] == {"Input_0": calls[0][1]["Input_0"]}              # 명세 본문은 Input_0 만
    assert calls[2][1]["tr_cont"] == "Y" and calls[2][2] == {"cts": "K1"}
    assert calls[3][2] is None and calls[3][1]["tr_cont"] == "Y"


def test_probe_skipped_outside_dry(monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")
    broker = _broker([_page([_row("1")], cts="K1", code="XA109"), _page([_row("2")])])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert len(rows) == 2 and len(broker._session.calls) == 2              # 비교 조회 없음
    assert "진단·비교" not in capsys.readouterr().out


def test_probe_exceptions_are_swallowed(monkeypatch, capsys):
    """비교 조회가 예외를 내도 본 조회 결과·흐름에 영향이 없습니다 (진단은 부수 효과 없음)."""
    import config
    monkeypatch.setattr(config, "TRADE_MODE", "DRY")
    broker = _broker([
        _page([_row("1")], cts="K1", code="XA109"),    # 본 조회 1페이지
        ValueError("boom"),                            # 비교1: 예외
        _page([]), _page([]), _page([]),               # 비교2~4
        _page([_row("2")]),                            # 본 조회 2페이지
    ])
    rows = broker._fetch_history_day("TQQQ", "20261117", "t")
    assert [r["orr_no"] for r in rows] == ["1", "2"]
    assert "조회 오류 ValueError" in capsys.readouterr().out


# ── 누락 주문 분할 진단 (DRY 한정, 조회 전용) ─────────────────────────────────

def _dry(monkeypatch):
    import config
    monkeypatch.setattr(config, "TRADE_MODE", "DRY")


def _missing(*ids, session="2026-11-17"):
    return [{"odno": i, "session": session, "submitted_at": "20261118052334"} for i in ids]












def test_trading_bot_hook_only_runs_probe_in_dry(monkeypatch):
    import trading_bot
    import state as state_module

    calls = []

    class _B:
        def diag_probe_missing_orders(self, symbol, exchange, missing):
            calls.append((symbol, exchange, missing))

    state_module._DIAG_MISSING_REVERSE["X"] = {"odno": "X", "session": "2026-11-17", "submitted_at": ""}
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    trading_bot._diag_probe_missing_reverse_orders(_B(), "SOXL", "AMS")
    assert calls == [] and state_module._DIAG_MISSING_REVERSE == {}               # LIVE: 호출 없이 비움
    state_module._DIAG_MISSING_REVERSE["X"] = {"odno": "X", "session": "2026-11-17", "submitted_at": ""}
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    trading_bot._diag_probe_missing_reverse_orders(_B(), "SOXL", "AMS")
    assert len(calls) == 1 and calls[0][2][0]["odno"] == "X"
    # 진단 오류는 매매 흐름으로 새지 않음
    class _Bad:
        def diag_probe_missing_orders(self, *a):
            raise RuntimeError("x")
    state_module._DIAG_MISSING_REVERSE["Y"] = {"odno": "Y", "session": "", "submitted_at": ""}
    trading_bot._diag_probe_missing_reverse_orders(_Bad(), "SOXL", "AMS")


def test_trading_bot_hook_reaches_real_broker_behind_dry_wrapper(monkeypatch):
    """(2026-11-19 DRY 에서 진단이 실행되지 않은 원인) DRY 의 broker 는 DryBroker 래퍼라 진단 메서드가 바로 보이지 않습니다.
    래퍼 뒤의 실제 브로커(_real)의 진단 메서드를 찾아 호출해야 합니다."""
    import trading_bot
    import state as state_module
    from broker.base import DryBroker

    calls = []

    class _Real:
        def diag_probe_missing_orders(self, symbol, exchange, missing):
            calls.append((symbol, [m["odno"] for m in missing]))

    wrapper = DryBroker.__new__(DryBroker)       # 래퍼는 속성 _real 로 실제 브로커를 가집니다
    wrapper._real = _Real()
    assert getattr(wrapper, "diag_probe_missing_orders", None) is None     # 래퍼에는 직접 없음 — 위 문제의 재현
    state_module._DIAG_MISSING_REVERSE["Z"] = {"odno": "Z", "session": "2026-11-17", "submitted_at": ""}
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    trading_bot._diag_probe_missing_reverse_orders(wrapper, "SOXL", "AMS")
    assert calls == [("SOXL", ["Z"])]


# ── DRY 실행 전 검증: DryBroker → _real → NHPlugBroker 실제 경로 (외부 호출 없음) ─────────────

def _real_dry_chain(responses):
    """실제 DryBroker(래퍼) 가 실제 NHPlugBroker(가짜 세션) 를 감싼 구조 — 운영 DRY 의 broker 와 같은 형태."""
    from broker.base import DryBroker
    real = _broker(responses)
    return DryBroker(real), real




def test_hook_reports_when_probe_unavailable_instead_of_silent(monkeypatch, capsys):
    import trading_bot
    import state as state_module
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    state_module._DIAG_MISSING_REVERSE["X"] = {"odno": "X", "session": "2026-11-17", "submitted_at": ""}
    trading_bot._diag_probe_missing_reverse_orders(object(), "SOXL", "AMS")    # 진단 메서드 없는 브로커
    assert "진단 불가" in capsys.readouterr().out


def test_hook_reports_probe_failure_instead_of_silent(monkeypatch, capsys):
    import trading_bot
    import state as state_module
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")

    class _Bad:
        def diag_probe_missing_orders(self, *a):
            raise RuntimeError("boom")

    state_module._DIAG_MISSING_REVERSE["X"] = {"odno": "X", "session": "2026-11-17", "submitted_at": ""}
    trading_bot._diag_probe_missing_reverse_orders(_Bad(), "SOXL", "AMS")      # 예외는 밖으로 나가지 않되
    out = capsys.readouterr().out
    assert "진단 시작" in out and "진단 건너뜀(오류): RuntimeError" in out        # 조용히 사라지지도 않음


def test_hook_is_wired_into_run_one_symbol_after_settlement():
    """훅이 run_one_symbol 안에서 update_T_from_history(정산·누락 수집) 직후에 호출되도록 연결돼 있어야 합니다."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "trading_bot.py").read_text(encoding="utf-8")
    func = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "run_one_symbol")
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    update = min(c.lineno for c in calls if c.func.id == "update_T_from_history")
    hook = [c.lineno for c in calls if c.func.id == "_diag_probe_missing_reverse_orders"]
    assert hook and min(hook) > update and min(hook) - update <= 3



# ── 15건 상한 우회(방향×체결 분할 + 오름·내림 1페이지) — 서버 동작을 흉내 낸 합성 데이터 ─────────────

class _FakeServer:
    """GSB10030 흉내: 한 조회 15건 상한, 정렬·방향·체결 필터 적용, orr_no 필터는 무시,
    상한을 넘으면 1페이지에 cts_flag=Y + XA109 신호를 주고 cts 를 실은 2페이지는 비어 있음 (2026-11-19 DRY 관측과 같은 동작)."""

    def __init__(self, rows, cap=15, ignore_sort=False, ignore_side=False, signal_below_cap=False):
        self.rows, self.cap = rows, cap
        self.ignore_sort, self.ignore_side, self.signal_below_cap = ignore_sort, ignore_side, signal_below_cap
        self.calls = []

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        inp = json_body["Input_0"]
        self.calls.append((inp, extra_headers))
        if extra_headers and extra_headers.get("cts"):
            return _page([], flag="N")
        rows = list(self.rows)
        if not self.ignore_side and inp["oss_sby_dit_cd"] in ("1", "2"):
            rows = [r for r in rows if r["sby_dit_nm"] == inp["oss_sby_dit_cd"]]
        if inp["ost_cns_dit"] == "1":
            rows = [r for r in rows if int(r["cns_qty"]) > 0]
        elif inp["ost_cns_dit"] == "2":
            rows = [r for r in rows if int(r["ny_cns_orr_qty"]) > 0]
        if not self.ignore_sort:
            rows.sort(key=lambda r: int(r["orr_no"]), reverse=inp["sot_dit"] == "1")
        page = rows[: self.cap]
        if len(rows) > self.cap or (self.signal_below_cap and rows):
            return _page(page, cts="K", flag="Y", code="XA109")
        return _page(page)


def _dataset(buy_open=0, buy_filled=0, sell_open=0, sell_filled=0, start=1000001):
    """주문번호가 종류별로 섞이도록 번갈아 배정한 합성 이력."""
    kinds = ([("2", 0, 1)] * buy_open + [("2", 1, 0)] * buy_filled + [("1", 0, 1)] * sell_open + [("1", 1, 0)] * sell_filled)
    order = sorted(range(len(kinds)), key=lambda i: (i * 7) % max(len(kinds), 1) * 1000 + i)   # 결정적으로 섞기
    rows = []
    for n, idx in enumerate(order):
        side, cns, open_q = kinds[idx]
        rows.append({"orr_no": str(start + n), "orr_dt": "20261117", "iem_nm": "SOXL", "sby_dit_nm": side,
                     "orr_qty": "1", "cns_qty": str(cns), "cns_pr": "50.0", "ny_cns_orr_qty": str(open_q)})
    return rows


def _server_broker(server):
    broker = _broker([])
    broker._session = server
    return broker


def _ids(rows):
    return {str(r["orr_no"]) for r in rows}


def test_partitioning_recovers_rows_beyond_the_cap_when_each_partition_fits(monkeypatch):
    """22건(15건 상한 초과): 기존 전체 조회는 15건에서 잘리지만, 분할하면 모든 분할이 상한 미만이라 전부 모입니다."""
    _dry(monkeypatch)
    rows = _dataset(buy_open=12, buy_filled=5, sell_open=3, sell_filled=2)
    server = _FakeServer(rows)
    broker = _server_broker(server)
    assert len(broker._fetch_history_day("SOXL", "20261117", "t", quiet=True)) == 15          # 기존 방식은 잘림
    union, complete = broker._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is True and union == _ids(rows) and len(union) == 22


def test_partition_hitting_cap_is_complete_only_when_ascending_and_descending_overlap(monkeypatch):
    _dry(monkeypatch)
    rows = _dataset(buy_open=20, buy_filled=1, sell_open=2)                                    # 매수·미체결 20건 → 오름15·내림15 겹침
    union, complete = _server_broker(_FakeServer(rows))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is True and union == _ids(rows) and len(union) == 23                       # 겹친 주문은 한 번만 셈


def test_partition_with_30_or_more_is_incomplete_fail_closed(monkeypatch):
    _dry(monkeypatch)
    rows = _dataset(buy_open=40, sell_open=2)                                                  # 오름15·내림15 겹침 없음 → 가운데 10건 미확인
    union, complete = _server_broker(_FakeServer(rows))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False and len(union) < 42


def test_filter_ignored_is_a_contradiction_not_complete(monkeypatch, capsys):
    _dry(monkeypatch)
    rows = _dataset(buy_open=3, sell_open=3)
    _, complete = _server_broker(_FakeServer(rows, ignore_side=True))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False
    assert "모순(" in capsys.readouterr().out


def test_sort_ignored_is_a_contradiction_not_complete(monkeypatch, capsys):
    _dry(monkeypatch)
    rows = _dataset(buy_open=5, buy_filled=1)
    _, complete = _server_broker(_FakeServer(rows, ignore_sort=True))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False and "정렬 방향이 요청과 다름" in capsys.readouterr().out


def test_pagination_signal_contradicting_row_count_is_incomplete(monkeypatch, capsys):
    """연속조회 신호는 있는데 반환이 상한 미만 — 신호와 데이터가 모순이므로 완전하다고 보지 않습니다."""
    _dry(monkeypatch)
    rows = _dataset(buy_open=4)
    _, complete = _server_broker(_FakeServer(rows, signal_below_cap=True))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False and "연속조회 신호가 있는데 상한 미만" in capsys.readouterr().out


def test_probe_reads_only_first_page_and_never_sends_cts(monkeypatch):
    """역순 조회의 cts 반복 오류를 피하려고 진단 조회는 첫 페이지만 읽고 cts 를 보내지 않습니다."""
    _dry(monkeypatch)
    server = _FakeServer(_dataset(buy_open=20))
    _server_broker(server)._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert len(server.calls) == 8                                                              # 4분할 × (오름, 내림)
    assert all(extra is None for _inp, extra in server.calls)
    descs = [inp for inp, _ in server.calls if inp["sot_dit"] == "1"]
    assert len(descs) == 4


def test_full_probe_through_dry_broker_chain_reports_final_findings_without_secrets(monkeypatch, capsys):
    """DryBroker → _real(NHPlugBroker) 경로에서 진단 전체(기존 조회 / 역순 1페이지 / 분할)가 돌고, 시작 로그가 나오며,
    원문 주문번호·cts 가 출력되지 않습니다."""
    import trading_bot
    import state as state_module
    from broker.base import DryBroker
    _dry(monkeypatch)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    rows = _dataset(buy_open=13, buy_filled=4, sell_open=3, sell_filled=2)                      # 22건 > 15
    known = sorted(_ids(rows))
    missing_ids = [known[-1], known[-2], known[-3]]                                            # 기존 조회(오름 15건)에는 안 나오는 가장 새 주문들
    state_module._DIAG_MISSING_REVERSE.clear()
    for odno in missing_ids:
        state_module._DIAG_MISSING_REVERSE[odno] = {"odno": odno, "session": "2026-11-17", "submitted_at": ""}
    real = _server_broker(_FakeServer(rows))
    trading_bot._diag_probe_missing_reverse_orders(DryBroker(real), "SOXL", "AMS")
    out = capsys.readouterr().out
    assert "진단 시작: SOXL 누락 리버스 주문 3건" in out and "진단 종료: SOXL" in out
    assert "① 기존 전체 조회: 반환=15건" in out and "누락주문발견=0/3" in out                    # 기존 조회로는 못 찾음
    assert "② 전체 역순 1페이지: 반환=15건" in out and "정렬=내림" in out and "누락주문발견=3/3" in out  # 역순 1페이지는 가장 새 주문을 줌
    assert "누락주문 최종발견=3/3 완전성=보장" in out
    for odno in known:
        assert odno not in out


def test_partial_fill_appears_in_both_fill_partitions_and_is_counted_once(monkeypatch):
    """부분체결 주문(체결수량>0 이면서 미체결수량>0)은 체결·미체결 분할 양쪽에 나오는 것이 정상입니다 — 합집합에서 한 번만 셉니다."""
    _dry(monkeypatch)
    rows = _dataset(buy_open=3)
    rows.append({"orr_no": "1000099", "orr_dt": "20261117", "iem_nm": "SOXL", "sby_dit_nm": "2",
                 "orr_qty": "2", "cns_qty": "1", "cns_pr": "50.0", "ny_cns_orr_qty": "1"})
    union, complete = _server_broker(_FakeServer(rows))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is True and union == _ids(rows) and len(union) == 4


class _SideOverlapServer(_FakeServer):
    """매수·매도 필터는 무시하지 않지만 매도 분할에도 같은 주문을 돌려주는 모순된 서버."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        inp = json_body["Input_0"]
        if inp["oss_sby_dit_cd"] == "1":
            inp = {**inp, "oss_sby_dit_cd": "2"}
            json_body = {**json_body, "Input_0": inp}
        return super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)


def test_same_order_in_buy_and_sell_partitions_is_a_contradiction(monkeypatch, capsys):
    _dry(monkeypatch)
    rows = _dataset(buy_open=4)
    # 매도 분할이 매수 행을 돌려주면 행 내용 검증(매도 필터인데 매수 행)에서 이미 모순으로 걸립니다
    _, complete = _server_broker(_SideOverlapServer(rows))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False and "모순(" in capsys.readouterr().out


class _DescDiffersServer(_FakeServer):
    """상한 미만인데 내림 조회가 오름 조회와 다른 집합을 돌려주는 서버."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        page = super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)
        if json_body["Input_0"]["sot_dit"] == "1" and page.json()["Output_0"]:
            data = page.json()
            data["Output_0"] = data["Output_0"][:-1]                  # 한 건 빠짐 (내림 정렬은 유지)
            return _Resp(data, page.headers)
        return page


def test_below_cap_but_descending_set_differs_is_a_contradiction(monkeypatch, capsys):
    _dry(monkeypatch)
    rows = _dataset(buy_open=5)
    _, complete = _server_broker(_DescDiffersServer(rows))._enumerate_day_partitioned("SOXL", "20261117", "t")
    assert complete is False and "상한 미만인데 오름·내림 집합이 다름" in capsys.readouterr().out
