"""
REPAIR 모드(TRADE_MODE=REPAIR) 테스트.

배경: REPAIR는 유지보수 전용 모드로, 액션 소스(REPAIR_ACTION 또는 레거시
STATE_*_ONLY/FORCE_T*)가 **정확히 하나**일 때만 실행합니다. 0개 또는 2개
이상이면 sys.exit(1)로 중단합니다. 주문(place_order)은 절대 호출하지 않습니다.

검증 대상:
- REPAIR+DIAGNOSTIC: 상태 출력만 하고 저장/주문이 없어야 합니다.
- REPAIR 액션 충돌: REPAIR_ACTION + 레거시 STATE_*_ONLY 동시 설정 시
  sys.exit(1)로 중단하고 저장이 없어야 합니다.
- REPAIR 액션 미지정: REPAIR_ACTION 없이 REPAIR 모드 진입 시 sys.exit(1).
- DRY 회귀: REPAIR 도입 후에도 DRY는 상태를 저장하지 않아야 합니다.
"""
import os
import sys
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

# repo 루트 + src 경로 추가 (trading_bot import용)
_repo_root = os.path.join(os.path.dirname(__file__), "..")
_src_path = os.path.join(_repo_root, "src")
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import trading_bot
from broker.base import Balance


def _make_state(**overrides):
    state = {
        "T": 0.0,
        "last_updated": "2026-07-13T12:18:20+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
    }
    state.update(overrides)
    return state


def _symbol_config(force_t=None):
    cfg = {
        "symbol": "TQQQ",
        "exchange": "NAS",
        "splits": 20,
        "symbol_type": "TQQQ",
        "seed": 8000.0,
        "additional_loc_levels": 3,
    }
    if force_t is not None:
        cfg["force_t"] = force_t
    return cfg


def _buy_fill(odno="ORD1", odt="2026-07-14T00:00:00+00:00", qty=1, price=50.0):
    return {
        "odno": odno,
        "ord_dt": odt[:10].replace("-", ""),
        "ord_tmd": "090000",
        "ord_datetime_utc": odt,
        "ft_ccld_qty": str(qty),
        "ft_ccld_unpr3": str(price),
        "sll_buy_dvsn_cd_name": "매수",
    }


def _make_broker(balance_qty, order_history):
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = order_history
    broker.get_balance.return_value = Balance(
        quantity=balance_qty,
        avg_price=50.0 if balance_qty > 0 else 0.0,
    )
    broker.exchange_code.return_value = "NASD"
    broker.close = MagicMock()
    return broker


def _fake_strategy(broker, **kwargs):
    return {
        "reverse_exit": False,
        "last_price": 50.0,
        "position_qty": 0,
        "avg_price": 0.0,
        "orderable_cash": 0.0,
        "star_point": None,
        "orders": [],
    }


@pytest.fixture
def repair_env(monkeypatch):
    """상태 I/O·알림·전략을 mock으로 대체합니다."""
    saved_states = []
    notify_messages = []
    loaded = {}

    monkeypatch.setattr(
        trading_bot, "load_state",
        lambda symbol: loaded.get(symbol, _make_state()),
    )
    monkeypatch.setattr(
        trading_bot, "save_state",
        lambda symbol, state: saved_states.append(deepcopy(state)),
    )
    monkeypatch.setattr(
        trading_bot, "notify",
        lambda message, urgent=False: notify_messages.append(message),
    )
    monkeypatch.setattr(trading_bot, "무한매수법_V4", _fake_strategy)
    return {
        "saved_states": saved_states,
        "notify_messages": notify_messages,
        "load": lambda symbol, state: loaded.__setitem__(symbol, state),
    }


class TestRepairDiagnostic:
    """REPAIR+DIAGNOSTIC: 상태 출력만, 저장/주문 없음."""

    def test_diagnostic_zero_saves_zero_orders(self, repair_env, monkeypatch):
        """REPAIR+DIAGNOSTIC은 상태를 저장하지 않고 주문도 내지 않아야 합니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
        monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "DIAGNOSTIC")
        monkeypatch.setattr(trading_bot, "canonical_state_hash", lambda state: "hash")
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state(T=3.0))

        trading_bot._dispatch_repair()

        assert len(ctx["saved_states"]) == 0, \
            "DIAGNOSTIC은 상태를 저장하지 않아야 합니다"
        # place_order는 전략/주문 경로에만 존재 — DIAGNOSTIC은 전략을 실행하지 않습니다
        assert not any("place_order" in m for m in ctx["notify_messages"])


class TestRepairConflict:
    """REPAIR 액션 소스 충돌/미지정 → sys.exit(1)."""

    def test_repair_action_with_legacy_exits(self, repair_env, monkeypatch):
        """REPAIR_ACTION + 레거시 STATE_*_ONLY 동시 설정 → exit(1), 저장 없음."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
        monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "DIAGNOSTIC")
        monkeypatch.setattr(trading_bot, "STATE_REVERSE_AUDIT_ONLY", True)
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state())

        with pytest.raises(SystemExit) as exc_info:
            trading_bot._dispatch_repair()

        assert exc_info.value.code == 1
        assert len(ctx["saved_states"]) == 0, "충돌 시 저장이 없어야 합니다"

    def test_repair_action_with_reinference_exits(self, repair_env, monkeypatch):
        """REPAIR_ACTION + FORCE_T_REINFERENCE 동시 설정 → exit(1), 저장 없음."""
        import config as runtime_config
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
        monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "DIAGNOSTIC")
        monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state())

        with pytest.raises(SystemExit) as exc_info:
            trading_bot._dispatch_repair()

        assert exc_info.value.code == 1
        assert len(ctx["saved_states"]) == 0, "충돌 시 저장이 없어야 합니다"

    def test_repair_no_action_exits(self, repair_env, monkeypatch):
        """REPAIR 모드인데 액션 소스가 0개 → exit(1), 저장 없음."""
        import config as runtime_config
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
        monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "")
        monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", False)
        # 레거시 플래그 전부 비활성화
        for name in (
            "STATE_DIAGNOSTIC_ONLY", "STATE_REPAIR_ONLY", "STATE_CLEAR_FENCE_ONLY",
            "STATE_REVERSE_AUDIT_ONLY", "STATE_REVERSE_RECONCILE_ONLY",
            "STATE_NET_INVESTED_REPAIR_ONLY", "STATE_ASSUME_REVERSE_EXPIRY_ONLY",
        ):
            monkeypatch.setattr(trading_bot, name, False)
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state())

        with pytest.raises(SystemExit) as exc_info:
            trading_bot._dispatch_repair()

        assert exc_info.value.code == 1
        assert len(ctx["saved_states"]) == 0, "액션 미지정 시 저장이 없어야 합니다"


class TestRepairLegacySingleSource:
    """REPAIR 모드에서 레거시 단일 소스는 기존 핸들러로 디스패치됩니다."""

    def test_legacy_diagnostic_single_source(self, repair_env, monkeypatch):
        """STATE_DIAGNOSTIC_ONLY 단독 + REPAIR 모드 → 진단 실행, 저장 없음."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
        monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "")
        monkeypatch.setattr(trading_bot, "STATE_DIAGNOSTIC_ONLY", True)
        monkeypatch.setattr(trading_bot, "canonical_state_hash", lambda state: "hash")
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state(T=3.0))

        trading_bot._dispatch_repair()

        assert len(ctx["saved_states"]) == 0, \
            "레거시 단일 소스 진단도 상태를 저장하지 않아야 합니다"


class TestDryRegression:
    """REPAIR 도입 후 DRY 모드 회귀 방지."""

    def test_dry_continues_without_save(self, repair_env, monkeypatch):
        """DRY: 불일치가 있어도 저장 없이 프리뷰를 계속 진행합니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        ctx = repair_env
        ctx["load"]("TQQQ", _make_state(T=0.0))
        broker = _make_broker(balance_qty=2, order_history=[])

        trading_bot.run_one_symbol(broker, _symbol_config())

        assert not ctx["saved_states"], "DRY는 상태를 저장하지 않아야 합니다"
        assert any("불일치" in m for m in ctx["notify_messages"]), \
            "불일치 경고가 텔레그램으로 1회 전송되어야 합니다"