"""
US 세션 날짜 헬퍼 + 일봉 캔들 날짜 매핑 테스트 (네트워크 없음).

- market_utils: us_session_date_now / us_session_closed / us_session_last_closed_date
- broker.base: Broker.get_daily_candles 기본 구현 (date=None)
- DryBroker: get_daily_candles 위임
- toss.adapter: TossBroker.get_daily_candles (mocked candles payload)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from broker.base import Broker, DryBroker, BrokerError
from broker.market_utils import (
    us_session_date_now,
    us_session_closed,
    us_session_last_closed_date,
    _get_xnys_calendar,
)


# ═══════════════════════════════════════════════════════════════════
# market_utils: us_session_date_now
# ═══════════════════════════════════════════════════════════════════

class TestUsSessionDateNow:
    def test_friday_session_during_trading(self):
        """금요일 장중 → 금요일 세션 날짜"""
        # 2026-10-16 (금) 15:00 ET = 19:00 UTC (EDT)
        now = datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc)
        assert us_session_date_now(now) == "2026-10-16"

    def test_saturday_returns_friday(self):
        """토요일 → 직전 금요일 세션"""
        # 2026-10-17 (토) 12:00 ET = 16:00 UTC
        now = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
        assert us_session_date_now(now) == "2026-10-16"

    def test_sunday_returns_friday(self):
        """일요일 → 직전 금요일 세션"""
        now = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)
        assert us_session_date_now(now) == "2026-10-16"

    def test_holiday_returns_previous_session(self):
        """추수감사절(2026-11-26) → 직전 세션 2026-11-25"""
        now = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
        assert us_session_date_now(now) == "2026-11-25"

    def test_tuesday_session(self):
        """화요일 장중 → 화요일 세션"""
        now = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
        assert us_session_date_now(now) == "2026-10-20"

    def test_naive_datetime_treated_as_utc(self):
        """timezone 없는 datetime은 UTC로 간주"""
        naive = datetime(2026, 10, 16, 19, 0)
        assert us_session_date_now(naive) == "2026-10-16"


# ═══════════════════════════════════════════════════════════════════
# market_utils: us_session_closed
# ═══════════════════════════════════════════════════════════════════

class TestUsSessionClosed:
    def test_normal_session_after_close(self):
        """금요일 정규 마감(16:00 ET = 20:00 UTC) 후 → True"""
        # 2026-10-16 (금) 마감 = 20:00 UTC
        now = datetime(2026, 10, 16, 20, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-10-16", now_utc=now) is True

    def test_normal_session_before_close(self):
        """금요일 장중(15:00 ET = 19:00 UTC) → False"""
        now = datetime(2026, 10, 16, 19, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-10-16", now_utc=now) is False

    def test_non_session_date_returns_false(self):
        """토요일 → False (세션 없음)"""
        now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-10-17", now_utc=now) is False

    def test_holiday_returns_false(self):
        """추수감사절 → False"""
        now = datetime(2026, 11, 26, 15, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-11-26", now_utc=now) is False

    def test_early_close_day(self):
        """추수감사절 다음날(2026-11-27) 조기마감 13:00 ET = 18:00 UTC
        18:00 UTC 이전이면 False, 이후면 True"""
        before_early = datetime(2026, 11, 27, 17, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-11-27", now_utc=before_early) is False
        after_early = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
        assert us_session_closed("2026-11-27", now_utc=after_early) is True

    def test_naive_datetime_treated_as_utc(self):
        """timezone 없는 datetime은 UTC로 간주"""
        naive = datetime(2026, 10, 16, 20, 0, 0)
        assert us_session_closed("2026-10-16", now_utc=naive) is True


# ═══════════════════════════════════════════════════════════════════
# market_utils: us_session_last_closed_date
# ═══════════════════════════════════════════════════════════════════

class TestUsSessionLastClosedDate:
    def test_after_friday_close(self):
        """금요일 마감 후 (토요일) → 금요일"""
        now = datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc)
        assert us_session_last_closed_date(now) == "2026-10-16"

    def test_during_tuesday_session(self):
        """화요일 장중 → 월요일 (화요일은 아직 마감 안 됨)"""
        now = datetime(2026, 10, 20, 18, 0, 0, tzinfo=timezone.utc)
        assert us_session_last_closed_date(now) == "2026-10-19"

    def test_tuesday_after_close(self):
        """화요일 마감 후 (수요일 새벽) → 화요일"""
        now = datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc)
        assert us_session_last_closed_date(now) == "2026-10-20"

    def test_monday_before_open(self):
        """월요일 장 시작 전 → 금요일"""
        # 2026-10-19 (월) 08:00 ET = 12:00 UTC (장 시작 09:30 ET 이전)
        now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        assert us_session_last_closed_date(now) == "2026-10-16"


# ═══════════════════════════════════════════════════════════════════
# broker.base: get_daily_candles 기본 구현 (date=None)
# ═══════════════════════════════════════════════════════════════════

class _StubBroker(Broker):
    """get_daily_closes만 구현한 스텁 — 나머지 추상 메서드는 no-op."""
    name = property(lambda self: "stub")
    is_trading_day = lambda self: True
    get_stock_price = lambda self, symbol, exchange: None
    get_stock_quotation = lambda self, symbol, exchange: None
    get_balance = lambda self, symbol, exchange: None
    get_purchase_amount = lambda self, symbol, exchange: None
    get_order_history = lambda self, symbol, exchange, days=30, verbose=False, limit=100: []
    place_order = lambda self, symbol, exchange, side, quantity, price, order_type: None
    exchange_code = lambda self, user_code: user_code

    def __init__(self, closes):
        self._closes = closes

    def get_daily_closes(self, symbol, exchange, days=5):
        return self._closes


class TestBaseGetDailyCandles:
    def test_default_returns_date_none(self):
        """기본 구현: date=None, close 값 보존, 오래된 순 유지"""
        stub = _StubBroker([65.0, 66.0, 67.0])
        result = stub.get_daily_candles("TQQQ", "NAS", days=3)
        assert result == [
            {"date": None, "close": 65.0},
            {"date": None, "close": 66.0},
            {"date": None, "close": 67.0},
        ]

    def test_default_empty_closes(self):
        stub = _StubBroker([])
        assert stub.get_daily_candles("TQQQ", "NAS") == []


# ═══════════════════════════════════════════════════════════════════
# DryBroker: get_daily_candles 위임
# ═══════════════════════════════════════════════════════════════════

class TestDryBrokerGetDailyCandles:
    def test_delegates_to_real_broker(self):
        """DryBroker가 실제 브로커의 get_daily_candles를 위임"""
        real = MagicMock()
        real.get_daily_candles.return_value = [
            {"date": "2026-10-16", "close": 68.21},
            {"date": "2026-10-19", "close": 70.64},
        ]
        dry = DryBroker(real)
        result = dry.get_daily_candles("TQQQ", "NAS", days=2)
        assert result == [
            {"date": "2026-10-16", "close": 68.21},
            {"date": "2026-10-19", "close": 70.64},
        ]
        real.get_daily_candles.assert_called_once_with("TQQQ", "NAS", 2)

    def test_dated_broker_keeps_dates_through_dry(self):
        """DryBroker가 날짜 있는 브로커를 감싸도 날짜 손실 없음"""
        real = MagicMock()
        dated = [{"date": "2026-10-16", "close": 68.21}]
        real.get_daily_candles.return_value = dated
        dry = DryBroker(real)
        assert dry.get_daily_candles("TQQQ", "NAS") == dated


# ═══════════════════════════════════════════════════════════════════
# toss.adapter: TossBroker.get_daily_candles (mocked)
# ═══════════════════════════════════════════════════════════════════

def _make_toss_broker(monkeypatch, canned_data):
    """_request_with_rate_retry를 canned 응답으로 대체한 TossBroker 생성."""
    from broker.toss.adapter import TossBroker
    monkeypatch.setattr(
        "config.BROKER_CONFIG",
        {"domain": "https://openapi.tossinvest.com", "client_id": "x",
         "client_secret": "y", "account_seq": "z"},
        raising=False,
    )
    broker = TossBroker.__new__(TossBroker)
    broker._domain = "https://openapi.tossinvest.com"
    broker._client_id = "x"
    broker._client_secret = "y"
    broker._account_seq = "z"
    broker._rate_limit_wait = 0.0
    broker._session = MagicMock()
    broker._get_token = lambda: "fake-token"
    broker._request_with_rate_retry = MagicMock(return_value=canned_data)
    return broker


class TestTossGetDailyCandles:
    def test_dates_and_order(self, monkeypatch):
        """정상 캔들: 날짜 정상 추출, 오래된 순, close 필터링"""
        canned = {"result": {"candles": [
            # 최신 → 과거 순서로 (API 원본)
            {"timestamp": "2026-10-20T13:00:00.000+09:00", "closePrice": "70.2"},
            {"timestamp": "2026-10-19T13:00:00.000+09:00", "closePrice": "70.64"},
            {"timestamp": "2026-10-16T14:00:00.000+09:00", "closePrice": "68.21"},
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=3)
        assert result == [
            {"date": "2026-10-16", "close": 68.21},
            {"date": "2026-10-19", "close": 70.64},
            {"date": "2026-10-20", "close": 70.2},
        ]

    def test_skips_nonpositive_close(self, monkeypatch):
        """close ≤ 0 캔들 제외"""
        canned = {"result": {"candles": [
            {"timestamp": "2026-10-20T13:00:00.000+09:00", "closePrice": "70.2"},
            {"timestamp": "2026-10-19T13:00:00.000+09:00", "closePrice": "0"},
            {"timestamp": "2026-10-16T14:00:00.000+09:00", "closePrice": "-1.5"},
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=3)
        assert result == [{"date": "2026-10-20", "close": 70.2}]

    def test_skips_unparseable_date(self, monkeypatch):
        """날짜 파싱 불가 캔들 제외 (malformed timestamp)"""
        canned = {"result": {"candles": [
            {"timestamp": "2026-10-20T13:00:00.000+09:00", "closePrice": "70.2"},
            {"timestamp": "not-a-date", "closePrice": "70.64"},
            {"timestamp": "", "closePrice": "68.21"},
            {"closePrice": "65.0"},  # timestamp 없음
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=4)
        assert result == [{"date": "2026-10-20", "close": 70.2}]

    def test_skips_malformed_close(self, monkeypatch):
        """close 파싱 불가 캔들 제외"""
        canned = {"result": {"candles": [
            {"timestamp": "2026-10-20T13:00:00.000+09:00", "closePrice": "abc"},
            {"timestamp": "2026-10-19T13:00:00.000+09:00", "closePrice": "70.64"},
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=2)
        assert result == [{"date": "2026-10-19", "close": 70.64}]

    def test_empty_candles(self, monkeypatch):
        """빈 캔들 목록 → 빈 리스트"""
        canned = {"result": {"candles": []}}
        broker = _make_toss_broker(monkeypatch, canned)
        assert broker.get_daily_candles("TQQQ", "NAS") == []

    def test_est_winter_timestamp(self, monkeypatch):
        """EST(겨울) timestamp도 올바른 ET 날짜로 변환"""
        # 2026-01-15 (목) 자정 ET = 14:00 KST (EST = UTC-5)
        canned = {"result": {"candles": [
            {"timestamp": "2026-01-15T14:00:00.000+09:00", "closePrice": "55.0"},
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=1)
        assert result == [{"date": "2026-01-15", "close": 55.0}]

    def test_dst_and_est_seasonal_offsets(self, monkeypatch):
        """DST(13:00 KST)와 EST(14:00 KST) 계절별 오프셋 모두 검증.

        토스 일봉 timestamp는 미국 ET 자정을 KST로 표현하며,
        서머타임(EDT) 동안에는 13:00+09:00, 표준시(EST) 동안에는
        14:00+09:00로 표시됩니다 (계절별 자동 전환).
        라이브 API 200건(2025-11 ~ 2026-10) 대조 검증 완료 (2026-10-20):
        ET 변환 날짜 == XNYS 세션 날짜, 불일치 0건.
        """
        # DST 기간: 2026-10-20 (화) 자정 ET = 13:00 KST (EDT = UTC-4)
        # EST 기간: 2026-02-27 (금) 자정 ET = 14:00 KST (EST = UTC-5)
        # API는 최신 → 과거 순서 반환
        canned = {"result": {"candles": [
            {"timestamp": "2026-10-20T13:00:00.000+09:00", "closePrice": "70.20"},
            {"timestamp": "2026-02-27T14:00:00.000+09:00", "closePrice": "55.30"},
        ]}}
        broker = _make_toss_broker(monkeypatch, canned)
        result = broker.get_daily_candles("TQQQ", "NAS", days=2)
        # 오래된 순(EST 먼저, DST 나중에) + 각 세션 날짜 정확히 매핑
        assert result == [
            {"date": "2026-02-27", "close": 55.30},
            {"date": "2026-10-20", "close": 70.20},
        ]

    def test_broker_error_propagates(self, monkeypatch):
        """_request_with_rate_retry가 BrokerError 발생 시 전파"""
        broker = _make_toss_broker(monkeypatch, {})
        broker._request_with_rate_retry.side_effect = BrokerError("API 오류")
        with pytest.raises(BrokerError):
            broker.get_daily_candles("TQQQ", "NAS")
