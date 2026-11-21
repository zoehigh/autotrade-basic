"""리버스 종료 판정 — 날짜 포함 일봉 회귀 테스트.

검증 시나리오:
  1. 리버스 첫날은 종료 판정 없이 MOC 매도 경로 유지
  2. 확정 종가가 종료선 초과 → 종료
  3. 현재가만 종료선 초과 → 리버스 유지
  4. 확정 종가 초과·현재가 이하 → 확정 종가 기준 종료
  5. 날짜 누락·지연·API 오류 → EXIT_UNCONFIRMED 보류
  6. 휴장·주말·조기 폐장 (XNYS 캘린더)
  7. 미국 서머타임 전환
  8. 당일 미확정 봉이 포함된 응답
  9. 기존 get_daily_closes() 사용처 회귀 (브로커 계약 테스트에 포함)
 10. 동일 종료 판정의 일봉 API 호출 1회 보장
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker.market_utils import normalize_daily_candle_date, us_session_last_closed_date
from strategy import HOLD_EXIT_UNCONFIRMED, execute_reverse_mode

PREV = "2026-11-25"          # 확정된 직전 정규장 세션 (테스트용 고정값)
EXIT_LINE = 65.5732 * 0.85   # TQQQ 종료선 ≈ 55.74
ABOVE = 59.26                # 종료선 위 확정 종가
BELOW = 54.0                 # 종료선 아래 확정 종가


@pytest.fixture(autouse=True)
def _fixed_session(monkeypatch):
    """세션 날짜·별지점을 고정해 캘린더/시계에 좌우되지 않게 합니다."""
    monkeypatch.setattr("strategy.us_session_last_closed_date", lambda *a, **k: PREV)
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)


def _run(candles, last_price, *, day_count=1, dated=True, broker=None):
    state = {
        "T": 19.7734,
        "reverse_mode": {"active": True, "day_count": day_count},
        "close_prices": [59.0],
    }
    if broker is None:
        broker = MagicMock()
        broker.provides_daily_candle_dates = dated
        broker.get_daily_candles.return_value = candles
    result = execute_reverse_mode(
        broker, "TQQQ", "NAS", 20, "TQQQ",
        position_qty=144, avg_price=65.5732, orderable_cash=1000.0,
        last_price=last_price, state=state,
    )
    return result, broker


def _dated(close, date=PREV):
    return [{"date": date, "close": close}]


# ── 1. 첫날: 종료 판정 생략 + MOC 유지 ────────────────────────────────────────
def test_first_day_keeps_moc_and_never_queries_daily_candles():
    result, broker = _run(_dated(ABOVE), ABOVE, day_count=0)
    assert result["exit"] is False
    assert [o["order_type"] for o in result["orders"]] == ["MOC"]
    assert result["reverse_day"] == 1
    broker.get_daily_candles.assert_not_called()


# ── 2·4. 확정 종가가 종료선 초과 → 종료 (현재가가 아래여도 동일) ─────────────────
def test_confirmed_close_above_exits_even_when_last_price_below():
    result, broker = _run(_dated(ABOVE), BELOW)
    assert result["exit"] is True
    assert result["orders"] == []
    assert broker.get_daily_candles.call_count == 1     # 10. 중복 조회 없음


# ── 3. 현재가만 종료선 초과 → 리버스 유지 ──────────────────────────────────────
def test_last_price_alone_above_keeps_reverse():
    result, broker = _run(_dated(BELOW), ABOVE)
    assert result["exit"] is False
    assert result["orders"], "확정 종가가 종료선 아래면 리버스 주문이 나가야 합니다"
    assert all(o.get("reverse_action") for o in result["orders"])
    assert broker.get_daily_candles.call_count == 1


# ── 5. 날짜 누락·지연·API 오류 → 보류 ──────────────────────────────────────────
@pytest.mark.parametrize("candles", [
    [{"date": None, "close": ABOVE}],                 # 날짜 누락
    [{"date": "2020-01-02", "close": ABOVE}],         # 세션 불일치(지연)
    [],                                               # 빈 응답
    [{"date": PREV, "close": 0}],                     # 유효하지 않은 종가
])
def test_dated_broker_holds_on_missing_or_mismatched_date(candles):
    result, broker = _run(candles, ABOVE)
    assert result["exit"] is False
    assert result["orders"] == []
    assert result["hold_reason"] == HOLD_EXIT_UNCONFIRMED
    assert broker.get_daily_candles.call_count == 1


def test_dated_broker_holds_on_api_error():
    broker = MagicMock()
    broker.provides_daily_candle_dates = True
    broker.get_daily_candles.side_effect = RuntimeError("일봉 조회 실패")
    result, broker = _run(None, ABOVE, broker=broker)
    assert result["exit"] is False
    assert result["orders"] == []
    assert result["hold_reason"] == HOLD_EXIT_UNCONFIRMED
    broker.get_daily_candles.assert_called_once()


def test_undated_broker_keeps_legacy_last_price_fallback():
    """날짜 미제공 브로커(기본 구현)는 기존처럼 현재가로 판정 — 정지시키지 않습니다."""
    result, _ = _run([{"date": None, "close": ABOVE}], ABOVE, dated=False)
    assert result["exit"] is True
    assert result["orders"] == []


# ── 8. 당일 미확정 봉이 포함된 응답 ────────────────────────────────────────────
def test_today_unconfirmed_candle_is_ignored():
    candles = [
        {"date": "2026-11-26", "close": ABOVE},   # 미확정 당일 봉 (높은 가격)
        {"date": PREV, "close": BELOW},           # 확정 직전 세션 (낮은 가격)
    ]
    result, _ = _run(candles, ABOVE)
    assert result["exit"] is False, "당일 미확정 봉으로 종료하면 안 됩니다"
    assert result["orders"]


# ── 6·7. XNYS 캘린더: 휴장·주말·조기 폐장·서머타임 ──────────────────────────────
@pytest.mark.parametrize("now_utc, expected", [
    (datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc), "2026-11-13"),  # 월요일 장전 → 금요일
    (datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc), "2026-11-25"),  # 추수감사절 다음 날 장전
    (datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc), "2026-11-27"),  # 조기마감(13:00 ET) 이후
    (datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc), "2026-03-06"),   # DST 전환 직후 월요일 장전
    (datetime(2026, 3, 10, 21, 0, tzinfo=timezone.utc), "2026-03-10"),  # EDT 정규 마감(17:00Z) 이후
])
def test_last_closed_session_calendar(now_utc, expected):
    assert us_session_last_closed_date(now_utc) == expected


@pytest.mark.parametrize("value, expected", [
    ("20261113", "2026-11-13"),
    ("2026-11-13", "2026-11-13"),
    (20261113, "2026-11-13"),
    ("2026/11/13", "2026-11-13"),
    ("20260230", None),       # 유효하지 않은 날짜
    ("", None),
    (None, None),
    (True, None),
])
def test_normalize_daily_candle_date(value, expected):
    assert normalize_daily_candle_date(value) == expected
