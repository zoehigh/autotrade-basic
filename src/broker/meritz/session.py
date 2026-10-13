"""
메리츠증권 HTTP 세션 관리 — 공통 헤더와 타임아웃을 관리하는 세션 래퍼.

메리츠증권 Open API 호출 시 필요한 공통 헤더를 자동으로 추가합니다:
- authorization: Bearer {access_token}
- mac_address: 12자리 대문자 HEX (단말 식별용)
- Accept: application/json

MAC 주소는 uuid.getnode()에서 유도하며, MERITZ_MAC_ADDRESS 환경변수로
override할 수 있습니다 (세션 생성 시점의 값으로 고정).
"""
import os
import re
import uuid

import certifi
import requests

# MAC 주소 자리수 (12자리 HEX = 48bit)
MAC_ADDRESS_LENGTH = 12

_HEX_RE = re.compile(r"^[0-9A-F]{12}$")


def resolve_mac_address(override: str | None = None) -> str:
    """
    단말 식별용 MAC 주소를 12자리 대문자 HEX로 반환합니다.

    Parameters:
        override: 명시적 MAC 주소 (MERITZ_MAC_ADDRESS). 구분자(-, :, .)는 제거됩니다.
                  비어있으면 uuid.getnode()에서 유도합니다.

    Returns:
        str: 12자리 대문자 HEX 문자열

    Raises:
        ValueError: override가 12자리 HEX 형식이 아닌 경우
    """
    raw = (override or "").strip()
    if raw:
        cleaned = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
        if not _HEX_RE.match(cleaned):
            raise ValueError(
                f"MERITZ_MAC_ADDRESS 형식이 올바르지 않습니다: '{override}' "
                f"(12자리 HEX 예: A1B2C3D4E5F6)"
            )
        return cleaned

    # 미설정 시 로컬 NIC 주소를 사용 (48bit 정수 → 12자리 HEX)
    return f"{uuid.getnode():012X}"


class MeritzSession:
    """
    메리츠증권 API 호출을 위한 requests.Session 래퍼.

    모든 요청에 authorization/mac_address 공통 헤더를 자동으로 추가하고,
    TLS 검증 및 타임아웃 설정을 관리합니다.
    """

    def __init__(
        self,
        base_url: str,
        mac_address: str | None = None,
        timeout: tuple[float, float] = (10, 30),
    ):
        """
        Parameters:
            base_url: 메리츠증권 API 베이스 URL (예: https://openapi.meritz.com)
            mac_address: MAC 주소 override (비어있으면 uuid.getnode() 유도)
            timeout: (connect_timeout, read_timeout) 초 단위
        """
        self._base_url = base_url.rstrip("/")
        self._mac_address = resolve_mac_address(
            mac_address or os.getenv("MERITZ_MAC_ADDRESS", "")
        )
        self._timeout = timeout

        self._session = requests.Session()
        self._session.verify = certifi.where()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> tuple[float, float]:
        return self._timeout

    @property
    def mac_address(self) -> str:
        return self._mac_address

    def _build_headers(self, token: str, extra_headers: dict | None = None) -> dict:
        """authorization + mac_address 공통 헤더를 구성합니다."""
        headers = {
            "authorization": f"Bearer {token}",
            "mac_address": self._mac_address,
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def get(
        self,
        path: str,
        token: str,
        params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> requests.Response:
        """GET 요청을 실행합니다 → requests.Response."""
        url = f"{self._base_url}{path}"
        headers = self._build_headers(token, extra_headers)
        return self._session.get(
            url, headers=headers, params=params, timeout=self._timeout
        )

    def post(
        self,
        path: str,
        token: str,
        json_body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> requests.Response:
        """POST 요청을 실행합니다 → requests.Response."""
        url = f"{self._base_url}{path}"
        headers = self._build_headers(token, extra_headers)
        headers["Content-Type"] = "application/json"
        return self._session.post(
            url, headers=headers, json=json_body, timeout=self._timeout
        )

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()