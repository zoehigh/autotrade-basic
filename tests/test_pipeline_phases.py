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
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from broker.base import Balance, PurchaseAmount, ReadOnlyBroker
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
    return datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
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
    )


def _write_evidence(
    tmp_path, symbol="TQQQ", run_id="run-test", committed=True, balance_qty=0
):
    path = tmp_path / f"settlement-{symbol}-20261024T120000Z.json"
    path.write_text(
        json.dumps(
            {
                "symbol": symbol,
                "run_id": run_id,
                "evidence_version": 2,
                "mode": "LIVE",
                "committed": committed,
                "balance_qty": balance_qty,
            }
        ),
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
        assert approval["session"] == "2026-10-24"
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
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
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

    def test_rebuild_tolerance_enforced(self):
        approved = _orders_buy(50.0)
        rebuilt_close = _orders_buy(50.01)
        rebuilt_far = _orders_buy(52.0)
        assert pipeline_execute.compare_order_plans(approved, rebuilt_close) == []
        problems = pipeline_execute.compare_order_plans(approved, rebuilt_far)
        assert any("가격 불일치" in p for p in problems), problems

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


class TestForceTSingleSave:
    """P1-D: FORCE_T 는 검증 후 1회만 저장 (조기 저장 금지)."""

    def test_force_t_saves_once_after_validations(self, io):
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        config = _symbol_config()
        config["force_t"] = 5.0
        settled = pipeline.settle_positions(
            ReadOnlyBroker(_make_broker(balance_qty=10, history=[buyer])), config
        )
        assert settled.T == pytest.approx(5.0)
        assert len(io["saved"]) == 1, "FORCE_T 포함 정산은 정확히 1회 저장해야 합니다"
        assert io["saved"][0]["T"] == pytest.approx(5.0)

    def test_force_t_not_saved_when_later_validation_fails(self, io, monkeypatch):
        """P1-D: FORCE_T 이후 검증이 실패하면 보정값을 저장하지 않습니다."""
        state, buyer = TestSettle()._processed_state()
        io["load"]("TQQQ", deepcopy(state))
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        config = _symbol_config()
        config["force_t"] = 5.0

        def _boom(**kwargs):
            raise RuntimeError("cycle report failure")

        monkeypatch.setattr(trading_bot, "generate_cycle_report", _boom)
        broker = _make_broker(balance_qty=0, history=[buyer])
        with pytest.raises(RuntimeError, match="cycle report failure"):
            pipeline.settle_positions(ReadOnlyBroker(broker), config)
        assert io["saved"] == [], "검증 실패 시 FORCE_T 를 조기 저장하면 안 됩니다"


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
               orders=None, order_metas=None):
        evidence = _write_evidence(tmp_path, balance_qty=0)
        approval = {
            "plan_id": self.PLAN_ID,
            "symbol": "TQQQ",
            "exchange": "NAS",
            "consumed": True,
            "orders": orders or _orders_buy(),
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

    def test_consumed_without_record_still_reports_execution_failed(
        self, io, monkeypatch, tmp_path, capsys
    ):
        state, _plan, path = _approve(tmp_path)
        pipeline.claim_approval(pipeline.load_approval(str(path)), now=_fixed_now())
        io["load"]("TQQQ", state)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(trading_bot, "datetime", _FixedDateTime)
        monkeypatch.setattr(
            trading_bot, "verify_evidence_file_with_ops", lambda *a, **k: (True, [])
        )
        monkeypatch.setenv("APPROVAL_FILE", str(path))
        assert trading_bot._run_verify_phase() == 0
        assert "outcome=execution-failed" in capsys.readouterr().out


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
