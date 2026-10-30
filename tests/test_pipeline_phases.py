"""
파이프라인 단계 분리(SETTLE / PREVIEW / APPROVE / EXECUTE / VERIFY) 계약 테스트.

리뷰 HOLD(P0/P1) 반영 범위:
- RUN_PHASE 없는 LIVE fail-closed / 레거시 LIVE 승인 게이트
- fence·미확정 정산이 있으면 PREVIEW/APPROVE/EXECUTE 거부
- EXECUTE 재조회·계획 재계산·가격/수량 허용오차·매도<=보유
- 승인 단일 사용(파일 잠금) + 동시 claim 거부
- PREVIEW 계획 파일 → APPROVE 는 그 파일만 승인 (재생성 금지)
- 정산 실패 시 저장 금지 / evidence 검증·기록 실패 시 저장 금지
- 지연 이력 정착성 확인 / RUN_ID evidence 연결 / VERIFY 독립 검증

실제 주문/네트워크/운영 state 파일은 사용하지 않습니다.
"""
import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

import pytest

_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
for path in (_repo_root, _src_path):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import trading_bot
import pipeline
import pipeline_execute
from broker import create_broker, validate_run_phase
from broker.base import Balance, OrderResult, PurchaseAmount, ReadOnlyBroker
from state import canonical_state_hash


# ── 공통 헬퍼 ─────────────────────────────────────────────────────


def _base_state(**overrides):
    state = {
        "T": 0.0,
        "last_updated": "2026-10-20T11:14:03+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
        "position_qty": None,
        "sell_t_effects": {},
        # SETTLE 이 저장한 마지막 커밋 정산 id (P1-E 바인딩)
        "last_settlement_id": "run-test",
        "last_settlement_evidence": "",
        # 세션 분리: 정산 대상(금요일 마감) / 실행 대상(월요일 세션).
        # _fixed_now()=2026-10-26(월) 05:30 ET(프리장) 기준과 일치시킵니다.
        "settled_session": "2026-10-23",
        "execution_session": "2026-10-26",
    }
    state.update(overrides)
    return state


def _symbol_config(symbol="TQQQ"):
    return {
        "symbol": symbol,
        "exchange": "NAS",
        "splits": 20,
        "symbol_type": symbol,
        "seed": 8000.0,
        "additional_loc_levels": 3,
    }


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


def _fake_strategy(broker, **kwargs):
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


def _seed_cap_strategy(price=50.0):
    """state.net_invested 를 실제로 읽어 시드 캡을 적용하는 테스트 전략.

    `build_order_plan` 이 전략에 넘긴 state 의 net_invested 가 그대로
    주문 여부를 좌우하므로, EXECUTE 오버레이(P1)가 동작하는지 검증할 수 있습니다.
    """

    def _strategy(broker, **kwargs):
        result = _fake_strategy(broker, **kwargs)
        state = kwargs.get("state") or {}
        seed = float(kwargs.get("seed", 0.0) or 0.0)
        net_invested = float(state.get("net_invested", 0.0) or 0.0)
        remaining = max(seed - net_invested, 0.0)
        result["orderable_cash"] = remaining
        result["orders"] = _orders_buy(price) if remaining >= price else []
        return result

    return _strategy


def _deferred_fill(odno="DEF1", side="매수", qty="1", amount="100", **extra):
    row = {
        "odno": odno,
        "ord_dt": "20261026",
        "ord_tmd": "040000",
        "ord_datetime_utc": "2026-10-26T08:00:00+00:00",
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": qty,
        "ft_ccld_amt3": amount,
        "ft_ccld_unpr3": "100",
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
    }
    row.update(extra)
    return row


def _bot_order_meta(odno="DEF1", *, side="BUY", qty=1, **extra):
    """`orders_meta` 등록 항목 — 보류 체결이 **봇 주문**임을 증명합니다.

    APPROVE/EXECUTE 는 실행 세션 보류 체결을 재정산 허용 목록에 넣기 전에
    `state["orders_meta"]` 등록 여부를 확인합니다 (수동 체결 배제 — P2 리뷰).
    그래서 봇 주문을 흉내내는 테스트는 메타를 함께 넣어야 합니다.
    """
    meta = {
        "side": side,
        "total_qty": qty,
        "t_target": 0.0,
        "processed_filled_qty": 0,
    }
    meta.update(extra)
    return {odno: meta}


def _make_broker(balance_qty=0, history=None):
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = list(history or [])
    broker.get_balance.return_value = Balance(
        quantity=balance_qty,
        avg_price=50.0 if balance_qty else 0.0,
    )
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=10000.0)
    broker.exchange_code.return_value = "NASD"
    broker.supports_open_orders_query = False
    broker.close = MagicMock()
    return broker


@pytest.fixture
def io(monkeypatch, tmp_path):
    """상태 I/O / 알림 / 전략 / 산출물 경로를 격리합니다."""
    loaded = {}
    saved = []
    notifications = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUN_ID", "run-test")
    # EXECUTE 경계 게이트(LIVE+RUN_PHASE)를 통과하는 기본 실행 위상입니다.
    # 위상 거부 자체를 검증하는 테스트는 이 값을 덮어씁니다.
    monkeypatch.setenv("RUN_PHASE", "EXECUTE")
    # 전역 잠금도 테스트별로 격리합니다 (저장소 루트 .run.lock 공유 방지).
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
    monkeypatch.delenv("RUN_LOCK_FD", raising=False)
    monkeypatch.setenv("APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: loaded[symbol])
    monkeypatch.setattr(
        trading_bot,
        "save_state",
        lambda symbol, state: saved.append(deepcopy(state)),
    )
    monkeypatch.setattr(
        trading_bot, "notify", lambda message, urgent=False: notifications.append(message)
    )
    monkeypatch.setattr(trading_bot, "무한매수법_V4", _fake_strategy)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")
    monkeypatch.setattr(trading_bot, "BROKER", "mockbroker")
    return {
        "loaded": loaded,
        "saved": saved,
        "notifications": notifications,
        "load": lambda symbol, state: loaded.__setitem__(symbol, state),
    }


def _fixed_now():
    # 2026-10-26(월) 09:30 UTC = 05:30 ET (프리장, 실행 세션 당일).
    return datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
        return fixed.astimezone(tz) if tz else fixed


def _make_plan(state, orders=None, run_id="run-test", ttl_seconds=32400):
    plan = pipeline.OrderPlan(
        plan_id="",
        symbol="TQQQ",
        exchange="NAS",
        session="",
        orders=orders or _orders_buy(),
        state_hash=canonical_state_hash(state),
        plan_hash="",
        settlement_run_id=run_id,
        created_at_utc="",
        expires_at_utc="",
    )
    pipeline._prepare_plan_identity(
        plan, now=_fixed_now(), ttl_seconds=ttl_seconds
    )
    return plan


def _approve(tmp_path, state=None, orders=None, run_id="run-test"):
    state = state or _base_state()
    _write_evidence(tmp_path, run_id=run_id)
    plan = _make_plan(state, orders=orders, run_id=run_id)
    path = pipeline.approve_order_plan(plan, approvals_dir=str(tmp_path))
    return state, plan, path


def _plan_hash(approval):
    return pipeline.compute_plan_hash(
        approval["plan_id"],
        approval["symbol"],
        approval["exchange"],
        approval["session"],
        approval["state_hash"],
        approval["settlement_run_id"],
        approval["orders"],
        approval.get("state_patch"),
        approval.get("evidence_id"),
        approval.get("broker"),
        approval.get("broker_mode"),
        approval.get("account_id"),
        approval.get("created_at_utc"),
        approval.get("expires_at_utc"),
        approval.get("account_display"),
        approval.get("tolerance_policy"),
    )


def _write_evidence(
    tmp_path, symbol="TQQQ", run_id="run-test", committed=True, balance_qty=0,
    deferred_fills=None,
):
    payload = {
        "symbol": symbol,
        "run_id": run_id,
        "evidence_version": 2,
        "mode": "LIVE",
        "committed": committed,
        "balance_qty": balance_qty,
    }
    if deferred_fills is not None:
        payload["deferred_fills"] = deferred_fills
    path = tmp_path / f"settlement-{symbol}-20261024T120000Z.json"
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


# ── SETTLE ────────────────────────────────────────────────────────


class TestSettle:
    def _processed_state(self):
        buyer = {
            "odno": "B1",
            "ord_dt": "20260711",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-07-12T00:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "10",
            "ft_ccld_amt3": "500",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        state = _base_state(
            T=2.0,
            net_invested=500.0,
            position_qty=10,
            orders_meta={
                "B1": {
                    "side": "BUY",
                    "total_qty": 10,
                    "t_target": 1.0,
                    "processed_filled_qty": 10,
                    "processed_filled_amount": 500.0,
                }
            },
        )
        return state, buyer

    def test_settle_is_idempotent_and_saves_once(self, io):
        state, buyer = self._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[buyer])

        first = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert first.halt_reason is None
        assert first.T == pytest.approx(2.0)
        assert first.state["net_invested"] == pytest.approx(500.0)
        assert len(io["saved"]) == 1, "LIVE SETTLE 는 1회만 저장해야 합니다"
        assert broker.place_order.call_count == 0

        io["load"]("TQQQ", deepcopy(state))
        second = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert second.T == pytest.approx(first.T)
        assert second.state["net_invested"] == pytest.approx(first.state["net_invested"])
        assert len(io["saved"]) == 2, "재실행도 각 1회씩만 저장해야 합니다"

    def test_settle_cycle_seed_idempotent(self, io):
        state, buyer = self._processed_state()
        state["effective_seed"] = 1234.0
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[buyer])

        first = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        io["load"]("TQQQ", deepcopy(first.state))
        second = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert second.effective_seed == pytest.approx(first.effective_seed)
        assert second.state["effective_seed"] == pytest.approx(1234.0)

    def test_settle_dry_never_saves(self, io, monkeypatch):
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        io["load"]("TQQQ", _base_state())
        settled = pipeline.settle_positions(
            ReadOnlyBroker(_make_broker()), _symbol_config()
        )
        assert settled.halt_reason is None
        assert io["saved"] == [], "DRY SETTLE 는 상태를 저장하지 않아야 합니다"

    def test_settle_evidence_failure_blocks_save(self, io, monkeypatch):
        state, buyer = self._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(
            pipeline, "_verify_settlement_evidence", lambda evidence: ["강제 실패"]
        )
        with pytest.raises(trading_bot.SymbolScopedError, match="독립 검증 실패"):
            pipeline.settle_positions(
                ReadOnlyBroker(_make_broker(balance_qty=10, history=[buyer])),
                _symbol_config(),
            )
        assert io["saved"] == [], "evidence 검증 실패 시 저장하면 안 됩니다"

    def test_settle_history_not_settled_blocks_save(self, io, monkeypatch):
        state, buyer = self._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setenv("HISTORY_SETTLEDNESS_CHECK", "true")
        monkeypatch.setenv("HISTORY_SETTLEDNESS_DELAY_SECONDS", "0")
        broker = _make_broker(balance_qty=10, history=[buyer])
        broker.get_order_history.side_effect = [[buyer], []]
        settled = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert settled.halt_reason == "history_not_settled"
        assert io["saved"] == []

    def test_settle_run_id_propagates_to_evidence(self, io):
        state, buyer = self._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        settled = pipeline.settle_positions(
            ReadOnlyBroker(_make_broker(balance_qty=10, history=[buyer])),
            _symbol_config(),
        )
        assert settled.settlement_run_id == "run-test"
        assert settled.evidence_id == "run-test"
        assert settled.evidence_path
        evidence = Path(settled.evidence_path).read_text(encoding="utf-8")
        assert '"run_id": "run-test"' in evidence


# ── PREVIEW / ReadOnlyBroker ──────────────────────────────────────


class TestPreviewAndReadOnly:
    def test_preview_no_save_no_order_and_writes_plan(self, io):
        io["load"]("TQQQ", _base_state())
        _write_evidence(Path("."))
        broker = _make_broker()
        settled = trading_bot._settled_from_state(_symbol_config())
        plan = pipeline.build_order_plan(settled, ReadOnlyBroker(broker))
        path = pipeline.write_order_plan(plan)

        assert len(plan.orders) == 1
        assert Path(path).exists()
        assert io["saved"] == []
        assert broker.place_order.call_count == 0

    def test_preview_uses_reinvested_effective_seed(self, io, monkeypatch):
        seen = {}

        def _capture(broker, **kwargs):
            seen.update(kwargs)
            return {
                "reverse_exit": False,
                "last_price": 50.0,
                "position_qty": 0,
                "avg_price": 0.0,
                "orderable_cash": 1000.0,
                "star_point": None,
                "price_cap_notes": [],
                "orders": [],
            }

        monkeypatch.setattr(trading_bot, "무한매수법_V4", _capture)
        io["load"]("TQQQ", _base_state(effective_seed=1234.0))
        _write_evidence(Path("."))
        pipeline.build_order_plan(
            trading_bot._settled_from_state(_symbol_config()),
            ReadOnlyBroker(_make_broker()),
        )
        assert seen["seed"] == pytest.approx(1234.0)

    def test_readonly_broker_blocks_all_order_apis_and_hides_real(self):
        broker = _make_broker()
        readonly = ReadOnlyBroker(broker)
        with pytest.raises(RuntimeError, match="주문"):
            readonly.place_order("TQQQ", "NASD", "BUY", 1, 50.0, "LIMIT")
        with pytest.raises(RuntimeError, match="정정"):
            readonly.modify_order("OD1")
        with pytest.raises(RuntimeError, match="취소"):
            readonly.cancel_order("OD1")
        assert broker.place_order.call_count == 0
        assert broker.cancel_order.call_count == 0
        assert not hasattr(readonly, "_real"), "내부 브로커를 _real 로 노출하면 안 됩니다"

    def test_pipeline_module_has_no_order_api_strings(self):
        source = (_src_path / "pipeline.py").read_text(encoding="utf-8")
        for forbidden in ("place_order", "modify_order", "cancel_order"):
            assert forbidden not in source
        execute_source = (_src_path / "pipeline_execute.py").read_text(encoding="utf-8")
        assert "place_order" in execute_source
        assert "_cancel_open_bot_orders" in execute_source


# ── RUN_PHASE 조합 검증 ───────────────────────────────────────────


class TestPhaseMatrix:
    @pytest.mark.parametrize(
        "phase,mode",
        [
            ("SETTLE", "DRY"),
            ("SETTLE", "LIVE"),
            ("PREVIEW", "DRY"),
            ("PREVIEW", "LIVE"),
            ("APPROVE", "LIVE"),
            ("EXECUTE", "LIVE"),
            ("VERIFY", "DRY"),
            ("VERIFY", "LIVE"),
            ("", "DRY"),
        ],
    )
    def test_valid_combinations(self, phase, mode):
        assert validate_run_phase(phase, mode) == phase

    @pytest.mark.parametrize(
        "phase,mode",
        [
            ("", "LIVE"),
            ("APPROVE", "DRY"),
            ("EXECUTE", "DRY"),
            ("APPROVE", "REPAIR"),
            ("EXECUTE", "REPAIR"),
            ("BOGUS", "LIVE"),
        ],
    )
    def test_invalid_combinations_exit_before_broker(self, phase, mode):
        with pytest.raises(SystemExit) as exc_info:
            validate_run_phase(phase, mode)
        assert exc_info.value.code == 1

    def test_create_broker_blocks_live_without_phase(self, monkeypatch):
        monkeypatch.delenv("RUN_PHASE", raising=False)
        monkeypatch.setenv("TRADE_MODE", "LIVE")
        with pytest.raises(SystemExit) as exc_info:
            create_broker()
        assert exc_info.value.code == 1


# ── 승인 파일 / 계획 파일 ─────────────────────────────────────────


class TestApprovalFile:
    def test_approval_roundtrip_and_schema(self, tmp_path):
        state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        assert approval["schema_version"] == pipeline.APPROVAL_SCHEMA_VERSION
        assert approval["plan_id"] == plan.plan_id
        # 세션은 "실행 대상 세션"입니다. _fixed_now()=2026-10-26(월) 05:30 ET 의
        # 직전 마감 세션은 2026-10-23(금), 실행 대상은 2026-10-26(월) 입니다.
        assert approval["session"] == "2026-10-26"
        assert approval["state_hash"] == canonical_state_hash(state)
        assert approval["settlement_run_id"] == "run-test"
        assert approval["evidence_id"] == "run-test"
        assert approval["broker"] == "mockbroker" or approval["broker"]
        assert approval["plan_hash"] == _plan_hash(approval)
        assert approval["consumed"] is False
        assert pipeline.verify_approval(approval, state, now=_fixed_now()) == []

    def test_short_ttl_rejected(self):
        with pytest.raises(ValueError, match="TTL"):
            _make_plan(_base_state(), ttl_seconds=3600)

    def test_missing_approval_rejected(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            pipeline.load_approval("does-not-exist", approvals_dir=str(tmp_path))

    def test_expired_approval_rejected(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["expires_at_utc"] = (_fixed_now() - timedelta(hours=1)).isoformat()
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("만료" in p for p in problems), problems

    def test_state_changed_rejected(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        changed = deepcopy(state)
        changed["T"] = 7.0
        problems = pipeline.verify_approval(approval, changed, now=_fixed_now())
        assert any("상태 해시 불일치" in p for p in problems), problems

    def test_tampered_plan_hash_rejected(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["orders"][0]["quantity"] = 999
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("plan_hash" in p for p in problems), problems

    def test_single_use_claim_rejects_reuse(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        pipeline.claim_approval(approval, now=_fixed_now())
        reloaded = pipeline.load_approval(str(path))
        assert reloaded["consumed"] is True
        problems = pipeline.verify_approval(reloaded, state, now=_fixed_now())
        assert any("consumed" in p for p in problems), problems
        with pytest.raises(RuntimeError, match="이미 사용된"):
            pipeline.claim_approval(reloaded, now=_fixed_now())

    def test_concurrent_double_claim_only_one_wins(self, tmp_path):
        _state, _plan, path = _approve(tmp_path)
        results = []
        barrier = threading.Barrier(2)

        def _claim():
            approval = pipeline.load_approval(str(path))
            barrier.wait()
            try:
                pipeline.claim_approval(approval, now=_fixed_now())
                results.append("ok")
            except Exception as error:
                results.append(type(error).__name__)

        threads = [threading.Thread(target=_claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(results) == ["RuntimeError", "ok"]

    def test_exact_plan_approval_never_regenerates(self, io, tmp_path):
        state = _base_state()
        io["load"]("TQQQ", state)
        plan = _make_plan(state)
        plan_path = pipeline.write_order_plan(plan, plans_dir=str(tmp_path))
        approval_path = pipeline.approve_plan_file(
            plan.plan_id,
            approvals_dir=str(tmp_path),
            plans_dir=str(tmp_path),
            now=_fixed_now(),
        )
        approval = pipeline.load_approval(str(approval_path))
        assert approval["plan_id"] == plan.plan_id
        assert approval["orders"] == plan.orders

    def test_exact_plan_approval_rejects_state_change(self, tmp_path):
        state = _base_state()
        plan = _make_plan(state)
        pipeline.write_order_plan(plan, plans_dir=str(tmp_path))
        changed = deepcopy(state)
        changed["T"] = 3.0
        # approve_plan_file 는 load_state 로 현재 상태를 읽습니다.
        original_loader = trading_bot.load_state
        trading_bot.load_state = lambda symbol: changed
        try:
            with pytest.raises(RuntimeError, match="state 가 PREVIEW 이후 변경"):
                pipeline.approve_plan_file(
                    plan.plan_id,
                    approvals_dir=str(tmp_path),
                    plans_dir=str(tmp_path),
                    now=_fixed_now(),
                )
        finally:
            trading_bot.load_state = original_loader


# ── RUN_PHASE 디스패처 ────────────────────────────────────────────


class TestPhaseDispatcher:
    def _prepare(self, io, monkeypatch):
        monkeypatch.setattr(trading_bot, "SYMBOLS", [_symbol_config()])
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        io["load"]("TQQQ", _base_state())
        _write_evidence(Path("."))

    def test_preview_phase_writes_plan_no_save_no_order(self, io, monkeypatch):
        self._prepare(io, monkeypatch)
        broker = _make_broker()
        failures = trading_bot._dispatch_pipeline_phase("PREVIEW", broker)
        assert failures == 0
        assert io["saved"] == []
        assert broker.place_order.call_count == 0
        plans = list(Path("plans").glob("*.json"))
        assert plans, "PREVIEW 는 계획 파일을 남겨야 합니다"

    def test_approve_phase_requires_explicit_plan(self, io, monkeypatch):
        self._prepare(io, monkeypatch)
        monkeypatch.delenv("PLAN_ID", raising=False)
        monkeypatch.delenv("PLAN_FILE", raising=False)
        broker = _make_broker()
        assert trading_bot._dispatch_pipeline_phase("APPROVE", broker) == 1
        assert broker.place_order.call_count == 0

    def test_approve_phase_uses_preview_plan_file(self, io, monkeypatch):
        self._prepare(io, monkeypatch)
        broker = _make_broker()
        assert trading_bot._dispatch_pipeline_phase("PREVIEW", broker) == 0
        plan_path = next(Path("plans").glob("*.json"))
        plan_id = plan_path.stem
        monkeypatch.setenv("PLAN_ID", plan_id)

        assert trading_bot._dispatch_pipeline_phase("APPROVE", broker) == 0
        approval = pipeline.load_approval(
            plan_id, approvals_dir=str(Path("approvals"))
        )
        assert approval["plan_id"] == plan_id
        assert approval["consumed"] is False
        assert broker.place_order.call_count == 0

    def _patch_verify_evidence(self, monkeypatch):
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )

    def test_verify_phase_accepts_consumed_approval(self, io, monkeypatch, tmp_path):
        state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        pipeline.claim_approval(approval, now=_fixed_now())
        # 소비된 승인은 실제 전송 기록 + EXECUTE 의 명시적 executed 기록이 필요합니다.
        pipeline_execute._write_transmitted_plan(path, plan, approval, now=_fixed_now())
        # 소비된 승인은 EXECUTE 의 명시적 executed 기록이 있어야 성공 판정됩니다.
        Path(str(path) + ".recovery.json").write_text(
            json.dumps({"plan_id": plan.plan_id, "outcome": "executed"}),
            encoding="utf-8",
        )
        _write_evidence(tmp_path)
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        self._patch_verify_evidence(monkeypatch)
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 0

    def test_verify_phase_reports_unresolved_fence(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        _write_evidence(tmp_path)
        fenced = deepcopy(state)
        fenced["pending_order_intent"] = {"symbol": "TQQQ"}
        io["load"]("TQQQ", fenced)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        self._patch_verify_evidence(monkeypatch)
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1

    def test_verify_phase_requires_evidence(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        for evidence in tmp_path.glob("settlement-*.json"):
            evidence.unlink()
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        self._patch_verify_evidence(monkeypatch)
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1

    def test_execute_phase_requires_plan_id(self, io, monkeypatch):
        monkeypatch.delenv("APPROVAL_PLAN_ID", raising=False)
        monkeypatch.delenv("APPROVAL_FILE", raising=False)
        broker = _make_broker()
        assert trading_bot._dispatch_pipeline_phase("EXECUTE", broker) == 1
        assert broker.place_order.call_count == 0


# ── EXECUTE ───────────────────────────────────────────────────────


class TestExecute:
    def test_public_execute_orders_removed(self, io):
        """주문 루프는 execute_approved_plan() 내부 함수뿐입니다 (P0)."""
        assert not hasattr(pipeline_execute, "execute_orders")
        with pytest.raises(ImportError):
            exec("from pipeline_execute import execute_orders", {})
        broker = _make_broker()
        assert broker.place_order.call_count == 0

    def test_fence_blocks_preview_and_execute(self, io, tmp_path):
        fenced = _base_state(
            pending_order_batch={"session": "2026-10-23", "orders": []}
        )
        io["load"]("TQQQ", fenced)
        with pytest.raises(RuntimeError, match="미해결 주문/정산"):
            trading_bot._settled_from_state(_symbol_config())

        _state, _plan, path = _approve(tmp_path, state=fenced)
        with pytest.raises(RuntimeError, match="미해결 주문/정산"):
            pipeline_execute.execute_approved_plan(
                _make_broker(), str(path), now=_fixed_now(), state_override=fenced
            )
        approval = pipeline.load_approval(str(path))
        assert approval["consumed"] is False, "fence 거부 시 승인을 소비하면 안 됩니다"

    def test_approve_refuses_unsettled_new_fill(self, io, tmp_path):
        state = _base_state()
        io["load"]("TQQQ", state)
        plan = _make_plan(state)
        pipeline.write_order_plan(plan, plans_dir=str(tmp_path))
        fill = {
            "odno": "NEW1",
            "ord_dt": "20261024",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-24T02:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        broker = _make_broker(balance_qty=1, history=[fill])
        with pytest.raises(trading_bot.SymbolScopedError, match="새 체결|재정산"):
            pipeline.approve_plan_file(
                plan.plan_id,
                approvals_dir=str(tmp_path),
                plans_dir=str(tmp_path),
                now=_fixed_now(),
                broker=broker,
            )

    def test_unconfirmed_amount_blocks_execute_plan(self, io, tmp_path):
        state = _base_state(_pending_amount_odnos=["OD1"])
        _state, _plan, path = _approve(tmp_path, state=state)
        broker = _make_broker()
        with pytest.raises(RuntimeError, match="미해결 주문/정산"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now(), state_override=state
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_execute_aborts_on_new_unsettled_fill(self, io, tmp_path):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        fill = {
            "odno": "NEW1",
            "ord_dt": "20261024",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-24T02:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        broker = _make_broker(balance_qty=1, history=[fill])
        with pytest.raises(RuntimeError, match="새 체결|재정산"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_execute_allows_deferred_execution_session_fill(self, io, tmp_path):
        """SETTLE 이 실행 세션으로 미룬 체결은 잔고 교차검증을 통과하면 허용됩니다.

        이 체결은 state 워터마크 앞이라 재정산 비교에서는 '새 체결'로 보입니다.
        구현은 `allowed_fill_odnos` 로 넘기되, `position_qty` + 미룬 순변동이
        브로커 잔고와 **정확히 일치**할 때만 허용합니다 (조용한 통과 금지).
        """
        state = _base_state(position_qty=0, orders_meta=_bot_order_meta("DEF1"))
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        fill = {
            "odno": "DEF1",
            "ord_dt": "20261026",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-26T08:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        broker = _make_broker(balance_qty=1, history=[fill])

        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )

        assert result["outcome"] == "executed"
        assert broker.place_order.call_count == 1

    def test_execute_deferred_fill_balance_mismatch_fails_closed(self, io, tmp_path):
        """미룬 체결로 설명되지 않는 잔고 차이는 EXECUTE 를 중단해야 합니다."""
        state = _base_state(position_qty=0, orders_meta=_bot_order_meta("DEF1"))
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        fill = {
            "odno": "DEF1",
            "ord_dt": "20261026",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-26T08:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        # 잔고 2주인데 미룬 체결은 1주 → 설명되지 않는 +1주가 있음.
        broker = _make_broker(balance_qty=2, history=[fill])
        with pytest.raises(trading_bot.SymbolScopedError, match="보류 체결 검증 실패"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_deferred_buy_spend_counts_against_dollar_seed_cap(
        self, io, monkeypatch, tmp_path
    ):
        """P1: 미룬 매수 $100 이 시드 캡에 반영되어 추가 매수를 허용하지 않습니다.

        seed=$10,000 / state.net_invested=$9,900 / 미룬 매수 $100 →
        재계산 시 남은 시드는 $0 이므로 승인된 매수 주문을 낼 수 없습니다.
        수량 reconciliation 만으로는 이 달러 초과를 막지 못합니다.
        """
        state = _base_state(
            T=10.0, net_invested=9900.0, position_qty=198,
            orders_meta=_bot_order_meta("DEF1"),
        )
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(
            trading_bot, "무한매수법_V4", _seed_cap_strategy(price=50.0)
        )
        fill = _deferred_fill(odno="DEF1", amount="100")
        broker = _make_broker(balance_qty=199, history=[fill])

        spend, pending = pipeline._deferred_buy_spend(state, [fill])
        assert (spend, pending) == (100.0, [])

        with pytest.raises(RuntimeError, match="주문 개수 불일치|허용 오차"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_deferred_buy_without_confirmed_amount_blocks_execute(
        self, io, monkeypatch, tmp_path
    ):
        """P1: 금액 미확정 미룬 매수는 추정하지 않고 EXECUTE 를 차단합니다."""
        state = _base_state(
            T=10.0, net_invested=9900.0, position_qty=198,
            orders_meta=_bot_order_meta("DEF1"),
        )
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(
            trading_bot, "무한매수법_V4", _seed_cap_strategy(price=50.0)
        )
        fill = _deferred_fill(odno="DEF1", amount="0")
        broker = _make_broker(balance_qty=199, history=[fill])

        with pytest.raises(
            trading_bot.SymbolScopedError,
            match="금액이 확정되지 않았|금액 미확정|재정산 보류",
        ):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_deferred_sell_does_not_loosen_seed_cap(
        self, io, monkeypatch, tmp_path
    ):
        """P1: 미룬 매도는 시드 캡에서 제외됩니다 (보수적 비대칭).

        남은 시드 $40 에 미룬 매도 $100 을 더해 $140 으로 인정하면
        $50 매수가 허용되어 버립니다. 매도 회수액은 인정하지 않아야 합니다.
        """
        state = _base_state(
            T=10.0, net_invested=9960.0, position_qty=10,
            orders_meta=_bot_order_meta("DEFSELL", side="SELL", qty=1),
        )
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(
            trading_bot, "무한매수법_V4", _seed_cap_strategy(price=50.0)
        )
        fill = _deferred_fill(odno="DEFSELL", side="매도", amount="100")
        broker = _make_broker(balance_qty=9, history=[fill])

        assert pipeline._deferred_buy_spend(state, [fill]) == (0.0, [])

        with pytest.raises(RuntimeError, match="주문 개수 불일치|허용 오차"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0

    def test_rebuild_tolerance_enforced(self):
        approved = _orders_buy(50.0)
        rebuilt_close = _orders_buy(50.01)
        rebuilt_far = _orders_buy(52.0)
        policy = pipeline.execution_tolerance_policy()
        assert pipeline_execute.compare_order_plans(
            approved, rebuilt_close, policy=policy
        ) == []
        problems = pipeline_execute.compare_order_plans(
            approved, rebuilt_far, policy=policy
        )
        assert any("가격 불일치" in p for p in problems), problems
        # 정책 없이 비교하면(승인 바인딩 없음) 통과시키지 않습니다 (fail-closed).
        assert pipeline_execute.compare_order_plans(approved, rebuilt_close)

    def test_live_refresh_false_fails_closed_before_side_effects(self, io, tmp_path):
        """LIVE 에서는 계획 재계산 생략을 승인/브로커/주문 부수효과 전에 거부합니다."""
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="refresh=False"):
            pipeline_execute.execute_approved_plan(
                broker,
                str(path),
                now=_fixed_now(),
                refresh=False,
                state_override=deepcopy(state),
            )

        assert broker.place_order.call_count == 0
        # 승인 로드/세션/만료 검증 전에 거부되므로 브로커 조회도 없습니다.
        assert broker.get_order_history.call_count == 0
        assert broker.get_balance.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False, (
            "재계산 거부 시 승인을 소비하면 안 됩니다"
        )
        assert not Path(pipeline.transmitted_plan_path(str(path))).exists()
        assert io["saved"] == []

    def test_flow_helper_rejects_live_refresh_false(self, io, tmp_path):
        """테스트 헬퍼도 LIVE 에서 refresh=False 를 전달하지 못합니다."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pipeline_flow

        state = _base_state()
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="refresh=False"):
            pipeline_flow.run_approved(
                tmp_path,
                broker,
                state,
                _orders_buy(),
                refresh=False,
                now=_fixed_now(),
            )

        assert broker.place_order.call_count == 0
        assert not (tmp_path / "approvals").exists(), (
            "거부 시 승인 파일을 만들면 안 됩니다"
        )

    def test_sell_total_over_holdings_blocks(self, io, monkeypatch, tmp_path):
        sell_orders = [
            {
                "comment": "테스트 매도",
                "side": "SELL",
                "quantity": 5,
                "price": 50.0,
                "order_type": "LIMIT",
            }
        ]

        def _sell_strategy(broker, **kwargs):
            result = _fake_strategy(broker, **kwargs)
            result["orders"] = sell_orders
            return result

        monkeypatch.setattr(trading_bot, "무한매수법_V4", _sell_strategy)
        state, _plan, path = _approve(
            tmp_path, state=_base_state(), orders=sell_orders
        )
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=0)
        with pytest.raises(RuntimeError, match="매도 합계"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0

    def test_crash_mid_execute_consumed_and_fence_recovery(self, io, tmp_path):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker()
        broker.place_order.side_effect = RuntimeError("network down")

        with pytest.raises(trading_bot.SymbolScopedError, match="fence"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )

        approval = pipeline.load_approval(str(path))
        assert approval["consumed"] is True
        recovery = Path(str(path) + ".recovery.json")
        assert recovery.exists()
        assert '"outcome": "execution-failed"' in recovery.read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="이미 사용된"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 1

        fenced = next((s for s in io["saved"] if s.get("pending_order_intent")), None)
        assert fenced is not None
        assert fenced["pending_order_batch"] is not None
        fenced["pending_order_intent"]["submitted_session"] = "2020-01-01"
        fenced["pending_order_batch"]["session"] = "2020-01-01"
        assert trading_bot._recover_order_fence(
            fenced, "TQQQ", live_qty=0, persist=False
        ) is True
        assert fenced["pending_order_intent"] is None

    def test_cancel_failure_does_not_burn_approval(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))

        def _boom(*args, **kwargs):
            raise trading_bot.SymbolScopedError("취소 확정 실패")

        monkeypatch.setattr(trading_bot, "_cancel_open_bot_orders", _boom)
        with pytest.raises(trading_bot.SymbolScopedError):
            pipeline_execute.execute_approved_plan(
                _make_broker(), str(path), now=_fixed_now()
            )
        approval = pipeline.load_approval(str(path))
        assert approval["consumed"] is False, "취소 실패는 승인을 소비하면 안 됩니다"
        recovery = Path(str(path) + ".recovery.json")
        assert recovery.exists()
        assert '"outcome": "approval-skipped"' in recovery.read_text(encoding="utf-8")
        assert not Path(str(path) + ".deferred.json").exists(), (
            "선점 전(approval-skipped) 중단은 보류 체결 sidecar 를 남기면 안 됩니다"
        )

        # 조건이 해소되면 같은 승인으로 재시도할 수 있고, 결과 기록은 executed 로
        # 승격됩니다 (approval-skipped 는 소진이 아님).
        monkeypatch.setattr(
            trading_bot, "_cancel_open_bot_orders", lambda *a, **k: 0
        )
        retried = pipeline_execute.execute_approved_plan(
            _make_broker(), str(path), now=_fixed_now()
        )
        assert retried["outcome"] == "executed"
        assert '"outcome": "executed"' in recovery.read_text(encoding="utf-8")


# ── EXECUTE 경계 게이트 (함수 자체 방어) ──────────────────────────


class TestExecutePhaseBoundaryGate:
    """직접 호출도 LIVE+RUN_PHASE=EXECUTE 가 아니면 주문 경로에 도달하지 못합니다."""

    @pytest.mark.parametrize(
        "mode,phase",
        [
            ("DRY", "EXECUTE"),   # DRY + 유효한 승인 + 실제(모의) 브로커
            ("LIVE", "PREVIEW"),  # 잘못된 위상
        ],
    )
    def test_non_live_or_wrong_phase_refused_without_side_effects(
        self, io, monkeypatch, tmp_path, mode, phase
    ):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(trading_bot, "TRADE_MODE", mode)
        monkeypatch.setenv("RUN_PHASE", phase)
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="LIVE.*EXECUTE"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )

        assert broker.place_order.call_count == 0
        # 게이트는 승인 로드/브로커 조회 이전이어야 합니다.
        assert broker.get_order_history.call_count == 0
        assert broker.get_balance.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False, (
            "위상 거부 시 승인을 소비하면 안 됩니다"
        )
        assert not Path(pipeline.transmitted_plan_path(str(path))).exists()
        assert not Path(str(path) + ".executed.json").exists()
        assert io["saved"] == []

    def test_private_entry_point_also_refuses_dry(
        self, io, monkeypatch, tmp_path
    ):
        """`_execute_approved_plan` 직접 호출도 같은 경계 게이트를 통과해야 합니다."""
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        monkeypatch.setenv("RUN_PHASE", "EXECUTE")
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="LIVE.*EXECUTE"):
            pipeline_execute._execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )

        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False
        assert not Path(pipeline.transmitted_plan_path(str(path))).exists()
        assert io["saved"] == []


# ── Round-2 HOLD 회귀 테스트 ─────────────────────────────────────


class TestLiveApprovalGate:
    """P0-A: 레거시 run_one_symbol LIVE 경로도 명시적 승인 없이는 실행 금지."""

    def _settled(self, state):
        return pipeline.SettledState(
            state=state,
            symbol="TQQQ",
            exchange="NAS",
            splits=20,
            symbol_type="TQQQ",
            seed=8000.0,
            effective_seed=0.0,
            T=0.0,
            settlement_run_id="run-test",
            evidence_id="run-test",
            session="2026-10-24",
        )

    def test_live_without_approval_refuses_and_does_not_autoapprove(
        self, io, monkeypatch
    ):
        state = _base_state()
        io["load"]("TQQQ", state)
        _write_evidence(Path("."))
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(
            trading_bot, "settle_positions", lambda broker, cfg: self._settled(state)
        )
        monkeypatch.delenv("APPROVAL_PLAN_ID", raising=False)
        monkeypatch.delenv("APPROVAL_FILE", raising=False)
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="명시적 승인이 필요"):
            trading_bot.run_one_symbol(broker, _symbol_config())

        assert broker.place_order.call_count == 0
        assert list(Path("approvals").glob("*.json")) == [], \
            "승인 파일을 자동 생성하면 안 됩니다 (P0-A)"
        assert list(Path("plans").glob("*.json")), \
            "PREVIEW 계획 파일은 남겨 명시적 APPROVE 가 가능해야 합니다"

    def test_dry_preview_still_requires_no_approval(self, io, monkeypatch):
        state = _base_state()
        io["load"]("TQQQ", state)
        _write_evidence(Path("."))
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        monkeypatch.setattr(
            trading_bot, "settle_positions", lambda broker, cfg: self._settled(state)
        )
        broker = _make_broker()
        assert trading_bot.run_one_symbol(broker, _symbol_config()) is None
        assert broker.place_order.call_count == 0


class TestSettlementEvidenceBinding:
    """P1-E: 실패 SETTLE evidence 는 사용 불가, PREVIEW/APPROVE 는 커밋 id 에 바인딩."""

    def test_failed_settle_evidence_is_not_committed(self, io):
        state, buyer = TestSettle()._processed_state()
        # 체결금액을 확인할 수 없는 신규 체결 → LIVE 정산 보류 대상
        unconfirmed = {
            "odno": "U1",
            "ord_dt": "20261021",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-21T19:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[buyer, unconfirmed])

        with pytest.raises(trading_bot.SymbolScopedError, match="보류"):
            pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())

        evidence_files = list(Path(".").glob("settlement-TQQQ-*.json"))
        assert evidence_files, "실패 근거도 감사용으로 남겨야 합니다"
        assert '"committed": false' in evidence_files[0].read_text(encoding="utf-8")
        assert pipeline.evidence_file_for_run("TQQQ", "run-test") == ""
        assert io["saved"] == []

    def test_preview_requires_committed_settlement(self, io):
        state = _base_state()
        state.pop("last_settlement_id", None)
        io["load"]("TQQQ", state)
        with pytest.raises(RuntimeError, match="커밋된 정산 근거"):
            trading_bot._settled_from_state(_symbol_config())

    def test_preview_requires_committed_evidence_file(self, io):
        io["load"]("TQQQ", _base_state())
        # state id 는 있지만 커밋된 evidence 파일이 없습니다.
        with pytest.raises(RuntimeError, match="정산 근거 파일"):
            trading_bot._settled_from_state(_symbol_config())

    def test_approve_rejects_plan_from_other_settlement(self, io, tmp_path):
        state = _base_state()
        io["load"]("TQQQ", state)
        _write_evidence(tmp_path, run_id="run-other")
        plan = _make_plan(state, run_id="run-other")
        pipeline.write_order_plan(plan, plans_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="마지막 커밋"):
            pipeline.approve_plan_file(
                plan.plan_id,
                approvals_dir=str(tmp_path),
                plans_dir=str(tmp_path),
                now=_fixed_now(),
            )

    def test_execute_requires_committed_evidence(self, io, tmp_path):
        state, _plan, path = _approve(tmp_path)
        for evidence in tmp_path.glob("settlement-*.json"):
            evidence.unlink()
        io["load"]("TQQQ", deepcopy(state))
        with pytest.raises(RuntimeError, match="커밋된 SETTLE 근거"):
            pipeline_execute.execute_approved_plan(
                _make_broker(), str(path), now=_fixed_now()
            )


class TestForceTSettleRefusal:
    """P2: SETTLE 경로에서는 FORCE_T 를 적용하지 않습니다 (REPAIR 단독 경로)."""

    def test_force_t_is_refused_in_settle_without_apply_or_save(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        config = _symbol_config()
        config["force_t"] = 5.0
        broker = _make_broker(balance_qty=10, history=[buyer])

        # 상태/브로커/근거에 손대기 전에 거부되어야 합니다.
        with pytest.raises(RuntimeError, match="REPAIR.*FORCE_T|FORCE_T.*REPAIR"):
            pipeline.settle_positions(ReadOnlyBroker(broker), config)

        assert io["saved"] == [], "SETTLE 은 FORCE_T 를 적용/저장하면 안 됩니다"
        assert broker.get_order_history.call_count == 0
        assert broker.get_balance.call_count == 0
        assert list(Path(".").glob("settlement-*.json")) == [], (
            "거부된 SETTLE 은 정산 근거도 남기지 않아야 합니다"
        )


class TestRepairForceTValidation:
    """P1: REPAIR FORCE_T 는 저장 전 검증을 통과해야 합니다."""

    def _run_repair(self, monkeypatch, state, *, history=None, balance_qty=0,
                    balance_error=None, balance_none=False):
        saved = []
        broker = _make_broker(balance_qty=balance_qty, history=history or [])
        if balance_error is not None:
            broker.get_balance.side_effect = balance_error
        if balance_none:
            broker.get_balance.return_value = None
        config = _symbol_config()
        config["force_t"] = 5.0
        monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
        monkeypatch.setattr(trading_bot, "SYMBOLS", [config])
        monkeypatch.setattr(
            trading_bot, "load_state", lambda symbol: deepcopy(state)
        )
        monkeypatch.setattr(
            trading_bot,
            "save_state",
            lambda symbol, new_state: saved.append(deepcopy(new_state)),
        )
        trading_bot._repair_force_t()
        return saved

    def test_force_t_with_pending_fence_is_not_saved(self, monkeypatch):
        state = _base_state(pending_order_intent={"symbol": "TQQQ"})
        saved = self._run_repair(monkeypatch, state)
        assert saved == [], "fence 가 있는데 FORCE_T 를 저장하면 안 됩니다"

    def test_force_t_with_settlement_hold_is_not_saved(self, monkeypatch):
        state = _base_state(_settlement_hold="근거 부족")
        saved = self._run_repair(monkeypatch, state)
        assert saved == [], "정산 보류 상태에서 FORCE_T 를 저장하면 안 됩니다"

    def test_force_t_not_saved_when_balance_lookup_fails(self, monkeypatch):
        """잔고 조회 실패 시 보유수량 미확인 상태로 FORCE_T 를 저장하지 않습니다."""
        state = _base_state(T=1.0)
        saved = self._run_repair(
            monkeypatch,
            state,
            balance_error=RuntimeError("balance api down"),
        )
        assert saved == [], (
            "잔고를 확인하지 못하면 position_qty 로 보류를 피해가 저장하면 안 됩니다"
        )

    def test_force_t_balance_none_triggers_full_reinference_hold(self, monkeypatch):
        """get_balance() None(보유 0 확정)이면 전체 재추정이 돌아 정산 보류를 잡습니다."""
        state = _base_state(T=1.0)
        unconfirmed = {
            "odno": "ORD1",
            "ord_dt": "20260714",
            "ord_tmd": "090000",
            "ord_datetime_utc": "2026-07-14T00:00:00+00:00",
            "ft_ccld_qty": "1",
            "sll_buy_dvsn_cd_name": "매수",
            "nccs_qty": "0",
            # 체결금액(ft_ccld_amt3)/평균가/현재가 없음 → 금액 미확정
        }
        saved = self._run_repair(
            monkeypatch,
            state,
            history=[unconfirmed],
            balance_none=True,
        )
        assert saved == [], (
            "잔고 None 은 보유 0 확정이므로 전체 재추정 보류를 무시하고 저장하면 안 됩니다"
        )

    def test_force_t_balance_none_with_empty_history_saves(self, monkeypatch):
        """get_balance() None + 깨끗한 빈 이력은 보유 0 확정으로 보고 저장합니다."""
        state = _base_state(T=1.0)
        saved = self._run_repair(monkeypatch, state, balance_none=True)
        assert len(saved) == 1, "잔고 None(보유 0) + 빈 이력은 정확히 1회 저장해야 합니다"
        assert saved[0]["T"] == pytest.approx(5.0)

    def test_force_t_saves_once_when_validation_passes(self, monkeypatch):
        state = _base_state(T=1.0)
        saved = self._run_repair(monkeypatch, state)
        assert len(saved) == 1, "검증 통과 시 정확히 1회 저장해야 합니다"
        assert saved[0]["T"] == pytest.approx(5.0)


class TestApprovalFingerprint:
    """P2-D: 승인 파일에는 계좌번호 원문 대신 HMAC 지문 + 마스킹 표시값만 저장."""

    RAW_ACCOUNT = "12345678-01"
    SECRET = "server-side-fingerprint-secret"

    def _patch_account(self, monkeypatch, raw=None):
        import config as runtime_config

        monkeypatch.setattr(
            runtime_config,
            "BROKER_CONFIG",
            {"account_no": raw if raw is not None else self.RAW_ACCOUNT},
        )

    def test_hmac_fingerprint_and_masked_display(self, io, tmp_path, monkeypatch):
        self._patch_account(monkeypatch)
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, self.SECRET)
        state, plan, path = _approve(tmp_path)
        text = Path(path).read_text(encoding="utf-8")
        assert self.RAW_ACCOUNT not in text
        approval = pipeline.load_approval(str(path))
        assert approval["account_id"].startswith("hmac-sha256:")
        assert (
            approval["account_id"]
            == pipeline._account_fingerprint(self.RAW_ACCOUNT)
        )
        assert approval["account_display"] == "****8-01"
        assert pipeline.verify_approval(approval, state, now=_fixed_now()) == []

    def test_secret_changes_fingerprint(self, io, monkeypatch):
        monkeypatch.delenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, raising=False)
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        default_fp = pipeline._account_fingerprint(self.RAW_ACCOUNT)
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, self.SECRET)
        secret_fp = pipeline._account_fingerprint(self.RAW_ACCOUNT)
        assert default_fp.startswith("hmac-sha256:")
        assert secret_fp.startswith("hmac-sha256:")
        assert default_fp != secret_fp, "비밀값이 다르면 지문도 달라야 합니다"

    def test_live_without_secret_fails_closed(self, io, monkeypatch):
        self._patch_account(monkeypatch)
        monkeypatch.delenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, raising=False)
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        with pytest.raises(RuntimeError, match=pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV):
            pipeline._broker_identity()

    def test_dry_without_secret_uses_default_key(self, io, monkeypatch):
        self._patch_account(monkeypatch)
        monkeypatch.delenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, raising=False)
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        identity = pipeline._broker_identity()
        assert identity["account_id"].startswith("hmac-sha256:")
        assert identity["account_display"] == "****8-01"

    def test_raw_account_in_approval_is_rejected(self, io, tmp_path, monkeypatch):
        self._patch_account(monkeypatch)
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, self.SECRET)
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["account_id"] = self.RAW_ACCOUNT  # 구버전/수동 편집
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("account_id" in p for p in problems), problems

    def test_account_display_tamper_breaks_plan_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, self.SECRET)
        self._patch_account(monkeypatch)
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["account_display"] = "****9999"
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("plan_hash" in p for p in problems), problems


class TestPlanHashExpiry:
    """P2-H: created/expires 를 plan_hash 에 포함, 구버전 schema 는 명시 거부."""

    def test_expiry_tamper_breaks_plan_hash(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["expires_at_utc"] = (
            _fixed_now() + timedelta(hours=48)
        ).isoformat()
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("plan_hash" in p for p in problems), problems

    def test_created_at_tamper_breaks_plan_hash(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["created_at_utc"] = "2020-01-01T00:00:00+00:00"
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("plan_hash" in p for p in problems), problems

    def test_old_plan_schema_rejected_clearly(self, io, tmp_path):
        state = _base_state()
        io["load"]("TQQQ", state)
        plan = _make_plan(state)
        plan_file = Path(pipeline.write_order_plan(plan, plans_dir=str(tmp_path)))
        data = json.loads(plan_file.read_text(encoding="utf-8"))
        data["schema_version"] = 1
        plan_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            pipeline.approve_plan_file(
                plan.plan_id,
                approvals_dir=str(tmp_path),
                plans_dir=str(tmp_path),
                now=_fixed_now(),
            )

    def test_old_approval_schema_rejected_clearly(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["schema_version"] = 1
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("schema_version" in p for p in problems), problems


class TestHistorySettledness:
    """P1-F: 반복 조회 동일성 외 완전성/세션 종료 규칙."""

    def test_watermark_missing_from_history_blocks(self):
        state = _base_state(last_processed_ordno="B1")
        problems = pipeline._verify_history_complete(
            state, [], 30, now=_fixed_now()
        )
        assert any("워터마크" in p for p in problems), problems

    def test_recent_orders_meta_missing_blocks(self):
        state = _base_state(
            last_updated="2026-10-24T00:00:00+00:00",
            orders_meta={
                "B2": {
                    "submitted_session": "2026-10-24",
                    "submitted_at": "20261024040000",
                }
            },
        )
        problems = pipeline._verify_history_complete(
            state, [], 30, now=_fixed_now()
        )
        assert any("orders_meta" in p for p in problems), problems

    def test_recent_open_order_allowed_while_session_open(self):
        row = {
            "odno": "O1",
            "prcs_stat_name": "미체결",
            "ord_datetime_utc": "2026-10-24T15:50:00+00:00",
        }
        now = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert pipeline._history_has_recent_open_order([row], now=now) is False

    def test_recent_open_order_blocks_after_session_close(self):
        row = {
            "odno": "O1",
            "prcs_stat_name": "미체결",
            "ord_datetime_utc": "2026-10-24T20:25:00+00:00",
        }
        now = datetime(2026, 10, 24, 16, 30, tzinfo=ZoneInfo("America/New_York"))
        assert pipeline._history_has_recent_open_order([row], now=now) is True

    def test_settle_blocks_on_incomplete_history(self, io, monkeypatch):
        state, buyer = TestSettle()._processed_state()
        state["orders_meta"] = {
            "MISSING": {
                "submitted_session": "2026-10-24",
                "submitted_at": "20261024040000",
            }
        }
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setenv("HISTORY_SETTLEDNESS_CHECK", "true")
        monkeypatch.setenv("HISTORY_SETTLEDNESS_DELAY_SECONDS", "0")
        broker = _make_broker(balance_qty=10, history=[buyer])
        settled = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert settled.halt_reason == "history_not_settled"
        assert io["saved"] == []


class TestVerifyPhaseOutcomes:
    """P1-C: VERIFY 는 trading_bot.py 로 실행되며 outcome 을 구분한다."""

    def test_cron_verify_uses_trading_bot_phase(self):
        cron = (_repo_root / "ops" / "cron.example").read_text(encoding="utf-8")
        verify_line = next(
            line
            for line in cron.splitlines()
            if "RUN_PHASE=VERIFY" in line and "run_daily.sh" in line
        )
        assert "run_daily.sh" in verify_line
        assert "verify_run.sh" not in verify_line
        launcher = (_repo_root / "ops" / "run_daily.sh").read_text(encoding="utf-8")
        assert "trading_bot.py" in launcher

    def test_verify_reports_approval_skipped(self, io, monkeypatch, tmp_path, capsys):
        state, _plan, path = _approve(tmp_path)
        for evidence in tmp_path.glob("settlement-*.json"):
            evidence.unlink()
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1
        assert "approval-skipped" in capsys.readouterr().out

    def test_verify_reports_execution_failed(self, io, monkeypatch, tmp_path, capsys):
        state, _plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        Path(str(path) + ".recovery.json").write_text(
            json.dumps({"outcome": "execution-failed", "reason": "강제 실패"}),
            encoding="utf-8",
        )
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot,
            "verify_evidence_file_with_ops",
            lambda *a, **k: (False, ["강제 검증 실패"]),
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1
        assert "execution-failed" in capsys.readouterr().out

    def test_verify_runs_readonly_broker_checks(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        fill = {
            "odno": "NEW1",
            "ord_dt": "20261024",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-24T02:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        broker = _make_broker(balance_qty=1, history=[fill])
        assert trading_bot._run_verify_phase(broker=broker) == 1


class TestExecuteOrdersGate:
    """P0: 주문 루프는 execute_approved_plan() 내부 함수로만 존재합니다.

    이전 라운드의 `_APPROVAL_CLAIM_TOKEN` 관례와 `_ClaimedApproval`/`_issue_claim`
    핸들 발급 API 를 제거했습니다. 모듈에 실행 함수/핸들이 없으므로 같은
    프로세스에서 import 해 임의 계획/state 를 주입할 수 없습니다.
    """

    def test_no_public_execute_or_handle_api(self):
        assert not hasattr(pipeline_execute, "execute_orders")
        assert not hasattr(pipeline_execute, "_APPROVAL_CLAIM_TOKEN")
        assert not hasattr(pipeline_execute, "_ClaimedApproval")
        assert not hasattr(pipeline_execute, "_ISSUED_CLAIMS")
        assert not hasattr(pipeline_execute, "_issue_claim")
        with pytest.raises(ImportError):
            exec("from pipeline_execute import execute_orders", {})

    def test_order_loop_lives_only_inside_approved_plan(self):
        """모듈 레벨 실행 함수는 execute_approved_plan 하나뿐입니다."""
        public = [
            name
            for name, obj in vars(pipeline_execute).items()
            if callable(obj)
            and getattr(obj, "__module__", "") == pipeline_execute.__name__
            and name.startswith("execute")
        ]
        assert public == ["execute_approved_plan"], public
        source = (_src_path / "pipeline_execute.py").read_text(encoding="utf-8")
        # 주문 루프 본문이 execute_approved_plan 안에서만 정의됩니다.
        assert "    def _run_orders():" in source
        assert "place_order(" in source

    def test_executed_approval_cannot_be_reexecuted_after_restart(
        self, io, monkeypatch, tmp_path
    ):
        """실행 완료된 승인은 프로세스 재시작 후에도 재실행 불가."""
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker()
        first = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert first["outcome"] == "executed"
        assert broker.place_order.call_count == 1

        with pytest.raises(RuntimeError, match="이미 사용/실행된 승인|이미 사용된"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 1, "재실행되면 안 됩니다"

        # 결과 기록(executed)도 덮어쓰지 않습니다.
        record = json.loads(
            Path(str(path) + ".recovery.json").read_text(encoding="utf-8")
        )
        assert record["outcome"] == "executed"

    def test_consumed_approval_cannot_be_executed(self, io, tmp_path):
        """이미 consumed=true 인 승인은 실행 경로로도 다시 실행할 수 없습니다."""
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        broker = _make_broker()

        with pytest.raises(RuntimeError, match="이미 사용|이미 실행"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        # 실행 마커도 같은 파일에 남아 있어야 합니다.
        assert Path(str(path) + ".executed.json").exists()


# ── Round-3 HOLD 회귀 테스트 ─────────────────────────────────────


class TestSettlementIdPersistence:
    """P1-A: last_settlement_id 계열이 실제 state 파일에 저장/복원되어야 합니다."""

    def test_settle_save_reload_then_settled_from_state(
        self, io, monkeypatch, tmp_path
    ):
        import state as state_module

        monkeypatch.setattr(state_module, "_STATE_FILE", str(tmp_path / ".state.json"))
        monkeypatch.setattr(trading_bot, "load_state", state_module.load_state)
        monkeypatch.setattr(trading_bot, "save_state", state_module.save_state)
        monkeypatch.chdir(tmp_path)

        state, buyer = TestSettle()._processed_state()
        state["last_settlement_id"] = ""
        state["last_settlement_evidence"] = ""
        state["last_settlement_committed_at_utc"] = ""
        state_module.save_state("TQQQ", deepcopy(state))

        broker = _make_broker(balance_qty=10, history=[buyer])
        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker), _symbol_config()
        )
        assert settled.halt_reason is None

        # 실제 파일을 다시 읽어 필드가 남았는지 확인합니다.
        reloaded = state_module.load_state("TQQQ")
        assert reloaded["last_settlement_id"] == "run-test"
        assert reloaded["last_settlement_evidence"].endswith(".json")
        assert reloaded["last_settlement_committed_at_utc"].startswith("2026-")

        # 저장된 id 로 PREVIEW/EXECUTE 재계산용 SettledState 가 만들어져야 합니다.
        rebuilt = pipeline.settled_from_state(_symbol_config())
        assert rebuilt.settlement_run_id == "run-test"
        assert rebuilt.evidence_path.endswith(".json")

    def test_canonical_hash_includes_last_settlement_id(self):
        base = _base_state(last_settlement_id="run-a")
        other = _base_state(last_settlement_id="run-b")
        assert canonical_state_hash(base) != canonical_state_hash(other)


class TestVerifyOrderAcceptance:
    """P1-B: VERIFY 는 승인 주문별 접수/잔량/체결 + 기대 잔고를 대조합니다."""

    PLAN_ID = "TQQQ-2026-10-24-run-test"

    def _setup(self, monkeypatch, tmp_path, *, meta_session, history, balance_qty,
               orders=None, order_metas=None, tolerance_policy=None):
        evidence = _write_evidence(tmp_path, balance_qty=0)
        approval = {
            "plan_id": self.PLAN_ID,
            "symbol": "TQQQ",
            "exchange": "NAS",
            "consumed": True,
            "orders": orders or _orders_buy(),
            # 승인 시점에 고정된 허용오차 — VERIFY 가 env 대신 이 값을 씁니다.
            "tolerance_policy": tolerance_policy
            or {"price_pct": 0.005, "price_abs": 0.02, "qty": 0},
            "_evidence_path": str(evidence),
        }
        state = _base_state(
            orders_meta=order_metas if order_metas is not None else {
                "NEW1": {
                    "side": "BUY",
                    "total_qty": 1,
                    "plan_id": self.PLAN_ID,
                    "plan_order_index": 1,
                    "submitted_session": meta_session,
                }
            }
        )
        for meta in state["orders_meta"].values():
            meta.setdefault("processed_filled_qty", 0)
        broker = _make_broker(balance_qty=balance_qty, history=history)
        return approval, state, broker

    def _filled_row(self, filled="1", remaining="0", side="매수", **extra):
        row = {
            "odno": "NEW1",
            "ord_dt": "20261024",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-24T02:00:00+00:00",
            "sll_buy_dvsn_cd_name": side,
            "ft_ccld_qty": filled,
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": remaining,
            "prcs_stat_name": "체결" if remaining == "0" else "미체결",
        }
        row.update(extra)
        return row

    def test_filled_order_matches_expected_balance(self, monkeypatch, tmp_path):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _TZ

        today = _dt.now(_TZ("America/New_York")).date().isoformat()
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session=today,
            history=[self._filled_row()],
            balance_qty=1,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_normal_fill_not_failed_by_reconciliation(self, monkeypatch, tmp_path):
        """승인 주문 체결로 인한 T/잔고 변화는 VERIFY 실패가 아닙니다 (P1-B)."""
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_unapproved_fill_triggers_reconciliation_failure(self, monkeypatch, tmp_path):
        """승인 계획에 없는 체결이 끼면 재정산 불일치로 실패합니다 (P1-B)."""
        manual = self._filled_row()
        manual.update({"odno": "MANUAL1", "ft_ccld_amt3": "50"})
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(), manual],
            balance_qty=2,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("재정산 불일치" in p for p in problems), problems

    def test_meta_quantity_mismatch_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=1,
            order_metas={
                "NEW1": {
                    "side": "BUY",
                    "total_qty": 2,
                    "plan_id": self.PLAN_ID,
                    "plan_order_index": 1,
                    "submitted_session": "2026-10-24",
                }
            },
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("orders_meta 수량 불일치" in p for p in problems), problems

    def test_meta_side_mismatch_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=1,
            order_metas={
                "NEW1": {
                    "side": "SELL",
                    "total_qty": 1,
                    "plan_id": self.PLAN_ID,
                    "plan_order_index": 1,
                    "submitted_session": "2026-10-24",
                }
            },
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("orders_meta side 불일치" in p for p in problems), problems

    def test_history_price_mismatch_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(price=55.0)],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문가 불일치" in p for p in problems), problems

    def test_history_price_match_passes(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(price=50.01)],
            balance_qty=1,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_meritz_normalized_order_price_mismatch_detected(
        self, monkeypatch, tmp_path
    ):
        """MERITZ 가 정규화한 _order_price 도 승인 주문가와 비교합니다."""
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(_order_price=55.0)],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문가 불일치" in p for p in problems), problems

    def test_meritz_order_class_code_matches_approved_type(
        self, monkeypatch, tmp_path
    ):
        """MERITZ oder_cls_code 34 는 승인 MOC 와 일치합니다."""
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 1,
                "price": 50.0,
                "order_type": "MOC",
            }
        ]
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(_oder_cls_code="34", _order_price=50.0)],
            balance_qty=1,
            orders=orders,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_meritz_order_class_code_mismatch_detected(
        self, monkeypatch, tmp_path
    ):
        """MERITZ oder_cls_code 32(LOC)는 승인 MOC 와 다르면 실패합니다."""
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 1,
                "price": 50.0,
                "order_type": "MOC",
            }
        ]
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(_oder_cls_code="32", _order_price=50.0)],
            balance_qty=1,
            orders=orders,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문유형 불일치" in p for p in problems), problems

    def test_meritz_missing_class_code_is_not_silently_skipped(
        self, monkeypatch, tmp_path
    ):
        """MERITZ 이력에 주문유형 키가 없으면 조용히 통과하지 않고 실패합니다 (P1)."""
        monkeypatch.setattr(trading_bot, "BROKER", "meritz")
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 1,
                "price": 50.0,
                "order_type": "MOC",
            }
        ]
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(_order_price=50.0)],
            balance_qty=1,
            orders=orders,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문유형 필드 없음" in p for p in problems), problems

    def test_unknown_order_type_value_is_not_silently_skipped(
        self, monkeypatch, tmp_path
    ):
        """주문유형 키가 있는데 표준값/메리츠 코드로 해석되지 않으면 실패합니다 (P1)."""
        monkeypatch.setattr(trading_bot, "BROKER", "mockbroker")
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(order_type="WEIRD")],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문유형 필드 없음" in p for p in problems), problems

    def test_history_order_type_mismatch_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(order_type="MOC")],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문유형 불일치" in p for p in problems), problems

    def test_original_order_qty_matches_approved_passes(
        self, monkeypatch, tmp_path
    ):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(ft_ord_qty="1")],
            balance_qty=1,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_original_order_qty_over_approved_detected(
        self, monkeypatch, tmp_path
    ):
        """브로커 원주문 수량(12)이 승인 수량(10)보다 크면 실패합니다 (P1-B)."""
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 10,
                "price": 50.0,
                "order_type": "LIMIT",
            }
        ]
        order_metas = {
            "NEW1": {
                "side": "BUY",
                "total_qty": 10,
                "plan_id": self.PLAN_ID,
                "plan_order_index": 1,
                "submitted_session": "2026-10-24",
            }
        }
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[
                self._filled_row(
                    filled="0", remaining="12", ft_ord_qty="12"
                )
            ],
            balance_qty=0,
            orders=orders,
            order_metas=order_metas,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문 수량 불일치" in p for p in problems), problems
        assert any("12" in p and "10" in p for p in problems), problems

    def test_original_order_qty_below_filled_detected(
        self, monkeypatch, tmp_path
    ):
        """원주문 수량(1)보다 체결수량(2)이 크면 실패합니다."""
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 2,
                "price": 50.0,
                "order_type": "LIMIT",
            }
        ]
        order_metas = {
            "NEW1": {
                "side": "BUY",
                "total_qty": 2,
                "plan_id": self.PLAN_ID,
                "plan_order_index": 1,
                "submitted_session": "2026-10-24",
            }
        }
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[
                self._filled_row(
                    filled="2", remaining="0", ft_ord_qty="1"
                )
            ],
            balance_qty=2,
            orders=orders,
            order_metas=order_metas,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("원주문 수량을 넘습니다" in p for p in problems), problems

    def test_original_order_qty_absent_is_lenient(
        self, monkeypatch, tmp_path, capsys
    ):
        """원주문 수량 필드가 없는 브로커는 체결/잔량 기준으로 통과합니다."""
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],  # ft_ord_qty 필드 없음
            balance_qty=1,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []
        out = capsys.readouterr().out
        assert "원주문 수량 필드 없음" in out

    def test_original_order_qty_missing_key_lenient_but_zero_value_fails(
        self, monkeypatch, tmp_path, capsys
    ):
        """필드 부재는 관대, 0으로 존재하면 "읽을 수 없음" 실패로 구분합니다.

        어댑터가 누락된 원주문 수량을 "0"으로 합성하면 이 구분이 사라지므로
        (필드 부재 → 실패) 회귀를 막는 테스트입니다.
        """
        assert trading_bot._row_original_qty(self._filled_row()) == (None, False)
        assert trading_bot._row_original_qty(
            self._filled_row(ft_ord_qty="0")
        ) == (None, True)

        # 필드 부재 → lenient 통과
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=1,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []
        assert "원주문 수량 필드 없음" in capsys.readouterr().out

        # 0으로 존재 → 실패 (합성 "0"이면 이 경로로 빠짐)
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(ft_ord_qty="0")],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("원주문 수량을 읽을 수 없습니다" in p for p in problems), problems

    def test_original_order_qty_present_but_side_mismatch_fails(
        self, monkeypatch, tmp_path
    ):
        """원주문 수량이 정상이어도 기존 side 검사는 그대로 실패합니다."""
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(side="매도", ft_ord_qty="1")],
            balance_qty=1,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("side 불일치" in p for p in problems), problems

    def _qty10_case(
        self, monkeypatch, tmp_path, *, filled, remaining, balance_qty
    ):
        """승인 수량 10주 기준 체결/잔량 조합을 만듭니다."""
        orders = [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 10,
                "price": 50.0,
                "order_type": "LIMIT",
            }
        ]
        order_metas = {
            "NEW1": {
                "side": "BUY",
                "total_qty": 10,
                "plan_id": self.PLAN_ID,
                "plan_order_index": 1,
                "submitted_session": "2026-10-24",
            }
        }
        return self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(filled=filled, remaining=remaining)],
            balance_qty=balance_qty,
            orders=orders,
            order_metas=order_metas,
        )

    def test_filled_plus_remaining_over_approved_detected(
        self, monkeypatch, tmp_path
    ):
        """체결 0 + 잔량 12 가 승인 10주를 넘으면 실패합니다 (P1-B)."""
        approval, state, broker = self._qty10_case(
            monkeypatch, tmp_path, filled="0", remaining="12", balance_qty=0
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("체결+잔량이 승인 수량을 넘습니다" in p for p in problems), problems
        assert any("12" in p and "10" in p for p in problems), problems

    def test_partial_fill_plus_remaining_over_approved_detected(
        self, monkeypatch, tmp_path
    ):
        """체결 6 + 잔량 5 = 11 이 승인 10주를 넘으면 실패합니다."""
        approval, state, broker = self._qty10_case(
            monkeypatch, tmp_path, filled="6", remaining="5", balance_qty=6
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("체결+잔량이 승인 수량을 넘습니다" in p for p in problems), problems

    def test_partial_fill_plus_remaining_equal_approved_passes(
        self, monkeypatch, tmp_path
    ):
        """체결 5 + 잔량 5 = 10 이면 승인 수량과 같으므로 통과합니다."""
        approval, state, broker = self._qty10_case(
            monkeypatch, tmp_path, filled="5", remaining="5", balance_qty=5
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_full_fill_passes(self, monkeypatch, tmp_path):
        """체결 10 + 잔량 0 = 10 이면 통과합니다."""
        approval, state, broker = self._qty10_case(
            monkeypatch, tmp_path, filled="10", remaining="0", balance_qty=10
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_missing_orders_meta_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=1,
        )
        state["orders_meta"] = {}
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("orders_meta" in p for p in problems), problems

    def test_unexpected_balance_detected(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row()],
            balance_qty=5,  # 기대=1 인데 브로커는 5
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("기대 결과" in p for p in problems), problems

    def test_same_session_missing_row_is_tolerated(self, monkeypatch, tmp_path):
        """CLOSED 이력 반영이 늦는 브로커(토스)의 같은 세션 미노출은 실패가 아닙니다."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _TZ

        today = _dt.now(_TZ("America/New_York")).date().isoformat()
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session=today,
            history=[],
            balance_qty=0,
        )
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_previous_session_missing_row_fails(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-01",
            history=[],
            balance_qty=0,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("이력에 없습니다" in p for p in problems), problems

    def test_unexecuted_approval_compares_settle_balance(self, monkeypatch, tmp_path):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[],
            balance_qty=3,
        )
        approval["consumed"] = False
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("미실행 승인" in p for p in problems), problems

    def test_widened_env_after_approval_does_not_widen_broker_price_check(
        self, monkeypatch, tmp_path
    ):
        """승인 후 env 를 넓혀도 브로커 이력 주문가 대조는 승인 정책을 넘지 못합니다."""
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(price=55.0)],
            balance_qty=1,
        )
        # 승인 정책은 ±$0.25 (50 * 0.005 / abs 0.02) — $55 는 승인 범위 밖입니다.
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "10")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0.50")
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("주문가 불일치" in p for p in problems), problems

    def test_widened_env_after_approval_still_passes_in_tolerance_history(
        self, monkeypatch, tmp_path
    ):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(price=50.2)],
            balance_qty=1,
        )
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "10")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0.50")
        assert trading_bot._verify_broker_readonly(approval, state, broker) == []

    def test_missing_approved_tolerance_policy_fails_closed(
        self, monkeypatch, tmp_path
    ):
        approval, state, broker = self._setup(
            monkeypatch, tmp_path,
            meta_session="2026-10-24",
            history=[self._filled_row(price=50.0)],
            balance_qty=1,
        )
        approval.pop("tolerance_policy", None)
        # env 를 넓혀도 승인 정책이 없으면 대조 자체를 거부합니다.
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "10")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0.50")
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("허용오차 정책" in p for p in problems), problems


class TestVerifyDeferredBaseline:
    """P2: SETTLE 이 미룬 체결 근거를 VERIFY 가 live 이력과 대조하고 기대 잔고에 반영."""

    PLAN_ID = "TQQQ-2026-10-24-run-test"

    def _setup(
        self, tmp_path, *, history, balance_qty, deferred_fills,
        evidence_balance_qty=0,
    ):
        evidence = _write_evidence(
            tmp_path,
            balance_qty=evidence_balance_qty,
            deferred_fills=deferred_fills,
        )
        approval = {
            "plan_id": self.PLAN_ID,
            "symbol": "TQQQ",
            "exchange": "NAS",
            "consumed": True,
            "orders": _orders_buy(),
            "tolerance_policy": {"price_pct": 0.005, "price_abs": 0.02, "qty": 0},
            "_evidence_path": str(evidence),
        }
        state = _base_state(
            orders_meta={
                "NEW1": {
                    "side": "BUY",
                    "total_qty": 1,
                    "plan_id": self.PLAN_ID,
                    "plan_order_index": 1,
                    "submitted_session": "2026-10-24",
                    "processed_filled_qty": 0,
                }
            }
        )
        broker = _make_broker(balance_qty=balance_qty, history=history)
        return approval, state, broker

    def _approved_fill(self):
        return {
            "odno": "NEW1",
            "ord_dt": "20261024",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-24T02:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }

    def _deferred_row(self):
        return _deferred_fill(odno="DEF1", side="매수", qty="1", amount="100")

    def _deferred_evidence(self, qty=1):
        return [
            {
                "odno": "DEF1",
                "side": "매수",
                "qty": qty,
                "session": "20261026",
                "amount": 100.0,
                "amount_confirmed": True,
            }
        ]

    def test_deferred_baseline_present_passes(self, tmp_path):
        approval, state, broker = self._setup(
            tmp_path,
            history=[self._deferred_row(), self._approved_fill()],
            balance_qty=2,
            deferred_fills=self._deferred_evidence(),
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_tampered_deferred_row_fails(self, tmp_path):
        approval, state, broker = self._setup(
            tmp_path,
            history=[self._deferred_row(), self._approved_fill()],
            balance_qty=2,
            deferred_fills=self._deferred_evidence(qty=2),
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("보류 체결" in p and "수량 불일치" in p for p in problems), problems

    def test_missing_deferred_row_fails(self, tmp_path):
        approval, state, broker = self._setup(
            tmp_path,
            history=[self._approved_fill()],
            balance_qty=1,
            deferred_fills=self._deferred_evidence(),
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "보류 체결" in p and "브로커 주문이력에 없습니다" in p
            for p in problems
        ), problems

    def test_old_evidence_without_baseline_keeps_strict_behavior(self, tmp_path):
        """deferred_fills 키가 없는 구버전 근거는 기존 엄격 동작 그대로입니다."""
        approval, state, broker = self._setup(
            tmp_path,
            history=[self._deferred_row(), self._approved_fill()],
            balance_qty=2,
            deferred_fills=None,
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("재정산 불일치" in p for p in problems), problems
        assert any("브로커 잔고가 기대 결과와 다릅니다" in p for p in problems), problems


class TestVerifyExecuteDeferredRecord:
    """EXECUTE 가 동결한 보류 체결 기록으로 VERIFY 기준선을 고정합니다."""

    def _execute_with_deferred_fill(
        self, io, tmp_path, *, deferred_qty=1, evidence_qty=None
    ):
        """SETTLE 이후·EXECUTE 이전에 도착한 실행 세션 체결을 포함해 EXECUTE 합니다.

        `evidence_qty` 를 주면 SETTLE 근거를 그 수량으로 먼저 기록합니다. EXECUTE
        시점 누적(`deferred_qty`)이 더 크면 "SETTLE 스냅샷 → EXECUTE 수용" 증가
        흐름이 됩니다.
        """
        state = _base_state(
            position_qty=0, orders_meta=_bot_order_meta("DEF1")
        )
        state, _plan, path = _approve(tmp_path, state=state)
        evidence_path = tmp_path / "settlement-TQQQ-20261024T120000Z.json"
        if evidence_qty is not None:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["balance_qty"] = 0
            evidence["deferred_fills"] = [
                {
                    "odno": "DEF1",
                    "side": "매수",
                    "qty": evidence_qty,
                    "session": "20261026",
                    "amount": 50.0 * evidence_qty,
                    "amount_confirmed": True,
                }
            ]
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

        io["load"]("TQQQ", deepcopy(state))
        deferred = _deferred_fill(
            odno="DEF1",
            side="매수",
            qty=str(deferred_qty),
            amount=str(50 * deferred_qty),
        )
        broker = _make_broker(balance_qty=deferred_qty, history=[deferred])
        broker.place_order.return_value = OrderResult(
            order_id="EXEC1",
            order_time="2026-10-26T09:30:00+09:00",
            is_reservation=False,
        )
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert result["outcome"] == "executed"
        assert broker.place_order.call_count == 1

        approval = pipeline.load_approval(str(path))
        approval["_evidence_path"] = str(evidence_path)
        verification_state = deepcopy(io["saved"][-1])
        verification_history = [deferred, self._approved_fill()]
        verification_broker = _make_broker(
            balance_qty=deferred_qty + 1, history=verification_history
        )
        return approval, verification_state, verification_broker, path

    def _approved_fill(self):
        return {
            "odno": "EXEC1",
            "ord_dt": "20261026",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-26T08:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }

    def test_pre_execute_fill_recorded_and_verify_passes(self, io, tmp_path):
        approval, state, broker, path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        record_path = Path(str(path) + ".deferred.json")
        assert record_path.exists(), "EXECUTE 는 수용한 보류 체결 집합을 동결해야 합니다"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["deferred_fills"] == [
            {
                "odno": "DEF1",
                "side": "매수",
                "qty": 1,
                "session": "20261026",
                "amount": 50.0,
                "amount_confirmed": True,
            }
        ]
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_settle_to_execute_growth_uses_frozen_baseline(self, io, tmp_path):
        """SETTLE 근거 1주 → EXECUTE 동결 2주. VERIFY 는 동결 수량을 기준선으로 씁니다."""
        approval, state, broker, path = self._execute_with_deferred_fill(
            io, tmp_path, deferred_qty=2, evidence_qty=1
        )
        record = json.loads(
            Path(str(path) + ".deferred.json").read_text(encoding="utf-8")
        )
        assert record["deferred_fills"][0]["qty"] == 2, (
            "EXECUTE 는 SETTLE 이후 늘어난 누적 체결을 동결해야 합니다"
        )

        # 기대잔고 = SETTLE 0 + 동결 2 + 승인주문 체결 1 = 3.
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_growth_after_execute_fails(self, io, tmp_path):
        """EXECUTE 동결 이후 누적이 3주로 늘면 소급 허용하지 않고 실패합니다."""
        approval, state, _broker, _path = self._execute_with_deferred_fill(
            io, tmp_path, deferred_qty=2, evidence_qty=1
        )
        grown = _deferred_fill(odno="DEF1", side="매수", qty="3", amount="150")
        broker = _make_broker(
            balance_qty=4, history=[grown, self._approved_fill()]
        )

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "EXECUTE 보류 체결" in p and "수량 불일치" in p for p in problems
        ), problems

    def test_frozen_live_mismatch_fails(self, io, tmp_path):
        """동결 기록은 2주인데 live 이력이 1주면 (되돌림/변조) 실패합니다."""
        approval, state, _broker, _path = self._execute_with_deferred_fill(
            io, tmp_path, deferred_qty=2, evidence_qty=1
        )
        rolled_back = _deferred_fill(
            odno="DEF1", side="매수", qty="1", amount="50"
        )
        broker = _make_broker(
            balance_qty=2, history=[rolled_back, self._approved_fill()]
        )

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "EXECUTE 보류 체결" in p and "수량 불일치" in p for p in problems
        ), problems

    def test_execute_record_live_session_mismatch_fails(self, io, tmp_path):
        """동결 기록 세션(20261026)과 live 이력 세션(20261027)이 다르면 실패합니다."""
        approval, state, _broker, path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        record = json.loads(
            Path(str(path) + ".deferred.json").read_text(encoding="utf-8")
        )
        assert record["deferred_fills"][0]["session"] == "20261026"

        moved = _deferred_fill(
            odno="DEF1", side="매수", qty="1", amount="50", ord_dt="20261027"
        )
        broker = _make_broker(
            balance_qty=2, history=[moved, self._approved_fill()]
        )

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "EXECUTE 보류 체결" in p
            and "세션 불일치" in p
            and "기록=20261026" in p
            and "이력=20261027" in p
            for p in problems
        ), problems

    def test_execute_record_live_session_match_passes(self, io, tmp_path):
        """live 이력 세션이 동결 기록/실행 세션과 같으면 통과합니다."""
        approval, state, broker, _path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_execute_record_live_session_unparseable_fails(self, io, tmp_path):
        """live 이력 행의 세션을 해석할 수 없으면 fail-closed 입니다."""
        approval, state, _broker, _path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        undated = _deferred_fill(
            odno="DEF1", side="매수", qty="1", amount="50"
        )
        for key in ("ord_dt", "ord_tmd", "ord_datetime_utc"):
            undated.pop(key, None)
        broker = _make_broker(
            balance_qty=2, history=[undated, self._approved_fill()]
        )

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "EXECUTE 보류 체결" in p and "이력 세션을 해석할 수 없습니다" in p
            for p in problems
        ), problems

    def test_evidence_and_execute_record_overlap_not_double_counted(
        self, io, tmp_path
    ):
        """같은 odno 가 SETTLE 근거와 EXECUTE 기록에 모두 있어도 1회만 계산합니다."""
        approval, state, broker, _path = self._execute_with_deferred_fill(io, tmp_path)
        evidence_path = Path(str(approval["_evidence_path"]))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["deferred_fills"] = [
            {
                "odno": "DEF1",
                "side": "매수",
                "qty": 1,
                "session": "20261026",
                "amount": 50.0,
                "amount_confirmed": True,
            }
        ]
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert problems == [], problems

    def test_tampered_execute_record_fails(self, io, tmp_path):
        approval, state, broker, path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        record_path = Path(str(path) + ".deferred.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["deferred_fills"][0]["qty"] = 2
        record_path.write_text(json.dumps(record), encoding="utf-8")

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any(
            "EXECUTE 보류 체결" in p and "수량 불일치" in p
            for p in problems
        ), problems

    def test_missing_execute_record_keeps_legacy_strictness(self, io, tmp_path):
        approval, state, broker, path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        Path(str(path) + ".deferred.json").unlink()

        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("재정산 불일치" in p for p in problems), problems
        assert any("브로커 잔고가 기대 결과와 다릅니다" in p for p in problems), problems

    def test_fill_after_execute_fails(self, io, tmp_path):
        approval, state, _broker, path = self._execute_with_deferred_fill(
            io, tmp_path
        )
        # EXECUTE 기록에도 승인 계획에도 없는, EXECUTE 이후 도착한 체결.
        late_fill = _deferred_fill(odno="LATE1", side="매수", qty="1", amount="50")
        broker = _make_broker(
            balance_qty=3,
            history=[
                _deferred_fill(odno="DEF1", side="매수", qty="1", amount="50"),
                self._approved_fill(),
                late_fill,
            ],
        )
        problems = trading_bot._verify_broker_readonly(approval, state, broker)
        assert any("재정산 불일치" in p for p in problems), problems


class TestVerifyExecutedOutcome:
    """P1-C: 정상 실행은 명시적 executed 기록을 남겨 VERIFY 가 성공 판정합니다."""

    def test_clean_execution_records_executed_and_verify_passes(
        self, io, monkeypatch, tmp_path
    ):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)

        broker = _make_broker()
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert result["outcome"] == "executed"
        record = json.loads(
            Path(str(path) + ".recovery.json").read_text(encoding="utf-8")
        )
        assert record["outcome"] == "executed"

        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 0

    def test_partial_failure_is_not_recorded_as_success(
        self, io, monkeypatch, tmp_path
    ):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.chdir(tmp_path)
        broker = _make_broker()
        broker.place_order.side_effect = trading_bot.OrderNotAcceptedError("거부")
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert result["outcome"] == "execution-failed"
        record = json.loads(
            Path(str(path) + ".recovery.json").read_text(encoding="utf-8")
        )
        assert record["outcome"] == "execution-failed"

    def test_consumed_without_record_is_verification_failure(
        self, io, monkeypatch, tmp_path, capsys
    ):
        """소비됐지만 결과 기록이 없으면 성공(exit 0)이 아니라 검증 실패(exit 1)."""
        state, _plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1
        out = capsys.readouterr().out
        assert "execution-failed" in out
        assert "실행 결과 기록" in out


class TestOpsCronHandoff:
    """Ops: PLAN_ID/APPROVAL_PLAN_ID 는 실행마다 달라지는 수동 전달 값입니다."""

    def test_cron_documents_dynamic_plan_id_handoff(self):
        cron = (_repo_root / "ops" / "cron.example").read_text(encoding="utf-8")
        assert "PREVIEW" in cron
        assert "PLAN_ID=<PREVIEW plan_id>" in cron
        assert "APPROVAL_PLAN_ID=<plan_id>" in cron
        assert "RUN_PHASE=VERIFY" in cron
        # 주석 상태로만 유지 (자동 cron 활성화 금지)
        for line in cron.splitlines():
            stripped = line.strip()
            if "RUN_PHASE=" in stripped:
                assert stripped.startswith("#"), line

    def test_cron_documents_morning_settle_and_1830_execute(self):
        cron = (_repo_root / "ops" / "cron.example").read_text(encoding="utf-8")
        assert "5 10  * * 1-5 RUN_PHASE=SETTLE" in cron
        assert "30 18 * * 1-5 RUN_PHASE=EXECUTE" in cron
        assert "45 18 * * 1-5 RUN_PHASE=VERIFY" in cron
        # 세션 분리(아침 정산 대상 ≠ 실행 대상)와 계좌 없는 브로커 바인딩 문서화
        assert "다음 세션" in cron
        assert "MERITZ_ACCOUNT_BINDING" in cron
        assert "ACCOUNT_FINGERPRINT_SECRET" in cron
        assert "RUN_LOCK_HELD" in cron
        assert "AFTER_HOURS_END_ET_HOUR" in cron

    def test_launcher_distinguishes_approval_skipped_alert(self):
        """HOLD 6: exit 2(승인만 됨)와 exit 1(실행 실패) 알림 문구를 구분."""
        launcher = (_repo_root / "ops" / "run_daily.sh").read_text(encoding="utf-8")
        cron = (_repo_root / "ops" / "cron.example").read_text(encoding="utf-8")
        assert 'if [[ "$rc" == "2" ]]' in launcher
        assert "승인만 됨" in launcher
        assert "실행 실패 아님" in launcher
        assert "자동매매 실행 실패" in launcher
        assert "2=승인만 되고 미실행" in cron
        assert "실행 실패(exit 1)와 다른 문구" in cron


# ── Round-4 HOLD 회귀 테스트 ─────────────────────────────────────


class TestExecuteTransmitsRebuiltPlan:
    """HOLD 1: EXECUTE 는 승인 계획(게이트)이 아니라 **재계산 계획**을 전송합니다."""

    def _price_strategy(self, price):
        def _strategy(broker, **kwargs):
            result = _fake_strategy(broker, **kwargs)
            result["orders"] = _orders_buy(price)
            return result

        return _strategy

    def test_place_order_receives_rebuilt_values(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        assert approval["orders"][0]["price"] == 50.0
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.chdir(tmp_path)
        # 승인 후 시세가 살짝 움직여 허용오차(±$0.25) 안에서 드리프트합니다.
        monkeypatch.setattr(trading_bot, "무한매수법_V4", self._price_strategy(50.01))

        broker = _make_broker()
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )

        assert result["outcome"] == "executed"
        assert broker.place_order.call_count == 1
        sent_price = float(broker.place_order.call_args.args[4])
        assert sent_price == pytest.approx(50.01), (
            "place_order 는 stale 승인값(50.0)이 아니라 재계산값(50.01)을 받아야 합니다"
        )

        transmitted_path = Path(str(path) + ".transmitted.json")
        assert transmitted_path.exists()
        transmitted = json.loads(transmitted_path.read_text(encoding="utf-8"))
        assert transmitted["orders"][0]["price"] == pytest.approx(50.01)
        assert transmitted["approval_plan_hash"] == approval["plan_hash"]
        assert transmitted["source"] == "rebuilt"

    def test_drift_beyond_tolerance_blocks_transmission(self, io, monkeypatch, tmp_path):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "무한매수법_V4", self._price_strategy(60.0))
        broker = _make_broker()
        with pytest.raises(RuntimeError, match="허용 오차"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False
        assert not Path(str(path) + ".transmitted.json").exists()


class TestPreviewImmutableIds:
    """HOLD 2: PREVIEW 1회당 고유 계획 파일. APPROVE 는 확인한 id+내용만 승인."""

    def test_second_preview_does_not_overwrite_first(self, io, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state()
        io["load"]("TQQQ", state)
        _write_evidence(tmp_path)
        settled = trading_bot._settled_from_state(_symbol_config())
        broker = ReadOnlyBroker(_make_broker())

        plan1 = pipeline.build_order_plan(settled, broker)
        path1 = pipeline.write_order_plan(plan1, plans_dir=str(tmp_path / "plans"))
        first_content = Path(path1).read_text(encoding="utf-8")

        plan2 = pipeline.build_order_plan(settled, broker)
        path2 = pipeline.write_order_plan(plan2, plans_dir=str(tmp_path / "plans"))

        assert path1 != path2, "두 번째 PREVIEW 가 첫 계획 파일을 덮어쓰면 안 됩니다"
        assert plan1.plan_id != plan2.plan_id
        assert Path(path1).read_text(encoding="utf-8") == first_content
        assert Path(path2).exists()

    def test_approve_of_id_a_approves_a_content(self, io, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state()
        io["load"]("TQQQ", state)
        _write_evidence(tmp_path)
        plans_dir = str(tmp_path / "plans")
        plan_a = _make_plan(state, orders=_orders_buy(50.0))
        plan_b = _make_plan(state, orders=_orders_buy(51.0))
        pipeline.write_order_plan(plan_a, plans_dir=plans_dir)
        pipeline.write_order_plan(plan_b, plans_dir=plans_dir)

        approval_path = pipeline.approve_plan_file(
            plan_a.plan_id,
            approvals_dir=str(tmp_path / "approvals"),
            plans_dir=plans_dir,
            now=_fixed_now(),
        )
        approval = pipeline.load_approval(str(approval_path))
        assert approval["plan_id"] == plan_a.plan_id
        assert approval["orders"][0]["price"] == 50.0

    def test_write_order_plan_refuses_overwrite(self, io, tmp_path):
        state = _base_state()
        plan = _make_plan(state)
        pipeline.write_order_plan(plan, plans_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="덮어쓰기 금지"):
            pipeline.write_order_plan(plan, plans_dir=str(tmp_path))


class TestSessionSeparation:
    """HOLD 3: 정산 대상(settled) 세션과 실행 대상(execution) 세션을 분리."""

    KST = ZoneInfo("Asia/Seoul")

    def test_kst_morning_preview_and_evening_execute_share_session(self):
        preview_now = datetime(2026, 10, 27, 11, 0, tzinfo=self.KST)  # ET 월요일 22:00
        execute_now = datetime(2026, 10, 27, 18, 30, tzinfo=self.KST)  # ET 화요일 05:30
        assert pipeline._et_today(preview_now) == "2026-10-26"
        assert pipeline._et_today(execute_now) == "2026-10-27"
        # 두 시각의 세션 계산이 동일해야 합니다 (ET 날짜가 달라도).
        assert pipeline.settlement_sessions(preview_now) == ("2026-10-26", "2026-10-27")
        assert pipeline.settlement_sessions(execute_now) == ("2026-10-26", "2026-10-27")

        state = _base_state(
            settled_session="2026-10-26", execution_session="2026-10-27"
        )
        plan = pipeline.OrderPlan(
            plan_id="",
            symbol="TQQQ",
            exchange="NAS",
            session="",
            orders=_orders_buy(),
            state_hash=canonical_state_hash(state),
            plan_hash="",
            settlement_run_id="run-test",
            created_at_utc="",
            expires_at_utc="",
        )
        pipeline._prepare_plan_identity(plan, now=preview_now)
        assert plan.session == "2026-10-27", "계획 세션은 실행 대상 세션이어야 합니다"
        assert pipeline.verify_approval(plan.to_approval(), state, now=execute_now) == []

    def test_weekend_settles_friday_and_executes_monday(self):
        # KST 일요일 11:00 = ET 토요일 22:00 → _et_today 는 토요일이지만,
        # 정산 대상은 금요일 마감 세션이고 실행 대상은 월요일입니다.
        sunday_morning = datetime(2026, 10, 25, 11, 0, tzinfo=self.KST)
        assert pipeline._et_today(sunday_morning) == "2026-10-24"
        assert pipeline.settlement_sessions(sunday_morning) == (
            "2026-10-23",
            "2026-10-26",
        )

    def test_holiday_is_skipped(self):
        # 2026-11-26 은 추수감사절 휴장 → 다음 세션은 11-27(금).
        after_thanksgiving = datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc)
        assert pipeline.settlement_sessions(after_thanksgiving) == (
            "2026-11-25",
            "2026-11-27",
        )
        # 2026-12-25(금) 크리스마스 → 다음 세션은 12-28(월).
        christmas_evening = datetime(2026, 12, 26, 1, 0, tzinfo=timezone.utc)
        assert pipeline.settlement_sessions(christmas_evening) == (
            "2026-12-24",
            "2026-12-28",
        )

    def test_dst_boundaries(self):
        # 2026-11-01 서머타임 종료(EDT→EST). 금요일 마감 → 월요일 세션.
        after_fall_back = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)
        assert pipeline.settlement_sessions(after_fall_back) == (
            "2026-10-30",
            "2026-11-02",
        )
        # 2026-03-08 서머타임 시작(EST→EDT). 금요일 마감 → 월요일 세션.
        after_spring_forward = datetime(2026, 3, 9, 1, 0, tzinfo=timezone.utc)
        assert pipeline.settlement_sessions(after_spring_forward) == (
            "2026-03-06",
            "2026-03-09",
        )

    def test_stale_approval_session_is_rejected(self):
        state = _base_state(
            settled_session="2026-10-23", execution_session="2026-10-26"
        )
        plan = pipeline.OrderPlan(
            plan_id="",
            symbol="TQQQ",
            exchange="NAS",
            session="2026-10-23",  # 이전 세션 승인
            orders=_orders_buy(),
            state_hash=canonical_state_hash(state),
            plan_hash="",
            settlement_run_id="run-test",
            created_at_utc="",
            expires_at_utc="",
        )
        pipeline._prepare_plan_identity(
            plan, now=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        )
        approval = plan.to_approval()
        problems = pipeline.verify_approval(
            approval,
            state,
            now=datetime(2026, 10, 27, 18, 30, tzinfo=self.KST),
        )
        assert any("세션 불일치" in p for p in problems), problems


class TestSettleGates:
    """HOLD 4: SETTLE 은 대상 세션 마감 + 실잔고 조회 성공이 필수, 보류=실패."""

    def test_balance_failure_halts_and_does_not_save(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[buyer])
        broker.get_balance.side_effect = RuntimeError("balance api down")

        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker),
            _symbol_config(),
            now=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
        )

        assert settled.halt_reason == "balance_unavailable"
        assert io["saved"] == [], "잔고 미확인 SETTLE 은 저장하면 안 됩니다"

    def test_dispatch_settle_counts_halt_as_failure(self, io, monkeypatch):
        monkeypatch.setattr(trading_bot, "SYMBOLS", [_symbol_config()])
        monkeypatch.setattr(
            trading_bot,
            "settle_positions",
            lambda broker, config: pipeline.SettledState(
                state={},
                symbol=config["symbol"],
                exchange=config["exchange"],
                splits=config["splits"],
                symbol_type=config["symbol_type"],
                seed=config["seed"],
                effective_seed=0.0,
                T=0.0,
                halt_reason="history_not_settled",
            ),
        )
        assert trading_bot._dispatch_pipeline_phase("SETTLE", _make_broker()) == 1

    def test_settle_uses_closed_session_not_today(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[buyer])
        broker.is_trading_day.return_value = False  # 오늘(ET 토요일)은 휴장

        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker),
            _symbol_config(),
            now=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
        )

        assert settled.halt_reason is None, (
            "휴장일이어도 직전 마감 세션을 정산해야 합니다 (오늘 영업일 여부와 무관)"
        )
        assert settled.settled_session == "2026-10-23"
        assert settled.session == "2026-10-26"
        assert len(io["saved"]) == 1
        assert io["saved"][0]["settled_session"] == "2026-10-23"
        assert io["saved"][0]["execution_session"] == "2026-10-26"


class TestEvidenceFinalConsistency:
    """HOLD 5: 후처리(MAX_T/사이클 종료)를 기록하고 최종 후보를 검증."""

    def test_max_t_records_post_processing(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        config = _symbol_config()
        config["max_t"] = 1.0
        settled = pipeline.settle_positions(
            ReadOnlyBroker(_make_broker(balance_qty=10, history=[buyer])), config
        )
        assert settled.T == pytest.approx(1.0)
        evidence = json.loads(Path(settled.evidence_path).read_text(encoding="utf-8"))
        post = evidence["post_processing"]
        assert post["applied"] is True
        assert any(step["step"] == "max_t" for step in post["steps"])
        assert post["final_candidate"]["T"] == pytest.approx(1.0)

    def test_final_candidate_verification_blocks_save(self, io, monkeypatch):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(
            pipeline,
            "verify_evidence_file_with_ops",
            lambda *a, **k: (False, ["강제 최종 후보 불일치"]),
        )
        with pytest.raises(trading_bot.SymbolScopedError, match="최종 저장 후보"):
            pipeline.settle_positions(
                ReadOnlyBroker(_make_broker(balance_qty=10, history=[buyer])),
                _symbol_config(),
            )
        assert io["saved"] == [], "최종 후보 검증 실패 시 저장하면 안 됩니다"

    def test_cycle_end_seed_change_is_verified(self, tmp_path):
        ops = pipeline._load_verify_run_module()
        evidence = {
            "symbol": "TQQQ",
            "run_id": "run-cycle",
            "mode": "LIVE",
            "broker": "mockbroker",
            "broker_mode": "demo",
            "evidence_version": 2,
            "settlement_mode": "incremental",
            "T_before": 2.0,
            "net_invested_before": 500.0,
            "T_after": 2.0,
            "net_invested_after": 500.0,
            "ledger_before": {"start_T": 2.0, "entries": []},
            "ledger_after": {"start_T": 2.0, "entries": []},
            "orders": [],
            # 사이클 시드는 원시 체결로 독립 재계산됩니다.
            # buy 10 @ $50, sell 10 @ $60, commission 0 → +$100 → 1100.
            "fills": [
                {
                    "odno": "B1",
                    "side": "매수",
                    "qty": 10,
                    "price": 50.0,
                    "ord_dt": "20260105",
                    "ts": "2026-01-05T15:00:00+00:00",
                },
                {
                    "odno": "S1",
                    "side": "매도",
                    "qty": 10,
                    "price": 60.0,
                    "ord_dt": "20260120",
                    "ts": "2026-01-20T15:00:00+00:00",
                },
            ],
            "max_t": 20,
            "post_processing": {
                "applied": True,
                "steps": [
                    {
                        "step": "cycle_end",
                        "cycle_start": "2026-01-01",
                        "cycle_end": "2026-01-20",
                        "seed": 1000.0,
                        "commission_rate": 0.0,
                        "new_cycle_started": False,
                        "reinvest": True,
                        "next_cycle_seed": 1100.0,
                    }
                ],
                "final_candidate": {
                    "T": 0.0,
                    "net_invested": 0.0,
                    "effective_seed": 1100.0,
                    "cycle_start_date": "",
                },
            },
        }
        path = tmp_path / "settlement-TQQQ-cycle.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")

        good_state = {
            "T": 0.0,
            "net_invested": 0.0,
            "effective_seed": 1100.0,
            "cycle_start_date": "",
        }
        _ok, problems = ops.verify_evidence_file(
            path,
            broker="mockbroker",
            mode="LIVE",
            run_id="run-cycle",
            broker_mode="demo",
            final_state=good_state,
        )
        assert not any("최종 저장" in p or "effective_seed 불일치" in p for p in problems), problems
        assert not any("최종 후보 스냅샷" in p for p in problems), problems

        bad_state = dict(good_state, effective_seed=999.0)
        _ok, problems = ops.verify_evidence_file(
            path,
            broker="mockbroker",
            mode="LIVE",
            run_id="run-cycle",
            broker_mode="demo",
            final_state=bad_state,
        )
        assert any("effective_seed 불일치" in p for p in problems), problems


class TestGlobalLock:
    """HOLD 6: 셸/파이썬이 같은 .run.lock 으로 state 변경을 직렬화."""

    def test_reentrant_same_process(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
        monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
        with pipeline.global_lock(timeout_seconds=1):
            with pipeline.global_lock(timeout_seconds=1):
                pass  # 같은 프로세스 재진입은 교착하지 않아야 합니다

    def test_cross_process_exclusion(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
        lock_path = tmp_path / ".run.lock"
        monkeypatch.setenv("RUN_LOCK_PATH", str(lock_path))
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_src_path)!r})\n"
            "import pipeline\n"
            "try:\n"
            "    with pipeline.global_lock(timeout_seconds=0.3):\n"
            "        print('acquired')\n"
            "except TimeoutError:\n"
            "    print('busy')\n"
            "    sys.exit(7)\n"
        )
        env = dict(os.environ)
        env.pop("RUN_LOCK_HELD", None)
        env["RUN_LOCK_PATH"] = str(lock_path)

        with pipeline.global_lock(timeout_seconds=2):
            busy = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(tmp_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        assert busy.returncode == 7, busy.stdout + busy.stderr
        assert "busy" in busy.stdout

        free = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert free.returncode == 0, free.stdout + free.stderr
        assert "acquired" in free.stdout

    def test_run_lock_held_env_bypasses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUN_LOCK_HELD", "1")
        monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
        with pipeline.global_lock(timeout_seconds=1):
            pass


class TestVerifyExitCodes:
    """HOLD 7: 실행 실패/기록 누락은 검증 실패(nonzero), 승인만 됨은 별도 코드."""

    def test_approval_skipped_without_problems_returns_2(
        self, io, monkeypatch, tmp_path
    ):
        state, _plan, path = _approve(tmp_path)
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 2


class TestMeritzAccountBinding:
    """HOLD 8: 계좌번호 없는 브로커도 승인에 계좌 바인딩(비민감)을 남깁니다."""

    def _as_meritz(self, monkeypatch, cfg=None):
        import config as runtime_config

        monkeypatch.setattr(trading_bot, "BROKER", "meritz")
        monkeypatch.setattr(trading_bot, "BROKER_MODE", "real")
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(runtime_config, "BROKER_CONFIG", cfg or {})
        for name in (
            "MERITZ_ACCOUNT_BINDING",
            pipeline.ACCOUNT_BINDING_ENV,
            pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV,
        ):
            monkeypatch.delenv(name, raising=False)

    def test_meritz_without_binding_is_rejected(self, io, monkeypatch):
        self._as_meritz(monkeypatch, cfg={})
        with pytest.raises(RuntimeError, match="계좌 바인딩"):
            pipeline._broker_identity()

    def test_meritz_explicit_binding_is_hashed(self, io, monkeypatch):
        self._as_meritz(monkeypatch, cfg={})
        monkeypatch.setenv("MERITZ_ACCOUNT_BINDING", "meritz-main")
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, "server-secret")
        identity = pipeline._broker_identity()
        assert identity["account_id"].startswith("hmac-sha256:")
        assert identity["account_display"] == "****main"
        assert "meritz-main" not in identity["account_id"]

    def test_meritz_credential_fingerprint_never_leaks_secret(self, io, monkeypatch):
        self._as_meritz(
            monkeypatch,
            cfg={"app_key": "AK-123", "app_secret": "SUPER-SECRET-VALUE"},
        )
        monkeypatch.setenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, "server-secret")
        identity = pipeline._broker_identity()
        assert identity["account_id"].startswith("hmac-sha256:")
        assert identity["account_display"] == "meritz/real"
        dumped = json.dumps(identity)
        assert "SUPER-SECRET-VALUE" not in dumped
        assert "AK-123" not in dumped

    def test_other_accountless_broker_keeps_legacy_empty_binding(
        self, io, monkeypatch
    ):
        import config as runtime_config

        monkeypatch.setattr(trading_bot, "BROKER", "ls")
        monkeypatch.setattr(trading_bot, "BROKER_MODE", "real")
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(
            runtime_config, "BROKER_CONFIG", {"app_key": "K", "app_secret": "S"}
        )
        monkeypatch.delenv(pipeline.ACCOUNT_FINGERPRINT_SECRET_ENV, raising=False)
        monkeypatch.delenv(pipeline.ACCOUNT_BINDING_ENV, raising=False)
        monkeypatch.delenv("LS_ACCOUNT_BINDING", raising=False)
        assert pipeline._broker_identity()["account_id"] == ""


# ── Round-5 HOLD 회귀 테스트 ─────────────────────────────────────


class TestAfterHoursSettlementGate:
    """HOLD 1: 정규장 마감과 애프터아워 종료를 분리해 정산 시각을 검증."""

    KST = ZoneInfo("Asia/Seoul")

    def test_winter_kst_0930_is_before_after_hours_end(self):
        # 2026-12-01 09:30 KST = 2026-11-30 19:30 EST (정규장 마감 후, 애프터아워 전).
        from broker.market_utils import (
            us_session_after_hours_end,
            us_session_last_closed_date,
        )

        now = datetime(2026, 12, 1, 9, 30, tzinfo=self.KST)
        assert us_session_last_closed_date(now) == "2026-11-30"
        assert us_session_after_hours_end("2026-11-30", now) is False
        # 정산 대상은 직전 정규장 마감 세션이고 실행 대상은 다음 세션입니다.
        assert pipeline.settlement_sessions(now) == ("2026-11-30", "2026-12-01")

    def test_et_1601_is_regular_closed_but_after_hours_running(self):
        from broker.market_utils import (
            us_session_after_hours_end,
            us_session_closed,
        )

        et = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 30, 16, 1, tzinfo=et)
        assert us_session_closed("2026-11-30", now) is True
        assert us_session_after_hours_end("2026-11-30", now) is False

    def test_just_before_and_after_after_hours_end(self, monkeypatch):
        from broker.market_utils import us_session_after_hours_end

        monkeypatch.delenv("AFTER_HOURS_END_ET_HOUR", raising=False)
        et = ZoneInfo("America/New_York")
        before = datetime(2026, 11, 30, 19, 59, tzinfo=et)
        after = datetime(2026, 11, 30, 20, 0, tzinfo=et)
        assert us_session_after_hours_end("2026-11-30", before) is False
        assert us_session_after_hours_end("2026-11-30", after) is True

        monkeypatch.setenv("AFTER_HOURS_END_ET_HOUR", "17")
        at_17 = datetime(2026, 11, 30, 17, 0, tzinfo=et)
        assert us_session_after_hours_end("2026-11-30", at_17) is True

    def test_settle_halts_before_after_hours_end(self, io):
        state, _buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=10, history=[])
        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker),
            _symbol_config(),
            now=datetime(2026, 12, 1, 9, 30, tzinfo=self.KST),
        )
        assert settled.halt_reason == "after_hours_not_ended"
        assert io["saved"] == [], "애프터아워 종료 전 SETTLE 은 저장하면 안 됩니다"

    def test_in_progress_session_fills_are_never_finalized(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        in_progress_fill = dict(buyer)
        in_progress_fill.update(
            {
                "odno": "TODAY1",
                "ord_dt": "20261201",
                "ord_datetime_utc": "2026-12-01T16:00:00+00:00",
                "ft_ccld_qty": "1",
                "ft_ccld_amt3": "60",
                "ft_ccld_unpr3": "60",
            }
        )
        broker = _make_broker(balance_qty=11, history=[buyer, in_progress_fill])
        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker),
            _symbol_config(),
            now=datetime(2026, 12, 1, 11, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        assert settled.halt_reason == "in_progress_session_fills"
        assert io["saved"] == []

    def test_in_progress_session_is_never_the_settle_target(self):
        from broker.market_utils import us_session_in_progress

        et = ZoneInfo("America/New_York")
        # 2026-12-01(화) 11:00 ET — 정규장 진행 중.
        now = datetime(2026, 12, 1, 11, 0, tzinfo=et)
        assert us_session_in_progress(now) == "2026-12-01"
        settled, execution = pipeline.settlement_sessions(now)
        assert settled == "2026-11-30", "진행 중 세션의 체결을 정산 대상으로 삼으면 안 됩니다"
        assert execution == "2026-12-01"
        assert settled != us_session_in_progress(now)

    @pytest.mark.parametrize("hour", [5, 8])
    def test_execution_session_premarket_fills_are_deferred_then_settled(
        self, io, hour
    ):
        """실행 세션 프리마켓(ET 05:00/08:00) 체결은 SETTLE 을 막지 않고 **미룹니다**.

        실행 세션은 아직 마감되지 않았으므로, 그 체결을 직전 세션 정산에 확정하면
        안 됩니다. 대신 T/순투입/원장에서 제외하고 워터마크를 그 앞에 멈춘 뒤,
        그 세션의 **다음 SETTLE** 에서 다시 반영해야 합니다.
        """
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        premarket_fill = dict(buyer)
        premarket_fill.update(
            {
                "odno": f"PREMARKET{hour}",
                "ord_dt": "20261202",
                "ord_tmd": f"{hour:02d}0500",
                "ord_datetime_utc": "2026-12-02T10:05:00+00:00",
                "ft_ccld_qty": "1",
                "ft_ccld_amt3": "60",
                "ft_ccld_unpr3": "60",
            }
        )
        now = datetime(
            2026, 12, 2, hour, 0, tzinfo=ZoneInfo("America/New_York")
        )
        # 전제 확인: 이 시각의 정산 대상은 12/1, 실행 대상은 12/2, 정규장 진행 중 아님.
        from broker.market_utils import us_session_in_progress

        assert pipeline.settlement_sessions(now) == ("2026-12-01", "2026-12-02")
        assert us_session_in_progress(now) is None
        broker = _make_broker(balance_qty=11, history=[buyer, premarket_fill])
        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker), _symbol_config(), now=now
        )

        # 1) SETTLE 은 진행되고, 미룬 체결은 T/순투입/원장에 반영되지 않습니다.
        assert settled.halt_reason is None
        assert settled.T == pytest.approx(2.0)
        assert settled.state["net_invested"] == pytest.approx(500.0)
        assert settled.state["last_updated"] == state["last_updated"], (
            "미룬 체결 너머로 워터마크가 전진하면 다음 정산에서 누락됩니다"
        )
        assert settled.state["position_qty"] == 10, (
            "잔고(11주)에서 미룬 순변동(+1주)을 되감아 정산 세션 시점 보유량이어야 합니다"
        )
        assert len(io["saved"]) == 1
        evidence = json.loads(Path(settled.evidence_path).read_text(encoding="utf-8"))
        assert f"PREMARKET{hour}" not in {
            fill["odno"] for fill in evidence["fills"]
        }, "미룬 체결은 이번 정산 근거에 포함되면 안 됩니다"
        assert evidence["deferred_fills"] == [
            {
                "odno": f"PREMARKET{hour}",
                "side": "매수",
                "qty": 1,
                "session": "20261202",
                "amount": 60.0,
                "amount_confirmed": True,
            }
        ], "미룬 체결은 근거(deferred_fills)에 남아 VERIFY 기준선으로 쓰여야 합니다"

        # 2) 그 세션의 다음 SETTLE 이 미룬 체결을 정상적으로 집어갑니다.
        io["load"]("TQQQ", deepcopy(settled.state))
        next_settle_now = datetime(
            2026, 12, 3, 5, 0, tzinfo=ZoneInfo("America/New_York")
        )
        assert pipeline.settlement_sessions(next_settle_now)[0] == "2026-12-02"
        settled_next = pipeline.settle_positions(
            ReadOnlyBroker(
                _make_broker(balance_qty=11, history=[buyer, premarket_fill])
            ),
            _symbol_config(),
            now=next_settle_now,
        )
        assert settled_next.halt_reason is None
        assert settled_next.state["net_invested"] == pytest.approx(560.0)
        assert settled_next.T == pytest.approx(2.5)
        assert settled_next.state["last_updated"] == premarket_fill["ord_datetime_utc"]
        evidence_next = json.loads(
            Path(settled_next.evidence_path).read_text(encoding="utf-8")
        )
        assert f"PREMARKET{hour}" in {
            fill["odno"] for fill in evidence_next["fills"]
        }, "다음 SETTLE 이 미룬 체결을 반영해야 합니다"
        assert evidence_next["deferred_fills"] == [], (
            "반영된 체결은 더 이상 보류 목록에 남으면 안 됩니다"
        )

    def test_fills_after_execution_session_halt_without_saving(self, io):
        """**실행 세션보다 뒤인** 세션 체결은 미래/범위 밖이므로 저장 없이 보류."""
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        future_fill = dict(buyer)
        future_fill.update(
            {
                "odno": "FUTURE1",
                "ord_dt": "20261203",
                "ord_tmd": "050000",
                "ord_datetime_utc": "2026-12-03T10:05:00+00:00",
                "ft_ccld_qty": "1",
                "ft_ccld_amt3": "60",
                "ft_ccld_unpr3": "60",
            }
        )
        now = datetime(2026, 12, 2, 5, 0, tzinfo=ZoneInfo("America/New_York"))
        assert pipeline.settlement_sessions(now) == ("2026-12-01", "2026-12-02")
        broker = _make_broker(balance_qty=11, history=[buyer, future_fill])
        settled = pipeline.settle_positions(
            ReadOnlyBroker(broker), _symbol_config(), now=now
        )
        assert settled.halt_reason == "post_settled_session_fills"
        assert io["saved"] == [], "실행 세션 이후 체결이 있으면 SETTLE 은 저장하면 안 됩니다"


class TestReVerifyAfterWait:
    """HOLD 2: 대기가 필요하면 **먼저** 대기하고 전체 게이트를 다시 통과."""

    def _price_strategy(self, price):
        def _strategy(broker, **kwargs):
            result = _fake_strategy(broker, **kwargs)
            result["orders"] = _orders_buy(price)
            return result

        return _strategy

    def _prepare(self, io, monkeypatch, tmp_path):
        state, plan, path = _approve(tmp_path)
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        return path

    def test_wait_happens_before_claim_and_transmit(self, io, monkeypatch, tmp_path):
        path = self._prepare(io, monkeypatch, tmp_path)
        observed = {}

        def fake_wait(tb, now=None):
            observed["consumed"] = pipeline.load_approval(str(path)).get("consumed")
            observed["transmitted"] = Path(str(path) + ".transmitted.json").exists()
            observed["orders"] = 0
            return _fixed_now()

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        broker = _make_broker()
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert result["outcome"] == "executed"
        assert observed["consumed"] is False, "대기 전에 승인을 소비하면 안 됩니다"
        assert observed["transmitted"] is False, "대기 전에 전송 계획을 쓰면 안 됩니다"

    def test_approval_claimed_during_wait_is_reloaded_and_rejected(
        self, io, monkeypatch, tmp_path
    ):
        path = self._prepare(io, monkeypatch, tmp_path)

        def fake_wait(tb, now=None):
            pipeline.claim_approval(
                pipeline.load_approval(str(path)), now=_fixed_now()
            )
            return _fixed_now()

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        broker = _make_broker()
        with pytest.raises(RuntimeError, match="이미 사용된 승인"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0

    def test_ttl_expiry_during_wait_blocks_execution(self, io, monkeypatch, tmp_path):
        path = self._prepare(io, monkeypatch, tmp_path)

        def fake_wait(tb, now=None):
            return _fixed_now() + timedelta(hours=10)

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        broker = _make_broker()
        with pytest.raises(RuntimeError, match="만료"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False
        assert not Path(str(path) + ".transmitted.json").exists()

    def test_fill_increase_during_wait_blocks_execution(self, io, monkeypatch, tmp_path):
        path = self._prepare(io, monkeypatch, tmp_path)
        broker = _make_broker()
        new_fill = {
            "odno": "LATE1",
            "ord_dt": "20261021",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-21T00:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "1",
            "ft_ccld_amt3": "50",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }

        def fake_wait(tb, now=None):
            broker.get_order_history.return_value = [new_fill]
            return _fixed_now()

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        with pytest.raises(trading_bot.SymbolScopedError, match="새 체결"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_price_jump_during_wait_blocks_execution(self, io, monkeypatch, tmp_path):
        path = self._prepare(io, monkeypatch, tmp_path)
        broker = _make_broker()

        def fake_wait(tb, now=None):
            monkeypatch.setattr(
                trading_bot, "무한매수법_V4", self._price_strategy(60.0)
            )
            return _fixed_now()

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        with pytest.raises(RuntimeError, match="허용 오차"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_widened_env_after_approval_cannot_widen_risk(
        self, io, monkeypatch, tmp_path
    ):
        """승인 후 env 를 넓혀도 승인 범위 밖 주문은 실행하지 않습니다."""
        path = self._prepare(io, monkeypatch, tmp_path)
        broker = _make_broker()
        # 승인 허용오차(±$0.25)보다 넓게 env 를 바꿉니다.
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "10")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0.50")

        def fake_wait(tb, now=None):
            # ±$0.5 드리프트: 넓힌 env 에서는 통과하지만 승인 범위 밖입니다.
            monkeypatch.setattr(
                trading_bot, "무한매수법_V4", self._price_strategy(50.5)
            )
            return _fixed_now()

        monkeypatch.setattr(pipeline_execute, "_wait_until_pre_market", fake_wait)
        with pytest.raises(RuntimeError, match="허용 오차"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False
        assert not Path(str(path) + ".transmitted.json").exists()

    def test_early_execute_on_wrong_trading_day_is_rejected(
        self, io, monkeypatch, tmp_path
    ):
        path = self._prepare(io, monkeypatch, tmp_path)
        broker = _make_broker()
        # 2026-10-25(일) 23:00 UTC = 19:00 ET — 실행 세션(월) 전날 밤.
        wrong_day = datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="아직 시작되지 않았습니다"):
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=wrong_day
            )
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False


class TestTransmittedPlanIntegrity:
    """HOLD 3: VERIFY 가 실제 전송 계획의 해시/바인딩/허용오차를 독립 검증."""

    def _consumed_with_record(self, tmp_path):
        state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["consumed"] = True
        pipeline_execute._write_transmitted_plan(
            path, plan, approval, now=_fixed_now()
        )
        return approval, path, plan

    def _write_record(self, path, data):
        Path(str(path) + ".transmitted.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_valid_transmitted_record_passes(self, tmp_path):
        approval, _path, _plan = self._consumed_with_record(tmp_path)
        assert pipeline.transmitted_plan_problems(approval) == []
        assert trading_bot._verify_transmitted_plan(approval) == []

    def test_tampered_orders_hash_mismatch_detected(self, tmp_path):
        approval, path, _plan = self._consumed_with_record(tmp_path)
        data = json.loads(
            Path(str(path) + ".transmitted.json").read_text(encoding="utf-8")
        )
        data["orders"][0]["quantity"] = 999
        self._write_record(path, data)
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("transmitted_plan_hash" in p for p in problems), problems

    def test_foreign_plan_record_detected(self, tmp_path):
        approval, path, _plan = self._consumed_with_record(tmp_path)
        data = json.loads(
            Path(str(path) + ".transmitted.json").read_text(encoding="utf-8")
        )
        data["plan_id"] = "TQQQ-OTHER-PLAN"
        self._write_record(path, data)
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("plan_id 불일치" in p for p in problems), problems

    def test_wrong_approval_binding_detected(self, tmp_path):
        approval, path, _plan = self._consumed_with_record(tmp_path)
        data = json.loads(
            Path(str(path) + ".transmitted.json").read_text(encoding="utf-8")
        )
        data["approval_plan_hash"] = "deadbeef"
        self._write_record(path, data)
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("다른 승인" in p for p in problems), problems

    def test_missing_record_detected(self, tmp_path):
        approval, path, _plan = self._consumed_with_record(tmp_path)
        Path(str(path) + ".transmitted.json").unlink()
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("없습니다" in p for p in problems), problems

    def test_corrupt_record_detected(self, tmp_path):
        approval, path, _plan = self._consumed_with_record(tmp_path)
        Path(str(path) + ".transmitted.json").write_text("{", encoding="utf-8")
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("읽을 수 없습니다" in p for p in problems), problems

    def test_transmitted_orders_outside_approval_tolerance_detected(self, tmp_path):
        approval, path, plan = self._consumed_with_record(tmp_path)
        # 허용오차(기본 ±$0.25)를 크게 벗어난 가격이지만, 해시/바인딩은 유효하게
        # 다시 기록해 **허용오차 검사**가 잡아내는지 확인합니다.
        plan.orders = [dict(_orders_buy(60.0)[0])]
        pipeline_execute._write_transmitted_plan(
            path, plan, approval, now=_fixed_now()
        )
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("가격 불일치" in p for p in problems), problems
        assert not any("transmitted_plan_hash" in p for p in problems), problems
        assert any("가격 불일치" in p for p in trading_bot._verify_transmitted_plan(approval))

    def test_verify_requires_record_for_consumed_approval(
        self, io, monkeypatch, tmp_path, capsys
    ):
        state, plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        Path(str(path) + ".recovery.json").write_text(
            json.dumps({"plan_id": plan.plan_id, "outcome": "executed"}),
            encoding="utf-8",
        )
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 1
        assert "전송 계획 기록" in capsys.readouterr().out


class TestLockPathUnification:
    """HOLD 4: 셸/파이썬이 같은 절대 잠금 경로를 사용하고 플래그를 검증."""

    def _child_script(self, lock_path):
        return (
            "import sys\n"
            f"sys.path.insert(0, {str(_src_path)!r})\n"
            "import pipeline\n"
            "try:\n"
            "    with pipeline.global_lock(timeout_seconds=0.3):\n"
            "        print('acquired')\n"
            "except TimeoutError:\n"
            "    print('busy')\n"
            "    sys.exit(7)\n"
        )

    def _run_child(self, script, lock_path, *, flag=False, pass_fds=(), lock_fd=None):
        env = dict(os.environ)
        env.pop("RUN_LOCK_HELD", None)
        env.pop("RUN_LOCK_FD", None)
        env["RUN_LOCK_PATH"] = str(lock_path)
        if flag:
            env["RUN_LOCK_HELD"] = "1"
        if lock_fd is not None:
            env["RUN_LOCK_FD"] = str(lock_fd)
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(lock_path).parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            pass_fds=tuple(pass_fds),
        )

    def test_run_lock_path_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUN_LOCK_PATH", "nested/.run.lock")
        resolved = pipeline.run_lock_path()
        assert os.path.isabs(resolved)
        assert resolved.endswith(os.path.join("nested", ".run.lock"))

    def test_launcher_exports_resolved_lock_path(self):
        launcher = (_repo_root / "ops" / "run_daily.sh").read_text(encoding="utf-8")
        # `.env` 를 먼저 읽은 뒤 잠금 경로를 확정해야 .env 전용 경로가 셸/파이썬에
        # 같은 값으로 전달됩니다 (경로 분리 방지).
        env_pos = launcher.index(". ./.env")
        lock_pos = launcher.index('LOCK_PATH="${OVERRIDE_LOCK_PATH:-${RUN_LOCK_PATH:-$APP_DIR/.run.lock}}"')
        assert env_pos < lock_pos
        assert 'export RUN_LOCK_PATH="$LOCK_PATH"' in launcher
        assert 'exec 9>"$LOCK_PATH"' in launcher
        assert 'export RUN_LOCK_FD=9' in launcher
        assert 'export RUN_LOCK_HELD=1' in launcher

    def test_shell_holder_blocks_python_lock(self, tmp_path):
        lock = tmp_path / "shared.run.lock"
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 9>"{lock}"; flock 9; echo ready; sleep 30'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "ready"
            child = self._run_child(self._child_script(lock), lock)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout

    def test_run_lock_held_without_actual_lock_still_takes_lock(
        self, tmp_path, monkeypatch
    ):
        lock = tmp_path / "custom.run.lock"
        monkeypatch.setenv("RUN_LOCK_PATH", str(lock))
        monkeypatch.setenv("RUN_LOCK_HELD", "1")  # 런처 없이 남은 플래그
        with pipeline.global_lock(timeout_seconds=1):
            child = self._run_child(self._child_script(lock), lock, flag=False)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout

    def test_run_lock_held_with_actual_lock_bypasses(self, tmp_path, monkeypatch):
        lock = tmp_path / "held.run.lock"
        monkeypatch.setenv("RUN_LOCK_PATH", str(lock))
        monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
        with pipeline.global_lock(timeout_seconds=1):
            monkeypatch.setenv("RUN_LOCK_HELD", "1")
            # 실제 잠금이 잡혀 있으므로 우회해야 합니다 (교착 금지).
            with pipeline.global_lock(timeout_seconds=0.2):
                pass

    # ── P1: 플래그만으로는 우회 금지 ────────────────────────────────
    def test_stale_flag_with_foreign_holder_blocks(self, tmp_path):
        """플래그만 남은 직접 CLI 는 다른 실행과 **동시 진행하면 안 됩니다**."""
        lock = tmp_path / "stale.run.lock"
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 9>"{lock}"; flock 9; echo ready; sleep 30'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "ready"
            # RUN_LOCK_HELD=1 이지만 런처 fd(9)를 물려받지 않은 자식.
            child = self._run_child(
                self._child_script(lock), lock, flag=True
            )
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout
        assert "직접 잠급니다" in child.stdout

    def test_inherited_fd_hold_is_reentrant_without_deadlock(self, tmp_path):
        """런처가 물려준 fd 가 같은 파일을 가리키면 재진입으로 인정합니다."""
        lock = tmp_path / "inherited.run.lock"
        lock.touch()
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = self._run_child(
                self._child_script(lock),
                lock,
                flag=True,
                pass_fds=(fd,),
                lock_fd=fd,
            )
        finally:
            os.close(fd)
        assert child.returncode == 0, child.stdout + child.stderr
        assert "acquired" in child.stdout

    def test_inherited_fd_mismatch_does_not_bypass(self, tmp_path):
        """fd 번호만 맞고 **다른 파일**이면 상속으로 인정하지 않습니다."""
        lock = tmp_path / "target.run.lock"
        other = tmp_path / "other.run.lock"
        lock.touch()
        other.touch()
        other_fd = os.open(str(other), os.O_RDWR | os.O_CREAT, 0o600)
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 9>"{lock}"; flock 9; echo ready; sleep 30'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "ready"
            # 자식의 fd 는 other 를 가리키므로 잠금 증거가 아닙니다 → 대기/차단.
            child = self._run_child(
                self._child_script(lock),
                lock,
                flag=True,
                pass_fds=(other_fd,),
                lock_fd=other_fd,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            os.close(other_fd)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout

    def test_fd_to_same_file_but_not_holder_does_not_bypass(self, tmp_path):
        """같은 파일 fd 라도 **그 fd 가 잠금 보유자가 아니면** 상속이 아닙니다."""
        import fcntl

        lock = tmp_path / "not-holder.run.lock"
        lock.touch()
        inherited = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        holder_fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = self._run_child(
                self._child_script(lock),
                lock,
                flag=True,
                pass_fds=(inherited,),
                lock_fd=inherited,
            )
        finally:
            os.close(holder_fd)
            os.close(inherited)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout

    def test_env_only_lock_path_resolved_before_locking(self, tmp_path):
        """`.env` 에만 있는 RUN_LOCK_PATH 도 셸이 먼저 해석해 잠급니다."""
        home = tmp_path / "home"
        app_dir = home / "investment" / "muhan-v4"
        app_dir.mkdir(parents=True)
        (home / ".local" / "bin").mkdir(parents=True)
        (app_dir / ".env").write_text(
            "RUN_LOCK_PATH=.locks/env-only.run.lock\n", encoding="utf-8"
        )
        record = tmp_path / "launcher-env.txt"
        fake_uv = home / ".local" / "bin" / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env bash\n"
            'env > "$RECORD"\n'
            'readlink /dev/fd/9 >> "$RECORD" 2>/dev/null '
            '|| readlink /proc/self/fd/9 >> "$RECORD"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["RECORD"] = str(record)
        env["PATH"] = str(home / ".local" / "bin") + ":" + env.get("PATH", "")
        for key in ("RUN_LOCK_PATH", "RUN_LOCK_FD", "RUN_LOCK_HELD",
                    "TELEGRAM_BOT_TOKEN", "RUN_PHASE"):
            env.pop(key, None)
        result = subprocess.run(
            ["bash", str(_repo_root / "ops" / "run_daily.sh")],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lines = record.read_text(encoding="utf-8").splitlines()
        expected = str(app_dir / ".locks" / "env-only.run.lock")
        assert f"RUN_LOCK_PATH={expected}" in lines
        assert "RUN_LOCK_FD=9" in lines
        assert "RUN_LOCK_HELD=1" in lines
        # fd 9 가 실제로 .env 경로를 가리켜야 자식이 상속을 인정합니다.
        assert lines[-1] == expected
        assert Path(expected).exists()
        assert not (app_dir / ".run.lock").exists(), (
            "기본 경로가 따로 잠기면 셸/파이썬 잠금 도메인이 갈라집니다"
        )
        # 이 경로를 직접 CLI(플래그 없음)가 잡으려 하면 같은 도메인이라 차단됩니다.
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 9>"{expected}"; flock 9; echo ready; sleep 30'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "ready"
            child = self._run_child(self._child_script(expected), expected)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        assert child.returncode == 7, child.stdout + child.stderr
        assert "busy" in child.stdout


class TestCycleSeedIndependentRecompute:
    """HOLD 5: 사이클 시드를 원시 체결/경계/수수료/직전 시드로 독립 재계산."""

    def _evidence(self, next_seed=1100.0, extra_fills=None):
        fills = [
            {
                "odno": "B1",
                "side": "매수",
                "qty": 10,
                "price": 50.0,
                "ord_dt": "20260105",
                "ts": "2026-01-05T15:00:00+00:00",
            },
            {
                "odno": "S1",
                "side": "매도",
                "qty": 10,
                "price": 60.0,
                "ord_dt": "20260120",
                "ts": "2026-01-20T15:00:00+00:00",
            },
        ]
        fills.extend(extra_fills or [])
        return {
            "symbol": "TQQQ",
            "run_id": "run-cycle",
            "mode": "LIVE",
            "broker": "mockbroker",
            "broker_mode": "demo",
            "evidence_version": 2,
            "settlement_mode": "incremental",
            "T_before": 2.0,
            "net_invested_before": 500.0,
            "T_after": 2.0,
            "net_invested_after": 500.0,
            "ledger_before": {"start_T": 2.0, "entries": []},
            "ledger_after": {"start_T": 2.0, "entries": []},
            "orders": [],
            "fills": fills,
            "max_t": 20,
            "post_processing": {
                "applied": True,
                "steps": [
                    {
                        "step": "cycle_end",
                        "cycle_start": "2026-01-01",
                        "cycle_end": "2026-01-20",
                        "seed": 1000.0,
                        "commission_rate": 0.0,
                        "new_cycle_started": False,
                        "reinvest": True,
                        "next_cycle_seed": next_seed,
                    }
                ],
                "final_candidate": {
                    "T": 0.0,
                    "net_invested": 0.0,
                    "effective_seed": next_seed,
                    "cycle_start_date": "",
                },
            },
        }

    def _verify(self, tmp_path, evidence, final_seed):
        ops = pipeline._load_verify_run_module()
        path = tmp_path / "settlement-TQQQ-cycle.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return ops.verify_evidence_file(
            path,
            broker="mockbroker",
            mode="LIVE",
            run_id="run-cycle",
            broker_mode="demo",
            final_state={
                "T": 0.0,
                "net_invested": 0.0,
                "effective_seed": final_seed,
                "cycle_start_date": "",
            },
        )

    def test_correct_seed_passes_independent_check(self, tmp_path):
        _ok, problems = self._verify(tmp_path, self._evidence(), 1100.0)
        assert not any("next_cycle_seed 불일치" in p for p in problems), problems
        assert not any("effective_seed 불일치" in p for p in problems), problems

    def test_evidence_and_state_agreeing_on_wrong_seed_fails(self, tmp_path):
        # evidence 와 state 가 **둘 다** 9999 로 조작되어도 독립 재계산이 잡아냅니다.
        evidence = self._evidence(next_seed=9999.0)
        ok, problems = self._verify(tmp_path, evidence, 9999.0)
        assert ok is False
        assert any("next_cycle_seed 불일치" in p for p in problems), problems
        assert any("effective_seed 불일치" in p for p in problems), problems

    def test_same_day_rebuy_after_liquidation_is_excluded(self, tmp_path):
        rebuy = {
            "odno": "B2",
            "side": "매수",
            "qty": 10,
            "price": 70.0,
            "ord_dt": "20260120",
            "ts": "2026-01-20T15:05:00+00:00",
        }
        # 청산 이후 같은 날 재매수를 포함해 계산한(잘못된) 시드.
        evidence = self._evidence(next_seed=400.0, extra_fills=[rebuy])
        ok, problems = self._verify(tmp_path, evidence, 400.0)
        assert ok is False
        assert any("next_cycle_seed 불일치" in p for p in problems), problems

    def test_real_cycle_end_save_then_rerun_is_idempotent(self, io):
        buyer = {
            "odno": "B1",
            "ord_dt": "20261010",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-10T00:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매수",
            "ft_ccld_qty": "10",
            "ft_ccld_amt3": "500",
            "ft_ccld_unpr3": "50",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        seller = {
            "odno": "S1",
            "ord_dt": "20261021",
            "ord_tmd": "040000",
            "ord_datetime_utc": "2026-10-21T00:00:00+00:00",
            "sll_buy_dvsn_cd_name": "매도",
            "ft_ccld_qty": "10",
            "ft_ccld_amt3": "600",
            "ft_ccld_unpr3": "60",
            "nccs_qty": "0",
            "prcs_stat_name": "체결",
        }
        state = _base_state(
            T=2.0,
            net_invested=500.0,
            position_qty=10,
            cycle_start_date="2026-10-01",
            orders_meta={
                "B1": {
                    "side": "BUY",
                    "total_qty": 10,
                    "t_target": 1.0,
                    "processed_filled_qty": 10,
                    "processed_filled_amount": 500.0,
                },
                "S1": {
                    "side": "SELL",
                    "total_qty": 10,
                    "t_target": 1.0,
                    "processed_filled_qty": 0,
                    "processed_filled_amount": 0.0,
                },
            },
        )
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=0, history=[buyer, seller])
        first = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert first.halt_reason is None, first.halt_reason
        assert first.effective_seed and first.effective_seed > 0
        saved = io["saved"][-1]
        assert saved["effective_seed"] == pytest.approx(first.effective_seed)

        # 저장된 상태로 재실행 — 사이클 종료를 다시 처리하지 않고 시드가 유지됩니다.
        io["load"]("TQQQ", deepcopy(saved))
        second = pipeline.settle_positions(ReadOnlyBroker(broker), _symbol_config())
        assert second.halt_reason is None, second.halt_reason
        assert second.effective_seed == pytest.approx(first.effective_seed)
        assert io["saved"][-1]["effective_seed"] == pytest.approx(first.effective_seed)
        second_evidence = json.loads(
            Path(second.evidence_path).read_text(encoding="utf-8")
        )
        steps = [
            step.get("step")
            for step in (second_evidence.get("post_processing") or {}).get("steps", [])
        ]
        assert "cycle_end" not in steps, "재실행에서 사이클 종료를 또 처리하면 안 됩니다"


# ── P1: 사이클 이력 완전성/정렬 ────────────────────────────────────


class TestCycleCompleteness:
    """사이클 시드 검증은 **완전한 이력**에서만 계산합니다 (불완전 = 검증 불가)."""

    def _evidence(self):
        return TestCycleSeedIndependentRecompute()._evidence()

    def _step(self, evidence):
        return evidence["post_processing"]["steps"][0]

    def test_missing_cycle_start_is_unverifiable(self, tmp_path):
        ops = pipeline._load_verify_run_module()
        evidence = self._evidence()
        step = dict(self._step(evidence), cycle_start="")
        expected, problems = ops._expected_cycle_seed(evidence, step)
        assert expected is None
        assert any("cycle_start" in p for p in problems), problems

    def test_sells_only_history_is_unverifiable(self):
        ops = pipeline._load_verify_run_module()
        evidence = self._evidence()
        evidence["fills"] = [
            {
                "odno": "S1",
                "side": "매도",
                "qty": 10,
                "price": 60.0,
                "ord_dt": "20260120",
                "ts": "2026-01-20T15:00:00+00:00",
            }
        ]
        expected, problems = ops._expected_cycle_seed(evidence, self._step(evidence))
        assert expected is None
        assert any("매수 이력" in p for p in problems), problems

    def test_buys_missing_overdraw_is_unverifiable(self):
        """매도가 매수 누적을 넘으면 0 으로 clamp 하지 않고 실패합니다."""
        ops = pipeline._load_verify_run_module()
        evidence = self._evidence()
        evidence["fills"] = [
            {
                "odno": "B1",
                "side": "매수",
                "qty": 5,
                "price": 50.0,
                "ord_dt": "20260105",
                "ts": "2026-01-05T15:00:00+00:00",
            },
            {
                "odno": "S1",
                "side": "매도",
                "qty": 10,
                "price": 60.0,
                "ord_dt": "20260120",
                "ts": "2026-01-20T15:00:00+00:00",
            },
        ]
        expected, problems = ops._expected_cycle_seed(evidence, self._step(evidence))
        assert expected is None
        assert any("초과" in p or "매수 이력" in p for p in problems), problems

    def test_no_liquidation_event_is_unverifiable(self):
        ops = pipeline._load_verify_run_module()
        evidence = self._evidence()
        evidence["fills"] = [
            {
                "odno": "B1",
                "side": "매수",
                "qty": 10,
                "price": 50.0,
                "ord_dt": "20260105",
                "ts": "2026-01-05T15:00:00+00:00",
            },
            {
                "odno": "S1",
                "side": "매도",
                "qty": 3,
                "price": 55.0,
                "ord_dt": "20260110",
                "ts": "2026-01-10T15:00:00+00:00",
            },
        ]
        expected, problems = ops._expected_cycle_seed(evidence, self._step(evidence))
        assert expected is None
        assert any("청산" in p for p in problems), problems

    def test_same_second_liquidation_rebuy_order_is_input_independent(self):
        """같은 초 청산/재매수는 (시각, odno) 로 경계가 고정되어야 합니다."""
        ops = pipeline._load_verify_run_module()
        base = self._evidence()
        buy = {
            "odno": "B1",
            "side": "매수",
            "qty": 10,
            "price": 50.0,
            "ord_dt": "20260120",
            "ts": "2026-01-20T15:00:00+00:00",
        }
        liquidate = {
            "odno": "S1",
            "side": "매도",
            "qty": 10,
            "price": 60.0,
            "ord_dt": "20260120",
            "ts": "2026-01-20T15:00:00+00:00",
        }
        rebuy = {
            "odno": "T2",
            "side": "매수",
            "qty": 5,
            "price": 70.0,
            "ord_dt": "20260120",
            "ts": "2026-01-20T15:00:00+00:00",
        }
        results = []
        for order in ([buy, rebuy, liquidate], [buy, liquidate, rebuy]):
            evidence = dict(base, fills=order)
            expected, problems = ops._expected_cycle_seed(
                evidence, self._step(evidence)
            )
            assert not problems, problems
            selected, _problems, event = ops._select_cycle_fills(
                order, "20260101", "20260120"
            )
            results.append(
                (
                    expected,
                    [fill["odno"] for fill in selected],
                    event["odno"] if event else None,
                )
            )
        assert results[0] == results[1] == (1100.0, ["B1", "S1"], "S1")

    def test_duplicate_same_second_odno_is_indeterminate(self):
        ops = pipeline._load_verify_run_module()
        fill = {
            "odno": "B1",
            "side": "매수",
            "qty": 10,
            "price": 50.0,
            "ord_dt": "20260105",
            "ts": "2026-01-05T15:00:00+00:00",
        }
        selected, problems, event = ops._select_cycle_fills(
            [fill, dict(fill, qty=9)], "20260101", "20260120"
        )
        assert any("중복" in p for p in problems), problems
        assert event is None

    def test_verify_evidence_file_fails_for_incomplete_cycle(self, tmp_path):
        ops = pipeline._load_verify_run_module()
        evidence = self._evidence()
        evidence["fills"] = [
            {
                "odno": "S1",
                "side": "매도",
                "qty": 10,
                "price": 60.0,
                "ord_dt": "20260120",
                "ts": "2026-01-20T15:00:00+00:00",
            }
        ]
        path = tmp_path / "settlement-TQQQ-incomplete.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        ok, problems = ops.verify_evidence_file(
            path,
            broker="mockbroker",
            mode="LIVE",
            run_id="run-cycle",
            broker_mode="demo",
            final_state={
                "T": 0.0,
                "net_invested": 0.0,
                "effective_seed": 1100.0,
                "cycle_start_date": "",
            },
        )
        assert ok is False
        assert any("매수 이력" in p for p in problems), problems


# ── P1: 승인 허용오차 바인딩 ───────────────────────────────────────


class TestToleranceBinding:
    """허용오차는 승인 시점에 고정되고, 이후 env 는 좁히는 것만 허용."""

    def test_approval_records_and_hashes_tolerance_policy(self, tmp_path):
        _state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        assert approval["tolerance_policy"] == pipeline.execution_tolerance_policy()
        assert plan.tolerance_policy == approval["tolerance_policy"]
        assert approval["plan_hash"] == _plan_hash(approval)

    def test_tampered_tolerance_policy_breaks_plan_hash(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval["tolerance_policy"] = dict(
            approval["tolerance_policy"], price_abs=99.0
        )
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("plan_hash" in p for p in problems), problems

    def test_widened_env_after_approval_still_rejects_out_of_range(self, tmp_path,
                                                                  monkeypatch):
        _state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approved_policy = dict(approval["tolerance_policy"])
        # 승인 후 env 를 크게 넓힙니다 (±$10, 50%).
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "10")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0.50")
        # 승인 허용오차(±$0.25)는 넘지만 넓힌 env 안에는 들어오는 가격.
        plan.orders = [dict(_orders_buy(50.5)[0])]
        pipeline_execute._write_transmitted_plan(
            path, plan, approval, now=_fixed_now()
        )
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("가격 불일치" in p for p in problems), problems
        # 유효 정책은 승인 정책을 넘지 않습니다.
        effective = pipeline.effective_tolerance_policy(approved_policy)
        assert effective["price_abs"] == approved_policy["price_abs"]
        assert effective["price_pct"] == approved_policy["price_pct"]
        # EXECUTE 경로도 같은 정책으로 비교합니다.
        drift = pipeline_execute.compare_order_plans(
            approval["orders"], plan.orders, policy=effective
        )
        assert any("가격 불일치" in p for p in drift), drift

    def test_narrowed_env_after_approval_is_applied(self, tmp_path, monkeypatch):
        _state, plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "0.001")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "0")
        plan.orders = [dict(_orders_buy(50.01)[0])]
        pipeline_execute._write_transmitted_plan(
            path, plan, approval, now=_fixed_now()
        )
        problems = pipeline.transmitted_plan_problems(approval)
        assert any("가격 불일치" in p for p in problems), problems

    def test_missing_bound_policy_is_rejected(self, tmp_path):
        state, _plan, path = _approve(tmp_path)
        approval = pipeline.load_approval(str(path))
        approval.pop("tolerance_policy", None)
        problems = pipeline.verify_approval(approval, state, now=_fixed_now())
        assert any("허용오차 정책" in p for p in problems), problems
        assert pipeline_execute.compare_order_plans(
            approval["orders"], _orders_buy(50.0)
        ), "정책 없는 비교는 fail-closed 여야 합니다"

    @pytest.mark.parametrize(
        "policy",
        [
            {"price_pct": float("nan"), "price_abs": 0.02, "qty": 0},
            {"price_pct": 0.005, "price_abs": float("nan"), "qty": 0},
            {"price_pct": float("inf"), "price_abs": 0.02, "qty": 0},
            {"price_pct": 0.005, "price_abs": float("-inf"), "qty": 0},
            {"price_pct": -0.001, "price_abs": 0.02, "qty": 0},
            {"price_pct": 0.005, "price_abs": -1.0, "qty": 0},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": -1},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": True},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": 1.0},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": float("nan")},
            {"price_pct": 0.005, "price_abs": 0.02},  # qty 키 없음
            {"price_pct": 0.005, "qty": 0},  # price_abs 키 없음
            "not-a-dict",
        ],
    )
    def test_invalid_policy_is_rejected(self, policy):
        assert pipeline.normalize_tolerance_policy(policy) is None

    def test_valid_policy_is_normalized(self):
        normalized = pipeline.normalize_tolerance_policy(
            {"price_pct": 0.005, "price_abs": 0.02, "qty": 0}
        )
        assert normalized == {"price_pct": 0.005, "price_abs": 0.02, "qty": 0}

    @pytest.mark.parametrize(
        "approved",
        [
            {"price_pct": float("nan"), "price_abs": 0.02, "qty": 0},
            {"price_pct": float("inf"), "price_abs": 0.02, "qty": 0},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": True},
            {"price_pct": 0.005, "price_abs": 0.02, "qty": 1.5},
            {"price_pct": 0.005, "price_abs": 0.02},
        ],
    )
    def test_effective_policy_with_bad_approved_policy_is_none(self, approved):
        """승인 정책이 손상되면 유효 정책은 None (호출 측 fail-closed)."""
        assert pipeline.effective_tolerance_policy(approved) is None

    def test_non_finite_env_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_ABS", "inf")
        monkeypatch.setenv("EXECUTION_PRICE_TOLERANCE_PCT", "nan")
        policy = pipeline.execution_tolerance_policy()
        assert policy["price_abs"] == pipeline.DEFAULT_TOLERANCE_PRICE_ABS
        assert policy["price_pct"] == pipeline.DEFAULT_TOLERANCE_PRICE_PCT


# ── P2: 모의투자 자동 실행 대기 검증 ──────────────────────────────────

@pytest.mark.parametrize('mode', ['demo', 'real'])
@pytest.mark.parametrize('dst,hour', [(True, 17), (False, 18)])
def test_live_waits_for_pre_market(mode, dst, hour):
    """real/demo × 서머타임/동절기: LIVE 는 프리장 개장까지 대기합니다."""
    now = datetime(2026, 10, 26, 10, 15, tzinfo=ZoneInfo('Asia/Seoul'))
    sleep = MagicMock()
    tb = SimpleNamespace(
        BROKER_MODE=mode, TRADE_MODE='LIVE',
        get_kst_now=lambda: now, is_us_dst=lambda: dst,
        time=SimpleNamespace(sleep=sleep),
    )
    pipeline_execute._wait_until_pre_market(tb)
    sleep.assert_called_once_with((hour - 10) * 3600 - 15 * 60)


def test_dry_never_waits_for_pre_market():
    """DRY 는 시계를 읽지 않고 대기하지 않습니다."""
    tb = SimpleNamespace(
        TRADE_MODE='DRY',
        get_kst_now=MagicMock(side_effect=AssertionError('clock accessed')),
    )
    assert pipeline_execute._wait_until_pre_market(tb) is None


def test_morning_demo_auto_executes_after_wait(io, monkeypatch):
    """아침 demo AUTO_EXECUTE 는 대기 후 정상 주문합니다."""
    TestPhaseDispatcher()._prepare(io, monkeypatch)
    clock = [datetime(2026, 10, 26, 1, 15, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0]

    def advance(seconds):
        clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(trading_bot, 'datetime', Clock)
    monkeypatch.setattr(trading_bot, 'get_kst_now', lambda: clock[0].astimezone(ZoneInfo('Asia/Seoul')))
    monkeypatch.setattr(trading_bot, 'is_us_dst', lambda: True)
    monkeypatch.setattr(trading_bot.time, 'sleep', advance)
    monkeypatch.setattr(trading_bot, 'AUTO_EXECUTE', True)
    monkeypatch.setenv('RUN_PHASE', 'PREVIEW')
    broker = _make_broker()
    broker.place_order.return_value = OrderResult(order_id='EXEC1', order_time='2026-10-26T17:00:00+09:00', is_reservation=False)
    assert trading_bot._dispatch_pipeline_phase('PREVIEW', broker) == 0
    assert broker.place_order.call_count == 1
    assert clock[0].astimezone(ZoneInfo('Asia/Seoul')).hour == 17


# ── P1/P2: AUTO_EXECUTE(opt-in) 결함 재현 테스트 ─────────────────────

class TestAutoExecuteDefectRepro:
    """P1: 공통 상태 장애(GlobalScopedError)가 다음 종목을 삼키지 않아야 합니다.
    P2: DRY 모드에서 AUTO_EXECUTE 는 승인·실행을 건너뛰고 PREVIEW 만 생성해야 합니다.
    """

    def test_global_failure_must_stop_all_symbols(self, io, monkeypatch):
        monkeypatch.setattr(trading_bot, 'AUTO_EXECUTE', True)
        monkeypatch.setattr(trading_bot, 'SYMBOLS', [_symbol_config(), _symbol_config('SOXL')])
        monkeypatch.setattr(trading_bot, '_settled_from_state', lambda cfg: cfg)
        monkeypatch.setattr(trading_bot, 'build_order_plan',
                            lambda cfg, broker: SimpleNamespace(symbol=cfg['symbol'], plan_id=cfg['symbol'], orders=[{}], state_hash='hash'))
        monkeypatch.setattr(trading_bot, 'write_order_plan', lambda *a, **k: 'plan.json')
        monkeypatch.setattr(trading_bot, 'approve_plan_file', lambda *a, **k: 'approval.json')

        execute = MagicMock(side_effect=trading_bot.GlobalScopedError('state save failed'))
        monkeypatch.setattr(trading_bot, 'execute_approved_plan', execute)
        with pytest.raises(trading_bot.GlobalScopedError):
            trading_bot._dispatch_pipeline_phase('PREVIEW', _make_broker())
        assert execute.call_count == 1

    def test_dry_auto_preview_should_not_fail(self, io, monkeypatch):
        monkeypatch.setattr(trading_bot, 'AUTO_EXECUTE', True)
        monkeypatch.setattr(trading_bot, 'SYMBOLS', [_symbol_config(), _symbol_config('SOXL')])
        monkeypatch.setattr(trading_bot, '_settled_from_state', lambda cfg: cfg)
        monkeypatch.setattr(trading_bot, 'build_order_plan',
                            lambda cfg, broker: SimpleNamespace(symbol=cfg['symbol'], plan_id=cfg['symbol'], orders=[{}], state_hash='hash'))
        monkeypatch.setattr(trading_bot, 'write_order_plan', lambda *a, **k: 'plan.json')
        monkeypatch.setattr(trading_bot, 'approve_plan_file', lambda *a, **k: 'approval.json')
        monkeypatch.setattr(trading_bot, 'TRADE_MODE', 'DRY')
        assert trading_bot._dispatch_pipeline_phase('PREVIEW', _make_broker()) == 0


# ── P2(리뷰): 보류 체결 소유권(orders_meta) 검증 ──────────────────────
#
# 회귀: `_deferred_execution_session_fills()` 는 **날짜 조건만** 보고 실행 세션
# 체결을 모읍니다. 그 집합을 재정산 허용 목록에 그대로 넣으면, 봇이 내지 않은
# **수동 체결**도 (날짜·잔고만 맞으면) 봇 실행 결과로 흡수됩니다. 그래서 허용
# 목록에 넣기 전에 `state["orders_meta"]` 등록 여부를 확인하고, 미등록(수동)
# 체결은 fail-closed 로 중단합니다.


class TestDeferredFillOwnership:
    """보류 체결이 봇 주문인지 확인한 뒤에만 허용 목록에 넣습니다."""

    def _run_execute(self, io, tmp_path, *, history, balance_qty, state):
        state, _plan, path = _approve(tmp_path, state=state)
        io["load"]("TQQQ", deepcopy(state))
        broker = _make_broker(balance_qty=balance_qty, history=history)
        broker.place_order.return_value = OrderResult(
            order_id="EXEC1",
            order_time="2026-10-26T17:00:00+09:00",
            is_reservation=False,
        )
        return state, broker, path

    def test_manual_deferred_fill_is_not_admitted(self, io, tmp_path):
        """수동 체결만 있으면 허용 목록에 넣지 않고 중단합니다 (조용한 통과 금지)."""
        # given: state 에 등록된 봇 주문이 하나도 없고, 실행 세션에 수동 매수 1주 체결
        manual = _deferred_fill(odno="MANUAL-77", qty="1", amount="50")
        state = _base_state(position_qty=0)
        state, broker, path = self._run_execute(
            io, tmp_path, history=[manual], balance_qty=1, state=state
        )
        # and: 날짜 조건만 보는 수집 함수는 이 행을 그대로 담습니다 (회귀 지점)
        assert [row["odno"] for row in pipeline._deferred_execution_session_fills(
            state, [manual]
        )] == ["MANUAL-77"]
        # when: EXECUTE
        with pytest.raises(
            trading_bot.SymbolScopedError, match="등록되어 있지 않습니다"
        ) as error:
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        # then: 주문을 내지 않고 승인도 소비하지 않습니다.
        assert "수동 체결" in str(error.value)
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False
        # and: 공개 메시지에는 원문 주문번호를 남기지 않습니다 (가명 토큰만).
        assert "MANUAL-77" not in str(error.value)

    def test_mixed_bot_and_manual_deferred_fills_blocks(self, io, tmp_path):
        """봇 체결과 수동 체결이 섞이면 수동 건 때문에 전체를 중단합니다."""
        # given: 봇 주문 DEF1 등록 + 실행 세션 체결 2건 (봇 1주 매수 / 수동 2주 매수)
        bot_fill = _deferred_fill(odno="DEF1", qty="1", amount="50")
        manual_fill = _deferred_fill(odno="MANUAL-88", qty="2", amount="100")
        state = _base_state(
            position_qty=0, orders_meta=_bot_order_meta("DEF1")
        )
        state, broker, path = self._run_execute(
            io, tmp_path,
            history=[bot_fill, manual_fill],
            balance_qty=3,  # 잔고는 두 체결을 정확히 설명합니다 (소유권만 문제)
            state=state,
        )
        # when
        with pytest.raises(
            trading_bot.SymbolScopedError, match="등록되어 있지 않습니다"
        ) as error:
            pipeline_execute.execute_approved_plan(
                broker, str(path), now=_fixed_now()
            )
        # then: 잔고 교차검증 실패가 아니라 **소유권** 사유로 중단됩니다.
        assert "보류 체결 검증 실패" not in str(error.value)
        assert "1건" in str(error.value)
        assert broker.place_order.call_count == 0
        assert pipeline.load_approval(str(path))["consumed"] is False

    def test_manual_fill_that_balance_explains_used_to_be_admitted(self):
        """수정 전에는 날짜·잔고만 맞으면 수동 체결이 허용 목록에 들어갔습니다.

        수정 후에도 수집 함수는 행을 반환하지만(날짜 조건만), 소유권 검사가
        그 행을 걸러내는지 함께 고정합니다.
        """
        manual = _deferred_fill(odno="MANUAL-99", qty="1", amount="50")
        state = _base_state(position_qty=0)
        # given: 날짜 조건만 보면 "보류 체결"로 수집됩니다
        collected = pipeline._deferred_execution_session_fills(state, [manual])
        assert len(collected) == 1
        # when / then: 소유권 검사가 수동 체결로 분류합니다
        assert pipeline._unregistered_deferred_fills(state, collected) == collected

    def test_registered_bot_fill_is_allowed(self, io, tmp_path):
        """등록된 봇 주문 체결은 기존처럼 허용 목록에 들어가 실행됩니다."""
        fill = _deferred_fill(odno="DEF1", qty="1", amount="50")
        state = _base_state(
            position_qty=0, orders_meta=_bot_order_meta("DEF1")
        )
        state, broker, path = self._run_execute(
            io, tmp_path, history=[fill], balance_qty=1, state=state
        )
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=_fixed_now()
        )
        assert result["outcome"] == "executed"
        assert broker.place_order.call_count == 1

    def test_int_key_orders_meta_is_normalized(self):
        """orders_meta 키가 int 여도 봇 주문으로 인정합니다 (문자열 정규화)."""
        # given: 손으로 만든/구버전 state 처럼 int 키로 저장된 주문 메타
        state = _base_state(orders_meta={4615: {"side": "BUY", "total_qty": 1}})
        row = _deferred_fill(odno="4615", qty="1", amount="50")
        # when / then: 문자열로 맞춰 비교하므로 수동 체결로 오판하지 않습니다.
        assert pipeline._unregistered_deferred_fills(state, [row]) == []
        # and: 이력 주문번호가 int 인 경우도 같은 기준으로 매칭됩니다.
        assert pipeline._unregistered_deferred_fills(
            state, [_deferred_fill(odno=4615, qty="1", amount="50")]
        ) == []

    def test_reverse_order_meta_is_not_manual(self):
        """리버스 주문(orders_meta 에 reverse_* 메타로 등록)도 봇 주문입니다."""
        reverse_meta = _bot_order_meta(
            "REV1", side="SELL",
            reverse_action="sell",
            reverse_day=1,
            cycle_id="c1",
            reverse_base_t=19.0,
            reverse_t_factor=0.9,
        )
        state = _base_state(orders_meta=reverse_meta)
        row = _deferred_fill(odno="REV1", side="매도", qty="8", amount="400")
        assert pipeline._unregistered_deferred_fills(state, [row]) == []

    def test_row_without_odno_is_manual(self):
        """주문번호를 읽을 수 없는 체결은 소유를 증명할 수 없어 수동 취급입니다."""
        state = _base_state(orders_meta=_bot_order_meta("DEF1"))
        broken = _deferred_fill(odno="", qty="1", amount="50")
        assert pipeline._unregistered_deferred_fills(state, [broken]) == [broken]

    def test_empty_orders_meta_makes_every_fill_manual(self):
        """orders_meta 가 비면 어떤 실행 세션 체결도 허용할 수 없습니다."""
        state = _base_state(orders_meta={})
        rows = [
            _deferred_fill(odno="A1", qty="1", amount="50"),
            _deferred_fill(odno="A2", side="매도", qty="1", amount="50"),
        ]
        assert pipeline._unregistered_deferred_fills(state, rows) == rows
