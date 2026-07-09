"""
LS증권 OAuth2 인증 — 접근 토큰 발급/캐싱/재시도.

LS증권 OPEN API 인증 방식:
- Endpoint: POST /oauth2/token
- Content-Type: application/x-www-form-urlencoded (KIS는 JSON과 다름)
- Parameters: grant_type=client_credentials, appkey, appsecretkey, scope=oob
- 토큰 유효기간: 발급일 익일 07시까지 (KIS는 24시간)
"""
import random
import time

import certifi
import requests

from config import (
    LS_APP_KEY,
    LS_APP_SECRET,
    BROKER_CONFIG,
    HTTP_TIMEOUT,
)

# Base URL (실전/모의 동일, AppKey로 환경 구분)
BASE_URL = BROKER_CONFIG.get("domain", "https://openapi.ls-sec.co.kr:8080")

# 발급받은 토큰을 캐시하는 전역 변수
_cached_token = None


def get_access_token(session=None) -> dict:
    """
    LS증권 API에서 access token을 발급받습니다.

    OAuth2 client_credentials 방식:
    - POST /oauth2/token
    - body 파라미터: grant_type=client_credentials, appkey, appsecretkey, scope=oob
    - Content-Type: application/x-www-form-urlencoded

    토큰 캐싱:
    - 한 번 발급받은 토큰은 전역 변수(_cached_token)에 저장되어 재사용됩니다.
    - 프로그램 실행 중 동일한 토큰을 반복 호출하면 API 요청 없이 캐시된 토큰을 반환합니다.

    자동 재시도:
    - 타임아웃/연결오류: 지수 백오프 + jitter 후 재시도 (최대 3회)

    Returns:
        dict: access token과 관련 정보를 포함한 딕셔너리
              { 'access_token': '...', 'token_type': 'Bearer', ... }

    Raises:
        Exception: API 호출 실패 또는 필수 환경변수 미설정 시 예외 발생
    """
    global _cached_token

    # 캐시된 토큰이 있으면 즉시 반환
    if _cached_token is not None:
        return _cached_token

    # 환경변수 설정 확인
    if not LS_APP_KEY or not LS_APP_SECRET:
        raise Exception(
            "환경변수 LS_APP_KEY와 LS_APP_SECRET이 설정되어야 합니다. "
            ".env 파일을 확인해주세요."
        )

    # API 호출 정보
    url = f"{BASE_URL}/oauth2/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    params = {
        "grant_type": "client_credentials",
        "appkey": LS_APP_KEY,
        "appsecretkey": LS_APP_SECRET,
        "scope": "oob",
    }

    MAX_RETRIES = 3
    network_retry_count = 0

    while True:
        try:
            http = session.post if session else requests.post
            response = http(
                url,
                verify=certifi.where(),
                headers=headers,
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            response_data = response.json()

            if "access_token" in response_data:
                _cached_token = response_data
                print("[LS 인증] 토큰 발급 성공")
                return response_data

            error_code = response_data.get("error_code", "")
            error_desc = response_data.get("error_description", "알 수 없는 오류")
            raise Exception(f"토큰 발급 실패 [{error_code}]: {error_desc}")

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            network_retry_count += 1
            if network_retry_count <= MAX_RETRIES:
                wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                print(f"⏳ LS 토큰 발급 타임아웃: {str(e)[:60]}...")
                print(f"   {wait:.1f}초 후 재시도... ({network_retry_count}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise Exception(f"토큰 발급 실패 (네트워크): {str(e)}")
