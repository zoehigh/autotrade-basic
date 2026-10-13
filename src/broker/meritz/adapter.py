"""
MeritzBroker — 메리츠증권 Broker 구현체.

메리츠증권 Open API (REST)를 통해 미국 주식 자동매매를 실행합니다.
- 실전 전용: 모의투자/demo 계좌가 없어 BROKER_MODE=real 만 허용합니다
- OAuth2 Client Credentials 인증 (scope=login, 계좌번호 파라미터 없음)
- 모든 요청에 authorization(Bearer) + mac_address 헤더
- 응답 판정은 HTTP 상태가 아니라 **본문 rsp_cd** 기준입니다
- 주문번호는 날짜 정보가 없으므로 `YYYYMMDD:제로패딩주문번호` 합성 ID로 관리

사용법:
    broker = MeritzBroker()
    price = broker.get_stock_price("TQQQ", "NAS")
    result = broker.place_order("TQQQ", "OQ", "BUY", 10, 50.0, "LOC")

DRY 모드는 DryBroker 래퍼로 처리 — MeritzBroker 자체는 항상 LIVE로 동작합니다.

엔드포인트 (메리츠 Open API, openapi.json 2026-10-02 기준):
    시세    GET  /market/v1/overseas/prices        (mrkt_div_code/mrkt_cls_code/iscd/dely_rltm_cls_code)
    일봉    GET  /market/v1/overseas/candles/days   (+ mod_stpr_cls_code=0, 최신 40건)
    보유    GET  /accounts/v1/me/overseas/holdings  (rsp 5766, ovrs_stck_symb_iscd 매칭)
    매수가능 GET  /trading/v1/overseas/orders/estimate (rsp 5766, crcd=USD 행)
    이력    GET  /trading/v1/overseas/orders/history (oder_sta_date/oder_fin_date/cntg_cls_code, tr_cont_key 페이지)
    당일    GET  /trading/v1/overseas/orders/today   (종목 파라미터 없음 → iscd 클라이언트 필터)
    매수    POST /trading/v1/overseas/orders/buy     (응답 data.oder_no/warn_cls_code/warn_msg)
    매도    POST /trading/v1/overseas/orders/sell
    취소    POST /trading/v1/overseas/orders/cancel  (oder_date/orgl_oder_no/oder_cls_code/odqt)
"""
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from broker.base import (
    Broker,
    StockPrice,
    StockQuotation,
    Balance,
    PurchaseAmount,
    OrderResult,
    BrokerError,
    AuthError,
    OrderError,
    OrderNotAcceptedError,
)
from broker.market_utils import get_kst_now, is_us_trading_day, normalize_order_price
from broker.meritz.auth import (
    get_access_token,
    invalidate_token,
    is_token_error_code,
)
from broker.meritz.session import MeritzSession
from broker.meritz.exchange import get_order_vendor_code, get_quote_exchange_code

KST_TZ = ZoneInfo("Asia/Seoul")
ET_TZ = ZoneInfo("America/New_York")   # 일봉 "당일 미확정 봉" 판정용

# ── API 경로 ──
PATH_PRICE = "/market/v1/overseas/prices"
PATH_DAILY_CANDLES = "/market/v1/overseas/candles/days"
PATH_HOLDINGS = "/accounts/v1/me/overseas/holdings"
PATH_ESTIMATE = "/trading/v1/overseas/orders/estimate"
PATH_ORDER_HISTORY = "/trading/v1/overseas/orders/history"
PATH_ORDER_TODAY = "/trading/v1/overseas/orders/today"
PATH_ORDER_BUY = "/trading/v1/overseas/orders/buy"
PATH_ORDER_SELL = "/trading/v1/overseas/orders/sell"
PATH_ORDER_CANCEL = "/trading/v1/overseas/orders/cancel"

# ── 응답 판정 코드 ──
# 성공 코드(rsp_cd)는 API 계열별로 상이합니다.
_MARKET_SUCCESS_RSP_CDS = {"0000"}                       # 시세/일봉
_ACCOUNT_SUCCESS_RSP_CDS = {"5766", "0000"}              # 보유 / 주문가능금액 (공식 5766)
_TRADE_QUERY_SUCCESS_RSP_CDS = {"5766", "5762", "0000", "0001"}  # 이력(5762=다음페이지)/당일
_CANCEL_SUCCESS_RSP_CDS = {"0001", "0000"}               # 주문 취소 접수
_ORDER_ACCEPT_RSP_CD = "0001"                            # 주문 접수 성공
_NO_DATA_RSP_CD = "5820"                                 # 조회결과 없음
_QUERY_RETRY_RSP_CD = "EGW00200"                         # 조회 API 전용 재시도 코드
_QUERY_RETRY_LIMIT = 2
_MAX_HISTORY_PAGES = 10                                  # 이력 페이지 순회 상한 (무한 루프 방지)

# ── 주문 유형 → 주문구분(oder_cls_code, openapi.json 2026-10-02) ──
# 01: 지정가 / 05: 시장가 / 31: LOC... 등 (아래 공식 매핑 사용)
_ORDER_TYPE_MAP = {
    "LIMIT": "01",
    "MKT": "05",
    "LOO": "31",
    "LOC": "32",
    "MOO": "33",
    "MOC": "34",
}
# 시장가 계열(05/33/34/37/38)은 단가를 0으로 전송합니다 (37/38은 봇 미사용)
_MARKET_ORDER_TYPES = {"MKT", "MOC", "MOO"}

# ── aspr_type_code → 주문구분 역매핑 (이력에서 취소 메타 복원용) ──
_ASPR_TO_ODER_CLS_CODE = {
    "1": "05",   # 시장가
    "2": "01",   # 지정가
    "4": "31",   # 장시작 LOO
    "5": "32",   # 장시작 LOC
    "6": "33",   # 장마감 MOO
    "7": "34",   # 장마감 MOC
}

# ── 주문 경고(warn_cls_code) 처리 ──
_WARN_NONE = "0"
_WARN_CONFIRMABLE = {"1", "3"}   # 재확인 전송(1회) 가능
_WARN_NO_RESEND = {"2"}          # 재전송 금지 (중복 주문 위험)
_ENV_CONFIRM_WARNINGS = "MERITZ_CONFIRM_WARNINGS"

# ── 합성 주문번호 ──
_ODNO_SEPARATOR = ":"
_ORDER_NO_PAD_WIDTH = 10
_CANCEL_STATUS_NAME = "취소"
_REJECT_STATUS_NAME = "거부"
_CANCEL_REQUIRED_FIELDS = ("oder_date", "orgl_oder_no", "oder_cls_code", "odqt")


class _ResponseParseError(BrokerError):
    """응답 본문이 JSON이 아니어서 파싱에 실패한 경우 (접수 여부 불확실)."""


class MeritzBroker(Broker):
    """
    메리츠증권 미국주식 REST API Broker 구현체.

    config.py의 BROKER_CONFIG["meritz"]에서 설정을 읽습니다.
    모의투자 계좌가 없어 BROKER_MODE != "real"이면 생성자에서 즉시 실패합니다.
    """

    def __init__(self):
        from config import BROKER_CONFIG, BROKER_MODE, HTTP_TIMEOUT, MERITZ_DEFAULT_BASE_URL

        if BROKER_MODE != "real":
            raise ValueError(
                "메리츠증권은 모의투자/demo를 제공하지 않습니다. "
                f"BROKER_MODE=real 로 설정한 뒤 사용하세요 (현재: {BROKER_MODE})."
            )

        self._base_url = (
            BROKER_CONFIG.get("base_url") or BROKER_CONFIG.get("domain") or ""
        ).strip() or MERITZ_DEFAULT_BASE_URL

        self._app_key = BROKER_CONFIG.get("app_key", "")
        self._app_secret = BROKER_CONFIG.get("app_secret", "")
        if not self._app_key or not self._app_secret:
            raise ValueError(
                "환경변수 MERITZ_APP_KEY와 MERITZ_APP_SECRET이 설정되어야 합니다. "
                "(.env 파일을 확인해주세요.)"
            )

        self._session = MeritzSession(
            self._base_url,
            mac_address=BROKER_CONFIG.get("mac_address", ""),
            timeout=HTTP_TIMEOUT,
        )

        # 호출 간격 (메리츠 rate-limit은 문서상 공개되지 않아 보수적으로 적용)
        self._rate_limit_wait = 0.1

        # 취소 API에 필요한 제출 메타(oder_cls_code/수량) — 합성 주문번호 기준
        self._submitted_orders: dict[str, dict] = {}

    @property
    def name(self) -> str:
        """증권사 식별자"""
        return "meritz"

    # ═══════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (auth.get_access_token 캐싱 포함)."""
        try:
            return get_access_token(
                base_url=self._base_url,
                app_key=self._app_key,
                app_secret=self._app_secret,
                timeout=self._session.timeout,
            )
        except AuthError:
            raise
        except Exception as e:
            raise BrokerError(f"메리츠 토큰 획득 실패: {str(e)}")

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        """문자열/숫자 값을 float로 안전 변환합니다."""
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        """문자열/숫자 값을 int로 안전 변환합니다."""
        if value is None or value == "":
            return default
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _digits(value) -> str:
        """숫자만 남깁니다 (YYYYMMDDHHMMSS 형식 정리용)."""
        return re.sub(r"\D", "", str(value or ""))

    @staticmethod
    def _first_value(row: dict, keys: tuple):
        """여러 후보 필드 중 값이 있는 첫 번째 값을 반환합니다."""
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _extract_rows(body: dict) -> list[dict]:
        """응답 본문에서 목록 데이터를 추출합니다 (data > list/rows/items)."""
        data = body.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "rows", "items", "output"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _symbol_matches(iscd: str, symbol: str) -> bool:
        """
        iscd(접미사 포함, 예: TQQQ.OQ)가 심볼과 일치하는지 확인합니다 (prefix 매칭).

        접미사(.OQ/.NY/.AX)는 무시하되, 'TQQQ'가 'QQQ'와 같은 오매칭을 막기 위해
        심볼 바로 뒤 문자가 접미사 구분자(.) 또는 문자열 끝인지 확인합니다.
        """
        code = str(iscd or "").strip().upper()
        target = str(symbol or "").strip().upper()
        if not code or not target:
            return False
        if not code.startswith(target):
            return False
        rest = code[len(target):]
        return rest == "" or rest.startswith(".")

    @staticmethod
    def _next_page_key(body: dict) -> str:
        """
        주문이력 다음 페이지 키(tr_cont_key)를 **원문 그대로** 반환합니다.

        본문 최상위에 있을 수 있고, data 객체 안에 있을 수도 있습니다.
        없으면 빈 문자열 (마지막 페이지).
        """
        key = body.get("tr_cont_key")
        if key in (None, ""):
            data = body.get("data")
            if isinstance(data, dict):
                key = data.get("tr_cont_key")
        return str(key).strip() if key not in (None, "") else ""

    @staticmethod
    def _error_message(body: dict) -> str:
        """rsp_msg + rsp_sub_msg 을 합쳐 오류 메시지를 만듭니다."""
        rsp_msg = str(body.get("rsp_msg", "") or "")
        rsp_sub_msg = str(body.get("rsp_sub_msg", "") or "")
        if rsp_msg and rsp_sub_msg and rsp_sub_msg != rsp_msg:
            return f"{rsp_msg} ({rsp_sub_msg})"
        return rsp_msg or rsp_sub_msg or "알 수 없는 오류"

    def _warn_cls_code(self, body: dict) -> str:
        """
        주문/취소 응답의 warn_cls_code를 반환합니다.

        공식 스키마 기준 warn_cls_code는 **data 객체 안**에 있으며,
        일부 응답은 최상위에도 포함할 수 있어 양쪽을 모두 확인합니다.
        """
        data = body.get("data")
        if isinstance(data, dict):
            value = self._first_value(data, ("warn_cls_code",))
            if value not in (None, ""):
                return str(value).strip()
        value = body.get("warn_cls_code")
        if value in (None, ""):
            return _WARN_NONE
        return str(value).strip()

    def _check_rsp_cd(self, body: dict, success_codes: set, context: str) -> None:
        """
        본문 rsp_cd로 성공 여부를 판정합니다.

        - HTTP 상태코드는 보지 않습니다 (비즈니스 오류가 200으로 내려옵니다)
        - 5820(조회결과 없음)은 오류로 올리지 않고 data 유무로 판단합니다
        """
        rsp_cd = str(body.get("rsp_cd", "") or "")
        if rsp_cd in success_codes:
            return
        if rsp_cd == _NO_DATA_RSP_CD:
            # 조회결과 없음 → 빈 결과로 처리 (호출부가 None/[]로 변환)
            return
        raise BrokerError(f"메리츠 {context} 실패 [{rsp_cd}]: {self._error_message(body)}")

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        is_query: bool = True,
    ) -> dict:
        """
        메리츠 REST 공통 요청 래퍼 → 응답 본문 dict.

        재시도 정책:
        - EGW00121/EGW00123 (토큰 만료/무효): 토큰 1회 재발급 후 재전송 (주문 포함)
        - EGW00200: **조회 API만** 제한 재시도 (주문 POST는 재시도하지 않음)
        - 네트워크 타임아웃/비JSON: 재시도 없이 예외 (주문은 접수 여부 불확실)
        """
        token_reissued = False
        query_retry = 0

        while True:
            time.sleep(self._rate_limit_wait)
            token = self._get_token()
            if method.upper() == "GET":
                resp = self._session.get(path, token, params=params)
            else:
                resp = self._session.post(path, token, json_body=json_body)

            body = self._parse_response_body(resp, path)
            rsp_cd = str(body.get("rsp_cd", "") or "")

            # 토큰 만료/무효 → 1회 재발급 후 재전송
            if is_token_error_code(rsp_cd) and not token_reissued:
                token_reissued = True
                invalidate_token()
                print(f"[메리츠] 토큰 오류 [{rsp_cd}] — 토큰 재발급 후 1회 재시도합니다.")
                continue

            # 조회 API 전용 제한 재시도
            if is_query and rsp_cd == _QUERY_RETRY_RSP_CD and query_retry < _QUERY_RETRY_LIMIT:
                query_retry += 1
                print(
                    f"[메리츠] 조회 제한 응답 [{rsp_cd}] — "
                    f"{self._rate_limit_wait * query_retry:.1f}초 후 재시도 "
                    f"({query_retry}/{_QUERY_RETRY_LIMIT})"
                )
                time.sleep(self._rate_limit_wait * query_retry)
                continue

            return body

    def _parse_response_body(self, resp, path: str) -> dict:
        """응답 본문을 dict로 파싱합니다 (비JSON은 _ResponseParseError)."""
        try:
            body = resp.json()
        except (ValueError, TypeError) as e:
            status = getattr(resp, "status_code", "?")
            raise _ResponseParseError(
                f"메리츠 응답 파싱 실패 ({path}, HTTP {status}): {str(e)[:80]}"
            )
        if not isinstance(body, dict):
            raise _ResponseParseError(
                f"메리츠 응답 형식 오류 ({path}): dict가 아닌 응답이 왔습니다."
            )
        return body

    # ── 합성 주문번호 (YYYYMMDD:제로패딩주문번호) ──

    @classmethod
    def _build_synthetic_odno(cls, oder_date: str, oder_no: str) -> str:
        """
        날짜 없는 주문번호를 충돌 없이 관리하기 위한 합성 ID를 만듭니다.

        다른 날짜에 동일한 주문번호가 재사용되어도 서로 다른 ID가 되도록
        주문일자를 접두어로 붙이고, 주문번호는 10자리로 제로패딩합니다.
        """
        date = cls._digits(oder_date)[:8] or get_kst_now().strftime("%Y%m%d")
        seq = cls._digits(oder_no).zfill(_ORDER_NO_PAD_WIDTH)
        return f"{date}{_ODNO_SEPARATOR}{seq}"

    @classmethod
    def _parse_synthetic_odno(cls, odno: str) -> tuple[str, str]:
        """
        합성 주문번호를 (oder_date, orgl_oder_no)로 분해합니다.

        합성 ID가 아닌 순수 주문번호이면 주문일자는 오늘(KST)로 간주합니다.
        """
        text = str(odno or "").strip()
        if not text:
            raise OrderNotAcceptedError("취소할 주문번호가 비어있습니다")

        if _ODNO_SEPARATOR in text:
            date_part, _, no_part = text.partition(_ODNO_SEPARATOR)
            oder_date = cls._digits(date_part)[:8]
            seq = cls._digits(no_part)
        else:
            oder_date = get_kst_now().strftime("%Y%m%d")
            seq = cls._digits(text)

        if not oder_date or len(oder_date) != 8 or not seq:
            raise OrderNotAcceptedError(
                f"주문번호 형식을 해석할 수 없습니다: odno={odno}"
                f"(기대 형식: YYYYMMDD:주문번호)"
            )
        return oder_date, str(int(seq))

    # ═══════════════════════════════════════════════════════════════════
    # 시장 정보
    # ═══════════════════════════════════════════════════════════════════

    def is_trading_day(self) -> bool:
        """오늘이 미국 증시 영업일인지 확인합니다 (NYSE 기준)."""
        return is_us_trading_day()

    # ═══════════════════════════════════════════════════════════════════
    # 조회 API
    # ═══════════════════════════════════════════════════════════════════

    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        """
        해외주식 현재가를 조회합니다 → StockPrice(open, last).

        GET /market/v1/overseas/prices (공식 필수 query 4종)
        """
        open_price, last = self._fetch_price_data(symbol, exchange, "현재가 조회")
        return StockPrice(open=open_price, last=last)

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
        현재가 + 주문 가능 여부 조회 → StockQuotation(tradable, last).

        메리츠 현재가 응답에 주문가능여부 전용 필드가 없으므로 tradable=True 기본값을
        사용합니다 (get_stock_price와 동일 API).
        """
        _open_price, last = self._fetch_price_data(symbol, exchange, "호가 조회")
        return StockQuotation(tradable=True, last=last)

    def _fetch_price_data(self, symbol: str, exchange: str, context: str) -> tuple[float, float]:
        """
        시세 조회 1건 실행 → (open, last).

        공식 스키마: 응답 data는 **객체**이며 iscd(종목코드)가 비어있으면 실패로 처리합니다.
        """
        params = {
            "mrkt_div_code": "OV",                             # 해외주식
            "mrkt_cls_code": get_quote_exchange_code(exchange),  # OQ/NY/AX (2자리)
            "iscd": symbol.upper(),                            # 대문자 심볼 (예: TQQQ)
            "dely_rltm_cls_code": "0",                         # 실시간
        }
        try:
            body = self._request("GET", PATH_PRICE, params=params)
            self._check_rsp_cd(body, _MARKET_SUCCESS_RSP_CDS, context)
        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"{context} 실패: {str(e)}")

        data = body.get("data")
        if not isinstance(data, dict) or not str(data.get("iscd") or "").strip():
            raise BrokerError(f"{context} 실패: {symbol} 시세 데이터가 없습니다 (iscd 비어있음)")

        last = self._to_float(data.get("prpr"))
        open_price = self._to_float(data.get("oprc"))
        return open_price, last

    def get_daily_closes(self, symbol: str, exchange: str, days: int = 5) -> list[float]:
        """
        해외주식 일봉 종가를 조회합니다 → list[float] (오래된 순).

        GET /market/v1/overseas/candles/days
        - 공식 필수 query 5종만 전송 (from/to 생략 → 최신 40건 반환)
        - 종가는 prpr 필드 사용
        - 당일(ET) 미확정 봉 제외: date가 오늘(ET) 이후인 행은 제외
        """
        params = {
            "mrkt_div_code": "OV",
            "mrkt_cls_code": get_quote_exchange_code(exchange),
            "iscd": symbol.upper(),
            "dely_rltm_cls_code": "0",
            "mod_stpr_cls_code": "0",
        }
        try:
            body = self._request("GET", PATH_DAILY_CANDLES, params=params)
            self._check_rsp_cd(body, _MARKET_SUCCESS_RSP_CDS, "일봉 종가 조회")
        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"일봉 종가 조회 실패: {str(e)}")

        rows = self._extract_rows(body)
        today_et = datetime.now(ET_TZ).strftime("%Y%m%d")

        # 당일 미확정 봉 제외 (date >= 오늘 ET) 후 날짜순 정렬 → 최신 days개
        finalized: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_str = self._digits(row.get("date"))[:8]
            close = self._to_float(row.get("prpr"))
            if not date_str or date_str >= today_et or close <= 0:
                continue
            finalized[date_str] = close

        closes = [finalized[key] for key in sorted(finalized)]
        return closes[-days:]

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        GET /accounts/v1/me/overseas/holdings (성공 rsp_cd=5766)
        - 종목 매칭은 ovrs_stck_symb_iscd 기준 — 일치하는 행이 없으면 None
          (첫 행 fallback 금지)
        - 보유 수량이 0이면 None을 반환합니다.
        """
        params = {
            "schl_wrt_dic": get_quote_exchange_code(exchange),
            "prdt_cd": symbol.upper(),
        }
        try:
            body = self._request("GET", PATH_HOLDINGS, params=params)
            self._check_rsp_cd(body, _ACCOUNT_SUCCESS_RSP_CDS, "잔고 조회")
        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"잔고 조회 실패: {str(e)}")

        rows = self._extract_rows(body)
        if not rows:
            return None

        row = None
        for item in rows:
            iscd = str(item.get("ovrs_stck_symb_iscd") or item.get("iscd") or "")
            if self._symbol_matches(iscd, symbol):
                row = item
                break
        if row is None:
            # 종목 불일치 → 보유 없음으로 처리 (첫 행 fallback 금지)
            return None

        quantity = self._to_int(
            self._first_value(row, ("rmnd_dqty", "seln_able_dqty", "hdng_qty"))
        )
        if quantity <= 0:
            return None

        avg_price = self._to_float(
            self._first_value(row, ("avrg_unpr", "avg_prc", "pchs_avg_pric"))
        )
        return Balance(quantity=quantity, avg_price=avg_price)

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """
        주문 가능 금액을 조회합니다 → PurchaseAmount(orderable_cash).

        GET /trading/v1/overseas/orders/estimate (성공 rsp_cd=5766)
        응답은 통화별 행이며, 공식 통화 필드 **crcd** 가 "USD"인 행을 사용합니다.
        """
        params = {
            "schl_wrt_dic": get_order_vendor_code(exchange),
            "prdt_cd": symbol.upper(),
            "ord_qty": 1,
        }
        try:
            body = self._request("GET", PATH_ESTIMATE, params=params)
            self._check_rsp_cd(body, _ACCOUNT_SUCCESS_RSP_CDS, "매수가능금액 조회")
        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"매수가능금액 조회 실패: {str(e)}")

        rows = self._extract_rows(body)
        if not rows:
            return PurchaseAmount(orderable_cash=0.0)

        amount_keys = ("oder_crnc_oder_able_camt", "crnc_oder_able_camt", "oder_able_camt")
        # 공식 통화 필드는 crcd (구형 crnc_cd/crcy_cd는 호환 fallback)
        currency_keys = ("crcd", "crnc_cd", "crcy_cd", "cur_cd", "ccy")

        usd_row = None
        for item in rows:
            currency = str(self._first_value(item, currency_keys) or "").strip().upper()
            if currency == "USD":
                usd_row = item
                break

        if usd_row is None:
            if len(rows) == 1:
                print(
                    f"[메리츠] {symbol} 매수가능금액 응답에 USD 행이 없어 단일 행 값을 사용합니다."
                )
                usd_row = rows[0]
            else:
                print(
                    f"[메리츠] {symbol} 매수가능금액 응답에서 USD 행을 찾지 못했습니다 (0 처리)."
                )
                return PurchaseAmount(orderable_cash=0.0)

        return PurchaseAmount(
            orderable_cash=self._to_float(self._first_value(usd_row, amount_keys))
        )

    # ── 주문 이력 ──

    def _normalize_order_row(self, raw: dict, source: str) -> dict:
        """
        메리츠 주문 이력 행(openapi.json 2026-10-02)을 state.py 표준 필드로 변환합니다.

        공식 행 필드:
            der_date, der_no, orgl_oder_no, iscd(접미사 .OQ), seln_byov_clcd(1=매도/2=매수),
            der_dqty, ovrs_stck_oder_prc, cntg_dqty, cntg_crnc_prc, cntg_camt,
            ovrs_stck_oder_stat_cls_code(0=정상/비0=거부),
            der_rctf_cncl_cls_code(0=원주문/1=정정/2=취소), rctf_cncl_cls_name,
            der_exec_ymdh/cntg_ymdh(KST), aspr_type_code(취소메타 역매핑), crcd
            (당일 API는 odqt/cntg_qty/ncnt_qty 표기 — 양쪽 모두 인식)

        시각 규칙: der_exec_ymdh/cntg_ymdh 는 **한국(KST) 일시**이므로
        그대로 KST로 파싱합니다 (resolve_real_kst_from_ord 미적용).
        ord_dt/ord_tmd → ord_datetime_kst/ord_datetime_utc, 플래그 `_ord_dt_is_kst_date`.
        """
        iscd = str(raw.get("iscd") or "").strip()
        # 공식 행 필드는 der_date/der_no/der_dqty 계열입니다 (oder_ 계열은 별칭 fallback)
        oder_no = str(self._first_value(raw, ("der_no", "oder_no", "ord_no")) or "").strip()
        oder_date = self._digits(
            self._first_value(raw, ("der_date", "oder_date", "oder_dt", "ord_dt"))
        )[:8]
        orgl_oder_no = str(
            self._first_value(raw, ("orgl_oder_no", "orgl_ord_no")) or ""
        ).strip()

        # 주문일시(der_exec_ymdh) 우선, 없으면 체결일시(cntg_ymdh)
        exec_ymdh = str(
            self._first_value(raw, ("der_exec_ymdh", "oder_exec_ymdh", "exec_ymdh")) or ""
        ).strip()
        cntg_ymdh = str(self._first_value(raw, ("cntg_ymdh",)) or "").strip()
        stamp = self._digits(exec_ymdh or cntg_ymdh)
        if len(stamp) >= 14:
            ord_dt, ord_tmd = stamp[:8], stamp[8:14]
        else:
            ord_dt = oder_date or stamp[:8]
            ord_tmd = "000000"
        if not ord_dt:
            ord_dt = get_kst_now().strftime("%Y%m%d")

        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None
        if len(ord_dt) == 8 and len(ord_tmd) == 6:
            try:
                kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                kst_dt = kst_dt.replace(tzinfo=KST_TZ)
                ord_datetime_kst_iso = kst_dt.isoformat()
                ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except ValueError:
                pass

        # 주문수량/체결수량 (기간=der_dqty/cntg_dqty, 당일=odqt/cntg_qty)
        ordered_qty = self._to_int(
            self._first_value(raw, ("der_dqty", "odqt", "oder_dqty", "oder_qty"))
        )
        filled_qty = self._to_int(
            self._first_value(raw, ("cntg_dqty", "cntg_qty", "ft_ccld_qty", "ccl_qty"))
        )
        # 체결 평균가/금액 (cntg_crnc_prc=체결 통화가, cntg_camt=체결 금액)
        avg_price = self._to_float(
            self._first_value(raw, ("cntg_crnc_prc", "avg_prc", "cntg_prc"))
        )
        amount = self._to_float(self._first_value(raw, ("cntg_camt", "cntg_amt")))
        if not amount and avg_price and filled_qty:
            amount = avg_price * filled_qty
        if not avg_price and amount and filled_qty:
            avg_price = amount / filled_qty

        # 잔량: 당일 ncnt_qty는 공식 신뢰값(0 포함) → 있으면 그대로 사용,
        # 없으면 주문수량-체결수량으로 계산 (구형 ncnt_odqt는 문서상 항상 0 → 신뢰하지 않음)
        if raw.get("ncnt_qty") not in (None, ""):
            remaining = self._to_int(raw.get("ncnt_qty"))
        else:
            remaining = max(ordered_qty - filled_qty, 0)

        # 상태: ovrs_stck_oder_stat_cls_code(0 정상/비0 거부) + rctf(0 원/1 정정/2 취소)
        stat_code = str(raw.get("ovrs_stck_oder_stat_cls_code") or "").strip()
        rctf_code = str(
            raw.get("der_rctf_cncl_cls_code")
            if raw.get("der_rctf_cncl_cls_code") not in (None, "")
            else raw.get("oder_rctf_cncl_cls_code")
            or ""
        ).strip()
        rctf_name = str(raw.get("rctf_cncl_cls_name") or "").strip()
        is_rejected = stat_code not in ("", "0")
        is_cancel = (not is_rejected) and (
            rctf_code == "2" or _CANCEL_STATUS_NAME in rctf_name
        )

        if is_rejected:
            status = _REJECT_STATUS_NAME
        elif is_cancel:
            status = _CANCEL_STATUS_NAME
            remaining = 0
        elif filled_qty <= 0:
            status = "미체결"
        elif remaining > 0:
            status = "부분체결"
        else:
            status = "체결"

        # 매수/매도: seln_byov_clcd (1=매도, 2=매수)
        side = {"1": "매도", "2": "매수"}.get(
            str(raw.get("seln_byov_clcd") or "").strip(), ""
        )

        # 주문구분: aspr_type_code 역매핑 (취소 메타 복원용)
        aspr_code = str(raw.get("aspr_type_code") or "").strip()
        oder_cls_code = _ASPR_TO_ODER_CLS_CODE.get(aspr_code, "") or str(
            raw.get("oder_cls_code") or ""
        )

        return {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            # ord_dt가 KST 일자임 (KIS/LS/키움의 미국영업일 컨벤션과 다름)
            "_ord_dt_is_kst_date": True,
            "prdt_name": str(self._first_value(raw, ("prdt_nm", "prdt_nme", "stk_nm")) or ""),
            "sll_buy_dvsn_cd_name": side,
            "ft_ord_qty": str(ordered_qty),
            "ft_ccld_qty": str(filled_qty),
            "ft_ccld_unpr3": str(avg_price),
            "ft_ccld_amt3": str(amount),
            "nccs_qty": str(max(remaining, 0)),
            "prcs_stat_name": status,
            "tr_mket_name": str(self._first_value(raw, ("mkt_nm", "tr_mkt_nm")) or ""),
            "tr_crcy_cd": str(raw.get("crcd") or raw.get("crnc_cd") or "USD"),
            "odno": self._build_synthetic_odno(oder_date, oder_no),
            # 거래소는 iscd 접미사(예: TQQQ.OQ → OQ)
            "ovrs_excg_cd": iscd.partition(".")[2] if "." in iscd else "",
            # ── 내부 전용 메타 (state.py는 사용하지 않음) ──
            "_iscd": iscd,
            "_oder_date": oder_date,
            "_oder_cls_code": oder_cls_code,
            "_order_price": self._to_float(raw.get("ovrs_stck_oder_prc")),
            "_ordered_qty": ordered_qty,
            "_filled_qty": filled_qty,
            "_remaining": max(remaining, 0),
            "_is_cancel": is_cancel,
            "_is_rejected": is_rejected,
            "_plain_oder_no": str(int(self._digits(oder_no))) if self._digits(oder_no) else "",
            "_plain_orgl_oder_no": (
                str(int(self._digits(orgl_oder_no))) if self._digits(orgl_oder_no) else ""
            ),
            "_source": source,
        }

    def _merge_order_rows(self, period_rows: list[dict], today_rows: list[dict]) -> list[dict]:
        """
        기간 이력과 당일 이력을 합칩니다.

        - 같은 주문은 당일 이력을 우선 반영합니다.
          ncnt_odqt는 문서상 항상 0이므로 잔량은 당일 이력의 값을 신뢰합니다.
        - 취소 행은 원주문 상태(취소/잔량 0)에 반영합니다.
        """
        merged: dict[str, dict] = {}
        order: list[str] = []

        for item in list(period_rows) + list(today_rows):
            key = item["odno"]
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(item)
                order.append(key)
                continue

            # 당일 이력이 잔량·체결의 우선 기준
            existing["nccs_qty"] = item["nccs_qty"]
            existing["_remaining"] = item["_remaining"]
            existing["prcs_stat_name"] = item["prcs_stat_name"]
            if item["_filled_qty"] > existing["_filled_qty"]:
                existing["ft_ccld_qty"] = item["ft_ccld_qty"]
                existing["ft_ccld_unpr3"] = item["ft_ccld_unpr3"]
                existing["ft_ccld_amt3"] = item["ft_ccld_amt3"]
                existing["_filled_qty"] = item["_filled_qty"]
            if item["_is_cancel"]:
                existing["_is_cancel"] = True
                existing["prcs_stat_name"] = _CANCEL_STATUS_NAME
                existing["nccs_qty"] = "0"
                existing["_remaining"] = 0

        # 취소행을 원주문에 반영 — (주문일자 _oder_date, 원주문번호) 쌍으로 매칭합니다.
        # 번호만으로 매칭하면 다른 날짜의 동일 주문번호를 오연결할 수 있습니다.
        for item in merged.values():
            if not item["_is_cancel"] or not item["_plain_orgl_oder_no"]:
                continue
            cancel_date = item.get("_oder_date") or item["ord_dt"]
            for target in merged.values():
                if target is item or target["_is_cancel"]:
                    continue
                target_date = target.get("_oder_date") or target["ord_dt"]
                if not cancel_date or not target_date:
                    continue
                if (
                    target["_plain_oder_no"] == item["_plain_orgl_oder_no"]
                    and target_date == cancel_date
                ):
                    target["_is_cancel"] = True
                    target["prcs_stat_name"] = _CANCEL_STATUS_NAME
                    target["nccs_qty"] = "0"
                    target["_remaining"] = 0
                    break

        rows = [merged[key] for key in order]
        rows.sort(key=lambda r: (r["ord_dt"], r["ord_tmd"], r["odno"]), reverse=True)
        return rows

    def _fetch_today_rows(self, symbol: str) -> list[dict]:
        """
        당일 주문 이력을 조회해 정규화합니다.

        GET /trading/v1/overseas/orders/today — 공식 스키마에 종목/거래소
        파라미터가 없으므로 빈 파라미터로 조회한 뒤 iscd로 클라이언트 필터링합니다.
        """
        body = self._request("GET", PATH_ORDER_TODAY)
        self._check_rsp_cd(body, _TRADE_QUERY_SUCCESS_RSP_CDS, "당일 주문이력 조회")
        rows = []
        for row in self._extract_rows(body):
            normalized = self._normalize_order_row(row, "today")
            if self._symbol_matches(normalized.get("_iscd", ""), symbol):
                rows.append(normalized)
        return rows

    def get_order_history(
        self,
        symbol: str,
        exchange: str,
        days: int = 30,
        verbose: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """
        주문 내역을 조회합니다 → list[dict] (state.py 표준 필드).

        GET /trading/v1/overseas/orders/history (기간) + /orders/today (당일) 병합

        - 이력 API에는 종목 파라미터가 없으므로 iscd로 클라이언트 필터링합니다.
        - rsp_cd=5762면 다음 페이지가 있어 body의 tr_cont_key를 원문 그대로
          다음 요청에 전달합니다 (_MAX_HISTORY_PAGES 상한).
        - 5820(조회결과 없음) → 빈 목록

        표준 필드 16종:
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        now_kst = get_kst_now()
        start_date = (now_kst - timedelta(days=days)).strftime("%Y%m%d")
        end_date = now_kst.strftime("%Y%m%d")

        # 공식 필수 query 3종 (cntg_cls_code="0" = 공식 기본값 미확인 → 명시 전송)
        params: dict = {
            "oder_sta_date": start_date,
            "oder_fin_date": end_date,
            "cntg_cls_code": "0",
        }

        print(f"[주문이력] {symbol} 조회: {start_date} ~ {end_date} (기간 + 당일)")

        try:
            period_rows: list[dict] = []
            for page in range(_MAX_HISTORY_PAGES):
                body = self._request("GET", PATH_ORDER_HISTORY, params=params)
                if str(body.get("rsp_cd", "") or "") == _NO_DATA_RSP_CD:
                    break  # 5820 → 조회결과 없음
                self._check_rsp_cd(body, _TRADE_QUERY_SUCCESS_RSP_CDS, "주문이력 조회")
                period_rows.extend(
                    self._normalize_order_row(row, "history")
                    for row in self._extract_rows(body)
                )
                next_key = self._next_page_key(body)
                if not next_key:
                    break
                params = dict(params, tr_cont_key=next_key)
            else:
                print(
                    f"[주문이력] 페이지 상한({_MAX_HISTORY_PAGES}) 도달 — "
                    "남은 페이지는 가져오지 않습니다"
                )

            # 종목 파라미터가 없으므로 클라이언트 필터링 (iscd prefix)
            period_rows = [
                row
                for row in period_rows
                if self._symbol_matches(row.get("_iscd", ""), symbol)
            ]

            # 당일 이력: ncnt_qty(잔량 신뢰값)의 유일한 출처
            try:
                today_rows = self._fetch_today_rows(symbol)
            except BrokerError as e:
                print(f"[메리츠] 당일 이력 조회 실패 → 기간 이력만 사용합니다: {str(e)[:80]}")
                today_rows = []
        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"주문이력 조회 실패: {str(e)}")

        order_history = self._merge_order_rows(period_rows, today_rows)
        print(
            f"[주문이력] {symbol} 기간 {len(period_rows)}건 / 당일 {len(today_rows)}건 "
            f"→ 병합 {len(order_history)}건"
        )

        if verbose and order_history:
            try:
                n = int(limit) if limit and int(limit) > 0 else 100
            except (ValueError, TypeError):
                n = 100
            n = min(n, len(order_history))

            print(f"[주문이력 요약] {symbol} 최근 {n}건")
            for item in order_history[:n]:
                ord_dt = item.get("ord_dt", "")
                if ord_dt and len(ord_dt) == 8:
                    date_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                    header = "한국영업일" if item.get("_ord_dt_is_kst_date") else "거래일"
                else:
                    header, date_str = "거래일", "(날짜없음)"

                kst_iso = item.get("ord_datetime_kst")
                if kst_iso:
                    try:
                        kst_str = (
                            datetime.fromisoformat(kst_iso).strftime("%Y-%m-%d %H:%M:%S")
                            + " KST"
                        )
                    except (ValueError, TypeError):
                        kst_str = kst_iso
                else:
                    kst_str = "(시간없음)"

                print(
                    f"{header} {date_str} | 한국시각 {kst_str} | odno={item.get('odno','')}"
                    f" | {item.get('sll_buy_dvsn_cd_name','')}"
                    f" | qty={item.get('ft_ccld_qty','0')}"
                    f" | price={self._to_float(item.get('ft_ccld_unpr3')):.2f}"
                    f" | amt={self._to_float(item.get('ft_ccld_amt3')):.2f}"
                    f" | {item.get('prcs_stat_name','')}"
                )

        return order_history

    # ═══════════════════════════════════════════════════════════════════
    # 주문 API
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _confirm_warning_codes() -> set:
        """
        재확인 전송을 허용한 경고 코드 목록을 읽습니다 (기본 OFF).

        MERITZ_CONFIRM_WARNINGS에 명시된 warn_cls_code만
        warn_cnfr_yn=A 1회 재전송합니다 (예: "1,3").
        """
        raw = os.getenv(_ENV_CONFIRM_WARNINGS, "") or ""
        return {token.strip() for token in raw.replace(";", ",").split(",") if token.strip()}

    def place_order(
        self,
        symbol: str,
        exchange: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
    ) -> Optional[OrderResult]:
        """
        해외주식 주문을 실행합니다 → Optional[OrderResult].

        POST /trading/v1/overseas/orders/buy | /sell

        - 메리츠는 모의투자가 없어 항상 LIVE로 동작합니다 (DRY는 DryBroker 처리)
        - 예약주문을 사용하지 않으므로 is_reservation=False
        - 성공 조건: rsp_cd == "0001" AND warn_cls_code == 0 AND oder_no 존재
          (하나라도 어긋나면 OrderResult를 만들지 않습니다)
        - 네트워크 타임아웃/비JSON → OrderError (접수 여부 불확실, fence 유지)

        exchange 규약 (KIS place_order와 동일):
          - 호출 측에서 broker.exchange_code()로 변환된 시세 코드를 받으므로,
            주문 벤더코드(4자리)로 한 번 더 변환해 전송합니다.
        """
        cls_code = _ORDER_TYPE_MAP.get((order_type or "").upper())
        if cls_code is None:
            raise OrderNotAcceptedError(f"지원하지 않는 주문 유형입니다: {order_type}")

        if side not in ("BUY", "SELL"):
            raise OrderNotAcceptedError(f"지원하지 않는 주문 방향입니다: {side}")

        # 시장가 계열은 단가 없이 접수되므로 0을 전송합니다
        if (order_type or "").upper() in _MARKET_ORDER_TYPES:
            price = 0.0
        else:
            # 브로커 호가 단위 규칙에 맞춰 주문가를 정규화 ($1+ → 소수점 2자리 등)
            price = normalize_order_price(price)

        vendor_code = get_order_vendor_code(exchange)
        path = PATH_ORDER_BUY if side == "BUY" else PATH_ORDER_SELL
        # 공식 주문 body 7필드 (openapi.json 2026-10-02)
        order_body = {
            "ovrs_stck_symb_iscd": symbol.upper(),
            "ovrs_exch_vndr_idno": vendor_code,
            "oder_cls_code": cls_code,
            "oder_cond_cls_code": "0",           # 주문조건: 0=일반 (기본값)
            "odqt": int(quantity),
            "ovrs_stck_oder_prc": float(price),  # 시장가 계열(05/33/34)은 0
            "warn_cnfr_yn": "N",                 # 경고 1차는 미확인으로 전송
        }

        order_id = self._submit_order(
            path=path,
            order_body=order_body,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
        )

        # 취소 API 복구에 필요한 메타 캐시 (합성 주문번호 기준)
        self._submitted_orders[order_id] = {
            "oder_cls_code": cls_code,
            "odqt": quantity,
        }

        print("\n========== [LIVE 모드] 메리츠 주문 성공 ==========")
        print(f"종목 코드: {symbol}")
        print(f"거래소 벤더코드: {vendor_code}")
        print(f"주문구분: {order_type} → {cls_code}")
        print(f"주문번호(합성): {order_id}")
        print(f"주문수량: {quantity}주")
        print(f"주문가격: ${price}")
        print("==================================================\n")

        return OrderResult(
            order_id=order_id,
            order_time=get_kst_now().strftime("%Y%m%d%H%M%S"),
            is_reservation=False,
        )

    def _submit_order(
        self,
        path: str,
        order_body: dict,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
    ) -> str:
        """
        주문 1건을 전송하고 합성 주문번호를 반환합니다.

        경고(warn_cls_code) 처리 (공식: 응답 data 안에 warn_cls_code/der_no):
        - 0: 정상 접수
        - 1/3: 기본 OrderNotAcceptedError,
                MERITZ_CONFIRM_WARNINGS에 명시된 경우에만 warn_cnfr_yn="A" 1회 재전송
        - 2: 재전송 없이 OrderNotAcceptedError (중복 주문 위험)
        - rsp_cd != "0001": 브로커 거부 → OrderNotAcceptedError
        - der_no(주문번호) 누락: 접수 여부 불확실 → OrderError
        """
        confirm_sent = False
        while True:
            try:
                body = self._request("POST", path, json_body=order_body, is_query=False)
            except _ResponseParseError as e:
                # 비JSON 응답 → 접수 여부 불확실 (fence 유지)
                raise OrderError(str(e)) from e
            except AuthError as e:
                # 토큰 발급 실패 = 주문 전송 전 실패 → 미접수 확정
                raise OrderNotAcceptedError(str(e)) from e
            except BrokerError as e:
                # rsp_cd 기반 거부 → 미접수 확정
                raise OrderNotAcceptedError(str(e)) from e
            except requests.exceptions.RequestException as e:
                # 네트워크 타임아웃/연결 오류 → 접수 여부 불확실 (fence 유지)
                raise OrderError(f"주문 실행 실패 (접수 여부 불확실): {str(e)}") from e

            rsp_cd = str(body.get("rsp_cd", "") or "")
            warn_cls_code = self._warn_cls_code(body)
            message = self._error_message(body)

            if rsp_cd != _ORDER_ACCEPT_RSP_CD:
                raise OrderNotAcceptedError(
                    f"메리츠 주문 접수 거부 [{rsp_cd}]: {message} "
                    f"({symbol} {side} {quantity}주 ${price})"
                )

            if warn_cls_code and warn_cls_code != _WARN_NONE:
                if warn_cls_code in _WARN_NO_RESEND:
                    raise OrderNotAcceptedError(
                        f"메리츠 주문 경고(등급 {warn_cls_code}, 재전송 불가): {message}"
                    )
                if (
                    warn_cls_code in _WARN_CONFIRMABLE
                    and warn_cls_code in self._confirm_warning_codes()
                    and not confirm_sent
                ):
                    confirm_sent = True
                    print(
                        f"[메리츠] 주문 경고(등급 {warn_cls_code}) → "
                        f"warn_cnfr_yn=A 로 1회 재전송합니다: {symbol} {side} {quantity}주"
                    )
                    order_body = dict(order_body, warn_cnfr_yn="A")
                    continue
                raise OrderNotAcceptedError(
                    f"메리츠 주문 경고(등급 {warn_cls_code}, 확인 미설정): {message}"
                    f" (확인 허용: {_ENV_CONFIRM_WARNINGS} 환경변수)"
                )

            oder_no = str(self._order_no_from(body) or "").strip()
            if not oder_no:
                # 접수 응답인데 주문번호가 없으면 상태를 확정할 수 없습니다
                raise OrderError(
                    f"메리츠 주문 응답에 주문번호(der_no)가 없습니다: {message} "
                    f"({symbol} {side} {quantity}주 ${price})"
                )

            return self._build_synthetic_odno(self._oder_date_from(body), oder_no)

    @classmethod
    def _order_no_from(cls, body: dict) -> str:
        """주문 접수 응답에서 주문번호를 꺼냅니다 (data.der_no 우선 → 최상위 fallback)."""
        data = body.get("data")
        if isinstance(data, dict):
            value = cls._first_value(data, ("der_no", "oder_no"))
            if value not in (None, ""):
                return str(value)
        value = cls._first_value(body, ("der_no", "oder_no"))
        return str(value) if value not in (None, "") else ""

    @classmethod
    def _oder_date_from(cls, body: dict) -> str:
        """주문 접수 응답에서 주문일자(YYYYMMDD)를 꺼냅니다 (없으면 빈 문자열)."""
        data = body.get("data")
        if isinstance(data, dict):
            value = cls._digits(cls._first_value(data, ("oder_date", "der_date")))
            if value:
                return value[:8]
        value = cls._digits(cls._first_value(body, ("oder_date", "der_date")))
        return value[:8]

    # ═══════════════════════════════════════════════════════════════════
    # 주문 취소 (concrete adapter 전용 — Broker ABC에 정의 없음)
    # ═══════════════════════════════════════════════════════════════════

    def cancel_order(
        self,
        odno: str,
        symbol: str | None = None,
        exchange: str | None = None,
    ) -> None:
        """
        메리츠 주문을 취소합니다.

        POST /trading/v1/overseas/orders/cancel
        필수 4필드: oder_date, orgl_oder_no, oder_cls_code, odqt

        - odno는 합성 주문번호(YYYYMMDD:주문번호)에서 oder_date/orgl_oder_no 를 분해합니다
        - oder_cls_code/odqt 는 제출 시 캐시한 메타를 사용하고,
          캐시가 없으면 당일 이력에서 복원합니다
        - 응답 data.warn_cls_code != 0 → 취소 미접수 (OrderNotAcceptedError)
        - 이미 취소/체결된 주문 등 브로커 거부는 OrderNotAcceptedError
        - 네트워크 오류·비JSON은 OrderError (취소 여부 불확실)
        """
        oder_date, orgl_oder_no = self._parse_synthetic_odno(odno)
        cache_key = self._build_synthetic_odno(oder_date, orgl_oder_no)
        meta = self._submitted_orders.get(cache_key)

        if meta is None and symbol:
            meta = self._lookup_cancel_meta(symbol, orgl_oder_no)
            if meta is not None:
                self._submitted_orders[cache_key] = meta

        if meta is None:
            raise OrderNotAcceptedError(
                f"취소할 주문의 주문구분/수량을 확인할 수 없습니다: odno={odno}. "
                f"주문 시점 캐시가 없고 당일 이력에도 없습니다."
            )

        body = {
            "oder_date": oder_date,
            "orgl_oder_no": orgl_oder_no,
            "oder_cls_code": meta["oder_cls_code"],
            "odqt": int(meta["odqt"]),
        }
        assert all(body.get(field) not in (None, "") for field in _CANCEL_REQUIRED_FIELDS)

        try:
            resp_body = self._request("POST", PATH_ORDER_CANCEL, json_body=body, is_query=False)
            self._check_rsp_cd(resp_body, _CANCEL_SUCCESS_RSP_CDS, "주문 취소")
        except _ResponseParseError as e:
            raise OrderError(str(e)) from e
        except AuthError as e:
            raise OrderNotAcceptedError(str(e)) from e
        except BrokerError as e:
            # rsp_cd 기반 거부(취소 불가/이미 체결 등) → 취소 미확정
            raise OrderNotAcceptedError(f"메리츠 주문 취소 실패: {str(e)}") from e
        except requests.exceptions.RequestException as e:
            raise OrderError(f"메리츠 주문 취소 실패 (취소 여부 불확실): {str(e)}") from e

        # 공식 스키마: 취소 응답도 data.warn_cls_code로 경고를 반환합니다
        warn_code = self._warn_cls_code(resp_body)
        if warn_code != _WARN_NONE:
            raise OrderNotAcceptedError(
                f"메리츠 주문 취소 미접수 [warn_cls_code={warn_code}]: "
                f"{self._error_message(resp_body)} (odno={odno})"
            )

        print(f"[메리츠] 주문 취소 성공: odno={odno} (oder_date={oder_date})")

    def _lookup_cancel_meta(
        self,
        symbol: str,
        orgl_oder_no: str,
    ) -> Optional[dict]:
        """당일 이력에서 취소에 필요한 주문구분/수량(odqt)을 복원합니다 (없으면 None)."""
        try:
            rows = self._fetch_today_rows(symbol)
        except BrokerError as e:
            print(f"[메리츠] 취소용 당일 이력 조회 실패: {str(e)[:80]}")
            return None

        for row in rows:
            if row["_plain_oder_no"] == orgl_oder_no and row["_ordered_qty"] > 0:
                return {
                    "oder_cls_code": row["_oder_cls_code"] or _ORDER_TYPE_MAP["LIMIT"],
                    "odqt": row["_ordered_qty"],
                }
        return None

    # ═══════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → 시세 조회 코드 변환 (예: 'NAS' → 'OQ')."""
        return get_quote_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()