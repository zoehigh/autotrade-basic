"""리버스 종료·재진입이 실제 파이프라인(run_one_symbol, LIVE)에서 상태·fence·메타데이터를 올바르게 남기는지 검증합니다.

전략은 실제 `무한매수법_V4` 를 쓰고 브로커만 모의합니다. 외부 호출·실주문 없음.
"""
import os
import sys
from copy import deepcopy
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

_repo_root = os.path.join(os.path.dirname(__file__), "..")
_src_path = os.path.join(_repo_root, "src")
for _p in (_repo_root, _src_path):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import trading_bot
from broker.base import Balance, OrderResult, PurchaseAmount, StockPrice, StockQuotation
from broker.market_utils import us_session_last_closed_date


def _et_today():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _config():
    return {"symbol": "TQQQ", "exchange": "NAS", "splits": 20, "symbol_type": "TQQQ",
            "seed": 9696.18, "additional_loc_levels": 3}


def _initial_state(**over):
    """진입 직전 상태: 직전 정산까지 반영, T>19, 리버스 미시작."""
    state = {
        "T": 19.7734, "last_updated": "2026-11-10T09:30:00+00:00", "cycle_start_date": "",
        "effective_seed": 0.0, "last_processed_ordno": "", "additional_loc_odno": [],
        "balance_mismatch": {}, "state_version": "v2", "close_prices": [59.0],
        "net_invested": 8000.0, "net_invested_status": "valid", "position_qty": 144,
        "pending_order_intent": None, "pending_order_batch": None,
        "reverse_mode": {}, "orders_meta": {},
    }
    state.update(over)
    return state


def _moc_close_utc():
    """MOC 는 정규장 마감(16:00 ET)에 체결됩니다 — 오늘(ET) 세션의 마감 시각을 UTC 로."""
    close_et = datetime.now(ZoneInfo("America/New_York")).replace(hour=16, minute=0, second=0, microsecond=0)
    return close_et.astimezone(ZoneInfo("UTC")).isoformat()


def _row(odno, side, qty, filled, price, utc, ord_dt, status):
    return {"odno": odno, "sll_buy_dvsn_cd_name": side, "ft_ord_qty": str(qty), "ft_ccld_qty": str(filled),
            "ft_ccld_unpr3": f"{price:.2f}", "ft_ccld_amt3": f"{filled * price:.2f}",
            "nccs_qty": str(qty - filled if status in ("미체결", "부분체결") else 0),
            "prcs_stat_name": status, "ord_dt": ord_dt, "ord_tmd": "041500", "ord_datetime_utc": utc,
            "ord_datetime_kst": utc, "tr_mket_name": "NASD", "ovrs_excg_cd": "NASD"}


_ORDER_SEQ = {"n": 0}


@pytest.fixture
def env(monkeypatch):
    saved = []
    holder = {}
    monkeypatch.setattr("strategy.get_finnhub_ma5", lambda symbol: None)
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: holder["state"])
    monkeypatch.setattr(trading_bot, "save_state", lambda symbol, state: saved.append(deepcopy(state)))
    monkeypatch.setattr(trading_bot, "notify", lambda message, urgent=False, **kw: None)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")   # 실전 pre-market 대기(sleep) 회피
    return holder, saved


def _broker(history, last=59.26, qty=144, avg=65.5732, cash=177.5, close=59.26, candle_date="auto"):
    broker = MagicMock()
    broker.name = "mock"
    broker.is_trading_day.return_value = True
    broker.get_order_history.return_value = history
    broker.exchange_code.return_value = "NASD"
    broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=last)
    broker.get_stock_price.return_value = StockPrice(open=last, last=last)
    broker.get_balance.return_value = Balance(quantity=qty, avg_price=avg)
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=cash)
    if close is None:
        broker.get_daily_candles.side_effect = RuntimeError("일봉 조회 실패")
    else:
        date = us_session_last_closed_date() if candle_date == "auto" else candle_date
        broker.get_daily_candles.return_value = [{"date": date, "close": close}]

    def _place(symbol, exch, side, qty_, price, order_type):
        _ORDER_SEQ["n"] += 1      # 브로커 인스턴스가 바뀌어도 주문번호는 겹치지 않는다
        return OrderResult(order_id=f"20261112:{_ORDER_SEQ['n']:08d}", order_time="20261112183020",
                           is_reservation=False)

    broker.place_order.side_effect = _place
    return broker


def _buy_history(qty=144):
    return [_row("20261105:00000001", "매수", qty, qty, 65.5732, "2026-11-05T09:30:00+00:00", "20261105", "체결")]


def _placed(broker):
    return [(c.args[2], c.args[3], c.args[5]) for c in broker.place_order.call_args_list]   # (side, qty, type)


def _day1(env):
    """실제 파이프라인으로 1일차를 실행: T>19 진입 첫날 → MOC 14주 접수. (상태는 파이프라인이 저장한 그대로)"""
    holder, saved = env
    holder["state"] = _initial_state()
    broker = _broker(_buy_history(144), close=None)      # 첫날은 확정 종가를 조회하지 않는다 (조회 실패여도 무관)
    trading_bot.run_one_symbol(broker, _config())
    assert _placed(broker) == [("SELL", 14, "MOC")]
    state = deepcopy(saved[-1])
    odno = next(iter(state["orders_meta"]))
    return state, odno


def _next_session(state):
    """다음 거래일로 간주: 동일 세션 중복 가드(last_planned_session)를 어제로 돌린다."""
    state = deepcopy(state)
    state["reverse_mode"]["last_planned_session"] = "2000-01-03"
    return state


def _day2_history(odno, filled, status, held):
    rows = _buy_history(144)
    rows.append(_row(odno, "매도", 14, filled, 59.26, _moc_close_utc(), datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d"), status))
    return rows


def test_day1_entry_registers_reverse_metadata_and_fence_cleared(env):
    """진입 첫날(종료선 위 종가여도): MOC 14주 + 리버스 메타(1일차, cycle_id) + fence 해제 + T 불변."""
    state, odno = _day1(env)
    meta = state["orders_meta"][odno]
    assert meta["reverse_action"] == "sell" and meta["reverse_day"] == 1
    assert meta["cycle_id"] == state["reverse_mode"]["cycle_id"] and meta["reverse_base_t"] == 19.7734
    assert meta["total_qty"] == 14 and meta["processed_filled_qty"] == 0
    assert state["reverse_mode"]["active"] is True and state["reverse_mode"]["last_planned_session"] == _et_today()
    assert state["pending_order_batch"] is None and state["pending_order_intent"] is None
    assert state["T"] == 19.7734, "T 는 계획만으로 바뀌지 않습니다"


def test_full_pipeline_fill_then_exit_day_places_normal_orders(env):
    """1일차 MOC 전량 체결 → 다음 거래일 종료 판정 → 같은 실행에서 일반 주문, 일반 메타(리버스 필드 없음), 상태 정리."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 14, "체결", 130), qty=130, cash=2000.0, close=59.26)

    trading_bot.run_one_symbol(broker, _config())

    placed = _placed(broker)
    assert placed and all(t in ("LOC", "LIMIT") for _, _, t in placed), placed
    assert any(side == "BUY" for side, _, _ in placed)
    final = saved[-1]
    assert final["T"] == round(19.7734 * 0.9, 4), "T 는 체결 반영(×0.9)으로만 바뀝니다"
    assert final["reverse_mode"] == {}
    new_metas = [m for k, m in final["orders_meta"].items() if k != odno]
    assert len(new_metas) == len(placed)
    assert not any("reverse_action" in m or "cycle_id" in m for m in new_metas)
    assert final["orders_meta"][odno]["terminal"] is True
    assert final["pending_order_batch"] is None and final["pending_order_intent"] is None
    assert broker.get_stock_price.call_count == 1, "재귀 호출이 시세를 다시 조회하면 안 됩니다"


def test_one_share_fill_then_exit_opens_new_cycle_day1(env):
    """1주만 체결·잔량 취소(T>19 유지) → 종료 판정일에 '남은 수량 재시도'가 아니라 새 cycle 의 1일차 MOC(현재 보유량 1/10)."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 1, "취소", 143), qty=143, cash=177.5, close=59.26)

    trading_bot.run_one_symbol(broker, _config())

    assert _placed(broker) == [("SELL", 14, "MOC")]
    final = saved[-1]
    expected_t = round(19.7734 * (1 - 0.1 * 1 / 14), 4)
    assert final["T"] == expected_t and expected_t > 19
    assert final["orders_meta"][odno]["terminal"] is True
    rev = final["reverse_mode"]
    assert rev["active"] is True and rev["cycle_id"] != state["reverse_mode"]["cycle_id"]
    assert rev["last_planned_session"] == _et_today() and rev.get("day_count", 0) == 0
    new = [m for k, m in final["orders_meta"].items() if k != odno]
    assert len(new) == 1 and new[0]["reverse_day"] == 1 and new[0]["cycle_id"] == rev["cycle_id"]
    assert new[0]["reverse_base_t"] == expected_t and new[0]["total_qty"] == 14
    assert final["orders_meta"][odno]["cycle_id"] == state["reverse_mode"]["cycle_id"], "이전 cycle 메타 보존"
    assert final["pending_order_batch"] is None and final["pending_order_intent"] is None


def test_same_session_rerun_after_reentry_places_nothing(env):
    """재진입 1일차 MOC 접수 직후 같은 세션 재실행: 신규 주문 없음."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    trading_bot.run_one_symbol(_broker(_day2_history(odno, 1, "취소", 143), qty=143, close=59.26), _config())
    holder["state"] = deepcopy(saved[-1])
    new_odno = [k for k in saved[-1]["orders_meta"] if k != odno][0]
    hist = _day2_history(odno, 1, "취소", 143)    # 새 MOC 는 아직 이력에 미체결로 보이지 않을 수도 있음(조회 지연)
    broker2 = _broker(hist, qty=143, close=59.26)
    trading_bot.run_one_symbol(broker2, _config())
    assert broker2.place_order.call_count == 0, new_odno


def test_unfilled_open_day1_blocks_day2_orders(env):
    """1일차 MOC 가 다음 날까지 미체결(미종결): 신규 주문 없음, T·day_count 불변."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 0, "미체결", 144), qty=144, close=59.26)

    trading_bot.run_one_symbol(broker, _config())

    assert broker.place_order.call_count == 0
    final = saved[-1] if saved else holder["state"]
    assert final["T"] == 19.7734 and final["reverse_mode"].get("day_count", 0) == 0


def test_rejected_day1_is_retried_as_day1(env):
    """1일차 MOC 거부(체결 0, 종결): day_count 불변 → 다음 거래일 1일차 MOC 재시도 (종료 판정 없음)."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 0, "거부", 144), qty=144, close=59.26)

    trading_bot.run_one_symbol(broker, _config())

    assert _placed(broker) == [("SELL", 14, "MOC")]
    assert saved[-1]["T"] == 19.7734


def test_unconfirmed_close_on_day2_places_nothing_and_keeps_state(env):
    """2일차 확정 종가 미확보: 주문 없음, 체결 반영 외 상태 변화 없음, fence 없음."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 14, "체결", 130), qty=130, cash=2000.0, close=None)

    trading_bot.run_one_symbol(broker, _config())

    assert broker.place_order.call_count == 0
    final = saved[-1] if saved else holder["state"]
    assert final["reverse_mode"]["active"] is True and final["reverse_mode"].get("cycle_id")
    assert final["T"] == round(19.7734 * 0.9, 4)
    assert not final.get("pending_order_batch") and not final.get("pending_order_intent")


def test_undated_broker_day2_is_not_stalled_in_pipeline(env):
    """일봉 날짜를 제공하지 않는 브로커(KIS 등)로도 2일차 이후 리버스가 정지하지 않는다 (기존 방식으로 판정, Codex P1)."""
    holder, saved = env
    state, odno = _day1(env)
    holder["state"] = _next_session(state)
    broker = _broker(_day2_history(odno, 14, "체결", 130), qty=130, cash=2000.0, close=59.26, candle_date=None)

    trading_bot.run_one_symbol(broker, _config())

    assert broker.place_order.call_count > 0, "날짜 미제공 브로커는 보류가 아니라 기존 방식으로 진행해야 합니다"
    assert saved[-1]["reverse_mode"] == {}
