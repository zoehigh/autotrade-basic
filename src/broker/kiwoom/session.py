"""
키움증권 HTTP 세션 — TR ID 기반 path/method 라우팅.

KIS는 URL path로 기능을 구분하지만, 키움은 기능 그룹별 path + api-id 헤더로 분기합니다.
"""
import certifi
import requests
from broker.kiwoom.tr_registry import get_endpoint


class KiwoomSession:
    """키움증권 API 호출을 위한 requests.Session 래퍼."""

    def __init__(self, domain: str, timeout=None):
        self.domain = domain
        self.session = requests.Session()
        self.session.verify = certifi.where()
        self.timeout = timeout  # (connect, read) 튜플 또는 단일 값

    def request_with_tr(self, tr_id: str, body: dict, token: str) -> requests.Response:
        """
        TR ID 기반으로 path/method를 자동 결정하여 요청.
        tr_registry에서 (method, path)를 조회하고 api-id 헤더에 TR ID를 설정합니다.
        """
        method, path = get_endpoint(tr_id)
        url = f"{self.domain}{path}"

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
            "api-id": tr_id,
        }

        return self.session.request(
            method, url, headers=headers, json=body, timeout=self.timeout
        )

    def close(self):
        self.session.close()
