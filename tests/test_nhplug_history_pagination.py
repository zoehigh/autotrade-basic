"""NHPlug 주문체결내역(GSB10030) 하루치 완전 수집 회귀 테스트 (외부 호출 없음).

배경: 2026-11-18 NHPlug 모의 실행이 `20261117` 이력 조회에서 XA109("모의투자 조회가 계속 됩니다")를 받았는데
어댑터가 날짜를 "건너뜀"으로 넘겨 이력이 비었고, 포지션 불일치(TQQQ 108↔113)로 중단됐다. 이어진 DRY 진단에서
서버는 한 조회에 **15건까지만** 돌려주고(연속조회는 비어 오거나 키가 반복) 하루 21건 중 가장 새 주문 6건(SOXL 리버스
추가매수)이 이력에서 빠져 `RECONCILIATION_PENDING` 이 되는 것이 확인됐다.

수집 방식: 오름·내림 **첫 페이지**(가장 작은 15건 + 가장 큰 15건)를 조합해 겹치면 하루치 전체로 증명하고, 겹치지 않으면
방향(매수/매도)으로 분할해 같은 규칙을 적용하며, 증명하지 못하면 NHPlugPagingError(Fail-Closed). 전제: 한 응답 최대
15건(관측값), 정렬은 주문번호 기준(응답으로 검증).
"""
import pytest

import broker.nhplug.adapter as adapter_mod
from broker.base import BrokerError
from broker.nhplug.adapter import NHPlugBroker, NHPlugPagingError


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


# ── _request_with_rate_retry: 계속 신호 처리 ─────────────────────────────────────────────

def test_continue_code_is_not_an_error_only_when_meta_is_passed():
    """meta 를 넘긴 이력 조회에서만 XA109 가 '계속 신호'입니다. 넘기지 않는 기존 호출은 그대로 오류입니다 (회귀)."""
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
    resp = _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
    broker = _broker([resp])
    with pytest.raises(BrokerError, match="14580"):
        broker._request_with_rate_retry("/x", {}, "t", meta={})


def test_response_headers_are_returned_lowercased():
    broker = _broker([_page([], cts="K1", flag="Y")])
    meta = {}
    broker._request_with_rate_retry("/x", {}, "t", meta=meta)
    assert meta["headers"] == {"cts": "K1", "cts_flag": "Y"}


# ── _probe_page1: 첫 페이지만, cts 미전송 ────────────────────────────────────────────────

def test_probe_page1_reads_only_the_first_page_and_never_sends_cts():
    broker = _broker([_page([_row("1"), _row("2")], cts="K1", flag="Y", code="XA109")])
    page = broker._probe_page1("TQQQ", "20261117", "t", {"sot_dit": "1"})
    assert page["ids"] == ["1", "2"] and page["signal"] is True and page["flag"] == "Y" and page["cts"] is True
    assert len(broker._session.calls) == 1 and broker._session.calls[0][2] is None
    assert broker._session.calls[0][1]["Input_0"]["sot_dit"] == "1"


# ── _judge_partition: 완전성 판정 규칙 (단위) ──────────────────────────────────────────────

def _resp(ids, signal=False, flag="없음", side="2"):
    rows = [{"orr_no": i, "sby_dit_nm": side, "cns_qty": "0", "ny_cns_orr_qty": "1"} for i in ids]
    return {"rows": rows, "ids": [str(i) for i in ids], "signal": signal, "flag": flag, "code": "없음", "cts": False}


def _judge(asc_ids, desc_ids, **kw):
    broker = _broker([])
    asc, desc = _resp([str(i) for i in asc_ids], **kw), _resp([str(i) for i in desc_ids])
    return broker._judge_partition(asc, desc, "0", "0")


def test_judge_under_cap_equal_sets_is_complete():
    _union, verdict, complete = _judge(range(1, 9), range(8, 0, -1))
    assert complete is True and verdict.startswith("완전")


def test_judge_under_cap_with_signal_or_different_sets_is_contradiction():
    assert _judge(range(1, 9), range(8, 0, -1), signal=True)[1].startswith("모순")
    assert _judge(range(1, 9), range(7, 0, -1))[1].startswith("모순")


def test_judge_cap_hit_with_consistent_overlap_is_complete_and_covers_all():
    """21건 재현: 오름 1~15, 내림 21~7 — 겹침 구간(7~15)이 양쪽에 모두 있고 합집합이 전체 21건."""
    union, verdict, complete = _judge(range(1, 16), range(21, 6, -1))
    assert complete is True and union == {str(i) for i in range(1, 22)}


def test_judge_exactly_cap_orders_same_sets_is_complete():
    assert _judge(range(1, 16), range(15, 0, -1))[2] is True


def test_judge_cap_hit_without_overlap_is_incomplete_not_contradiction():
    _union, verdict, complete = _judge(range(1, 16), range(40, 25, -1))
    assert complete is False and verdict.startswith("불완전")


def test_judge_asymmetric_counts_fail_closed_even_when_sets_overlap():
    """오름 15건·내림 5건: 오름이 상한이면 내림도 15건이어야 한다 — 겹치더라도 완전성을 증명하지 못한다."""
    _union, verdict, complete = _judge(range(1, 16), range(15, 10, -1))
    assert complete is False and "비대칭" in verdict
    _union, verdict, complete = _judge(range(1, 16), [])                      # 내림이 비어 있는 경우
    assert complete is False and verdict.startswith("모순")


def test_judge_inconsistent_overlap_range_fails_closed():
    """오름 1~15, 내림이 10~14·16~25 (15 가 겹침 구간인데 내림에 없음) — 서로 일관되지 않은 응답."""
    desc = [25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 14, 13, 12, 11, 10]
    _union, verdict, complete = _judge(range(1, 16), desc)
    assert complete is False and "겹침 구간" in verdict


def test_judge_interleaved_ranges_without_common_order_fail_closed():
    """오름 {1,3,..,29}, 내림 {2,4,..,30}: 구간은 겹치는데 공통 주문이 없음 (정렬 가정이 깨진 응답) — 겹침 구간 일관성 검사로 걸립니다."""
    _union, verdict, complete = _judge(range(1, 30, 2), range(30, 0, -2))
    assert complete is False and verdict.startswith("모순")


def test_judge_response_above_cap_or_unsorted_or_duplicated_is_contradiction():
    assert _judge(range(1, 17), range(16, 0, -1))[1].startswith("모순")        # 16건 > 상한
    assert _judge([1, 3, 2], [3, 2, 1])[1].startswith("모순")                  # 오름 정렬 위반
    assert _judge([1, 2, 2], [2, 1])[1].startswith("모순")                     # 중복


def test_judge_filter_semantics_are_checked_for_partitions():
    broker = _broker([])
    asc = _resp(["1", "2"], side="1")                                        # 매수 필터인데 매도 행
    desc = _resp(["2", "1"], side="1")
    assert broker._judge_partition(asc, desc, "2", "0")[1].startswith("모순")


# ── 서버 동작을 흉내 낸 합성 데이터 (15건 상한·정렬·필터·신호) ──────────────────────────────────

class _FakeServer:
    """GSB10030 흉내: 한 조회 15건 상한, 정렬·방향·체결 필터 적용, orr_no 필터는 무시,
    상한을 넘으면 1페이지에 cts_flag=Y + XA109 신호 (2026-11-19 DRY 관측과 같은 동작)."""

    def __init__(self, rows, cap=15, ignore_sort=False, ignore_side=False, signal_below_cap=False):
        self.rows, self.cap = rows, cap
        self.ignore_sort, self.ignore_side, self.signal_below_cap = ignore_sort, ignore_side, signal_below_cap
        self.calls = []

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        inp = json_body["Input_0"]
        self.calls.append((inp, extra_headers))
        rows = [r for r in self.rows if r["orr_dt"] == inp["orr_dt"]]
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


def _dataset(buy_open=0, buy_filled=0, sell_open=0, sell_filled=0, buy_canceled=0, sell_canceled=0, start=1000001):
    """주문번호가 종류별로 섞이도록 배정한 합성 이력."""
    # (방향, 체결수량, 미체결수량): 0체결 종결(취소·거부)은 체결수량·미체결수량이 모두 0 — 체결/미체결 필터 어디에도 들지 않음
    kinds = ([("2", 0, 1)] * buy_open + [("2", 1, 0)] * buy_filled + [("1", 0, 1)] * sell_open + [("1", 1, 0)] * sell_filled
             + [("2", 0, 0)] * buy_canceled + [("1", 0, 0)] * sell_canceled)
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


def _history_via_server(server, monkeypatch, days=1):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(adapter_mod, "get_kst_now", lambda: datetime(2026, 11, 18, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")))
    return _server_broker(server).get_order_history("SOXL", "AMS", days=days)


def _history_ids(history):
    return {h["odno"] for h in history}


# ── get_order_history 실제 수집 경로 (오름·내림 조합 / 분할 / Fail-Closed) ─────────────────────────

def test_21_orders_all_restored_in_history_path_including_the_six_missing(monkeypatch, capsys):
    """2026-11-17 SOXL 재현: 하루 21건(미체결 다수). 오름 첫 페이지만으로는 15건에서 잘려 가장 새 주문 6건이 빠지지만,
    수집 경로가 오름·내림 첫 페이지를 조합해 21건 전부를 정산용 이력 행으로 돌려줍니다."""
    rows = _dataset(buy_open=15, buy_filled=3, sell_open=3)
    assert len(rows) == 21
    newest_six = sorted(_ids(rows), key=int)[-6:]
    first_page = _server_broker(_FakeServer(rows))._probe_page1("SOXL", "20261117", "t", {"sot_dit": "0"})
    assert not (set(newest_six) & set(first_page["ids"]))                       # 오름 첫 페이지에는 6건이 없다
    history = _history_via_server(_FakeServer(rows), monkeypatch)
    assert _history_ids(history) == _ids(rows) and len(history) == 21
    assert set(newest_six) <= _history_ids(history)
    assert "오름·내림 조합으로 완전 수집: 21건" in capsys.readouterr().out


def test_history_rows_keep_settlement_fields_for_filled_open_and_partial(monkeypatch):
    """체결·미체결·부분체결 행이 정산에 필요한 수량·상태 필드로 그대로 보존됩니다."""
    rows = _dataset(buy_open=14, buy_filled=2)
    rows.append({"orr_no": "1000099", "orr_dt": "20261117", "iem_nm": "SOXL", "sby_dit_nm": "2",
                 "orr_qty": "4", "cns_qty": "1", "cns_pr": "51.5", "ny_cns_orr_qty": "3"})            # 부분체결
    assert len(rows) == 17
    history = {h["odno"]: h for h in _history_via_server(_FakeServer(rows), monkeypatch)}
    assert len(history) == 17
    filled = next(h for h in history.values() if h["ft_ccld_qty"] == "1" and h["nccs_qty"] == "0")
    assert filled["prcs_stat_name"] == "체결"
    opened = next(h for h in history.values() if h["ft_ccld_qty"] == "0" and h["nccs_qty"] == "1")
    assert opened["prcs_stat_name"] == "미체결"
    partial = history["1000099"]
    assert (partial["ft_ord_qty"], partial["ft_ccld_qty"], partial["nccs_qty"], partial["ft_ccld_unpr3"]) == ("4", "1", "3", "51.5")


def test_under_cap_day_is_returned_directly_without_extra_calls(monkeypatch):
    """15건 미만이고 계속 신호도 없는 날짜는 기존처럼 한 번의 조회로 끝납니다 (추가 호출 없음)."""
    server = _FakeServer(_dataset(buy_open=8))
    assert len(_history_via_server(server, monkeypatch)) == 8
    assert len(server.calls) == 2                                                  # 두 날짜(20261117·20261118) × 1회


def test_exactly_cap_orders_day_is_complete(monkeypatch):
    rows = _dataset(buy_open=10, sell_open=5)                                      # 정확히 15건
    assert _history_ids(_history_via_server(_FakeServer(rows), monkeypatch)) == _ids(rows)


def test_30_or_more_orders_recovered_by_side_partition_when_each_side_fits(monkeypatch, capsys):
    rows = _dataset(buy_open=20, buy_filled=8, sell_open=7)                        # 35건: 전체 오름15·내림15 는 겹치지 않음
    history = _history_via_server(_FakeServer(rows), monkeypatch)
    assert _history_ids(history) == _ids(rows) and len(history) == 35
    assert "방향(매수/매도) 분할로 완전 수집: 35건" in capsys.readouterr().out


def test_30_or_more_in_one_side_fails_closed(monkeypatch):
    with pytest.raises(NHPlugPagingError, match="완전성 확인 실패"):
        _history_via_server(_FakeServer(_dataset(buy_open=40)), monkeypatch)


def test_zero_fill_terminal_orders_are_preserved_when_partitioning(monkeypatch):
    """(Codex P1) 체결수량·미체결수량이 모두 0인 취소·거부 주문은 체결/미체결 필터에 안 걸립니다. 30건 이상 날짜를
    방향으로만 분할해 이런 주문(리버스 취소 기록 등)이 조용히 빠지지 않아야 합니다."""
    rows = _dataset(buy_filled=6, buy_open=6, buy_canceled=8, sell_filled=4, sell_open=4, sell_canceled=3)   # 31건
    history = _history_via_server(_FakeServer(rows), monkeypatch)
    assert _history_ids(history) == _ids(rows) and len(history) == 31
    assert len([h for h in history if h["ft_ccld_qty"] == "0" and h["nccs_qty"] == "0"]) == 11


def test_filter_mismatch_in_partition_fails_closed(monkeypatch):
    with pytest.raises(NHPlugPagingError, match="모순"):
        _history_via_server(_FakeServer(_dataset(buy_open=20, buy_filled=8, sell_open=7), ignore_side=True), monkeypatch)


def test_sort_mismatch_fails_closed(monkeypatch):
    with pytest.raises(NHPlugPagingError, match="정렬 방향"):
        _history_via_server(_FakeServer(_dataset(buy_open=16), ignore_sort=True), monkeypatch)


def test_unsorted_response_below_cap_fails_closed(monkeypatch):
    """상한 미만이어도 요청한 정렬(오름)이 지켜지지 않은 응답은 믿지 않습니다."""
    with pytest.raises(NHPlugPagingError, match="정렬 방향"):
        _history_via_server(_FakeServer(list(reversed(_dataset(buy_open=8))), ignore_sort=True), monkeypatch)


def test_continuation_signal_with_under_cap_rows_is_not_trusted(monkeypatch):
    """15건 미만인데 연속조회 신호가 있으면(신호와 데이터 모순) 바로 반환하지 않고 완전성 확인으로 넘어가 실패합니다."""
    with pytest.raises(NHPlugPagingError, match="연속조회 신호가 있는데 상한 미만"):
        _history_via_server(_FakeServer(_dataset(buy_open=4), signal_below_cap=True), monkeypatch)


class _AsymmetricServer(_FakeServer):
    """오름 조회는 15건을 주는데 내림 조회는 비어 있는 비대칭 서버."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        if json_body["Input_0"]["sot_dit"] == "1":
            self.calls.append((json_body["Input_0"], extra_headers))
            return _page([])
        return super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)


def test_asymmetric_response_fails_closed(monkeypatch):
    with pytest.raises(NHPlugPagingError):
        _history_via_server(_AsymmetricServer(_dataset(buy_open=18)), monkeypatch)


class _ShortDescServer(_FakeServer):
    """내림 조회가 가장 큰 5건만 돌려주는 서버 (상한 15 보다 적게 — 오름과는 겹칠 수 있음)."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        page = super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)
        if json_body["Input_0"]["sot_dit"] == "1" and len(page.json()["Output_0"]) == 15:
            data = page.json()
            data["Output_0"] = data["Output_0"][:5]
            return _Resp(data, page.headers)
        return page


def test_overlapping_but_asymmetric_counts_fail_closed_in_history_path(monkeypatch):
    """오름 15건·내림 5건이 겹쳐도(내림이 잘렸을 가능성) 완전하다고 보지 않고 실패합니다."""
    with pytest.raises(NHPlugPagingError):
        _history_via_server(_ShortDescServer(_dataset(buy_open=17)), monkeypatch)


class _ConflictServer(_FakeServer):
    """같은 주문이 내림 응답에서는 체결수량이 다르게 보이는 서버."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        page = super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)
        if json_body["Input_0"]["sot_dit"] == "1" and page.json()["Output_0"]:
            data = page.json()
            data["Output_0"] = [dict(r) for r in data["Output_0"]]
            data["Output_0"][-1]["cns_qty"] = "5"                      # 오름·내림 겹치는 주문 중 하나의 체결수량이 다름
            return _Resp(data, page.headers)
        return page


def test_conflicting_duplicate_order_fails_closed(monkeypatch):
    with pytest.raises(NHPlugPagingError):
        _history_via_server(_ConflictServer(_dataset(buy_open=20)), monkeypatch)


def test_identical_duplicate_across_ascending_and_descending_is_counted_once(monkeypatch):
    rows = _dataset(buy_open=20)                                                   # 오름·내림이 10건 겹침
    history = _history_via_server(_FakeServer(rows), monkeypatch)
    assert len(history) == 20 and _history_ids(history) == _ids(rows)


class _BadAllDescServer(_FakeServer):
    """전체 조회의 내림 응답만 오름차순으로 돌려주는 서버 (분할 조회는 정상)."""

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        inp = json_body["Input_0"]
        if inp["sot_dit"] == "1" and inp["oss_sby_dit_cd"] == "0":
            json_body = {**json_body, "Input_0": {**inp, "sot_dit": "0"}}
        return super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)


def test_contradiction_in_all_orders_check_is_not_rescued_by_partitions(monkeypatch):
    """전체 오름·내림 조합이 모순(정렬 위반)이면 분할이 정상이어도 서버 응답을 신뢰할 수 없으므로 실패합니다."""
    with pytest.raises(NHPlugPagingError, match="모순"):
        _history_via_server(_BadAllDescServer(_dataset(buy_open=20)), monkeypatch)


class _FailingServer(_FakeServer):
    def __init__(self, rows, fail_on):
        super().__init__(rows)
        self.fail_on = fail_on

    def request(self, method, path, token, json_body=None, extra_headers=None, domain=None):
        if self.fail_on(json_body["Input_0"]):
            return _Resp({"message": {"msg_code": "14580", "usr_msg": "모의투자 장종료"}})
        return super().request(method, path, token, json_body=json_body, extra_headers=extra_headers, domain=domain)


@pytest.mark.parametrize("fail_on", [
    lambda inp: True,                                                              # 첫(오름) 조회부터 오류
    lambda inp: inp["sot_dit"] == "1" and inp["oss_sby_dit_cd"] == "0",            # 내림 조회 오류
    lambda inp: inp["oss_sby_dit_cd"] == "2" and inp["sot_dit"] == "1",            # 분할(매수·내림) 조회 오류
])
def test_query_failure_fails_closed_instead_of_skipping_the_date(monkeypatch, fail_on):
    """날짜별 BrokerError 를 '건너뜀'으로 넘기지 않습니다 — 그 날짜의 체결이 이력에서 사라져 정산이 틀어지는 것을 막습니다."""
    with pytest.raises(NHPlugPagingError, match="일부 이력만으로 진행하지 않습니다"):
        _history_via_server(_FailingServer(_dataset(buy_open=20, buy_filled=8, sell_open=7), fail_on), monkeypatch)


def test_multiple_dates_are_collected_independently(monkeypatch):
    rows = _dataset(buy_open=3) + [{**r, "orr_dt": "20261118", "orr_no": str(2000000 + i)} for i, r in enumerate(_dataset(sell_open=2))]
    assert len(_history_via_server(_FakeServer(rows), monkeypatch)) == 5


# ── SOXL 리버스 미체결 주문 정산 회귀 (2026-11-17: 누락 6건 → RECONCILIATION_PENDING) ────────────────

def _history_with_open_tail():
    """21건: 앞쪽 3건은 체결, 나머지 18건은 미체결 (가장 새 주문 6건은 전부 미체결 리버스 추가매수)."""
    rows = []
    for n in range(21):
        filled = n < 3
        rows.append({"orr_no": str(1000001 + n), "orr_dt": "20261117", "iem_nm": "SOXL", "sby_dit_nm": "2",
                     "orr_qty": "1", "cns_qty": "1" if filled else "0", "cns_pr": "50.0", "ny_cns_orr_qty": "0" if filled else "1"})
    return rows


def _soxl_reverse_state(order_ids):
    meta = {"side": "BUY", "total_qty": 1, "processed_filled_qty": 0, "reverse_action": "buy", "reverse_day": 4,
            "cycle_id": "C1", "submitted_at": "20261118052334", "submitted_session": "2026-11-17"}
    return {"T": 18.0, "reverse_mode": {"active": True, "cycle_id": "C1", "day_count": 3},
            "orders_meta": {oid: dict(meta) for oid in order_ids}}


def test_soxl_reverse_unfilled_orders_settle_when_history_is_complete(monkeypatch):
    """하루 21건 중 가장 새 주문 6건이 SOXL 리버스 추가매수(미체결): 완전한 이력이면 6건 모두 이전 세션 미체결로 확인되어
    만료 가정(자동 종결)되고 보류(reverse_order_history_missing)가 생기지 않습니다."""
    import config
    from state import reconcile_reverse_fills
    monkeypatch.setattr(config, "BROKER_MODE", "demo")
    rows = _history_with_open_tail()
    newest_six = sorted(_ids(rows), key=int)[-6:]
    history = _history_via_server(_FakeServer(rows), monkeypatch)
    state = _soxl_reverse_state(newest_six)
    reconcile_reverse_fills(state, history)
    metas = state["orders_meta"]
    assert all(metas[i].get("terminal") and metas[i].get("terminal_assumed") for i in newest_six)
    assert state["reverse_mode"].get("reconciliation_error") is None
    assert state["reverse_mode"].get("reconciliation_only") is None
    assert state["T"] == 18.0                                                      # 체결 0 → T 변화 없음


def test_soxl_reverse_orders_still_hold_when_history_is_truncated(monkeypatch):
    """대조: 오름 첫 페이지(15건)만 쓰던 이전 방식에서는 같은 6건이 이력에 없어 보류됩니다 (수정 전 증상)."""
    import config
    from state import reconcile_reverse_fills
    monkeypatch.setattr(config, "BROKER_MODE", "real")                             # 만료 가정 경로를 타지 않게
    rows = _history_with_open_tail()
    newest_six = sorted(_ids(rows), key=int)[-6:]
    first_page = _server_broker(_FakeServer(rows))._probe_page1("SOXL", "20261117", "t", {"sot_dit": "0"})
    broker = _broker([])
    truncated = [broker._normalize_unexecuted_item(r) for r in first_page["rows"]]
    state = _soxl_reverse_state(newest_six)
    reconcile_reverse_fills(state, truncated)
    assert state["reverse_mode"].get("reconciliation_error") == "reverse_order_history_missing"
