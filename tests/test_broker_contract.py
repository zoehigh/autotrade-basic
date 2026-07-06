"""
Broker 계약 테스트 — 모든 Broker 구현체가 인터페이스 계약을 준수하는지 검증.

각 브로커(KIS, Kiwoom)는 Mock HTTP 응답을 주입받아:
- 9개 메서드 반환 타입 (dataclass) 검증
- get_order_history 표준 필드 존재 검증
- 예외 처리 검증
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from broker.base import (
    Broker,
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    BrokerError,
    OrderError,
)


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
        assert result.order_time == "103000"
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

        def _side_effect(tr_id, body, token):
            if tr_id == TR_PRICE:  # usa20100 — 현재가
                return _make_response({
                    "return_code": 0, "output": {"open": "50.00", "last": "52.00"},
                })
            elif tr_id == TR_ORDERBOOK:  # usa20101 — 10호가
                return _make_response({
                    "return_code": 0, "output": {"ordy": "Y", "last": "52.00"},
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
            "output": {"open": "50.00", "last": "52.50"},
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
        """ordy=Y 응답에서 tradable=True가 반환되어야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": {"ordy": "Y", "last": "52.00"},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is True
        assert result.last == 52.0

    def test_get_stock_quotation_not_tradable(self):
        """ordy=N 응답에서 tradable=False가 반환되어야 합니다."""
        self._set_mock_response({
            "return_code": 0,
            "output": {"ordy": "N", "last": "0"},
        })
        broker = self._create_broker()
        result = broker.get_stock_quotation("TQQQ", "NAS")
        assert result.tradable is False

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
                    "hold_qty": "10",
                    "avg_price": "48.50",
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
            "output": {"ord_psbl_cash": "5000.00"},
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
        self._set_mock_response({
            "return_code": 0,
            "output": {"ODNO": "202606150001", "ORD_TMD": "103000"},
        })
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NAS", "BUY", 1, 50.0, "LIMIT")

        assert result is not None
        assert isinstance(result, OrderResult)
        assert result.order_id == "202606150001"
        assert result.order_time == "103000"
        assert result.is_reservation is False

    # ── Base 계약 테스트 오버라이드 ────────────────────────────────────
    # KiwoomBroker.place_order는 exchange 인자로 사용자 코드(NAS/NYS/AMS)를 받고
    # 내부에서 get_api_exchange_code()로 변환하므로 "NASD" 대신 "NAS"를 사용합니다.

    def test_place_order_returns_orderresult_or_none(self):
        broker = self._create_broker()
        result = broker.place_order("TQQQ", "NAS", "BUY", 1, 50.0, "LIMIT")
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
