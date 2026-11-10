"""메리츠 가격 정정 감시기 테스트 (mock 전용 — 실제 브로커·알림·정정 호출 없음)."""
import fcntl
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))

import meritz_reprice as rp
import meritz_reprice_watch as watch
import meritz_watch as mw
import run_lock
import state as st
from broker.base import OrderError
from broker.market_utils import _get_xnys_calendar
from test_meritz_reprice import NEW, OLD, ET_TODAY, _row, _state

ET = ZoneInfo("America/New_York")
CAL = _get_xnys_calendar()
UTC = timezone.utc


def utc_from_et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(UTC)


SETTINGS = mw.load_settings({})


# ── 시간 창: ET 기준, 서머타임·조기마감·휴장일 ────────────────────────

def test_window_winter_inside_and_edges():
    ok, _, session = mw.watch_window(utc_from_et(2026, 11, 10, 12, 0), SETTINGS, CAL)
    assert ok and session == "20261110"
    assert not mw.watch_window(utc_from_et(2026, 11, 10, 5, 59), SETTINGS, CAL)[0]    # 시작 전
    assert mw.watch_window(utc_from_et(2026, 11, 10, 6, 0), SETTINGS, CAL)[0]         # 시작 포함
    assert mw.watch_window(utc_from_et(2026, 11, 10, 15, 29), SETTINGS, CAL)[0]
    assert not mw.watch_window(utc_from_et(2026, 11, 10, 15, 30), SETTINGS, CAL)[0]   # 종료 제외


def test_same_kst_clock_time_differs_between_summer_and_winter():
    """KST 19:00 은 서머타임에는 감시 창 안(06:00 ET), 동절기에는 밖(05:00 ET)."""
    summer = datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)
    winter = datetime(2026, 11, 10, 19, 0, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)
    assert mw.watch_window(summer, SETTINGS, CAL)[0] is True
    assert mw.watch_window(winter, SETTINGS, CAL)[0] is False
    winter_20 = datetime(2026, 11, 10, 20, 0, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)
    assert mw.watch_window(winter_20, SETTINGS, CAL)[0] is True


def test_window_closed_on_holiday_and_weekend():
    assert not mw.watch_window(utc_from_et(2026, 11, 26, 12, 0), SETTINGS, CAL)[0]  # 추수감사절
    assert not mw.watch_window(utc_from_et(2026, 11, 14, 12, 0), SETTINGS, CAL)[0]  # 토요일


def test_window_shrinks_on_early_close_day():
    day = "2026-11-27"  # 추수감사절 다음 날 조기 마감
    close_et = CAL.session_close(day).tz_convert(ET)
    assert close_et.hour < 16
    end = close_et - timedelta(minutes=SETTINGS["close_buffer_min"])
    inside = end.to_pydatetime() - timedelta(minutes=1)
    assert mw.watch_window(inside.astimezone(UTC), SETTINGS, CAL)[0]
    assert not mw.watch_window(end.to_pydatetime().astimezone(UTC), SETTINGS, CAL)[0]


def test_window_fails_closed_when_calendar_breaks():
    class Broken:
        def is_session(self, day):
            raise RuntimeError("boom")
    ok, why, _ = mw.watch_window(utc_from_et(2026, 11, 10, 12, 0), SETTINGS, Broken())
    assert not ok and "달력" in why


# ── 설정 검증 ────────────────────────────────────────────────────────

def test_defaults_are_dry_and_safe():
    s = mw.load_settings({})
    assert s["mode"] == "DRY" and s["max_amends_per_day"] == 3 and s["min_interval_min"] == 20


@pytest.mark.parametrize("env", [
    {"REPRICE_WATCH_MODE": "LIVE"}, {"REPRICE_WATCH_MODE": "yes"},
    {"REPRICE_WATCH_START_ET": "25:00"}, {"REPRICE_WATCH_START_ET": "abc"},
    {"REPRICE_WATCH_START_ET": "16:00", "REPRICE_WATCH_END_ET": "15:00"},
    {"REPRICE_WATCH_MIN_STEP": "0"}, {"REPRICE_WATCH_MIN_STEP": "inf"}, {"REPRICE_WATCH_MIN_STEP": "nan"},
    {"REPRICE_WATCH_MAX_AMENDS_PER_DAY": "0"}, {"REPRICE_WATCH_MIN_INTERVAL_MIN": "-5"},
])
def test_invalid_settings_are_rejected(env):
    with pytest.raises(mw.WatchConfigError):
        mw.load_settings(env)


def test_apply_requires_all_independent_gates():
    s = mw.load_settings({"REPRICE_WATCH_MODE": "APPLY"})
    cfg = SimpleNamespace(BROKER="meritz", TRADE_MODE="LIVE")
    assert mw.apply_gate_problems(s, cfg, {"ALLOW_BROKER_ORDERS": "1"}) == []
    assert mw.apply_gate_problems(s, cfg, {}) == ["ALLOW_BROKER_ORDERS=1 이 아님"]
    assert len(mw.apply_gate_problems(s, SimpleNamespace(BROKER="kis", TRADE_MODE="DRY"), {})) == 3
    assert mw.apply_gate_problems(mw.load_settings({}), SimpleNamespace(BROKER="kis", TRADE_MODE="DRY"), {}) == []


# ── 기록·간격·한도·알림 중복 ──────────────────────────────────────────

def test_throttle_interval_and_daily_cap_and_rollover(tmp_path):
    path = str(tmp_path / "w.json")
    now = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    record, _ = mw.load_record(path, "20261110")
    assert mw.throttle_reason(record, "SOXL", SETTINGS, now) is None
    mw.record_attempt(record, "SOXL", now, amended=True)
    assert "간격" in mw.throttle_reason(record, "SOXL", SETTINGS, now + timedelta(minutes=5))
    assert mw.throttle_reason(record, "SOXL", SETTINGS, now + timedelta(minutes=21)) is None
    for _ in range(2):
        mw.record_attempt(record, "SOXL", now, amended=True)
    assert "한도" in mw.throttle_reason(record, "SOXL", SETTINGS, now + timedelta(hours=3))
    mw.save_record(path, record)
    again, _ = mw.load_record(path, "20261110")
    assert again["symbols"]["SOXL"]["amends"] == 3
    next_day, _ = mw.load_record(path, "20261111")  # 세션이 바뀌면 새 기록
    assert next_day["symbols"] == {}


def test_corrupt_record_starts_fresh_with_warning(tmp_path):
    path = tmp_path / "w.json"
    path.write_text("{broken")
    record, warning = mw.load_record(str(path), "20261110")
    assert record["symbols"] == {} and warning


def test_should_notify_dedupes_and_repeats_after_interval():
    record = {"notified": {}}
    now = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    assert mw.should_notify(record, "k", "v", now)
    assert not mw.should_notify(record, "k", "v", now + timedelta(minutes=1))
    assert mw.should_notify(record, "k", "v2", now + timedelta(minutes=1))
    assert mw.should_notify(record, "r", "v", now, repeat_after_min=60)
    assert not mw.should_notify(record, "r", "v", now + timedelta(minutes=30), repeat_after_min=60)
    assert mw.should_notify(record, "r", "v", now + timedelta(minutes=61), repeat_after_min=60)


# ── run_once: 가짜 브로커/알림/달력으로 전체 흐름 ─────────────────────

class OpenCalendar:
    """항상 열린 달력 — 테스트가 실제 시각에 의존하지 않게 합니다."""
    def is_session(self, day):
        return True

    def session_close(self, day):
        return pd.Timestamp(f"{day} 23:59:59", tz=ET)


class FakeBroker:
    def __init__(self, last=65.0, error=None, new_row_rejected=False, history_error=None):
        self.last, self.error, self.new_row_rejected, self.history_error = last, error, new_row_rejected, history_error
        self.calls, self.closed = [], False
        self.history = [_row()]

    def exchange_code(self, ex):
        return "NY"

    def get_order_history(self, *a, **k):
        if self.history_error:
            raise self.history_error
        return deepcopy(self.history)

    def get_stock_price(self, *a, **k):
        return SimpleNamespace(last=self.last)

    def modify_order(self, odno, symbol, exchange, side, qty, price, order_type):
        self.calls.append((odno, qty, price, order_type))
        if self.error:
            raise self.error
        self.history[0] = _row(status="정정됨")
        self.history.append(_row(odno=NEW, rctf="1", orgl="100", price=price,
                                 status="거부" if self.new_row_rejected else "미체결"))
        return NEW

    def close(self):
        self.closed = True


class Notifier:
    def __init__(self):
        self.sent = []

    def __call__(self, message, urgent=False):
        self.sent.append((urgent, message))


ENV = {"REPRICE_WATCH_START_ET": "00:00", "REPRICE_WATCH_END_ET": "23:59", "REPRICE_WATCH_CLOSE_BUFFER_MIN": "0"}
APPLY_ENV = {**ENV, "REPRICE_WATCH_MODE": "APPLY", "ALLOW_BROKER_ORDERS": "1"}
CONFIG_LIVE = SimpleNamespace(BROKER="meritz", TRADE_MODE="LIVE", SYMBOLS=[{"symbol": "SOXL", "exchange": "AMS"}])
CONFIG_DRY = SimpleNamespace(BROKER="meritz", TRADE_MODE="DRY", SYMBOLS=[{"symbol": "SOXL", "exchange": "AMS"}])
NOW = datetime.now(UTC)


@pytest.fixture
def world():
    st.save_state("SOXL", _state())
    return SimpleNamespace(broker=FakeBroker(), notifier=Notifier())


def run(world, env=None, config=CONFIG_DRY, now=NOW, calendar=None, broker=None):
    created = []

    def factory():
        created.append(1)
        return broker or world.broker

    code = watch.run_once(env if env is not None else ENV, now, config, factory, world.notifier,
                          calendar or OpenCalendar())
    return code, created


def test_dry_reports_candidate_without_writing_trading_state_or_calling_modify(world):
    before = open(st._STATE_FILE).read()
    code, created = run(world)
    assert code == 0 and created and world.broker.calls == []
    assert open(st._STATE_FILE).read() == before
    assert len(world.notifier.sent) == 1 and not world.notifier.sent[0][0]  # 일반(비긴급) 1건
    assert "DRY" in world.notifier.sent[0][1] and "73.69" in world.notifier.sent[0][1]
    assert not os.path.exists(run_lock.journal_path()) and not os.path.exists(run_lock.watch_halt_path())
    # 같은 내용은 반복 알리지 않음 (크론이 10분마다 호출)
    run(world)
    assert len(world.notifier.sent) == 1 and world.broker.closed


def test_outside_window_exits_without_touching_broker(world):
    far_morning = utc_from_et(2026, 11, 10, 3, 0)
    code, created = run(world, env={}, now=far_morning, calendar=CAL)
    assert code == 0 and created == [] and world.notifier.sent == []


def test_apply_refused_without_all_gates_and_never_downgrades_to_dry(world):
    code, created = run(world, env={**ENV, "REPRICE_WATCH_MODE": "APPLY"}, config=CONFIG_LIVE)  # ALLOW 없음
    assert code == 2 and created == []
    code, created = run(world, env=APPLY_ENV, config=CONFIG_DRY)  # TRADE_MODE=DRY
    assert code == 2 and created == [] and world.broker.calls == []


def test_invalid_mode_is_config_error(world):
    code, created = run(world, env={**ENV, "REPRICE_WATCH_MODE": "maybe"})
    assert code == 2 and created == []


def test_non_meritz_broker_is_refused(world):
    code, created = run(world, config=SimpleNamespace(BROKER="kis", TRADE_MODE="DRY", SYMBOLS=[]))
    assert code == 2 and created == []


def test_apply_amends_once_relinks_meta_and_throttles_next_run(world):
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 0 and len(world.broker.calls) == 1 and world.broker.calls[0][2] == 73.69
    metas = st.load_state("SOXL")["orders_meta"]
    assert NEW in metas and OLD not in metas and metas[NEW]["original_price"] == 73.69
    assert rp.read_journal(run_lock.journal_path()) == []
    assert any("정정 완료" in m for _u, m in world.notifier.sent)
    # 바로 다시 실행 → 새 주문은 이미 목표가(NOOP)이고 간격 제한도 있어 추가 정정 없음
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert len(world.broker.calls) == 1


def test_apply_respects_daily_cap_and_min_interval(world):
    record = {"session": ET_TODAY, "symbols": {"SOXL": {"amends": 3, "attempts": 3,
              "last_attempt_utc": (NOW - timedelta(hours=3)).isoformat()}}, "notified": {}}
    mw.save_record(run_lock.watch_state_path(), record)
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert world.broker.calls == [] and any("한도" in m for _u, m in world.notifier.sent)
    record["symbols"]["SOXL"] = {"amends": 1, "attempts": 1, "last_attempt_utc": (NOW - timedelta(minutes=3)).isoformat()}
    mw.save_record(run_lock.watch_state_path(), record)
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert world.broker.calls == []


def test_min_step_skips_tiny_changes(world):
    world.broker.history = [_row(price=73.66)]  # 목표 73.69 와 $0.03 차이
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert world.broker.calls == []


def test_uncertain_amend_halts_and_next_run_refuses_even_without_broker(world):
    world.broker.error = OrderError("timeout")
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and len(world.broker.calls) == 1
    halt = json.load(open(run_lock.watch_halt_path()))
    assert halt["result"] == "UNCERTAIN" and halt["symbol"] == "SOXL"
    assert any(urgent for urgent, _m in world.notifier.sent)
    assert len(rp.read_journal(run_lock.journal_path())) == 1       # 복구 근거 보존
    # 중단 상태: 다음 실행은 브로커를 만들지도, 재주문하지도 않음
    code, created = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and created == [] and len(world.broker.calls) == 1
    # DRY 는 중단 표식과 무관하게 계획 알림만 가능 (journal 이 있으면 복구 안내)
    code, _ = run(world, env=ENV, config=CONFIG_DRY)
    assert code == 1 and len(world.broker.calls) == 1


def test_late_rejection_halts_without_moving_meta(world):
    world.broker.new_row_rejected = True
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and json.load(open(run_lock.watch_halt_path()))["result"] == "REJECTED_LATE"
    assert OLD in st.load_state("SOXL")["orders_meta"]


def test_apply_recovers_resolvable_journal_then_continues(world):
    # 정정은 접수됐지만 state 연결 전에 종료된 상황: 이력에 새 주문 있음, journal 남음
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69,
                                                "original_price": 73.69}])
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    metas = st.load_state("SOXL")["orders_meta"]
    assert code == 0 and NEW in metas and OLD not in metas and rp.read_journal(run_lock.journal_path()) == []
    assert world.broker.calls == []


def test_apply_halts_on_unresolvable_journal(world):
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69}])
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)  # 이력에 정정 행 없음
    assert code == 1 and json.load(open(run_lock.watch_halt_path()))["result"] == "JOURNAL_UNRESOLVED"
    assert world.broker.calls == [] and len(rp.read_journal(run_lock.journal_path())) == 1


def test_dry_with_journal_never_writes_and_alerts_urgently(world):
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69}])
    before = open(run_lock.journal_path()).read(), open(st._STATE_FILE).read()
    code, _ = run(world, env=ENV, config=CONFIG_DRY)
    assert code == 1 and (open(run_lock.journal_path()).read(), open(st._STATE_FILE).read()) == before
    assert world.notifier.sent and world.notifier.sent[0][0] is True


@pytest.mark.parametrize("lock", ["watch_lock_path", "run_lock_path", "bot_lock_path"])
def test_busy_lock_skips_quietly_without_broker(world, lock):
    held = open(getattr(run_lock, lock)(), "a")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        code, created = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    finally:
        held.close()
    assert code == 0 and created == [] and world.notifier.sent == []


def test_locks_are_released_after_run(world):
    run(world)
    for path in (run_lock.watch_lock_path(), run_lock.run_lock_path(), run_lock.bot_lock_path()):
        handle = run_lock.acquire_nonblocking(path)
        handle.close()


def test_query_error_alerts_once_and_exits_nonzero_without_amending(world):
    world.broker.history_error = RuntimeError("api down")
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and world.broker.calls == [] and len(world.notifier.sent) == 1 and world.notifier.sent[0][0]
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)  # 같은 오류 반복 → 알림 중복 없음
    assert len(world.notifier.sent) == 1
    assert not os.path.exists(run_lock.watch_halt_path())  # 정정 전 조회 오류는 중단 표식 대상이 아님


def test_symbol_filter_and_empty_selection(world):
    code, created = run(world, env={**ENV, "REPRICE_WATCH_SYMBOLS": "TQQQ"})
    assert code == 2 and created == []


def test_notify_failure_does_not_break_the_run(world):
    def broken(message, urgent=False):
        raise RuntimeError("telegram down")
    world.notifier = broken
    code, _ = run(world)
    assert code == 0


def test_launcher_script_never_lets_dotenv_enable_apply():
    script = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "run_reprice_watch.sh")).read()
    assert "unset REPRICE_WATCH_MODE TRADE_MODE ALLOW_BROKER_ORDERS" in script
    assert 'REPRICE_WATCH_MODE="${_mode:-DRY}"' in script and 'TRADE_MODE="${_trade:-DRY}"' in script
    assert script.index(". ./.env") < script.index("unset REPRICE_WATCH_MODE")
