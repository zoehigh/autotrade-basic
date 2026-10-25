"""
Broker 계약 테스트 — 모든 Broker 구현체가 인터페이스 계약을 준수하는지 검증.

각 브로커(KIS, Kiwoom)는 Mock HTTP 응답을 주입받아:
- 9개 메서드 반환 타입 (dataclass) 검증
- get_order_history 표준 필드 존재 검증
- 예외 처리 검증
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from broker.base import (
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    BrokerError,
    OrderError,
    OrderNotAcceptedError,
)

# NHPLUG 자격증명이 없어도 mock 기반 계약 테스트가 실행되도록,
# 실제 키가 없을 때만 모의 자격증명을 주입합니다.
# (conftest의 자격증명 skip 로직은 collection 시점에 env를 읽으므로
#  모듈 import 시점에 주입해야 nhplug 마커 테스트가 skip되지 않습니다.)
if not os.getenv("NHPLUG_APP_KEY") and not os.getenv("NHPLUG_APP_SECRET"):
    os.environ.setdefault("NHPLUG_APP_KEY", "test_nhplug_app_key")
    os.environ.setdefault("NHPLUG_APP_SECRET", "test_nhplug_app_secret")
if not os.getenv("NHPLUG_ACCT_NO"):
    os.environ.setdefault("NHPLUG_ACCT_NO", "test_nhplug_acct_no")


# ═══════════════════════════════════════════════════════════════════════
# 공통 Mock 응답 팩토리
# ═══════════════════════════════════════════════════════════════════════

def _make_response(json_data, status_code=200, headers=None):
    """requests.Response를 흉내내는 MagicMock을 생성합니다."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.raise_for_status = MagicMock() if resp.ok else MagicMock(
        side_effect=ConnectionError(f"HTTP {status_code}")
    )
    resp.headers = headers or {"tr_cont": ""}
    return resp


# ═══════════════════════════════════════════════════════════════════════
# BrokerContractTest — 추상 베이스
# ═══════════════════════════════════════════════════════════════════════

class BrokerContractTest:
    """
    브로커 계약 테스트 베이스 클래스.
    서브클래스는 _create_broker()를 구현하고 필요한 mock을 fixture로 설정합니다.

    계약 검증 항목:
    - 속성: name (str, non-empty)
    - 시장 정보: is_trading_day (→ bool)
    - 조회 API: get_stock_price, get_stock_quotation, get_balance,
                get_purchase_amount, get_order_history (→ dataclass / list[dict])
    - 주문 API: place_order (→ OrderResult | None)
    - 유틸리티: exchange_code (→ str)
    - 라이프사이클: close (→ no-op)
    """

    def _create_broker(self):
        """테스트할 Broker 인스턴스를 반환."""
        raise NotImplementedError

    # ── 속성 계약 ───────────────────────────────────────────────────────

    def test_name_returns_nonempty_string(self):
        broker = self._create_broker()
        assert isinstance(broker.name, str)
        assert len(broker.name) > 0

    def test_name_is_lowercase(self):
        broker = self._create_broker()
        assert broker.name == broker.name.lower()

    # ── 시장 정보 계약 ──────────────────────────────────────────────────

    def test_is_trading_day_returns_bool(self):
        broker = self._create_broker()
        result = broker.is_trading_day()
        assert isinstance(result, bool)

    # ── 조회 API 계약 ──────────────────────────────────────────────────

    def test_get_stock_price_returns_stockprice(self):
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert isinstance(result, StockPrice)
        assert isinstance(result.open, float)
        assert isinstance(result.last, float)

    def test_get_stock_quotation_returns_quotation(self):
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert isinstance(result, StockQuotation)
        assert isinstance(result.tradable, bool)
        assert isinstance(result.last, float)

    def test_get_balance_returns_balance_or_none(self):
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        if result is not None:
            assert isinstance(result, Balance)
            assert isinstance(result.quantity, int)
            assert isinstance(result.avg_price, float)

    def test_get_purchase_amount_returns_purchaseamount(self):
        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert isinstance(result, PurchaseAmount)
        assert isinstance(result.orderable_cash, float)

    def test_get_order_history_returns_list_of_dicts(self):
        broker = self._create_broker()
        result = broker.get_order_history("TQQQ", "NAS")
        assert isinstance(result, list)
        if result:
            for item in result:
                assert isinstance(item, dict)

    def test_get_order_history_contains_standard_fields(self):
        """get_order_history의 각 항목은 state.py가 기대하는 표준 필드를 포함해야 합니다."""
        broker = self._create_broker()
        result = broker.get_order_history("TQQQ", "NAS")
        if not result:
            pytest.skip("Skip standard field validation: no order history.")

        standard_fields = {
            "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
            "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
            "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
            "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
        }

        for item in result:
            missing = standard_fields - set(item.keys())
            assert not missing, (
                f"표준 필드 누락: {missing}\n"
                f"  item keys: {sorted(item.keys())}"
            )

    # ── 주문 API 계약 ──────────────────────────────────────────────────

    def test_place_order_returns_orderresult_or_none(self):
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NASD", "BUY", 1, 50.0, "LIMIT")
        if result is not None:
            assert isinstance(result, OrderResult)
            assert isinstance(result.order_id, str)
            assert isinstance(result.order_time, str)
            assert isinstance(result.is_reservation, bool)

    # ── 유틸리티 계약 ───────────────────────────────────────────────────

    def test_exchange_code_returns_string(self):
        broker = self._create_broker()
        result = broker.exchange_code("NAS")
        assert isinstance(result, str)
        assert len(result) > 0

    # ── 라이프사이클 계약 ──────────────────────────────────────────────

    def test_close_does_not_raise(self):
        broker = self._create_broker()
        try:
            broker.close()
        except Exception as e:
            pytest.fail(f"close()가 예외를 발생시켰습니다: {e}")


# ═══════════════════════════════════════════════════════════════════════
# KISBroker 계약 테스트
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_patch_kis_dependencies")
class TestKISBrokerContract(BrokerContractTest):
    """
    KISBroker 인터페이스 계약 테스트.

    KISSession.request를 mock하여 실제 API 호출 없이
    모든 Broker 계약을 검증합니다.
    """

    @pytest.fixture(autouse=True)
    def _patch_kis_dependencies(self, request):
        """
        KISBroker가 의존하는 모듈들을 patch합니다.

        - KISSession.request → mock 응답 반환
        - get_access_token → 가짜 토큰 반환
        - is_us_trading_day → True
        - KIS_MODE → "real" (예약주문 경로 회피)
        - KIS_ACCOUNT_NO, ACNT_PRDT_CD → 가짜 값
        - is_kst_regular_market → True
        - get_kst_now → 고정 시각
        """
        fixed_kst = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

        target = "broker.kis.adapter"
        patches = [
            patch(f"{target}.KISSession"),
            patch(f"{target}.get_access_token", return_value={
                "access_token": "mock_token",
                "token_type": "Bearer",
                "expires_in": 86400,
                "access_token_token_expired": "2026-07-07 00:00:00",
            }),
            patch(f"{target}.is_us_trading_day", return_value=True),
            patch(f"{target}.KIS_MODE", "real"),
            patch(f"{target}.KIS_ACCOUNT_NO", "12345678"),
            patch(f"{target}.ACNT_PRDT_CD", "01"),
            patch(f"{target}.is_kst_regular_market", return_value=True),
            patch(f"{target}.get_kst_now", return_value=fixed_kst),
        ]

        for p in patches:
            p.start()
            request.addfinalizer(p.stop)

        # KISSession mock: 인스턴스 설정
        mock_session_cls = sys.modules[f"{target}"].KISSession
        mock_session_instance = MagicMock()
        mock_session_cls.return_value = mock_session_instance
        self._mock_session = mock_session_instance

        # 기본 응답: 모든 API가 기본적으로 성공하는 응답
        self._setup_default_responses(mock_session_instance)

    def _setup_default_responses(self, mock_session):
        """
        mock KISSession.request에 경로 기반 기본 성공 응답을 설정합니다.

        각 API 메서드는 다른 output 구조가 필요하므로 side_effect를 사용합니다:
        - price-detail(/quotations/price-detail): output=dict
        - quotation(/quotations/price): output=dict
        - balance(/inquire-balance): output1=list
        - purchase-amount(/inquire-psamount): output=dict
        - order-history(/inquire-ccnl): output=list
        - order(/trading/order): output=dict
        """
        def _side_effect(method, path, *args, **kwargs):
            if "price-detail" in path:
                return _make_response({
                    "rt_cd": "0", "output": {"open": "50.00", "last": "52.00"},
                })
            elif "quotations/price" in path:
                return _make_response({
                    "rt_cd": "0", "output": {"ordy": "Y", "last": "52.00"},
                })
            elif "inquire-balance" in path:
                return _make_response({
                    "rt_cd": "0", "output1": [], "output2": [],
                })
            elif "inquire-psamount" in path:
                return _make_response({
                    "rt_cd": "0", "output": {"ord_psbl_frcr_amt": "5000.00"},
                })
            elif "inquire-ccnl" in path:
                return _make_response({
                    "rt_cd": "0", "output": [],
                })
            elif "trading/order" in path or "order-resv" in path:
                return _make_response({
                    "rt_cd": "0", "output": {"ODNO": "202606150001", "ORD_TMD": "103000"},
                })
            else:
                return _make_response({
                    "rt_cd": "0", "output": {},
                })

        mock_session.request.side_effect = _side_effect

    def _create_broker(self):
        """mock이 주입된 KISBroker 인스턴스를 반환."""
        from broker.kis.adapter import KISBroker
        return KISBroker()

    def _set_mock_response(self, json_data, status_code=200, headers=None):
        """side_effect를 제거하고 return_value로 단일 응답을 설정합니다."""
        self._mock_session.request.side_effect = None
        self._mock_session.request.return_value = _make_response(
            json_data, status_code, headers
        )

    def test_get_stock_price_values(self):
        """반환된 StockPrice의 open/last 값이 응답과 일치해야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": {"open": "50.00", "last": "52.50"},
        })
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert result.open == 50.0
        assert result.last == 52.50

    def test_get_stock_price_raises_brokererror_on_api_error(self):
        """rt_cd != 0 응답에서 BrokerError가 발생해야 합니다."""
        self._set_mock_response({
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "API 호출 실패",
        })
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="API 호출 실패"):
            broker.get_stock_price("TQQQ", "NAS")

    def test_get_stock_quotation_returns_tradable(self):
        """ordy=Y 응답에서 tradable=True가 반환되어야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": {"ordy": "Y", "last": "52.00"},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is True
        assert result.last == 52.0

    def test_get_stock_quotation_not_tradable(self):
        """ordy=N 응답에서 tradable=False가 반환되어야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": {"ordy": "N", "last": "0"},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is False

    def test_get_balance_returns_none_when_no_position(self):
        """잔고가 없으면 None을 반환해야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output1": [],
            "output2": [],
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is None

    def test_get_balance_with_position(self):
        """잔고가 있으면 Balance dataclass를 반환해야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output1": [
                {
                    "ovrs_pdno": "TQQQ",
                    "ovrs_cblc_qty": "10",
                    "pchs_avg_pric": "48.50",
                }
            ],
            "output2": [],
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is not None
        assert result.quantity == 10
        assert result.avg_price == 48.50

    def test_get_balance_raises_brokererror_on_api_error(self):
        """API 오류(rt_cd != 0)는 BrokerError로 래핑되어야 합니다."""
        self._set_mock_response({
            "rt_cd": "1",
            "msg_cd": "EGW00999",
            "msg1": "잔고 조회 실패",
        })
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="잔고 조회 실패"):
            broker.get_balance("TQQQ", "NAS")

    def test_get_purchase_amount_returns_amount(self):
        """매수가능금액이 PurchaseAmount로 반환되어야 합니다."""
        # get_stock_quotation 호출 먼저 -> 성공
        quotation_resp = _make_response({
            "rt_cd": "0",
            "output": {"ordy": "Y", "last": "52.00"},
        })
        purchase_resp = _make_response({
            "rt_cd": "0",
            "output": {"ord_psbl_frcr_amt": "5000.00"},
        })
        # side_effect를 초기화하고 순차 응답 설정
        self._mock_session.request.side_effect = None
        self._mock_session.request.side_effect = [quotation_resp, purchase_resp]

        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert isinstance(result, PurchaseAmount)
        assert result.orderable_cash == 5000.0

    def test_get_order_history_with_data(self):
        """get_order_history가 각 항목에 표준 필드를 포함해야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": [
                {
                    "ord_dt": "20260615",
                    "ord_tmd": "093000",
                    "prdt_name": "TQQQ",
                    "sll_buy_dvsn_cd_name": "매수",
                    "ft_ord_qty": "5",
                    "ft_ccld_qty": "5",
                    "ft_ccld_unpr3": "54.00",
                    "ft_ccld_amt3": "270.00",
                    "nccs_qty": "0",
                    "prcs_stat_name": "체결완료",
                    "tr_mket_name": "NASDAQ",
                    "tr_crcy_cd": "USD",
                    "odno": "36267",
                    "ovrs_excg_cd": "NASD",
                }
            ],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        item = history[0]

        standard_fields = {
            "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
            "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
            "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
            "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
        }
        missing = standard_fields - set(item.keys())
        assert not missing, f"표준 필드 누락: {missing}"

        assert item["ord_dt"] == "20260615"
        assert item["sll_buy_dvsn_cd_name"] == "매수"
        assert item["ft_ccld_qty"] == "5"
        assert item["odno"] == "36267"

    def test_get_order_history_missing_qty_field_omits_ft_ord_qty(self):
        """원주문 수량이 응답에 없으면 "0"을 합성하지 않고 키를 생략합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": [
                {
                    "ord_dt": "20260615",
                    "ord_tmd": "093000",
                    "prdt_name": "TQQQ",
                    "sll_buy_dvsn_cd_name": "매수",
                    "ft_ccld_qty": "1",
                    "ft_ccld_unpr3": "54.00",
                    "ft_ccld_amt3": "54.00",
                    "nccs_qty": "0",
                    "prcs_stat_name": "체결완료",
                    "tr_mket_name": "NASDAQ",
                    "tr_crcy_cd": "USD",
                    "odno": "36267",
                    "ovrs_excg_cd": "NASD",
                }
            ],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        assert "ft_ord_qty" not in history[0]
        assert history[0]["ft_ccld_qty"] == "1"

    def test_get_order_history_empty(self):
        """체결 내역이 없으면 빈 리스트를 반환해야 합니다."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": [],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")
        assert history == []

    def test_place_order_returns_orderresult(self):
        """성공적인 주문은 OrderResult(order_id, order_time, is_reservation=False)를 반환."""
        self._set_mock_response({
            "rt_cd": "0",
            "output": {"ODNO": "202606150001", "ORD_TMD": "103000"},
        })
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NASD", "BUY", 1, 50.0, "LIMIT")

        assert result is not None
        assert isinstance(result, OrderResult)
        assert result.order_id == "202606150001"
        # 주문 시각은 API 응답이 아니라 로컬 KST(YYYYMMDDHHMMSS)를 사용합니다
        assert len(result.order_time) == 14 and result.order_time.isdigit()
        assert result.is_reservation is False

    def test_place_order_raises_ordererror(self):
        """주문 실패 시 OrderError가 발생해야 합니다."""
        self._set_mock_response({
            "rt_cd": "1",
            "msg_cd": "EGW00201",
            "msg1": "초당 거래건수를 초과하였습니다.",
        })
        broker = self._create_broker()
        with pytest.raises(OrderError, match="초당 거래건수"):
            broker.place_order("TQQQ", "NASD", "BUY", 1, 50.0, "LIMIT")

    # ── KIS 거래소 코드 매핑 (KIS 전용) ───────────────────────────────

    def test_exchange_code_nas_to_nasd(self):
        broker = self._create_broker()
        assert broker.exchange_code("NAS") == "NASD"

    def test_exchange_code_nys_to_nyse(self):
        broker = self._create_broker()
        assert broker.exchange_code("NYS") == "NYSE"

    def test_exchange_code_ams_to_amex(self):
        broker = self._create_broker()
        assert broker.exchange_code("AMS") == "AMEX"


# ═══════════════════════════════════════════════════════════════════════
# KiwoomBroker 계약 테스트
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_patch_kiwoom_dependencies")
class TestKiwoomBrokerContract(BrokerContractTest):
    """
    KiwoomBroker 인터페이스 계약 테스트.

    KiwoomSession.request_with_tr를 mock하여 실제 API 호출 없이
    모든 Broker 계약을 검증합니다.
    """

    @pytest.fixture(autouse=True)
    def _patch_kiwoom_dependencies(self, request):
        """
        KiwoomBroker가 의존하는 모듈들을 patch합니다.

        - KiwoomSession → mock 인스턴스 반환
        - get_access_token → 가짜 토큰 반환 ("mock_token")
        - is_us_trading_day → True
        - get_kst_now → 고정 시각
        - config.BROKER_CONFIG, config.BROKER_MODE, config.HTTP_TIMEOUT → 가짜 값
        """
        fixed_kst = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

        target = "broker.kiwoom.adapter"
        patches = [
            patch(f"{target}.KiwoomSession"),
            patch(f"{target}.get_access_token", return_value="mock_token"),
            patch(f"{target}.is_us_trading_day", return_value=True),
            patch(f"{target}.get_kst_now", return_value=fixed_kst),
            patch("config.BROKER_CONFIG", {
                "app_key": "test",
                "app_secret": "test",
                "account_no": "12345678",
                "domain": "https://mockapi.kiwoom.com",
                "acnt_prdt_cd": "",
            }),
            patch("config.BROKER_MODE", "real"),
            patch("config.HTTP_TIMEOUT", (10, 30)),
        ]

        for p in patches:
            p.start()
            request.addfinalizer(p.stop)

        # KiwoomSession mock: 인스턴스 설정
        mock_session_cls = sys.modules[f"{target}"].KiwoomSession
        mock_session_instance = MagicMock()
        mock_session_cls.return_value = mock_session_instance
        self._mock_session = mock_session_instance

        # 기본 응답: 모든 API가 기본적으로 성공하는 응답
        self._setup_default_responses(mock_session_instance)

    def _setup_default_responses(self, mock_session):
        """
        mock KiwoomSession.request_with_tr에 TR ID 기반 기본 성공 응답을 설정합니다.

        키움은 TR ID로 API를 구분하므로 side_effect에서 tr_id를 기준으로 라우팅합니다.
        """
        from broker.kiwoom.tr_registry import (
            TR_PRICE, TR_ORDERBOOK, TR_BALANCE, TR_DEPOSIT,
            TR_HISTORY, TR_BUY, TR_SELL,
        )

        def _side_effect(tr_id, body, token, extra_headers=None):
            if tr_id == TR_PRICE:  # usa20100 — 현재가
                return _make_response({
                    "return_code": 0, "output": {"open_pric": "50.00", "cur_prc": "52.00"},
                })
            elif tr_id == TR_ORDERBOOK:  # usa20101 — 10호가
                return _make_response({
                    "return_code": 0, "output": {"cur_prc": "52.00"},
                })
            elif tr_id == TR_BALANCE:  # ust21070 — 잔고
                return _make_response({
                    "return_code": 0, "output": [],
                })
            elif tr_id == TR_DEPOSIT:  # ust21110 — 예수금
                return _make_response({
                    "return_code": 0, "output": {"ord_psbl_cash": "5000.00"},
                })
            elif tr_id == TR_HISTORY:  # ust21100 — 거래내역
                return _make_response({
                    "return_code": 0, "output": [],
                })
            elif tr_id in (TR_BUY, TR_SELL):  # ust20000 / ust20001 — 주문
                return _make_response({
                    "return_code": 0, "output": {"ODNO": "202606150001", "ORD_TMD": "103000"},
                })
            else:
                return _make_response({
                    "return_code": 0, "output": {},
                })

        mock_session.request_with_tr.side_effect = _side_effect

    def _create_broker(self):
        """mock이 주입된 KiwoomBroker 인스턴스를 반환."""
        from broker.kiwoom.adapter import KiwoomBroker
        return KiwoomBroker()

    def _set_mock_response(self, json_data, status_code=200, headers=None):
        """side_effect를 제거하고 return_value로 단일 응답을 설정합니다."""
        self._mock_session.request_with_tr.side_effect = None
        self._mock_session.request_with_tr.return_value = _make_response(
            json_data, status_code, headers
        )

    def test_get_stock_price_values(self):
        """반환된 StockPrice의 open/last 값이 응답과 일치해야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": {"open_pric": "50.00", "cur_prc": "52.50"},
        })
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert result.open == 50.0
        assert result.last == 52.50

    def test_get_stock_price_raises_brokererror_on_api_error(self):
        """return_code != 0 응답에서 BrokerError가 발생해야 합니다."""
        self._set_mock_response({
            "return_code": -1,
            "return_msg": "API 호출 실패",
        })
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="API 호출 실패"):
            broker.get_stock_price("TQQQ", "NAS")

    def test_get_stock_quotation_returns_tradable(self):
        """tradable은 항상 True(안전 기본값)가 반환되어야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": {"cur_prc": "52.00"},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is True
        assert result.last == 52.0

    def test_get_balance_returns_none_when_no_position(self):
        """잔고가 없으면 None을 반환해야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": [],
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is None

    def test_get_balance_with_position(self):
        """잔고가 있으면 Balance dataclass를 반환해야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": [
                {
                    "stk_cd": "TQQQ",
                    "poss_qty": "10",
                    "frgn_stk_book_uv": "48.50",
                }
            ],
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is not None
        assert result.quantity == 10
        assert result.avg_price == 48.50

    def test_get_purchase_amount_returns_amount(self):
        """매수가능금액이 PurchaseAmount로 반환되어야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            # 어댑터는 여러 통화 중 crnc_code=="USD" 항목을 찾으므로 USD 지정이 필수다.
            "result_list": [{"crnc_code": "USD", "fc_ord_alowa": "5000.00"}],
        })
        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert isinstance(result, PurchaseAmount)
        assert result.orderable_cash == 5000.0

    def test_get_order_history_with_data(self):
        """get_order_history가 각 항목에 표준 필드를 포함해야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": [
                {
                    "ord_dt": "20260615",
                    "ord_tmd": "093000",
                    "prdt_name": "TQQQ",
                    "sll_buy_dvsn_cd_name": "매수",
                    "ft_ord_qty": "5",
                    "ft_ccld_qty": "5",
                    "ft_ccld_unpr3": "54.00",
                    "ft_ccld_amt3": "270.00",
                    "nccs_qty": "0",
                    "prcs_stat_name": "체결완료",
                    "tr_mket_name": "NASDAQ",
                    "tr_crcy_cd": "USD",
                    "odno": "36267",
                    "ovrs_excg_cd": "ND",
                }
            ],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        item = history[0]

        standard_fields = {
            "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
            "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
            "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
            "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
        }
        missing = standard_fields - set(item.keys())
        assert not missing, f"표준 필드 누락: {missing}"

        assert item["ord_dt"] == "20260615"
        assert item["sll_buy_dvsn_cd_name"] == "매수"
        assert item["ft_ccld_qty"] == "5"
        assert item["odno"] == "36267"

    def test_get_order_history_paginates_with_cont_headers(self):
        """실전 주문이력은 응답 Header의 cont-yn/next-key로 다음 페이지를 조회해야 합니다."""
        from broker.kiwoom.tr_registry import TR_HISTORY

        calls = []
        responses = [
            _make_response(
                {
                    "return_code": 0,
                    "output": [
                        {
                            "ord_dt": "20260615",
                            "ord_tmd": "093000",
                            "prdt_name": "TQQQ",
                            "sll_buy_dvsn_cd_name": "매수",
                            "ft_ord_qty": "5",
                            "ft_ccld_qty": "5",
                            "ft_ccld_unpr3": "54.00",
                            "ft_ccld_amt3": "270.00",
                            "nccs_qty": "0",
                            "prcs_stat_name": "체결완료",
                            "tr_mket_name": "NASDAQ",
                            "tr_crcy_cd": "USD",
                            "odno": "36267",
                            "ovrs_excg_cd": "ND",
                        }
                    ],
                },
                headers={"cont-yn": "Y", "next-key": "NEXT-1", "api-id": TR_HISTORY},
            ),
            _make_response(
                {
                    "return_code": 0,
                    "output": [
                        {
                            "ord_dt": "20260616",
                            "ord_tmd": "094500",
                            "prdt_name": "TQQQ",
                            "sll_buy_dvsn_cd_name": "매도",
                            "ft_ord_qty": "5",
                            "ft_ccld_qty": "5",
                            "ft_ccld_unpr3": "55.00",
                            "ft_ccld_amt3": "275.00",
                            "nccs_qty": "0",
                            "prcs_stat_name": "체결완료",
                            "tr_mket_name": "NASDAQ",
                            "tr_crcy_cd": "USD",
                            "odno": "36268",
                            "ovrs_excg_cd": "ND",
                        }
                    ],
                },
                headers={"cont-yn": "N", "next-key": "", "api-id": TR_HISTORY},
            ),
        ]

        def _side_effect(tr_id, body, token, extra_headers=None):
            assert tr_id == TR_HISTORY
            calls.append({
                "body": dict(body),
                "extra_headers": dict(extra_headers or {}),
            })
            return responses.pop(0)

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 2
        assert len(calls) == 2
        assert calls[0]["extra_headers"] == {"cont-yn": "N", "next-key": ""}
        assert calls[1]["extra_headers"] == {"cont-yn": "Y", "next-key": "NEXT-1"}

    def test_get_order_history_missing_qty_field_omits_ft_ord_qty(self):
        """원주문 수량이 응답에 없으면 "0"을 합성하지 않고 키를 생략합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": [
                {
                    "ord_dt": "20260615",
                    "ord_tmd": "093000",
                    "prdt_name": "TQQQ",
                    "sll_buy_dvsn_cd_name": "매수",
                    "ft_ccld_qty": "1",
                    "ft_ccld_unpr3": "54.00",
                    "ft_ccld_amt3": "54.00",
                    "nccs_qty": "0",
                    "prcs_stat_name": "체결완료",
                    "tr_mket_name": "NASDAQ",
                    "tr_crcy_cd": "USD",
                    "odno": "36267",
                    "ovrs_excg_cd": "ND",
                }
            ],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        assert "ft_ord_qty" not in history[0]
        assert history[0]["ft_ccld_qty"] == "1"

    def test_get_order_history_empty(self):
        """체결 내역이 없으면 빈 리스트를 반환해야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": [],
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")
        assert history == []

    def test_place_order_returns_orderresult(self):
        """성공적인 주문은 OrderResult(order_id, order_time, is_reservation=False)를 반환."""
        # 키움 주문 API는 flat 구조 + 소문자 ord_no 를 반환하고 주문시각은 미제공하므로,
        # 어댑터는 get_kst_now() 로컬 시각(YYYYMMDDHHMMSS)을 사용한다.
        self._set_mock_response({
            "return_code": 0,
            "ord_no": "202606150001",  # KIWOOM 은 flat 구조 + 소문자 ord_no
        })
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "ND", "BUY", 1, 50.0, "LIMIT")

        assert result is not None
        assert isinstance(result, OrderResult)
        assert result.order_id == "202606150001"
        assert len(result.order_time) == 14 and result.order_time.isdigit()
        assert result.is_reservation is False

    # ── Base 계약 테스트 오버라이드 ────────────────────────────────────
    # KiwoomBroker.place_order는 KIS place_order와 동일하게 변환된 API 거래소 코드를
    # 받습니다 (호출 측에서 broker.exchange_code()로 변환). 따라서 "NASD"가 아닌
    # 키움 API 코드 "ND"를 사용합니다.

    def test_place_order_returns_orderresult_or_none(self):
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "ND", "BUY", 1, 50.0, "LIMIT")
        if result is not None:
            assert isinstance(result, OrderResult)
            assert isinstance(result.order_id, str)
            assert isinstance(result.order_time, str)
            assert isinstance(result.is_reservation, bool)

    # ── 키움 거래소 코드 매핑 (키움 전용) ─────────────────────────────
    # 키움은 KIS와 다른 거래소 코드 체계 사용: NA=AMEX, ND=NASDAQ, NY=NYSE

    def test_exchange_code_nas_to_nd(self):
        broker = self._create_broker()
        assert broker.exchange_code("NAS") == "ND"

    def test_exchange_code_nys_to_ny(self):
        broker = self._create_broker()
        assert broker.exchange_code("NYS") == "NY"

    def test_exchange_code_ams_to_na(self):
        broker = self._create_broker()
        assert broker.exchange_code("AMS") == "NA"


# ═══════════════════════════════════════════════════════════════════════
# KiwoomBroker 모의투자(demo) 계약 테스트 — ust21150
# ═══════════════════════════════════════════════════════════════════════

class TestKiwoomDemoBrokerContract:
    """
    KiwoomBroker 모의투자(demo) 계약 테스트.

    KiwoomSession.request_with_tr를 mock하여 ust21150(일별 주문체결내역)의
    응답 처리와 빈 결과 로깅을 검증합니다.
    """

    @pytest.fixture(autouse=True)
    def _patch_kiwoom_demo_dependencies(self, request):
        fixed_kst = datetime(2026, 7, 28, 4, 15, 0, tzinfo=timezone.utc)
        target = "broker.kiwoom.adapter"
        patches = [
            patch(f"{target}.KiwoomSession"),
            patch(f"{target}.get_access_token", return_value="mock_token"),
            patch(f"{target}.is_us_trading_day", return_value=True),
            patch(f"{target}.get_kst_now", return_value=fixed_kst),
            patch("config.BROKER_CONFIG", {
                "app_key": "test", "app_secret": "test",
                "account_no": "12345678",
                "domain": "https://mockapi.kiwoom.com",
                "acnt_prdt_cd": "",
            }),
            patch("config.BROKER_MODE", "demo"),
            patch("config.HTTP_TIMEOUT", (10, 30)),
        ]
        for p in patches:
            p.start()
            request.addfinalizer(p.stop)
        mock_session_cls = sys.modules[f"{target}"].KiwoomSession
        mock_session_instance = MagicMock()
        mock_session_cls.return_value = mock_session_instance
        self._mock_session = mock_session_instance

    def _create_broker(self):
        from broker.kiwoom.adapter import KiwoomBroker
        return KiwoomBroker()

    def _make_demo_item(self, **overrides):
        item = {
            "ord_no": "1001",
            "ord_time": "09:30:00",
            "frgn_stk_nm": "TQQQ",
            "slby_tp_nm": "매수",
            "ord_qty": "10",
            "cntr_qty": "10",
            "cntr_uv": "50.00",
            "ord_remnq": "0",
            "ord_stat_nm": "체결완료",
            "stex_nm": "NASDAQ",
            "crnc_code": "USD",
        }
        item.update(overrides)
        return item

    def test_get_order_history_demo_returns_items(self):
        """ust21150 정상 응답: result_list 항목들이 order_history에 포함되어야 함."""
        from broker.kiwoom.tr_registry import TR_HISTORY_DEMO

        def _side_effect(tr_id, body, token, extra_headers=None):
            if tr_id != TR_HISTORY_DEMO:
                return _make_response({"return_code": 0})
            ord_dt = body.get("ord_dt", "")
            if ord_dt == "20260728":
                return _make_response({
                    "return_code": 0,
                    "result_list": [
                        self._make_demo_item(ord_no="2001"),
                        self._make_demo_item(ord_no="2002", cntr_qty="5", cntr_uv="55.00"),
                    ],
                })
            return _make_response({"return_code": 0, "result_list": []})

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS", days=1)

        assert len(history) == 2
        assert history[0]["odno"] == "2001"
        assert history[1]["odno"] == "2002"

    def test_get_order_history_demo_empty_result(self):
        """ust21150 빈 결과: 모든 날짜 result_list=[] → 빈 리스트 반환."""
        from broker.kiwoom.tr_registry import TR_HISTORY_DEMO

        def _side_effect(tr_id, body, token, extra_headers=None):
            if tr_id != TR_HISTORY_DEMO:
                return _make_response({"return_code": 0})
            return _make_response({"return_code": 0, "result_list": []})

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS", days=1)

        assert len(history) == 0

    def test_get_order_history_demo_501724_error(self):
        """ust21150 501724 에러: BrokerError가 날짜 루프에서 처리되어 빈 리스트 반환."""
        from broker.kiwoom.tr_registry import TR_HISTORY_DEMO

        call_count = 0
        def _side_effect(tr_id, body, token, extra_headers=None):
            nonlocal call_count
            if tr_id == TR_HISTORY_DEMO:
                call_count += 1
                if call_count == 1:
                    raise BrokerError("키움 API 오류 (return_code=20): 501724: 관련자료가 없습니다")
                return _make_response({"return_code": 0, "result_list": []})
            return _make_response({"return_code": 0})

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS", days=1)

        assert len(history) == 0

    def test_get_order_history_demo_field_normalization(self):
        """ust21150 응답 필드가 표준 필드로 정확히 변환되어야 함."""
        from broker.kiwoom.tr_registry import TR_HISTORY_DEMO

        def _side_effect(tr_id, body, token, extra_headers=None):
            if tr_id != TR_HISTORY_DEMO:
                return _make_response({"return_code": 0})
            ord_dt = body.get("ord_dt", "")
            if ord_dt == "20260728":
                return _make_response({
                    "return_code": 0,
                    "result_list": [self._make_demo_item(
                        ord_no="12345",
                        ord_time="14:30:00",
                        cntr_qty="3",
                        cntr_uv="52.50",
                    )],
                })
            return _make_response({"return_code": 0, "result_list": []})

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS", days=1)

        assert len(history) == 1
        item = history[0]
        standard_fields = {
            "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
            "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
            "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
            "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
        }
        missing = standard_fields - set(item.keys())
        assert not missing, f"표준 필드 누락: {missing}"

        assert item["odno"] == "12345"
        assert item["sll_buy_dvsn_cd_name"] == "매수"
        assert item["ft_ccld_qty"] == "3"
        assert item["ft_ccld_unpr3"] == "52.5"
        assert item["ft_ccld_amt3"] == "157.5"
        assert item["ord_dt"] == "20260728"
        assert item["prdt_name"] == "TQQQ"
        assert item["tr_mket_name"] == "NASDAQ"
        assert item["ovrs_excg_cd"] == ""

        # 모의(ust21150)도 ord_dt=미국영업일 컨벤션 (2026-08-20 실측) → 보정 플래그/복원 시각
        assert item["_ord_dt_is_us_trading_date"] is True
        # ord_dt=20260728 + 14:30 KST → ET round-trip(offset 0) 성공 → 동일 시각 복원
        assert item["_resolved_kst_iso"] == "2026-07-28T14:30:00+09:00"

    def test_get_order_history_demo_missing_qty_field_omits_ft_ord_qty(self):
        """ust21150 원주문 수량(ord_qty)이 없으면 "0"을 합성하지 않습니다."""
        from broker.kiwoom.tr_registry import TR_HISTORY_DEMO

        def _side_effect(tr_id, body, token, extra_headers=None):
            if tr_id != TR_HISTORY_DEMO:
                return _make_response({"return_code": 0})
            ord_dt = body.get("ord_dt", "")
            if ord_dt == "20260728":
                return _make_response({
                    "return_code": 0,
                    "result_list": [self._make_demo_item(
                        ord_no="12345",
                        ord_time="14:30:00",
                        cntr_qty="1",
                        cntr_uv="52.50",
                    )],
                })
            return _make_response({"return_code": 0, "result_list": []})

        self._mock_session.request_with_tr.side_effect = _side_effect
        broker = self._create_broker()
        # ord_qty만 제거해 "필드 없음"을 재현합니다.
        original_make_demo_item = self._make_demo_item

        def _make_demo_item_without_qty(**overrides):
            item = original_make_demo_item(**overrides)
            item.pop("ord_qty", None)
            return item

        self._make_demo_item = _make_demo_item_without_qty
        try:
            history = broker.get_order_history("TQQQ", "NAS", days=1)
        finally:
            self._make_demo_item = original_make_demo_item

        assert len(history) == 1
        assert "ft_ord_qty" not in history[0]
        assert history[0]["ft_ccld_qty"] == "1"


# ═══════════════════════════════════════════════════════════════════════
# TossBroker 계약 테스트
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_patch_toss_dependencies")
class TestTossBrokerContract(BrokerContractTest):
    """
    TossBroker 인터페이스 계약 테스트.

    TossSession.get/post를 mock하여 실제 API 호출 없이
    모든 Broker 계약을 검증합니다.
    """

    @pytest.fixture(autouse=True)
    def _patch_toss_dependencies(self, request):
        """
        TossBroker가 의존하는 모듈들을 patch합니다.

        - TossSession → mock 인스턴스 반환
        - get_access_token → 가짜 토큰 반환
        - is_us_trading_day → True
        - get_kst_now → 고정 시각
        - config.BROKER_CONFIG, config.HTTP_TIMEOUT → 가짜 값
        """
        fixed_kst = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

        target = "broker.toss.adapter"
        patches = [
            patch(f"{target}.TossSession"),
            patch(f"{target}.get_access_token", return_value="mock_token"),
            patch(f"{target}.is_us_trading_day", return_value=True),
            patch(f"{target}.get_kst_now", return_value=fixed_kst),
            patch("config.BROKER_CONFIG", {
                "client_id": "test_client_id",
                "client_secret": "test_client_secret",
                "account_seq": "1",
                "domain": "https://openapi.tossinvest.com",
            }),
            patch("config.BROKER_MODE", "real"),
            patch("config.HTTP_TIMEOUT", (10, 30)),
        ]

        for p in patches:
            p.start()
            request.addfinalizer(p.stop)

        # TossSession mock: 인스턴스 설정
        mock_session_cls = sys.modules[f"{target}"].TossSession
        mock_session_instance = MagicMock()
        mock_session_cls.return_value = mock_session_instance
        self._mock_session = mock_session_instance

        # 기본 응답 설정
        self._setup_default_responses(mock_session_instance)

    def _setup_default_responses(self, mock_session):
        """
        mock TossSession에 경로 기반 기본 성공 응답을 설정합니다.
        """

        def _get_side_effect(path, token, params=None, extra_headers=None):
            if "/api/v1/prices" in path:
                return _make_response({
                    "result": [{"symbol": "TQQQ", "lastPrice": "52.00"}],
                })
            elif "/api/v1/holdings" in path:
                return _make_response({
                    "result": {"items": [], "overview": {}},
                })
            elif "/api/v1/buying-power" in path:
                return _make_response({
                    "result": {"cashBuyingPower": "5000.00"},
                })
            elif "/api/v1/orders" in path:
                return _make_response(
                    {"result": {"orders": [], "nextCursor": None}},
                    headers={"X-RateLimit-Limit": "6"},
                )
            else:
                return _make_response({"result": {}})

        def _post_side_effect(path, token, json_body=None, extra_headers=None):
            if "/api/v1/orders" in path:
                return _make_response({
                    "result": {"orderId": "mock_order_123"},
                })
            return _make_response({"result": {}})

        mock_session.get.side_effect = _get_side_effect
        mock_session.post.side_effect = _post_side_effect

    def _create_broker(self):
        """mock이 주입된 TossBroker 인스턴스를 반환."""
        from broker.toss.adapter import TossBroker
        return TossBroker()

    def _set_mock_response(self, json_data, status_code=200, headers=None):
        """side_effect를 제거하고 return_value로 단일 응답을 설정합니다."""
        self._mock_session.get.side_effect = None
        self._mock_session.post.side_effect = None
        self._mock_session.get.return_value = _make_response(
            json_data, status_code, headers
        )
        self._mock_session.post.return_value = _make_response(
            json_data, status_code, headers
        )

    # ── 토스 전용 테스트 ──────────────────────────────────────────────

    def test_get_stock_price_values(self):
        """반환된 StockPrice의 last 값이 응답과 일치해야 합니다 (open=0)."""
        self._set_mock_response({
            "result": [{"symbol": "TQQQ", "lastPrice": "52.50"}],
        })
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert result.open == 0.0
        assert result.last == 52.50

    def test_get_stock_price_empty_result_raises(self):
        """결과가 비어있으면 BrokerError가 발생해야 합니다."""
        self._set_mock_response({"result": []})
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="심볼을 찾을 수 없습니다"):
            broker.get_stock_price("INVALID", "NAS")

    def test_get_stock_quotation_returns_tradable(self):
        """토스는 tradable=True를 기본값으로 반환해야 합니다."""
        self._set_mock_response({
            "result": [{"symbol": "TQQQ", "lastPrice": "52.00"}],
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is True
        assert result.last == 52.0

    def test_get_balance_with_position(self):
        """잔고가 있으면 Balance dataclass를 반환해야 합니다."""
        self._set_mock_response({
            "result": {
                "items": [
                    {
                        "symbol": "TQQQ",
                        "name": "ProShares UltraPro QQQ",
                        "quantity": 10,
                        "averagePurchasePrice": "48.50",
                    }
                ],
                "overview": {},
            },
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is not None
        assert result.quantity == 10
        assert result.avg_price == 48.5

    def test_get_balance_no_position_returns_none(self):
        """잔고가 없으면 None을 반환해야 합니다."""
        self._set_mock_response({
            "result": {"items": [], "overview": {}},
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is None

    def test_get_purchase_amount_returns_value(self):
        """매수가능금액을 올바르게 반환해야 합니다."""
        self._set_mock_response({
            "result": {"cashBuyingPower": "5000.00"},
        })
        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert result.orderable_cash == 5000.0

    def test_get_order_history_normalizes_side(self):
        """side 필드가 "매수"/"매도"로 올바르게 정규화되어야 합니다."""
        self._set_mock_response({
            "result": {
                "orders": [
                    {
                        "orderId": "ord_001",
                        "symbol": "TQQQ",
                        "side": "BUY",
                        "orderType": "LIMIT",
                        "timeInForce": "DAY",
                        "status": "FILLED",
                        "quantity": 10,
                        "price": "50.00",
                        "orderedAt": "2026-06-15T10:30:00+09:00",
                        "execution": {
                            "filledQuantity": 10,
                            "averagePrice": "50.00",
                            "filledAmount": "500.00",
                        },
                    },
                    {
                        "orderId": "ord_002",
                        "symbol": "TQQQ",
                        "side": "SELL",
                        "orderType": "LIMIT",
                        "timeInForce": "DAY",
                        "status": "FILLED",
                        "quantity": 5,
                        "price": "55.00",
                        "orderedAt": "2026-06-16T14:00:00+09:00",
                        "execution": {
                            "filledQuantity": 5,
                            "averagePrice": "55.00",
                            "filledAmount": "275.00",
                        },
                    },
                ],
                "nextCursor": None,
            },
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 2
        assert history[0]["sll_buy_dvsn_cd_name"] == "매수"
        assert history[1]["sll_buy_dvsn_cd_name"] == "매도"
        assert history[0]["ft_ccld_qty"] == "10"
        assert history[0]["ft_ccld_unpr3"] == "50.0"
        assert history[0]["odno"] == "ord_001"
        assert history[0]["ord_dt"] == "20260615"
        assert history[0]["ord_tmd"] == "103000"
        assert history[0]["ft_ccld_amt3"] == "500.0"

    def test_get_order_history_empty(self):
        """체결 내역이 없으면 빈 리스트를 반환해야 합니다."""
        self._set_mock_response({
            "result": {"orders": [], "nextCursor": None},
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")
        assert history == []

    def test_place_order_returns_orderresult(self):
        """성공적인 주문은 OrderResult를 반환해야 합니다."""
        self._set_mock_response({
            "result": {"orderId": "mock_order_123"},
        })
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NASDAQ", "BUY", 10, 50.0, "LIMIT")

        assert result is not None
        assert isinstance(result, OrderResult)
        assert result.order_id == "mock_order_123"
        assert result.is_reservation is False

    def test_place_order_loc_uses_cls_time_in_force(self):
        """LOC 주문은 timeInForce=CLS로 변환되어야 합니다."""
        calls = {}

        def _capture_post(path, token, json_body=None, extra_headers=None):
            calls["body"] = json_body
            return _make_response({"result": {"orderId": "mock_loc_001"}})

        self._mock_session.post.side_effect = _capture_post
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NASDAQ", "BUY", 10, 50.0, "LOC")

        assert result is not None
        assert calls["body"]["orderType"] == "LIMIT"
        assert calls["body"]["timeInForce"] == "CLS"

    def test_place_order_sell_moc_converts_to_loc_0_01(self):
        """SELL MOC는 토스 미지원 → LOC($0.01)로 변환되어 종가 체결되어야 합니다."""
        calls = {}

        def _capture_post(path, token, json_body=None, extra_headers=None):
            calls["body"] = json_body
            return _make_response({"result": {"orderId": "mock_moc_001"}})

        self._mock_session.post.side_effect = _capture_post
        broker = self._create_broker()
        result = broker.place_order("SOXL", "NYSE", "SELL", 10, 52.0, "MOC")

        assert result is not None
        assert calls["body"]["side"] == "SELL"
        assert calls["body"]["orderType"] == "LIMIT"
        assert calls["body"]["timeInForce"] == "CLS"
        assert calls["body"]["price"] == 0.01

    def test_place_order_buy_moc_raises(self):
        """BUY MOC는 변환하지 않고 지원하지 않는 유형으로 거부해야 합니다."""
        broker = self._create_broker()
        with pytest.raises(OrderError, match="지원하지 않는 주문 유형"):
            broker.place_order("TQQQ", "NASDAQ", "BUY", 10, 52.0, "MOC")

    def test_place_order_invalid_type_raises(self):
        """지원하지 않는 주문 유형은 OrderError가 발생해야 합니다."""
        broker = self._create_broker()
        with pytest.raises(OrderError, match="지원하지 않는 주문 유형"):
            broker.place_order("TQQQ", "NASDAQ", "BUY", 10, 50.0, "INVALID")

    # ── 토스 거래소 코드 매핑 ────────────────────────────────────────
    # 토스는 미국 주식 거래소 이름을 그대로 사용

    def test_exchange_code_nas_to_nasdaq(self):
        broker = self._create_broker()
        assert broker.exchange_code("NAS") == "NASDAQ"

    def test_exchange_code_nys_to_nyse(self):
        broker = self._create_broker()
        assert broker.exchange_code("NYS") == "NYSE"

    def test_exchange_code_ams_to_amex(self):
        broker = self._create_broker()
        assert broker.exchange_code("AMS") == "AMEX"

    # ── Base 계약 테스트 오버라이드 ────────────────────────────────────

    def test_place_order_returns_orderresult_or_none(self):
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NASDAQ", "BUY", 1, 50.0, "LIMIT")
        if result is not None:
            assert isinstance(result, OrderResult)
            assert isinstance(result.order_id, str)
            assert isinstance(result.order_time, str)
            assert isinstance(result.is_reservation, bool)


# ═══════════════════════════════════════════════════════════════════════
# NHPlugBroker 계약 테스트
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.nhplug
@pytest.mark.usefixtures("_patch_nhplug_dependencies")
class TestNHPlugBrokerContract(BrokerContractTest):
    """
    NHPlugBroker 인터페이스 계약 테스트.

    NHPlugSession.request를 mock하여 실제 API 호출 없이
    모든 Broker 계약을 검증합니다.
    """

    @pytest.fixture(autouse=True)
    def _patch_nhplug_dependencies(self, request):
        """
        NHPlugBroker가 의존하는 모듈들을 patch합니다.

        - NHPlugSession → mock 인스턴스 반환
        - get_access_token → 가짜 토큰 반환
        - is_us_trading_day → True
        - get_kst_now → 고정 시각
        - config.BROKER_CONFIG, config.BROKER_MODE, config.HTTP_TIMEOUT → 가짜 값
        """
        fixed_kst = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

        target = "broker.nhplug.adapter"
        patches = [
            patch(f"{target}.NHPlugSession"),
            patch(f"{target}.get_access_token", return_value="mock_token"),
            patch(f"{target}.is_us_trading_day", return_value=True),
            patch(f"{target}.get_kst_now", return_value=fixed_kst),
            patch("config.BROKER_CONFIG", {
                "app_key": "test",
                "app_secret": "test",
                "account_no": "12345678",
                "acct_type": "01",
                "domain": "https://api.nhplug.com:8443",
                "live_domain": "https://api.nhplug.com:8443",
            }),
            patch("config.BROKER_MODE", "real"),
            patch("config.HTTP_TIMEOUT", (10, 30)),
        ]

        for p in patches:
            p.start()
            request.addfinalizer(p.stop)

        # NHPlugSession mock: 인스턴스 설정
        mock_session_cls = sys.modules[f"{target}"].NHPlugSession
        mock_session_instance = MagicMock()
        mock_session_cls.return_value = mock_session_instance
        self._mock_session = mock_session_instance

        # 기본 응답: 모든 API가 기본적으로 성공하는 응답
        self._setup_default_responses(mock_session_instance)

    def _setup_default_responses(self, mock_session):
        """
        mock NHPlugSession.request에 경로 기반 기본 성공 응답을 설정합니다.

        NHPLUG는 모든 TR이 POST이며 경로로 API를 구분하므로
        side_effect에서 path를 기준으로 라우팅합니다.
        """
        def _side_effect(method, path, token, json_body=None, extra_headers=None, domain=None):
            if "quote/v1/current" in path:
                return _make_response({
                    "Output_0": {"trdprc": "52.00", "open_prc": "50.00"},
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            elif "inquiry/v1/balance" in path:
                return _make_response({
                    "Output_1": [],
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            elif "inquiry/v1/buyableAmount" in path:
                return _make_response({
                    "Output_0": {"orr_pbl_amt": "5000.00"},
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            elif "inquiry/v1/dailyTransaction" in path:
                return _make_response({
                    "Output_0": [],
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            elif "quote/v1/period" in path:
                return _make_response({
                    "Output_1": [],
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            elif "order/v1/buy" in path or "order/v1/sell" in path:
                return _make_response({
                    "Output_0": {"orr_no": "202606150001"},
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            else:
                return _make_response({
                    "Output_0": {},
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })

        mock_session.request.side_effect = _side_effect

    def _create_broker(self):
        """mock이 주입된 NHPlugBroker 인스턴스를 반환."""
        from broker.nhplug.adapter import NHPlugBroker
        broker = NHPlugBroker()
        # rate-limit 대기 시간을 0으로 설정해 테스트 속도를 보장합니다.
        broker._rate_limit_wait = 0
        return broker

    def _set_mock_response(self, json_data, status_code=200, headers=None):
        """side_effect를 제거하고 return_value로 단일 응답을 설정합니다."""
        self._mock_session.request.side_effect = None
        self._mock_session.request.return_value = _make_response(
            json_data, status_code, headers
        )

    # ── NHPLUG 전용 테스트 ────────────────────────────────────────────

    def test_get_stock_price_values(self):
        """반환된 StockPrice의 open/last 값이 Output_0 응답과 일치해야 합니다."""
        self._set_mock_response({
            "Output_0": {"open_prc": "50.00", "trdprc": "52.50"},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert result.open == 50.0
        assert result.last == 52.50

    def test_get_stock_price_lowercase_output(self):
        """소문자 output 키도 방어적으로 처리되어야 합니다."""
        self._set_mock_response({
            "output": {"open_prc": "50.00", "trdprc": "52.50"},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_stock_price("TQQQ", "NAS")
        assert result.open == 50.0
        assert result.last == 52.50

    def test_get_stock_price_raises_brokererror_on_message_envelope(self):
        """message 봉투(msg_code != 00000) 응답에서 BrokerError가 발생해야 합니다."""
        self._set_mock_response({
            "message": {"msg_code": "E0001", "usr_msg": "현재가 조회 실패"},
        })
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="현재가 조회 실패"):
            broker.get_stock_price("TQQQ", "NAS")

    def test_get_stock_quotation_returns_tradable(self):
        """NHPLUG 현재가 응답에서 tradable=True(기본값)와 last가 반환되어야 합니다."""
        self._set_mock_response({
            "Output_0": {"trdprc": "52.00"},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is True
        assert result.last == 52.0

    def test_get_balance_returns_none_when_no_position(self):
        """잔고가 없으면 None을 반환해야 합니다."""
        self._set_mock_response({
            "Output_1": [],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is None

    def test_get_balance_with_position(self):
        """잔고가 있으면 Balance dataclass를 반환해야 합니다."""
        self._set_mock_response({
            "Output_1": [
                {"iem_cd": "TQQQ", "cns_bse_bnc_qty": "10", "fc_phs_uit_pr": "48.50"},
            ],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_balance("TQQQ", "NAS")
        assert result is not None
        assert result.quantity == 10
        assert result.avg_price == 48.50

    def test_get_purchase_amount_returns_amount(self):
        """매수가능금액이 PurchaseAmount로 반환되어야 합니다."""
        self._set_mock_response({
            "Output_0": {"orr_pbl_amt": "5000.00"},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert isinstance(result, PurchaseAmount)
        assert result.orderable_cash == 5000.0

    def test_get_order_history_with_data(self):
        """get_order_history가 각 항목에 표준 필드를 포함해야 합니다."""
        self._set_mock_response({
            "Output_0": [
                {
                    "ord_dt": "20260615",
                    "ord_tmd": "093000",
                    "prdt_name": "TQQQ",
                    "sll_buy_dvsn_cd_name": "매수",
                    "ft_ord_qty": "5",
                    "ft_ccld_qty": "5",
                    "ft_ccld_unpr3": "54.00",
                    "ft_ccld_amt3": "270.00",
                    "nccs_qty": "0",
                    "prcs_stat_name": "체결완료",
                    "tr_mket_name": "NASDAQ",
                    "tr_crcy_cd": "USD",
                    "odno": "36267",
                    "ovrs_excg_cd": "NASD",
                }
            ],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        item = history[0]

        standard_fields = {
            "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
            "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
            "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
            "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
        }
        missing = standard_fields - set(item.keys())
        assert not missing, f"표준 필드 누락: {missing}"

        assert item["ord_dt"] == "20260615"
        assert item["sll_buy_dvsn_cd_name"] == "매수"
        assert item["ft_ccld_qty"] == "5"
        assert item["odno"] == "36267"

    def test_get_order_history_missing_qty_field_omits_ft_ord_qty(self):
        """GSB10030 원주문 수량(orr_qty)이 없으면 "0"을 합성하지 않습니다."""
        self._set_mock_response({
            "Output_0": [
                {
                    "orr_dt": "20260615",
                    "orr_no": "36267",
                    "iem_cd": "TQQQ",
                    "sby_dit_nm": "2",
                    "cns_qty": "1",
                    "cns_pr": "54.00",
                    "ny_cns_orr_qty": "0",
                    "tr_mket_name": "NASDAQ",
                }
            ],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")

        assert len(history) == 1
        assert "ft_ord_qty" not in history[0]
        assert history[0]["ft_ccld_qty"] == "1"

    def test_normalize_order_item_missing_qty_field_omits_ft_ord_qty(self):
        """KIS 호환 필드 경로에서도 수량 필드가 없으면 키를 생략합니다."""
        broker = self._create_broker()
        row = broker._normalize_order_item({
            "ord_dt": "20260615",
            "ord_tmd": "093000",
            "prdt_name": "TQQQ",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
        })

        assert "ft_ord_qty" not in row
        assert row["ft_ccld_qty"] == "1"

    def test_get_order_history_empty(self):
        """체결 내역이 없으면 빈 리스트를 반환해야 합니다."""
        self._set_mock_response({
            "Output_0": [],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        history = broker.get_order_history("TQQQ", "NAS")
        assert history == []

    def test_place_order_returns_orderresult(self):
        """성공적인 주문은 OrderResult(order_id, order_time, is_reservation=False)를 반환."""
        self._set_mock_response({
            "Output_0": {"orr_no": "202606150001"},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        assert result is not None
        assert isinstance(result, OrderResult)
        assert result.order_id == "202606150001"
        assert result.is_reservation is False

    def test_place_order_message_envelope_raises_order_not_accepted(self):
        """message 봉투 거부 응답은 OrderNotAcceptedError로 래핑되어야 합니다."""
        self._set_mock_response({
            "message": {"msg_code": "E0001", "usr_msg": "주문 거부"},
        })
        broker = self._create_broker()
        with pytest.raises(OrderNotAcceptedError, match="주문 거부"):
            broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

    def test_place_order_rsp_cd_error_raises_order_not_accepted(self):
        """rsp_cd 비성공 코드(예: 14580 모의투자 장종료)는 OrderNotAcceptedError로 처리되어야 합니다."""
        self._set_mock_response({
            "rsp_cd": "14580",
            "rsp_msg": "모의투자 장종료 입니다.",
        })
        broker = self._create_broker()
        with pytest.raises(OrderNotAcceptedError, match="모의투자 장종료"):
            broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

    def test_get_stock_price_rsp_cd_error_raises_brokererror(self):
        """조회 API에서 rsp_cd 비성공 코드 응답은 BrokerError로 처리되어야 합니다."""
        self._set_mock_response({
            "rsp_cd": "14580",
            "rsp_msg": "모의투자 장종료 입니다.",
        })
        broker = self._create_broker()
        with pytest.raises(BrokerError, match="모의투자 장종료"):
            broker.get_stock_price("TQQQ", "NAS")

    def test_get_purchase_amount_rsp_cd_xa102_is_success(self):
        """rsp_cd=XA102(모의투자 조회 완료)는 성공으로 처리되어야 합니다."""
        self._set_mock_response({
            "rsp_cd": "XA102",
            "rsp_msg": "모의투자 조회가 완료되었습니다",
            "Output_0": {"orr_pbl_amt": "5000.00"},
        })
        broker = self._create_broker()
        result = broker.get_purchase_amount("TQQQ", "NAS")
        assert isinstance(result, PurchaseAmount)
        assert result.orderable_cash == 5000.0

    def test_place_order_loc_converts_to_limit_in_demo(self):
        """데모 모드에서 LOC 주문은 LIMIT(00)으로 변환되어 전송되어야 합니다."""
        calls = {}

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls["body"] = json_body
            return _make_response({
                "Output_0": {"orr_no": "demo_loc_001"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LOC")

        assert result is not None
        assert calls["body"]["tr_cd"] == "GSO10010"
        assert calls["body"]["Input_0"]["ahi_nmn_pr_tp_cd"] == "00"
        assert calls["body"]["Input_0"]["fc_sec_trd_nat_cd"] == "200"
        assert calls["body"]["Input_0"]["iem_cd"] == "TQQQ"
        # orr_qty는 integer(int64), fc_orr_uit_pr는 number(double) — 문자열 금지
        assert calls["body"]["Input_0"]["orr_qty"] == 1
        assert isinstance(calls["body"]["Input_0"]["orr_qty"], int)
        assert calls["body"]["Input_0"]["fc_orr_uit_pr"] == 50.0
        assert isinstance(calls["body"]["Input_0"]["fc_orr_uit_pr"], float)
        assert calls["body"]["Input_0"]["wtm_cur_knd_cd"] == "1"

    def test_place_order_moc_keeps_14_in_real(self):
        """실전 모드에서 MOC 주문은 MOC(14)로 유지되어 전송되어야 합니다."""
        calls = {}

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls["body"] = json_body
            return _make_response({
                "Output_0": {"orr_no": "real_moc_001"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "MOC")

        assert result is not None
        assert calls["body"]["tr_cd"] == "GSO10010"
        assert calls["body"]["Input_0"]["ahi_nmn_pr_tp_cd"] == "14"
        assert calls["body"]["Input_0"]["wtm_cur_knd_cd"] == "1"

    def test_place_order_sell_has_no_wtm_cur_knd_cd(self):
        """매도 주문에는 wtm_cur_knd_cd가 포함되지 않아야 합니다 (매수 전용)."""
        calls = {}

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls["body"] = json_body
            return _make_response({
                "Output_0": {"orr_no": "real_sell_001"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "200", "SELL", 1, 50.0, "LIMIT")

        assert result is not None
        assert calls["body"]["tr_cd"] == "GSO10020"
        assert calls["body"]["Input_0"]["ahi_nmn_pr_tp_cd"] == "00"
        assert "wtm_cur_knd_cd" not in calls["body"]["Input_0"]

    # ── 계좌번호 자동 조회 (acct_no) ──────────────────────────────────

    def test_get_account_no_uses_configured_value(self):
        """NHPLUG_ACCT_NO 설정 시 설정값을 사용하고 acctinfo를 호출하지 않아야 합니다."""
        calls = []

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls.append({"path": path, "body": json_body})
            if "order/v1/buy" in path:
                return _make_response({
                    "Output_0": {"orr_no": "cfg_acct_001"},
                    "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
                })
            return _make_response({
                "Output_0": {},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture
        broker = self._create_broker()  # fixture가 account_no="12345678" 설정
        result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        assert result is not None
        # acctinfo를 호출하지 않고 설정값을 그대로 사용해야 합니다.
        assert "/n2/acctinfo" not in [c["path"] for c in calls]
        assert calls[-1]["body"]["Input_0"]["act_no"] == "12345678"

    def test_get_account_no_auto_selects_from_acctinfo(self):
        """NHPLUG_ACCT_NO 미설정 시 BrokerError가 발생하고 acctinfo를 호출하지 않아야 합니다."""
        calls = []

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls.append({"path": path, "body": json_body})
            return _make_response({
                "Output_0": {"orr_no": "should_not_succeed"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            with pytest.raises(BrokerError, match="NHPLUG_ACCT_NO"):
                broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        # 자동 선택 제거 — acctinfo나 주문 API를 호출하지 않아야 합니다.
        assert "/n2/acctinfo" not in [c["path"] for c in calls]
        assert not any("order/v1/buy" in c["path"] for c in calls)

    def test_get_account_no_real_prefers_01(self):
        """실전 모드에서도 NHPLUG_ACCT_NO 미설정 시 BrokerError가 발생해야 합니다."""
        calls = []

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls.append({"path": path, "body": json_body})
            return _make_response({
                "Output_0": {"orr_no": "should_not_succeed"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _capture

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "real"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "",
            "acct_type": "01",
            "domain": "https://api.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            with pytest.raises(BrokerError, match="NHPLUG_ACCT_NO"):
                broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        assert "/n2/acctinfo" not in [c["path"] for c in calls]

    def test_get_account_no_raises_when_no_matching_account(self):
        """NHPLUG_ACCT_NO 미설정 시 BrokerError가 발생해야 합니다 (계좌 자동 조회 없음)."""
        calls = []

        def _capture(method, path, token, json_body=None, extra_headers=None, domain=None):
            calls.append({"path": path, "body": json_body})
            return _make_response({"Output_0": {}})

        self._mock_session.request.side_effect = _capture

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            with pytest.raises(BrokerError, match="NHPLUG_ACCT_NO"):
                broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        assert "/n2/acctinfo" not in [c["path"] for c in calls]

    # ── 토큰 발급 도메인 (항상 운영 도메인) ────────────────────────────

    def test_get_token_uses_live_domain_in_demo(self):
        """데모 모드에서도 토큰 발급은 운영 도메인으로 요청되어야 합니다."""
        captured = {}

        def _fake_get_access_token(domain, app_key, app_secret, timeout, session):
            captured["domain"] = domain
            return "mock_token"

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
            "live_domain": "https://api.nhplug.com:8443",
        }), patch(
            "broker.nhplug.adapter.get_access_token",
            side_effect=_fake_get_access_token,
        ):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            broker.get_stock_price("TQQQ", "NAS")

        # 모의투자 도메인(moapi)이 아닌 운영 도메인으로 토큰을 요청해야 합니다.
        assert captured["domain"] == "https://api.nhplug.com:8443"

    def test_get_access_token_uses_live_domain_url(self):
        """get_access_token은 모의 도메인을 전달받아도 운영 도메인 URL로 토큰을 요청해야 합니다."""
        from broker.nhplug import auth as nhplug_auth

        # 전역 토큰 캐시 초기화 (이전 테스트의 캐시 영향 방지)
        nhplug_auth._cached_token = None
        nhplug_auth._token_expires_at = 0.0

        captured = {}

        def _fake_post(url, **kwargs):
            captured["url"] = url
            resp = MagicMock()
            resp.status_code = 200
            resp.ok = True
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "access_token": "mock_token",
                "expires_in": 86400,
            }
            return resp

        mock_session = MagicMock()
        mock_session.post.side_effect = _fake_post

        token = nhplug_auth.get_access_token(
            domain="https://moapi.nhplug.com:8443",
            app_key="test",
            app_secret="test",
            timeout=(10, 30),
            session=mock_session,
        )

        assert token == "mock_token"
        # 모의투자 도메인을 전달해도 운영 도메인 URL로 발급 요청이 가야 합니다.
        assert captured["url"] == "https://api.nhplug.com:8443/oauth2/token"

    # ── 시세 API 운영 서버 라우팅 (모의 서버는 시세 미지원) ────────────

    def test_quote_current_uses_live_domain_in_demo(self):
        """데모 모드에서도 시세(current)는 운영 도메인으로 호출되어야 합니다."""
        captured = {}

        def _side_effect(method, path, token, json_body=None, extra_headers=None, domain=None):
            captured["path"] = path
            captured["domain"] = domain
            return _make_response({
                "Output_0": {"trdprc": "52.00", "open_prc": "50.00"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _side_effect

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "12345678",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
            "live_domain": "https://api.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            broker.get_stock_price("TQQQ", "NAS")

        assert captured["path"] == "/gbstock/quote/v1/current"
        assert captured["domain"] == "https://api.nhplug.com:8443"

    def test_quote_period_uses_live_domain_in_demo(self):
        """데모 모드에서도 기간별시세(period)는 운영 도메인으로 호출되어야 합니다."""
        captured = {}

        def _side_effect(method, path, token, json_body=None, extra_headers=None, domain=None):
            captured["path"] = path
            captured["domain"] = domain
            return _make_response({
                "Output_1": [{"close_prc": "52.00"}],
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _side_effect

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "12345678",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
            "live_domain": "https://api.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            broker.get_daily_closes("TQQQ", "NAS", days=5)

        assert captured["path"] == "/gbstock/quote/v1/period"
        assert captured["domain"] == "https://api.nhplug.com:8443"

    def test_inquiry_keeps_mode_domain_in_demo(self):
        """데모 모드에서 조회(balance)는 모의 도메인(domain 미지정)으로 호출되어야 합니다."""
        captured = {}

        def _side_effect(method, path, token, json_body=None, extra_headers=None, domain=None):
            captured["path"] = path
            captured["domain"] = domain
            return _make_response({
                "Output_1": [],
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _side_effect

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "12345678",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
            "live_domain": "https://api.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            broker.get_balance("TQQQ", "NAS")

        assert captured["path"] == "/gbstock/inquiry/v1/balance"
        # domain 미지정(None) → 세션이 모드별 도메인(moapi)을 사용합니다.
        assert captured["domain"] is None

    def test_order_keeps_mode_domain_in_demo(self):
        """데모 모드에서 주문(buy)은 모의 도메인(domain 미지정)으로 호출되어야 합니다."""
        captured = {}

        def _side_effect(method, path, token, json_body=None, extra_headers=None, domain=None):
            captured["path"] = path
            captured["domain"] = domain
            return _make_response({
                "Output_0": {"orr_no": "demo_order_001"},
                "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
            })

        self._mock_session.request.side_effect = _side_effect

        from broker.nhplug.adapter import NHPlugBroker
        with patch("config.BROKER_MODE", "demo"), patch("config.BROKER_CONFIG", {
            "app_key": "test",
            "app_secret": "test",
            "account_no": "12345678",
            "acct_type": "03",
            "domain": "https://moapi.nhplug.com:8443",
            "live_domain": "https://api.nhplug.com:8443",
        }):
            broker = NHPlugBroker()
            broker._rate_limit_wait = 0
            broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")

        assert captured["path"] == "/gbstock/order/v1/buy"
        # domain 미지정(None) → 세션이 모드별 도메인(moapi)을 사용합니다.
        assert captured["domain"] is None

    def test_session_request_uses_domain_param(self):
        """NHPlugSession.request는 domain 파라미터로 URL 베이스를 결정해야 합니다."""
        from broker.nhplug.session import NHPlugSession

        session = NHPlugSession(
            "https://moapi.nhplug.com:8443",
            app_key="test",
            app_secret="test",
            timeout=(10, 30),
        )
        # 내부 requests.Session을 mock (실제 네트워크 호출 방지)
        session._session = MagicMock()
        session._session.request.return_value = _make_response({
            "Output_0": {},
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })

        # domain 지정 → 운영 도메인 URL
        session.request(
            "POST", "/gbstock/quote/v1/current", "mock_token",
            json_body={}, domain="https://api.nhplug.com:8443",
        )
        url = session._session.request.call_args[0][1]
        assert url == "https://api.nhplug.com:8443/gbstock/quote/v1/current"

        # domain 미지정 → 모드별 도메인(moapi) URL
        session.request(
            "POST", "/gbstock/inquiry/v1/balance", "mock_token", json_body={},
        )
        url = session._session.request.call_args[0][1]
        assert url == "https://moapi.nhplug.com:8443/gbstock/inquiry/v1/balance"

    def test_place_order_network_timeout_raises_ordererror(self):
        """네트워크 타임아웃은 OrderError(접수 불확실)로 래핑되어야 합니다."""
        self._mock_session.request.side_effect = requests.exceptions.Timeout(
            "connection timed out"
        )
        broker = self._create_broker()
        with pytest.raises(OrderError) as excinfo:
            broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")
        # OrderNotAcceptedError(미접수 확정)가 아닌 일반 OrderError여야 합니다.
        assert not isinstance(excinfo.value, OrderNotAcceptedError)

    def test_get_daily_closes_returns_oldest_first(self):
        """get_daily_closes는 오래된 순으로 정렬된 종가 리스트를 반환해야 합니다."""
        self._set_mock_response({
            "Output_1": [
                {"close_prc": "55.00"},
                {"close_prc": "54.00"},
                {"close_prc": "53.00"},
                {"close_prc": "52.00"},
                {"close_prc": "51.00"},
            ],
            "message": {"msg_code": "", "usr_msg": "", "dvlp_msg": ""},
        })
        broker = self._create_broker()
        closes = broker.get_daily_closes("TQQQ", "NAS", days=5)
        assert closes == [51.0, 52.0, 53.0, 54.0, 55.0]

    # ── NHPLUG 거래소 코드 매핑 (NHPLUG 전용) ─────────────────────────
    # NHPLUG는 미국 주식 모두 fc_sec_trd_nat_cd="200"을 사용합니다 (NAS/NYS/AMS 구분 없음)

    def test_exchange_code_nas_to_200(self):
        broker = self._create_broker()
        assert broker.exchange_code("NAS") == "200"

    def test_exchange_code_nys_to_200(self):
        broker = self._create_broker()
        assert broker.exchange_code("NYS") == "200"

    def test_exchange_code_ams_to_200(self):
        broker = self._create_broker()
        assert broker.exchange_code("AMS") == "200"

    # ── Base 계약 테스트 오버라이드 ────────────────────────────────────
    # NHPLUG place_order는 broker.exchange_code()로 변환된 API 국가코드("200")를 받습니다.

    def test_place_order_returns_orderresult_or_none(self):
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "200", "BUY", 1, 50.0, "LIMIT")
        if result is not None:
            assert isinstance(result, OrderResult)
            assert isinstance(result.order_id, str)
            assert isinstance(result.order_time, str)
            assert isinstance(result.is_reservation, bool)
