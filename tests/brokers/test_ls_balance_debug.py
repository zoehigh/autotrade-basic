"""
COSOQ00201 디버그 v2 — rate-limit 회피를 위한 2초 간격
"""
import json
import time
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

import certifi
import requests

from broker.ls.auth import get_access_token
from broker.ls.order_types import TR_ID_BALANCE
from config import BROKER_CONFIG, HTTP_TIMEOUT

BASE_URL = BROKER_CONFIG.get("domain", "https://openapi.ls-sec.co.kr:8080")
PATH = "/overseas-stock/accno"
today = "20260710"

session = requests.Session()
session.verify = certifi.where()

token_data = get_access_token(session=session)
access_token = token_data["access_token"]

def try_method(label, body, use_data=False):
    time.sleep(2.0)  # rate-limit 회피
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "authorization": f"Bearer {access_token}",
        "tr_cd": TR_ID_BALANCE,
        "tr_cont": "N",
        "tr_cont_key": "",
    }
    url = f"{BASE_URL}{PATH}"
    
    if use_data:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        print(f"  payload: {payload!r}")
        resp = session.post(url, headers=headers, data=payload, timeout=HTTP_TIMEOUT)
    else:
        resp = session.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
    
    print(f"  status: {resp.status_code}")
    print(f"  body:   {resp.text[:400]}")
    print()

# === Test 1: json=body (current approach) ===
print("=" * 70)
print("T1: json=body — RecCnt=1, BaseDt=today, CrcyCode=USD")
print("=" * 70)
try_method("T1", {
    "COSOQ00201InBlock1": {
        "RecCnt": 1,
        "BaseDt": today,
        "CrcyCode": "USD",
        "AstkBalTpCode": "00",
    }
}, use_data=False)

# === Test 2: data=json.dumps — same values ===
print("=" * 70)
print("T2: data=json.dumps — RecCnt=1, BaseDt=today, CrcyCode=USD")
print("=" * 70)
try_method("T2", {
    "COSOQ00201InBlock1": {
        "RecCnt": 1,
        "BaseDt": today,
        "CrcyCode": "USD",
        "AstkBalTpCode": "00",
    }
}, use_data=True)

# === Test 3: data=json.dumps — empty BaseDt, USD ===
print("=" * 70)
print('T3: data=json.dumps — RecCnt=1, BaseDt="", CrcyCode=USD')
print("=" * 70)
try_method("T3", {
    "COSOQ00201InBlock1": {
        "RecCnt": 1,
        "BaseDt": "",
        "CrcyCode": "USD",
        "AstkBalTpCode": "00",
    }
}, use_data=True)

# === Test 4: data=json.dumps — with mac_address ===
print("=" * 70)
print('T4: data=json.dumps + mac_address header — RecCnt=1, BaseDt=today')
print("=" * 70)
time.sleep(2.0)
headers = {
    "Content-Type": "application/json; charset=UTF-8",
    "authorization": f"Bearer {access_token}",
    "tr_cd": TR_ID_BALANCE,
    "tr_cont": "N",
    "tr_cont_key": "",
    "mac_address": "",
}
url = f"{BASE_URL}{PATH}"
body = {
    "COSOQ00201InBlock1": {
        "RecCnt": 1,
        "BaseDt": today,
        "CrcyCode": "USD",
        "AstkBalTpCode": "00",
    }
}
payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
resp = session.post(url, headers=headers, data=payload, timeout=HTTP_TIMEOUT)
print(f"  status: {resp.status_code}")
print(f"  body:   {resp.text[:400]}")
