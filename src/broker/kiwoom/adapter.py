"""
KiwoomBroker — 키움증권 Broker 구현체.

키움증권 미국주식 REST API를 Broker 인터페이스로 구현합니다.
- 모든 TR은 POST 방식 (tr_registry.py 참고)
- 인증: Authorization Bearer 헤더 + api-id 헤더
- 응답 검증: return_code == 0 (정수)
- 숫자 필드는 문자열로 반환되므로 float()/int() 변환 필요
- 모의투자(demo)도 지원 (2026.07.02부터 미국주식 모의 지원)

사용법:
    broker = KiwoomBroker()
    price = broker.get_stock_price("TQQQ", "NAS")
    result = broker.place_order("TQQQ", "NAS", "BUY", 10, 50.0, "LOC")

DRY 모드는 DryBroker 래퍼로 처리 — KiwoomBroker 자체는 항상 LIVE로 동작.
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
    OrderError,
)
from broker.market_utils import get_kst_now, is_us_trading_day
from broker.kiwoom.session import KiwoomSession
from broker.kiwoom.auth import get_access_token
from broker.kiwoom.exchange import get_api_exchange_code
from broker.kiwoom.tr_registry import (
    TR_PRICE,
    TR_ORDERBOOK,
    TR_BALANCE,
    TR_HISTORY,
    TR_HISTORY_DEMO,
    TR_DEPOSIT,
    TR_BUY,
    TR_SELL,
)


# ── 키움증권 주문 유형 코드 매핑 ─────────────────────────────────────
# TODO: 실전 응답 확인 후 코드값 보정 필요
_KIWOOM_ORDER_TYPE_MAP = {
    "LIMIT": "00",  # 지정가
    "LOC":   "30",  # 장마감지정가
    "LOO":   "32",  # 장개시지정가
    "MOO":   "31",  # 장개시시장가
    "MOC":   "33",  # 장마감시장가
}

# 모의투자에서 지원하지 않는 주문 유형 — LIMIT으로 자동 변환
_DEMO_UNSUPPORTED_ORDER_TYPES = {"LOC", "LOO", "MOO", "MOC"}


def _parse_price(value, default: float = 0.0) -> float:
    """
    키움 가격 문자열을 float로 안전 변환합니다.

    키움 API는 가격 필드를 '부호 포함 문자열'로 반환합니다:
      - "-73.78" → 실제 가격 73.78 (앞 부호는 전일대비 등락 방향, 실제값은 절대값)
      - "+90800" → 90800
      - "" / None → 장외 시간 등 미가용 → default
    레거시 OpenAPI+의 2중 부호("--4500")도 lstrip으로 방어합니다.
    """
    if value is None:
        return default
    s = str(value).strip().replace(",", "")
    if s == "":
        return default
    s = s.lstrip("+-")
    if s == "":
        return default
    try:
        return abs(float(s))
    except ValueError:
        return default


class KiwoomBroker(Broker):
    """
    키움증권 미국주식 REST API Broker 구현체.

    config.py의 BROKER_CONFIG["kiwoom"]에서 설정을 읽습니다.
    BROKER_MODE에 따라 실전(real) / 모의(demo) 도메인이 결정됩니다.
    """

    def __init__(self):
        from config import BROKER_CONFIG, BROKER_MODE, HTTP_TIMEOUT

        self._mode = BROKER_MODE  # "real" or "demo"
        self._domain = BROKER_CONFIG["domain"]
        self._app_key = BROKER_CONFIG["app_key"]
        self._app_secret = BROKER_CONFIG["app_secret"]
        self._account_no = BROKER_CONFIG["account_no"]
        self._session = KiwoomSession(self._domain, timeout=HTTP_TIMEOUT)

        # rate limit: 실전 10회/초, 모의 1회/초
        self._rate_limit_wait = 0.1 if self._mode == "real" else 1.0

    # ═══════════════════════════════════════════════════════════════════
    # 속성
    # ═══════════════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """증권사 식별자"""
        return "kiwoom"

    # ═══════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (auth.get_access_token 캐싱 포함)."""
        try:
            return get_access_token(
                domain=self._domain,
                app_key=self._app_key,
                app_secret=self._app_secret,
                timeout=self._session.timeout,
            )
        except Exception as e:
            raise BrokerError(f"키움 토큰 획득 실패: {str(e)}")

    def _check_account(self):
        """계좌번호 설정 여부를 검증합니다."""
        if not self._account_no:
            raise BrokerError(
                "KIWOOM_ACCOUNT_NO가 설정되어 있지 않습니다. "
                "환경변수 또는 .env 파일에 KIWOOM_ACCOUNT_NO를 추가하세요."
            )

    def _check_response(self, resp: requests.Response) -> dict:
        """
        키움 공통 응답 검증: return_code == 0.

        Raises:
            BrokerError: return_code가 0이 아니거나 HTTP 오류인 경우
        """
        resp.raise_for_status()
        body = resp.json()

        return_code = body.get("return_code")
        if return_code is None or return_code != 0:
            msg = body.get("return_msg", "알 수 없는 오류")
            raise BrokerError(f"키움 API 오류 (return_code={return_code}): {msg}")

        return body

    def _request_with_rate_retry(self, tr_id: str, body: dict, token: str) -> dict:
        """
        키움 API 요청 래퍼 — rate-limit/타임아웃 재시도 포함.

        - rate-limit(HTTP 429 또는 return_code != 0): fixed-wait 후 재시도 (최대 3회)
        - 타임아웃(ConnectTimeout, ReadTimeout): 지수 백오프 + jitter 후 재시도 (최대 3회)

        항상 dict(파싱된 JSON)를 반환하거나 예외를 발생시킵니다.
        """
        MAX_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_RETRIES:
            try:
                for _ in range(MAX_RETRIES + 1):
                    resp = self._session.request_with_tr(tr_id, body, token)
                    try:
                        resp.raise_for_status()
                    except requests.exceptions.HTTPError:
                        # rate-limit 감지 (HTTP 429 또는 응답 내 오류코드)
                        try:
                            data = resp.json()
                            return_code = data.get("return_code")
                            return_msg = data.get("return_msg", "")
                        except Exception:
                            return_code = None
                            return_msg = ""

                        is_rate_limit = (
                            resp.status_code == 429
                            or (return_code is not None and return_code != 0
                                and "초당" in return_msg)
                        )

                        if is_rate_limit:
                            time.sleep(self._rate_limit_wait)
                            continue
                        raise

                    return self._check_response(resp)

                raise BrokerError("API 호출 실패: 초당 호출 제한 재시도 초과")

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f"⏳ 타임아웃/연결 오류 발생: {str(e)[:60]}...")
                    print(f"   {wait:.1f}초 후 재시도합니다... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise

        # 도달 불가능 — 루프는 항상 return 또는 raise로 종료
        raise BrokerError("API 호출 실패: 재시도 한도 초과")

    def _normalize_order_item(self, raw: dict) -> dict:
        """
        키움 응답 필드를 state.py가 기대하는 표준 필드명으로 변환.

        TODO: 실전 응답 확인 후 키움의 실제 응답 필드명으로 매핑 보정 필요.
        현재는 KIS와 동일한 필드명을 가정 (한국 증권사 API는 유사한 필드명 사용).
        """
        ord_dt = raw.get("ord_dt", "")
        ord_tmd_raw = raw.get("ord_tmd", "")
        ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""

        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None
        if ord_dt and ord_tmd:
            try:
                kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                ord_datetime_kst_iso = kst_dt.isoformat()
                ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except Exception:
                pass

        return {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd_raw,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "prdt_name": raw.get("prdt_name", ""),
            "sll_buy_dvsn_cd_name": raw.get("sll_buy_dvsn_cd_name", ""),
            "ft_ord_qty": raw.get("ft_ord_qty", "0"),
            "ft_ccld_qty": raw.get("ft_ccld_qty", "0"),
            # 키움 가격/금액 필드는 부호 포함 문자열 → 절대값 문자열로 정규화
            "ft_ccld_unpr3": str(_parse_price(raw.get("ft_ccld_unpr3"))),
            "ft_ccld_amt3": str(_parse_price(raw.get("ft_ccld_amt3"))),
            "nccs_qty": raw.get("nccs_qty", "0"),
            "prcs_stat_name": raw.get("prcs_stat_name", ""),
            "tr_mket_name": raw.get("tr_mket_name", ""),
            "tr_crcy_cd": raw.get("tr_crcy_cd", "USD"),
            "odno": raw.get("odno", ""),
            "ovrs_excg_cd": raw.get("ovrs_excg_cd", ""),
        }

    def _normalize_ust21150_item(self, raw: dict, ord_dt: str = "") -> dict:
        """
        ust21150(일별 주문체결내역) 응답을 state.py 표준 필드로 변환.

        ust21150은 모의투자(mockapi)에서 ust21100(거래내역) 미지원을 대체하기 위한 TR.
        ust21150 응답에는 ord_dt가 없으므로 요청 파라미터에서 주입받아 사용.

        참고: state.py는 ft_ccld_qty > 0으로 체결 여부 판단 (prcs_stat_name 사용 안 함).
        """
        # ord_dt는 요청에서 주입 (응답에 없음)
        ord_dt = ord_dt or raw.get("ord_dt", "")
        ord_time_raw = raw.get("ord_time", "")  # HH:mm:ss
        # HHMMSS로 변환 (콜론 제거)
        ord_tmd = ord_time_raw.replace(":", "") if ord_time_raw else ""

        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None
        if ord_dt and ord_tmd:
            try:
                kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                ord_datetime_kst_iso = kst_dt.isoformat()
                ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except Exception:
                pass

        # 체결금액 계산 (cntr_amt가 응답에 없으므로 cntr_qty × cntr_uv로 계산)
        # 키움 가격 필드는 부호 포함 문자열이므로 _parse_price()로 절대값 변환.
        cntr_qty = int(_parse_price(raw.get("cntr_qty")))
        cntr_uv = _parse_price(raw.get("cntr_uv"))
        ft_ccld_amt3 = str(cntr_qty * cntr_uv)

        return {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "prdt_name": raw.get("frgn_stk_nm", ""),
            "sll_buy_dvsn_cd_name": raw.get("slby_tp_nm", ""),
            "ft_ord_qty": raw.get("ord_qty", "0"),
            "ft_ccld_qty": str(cntr_qty),
            "ft_ccld_unpr3": str(cntr_uv),
            "ft_ccld_amt3": ft_ccld_amt3,
            "nccs_qty": raw.get("ord_remnq", "0"),
            "prcs_stat_name": raw.get("ord_stat_nm", ""),
            "tr_mket_name": raw.get("stex_nm", ""),
            "tr_crcy_cd": raw.get("crnc_code", "USD"),
            "odno": raw.get("ord_no", ""),
            "ovrs_excg_cd": "",  # ust21150에 없음
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

        TR: usa20100 (현재가)
        키움은 가격을 '부호 포함 문자열'로 반환하므로 _parse_price()로 변환합니다.
        장외 시간에는 open_pric이 빈 문자열로 올 수 있습니다.
        """
        token = self._get_token()
        api_exch = get_api_exchange_code(exchange)

        body = {
            "stk_cd": symbol.upper(),
            "stex_tp": api_exch,
        }

        try:
            data = self._request_with_rate_retry(TR_PRICE, body, token)
            # 키움 응답은 flat 구조(output 중첩 없이 최상위에 필드)이므로,
            # output 키가 있으면 그 값을, 없으면 data 자체를 사용.
            output = data.get("output", data)
            return StockPrice(
                open=_parse_price(output.get("open_pric")),
                last=_parse_price(output.get("cur_prc")),
            )
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재가 조회 실패: {str(e)}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
        현재가 조회 + 주문 가능 여부 → StockQuotation(tradable, last).

        TR: usa20101 (10호가)
        - cur_prc: 현재가 (부호 포함 문자열 → _parse_price()로 절대값 변환)

        참고: usa20101 응답에는 주문가능여부 전용 필드가 없음 (trd_susp_tp는 usa20100에만 존재).
        tradable은 현재 주문 결정을 제어하지 않으므로 안전 기본값 True 사용.
        주문 시도 시 API가 거부하면 그때 처리.
        """
        token = self._get_token()
        api_exch = get_api_exchange_code(exchange)

        body = {
            "stk_cd": symbol.upper(),
            "stex_tp": api_exch,
        }

        try:
            data = self._request_with_rate_retry(TR_ORDERBOOK, body, token)
            # 키움 응답은 flat 구조(output 중첩 없이 최상위에 필드)이므로,
            # output 키가 있으면 그 값을, 없으면 data 자체를 사용.
            output = data.get("output", data)
            return StockQuotation(
                tradable=True,
                last=_parse_price(output.get("cur_prc")),
            )
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"호가 조회 실패: {str(e)}")

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        TR: ust21070 (잔고)
        해당 종목의 잔고가 없으면 None을 반환합니다.
        """
        self._check_account()
        token = self._get_token()
        api_exch = get_api_exchange_code(exchange)

        body = {
            "stex_tp": api_exch,
            "stk_cd": symbol.upper(),
        }

        try:
            data = self._request_with_rate_retry(TR_BALANCE, body, token)
            # 키움 응답은 result_list 사용 (output이 아님). result_list가 없으면 output fallback.
            output = data.get("result_list", data.get("output", []))
            if not output:
                return None

            # 종목 코드로 해당 항목 찾기
            for item in output:
                stk_cd = item.get("stk_cd", "").upper()
                if symbol.upper() in stk_cd:
                    return Balance(
                        quantity=int(_parse_price(item.get("poss_qty"))),
                        avg_price=_parse_price(item.get("frgn_stk_book_uv")),
                    )

            return None

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"잔고 조회 실패: {str(e)}")

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """
        주문 가능 금액을 조회합니다 → PurchaseAmount(orderable_cash).

        TR: ust21110 (예수금)
        계좌의 외화(USD) 주문가능금액을 반환합니다.
        응답 result_list[].fc_ord_alowa 필드 사용 (모의투자 검증 완료).
        모의투자에서 외화 예수금이 없으면 빈 result_list → 0.0 반환.
        """
        self._check_account()
        token = self._get_token()

        body = {}

        try:
            data = self._request_with_rate_retry(TR_DEPOSIT, body, token)
            # 키움 예수금 TR 응답: result_list (output이 아님).
            # 모의투자에서는 result_list가 빈 리스트일 수 있음 (해당조회내역 없음).
            result_list = data.get("result_list", [])
            if not result_list:
                # 모의투자 등에서 외화 예수금 데이터가 없는 경우 0 반환
                return PurchaseAmount(orderable_cash=0.0)

            # result_list에서 외화 주문가능금액 추출.
            # 모의투자 응답으로 검증 완료: result_list[0].fc_ord_alowa (USD 주문가능금액).
            item = result_list[0] if isinstance(result_list, list) else result_list
            return PurchaseAmount(
                orderable_cash=_parse_price(item.get("fc_ord_alowa")),
            )

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
        해외주식 주문 체결 내역을 조회합니다 → list[dict].

        TR:
        - 실전: ust21100 (거래내역)
        - 모의: ust21150 (일별 주문체결내역 — ust21100/ust21180 미지원 대안)

        각 dict는 state.py가 기대하는 표준 필드를 포함합니다:
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd

        TODO: 키움의 연속조회(페이지네이션) 지원 필요 시 추가 구현.
        # 참고: 모의투자(mockapi.kiwoom.com)에서는 거래내역 TR(ust21100)과
        # 기간별 주문내역 TR(ust21180)이 "해당업무가 제공되지 않습니다" 오류로 지원되지 않음.
        # 실전(api.kiwoom.com)에서만 동작.
        # 모의투자에서는 ust21150(일별 주문체결내역)을 사용.
        """
        self._check_account()
        token = self._get_token()

        # 날짜 계산 (KST 기준)
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ord_end_dt = now_kst.strftime("%Y%m%d")
        ord_strt_dt = start_date.strftime("%Y%m%d")

        api_exch = get_api_exchange_code(exchange)

        is_demo = (self._mode == "demo")

        order_history = []

        if is_demo:
            # 모의투자: ust21150(일별 주문체결내역) — ust21100/ust21180 미지원 대안
            # ust21150는 일별 조회이므로 days 범위를 순회하며 각 날짜별로 조회 후 합침
            tr_id = TR_HISTORY_DEMO
            from datetime import timedelta as _td
            date_list = []
            cur = start_date
            while cur <= now_kst:
                date_list.append(cur.strftime("%Y%m%d"))
                cur = cur + _td(days=1)

            print(f"[주문이력] {symbol} 체결내역 조회(ust21150, 모의투자): {ord_strt_dt}~{ord_end_dt} ({len(date_list)}일)")

            for dt in date_list:
                body = {
                    "ord_dt": dt,
                    "query_tp": "1",   # 주문순
                    "slby_tp": "0",    # 전체 (매도+매수)
                    "stk_cd": symbol.upper(),
                    "stex_tp": api_exch,
                }
                try:
                    data = self._request_with_rate_retry(tr_id, body, token)
                    raw_items = data.get("result_list", [])
                    for item in raw_items:
                        normalized = self._normalize_ust21150_item(item, ord_dt=dt)
                        order_history.append(normalized)
                except BrokerError as e:
                    # 빈 결과(501724)는 정상이므로 조용히 건너뛰고, 그 외 실패만 로깅.
                    msg = str(e)
                    if "501724" in msg or "관련자료가 없습니다" in msg:
                        pass  # 해당 날짜 체결내역 없음 — 정상
                    else:
                        print(f"[주문이력] {dt} 조회 실패: {msg[:80]}")
                time.sleep(self._rate_limit_wait)  # 모의투자 1회/초 rate-limit

            print(f"[주문이력] {symbol} 체결내역 총 {len(order_history)}건 조회 완료")
        else:
            # 실전: ust21100(거래내역)
            tr_id = TR_HISTORY
            body = {
                "tp": "3",                   # 구분: 매매 (ust21100 필수 — 공식 문서는 선택이나 실서버 강제)
                "krw_repl_skip_yn": "Y",  # 원화대용입출금제외여부: KRW 대체거래 제외 (미국주식은 KRW 대체거래 없음, ust21100 필수 — 공식 문서는 선택이나 실서버 강제)
                "stk_cd": symbol.upper(),
                "stex_tp": api_exch,
                "strt_dt": ord_strt_dt,
                "end_dt": ord_end_dt,
            }
            print(f"[주문이력] {symbol} 체결내역 조회(ust21100, 실전): {ord_strt_dt}~{ord_end_dt}")

            try:
                data = self._request_with_rate_retry(tr_id, body, token)
                raw_items = data.get("result_list", data.get("output", []))

                for item in raw_items:
                    normalized = self._normalize_order_item(item)
                    order_history.append(normalized)

            except BrokerError as e:
                # 키움은 빈 결과를 return_code=20 + 501724(관련자료가 없습니다)로 반환.
                # 빈 결과는 정상(체결 이력 없음)이므로 빈 리스트로 처리하고 T=0 유지.
                msg = str(e)
                if "501724" in msg or "관련자료가 없습니다" in msg:
                    print(f"[주문이력] {symbol} 체결내역 없음 (빈 결과)")
                    order_history = []
                else:
                    raise
            except requests.exceptions.RequestException as e:
                raise BrokerError(f"주문체결내역 조회 실패: {str(e)}")

            print(f"[주문이력] {symbol} 체결내역 총 {len(order_history)}건 조회 완료")

        # Human-friendly summary (optional) — 공통 처리 (모의/실전 모두)
        if verbose and order_history:
            try:
                n = int(limit) if limit and int(limit) > 0 else 100
            except Exception:
                n = 100
            n = min(n, len(order_history))

            print(f"[주문이력 요약] {symbol} 최근 {n}건 (간단 요약)")
            for item in order_history[:n]:
                kst_iso = item.get("ord_datetime_kst")
                if kst_iso:
                    try:
                        kst_dt = datetime.fromisoformat(kst_iso)
                        kst_str = kst_dt.strftime("%Y-%m-%d %H:%M:%S") + " KST"
                    except Exception:
                        kst_str = kst_iso
                else:
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd = (item.get("ord_tmd") or "").zfill(6)
                    if ord_dt and len(ord_dt) == 8 and ord_tmd:
                        try:
                            kst_str = (
                                f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]} "
                                f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]} KST"
                            )
                        except Exception:
                            kst_str = f"{ord_dt} {ord_tmd}"
                    else:
                        kst_str = "(시간없음)"

                odno = item.get("odno", "")
                side = item.get("sll_buy_dvsn_cd_name", "")
                qty = item.get("ft_ccld_qty", "0")
                price = item.get("ft_ccld_unpr3", "0")
                amt = item.get("ft_ccld_amt3", "0")

                try:
                    price_s = f"{_parse_price(price):.2f}"
                except Exception:
                    price_s = price
                try:
                    amt_s = f"{_parse_price(amt):.2f}"
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

        TR: ust20000 (매수) / ust20001 (매도)

        - 모의투자 미지원 주문 유형(LOC 등)은 LIMIT으로 자동 변환합니다.
        - DRY 모드는 DryBroker가 처리하므로, 이 메서드는 항상 LIVE로 동작합니다.
        - 키움 REST API는 예약 주문을 지원하지 않습니다 (ust20000/ust20001 body에
          예약 주문 파라미터가 없고, 예약 주문 전용 TR도 없음).
          따라서 OrderResult.is_reservation은 항상 False입니다.
          예약 주문은 영웅문 HTS/모바일앱에서만 가능합니다.
        - 장 마감 후(미국 장 시간 외, ET 09:30~16:00 외) 주문 시 RC4058 오류 발생.

        Returns:
            OrderResult: 주문 성공 시 (주문번호, 시각, 예약여부=False)
        """
        self._check_account()
        token = self._get_token()

        # 모의투자 미지원 주문 유형 자동 변환
        if self._mode != "real" and order_type in _DEMO_UNSUPPORTED_ORDER_TYPES:
            print(f"⚠️  모의투자 미지원 주문 유형: {order_type} → LIMIT(지정가)으로 자동 변환합니다.")
            order_type = "LIMIT"

        ord_dvsn_cd = _KIWOOM_ORDER_TYPE_MAP.get(order_type)
        if ord_dvsn_cd is None:
            raise OrderError(f"지원하지 않는 주문 유형입니다: {order_type}")

        tr_id = TR_BUY if side == "BUY" else TR_SELL
        api_exch = get_api_exchange_code(exchange)

        body = {
            "stk_cd": symbol.upper(),
            "stex_tp": api_exch,
            "ord_qty": str(quantity),
            "ord_uv": str(price),
            "trde_tp": ord_dvsn_cd,
        }

        try:
            data = self._request_with_rate_retry(tr_id, body, token)
            output = data.get("output", {})

            print("\n========== [LIVE 모드] 주문 성공 ==========")
            print(f"종목 코드: {symbol}")
            print(f"주문번호: {output.get('ODNO', '')}")
            print(f"주문시각: {output.get('ORD_TMD', '')}")
            print(f"주문수량: {quantity}주")
            print(f"주문가격: ${price}")
            print("==========================================\n")

            return OrderResult(
                order_id=output.get("ODNO", ""),
                order_time=output.get("ORD_TMD", ""),
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
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → 'ND')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
