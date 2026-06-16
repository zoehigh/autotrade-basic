import certifi
import requests
from config import KIS_DOMAIN, KIS_APP_KEY, KIS_APP_SECRET


class KISSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = certifi.where()
        self.session.headers.update({
            "content-type": "application/json; charset=utf-8",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        })

    def request(self, method, path, **kwargs):
        url = f"{KIS_DOMAIN}{path}"
        return self.session.request(method, url, **kwargs)

    def close(self):
        self.session.close()
