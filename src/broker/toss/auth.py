"""
토스증권 OAuth2 인증 — 접근 토큰 발급/캐싱/재시도.

토스증권 Open API 인증 방식:
- Endpoint: POST https://openapi.tossinvest.com/oauth2/token
- Content-Type: application/x-www-form-urlencoded
- Parameters: grant_type=client_credentials, client_id, client_secret
- 토큰 유효기간: expires_in (86400초 = 24시간)
- refresh token 미제공 — 만료 시 재발급 (이전 토큰 무효화)
- client당 유효 토큰 1개
"""
import random
import time

import certifi
import requests

from broker.base import AuthError

# 발급받은 토큰을 캐시하는 전역 변수
_cached_token = None
_token_expires_at = 0.0


def get_access_token(
    domain: str,
    client_id: str,
    client_secret: str,
    timeout: tuple[float, float] = (10, 30),
) -> str:
    """
    토스증권 API에서 access token을 발급받습니다.

    OAuth2 client_credentials 방식:
    - POST {domain}/oauth2/token
    - body: grant_type=client_credentials, client_id, client_secret
    - Content-Type: application/x-www-form-urlencoded

    토큰 캐싱:
    - 한 번 발급받은 토큰은 전역 변수에 저장되어 재사용됩니다.
    - 만료 60초 전에 자동 재발급합니다.

    Returns:
        str: access token 문자열

    Raises:
        AuthError: 인증 실패 (잘못된 키, 허용되지 않은 IP 등)
        BrokerError: 네트워크 오류
    """
    global _cached_token, _token_expires_at

    now = time.time()

    # 캐시된 토큰이 유효하면 즉시 반환 (만료 60초 전까지)
    if _cached_token is not None and now < _token_expires_at - 60:
        return _cached_token

    if not client_id or not client_secret:
        raise AuthError(
            "환경변수 TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET이 설정되어야 합니다. "
            ".env 파일을 확인해주세요."
        )

    url = f"{domain}/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    MAX_RETRIES = 3
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

            # HTTP 403 — 허용되지 않은 IP
            if response.status_code == 403:
                raise AuthError(
                    "토스증권 API 접근 거부 (403): "
                    "허용되지 않은 IP에서의 요청입니다. "
                    "토스증권 WTS > 설정 > Open API > 허용 IP 관리에서 IP를 등록하세요."
                )

            # HTTP 401 — 클라이언트 인증 실패
            if response.status_code == 401:
                raise AuthError(
                    "토스증권 클라이언트 인증 실패 (401): "
                    "client_id 또는 client_secret이 잘못되었거나 비활성 상태입니다."
                )

            response.raise_for_status()
            body = response.json()

            token = body.get("access_token")
            if not token:
                error = body.get("error", "unknown")
                desc = body.get("error_description", "알 수 없는 오류")
                raise AuthError(f"토큰 발급 실패 [{error}]: {desc}")

            expires_in = int(body.get("expires_in", 86400))
            _cached_token = token
            _token_expires_at = now + expires_in
            print("[토스 인증] 토큰 발급 성공")
            return token

        except AuthError:
            raise

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            network_retry_count += 1
            if network_retry_count <= MAX_RETRIES:
                wait = min(30, 2 ** network_retry_count) * random.uniform(0.75, 1.25)
                print(f"토스 토큰 발급 타임아웃: {str(e)[:60]}...")
                print(f"   {wait:.1f}초 후 재시도... ({network_retry_count}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise AuthError(f"토스 토큰 발급 실패 (네트워크): {str(e)}")
