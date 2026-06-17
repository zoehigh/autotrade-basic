import certifi
import requests
from config import KIS_DOMAIN, KIS_APP_KEY, KIS_APP_SECRET, KIS_TIMEOUT


class KISSession:
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
