"""
KIS HTTP 세션 관리 — 한국투자증권 API 공통 헤더/인증 설정.

기존 src/kis_session.py에서 이관. KISBroker가 내부적으로 사용합니다.
"""
import certifi
import requests
from config import KIS_DOMAIN, KIS_APP_KEY, KIS_APP_SECRET, KIS_TIMEOUT


class KISSession:
    """
    한국투자증권 API 호출을 위한 requests.Session 래퍼.

    모든 요청에 appkey, appsecret, content-type 헤더를 자동으로 추가하고,
    certifi 인증서로 TLS 검증을 수행합니다.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = certifi.where()
        self.session.headers.update({
            "content-type": "application/json; charset=utf-8",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        })
        self.timeout = KIS_TIMEOUT

    def request(self, method, path, **kwargs):
        url = f"{KIS_DOMAIN}{path}"
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, **kwargs)

    def post(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)

    def close(self):
        self.session.close()
