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
)
from broker.market_utils import get_kst_now, is_us_trading_day
from broker.toss.auth import get_access_token
from broker.toss.session import TossSession
from broker.toss.exchange import get_api_exchange_code


# ── 주문 방향 매핑 (Toss API "BUY"/"SELL" → state.py "매수"/"매도") ──
_SIDE_MAP = {"BUY": "매수", "SELL": "매도"}

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


class TossBroker(Broker):
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

                    resp.raise_for_status()
                    body = resp.json()

                    # 토스 공통 에러 envelope 처리
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
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f"토스 API 타임아웃: {str(e)[:60]}...")
                    print(f"   {wait:.1f}초 후 재시도... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise BrokerError(f"토스 API 호출 실패 (네트워크): {str(e)}")

        raise BrokerError("API 호출 실패: 재시도 한도 초과")

    def _normalize_order_item(self, raw: dict) -> dict:
        """
        토스 주문 응답(Order 모델)을 state.py 표준 필드로 변환.

        핵심 변환:
        - side: "BUY"/"SELL" → "매수"/"매도"
        - orderedAt: ISO 8601 → ord_dt(YYYYMMDD), ord_tmd(HHMMSS)
        - execution.filledQuantity → ft_ccld_qty
        - execution.averagePrice → ft_ccld_unpr3
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

        # 체결 정보 추출
        execution = raw.get("execution", {}) or {}
        filled_qty = int(float(execution.get("filledQuantity", 0)))
        avg_price = float(execution.get("averagePrice", 0)) if execution.get("averagePrice") else 0.0
        quantity = int(float(raw.get("quantity", 0)))
        filled_amt = str(filled_qty * avg_price) if filled_qty > 0 and avg_price > 0 else "0"

        return {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "prdt_name": raw.get("symbol", ""),
            "sll_buy_dvsn_cd_name": sll_buy_dvsn_cd_name,
            "ft_ord_qty": str(quantity),
            "ft_ccld_qty": str(filled_qty),
            "ft_ccld_unpr3": str(avg_price),
            "ft_ccld_amt3": filled_amt,
            "nccs_qty": str(quantity - filled_qty),
            "prcs_stat_name": raw.get("status", ""),
            "tr_mket_name": "",
            "tr_crcy_cd": "USD",
            "odno": raw.get("orderId", ""),
            "ovrs_excg_cd": "",
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

    def get_order_history(
        self,
        symbol: str,
        exchange: str,
        days: int = 30,
        verbose: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """
        주문 체결 내역을 조회합니다 → list[dict].

        토스 API: GET /api/v1/orders?status=CLOSED&symbol={ticker}&from={date}&to={date}
        커서 기반 페이지네이션으로 전체 체결 이력을 수집합니다.

        각 dict는 state.py가 기대하는 표준 필드를 포함합니다:
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        token = self._get_token()
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)

        ticker = symbol.upper()

        # 토스 API 날짜 형식: YYYY-MM-DD
        from_date = start_date.strftime("%Y-%m-%d")
        to_date = now_kst.strftime("%Y-%m-%d")

        order_history: list[dict] = []
        cursor = None
        page_no = 1
        MAX_PAGES = 20

        print(f"[주문이력] {symbol} 체결내역 조회: {from_date} ~ {to_date}")

        try:
            while page_no <= MAX_PAGES:
                params: dict = {
                    "status": "CLOSED",
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

                print(f"[주문이력] {symbol} 페이지 {page_no} 조회 성공: {len(orders)}건")

                for item in orders:
                    normalized = self._normalize_order_item(item)
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

        print(f"[주문이력] {symbol} 총 {len(order_history)}건 조회 완료")

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
                try:
                    price_s = f"{float(price):.2f}"
                except Exception:
                    price_s = price
                try:
                    amt_s = f"{float(amt):.2f}"
                except Exception:
                    amt_s = amt

                print(f"{kst_str} | odno={odno} | {side} | qty={qty} | price={price_s} | amt={amt_s}")

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

        order_mapping = _TOSS_ORDER_TYPE_MAP.get(order_type)
        if order_mapping is None:
            raise OrderError(f"지원하지 않는 주문 유형입니다: {order_type}")

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
                "POST", "/api/v1/orders", token, json_body=order_body
            )
            result = data.get("result", {})
            order_id = result.get("orderId", "")

            print("\n========== [LIVE 모드] 주문 성공 ==========")
            print(f"종목 코드: {symbol}")
            print(f"주문번호: {order_id}")
            print(f"주문수량: {quantity}주")
            print(f"주문가격: ${price}")
            print("==========================================\n")

            return OrderResult(
                order_id=order_id,
                order_time=get_kst_now().strftime("%Y%m%d%H%M%S"),
                is_reservation=False,
            )

        except BrokerError as e:
            raise OrderError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise OrderError(f"주문 실행 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → 'NASDAQ')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
