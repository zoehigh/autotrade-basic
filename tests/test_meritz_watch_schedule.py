"""감시기 스케줄 검증 — 크론 줄(ops/cron.example) × 감시 창(미국 NYSE 달력, ET 기준).

  1) 서버 크론 시간대(KST)와 ET 변환: 서머타임·동절기 전환, 휴장일, 조기 폐장
  2) KST 자정 이후 미국 세션 날짜 연결: 자정 전후 실행이 같은 ET 세션이고, 18:30 KST 주문과 같은 세션으로 묶임
  3) 크론 커버리지: 실제 크론 줄이 모든 세션의 감시 창을 빈틈없이 덮고, 창 밖 호출은 즉시 종료(조용한 무동작)
"""
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meritz_watch as mw
import state as st
from broker.market_utils import _get_xnys_calendar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST, ET, UTC = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York"), timezone.utc
CAL = _get_xnys_calendar()
SETTINGS = mw.load_settings({})                      # 기본값: ET 06:00 ~ min(15:30, 마감−30분)


def kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(UTC)


# ── 크론 줄 파싱 (문서의 줄이 곧 검증 대상) ──────────────────────────────

def _watch_cron_fields():
    for line in open(os.path.join(ROOT, "ops", "cron.example"), encoding="utf-8"):
        m = re.match(r"#\s+((?:\S+\s+){4}\S+)\s+/\S*run_reprice_watch\.sh\s*$", line)   # 환경변수 없는 DRY 줄만
        if m:
            return m.group(1).split()
    raise AssertionError("cron.example 에 DRY 감시 줄이 없습니다")


def _expand(field, lo, hi):
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        values.update(range(a, b + 1, step))
    return values


MIN, HOUR, DOM, MON, DOW = _watch_cron_fields()
MINUTES, HOURS, DOWS = _expand(MIN, 0, 59), _expand(HOUR, 0, 23), _expand(DOW, 0, 7)
assert DOM == "*" and MON == "*"


def cron_fires(dt_utc):
    local = dt_utc.astimezone(KST)                    # 서버 CRON_TZ=Asia/Seoul
    cron_dow = (local.weekday() + 1) % 7              # 크론: 일=0, 월=1 … 토=6
    return local.minute in MINUTES and local.hour in HOURS and (cron_dow in DOWS or (cron_dow == 0 and 7 in DOWS))


def test_documented_cron_line_is_the_recommended_one():
    assert (MIN, HOUR, DOM, MON, DOW) == ("*/20", "19-23,0-5", "*", "*", "1-6")


# ── 1) KST 시각 ↔ 감시 창 (서머타임·휴장·조기폐장) ──────────────────────

@pytest.mark.parametrize("when,expected_start_kst", [
    ((2026, 11, 12), (2026, 11, 12, 20)),    # 동절기(EST): ET 06:00 = 20:00 KST
    ((2027, 3, 12), (2027, 3, 12, 20)),      # 서머타임 시작 전 금요일 (EST)
    ((2027, 3, 15), (2027, 3, 15, 19)),      # 서머타임(2027-03-14 일요일 시작) 첫 영업일: 19:00 KST
    ((2027, 11, 5), (2027, 11, 5, 19)),      # 서머타임 종료(2027-11-07) 직전 금요일 (EDT)
    ((2027, 11, 8), (2027, 11, 8, 20)),      # 종료 후 첫 영업일 (EST)
])
def test_window_starts_at_et_0600_across_dst(when, expected_start_kst):
    start = kst(*expected_start_kst)
    assert mw.watch_window(start, SETTINGS, CAL)[0] is True                      # 정각에 열림
    assert mw.watch_window(start - timedelta(minutes=1), SETTINGS, CAL)[0] is False   # 1분 전에는 닫힘


def test_window_end_is_1530_et_in_both_regimes():
    # 동절기: 15:30 ET = 다음 날 05:30 KST / 서머타임: 04:30 KST
    assert mw.watch_window(kst(2026, 11, 13, 5, 29), SETTINGS, CAL)[0] is True
    assert mw.watch_window(kst(2026, 11, 13, 5, 30), SETTINGS, CAL)[0] is False
    assert mw.watch_window(kst(2027, 3, 16, 4, 29), SETTINGS, CAL)[0] is True
    assert mw.watch_window(kst(2027, 3, 16, 4, 30), SETTINGS, CAL)[0] is False


@pytest.mark.parametrize("day_kst_evening", [(2026, 11, 26), (2026, 12, 25), (2027, 1, 18), (2026, 11, 14), (2026, 11, 15)])
def test_holidays_and_weekends_are_closed_all_evening(day_kst_evening):
    """추수감사절·성탄절·MLK·토·일: ET 날짜가 휴장이면 그 KST 저녁 전체가 닫힘 (달력 판단)."""
    y, m, d = day_kst_evening
    for hour in (20, 22, 23):
        inside, why, _ = mw.watch_window(kst(y, m, d, hour), SETTINGS, CAL)
        assert inside is False and "휴장" in why


@pytest.mark.parametrize("closed_session_day", [(2026, 11, 26), (2026, 12, 25)])
def test_after_midnight_hours_of_a_holiday_session_stay_closed(closed_session_day):
    """휴장일 자정 이후(KST 다음 날 새벽)도 그 ET 날짜가 휴장이므로 닫힘. (실제로는 전날 저녁 세션이 아님)"""
    y, m, d = closed_session_day
    nxt = date(y, m, d) + timedelta(days=1)
    inside, why, session = mw.watch_window(kst(nxt.year, nxt.month, nxt.day, 0, 20), SETTINGS, CAL)
    assert session == f"{y}{m:02d}{d:02d}" and inside is False


@pytest.mark.parametrize("session_day", [(2026, 11, 27), (2026, 12, 24)])
def test_early_close_shortens_the_window_to_close_minus_buffer(session_day):
    """조기 폐장(13:00 ET): 창 종료 = 12:30 ET (= 다음 날 02:30 KST, 동절기)."""
    y, m, d = session_day
    assert CAL.session_close(f"{y}-{m:02d}-{d:02d}").tz_convert(ET).strftime("%H:%M") == "13:00"
    et = lambda h, mi: datetime(y, m, d, h, mi, tzinfo=ET).astimezone(UTC)
    assert mw.watch_window(et(12, 29), SETTINGS, CAL)[0] is True
    inside, why, _ = mw.watch_window(et(12, 30), SETTINGS, CAL)
    assert inside is False and "종료" in why
    assert mw.watch_window(et(14, 0), SETTINGS, CAL)[0] is False                   # 폐장 후에도 닫힘


def test_calendar_failure_is_fail_closed():
    class Broken:
        def is_session(self, day):
            raise RuntimeError("calendar down")
    inside, why, _ = mw.watch_window(kst(2026, 11, 12, 21), SETTINGS, Broken())
    assert inside is False and "달력" in why


# ── 2) KST 자정 이후 미국 세션 날짜 연결 ─────────────────────────────────

def _kst_order_row(y, mo, d, h=18, mi=30):
    """메리츠 이력 행: ord_dt 는 KST 날짜 (봇의 18:30 KST 주문과 같은 형태)."""
    return {"ord_dt": f"{y}{mo:02d}{d:02d}", "ord_tmd": f"{h:02d}{mi:02d}00", "_ord_dt_is_kst_date": True}


@pytest.mark.parametrize("order_day", [(2026, 11, 10), (2026, 11, 13), (2027, 3, 15), (2027, 3, 12)])
def test_after_midnight_runs_use_the_same_session_as_the_1830_order(order_day):
    """KST D 18:30 에 낸 주문의 세션 = KST D 저녁·D+1 새벽 감시 실행의 세션 (ET 날짜 D). 자정을 넘어도 끊기지 않는다."""
    y, m, d = order_day
    order_session = st._order_us_session_date(_kst_order_row(y, m, d))
    assert order_session == f"{y}{m:02d}{d:02d}"                                   # 18:30 KST = 같은 ET 날짜
    nxt = date(y, m, d) + timedelta(days=1)
    for when in (kst(y, m, d, 21), kst(y, m, d, 23, 40), kst(nxt.year, nxt.month, nxt.day, 0, 20),
                 kst(nxt.year, nxt.month, nxt.day, 3, 0)):
        inside, _why, session = mw.watch_window(when, SETTINGS, CAL)
        assert session == order_session, f"{when.astimezone(KST):%m-%d %H:%M KST} → {session}"
        assert inside is True


def test_watch_record_survives_kst_midnight_and_rolls_at_the_next_session(tmp_path):
    """감시 기록(일일 정정 횟수)은 ET 세션 기준 — KST 자정에 초기화되지 않고, 다음 세션에서 새로 시작."""
    path = str(tmp_path / "w.json")
    _inside, _why, before = mw.watch_window(kst(2026, 11, 12, 23, 40), SETTINGS, CAL)
    record, _ = mw.load_record(path, before)
    mw.record_attempt(record, "SOXL", kst(2026, 11, 12, 23, 40), amended=True)
    mw.save_record(path, record)
    _inside, _why, after = mw.watch_window(kst(2026, 11, 13, 0, 20), SETTINGS, CAL)
    assert after == before == "20261112"
    again, _ = mw.load_record(path, after)
    assert again["symbols"]["SOXL"]["amends"] == 1                                  # 자정을 넘어도 유지
    _inside, _why, next_session = mw.watch_window(kst(2026, 11, 13, 20, 0), SETTINGS, CAL)
    assert next_session == "20261113"
    fresh, _ = mw.load_record(path, next_session)
    assert fresh["symbols"] == {}                                                   # 다음 세션은 새 기록


# ── 3) 크론 커버리지 (실제 크론 줄 × 실제 달력) ─────────────────────────

def _all_sessions(start=date(2026, 11, 12), end=date(2027, 9, 30)):
    day = start
    while day <= end:
        if CAL.is_session(day.isoformat()):
            yield day
        day += timedelta(days=1)


def test_cron_line_covers_every_session_window_without_gaps():
    """모든 세션(조기폐장·서머타임 전환 포함)에서: 창 안의 크론 실행이 창 시작 후 20분 안에 시작하고,
    실행 사이 간격이 20분을 넘지 않으며, 창 종료 20분 전 이후에도 실행이 남는다."""
    checked = 0
    for day in _all_sessions():
        start_et = datetime(day.year, day.month, day.day, 6, 0, tzinfo=ET)
        close_et = CAL.session_close(day.isoformat()).tz_convert(ET).to_pydatetime()
        end_et = min(datetime(day.year, day.month, day.day, 15, 30, tzinfo=ET), close_et - timedelta(minutes=30))
        fires = []
        t = start_et.astimezone(UTC) - timedelta(hours=2)
        stop = end_et.astimezone(UTC) + timedelta(hours=2)
        while t <= stop:
            if cron_fires(t):
                inside, _why, session = mw.watch_window(t, SETTINGS, CAL)
                if inside:
                    assert session == day.strftime("%Y%m%d")
                    fires.append(t)
            t += timedelta(minutes=1)
        assert fires, f"{day}: 감시 창 안에 크론 실행이 없음"
        assert fires[0] - start_et.astimezone(UTC) <= timedelta(minutes=20), f"{day}: 시작 지연"
        assert end_et.astimezone(UTC) - fires[-1] <= timedelta(minutes=20), f"{day}: 종료 전 공백"
        assert all(b - a <= timedelta(minutes=20) for a, b in zip(fires, fires[1:])), f"{day}: 실행 간 공백"
        checked += 1
    assert checked > 200                                                              # 거의 1년치 세션을 실제로 검사했는지


def test_out_of_window_cron_fires_are_quiet_noops():
    """크론이 부르는 시각 중 창 밖은 즉시 종료(0)해야 한다 — 매 실행이 창 안일 필요는 없다."""
    outside = 0
    t = kst(2026, 11, 12, 18, 0)
    for _ in range(0, 24 * 3):
        if cron_fires(t) and not mw.watch_window(t, SETTINGS, CAL)[0]:
            outside += 1
        t += timedelta(minutes=20)
    assert outside > 0
