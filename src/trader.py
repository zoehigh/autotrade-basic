# 실제 주문을 실행하는 코드
import random
import requests
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from authentication import get_access_token
from config import KIS_MODE


class ReservationOrderRequired(Exception):
    """
    모의투자에서 정규장 외 시간에 일반 주문을 시도하면 발생하는 예외입니다.

    - place_overseas_order()가 이 예외를 raise하면
      호출자는 place_overseas_reservation_order()를 대신 호출해야 합니다.
    - 두 함수는 서로 다른 엔드포인트를 사용하므로 각자 독립적으로 관리합니다.
    """
    pass


def _mask_account_no(acct):
    """
    계좌번호를 안전하게 마스킹합니다.
    - 길이가 4 이하이면 전체를 '*'로 대체
    - 그 외에는 앞2자리와 끝2자리를 남기고 중간은 '*'로 대체
    """
    s = str(acct) if acct is not None else ""
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _request_with_rate_retry(session, method, path, headers=None, params=None, json=None):
    """
    requests.request 래퍼.
    - rate-limit(EGW00201/EGW00215, 초당 거래건수 초과): fixed-wait 후 재시도 (최대 3회)
    - 타임아웃(ConnectTimeout, ReadTimeout): 지수 백오프 + jitter 후 재시도 (최대 3회)
    - rate-limit과 타임아웃 재시도는 독립적인 카운터로 관리됩니다

    실전(real): 초당 20회 → 대기 0.05s
    모의(demo): 초당 1회 → 대기 1.0s
    """
    rate_limit_wait = 0.05 if KIS_MODE == "real" else 1.0
    MAX_RETRIES = 3
    network_retry_count = 0

    while network_retry_count <= MAX_RETRIES:
        try:
            for _ in range(MAX_RETRIES + 1):
                resp = session.request(method, path, headers=headers, params=params, json=json)
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
                        msg_cd in ("EGW00201", "EGW00215") or
                        "초당 거래건수" in msg1 or
                        "초당 거래건수" in resp.text
                    )

                    if is_rate_limit:
                        time.sleep(rate_limit_wait)
                        continue
                    raise

                return resp

            raise Exception("API 호출 실패: 초당 호출 제한 재시도 초과")

        except requests.exceptions.Timeout as e:
            network_retry_count += 1
            if network_retry_count <= MAX_RETRIES:
                wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                print(f"⏳ 타임아웃 오류 발생: {str(e)[:60]}...")
                print(f"   {wait:.1f}초 후 재시도합니다... ({network_retry_count}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise


def get_overseas_stock_price(session, symbol, exchange_code="NAS"):
    """
    한국투자증권 API를 사용하여 해외주식의 현재가를 조회합니다.
    
    해외주식 시세는 지연시세(무료)로 제공됩니다.
    - 미국(NAS, NYS): 실시간 지연 없음
    - 기타 지역: 15분 지연
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NAS: 나스닥 (기본값)
            - NYS: 뉴욕
            - HKS: 홍콩
            - TSE: 도쿄
            - SHS: 상해
            - 기타 코드는 공식 문서 참고
    
    Returns:
        dict: 현재가 정보를 포함한 딕셔너리
              주요 필드:
              - rsym: 종목 코드
              - last: 현재가
              - open: 시가
              - high: 고가
              - low: 저가
              - base: 전일 종가
              - tvol: 거래량
              - tamt: 거래대금
              - perx: PER
              - pbrx: PBR
              - epsx: EPS
              - t_xprc: 원환산 당일 가격
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
    """
    
    # Step 1: 접근 토큰 획득
    # 한 번 발급한 토큰은 캐싱되어 재사용됩니다
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 요청 경로 설정
    path = "/uapi/overseas-price/v1/quotations/price-detail"
    
    # Step 3: 요청 헤더 설정
    # authorization 헤더에 "Bearer" 접두사를 붙여야 합니다
    # appkey, appsecret, content-type은 KISSession에서 공통 관리
    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": "HHDFS76200200"  # 해외주식 현재가상세 조회 API의 거래 ID
    }
    
    # Step 4: Query Parameter 설정
    # 사용자 권한 정보와 조회 조건을 포함합니다
    params = {
        "AUTH": "",  # 사용자 권한 정보 (개인 고객은 빈 값)
        "EXCD": exchange_code,  # 거래소 코드 (NAS = 나스닥)
        "SYMB": symbol  # 종목 코드 (예: TQQQ)
    }
    
    # Step 5: API 호출
    try:
        response = _request_with_rate_retry(session, "GET", path, headers=headers, params=params)
        response.raise_for_status()  # HTTP 에러 발생 시 예외 던지기
        
        # Step 6: 응답 데이터 추출
        response_data = response.json()
        
        # API 응답이 정상인지 확인
        if response_data.get("rt_cd") != "0":  # rt_cd가 0이면 성공
            msg = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"API 호출 실패: {msg}")
        
        # 가격 정보 반환
        return response_data.get("output", {})
    
    except requests.exceptions.RequestException as e:
        raise Exception(f"현재가 조회 실패: {str(e)}")


def get_overseas_stock_quotation(session, symbol, exchange_code="NAS"):
    """
    한국투자증권 API를 사용하여 해외주식의 현재체결가를 조회합니다.
    
    주문 가능 여부(ordy)를 확인하기 위해 사용합니다.
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NAS: 나스닥 (기본값)
            - NYS: 뉴욕
            - HKS: 홍콩
            - TSE: 도쿄
            - SHS: 상해
            - SZS: 심천
            - HSX: 호치민
            - HNX: 하노이
            - BAQ: 나스닥(주간거래)
            - BAY: 뉴욕(주간거래)
            - BAA: 아멕스(주간거래)
    
    Returns:
        dict: 현재체결가 정보를 포함한 딕셔너리
              주요 필드:
              - rsym: 실시간조회종목코드 (D+시장구분+종목코드)
              - last: 현재가
              - base: 전일 종가
              - diff: 대비 (현재가 - 전일종가)
              - rate: 등락율
              - sign: 대비기호 (1:상한, 2:상승, 3:보합, 4:하한, 5:하락)
              - tvol: 거래량
              - tamt: 거래대금
              - ordy: 매수가능여부 (주문 가능 여부)
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
    """
    
    # Step 1: 접근 토큰 획득
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 요청 경로 설정
    path = "/uapi/overseas-price/v1/quotations/price"
    
    # Step 3: 요청 헤더 설정
    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": "HHDFS00000300"  # 해외주식 현재체결가 조회 API의 거래 ID
    }
    
    # Step 4: Query Parameter 설정
    params = {
        "AUTH": "",  # 사용자 권한 정보 (개인 고객은 빈 값)
        "EXCD": exchange_code,  # 거래소 코드
        "SYMB": symbol  # 종목 코드
    }
    
    # Step 5: API 호출
    try:
        response = _request_with_rate_retry(session, "GET", path, headers=headers, params=params)
        response.raise_for_status()
        
        # Step 6: 응답 데이터 추출
        response_data = response.json()
        
        # API 응답이 정상인지 확인
        if response_data.get("rt_cd") != "0":
            msg = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"API 호출 실패: {msg}")
        
        # 현재체결가 정보 반환
        return response_data.get("output", {})
    
    except requests.exceptions.RequestException as e:
        raise Exception(f"현재체결가 조회 실패: {str(e)}")


def _convert_exchange_code(exchange_code):
    """
    API 호출에 사용되는 거래소 코드를 변환합니다.
    
    Parameters:
        exchange_code (str): 사용자 입력 거래소 코드 (NAS, NYS, HKS 등)
    
    Returns:
        tuple: (API 요청용 거래소 코드, 통화 코드)
    """
    exchange_map = {
        "NAS": ("NASD", "USD"),      # 나스닥
        "NYS": ("NYSE", "USD"),      # 뉴욕
        "AMS": ("AMEX", "USD"),      # 아멕스
        "HKS": ("SEHK", "HKD"),      # 홍콩
        "TSE": ("TKSE", "JPY"),      # 도쿄
        "SHS": ("SHAA", "CNY"),      # 상해
        "SZS": ("SZAA", "CNY"),      # 심천
        "HSX": ("HASE", "VND"),      # 베트남 하노이
        "HNX": ("VNSE", "VND"),      # 베트남 호치민
    }
    
    if exchange_code in exchange_map:
        return exchange_map[exchange_code]
    else:
        raise Exception(f"지원하지 않는 거래소 코드입니다: {exchange_code}")


def _get_kst_now():
    """한국시간(KST) 현재 시각을 반환합니다."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _is_us_dst() -> bool:
    """현재 시각 기준으로 미국 동부시간(ET)의 서머타임 적용 여부를 반환합니다."""
    ny_now = datetime.now(ZoneInfo("America/New_York"))
    return bool(ny_now.dst() and ny_now.dst() != timedelta(0))


def _is_kst_regular_market(now_kst: datetime) -> bool:
    """KST 기준 정규장 여부 검사.

    정규장 시간 (KST):
      - 서머타임(미국 DST 적용): 23:30 ~ 익일 06:00
      - 비서머타임: 22:30 ~ 익일 05:00
    """
    is_dst = _is_us_dst()
    t = now_kst.time()
    if is_dst:
        start = dtime(23, 30)
        end = dtime(6, 0)
    else:
        start = dtime(22, 30)
        end = dtime(5, 0)

    # wrap-around 범위 처리
    return (t >= start) or (t <= end)


def _is_kst_reserve_window(now_kst: datetime) -> bool:
    """KST 기준 예약주문 가능시간 검사.

    예약주문 가능시간 (KST):
      - 서머타임(미국 DST 적용): 10:00 ~ 22:20
      - 비서타임: 10:00 ~ 23:20
    """
    is_dst = _is_us_dst()
    t = now_kst.time()
    start = dtime(10, 0)
    end = dtime(22, 20) if is_dst else dtime(23, 20)
    return (t >= start) and (t <= end)


def get_overseas_balance(session, symbol, exchange_code="NAS"):
    """
    한국투자증권 API를 사용하여 해외주식의 보유 잔고를 조회합니다.
    
    특정 종목의 보유 수량과 평단가 정보를 반환합니다.
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NAS: 나스닥
            - NYS: 뉴욕
            - AMS: 아멕스
            - HKS: 홍콩
            - TSE: 도쿄
            - SHS: 상해
            - SZS: 심천
            - HSX: 호치민
            - HNX: 하노이
    
    Returns:
        dict: 특정 종목의 잔고 정보
              - symbol: 종목 코드
              - quantity: 보유 수량 (ovrs_cblc_qty)
              - avg_price: 평단가 (pchs_avg_pric)
              - 기타 필드: 해외주식명, 평가손익율, 거래통화코드 등
        
        None: 해당 종목의 잔고가 없을 경우
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
    """
    
    from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD

    # 필수 설정 검증: KIS_ACCOUNT_NO는 필수입니다
    if not KIS_ACCOUNT_NO:
        raise Exception("KIS_ACCOUNT_NO가 설정되어 있지 않습니다. 환경변수 또는 .env 파일에 KIS_ACCOUNT_NO를 추가하세요.")

    # Step 1: 접근 토큰 획득
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 거래소 코드와 통화 코드 변환
    try:
        api_exchange_code, currency_code = _convert_exchange_code(exchange_code)
    except Exception as e:
        raise Exception(f"거래소 코드 변환 실패: {str(e)}")
    
    # Step 3: 요청 경로 설정
    path = "/uapi/overseas-stock/v1/trading/inquire-balance"
    
    # Step 4: 요청 헤더 설정
    # TR_ID는 실전/모의에 따라 다릅니다
    balance_tr_id = "TTTS3012R" if KIS_MODE == "real" else "VTTS3012R"
    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": balance_tr_id
    }
    
    # Step 5: Query Parameter 설정
    params = {
        "CANO": KIS_ACCOUNT_NO,           # 종합계좌번호 (8자리)
        "ACNT_PRDT_CD": ACNT_PRDT_CD,    # 계좌상품코드 (01)
        "OVRS_EXCG_CD": api_exchange_code,  # 해외거래소코드
        "TR_CRCY_CD": currency_code,      # 거래통화코드
        "CTX_AREA_FK200": "",             # 연속조회검색조건200 (초기 조회시 공란)
        "CTX_AREA_NK200": ""              # 연속조회키200 (초기 조회시 공란)
    }
    
    # Step 6: API 호출
    try:
        response = _request_with_rate_retry(session, "GET", path, headers=headers, params=params)
        response.raise_for_status()
        
        # Step 7: 응답 데이터 추출
        response_data = response.json()
        
        # API 응답이 정상인지 확인
        if response_data.get("rt_cd") != "0":
            msg = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"API 호출 실패: {msg}")
        
        # Step 8: output1 (잔고 정보 배열)에서 해당 종목 찾기
        output1 = response_data.get("output1", [])
        
        if not output1:
            return None  # 보유 잔고가 없음
        
        # 종목 코드로 해당 항목 찾기
        for item in output1:
            # API에서 반환된 해외상품번호에서 종목 코드 추출
            ovrs_pdno = item.get("ovrs_pdno", "")
            
            # 해외상품번호는 보통 종목코드를 포함하고 있음
            if symbol.upper() in ovrs_pdno.upper():
                return {
                    "symbol": symbol.upper(),
                    "quantity": item.get("ovrs_cblc_qty", "0"),  # 보유 수량
                    "avg_price": item.get("pchs_avg_pric", "0"),  # 평단가
                    "item_name": item.get("ovrs_item_name", ""),  # 해외종목명
                    "eval_rate": item.get("evlu_pfls_rt", "0"),  # 평가손익율
                    "currency": item.get("tr_crcy_cd", ""),  # 거래통화코드
                    "exchange": item.get("ovrs_excg_cd", ""),  # 거래소코드
                    "current_price": item.get("now_pric2", "0"),  # 현재가
                    "eval_amount": item.get("ovrs_stck_evlu_amt", "0")  # 평가금액
                }
        
        # 해당 종목의 잔고가 없음
        return None
    
    except requests.exceptions.RequestException as e:
        # 가능하면 서버 응답 본문을 함께 표시하여 디버깅에 도움을 줍니다
        resp_info = ""
        try:
            if hasattr(e, 'response') and e.response is not None:
                resp = e.response
                resp_info = f" (status={resp.status_code}) response_body={resp.text}"
        except Exception:
            # 응답 파싱 중 문제 발생하면 무시하고 원래 예외 메시지 사용
            resp_info = ""

        raise Exception(f"잔고 조회 실패: {str(e)}{resp_info}")


def get_overseas_purchase_amount(session, symbol, exchange_code="NAS"):
    """
    한국투자증권 API를 사용하여 해외주식의 매수가능금액을 조회합니다.
    
    주문가능외화금액(ord_psbl_frcr_amt) 정보를 포함하여 반환합니다.
    이 함수는 현재가를 기준으로 매수 가능한 외화 금액을 확인합니다.
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NAS: 나스닥
            - NYS: 뉴욕
            - AMS: 아멕스
            - HKS: 홍콩
            - TSE: 도쿄
            - SHS: 상해
            - SZS: 심천
            - HSX: 호치민
            - HNX: 하노이
    
    Returns:
        dict: 매수가능금액 정보를 포함한 딕셔너리
              주요 필드:
              - ord_psbl_frcr_amt: 주문가능외화금액 (핵심 정보)
              - max_ord_psbl_qty: 최대주문가능수량
              - ord_psbl_qty: 주문가능수량
              - exrt: 환율
              - tr_crcy_cd: 거래통화코드
              - 기타 필드 참고
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
    """
    
    from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD
    
    # Step 1: 접근 토큰 획득
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 현재가 조회 (단가 정보 필요)
    # 먼저 현재 가격을 조회하여 OVRS_ORD_UNPR (주문단가)로 사용
    try:
        quotation = get_overseas_stock_quotation(session, symbol=symbol, exchange_code=exchange_code)
        current_price = quotation.get("last", "0")
        
        if not current_price or current_price == "0":
            raise Exception("현재가 조회 실패: 유효한 가격을 얻을 수 없습니다")
    except Exception as e:
        raise Exception(f"현재가 조회 실패: {str(e)}")
    
    # Step 3: 거래소 코드와 통화 코드 변환
    try:
        api_exchange_code, currency_code = _convert_exchange_code(exchange_code)
    except Exception as e:
        raise Exception(f"거래소 코드 변환 실패: {str(e)}")
    
    # Step 4: 요청 경로 설정
    path = "/uapi/overseas-stock/v1/trading/inquire-psamount"
    
    # Step 5: 요청 헤더 설정
    psamount_tr_id = "TTTS3007R" if KIS_MODE == "real" else "VTTS3007R"
    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": psamount_tr_id
    }
    
    # Step 6: Query Parameter 설정
    params = {
        "CANO": KIS_ACCOUNT_NO,           # 종합계좌번호 (8자리)
        "ACNT_PRDT_CD": ACNT_PRDT_CD,    # 계좌상품코드 (01)
        "OVRS_EXCG_CD": api_exchange_code,  # 해외거래소코드
        "OVRS_ORD_UNPR": current_price,   # 해외주문단가 (현재가 사용)
        "ITEM_CD": symbol.upper()         # 종목코드
    }
    
    # Step 7: API 호출
    try:
        response = _request_with_rate_retry(session, "GET", path, headers=headers, params=params)
        response.raise_for_status()
        
        # Step 8: 응답 데이터 추출
        response_data = response.json()
        
        # API 응답이 정상인지 확인
        if response_data.get("rt_cd") != "0":
            msg = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"API 호출 실패: {msg}")
        
        # Step 9: 매수가능금액 정보 반환
        output = response_data.get("output", {})
        
        if not output:
            raise Exception("매수가능금액 정보를 조회할 수 없습니다")
        
        return {
            "symbol": symbol.upper(),
            "current_price": current_price,  # 조회에 사용한 단가
            "ord_psbl_frcr_amt": output.get("ord_psbl_frcr_amt", "0"),  # 주문가능외화금액 (핵심)
            "max_ord_psbl_qty": output.get("max_ord_psbl_qty", "0"),    # 최대주문가능수량
            "ord_psbl_qty": output.get("ord_psbl_qty", "0"),            # 주문가능수량
            "exrt": output.get("exrt", "0"),                            # 환율
            "tr_crcy_cd": output.get("tr_crcy_cd", ""),                 # 거래통화코드
            "ovrs_ord_psbl_amt": output.get("ovrs_ord_psbl_amt", "0"),  # 해외주문가능금액
            "frcr_ord_psbl_amt1": output.get("frcr_ord_psbl_amt1", "0"), # 외화주문가능금액1
            "ovrs_max_ord_psbl_qty": output.get("ovrs_max_ord_psbl_qty", "0"), # 해외최대주문가능수량
            "sll_ruse_psbl_amt": output.get("sll_ruse_psbl_amt", "0")   # 매도재사용가능금액
        }
    
    except requests.exceptions.RequestException as e:
        raise Exception(f"매수가능금액 조회 실패: {str(e)}")


def get_overseas_order_history(session, symbol, exchange_code="NAS", days=30, verbose=False, limit=100):
    """
    한국투자증권 API를 사용하여 해외주식의 최근 주문체결내역을 조회합니다.
    
    최근 N일(기본 30일)의 체결내역을 전부 조회합니다.
    연속조회(tr_cont)를 통해 한 번에 최대 20건(실전) / 15건(모의)씩 여러 페이지를 모두 수집합니다.
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NAS: 나스닥
            - NYS: 뉴욕
            - AMS: 아멕스
            - HKS: 홍콩
            - TSE: 도쿄
            - SHS: 상해
            - SZS: 심천
            - HSX: 호치민
            - HNX: 하노이
        days (int): 조회 기간 (기본 30일)
    
    Returns:
        list: 주문체결내역 배열 (최신순으로 정렬)
              각 항목의 필드:
              - ord_dt: 주문일자
              - prdt_name: 상품명 (종목명)
              - sll_buy_dvsn_cd_name: 매도매수구분 (매도/매수)
              - ft_ord_qty: 주문수량
              - ft_ccld_qty: 체결수량 (핵심 정보)
              - ft_ccld_unpr3: 체결단가
              - ft_ccld_amt3: 체결금액
              - prcs_stat_name: 처리상태
              - 기타 필드 참고
        
        []: 체결내역이 없을 경우 빈 배열

    Note:
        - `verbose` (bool): True이면 사람이 읽기 쉬운 요약(최근 `limit`건)을 로그로 출력합니다.
        - `limit` (int): `verbose`일 때 출력할 최근 건수(기본 10)
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
    """
    
    from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD
    from datetime import datetime, timedelta

    # 필수 설정 검증: KIS_ACCOUNT_NO는 필수입니다
    if not KIS_ACCOUNT_NO:
        raise Exception("KIS_ACCOUNT_NO가 설정되어 있지 않습니다. 환경변수 또는 .env 파일에 KIS_ACCOUNT_NO를 추가하세요.")
    
    # Step 1: 접근 토큰 획득
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 날짜 계산 (KST 기준으로 API 요청 날짜를 생성)
    now_kst = _get_kst_now()
    start_date = now_kst - timedelta(days=days)

    ord_end_dt = now_kst.strftime("%Y%m%d")
    ord_strt_dt = start_date.strftime("%Y%m%d")
    
    # Step 3: 거래소 코드와 통화 코드 변환
    try:
        api_exchange_code, currency_code = _convert_exchange_code(exchange_code)
    except Exception as e:
        raise Exception(f"거래소 코드 변환 실패: {str(e)}")
    
    # Step 4: 요청 경로 설정
    path = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
    
    # Step 5: 요청 헤더 설정
    order_history_tr_id = "TTTS3035R" if KIS_MODE == "real" else "VTTS3035R"
    base_headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": order_history_tr_id
    }
    
    # Step 6: Query Parameter 기본값 설정
    base_params = {
        "CANO": KIS_ACCOUNT_NO,           # 종합계좌번호 (8자리)
        "ACNT_PRDT_CD": ACNT_PRDT_CD,    # 계좌상품코드 (01)
        "PDNO": symbol.upper(),           # 상품번호 (종목 코드 - API 레벨에서 필터링)
        "ORD_STRT_DT": ord_strt_dt,       # 주문시작일자
        "ORD_END_DT": ord_end_dt,         # 주문종료일자
        "SLL_BUY_DVSN": "00",             # 매도매수구분 (00: 전체)
        "CCLD_NCCS_DVSN": "01",           # 체결미체결구분 (01: 체결만)
        "OVRS_EXCG_CD": api_exchange_code,  # 해외거래소코드
        "SORT_SQN": "AS",                 # 정렬순서 (AS: 역순, 최신이 먼저)
        "ORD_DT": "",                     # 주문일자 (Null)
        "ORD_GNO_BRNO": "",               # 주문채번지점번호 (Null)
        "ODNO": "",                       # 주문번호 (Null)
        "CTX_AREA_NK200": "",             # 연속조회키200 (초기조회는 공란)
        "CTX_AREA_FK200": ""              # 연속조회검색조건200 (초기조회는 공란)
    }

    # Step 7: 연속조회 루프 - 전체 체결 이력을 페이지 단위로 수집
    # API는 한 번에 최대 20건(실전) / 15건(모의)을 반환합니다.
    # 응답 헤더의 tr_cont가 "M"이면 다음 페이지가 있음을 의미합니다.
    order_history = []
    ctx_area_nk200 = ""
    ctx_area_fk200 = ""
    is_first_call = True

    print(f"[주문이력] {symbol} 체결내역 조회 시작: ord_strt_dt={ord_strt_dt}, ord_end_dt={ord_end_dt}")

    try:
        while True:
            headers = dict(base_headers)
            params = dict(base_params)

            if not is_first_call:
                # 2번째 이후 요청: 연속조회를 위한 헤더와 파라미터 추가
                headers["tr_cont"] = "N"
                params["CTX_AREA_NK200"] = ctx_area_nk200
                params["CTX_AREA_FK200"] = ctx_area_fk200

            print(f"[주문이력] {symbol} 체결내역 페이지 조회 시도: ord_strt_dt={ord_strt_dt}, ord_end_dt={ord_end_dt}, tr_cont={headers.get('tr_cont', '첫페이지')}")
            response = _request_with_rate_retry(session, "GET", path, headers=headers, params=params)
            response.raise_for_status()

            response_data = response.json()
            print(f"[주문이력] {symbol} 체결내역 페이지 조회 성공: {len(response_data.get('output', []))}건, tr_cont={response.headers.get('tr_cont', '')}")

            if response_data.get("rt_cd") != "0":
                msg = response_data.get("msg1", "알 수 없는 에러")
                print(f"[주문이력] {symbol} API 오류 상세:")
                print(f"  rt_cd={response_data.get('rt_cd')}")
                print(f"  msg1={msg}")
                print(f"  tr_id={headers.get('tr_id', '')}")
                print(f"  KIS_MODE={KIS_MODE}")
                print(f"  CANO={params.get('CANO', '')}")
                print(f"  OVRS_EXCG_CD={params.get('OVRS_EXCG_CD', '')}")
                print(f"  ORD_STRT_DT={params.get('ORD_STRT_DT', '')}~{params.get('ORD_END_DT', '')}")
                raise Exception(f"API 호출 실패: {msg}")

            # Step 8: 이번 페이지 체결내역 추출
            output = response_data.get("output", [])

            for item in output:
                # ord_dt: YYYYMMDD, ord_tmd: HHMMSS (may be empty)
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
                        ord_datetime_kst_iso = None
                        ord_datetime_utc_iso = None

                order_history.append({
                    "ord_dt": ord_dt,
                    "ord_tmd": ord_tmd_raw,
                    "ord_datetime_kst": ord_datetime_kst_iso,
                    "ord_datetime_utc": ord_datetime_utc_iso,
                    "prdt_name": item.get("prdt_name", ""),        # 상품명 (종목명)
                    "sll_buy_dvsn_cd_name": item.get("sll_buy_dvsn_cd_name", ""),  # 매도/매수 (핵심)
                    "ft_ord_qty": item.get("ft_ord_qty", "0"),     # 주문수량
                    "ft_ccld_qty": item.get("ft_ccld_qty", "0"),   # 체결수량 (핵심)
                    "ft_ccld_unpr3": item.get("ft_ccld_unpr3", "0"),  # 체결단가
                    "ft_ccld_amt3": item.get("ft_ccld_amt3", "0"),    # 체결금액
                    "nccs_qty": item.get("nccs_qty", "0"),         # 미체결수량
                    "prcs_stat_name": item.get("prcs_stat_name", ""),  # 처리상태
                    "tr_mket_name": item.get("tr_mket_name", ""),  # 거래시장명
                    "tr_crcy_cd": item.get("tr_crcy_cd", ""),      # 거래통화코드
                    "odno": item.get("odno", ""),                  # 주문번호
                    "ovrs_excg_cd": item.get("ovrs_excg_cd", "")   # 거래소코드
                })

            # Step 9: 다음 페이지 여부 확인
            # tr_cont가 "F" or "M"이면 더 가져올 데이터가 있음
            tr_cont = response.headers.get("tr_cont", "")
            if tr_cont != "M" and tr_cont != "F":
                # "D", "E" 또는 빈 값이면 마지막 페이지
                break

            # 다음 페이지를 위해 연속조회 키 저장
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
        raise Exception(f"주문체결내역 조회 실패: {str(e)}")


def place_overseas_order(session, symbol, exchange_code, order_type, quantity, price, side="BUY", trade_mode="DRY"):
    """
    해외주식 주문을 실행합니다.
    
    이 함수는 한국투자증권 API를 통해 해외주식 매수/매도 주문을 합니다.
    - DRY 모드: 주문 정보만 출력하고 실제로는 주문하지 않습니다
    - LIVE 모드: 실제로 주문을 실행하고 주문번호를 반환합니다
    
    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "AAPL", "TSLA")
        exchange_code (str): 거래소 코드
            - NASD: 나스닥
            - NYSE: 뉴욕
            - AMEX: 아멕스
            - SEHK: 홍콩
            - 기타 거래소는 공식 문서 참고
        order_type (str): 주문 구분
            - LIMIT: 지정가 (00)
            - LOC: 장마감지정가 (34)
            - LOO: 장개시지정가 (32)
            - MOO: 장개시시장가 (31)
            - MOC: 장마감시장가 (33)
        quantity (int): 주문 수량
        price (float): 주문 가격 (1주당 가격)
        side (str): 매수/매도 구분 ("BUY" 또는 "SELL", 기본값: "BUY")
        trade_mode (str): 거래 모드 ("DRY" 또는 "LIVE")
    
    Returns:
        dict: LIVE 모드일 때 주문번호(odno)를 포함한 딕셔너리
              {
                  "odno": "주문번호",
                  "org_no": "한국거래소전송주문조직번호",
                  "ord_tmd": "주문시각"
              }
              DRY 모드일 때는 None
    
    Raises:
        Exception: API 호출 실패 또는 필수 정보 미설정 시 예외 발생
                   실패 시 응답코드(msg_cd)와 응답메시지(msg1)를 포함하여 에러 발생
    """
    from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD
    
    # 필수 설정 검증: KIS_ACCOUNT_NO는 필수입니다
    if not KIS_ACCOUNT_NO:
        raise Exception("KIS_ACCOUNT_NO가 설정되어 있지 않습니다. 환경변수 또는 .env 파일에 KIS_ACCOUNT_NO를 추가하세요.")

    # 모의투자에서 지원하지 않는 주문 유형을 대체합니다.
    # 한국투자증권 모의투자 API는 LOC(34·장마감지정가) 등 일부 주문 유형을 지원하지 않습니다.
    # 모의투자 환경에서는 LOC 주문을 LIMIT(지정가)으로 자동 변환합니다.
    try:
        kis_mode_for_type = KIS_MODE
    except NameError:
        kis_mode_for_type = "demo"

    DEMO_UNSUPPORTED_ORDER_TYPES = {"LOC", "LOO", "MOO", "MOC"}
    if kis_mode_for_type != "real" and order_type in DEMO_UNSUPPORTED_ORDER_TYPES:
        print(f"⚠️  모의투자 미지원 주문 유형: {order_type} → LIMIT(지정가)으로 자동 변환합니다.")
        order_type = "LIMIT"

    # 주문 구분 코드 매핑
    order_type_map = {
        "LIMIT": "00",  # 지정가
        "LOC": "34",    # 장마감지정가
        "LOO": "32",    # 장개시지정가
        "MOO": "31",    # 장개시시장가
        "MOC": "33"     # 장마감시장가
    }
    
    if order_type not in order_type_map:
        raise Exception(f"지원하지 않는 주문 유형입니다: {order_type}")
    
    ord_dvsn = order_type_map[order_type]
    
    # 모의투자(데모)일 때: 정규장이 아닐 경우 예약주문으로 자동 전환
    try:
        kis_mode_val = KIS_MODE
    except NameError:
        kis_mode_val = "demo"

    if kis_mode_val != "real":
        # 현재 KST 시각을 확인
        now_kst = _get_kst_now()
        if not _is_kst_regular_market(now_kst):
            # 예약주문 가능시간이 아닌 경우에는 일반 예외로 중단
            if not _is_kst_reserve_window(now_kst):
                if trade_mode == "DRY":
                    print("⚠️  모의투자: 현재 시각은 정규장 외이며 예약주문 가능시간도 아닙니다. 주문이 필요하다면 가능한 시간에 다시 시도하세요. (KST 기준)")
                else:
                    raise Exception("모의투자: 예약주문 가능시간이 아닙니다 (KST 기준)")
            # 정규장 외이지만 예약주문 가능시간 → 호출자에게 예약주문 필요 신호를 보냅니다.
            # place_overseas_order는 일반 주문 엔드포인트만 담당하므로
            # 예약주문 엔드포인트 호출은 호출자가 직접 처리합니다.
            raise ReservationOrderRequired(
                f"모의투자: 정규장 외 시간입니다. 예약주문을 사용하세요. "
                f"(symbol={symbol}, exchange={exchange_code}, qty={quantity}, price={price})"
            )

    # DRY 모드일 때는 주문 정보만 출력
    if trade_mode == "DRY":
        print("\n========== [DRY 모드] 주문 정보 ==========")
        print(f"종목 코드: {symbol}")
        print(f"거래소: {exchange_code}")
        print(f"매수/매도: {side}")
        print(f"주문 유형: {order_type} ({ord_dvsn})")
        print(f"주문 수량: {quantity}주")
        print(f"주문 가격: ${price}")
        print(f"계좌 번호: {_mask_account_no(KIS_ACCOUNT_NO)}")
        print("실제 주문은 실행되지 않았습니다.")
        print("=========================================\n")
        return None
    
    # LIVE 모드일 때만 실제 주문 실행
    # Step 1: 접근 토큰 획득
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")
    
    # Step 2: 요청 경로 설정
    path = "/uapi/overseas-stock/v1/trading/order"
    
    # Step 3: TR_ID 결정 (실전/모의 및 매수/매도에 따라 다름)
    # 매수: TTTT1002U (실전) / VTTT1002U (모의)
    # 매도: TTTT1006U (실전) / VTTT1001U (모의)
    if side == "SELL":
        tr_id = "TTTT1006U" if KIS_MODE == "real" else "VTTT1001U"
    else:
        tr_id = "TTTT1002U" if KIS_MODE == "real" else "VTTT1002U"
    
    # Step 4: 요청 헤더 설정
    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": tr_id
    }
    
    # Step 5: 요청 바디 설정
    body = {
        "CANO": KIS_ACCOUNT_NO,           # 종합계좌번호 (8자리)
        "ACNT_PRDT_CD": ACNT_PRDT_CD,     # 계좌상품코드 (01)
        "OVRS_EXCG_CD": exchange_code,    # 해외거래소코드
        "PDNO": symbol,                   # 상품번호 (종목코드)
        "ORD_QTY": str(quantity),         # 주문수량
        "OVRS_ORD_UNPR": str(price),      # 해외주문단가 (1주당 가격)
        "ORD_SVR_DVSN_CD": "0",           # 주문서버구분코드 (기본값 "0")
        "ORD_DVSN": ord_dvsn              # 주문구분
    }
    
    # Step 6: API 호출
    try:
        response = _request_with_rate_retry(session, "POST", path, headers=headers, json=body)
        response.raise_for_status()
        
        # Step 7: 응답 데이터 추출
        response_data = response.json()
        
        # API 응답이 정상인지 확인
        if response_data.get("rt_cd") != "0":
            msg_cd = response_data.get("msg_cd", "")
            msg1 = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"주문 실패 (응답코드: {msg_cd}): {msg1}")
        
        # 주문 성공 정보 반환
        output = response_data.get("output", {})
        
        print("\n========== [LIVE 모드] 주문 성공 ==========")
        print(f"종목 코드: {symbol}")
        print(f"주문번호: {output.get('ODNO', '')}")
        print(f"주문시각: {output.get('ORD_TMD', '')}")
        print(f"주문수량: {quantity}주")
        print(f"주문가격: ${price}")
        print("==========================================\n")
        
        return {
            "odno": output.get("ODNO", ""),                       # 주문번호
            "org_no": output.get("KRX_FWDG_ORD_ORGNO", ""),      # 한국거래소전송주문조직번호
            "ord_tmd": output.get("ORD_TMD", "")                 # 주문시각
        }
    
    except requests.exceptions.RequestException as e:
        raise Exception(f"주문 실행 실패: {str(e)}")


def place_overseas_reservation_order(session, symbol: str, exchange_code: str, quantity: int, price: float, ord_dv: str = "usBuy"):
    """
    해외주식 예약주문 접수 API를 호출합니다.

    이 함수는 한국투자증권의 `/uapi/overseas-stock/v1/trading/order-resv` 엔드포인트를
    사용하여 미국장(정규장) 외 시간에 예약주문을 접수합니다. `ord_dv`는
    'usBuy'|'usSell'|'asia' 중 하나를 사용합니다.

    Returns: API 출력의 object 형태를 dict로 반환합니다.
    """
    from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD

    if not KIS_ACCOUNT_NO:
        raise Exception("KIS_ACCOUNT_NO가 설정되어 있지 않습니다. 환경변수 또는 .env 파일에 KIS_ACCOUNT_NO를 추가하세요.")

    # TR_ID 결정: 실전/모의 및 매수/매도/아시아 구분
    if KIS_MODE == "real":
        if ord_dv == "usBuy":
            tr_id = "TTTT3014U"
        elif ord_dv == "usSell":
            tr_id = "TTTT3016U"
        elif ord_dv == "asia":
            tr_id = "TTTS3013U"
        else:
            raise Exception("ord_dv can only be 'usBuy', 'usSell' or 'asia'")
    else:
        if ord_dv == "usBuy":
            tr_id = "VTTT3014U"
        elif ord_dv == "usSell":
            tr_id = "VTTT3016U"
        elif ord_dv == "asia":
            tr_id = "VTTS3013U"
        else:
            raise Exception("ord_dv can only be 'usBuy', 'usSell' or 'asia'")

    path = "/uapi/overseas-stock/v1/trading/order-resv"

    # access token
    try:
        token_data = get_access_token(session=session)
        access_token = token_data["access_token"]
    except Exception as e:
        raise Exception(f"토큰 획득 실패: {str(e)}")

    headers = {
        "authorization": f"Bearer {access_token}",
        "tr_id": tr_id
    }

    # API body (대문자 키 사용)
    body = {
        "CANO": KIS_ACCOUNT_NO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": symbol,
        "OVRS_EXCG_CD": exchange_code,
        "FT_ORD_QTY": str(quantity),
        "FT_ORD_UNPR3": str(price)
    }

    try:
        response = _request_with_rate_retry(session, "POST", path, headers=headers, json=body)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("rt_cd") != "0":
            msg_cd = response_data.get("msg_cd", "")
            msg1 = response_data.get("msg1", "알 수 없는 에러")
            raise Exception(f"예약주문 실패 (응답코드: {msg_cd}): {msg1}")

        output = response_data.get("output", {})
        print("\n========== [예약주문] 접수 성공 ==========")
        print(f"종목: {symbol}  수량: {quantity}  가격: {price}")
        print(f"예약주문번호(ODNO): {output.get('ODNO','')}")
        print("========================================\n")

        # 예약주문 API 원래 응답 필드를 그대로 반환합니다.
        # - odno           : 예약주문번호
        # - rsvn_ord_rcit_dt : 예약주문 접수일자 (YYYYMMDD)
        # - ovrs_rsvn_odno : 해외예약주문번호

        # 일반 주문(place_overseas_order)의 반환 형식과는 의도적으로 다릅니다.
        # RSVN_ORD_RCIT_DT 가 비어있으면 KST 오늘 날짜(YYYYMMDD)로 대체합니다.
        rsvn_dt = output.get("RSVN_ORD_RCIT_DT") or _get_kst_now().strftime("%Y%m%d")

        return {
            "odno": output.get("ODNO", ""),
            "rsvn_ord_rcit_dt": rsvn_dt,
            "ovrs_rsvn_odno": output.get("OVRS_RSVN_ODNO", ""),  # 해외예약주문번호
        }

    except requests.exceptions.RequestException as e:
        raise Exception(f"예약주문 호출 실패: {str(e)}")
