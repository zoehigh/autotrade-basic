"""
메리츠증권 OAuth2 인증 — 접근 토큰 발급/캐싱/무효화.

메리츠증권 Open API 인증 방식 (openapi.json 2026-10-02 기준):
- Endpoint: POST {base_url}/oauth2/token
- Content-Type: application/x-www-form-urlencoded
- Parameters(필수 4개): grant_type=client_credentials, client_id, client_secret, scope=login
- 계좌번호 파라미터 없음 — AppKey/AppSecret만으로 계좌가 식별됩니다
- 토큰 유효기간: expires_in(초) — 만료 60초 전부터 재발급
- refresh token 미제공 — 만료 시 재발급 (이전 토큰 무효)

토큰 오류 코드(EGW00121/EGW00123)는 HTTP 200 응답의 본문 rsp_cd로 내려옵니다.
auth 모듈은 캐시만 무효화하고, 실제 재전송은 호출부(adapter)가 1회만 수행합니다.
"""
import random
import time

import certifi
import requests

from broker.base import AuthError

# 토큰 만료/무효 응답 코드 — 호출부는 1회 재발급 후 재시도합니다.
TOKEN_ERROR_CODES = frozenset({"EGW00121", "EGW00123"})

# 만료 임박 재발급 기준 (초)
_TOKEN_REFRESH_MARGIN = 60

# 네트워크 재시도 횟수 (토큰 발급은 주문 미접수 확정이므로 재시도 가능)
_MAX_NETWORK_RETRIES = 3

_cached_token = None
_cached_expires_at = 0.0


def is_token_error_code(code) -> bool:
    """rsp_cd가 토큰 만료/무효 코드인지 확인합니다."""
    return str(code or "").strip() in TOKEN_ERROR_CODES


def invalidate_token() -> None:
    """캐시된 토큰을 폐기합니다 (다음 호출에서 재발급)."""
    global _cached_token, _cached_expires_at
    _cached_token = None
    _cached_expires_at = 0.0


def get_access_token(
    base_url: str,
    app_key: str,
    app_secret: str,
    timeout: tuple[float, float] = (10, 30),
) -> str:
    """
    메리츠증권 API에서 access token을 발급받습니다 → str.

    POST {base_url}/oauth2/token (form-urlencoded)
        grant_type=client_credentials, client_id=app_key, client_secret=app_secret,
        scope=login

    토큰 캐싱:
        - 프로세스 전역 캐시를 사용하며, 만료 60초 전부터 자동 재발급합니다.
        - EGW00121/EGW00123 응답을 받은 호출부가 invalidate_token()을 호출하면
          다음 get_access_token()에서 재발급됩니다.

    Returns:
        str: access token 문자열

    Raises:
        AuthError: 자격증명 누락/오류, 토큰 발급 실패
    """
    global _cached_token, _cached_expires_at

    now = time.time()
    if _cached_token is not None and now < _cached_expires_at - _TOKEN_REFRESH_MARGIN:
        return _cached_token

    if not app_key or not app_secret:
        raise AuthError(
            "환경변수 MERITZ_APP_KEY와 MERITZ_APP_SECRET이 설정되어야 합니다. "
            ".env 파일을 확인해주세요."
        )

    url = f"{base_url.rstrip('/')}/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": app_key,
        "client_secret": app_secret,
        "scope": "login",
    }

    network_retry_count = 0
    while True:
        try:
            response = requests.post(
                url,
                verify=certifi.where(),
                headers=headers,
                data=data,
                timeout=timeout,
            )

            if response.status_code == 401:
                raise AuthError(
                    "메리츠증권 클라이언트 인증 실패 (401): "
                    "MERITZ_APP_KEY 또는 MERITZ_APP_SECRET이 잘못되었거나 비활성 상태입니다."
                )

            if response.status_code == 403:
                raise AuthError(
                    "메리츠증권 API 접근 거부 (403): EGW00103 또는 권한 오류입니다. "
                    "AppKey 활성화 상태와 API 사용 권한을 확인하세요."
                )

            if response.status_code >= 400:
                raise AuthError(
                    f"메리츠증권 토큰 발급 실패 (HTTP {response.status_code})"
                )

            body = response.json()
            token = body.get("access_token")
            if not token:
                error = body.get("error") or body.get("rsp_cd") or "unknown"
                desc = (
                    body.get("error_description")
                    or body.get("rsp_msg")
                    or "알 수 없는 오류"
                )
                raise AuthError(f"메리츠 토큰 발급 실패 [{error}]: {desc}")

            expires_in = int(body.get("expires_in", 86400) or 86400)
            _cached_token = token
            _cached_expires_at = now + expires_in
            print("[메리츠 인증] 토큰 발급 성공")
            return token

        except AuthError:
            raise
        except (ValueError, TypeError) as e:
            raise AuthError(f"메리츠 토큰 응답 파싱 실패: {str(e)[:80]}")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            network_retry_count += 1
            if network_retry_count <= _MAX_NETWORK_RETRIES:
                wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                print(f"[메리츠 인증] 토큰 발급 타임아웃: {str(e)[:60]}...")
                print(
                    f"   {wait:.1f}초 후 재시도... "
                    f"({network_retry_count}/{_MAX_NETWORK_RETRIES})"
                )
                time.sleep(wait)
                continue
            raise AuthError(f"메리츠 토큰 발급 실패 (네트워크): {str(e)}")