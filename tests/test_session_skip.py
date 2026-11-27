"""주문 대상 세션 판정과 휴장·마감 후 정상 SKIP (Mock 브로커 전용).

- `broker.market_utils.us_order_window` / `ops/session_gate.py`: 판정표 (DST·조기폐장·휴장·주말)
- 휴장일 SETTLE: 직전 세션의 미정산 체결은 휴장일에도 정산합니다
- EXECUTE: 대상 세션이 없으면 주문 0건·승인 미소비, VERIFY 는 "실행 안 됨"(2) 유지
- 캘린더 오류·설정 오류·브로커 휴장 불일치는 SKIP 으로 숨기지 않습니다

워크플로(캐시 저장·정산 검증 제외) 동작은 `tests/test_workflow_run_outcomes.py` 가 덮습니다.
"""
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

_repo_root = Path(__file__).resolve().parents[1]
for _p in (_repo_root, _repo_root / "src", _repo_root / "ops"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pipeline
import pipeline_execute
import session_gate
import trading_bot
from broker import market_utils
from broker.base import ReadOnlyBroker
from broker.market_utils import us_order_window

from test_pipeline_e2e import _e2e_broker
from test_pipeline_phases import (  # noqa: F401  (io 는 fixture)
    _approve,
    _FixedDateTime,
    _base_state,
    _symbol_config,
    io,
)

KST = ZoneInfo("Asia/Seoul")


def _kst(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def _utc(text):
    return _kst(text).astimezone(timezone.utc)


# ── 판정표 ───────────────────────────────────────────────────────
# (실행 시각 KST, 대상 세션, window, 대기 초, 정산 가능, 직전 마감 세션)
DECISIONS = [
    # 정상 거래일: 정규장 중 / 프리장 / 자정 무렵 대기 / 아침(다음 세션 준비)
    ("2026-10-30 04:10", "2026-10-29", "open", 0, True, "2026-10-28"),
    ("2026-10-29 18:30", "2026-10-29", "open", 0, True, "2026-10-28"),
    ("2026-10-29 14:00", "2026-10-29", "open", 3 * 3600, True, "2026-10-28"),
    ("2026-10-30 10:15", "2026-10-30", "open", 6 * 3600 + 45 * 60, True, "2026-10-29"),
    # 정규장 마감 후, 애프터아워 종료 전 → 주문·정산 모두 불가
    ("2026-10-30 05:30", "2026-10-29", "closed", 0, False, "2026-10-29"),
    # DST: 서머타임 시작 직후 / 종료 직후
    ("2026-03-09 17:30", "2026-03-09", "open", 0, True, "2026-03-06"),
    ("2026-03-10 04:10", "2026-03-09", "open", 0, True, "2026-03-06"),
    ("2026-11-03 04:10", "2026-11-02", "open", 0, True, "2026-10-30"),
    ("2026-11-02 10:15", "2026-11-02", "open", 7 * 3600 + 45 * 60, True, "2026-10-30"),
    # 휴장일(추수감사절 11-26): 아침·정기 슬롯 모두 SKIP, 직전 세션 정산은 가능
    ("2026-11-26 10:15", "2026-11-26", "closed", 0, True, "2026-11-25"),
    ("2026-11-27 04:10", "2026-11-26", "closed", 0, True, "2026-11-25"),
    ("2026-11-27 05:20", "2026-11-26", "closed", 0, True, "2026-11-25"),
    # 휴장 다음 날(조기폐장일) 준비·프리장
    ("2026-11-27 10:15", "2026-11-27", "open", 7 * 3600 + 45 * 60, True, "2026-11-25"),
    ("2026-11-27 18:30", "2026-11-27", "open", 0, True, "2026-11-25"),
    # 조기폐장(ET 13:00) 후 정기 슬롯: 주문 SKIP, 정산은 애프터아워 종료 전이라 불가
    ("2026-11-28 02:59", "2026-11-27", "open", 0, True, "2026-11-25"),
    ("2026-11-28 03:00", "2026-11-27", "closed", 0, False, "2026-11-27"),
    ("2026-11-28 04:10", "2026-11-27", "closed", 0, False, "2026-11-27"),
    ("2026-11-28 05:15", "2026-11-27", "closed", 0, False, "2026-11-27"),
    # 주말
    ("2026-11-28 11:15", "2026-11-28", "closed", 0, True, "2026-11-27"),
    ("2026-10-31 14:00", "2026-10-31", "closed", 0, True, "2026-10-30"),
    ("2026-11-02 04:10", "2026-11-01", "closed", 0, True, "2026-10-30"),
]


@pytest.mark.parametrize("at,session,window,wait,due,settled", DECISIONS)
def test_session_gate_decision_table(at, session, window, wait, due, settled, tmp_path):
    result = session_gate.evaluate(_utc(at), state_path=tmp_path / "none.json")
    assert (
        result["session"],
        result["window"],
        int(result["wait_seconds"]),
        result["settlement_due"] == "true",
        result["settled_session"],
    ) == (session, window, wait, due, settled)


def test_order_window_reason_distinguishes_holiday_and_close():
    assert "휴장" in us_order_window(_utc("2026-11-27 04:10")).reason
    assert "마감" in us_order_window(_utc("2026-11-28 04:10")).reason


def test_calendar_out_of_range_raises_instead_of_skipping():
    with pytest.raises(Exception):
        us_order_window(datetime(2100, 1, 4, 12, 0, tzinfo=timezone.utc))


def test_gate_cli_exits_nonzero_on_calendar_error(monkeypatch, capsys):
    def boom(now_utc=None):
        raise RuntimeError("calendar broken")

    monkeypatch.setattr(session_gate, "us_order_window", boom)
    assert session_gate.main() == 1
    captured = capsys.readouterr()
    assert "window=" not in captured.out
    assert "판정 실패" in captured.err


# ── state 보류 표식 (읽기 전용) ───────────────────────────────────


def test_state_hold_reports_unresolved_markers_without_writing(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(
        json.dumps(
            {
                symbol: {"net_invested_status": "valid", **record}
                for symbol, record in {
                    "TQQQ": {
                        "pending_order_intent": {"side": "BUY"},
                        "balance_mismatch": {},
                    },
                    "SOXL": {
                        "balance_mismatch": {"note": "x"},
                        "pending_order_batch": None,
                    },
                    "OLD": {"pending_order_batch": {"orders": []}},
                }.items()
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    assert session_gate.state_hold(path, "TQQQ:NAS,SOXL:AMS") == (
        "TQQQ:pending_order_intent,SOXL:balance_mismatch"
    )
    assert "OLD:pending_order_batch" in session_gate.state_hold(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "record,held",
    [
        ({"net_invested": 0.0, "net_invested_status": "unresolved"}, True),
        ({"net_invested": -1.0, "net_invested_status": "unresolved"}, True),
        ({"net_invested": 0.0}, True),  # status 누락 = unresolved
        ({"net_invested": 0.0, "net_invested_status": "??"}, True),
        ({"net_invested": 500.0, "net_invested_status": "unresolved"}, False),
        ({"net_invested": 500.0}, False),  # 양수 레거시 state 는 기존 운용 유지
        ({"net_invested": 0.0, "net_invested_status": "valid"}, False),
    ],
)
def test_net_invested_hold_matches_strategy_gate(record, held, tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(json.dumps({"TQQQ": record}), encoding="utf-8")
    assert session_gate.state_hold(path) == (
        "TQQQ:net_invested_status" if held else ""
    )


def test_state_hold_clean_and_missing_file(tmp_path):
    path = tmp_path / ".state.json"
    assert session_gate.state_hold(path) == ""
    path.write_text(json.dumps({"TQQQ": _base_state()}), encoding="utf-8")
    assert session_gate.state_hold(path) == ""


@pytest.mark.parametrize(
    "content",
    ["{broken", "[]", '{"TQQQ": null}', '{"TQQQ": [], "SOXL": {}}'],
)
def test_corrupt_state_is_an_error_not_a_skip(content, tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        session_gate.evaluate(_utc("2026-11-27 04:10"), state_path=path)


# ── 휴장일·마감 후 SETTLE ─────────────────────────────────────────


def _late_fill():
    """11-25 세션 마감에 체결된 아직 반영되지 않은 매수 (LOC 체결 형태)."""
    return {
        "odno": "N1",
        "ord_dt": "20261125",
        "ord_tmd": "040000",
        "ord_datetime_utc": "2026-11-25T19:00:00+00:00",
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "2",
        "ft_ccld_amt3": "100",
        "ft_ccld_unpr3": "50",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }


@pytest.mark.parametrize(
    "at,settled_session,execution_session",
    [
        ("2026-11-27 04:10", "2026-11-25", "2026-11-27"),  # 휴장일 정기 슬롯
        ("2026-11-28 11:15", "2026-11-27", "2026-11-30"),  # 조기폐장 후 (애프터아워 종료)
    ],
)
def test_settle_applies_unsettled_fill_on_non_order_day(
    io, at, settled_session, execution_session
):
    state = _base_state(
        T=0.0,
        net_invested=0.0,
        position_qty=0,
        orders_meta={
            "N1": {
                "side": "BUY",
                "total_qty": 2,
                "t_target": 1.0,
                "processed_filled_qty": 0,
                "processed_filled_amount": 0.0,
                "fill_marker_version": 1,
                "submitted_session": "2026-11-25",
            }
        },
    )
    io["load"]("TQQQ", deepcopy(state))
    broker = _e2e_broker(balance_qty=2, history=[_late_fill()])
    broker.is_trading_day.return_value = False

    settled = pipeline.settle_positions(
        ReadOnlyBroker(broker), _symbol_config(), now=_utc(at)
    )

    assert settled.halt_reason is None
    assert (settled.settled_session, settled.session) == (
        settled_session,
        execution_session,
    )
    assert settled.T == pytest.approx(1.0)
    assert settled.state["net_invested"] == pytest.approx(100.0)
    assert len(io["saved"]) == 1
    assert pipeline.evidence_file_for_run("TQQQ", "run-test")
    assert broker.place_order.call_count == 0


def test_settle_before_after_hours_end_still_holds(io):
    """마감 직후 SETTLE 은 기존 안전장치대로 보류(저장 없음) — SKIP 판정과 별개입니다."""
    io["load"]("TQQQ", deepcopy(_base_state()))
    settled = pipeline.settle_positions(
        ReadOnlyBroker(_e2e_broker()),
        _symbol_config(),
        now=_utc("2026-11-28 04:10"),
    )
    assert settled.halt_reason == "after_hours_not_ended"
    assert io["saved"] == []


# ── EXECUTE 정상 SKIP ────────────────────────────────────────────


@pytest.fixture
def holiday_clock(monkeypatch):
    """LIVE 시계를 2026-11-27 04:10 KST(ET 추수감사절 낮)로 고정하고 sleep 을 감시합니다."""
    sleep = MagicMock()
    monkeypatch.setattr(trading_bot, "get_kst_now", lambda: _kst("2026-11-27 04:10"))
    monkeypatch.setattr(trading_bot.time, "sleep", sleep)
    return sleep


def test_execute_skips_closed_window_without_consuming_approval(
    io, holiday_clock, monkeypatch, tmp_path
):
    marker = tmp_path / "skip-marker"
    monkeypatch.setenv("RUN_SKIP_MARKER", str(marker))
    state, _plan, path = _approve(tmp_path)
    io["load"]("TQQQ", deepcopy(state))
    broker = _e2e_broker()

    result = pipeline_execute.execute_approved_plan(broker, str(path))

    assert result["failed"] == 0 and result["executed"] == 0
    assert "휴장" in result["skipped"]
    assert result["outcome"] == "approval-skipped"
    assert broker.place_order.call_count == 0
    assert broker.get_order_history.call_count == 0
    assert pipeline.load_approval(str(path))["consumed"] is False
    assert io["saved"] == [], "SKIP 은 state 를 저장하지 않습니다"
    assert pipeline.read_approval_outcome(str(path))["outcome"] == "approval-skipped"
    assert any("SKIP" in message for message in io["notifications"])
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    holiday_clock.assert_not_called()


def test_dispatch_execute_holiday_is_success_and_verify_still_not_executed(
    io, holiday_clock, monkeypatch, tmp_path
):
    state, _plan, path = _approve(tmp_path)
    io["load"]("TQQQ", deepcopy(state))
    broker = _e2e_broker()
    broker.is_trading_day.return_value = False
    monkeypatch.setenv("APPROVAL_FILE", str(path))

    assert trading_bot._dispatch_pipeline_phase("EXECUTE", broker) == 0
    assert broker.place_order.call_count == 0

    # 기존 주문 VERIFY 정책: 실행되지 않은 승인은 "통과"가 아닙니다 (exit 2).
    # VERIFY 의 승인 세션/만료 검사는 승인 세션 시각으로 고정합니다.
    monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
    monkeypatch.setattr(
        trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
    )
    assert trading_bot._run_verify_phase() == 2


def test_dispatch_execute_holiday_still_fails_on_plan_hash_mismatch(
    io, holiday_clock, monkeypatch, tmp_path
):
    """설정 오류(검토 해시 불일치)는 휴장일에도 SKIP 으로 숨기지 않습니다."""
    state, _plan, path = _approve(tmp_path)
    io["load"]("TQQQ", deepcopy(state))
    broker = _e2e_broker()
    broker.is_trading_day.return_value = False
    monkeypatch.setenv("APPROVAL_FILE", str(path))
    monkeypatch.setenv("REVIEWED_PLAN_HASH", "0" * 64)

    assert trading_bot._dispatch_pipeline_phase("EXECUTE", broker) == 1
    assert broker.place_order.call_count == 0


def test_dispatch_execute_broker_holiday_while_window_open_still_fails(
    io, monkeypatch, tmp_path
):
    """주문 창은 열려 있는데 브로커가 휴장이라고 하면 기존대로 실패합니다."""
    _state, _plan, path = _approve(tmp_path)
    monkeypatch.setattr(trading_bot, "get_kst_now", lambda: _kst("2026-10-29 18:30"))
    broker = _e2e_broker()
    broker.is_trading_day.return_value = False
    monkeypatch.setenv("APPROVAL_FILE", str(path))

    assert trading_bot._dispatch_pipeline_phase("EXECUTE", broker) == 1
    assert broker.place_order.call_count == 0


def test_used_approval_is_rejected_even_on_holiday(
    io, holiday_clock, monkeypatch, tmp_path
):
    marker = tmp_path / "skip-marker"
    monkeypatch.setenv("RUN_SKIP_MARKER", str(marker))
    state, _plan, path = _approve(tmp_path)
    io["load"]("TQQQ", deepcopy(state))
    approval = pipeline.load_approval(str(path))
    approval.pop("_path", None)
    approval["consumed"] = True
    Path(path).write_text(json.dumps(approval), encoding="utf-8")

    with pytest.raises(RuntimeError, match="이미 사용"):
        pipeline_execute.execute_approved_plan(_e2e_broker(), str(path))
    assert not marker.exists(), "거부된 승인은 SKIP 표식을 남기지 않습니다"
    with pytest.raises(Exception):
        pipeline_execute.execute_approved_plan(
            _e2e_broker(), str(tmp_path / "missing.json")
        )
    assert not marker.exists()


def _tamper(path, change):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    change(data)
    Path(path).write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    "case,expected",
    [
        ("orders", "plan_hash 불일치"),
        ("broker", "broker 불일치"),
        ("state_hash", "상태 해시 불일치"),
        ("pending_fence", "미해결 주문"),
        ("balance_mismatch", "이력-잔고 불일치"),
        ("evidence", "SETTLE 근거"),
    ],
)
def test_invalid_approval_or_hold_is_not_skipped_on_holiday(
    io, holiday_clock, monkeypatch, tmp_path, case, expected
):
    marker = tmp_path / "skip-marker"
    monkeypatch.setenv("RUN_SKIP_MARKER", str(marker))
    # 보류 사례는 승인 지문과 일치하는 state 로 만들어, 해시가 아니라 보류 검사가
    # 막는지 확인합니다.
    holds = {
        "pending_fence": {"pending_order_intent": {"side": "BUY"}},
        "balance_mismatch": {"balance_mismatch": {"note": "requires-attention"}},
    }
    state, _plan, path = _approve(tmp_path, state=_base_state(**holds.get(case, {})))
    state = deepcopy(state)
    if case == "orders":
        _tamper(path, lambda data: data["orders"][0].update(quantity=999))
    elif case == "broker":
        monkeypatch.setattr(trading_bot, "BROKER", "otherbroker")
        monkeypatch.setenv("BROKER", "otherbroker")
    elif case == "state_hash":
        state["T"] = 7.0
    elif case == "evidence":
        for evidence in tmp_path.glob("settlement-*.json"):
            evidence.unlink()
    io["load"]("TQQQ", state)
    broker = _e2e_broker()

    with pytest.raises(RuntimeError, match=expected):
        pipeline_execute.execute_approved_plan(broker, str(path))

    assert broker.place_order.call_count == 0
    assert not marker.exists()
    assert pipeline.read_approval_outcome(str(path)).get("outcome") is None
    assert pipeline.load_approval(str(path))["consumed"] is False
    assert io["saved"] == []


def test_time_checks_are_the_only_ones_relaxed(io, tmp_path):
    state, _plan, path = _approve(tmp_path)
    approval = pipeline.load_approval(str(path))
    late = _utc("2026-11-27 04:10")
    assert pipeline.verify_approval(approval, state, now=late)
    assert pipeline.verify_approval(approval, state, now=late, check_time=False) == []


def test_calendar_error_during_execute_is_not_a_skip(io, monkeypatch, tmp_path):
    _state, _plan, path = _approve(tmp_path)
    monkeypatch.setattr(trading_bot, "get_kst_now", lambda: _kst("2026-11-27 04:10"))

    def boom(now_utc=None):
        raise RuntimeError("calendar broken")

    monkeypatch.setattr(pipeline_execute, "us_order_window", boom)
    broker = _e2e_broker()
    with pytest.raises(RuntimeError, match="calendar broken"):
        pipeline_execute.execute_approved_plan(broker, str(path))
    assert broker.place_order.call_count == 0


def test_auto_execute_skip_is_not_reported_as_executed(io, monkeypatch):
    plan = SimpleNamespace(symbol="TQQQ", orders=[{}], strategy_result={})
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_symbol_config()])
    monkeypatch.setattr(
        trading_bot, "_preview_symbol_plan", lambda *a: (plan, "approval.json")
    )
    monkeypatch.setattr(
        trading_bot,
        "_execute_auto_approved_plan",
        lambda *a: {"failed": 0, "skipped": "휴장"},
    )

    assert trading_bot._dispatch_pipeline_phase("PREVIEW", _e2e_broker()) == 0
    assert not any("실행 완료" in message for message in io["notifications"])


# ── 레거시/DRY 휴장 표식 ─────────────────────────────────────────


def test_run_skip_marker_is_written_only_when_requested(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    monkeypatch.delenv("RUN_SKIP_MARKER", raising=False)
    trading_bot._write_run_skip_marker("휴장")
    assert not marker.exists()
    monkeypatch.setenv("RUN_SKIP_MARKER", str(marker))
    trading_bot._write_run_skip_marker("휴장\n둘째 줄")
    trading_bot._write_run_skip_marker("또 하나")
    assert marker.read_text(encoding="utf-8") == "휴장 둘째 줄\n또 하나\n"


@pytest.mark.parametrize("trading_day", [False, True])
def test_legacy_main_marks_skip_only_on_holiday(monkeypatch, tmp_path, trading_day):
    marker = tmp_path / "marker"
    monkeypatch.setenv("RUN_SKIP_MARKER", str(marker))
    monkeypatch.setenv("RUN_PHASE", "")
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [])
    monkeypatch.setattr(trading_bot, "_run_mode_dispatch", lambda: False)
    monkeypatch.setattr(trading_bot, "notify", lambda *a, **k: None)
    broker = _e2e_broker()
    broker.is_trading_day.return_value = trading_day
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)

    trading_bot.main()

    assert marker.exists() is (not trading_day)


def test_pre_market_wait_rule_is_shared_by_gate_and_bot():
    now = _kst("2026-10-30 10:15")
    assert market_utils.pre_market_wait_seconds(now, True) == 6 * 3600 + 45 * 60
    assert market_utils.pre_market_wait_seconds(_kst("2026-10-29 17:00"), True) == 0
