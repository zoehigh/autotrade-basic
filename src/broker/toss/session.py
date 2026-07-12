"""
토스증권 HTTP 세션 관리 — 공통 헤더와 타임아웃을 관리하는 세션 래퍼.

토스증권 Open API 호출 시 필요한 공통 헤더를 자동으로 추가합니다:
- Authorization: Bearer {access_token}
- X-Tossinvest-Account: {account_seq} (계좌 관련 API)
- Accept: application/json
"""
import certifi
import requests


class TossSession:
    """
    토스증권 API 호출을 위한 requests.Session 래퍼.

    모든 요청에 공통 헤더를 자동으로 추가하고,
    TLS 검증 및 타임아웃 설정을 관리합니다.
    """

    def __init__(
        self,
        domain: str,
        account_seq: str,
        timeout: tuple[float, float] = (10, 30),
    ):
        """
        Parameters:
            domain: 토스증권 API 베이스 URL (예: https://openapi.tossinvest.com)
            account_seq: 계좌 시퀀스 번호 (X-Tossinvest-Account 헤더 값)
            timeout: (connect_timeout, read_timeout) 초 단위
        """
        self._domain = domain
        self._account_seq = account_seq
        self._timeout = timeout

        self._session = requests.Session()
        self._session.verify = certifi.where()

    @property
    def timeout(self) -> tuple[float, float]:
        return self._timeout

    def _build_headers(
        self,
        token: str,
        extra_headers: dict | None = None,
    ) -> dict:
        """
        공통 헤더를 구성합니다.

        계좌 관련 API에는 X-Tossinvest-Account가 필요하지만,
        시세 조회 등에서는 불필요합니다. account_seq가 비어있으면
        해당 헤더를 추가하지 않습니다.
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if self._account_seq:
            headers["X-Tossinvest-Account"] = self._account_seq
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
        """
        GET 요청을 실행합니다.

        Parameters:
            path: API 경로 (예: "/api/v1/prices")
            token: OAuth2 access token
            params: 쿼리 파라미터
            extra_headers: 추가 헤더

        Returns:
            requests.Response
        """
        url = f"{self._domain}{path}"
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
        """
        POST 요청을 실행합니다.

        Parameters:
            path: API 경로 (예: "/api/v1/orders")
            token: OAuth2 access token
            json_body: JSON 요청 바디
            extra_headers: 추가 헤더

        Returns:
            requests.Response
        """
        url = f"{self._domain}{path}"
        headers = self._build_headers(token, extra_headers)
        headers["Content-Type"] = "application/json"
        return self._session.post(
            url, headers=headers, json=json_body, timeout=self._timeout
        )

    def close(self):
        """HTTP 세션을 종료합니다."""
        self._session.close()
