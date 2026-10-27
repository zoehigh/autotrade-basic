"""T 보정(FORCE_T / FORCE_T_REINFERENCE)의 실행 모드별 state 저장 원칙 회귀 테스트.

| 모드   | 목적                    | state 저장 |
|--------|-------------------------|-----------|
| DRY    | 전략·정산 미리보기       | 금지 (FORCE_T 가 설정되면 저장 없이 오류로 중단) |
| REPAIR | 명시적 state 복구·보정   | 허용 (주문 없음) |
| LIVE   | 실제 주문·정산           | 허용 (단, FORCE_T 는 금지 — 복구와 주문이 결합되므로 브로커 생성 전 종료 코드 1) |

- DRY/LIVE + {SYMBOL}_FORCE_T → 저장·브로커 호출·주문 없이 명시적 오류로 중단 (FORCE_T 는 REPAIR 전용)
- FORCE_T_REINFERENCE      → DRY(LIVE 는 DRY 로 고정)에서는 미리보기(저장 없음), REPAIR 에서는 실제 저장
- REPAIR 재추정: 재추정 T 가 MAX_T 를 넘으면 (자르지 않고) 저장 거부, 다종목은 전부 검증 후에만 저장
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
    saved, loaded, save_calls = [], {}, []
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: deepcopy(loaded.get(symbol, _state())))
    monkeypatch.setattr(trading_bot, "save_state", lambda symbol, state: saved.append(deepcopy(state)))

    def _save_states(updates):
        save_calls.append(sorted(updates))  # 파일 쓰기 1회 = 호출 1회
        saved.extend(deepcopy(st) for st in updates.values())
    monkeypatch.setattr(trading_bot, "save_states", _save_states)
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
    return {"saved": saved, "save_calls": save_calls, "load": lambda sym, st: loaded.__setitem__(sym, st)}


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


def test_repair_action_force_t_alone_fails_explicitly(env, monkeypatch, capsys):
    """REPAIR_ACTION=FORCE_T 단독은 값을 읽을 곳이 없어 무효 호출입니다 — 성공처럼 끝나면 안 됩니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(trading_bot, "REPAIR_ACTION", "FORCE_T")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config()])
    env["load"]("TQQQ", _state(T=3.0))
    _no_broker(monkeypatch)  # 브로커 생성 전에 실패해야 함
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "실패" in out and "TRADE_MODE=REPAIR TQQQ_FORCE_T" in out
    assert env["saved"] == []


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


# ═══════════════════════════════════════════════════════════════════
# 4) LIVE + {SYMBOL}_FORCE_T 금지 (복구와 주문의 결합 차단)
# ═══════════════════════════════════════════════════════════════════
def test_live_force_t_in_main_exits_1_before_broker_creation(env, monkeypatch, capsys):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config(force_t=20)])
    _no_broker(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        trading_bot.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "TQQQ_FORCE_T" in out and "LIVE" in out and "REPAIR" in out
    assert env["saved"] == []


def test_live_force_t_in_run_one_symbol_raises_before_any_io_or_order(env, monkeypatch):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    broker = _broker()
    with pytest.raises(RuntimeError, match="LIVE.*REPAIR"):
        trading_bot.run_one_symbol(broker, _config(force_t=20))
    assert env["saved"] == []
    broker.get_order_history.assert_not_called()
    broker.get_balance.assert_not_called()
    broker.place_order.assert_not_called()
    broker.cancel_order.assert_not_called()


def test_live_force_t_with_unresolved_reverse_orders_does_not_save(env, monkeypatch):
    """기존에는 LIVE 에서 미해결 리버스가 있으면 reconciliation_only 를 저장한 뒤 중단했습니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    env["load"]("TQQQ", _state(orders_meta={"R1": {"reverse_action": "SELL", "terminal": False}}))
    with pytest.raises(RuntimeError):
        trading_bot.run_one_symbol(_broker(), _config(force_t=5))
    assert env["saved"] == []


def test_live_without_force_t_is_not_blocked_by_the_guard():
    """가드는 FORCE_T 가 설정된 경우에만 동작합니다 (일반 LIVE 는 영향 없음)."""
    assert trading_bot._force_t_mode_block_message("LIVE", [_config()]) == ""
    assert trading_bot._force_t_mode_block_message("REPAIR", [_config(force_t=3)]) == ""
    assert trading_bot._force_t_mode_block_message("LIVE", [_config(force_t=3)]) != ""
    assert trading_bot._force_t_mode_block_message("DRY", [_config(force_t=3)]) != ""


def test_live_with_reinference_message_says_preview_and_no_save(env, monkeypatch, capsys):
    """LIVE + FORCE_T_REINFERENCE 는 DRY 미리보기로 고정되며, 로그가 '저장되지 않음'을 분명히 알려야 합니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_config()])

    class _Stop(Exception):
        pass

    def _stop():
        raise _Stop()
    monkeypatch.setattr(trading_bot, "create_broker", _stop)
    with pytest.raises(_Stop):
        trading_bot.main()
    out = capsys.readouterr().out
    assert "미리보기" in out and "저장되지 않" in out and "REPAIR" in out
    assert trading_bot.TRADE_MODE == "DRY"
    assert env["saved"] == []


# ═══════════════════════════════════════════════════════════════════
# 5) REPAIR 재추정의 MAX_T: 상한 초과 시 (자르지 않고) 저장 거부, 다종목은 사전 검증
# ═══════════════════════════════════════════════════════════════════
def _repair_reinference_env(env, monkeypatch, configs, t_by_symbol):
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "TRADE_MODE", "REPAIR")
    monkeypatch.setattr(runtime_config, "FORCE_T_REINFERENCE", True)
    monkeypatch.setattr(trading_bot, "SYMBOLS", configs)
    broker = _broker(history=[_fill()])
    monkeypatch.setattr(trading_bot, "create_broker", lambda: broker)
    monkeypatch.setattr(
        trading_bot, "update_T_from_history",
        lambda symbol, state, history, balance_qty=None: {**state, "T": t_by_symbol[symbol]},
    )
    return broker


def _cfg(symbol, max_t):
    c = _config()
    c["symbol"] = symbol
    c["max_t"] = max_t
    return c


def test_reinference_above_max_t_is_rejected_without_saving(env, monkeypatch, capsys):
    """추정 T=24 > MAX_T=20: 저장 거부, state 변경 없음, 주문 0건, 종료 코드 1 (20 으로 자르지 않음)."""
    broker = _repair_reinference_env(env, monkeypatch, [_cfg("TQQQ", 20.0)], {"TQQQ": 24.0})
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "T=24.0" in out and "MAX_T=20.0" in out and "저장을 거부" in out
    assert env["saved"] == []
    broker.place_order.assert_not_called()


@pytest.mark.parametrize("t,max_t", [(20.0, 20.0), (19.5, 20.0), (0.0, 20.0)])
def test_reinference_at_or_below_max_t_still_saves(env, monkeypatch, t, max_t):
    broker = _repair_reinference_env(env, monkeypatch, [_cfg("TQQQ", max_t)], {"TQQQ": t})
    trading_bot._dispatch_repair()
    assert [st["T"] for st in env["saved"]] == [t]
    broker.place_order.assert_not_called()


def test_reinference_without_max_t_keeps_existing_behavior(env, monkeypatch):
    _repair_reinference_env(env, monkeypatch, [_config()], {"TQQQ": 33.0})
    trading_bot._dispatch_repair()
    assert [st["T"] for st in env["saved"]] == [33.0]


def test_multi_symbol_failure_saves_no_symbol_at_all(env, monkeypatch, capsys):
    """첫 종목은 정상, 둘째가 상한 초과여도 첫 종목조차 먼저 저장하지 않습니다 (부분 복구 방지)."""
    configs = [_cfg("TQQQ", 20.0), _cfg("SOXL", 20.0)]
    _repair_reinference_env(env, monkeypatch, configs, {"TQQQ": 10.0, "SOXL": 25.0})
    with pytest.raises(SystemExit) as exc:
        trading_bot._dispatch_repair()
    assert exc.value.code == 1
    assert env["saved"] == [], "검증 실패 시 정상 종목도 저장되면 안 됩니다"
    out = capsys.readouterr().out
    assert "SOXL" in out and "어떤 종목도 저장하지 않습니다" in out


def test_multi_symbol_all_valid_saves_all(env, monkeypatch):
    configs = [_cfg("TQQQ", 20.0), _cfg("SOXL", 20.0)]
    _repair_reinference_env(env, monkeypatch, configs, {"TQQQ": 10.0, "SOXL": 20.0})
    trading_bot._dispatch_repair()
    assert sorted(st["T"] for st in env["saved"]) == [10.0, 20.0]


@pytest.mark.parametrize("bad_max", [float("nan"), float("inf"), -1.0, "20", True])
def test_invalid_max_t_is_rejected_without_saving(env, monkeypatch, bad_max):
    _repair_reinference_env(env, monkeypatch, [_cfg("TQQQ", bad_max)], {"TQQQ": 10.0})
    with pytest.raises(SystemExit):
        trading_bot._dispatch_repair()
    assert env["saved"] == []


@pytest.mark.parametrize("bad_t", [float("nan"), float("inf"), None, "5"])
def test_invalid_estimated_t_is_rejected_without_saving(env, monkeypatch, bad_t):
    _repair_reinference_env(env, monkeypatch, [_cfg("TQQQ", 20.0)], {"TQQQ": bad_t})
    with pytest.raises(SystemExit):
        trading_bot._dispatch_repair()
    assert env["saved"] == []


def test_dry_preview_shows_raw_and_capped_t(env, monkeypatch, capsys):
    """DRY 미리보기는 원시 추정 T 와 상한 적용 후 T 를 함께 보여줍니다."""
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    env["load"]("TQQQ", _state(T=0.0, last_updated="2026-07-10T00:00:00+00:00", cycle_start_date="2026-06-01"))
    cfg = _config()
    cfg["max_t"] = 20.0
    trading_bot.run_one_symbol(_broker(balance_qty=1, history=[_fill()]), cfg)
    out = capsys.readouterr().out
    assert "원시 추정 T=" in out and "상한 MAX_T=20.0" in out and "적용 T=" in out
    assert env["saved"] == []


# ═══════════════════════════════════════════════════════════════════
# 6) 운영 안전: 워크플로가 항상 넘기는 빈 FORCE_T env 가 LIVE 차단을 오발동시키지 않음
# ═══════════════════════════════════════════════════════════════════
def _parsed_symbols(monkeypatch, **env):
    base = {"SYMBOLS": "TQQQ:NAS", "TQQQ_SEED": "8000", "SOXL_SEED": "8000"}
    for key in ("TQQQ_FORCE_T", "SOXL_FORCE_T", "TQQQ_MAX_T", "SOXL_MAX_T"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    return runtime_config._parse_symbols()


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_force_t_env_from_workflow_does_not_block_live(monkeypatch, raw):
    """trade_base.yml 은 `TQQQ_FORCE_T: ${{ vars.TQQQ_FORCE_T }}` 를 항상 넘깁니다 (미설정이면 빈 문자열)."""
    symbols = _parsed_symbols(monkeypatch, TQQQ_FORCE_T=raw)
    assert symbols[0]["force_t"] is None
    assert trading_bot._force_t_mode_block_message("LIVE", symbols) == ""
    assert trading_bot._force_t_mode_block_message("DRY", symbols) == ""


def test_force_t_zero_reset_is_blocked_in_live_and_dry(monkeypatch):
    """FORCE_T=0(사이클 리셋)은 0.0 이라 'is not None' 으로 판정돼 DRY·LIVE 에서 차단됩니다."""
    symbols = _parsed_symbols(monkeypatch, TQQQ_FORCE_T="0")
    assert symbols[0]["force_t"] == 0.0
    assert trading_bot._force_t_mode_block_message("LIVE", symbols) != ""
    assert trading_bot._force_t_mode_block_message("DRY", symbols) != ""
    assert trading_bot._force_t_mode_block_message("REPAIR", symbols) == ""


def test_max_t_defaults_to_splits_when_unset(monkeypatch):
    """MAX_T 는 항상 채워집니다(기본 분할수) — REPAIR 재추정 상한 검사가 모든 종목에 적용됩니다."""
    symbols = _parsed_symbols(monkeypatch, TQQQ_SPLITS="20")
    assert symbols[0]["max_t"] == 20.0
