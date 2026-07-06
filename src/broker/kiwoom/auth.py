"""
키움증권 OAuth2 토큰 발급 및 캐싱.

KIS의 auth.py와 유사하지만 다음 차이점:
- 파라미터: secretkey (KIS는 appsecret)
- 응답 필드: token (KIS는 access_token), expires_dt (KIS는 expires_in)
- return_code == 0 이면 성공
"""
import time
import requests
from datetime import datetime

_cached_token = None
_cached_expires_at = 0.0  # epoch timestamp


def get_access_token(domain: str, app_key: str, app_secret: str, timeout=None) -> str:
    """
    키움증권 접근토큰을 발급받아 캐싱합니다.

    캐시된 토큰이 유효하면 재사용, 만료되었으면 재발급.
    """
    global _cached_token, _cached_expires_at

    # 캐시된 토큰이 유효하면 재사용 (만료 60초 전까지)
    if _cached_token and time.time() < _cached_expires_at - 60:
        return _cached_token

    url = f"{domain}/oauth2/token"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    return_code = data.get("return_code")
    if return_code != 0:
        raise RuntimeError(f"키움 토큰 발급 실패: {data.get('return_msg', 'unknown')}")

    _cached_token = data["token"]
    # expires_dt 파싱 (YYYYMMDDHHMMSS 포맷)
    expires_dt = data.get("expires_dt", "")
    if expires_dt:
        try:
            dt = datetime.strptime(expires_dt, "%Y%m%d%H%M%S")
            _cached_expires_at = dt.timestamp()
        except ValueError:
            # 파싱 실패 시 24시간으로 가정
            _cached_expires_at = time.time() + 86400
    else:
        _cached_expires_at = time.time() + 86400

    return _cached_token


def clear_cached_token():
    """캐시된 토큰을 삭제합니다 (테스트/재발급용)."""
    global _cached_token, _cached_expires_at
    _cached_token = None
    _cached_expires_at = 0.0
