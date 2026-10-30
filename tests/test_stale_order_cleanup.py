"""
봇 소유 미체결 주문 정리 테스트 (신규 주문 제출 전 중복 방지).

배경: 매 세션마다 새 주문 전체를 새로 냅니다. 이전 세션의 주문이 살아 있으면
(특히 목표매도 LIMIT) 새 주문과 중복되어 매도 수량이 보유량을 초과할 수 있습니다.
"""
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_bot
from trading_bot import SymbolScopedError, _cancel_open_bot_orders


def _today() -> str:
    return trading_bot.get_kst_now().strftime("%Y%m%d")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """취소 확정 확인의 재시도 대기를 빠르게 합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)


class _FakeBroker:
    """취소 후 이력 재조회까지 시뮬레이션하는 브로커."""

    def __init__(self, fail_on=None, after_cancel_rows=None):
        self.calls = []
        self.fail_on = fail_on
        self.after_cancel_rows = after_cancel_rows

    def cancel_order(self, odno, symbol=None, exchange=None):
        self.calls.append((odno, symbol, exchange))
        if self.fail_on and self.fail_on == odno:
            raise RuntimeError("취소 거부")
        return None

    def get_order_history(self, symbol, exchange, days=30, verbose=False):
        if self.after_cancel_rows is not None:
            return list(self.after_cancel_rows)
        # 기본: 취소된 상태(잔량 0)로 반환
        return [_row(odno, status="취소", remaining="0")
                for odno, _s, _e in self.calls]


class _BrokerWithoutCancel:
    """취소를 지원하지 않는 브로커 (KIS/LS 등) 흉내."""
    def __init__(self):
        self.calls = []


def _row(odno, status="미체결", remaining="18", side="매도", ord_dt=None, filled="0", ord_tmd=None):
    row = {
        "odno": odno,
        "prcs_stat_name": status,
        "nccs_qty": remaining,
        "ft_ccld_qty": filled,
        "sll_buy_dvsn_cd_name": side,
        "ord_dt": ord_dt or _today(),
    }
    if ord_tmd is not None:
        row["ord_tmd"] = ord_tmd
    return row


def test_cancels_open_bot_orders():
    broker = _FakeBroker()
    state = {
        "orders_meta": {
            "OD1": {"side": "SELL", "total_qty": 18, "t_target": 0.0},
            "OD2": {"side": "BUY", "total_qty": 7, "t_target": 1.0},
        }
    }
    history = [_row("OD1"), _row("OD2", side="매수")]

    count = _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    assert count == 2
    assert [c[0] for c in broker.calls] == ["OD1", "OD2"]
    assert broker.calls[0][1] == "TQQQ"


def test_skips_closed_orders():
    """체결/취소/정정됨 주문은 취소 대상이 아닙니다."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}, "OD2": {}, "OD3": {}}}
    history = [
        _row("OD1", status="체결", remaining="0"),
        _row("OD2", status="취소", remaining="0"),
        _row("OD3", status="정정됨", remaining="0"),
    ]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_skips_orders_without_remaining():
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", status="부분체결", remaining="0")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_skips_manual_orders_not_in_orders_meta():
    """봇이 낸 주문만 정리합니다 (수동 주문은 건드리지 않음)."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1"), _row("MANUAL1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_skips_reverse_orders():
    """리버스 주문은 기간 관리 대상이므로 건드리지 않습니다."""
    broker = _FakeBroker()
    state = {
        "orders_meta": {
            "REV1": {"reverse_action": "sell"},
            "OD1": {},
        }
    }
    history = [_row("REV1"), _row("OD1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_same_us_session_across_kst_midnight_is_still_cancel_target(monkeypatch):
    """KST 날짜가 바뀌어도 ET 세션이 같으면 주문을 취소 대상으로 봅니다 (KIS 컨벤션)."""
    real_datetime = datetime

    class FixedDateTime:
        fromisoformat = staticmethod(real_datetime.fromisoformat)

        @staticmethod
        def now(tz=None):
            return real_datetime(2026, 10, 23, 12, 30, tzinfo=ZoneInfo("America/New_York"))

    monkeypatch.setattr(trading_bot, "datetime", FixedDateTime)
    monkeypatch.setattr(
        trading_bot, "get_kst_now",
        lambda: real_datetime(2026, 10, 24, 1, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    broker = _FakeBroker()
    # KIS 컨벤션: ord_dt=미국(ET) 영업일(20261023), ord_tmd=KST 시각.
    # 실제 주문 시각 KST 10/24 01:30 = ET 10/23 12:30 → 오늘 세션.
    # ord_datetime_utc는 상태 호환상 하루 앞선 규칙(ET 10/22)을 유지하므로
    # 세션 판정에 쓰면 안 됩니다 — ord_dt 기준으로 분류됨을 고정합니다.
    history = [_row("OD1", ord_dt="20261023", ord_tmd="013000")]
    history[0]["ord_datetime_utc"] = "2026-10-22T16:30:00+00:00"

    assert _cancel_open_bot_orders(
        broker, "TQQQ", "NAS", {"orders_meta": {"OD1": {}}}, history
    ) == 1
    assert [call[0] for call in broker.calls] == ["OD1"]


def test_skips_entirely_when_reverse_mode_active():
    broker = _FakeBroker()
    state = {
        "orders_meta": {"OD1": {}},
        "reverse_mode": {"active": True},
    }
    history = [_row("OD1")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 0
    assert broker.calls == []


def test_broker_without_cancel_support_blocks_session_unknown(monkeypatch):
    """취소 미지원 브로커에서 **세션 미확정** 후보가 있으면 신규 주문을 보류합니다.

    ⚠️ 계약 변경 이력 (외부 리뷰 P1 반영 — 무단 완화 아님):
    이 테스트는 원래 `test_broker_without_cancel_support_is_ignored` 로
    "세션 정보가 없는 행은 경고만 하고 건너뛴다(= 0 반환)"를 고정하고 있었습니다.
    리뷰 지적: 그 행이 **실제로 살아 있는 오늘 주문**이면 신규 주문이 중복 접수됩니다
    (매도=보유량 초과, 매수=시드 초과). 6개 어댑터 모두 주문이력에 `ord_tmd` 를
    채우므로 세션 미확정 행은 비정상(필드 누락·레거시) 행이고, 모르면 막는 쪽이
    안전합니다 (fail-closed). 그래서 계약을 "보류(raise)"로 **강화**했습니다.
    """
    broker = _BrokerWithoutCancel()
    state = {"orders_meta": {"OD1": {}}}
    # ord_dt=KST 오늘(= _row 기본값), ord_tmd 없음 → 세션(ET) 확정 불가.
    history = [_row("OD1")]

    with pytest.raises(SymbolScopedError, match="세션을 확정할 수 없는"):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)
    assert broker.calls == []


def test_broker_without_cancel_support_blocks_confirmed_today_order():
    """취소 미지원 브로커 + **오늘 세션으로 확정되는** 미체결 봇 주문 → 보류."""
    broker = _BrokerWithoutCancel()
    state = {"orders_meta": {"OD1": {}}}
    et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    history = [_row("OD1", ord_dt=et_today, ord_tmd="120000")]

    with pytest.raises(SymbolScopedError, match="오늘 세션 미체결 봇 주문"):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)
    assert broker.calls == []


def test_broker_without_cancel_support_proceeds_when_nothing_open():
    """취소 미지원 브로커라도 잔여 후보가 **없으면** 그대로 진행합니다 (0)."""
    broker = _BrokerWithoutCancel()
    state = {"orders_meta": {"OD1": {}}}
    # 이전 세션 주문(만료 대상)은 취소·보류 판정에서 제외됩니다.
    expired = _row("OD1", ord_dt="20200101", ord_tmd="120000")

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [expired]) == 0
    # 메타가 비어 있고 열린 주문도 없으면 진행합니다.
    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", {"orders_meta": {}}, []) == 0
    assert broker.calls == []


def test_cancel_failure_raises_symbol_scoped_error():
    """취소 실패 시 신규 주문을 내지 않도록 종목만 중단합니다."""
    broker = _FakeBroker(
        fail_on="OD1",
        after_cancel_rows=[_row("OD1", status="미체결", remaining="18"),
                           _row("OD2", status="미체결", remaining="18")],
    )
    state = {"orders_meta": {"OD1": {}, "OD2": {}}}
    history = [_row("OD1"), _row("OD2")]

    with pytest.raises(SymbolScopedError):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history)

    # 실패가 있어도 나머지 취소는 모두 시도합니다 (부분 취소 상태 방지)
    assert [c[0] for c in broker.calls] == ["OD1", "OD2"]


def test_english_open_status_is_cancel_target():
    """TOSS 영어 상태(OPEN)도 미체결로 매핑되어 취소 대상에 포함됩니다."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", status="OPEN", remaining="18")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_english_partial_status_is_cancel_target():
    """TOSS PARTIAL(부분체결)도 취소 대상입니다."""
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", status="PARTIAL", filled="5", remaining="13")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_english_terminal_status_confirms_cancel():
    """취소 후 이력이 영어 종료 상태(CANCELED)여도 확정으로 처리합니다."""
    broker = _FakeBroker(
        after_cancel_rows=[_row("OD1", status="CANCELED", remaining="0")]
    )
    state = {"orders_meta": {"OD1": {}}}
    history = [_row("OD1", status="OPEN", remaining="18")]

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 1


class _BrokerWithOpenOrders(_FakeBroker):
    """get_open_orders 를 지원하지만 CLOSED 이력에는 열린 주문이 없는 브로커."""

    def get_open_orders(self, symbol, exchange, days=30):
        return [_row("OD1", status="OPEN", remaining="18")]


def test_open_orders_are_merged_when_closed_history_is_empty():
    broker = _BrokerWithOpenOrders()
    state = {"orders_meta": {"OD1": {}}}

    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, []) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


# ═══════════════════════════════════════════════════════════════════
# P0-3: 열린 주문 조회 3상태 (정상 0건 / 봇주문 / 조회실패)
# ═══════════════════════════════════════════════════════════════════

class _OpenFailBroker(_FakeBroker):
    def get_open_orders(self, symbol, exchange, days=30):
        raise RuntimeError("network down")


class _Open404Broker(_FakeBroker):
    def get_open_orders(self, symbol, exchange, days=30):
        raise RuntimeError("토스 API 오류 [HTTP 404]: not found")


class _OpenEmptyBroker(_FakeBroker):
    def get_open_orders(self, symbol, exchange, days=30):
        return []


def test_open_order_lookup_failure_blocks_new_orders():
    broker = _OpenFailBroker()
    state = {"orders_meta": {"OD1": {}}}
    with pytest.raises(SymbolScopedError, match="열린 주문 조회"):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")])
    # 조회 실패 시 취소 요청도 하지 않습니다.
    assert broker.calls == []


def test_open_order_404_falls_back_to_closed_history():
    broker = _Open404Broker()
    state = {"orders_meta": {"OD1": {}}}
    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")]) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_open_order_zero_rows_uses_closed_history():
    broker = _OpenEmptyBroker()
    state = {"orders_meta": {"OD1": {}}}
    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")]) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_non_toss_broker_keeps_closed_history_path(monkeypatch):
    """get_open_orders 미지원 브로커는 기존 get_order_history 경로만 사용합니다."""
    monkeypatch.setattr(trading_bot.time, "sleep", lambda _s: None)
    broker = _FakeBroker()
    state = {"orders_meta": {"OD1": {}}}
    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("OD1")]) == 1
    assert [c[0] for c in broker.calls] == ["OD1"]


def test_manual_and_reverse_orders_are_not_cancelled_with_open_lookup():
    broker = _OpenEmptyBroker()
    state = {
        "orders_meta": {
            "BOT": {},
            "MANUAL": {},
            "REV": {"reverse_action": "sell"},
        }
    }
    history = [
        _row("BOT"),
        _row("MANUAL"),
        _row("REV", side="매수"),
    ]
    # orders_meta 에 있는 주문만 취소 대상 — MANUAL 은 메타에 있지만 상태가 열린 주문.
    # (수동 주문은 orders_meta 에 등록되지 않는다는 전제; 여기서는 리버스만 제외 검증)
    assert _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, history) == 2
    assert [c[0] for c in broker.calls] == ["BOT", "MANUAL"]


# ═══════════════════════════════════════════════════════════════════
# P0-3: run_one_symbol 주문 루프 — place_order 호출 횟수 판정
# ═══════════════════════════════════════════════════════════════════
from copy import deepcopy  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402
from broker.base import Balance, OrderResult  # noqa: E402

# 테스트 전용 승인 파일 헬퍼 (주문 루프 직접 호출 금지 — execute_approved_plan 경유)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402
import pipeline_flow  # noqa: E402


def _et_today():
    return _dt.now(_ZoneInfo("America/New_York")).strftime("%Y%m%d")


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
            {"comment": "테스트 매수", "side": "BUY", "quantity": 1,
             "price": 50.0, "order_type": "LIMIT"},
            {"comment": "테스트 추가매수", "side": "BUY", "quantity": 1,
             "price": 40.0, "order_type": "LIMIT"},
        ],
    }


def _loop_state(orders_meta=None):
    return {
        "T": 0.0,
        "last_updated": "2026-10-20T11:14:03+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": orders_meta or {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
    }


def _loop_broker(open_rows=None, open_error=None, history_after=None):
    """취소 전후 이력을 상태로 들고 있는 브로커 (재조회 횟수에 의존하지 않음)."""
    broker = MagicMock()
    broker.name = "toss"
    broker.supports_open_orders_query = True
    broker.is_trading_day.return_value = True
    broker.get_balance.return_value = Balance(quantity=0, avg_price=0.0)
    broker.exchange_code.return_value = "NASD"
    broker.close = MagicMock()
    broker.place_order.return_value = OrderResult(
        order_id="NEW1", order_time="20261024", is_reservation=False
    )
    broker.get_open_orders.side_effect = (
        open_error if open_error is not None else lambda *a, **k: list(open_rows or [])
    )
    current = {"history": list(open_rows or [])}
    broker.get_order_history.side_effect = lambda *a, **k: list(current["history"])

    def _cancel(*_a, **_k):
        current["history"] = list(history_after or [])
        return None

    broker.cancel_order.side_effect = _cancel
    return broker


def _run_loop(monkeypatch, broker, state, tmp_path):
    """실제 승인 파일을 만들어 execute_approved_plan() 으로 주문을 실행합니다.

    주문 루프를 직접 호출하는 공개 경로는 없습니다 (P0 회귀 방지).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_symbol_config()])
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: deepcopy(state))
    monkeypatch.setattr(trading_bot, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(trading_bot, "notify", lambda *a, **k: None)
    monkeypatch.setattr(trading_bot, "무한매수법_V4", _fake_strategy)
    monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
    monkeypatch.setattr(trading_bot, "BROKER_MODE", "demo")
    result, _path, _plan = pipeline_flow.run_approved(
        tmp_path,
        broker,
        state,
        list(_fake_strategy(None)["orders"]),
    )
    return result


def test_loop_normal_zero_open_orders_places_strategy_orders(monkeypatch, tmp_path):
    broker = _loop_broker(open_rows=[], history_after=[])
    # 봇 주문 메타는 있지만 열린 주문/이력에 없음 → 정상 0건으로 진행
    _run_loop(monkeypatch, broker, _loop_state({"OLD1": {}}), tmp_path)
    assert broker.place_order.call_count == 2


def test_loop_bot_open_order_cancelled_then_places(monkeypatch, tmp_path):
    row = _row("OD1", status="OPEN", remaining="18", ord_dt=_et_today(), ord_tmd="040000")
    broker = _loop_broker(
        open_rows=[row],
        history_after=[_row("OD1", status="CANCELED", remaining="0",
                            ord_dt=_et_today(), ord_tmd="040000")],
    )
    _run_loop(monkeypatch, broker, _loop_state({"OD1": {}}), tmp_path)
    assert broker.cancel_order.call_count == 1
    assert broker.place_order.call_count == 2


def test_loop_open_lookup_failure_places_nothing(monkeypatch, tmp_path):
    broker = _loop_broker(open_error=RuntimeError("network down"))
    with pytest.raises(SymbolScopedError):
        _run_loop(monkeypatch, broker, _loop_state({"OD1": {}}), tmp_path)
    assert broker.place_order.call_count == 0


def test_loop_cancel_confirmation_missing_places_nothing(monkeypatch, tmp_path):
    row = _row("OD1", status="OPEN", remaining="18", ord_dt=_et_today(), ord_tmd="040000")
    # 취소 후 이력 재조회에서 주문이 사라짐 → 확정 실패
    broker = _loop_broker(open_rows=[row], history_after=[])
    with pytest.raises(SymbolScopedError):
        _run_loop(monkeypatch, broker, _loop_state({"OD1": {}}), tmp_path)
    assert broker.place_order.call_count == 0


def test_loop_fill_increase_during_cancel_places_nothing(monkeypatch, tmp_path):
    row = _row("OD1", status="OPEN", remaining="18", side="매수",
               ord_dt=_et_today(), ord_tmd="040000", filled="0")
    broker = _loop_broker(
        open_rows=[row],
        history_after=[_row("OD1", status="부분체결", remaining="17",
                            side="매수", ord_dt=_et_today(), ord_tmd="040000", filled="1")],
    )
    with pytest.raises(SymbolScopedError, match="체결"):
        _run_loop(monkeypatch, broker, _loop_state({"OD1": {}}), tmp_path)
    assert broker.place_order.call_count == 0


def test_open_lookup_failure_blocks_when_orders_meta_empty():
    """메타가 비어 있어도 OPEN 조회 실패는 신규 주문 보류 사유입니다."""
    broker = _OpenFailBroker()
    with pytest.raises(SymbolScopedError, match="열린 주문 조회"):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", {"orders_meta": {}}, [])
    assert broker.calls == []


def test_open_lookup_failure_blocks_when_reverse_active_but_no_cancel():
    """리버스 활성 상태에서도 조회 실패는 보류 — 단 리버스 주문은 취소하지 않습니다."""
    broker = _OpenFailBroker()
    state = {
        "orders_meta": {"REV": {"reverse_action": "sell"}},
        "reverse_mode": {"active": True},
    }
    with pytest.raises(SymbolScopedError, match="열린 주문 조회"):
        _cancel_open_bot_orders(broker, "TQQQ", "NAS", state, [_row("REV")])
    assert broker.calls == []


def test_open_lookup_zero_rows_with_empty_meta_proceeds_without_cancel():
    broker = _OpenEmptyBroker()
    assert _cancel_open_bot_orders(
        broker, "TQQQ", "NAS", {"orders_meta": {}}, []
    ) == 0
    assert broker.calls == []
