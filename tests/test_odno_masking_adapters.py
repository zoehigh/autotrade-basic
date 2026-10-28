"""브로커 어댑터 공개 로그의 주문번호 마스킹 동작 테스트 (mock 전용, 자격증명·네트워크 불필요).

각 어댑터의 HTTP 계층(세션/요청 래퍼)만 대체하고, 실제로 로그를 만드는 경로
(`get_order_history(verbose=True)` / `place_order` / `cancel_order`)를 호출합니다.
검증 규칙은 하나입니다 — **stdout 에 원문 주문번호가 없고 가명 토큰(`ord#…`)이 있어야 한다.**

또한 합성 주문번호(메리츠 `YYYYMMDD:주문번호`)가 쪼개지지 않고 그대로 유지되는지
(KIS 는 반대로 `str(int())` 정규화가 유지되는지)를 함께 고정합니다.

런타임은 오프라인입니다 — `requests` 를 직접 호출하지 않고, 브로커 마커도 붙이지 않습니다
(`RUN_BROKER_API_TESTS` 미설정 = 실 API 테스트 skip).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import log_mask  # noqa: E402
from broker.base import DryBroker, OrderResult  # noqa: E402

# 실제 형식과 같은 10자리 주문번호 (leading zero 포함)
RAW_KIS = "0000012345"
RAW_KIWOOM = "0000023456"
RAW_LS = "0000034567"
RAW_TOSS = "0000045678"
RAW_NHPLUG = "0000056789"
RAW_MERITZ = "0000067890"
TODAY = "20261028"


class FakeResponse:
    """requests.Response 흉내 — json/status/headers 만 제공합니다."""

    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def _assert_masked(out, raw, token):
    """stdout 에 원문이 없고 가명 토큰이 있어야 합니다."""
    assert token.startswith("ord#")
    assert raw not in out, f"원문 주문번호가 로그에 남았습니다:\n{out}"
    assert token in out, f"가명 토큰이 로그에 없습니다:\n{out}"


# ═══════════════════════════════════════════════════════════════════
# KIS
# ═══════════════════════════════════════════════════════════════════
def _kis_broker(monkeypatch):
    from broker.kis.adapter import KISBroker

    broker = KISBroker()
    broker._mode = "real"  # 정규장/예약주문 분기 회피
    broker._account_no = "12345678"
    broker._acnt_prdt_cd = "01"
    monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
    return broker


def test_kis_history_and_order_logs_are_masked(monkeypatch, capsys):
    broker = _kis_broker(monkeypatch)
    history_payload = {
        "rt_cd": "0",
        "output": [{
            "ord_dt": "20261027", "ord_tmd": "041000", "prdt_name": "TQQQ",
            "sll_buy_dvsn_cd_name": "매수", "ft_ord_qty": "10", "ft_ccld_qty": "10",
            "ft_ccld_unpr3": "50.0", "ft_ccld_amt3": "500.0", "nccs_qty": "0",
            "prcs_stat_name": "체결완료", "tr_mket_name": "NASDAQ", "tr_crcy_cd": "USD",
            "odno": RAW_KIS, "ovrs_excg_cd": "NASD",
        }],
    }
    monkeypatch.setattr(
        broker, "_request_with_rate_retry",
        lambda *a, **k: FakeResponse(history_payload, headers={"tr_cont": ""}),
    )
    history = broker.get_order_history("TQQQ", "NAS", days=30, verbose=True)
    out = capsys.readouterr().out

    # KIS 는 leading zero 를 제거한 trimmed 주문번호를 상태에 씁니다 (기존 동작 유지)
    assert history[0]["odno"] == "12345"
    _assert_masked(out, RAW_KIS, log_mask.mask_odno("12345"))

    monkeypatch.setattr(
        broker, "_request_with_rate_retry",
        lambda *a, **k: FakeResponse(
            {"rt_cd": "0", "output": {"ODNO": RAW_KIS, "ORD_TMD": "041000"}}
        ),
    )
    result = broker.place_order("TQQQ", "NASD", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    assert isinstance(result, OrderResult)
    # str(int()) 정규화 유지
    assert result.order_id == "12345"
    _assert_masked(out, RAW_KIS, log_mask.mask_odno("12345"))


# ═══════════════════════════════════════════════════════════════════
# KIWOOM
# ═══════════════════════════════════════════════════════════════════
def _kiwoom_broker(monkeypatch):
    from broker.kiwoom.adapter import KiwoomBroker

    monkeypatch.setattr(
        config, "BROKER_CONFIG",
        {"app_key": "k", "app_secret": "s", "account_no": "",
         "domain": "https://mockapi.kiwoom.com", "acnt_prdt_cd": ""},
        raising=False,
    )
    monkeypatch.setattr(config, "BROKER_MODE", "real", raising=False)
    broker = KiwoomBroker()
    broker._mode = "real"
    monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
    return broker


def test_kiwoom_history_and_order_logs_are_masked(monkeypatch, capsys):
    broker = _kiwoom_broker(monkeypatch)
    item = {
        "ord_dt": "20261027", "ord_tmd": "041000", "prdt_name": "TQQQ",
        "sll_buy_dvsn_cd_name": "매수", "ft_ord_qty": "10", "ft_ccld_qty": "10",
        "ft_ccld_unpr3": "50.0", "ft_ccld_amt3": "500.0", "nccs_qty": "0",
        "prcs_stat_name": "체결완료", "tr_mket_name": "NASDAQ", "tr_crcy_cd": "USD",
        "odno": RAW_KIWOOM, "ovrs_excg_cd": "NASD",
    }

    def fake_history(tr_id, body, token, extra_headers=None, return_headers=False, **kwargs):
        data = {"return_code": 0, "result_list": [dict(item)]}
        return (data, {"cont-yn": "N"}) if return_headers else data

    monkeypatch.setattr(broker, "_request_with_rate_retry", fake_history)
    broker.get_order_history("TQQQ", "NAS", days=7, verbose=True)
    out = capsys.readouterr().out
    _assert_masked(out, RAW_KIWOOM, log_mask.mask_odno(RAW_KIWOOM))

    def fake_order(tr_id, body, token, extra_headers=None, **kwargs):
        return {"return_code": 0, "ord_no": RAW_KIWOOM}

    monkeypatch.setattr(broker, "_request_with_rate_retry", fake_order)
    result = broker.place_order("TQQQ", "ND", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    assert result.order_id == RAW_KIWOOM
    _assert_masked(out, RAW_KIWOOM, log_mask.mask_odno(RAW_KIWOOM))


# ═══════════════════════════════════════════════════════════════════
# LS
# ═══════════════════════════════════════════════════════════════════
def _ls_broker(monkeypatch):
    from broker.ls.adapter import LSBroker

    broker = LSBroker()
    broker._mode = "real"
    monkeypatch.setattr(broker, "_post", lambda *a, **k: None)  # 각 테스트에서 교체
    return broker


def test_ls_history_and_order_logs_are_masked(monkeypatch, capsys):
    broker = _ls_broker(monkeypatch)
    history_payload = {
        "rsp_cd": "00000",
        "COSAQ00102OutBlock1": [{
            "ord_dt": "20261027", "ord_tmd": "041000", "prdt_name": "TQQQ",
            "sll_buy_dvsn_cd_name": "02", "ft_ord_qty": "10", "ft_ccld_qty": "10",
            "ft_ccld_unpr3": "50.0", "ft_ccld_amt3": "500.0", "nccs_qty": "0",
            "prcs_stat_name": "체결완료", "tr_mket_name": "NASDAQ", "tr_crcy_cd": "USD",
            "odno": RAW_LS, "ovrs_excg_cd": "NASD",
        }],
    }
    monkeypatch.setattr(broker, "_post", lambda *a, **k: FakeResponse(history_payload, headers={"tr_cont": "N"}))
    broker.get_order_history("TQQQ", "NAS", days=30, verbose=True)
    out = capsys.readouterr().out
    _assert_masked(out, RAW_LS, log_mask.mask_odno(RAW_LS))

    monkeypatch.setattr(
        broker, "_post",
        lambda *a, **k: FakeResponse({"rsp_cd": "00000", "COSAT00301OutBlock1": {"OrdNo": RAW_LS}}),
    )
    result = broker.place_order("TQQQ", "82", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    assert result.order_id == RAW_LS
    _assert_masked(out, RAW_LS, log_mask.mask_odno(RAW_LS))


# ═══════════════════════════════════════════════════════════════════
# TOSS
# ═══════════════════════════════════════════════════════════════════
def _toss_broker(monkeypatch):
    from broker.toss.adapter import TossBroker

    monkeypatch.setattr(
        config, "BROKER_CONFIG",
        {"domain": "https://mockapi.toss.im", "client_id": "c", "client_secret": "s",
         "account_seq": "1"},
        raising=False,
    )
    monkeypatch.setattr(config, "HTTP_TIMEOUT", (10, 30), raising=False)
    broker = TossBroker()
    broker._rate_limit_wait = 0
    monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
    return broker


def _toss_order(status="FILLED"):
    return {
        "orderId": RAW_TOSS,
        "symbol": "TQQQ",
        "side": "BUY",
        "status": status,
        "quantity": "10",
        "orderedAt": "2026-10-27T04:10:00+09:00",
        "execution": {"filledQuantity": "10", "averageFilledPrice": "50.0", "filledAmount": "500.0"},
    }


def test_toss_history_and_order_logs_are_masked(monkeypatch, capsys):
    broker = _toss_broker(monkeypatch)

    def fake_request(method, path, token, params=None, json_body=None,
                     extra_headers=None, return_headers=False, retry_network=True):
        if method.upper() == "GET":
            status = (params or {}).get("status")
            orders = [_toss_order()] if status == "CLOSED" else []
            return ({"result": {"orders": orders, "nextCursor": None}}, {"tr_cont": ""})
        return {"result": {"orderId": RAW_TOSS}}

    monkeypatch.setattr(broker, "_request_with_rate_retry", fake_request)
    broker.get_order_history("TQQQ", "NAS", days=7, verbose=True)
    out = capsys.readouterr().out
    _assert_masked(out, RAW_TOSS, log_mask.mask_odno(RAW_TOSS))

    result = broker.place_order("TQQQ", "NAS", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    assert result.order_id == RAW_TOSS
    _assert_masked(out, RAW_TOSS, log_mask.mask_odno(RAW_TOSS))


def test_toss_cancel_logs_are_masked(monkeypatch, capsys):
    broker = _toss_broker(monkeypatch)
    monkeypatch.setattr(
        broker._session, "post", lambda *a, **k: FakeResponse({"result": {}}, status_code=200)
    )
    broker.cancel_order(RAW_TOSS, "TQQQ", "NAS")
    out = capsys.readouterr().out
    _assert_masked(out, RAW_TOSS, log_mask.mask_odno(RAW_TOSS))


# ═══════════════════════════════════════════════════════════════════
# NHPLUG
# ═══════════════════════════════════════════════════════════════════
def _nhplug_broker(monkeypatch):
    from broker.nhplug.adapter import NHPlugBroker

    monkeypatch.setattr(
        config, "BROKER_CONFIG",
        {"domain": "https://mockapi.nhplug.com", "app_key": "k", "app_secret": "s",
         "account_no": "12345678", "acct_type": "01"},
        raising=False,
    )
    monkeypatch.setattr(config, "BROKER_MODE", "real", raising=False)
    monkeypatch.setattr(config, "HTTP_TIMEOUT", (10, 30), raising=False)
    broker = NHPlugBroker()
    broker._mode = "real"
    monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
    return broker


def test_nhplug_history_and_order_logs_are_masked(monkeypatch, capsys):
    broker = _nhplug_broker(monkeypatch)
    row = {
        "orr_dt": "20261027", "orr_tm": "041000", "orr_no": RAW_NHPLUG,
        "iem_cd": "TQQQ", "sby_dit_nm": "2", "orr_qty": "10", "cns_qty": "10",
        "cns_pr": "50.0", "orr_sts_nm": "체결", "prdt_nm": "TQQQ",
    }
    calls = []

    def fake_request(path, body, token, **kwargs):
        calls.append((path, body))
        return {"Output_0": [dict(row)]}

    monkeypatch.setattr(broker, "_request_with_rate_retry", fake_request)
    broker.get_order_history("TQQQ", "NAS", days=1, verbose=True)
    out = capsys.readouterr().out
    assert calls, "GSB10030(단일일) 조회가 호출되지 않았습니다"
    _assert_masked(out, RAW_NHPLUG, log_mask.mask_odno(RAW_NHPLUG))

    monkeypatch.setattr(
        broker, "_request_with_rate_retry",
        lambda path, body, token, **kwargs: {"Output_0": {"orr_no": RAW_NHPLUG}},
    )
    result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    assert result.order_id == RAW_NHPLUG
    _assert_masked(out, RAW_NHPLUG, log_mask.mask_odno(RAW_NHPLUG))


# ═══════════════════════════════════════════════════════════════════
# MERITZ
# ═══════════════════════════════════════════════════════════════════
class _MeritzSession:
    """MeritzSession 대체 — 큐에서 응답을 꺼내 반환하고 호출을 기록합니다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.timeout = (10, 30)

    def _next(self):
        if not self.responses:
            raise AssertionError("예상하지 못한 추가 호출")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, path, token, params=None, extra_headers=None):
        return self._next()

    def post(self, path, token, json_body=None, extra_headers=None):
        return self._next()

    def close(self):
        pass


def _meritz_broker(monkeypatch, responses):
    from broker.meritz import auth as meritz_auth
    from broker.meritz.adapter import MeritzBroker

    meritz_auth.invalidate_token()
    monkeypatch.setattr(config, "BROKER_MODE", "real", raising=False)
    monkeypatch.setattr(
        config, "BROKER_CONFIG",
        {"app_key": "k", "app_secret": "s", "base_url": "https://mockapi.meritz.com",
         "mac_address": ""},
        raising=False,
    )
    broker = MeritzBroker()
    broker._rate_limit_wait = 0
    monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
    broker._session = _MeritzSession(responses)
    return broker


def test_meritz_history_and_order_logs_are_masked(monkeypatch, capsys):
    today = "20261028"
    row = {
        "der_date": today, "der_no": "0000067890", "iscd": "TQQQ.OQ",
        "seln_byov_clcd": "2", "der_dqty": "10", "cntg_dqty": "10",
        "cntg_crnc_prc": "50.0", "cntg_camt": "500.0",
        "der_exec_ymdh": f"{today}041000", "prdt_name": "TQQQ",
        "ovrs_stck_oder_stat_cls_code": "0", "rctf_cncl_cls_code": "0",
    }
    broker = _meritz_broker(monkeypatch, [
        FakeResponse({"rsp_cd": "5766", "rsp_msg": "정상", "data": [row]}),
        FakeResponse({"rsp_cd": "5766", "rsp_msg": "정상", "data": []}),
    ])
    broker.get_order_history("TQQQ", "NAS", days=7, verbose=True)
    out = capsys.readouterr().out
    # 메리츠 이력의 odno 는 합성번호(YYYYMMDD:주문번호)이고, 마스킹은 그 합성번호 전체를 대상으로 합니다.
    composite = "20261028:0000067890"
    assert composite not in out and "0000067890" not in out
    assert log_mask.mask_odno(composite) in out

    # 주문: 합성 주문번호(YYYYMMDD:제로패딩번호)가 그대로 유지돼야 합니다
    monkeypatch.delenv("MERITZ_CONFIRM_WARNINGS", raising=False)
    broker = _meritz_broker(monkeypatch, [
        FakeResponse({"rsp_cd": "0001", "rsp_msg": "정상",
                      "data": {"der_no": "12390", "warn_cls_code": 0}}),
    ])
    result = broker.place_order("TQQQ", "NAS", "BUY", 1, 50.0, "LIMIT")
    out = capsys.readouterr().out
    # 합성번호는 `YYYYMMDD:제로패딩10자리` 형태를 그대로 유지 (쪼개지 않음)
    assert re.fullmatch(r"\d{8}:0000012390", result.order_id), result.order_id
    _assert_masked(out, "0000012390", log_mask.mask_odno(result.order_id))


# ═══════════════════════════════════════════════════════════════════
# 합성 주문번호 / 정규화 (단위)
# ═══════════════════════════════════════════════════════════════════
def test_meritz_composite_odno_is_never_split_by_masking():
    from broker.meritz.adapter import MeritzBroker

    composed = MeritzBroker._build_synthetic_odno("20261028", "12390")
    assert composed == "20261028:0000012390"

    token = log_mask.mask_odno(composed)
    assert token.startswith("ord#") and composed not in token
    # 마스킹은 표시만 바꾸고, 상태에 쓰는 합성번호는 그대로 분해돼야 합니다
    assert MeritzBroker._parse_synthetic_odno(composed) == ("20261028", "12390")


def test_kis_order_id_normalization_kept():
    """KIS 접수 응답은 leading zero 10자리 → 상태에는 trimmed 정규화 (기존 동작 유지).

    정규화가 끝난 값으로 마스킹해야 어댑터 로그(`str(int())` 결과)와 state 로그의
    토큰이 서로 대조됩니다. 정규화 전/후는 서로 다른 입력이므로 토큰도 다릅니다.
    """
    assert str(int(RAW_KIS)) == "12345"
    assert log_mask.mask_odno("12345") != log_mask.mask_odno(RAW_KIS)
    assert log_mask.mask_odno("12345") == log_mask.mask_odno("12345")


def test_dry_broker_cancel_log_is_masked(capsys):
    from unittest.mock import MagicMock

    broker = DryBroker(MagicMock())
    broker.cancel_order(RAW_KIS)
    out = capsys.readouterr().out
    _assert_masked(out, RAW_KIS, log_mask.mask_odno(RAW_KIS))
