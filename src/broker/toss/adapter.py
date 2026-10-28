"""
TossBroker — 토스증권 Broker 구현체.

토스증권 Open API (REST)를 통해 미국 주식 자동매매를 실행합니다.
- 실전만 지원 (모의투자/샌드박스 없음)
- 순수 REST JSON API (한국 증권사 TR 프로토콜과 다름)
- OAuth2 Client Credentials 인증
- 계좌 관련 API에는 X-Tossinvest-Account 헤더 필요
- Rate-limit: 그룹별 TPS (ORDER: 6, MARKET_DATA: 10 등)

사용법:
    broker = TossBroker()
    price = broker.get_stock_price("TQQQ", "NAS")

DRY 모드는 DryBroker 래퍼로 처리 — TossBroker 자체는 항상 LIVE로 동작.
"""
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

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
# 공개 로그용 주문번호 마스킹 (표시 전용 — 주문번호 값 자체는 절대 바꾸지 않습니다)
from log_mask import mask_odno
from broker.toss.auth import get_access_token
from broker.toss.session import TossSession
from broker.toss.exchange import get_api_exchange_code


# ── 주문 방향 매핑 (Toss API "BUY"/"SELL" → state.py "매수"/"매도") ──
_SIDE_MAP = {"BUY": "매수", "SELL": "매도"}

# ── 주문 상태 매핑 (Toss API 영어 상태 → state.py 표준 한국어 상태) ──
# 토스는 OPEN/PARTIAL/FILLED/CANCELED/REJECTED 같은 영어 상태를 반환합니다.
# state.py 와 취소 대상 선정 로직은 한국어 상태를 기준으로 하므로
# 브로커 경계에서 표준 상태로 변환합니다. (원본은 prcs_stat_name_raw 에 보존)
_TOSS_STATUS_TO_KOREAN = {
    "OPEN": "미체결",
    "PENDING": "미체결",
    "NEW": "미체결",
    "ACTIVE": "미체결",
    "SCHEDULED": "미체결",
    "PARTIAL": "부분체결",
    "PARTIALLY_FILLED": "부분체결",
    "FILLED": "체결",
    "CANCELED": "취소",
    "CANCELLED": "취소",
    "REJECTED": "거부",
    "EXPIRED": "만료",
    "CLOSED": "종료",
    "DELISTED": "종료",
}
_TOSS_TERMINAL_STATUSES = {
    "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "CLOSED", "DELISTED",
}


def map_order_status(status: str) -> str:
    """토스 주문 상태(영어)를 state.py 표준 한국어 상태로 변환합니다.

    알 수 없는 상태는 원본을 그대로 반환합니다 (종료로 간주하지 않음).
    """
    raw = str(status or "").strip()
    if not raw:
        return ""
    return _TOSS_STATUS_TO_KOREAN.get(raw.upper(), raw)


def _raw_order_needs_last_price(raw: dict) -> bool:
    """체결수량은 있는데 체결금액·평균가가 모두 없어 현재가 폴백이 필요한지."""
    def _num(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    execution = (raw or {}).get("execution") or {}
    qty = _num(execution.get("filledQuantity"))
    amount = _num(execution.get("filledAmount"))
    avg = _num(execution.get("averageFilledPrice"))
    if avg <= 0:
        avg = _num(execution.get("averagePrice"))
    return qty > 0 and amount <= 0 and avg <= 0


# ── 주문 유형 매핑 ──
# timeInForce=DAY + orderType=LIMIT = 일반 지정가 (LIMIT)
# timeInForce=CLS + orderType=LIMIT = 장마감지정가 (LOC)
_TOSS_ORDER_TYPE_MAP = {
    "LIMIT": ("LIMIT", "DAY"),
    "LOC":   ("LIMIT", "CLS"),
}

# 모의투자 미지원 주문 유형 — 토스는 모의투자가 없으므로 의미 없으나
# 기존 코드 호환을 위해 정의 (Limit으로 자동 변환)
_DEMO_UNSUPPORTED_ORDER_TYPES = {"LOC", "LOO", "MOO", "MOC"}

# 토스 MOC(장마감시장가) 미지원 → SELL MOC를 LOC(장마감지정가)로 변환할 때 쓰는
# 매도 제한가. $0.01은 어떤 종가에서도 충족되는 하한이라 종가에 체결되어
# MOC와 동일하게 동작합니다.
_MOC_SELL_PROXY_PRICE = 0.01


class TossBroker(Broker):
    # TOSS 는 status=OPEN 조회를 지원합니다 (실측 2026-10-23).
    supports_open_orders_query = True
    """
    토스증권 미국주식 REST API Broker 구현체.

    config.py의 BROKER_CONFIG["toss"]에서 설정을 읽습니다.
    모의투자 지원하지 않으며, 모든 주문은 실전 계좌에 즉시 실행됩니다.
    """

    def __init__(self):
        from config import BROKER_CONFIG, HTTP_TIMEOUT

        self._domain = BROKER_CONFIG["domain"]
        self._client_id = BROKER_CONFIG["client_id"]
        self._client_secret = BROKER_CONFIG["client_secret"]
        self._account_seq = BROKER_CONFIG["account_seq"]
        self._session = TossSession(
            self._domain,
            account_seq=self._account_seq,
            timeout=HTTP_TIMEOUT,
        )

        # rate-limit: 토스는 그룹별 TPS가 다름
        # ORDER: 6/sec, MARKET_DATA: 10/sec, ACCOUNT: 1/sec
        # 안전하게 가장 보수적인 값 사용
        self._rate_limit_wait = 0.2  # 5/sec (ORDER 그룹 기반)

    # ═══════════════════════════════════════════════════════════════════
    # 속성
    # ═══════════════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """증권사 식별자"""
        return "toss"

    # ═══════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (auth.get_access_token 캐싱 포함)."""
        try:
            return get_access_token(
                domain=self._domain,
                client_id=self._client_id,
                client_secret=self._client_secret,
                timeout=self._session.timeout,
            )
        except AuthError:
            raise
        except Exception as e:
            raise BrokerError(f"토스 토큰 획득 실패: {str(e)}")

    def _request_with_rate_retry(
        self,
        method: str,
        path: str,
        token: str,
        params: dict | None = None,
        json_body: dict | None = None,
        extra_headers: dict | None = None,
        return_headers: bool = False,
        retry_network: bool = True,
    ) -> dict | tuple[dict, requests.structures.CaseInsensitiveDict]:
        """
        토스 API 요청 래퍼 — rate-limit/타임아웃 재시도 포함.

        - rate-limit (HTTP 429): Retry-After 헤더 기반 대기 + 지수 백오프, 최대 3회
        - 타임아웃: 지수 백오프 + jitter, 최대 3회

        기본은 dict(파싱된 JSON)를 반환합니다. return_headers=True면
        (dict, response.headers)를 반환합니다.
        """
        MAX_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_RETRIES:
            try:
                for retry in range(MAX_RETRIES + 1):
                    time.sleep(self._rate_limit_wait)

                    if method.upper() == "GET":
                        resp = self._session.get(
                            path, token, params=params, extra_headers=extra_headers
                        )
                    else:
                        resp = self._session.post(
                            path, token, json_body=json_body, extra_headers=extra_headers
                        )

                    # rate-limit (429) 처리
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After", "1")
                        try:
                            wait_time = float(retry_after)
                        except (ValueError, TypeError):
                            wait_time = 1.0
                        wait_time = max(wait_time, 1.0) * (2 ** retry)
                        print(f"토스 rate-limit 초과 (429), {wait_time:.1f}초 후 재시도...")
                        time.sleep(wait_time)
                        continue

                    # 4xx/5xx: 에러 envelope 먼저 파싱后再 raise (raise_for_status 전에)
                    if resp.status_code >= 400:
                        try:
                            err_body = resp.json()
                        except (ValueError, TypeError):
                            err_body = {}
                        err_envelope = err_body.get("error", {}) if isinstance(err_body, dict) else {}
                        if err_envelope:
                            code = err_envelope.get("code", "")
                            message = err_envelope.get("message", "알 수 없는 오류")
                            if code in ("invalid-token", "expired-token", "edge-blocked"):
                                raise AuthError(f"토스 인증 오류 [{code}]: {message}")
                            raise BrokerError(f"토스 API 오류 [{code}]: {message}")
                        # envelope 없음 — transport 수준 이상으로 간주
                        reason = resp.reason or resp.status_code
                        raise BrokerError(f"토스 API 오류 [HTTP {resp.status_code}]: {str(reason)[:100]}")

                    body = resp.json()

                    # 토스 공통 에러 envelope 처리 (200 + error)
                    if "error" in body:
                        error = body["error"]
                        code = error.get("code", "")
                        message = error.get("message", "알 수 없는 오류")

                        # 인증 오류
                        if code in ("invalid-token", "expired-token", "edge-blocked"):
                            raise AuthError(f"토스 인증 오류 [{code}]: {message}")

                        # 그 외 비즈니스 오류
                        raise BrokerError(f"토스 API 오류 [{code}]: {message}")

                    if return_headers:
                        return body, resp.headers
                    return body

                raise BrokerError("API 호출 실패: rate-limit 재시도 초과")

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if not retry_network:
                    raise
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f"토스 API 타임아웃: {str(e)[:60]}...")
                    print(f"   {wait:.1f}초 후 재시도... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise BrokerError(f"토스 API 호출 실패 (네트워크): {str(e)}")

        raise BrokerError("API 호출 실패: 재시도 한도 초과")

    def _normalize_order_item(self, raw: dict, last_price: float | None = None) -> dict:
        """
        토스 주문 응답(Order 모델)을 state.py 표준 필드로 변환.

        핵심 변환:
        - side: "BUY"/"SELL" → "매수"/"매도"
        - status: OPEN/PARTIAL/FILLED... → 미체결/부분체결/체결...
        - orderedAt: ISO 8601 KST → ord_dt(YYYYMMDD), ord_tmd(HHMMSS)
        - execution.filledQuantity → ft_ccld_qty
        - execution.averageFilledPrice → ft_ccld_unpr3
        - execution.filledAmount → ft_ccld_amt3

        체결금액 방어:
          filledAmount 가 0/누락인데 체결수량이 있으면
          `수량 × 평균체결가` → `수량 × 현재가(last_price)` 순으로 재계산합니다.
          둘 다 없으면 경고 후 해당 체결을 제외(qty=0)합니다 — 조용히 0원 반영 금지.
        """
        # 주문 방향 정규화
        side_raw = raw.get("side", "")
        sll_buy_dvsn_cd_name = _SIDE_MAP.get(side_raw, "")

        # 시간 변환 (orderedAt: ISO 8601 KST → ord_dt, ord_tmd)
        ordered_at = raw.get("orderedAt", "")
        ord_dt = ""
        ord_tmd = ""
        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None

        if ordered_at:
            try:
                kst_dt = datetime.fromisoformat(ordered_at)
                ord_dt = kst_dt.strftime("%Y%m%d")
                ord_tmd = kst_dt.strftime("%H%M%S")
                ord_datetime_kst_iso = kst_dt.isoformat()
                ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except (ValueError, TypeError):
                pass

        # 체결 정보 추출 (토스 실제 필드명: averageFilledPrice / filledAmount)
        execution = raw.get("execution", {}) or {}

        def _num(value) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        filled_qty = int(_num(execution.get("filledQuantity")))
        avg_price = _num(execution.get("averageFilledPrice"))
        if avg_price <= 0:
            # 구버전/문서 표기 호환
            avg_price = _num(execution.get("averagePrice"))
        quantity = int(_num(raw.get("quantity")))

        filled_amount = _num(execution.get("filledAmount"))
        amount_source = "broker" if filled_amount > 0 else ""
        amount_estimated = False
        amount_unavailable = filled_qty > 0 and filled_amount <= 0
        average_price_for_row = avg_price

        # 체결수량은 **그대로 보존**합니다. 금액이 없으면 현재가/평균가로 추정하지 않고
        # 금액 미확정 상태로 넘겨, 공통 보류 로직(_fill_amount_is_available →
        # _pending_amount_odnos → SymbolScopedError)이 정산을 멈추게 합니다.
        # (수량을 0으로 바꾸면 보류 대상에서 사라져 워터마크 뒤로 밀린 체결이 영구 누락됩니다)
        if amount_unavailable:
            print(
                f"[경고] 토스 주문 {raw.get('orderId', '')} 체결 {filled_qty}주 "
                f"금액 미확정(filledAmount 없음) → 정산 보류 대상으로 전달합니다"
            )

        # 평균가가 없어도 체결금액이 있으면 단가를 역산해 표시합니다.
        effective_price = average_price_for_row if filled_qty > 0 else 0.0
        if effective_price <= 0 and filled_amount > 0 and filled_qty > 0:
            effective_price = round(filled_amount / filled_qty, 4)

        status_raw = str(raw.get("status", "") or "")
        status = map_order_status(status_raw)
        is_terminal = status_raw.upper() in _TOSS_TERMINAL_STATUSES
        remaining = 0 if is_terminal else max(quantity - filled_qty, 0)

        return {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "prdt_name": raw.get("symbol", ""),
            "sll_buy_dvsn_cd_name": sll_buy_dvsn_cd_name,
            "ft_ord_qty": str(quantity),
            "ft_ccld_qty": str(filled_qty),
            "ft_ccld_unpr3": str(effective_price),
            "ft_ccld_amt3": str(filled_amount) if filled_amount > 0 else "0",
            "nccs_qty": str(remaining),
            "prcs_stat_name": status or status_raw,
            "prcs_stat_name_raw": status_raw,
            "tr_mket_name": "",
            "tr_crcy_cd": "USD",
            "odno": raw.get("orderId", ""),
            "ovrs_excg_cd": "",
            # 체결금액 확정/미확정 근거 (로그·evidence 추적용)
            "_ft_ccld_amt_source": amount_source,
            "_ft_ccld_amt_estimated": amount_estimated,
            "_skipped_fill_qty": "0",
            "_fill_amount_unavailable": amount_unavailable,
            # orderedAt 기반 ord_dt는 KST 날짜 (미국영업일 아님)
            "_ord_dt_is_kst_date": True,
        }

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

        토스 API: GET /api/v1/prices?symbols={ticker}
        토스는 시가(open)를 별도 API에서만 제공하므로 open=0 반환.
        전략에서 open 필드는 미사용 확인됨.
        """
        token = self._get_token()
        ticker = symbol.upper()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/prices", token, params={"symbols": ticker}
            )
            result = data.get("result", [])
            if not result:
                raise BrokerError(f"현재가 조회 실패: {symbol} 심볼을 찾을 수 없습니다")

            price_item = result[0] if isinstance(result, list) else result
            last_price = float(price_item.get("lastPrice", 0))

            return StockPrice(open=0.0, last=last_price)

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재가 조회 실패: {str(e)}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
        현재가 + 주문 가능 여부 조회 → StockQuotation(tradable, last).

        토스는 주문가능여부 전용 필드가 없으므로 tradable=True 기본값 사용.
        get_stock_price와 동일 API 사용.
        """
        token = self._get_token()
        ticker = symbol.upper()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/prices", token, params={"symbols": ticker}
            )
            result = data.get("result", [])
            if not result:
                raise BrokerError(f"호가 조회 실패: {symbol} 심볼을 찾을 수 없습니다")

            price_item = result[0] if isinstance(result, list) else result
            last_price = float(price_item.get("lastPrice", 0))

            return StockQuotation(tradable=True, last=last_price)

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"호가 조회 실패: {str(e)}")

    def get_daily_closes(self, symbol: str, exchange: str, days: int = 5) -> list[float]:
        """
        해외주식 일봉 종가를 조회합니다 → list[float] (오래된 순).

        토스 API: GET /api/v1/candles?symbol={ticker}&interval=1d&count=N
        """
        token = self._get_token()
        ticker = symbol.upper()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/candles", token,
                params={"symbol": ticker, "interval": "1d", "count": days},
            )
            result = data.get("result", {})
            candles = result.get("candles", [])

            closes = []
            for c in reversed(candles):
                try:
                    price = float(c.get("closePrice", 0))
                    if price > 0:
                        closes.append(price)
                except (ValueError, TypeError):
                    continue

            return closes[-days:]

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"일봉 종가 조회 실패: {str(e)}")

    def get_daily_candles(self, symbol: str, exchange: str, days: int = 5) -> list[dict]:
        """
        해외주식 일봉 캔들을 날짜 포함 조회합니다 → list[dict] (오래된 순).

        토스 API: GET /api/v1/candles?symbol={ticker}&interval=1d&count=N
        각 캔들의 timestamp 필드(ISO8601, KST)에서 미국 세션 날짜를 추출합니다.
        close ≤ 0 또는 날짜 파싱 불가인 캔들은 건너뜁니다.
        """
        token = self._get_token()
        ticker = symbol.upper()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/candles", token,
                params={"symbol": ticker, "interval": "1d", "count": days},
            )
            result = data.get("result", {})
            candles = result.get("candles", [])

            out: list[dict] = []
            for c in reversed(candles):
                try:
                    price = float(c.get("closePrice", 0))
                except (ValueError, TypeError):
                    continue
                if price <= 0:
                    continue

                date_str = self._parse_toss_candle_date(c.get("timestamp"))
                if date_str is None:
                    continue

                out.append({"date": date_str, "close": price})

            return out[-days:]

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"일봉 캔들 조회 실패: {str(e)}")

    @staticmethod
    def _parse_toss_candle_date(timestamp) -> str | None:
        """
        토스 캔들 timestamp(ISO8601, KST)에서 미국 세션 날짜("YYYY-MM-DD")를 추출합니다.

        토스 일봉 timestamp는 자정 ET에 해당하는 KST 시각이므로
        America/New_York으로 변환 후 날짜를 취합니다.
        파싱 실패 시 None을 반환합니다.
        """
        if not timestamp or not isinstance(timestamp, str):
            return None
        try:
            dt = datetime.fromisoformat(timestamp)
            return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        토스 API: GET /api/v1/holdings?symbol={ticker}
        해당 종목의 잔고가 없으면 None을 반환합니다.
        """
        token = self._get_token()
        ticker = symbol.upper()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/holdings", token, params={"symbol": ticker}
            )
            result = data.get("result", {})
            items = result.get("items", [])

            if not items:
                return None

            for item in items:
                if item.get("symbol", "").upper() == ticker:
                    qty = int(float(item.get("quantity", 0)))
                    avg_price = float(item.get("averagePurchasePrice", 0))
                    if qty <= 0:
                        return None
                    return Balance(quantity=qty, avg_price=avg_price)

            return None

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"잔고 조회 실패: {str(e)}")

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """
        주문 가능 금액을 조회합니다 → PurchaseAmount(orderable_cash).

        토스 API: GET /api/v1/buying-power?currency=USD
        """
        token = self._get_token()

        try:
            data = self._request_with_rate_retry(
                "GET", "/api/v1/buying-power", token, params={"currency": "USD"}
            )
            result = data.get("result", {})
            cash_buying_power = float(result.get("cashBuyingPower", 0))

            return PurchaseAmount(orderable_cash=cash_buying_power)

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"매수가능금액 조회 실패: {str(e)}")

    def _fetch_orders(
        self,
        symbol: str,
        exchange: str,
        status: str,
        days: int = 30,
        limit: int = 100,
        verbose: bool = False,
    ) -> list[dict]:
        """지정한 status(CLOSED/OPEN)의 주문을 페이지네이션으로 조회합니다.

        GET /api/v1/orders?status={status}&symbol={ticker}&from={date}&to={date}

        토스 API는 status=OPEN 을 지원합니다 (실측 2026-10-23: HTTP 200).
        status=ALL 은 지원하지 않으므로 CLOSED/OPEN 을 각각 조회해 합칩니다.
        """
        token = self._get_token()
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ticker = symbol.upper()
        from_date = start_date.strftime("%Y-%m-%d")
        to_date = now_kst.strftime("%Y-%m-%d")

        label = "미체결" if status == "OPEN" else "체결내역"
        order_history: list[dict] = []
        cursor = None
        page_no = 1
        MAX_PAGES = 20

        print(f"[주문이력] {symbol} {label} 조회: {from_date} ~ {to_date}")

        try:
            while page_no <= MAX_PAGES:
                params: dict = {
                    "status": status,
                    "symbol": ticker,
                    "from": from_date,
                    "to": to_date,
                    "limit": min(limit, 100),
                }
                if cursor:
                    params["cursor"] = cursor

                data, resp_headers = self._request_with_rate_retry(
                    "GET", "/api/v1/orders", token,
                    params=params,
                    return_headers=True,
                )

                result = data.get("result", {})
                orders = result.get("orders", [])
                next_cursor = result.get("nextCursor")

                print(f"[주문이력] {symbol} {label} 페이지 {page_no}: {len(orders)}건")

                # 평균가·체결금액이 모두 없는 행이 있으면 현재가 1회 조회로 폴백합니다.
                last_price = None
                if any(_raw_order_needs_last_price(item) for item in orders):
                    try:
                        last_price = self.get_stock_price(symbol, exchange).last
                    except Exception as price_error:
                        print(
                            f"[경고] 토스 체결금액 폴백용 현재가 조회 실패: "
                            f"{str(price_error)[:100]}"
                        )

                for item in orders:
                    normalized = self._normalize_order_item(
                        item, last_price=last_price
                    )
                    order_history.append(normalized)

                if not next_cursor:
                    break

                cursor = next_cursor
                page_no += 1
                time.sleep(self._rate_limit_wait)

        except BrokerError:
            raise
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"주문체결내역 조회 실패: {str(e)}")

        print(f"[주문이력] {symbol} {label} 총 {len(order_history)}건 조회 완료")
        return order_history

    def get_open_orders(
        self,
        symbol: str,
        exchange: str,
        days: int = 30,
        limit: int = 100,
    ) -> list[dict]:
        """미체결(열린) 주문만 조회합니다 → list[dict] (state.py 표준 필드).

        토스 `GET /api/v1/orders?status=OPEN` 은 실측으로 지원됩니다.
        열린 주문은 CLOSED 이력에 나타나지 않으므로, 신규 주문 전 중복 방지를
        위해 반드시 이 경로로 조회해야 합니다.
        """
        return self._fetch_orders(
            symbol, exchange, "OPEN", days=days, limit=limit, verbose=False
        )

    def get_order_history(
        self,
        symbol: str,
        exchange: str,
        days: int = 30,
        verbose: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """
        주문 체결 내역 + 열린 주문을 조회합니다 → list[dict].

        토스 API: GET /api/v1/orders?status=CLOSED / status=OPEN
        커서 기반 페이지네이션으로 전체 이력을 수집합니다.

        중요: CLOSED 만 조회하면 아직 살아있는 OPEN/PARTIAL 주문이 이력에
        나타나지 않아 `_cancel_open_bot_orders` 가 취소 대상을 찾지 못합니다.
        그래서 CLOSED 와 OPEN 을 모두 조회해 주문번호 기준으로 합칩니다.
        (OPEN 조회가 실패하면 CLOSED 만으로 계속 진행 — 중복 주문 방지는
         다음 실행의 OPEN 조회와 잔여 주문 취소 확인에 맡깁니다)

        각 dict는 state.py가 기대하는 표준 필드를 포함합니다:
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        closed_orders = self._fetch_orders(
            symbol, exchange, "CLOSED", days=days, limit=limit, verbose=verbose
        )
        try:
            open_orders = self._fetch_orders(
                symbol, exchange, "OPEN", days=days, limit=limit, verbose=False
            )
        except BrokerError as open_error:
            # OPEN 조회가 일시적으로 거부/실패해도 CLOSED 정산은 계속합니다.
            print(
                f"[경고] 토스 미체결 주문 조회 실패 — CLOSED 이력만 사용합니다: "
                f"{str(open_error)[:120]}"
            )
            open_orders = []

        order_history = list(closed_orders)
        seen_odnos = {str(item.get("odno", "")) for item in order_history}
        for item in open_orders:
            odno = str(item.get("odno", ""))
            if odno and odno in seen_odnos:
                continue
            order_history.append(item)
            seen_odnos.add(odno)

        # Human-friendly summary (verbose 모드)
        if verbose and order_history:
            n = min(limit if limit and limit > 0 else 100, len(order_history))
            print(f"[주문이력 요약] {symbol} 최근 {n}건")
            for item in order_history[:n]:
                kst_iso = item.get("ord_datetime_kst")
                if kst_iso:
                    try:
                        kst_dt = datetime.fromisoformat(kst_iso)
                        kst_str = kst_dt.strftime("%Y-%m-%d %H:%M:%S") + " KST"
                    except Exception:
                        kst_str = kst_iso
                else:
                    kst_str = "(시간없음)"

                odno = item.get("odno", "")
                side = item.get("sll_buy_dvsn_cd_name", "")
                qty = item.get("ft_ccld_qty", "0")
                price = item.get("ft_ccld_unpr3", "0")
                amt = item.get("ft_ccld_amt3", "0")
                status = item.get("prcs_stat_name", "")
                try:
                    price_s = f"{float(price):.2f}"
                except Exception:
                    price_s = price
                try:
                    amt_s = f"{float(amt):.2f}"
                except Exception:
                    amt_s = amt

                print(
                    f"{kst_str} | odno={mask_odno(odno)} | {side} | status={status} | "
                    f"qty={qty} | price={price_s} | amt={amt_s}"
                )

        return order_history

    # ═══════════════════════════════════════════════════════════════════
    # 주문 API
    # ═══════════════════════════════════════════════════════════════════

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

        토스 API: POST /api/v1/orders

        - DRY 모드는 DryBroker가 처리하므로, 이 메서드는 항상 LIVE로 동작합니다.
        - 토스는 모의투자가 없으므로 모든 주문은 실전 계좌에 즉시 실행됩니다.
        - 예약주문은 별도 API(conditional-orders)가 있지만 현재 사용하지 않음.

        exchange 규약 (KIS place_order와 동일):
          - place_order는 호출 측에서 broker.exchange_code()로 변환된
            거래소 코드를 받습니다.
        """
        token = self._get_token()

        # 모의투자 미지원 주문 유형 자동 변환 (기존 코드 호환)
        # 토스는 모의투자가 없으므로 이 블록은 사실상 동작하지 않지만,
        # 기존 파이프라인 호환을 위해 유지
        from config import BROKER_MODE
        if BROKER_MODE != "real" and order_type in _DEMO_UNSUPPORTED_ORDER_TYPES:
            print(f"토스 미지원 주문 유형: {order_type} → LIMIT(지정가)으로 자동 변환합니다.")
            order_type = "LIMIT"

        # 브로커 호가 단위 규칙에 맞춰 주문가를 정규화 ($1+ → 소수점 2자리 등)
        price = normalize_order_price(price)

        # 토스는 MOC(장마감시장가)를 지원하지 않습니다.
        # 리버스모드 1일차 MOC 매도를 LOC(장마감지정가)로 변환해 종가 체결을 구현합니다.
        # SELL MOC에 $0.01 제한가를 걸면 어떤 종가에서도 충족되므로 항상 종가에 체결됩니다.
        # (BUY MOC는 $0.01 LOC로는 체결될 수 없으므로 변환하지 않습니다.)
        if order_type == "MOC" and side == "SELL":
            print("⚠️  토스 MOC 미지원: SELL MOC → LOC($0.01)으로 변환 (종가 체결)")
            order_type = "LOC"
            price = _MOC_SELL_PROXY_PRICE

        order_mapping = _TOSS_ORDER_TYPE_MAP.get(order_type)
        if order_mapping is None:
            raise OrderNotAcceptedError(f"지원하지 않는 주문 유형입니다: {order_type}")

        toss_order_type, time_in_force = order_mapping

        # 토스 주문 생성 요청
        order_body = {
            "symbol": symbol.upper(),
            "side": side,
            "orderType": toss_order_type,
            "timeInForce": time_in_force,
            "quantity": quantity,
            "price": price,
        }

        try:
            data = self._request_with_rate_retry(
                "POST", "/api/v1/orders", token, json_body=order_body,
                retry_network=False,
            )
            result = data.get("result", {})
            order_id = result.get("orderId", "")

            print("\n========== [LIVE 모드] 주문 성공 ==========")
            print(f"종목 코드: {symbol}")
            print(f"주문번호: {mask_odno(order_id)}")
            print(f"주문수량: {quantity}주")
            print(f"주문가격: ${price}")
            print("==========================================\n")

            return OrderResult(
                order_id=order_id,
                order_time=get_kst_now().strftime("%Y%m%d%H%M%S"),
                is_reservation=False,
            )

        except AuthError as e:
            # 토큰/인증 오류 → 접수 여부 불확실 (보수적으로 fence 유지)
            raise OrderError(str(e)) from e
        except BrokerError as e:
            # error envelope 거부 응답 또는 rate-limit 재시도 초과 → 주문 미접수가 확정
            raise OrderNotAcceptedError(str(e)) from e
        except requests.exceptions.RequestException as e:
            # 네트워크 타임아웃/연결 오류 → 접수 여부 불확실
            raise OrderError(f"주문 실행 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════
    # 주문 취소
    # ═══════════════════════════════════════════════════════════════════

    def cancel_order(
        self,
        odno: str,
        symbol: str | None = None,
        exchange: str | None = None,
    ) -> None:
        """
        토스 주문을 취소합니다.

        POST /api/v1/orders/{orderId}/cancel (json_body={})

        - 이미 취소된 주문은 idempotent하게 성공으로 처리
        - 취소 불가능한 주문(이미 체결, 취소 제한 등)은 OrderNotAcceptedError 발생
        - 처리 중인 주문은 1회 재시도
        """
        odno = str(odno).strip() if odno else ""
        if not odno:
            raise OrderNotAcceptedError("취소할 주문번호가 비어있습니다")

        token = self._get_token()
        path = f"/api/v1/orders/{odno}/cancel"

        MAX_NETWORK_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_NETWORK_RETRIES:
            try:
                time.sleep(self._rate_limit_wait)
                resp = self._session.post(path, token, json_body={})

                # rate-limit (429) 처리
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "1")
                    try:
                        wait_time = float(retry_after)
                    except (ValueError, TypeError):
                        wait_time = 1.0
                    wait_time = max(wait_time, 1.0)
                    print(f"토스 rate-limit 초과 (429), {wait_time:.1f}초 후 재시도...")
                    time.sleep(wait_time)
                    continue

                # 응답 바디 파싱
                try:
                    body = resp.json()
                except (ValueError, TypeError):
                    body = {}

                # 성공: 200 + error envelope 없음
                if resp.status_code == 200 and "error" not in body:
                    print(f"[토스] 주문 취소 성공: odno={mask_odno(odno)}")
                    return None

                # error envelope 처리
                error = body.get("error", {})
                code = error.get("code", "")
                message = error.get("message", "")

                # 인증 오류 → OrderError (접수 여부 불확실)
                if code in ("invalid-token", "expired-token", "edge-blocked"):
                    raise OrderError(f"토스 인증 오류 [{code}]: {message}")

                # 이미 취소됨 (409) → idempotent 성공
                if resp.status_code == 409 and code == "already-canceled":
                    print(f"[토스] 주문 이미 취소됨 (idempotent): odno={mask_odno(odno)}")
                    return None

                # 처리 중 → 1회 재시도
                if code == "already-processing":
                    retry_after = 1.0
                    data = error.get("data") or {}
                    try:
                        retry_after = float(data.get("retryAfterSeconds", 1.0))
                    except (ValueError, TypeError):
                        retry_after = 1.0
                    print(
                        f"[토스] 주문 처리 중 (already-processing), "
                        f"{retry_after:.1f}초 후 재시도: odno={mask_odno(odno)}"
                    )
                    time.sleep(retry_after)
                    # 1회 재시도
                    time.sleep(self._rate_limit_wait)
                    resp2 = self._session.post(path, token, json_body={})
                    try:
                        body2 = resp2.json()
                    except (ValueError, TypeError):
                        body2 = {}
                    if resp2.status_code == 200 and "error" not in body2:
                        print(f"[토스] 주문 취소 성공 (재시도): odno={mask_odno(odno)}")
                        return None
                    error2 = body2.get("error", {})
                    code2 = error2.get("code", "")
                    msg2 = error2.get("message", "")
                    if code2 == "already-canceled":
                        print(f"[토스] 주문 이미 취소됨 (idempotent): odno={mask_odno(odno)}")
                        return None
                    if code2 in (
                        "already-filled", "cancel-restricted",
                        "order-hours-closed", "order-not-found",
                    ):
                        raise OrderNotAcceptedError(
                            f"토스 주문 취소 불가 [{code2}]: {msg2}"
                        )
                    raise OrderError(f"토스 주문 취소 실패 [{code2}]: {msg2}")

                # 확정적 취소 불가
                if code in (
                    "already-filled", "cancel-restricted",
                    "order-hours-closed", "order-not-found",
                ):
                    raise OrderNotAcceptedError(
                        f"토스 주문 취소 불가 [{code}]: {message}"
                    )

                # 기타 오류
                if code:
                    raise OrderError(f"토스 주문 취소 실패 [{code}]: {message}")

                raise OrderError(
                    f"토스 주문 취소 실패: HTTP {resp.status_code}"
                )

            except AuthError as e:
                raise OrderError(str(e)) from e
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                network_retry_count += 1
                if network_retry_count <= MAX_NETWORK_RETRIES:
                    wait = (
                        min(30, 2 ** network_retry_count)
                        * random.uniform(0.75, 1.25)
                    )
                    print(f"토스 API 타임아웃: {str(e)[:60]}...")
                    print(
                        f"   {wait:.1f}초 후 재시도... "
                        f"({network_retry_count}/{MAX_NETWORK_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                raise OrderError(f"토스 API 호출 실패 (네트워크): {str(e)}")

        raise OrderError("토스 API 호출 실패: 재시도 한도 초과")

    # ═══════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → 'NASDAQ')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
