"""
메리츠증권(Meritz) Broker 단위 테스트 — mock 기반, 자격증명 불필요.

실 API 호출은 없습니다. 모든 요청은 FakeSession이 가로채며,
토큰은 _get_token을 대체해 발급 없이 처리합니다.

공식 스키마(openapi.json 2026-10-02) 기준:
- 토큰 form 4필수 (scope=login 포함)
- 시세 query: mrkt_div_code/mrkt_cls_code(2자리 OQ·NY·AX)/iscd/dely_rltm_cls_code
- 일봉: mod_stpr_cls_code=0, 당일(ET) 미확정 봉 제외, 종가 prpr
- 거래소: 시세 OQ/NY/AX · 주문벤더 0537/0321/0066 (구형 OQNAS/OXAMS/0538/0539 삭제)
- 이력 query: oder_sta_date/oder_fin_date/cntg_cls_code 필수 3종 + tr_cont_key 페이지
- 이력 행: der_date/der_no/der_dqty/seln_byov_clcd/aspr_type_code/ncnt_qty(당일)
- 주문 body: ovrs_stck_symb_iscd/ovrs_exch_vndr_idno/oder_cls_code(01·05·31~34)/
  odqt/ovrs_stck_oder_prc/warn_cnfr_yn (+oder_cond_cls_code) — 응답은 body["data"]
- 취소 body: oder_date/orgl_oder_no/oder_cls_code/odqt

커버 범위:
- 응답 판정 (성공코드 / 업무오류 / 5820 조회결과없음 / EGW00200 조회 한정 재시도)
- 토큰 오류(EGW00121/EGW00123) 1회 재발급 후 재시도, scope=login
- 주문 경고 등급 0/1/2/3 처리 및 MERITZ_CONFIRM_WARNINGS 재전송
- 합성 주문번호 충돌 방지, KST 시각 플래그 (_ord_dt_is_kst_date)
- POST 네트워크 타임아웃/비JSON → OrderError
- 취소 필수 4필드 복원 (제출 캐시 / 당일 이력 fallback) + (der_date, 번호) 쌍 매칭
"""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

import config
from broker.base import (
    OrderError,
    OrderNotAcceptedError,
    BrokerError,
    AuthError,
)
from broker.meritz import auth as meritz_auth
from broker.meritz.adapter import MeritzBroker
from broker.meritz.exchange import (
    get_order_vendor_code,
    get_quote_exchange_code,
)
from broker.meritz.session import resolve_mac_address

BASE_URL = "https://openapi.test.meritz.com"
ET_TZ = ZoneInfo("America/New_York")

# state.py가 기대하는 표준 필드 16종
STANDARD_FIELDS = (
    "ord_dt",
    "ord_tmd",
    "ord_datetime_kst",
    "ord_datetime_utc",
    "prdt_name",
    "sll_buy_dvsn_cd_name",
    "ft_ord_qty",
    "ft_ccld_qty",
    "ft_ccld_unpr3",
    "ft_ccld_amt3",
    "nccs_qty",
    "prcs_stat_name",
    "tr_mket_name",
    "tr_crcy_cd",
    "odno",
    "ovrs_excg_cd",
)


def et_days_ago(days: int) -> str:
    """미국(ET) 기준 days일 전 날짜(YYYYMMDD)."""
    return (datetime.now(ET_TZ) - timedelta(days=days)).strftime("%Y%m%d")


# ═══════════════════════════════════════════════════════════════════
# 테스트 더블
# ═══════════════════════════════════════════════════════════════════

class FakeResponse:
    """requests.Response 흉내 — json() 또는 HTTP 상태를 그대로 반환."""

    def __init__(self, payload=None, status_code=200, invalid_json=False):
        self.status_code = status_code
        self.headers = {}
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def ok(rsp_cd="0000", data=None, **extra):
    """성공 응답 본문 생성."""
    body = {"rsp_cd": rsp_cd, "rsp_msg": "정상 처리", "data": data if data is not None else []}
    body.update(extra)
    return FakeResponse(body)


def price_ok(prpr="72.5", oprc="71.0", iscd="TQQQ", rsp_cd="0000"):
    """시세 조회(data=객체) 성공 응답 — iscd가 있으면 조회 성공으로 판정됩니다."""
    return ok(rsp_cd=rsp_cd, data={"prpr": prpr, "oprc": oprc, "iscd": iscd})


class FakeSession:
    """MeritzSession 대체 — 호출을 기록하고 큐에서 응답을 꺼내 반환."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.timeout = (10, 30)

    def _next(self, method, path, token, payload):
        self.calls.append({"method": method, "path": path, "token": token, **payload})
        if not self.responses:
            raise AssertionError(f"예상하지 못한 추가 호출: {method} {path}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, path, token, params=None, extra_headers=None):
        return self._next("GET", path, token, {"params": params})

    def post(self, path, token, json_body=None, extra_headers=None):
        return self._next("POST", path, token, {"json_body": json_body})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_token_cache():
    meritz_auth.invalidate_token()
    yield
    meritz_auth.invalidate_token()


@pytest.fixture
def make_broker(monkeypatch):
    """MeritzBroker 생성 헬퍼 — config를 monkeypatch하고 세션을 교체합니다."""

    def _factory(responses=None, broker_mode="real", config_override=None):
        monkeypatch.setattr(config, "BROKER_MODE", broker_mode, raising=False)
        monkeypatch.setattr(
            config,
            "BROKER_CONFIG",
            config_override
            if config_override is not None
            else {
                "app_key": "test-key",
                "app_secret": "test-secret",
                "base_url": BASE_URL,
                "mac_address": "",
            },
            raising=False,
        )
        broker = MeritzBroker()
        broker._rate_limit_wait = 0  # 테스트 속도
        monkeypatch.setattr(broker, "_get_token", lambda: "TEST_TOKEN")
        session = FakeSession(responses)
        broker._session = session
        return broker, session

    return _factory


# ═══════════════════════════════════════════════════════════════════
# 생성자 / 설정
# ═══════════════════════════════════════════════════════════════════

def test_constructor_rejects_non_real_mode(make_broker):
    """메리츠는 모의계좌가 없어 BROKER_MODE != real 이면 생성자에서 실패."""
    with pytest.raises(ValueError, match="모의투자"):
        make_broker(broker_mode="demo")


def test_constructor_requires_credentials(make_broker):
    """자격증명 누락 시 즉시 실패 — base_url은 운영 기본값으로 대체됩니다."""
    with pytest.raises(ValueError, match="MERITZ_APP_KEY"):
        make_broker(config_override={"app_key": "", "app_secret": "s", "base_url": BASE_URL})


def test_constructor_defaults_base_url(make_broker):
    """MERITZ_BASE_URL 미설정·빈 값이면 운영 서버 기본 주소를 사용합니다."""
    from config import MERITZ_DEFAULT_BASE_URL

    broker, _session = make_broker(
        config_override={"app_key": "k", "app_secret": "s", "base_url": ""}
    )
    assert broker._base_url == MERITZ_DEFAULT_BASE_URL


def test_constructor_real_mode_ok(make_broker):
    broker, _session = make_broker()
    assert broker.name == "meritz"
    assert broker.is_trading_day() in (True, False)


def test_broker_config_contains_meritz(monkeypatch):
    """config._get_broker_config("meritz")에 MERITZ_* 항목이 매핑되어 있어야 합니다."""
    import importlib

    monkeypatch.setenv("MERITZ_APP_KEY", "mk")
    monkeypatch.setenv("MERITZ_APP_SECRET", "ms")
    monkeypatch.setenv("MERITZ_BASE_URL", BASE_URL)
    monkeypatch.setenv("MERITZ_MAC_ADDRESS", "aa-bb-cc-dd-ee-ff")
    reloaded = importlib.reload(config)
    try:
        cfg = reloaded._get_broker_config("meritz")
        assert cfg["app_key"] == "mk"
        assert cfg["app_secret"] == "ms"
        assert cfg["base_url"] == BASE_URL
        # MAC 주소는 세션 생성 시점에 12자리 HEX로 정규화됩니다
        assert cfg["mac_address"] == "aa-bb-cc-dd-ee-ff"
        # 계좌번호 파라미터는 없습니다
        assert "account_no" not in cfg
    finally:
        importlib.reload(config)


# ═══════════════════════════════════════════════════════════════════
# 거래소 코드 / MAC 주소
# ═══════════════════════════════════════════════════════════════════

def test_exchange_code_separates_quote_and_vendor():
    """시세 코드(2자리)와 주문 벤더코드(4자리)는 서로 다른 체계입니다 (공식 표 2026-10-02)."""
    # 시세용 mrkt_cls_code: NAS→OQ, NYS→NY, AMS→AX
    assert get_quote_exchange_code("NAS") == "OQ"
    assert get_quote_exchange_code("NYS") == "NY"
    assert get_quote_exchange_code("AMS") == "AX"
    # 이미 변환된 시세 코드도 그대로 허용
    assert get_quote_exchange_code("OQ") == "OQ"

    # 주문 벤더코드: NASDAQ 0537 / NYSE 0321 / AMEX 0066
    assert get_order_vendor_code("NAS") == "0537"
    assert get_order_vendor_code("OQ") == "0537"     # place_order가 받는 시세 코드
    assert get_order_vendor_code("0537") == "0537"   # 이미 벤더코드
    assert get_order_vendor_code("NYS") == "0321"
    assert get_order_vendor_code("NY") == "0321"
    assert get_order_vendor_code("AMS") == "0066"    # SOXL은 AMS 유지
    assert get_order_vendor_code("AX") == "0066"

    with pytest.raises(ValueError):
        get_quote_exchange_code("XXX")
    with pytest.raises(ValueError):
        get_order_vendor_code("XXX")


def test_exchange_legacy_codes_are_removed():
    """구형 코드(OQNAS/OWNYS/OXAMS)는 스키마에서 삭제되어 더 이상 해석되지 않습니다."""
    for legacy in ("OQNAS", "OWNYS", "OXAMS"):
        with pytest.raises(ValueError):
            get_quote_exchange_code(legacy)
        with pytest.raises(ValueError):
            get_order_vendor_code(legacy)


def test_mac_address_is_12_digit_upper_hex(monkeypatch):
    """MAC 주소는 12자리 대문자 HEX이며 MERITZ_MAC_ADDRESS로 override 합니다."""
    generated = resolve_mac_address()
    assert len(generated) == 12
    assert generated == generated.upper()
    int(generated, 16)  # HEX 검증

    assert resolve_mac_address("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"
    with pytest.raises(ValueError):
        resolve_mac_address("ZZ:BB:CC:DD:EE:FF")


def test_session_headers_include_bearer_and_mac(monkeypatch):
    """모든 REST 요청에 authorization(Bearer) + mac_address 헤더가 붙습니다."""
    monkeypatch.setenv("MERITZ_MAC_ADDRESS", "AABBCCDDEEFF")
    from broker.meritz.session import MeritzSession

    session = MeritzSession(BASE_URL, mac_address=os.environ["MERITZ_MAC_ADDRESS"])
    headers = session._build_headers("TOKEN123")
    assert headers["authorization"] == "Bearer TOKEN123"
    assert headers["mac_address"] == "AABBCCDDEEFF"


# ═══════════════════════════════════════════════════════════════════
# 응답 판정 / 시세·일봉·잔고·매수가능 (공식 스키마)
# ═══════════════════════════════════════════════════════════════════

def test_query_success_uses_rsp_cd(make_broker):
    """rsp_cd=0000 이면 성공 — HTTP 상태가 500이어도 본문이 우선입니다."""
    broker, session = make_broker(
        [FakeResponse({"rsp_cd": "0000", "data": {"prpr": "72.5", "oprc": "71.0", "iscd": "TQQQ"}},
                      status_code=500)]
    )
    price = broker.get_stock_price("TQQQ", "NAS")
    assert price.last == 72.5
    assert price.open == 71.0
    assert session.calls[0]["path"] == "/market/v1/overseas/prices"


def test_price_query_params_official(make_broker):
    """시세 query는 공식 필수 4종만 전송합니다 (schl_wrt_dic/prdt_cd 아님)."""
    broker, session = make_broker([price_ok()])
    broker.get_stock_price("TQQQ", "NAS")
    assert session.calls[0]["params"] == {
        "mrkt_div_code": "OV",
        "mrkt_cls_code": "OQ",      # 2자리 시세 코드
        "iscd": "TQQQ",             # 대문자 심볼
        "dely_rltm_cls_code": "0",
    }


def test_price_empty_iscd_is_broker_error(make_broker):
    """응답 data가 객체가 아니거나 iscd가 비어 있으면 조회 실패입니다."""
    broker, _session = make_broker([ok(data={"prpr": "72.5", "iscd": ""})])
    with pytest.raises(BrokerError, match="iscd"):
        broker.get_stock_price("TQQQ", "NAS")


def test_query_business_error_raises_broker_error(make_broker):
    """업무 오류(HTTP 500 + rsp_cd)는 rsp_cd/rsp_msg 로 판정합니다."""
    broker, _session = make_broker(
        [
            FakeResponse(
                {"rsp_cd": "EGW99999", "rsp_msg": "조회 실패", "rsp_sub_msg": "잘못된 종목"},
                status_code=500,
            )
        ]
    )
    with pytest.raises(BrokerError) as exc:
        broker.get_stock_price("TQQQ", "NAS")
    message = str(exc.value)
    assert "EGW99999" in message
    assert "조회 실패" in message and "잘못된 종목" in message


def test_5820_empty_data_returns_none_balance(make_broker):
    """5820(조회결과 없음) + 빈 data는 오류가 아니라 잔고 없음(None)입니다."""
    broker, _session = make_broker([FakeResponse({"rsp_cd": "5820", "data": []})])
    assert broker.get_balance("TQQQ", "NAS") is None


def test_5820_with_data_returns_balance(make_broker):
    """5820이어도 data가 있으면 그대로 사용합니다 (공식 필드 기준)."""
    broker, _session = make_broker(
        [
            FakeResponse(
                {
                    "rsp_cd": "5820",
                    "data": [
                        {
                            "ovrs_stck_symb_iscd": "TQQQ.OQ",
                            "rmnd_dqty": 3,
                            "seln_able_dqty": 3,
                            "avrg_unpr": 70.1,
                        }
                    ],
                }
            )
        ]
    )
    balance = broker.get_balance("TQQQ", "NAS")
    assert balance.quantity == 3
    assert balance.avg_price == pytest.approx(70.1)


def test_balance_symbol_mismatch_returns_none(make_broker):
    """ovrs_stck_symb_iscd가 심볼과 다르면 첫 행 fallback 없이 None."""
    broker, _session = make_broker(
        [
            ok(
                rsp_cd="5766",
                data=[
                    {"ovrs_stck_symb_iscd": "SOXL.AX", "rmnd_dqty": 5, "avrg_unpr": 30.0},
                    {"ovrs_stck_symb_iscd": "SOXL", "rmnd_dqty": 2, "avrg_unpr": 70.0},
                ],
            )
        ]
    )
    # TQQQ 행이 전혀 없으면 첫 행(SOXL)로 대체하지 않고 None
    assert broker.get_balance("TQQQ", "NAS") is None


def test_daily_closes_returned_oldest_first(make_broker):
    """일봉은 prpr 종가를 오래된 순으로, 당일(ET) 미확정 봉은 제외합니다."""
    broker, _session = make_broker(
        [
            ok(
                data=[
                    {"date": et_days_ago(0), "prpr": "99.9"},   # 당일(ET) → 제외
                    {"date": et_days_ago(1), "prpr": "73.0"},
                    {"date": et_days_ago(2), "prpr": "72.0"},
                    {"date": et_days_ago(3), "prpr": "71.0"},
                ]
            ),
        ]
    )
    assert broker.get_daily_closes("TQQQ", "NAS", days=3) == [71.0, 72.0, 73.0]


def test_daily_closes_params_official(make_broker):
    """일봉 query는 공식 5종 (mod_stpr_cls_code 포함, from/to/days 없음)."""
    broker, session = make_broker([ok(data=[])])
    broker.get_daily_closes("TQQQ", "NAS", days=5)
    assert session.calls[0]["params"] == {
        "mrkt_div_code": "OV",
        "mrkt_cls_code": "OQ",
        "iscd": "TQQQ",
        "dely_rltm_cls_code": "0",
        "mod_stpr_cls_code": "0",
    }


def test_purchase_amount_uses_usd_row(make_broker):
    """매수가능금액은 공식 통화 필드 crcd가 USD인 행을 사용합니다."""
    broker, _session = make_broker(
        [
            ok(
                rsp_cd="5766",
                data=[
                    {"crcd": "KRW", "oder_crnc_oder_able_camt": "100"},
                    {"crcd": "USD", "oder_crnc_oder_able_camt": "12345.67"},
                ],
            )
        ]
    )
    assert broker.get_purchase_amount("TQQQ", "NAS").orderable_cash == pytest.approx(12345.67)


def test_purchase_amount_empty_returns_zero(make_broker):
    broker, _session = make_broker([ok(rsp_cd="5766", data=[])])
    assert broker.get_purchase_amount("TQQQ", "NAS").orderable_cash == 0.0


# ═══════════════════════════════════════════════════════════════════
# 토큰 재발급 / 조회 한정 재시도
# ═══════════════════════════════════════════════════════════════════

def test_token_error_triggers_single_reissue(make_broker, monkeypatch):
    """EGW00121 응답 시 토큰을 1회 재발급하고 그 요청만 재시도합니다."""
    broker, session = make_broker(
        [
            FakeResponse({"rsp_cd": "EGW00121", "rsp_msg": "토큰 만료"}),
            price_ok(prpr="70.0", oprc="69.5"),
        ]
    )
    issued = []
    monkeypatch.setattr(broker, "_get_token", lambda: issued.append(1) or f"TOKEN{len(issued)}")
    invalidated = []
    monkeypatch.setattr(
        "broker.meritz.adapter.invalidate_token", lambda: invalidated.append(1)
    )

    price = broker.get_stock_price("TQQQ", "NAS")

    assert price.last == 70.0
    assert len(issued) == 2          # 토큰 2회 발급
    assert len(invalidated) == 1    # 1회만 무효화
    assert len(session.calls) == 2  # 요청 1회만 재시도


def test_token_error_persisting_is_not_retried_forever(make_broker, monkeypatch):
    """토큰 오류가 반복돼도 재시도는 1회로 제한됩니다."""
    broker, session = make_broker(
        [
            FakeResponse({"rsp_cd": "EGW00123", "rsp_msg": "무효 토큰"}),
            FakeResponse({"rsp_cd": "EGW00123", "rsp_msg": "무효 토큰"}),
        ]
    )
    monkeypatch.setattr(broker, "_get_token", lambda: "TOKEN")
    with pytest.raises(BrokerError):
        broker.get_stock_price("TQQQ", "NAS")
    assert len(session.calls) == 2


def test_query_retry_code_retried_for_queries(make_broker):
    """EGW00200은 조회 API에서만 제한 재시도됩니다."""
    broker, session = make_broker(
        [
            FakeResponse({"rsp_cd": "EGW00200", "rsp_msg": "조회 제한"}),
            price_ok(prpr="70.0"),
        ]
    )
    assert broker.get_stock_price("TQQQ", "NAS").last == 70.0
    assert len(session.calls) == 2


def test_order_post_is_not_retried_on_query_retry_code(make_broker):
    """주문 POST는 EGW00200이어도 재전송하지 않습니다 (중복 주문 방지)."""
    broker, session = make_broker(
        [FakeResponse({"rsp_cd": "EGW00200", "rsp_msg": "조회 제한"})]
    )
    with pytest.raises(OrderNotAcceptedError):
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert len(session.calls) == 1


# ═══════════════════════════════════════════════════════════════════
# 주문 이력 (표준 16필드 / 공식 파라미터 / 합성 ID / KST 플래그)
# ═══════════════════════════════════════════════════════════════════

PERIOD_ROW = {
    "der_date": "20260806",
    "der_no": "4615",
    "orgl_oder_no": "",
    "iscd": "TQQQ.OQ",
    "seln_byov_clcd": "2",              # 2 = 매수
    "der_dqty": "10",
    "ovrs_stck_oder_prc": "71.0",
    "cntg_dqty": "4",
    "cntg_crnc_prc": "70.0",
    "cntg_camt": "280.0",
    "ovrs_stck_oder_stat_cls_code": "0",  # 0 = 정상
    "der_rctf_cncl_cls_code": "0",        # 0 = 원주문
    "rctf_cncl_cls_name": "원주문",
    "der_exec_ymdh": "20260806 04:16:50",  # KST 일시
    "cntg_ymdh": "20260806 04:20:00",
    "aspr_type_code": "5",               # 5 → LOC(32) 역매핑
    "crcd": "USD",
    "prdt_nm": "TQQQ",
    "mkt_nm": "나스닥",
}

CANCEL_ROW = {
    "der_date": "20260806",
    "der_no": "4700",
    "orgl_oder_no": "4615",
    "iscd": "TQQQ.OQ",
    "seln_byov_clcd": "2",
    "der_dqty": "0",
    "cntg_dqty": "0",
    "ovrs_stck_oder_stat_cls_code": "0",
    "der_rctf_cncl_cls_code": "2",        # 2 = 취소
    "rctf_cncl_cls_name": "취소",
    "der_exec_ymdh": "20260806 05:00:00",
    "aspr_type_code": "5",
    "crcd": "USD",
    "prdt_nm": "TQQQ",
}


def test_order_history_contains_standard_fields(make_broker):
    """표준 16개 필드를 모두 포함합니다."""
    broker, _session = make_broker(
        [ok(rsp_cd="5766", data=[dict(PERIOD_ROW)]), ok(rsp_cd="5766", data=[])]
    )
    row = broker.get_order_history("TQQQ", "NAS", days=30)[0]
    missing = [field for field in STANDARD_FIELDS if field not in row]
    assert missing == []
    assert row["sll_buy_dvsn_cd_name"] == "매수"   # seln_byov_clcd=2
    assert row["tr_crcy_cd"] == "USD"              # crcd
    assert row["ft_ord_qty"] == "10"               # der_dqty
    assert row["ft_ccld_qty"] == "4"               # cntg_dqty
    assert row["ft_ccld_amt3"] == "280.0"          # cntg_camt
    assert row["ovrs_excg_cd"] == "OQ"             # iscd 접미사
    assert row["prcs_stat_name"] == "부분체결"      # 4체결 / 잔량 6


def test_history_query_params_official(make_broker):
    """이력 query는 공식 필수 3종 + tr_cont_key만, 당일 API는 파라미터 없음."""
    broker, session = make_broker(
        [ok(rsp_cd="5766", data=[]), ok(rsp_cd="5766", data=[])]
    )
    broker.get_order_history("TQQQ", "NAS")

    history_params = session.calls[0]["params"]
    assert set(history_params) == {"oder_sta_date", "oder_fin_date", "cntg_cls_code"}
    assert len(history_params["oder_sta_date"]) == 8
    assert len(history_params["oder_fin_date"]) == 8
    assert history_params["cntg_cls_code"] == "0"
    assert "prdt_cd" not in history_params
    assert "schl_wrt_dic" not in history_params

    # 당일 API는 공식 스키마상 종목 파라미터가 없습니다
    assert session.calls[1]["path"].endswith("/orders/today")
    assert session.calls[1]["params"] in ({}, None)


def test_history_filters_rows_by_symbol(make_broker):
    """이력 API에 종목 파라미터가 없으므로 iscd prefix로 클라이언트 필터링합니다."""
    other = dict(PERIOD_ROW, der_no="9999", iscd="SOXL.AX")
    broker, _session = make_broker(
        [ok(rsp_cd="5766", data=[dict(PERIOD_ROW), other]), ok(rsp_cd="5766", data=[])]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    assert [row["_plain_oder_no"] for row in rows] == ["4615"]


def test_history_pagination_follows_tr_cont_key(make_broker):
    """rsp_cd=5762/다음 페이지는 tr_cont_key를 원문 그대로 다음 요청에 전달합니다."""
    page2 = dict(PERIOD_ROW, der_no="4614", der_date="20260805",
                 der_exec_ymdh="20260805 04:16:50")
    broker, session = make_broker(
        [
            ok(rsp_cd="5766", data=[dict(PERIOD_ROW)], tr_cont_key="NEXTKEY"),
            ok(rsp_cd="5766", data=[page2]),     # 키 없음 → 마지막 페이지
            ok(rsp_cd="5766", data=[]),          # 당일
        ]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    assert len(rows) == 2
    assert "tr_cont_key" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["tr_cont_key"] == "NEXTKEY"
    assert len(session.calls) == 3  # 페이지 2 + 당일 1


def test_history_5820_returns_empty(make_broker):
    """5820(조회결과 없음) → 빈 목록, 당일 조회는 계속 수행합니다."""
    broker, session = make_broker(
        [FakeResponse({"rsp_cd": "5820", "data": []}), ok(rsp_cd="5766", data=[])]
    )
    assert broker.get_order_history("TQQQ", "NAS") == []
    assert len(session.calls) == 2


def test_order_history_kst_flag_and_no_et_shift(make_broker):
    """der_exec_ymdh 는 KST 일시 — ord_dt 를 그대로 KST로 해석합니다."""
    broker, _session = make_broker(
        [ok(rsp_cd="5766", data=[dict(PERIOD_ROW)]), ok(rsp_cd="5766", data=[])]
    )
    row = broker.get_order_history("TQQQ", "NAS")[0]
    assert row["_ord_dt_is_kst_date"] is True
    assert "_ord_dt_is_us_trading_date" not in row
    assert row["ord_dt"] == "20260806"
    assert row["ord_tmd"] == "041650"
    assert row["ord_datetime_kst"].startswith("2026-08-06T04:16:50")
    assert row["ord_datetime_kst"].endswith("+09:00")
    # resolve_real_kst_from_ord 미적용 → +1일 보정이 없어야 합니다
    assert not row["ord_datetime_kst"].startswith("2026-08-07")


def test_state_order_real_kst_date_flag_skips_et_restore():
    """state._order_real_kst_date: 메리츠(_ord_dt_is_kst_date)는 ET 복원 생략,
    플래그 없는 기존 브로커(KIS 계열)는 기존 ET round-trip 복원을 그대로 유지합니다."""
    from state import _order_real_kst_date

    meritz_row = {"ord_dt": "20260806", "ord_tmd": "041650", "_ord_dt_is_kst_date": True}
    assert _order_real_kst_date(meritz_row) == "20260806"

    legacy_row = {"ord_dt": "20260806", "ord_tmd": "041650"}
    assert _order_real_kst_date(legacy_row) == "20260807"  # KIS 계열 +1일 복원 불변


def test_order_history_today_remaining_wins(make_broker):
    """당일 행의 ncnt_qty(공식 잔량 신뢰값)를 우선 사용합니다."""
    today_row = {
        **PERIOD_ROW,
        "odqt": "10",
        "cntg_dqty": "7",
        "cntg_camt": "490.0",
        "ncnt_qty": "3",
    }
    broker, _session = make_broker(
        [
            ok(rsp_cd="5766", data=[dict(PERIOD_ROW)]),
            ok(rsp_cd="5766", data=[today_row]),
        ]
    )
    row = broker.get_order_history("TQQQ", "NAS")[0]
    assert row["ft_ccld_qty"] == "7"
    assert row["nccs_qty"] == "3"  # 당일 ncnt_qty=3 (당일 이력 기준)


def test_order_history_cancel_row_updates_original(make_broker):
    """취소행은 (der_date, 원주문번호) 쌍으로 원주문에 반영됩니다."""
    broker, _session = make_broker(
        [
            ok(rsp_cd="5766", data=[dict(PERIOD_ROW)]),
            ok(rsp_cd="5766", data=[dict(CANCEL_ROW)]),
        ]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    original = next(r for r in rows if r["odno"].endswith("0000004615"))
    assert original["prcs_stat_name"] == "취소"
    assert original["nccs_qty"] == "0"
    assert original["_remaining"] == 0


def test_cancel_pair_requires_same_date(make_broker):
    """번호는 같아도 주문일자가 다르면 취소 미연결 (날짜 쌍 매칭 검증)."""
    older = dict(
        PERIOD_ROW,
        der_date="20260805",
        der_no="4615",
        der_exec_ymdh="20260805 04:16:50",
    )
    cross_date_cancel = dict(CANCEL_ROW, der_date="20260806")
    broker, _session = make_broker(
        [ok(rsp_cd="5766", data=[older, cross_date_cancel]), ok(rsp_cd="5766", data=[])]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    original = next(r for r in rows if r["_plain_oder_no"] == "4615")
    assert original["prcs_stat_name"] != "취소"  # 다른 날짜 → 미연결
    cancel_row = next(r for r in rows if r["_plain_oder_no"] == "4700")
    assert cancel_row["prcs_stat_name"] == "취소"


def test_order_history_survives_today_failure(make_broker):
    """당일 이력 조회가 실패해도 기간 이력으로 진행합니다."""
    broker, _session = make_broker(
        [
            ok(rsp_cd="5766", data=[dict(PERIOD_ROW)]),
            FakeResponse({"rsp_cd": "EGW88888", "rsp_msg": "당일 조회 실패"}),
        ]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    assert len(rows) == 1


def test_history_aspr_type_code_reverse_map(make_broker):
    """aspr_type_code 역매핑: 1→05, 2→01, 4→31, 5→32, 6→33, 7→34."""
    cases = [("1", "05"), ("2", "01"), ("4", "31"), ("5", "32"), ("6", "33"), ("7", "34")]
    rows_data = [
        dict(PERIOD_ROW, der_no=str(5000 + index), aspr_type_code=aspr)
        for index, (aspr, _expect) in enumerate(cases)
    ]
    broker, _session = make_broker(
        [ok(rsp_cd="5766", data=rows_data), ok(rsp_cd="5766", data=[])]
    )
    rows = broker.get_order_history("TQQQ", "NAS")
    got = {row["_plain_oder_no"]: row["_oder_cls_code"] for row in rows}
    expected = {str(5000 + index): expect for index, (_aspr, expect) in enumerate(cases)}
    assert got == expected


def test_synthetic_odno_prevents_collision():
    """서로 다른 날짜의 같은 주문번호는 서로 다른 합성 ID가 됩니다."""
    a = MeritzBroker._build_synthetic_odno("20260806", "4615")
    b = MeritzBroker._build_synthetic_odno("20260807", "4615")
    assert a != b
    assert a == "20260806:0000004615"
    assert b == "20260807:0000004615"
    # 제로패딩으로 자릿수가 다른 번호도 충돌하지 않습니다
    assert MeritzBroker._build_synthetic_odno("20260806", "1") != a
    assert MeritzBroker._parse_synthetic_odno(a) == ("20260806", "4615")
    assert MeritzBroker._parse_synthetic_odno("4615")[1] == "4615"


# ═══════════════════════════════════════════════════════════════════
# 주문 접수 / 경고 등급
# ═══════════════════════════════════════════════════════════════════

def accepted_response(der_no="4615", oder_date="20260806", warn_cls_code="0"):
    """주문 접수 응답 — 공식 스키마대로 warn/주문번호는 data 안에 있습니다."""
    return FakeResponse(
        {
            "rsp_cd": "0001",
            "rsp_msg": "정상 접수",
            "data": {
                "der_no": der_no,
                "oder_date": oder_date,
                "warn_cls_code": warn_cls_code,
            },
        }
    )


def test_place_order_success_and_mapping(make_broker):
    """성공 시 공식 body 7필드 + OrderResult(합성 ID, is_reservation=False)."""
    broker, session = make_broker([accepted_response()])
    result = broker.place_order("TQQQ", "OQ", "BUY", 10, 70.0, "LIMIT")
    assert result.order_id == "20260806:0000004615"
    assert result.is_reservation is False
    assert len(result.order_time) == 14

    call = session.calls[0]
    assert call["path"] == "/trading/v1/overseas/orders/buy"
    assert call["json_body"] == {
        "ovrs_stck_symb_iscd": "TQQQ",
        "ovrs_exch_vndr_idno": "0537",   # OQ → NASDAQ 벤더코드
        "oder_cls_code": "01",           # LIMIT
        "oder_cond_cls_code": "0",
        "odqt": 10,
        "ovrs_stck_oder_prc": 70.0,
        "warn_cnfr_yn": "N",             # 1차는 미확인
    }


@pytest.mark.parametrize(
    "order_type,cls_code,expect_price",
    [
        ("LIMIT", "01", 70.0),   # 지정가
        ("MKT", "05", 0.0),      # 시장가 → 단가 0
        ("LOO", "31", 70.0),     # 장시작 지정가
        ("LOC", "32", 70.0),     # 장시작 지정가
        ("MOO", "33", 0.0),      # 장시작 시장가 → 단가 0
        ("MOC", "34", 0.0),      # 장마감 시장가 → 단가 0
    ],
)
def test_place_order_type_mappings(make_broker, order_type, cls_code, expect_price):
    """공식 주문구분 매핑 전체 + 시장가 계열 단가 0."""
    broker, session = make_broker([accepted_response()])
    broker.place_order("TQQQ", "NAS", "BUY", 1, 70.0, order_type)
    body = session.calls[0]["json_body"]
    assert body["oder_cls_code"] == cls_code
    assert body["ovrs_stck_oder_prc"] == expect_price
    assert body["ovrs_exch_vndr_idno"] == "0537"


def test_place_order_sell_path_and_price_normalization(make_broker):
    """매도 경로 + 호가 단위 정규화 + 시장가 계열은 단가 0."""
    broker, session = make_broker([accepted_response(), accepted_response(der_no="4616")])
    broker.place_order("SOXL", "AX", "SELL", 3, 70.12345, "LIMIT")
    broker.place_order("SOXL", "AX", "SELL", 3, 70.0, "MOC")

    sell = session.calls[0]
    assert sell["path"] == "/trading/v1/overseas/orders/sell"
    assert sell["json_body"]["ovrs_exch_vndr_idno"] == "0066"  # AMEX
    assert sell["json_body"]["ovrs_stck_oder_prc"] == 70.12    # $1+ → 2자리 버림
    assert session.calls[1]["json_body"]["oder_cls_code"] == "34"  # MOC
    assert session.calls[1]["json_body"]["ovrs_stck_oder_prc"] == 0.0


def test_place_order_rejects_unknown_type(make_broker):
    """미지원 주문 유형은 전송 전에 차단됩니다 (미접수 확정)."""
    broker, session = make_broker([])
    with pytest.raises(OrderNotAcceptedError):
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "UNKNOWN")
    assert session.calls == []


def test_place_order_business_rejection(make_broker):
    """rsp_cd != 0001 이면 거부 → OrderNotAcceptedError."""
    broker, _session = make_broker(
        [
            FakeResponse(
                {"rsp_cd": "0002", "rsp_msg": "거부", "rsp_sub_msg": "주문가능금액 부족"}
            )
        ]
    )
    with pytest.raises(OrderNotAcceptedError) as exc:
        broker.place_order("TQQQ", "OQ", "BUY", 100, 70.0, "LIMIT")
    assert "주문가능금액 부족" in str(exc.value)


def test_place_order_warn0_success(make_broker):
    """warn_cls_code=0 이면 정상 접수됩니다."""
    broker, _session = make_broker([accepted_response(warn_cls_code="0")])
    assert broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT") is not None


@pytest.mark.parametrize("warn_code", ["1", "3"])
def test_place_order_warn1_3_default_rejected(make_broker, warn_code):
    """경고 1/3은 기본 OrderNotAcceptedError (자동확인 OFF)."""
    broker, session = make_broker([accepted_response(warn_cls_code=warn_code)])
    with pytest.raises(OrderNotAcceptedError):
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert len(session.calls) == 1  # 재전송 없음


@pytest.mark.parametrize("warn_code", ["1", "3"])
def test_place_order_warn1_3_confirm_resends_once(make_broker, monkeypatch, warn_code):
    """MERITZ_CONFIRM_WARNINGS에 명시된 등급만 warn_cnfr_yn=A 1회 재전송."""
    monkeypatch.setenv("MERITZ_CONFIRM_WARNINGS", warn_code)
    broker, session = make_broker(
        [
            accepted_response(warn_cls_code=warn_code),
            accepted_response(warn_cls_code="0"),
        ]
    )
    result = broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert result.order_id == "20260806:0000004615"
    assert len(session.calls) == 2
    assert session.calls[1]["json_body"]["warn_cnfr_yn"] == "A"


def test_place_order_warn2_never_resends(make_broker, monkeypatch):
    """경고 2는 재전송 없이 OrderNotAcceptedError 입니다."""
    monkeypatch.setenv("MERITZ_CONFIRM_WARNINGS", "1,2,3")
    broker, session = make_broker([accepted_response(warn_cls_code="2")])
    with pytest.raises(OrderNotAcceptedError):
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert len(session.calls) == 1


def test_place_order_warn_unknown_code_rejected(make_broker):
    """문서에 없는 경고 등급도 안전하게 거부합니다."""
    broker, _session = make_broker([accepted_response(warn_cls_code="9")])
    with pytest.raises(OrderNotAcceptedError):
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")


def test_place_order_timeout_is_order_error(make_broker):
    """POST 네트워크 타임아웃은 접수 여부가 불확실 → OrderError (fence 유지)."""
    broker, session = make_broker([requests.exceptions.Timeout("timed out")])
    with pytest.raises(OrderError) as exc:
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert not isinstance(exc.value, OrderNotAcceptedError)
    assert len(session.calls) == 1  # 네트워크 재시도 없음


def test_place_order_non_json_is_order_error(make_broker):
    """비JSON 응답도 접수 여부가 불확실 → OrderError."""
    broker, _session = make_broker([FakeResponse(status_code=502, invalid_json=True)])
    with pytest.raises(OrderError) as exc:
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert not isinstance(exc.value, OrderNotAcceptedError)


def test_place_order_missing_der_no_is_order_error(make_broker):
    """접수 응답에 주문번호(der_no)가 없으면 OrderError 로 상태 불확실을 유지합니다."""
    broker, _session = make_broker(
        [FakeResponse({"rsp_cd": "0001", "rsp_msg": "정상 접수", "data": {"warn_cls_code": "0"}})]
    )
    with pytest.raises(OrderError) as exc:
        broker.place_order("TQQQ", "OQ", "BUY", 1, 70.0, "LIMIT")
    assert not isinstance(exc.value, OrderNotAcceptedError)


# ═══════════════════════════════════════════════════════════════════
# 주문 취소
# ═══════════════════════════════════════════════════════════════════

def test_cancel_order_uses_submission_cache(make_broker):
    """제출 캐시에서 oder_cls_code/수량을 복원해 공식 4필드를 전송합니다."""
    broker, session = make_broker(
        [accepted_response(), ok(rsp_cd="0001", data={"warn_cls_code": "0"})]
    )
    order_id = broker.place_order("TQQQ", "OQ", "BUY", 10, 70.0, "LOC").order_id
    broker.cancel_order(order_id)

    call = session.calls[1]
    assert call["path"] == "/trading/v1/overseas/orders/cancel"
    assert call["json_body"] == {
        "oder_date": "20260806",
        "orgl_oder_no": "4615",
        "oder_cls_code": "32",   # LOC
        "odqt": 10,              # 공식 수량 필드명
    }


def test_cancel_order_falls_back_to_today_history(make_broker):
    """캐시가 없으면 당일 이력에서 aspr_type_code/수량을 복원합니다."""
    today_row = {
        "der_date": "20260806",
        "der_no": "4615",
        "iscd": "TQQQ.OQ",
        "seln_byov_clcd": "2",
        "der_exec_ymdh": "20260806 04:16:50",
        "der_dqty": "5",
        "cntg_dqty": "0",
        "ovrs_stck_oder_stat_cls_code": "0",
        "aspr_type_code": "5",   # 5 → LOC(32)
        "crcd": "USD",
    }
    broker, session = make_broker(
        [
            ok(rsp_cd="5766", data=[today_row]),
            ok(rsp_cd="0001", data={"warn_cls_code": "0"}),
        ]
    )
    broker.cancel_order("20260806:0000004615", symbol="TQQQ", exchange="NAS")

    call = session.calls[1]
    assert call["path"] == "/trading/v1/overseas/orders/cancel"
    assert call["json_body"]["oder_cls_code"] == "32"
    assert call["json_body"]["odqt"] == 5
    assert call["json_body"]["orgl_oder_no"] == "4615"


def test_cancel_order_without_meta_is_not_accepted(make_broker):
    """캐시에도 당일 이력에도 없으면 전송 없이 차단합니다."""
    broker, session = make_broker([ok(rsp_cd="5766", data=[])])
    with pytest.raises(OrderNotAcceptedError):
        broker.cancel_order("20260806:0000009999", symbol="TQQQ", exchange="NAS")
    assert len(session.calls) == 1  # 오늘 이력 조회만 수행 (취소 POST 없음)


def test_cancel_order_rejection_is_not_accepted(make_broker):
    """취소 거부는 OrderNotAcceptedError, 네트워크 오류는 OrderError."""
    broker, _session = make_broker(
        [FakeResponse({"rsp_cd": "0003", "rsp_msg": "취소 불가"})]
    )
    broker._submitted_orders["20260806:0000004615"] = {
        "oder_cls_code": "31",
        "odqt": 1,
    }
    with pytest.raises(OrderNotAcceptedError):
        broker.cancel_order("20260806:0000004615")

    broker2, _s2 = make_broker([requests.exceptions.ConnectionError("reset")])
    broker2._submitted_orders["20260806:0000004615"] = {
        "oder_cls_code": "31",
        "odqt": 1,
    }
    with pytest.raises(OrderError) as exc:
        broker2.cancel_order("20260806:0000004615")
    assert not isinstance(exc.value, OrderNotAcceptedError)


def test_cancel_response_warn_code_rejects(make_broker):
    """취소 응답 data.warn_cls_code != 0 이면 미접수로 판정합니다."""
    broker, session = make_broker(
        [ok(rsp_cd="0001", data={"warn_cls_code": "1"})]
    )
    broker._submitted_orders["20260806:0000004615"] = {
        "oder_cls_code": "31",
        "odqt": 1,
    }
    with pytest.raises(OrderNotAcceptedError, match="warn_cls_code"):
        broker.cancel_order("20260806:0000004615")
    assert len(session.calls) == 1


def test_cancel_order_rejects_empty_odno(make_broker):
    broker, session = make_broker([])
    with pytest.raises(OrderNotAcceptedError):
        broker.cancel_order("")
    assert session.calls == []


# ═══════════════════════════════════════════════════════════════════
# auth 모듈
# ═══════════════════════════════════════════════════════════════════

def test_auth_token_cached_until_expiry(monkeypatch):
    """토큰은 만료 60초 전까지 재사용됩니다."""
    posted = []

    def fake_post(url, **kwargs):
        posted.append(url)
        return FakeResponse({"access_token": f"tok{len(posted)}", "expires_in": 3600})

    monkeypatch.setattr("broker.meritz.auth.requests.post", fake_post)

    first = meritz_auth.get_access_token(BASE_URL, "k", "s")
    assert first == "tok1"

    # 만료 임박(60초 이내)이면 재발급
    meritz_auth._cached_expires_at = time.time() + 30
    assert meritz_auth.get_access_token(BASE_URL, "k", "s") == "tok2"
    assert len(posted) == 2

    # 유효한 캐시는 재사용 (재발급 없음)
    meritz_auth._cached_expires_at = time.time() + 3600
    assert meritz_auth.get_access_token(BASE_URL, "k", "s") == "tok2"
    assert len(posted) == 2

    # 만료 후에는 재발급
    meritz_auth._cached_expires_at = time.time() - 1
    assert meritz_auth.get_access_token(BASE_URL, "k", "s") == "tok3"
    assert len(posted) == 3


def test_auth_token_request_includes_scope(monkeypatch):
    """토큰 요청 form은 공식 필수 4개 — scope=login 포함."""
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse({"access_token": "tok-scope", "expires_in": 3600})

    monkeypatch.setattr("broker.meritz.auth.requests.post", fake_post)
    assert meritz_auth.get_access_token(BASE_URL, "k", "s") == "tok-scope"
    assert captured["data"] == {
        "grant_type": "client_credentials",
        "client_id": "k",
        "client_secret": "s",
        "scope": "login",
    }


def test_auth_403_message_has_no_ip_claim(monkeypatch):
    """403 응답 메시지는 IP 차단을 단정하지 않고 권한 기준으로 안내합니다."""
    def fake_post(url, **kwargs):
        return FakeResponse(status_code=403)

    monkeypatch.setattr("broker.meritz.auth.requests.post", fake_post)
    with pytest.raises(AuthError) as exc:
        meritz_auth.get_access_token(BASE_URL, "k", "s")
    message = str(exc.value)
    assert "허용되지 않은 IP" not in message
    assert "권한" in message


def test_auth_requires_credentials():
    with pytest.raises(AuthError, match="MERITZ_APP_KEY"):
        meritz_auth.get_access_token(BASE_URL, "", "")


def test_auth_token_error_codes():
    assert meritz_auth.is_token_error_code("EGW00121")
    assert meritz_auth.is_token_error_code("EGW00123")
    assert not meritz_auth.is_token_error_code("EGW00200")
    assert not meritz_auth.is_token_error_code(None)


# ═══════════════════════════════════════════════════════════════════
# create_broker 등록
# ═══════════════════════════════════════════════════════════════════

def test_create_broker_supports_meritz(monkeypatch):
    """create_broker에 meritz 분기가 있습니다."""
    import inspect

    from broker import create_broker

    assert "meritz" in inspect.getsource(create_broker)
