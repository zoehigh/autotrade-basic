"""
시장/시간 공통 유틸리티 — 증권사 무관한 공통 함수들.

KIS, 키움, LS 등 모든 증권사가 공통으로 사용하는
시간 계산, 미국 장 영업일 확인 등의 유틸리티를 제공합니다.
"""
import os
from datetime import date as _date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


_XNYS_CALENDAR = None


def get_kst_now() -> datetime:
    """한국시간(KST) 현재 시각을 반환합니다."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


def resolve_real_kst_from_ord(ord_dt: str, ord_tmd: str):
    """KIS 계열(주문일=미국 영업일, 주문시각=KST) 주문이력의 실제 한국 시각을 복원합니다.

    한국 증권사(KIS/LS/키움) 해외주식 주문이력 API는 ord_dt=미국(ET) 영업일,
    ord_tmd=KST 시각을 반환합니다. 봇이 장중(KST 자정 전후) 주문하므로 KST
    날짜는 ord_dt 또는 ord_dt+1입니다. 후보 KST 시각을 ET로 되돌렸을 때
    날짜가 ord_dt와 일치하는 후보를 선택합니다.

    실패 시 None을 반환합니다.
    """
    kst_tz = ZoneInfo("Asia/Seoul")
    et_tz = ZoneInfo("America/New_York")
    for offset_days in (0, 1):
        try:
            candidate = datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S")
            candidate = candidate.replace(tzinfo=kst_tz) + timedelta(days=offset_days)
            if candidate.astimezone(et_tz).strftime("%Y%m%d") == ord_dt:
                return candidate
        except Exception:
            continue
    try:
        return datetime.strptime(ord_dt + ord_tmd, "%Y%m%d%H%M%S").replace(tzinfo=kst_tz)
    except Exception:
        return None


def normalize_order_price(price) -> float:
    """
    미국 주식 호가 단위 규칙에 맞춰 주문가를 정규화합니다 (버림).

    규칙:
    - $1.00 미만: 소수점 4자리까지 ($0.0001 단위)
    - $1.00 이상: 소수점 2자리까지 ($0.01 단위)

    브로커가 거부하는 소수점 자릿수 초과 가격을 미리 차단합니다.

    IEEE 754 부동소수점 오차 방지를 위해 Decimal 연산을 사용합니다.
    예: math.floor(140.42 * 100) / 100 = 140.41 (버그) → 이 함수는 140.42 반환
    """
    from decimal import Decimal
    price = float(price)
    if price < 1.0:
        return float(int(Decimal(str(price)) * 10000) / 10000)
    return float(int(Decimal(str(price)) * 100) / 100)


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


def _get_xnys_calendar():
    """XNYS 캘린더를 반환합니다 (캐시된 인스턴스 재사용)."""
    global _XNYS_CALENDAR
    if _XNYS_CALENDAR is None:
        _XNYS_CALENDAR = xcals.get_calendar("XNYS")
    return _XNYS_CALENDAR


def us_session_date_now(now_utc: datetime | None = None) -> str | None:
    """
    현재(또는 지정 시각) 기준 미국(XNYS) 세션 날짜를 반환합니다.

    ET(America/New_York) 기준 날짜를 사용하며, 오늘날 세션이 아니면
    가장 최근 세션 날짜(≤ 오늘)를 반환합니다.

    Parameters:
        now_utc (datetime | None): 기준 시각(UTC). None이면 현재 시각 사용.

    Returns:
        str | None: "YYYY-MM-DD" 형식의 세션 날짜.
            None은 캘린더 범위 이전(now_utc가 첫 세션보다 이전)으로
            이전 세기를 찾을 수 없을 때만 반환됩니다. 운영 환경에서는
            사실상 발생하지 않지만, None을 받으면 **미정산(pending)**으로
            처리하고 세션 날짜 확정 전에는 settlement를 보류해야 합니다.
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    date_str = now_et.strftime("%Y-%m-%d")
    # 오늘 세션이면 오늘, 아니면 가장 최근 세션 (≤ 오늘)
    session = cal.date_to_session(date_str, direction="previous")
    return session.strftime("%Y-%m-%d") if session is not None else None


def us_session_closed(session_date: str, now_utc: datetime | None = None) -> bool:
    """
    지정된 세션 날짜의 장이 이미 마감되었는지 확인합니다.

    XNYS 캘린더의 세션 마감 시각(조기 마감 포함)을 기준으로 판단합니다.

    Parameters:
        session_date (str): "YYYY-MM-DD" 형식의 세션 날짜.
        now_utc (datetime | None): 기준 시각(UTC). None이면 현재 시각 사용.

    Returns:
        bool: 세션이 존재하고 현재 UTC ≥ 세션 마감 시각이면 True.
              세션이 없는 날짜(비영업일)는 False (호출 측에서 "미정산"으로 처리).
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    if not cal.is_session(session_date):
        return False

    close_ts = cal.session_close(session_date)  # pandas.Timestamp (UTC)
    close_utc = close_ts.to_pydatetime()
    return now_utc >= close_utc


AFTER_HOURS_END_ET_HOUR_ENV = "AFTER_HOURS_END_ET_HOUR"
DEFAULT_AFTER_HOURS_END_ET_HOUR = 20


def after_hours_end_et_hour() -> int:
    """미국 애프터아워(시간외) 종료 시각(ET, 정수시)을 반환합니다.

    `us_session_closed()` 는 **정규장 마감(16:00 ET)** 만 판단합니다. 정산은
    그 뒤에 이어지는 애프터아워 체결까지 확정되어야 하므로 별도 기준이
    필요합니다. 기본 20:00 ET 이며 `AFTER_HOURS_END_ET_HOUR` 로 조정합니다.

    조기폐장일에도 보수적으로 20:00 를 씁니다(실제 17:00 종료보다 늦게 기다림).
    """
    raw = os.getenv(AFTER_HOURS_END_ET_HOUR_ENV, "").strip()
    if raw:
        try:
            hour = int(raw)
        except ValueError:
            hour = DEFAULT_AFTER_HOURS_END_ET_HOUR
        if 0 <= hour <= 23:
            return hour
    return DEFAULT_AFTER_HOURS_END_ET_HOUR


def us_session_after_hours_end(
    session_date: str, now_utc: datetime | None = None
) -> bool:
    """지정 세션의 **애프터아워 창까지** 끝났는지 확인합니다.

    정규장 마감(`us_session_closed`)과 분리된 기준입니다. KST 09:30 은
    동절기 기준 ET 19:30 이라 정규장은 닫혔지만 애프터아워(20:00 종료)는
    아직 진행 중입니다. 이 함수가 False 이면 SETTLE 은 보류해야 합니다.

    Parameters:
        session_date (str): "YYYY-MM-DD" 형식의 세션 날짜.
        now_utc (datetime | None): 기준 시각(UTC). None 이면 현재 시각.

    Returns:
        bool: 세션이 존재하고 now ≥ 해당 세션일의 애프터아워 종료(ET)이면 True.
    """
    cal = _get_xnys_calendar()
    if not session_date or not cal.is_session(session_date):
        return False
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    try:
        session_day = _date.fromisoformat(str(session_date))
    except ValueError:
        return False
    end_et = datetime.combine(
        session_day,
        dtime(hour=after_hours_end_et_hour()),
        tzinfo=ZoneInfo("America/New_York"),
    )
    return now_utc.astimezone(timezone.utc) >= end_et.astimezone(timezone.utc)


def us_session_in_progress(now_utc: datetime | None = None) -> str | None:
    """현재 **정규장이 진행 중**인 세션 날짜를 반환합니다 (없으면 None).

    정산 대상 선택에서 "진행 중인 세션"을 실수로 포함하지 않도록 확인하는
    진단용 helper 입니다. `us_session_last_closed_date()` 는 정규장이 닫힌
    세션만 반환하므로, 진행 중 세션의 체결은 그 대상에 포함되지 않습니다.
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    session_date = us_session_date_now(now_utc)
    if not session_date or not cal.is_session(session_date):
        return None
    try:
        open_ts = cal.session_open(session_date).to_pydatetime()
        close_ts = cal.session_close(session_date).to_pydatetime()
    except Exception:
        return None
    if open_ts.astimezone(timezone.utc) <= now_utc < close_ts.astimezone(timezone.utc):
        return session_date
    return None


def us_session_last_closed_date(now_utc: datetime | None = None) -> str | None:
    """
    이미 마감된 가장 최근 세션 날짜를 반환합니다.

    Parameters:
        now_utc (datetime | None): 기준 시각(UTC). None이면 현재 시각 사용.

    Returns:
        str | None: 가장 최근 마감된 세션 날짜("YYYY-MM-DD").
                    아직 마감된 세션이 없으면 None.
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    today_et = now_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    # 오늘(ET) 기준으로 가장 최근 세션부터 확인
    session_str = today_et
    prev_session = cal.date_to_session(today_et, direction="previous")
    if prev_session is not None:
        session_str = prev_session.strftime("%Y-%m-%d")

    checked = set()
    while session_str is not None and session_str not in checked:
        checked.add(session_str)
        if us_session_closed(session_str, now_utc=now_utc):
            return session_str
        # 이전 세션으로 이동: 현재 세션 날짜 - 1일 후 date_to_session(previous)
        d = _date.fromisoformat(session_str) - timedelta(days=1)
        nxt = cal.date_to_session(d.strftime("%Y-%m-%d"), direction="previous")
        if nxt is None:
            return None
        session_str = nxt.strftime("%Y-%m-%d")
    return None


def next_us_session(session_date: str) -> str | None:
    """
    지정한 세션 **다음** 미국 정규 세션 날짜를 반환합니다 (주말/공휴일 건너뜀).

    PREVIEW/APPROVE/EXECUTE 의 실행 대상 세션을 계산할 때 사용합니다.
    KST 아침 정산(setttle) 대상 세션(직전 마감 세션)의 다음 세션이 곧
    "오늘 밤 실행할 세션" 입니다. exchange_calendars(XNYS) 를 쓰므로
    주말·공휴일·서머타임 경계를 모두 반영합니다.

    Parameters:
        session_date (str): "YYYY-MM-DD" 형식의 기준 날짜/세션.

    Returns:
        str | None: 다음 세션 날짜. 캘린더 범위를 벗어나면 None.
    """
    cal = _get_xnys_calendar()
    try:
        if cal.is_session(session_date):
            nxt = cal.next_session(session_date)
        else:
            # 세션이 아닌 날짜(주말/공휴일)면 그 이후 첫 세션을 찾습니다.
            nxt = cal.date_to_session(session_date, direction="next")
            if nxt is not None and nxt.strftime("%Y-%m-%d") < session_date:
                nxt = cal.next_session(nxt)
    except Exception:
        return None
    return nxt.strftime("%Y-%m-%d") if nxt is not None else None
