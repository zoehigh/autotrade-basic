"""
KISBroker — 한국투자증권 Broker 구현체.

기존 src/trader.py의 모든 API 호출을 Broker 인터페이스로 통합.
- session, trade_mode, ReservationOrderRequired를 내부화
- 조회 API는 dataclass 반환 (StockPrice, StockQuotation, Balance, PurchaseAmount)
- 주문 API는 예약주문을 내부 처리하고 OrderResult 반환
- 주문 이력은 기존 dict 형식 유지 (state.py 호환)
"""
import random
import requests
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

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
from broker.kis.session import KISSession
from broker.kis.auth import get_access_token
from broker.kis.exchange import convert_exchange_code, get_api_exchange_code
from broker.kis.order_types import (
    get_ord_dvsn,
    select_tr_id,
    get_reservation_tr_id,
    TR_ID_PRICE_DETAIL,
    TR_ID_QUOTATION,
    TR_ID_BALANCE,
    TR_ID_PURCHASE_AMOUNT,
    TR_ID_ORDER_HISTORY,
    TR_ID_BUY_ORDER,
    TR_ID_SELL_ORDER,
    DEMO_UNSUPPORTED_ORDER_TYPES,
)
from broker.kis.market_hours import (
    is_kst_regular_market,
    is_kst_reserve_window,
    mask_account_no,
)
from config import KIS_MODE, KIS_ACCOUNT_NO, ACNT_PRDT_CD


class KISBroker(Broker):
    """
    한국투자증권 API Broker 구현체.

    사용법:
        broker = KISBroker()
        price = broker.get_stock_price("TQQQ", "NAS")
        result = broker.place_order("TQQQ", "NASD", "BUY", 10, 50.0, "LOC")

    DRY 모드는 DryBroker 래퍼로 처리 — KISBroker 자체는 항상 LIVE로 동작.
    """

    def __init__(self):
        self._session = KISSession()
        self._mode = KIS_MODE
        self._account_no = KIS_ACCOUNT_NO
        self._acnt_prdt_cd = ACNT_PRDT_CD

    @property
    def name(self) -> str:
        return "kis"

    # ═══════════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ═══════════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (캐싱 포함)."""
        try:
            token_data = get_access_token(session=self._session)
            return token_data["access_token"]
        except Exception as e:
            raise BrokerError(f"토큰 획득 실패: {str(e)}")

    def _request_with_rate_retry(
        self, method, path, headers=None, params=None, json=None
    ) -> requests.Response:
        """
        requests 래퍼 — rate-limit/타임아웃 재시도 포함.

        - rate-limit(EGW00201/EGW00215, 초당 거래건수 초과): fixed-wait 후 재시도 (최대 3회)
        - 타임아웃(ConnectTimeout, ReadTimeout): 지수 백오프 + jitter 후 재시도 (최대 3회)

        항상 Response를 반환하거나 예외를 발생시킵니다.
        """
        rate_limit_wait = 0.05 if self._mode == "real" else 1.0
        MAX_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_RETRIES:
            try:
                for _ in range(MAX_RETRIES + 1):
                    resp = self._session.request(
                        method, path, headers=headers, params=params, json=json
                    )
                    try:
                        resp.raise_for_status()
                    except requests.exceptions.HTTPError:
                        try:
                            data = resp.json()
                            msg_cd = data.get("msg_cd") or data.get("message") or ""
                            msg1 = data.get("msg1", "")
                        except Exception:
                            msg_cd = ""
                            msg1 = ""

                        is_rate_limit = (
                            msg_cd in ("EGW00201", "EGW00215")
                            or "초당 거래건수" in msg1
                            or "초당 거래건수" in resp.text
                        )

                        if is_rate_limit:
                            time.sleep(rate_limit_wait)
                            continue
                        raise

                    return resp

                raise BrokerError("API 호출 실패: 초당 호출 제한 재시도 초과")

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f"⏳ 타임아웃 오류 발생: {str(e)[:60]}...")
                    print(f"   {wait:.1f}초 후 재시도합니다... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise

        # 도달 불가능 — 루프는 항상 return 또는 raise로 종료
        raise BrokerError("API 호출 실패: 재시도 한도 초과")

    def _check_account(self):
        """계좌번호 설정 여부를 검증합니다."""
        if not self._account_no:
            raise BrokerError(
                "KIS_ACCOUNT_NO가 설정되어 있지 않습니다. "
                "환경변수 또는 .env 파일에 KIS_ACCOUNT_NO를 추가하세요."
            )

    # ═══════════════════════════════════════════════════════════════════════
    # 시장 정보
    # ═══════════════════════════════════════════════════════════════════════

    def is_trading_day(self) -> bool:
        """오늘이 미국 증시 영업일인지 확인합니다 (NYSE 기준)."""
        return is_us_trading_day()

    # ═══════════════════════════════════════════════════════════════════════
    # 조회 API
    # ═══════════════════════════════════════════════════════════════════════

    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        """해외주식 현재가를 조회합니다 → StockPrice(open, last)."""
        access_token = self._get_token()

        path = "/uapi/overseas-price/v1/quotations/price-detail"
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": TR_ID_PRICE_DETAIL,
        }
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": symbol,
        }

        try:
            response = self._request_with_rate_retry(
                "GET", path, headers=headers, params=params
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg = response_data.get("msg1", "알 수 없는 에러")
                raise BrokerError(f"API 호출 실패: {msg}")

            output = response_data.get("output", {})
            return StockPrice(
                open=float(output.get("open", "0")),
                last=float(output.get("last", "0")),
            )

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재가 조회 실패: {str(e)}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """해외주식 현재체결가를 조회합니다 → StockQuotation(tradable, last)."""
        access_token = self._get_token()

        path = "/uapi/overseas-price/v1/quotations/price"
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": TR_ID_QUOTATION,
        }
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": symbol,
        }

        try:
            response = self._request_with_rate_retry(
                "GET", path, headers=headers, params=params
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg = response_data.get("msg1", "알 수 없는 에러")
                raise BrokerError(f"API 호출 실패: {msg}")

            output = response_data.get("output", {})
            return StockQuotation(
                tradable=output.get("ordy", "N") == "Y",
                last=float(output.get("last", "0")),
            )

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재체결가 조회 실패: {str(e)}")

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        해당 종목의 잔고가 없으면 None을 반환합니다.
        """
        self._check_account()
        access_token = self._get_token()

        api_exchange_code, currency_code = convert_exchange_code(exchange)

        path = "/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = select_tr_id(TR_ID_BALANCE, self._mode)
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": tr_id,
        }
        params = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": api_exchange_code,
            "TR_CRCY_CD": currency_code,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        try:
            response = self._request_with_rate_retry(
                "GET", path, headers=headers, params=params
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg = response_data.get("msg1", "알 수 없는 에러")
                raise BrokerError(f"API 호출 실패: {msg}")

            output1 = response_data.get("output1", [])
            if not output1:
                return None

            # 종목 코드로 해당 항목 찾기
            for item in output1:
                ovrs_pdno = item.get("ovrs_pdno", "")
                if symbol.upper() in ovrs_pdno.upper():
                    return Balance(
                        quantity=int(float(item.get("ovrs_cblc_qty", "0"))),
                        avg_price=float(item.get("pchs_avg_pric", "0")),
                    )

            return None

        except requests.exceptions.RequestException as e:
            resp_info = ""
            try:
                if hasattr(e, "response") and e.response is not None:
                    resp = e.response
                    resp_info = f" (status={resp.status_code}) response_body={resp.text}"
            except Exception:
                resp_info = ""
            raise BrokerError(f"잔고 조회 실패: {str(e)}{resp_info}")

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """
        해외주식 매수가능금액을 조회합니다 → PurchaseAmount(orderable_cash).

        현재가를 내부적으로 조회하여 주문단가로 사용합니다.
        """
        self._check_account()
        access_token = self._get_token()

        # 현재가 조회 (단가 정보 필요)
        quotation = self.get_stock_quotation(symbol, exchange)
        current_price = quotation.last
        if current_price <= 0:
            raise BrokerError("현재가 조회 실패: 유효한 가격을 얻을 수 없습니다")

        api_exchange_code, _ = convert_exchange_code(exchange)

        path = "/uapi/overseas-stock/v1/trading/inquire-psamount"
        tr_id = select_tr_id(TR_ID_PURCHASE_AMOUNT, self._mode)
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": tr_id,
        }
        params = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": api_exchange_code,
            "OVRS_ORD_UNPR": str(current_price),
            "ITEM_CD": symbol.upper(),
        }

        try:
            response = self._request_with_rate_retry(
                "GET", path, headers=headers, params=params
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg = response_data.get("msg1", "알 수 없는 에러")
                raise BrokerError(f"API 호출 실패: {msg}")

            output = response_data.get("output", {})
            if not output:
                raise BrokerError("매수가능금액 정보를 조회할 수 없습니다")

            return PurchaseAmount(
                orderable_cash=float(output.get("ord_psbl_frcr_amt", "0")),
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
        해외주식 주문체결내역을 조회합니다 → list[dict].

        연속조회(tr_cont)를 통해 전체 체결 이력을 수집합니다.
        각 dict는 state.py가 기대하는 표준 필드를 포함합니다.
        """
        self._check_account()
        access_token = self._get_token()

        # 날짜 계산 (KST 기준)
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ord_end_dt = now_kst.strftime("%Y%m%d")
        ord_strt_dt = start_date.strftime("%Y%m%d")

        api_exchange_code, _ = convert_exchange_code(exchange)

        path = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
        tr_id = select_tr_id(TR_ID_ORDER_HISTORY, self._mode)
        base_headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": tr_id,
        }
        base_params = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": symbol.upper(),
            "ORD_STRT_DT": ord_strt_dt,
            "ORD_END_DT": ord_end_dt,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "01",
            "OVRS_EXCG_CD": api_exchange_code,
            "SORT_SQN": "AS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }

        print(f"[주문이력] {symbol} 체결내역 조회 시작: ord_strt_dt={ord_strt_dt}, ord_end_dt={ord_end_dt}")

        order_history = []
        ctx_area_nk200 = ""
        ctx_area_fk200 = ""
        is_first_call = True

        try:
            while True:
                headers = dict(base_headers)
                params = dict(base_params)

                if not is_first_call:
                    headers["tr_cont"] = "N"
                    params["CTX_AREA_NK200"] = ctx_area_nk200
                    params["CTX_AREA_FK200"] = ctx_area_fk200

                print(f"[주문이력] {symbol} 체결내역 페이지 조회 시도: tr_cont={headers.get('tr_cont', '첫페이지')}")
                response = self._request_with_rate_retry(
                    "GET", path, headers=headers, params=params
                )
                response.raise_for_status()

                response_data = response.json()
                print(f"[주문이력] {symbol} 체결내역 페이지 조회 성공: {len(response_data.get('output', []))}건, tr_cont={response.headers.get('tr_cont', '')}")

                if response_data.get("rt_cd") != "0":
                    msg = response_data.get("msg1", "알 수 없는 에러")
                    print(f"[주문이력] {symbol} API 오류 상세:")
                    print(f"  rt_cd={response_data.get('rt_cd')}")
                    print(f"  msg1={msg}")
                    print(f"  tr_id={headers.get('tr_id', '')}")
                    print(f"  KIS_MODE={self._mode}")
                    print(f"  CANO={params.get('CANO', '')}")
                    print(f"  OVRS_EXCG_CD={params.get('OVRS_EXCG_CD', '')}")
                    print(f"  ORD_STRT_DT={params.get('ORD_STRT_DT', '')}~{params.get('ORD_END_DT', '')}")
                    raise BrokerError(f"API 호출 실패: {msg}")

                # 이번 페이지 체결내역 추출
                output = response_data.get("output", [])

                for item in output:
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd_raw = item.get("ord_tmd", "")
                    ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""

                    ord_datetime_kst_iso = None
                    ord_datetime_utc_iso = None
                    if ord_dt and ord_tmd:
                        try:
                            kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                            kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                            ord_datetime_kst_iso = kst_dt.isoformat()
                            ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
                        except Exception as e:
                            print(f"[주문이력] {symbol} ord_dt/ord_tmd 파싱 실패: ord_dt={ord_dt}, ord_tmd={ord_tmd_raw} ({e})")

                    order_history.append({
                        "ord_dt": ord_dt,
                        "ord_tmd": ord_tmd_raw,
                        "ord_datetime_kst": ord_datetime_kst_iso,
                        "ord_datetime_utc": ord_datetime_utc_iso,
                        "prdt_name": item.get("prdt_name", ""),
                        "sll_buy_dvsn_cd_name": item.get("sll_buy_dvsn_cd_name", ""),
                        "ft_ord_qty": item.get("ft_ord_qty", "0"),
                        "ft_ccld_qty": item.get("ft_ccld_qty", "0"),
                        "ft_ccld_unpr3": item.get("ft_ccld_unpr3", "0"),
                        "ft_ccld_amt3": item.get("ft_ccld_amt3", "0"),
                        "nccs_qty": item.get("nccs_qty", "0"),
                        "prcs_stat_name": item.get("prcs_stat_name", ""),
                        "tr_mket_name": item.get("tr_mket_name", ""),
                        "tr_crcy_cd": item.get("tr_crcy_cd", ""),
                        "odno": item.get("odno", ""),
                        "ovrs_excg_cd": item.get("ovrs_excg_cd", ""),
                    })

                # 다음 페이지 여부 확인
                tr_cont = response.headers.get("tr_cont", "")
                if tr_cont != "M" and tr_cont != "F":
                    break

                ctx_area_nk200 = response_data.get("ctx_area_nk200", "")
                ctx_area_fk200 = response_data.get("ctx_area_fk200", "")
                is_first_call = False

            print(f"[주문이력] {symbol} 체결내역 총 {len(order_history)}건 조회 완료")

            # Human-friendly summary (optional)
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
                                kst_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]} KST"
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
                        price_s = f"{float(price):.2f}"
                    except Exception:
                        price_s = price
                    try:
                        amt_s = f"{float(amt):.2f}"
                    except Exception:
                        amt_s = amt

                    print(f"{kst_str} | odno={odno} | {side} | qty={qty} | price={price_s} | amt={amt_s}")

            return order_history

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"주문체결내역 조회 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════════
    # 주문 API
    # ═══════════════════════════════════════════════════════════════════════

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
        해외주식 주문을 실행합니다.

        - 모의투자에서 정규장 외 시간이면 예약주문으로 자동 전환합니다.
        - 모의투자 미지원 주문 유형(LOC 등)은 LIMIT으로 자동 변환합니다.
        - DRY 모드는 DryBroker가 처리하므로, 이 메서드는 항상 LIVE로 동작합니다.

        Returns:
            OrderResult: 주문 성공 시 (주문번호, 시각, 예약여부)
        """
        self._check_account()
        access_token = self._get_token()

        # 모의투자 미지원 주문 유형 자동 변환
        if self._mode != "real" and order_type in DEMO_UNSUPPORTED_ORDER_TYPES:
            print(f"⚠️  모의투자 미지원 주문 유형: {order_type} → LIMIT(지정가)으로 자동 변환합니다.")
            order_type = "LIMIT"

        ord_dvsn = get_ord_dvsn(order_type)

        # 모의투자: 정규장 외 시간이면 예약주문으로 전환
        if self._mode != "real":
            now_kst = get_kst_now()
            if not is_kst_regular_market(now_kst):
                if not is_kst_reserve_window(now_kst):
                    raise OrderError(
                        "모의투자: 예약주문 가능시간이 아닙니다 (KST 기준). "
                        "정규장 시간 또는 예약주문 가능시간에 다시 시도하세요."
                    )
                # 정규장 외이지만 예약주문 가능시간 → 예약주문으로 전환
                return self._place_reservation_order(
                    symbol, exchange, side, quantity, price
                )

        # 정규 주문 실행
        return self._place_regular_order(
            symbol, exchange, side, quantity, price, ord_dvsn, access_token
        )

    def _place_regular_order(
        self,
        symbol: str,
        exchange: str,
        side: str,
        quantity: int,
        price: float,
        ord_dvsn: str,
        access_token: str,
    ) -> OrderResult:
        """일반 주문을 실행합니다."""
        # TR_ID 결정 (실전/모의 및 매수/매도에 따라 다름)
        if side == "SELL":
            tr_id = select_tr_id(TR_ID_SELL_ORDER, self._mode)
        else:
            tr_id = select_tr_id(TR_ID_BUY_ORDER, self._mode)

        path = "/uapi/overseas-stock/v1/trading/order"
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": tr_id,
        }
        body = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        try:
            response = self._request_with_rate_retry(
                "POST", path, headers=headers, json=body
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg_cd = response_data.get("msg_cd", "")
                msg1 = response_data.get("msg1", "알 수 없는 에러")
                raise OrderError(f"주문 실패 (응답코드: {msg_cd}): {msg1}")

            output = response_data.get("output", {})

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

        except requests.exceptions.RequestException as e:
            raise OrderError(f"주문 실행 실패: {str(e)}")

    def _place_reservation_order(
        self,
        symbol: str,
        exchange: str,
        side: str,
        quantity: int,
        price: float,
    ) -> OrderResult:
        """예약주문을 실행합니다 (모의투자 정규장 외 시간)."""
        access_token = self._get_token()

        # ord_dv 결정: 매수→usBuy, 매도→usSell
        ord_dv = "usSell" if side == "SELL" else "usBuy"
        tr_id = get_reservation_tr_id(ord_dv, self._mode)

        path = "/uapi/overseas-stock/v1/trading/order-resv"
        headers = {
            "authorization": f"Bearer {access_token}",
            "tr_id": tr_id,
        }
        body = {
            "CANO": self._account_no,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": symbol,
            "OVRS_EXCG_CD": exchange,
            "FT_ORD_QTY": str(quantity),
            "FT_ORD_UNPR3": str(price),
        }

        try:
            response = self._request_with_rate_retry(
                "POST", path, headers=headers, json=body
            )
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("rt_cd") != "0":
                msg_cd = response_data.get("msg_cd", "")
                msg1 = response_data.get("msg1", "알 수 없는 에러")
                raise OrderError(f"예약주문 실패 (응답코드: {msg_cd}): {msg1}")

            output = response_data.get("output", {})

            print("\n========== [예약주문] 접수 성공 ==========")
            print(f"종목: {symbol}  수량: {quantity}  가격: {price}")
            print(f"예약주문번호(ODNO): {output.get('ODNO', '')}")
            print("========================================\n")

            # RSVN_ORD_RCIT_DT가 비어있으면 KST 오늘 날짜로 대체
            rsvn_dt = output.get("RSVN_ORD_RCIT_DT") or get_kst_now().strftime("%Y%m%d")

            return OrderResult(
                order_id=output.get("ODNO", ""),
                order_time=rsvn_dt,
                is_reservation=True,
            )

        except requests.exceptions.RequestException as e:
            raise OrderError(f"예약주문 호출 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → 'NASD')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
