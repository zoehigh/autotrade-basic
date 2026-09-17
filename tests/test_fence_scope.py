"""
주문 실패 예외 범위(SymbolScopedError / GlobalScopedError) 분류 테스트.

배경: 불확실 실패(네트워크 오류 등)와 state 저장 실패가 모두 "fence를 유지"
RuntimeError 하나로 합쳐져 main()이 문자열 marker로 전체 중단 여부를 판단했습니다.
state 파일은 종목 레코드만 분리된 단일 .state.json이라 저장 실패는 공통 장애입니다.

분류 규칙:
- SymbolScopedError: 해당 종목만 중단, 다음 종목 계속 (브로커 불확실 실패)
- GlobalScopedError: 전체 중단 (주문 전 intent/주문 후 checkpoint 저장 실패)
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
from trading_bot import GlobalScopedError, SymbolScopedError
from broker.base import Balance, OrderError, OrderResult


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


def _symbol_config(symbol="TQQQ"):
    return {
        "symbol": symbol,
        "exchange": "NAS",
        "splits": 20,
        "symbol_type": symbol,
        "seed": 8000.0,
        "additional_loc_levels": 3,
    }


def _fake_strategy(broker, **kwargs):
    return {
        "reverse_exit": False,
        "last_price": 50.0,
        "position_qty": 0,
        "avg_price": 0.0,
        "orderable_cash": 1000.0,
        "star_point": None,
        "orders": [
            {
                "comment": "테스트 매수",
                "side": "BUY",
                "quantity": 1,
                "price": 50.0,
                "order_type": "LIMIT",
            }
        ],
    }


def _make_broker():
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = []
    broker.get_balance.return_value = Balance(quantity=0, avg_price=0.0)
    broker.exchange_code.return_value = "NASD"
    broker.close = MagicMock()
    return broker


# ─────────────────────────────────────────────────────────
# 예외 클래스 계층
# ─────────────────────────────────────────────────────────

class TestExceptionHierarchy:
    def test_both_are_runtime_error_subclasses(self):
        assert issubclass(SymbolScopedError, RuntimeError)
        assert issubclass(GlobalScopedError, RuntimeError)

    def test_scopes_are_distinct(self):
        assert not issubclass(SymbolScopedError, GlobalScopedError)
        assert not issubclass(GlobalScopedError, SymbolScopedError)


# ─────────────────────────────────────────────────────────
# run_one_symbol 주문 루프 — 예외 범위 분류
# ─────────────────────────────────────────────────────────

@pytest.fixture
def order_loop_env(monkeypatch):
    """run_one_symbol을 LIVE로 직접 호출해 주문 루프까지 도달시킵니다."""
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
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    # 실전 pre-market 대기(수면)를 피하기 위해 demo로 고정합니다.
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")
    return {
        "saved_states": saved_states,
        "notify_messages": notify_messages,
        "load": lambda symbol, state: loaded.__setitem__(symbol, state),
        "set_save": lambda fn: monkeypatch.setattr(trading_bot, "save_state", fn),
    }


class TestOrderLoopScope:
    def test_network_order_error_raises_symbol_scoped(self, order_loop_env):
        """네트워크 OrderError(불확실) → SymbolScopedError + fence 유지 문구."""
        ctx = order_loop_env
        ctx["load"]("TQQQ", _make_state())
        broker = _make_broker()
        broker.place_order.side_effect = OrderError("timed out")

        with pytest.raises(SymbolScopedError, match="fence를 유지"):
            trading_bot.run_one_symbol(broker, _symbol_config())

    def test_missing_order_id_raises_symbol_scoped(self, order_loop_env):
        """주문번호 누락(불확실) → SymbolScopedError."""
        ctx = order_loop_env
        ctx["load"]("TQQQ", _make_state())
        broker = _make_broker()
        broker.place_order.return_value = OrderResult(
            order_id="", order_time="2026-01-01", is_reservation=False
        )

        with pytest.raises(SymbolScopedError, match="유효한 주문번호가 없습니다"):
            trading_bot.run_one_symbol(broker, _symbol_config())

    def test_no_order_response_raises_symbol_scoped(self, order_loop_env):
        """접수 응답 없음(불확실) → SymbolScopedError."""
        ctx = order_loop_env
        ctx["load"]("TQQQ", _make_state())
        broker = _make_broker()
        broker.place_order.return_value = None

        with pytest.raises(SymbolScopedError, match="주문 접수 응답이 없어"):
            trading_bot.run_one_symbol(broker, _symbol_config())

    def test_intent_save_failure_raises_global_scoped(self, order_loop_env):
        """주문 전 intent 저장 실패(공통 장애) → GlobalScopedError."""
        ctx = order_loop_env
        ctx["load"]("TQQQ", _make_state())

        def _save_raising_on_intent(symbol, state):
            if state.get("pending_order_intent") is not None:
                raise OSError("disk full")
            ctx["saved_states"].append(deepcopy(state))

        ctx["set_save"](_save_raising_on_intent)
        broker = _make_broker()
        broker.place_order.return_value = OrderResult(
            order_id="ORD1", order_time="2026-01-01", is_reservation=False
        )

        with pytest.raises(GlobalScopedError, match="intent checkpoint 실패"):
            trading_bot.run_one_symbol(broker, _symbol_config())

    def test_checkpoint_save_failure_raises_global_scoped(self, order_loop_env):
        """주문 수락 후 checkpoint 저장 실패(공통 장애) → GlobalScopedError."""
        ctx = order_loop_env
        ctx["load"]("TQQQ", _make_state())

        def _save_raising_on_orders_meta(symbol, state):
            if state.get("orders_meta"):
                raise OSError("disk full")
            ctx["saved_states"].append(deepcopy(state))

        ctx["set_save"](_save_raising_on_orders_meta)
        broker = _make_broker()
        broker.place_order.return_value = OrderResult(
            order_id="ORD1", order_time="2026-01-01", is_reservation=False
        )

        with pytest.raises(GlobalScopedError, match="checkpoint에 실패했습니다"):
            trading_bot.run_one_symbol(broker, _symbol_config())


# ─────────────────────────────────────────────────────────
# main() 종목 루프 — 스코프별 진행/중단
# ─────────────────────────────────────────────────────────

class TestMainScope:
    def _patch_main(self, monkeypatch, run_one_symbol_impl):
        calls = []
        notify_messages = []

        def fake_run_one_symbol(broker, cfg):
            calls.append(cfg["symbol"])
            run_one_symbol_impl(broker, cfg)

        monkeypatch.setattr(trading_bot, "run_one_symbol", fake_run_one_symbol)
        monkeypatch.setattr(
            trading_bot, "SYMBOLS",
            [_symbol_config("TQQQ"), _symbol_config("SOXL")],
        )
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(trading_bot, "create_broker", lambda: MagicMock())
        monkeypatch.setattr(trading_bot, "load_state", lambda symbol: {})
        monkeypatch.setattr(
            trading_bot, "notify",
            lambda message, urgent=False: notify_messages.append(message),
        )
        monkeypatch.setattr(trading_bot.time, "sleep", lambda s: None)
        return calls, notify_messages

    def test_symbol_scoped_error_continues_to_next_symbol(self, monkeypatch):
        """SymbolScopedError → 로그/알림 후 다음 종목까지 처리됩니다."""
        def impl(broker, cfg):
            if cfg["symbol"] == "TQQQ":
                raise SymbolScopedError(
                    "주문 결과를 확정할 수 없어 주문 fence를 유지합니다."
                )

        calls, notify_messages = self._patch_main(monkeypatch, impl)

        trading_bot.main()

        assert calls == ["TQQQ", "SOXL"], \
            "SymbolScopedError는 해당 종목만 중단하고 다음 종목으로 계속 진행해야 합니다"
        assert any("fence를 유지" in m for m in notify_messages), \
            "종목 오류 알림에 fence 유지 문구가 포함되어야 합니다"

    def test_global_scoped_error_stops_all(self, monkeypatch):
        """GlobalScopedError → 전체 중단 (다음 종목 미처리 + 치명 종료)."""
        def impl(broker, cfg):
            if cfg["symbol"] == "TQQQ":
                raise GlobalScopedError("주문 전 intent checkpoint 실패: disk full")

        calls, _ = self._patch_main(monkeypatch, impl)
        exited = []
        monkeypatch.setattr(trading_bot.sys, "exit", lambda code: exited.append(code))

        trading_bot.main()

        assert calls == ["TQQQ"], \
            "GlobalScopedError는 전체 실행을 중단해야 합니다 (SOXL 미처리)"
        assert exited == [1], "치명 에러로 sys.exit(1)이 호출되어야 합니다"