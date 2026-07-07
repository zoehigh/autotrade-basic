"""
시장/시간 공통 유틸리티 — 증권사 무관한 공통 함수들.

KIS, 키움, LS 등 모든 증권사가 공통으로 사용하는
시간 계산, 미국 장 영업일 확인 등의 유틸리티를 제공합니다.
"""
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


_XNYS_CALENDAR = None


def get_kst_now() -> datetime:
    """한국시간(KST) 현재 시각을 반환합니다."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


def is_us_dst() -> bool:
    """현재 시각 기준으로 미국 동부시간(ET)의 서머타임 적용 여부를 반환합니다."""
    ny_now = datetime.now(ZoneInfo("America/New_York"))
    return bool(ny_now.dst() and ny_now.dst() != timedelta(0))


def is_us_trading_day() -> bool:
    """
    오늘이 미국 증시 영업일인지 확인합니다 (NYSE 기준).

    exchange_calendars 라이브러리의 XNYS(뉴욕증권거래소) 캘린더를 사용하여
    오늘 날짜가 정규 세션일인지 판단합니다.

    Returns:
        True: 오늘은 영업일 (정규장이 열리는 날)
        False: 오늘은 휴장일 (주말 또는 공휴일)
    """
    global _XNYS_CALENDAR
    if _XNYS_CALENDAR is None:
        _XNYS_CALENDAR = xcals.get_calendar("XNYS")
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return _XNYS_CALENDAR.is_session(now_et.strftime("%Y-%m-%d"))
