# 한국투자증권 API 인증을 담당하는 파일
import requests
import time
import certifi
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN

# 발급받은 토큰을 캐시하는 전역 변수
# 프로그램 실행 중 한 번 발급한 토큰을 재사용하여 불필요한 API 호출을 줄입니다
_cached_token = None


def get_access_token(session=None):
    """
    한국투자증권 API에서 access token을 발급받습니다.
    
    이 함수는 한국투자증권의 OAuth2 Client Credentials 절차를 따릅니다.
    - token은 발급 후 24시간 동안 유효합니다
    - 6시간 이내에 재발급 요청하면 이전 token을 반환합니다
    
    토큰 캐싱:
    - 한 번 발급받은 토큰은 전역 변수(_cached_token)에 저장되어 재사용됩니다
    - 프로그램 실행 중 동일한 토큰을 반복 호출하면 API 요청 없이 캐시된 토큰을 반환합니다
    
    자동 재시도:
    - EGW00133 오류(1분당 1회 제한) 발생 시 1분 대기 후 자동으로 재시도합니다
    - 타임아웃 오류(ConnectTimeout, ReadTimeout) 발생 시 2초→4초→8초 지수 백오프 후 재시도합니다
    - 각 오류 유형별 최대 3회까지 자동 재시도하며, 프로그램은 중단되지 않습니다
    
    Returns:
        dict: access token과 관련 정보를 포함한 딕셔너리
              {
                  'access_token': 'Bearer...',
                  'token_type': 'Bearer',
                  'expires_in': 초 단위 유효기간,
                  'access_token_token_expired': '2024-01-01 00:00:00' 형식의 유효기간
              }
    
    Raises:
        Exception: API 호출 실패 또는 필수 환경변수 미설정 시 예외 발생
    """
    
    global _cached_token
    
    # 캐시된 토큰이 있으면 즉시 반환합니다
    # 이렇게 하면 같은 토큰을 여러 번 요청할 때 API 호출을 하지 않아 효율적입니다
    if _cached_token is not None:
        return _cached_token
    
    # 환경변수가 설정되어 있는지 확인
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        raise Exception(
            "환경변수 KIS_APP_KEY와 KIS_APP_SECRET이 설정되어야 합니다. "
            ".env 파일을 확인해주세요."
        )
    
    # API 호출에 필요한 정보 준비
    url = f"{KIS_DOMAIN}/oauth2/tokenP"
    
    # 요청 헤더 설정
    headers = {
        "Content-Type": "application/json; charset=UTF-8"
    }
    
    # 요청 바디 설정
    # grant_type은 항상 "client_credentials"로 고정됩니다
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }
    
    max_retries = 3
    retry_count = 0
    network_max_retries = 3
    network_retry_count = 0

    while True:
        try:
            http = session.post if session else requests.post
            response = http(url, json=body, headers=headers, verify=certifi.where())
            response_data = response.json()

            if "error_code" in response_data:
                error_code = response_data.get("error_code")
                error_description = response_data.get("error_description", "알 수 없는 오류")

                if error_code == "EGW00133":
                    retry_count += 1

                    if retry_count < max_retries:
                        print("⏳ 토큰 발급 제한 감지 (EGW00133)")
                        print(f"   사유: {error_description}")
                        print(f"   1분 대기 후 재시도합니다... ({retry_count}/{max_retries})")
                        time.sleep(60)
                        continue
                    else:
                        raise Exception(
                            f"토큰 발급 실패: 최대 재시도 횟수({max_retries}회) 초과. "
                            f"1분 후 다시 시도해주세요."
                        )
                else:
                    raise Exception(f"토큰 발급 실패: [{error_code}] {error_description}")
            else:
                _cached_token = response_data
                return response_data

        except requests.exceptions.Timeout as e:
            network_retry_count += 1
            if network_retry_count <= network_max_retries:
                wait = 2 ** network_retry_count
                print(f"⏳ 타임아웃 오류 발생: {str(e)[:60]}...")
                print(f"   {wait}초 후 재시도합니다... ({network_retry_count}/{network_max_retries})")
                time.sleep(wait)
                continue
            else:
                error_msg = str(e)
                raise Exception(f"토큰 발급 실패: {error_msg}")
