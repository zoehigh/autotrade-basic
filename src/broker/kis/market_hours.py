"""
KIS 모의투자 장 시간 로직 — 정규장/예약주문 가능시간 판단.

기존 src/trader.py에서 추출. KISBroker가 내부적으로 사용합니다.
한국투자증권 모의투자 환경에서만 적용되는 제약사항입니다.
"""
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from broker.market_utils import get_kst_now, is_us_dst


def is_kst_regular_market(now_kst: Optional[datetime] = None) -> bool:
    """
    KST 기준 정규장 여부 검사.

    정규장 시간 (KST):
      - 서머타임(미국 DST 적용): 23:30 ~ 익일 06:00
      - 비서머타임: 22:30 ~ 익일 05:00
    """
    if now_kst is None:
        now_kst = get_kst_now()
    is_dst = is_us_dst()
    t = now_kst.time()
    if is_dst:
        start = dtime(23, 30)
        end = dtime(6, 0)
    else:
        start = dtime(22, 30)
        end = dtime(5, 0)

    # wrap-around 범위 처리
    return (t >= start) or (t <= end)


def is_kst_reserve_window(now_kst: Optional[datetime] = None) -> bool:
    """
    KST 기준 예약주문 가능시간 검사.

    예약주문 가능시간 (KST):
      - 서머타임(미국 DST 적용): 10:00 ~ 22:20
      - 비서타임: 10:00 ~ 23:20
    """
    if now_kst is None:
        now_kst = get_kst_now()
    is_dst = is_us_dst()
    t = now_kst.time()
    start = dtime(10, 0)
    end = dtime(22, 20) if is_dst else dtime(23, 20)
    return (t >= start) and (t <= end)


def mask_account_no(acct) -> str:
    """
    계좌번호를 안전하게 마스킹합니다.
    - 길이가 4 이하이면 전체를 '*'로 대체
    - 그 외에는 앞2자리와 끝2자리를 남기고 중간은 '*'로 대체
    """
    s = str(acct) if acct is not None else ""
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]
