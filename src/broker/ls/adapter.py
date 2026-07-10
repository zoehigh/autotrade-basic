"""
LSBroker — LS증권 Broker 구현체.

LS증권 OPEN API (REST)를 통해 미국 주식 자동매매를 실행합니다.
- 실전/모의투자 동일 URL (AppKey로 환경 구분)
- POST 기반 API (KIS와 달리 거의 모든 TR이 POST)
- 조회 Rate-Limit: 초당 1회 (매우 제한적)
- 주문 Rate-Limit: 초당 10회

사용법:
    broker = LSBroker()
    price = broker.get_stock_price("TQQQ", "NAS")

DRY 모드는 DryBroker 래퍼로 처리 — LSBroker 자체는 항상 LIVE로 동작.
"""
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import certifi
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
from broker.ls.auth import get_access_token
from broker.ls.exchange import (
    convert_exchange_code,
    get_api_exchange_code,
    build_symbol,
)
from broker.ls.order_types import (
    get_ord_dvsn,
    DEMO_UNSUPPORTED_ORDER_TYPES,
    TR_ID_PRICE,
    TR_ID_BALANCE,
    TR_ID_ORDER_HISTORY,
    TR_ID_ORDER,
)
from config import BROKER_MODE, BROKER_CONFIG, HTTP_TIMEOUT, FINNHUB_API_KEY


_BASE_URL = BROKER_CONFIG.get("domain", "https://openapi.ls-sec.co.kr:8080")


class LSBroker(Broker):
    """
    LS증권 API Broker 구현체.

    사용법:
        broker = LSBroker()
        price = broker.get_stock_price("TQQQ", "NAS")
        result = broker.place_order("TQQQ", "NASD", "BUY", 10, 50.0, "LOC")

    DRY 모드는 DryBroker 래퍼로 처리 — LSBroker 자체는 항상 LIVE로 동작.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.verify = certifi.where()
        self._mode = BROKER_MODE
        self._finnhub_cache: dict[str, tuple[float, StockPrice]] = {}
        self._finnhub_ttl = 10  # seconds

    @property
    def name(self) -> str:
        return "ls"

    # ══════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ══════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (캐싱 포함)."""
        try:
            token_data = get_access_token(session=self._session)
            return token_data["access_token"]
        except Exception as e:
            raise BrokerError(f"토큰 획득 실패: {str(e)}")

    def _post(
        self,
        path: str,
        tr_id: str,
        body: dict,
        extra_headers: Optional[dict] = None,
    ) -> requests.Response:
        """
        LS API POST 요청을 실행합니다.

        모든 TR이 POST인 LS API의 특성상 POST 전용 래퍼.
        rate-limit 대기(1초) 및 타임아웃 재시도를 포함합니다.

        Rate-limit:
        - 조회 TR (g3101, COSOQ00201, COSAQ00102): 초당 1회
        - 주문 TR (COSAT00301): 초당 10회
        - 안전하게 모든 요청에 1.0s wait 적용
        """
        access_token = self._get_token()

        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "authorization": f"Bearer {access_token}",
            "tr_cd": tr_id,
            "tr_cont": "N",
            "tr_cont_key": "",
        }
        if extra_headers:
            headers.update(extra_headers)

        url = f"{_BASE_URL}{path}"

        MAX_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_RETRIES:
            try:
                # Rate-limit 대기 (1초) — 조회 TR 기준
                time.sleep(1.0)

                resp = self._session.post(
                    url, headers=headers, json=body, timeout=HTTP_TIMEOUT
                )

                # LS API는 비즈니스 오류를 HTTP 500 + JSON rsp_cd로 반환
                # raise_for_status()를 사용하지 않고 caller가 rsp_cd를 직접 확인
                if resp.status_code == 429:
                    print(f"⏳ LS rate-limit 초과 (429), 1초 후 재시도...")
                    time.sleep(1.0)
                    continue

                return resp

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f"⏳ LS API 타임아웃: {str(e)[:60]}...")
                    print(f"   {wait:.1f}초 후 재시도... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise BrokerError(f"LS API 호출 실패 (네트워크): {str(e)}")

        raise BrokerError("LS API 호출 실패: 재시도 한도 초과")

    # ══════════════════════════════════════════════════════════════════
    # 시장 정보
    # ══════════════════════════════════════════════════════════════════

    def is_trading_day(self) -> bool:
        """오늘이 미국 증시 영업일인지 확인합니다 (NYSE 기준)."""
        return is_us_trading_day()

    # ══════════════════════════════════════════════════════════════════
    # 조회 API
    # ══════════════════════════════════════════════════════════════════

    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        """
        해외주식 현재가를 조회합니다 → StockPrice(open, last).

        1차: LS g3101 TR (실전/모의 모두 시도)
        2차: LS 실패 + 모의투자 환경 → Finnhub fallback (설정된 경우)

        LS 모의투자 환경에서는 g3101이 해외주식 현재가를 지원하지 않습니다
        (rsp_cd="" 반환). 이 경우 Finnhub 무료 API로 fallback합니다.
        Finnhub를 사용하려면 .env에 FINNHUB_API_KEY를 설정하세요.
        """
        # 1. LS API 조회
        try:
            price = self._get_stock_price_ls(symbol, exchange)
            if price.last > 0:
                return price
        except BrokerError:
            pass

        # 2. LS 실패 (rsp_cd="") → 모의투자에서 Finnhub fallback
        if self._mode != "real" and FINNHUB_API_KEY:
            try:
                symbol_upper = symbol.upper()
                price = self._get_stock_price_finnhub(symbol_upper)
                if price.last > 0:
                    print(f"[Finnhub Fallback] {symbol}: LS API 실패, Finnhub 사용 → ${price.last:.2f}")
                    return price
            except BrokerError:
                pass

        # 3. 모두 실패
        if self._mode != "real" and not FINNHUB_API_KEY:
            raise BrokerError(
                f"현재가 조회 실패: LS 모의투자에서 g3101 미지원 "
                f"(rsp_cd='')이고 FINNHUB_API_KEY가 설정되지 않았습니다. "
                f".env 파일에 FINNHUB_API_KEY를 추가하세요."
            )
        return StockPrice(open=0.0, last=0.0)

    def _get_stock_price_ls(self, symbol: str, exchange: str) -> StockPrice:
        """LS g3101 TR로 현재가 조회 (내부 호출용)."""
        ls_exch, _ = convert_exchange_code(exchange)
        keysymbol = build_symbol(symbol, ls_exch)

        body = {
            "g3101InBlock": {
                "delaygb": "R",
                "keysymbol": keysymbol,
                "exchcd": ls_exch,
                "symbol": symbol.upper(),
            }
        }

        try:
            resp = self._post("/overseas-stock/market-data", tr_id=TR_ID_PRICE, body=body)
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            rsp_msg = data.get("rsp_msg", "")
            if rsp_cd != "00000":
                # LS 모의투자 환경에서 g3101은 해외주식 현재가를 지원하지 않음
                if rsp_cd == "":
                    raise BrokerError(f"현재가 조회 실패: rsp_cd='' (LS 모의투자 g3101 미지원)")
                raise BrokerError(f"현재가 조회 실패: {rsp_msg}")

            output = data.get("g3101OutBlock", {}).copy()
            if not output:
                raise BrokerError(f"현재가 조회 실패: 응답에 g3101OutBlock 없음 (msg: {rsp_msg})")

            return StockPrice(
                open=float(output.get("open", "0")),
                last=float(output.get("price", "0")),
            )

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재가 조회 실패 (네트워크): {str(e)}")

    def _get_stock_price_finnhub(self, symbol: str) -> StockPrice:
        """
        Finnhub 실시간 시세 조회 (10초 캐시).

        Finnhub는 US 종목에 대해 plain ticker를 사용합니다 (TQQQ, SOXL).
        Free tier: 60 calls/min, 실시간 시세, 개인용 무료.
        """
        now = time.time()

        if symbol in self._finnhub_cache:
            cached_time, cached_price = self._finnhub_cache[symbol]
            if now - cached_time < self._finnhub_ttl:
                age = now - cached_time
                print(f"[Finnhub Cache] {symbol}: 캐시 사용 (age={age:.1f}s) → ${cached_price.last:.2f}")
                return cached_price

        url = "https://finnhub.io/api/v1/quote"
        params = {"symbol": symbol, "token": FINNHUB_API_KEY}

        try:
            resp = self._session.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            current = float(data.get("c", 0))
            open_price = float(data.get("o", 0))

            if current <= 0:
                raise BrokerError(f"Finnhub 유효하지 않은 가격: symbol={symbol} data={data}")

            price = StockPrice(open=open_price, last=current)
            self._finnhub_cache[symbol] = (now, price)
            return price

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"Finnhub API 호출 실패 ({symbol}): {e}")
        except (ValueError, TypeError) as e:
            raise BrokerError(f"Finnhub 응답 파싱 실패 ({symbol}): {e}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
        해외주식 현재체결가를 조회합니다 → StockQuotation(tradable, last).

        LS TR: g3101 (현재가 조회와 동일 TR)
        g3101은 주문가능 여부 플래그를 제공하지 않으므로 tradable은 항상 True입니다.

        1차: LS g3101 TR (실전/모의 모두 시도)
        2차: LS 실패 + 모의투자 환경 → Finnhub fallback (설정된 경우)
        """
        # 1. LS g3101 TR 조회
        try:
            ls_exch, _ = convert_exchange_code(exchange)
            keysymbol = build_symbol(symbol, ls_exch)
            body = {
                "g3101InBlock": {
                    "delaygb": "R",
                    "keysymbol": keysymbol,
                    "exchcd": ls_exch,
                    "symbol": symbol.upper(),
                }
            }
            resp = self._post("/overseas-stock/market-data", tr_id=TR_ID_PRICE, body=body)
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            rsp_msg = data.get("rsp_msg", "")
            if rsp_cd != "00000":
                if rsp_cd == "":
                    raise BrokerError(f"현재체결가 조회 실패: rsp_cd='' (LS 모의투자 g3101 미지원)")
                raise BrokerError(f"현재체결가 조회 실패: {rsp_msg}")

            output = data.get("g3101OutBlock", {})
            return StockQuotation(
                tradable=True,
                last=float(output.get("price", "0")),
            )

        except (BrokerError, requests.exceptions.RequestException):
            pass

        # 2. LS 실패 (rsp_cd="") → 모의투자에서 Finnhub fallback
        if self._mode != "real" and FINNHUB_API_KEY:
            try:
                symbol_upper = symbol.upper()
                price = self._get_stock_price_finnhub(symbol_upper)
                if price.last > 0:
                    print(f"[Finnhub Fallback] {symbol}: LS g3101 실패, Finnhub 사용 → ${price.last:.2f}")
                    return StockQuotation(tradable=True, last=price.last)
            except BrokerError:
                pass

        # 3. 모두 실패
        if self._mode != "real" and not FINNHUB_API_KEY:
            raise BrokerError(
                f"현재체결가 조회 실패: LS 모의투자에서 g3101 미지원 "
                f"(rsp_cd='')이고 FINNHUB_API_KEY가 설정되지 않았습니다. "
                f".env 파일에 FINNHUB_API_KEY를 추가하세요."
            )
        return StockQuotation(tradable=True, last=0.0)

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        TR: COSOQ00201 (해외주식 종합잔고평가)
        OutBlock4: 개별 종목별 보유 수량/평단가
        조회할 데이터가 없으면 None을 반환합니다.
        """
        today = get_kst_now().strftime("%Y%m%d")

        body = {
            "COSOQ00201InBlock1": {
                "RecCnt": 1,
                "BaseDt": "",
                "CrcyCode": "USD",
                "AstkBalTpCode": "00",
            }
        }

        try:
            resp = self._post(
                "/overseas-stock/accno", tr_id=TR_ID_BALANCE, body=body
            )
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            rsp_msg = data.get("rsp_msg", "")
            if rsp_cd in ("00000", "00001"):
                pass  # 정상 응답
            elif rsp_cd in ("00200", "00707", "02679"):
                print(f"[잔고] {symbol} COSOQ00201: {rsp_msg} (None 반환)")
                return None
            else:
                raise BrokerError(f"잔고 조회 실패: {rsp_msg}")

            out_block4 = data.get("COSOQ00201OutBlock4", [])
            if not out_block4:
                return None

            sym_upper = symbol.upper()
            for item in out_block4:
                shtn_isu_no = item.get("ShtnIsuNo", "").upper()
                if sym_upper in shtn_isu_no:
                    return Balance(
                        quantity=int(float(item.get("AstkBalQty", "0"))),
                        avg_price=float(item.get("FcstckUprc", "0")),
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

        TR: COSOQ00201 (해외주식 종합잔고평가) OutBlock3 → FcurrOrdAbleAmt
        조회 실패 시 시드 기반 fallback.
        """

        def _seed_fallback(msg: str = ""):
            reason = f" ({msg})" if msg else ""
            print(f"[매수가능] {symbol} COSOQ00201 조회 불가{reason} — 시드 기반으로 진행")
            return PurchaseAmount(orderable_cash=1_000_000.0)

        _, currency = convert_exchange_code(exchange)
        body = {
            "COSOQ00201InBlock1": {
                "RecCnt": 1,
                "BaseDt": "",
                "CrcyCode": "USD",
                "AstkBalTpCode": "00",
            }
        }

        try:
            resp = self._post(
                "/overseas-stock/accno", tr_id=TR_ID_BALANCE, body=body
            )
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            if rsp_cd not in ("00000", "00001"):
                return _seed_fallback(data.get("rsp_msg", rsp_cd))

            out_block3 = data.get("COSOQ00201OutBlock3", [])
            if not out_block3:
                return _seed_fallback("OutBlock3 없음")

            for item in out_block3:
                if item.get("CrcyCode", "").upper() == currency.upper():
                    amt = float(item.get("FcurrOrdAbleAmt", "0"))
                    if amt > 0:
                        return PurchaseAmount(orderable_cash=amt)
                    return _seed_fallback(f"FcurrOrdAbleAmt={amt}")

            return _seed_fallback(f"{currency} 통화 없음")

        except (BrokerError, requests.exceptions.RequestException, ValueError) as e:
            return _seed_fallback(str(e)[:60])

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

        LS TR: COSAQ00103 (해외주식 체결내역조회)
        연속조회(tr_cont/tr_cont_key 헤더)를 통해 전체 체결 이력을 수집합니다.

        각 dict는 state.py가 기대하는 표준 필드를 포함합니다:
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        # 날짜 계산 (KST 기준)
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ord_end_dt = now_kst.strftime("%Y%m%d")
        ord_strt_dt = start_date.strftime("%Y%m%d")

        ls_exch, currency = convert_exchange_code(exchange)

        body = {
            "COSAQ00102InBlock1": {
                "RecCnt": 1,
                "QryTpCode": "1",        # 1:계좌별
                "BkseqTpCode": "2",      # 2:정순
                "OrdMktCode": ls_exch,   # 거래소코드
                "BnsTpCode": "0",        # 0:전체
                "IsuNo": symbol.upper(),
                "SrtOrdNo": 0,
                "OrdDt": "",
                "ExecYn": "2",           # 2:미체결 (체결내역은 따로?)
                "CrcyCode": currency,
                "ThdayBnsAppYn": "0",
                "LoanBalHldYn": "0",
            }
        }

        print(f"[주문이력] {symbol} 체결내역 조회 시작: {ord_strt_dt} ~ {ord_end_dt}")

        order_history: list[dict] = []
        tr_cont_key = ""
        is_first = True
        MAX_PAGES = 20
        page_count = 0

        try:
            while page_count < MAX_PAGES:
                page_count += 1
                extra_headers = {}
                if tr_cont_key:
                    extra_headers["tr_cont_key"] = tr_cont_key

                print(
                    f"[주문이력] {symbol} 페이지 {page_count} 조회 시도"
                    f"{' (연속키: ' + tr_cont_key[:8] + '...)' if tr_cont_key else ''}"
                )

                resp = self._post(
                    "/overseas-stock/accno",
                    tr_id=TR_ID_ORDER_HISTORY,
                    body=body,
                    extra_headers=extra_headers,
                )
                data = resp.json()

                rsp_cd = data.get("rsp_cd")
                if rsp_cd != "00000":
                    msg = data.get("rsp_msg", "알 수 없는 에러")
                    print(f"[주문이력] {symbol} API 오류: rsp_cd={rsp_cd} rsp_msg={msg}")
                    # 정상 케이스 → 빈 목록 반환
                    # 01900: 모의투자 미지원 TR
                    # 02679: 체결내역 없음 (실전 정상 응답)
                    if rsp_cd in ("01900", "02679"):
                        reason = "모의투자 미지원" if rsp_cd == "01900" else "체결내역 없음"
                        print(f"[주문이력] {symbol} {reason}({rsp_cd}), 빈 목록 반환")
                        return []
                    raise BrokerError(f"체결내역 조회 실패: {msg}")

                output = data.get("COSAQ00102OutBlock1", [])

                for item in output:
                    # 시간 파싱 (ord_dt + ord_tmd)
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd_raw = item.get("ord_tmd", "")
                    ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""

                    ord_datetime_kst_iso = None
                    ord_datetime_utc_iso = None
                    if ord_dt and ord_tmd:
                        try:
                            kst_dt = datetime.strptime(
                                ord_dt + ord_tmd, "%Y%m%d%H%M%S"
                            )
                            kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                            ord_datetime_kst_iso = kst_dt.isoformat()
                            ord_datetime_utc_iso = kst_dt.astimezone(
                                ZoneInfo("UTC")
                            ).isoformat()
                        except Exception as e:
                            print(
                                f"[주문이력] {symbol} 시간 파싱 실패: "
                                f"ord_dt={ord_dt} ord_tmd={ord_tmd_raw} ({e})"
                            )

                    # LS 응답 필드명 → 표준 필드명 매핑
                    order_history.append({
                        "ord_dt": ord_dt,
                        "ord_tmd": ord_tmd_raw,
                        "ord_datetime_kst": ord_datetime_kst_iso,
                        "ord_datetime_utc": ord_datetime_utc_iso,
                        "prdt_name": item.get("prdt_name", ""),
                        "sll_buy_dvsn_cd_name": (
                            "매수" if item.get("sll_buy_dvsn_cd_name") == "02"
                            else "매도" if item.get("sll_buy_dvsn_cd_name") == "01"
                            else item.get("sll_buy_dvsn_cd_name", "")
                        ),
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

                # 연속조회 여부 확인 (헤더)
                tr_cont = resp.headers.get("tr_cont", "N")
                tr_cont_key = resp.headers.get("tr_cont_key", "")

                if tr_cont != "Y" or not tr_cont_key:
                    break

            print(
                f"[주문이력] {symbol} 총 {len(order_history)}건 조회 완료"
                f" ({page_count}페이지)"
            )

            # Human-friendly 요약 (verbose 모드)
            if verbose and order_history:
                try:
                    n = int(limit) if limit and int(limit) > 0 else 100
                except Exception:
                    n = 100
                n = min(n, len(order_history))

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
                        ord_dt = item.get("ord_dt", "")
                        ord_tmd = (item.get("ord_tmd") or "").zfill(6)
                        if ord_dt and len(ord_dt) == 8 and ord_tmd:
                            kst_str = (
                                f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]} "
                                f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]} KST"
                            )
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
                    print(
                        f"{kst_str} | odno={odno} | {side}"
                        f" | qty={qty} | price={price_s} | amt={amt_s}"
                    )

            return order_history

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"체결내역 조회 실패: {str(e)}")

    # ══════════════════════════════════════════════════════════════════
    # 주문 API
    # ══════════════════════════════════════════════════════════════════

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

        LS TR: COSAT00301 (해외주식 신규주문)

        - 모의투자 미지원 주문 유형은 LIMIT으로 자동 변환합니다.
        - 모의투자 정규장 외 시간 예약주문은 보류 상태입니다.
        - DRY 모드는 DryBroker가 처리하므로, 이 메서드는 항상 LIVE로 동작합니다.

        Returns:
            OrderResult: 주문 성공 시 (주문번호, 시각, 예약여부)
        """
        # 모의투자 미지원 주문 유형 자동 변환
        if self._mode != "real" and order_type in DEMO_UNSUPPORTED_ORDER_TYPES:
            print(
                f"⚠️  모의투자 미지원 주문 유형: {order_type}"
                f" → LIMIT(지정가)으로 자동 변환합니다."
            )
            order_type = "LIMIT"

        ord_dvsn = get_ord_dvsn(order_type, side)
        ls_exch, _ = convert_exchange_code(exchange)

        # OrdPtnCode: 01=매도, 02=매수
        ord_ptn_code = "02" if side == "BUY" else "01"

        body = {
            "COSAT00301InBlock1": {
                "RecCnt": 1,
                "OrdPtnCode": ord_ptn_code,
                "OrgOrdNo": 0,                 # 신규주문: 0
                "OrdMktCode": ls_exch,
                "IsuNo": symbol.upper(),
                "OrdQty": int(quantity),
                "OvrsOrdPrc": float(price),
                "OrdprcPtnCode": ord_dvsn,
                "BrkTpCode": "",
            }
        }

        try:
            resp = self._post(
                "/overseas-stock/order", tr_id=TR_ID_ORDER, body=body
            )
            data = resp.json()

            if data.get("rsp_cd") != "00000":
                msg_cd = data.get("msg_cd", "")
                rsp_msg = data.get("rsp_msg", "알 수 없는 에러")
                raise OrderError(f"주문 실패 (응답코드: {msg_cd}): {rsp_msg}")

            output = data.get("COSAT00301OutBlock1", {})

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

    # ══════════════════════════════════════════════════════════════════
    # 유틸리티
    # ══════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 거래소 코드 변환 (예: 'NAS' → '82')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
