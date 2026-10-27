"""T 보정(FORCE_T / FORCE_T_REINFERENCE)의 실행 모드별 state 저장 원칙 회귀 테스트.

| 모드   | 목적                    | state 저장 |
|--------|-------------------------|-----------|
| DRY    | 전략·정산 미리보기       | 금지 (FORCE_T 가 설정되면 저장 없이 오류로 중단) |
| REPAIR | 명시적 state 복구·보정   | 허용 (주문 없음) |
| LIVE   | 실제 주문·정산           | 허용 |

- DRY + {SYMBOL}_FORCE_T  → 저장하지 않고 명시적 오류로 중단 (브로커 생성·조회 전)
- FORCE_T_REINFERENCE      → DRY 에서는 미리보기(저장 없음), REPAIR 에서는 실제 저장
- REPAIR 의 기존 복구 경로(FORCE_T / REINFERENCE)는 그대로 저장하고 주문을 내지 않음
- REPAIR_ACTION=FORCE_T 와 {SYMBOL}_FORCE_T 를 함께 설정하면 작업 소스 2개로 중단
"""
import os
import sys
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

_repo_root = os.path.join(os.path.dirname(__file__), "..")
for _p in (_repo_root, os.path.join(_repo_root, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as runtime_config
import trading_bot
from broker.base import Balance


def _state(**overrides):
    state = {
        "T": 3.0, "last_updated": "2026-07-13T12:18:20+00:00", "cycle_start_date": "",
        "effective_seed": 0.0, "last_processed_ordno": "", "additional_loc_odno": [],
        "orders_meta": {}, "balance_mismatch": {}, "state_version": "v2", "close_prices": [],
        "reverse_mode": {}, "pending_order_intent": None, "pending_order_batch": None,
    }
    state.update(overrides)
    return state


def _config(force_t=None):
    cfg = {"symbol": "TQQQ", "exchange": "NAS", "splits": 20, "symbol_type": "TQQQ",
           "seed": 8000.0, "additional_loc_levels": 3}
    if force_t is not None:
        cfg["force_t"] = force_t
    return cfg


def _fill(odno="ORD1", odt="2026-07-14T00:00:00+00:00", qty=1, price=50.0):
    return {
        "odno": odno, "ord_dt": odt[:10].replace("-", ""), "ord_tmd": "090000",
        "ord_datetime_utc": odt, "ft_ccld_qty": str(qty), "ft_ccld_unpr3": str(price),
        "ft_ccld_amt3": str(round(qty * price, 2)), "sll_buy_dvsn_cd_name": "매수",
    }


def _broker(balance_qty=1, history=()):
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = list(history)
    broker.get_balance.return_value = Balance(quantity=balance_qty, avg_price=50.0 if balance_qty else 0.0)
    broker.exchange_code.return_value = "NASD"
    return broker


def _fake_strategy(broker, **kwargs):
    return {"reverse_exit": False, "last_price": 50.0, "position_qty": 0, "avg_price": 0.0,
            "orderable_cash": 0.0, "star_point": None, "orders": []}


@pytest.fixture
def env(monkeypatch):
    saved, loaded = [], {}
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: deepcopy(loaded.get(symbol, _state())))
    monkeypatch.setattr(trading_bot, "save_state", lambda symbol, state: saved.append(deepcopy(state)))
    monkeypatch.setattr(trading_bot, "notify", lambda message, urgent=False: None)
    monkeypatch.setattr(trading_bot, "무한매수법_V4", _fake_strategy)
    # 모든 복구 소스를 끈 상태에서 시작 (실행 환경변수가 새어 들어오지 않도록)
    monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "")
    for flag in ("STATE_DIAGNOSTIC_ONLY", "STATE_REPAIR_ONLY", "STATE_CLEAR_FENCE_ONLY",
                 "STATE_REVERSE_AUDIT_ONLY", "STATE_REVERSE_RECONCILE_ONLY",
                 "STATE_NET_INVESTED_REPAIR_ONLY", "STATE_ASSUME_REVERSE_EXPIRY_ONLY",
                 "STATE_SET_CYCLE_START_ONLY"):
        monkeypatch.setattr(trading_bot, flag, False)
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", False)
    return {"saved": saved, "load": lambda sym, st: loaded.__setitem__(sym, st)}


def _no_broker(monkeypatch):
    def _fail():
        raise AssertionError("차단돼야 하는데 브로커가 생성됐습니다")
    monkeypatch.setattr(trading_bot, "create_broker", _fail)


# ═══════════════════════════════════════════════════════════════════
# 1) DRY + {SYMBOL}_FORCE_T → 저장 없이 명시적 오류
# ═══════════════════════════════════════════════════════════════════
def test_dry_force_t_in_run_one_symbol_raises_before_any_io(env, monkeypatch):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    broker = _broker()
    with pytest.raises(RuntimeError, match="TQQQ_FORCE_T.*REPAIR"):
        trading_bot.run_one_symbol(broker, _config(force_t=29))
    assert env["saved"] == []
    broker.get_order_history.assert_not_called()
    broker.get_balance.assert_not_called()


def test_dry_force_t_with_unresolved_reverse_orders_still_does_not_save(env, monkeypatch):
    """기존에는 미해결 리버스가 있으면 DRY 에서도 reconciliation_only 로 저장했습니다 — 이제 진입 전 차단."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    env["load"]("TQQQ", _state(orders_meta={"R1": {"reverse_action": "SELL", "terminal": False}}))
    with pytest.raises(RuntimeError):
        trading_bot.run_one_symbol(_broker(), _config(force_t=5))
    assert env["saved"] == []


def test_dry_force_t_in_main_exits_before_broker_creation(env, monkeypatch, capsys):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "DRY")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config(force_t=29)])
    _no_broker(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        trading_bot.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "TQQQ_FORCE_T" in out and "REPAIR" in out
    assert env["saved"] == []


def test_live_with_reinference_is_coerced_to_dry_then_force_t_is_rejected(env, monkeypatch):
    """LIVE + FORCE_T_REINFERENCE 는 DRY 로 고정되는데, 이때 FORCE_T 가 함께 있으면 저장 없이 중단합니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config(force_t=10)])
    _no_broker(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        trading_bot.main()
    assert exc.value.code == 1
    assert env["saved"] == []


def test_dry_without_force_t_is_not_blocked(env, monkeypatch):
    """회귀 방지: FORCE_T 가 없는 일반 DRY 는 기존대로 동작하고 저장하지 않습니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    env["load"]("TQQQ", _state(T=0.0))
    trading_bot.run_one_symbol(_broker(balance_qty=2), _config())
    assert env["saved"] == []


# ═══════════════════════════════════════════════════════════════════
# 2) FORCE_T_REINFERENCE: DRY = 미리보기(저장 없음), REPAIR = 실제 저장
# ═══════════════════════════════════════════════════════════════════
def test_reinference_in_dry_is_preview_only(env, monkeypatch):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "DRY")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    env["load"]("TQQQ", _state(T=0.0, last_updated="2026-07-10T00:00:00+00:00", cycle_start_date="2026-06-01"))
    broker = _broker(balance_qty=1, history=[_fill()])
    trading_bot.run_one_symbol(broker, _config())
    assert env["saved"] == [], "DRY 재추정은 미리보기이므로 state 를 저장하면 안 됩니다"
    broker.get_order_history.assert_called()  # 미리보기는 이력을 실제로 읽음


def test_reinference_in_repair_saves_and_places_no_orders(env, monkeypatch):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config()])
    env["load"]("TQQQ", _state(T=0.0, last_updated="2026-07-10T00:00:00+00:00", cycle_start_date="2026-06-01"))
    broker = _broker(balance_qty=1, history=[_fill()])
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
    trading_bot._dispatch_repair()
    assert len(env["saved"]) == 1, "REPAIR 재추정은 결과를 state 에 저장해야 합니다"
    broker.place_order.assert_not_called()
    broker.cancel_order.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 3) REPAIR 의 기존 복구 경로 유지
# ═══════════════════════════════════════════════════════════════════
def test_repair_force_t_still_saves_and_places_no_orders(env, monkeypatch):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config(force_t=29)])
    env["load"]("TQQQ", _state(T=3.0))
    broker = _broker(history=[_fill()])
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
    trading_bot._dispatch_repair()
    assert len(env["saved"]) == 1
    assert env["saved"][0]["T"] == 29
    assert env["saved"][0]["orders_meta"] == {}
    broker.place_order.assert_not_called()


def test_repair_action_force_t_alone_is_a_noop_without_symbol_force_t(env, monkeypatch, capsys):
    """현재 구현의 한계: REPAIR_ACTION=FORCE_T 단독은 종목별 FORCE_T 값이 없어 아무것도 적용하지 않습니다.

    값은 `{SYMBOL}_FORCE_T` 에서만 읽는데, 함께 설정하면 작업 소스 2개로 중단됩니다(아래 테스트).
    따라서 실제로 동작하는 T 강제 보정은 `TRADE_MODE=REPAIR TQQQ_FORCE_T=29` (레거시 단일 소스) 입니다.
    """
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "FORCE_T")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config()])
    env["load"]("TQQQ", _state(T=3.0))
    broker = _broker()
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
    trading_bot._dispatch_repair()
    assert env["saved"] == []
    assert "FORCE_T 미설정" in capsys.readouterr().out


def test_repair_action_force_t_plus_symbol_force_t_counts_two_sources(env, monkeypatch, capsys):
    """주의: REPAIR_ACTION=FORCE_T 와 {SYMBOL}_FORCE_T 를 함께 설정하면 작업 소스 2개로 중단합니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "FORCE_T")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config(force_t=29)])
    _no_broker(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    assert "2개" in capsys.readouterr().out
    assert env["saved"] == []
