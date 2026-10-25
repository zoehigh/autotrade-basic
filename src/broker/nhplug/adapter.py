"""
NHPlugBroker — NH투자증권 나무(NHPLUG) Broker 구현체.

NH투자증권 나무 Open API (REST)를 통해 미국 주식 자동매매를 실행합니다.
- 실전: https://api.nhplug.com:8443, 모의: https://moapi.nhplug.com:8443
- OAuth2 Client Credentials 인증 (form-data: appkey, appsecretkey, scope=oob)
- 모든 TR은 POST 방식 (gbstock)
- 응답 검증: HTTP 200 + message.msg_code가 비어있거나 "00000"이면 성공
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → LIMIT 자동 변환

사용법:
    broker = NHPlugBroker()
    price = broker.get_stock_price("TQQQ", "NAS")
    result = broker.place_order("TQQQ", "NASD", "BUY", 10, 50.0, "LOC")

DRY 모드는 DryBroker 래퍼로 처리 — NHPlugBroker 자체는 항상 LIVE로 동작.
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
from broker.market_utils import (
    get_kst_now,
    is_us_trading_day,
    normalize_order_price,
    resolve_real_kst_from_ord,
)
# 공개 로그용 주문번호 마스킹 (표시 전용 — 주문번호 값 자체는 절대 바꾸지 않습니다)
from log_mask import mask_odno
from broker.nhplug.session import NHPlugSession
from broker.nhplug.auth import get_access_token
from broker.nhplug.exchange import convert_exchange_code, get_api_exchange_code
from broker.nhplug.order_types import (
    get_ord_dvsn,
    DEMO_UNSUPPORTED_ORDER_TYPES,
    TR_ID_ACCTINFO,
    TR_ID_PRICE,
    TR_ID_PERIOD,
    TR_ID_BALANCE,
    TR_ID_BUYABLE_AMOUNT,
    TR_ID_DAILY_TRANSACTION,
    TR_ID_UNEXECUTED,
    TR_ID_BUY_ORDER,
    TR_ID_SELL_ORDER,
)

# NHPLUG rsp_cd 성공 코드 (openapi.json 정본 기준)
# 이 외의 rsp_cd가 HTTP 200 응답에 포함되면 실패로 판정합니다.
# XA102: 실측 확인 — 모의투자 buyableAmount(매수가능금액) 조회 시
#   rsp_cd="XA102", rsp_msg="모의투자 조회가 완료되었습니다"가 정상 성공 응답으로
#   반환됩니다 (HTTP 200, Output_0에 orr_pbl_amt 등 데이터 포함).
# 00048: 실측 확인 — 모의투자 매수주문이완료 (GH 로그 2026-08-31 00048/완료 + 잔고 3주/2주 보유 실증)
_SUCCESS_RSP_CODES = {"00000", "00166", "00221", "13578", "XA102", "00048"}


class NHPlugBroker(Broker):
    """
    NH투자증권 나무 API Broker 구현체.

    config.py의 BROKER_CONFIG["nhplug"]에서 설정을 읽습니다.
    BROKER_MODE에 따라 실전(real) / 모의(demo) 도메인이 결정됩니다.
    """

    def __init__(self):
        from config import BROKER_CONFIG, BROKER_MODE, HTTP_TIMEOUT

        self._mode = BROKER_MODE  # "real" or "demo"
        self._domain = BROKER_CONFIG["domain"]
        self._app_key = BROKER_CONFIG["app_key"]
        self._app_secret = BROKER_CONFIG["app_secret"]
        self._account_no = BROKER_CONFIG.get("account_no", "")
        self._acct_type = BROKER_CONFIG.get(
            "acct_type", "01" if self._mode == "real" else "03"
        )
        # 토큰 발급 전용 운영 도메인 — 모의투자 서버(moapi)는 토큰 발급을 지원하지 않습니다.
        self._live_domain = BROKER_CONFIG.get(
            "live_domain", "https://api.nhplug.com:8443"
        )
        self._session = NHPlugSession(
            self._domain,
            app_key=self._app_key,
            app_secret=self._app_secret,
            timeout=HTTP_TIMEOUT,
        )

        # rate-limit: 실전/모의 공통으로 보수적 대기 (모의 1회/초 기준)
        self._rate_limit_wait = 0.05 if self._mode == "real" else 1.0

    @property
    def name(self) -> str:
        """증권사 식별자"""
        return "nhplug"

    # ═══════════════════════════════════════════════════════════════════
    # 내부 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """접근 토큰을 획득합니다 (auth.get_access_token 캐싱 포함).

        토큰 발급은 항상 운영 도메인(self._live_domain)에서 수행합니다.
        모의투자 서버(moapi)는 토큰 발급을 지원하지 않아 403을 반환합니다.
        """
        try:
            return get_access_token(
                domain=self._live_domain,
                app_key=self._app_key,
                app_secret=self._app_secret,
                timeout=self._session.timeout,
                session=self._session,
            )
        except AuthError:
            raise
        except Exception as e:
            raise BrokerError(f"NHPLUG 토큰 획득 실패: {str(e)}")

    def _get_account_no(self) -> str:
        """
        주문/잔고 API에 사용할 계좌번호를 반환합니다.

        NHPLUG API의 acct_no는 모든 해외주식 주문/잔고 API의 필수 필드입니다.
        NHPLUG_ACCT_NO 환경변수가 필수이며, 미설정 시 BrokerError를 발생합니다.

        Returns:
            str: 계좌번호

        Raises:
            BrokerError: NHPLUG_ACCT_NO가 설정되지 않은 경우
        """
        if self._account_no:
            return self._account_no
        raise BrokerError(
            "NHPLUG_ACCT_NO 환경변수가 설정되지 않았습니다. "
            "NHPLUG 계좌번호를 설정해주세요."
        )

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
    def _extract_output(data: dict, block: str = "Output_0"):
        """
        응답에서 Output 블록을 추출합니다.

        NHPLUG 응답은 Output_0(단건)/Output_1(복수) 블록을 사용합니다.
        방어적으로 소문자 output 키도 지원합니다.
        """
        for key in (block, block.lower(), "output"):
            if key in data:
                return data[key]
        return None

    @staticmethod
    def _is_rate_limit_error(code: str, message: str) -> bool:
        """rate-limit 오류 여부를 판단합니다 (HTTP 429 또는 error envelope)."""
        code_l = (code or "").lower()
        msg = message or ""
        return (
            "rate" in code_l
            or "limit" in code_l
            or "초당" in msg
            or ("호출" in msg and "제한" in msg)
        )

    def _build_body(self, tr_cd: str, symbol: str, input_block: dict) -> dict:
        """NHPLUG 공통 요청 바디를 구성합니다."""
        return {
            "tr_cd": tr_cd,
            "tr_key": symbol.upper(),
            "tr_cont": "N",
            "Input_0": input_block,
        }

    def _input_block(self, **fields) -> dict:
        """
        Input_0 블록을 구성합니다.

        계좌번호(act_no)는 NHPLUG 주문/잔고 API의 필수 필드이므로 항상 포함합니다.
        NHPLUG_ACCT_NO 환경변수가 필수입니다.
        """
        block = {"acct_type": self._acct_type}
        block["act_no"] = self._get_account_no()
        block.update(fields)
        return block

    def _request_with_rate_retry(
        self,
        path: str,
        body: dict,
        token: str,
        retry_network: bool = True,
        domain: str | None = None,
    ) -> dict:
        """
        NHPLUG API 요청 래퍼 — rate-limit/타임아웃 재시도 포함.

        - rate-limit(HTTP 429 또는 message 봉투의 rate-limit 코드):
          fixed-wait 후 재시도 (최대 3회)
        - 타임아웃(ConnectTimeout, ReadTimeout): 지수 백오프 + jitter 후 재시도 (최대 3회)

        Parameters:
            path: API 경로
            body: 요청 바디
            token: 접근 토큰
            retry_network: 네트워크 오류 재시도 여부 (주문은 False)
            domain: 요청에 사용할 베이스 도메인. 미지정 시 모드별 도메인 사용.
                    시세 API(quote 4종)는 모의투자 서버(moapi)가 지원하지 않으므로
                    self._live_domain(운영 도메인)을 전달해 라우팅합니다.

        기본은 dict(파싱된 JSON)를 반환합니다.

        에러 판정 (openapi.json 정본 기준):
        - 성공: message.msg_code가 비어있거나 "00000"
        - 실패: message.msg_code가 비어있지 않고 "00000"이 아니면
          usr_msg/dvlp_msg로 BrokerError 발생
        - message 블록이 없으면 데이터 없음 (성공으로 간주)

        Raises:
            BrokerError: message 봉투(비즈니스 오류) 또는 재시도 초과 시
        """
        MAX_RETRIES = 3
        network_retry_count = 0

        while network_retry_count <= MAX_RETRIES:
            try:
                for _ in range(MAX_RETRIES + 1):
                    time.sleep(self._rate_limit_wait)
                    resp = self._session.request(
                        "POST", path, token, json_body=body, domain=domain
                    )

                    # rate-limit (HTTP 429) 처리
                    if resp.status_code == 429:
                        print("⏳ NHPLUG rate-limit 초과 (429), 재시도...")
                        time.sleep(self._rate_limit_wait)
                        continue

                    try:
                        resp.raise_for_status()
                    except requests.exceptions.HTTPError:
                        # HTTP 오류 응답에 rate-limit 코드가 있는지 확인
                        try:
                            data = resp.json()
                            message_block = data.get("message") or {}
                            code = str(message_block.get("msg_code", "") or "")
                            message = str(message_block.get("usr_msg", "") or "")
                            # 방어적 fallback: 비표준 error/rsp_cd 봉투도 확인
                            if not code:
                                error = data.get("error", {}) or {}
                                code = str(error.get("code", ""))
                                message = str(error.get("message", ""))
                            if not code:
                                code = str(data.get("rsp_cd", "") or "")
                                message = str(data.get("rsp_msg", "") or "")
                        except Exception:
                            code = ""
                            message = ""
                        if self._is_rate_limit_error(code, message):
                            time.sleep(self._rate_limit_wait)
                            continue
                        raise

                    # JSON 파싱
                    try:
                        data = resp.json()
                    except ValueError:
                        raise BrokerError(
                            f"NHPLUG 응답 파싱 실패: {resp.text[:200]}"
                        )

                    # HTTP 200 + 에러 봉투 처리 (NHPLUG 공통 에러 형식)
                    # 1) message 봉투: msg_code가 비어있거나 "00000"이거나 usr_msg에 "완료" 포함이면 성공 (공식 SDK 안전망)
                    message_block = data.get("message") or {}
                    msg_code = str(message_block.get("msg_code", "") or "")
                    usr_msg = str(message_block.get("usr_msg", "알 수 없는 오류"))
                    if msg_code and msg_code != "00000" and "완료" not in usr_msg:
                        dvlp_msg = str(message_block.get("dvlp_msg", ""))
                        if self._is_rate_limit_error(msg_code, usr_msg):
                            print(f"⏳ NHPLUG rate-limit 감지 ({msg_code}), 재시도...")
                            time.sleep(self._rate_limit_wait)
                            continue
                        detail = f" [{dvlp_msg}]" if dvlp_msg else ""
                        raise BrokerError(
                            f"NHPLUG API 오류 [{msg_code}]: {usr_msg}{detail}"
                        )

                    # 2) rsp_cd/rsp_msg 봉투: 성공 코드(00000/00166/00221/13578/XA102/00048)가
                    #    아니면 실패 (예: "14580" 모의투자 장종료) — 단 rsp_msg에 "완료" 포함이면 성공 (공식 SDK 안전망)
                    rsp_cd = str(data.get("rsp_cd", "") or "")
                    rsp_msg = str(data.get("rsp_msg", "알 수 없는 오류"))
                    if rsp_cd and rsp_cd not in _SUCCESS_RSP_CODES and "완료" not in rsp_msg:
                        if self._is_rate_limit_error(rsp_cd, rsp_msg):
                            print(f"⏳ NHPLUG rate-limit 감지 ({rsp_cd}), 재시도...")
                            time.sleep(self._rate_limit_wait)
                            continue
                        raise BrokerError(f"NHPLUG API 오류 [{rsp_cd}]: {rsp_msg}")

                    return data

                raise BrokerError("API 호출 실패: 초당 호출 제한 재시도 초과")

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if not retry_network:
                    raise
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

    def _normalize_unexecuted_item(self, raw: dict) -> dict:
        """
        GSB10030(unexecuted) 응답 필드를 state.py 표준 필드로 변환.
        openapi.json 기준: orr_dt, orr_tm, orr_no, iem_cd, sby_dit_nm, orr_qty, cns_qty, cns_pr, ny_cns_orr_qty 등
        → state 기대 필드(ord_dt, ord_tmd, odno, ft_ord_qty, ft_ccld_qty 등)로 매핑.
        """
        # GSB10030 필드 → KIS 호환 필드로 매핑
        ord_dt = raw.get("orr_dt", "") or raw.get("ord_dt", "")
        # orr_tm 은 HHMMSS 형태, 없으면 000000
        ord_tmd_raw = raw.get("orr_tm", "") or raw.get("ord_tmd", "") or raw.get("orr_tmd_raw", "")
        ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""
        # 수량/가격: 원본 필드 우선, 없으면 KIS 호환 필드 fallback
        ft_ord_qty = raw.get("orr_qty", raw.get("ft_ord_qty"))
        ft_ccld_qty = raw.get("cns_qty", raw.get("ft_ccld_qty", "0"))
        ft_ccld_unpr3 = raw.get("cns_pr", raw.get("ft_ccld_unpr3", "0"))
        # 체결금액은 단가*수량으로 계산하거나 fc_trd_amt 있으면 사용
        ft_ccld_amt3 = raw.get("ft_ccld_amt3", "")
        if not ft_ccld_amt3 or ft_ccld_amt3 == "0":
            try:
                amt = float(ft_ccld_unpr3) * float(ft_ccld_qty)
                ft_ccld_amt3 = str(amt) if amt else "0"
            except Exception:
                ft_ccld_amt3 = raw.get("fc_trd_amt", "0")
        nccs_qty = raw.get("ny_cns_orr_qty", raw.get("nccs_qty", "0"))
        odno = str(raw.get("orr_no", raw.get("odno", "")))
        sby = raw.get("sby_dit_nm", raw.get("sll_buy_dvsn_cd_name", ""))
        # 매도/매수 한글 보정: 0=전체, 1=매도, 2=매수 → 한글로 변환 (테스트 호환)
        if sby in ("1", "01"):
            sby = "매도"
        elif sby in ("2", "02"):
            sby = "매수"
        prcs_stat = raw.get("orr_sts_nm", raw.get("prcs_stat_name", ""))
        if not prcs_stat:
            # 체결/미체결 구분: cns_qty >0 이면 체결
            try:
                prcs_stat = "체결" if float(ft_ccld_qty) > 0 else "미체결"
            except Exception:
                prcs_stat = ""
        # 시간 처리: ord_dt(미국영업일)+ord_tmd(KST) → 실제 KST 복원 후 UTC 변환 (오라클 필수 조치 1)
        # GSB10030은 ord_tmd 없이 일자만 오므로 04:10 KST를 가정, resolve 실패 시 직접 파싱 폴백
        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None
        resolved_kst_iso = None
        if ord_dt:
            ord_tmd_for_resolve = ord_tmd if ord_tmd else "041000"
            try:
                resolved_kst = resolve_real_kst_from_ord(ord_dt, ord_tmd_for_resolve)
                if resolved_kst is not None:
                    ord_datetime_kst_iso = resolved_kst.isoformat()
                    ord_datetime_utc_iso = resolved_kst.astimezone(ZoneInfo("UTC")).isoformat()
                    resolved_kst_iso = resolved_kst.isoformat()
                elif ord_tmd:
                    # resolve 실패 시 기존 직접 파싱 유지 (폴백)
                    kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                    kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                    ord_datetime_kst_iso = kst_dt.isoformat()
                    ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except Exception:
                pass
        row = {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd_raw,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "_ord_dt_is_us_trading_date": True,
            "_resolved_kst_iso": resolved_kst_iso,
            "prdt_name": raw.get("tck_iem_cd", raw.get("iem_cd", raw.get("prdt_name", ""))),
            "sll_buy_dvsn_cd_name": sby,
            "ft_ccld_qty": str(ft_ccld_qty),
            "ft_ccld_unpr3": str(ft_ccld_unpr3),
            "ft_ccld_amt3": str(ft_ccld_amt3),
            "nccs_qty": str(nccs_qty),
            "prcs_stat_name": prcs_stat,
            "tr_mket_name": raw.get("tr_mket_name", ""),
            "tr_crcy_cd": raw.get("tr_crcy_cd", "USD"),
            "odno": odno,
            "ovrs_excg_cd": raw.get("ovrs_excg_cd", ""),
        }
        # 원주문 수량 필드가 없으면 "0"을 합성하지 않고 키를 생략합니다.
        if "orr_qty" in raw or "ft_ord_qty" in raw:
            row["ft_ord_qty"] = str(ft_ord_qty)
        return row

    def _normalize_order_item(self, raw: dict) -> dict:
        """
        NHPLUG 응답 필드를 state.py가 기대하는 표준 필드명으로 변환.

        TODO: dailyTransaction(GSB10040) 응답 필드명은 openapi.json 정본 기준으로
        재확인 필요 — 응답 필드명이 KIS와 다를 수 있습니다. 실전 응답 확인 전까지는
        기존 KIS 컨벤션 매핑(ord_dt/ord_tmd/prdt_name 등)을 유지합니다.
        """
        ord_dt = raw.get("ord_dt", "")
        ord_tmd_raw = raw.get("ord_tmd", "")
        ord_tmd = ord_tmd_raw.zfill(6) if ord_tmd_raw else ""

        ord_datetime_kst_iso = None
        ord_datetime_utc_iso = None
        if ord_dt and ord_tmd:
            try:
                # NHPLUG도 한국 증권사 컨벤션(ord_dt=미국(ET) 영업일, ord_tmd=KST)을
                # 따를 것으로 가정. 상태 체인(last_updated)은 이 규칙을 유지해야 하므로
                # ord_dt를 그대로 KST로 해석합니다 (KIS/LS/KIWOOM과 동일).
                kst_dt = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
                kst_dt = kst_dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                ord_datetime_kst_iso = kst_dt.isoformat()
                ord_datetime_utc_iso = kst_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except Exception:
                pass

        # 표시용 실제 KST 복원 (ord_dt=미국영업일 + ord_tmd=KST → 실제 KST)
        resolved_kst_iso = None
        if ord_dt and ord_tmd:
            try:
                resolved_kst = resolve_real_kst_from_ord(ord_dt, ord_tmd)
                if resolved_kst is not None:
                    resolved_kst_iso = resolved_kst.isoformat()
            except Exception:
                pass

        row = {
            "ord_dt": ord_dt,
            "ord_tmd": ord_tmd_raw,
            "ord_datetime_kst": ord_datetime_kst_iso,
            "ord_datetime_utc": ord_datetime_utc_iso,
            "_ord_dt_is_us_trading_date": True,
            "_resolved_kst_iso": resolved_kst_iso,
            "prdt_name": raw.get("prdt_name", ""),
            "sll_buy_dvsn_cd_name": raw.get("sll_buy_dvsn_cd_name", ""),
            "ft_ccld_qty": raw.get("ft_ccld_qty", "0"),
            "ft_ccld_unpr3": raw.get("ft_ccld_unpr3", "0"),
            "ft_ccld_amt3": raw.get("ft_ccld_amt3", "0"),
            "nccs_qty": raw.get("nccs_qty", "0"),
            "prcs_stat_name": raw.get("prcs_stat_name", ""),
            "tr_mket_name": raw.get("tr_mket_name", ""),
            "tr_crcy_cd": raw.get("tr_crcy_cd", "USD"),
            "odno": raw.get("odno", ""),
            "ovrs_excg_cd": raw.get("ovrs_excg_cd", ""),
        }
        # 원주문 수량 필드가 없으면 "0"을 합성하지 않고 키를 생략합니다.
        if "ft_ord_qty" in raw:
            row["ft_ord_qty"] = raw["ft_ord_qty"]
        return row

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

        TR: GSS10030 (현재가)
        Input_0: iem_cd (종목코드) — 거래소/통화 필드 없음
        응답 Output_0: trdprc(현재가), open_prc(시가)

        ⚠️ 시세 API는 모의투자 서버(moapi)가 지원하지 않으므로(IGW40019)
        항상 운영 도메인(self._live_domain)으로 라우팅합니다.
        """
        token = self._get_token()

        body = self._build_body(TR_ID_PRICE, symbol, {
            "iem_cd": symbol.upper(),
        })

        try:
            data = self._request_with_rate_retry(
                "/gbstock/quote/v1/current", body, token,
                domain=self._live_domain,
            )
            output = self._extract_output(data, "Output_0") or {}
            return StockPrice(
                open=self._to_float(output.get("open_prc")),
                last=self._to_float(output.get("trdprc")),
            )
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재가 조회 실패: {str(e)}")

    def get_stock_quotation(self, symbol: str, exchange: str) -> StockQuotation:
        """
        해외주식 현재체결가를 조회합니다 → StockQuotation(tradable, last).

        TR: GSS10030 (현재가)
        Input_0: iem_cd (종목코드) — 거래소/통화 필드 없음
        NHPLUG 현재가 응답에 주문가능여부 전용 필드가 없으므로 tradable=True 기본값 사용.

        ⚠️ 시세 API는 모의투자 서버(moapi)가 지원하지 않으므로(IGW40019)
        항상 운영 도메인(self._live_domain)으로 라우팅합니다.
        """
        token = self._get_token()

        body = self._build_body(TR_ID_PRICE, symbol, {
            "iem_cd": symbol.upper(),
        })

        try:
            data = self._request_with_rate_retry(
                "/gbstock/quote/v1/current", body, token,
                domain=self._live_domain,
            )
            output = self._extract_output(data, "Output_0") or {}
            return StockQuotation(
                tradable=True,
                last=self._to_float(output.get("trdprc")),
            )
        except requests.exceptions.RequestException as e:
            raise BrokerError(f"현재체결가 조회 실패: {str(e)}")

    def get_balance(self, symbol: str, exchange: str) -> Optional[Balance]:
        """
        해외주식 보유 잔고를 조회합니다 → Balance(quantity, avg_price).

        TR: GSB10010 (잔고)
        Input_0: act_no, qut_iqr_dit_cd("1"=정규장), fc_sec_trd_nat_cd("200"),
                 cur_cd("USD")
        응답: Output_0 = 객체(집계), Output_1 = 배열(종목별: iem_cd,
             cns_bse_bnc_qty, fc_phs_uit_pr 등)
        해당 종목의 잔고가 없으면 None을 반환합니다.
        """
        token = self._get_token()
        api_exch, currency = convert_exchange_code(exchange)

        body = self._build_body(TR_ID_BALANCE, symbol, self._input_block(
            qut_iqr_dit_cd="1",
            fc_sec_trd_nat_cd=api_exch,
            cur_cd=currency,
        ))

        try:
            data = self._request_with_rate_retry(
                "/gbstock/inquiry/v1/balance", body, token
            )
            output = self._extract_output(data, "Output_1") or []
            if isinstance(output, dict):
                output = [output]
            if not output:
                return None

            # 종목 코드로 해당 항목 찾기
            sym_upper = symbol.upper()
            for item in output:
                iem_cd = str(item.get("iem_cd", "")).upper()
                if sym_upper in iem_cd:
                    return Balance(
                        quantity=int(self._to_float(item.get("cns_bse_bnc_qty"))),
                        avg_price=self._to_float(item.get("fc_phs_uit_pr")),
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

        TR: GSB10020 (매수가능금액)
        Input_0: act_no, pcs_dit("1"=매수가능금액조회), fc_sec_trd_nat_cd("200"),
                 iem_cd, wtm_cur_knd_cd("1"), oss_orr_knd_cd("1"=GTS 미국시장주문),
                 ahi_nmn_pr_tp_cd("00")
        응답 Output_0: orr_pbl_amt(주문가능금액)
        """
        token = self._get_token()
        api_exch, currency = convert_exchange_code(exchange)

        body = self._build_body(TR_ID_BUYABLE_AMOUNT, symbol, self._input_block(
            pcs_dit="1",
            fc_sec_trd_nat_cd=api_exch,
            iem_cd=symbol.upper(),
            wtm_cur_knd_cd="1",
            oss_orr_knd_cd="1",
            ahi_nmn_pr_tp_cd="00",
        ))

        try:
            data = self._request_with_rate_retry(
                "/gbstock/inquiry/v1/buyableAmount", body, token
            )
            output = self._extract_output(data, "Output_0") or {}
            if not output:
                raise BrokerError("매수가능금액 정보를 조회할 수 없습니다")

            return PurchaseAmount(
                orderable_cash=self._to_float(output.get("orr_pbl_amt")),
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

        TR: GSB10030 (주문체결내역) — GSB10040(일별거래내역)은 원장용이라 주문체결 추적 불가
        Input_0: orr_dt(YYYYMMDD, 단일일), act_no, oss_sby_dit_cd("0"=전체),
                 sot_dit("0"=전체), ost_cns_dit("0"=전체), iem_cd(선택)
        ⚠️ 단일일 조회이므로 days 만큼 루프 호출(30일≈30회, 데모 1회/초 rate-limit 준수)
        응답: Output_0 = 배열(orr_dt, orr_no, cns_qty, cns_pr 등)
        각 dict는 state.py가 기대하는 표준 필드로 정규화(_normalize_unexecuted_item):
            ord_dt, ord_tmd, ord_datetime_kst, ord_datetime_utc,
            prdt_name, sll_buy_dvsn_cd_name, ft_ord_qty, ft_ccld_qty,
            ft_ccld_unpr3, ft_ccld_amt3, nccs_qty, prcs_stat_name,
            tr_mket_name, tr_crcy_cd, odno, ovrs_excg_cd
        """
        token = self._get_token()

        # 날짜 계산 (KST 기준) — GSB10030은 단일일(orr_dt) 조회라 루프
        now_kst = get_kst_now()
        start_date = now_kst - timedelta(days=days)
        ord_strt_dt = start_date.strftime("%Y%m%d")
        ord_end_dt = now_kst.strftime("%Y%m%d")

        print(f"[주문이력] {symbol} 체결내역 조회 시작: {ord_strt_dt} ~ {ord_end_dt} (GSB10030 unexecuted, {days}일 루프)")

        order_history: list[dict] = []
        seen_odnos: set[str] = set()

        try:
            # days일치 루프: orr_dt를 하루씩 조회 (과거→현재 순)
            for offset in range(days, -1, -1):
                dt = (now_kst - timedelta(days=offset)).strftime("%Y%m%d")
                body = self._build_body(TR_ID_UNEXECUTED, symbol, self._input_block(
                    orr_dt=dt,
                    oss_sby_dit_cd="0",
                    sot_dit="0",
                    ost_cns_dit="0",
                    iem_cd=symbol.upper(),
                ))
                try:
                    data = self._request_with_rate_retry(
                        "/gbstock/inquiry/v1/unexecuted", body, token
                    )
                except BrokerError as e:
                    # 0건/조회불가 등은 빈 결과로 처리, 치명 오류는 상위로
                    # "조회할 내역이 없습니다"(13578) 등은 성공 코드로 이미 처리됨
                    print(f"[주문이력] {symbol} {dt} 조회 중 BrokerError: {e} → 건너뜀")
                    continue
                output = self._extract_output(data, "Output_0") or []
                if isinstance(output, dict):
                    output = [output]
                if output:
                    print(f"[주문이력] {symbol} {dt} 조회 성공: {len(output)}건")
                for item in output:
                    norm = self._normalize_unexecuted_item(item)
                    # 중복 제거: 동일 odno+ord_dt는 1회만
                    dedup_key = f"{norm.get('odno')}:{norm.get('ord_dt')}:{norm.get('ft_ccld_qty')}"
                    if dedup_key in seen_odnos:
                        continue
                    seen_odnos.add(dedup_key)
                    # 심볼 필터: iem_cd가 있으면 해당 종목만 (서버가 필터 안 할 경우 대비)
                    prdt = norm.get("prdt_name", "")
                    if prdt and prdt.upper() != symbol.upper():
                        # iem_cd가 다른 종목이면 스킵 (원장 필터 보정)
                        continue
                    order_history.append(norm)

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
                    ord_dt = item.get("ord_dt", "")
                    ord_tmd = (item.get("ord_tmd") or "").zfill(6)

                    # 미국 영업일 (NHPLUG 원본 ord_dt — 가공 없이 표시)
                    if ord_dt and len(ord_dt) == 8:
                        us_dt_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                    else:
                        us_dt_str = "(날짜없음)"

                    # 실제 한국 시각 (ord_dt=미국영업일 + ord_tmd=KST → 복원)
                    real_kst = None
                    if ord_dt and ord_tmd:
                        real_kst = resolve_real_kst_from_ord(ord_dt, ord_tmd)
                    if real_kst:
                        kst_str = real_kst.strftime("%Y-%m-%d %H:%M:%S") + " KST"
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
                        f"미국영업일 {us_dt_str} | 한국시각 {kst_str} | odno={mask_odno(odno)}"
                        f" | {side} | qty={qty} | price={price_s} | amt={amt_s}"
                    )

            return order_history

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"주문체결내역 조회 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════
    # 일봉 종가
    # ═══════════════════════════════════════════════════════════════════

    def get_daily_closes(self, symbol: str, exchange: str, days: int = 5) -> list[float]:
        """
        해외주식 기간별시세(일봉)를 조회합니다 → list[float] (오래된 순).

        TR: GSC10060 (기간별시세)
        Input_0: iem_cd, end_dt(YYYYMMDD), count(조회건수), maxavg("0"),
                 gubun("3"=일), xtick("0001"), today_cls("0"=종료일조회),
                 market_cls("0"=전체)
        응답: Output_0 = 배열(date, trdprc, open_prc, high, low, hst_trdprc 등),
             Output_1 = 배열(trade_date, close_prc 등)

        ⚠️ 시세 API는 모의투자 서버(moapi)가 지원하지 않으므로(IGW40019)
        항상 운영 도메인(self._live_domain)으로 라우팅합니다.
        """
        token = self._get_token()

        now_kst = get_kst_now()
        end_dt = now_kst.strftime("%Y%m%d")

        body = self._build_body(TR_ID_PERIOD, symbol, {
            "iem_cd": symbol.upper(),
            "end_dt": end_dt,
            "count": str(max(days * 2, 10)),
            "maxavg": "0",
            "gubun": "3",
            "xtick": "0001",
            "today_cls": "0",
            "market_cls": "0",
        })

        try:
            data = self._request_with_rate_retry(
                "/gbstock/quote/v1/period", body, token,
                domain=self._live_domain,
            )
            output = self._extract_output(data, "Output_1") or []
            if isinstance(output, dict):
                output = [output]

            closes = []
            for item in output:
                close = self._to_float(item.get("close_prc"))
                if close > 0:
                    closes.append(close)

            # 최신순 반환 가정 → 역순 (오래된 순)
            closes.reverse()
            return closes[-days:]

        except requests.exceptions.RequestException as e:
            raise BrokerError(f"일자별종가 조회 실패: {str(e)}")

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
        해외주식 주문을 실행합니다.

        TR: GSO10010 (매수) / GSO10020 (매도)

        - 모의투자 미지원 주문 유형(LOC 등)은 LIMIT으로 자동 변환합니다.
        - DRY 모드는 DryBroker가 처리하므로, 이 메서드는 항상 LIVE로 동작합니다.
        - NHPLUG는 예약주문을 지원하지 않으므로 is_reservation은 항상 False입니다.

        exchange 규약 (KIS place_order와 동일):
          - place_order는 호출 측에서 broker.exchange_code()로 변환된 API 국가코드
            ("200"=미국)를 받습니다. 여기서는 추가 변환 없이 그대로 사용합니다.

        Input_0 (매수): act_no, fc_sec_trd_nat_cd("200"), iem_cd, orr_qty(int),
                        fc_orr_uit_pr(float, 지정가 계열 시 필수), ahi_nmn_pr_tp_cd,
                        wtm_cur_knd_cd("1")
        Input_0 (매도): 매수와 동일하되 wtm_cur_knd_cd 없음
        응답 Output_0: orr_no(주문번호)

        ⚠️ 타입 주의 (openapi.json 정본): orr_qty는 integer(int64),
        fc_orr_uit_pr는 number(double) — 문자열로 보내면 IGW40011
        "orr_qty 길이나 data type을 확인하세요" 400 에러가 발생합니다.

        Returns:
            OrderResult: 주문 성공 시 (주문번호, 시각, 예약여부=False)
        """
        token = self._get_token()

        # 모의투자 미지원 주문 유형 자동 변환
        if self._mode != "real" and order_type in DEMO_UNSUPPORTED_ORDER_TYPES:
            print(
                f"⚠️  모의투자 미지원 주문 유형: {order_type}"
                f" → LIMIT(지정가)으로 자동 변환합니다."
            )
            order_type = "LIMIT"

        # 브로커 호가 단위 규칙에 맞춰 주문가를 정규화 ($1+ → 소수점 2자리 등)
        price = normalize_order_price(price)

        ahi_nmn_pr_tp_cd = get_ord_dvsn(order_type)

        # exchange는 이미 broker.exchange_code()로 변환된 API 국가코드("200")이므로
        # 그대로 사용합니다. convert_exchange_code()를 다시 호출하면 이중 변환됩니다.
        api_exch = exchange

        if side == "BUY":
            tr_cd = TR_ID_BUY_ORDER
            path = "/gbstock/order/v1/buy"
            body = self._build_body(tr_cd, symbol, self._input_block(
                fc_sec_trd_nat_cd=api_exch,
                iem_cd=symbol.upper(),
                orr_qty=quantity,
                fc_orr_uit_pr=price,
                ahi_nmn_pr_tp_cd=ahi_nmn_pr_tp_cd,
                wtm_cur_knd_cd="1",
            ))
        else:
            tr_cd = TR_ID_SELL_ORDER
            path = "/gbstock/order/v1/sell"
            body = self._build_body(tr_cd, symbol, self._input_block(
                fc_sec_trd_nat_cd=api_exch,
                iem_cd=symbol.upper(),
                orr_qty=quantity,
                fc_orr_uit_pr=price,
                ahi_nmn_pr_tp_cd=ahi_nmn_pr_tp_cd,
            ))

        try:
            data = self._request_with_rate_retry(
                path, body, token, retry_network=False
            )
            output = self._extract_output(data, "Output_0") or {}
            order_id = output.get("orr_no", "")

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

        except BrokerError as e:
            # error envelope 거부 응답 또는 rate-limit 재시도 초과 → 주문 미접수가 확정
            raise OrderNotAcceptedError(str(e)) from e
        except requests.exceptions.RequestException as e:
            # 네트워크 타임아웃/연결 오류 → 접수 여부 불확실 (fence 유지)
            raise OrderError(f"주문 실행 실패: {str(e)}")

    # ═══════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════

    def exchange_code(self, user_code: str) -> str:
        """사용자 거래소 코드 → API 국가코드 변환 (예: 'NAS' → '200')."""
        return get_api_exchange_code(user_code)

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()