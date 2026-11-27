"""SETTLE → PREVIEW → APPROVE → EXECUTE → VERIFY **연결** E2E (Mock 브로커 전용).

단계별 계약은 `tests/test_pipeline_phases.py`, 리버스 종료 시나리오는
`tests/test_reverse_exit_pipeline.py` 가 이미 촘촘히 덮습니다. 이 파일은 그 사이의
**연결 구간**만 보강합니다 — 하나의 state/승인 파일이 단계 경계를 넘어갈 때
T·net_invested·evidence·plan_hash 가 일관적인지, 그리고 실패 경로가 주문 0건으로
끝나는지(fail-closed)를 한 흐름에서 확인합니다.

- 네트워크/텔레그램/실주문/.env 자격증명 없음: 브로커는 MagicMock, `notify`/state I/O 는 mock.
- `conftest.py` 의 소켓·requests 차단과 `.state.json` 격리가 그대로 적용됩니다.
- 각 테스트는 `tmp_path` 로 chdir 하고 승인/계획/정산 근거를 그 안에만 남깁니다.
"""
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
for _p in (_repo_root, _src_path):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pipeline
import pipeline_execute
import strategy
import trading_bot
from broker.base import (
    OrderResult,
    ReadOnlyBroker,
    StockPrice,
    StockQuotation,
)
from state import canonical_state_hash

# 단계별 테스트의 헬퍼를 **그대로** 재사용합니다 (중복 정의로 계약이 갈라지지 않게).
from test_pipeline_phases import (
    _FixedDateTime,
    _approve,
    _base_state,
    _fixed_now,
    _make_broker as _phase_broker,
    _make_plan,
    _symbol_config,
    _write_evidence,
)


# ── 공통 헬퍼 ─────────────────────────────────────────────────────


def _e2e_broker(balance_qty=0, history=None, last=50.0):
    """단계 테스트의 `_make_broker` + 전략/실행이 실제로 부르는 조회 스텁.

    전략(리버스 종료 판정)과 EXECUTE 재계산은 시세·잔고·일봉을 조회하므로
    `_make_broker` 위에 최소 스텁만 얹습니다. 네트워크는 타지 않습니다.
    """
    broker = _phase_broker(balance_qty=balance_qty, history=history)
    broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=last)
    broker.get_stock_price.return_value = StockPrice(open=last, last=last)
    broker.get_daily_candles.return_value = [{"date": None, "close": last}]
    broker.place_order.side_effect = lambda *args, **kwargs: OrderResult(
        order_id="0000000001", order_time="20261026093000", is_reservation=False
    )
    return broker


def _orders_buy(price=50.0):
    return [
        {
            "comment": "테스트 매수",
            "side": "BUY",
            "quantity": 1,
            "price": price,
            "order_type": "LIMIT",
        }
    ]


@pytest.fixture
def io(monkeypatch, tmp_path):
    """`test_pipeline_phases.io` 와 같은 격리(임시 cwd·경로·state I/O·알림 mock)."""
    loaded = {}
    saved = []
    notifications = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUN_ID", "run-test")
    monkeypatch.setenv("RUN_PHASE", "EXECUTE")
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
    monkeypatch.delenv("RUN_LOCK_FD", raising=False)
    monkeypatch.setenv("APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: loaded[symbol])
    monkeypatch.setattr(
        trading_bot, "save_state", lambda symbol, state: saved.append(deepcopy(state))
    )
    monkeypatch.setattr(
        trading_bot, "notify", lambda message, urgent=False: notifications.append(message)
    )
    monkeypatch.setattr(trading_bot, "무한매수법_V4", _fake_strategy)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")
    monkeypatch.setattr(trading_bot, "BROKER", "mockbroker")
    # SETTLE 이 기록하는 evidence/승인 바인딩은 `trading_bot.BROKER*` 가 아니라
    # env 를 읽는 경로가 있으므로, 같은 값으로 맞춰 VERIFY 의 브로커 대조를 통과시킵니다.
    monkeypatch.setenv("BROKER", "mockbroker")
    monkeypatch.setenv("BROKER_MODE", "demo")
    return {
        "loaded": loaded,
        "saved": saved,
        "notifications": notifications,
        "load": lambda symbol, state: loaded.__setitem__(symbol, state),
    }


def _fake_strategy(broker, **kwargs):
    """저장/주문 없이 고정 주문 1건만 돌려주는 결정적 전략."""
    return {
        "reverse_exit": False,
        "last_price": 50.0,
        "position_qty": 0,
        "avg_price": 0.0,
        "orderable_cash": 1000.0,
        "star_point": None,
        "price_cap_notes": [],
        "orders": _orders_buy(),
    }


# ── 시나리오 A: 정상 연결 ─────────────────────────────────────────


class TestE2EHappyPath:
    def test_settle_plan_approve_execute_verify_chain(self, io, monkeypatch, tmp_path):
        # PREVIEW 계획의 세션/만료가 고정 시각(2026-10-26)과 맞도록 시계를 고정합니다.
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        io["load"]("TQQQ", deepcopy(_base_state()))
        broker = _e2e_broker()
        read_only = ReadOnlyBroker(broker)

        # 1) SETTLE: mock 이력/잔고로 실제 정산 → state 저장은 정확히 1회.
        settled = pipeline.settle_positions(read_only, _symbol_config(), now=_fixed_now())
        assert settled.halt_reason is None
        assert len(io["saved"]) == 1, "SETTLE 은 LIVE 에서 1회만 저장해야 합니다"
        assert settled.T == pytest.approx(0.0)
        assert settled.state["net_invested"] == pytest.approx(0.0)
        evidence_path = pipeline.evidence_file_for_run("TQQQ", "run-test")
        assert evidence_path, "커밋된 SETTLE 근거가 있어야 합니다"

        # 2) PREVIEW: 실제 계획 생성 (주문/저장 없음).
        plan = pipeline.build_order_plan(settled, read_only)
        assert plan.orders and plan.plan_id and plan.plan_hash
        assert plan.state_hash == canonical_state_hash(settled.state)

        # 3) APPROVE: 실제 승인 파일. state 저장/주문은 없습니다.
        approval_path = pipeline.approve_order_plan(plan, approvals_dir=str(tmp_path))
        approval = pipeline.load_approval(str(approval_path))
        assert approval["plan_hash"] == plan.plan_hash
        assert approval["state_hash"] == plan.state_hash
        assert approval["settlement_run_id"] == "run-test"
        assert approval["evidence_id"] == "run-test"
        assert approval["consumed"] is False
        assert len(io["saved"]) == 1, "APPROVE 는 state 를 저장하지 않습니다"
        assert broker.place_order.call_count == 0

        # 4) EXECUTE: 실제 실행 (frozen now → 프리장 대기 생략).
        io["load"]("TQQQ", deepcopy(settled.state))
        result = pipeline_execute.execute_approved_plan(
            broker,
            str(approval_path),
            now=_fixed_now(),
            state_override=deepcopy(settled.state),
        )
        assert result["outcome"] == "executed"
        assert result["failed"] == 0
        assert broker.place_order.call_count == 1
        assert len(result["transmitted_orders"]) == len(approval["orders"])

        executed_state = deepcopy(io["saved"][-1])
        assert executed_state["pending_order_intent"] is None
        assert executed_state["pending_order_batch"] is None
        assert executed_state["T"] == pytest.approx(settled.T), (
            "EXECUTE 는 T 를 바꾸지 않습니다 (체결 반영은 SETTLE 의 책임)"
        )
        assert executed_state["net_invested"] == pytest.approx(
            settled.state["net_invested"]
        )
        odno = next(iter(executed_state["orders_meta"]))
        meta = executed_state["orders_meta"][odno]
        assert meta["plan_id"] == approval["plan_id"]
        assert meta["plan_order_index"] == 1
        transmitted = Path(str(approval_path) + ".transmitted.json")
        assert transmitted.exists()

        # 5) VERIFY: 소비된 승인 + 실행 기록 + 전송 계획 대조 + **실제 독립 검증**
        #    (`verify_evidence_file_with_ops`)까지 통과해야 exit 0 입니다.
        io["load"]("TQQQ", deepcopy(executed_state))
        monkeypatch.setenv("APPROVAL_FILE", str(approval_path))
        assert trading_bot._run_verify_phase() == 0

        # 6) 같은 승인 재실행: 소비된 승인 → 주문 0건 추가.
        with pytest.raises(RuntimeError, match="이미 사용"):
            pipeline_execute.execute_approved_plan(
                broker,
                str(approval_path),
                now=_fixed_now(),
                state_override=deepcopy(executed_state),
            )
        assert broker.place_order.call_count == 1, "재실행은 주문을 추가하지 않습니다"


# ── 시나리오 C: 승인 후 EXECUTE 재계산 보류 ───────────────────────


class TestE2EExecuteHold:
    def test_execute_blocks_reverse_hold_before_any_order(self, io, monkeypatch, tmp_path):
        """승인은 정상. EXECUTE 재계산에서 확정 종가 미확보(EXIT_UNCONFIRMED) → 주문 0건.

        PREVIEW 보류 집계는 `TestPreviewHoldAccounting` 이 덮습니다. 여기서는
        **승인 이후 실행 직전 재계산**이 stale 승인을 무시하고 보류하는 연결만 봅니다.
        """
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        # 리버스 2일차 진입 직전 상태 (T=19.5 > 19, 보유 100주).
        reverse_state = _base_state(
            T=19.5,
            net_invested=8000.0,
            position_qty=100,
            reverse_mode={
                "active": True,
                "day_count": 1,
                "cycle_id": "cycle-1",
                "last_planned_session": "2026-10-23",
            },
        )
        _write_evidence(tmp_path, run_id="run-test")
        plan = _make_plan(reverse_state, orders=_orders_buy(), run_id="run-test")
        approval_path = pipeline.approve_order_plan(plan, approvals_dir=str(tmp_path))
        approved = pipeline.load_approval(str(approval_path))
        assert len(approved["orders"]) == 1, "승인 시점에는 정상 주문이 존재해야 합니다"
        assert approved["consumed"] is False

        # 실행 시점: 확정 종가(일봉) 조회 실패 → 종료 판정 보류.
        monkeypatch.setattr(trading_bot, "무한매수법_V4", strategy.무한매수법_V4)
        broker = _e2e_broker(balance_qty=100)
        broker.get_daily_candles.side_effect = RuntimeError("일봉 조회 실패")
        io["load"]("TQQQ", deepcopy(reverse_state))

        with pytest.raises(RuntimeError, match=r"리버스 보류\(EXIT_UNCONFIRMED\)"):
            pipeline_execute.execute_approved_plan(
                broker,
                str(approval_path),
                now=_fixed_now(),
                state_override=deepcopy(reverse_state),
            )

        assert broker.place_order.call_count == 0, "보류는 주문 0건이어야 합니다"
        assert pipeline.load_approval(str(approval_path))["consumed"] is False, (
            "재계산 보류는 승인을 소비하면 안 됩니다"
        )
        saved_fences = [
            s for s in io["saved"]
            if s.get("pending_order_intent") or s.get("pending_order_batch")
        ]
        assert saved_fences == [], "주문 없는 보류는 fence 를 남기지 않습니다"


# ── 시나리오 D: EXECUTE 중 주문 타임아웃 → fence 유지 ─────────────


class TestE2ETimeoutFence:
    def test_timeout_keeps_fence_and_retry_cannot_duplicate(self, io, tmp_path):
        """주문 API TimeoutError 는 '미접수 확정'이 아니므로 fence 유지 + 재시도 차단.

        정산 읽기 타임아웃(`TestBrokerTimeoutDuringSettle`)과 달리, **주문 전송 중**
        타임아웃의 연결 동작(승인 소비·fence·재실행)을 확인합니다.
        """
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        broker = _e2e_broker()
        broker.place_order.side_effect = TimeoutError("주문 응답 시간 초과")

        with pytest.raises(trading_bot.SymbolScopedError, match="fence"):
            pipeline_execute.execute_approved_plan(broker, str(path), now=_fixed_now())

        assert broker.place_order.call_count == 1
        assert pipeline.load_approval(str(path))["consumed"] is True
        fenced = next(
            (s for s in io["saved"] if s.get("pending_order_intent")), None
        )
        assert fenced is not None, "불확실한 주문은 pending intent fence 를 남겨야 합니다"
        assert fenced["pending_order_batch"] is not None
        recovery = Path(str(path) + ".recovery.json")
        assert recovery.exists()
        assert '"outcome": "execution-failed"' in recovery.read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="이미 사용"):
            pipeline_execute.execute_approved_plan(broker, str(path), now=_fixed_now())
        assert broker.place_order.call_count == 1, "재시도는 중복 주문을 만들지 않습니다"


# ── 시나리오 E: 승인 후 state 드리프트 → 실행 차단 ───────────────


class TestE2EStateDrift:
    def test_state_changed_after_approval_blocks_before_orders(self, io, tmp_path):
        """승인 후 SETTLE 등으로 state(T/net_invested)가 바뀌면 실행 전에 거부.

        가격 드리프트(`TestExecuteTransmitsRebuiltPlan`)와 달리 **state 해시 자체**가
        달라진 경우로, 주문 API/승인 소비 이전에 fail-closed 인지 확인합니다.
        """
        state, _plan, path = _approve(tmp_path)
        changed = deepcopy(state)
        changed["T"] = 7.0
        changed["net_invested"] = 1234.0
        io["load"]("TQQQ", deepcopy(changed))
        broker = _e2e_broker()

        with pytest.raises(RuntimeError, match="상태 해시 불일치"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now(), state_override=deepcopy(changed)
            )

        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False


# ── 시나리오 F: 휴장일 — 정산은 실행, 주문은 정상 SKIP, 다음 세션에 실행 ──


class TestE2EHolidaySkipThenNextSession:
    def test_holiday_settles_skips_orders_and_executes_next_session(
        self, io, monkeypatch, tmp_path
    ):
        """11-26(휴장) SETTLE → PREVIEW(11-27 계획) → EXECUTE SKIP → 11-27 EXECUTE → VERIFY."""
        kst = ZoneInfo("Asia/Seoul")
        holiday = datetime(2026, 11, 26, 19, 10, tzinfo=timezone.utc)  # ET 11-26 14:10
        next_session = datetime(2026, 11, 27, 9, 30, tzinfo=timezone.utc)  # ET 04:30
        clock = [holiday]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0]

        sleep = MagicMock()
        monkeypatch.setattr(trading_bot, "datetime", Clock)
        monkeypatch.setattr(trading_bot, "get_kst_now", lambda: clock[0].astimezone(kst))
        monkeypatch.setattr(trading_bot, "is_us_dst", lambda: False)
        monkeypatch.setattr(trading_bot.time, "sleep", sleep)
        io["load"]("TQQQ", deepcopy(_base_state()))
        broker = _e2e_broker()
        broker.is_trading_day.return_value = False
        read_only = ReadOnlyBroker(broker)

        settled = pipeline.settle_positions(read_only, _symbol_config())
        assert settled.halt_reason is None
        assert (settled.settled_session, settled.session) == ("2026-11-25", "2026-11-27")
        assert len(io["saved"]) == 1
        io["load"]("TQQQ", deepcopy(settled.state))

        plan = pipeline.build_order_plan(settled, read_only)
        assert plan.session == "2026-11-27"
        approval_path = pipeline.approve_order_plan(plan, approvals_dir=str(tmp_path))

        skipped = pipeline_execute.execute_approved_plan(broker, str(approval_path))
        assert skipped["skipped"] and skipped["failed"] == 0
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(approval_path))["consumed"] is False
        assert len(io["saved"]) == 1, "SKIP 은 state 를 저장하지 않습니다"
        sleep.assert_not_called()

        # 다음 세션: 휴장일 승인은 TTL 이 지났을 수 있으므로 새 계획으로 실행합니다.
        clock[0] = next_session
        broker.is_trading_day.return_value = True
        new_plan = pipeline.build_order_plan(
            pipeline.settled_from_state(_symbol_config(), state=settled.state), read_only
        )
        new_path = pipeline.approve_order_plan(new_plan, approvals_dir=str(tmp_path))
        result = pipeline_execute.execute_approved_plan(broker, str(new_path))
        assert result["outcome"] == "executed"
        assert broker.place_order.call_count == len(new_plan.orders)
        sleep.assert_not_called()

        io["load"]("TQQQ", deepcopy(io["saved"][-1]))
        monkeypatch.setenv("APPROVAL_FILE", str(new_path))
        assert trading_bot._run_verify_phase() == 0
