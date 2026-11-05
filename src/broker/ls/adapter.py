"""
LSBroker - LS Broker .

LS OPEN API (REST)     .
- /  URL (AppKey  )
- POST  API (KIS    TR POST)
-  Rate-Limit:  1 ( )
-  Rate-Limit:  10

:
    broker = LSBroker()
    price = broker.get_stock_price("TQQQ", "NAS")

DRY  DryBroker   - LSBroker   LIVE .
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
    OrderNotAcceptedError,
)
from broker.market_utils import (
    select_recent_history,
    get_kst_now,
    is_us_trading_day,
    normalize_order_price,
    resolve_real_kst_from_ord,
)
#     (  -      )
from log_mask import mask_odno
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
    TR_ID_DAILY_CLOSE,
)
from config import BROKER_MODE, BROKER_CONFIG, HTTP_TIMEOUT, FINNHUB_API_KEY, LS_DEMO_BYPASS_BUGS


_BASE_URL = BROKER_CONFIG.get("domain", "https://openapi.ls-sec.co.kr:8080")


class LSBroker(Broker):
    """
    LS API Broker .

    :
        broker = LSBroker()
        price = broker.get_stock_price("TQQQ", "NAS")
        result = broker.place_order("TQQQ", "NASD", "BUY", 10, 50.0, "LOC")

    DRY  DryBroker   - LSBroker   LIVE .
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

    # 
    #  
    # 

    def _get_token(self) -> str:
        """   ( )."""
        try:
            token_data = get_access_token(session=self._session)
            return token_data["access_token"]
        except Exception as e:
            raise BrokerError(f"  : {str(e)}")

    def _post(
        self,
        path: str,
        tr_id: str,
        body: dict,
        extra_headers: Optional[dict] = None,
        retry_network: bool = True,
    ) -> requests.Response:
        """
        LS API POST  .

         TR POST LS API  POST  .
        rate-limit (1)    .

        Rate-limit:
        -  TR (g3101, COSOQ00201, COSAQ00102):  1
        -  TR (COSAT00301):  10
        -    1.0s wait 
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
                # Rate-limit  (1) -  TR 
                time.sleep(1.0)

                resp = self._session.post(
                    url, headers=headers, json=body, timeout=HTTP_TIMEOUT
                )

                # LS API   HTTP 500 + JSON rsp_cd 
                # raise_for_status()   caller rsp_cd  
                if resp.status_code == 429:
                    print(" LS rate-limit  (429), 1  ...")
                    time.sleep(1.0)
                    continue

                return resp

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if not retry_network:
                    raise
                network_retry_count += 1
                if network_retry_count <= MAX_RETRIES:
                    wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                    print(f" LS API : {str(e)[:60]}...")
                    print(f"   {wait:.1f}  ... ({network_retry_count}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise BrokerError(f"LS API   (): {str(e)}")

        raise BrokerError("LS API  :   ")

    # 
    #  
    # 

    def is_trading_day(self) -> bool:
        """     (NYSE )."""
        return is_us_trading_day()

    # 
    #  
    # 

    def get_daily_closes(self, symbol: str, exchange: str, days: int = 5) -> list[float]:
        """
            list[float] ( ).

        1: LS g3204 TR
        2: Finnhub candle API fallback
        """
        try:
            return self._get_daily_closes_ls(symbol, exchange, days)
        except BrokerError:
            pass

        if FINNHUB_API_KEY:
            try:
                return self._get_daily_closes_finnhub(symbol, days)
            except BrokerError as e:
                print(f"[Finnhub Fallback]   : {e}")

        raise BrokerError(f"  : LS g3204  Finnhub  ")

    def _get_daily_closes_ls(self, symbol: str, exchange: str, days: int = 5) -> list[float]:
        """LS g3204 TR   ( ,  gubun=2)."""
        ls_exch, _ = convert_exchange_code(exchange)
        keysymbol = build_symbol(symbol, ls_exch)

        body = {
            "g3204InBlock": {
                "delaygb": "R",
                "keysymbol": keysymbol,
                "exchcd": ls_exch,
                "symbol": symbol.upper(),
                "gubun": "2",
                "qrycnt": 100,
                "comp_yn": "N",
                "sdate": "",
                "edate": "",
                "cts_date": "",
                "cts_info": "",
                "sujung": "Y",
            }
        }

        try:
            resp = self._post("/overseas-stock/chart", tr_id=TR_ID_DAILY_CLOSE, body=body)
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            if rsp_cd != "00000":
                raise BrokerError(f"  : {data.get('rsp_msg', '')}")

            output = data.get("g3204OutBlock1", [])
            closes = []
            for item in output:
                try:
                    close = float(item.get("close", "0"))
                    if close > 0:
                        closes.append(close)
                except (ValueError, TypeError):
                    continue

            # g3204 (ascending)    days 
            return closes[-days:]

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"   (): {str(e)}")

    def _get_daily_closes_finnhub(self, symbol: str, days: int = 5) -> list[float]:
        """Finnhub candle API    (fallback)."""
        now = int(time.time())
        from_ts = now - (days * 2 + 5) * 86400

        url = "https://finnhub.io/api/v1/stock/candle"
        params = {
            "symbol": symbol.upper(),
            "resolution": "D",
            "from": from_ts,
            "to": now,
            "token": FINNHUB_API_KEY,
        }

        try:
            resp = self._session.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            if data.get("s") != "ok":
                raise BrokerError(f"Finnhub candle s!=ok: {symbol}")

            closes = [float(c) for c in data.get("c", []) if isinstance(c, (int, float)) and c > 0]
            if not closes:
                raise BrokerError(f"Finnhub   : {symbol}")

            return closes[-days:]

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"Finnhub API   ({symbol}): {e}")

    # 
    #  API
    # 

    def get_stock_price(self, symbol: str, exchange: str) -> StockPrice:
        """
            StockPrice(open, last).

        1: LS g3101 TR (/  )
        2: LS  +    Finnhub fallback ( )

        LS   g3101    
        (rsp_cd="" ).   Finnhub  API fallback.
        Finnhub  .env FINNHUB_API_KEY .
        """
        # 1. LS API 
        try:
            price = self._get_stock_price_ls(symbol, exchange)
            if price.last > 0:
                return price
        except BrokerError:
            pass

        # 2. LS  (rsp_cd="")  Finnhub fallback (/ )
        if FINNHUB_API_KEY:
            try:
                symbol_upper = symbol.upper()
                price = self._get_stock_price_finnhub(symbol_upper)
                if price.last > 0:
                    print(f"[Finnhub Fallback] {symbol}: LS API , Finnhub   ${price.last:.2f}")
                    return price
            except BrokerError:
                pass

        # 3.   (with Finnhub)
        if self._mode != "real" and not FINNHUB_API_KEY:
            raise BrokerError(
                f"  : LS  g3101  "
                f"(rsp_cd='') FINNHUB_API_KEY  . "
                f".env  FINNHUB_API_KEY ."
            )

        # 4. /          0.0 
        mode_label = "" if self._mode == "real" else ""
        print(f"[] {symbol}({exchange})   : "
              f"LS {mode_label} g3101    .")
        print(f"  :  , , ({symbol})/({exchange})  , "
              f" g3101 TR    .")
        print(f"  :   ( ~ ) , "
              f"/  .")
        return StockPrice(open=0.0, last=0.0)

    def _get_stock_price_ls(self, symbol: str, exchange: str) -> StockPrice:
        """LS g3101 TR   ( )."""
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
                # LS   g3101    
                if rsp_cd == "":
                    raise BrokerError(f"  : rsp_cd='' (LS  g3101 )")
                raise BrokerError(f"  : {rsp_msg}")

            output = data.get("g3101OutBlock", {}).copy()
            if not output:
                raise BrokerError(f"  :  g3101OutBlock  (msg: {rsp_msg})")

            return StockPrice(
                open=float(output.get("open", "0")),
                last=float(output.get("price", "0")),
            )

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"   (): {str(e)}")

    def _get_stock_price_finnhub(self, symbol: str) -> StockPrice:
        """
        Finnhub    (10 ).

        Finnhub US   plain ticker  (TQQQ, SOXL).
        Free tier: 60 calls/min,  ,  .
        """
        now = time.time()

        if symbol in self._finnhub_cache:
            cached_time, cached_price = self._finnhub_cache[symbol]
            if now - cached_time < self._finnhub_ttl:
                age = now - cached_time
                print(f"[Finnhub Cache] {symbol}:   (age={age:.1f}s)  ${cached_price.last:.2f}")
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
                raise BrokerError(f"Finnhub   : symbol={symbol} data={data}")

            price = StockPrice(open=open_price, last=current)
            self._finnhub_cache[symbol] = (now, price)
            return price

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"Finnhub API   ({symbol}): {e}")
        except (ValueError, TypeError) as e:
            raise BrokerError(f"Finnhub    ({symbol}): {e}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
            StockQuotation(tradable, last).

        LS TR: g3101 (   TR)
        g3101      tradable  True.

        1: LS g3101 TR (/  )
        2: LS  +    Finnhub fallback ( )
        """
        # 1. LS g3101 TR 
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
                    raise BrokerError(f"  : rsp_cd='' (LS  g3101 )")
                raise BrokerError(f"  : {rsp_msg}")

            output = data.get("g3101OutBlock", {})
            return StockQuotation(
                tradable=True,
                last=float(output.get("price", "0")),
            )

        except (BrokerError, requests.exceptions.RequestException):
            pass

        # 2. LS  (rsp_cd="")  Finnhub fallback (/ )
        if FINNHUB_API_KEY:
            try:
                symbol_upper = symbol.upper()
                price = self._get_stock_price_finnhub(symbol_upper)
                if price.last > 0:
                    print(f"[Finnhub Fallback] {symbol}: LS g3101 , Finnhub   ${price.last:.2f}")
                    return StockQuotation(tradable=True, last=price.last)
            except BrokerError:
                pass

        # 3.   (with Finnhub)
        if self._mode != "real" and not FINNHUB_API_KEY:
            raise BrokerError(
                f"  : LS  g3101  "
                f"(rsp_cd='') FINNHUB_API_KEY  . "
                f".env  FINNHUB_API_KEY ."
            )
        return StockQuotation(tradable=True, last=0.0)

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
             Balance(quantity, avg_price).

        TR: COSOQ00201 ( )
        OutBlock4:    /
           None .
        """
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
                pass  #  
            elif rsp_cd in ("00200", "00707", "02679"):
                print(f"[] {symbol} COSOQ00201: {rsp_msg} (None )")
                return None
            else:
                if LS_DEMO_BYPASS_BUGS:
                    print(f"[] {symbol} COSOQ00201: LS    (rsp_cd={rsp_cd}): {rsp_msg}")
                    return None
                raise BrokerError(f"  : {rsp_msg}")

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
            raise BrokerError(f"  : {str(e)}{resp_info}")

    def get_purchase_amount(self, symbol: str, exchange: str) -> PurchaseAmount:
        """
            PurchaseAmount(orderable_cash).

        TR: COSOQ00201 ( ) OutBlock3  FcurrOrdAbleAmt
             fallback.
        """

        def _seed_fallback(msg: str = ""):
            reason = f" ({msg})" if msg else ""
            print(f"[] {symbol} COSOQ00201  {reason} -   ")
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
                return _seed_fallback("OutBlock3 ")

            for item in out_block3:
                if item.get("CrcyCode", "").upper() == currency.upper():
                    amt = float(item.get("FcurrOrdAbleAmt", "0"))
                    if amt > 0:
                        return PurchaseAmount(orderable_cash=amt)
                    return _seed_fallback(f"FcurrOrdAbleAmt={amt}")

            return _seed_fallback(f"{currency}  ")

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
            list[dict].

        LS TR: COSAQ00103 ( )
        (tr_cont/tr_cont_key )     .

         dict state.py    :
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        #   (KST )
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ord_end_dt = now_kst.strftime("%Y%m%d")
        ord_strt_dt = start_date.strftime("%Y%m%d")

        ls_exch, currency = convert_exchange_code(exchange)

        body = {
            "COSAQ00102InBlock1": {
                "RecCnt": 1,
                "QryTpCode": "1",        # 1:
                "BkseqTpCode": "2",      # 2:
                "OrdMktCode": ls_exch,   # 
                "BnsTpCode": "0",        # 0:
                "IsuNo": symbol.upper(),
                "SrtOrdNo": 0,
                "OrdDt": "",
                "ExecYn": "2",           # 2: ( ?)
                "CrcyCode": currency,
                "ThdayBnsAppYn": "0",
                "LoanBalHldYn": "0",
            }
        }

        print(f"[] {symbol}   : {ord_strt_dt} ~ {ord_end_dt}")

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
                    f"[] {symbol}  {page_count}  "
                    f"{' (: ' + tr_cont_key[:8] + '...)' if tr_cont_key else ''}"
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
                    msg = data.get("rsp_msg", "   ")
                    print(f"[] {symbol} API : rsp_cd={rsp_cd} rsp_msg={msg}")
                    #      
                    # 01900:   TR
                    # 02679:   (  )
                    if rsp_cd in ("01900", "02679"):
                        reason = " " if rsp_cd == "01900" else " "
                        print(f"[] {symbol} {reason}({rsp_cd}),   ")
                        return []
                    raise BrokerError(f"  : {msg}")

                output = data.get("COSAQ00102OutBlock1", [])

                for item in output:
                    #   (ord_dt + ord_tmd)
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd_raw = item.get("ord_tmd", "")
                    ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""

                    ord_datetime_kst_iso = None
                    ord_datetime_utc_iso = None
                    if ord_dt and ord_tmd:
                        try:
                            # LS quirk:  (ord_dt) (ET) .
                            #  (last_updated)    
                            #      ord_datetime_utc .
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
                                f"[] {symbol}   : "
                                f"ord_dt={ord_dt} ord_tmd={ord_tmd_raw} ({e})"
                            )

                    # LS      
                    row = {
                        "ord_dt": ord_dt,
                        "ord_tmd": ord_tmd_raw,
                        "ord_datetime_kst": ord_datetime_kst_iso,
                        "ord_datetime_utc": ord_datetime_utc_iso,
                        "prdt_name": item.get("prdt_name", ""),
                        "sll_buy_dvsn_cd_name": (
                            "" if item.get("sll_buy_dvsn_cd_name") == "02"
                            else "" if item.get("sll_buy_dvsn_cd_name") == "01"
                            else item.get("sll_buy_dvsn_cd_name", "")
                        ),
                        "ft_ccld_qty": item.get("ft_ccld_qty", "0"),
                        "ft_ccld_unpr3": item.get("ft_ccld_unpr3", "0"),
                        "ft_ccld_amt3": item.get("ft_ccld_amt3", "0"),
                        "nccs_qty": item.get("nccs_qty", "0"),
                        "prcs_stat_name": item.get("prcs_stat_name", ""),
                        "tr_mket_name": item.get("tr_mket_name", ""),
                        "tr_crcy_cd": item.get("tr_crcy_cd", ""),
                        "odno": item.get("odno", ""),
                        "ovrs_excg_cd": item.get("ovrs_excg_cd", ""),
                    }
                    #     "0"    .
                    if "ft_ord_qty" in item:
                        row["ft_ord_qty"] = item["ft_ord_qty"]
                    order_history.append(row)

                #    ()
                tr_cont = resp.headers.get("tr_cont", "N")
                tr_cont_key = resp.headers.get("tr_cont_key", "")

                if tr_cont != "Y" or not tr_cont_key:
                    break

            print(
                f"[] {symbol}  {len(order_history)}  "
                f" ({page_count})"
            )

            # Human-friendly  (verbose )
            if verbose and order_history:
                try:
                    n = int(limit) if limit and int(limit) > 0 else 100
                except Exception:
                    n = 100
                n = min(n, len(order_history))
                print(f"[ ] {symbol}  {n}( {len(order_history)} , )")
                for item in select_recent_history(order_history, n):
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd = (item.get("ord_tmd") or "").zfill(6)

                    #   (KIS API  ord_dt -   )
                    if ord_dt and len(ord_dt) == 8:
                        us_dt_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                    else:
                        us_dt_str = "()"

                    #    (ord_dt= + ord_tmd=KST  )
                    real_kst = None
                    if ord_dt and ord_tmd:
                        real_kst = resolve_real_kst_from_ord(ord_dt, ord_tmd)
                    if real_kst:
                        kst_str = real_kst.strftime("%Y-%m-%d %H:%M:%S") + " KST"
                    else:
                        kst_str = "()"

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
                        f" {us_dt_str} |  {kst_str} | odno={mask_odno(odno)}"
                        f" | {side} | qty={qty} | price={price_s} | amt={amt_s}"
                    )
            return order_history

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"  : {str(e)}")

    # 
    #  API
    # 

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
          .

        LS TR: COSAT00301 ( )

        -     LIMIT  .
        -       .
        - DRY  DryBroker ,    LIVE .

        Returns:
            OrderResult:    (, , )
        """
        #      
        if self._mode != "real" and order_type in DEMO_UNSUPPORTED_ORDER_TYPES:
            print(
                f"     : {order_type}"
                f"  LIMIT()  ."
            )
            order_type = "LIMIT"

        ord_dvsn = get_ord_dvsn(order_type, side)
        # exchange  broker.exchange_code()  API ("82"/"81")
        #  . convert_exchange_code()    .
        ls_exch = exchange

        # OrdPtnCode: 01=, 02=
        ord_ptn_code = "02" if side == "BUY" else "01"

        #        ($1+   2 )
        price = normalize_order_price(price)

        body = {
            "COSAT00301InBlock1": {
                "RecCnt": 1,
                "OrdPtnCode": ord_ptn_code,
                "OrgOrdNo": 0,                 # : 0
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
                "/overseas-stock/order", tr_id=TR_ID_ORDER, body=body,
                retry_network=False,
            )
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            rsp_msg = data.get("rsp_msg", "   ")
            # LS     ():
            #   00000 = , 00040 =  , 00050 =  ()
            #   (2026-11-03 preflight : rsp_cd=00040 + "  " +   )
            if rsp_cd not in ("00000", "00040", "00050"):
                raise OrderNotAcceptedError(f"  (: {rsp_cd}): {rsp_msg}")

            #   : OutBlock2() , OutBlock1() 
            output = data.get("COSAT00301OutBlock2", {}) or data.get("COSAT00301OutBlock1", {})
            order_id = output.get("OrdNo", "") or output.get("ODNO", "")

            print("\n========== [LIVE ]   ==========")
            print(f" : {symbol}")
            print(f": {mask_odno(order_id)}")
            print(f": {quantity}")
            print(f": ${price}")
            print("==========================================\n")

            return OrderResult(
                order_id=order_id,
                order_time=get_kst_now().strftime("%Y%m%d%H%M%S"),
                is_reservation=False,
            )

        except requests.exceptions.RequestException as e:
            raise OrderError(f"  : {str(e)}")

    def cancel_order(
        self,
        odno: str,
        symbol: str | None = None,
        exchange: str | None = None,
    ) -> None:
        """
        LS   .

        TR: COSAT00301 () + OrdPtnCode="08" ().
        OrgOrdNo   (  ).

        : ~/ls-openapi-tr-guides.json []  
          - COSAT00301 OrdPtnCode: 01= / 02= / 08=
          - OrgOrdNo: "   "
          - COSMT00300 " "   ( )
          - COSAT00311 (07) , COSAT00400  * 

        -  rsp_cd  None  ( )
        -   rsp_cd  OrderNotAcceptedError (  )
        -    OrderError (  )
        """
        odno = str(odno).strip() if odno else ""
        if not odno:
            raise OrderNotAcceptedError("  ")

        #   (IsuNo)*(OrdMktCode)    .
        # (P2-3:    ) 1-arg    TypeError 
        # preflight except TypeError  symbol*exchange  .
        if not symbol or not exchange:
            raise TypeError(
                "LS cancel_order symbol exchange  "
                f"(odno={mask_odno(odno)})"
            )

        ls_exch = self.exchange_code(exchange)

        try:
            org_ord_no = int(odno)
        except (ValueError, TypeError):
            raise OrderNotAcceptedError(
                f"   : odno={mask_odno(odno)}"
            )

        body = {
            "COSAT00301InBlock1": {
                "RecCnt": 1,
                "OrdPtnCode": "08",       # 08 = 
                "OrgOrdNo": org_ord_no,    #    ()
                "OrdMktCode": ls_exch,
                "IsuNo": (symbol or "").upper(),
                "OrdQty": 0,
                "OvrsOrdPrc": 0.0,
                "OrdprcPtnCode": "00",
                "BrkTpCode": "",
            }
        }

        try:
            resp = self._post(
                "/overseas-stock/order",
                tr_id=TR_ID_ORDER,
                body=body,
                retry_network=False,
            )
            data = resp.json()

            rsp_cd = data.get("rsp_cd", "")
            rsp_msg = data.get("rsp_msg", "   ")

            # LS     ():
            #   00156 =   (2026-11-03 : " ")
            #   00000 =  (  ,    )
            if rsp_cd in ("00000", "00156"):
                print(f"[LS]   : odno={mask_odno(odno)}")
                return None

            raise OrderNotAcceptedError(
                f"LS    (: {rsp_cd}): {rsp_msg}"
            )

        except requests.exceptions.RequestException as e:
            raise OrderError(
                f"LS    (  ): {str(e)}"
            )

    # 
    # 
    # 

    def exchange_code(self, user_code: str) -> str:
        """    API    (: 'NAS'  '82')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP  ."""
        self._session.close()
