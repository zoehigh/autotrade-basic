"""
/   -    .

KIS, , LS     
 ,       .
"""
import os
from datetime import date as _date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


_XNYS_CALENDAR = None


def get_kst_now() -> datetime:
    """(KST)   ."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


def resolve_real_kst_from_ord(ord_dt: str, ord_tmd: str):
    """KIS (= , =KST)     .

     (KIS/LS/)   API ord_dt=(ET) ,
    ord_tmd=KST  .  (KST  )  KST
     ord_dt  ord_dt+1.  KST  ET  
     ord_dt   .

      None .
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


def _history_sort_key(item: dict) -> str:
    """     ( ) .      ."""
    ord_dt = str(item.get("ord_dt") or "")
    if len(ord_dt) == 8 and ord_dt.isdigit():
        ord_tmd = "".join(ch for ch in str(item.get("ord_tmd") or "") if ch.isdigit())
        ord_tmd = ord_tmd.zfill(6)[:6]
        # KIS/LS/: ord_dt= , ord_tmd=KST      KST  
        # (23:00  04:00)      KST   .
        # Toss/Meritz  ord_dt   KST (_ord_dt_is_kst_date)  .
        if not item.get("_ord_dt_is_kst_date"):
            real_kst = resolve_real_kst_from_ord(ord_dt, ord_tmd)
            if real_kst is not None:
                return real_kst.strftime("%Y%m%d%H%M%S")
        return ord_dt + ord_tmd
    kst_iso = str(item.get("ord_datetime_kst") or "")
    digits = "".join(ch for ch in kst_iso[:19] if ch.isdigit())
    return digits.ljust(14, "0") if digits else ""


def select_recent_history(order_history: list, n: int) -> list:
    """[ ]    n   .

               n
        (100  ).    
       (    ).
            ,     .
    """
    if n <= 0:
        return []
    ordered = sorted(order_history, key=_history_sort_key)  #  :   
    return ordered[-n:]


def normalize_order_price(price) -> float:
    """
            ().

    :
    - $1.00 :  4 ($0.0001 )
    - $1.00 :  2 ($0.01 )

           .

    IEEE 754     Decimal  .
    : math.floor(140.42 * 100) / 100 = 140.41 ()    140.42 
    """
    from decimal import Decimal
    price = float(price)
    if price < 1.0:
        return float(int(Decimal(str(price)) * 10000) / 10000)
    return float(int(Decimal(str(price)) * 100) / 100)


def is_us_dst() -> bool:
    """    (ET)    ."""
    ny_now = datetime.now(ZoneInfo("America/New_York"))
    return bool(ny_now.dst() and ny_now.dst() != timedelta(0))


def is_us_trading_day() -> bool:
    """
         (NYSE ).

    exchange_calendars  XNYS()  
        .

    Returns:
        True:   (  )
        False:   (  )
    """
    global _XNYS_CALENDAR
    if _XNYS_CALENDAR is None:
        _XNYS_CALENDAR = xcals.get_calendar("XNYS")
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return _XNYS_CALENDAR.is_session(now_et.strftime("%Y-%m-%d"))


def _get_xnys_calendar():
    """XNYS   (  )."""
    global _XNYS_CALENDAR
    if _XNYS_CALENDAR is None:
        _XNYS_CALENDAR = xcals.get_calendar("XNYS")
    return _XNYS_CALENDAR


def us_session_date_now(now_utc: datetime | None = None) -> str | None:
    """
    (  )  (XNYS)   .

    ET(America/New_York)   ,   
       ( ) .

    Parameters:
        now_utc (datetime | None):  (UTC). None   .

    Returns:
        str | None: "YYYY-MM-DD"   .
            None   (now_utc   )
                  .  
              , None  **(pending)**
                 settlement  .
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    date_str = now_et.strftime("%Y-%m-%d")
    #   ,     ( )
    session = cal.date_to_session(date_str, direction="previous")
    return session.strftime("%Y-%m-%d") if session is not None else None


def us_session_closed(session_date: str, now_utc: datetime | None = None) -> bool:
    """
          .

    XNYS    (  )  .

    Parameters:
        session_date (str): "YYYY-MM-DD"   .
        now_utc (datetime | None):  (UTC). None   .

    Returns:
        bool:    UTC     True.
                () False (  "" ).
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
    """ ()  (ET, ) .

    `us_session_closed()`  ** (16:00 ET)**  . 
            
    .  20:00 ET  `AFTER_HOURS_END_ET_HOUR`  .

      20:00  ( 17:00   ).
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
    """  ** **  .

     (`us_session_closed`)  . KST 09:30 
      ET 19:30    (20:00 )
      .   False  SETTLE   .

    Parameters:
        session_date (str): "YYYY-MM-DD"   .
        now_utc (datetime | None):  (UTC). None   .

    Returns:
        bool:   now     (ET) True.
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
    """ **  **    ( None).

       "  "    
     helper . `us_session_last_closed_date()`   
     ,        .
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
          .

    Parameters:
        now_utc (datetime | None):  (UTC). None   .

    Returns:
        str | None:     ("YYYY-MM-DD").
                        None.
    """
    cal = _get_xnys_calendar()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    today_et = now_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    # (ET)     
    session_str = today_et
    prev_session = cal.date_to_session(today_et, direction="previous")
    if prev_session is not None:
        session_str = prev_session.strftime("%Y-%m-%d")

    checked = set()
    while session_str is not None and session_str not in checked:
        checked.add(session_str)
        if us_session_closed(session_str, now_utc=now_utc):
            return session_str
        #   :    - 1  date_to_session(previous)
        d = _date.fromisoformat(session_str) - timedelta(days=1)
        nxt = cal.date_to_session(d.strftime("%Y-%m-%d"), direction="previous")
        if nxt is None:
            return None
        session_str = nxt.strftime("%Y-%m-%d")
    return None


def next_us_session(session_date: str) -> str | None:
    """
      ****      (/ ).

    PREVIEW/APPROVE/EXECUTE       .
    KST  (setttle)  (  )   
    "   " . exchange_calendars(XNYS)  
    **   .

    Parameters:
        session_date (str): "YYYY-MM-DD"   /.

    Returns:
        str | None:   .    None.
    """
    cal = _get_xnys_calendar()
    try:
        if cal.is_session(session_date):
            nxt = cal.next_session(session_date)
        else:
            #   (/)     .
            nxt = cal.date_to_session(session_date, direction="next")
            if nxt is not None and nxt.strftime("%Y-%m-%d") < session_date:
                nxt = cal.next_session(nxt)
    except Exception:
        return None
    return nxt.strftime("%Y-%m-%d") if nxt is not None else None
