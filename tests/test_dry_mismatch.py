"""
DRY 모드 불일치 가드 무력화 테스트.

배경: DRY(프리뷰/진단) 실행은 주문을 내지 않으므로 이력-잔고 불일치가
발생해도 상태 기록/자동보정/중단 없이 경고만 남기고 프리뷰를 계속 진행해야
합니다. LIVE에서는 기존처럼 기록 + 중단(RuntimeError)이 유지되어야 합니다.

검증 대상 분기 (trading_bot.run_one_symbol):
- branch 1: 이력 N주 vs 잔고 0  → DRY: 경고 + 사이클 종료 저장 skip
- branch 2: 이력 0 vs 잔고 N    → DRY: 경고만, 기록 없음 / LIVE: 기록 + raise
- branch 3: T=0 vs 실보유 N     → DRY: 경고만, 기록 없음
- FORCE_T + 불일치: DRY에서 FORCE_T 블록이 정상 적용되어야 함
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
def reconcile_env(monkeypatch):
    """상태 I/O·알림·전략을 mock으로 대체해 run_one_symbol을 직접 호출합니다."""
    saved_states = []
    notify_messages = []
    loaded = {}

    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: loaded[symbol])
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


class TestDryBranch2:
    """이력 0 vs 잔고 N — 실제 로그 사례 (history=0, broker=2)."""

    def test_dry_continues_without_record(self, reconcile_env, monkeypatch):
        """DRY: raise 없이 프리뷰 계속 + balance_mismatch 미기록."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        ctx = reconcile_env
        ctx["load"]("TQQQ", _make_state(T=0.0))
        broker = _make_broker(balance_qty=2, order_history=[])

        trading_bot.run_one_symbol(broker, _symbol_config())

        assert not ctx["saved_states"], "DRY는 상태를 저장하지 않아야 합니다"
        assert any("불일치" in m for m in ctx["notify_messages"]), \
            "불일치 경고가 텔레그램으로 1회 전송되어야 합니다"

    def test_live_raises_and_records(self, reconcile_env, monkeypatch):
        """LIVE: 기존대로 기록 + RuntimeError raise (회귀 방지)."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        ctx = reconcile_env
        ctx["load"]("TQQQ", _make_state(T=0.0))
        broker = _make_broker(balance_qty=2, order_history=[])

        with pytest.raises(RuntimeError, match="불일치"):
            trading_bot.run_one_symbol(broker, _symbol_config())

        assert any(
            s.get("balance_mismatch", {}).get("note") == "requires-attention"
            for s in ctx["saved_states"]
        ), "LIVE는 balance_mismatch를 기록해야 합니다"

    def test_dry_with_force_t_applies(self, reconcile_env, monkeypatch):
        """DRY + FORCE_T + 불일치: 가드가 무력화되므로 FORCE_T가 정상 적용됩니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        ctx = reconcile_env
        ctx["load"]("TQQQ", _make_state(T=0.0))
        broker = _make_broker(balance_qty=2, order_history=[])

        trading_bot.run_one_symbol(broker, _symbol_config(force_t=5.0))

        assert any(s["T"] == 5.0 for s in ctx["saved_states"]), \
            "DRY에서도 FORCE_T가 적용되어 저장되어야 합니다"


class TestDryBranch1:
    """이력 N주 vs 잔고 0 — 사이클 종료 경로와의 상호작용."""

    def test_dry_cycle_end_does_not_save(self, reconcile_env, monkeypatch):
        """DRY: 사이클 종료 리포트만 표시하고 캐시(T 리셋/시드) 저장을 생략합니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        ctx = reconcile_env
        ctx["load"]("TQQQ", _make_state(T=5.0, last_updated="2026-07-13T00:00:00+00:00"))
        broker = _make_broker(balance_qty=0, order_history=[_buy_fill()])

        trading_bot.run_one_symbol(broker, _symbol_config())

        assert len(ctx["saved_states"]) == 0, \
            "DRY 사이클 종료는 캐시를 저장하지 않아야 합니다"
        # 리포트는 로그/알림으로 제공됩니다
        assert any("사이클" in m or "🏁" in m for m in ctx["notify_messages"])


class TestDryBranch3:
    """T=0 vs 실보유 N — T 오추정 의심 케이스."""

    def test_dry_continues_without_record(self, reconcile_env, monkeypatch):
        """DRY: 경고만 남기고 프리뷰 계속 + 기록 없음."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        ctx = reconcile_env
        # 잔고가 이력과 일치(comp==live)하므로 branch 2가 아닌 branch 3가 발동합니다.
        # buy가 last_updated(07-10) 이전이라 T에는 반영되지 않고(T=0),
        # compute_position_from_history에는 반영되어 comp=1 입니다.
        ctx["load"]("TQQQ", _make_state(
            T=0.0,
            last_updated="2026-07-10T00:00:00+00:00",
            cycle_start_date="2026-06-01",
        ))
        broker = _make_broker(balance_qty=1, order_history=[_buy_fill(odt="2026-07-01T00:00:00+00:00")])

        trading_bot.run_one_symbol(broker, _symbol_config())

        for saved in ctx["saved_states"]:
            assert saved.get("balance_mismatch", {}) == {}, \
                "DRY는 balance_mismatch를 기록하지 않아야 합니다"
        assert any("T=0이지만" in m for m in ctx["notify_messages"]), \
            "T 오추정 경고가 전송되어야 합니다"
