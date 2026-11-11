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


class FlakyNotifier(Notifier):
    """발송 실패를 흉내냅니다 — 예외(fail='raise') 또는 실제 notifier 처럼 False 반환(fail='false')."""
    def __init__(self, fail):
        super().__init__()
        self.fail = fail

    def __call__(self, message, urgent=False):
        if self.fail == "raise":
            raise OSError("telegram down")
        if self.fail == "false":
            return False
        return super().__call__(message, urgent=urgent)


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
    # load_dotenv() 가 unset 된 값을 .env 에서 되살리므로 세 값 모두 명시적으로 export 해야 함
    assert 'export ALLOW_BROKER_ORDERS="${_allow:-0}"' in script


def test_dotenv_cannot_resurrect_a_gate_that_launcher_pinned(tmp_path, monkeypatch):
    """런처가 export 한 값은 python-dotenv(override=False)가 .env 로 바꾸지 못함 — 런처 설계의 전제."""
    from dotenv import load_dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("ALLOW_BROKER_ORDERS=1\nTRADE_MODE=LIVE\nREPRICE_WATCH_MODE=APPLY\n")
    for key, value in {"ALLOW_BROKER_ORDERS": "0", "TRADE_MODE": "DRY", "REPRICE_WATCH_MODE": "DRY"}.items():
        monkeypatch.setenv(key, value)
    load_dotenv(env_file)
    assert (os.environ["ALLOW_BROKER_ORDERS"], os.environ["TRADE_MODE"], os.environ["REPRICE_WATCH_MODE"]) == ("0", "DRY", "DRY")


# ── 2차 리뷰 반영: halt 우선, 복구 횟수 반영, 실행당 시도 제한, 손상 기록 ──────────

def _break_record_saves(monkeypatch):
    """감시 기록(.watch_state.json) 저장만 실패시킵니다 (halt 표식 등 다른 파일은 정상)."""
    real = mw.save_record
    switch = {"broken": True}

    def flaky(path, record):
        if switch["broken"] and path.endswith(".watch_state.json"):
            raise OSError("disk full")
        return real(path, record)
    monkeypatch.setattr(mw, "save_record", flaky)
    return switch


def test_halt_marker_is_written_even_if_record_save_fails(world, monkeypatch):
    world.broker.error = OrderError("timeout")           # → UNCERTAIN
    switch = _break_record_saves(monkeypatch)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and json.load(open(run_lock.watch_halt_path()))["result"] == "UNCERTAIN"
    switch["broken"] = False
    # 다음 실행은 사람 확인 없이 journal 을 자동 복구하지 않고 중단 상태를 유지
    world.broker.error = None
    code, created = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and created == [] and len(world.broker.calls) == 1
    assert len(rp.read_journal(run_lock.journal_path())) == 1


def test_record_save_failure_after_successful_amend_stops_further_amends(world, monkeypatch):
    _break_record_saves(monkeypatch)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and len(world.broker.calls) == 1
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "RECORD_SAVE_FAILED"
    assert NEW in st.load_state("SOXL")["orders_meta"]        # 정정 자체와 메타 연결은 완료된 상태


def test_recovered_journal_counts_toward_daily_amends_and_last_attempt(world):
    earlier = (NOW - timedelta(minutes=45)).isoformat()
    mw.save_record(run_lock.watch_state_path(), {"session": ET_TODAY, "notified": {},
                   "symbols": {"SOXL": {"amends": 2, "attempts": 2, "last_attempt_utc": (NOW - timedelta(hours=2)).isoformat()}}})
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7,
                                                "target_price": 73.69, "original_price": 73.69, "at": earlier}])
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    saved = json.load(open(run_lock.watch_state_path()))["symbols"]["SOXL"]
    assert code == 0 and saved["amends"] == 3
    assert saved["last_attempt_utc"] == earlier                # 복구된 정정의 실제 시도 시각


def test_uncertain_records_attempt_but_not_amend_and_requires_human_resolution(world):
    world.broker.error = OrderError("timeout")
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    info = json.load(open(run_lock.watch_state_path()))["symbols"]["SOXL"]
    assert info["attempts"] == 1 and info["amends"] == 0                  # 접수 여부 불명 — 정정으로 세지 않음
    os.remove(run_lock.watch_halt_path())                                  # halt 만 지워서는 재개되지 않음
    world.broker.error = None
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and OLD in st.load_state("SOXL")["orders_meta"]       # 자동 복구 없음
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "JOURNAL_REVIEW_REQUIRED"


def test_recovery_with_rejected_only_row_is_not_counted_as_amend(world):
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, status="거부", rctf="1", orgl="100", price=73.69)]
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7,
                                                "target_price": 73.69, "at": NOW.isoformat()}])
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    # 거부만 확인돼도 원주문 생존 불명 → 자동 정리·횟수 반영 없이 표식을 남기고 중단 (#12 원칙)
    assert code == 1 and json.load(open(run_lock.watch_halt_path()))["result"] == "JOURNAL_UNRESOLVED"
    assert json.load(open(run_lock.watch_state_path()))["symbols"].get("SOXL", {}).get("amends", 0) == 0
    kept = rp.read_journal(run_lock.journal_path())
    assert len(kept) == 1 and kept[0]["review_required"] == "REJECTED_LATE"


class MultiBroker(FakeBroker):
    """정정 후보가 여러 건인 상황 — 호출마다 새 주문번호를 발급합니다."""
    OLD2 = OLD.replace("100", "101")

    def __init__(self):
        super().__init__()
        self.history = [_row(), _row(odno=self.OLD2, price=60.0)]  # 두 번째는 가격 차이가 더 큼

    def modify_order(self, odno, symbol, exchange, side, qty, price, order_type):
        self.calls.append((odno, qty, price, order_type))
        new = NEW if len(self.calls) == 1 else NEW.replace("200", "201")
        for index, row in enumerate(self.history):
            if row["odno"] == odno:
                self.history[index] = _row(odno=odno, status="정정됨", price=row["_order_price"])
        self.history.append(_row(odno=new, rctf="1", orgl=odno.split(":")[-1].lstrip("0"), price=price))
        return new


def _two_order_world():
    state = _state()
    state["orders_meta"][MultiBroker.OLD2] = deepcopy(state["orders_meta"][OLD])
    state["orders_meta"][MultiBroker.OLD2]["order_price"] = 60.0
    st.save_state("SOXL", state)
    return SimpleNamespace(broker=MultiBroker(), notifier=Notifier())


def test_only_one_attempt_per_run_by_default_largest_gap_first():
    world = _two_order_world()
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert [c[0] for c in world.broker.calls] == [MultiBroker.OLD2]     # 가격 차이가 큰 주문 먼저, 1건만
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)                        # 바로 다음 실행은 최소 간격으로 보류
    assert len(world.broker.calls) == 1


def test_max_per_run_can_be_raised_explicitly():
    world = _two_order_world()
    run(world, env={**APPLY_ENV, "REPRICE_WATCH_MAX_PER_RUN": "2"}, config=CONFIG_LIVE)
    assert len(world.broker.calls) == 2


def test_corrupt_record_holds_apply_preserves_file_and_does_not_spam(world):
    open(run_lock.watch_state_path(), "w").write("{garbage")
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and world.broker.calls == []
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "RECORD_CORRUPT"
    moved = [f for f in os.listdir(os.path.dirname(run_lock.watch_state_path())) if ".watch_state.json.corrupt-" in f]
    assert len(moved) == 1 and open(os.path.join(os.path.dirname(run_lock.watch_state_path()), moved[0])).read() == "{garbage"
    first = len(world.notifier.sent)
    assert first == 1 and world.notifier.sent[0][0] is True
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)                        # 중단 상태 유지, 알림 반복 없음
    assert len(world.notifier.sent) == first and world.broker.calls == []


def test_corrupt_record_does_not_block_dry(world):
    open(run_lock.watch_state_path(), "w").write("{garbage")
    code, _ = run(world, env=ENV, config=CONFIG_DRY)
    assert code == 0 and world.broker.calls == []      # DRY 자체는 계속 동작
    assert any(".corrupt-" in f for f in os.listdir(os.path.dirname(run_lock.watch_state_path())))


@pytest.mark.parametrize("value", ["0", "-1", "abc", "inf"])
def test_max_per_run_validation(value):
    with pytest.raises(mw.WatchConfigError):
        mw.load_settings({"REPRICE_WATCH_MAX_PER_RUN": value})


# ── 3차 리뷰 반영: halt 저장 실패 대비, 복구 중 재종료, DRY 를 거친 손상 기록 ──────

def _recoverable_journal_world(world, at_minutes_ago=45):
    earlier = (NOW - timedelta(minutes=at_minutes_ago)).isoformat()
    mw.save_record(run_lock.watch_state_path(), {"session": ET_TODAY, "notified": {},
                   "symbols": {"SOXL": {"amends": 2, "attempts": 2, "last_attempt_utc": (NOW - timedelta(hours=2)).isoformat()}}})
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69,
                                                "original_price": 73.69, "at": earlier}])


def _amends():
    return json.load(open(run_lock.watch_state_path()))["symbols"]["SOXL"]["amends"]


def test_uncertain_with_unwritable_halt_marker_still_blocks_next_apply_via_journal(world, monkeypatch):
    world.broker.error = OrderError("timeout")
    real_write_halt = mw.write_halt

    def broken(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(mw, "write_halt", broken)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and not os.path.exists(run_lock.watch_halt_path())
    entries = rp.read_journal(run_lock.journal_path())
    assert len(entries) == 1 and entries[0]["review_required"] == "UNCERTAIN"       # 지속적 근거
    assert any("저장 실패" in m for _u, m in world.notifier.sent)                     # 표식 저장 실패를 사람에게 알림
    # 다음 실행(이번엔 halt 쓰기 가능): 이력에 정정이 보여도 자동 복구하지 않고 중단 표식을 다시 만든다
    monkeypatch.setattr(mw, "write_halt", real_write_halt)
    world.broker.error = None
    world.broker.history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and len(world.broker.calls) == 1
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "JOURNAL_REVIEW_REQUIRED"
    assert OLD in st.load_state("SOXL")["orders_meta"] and len(rp.read_journal(run_lock.journal_path())) == 1


def test_review_marked_journal_can_still_be_recovered_by_the_manual_tool(world):
    """사람이 확인한 뒤에는 수동 도구(--apply --confirm)로 복구할 수 있어야 함 — 표식이 복구를 영구 차단하지 않음."""
    entry = {"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69, "review_required": "UNCERTAIN"}
    left, _ = rp.resolve_journal([entry], [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)], _state())
    assert left == []


def test_crash_after_state_save_before_journal_removal_counts_amend_exactly_once(world, monkeypatch):
    _recoverable_journal_world(world)
    real_write = rp.write_journal

    def crash(path, entries):
        raise OSError("killed")
    monkeypatch.setattr(rp, "write_journal", crash)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)           # 복구 도중 종료 (state 는 이미 저장됨)
    assert code == 1 and NEW in st.load_state("SOXL")["orders_meta"] and OLD not in st.load_state("SOXL")["orders_meta"]
    assert len(rp.read_journal(run_lock.journal_path())) == 1          # 근거(journal)는 아직 남아 있음
    assert _amends() == 3                                              # 횟수는 이미 기록됨
    monkeypatch.setattr(rp, "write_journal", real_write)
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)                      # 재시작: 구 주문번호가 이미 없어도 안전
    assert rp.read_journal(run_lock.journal_path()) == [] and _amends() == 3   # 이중 가산 없음


def test_crash_after_record_before_state_save_counts_amend_exactly_once(world, monkeypatch):
    _recoverable_journal_world(world)
    real_save = st.save_state
    calls = {"n": 0}

    def crash_once(symbol, state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("killed")
        return real_save(symbol, state)
    monkeypatch.setattr(st, "save_state", crash_once)
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert OLD in st.load_state("SOXL")["orders_meta"] and _amends() == 3   # 횟수만 먼저 기록, state 는 그대로
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert NEW in st.load_state("SOXL")["orders_meta"] and rp.read_journal(run_lock.journal_path()) == []
    assert _amends() == 3


def test_record_save_failure_during_recovery_leaves_state_and_journal_untouched(world, monkeypatch):
    _recoverable_journal_world(world)
    _break_record_saves(monkeypatch)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and json.load(open(run_lock.watch_halt_path()))["result"] == "RECORD_SAVE_FAILED"
    assert OLD in st.load_state("SOXL")["orders_meta"] and len(rp.read_journal(run_lock.journal_path())) == 1


def test_dry_run_cannot_launder_a_corrupt_record_for_a_later_apply(world):
    open(run_lock.watch_state_path(), "w").write("{garbage")
    code, _ = run(world, env=ENV, config=CONFIG_DRY)                   # DRY 가 먼저 손상 파일을 만남
    assert code == 0
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "RECORD_CORRUPT"
    assert any(".corrupt-" in f for f in os.listdir(os.path.dirname(run_lock.watch_state_path())))
    code, created = run(world, env=APPLY_ENV, config=CONFIG_LIVE)      # 이후 APPLY: 새 기록이 있어도 보류
    assert code == 1 and created == [] and world.broker.calls == []
    # 사람이 확인하고 중단 표식을 지우면 재개
    os.remove(run_lock.watch_halt_path())
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert len(world.broker.calls) == 1


# ── 4차 리뷰 반영: REJECTED_LATE 근거 보존, 정상 성공 직후 종료 시 횟수 보존 ──────

class _Killed(BaseException):
    """프로세스가 강제 종료된 상황을 흉내냅니다 (Exception 이 아니라 except 로 잡히지 않음)."""


def test_rejected_late_with_unwritable_halt_keeps_journal_marker_and_blocks_other_candidates(world, monkeypatch):
    world.broker.new_row_rejected = True
    real_write_halt = mw.write_halt
    monkeypatch.setattr(mw, "write_halt", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and not os.path.exists(run_lock.watch_halt_path())
    entries = rp.read_journal(run_lock.journal_path())
    assert len(entries) == 1 and entries[0]["review_required"] == "REJECTED_LATE"      # 지속 근거가 남음
    assert OLD in st.load_state("SOXL")["orders_meta"]                                  # 메타는 이동하지 않음
    # 봇 정산·주문도 막는다: 어댑터가 원주문을 '정정됨·잔량 0'으로 보여 주므로 살아 있는 원주문이 취소에서 빠져
    # 신규 주문과 중복될 수 있다 (정산 중복 위험이 없어도 신규 주문 단계의 위험이 남음)
    assert "SOXL" in rp.unresolved_symbols()
    # 다음 APPLY: 다른 후보를 정정하지 않고 중단 표식을 다시 만든다
    monkeypatch.setattr(mw, "write_halt", real_write_halt)
    world.broker.new_row_rejected = False
    calls_before = len(world.broker.calls)
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and len(world.broker.calls) == calls_before
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "JOURNAL_REVIEW_REQUIRED"


def test_manual_tool_clears_rejected_late_marker(world):
    entry = {"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69, "review_required": "REJECTED_LATE"}
    history = [_row(status="정정됨"), _row(odno=NEW, status="거부", rctf="1", orgl="100", price=73.69)]
    left, messages = rp.resolve_journal([entry], history, _state(), original_checked=True)
    assert left == [] and any("거부 확인" in m for m in messages)
    held, _ = rp.resolve_journal([dict(entry)], history, _state())            # 확인 없이는 정리하지 않음
    assert len(held) == 1


@pytest.mark.parametrize("marker", ["UNCERTAIN", "LINK_FAILED", "REJECTED_LATE"])
def test_every_review_marker_blocks_the_bot_for_that_symbol(world, marker):
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69,
                                                "review_required": marker}])
    import trading_bot
    assert "SOXL" in rp.unresolved_symbols() and trading_bot._pending_amend_journal("SOXL")
    assert not trading_bot._pending_amend_journal("TQQQ")


def test_rejected_late_blocks_bot_new_orders_until_manual_tool_clears_it(world):
    """원주문이 살아 있어도 이력은 '정정됨·잔량 0'으로 보이므로(봇 취소가 건너뜀) 해소 전까지 신규 주문을 차단."""
    world.broker.new_row_rejected = True
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    import trading_bot
    assert trading_bot._pending_amend_journal("SOXL")                       # 봇 차단
    history = world.broker.get_order_history()
    left, messages = rp.resolve_journal(rp.read_journal(run_lock.journal_path()), history, st.load_state("SOXL"),
                                      original_checked=True)
    assert left == []                                                        # 수동 도구가 정리할 수 있음
    rp.write_journal(run_lock.journal_path(), left)
    assert not trading_bot._pending_amend_journal("SOXL")                   # 정리 후 봇 재개


def test_normal_success_keeps_recovery_evidence_until_count_is_recorded(world, monkeypatch):
    """정정 성공(state 연결 완료) 직후, 감시 기록 저장 전에 프로세스가 죽어도 횟수가 복구된다."""
    mw.save_record(run_lock.watch_state_path(), {"session": ET_TODAY, "notified": {}, "symbols": {
        "SOXL": {"amends": 2, "attempts": 2, "last_attempt_utc": (NOW - timedelta(hours=2)).isoformat()}}})
    real_save = mw.save_record

    def killed(path, record):
        if path.endswith(".watch_state.json"):
            raise _Killed()
        return real_save(path, record)
    monkeypatch.setattr(mw, "save_record", killed)
    with pytest.raises(_Killed):
        run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    monkeypatch.setattr(mw, "save_record", real_save)
    # 정정과 state 연결은 끝났지만 횟수는 아직 기록 전 — 복구 근거(journal)가 남아 있어야 함
    assert NEW in st.load_state("SOXL")["orders_meta"] and len(rp.read_journal(run_lock.journal_path())) == 1
    assert _amends() == 2
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)                       # 재시작: 복구가 횟수를 센다
    assert _amends() == 3 and rp.read_journal(run_lock.journal_path()) == []
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert _amends() == 3


def test_crash_after_count_recorded_before_journal_removal_does_not_double_count(world, monkeypatch):
    mw.save_record(run_lock.watch_state_path(), {"session": ET_TODAY, "notified": {}, "symbols": {
        "SOXL": {"amends": 2, "attempts": 2, "last_attempt_utc": (NOW - timedelta(hours=2)).isoformat()}}})
    real_drop = rp.drop_journal_entry
    monkeypatch.setattr(rp, "drop_journal_entry", lambda *a, **k: (_ for _ in ()).throw(_Killed()))
    with pytest.raises(_Killed):
        run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert _amends() == 3 and len(rp.read_journal(run_lock.journal_path())) == 1   # 기록은 끝, 근거는 남음
    monkeypatch.setattr(rp, "drop_journal_entry", real_drop)
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)                                  # 재시작: 같은 id 라 다시 세지 않음
    assert _amends() == 3 and rp.read_journal(run_lock.journal_path()) == []


def test_normal_success_path_still_ends_with_clean_journal_and_single_count(world):
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert rp.read_journal(run_lock.journal_path()) == [] and _amends() == 1
    record = json.load(open(run_lock.watch_state_path()))
    assert len(record["recovered_ids"]) == 1


def test_manual_apply_amend_default_still_cleans_up_immediately(tmp_path):
    """수동 도구 경로(기본값)는 기존대로 즉시 정리한다 — 감시기만 정리를 미룬다."""
    from test_meritz_reprice import FakeBroker as ManualBroker
    state = _state()
    item = rp.plan_order(OLD, state["orders_meta"][OLD], _row(), 65.0, {}, ET_TODAY)
    journal = str(tmp_path / "j.json")
    result = rp.apply_amend(ManualBroker(), "SOXL", "NY", item, state, journal, lambda s, st_: None,
                            lambda: [_row(odno=NEW, rctf="1", orgl="100", price=73.69)], verify_wait=0)
    assert result == "AMENDED" and rp.read_journal(journal) == []


@pytest.mark.parametrize("fail", ["raise", "false"])
def test_dry_plan_alert_is_retried_after_delivery_failure(world, fail):
    """발송에 실패한 알림은 중복 방지 기록에 남지 않아, 복구된 뒤 다음 실행에서 다시 보낸다."""
    world.notifier = FlakyNotifier(fail)
    run(world)
    record = json.load(open(run_lock.watch_state_path()))
    assert not any(k.startswith("plan:") for k in record["notified"])
    world.notifier.fail = None
    run(world)
    assert len(world.notifier.sent) == 1 and "DRY" in world.notifier.sent[0][1]
    run(world)
    assert len(world.notifier.sent) == 1                                     # 성공 뒤에는 다시 중복 방지


def test_halt_alert_is_retried_when_first_delivery_fails(world):
    """halt 알림이 실패하면 halt_reminder 를 등록하지 않아, 다음 실행의 중단 상태 알림이 다시 시도된다."""
    world.broker.error = OrderError("timeout")
    world.notifier = FlakyNotifier("false")
    run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert os.path.exists(run_lock.watch_halt_path())
    record = json.load(open(run_lock.watch_state_path()))
    assert "halt_reminder" not in record["notified"]
    world.notifier.fail = None
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 1 and any(u and "중단" in m for u, m in world.notifier.sent)


def test_production_notify_returns_notifier_result(monkeypatch):
    """main() 이 쓰는 실제 래퍼가 notifier.notify 의 성공/실패를 버리지 않는다 (재시도의 전제)."""
    import notifier
    for result in (True, False):
        monkeypatch.setattr(notifier, "notify", lambda message, urgent=False, telegram_message=None, r=result: r)
        assert watch.production_notify("m", urgent=True) is result


def test_main_wires_production_notify_and_failed_send_is_not_recorded(world, monkeypatch):
    """main() 경로 전체: 실제 래퍼 → notifier.notify 가 False 면 중복 방지 기록이 남지 않는다."""
    import notifier
    import config as real_config
    captured = {}

    def fake_run_once(env, now, config, factory, notify_fn, calendar):
        captured["notify_fn"] = notify_fn
        return 0

    real_run_once = watch.run_once
    monkeypatch.setattr(watch, "run_once", fake_run_once)
    watch.main()
    assert captured["notify_fn"] is watch.production_notify
    monkeypatch.setattr(watch, "run_once", real_run_once)
    monkeypatch.setattr(notifier, "notify", lambda message, urgent=False, telegram_message=None: False)
    world.notifier = watch.production_notify
    run(world)
    record = json.load(open(run_lock.watch_state_path()))
    assert not any(k.startswith("plan:") for k in record["notified"])
