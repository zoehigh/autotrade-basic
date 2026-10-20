"""
섀도우 원장(shadow/) 테스트.

검증 대상:
- (a) 연속 2회 전체 사이클(GENERATE+SETTLE): 가상 T/cash 이월 + 원장 4건.
- (b) 실제 state.json에 닿지 않습니다 (save_state 호출 0회).
- (c) broker.place_order를 호출하지 않습니다.
- (d) 전량매도 후 사이클 리셋 (T=0, effective_seed 갱신).
- (e) SHADOW_FEE_RATE 수수료가 매수/매도에 반영됩니다.
- (f) SHADOW_SLIPPAGE_BPS 슬리피지가 체결가에 반영됩니다.
- 2단계(v1.2): generate는 의도만 기록, settle은 종가 대조 체결/만료.
- v1.3(P0 정확도): 매도 T 비율규칙 / 리버스 T / BUY LOC ×1.19 상한(E1) / effective_seed.
- v1.4: 리버스 상태 영속화(연속성) / 세션 날짜 기반 정산(S1) / 미정산 의도 보호 /
  멱등 정산·원자적 스냅샷 / REINVEST / 버전 가드.
"""
import json
import os
import sys
from datetime import datetime as _real_datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# repo 루트 + src 경로 추가 (shadow/strategy import용)
_repo_root = os.path.join(os.path.dirname(__file__), "..")
_src_path = os.path.join(_repo_root, "src")
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import shadow.ledger as shadow_ledger
import strategy as strategy_module
from broker.base import Balance, PurchaseAmount, StockPrice, StockQuotation
from shadow.broker import VirtualBroker

# v1.4 가정 목록 (A1-A6 + C1 + E1 + S1 + R1)
_EXPECTED_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "C1", "E1", "S1", "R1"]

# 기본 테스트 세션 (월요일) — 결정적 시계용
_SESSION = "2026-01-05"


def _symbol_config(seed=8000.0):
    return {
        "symbol": "TQQQ",
        "exchange": "NAS",
        "splits": 20,
        "symbol_type": "TQQQ",
        "seed": seed,
        "additional_loc_levels": 3,
    }


def _make_broker(last=50.0):
    broker = MagicMock()
    broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=last)
    broker.get_stock_price.return_value = StockPrice(open=last - 1.0, last=last)
    broker.get_balance.return_value = Balance(quantity=0, avg_price=0.0)
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=8000.0)
    broker.get_daily_closes.return_value = [last]
    # v1.4 세션 날짜 기반 정산용 (날짜 있는 일봉 캔들)
    broker.get_daily_candles.return_value = [{"date": _SESSION, "close": last}]
    return broker


def _orders_strategy(orders):
    """지정된 주문 목록을 반환하는 가짜 전략 (state 기반 position)."""
    def _strategy(broker, **kwargs):
        state = kwargs.get("state", {})
        holdings = state.get("holdings", 0)
        return {
            "symbol": kwargs.get("symbol", "TQQQ"),
            "exchange": kwargs.get("exchange_code", "NAS"),
            "tradable": True,
            "open_price": 49.0,
            "last_price": 50.0,
            "position_qty": holdings,
            "avg_price": state.get("avg_price", 0.0),
            "orderable_cash": state.get("cash_usd", 8000.0),
            "seed": kwargs.get("seed", 8000.0),
            "remaining_seed": 8000.0,
            "T": kwargs.get("T", 0.0),
            "unit_amount": 0.0,
            "unit_qty": 0,
            "star_point": None,
            "star_buy_price": None,
            "take_profit_price": None,
            "orders": orders,
        }
    return _strategy


def _seed_snapshot(snapshot_dir, holdings=0, T=0.0, cash=8000.0, avg=0.0,
                   net_invested=0.0, effective_seed=None):
    """사전 상태 스냅샷을 생성합니다 (v1.4 스키마).

    effective_seed 생략 시 _load_snapshot의 setdefault가 config seed로 백필합니다.
    """
    snap_dir = os.path.join(snapshot_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    snap = {
        "symbol": "TQQQ",
        "schema_version": "v1.4",
        "phase": "settled",
        "T": T,
        "cash_usd": cash,
        "holdings": holdings,
        "avg_price": avg,
        "net_invested": net_invested,
        "net_invested_status": "valid",
        "reverse_mode": {"active": False},
        "close_prices": [],
        "pending_intents": [],
        "assumption_version": "v1.4",
        "as_of": "2026-01-01T00:00:00+00:00",
    }
    if effective_seed is not None:
        snap["effective_seed"] = effective_seed
    with open(os.path.join(snap_dir, "TQQQ_latest.json"), "w") as f:
        json.dump(snap, f)


def _load_latest(snapshot_dir):
    """최신 스냅샷 JSON을 읽습니다."""
    with open(os.path.join(snapshot_dir, "snapshots", "TQQQ_latest.json")) as f:
        return json.load(f)


def _read_ledger_events(snapshot_dir):
    """원장(JSONL) 이벤트를 파일 순서대로 모두 읽습니다."""
    ledger_dir = os.path.join(snapshot_dir, "ledger")
    events = []
    for name in sorted(os.listdir(ledger_dir)):
        with open(os.path.join(ledger_dir, name)) as f:
            events.extend(json.loads(line) for line in f if line.strip())
    return events


def _generate_and_settle(broker, cfg, snapshot_dir, close_price, session=_SESSION):
    """GENERATE → SETTLE 전체 사이클 (fake broker 종가/세션 날짜 고정).

    v1.4 세션 규칙 재현: generate는 세션 마감 전(closed=False),
    settle은 세션 마감 후(closed=True)로 가짜 시계를 전환합니다.
    """
    st = getattr(shadow_ledger, "_TEST_SESSION_STATE", None)
    if st is not None:
        st.update({"session": session, "closed": False})
    shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=snapshot_dir)
    broker.get_daily_closes.return_value = [close_price]
    broker.get_daily_candles.return_value = [{"date": session, "close": close_price}]
    if st is not None:
        st.update({"session": session, "closed": True})
    shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=snapshot_dir)


@pytest.fixture
def session_api(monkeypatch):
    """us_session_date_now/us_session_closed 가짜 — 결정적 세션 시계.

    state["closed"]: generate 시 False / settle 시 True (기본 False).
    state["closed_by_date"]: {날짜: bool} 지정 시 날짜별 마감 판정 (미지정 날짜=False).
    """
    state = {"session": _SESSION, "closed": False, "closed_by_date": None}

    def _closed(session_date, *args, **kwargs):
        by_date = state.get("closed_by_date")
        if by_date is not None:
            return bool(by_date.get(str(session_date), False))
        return bool(state["closed"])

    # raising=False: v1.3 이전(속성 없음) 코드에서도 재현 테스트가 돌 수 있도록
    monkeypatch.setattr(shadow_ledger, "us_session_date_now",
                        lambda *args, **kwargs: state["session"], raising=False)
    monkeypatch.setattr(shadow_ledger, "us_session_closed", _closed, raising=False)
    monkeypatch.setattr(shadow_ledger, "_TEST_SESSION_STATE", state, raising=False)
    return state


@pytest.fixture(autouse=True)
def _auto_session_api(session_api):
    """모든 섀도우 원장 테스트에 결정적 세션 시계를 적용합니다."""
    return session_api


@pytest.fixture
def real_strategy_env(session_api, monkeypatch, tmp_path):
    """실전 전략 + 세션 동기화 전략 시계 + 네트워크 차단 (리버스 연속성 테스트용).

    - strategy.datetime을 세션 날짜에 동기화 → strategy.py:137 중복가드가
      실제 운영과 동일하게 세션 비교를 수행합니다.
    - get_finnhub_ma5 차단 → 별지점은 close_prices/last_price fallback.
    """
    monkeypatch.setattr(strategy_module, "get_finnhub_ma5", lambda symbol: None)

    class _FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            base = _real_datetime.fromisoformat(
                f"{session_api['session']}T12:00:00+00:00"
            )
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(strategy_module, "datetime", _FakeDateTime)
    session_api["closed"] = False
    return SimpleNamespace(snapshot_dir=str(tmp_path), session=session_api)


def _fake_strategy(broker, **kwargs):
    """BUY 2주 @50 (t_target=1.0) 주문 1건을 반환하는 가짜 전략."""
    return {
        "symbol": kwargs.get("symbol", "TQQQ"),
        "exchange": kwargs.get("exchange_code", "NAS"),
        "tradable": True,
        "open_price": 49.0,
        "last_price": 50.0,
        "position_qty": 0,
        "avg_price": 0.0,
        "orderable_cash": 8000.0,
        "seed": kwargs.get("seed", 8000.0),
        "remaining_seed": 8000.0,
        "T": kwargs.get("T", 0.0),
        "unit_amount": 100.0,
        "unit_qty": 2,
        "star_point": None,
        "star_buy_price": None,
        "take_profit_price": None,
        "orders": [
            {"side": "BUY", "quantity": 2, "price": 50.0, "order_type": "LOC",
             "comment": "테스트 매수", "t_target": 1.0},
        ],
    }


@pytest.fixture
def shadow_env(monkeypatch, tmp_path):
    """가짜 전략 + 임시 스냅샷 디렉터리."""
    monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _fake_strategy)
    return str(tmp_path)


class TestShadowLedger:
    def test_two_runs_carry_forward_and_append(self, shadow_env):
        """(a) 2회 전체 사이클(GENERATE+SETTLE): T/cash 이월 + 원장 4건.

        기본 수수료(COMMISSION_RATE=0.0025)가 적용됩니다:
          BUY 2주 @50(종가) → cash -= 2*50*1.0025 = 100.25
        """
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        for _ in range(2):
            _generate_and_settle(broker, cfg, shadow_env, close_price=50.0)

        # 스냅샷: T=2.0, cash=8000-100.25*2, holdings=4, net_invested=200
        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["T"] == 2.0
        assert snap["cash_usd"] == 7799.5
        assert snap["holdings"] == 4
        assert snap["net_invested"] == 200.0
        assert snap["assumption_version"] == "v1.4"
        assert snap["schema_version"] == "v1.4"
        assert snap["phase"] == "settled"
        assert snap["pending_intents"] == []
        assert snap["effective_seed"] == 8000.0  # P0-4: 부트스트랩 시드

        # 원장: 4건 (intent 2 + settlement 2, JSONL 1파일)
        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        assert len(ledger_files) == 1
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 4
        assert all(e["assumption_version"] == "v1.4" for e in lines)
        assert all(e["assumptions"] == _EXPECTED_ASSUMPTIONS for e in lines)

        intents = [e for e in lines if e["event_type"] == "intent"]
        settlements = [e for e in lines if e["event_type"] == "settlement"]
        assert len(intents) == 2
        assert len(settlements) == 2
        assert all(e["status"] == "pending" for e in intents)
        assert all("virtual_state_before" in e for e in intents)
        assert all(e["filled"] is True for e in settlements)
        assert all(e["fill_price"] == 50.0 for e in settlements)
        assert all(e["fee_usd"] == 0.25 for e in settlements)  # 2*50*0.0025
        # intent_event_id 링크
        assert settlements[0]["intent_event_id"] == intents[0]["event_id"]
        assert settlements[1]["intent_event_id"] == intents[1]["event_id"]
        assert settlements[0]["virtual_state_after"]["T"] == 1.0
        assert settlements[1]["virtual_state_after"]["T"] == 2.0
        assert settlements[0]["virtual_state_after"]["cash_usd"] == 7899.75
        assert settlements[1]["virtual_state_after"]["cash_usd"] == 7799.5

    def test_no_save_state_call(self, shadow_env, monkeypatch):
        """(b) 실제 state.json에 닿지 않습니다 — save_state 호출 0회."""
        import state as real_state
        calls = []
        monkeypatch.setattr(real_state, "save_state", lambda *a, **k: calls.append(a))

        shadow_ledger.run_shadow_symbol(
            _make_broker(), _symbol_config(), snapshot_dir=shadow_env
        )

        assert calls == [], "섀도우 원장은 save_state를 호출하면 안 됩니다"

    def test_no_place_order_call(self, shadow_env):
        """(c) broker.place_order를 호출하지 않습니다."""
        broker = _make_broker()

        shadow_ledger.run_shadow_symbol(broker, _symbol_config(), snapshot_dir=shadow_env)

        broker.place_order.assert_not_called()

    def test_zero_orders_skips_ledger_but_updates_snapshot(self, shadow_env, monkeypatch):
        """주문 0건이면 원장 기록은 생략하고 스냅샷 as_of만 갱신합니다."""
        def _empty_strategy(broker, **kwargs):
            return {
                "symbol": kwargs.get("symbol", "TQQQ"),
                "exchange": kwargs.get("exchange_code", "NAS"),
                "tradable": True,
                "open_price": 49.0,
                "last_price": 50.0,
                "position_qty": 0,
                "avg_price": 0.0,
                "orderable_cash": 8000.0,
                "seed": kwargs.get("seed", 8000.0),
                "remaining_seed": 8000.0,
                "T": kwargs.get("T", 0.0),
                "unit_amount": 0.0,
                "unit_qty": 0,
                "star_point": None,
                "star_buy_price": None,
                "take_profit_price": None,
                "orders": [],
            }
        monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _empty_strategy)

        shadow_ledger.run_shadow_symbol(
            _make_broker(), _symbol_config(), snapshot_dir=shadow_env
        )

        assert not os.path.exists(os.path.join(shadow_env, "ledger")), \
            "주문이 없으면 원장 디렉터리를 만들지 않아야 합니다"
        assert os.path.exists(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")), \
            "스냅샷은 as_of 갱신을 위해 저장되어야 합니다"

    def test_fee_applied(self, shadow_env, monkeypatch):
        """(e) SHADOW_FEE_RATE 수수료가 매수 체결에 반영됩니다 (fee 1%)."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0.01")
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        _generate_and_settle(broker, cfg, shadow_env, close_price=50.0)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        # BUY 2주 @50, fee 1% → cash = 8000 - 2*50*1.01 = 7899
        assert snap["cash_usd"] == 7899.0
        assert snap["holdings"] == 2
        assert snap["net_invested"] == 100.0  # fee는 net_invested에 미포함

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = [e for e in events if e["event_type"] == "settlement"][0]
        assert settlement["fee_usd"] == 1.0
        assert settlement["fill_price"] == 50.0  # 슬리피지 없음

    def test_slippage_applied(self, shadow_env, monkeypatch):
        """(f) SHADOW_SLIPPAGE_BPS 슬리피지가 매수 체결가에 반영됩니다 (2%)."""
        monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "200")
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")  # 슬리피지만 검증
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        _generate_and_settle(broker, cfg, shadow_env, close_price=50.0)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        # BUY 2주 @50, slippage 2% → fill 51.0 → cash = 8000 - 2*51 = 7898
        assert snap["cash_usd"] == 7898.0
        assert snap["avg_price"] == 51.0
        assert snap["net_invested"] == 102.0

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = [e for e in events if e["event_type"] == "settlement"][0]
        assert settlement["fill_price"] == 51.0
        assert settlement["fee_usd"] == 0.0  # 수수료 미설정

    def test_cycle_reset_after_full_sell(self, shadow_env, monkeypatch):
        """(d) 전량매도(비율 ≥1.0) → 사이클 리셋 (T=0, effective_seed 갱신)."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")  # 수수료 없이 리셋만 검증

        def _sell_strategy(broker, **kwargs):
            state = kwargs.get("state", {})
            holdings = state.get("holdings", 0)
            return {
                "symbol": kwargs.get("symbol", "TQQQ"),
                "exchange": kwargs.get("exchange_code", "NAS"),
                "tradable": True,
                "open_price": 49.0,
                "last_price": 50.0,
                "position_qty": holdings,
                "avg_price": 50.0,
                "orderable_cash": 8000.0,
                "seed": kwargs.get("seed", 8000.0),
                "remaining_seed": 8000.0,
                "T": kwargs.get("T", 0.0),
                "unit_amount": 0.0,
                "unit_qty": 0,
                "star_point": None,
                "star_buy_price": None,
                "take_profit_price": None,
                "orders": [
                    {"side": "SELL", "quantity": holdings, "price": 50.0,
                     "order_type": "MOC", "comment": "테스트 전량매도",
                     "t_target": 0.9},
                ],
            }
        monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _sell_strategy)

        # 사전 스냅샷: T=5, holdings=5, cash=7500 (5주 @50 매수 후)
        _seed_snapshot(shadow_env, holdings=5, T=5.0, cash=7500.0, avg=50.0, net_invested=250.0)

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["T"] == 0.0
        assert snap["holdings"] == 0
        assert snap["net_invested"] == 0.0
        assert snap["avg_price"] == 0.0
        # cash = 7500 + 5*50 = 7750 (수수료 0) → cash 불변, effective_seed만 갱신
        assert snap["cash_usd"] == 7750.0
        assert snap["effective_seed"] == 7750.0  # 8000 + (7750 − 8000)


class TestVirtualBroker:
    """VirtualBroker: 전략이 가상 스냅샷을 읽도록 강제하는 래퍼."""

    def test_real_holdings_do_not_produce_phantom_sell(self, shadow_env, monkeypatch):
        """(a) 실제 브로커가 100주를 보유해도 가상 스냅샷(0주) 기준이면 SELL 없음.

        회귀 버그: run_shadow_symbol이 실제 브로커를 전략에 넘겨
        가상 포트폴리오에 없는 주식의 팬텀 매도가 발생하던 문제.
        """
        def _balance_based_strategy(broker, **kwargs):
            # 전략은 broker.get_balance()로 보유 수량을 판단합니다.
            balance = broker.get_balance(
                kwargs.get("symbol"), kwargs.get("exchange_code")
            )
            holdings = balance.quantity if balance else 0
            orders = []
            if holdings > 0:
                orders.append({
                    "side": "SELL", "quantity": holdings, "price": 50.0,
                    "order_type": "MOC", "comment": "테스트 매도", "t_target": 0.9,
                })
            return {
                "symbol": kwargs.get("symbol", "TQQQ"),
                "exchange": kwargs.get("exchange_code", "NAS"),
                "tradable": True,
                "open_price": 49.0,
                "last_price": 50.0,
                "position_qty": holdings,
                "avg_price": balance.avg_price if balance else 0.0,
                "orderable_cash": 8000.0,
                "seed": kwargs.get("seed", 8000.0),
                "remaining_seed": 8000.0,
                "T": kwargs.get("T", 0.0),
                "unit_amount": 0.0,
                "unit_qty": 0,
                "star_point": None,
                "star_buy_price": None,
                "take_profit_price": None,
                "orders": orders,
            }
        monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _balance_based_strategy)

        # 실제 브로커: 100주 보유 + 현금 $999,999 (버그 시 팬텀 매도 유발)
        real_broker = MagicMock()
        real_broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=50.0)
        real_broker.get_stock_price.return_value = StockPrice(open=49.0, last=50.0)
        real_broker.get_balance.return_value = Balance(quantity=100, avg_price=50.0)
        real_broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=999999.0)

        shadow_ledger.run_shadow_symbol(
            real_broker, _symbol_config(seed=8000.0), snapshot_dir=shadow_env
        )

        # 가상 스냅샷: holdings=0 그대로, cash=8000 그대로 (매도 없음)
        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["holdings"] == 0, "가상 포트폴리오에 없는 주식을 매도하면 안 됩니다"
        assert snap["cash_usd"] == 8000.0, "팬텀 매도로 가상 현금이 늘면 안 됩니다"
        assert snap["T"] == 0.0

        # 원장에 SELL 이벤트가 없어야 합니다 (주문 0건 → 원장 미생성)
        assert not os.path.exists(os.path.join(shadow_env, "ledger")), \
            "SELL 주문이 없으므로 원장이 생성되면 안 됩니다"

    def test_place_order_raises(self):
        """(b) VirtualBroker.place_order/cancel_order는 항상 RuntimeError."""
        real_broker = MagicMock()
        vb = VirtualBroker(real_broker, {"holdings": 0, "avg_price": 0.0, "cash_usd": 8000.0})

        with pytest.raises(RuntimeError, match="가상 체결만 사용"):
            vb.place_order("TQQQ", "NAS", "BUY", 1, 50.0, "LIMIT")
        with pytest.raises(RuntimeError, match="가상 체결만 사용"):
            vb.cancel_order("odno123")
        # 실제 브로커의 주문 메서드는 절대 호출되지 않아야 합니다
        real_broker.place_order.assert_not_called()
        real_broker.cancel_order.assert_not_called()

    def test_balance_and_purchase_amount_from_snapshot(self):
        """(c) get_balance/get_purchase_amount는 스냅샷 값을 제공합니다.

        스냅샷 dict를 live 참조하므로, 스냅샷 변경이 다음 읽기에 즉시 반영됩니다.
        """
        snapshot = {"holdings": 3, "avg_price": 70.0, "cash_usd": 5000.0}
        real_broker = MagicMock()
        real_broker.get_balance.return_value = Balance(quantity=100, avg_price=50.0)
        real_broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=999999.0)
        vb = VirtualBroker(real_broker, snapshot)

        # 가상 값 제공 (실제 브로커 값 아님)
        bal = vb.get_balance("TQQQ", "NAS")
        assert bal.quantity == 3
        assert bal.avg_price == 70.0
        ps = vb.get_purchase_amount("TQQQ", "NAS")
        assert ps.orderable_cash == 5000.0

        # holdings<=0 → None (실제 브로커와 동일 규약)
        snapshot["holdings"] = 0
        assert vb.get_balance("TQQQ", "NAS") is None

        # live 참조: 스냅샷 변경이 즉시 반영
        snapshot["holdings"] = 5
        snapshot["avg_price"] = 80.0
        snapshot["cash_usd"] = 6000.0
        bal2 = vb.get_balance("TQQQ", "NAS")
        assert bal2.quantity == 5
        assert bal2.avg_price == 80.0
        assert vb.get_purchase_amount("TQQQ", "NAS").orderable_cash == 6000.0

        # 실제 브로커 조회는 호출되지 않아야 합니다
        real_broker.get_balance.assert_not_called()
        real_broker.get_purchase_amount.assert_not_called()

    def test_delegates_market_data_to_real_broker(self):
        """시세/이력/거래일 등은 실제 브로커에 그대로 위임됩니다."""
        real_broker = MagicMock()
        real_broker.get_stock_price.return_value = StockPrice(open=49.0, last=50.0)
        real_broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=50.0)
        real_broker.get_daily_closes.return_value = [48.0, 49.0, 50.0]
        real_broker.get_order_history.return_value = [{"odno": "1"}]
        real_broker.is_trading_day.return_value = True
        real_broker.exchange_code.return_value = "NASD"
        real_broker.name = "fake"
        vb = VirtualBroker(real_broker, {"holdings": 0, "avg_price": 0.0, "cash_usd": 8000.0})

        assert vb.get_stock_price("TQQQ", "NAS").last == 50.0
        assert vb.get_stock_quotation("TQQQ", "NAS").tradable is True
        assert vb.get_daily_closes("TQQQ", "NAS", 5) == [48.0, 49.0, 50.0]
        assert vb.get_order_history("TQQQ", "NAS") == [{"odno": "1"}]
        assert vb.is_trading_day() is True
        assert vb.exchange_code("NAS") == "NASD"
        assert vb.name == "fake"
        real_broker.get_stock_price.assert_called_once()
        real_broker.get_daily_closes.assert_called_once()


class TestShadowTwoPhase:
    """2단계(v1.2) GENERATE/SETTLE: 의도 기록 → 종가 대조 체결/만료."""

    def test_generate_writes_pendings_no_state_change(self, shadow_env):
        """(a) GENERATE: 의도만 기록 — cash/holdings/T 변화 없음."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["T"] == 0.0
        assert snap["cash_usd"] == 8000.0
        assert snap["holdings"] == 0
        assert snap["net_invested"] == 0.0
        assert snap["effective_seed"] == 8000.0  # P0-4
        assert snap["phase"] == "generating"
        assert len(snap["pending_intents"]) == 1
        pi = snap["pending_intents"][0]
        assert pi["side"] == "BUY"
        assert pi["order_type"] == "LOC"
        assert pi["intent_price"] == 50.0
        assert pi["intent_qty"] == 2

        # 원장: intent 이벤트 1건 (virtual_state_before, status=pending)
        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "intent"
        assert ev["status"] == "pending"
        assert ev["assumption_version"] == "v1.4"
        assert ev["assumptions"] == _EXPECTED_ASSUMPTIONS
        assert "virtual_state_before" in ev
        assert "virtual_state_after" not in ev
        assert ev["virtual_state_before"]["cash_usd"] == 8000.0

    def test_settle_buy_loc_fills_at_close(self, shadow_env, session_api):
        """(b) SETTLE: BUY LOC는 close<=limit이면 종가에 체결됩니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        # v1.4: 세션 마감 + 해당 세션 날짜 캔들에서만 정산됩니다.
        session_api["closed"] = True
        broker.get_daily_closes.return_value = [45.0]
        broker.get_daily_candles.return_value = [{"date": _SESSION, "close": 45.0}]
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["phase"] == "settled"
        assert snap["pending_intents"] == []
        assert snap["holdings"] == 2
        assert snap["T"] == 1.0
        # BUY 2 @45(종가), fee 0.0025 → cash = 8000 - 2*45*1.0025 = 7909.775
        assert snap["cash_usd"] == 7909.775
        assert snap["avg_price"] == 45.0

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        assert len(events) == 2
        intent, settlement = events
        assert settlement["event_type"] == "settlement"
        assert settlement["intent_event_id"] == intent["event_id"]
        assert settlement["filled"] is True
        assert settlement["daily_close"] == 45.0
        assert settlement["fill_price"] == 45.0  # 종가 체결
        assert settlement["fill_qty"] == 2
        assert settlement["fill_ratio"] == 1.0
        assert settlement["fee_usd"] == 0.225  # 2*45*0.0025
        assert settlement["t_delta"] == 1.0

    def test_unfilled_expires_no_state_change(self, shadow_env, session_api):
        """(c) SETTLE: BUY LOC 미체결(close>limit) → 만료, 상태 변화 없음."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        session_api["closed"] = True
        broker.get_daily_closes.return_value = [55.0]
        broker.get_daily_candles.return_value = [{"date": _SESSION, "close": 55.0}]
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["phase"] == "settled"
        assert snap["pending_intents"] == []
        assert snap["T"] == 0.0
        assert snap["cash_usd"] == 8000.0
        assert snap["holdings"] == 0

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = events[1]
        assert settlement["event_type"] == "settlement"
        assert settlement["filled"] is False
        assert settlement["fill_qty"] == 0
        assert settlement["fill_ratio"] == 0.0
        assert settlement["fee_usd"] == 0.0
        assert settlement["t_delta"] == 0.0
        assert settlement["virtual_state_after"]["cash_usd"] == 8000.0

    def test_moc_always_fills(self, shadow_env, monkeypatch):
        """(d) SETTLE: MOC는 close와 무관하게 항상 종가 체결됩니다."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")  # 수수료 없이 체결만 검증
        _seed_snapshot(shadow_env, holdings=5, T=5.0, cash=7500.0, avg=50.0, net_invested=250.0)

        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 1, "price": 50.0,
                 "order_type": "MOC", "comment": "테스트 MOC 매도", "t_target": 0.9},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=60.0)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["holdings"] == 4
        # P0-1: 쿼터매도(1/5=0.2 <0.5) → T × 0.75 = 3.75 (t_target=0.9 무시)
        assert snap["T"] == 3.75
        assert snap["cash_usd"] == 7560.0  # 7500 + 1*60 (수수료 0)
        assert snap["net_invested"] == 190.0  # 250 - 60

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = events[1]
        assert settlement["filled"] is True
        assert settlement["fill_price"] == 60.0  # 종가 체결
        assert settlement["t_delta"] == -1.25  # 5 → 3.75

    def test_limit_day_fills_at_intent_price(self, shadow_env, monkeypatch):
        """(e) SETTLE: LIMIT DAY는 close가 조건을 만족하면 의도가격 체결 (C1)."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(shadow_env, holdings=5, T=5.0, cash=7500.0, avg=50.0, net_invested=250.0)

        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 2, "price": 55.0,
                 "order_type": "LIMIT", "comment": "테스트 익절 LIMIT", "t_target": 0.0},
            ]),
        )

        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)
        # close 60 >= limit 55 → 체결, 의도가격(55) 체결
        _generate_and_settle(broker, cfg, shadow_env, close_price=60.0)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["holdings"] == 3
        assert snap["cash_usd"] == 7610.0  # 7500 + 2*55 (의도가격, 수수료 0)
        # P0-1: 쿼터매도(2/5=0.4 <0.5) → T × 0.75 = 3.75 (t_target=0 무시)
        assert snap["T"] == 3.75

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = events[1]
        assert settlement["filled"] is True
        assert settlement["fill_price"] == 55.0  # 의도가격 (C1), close 60 아님
        assert settlement["daily_close"] == 60.0

        # 미체결 케이스: close 50 < limit 55 → SELL LIMIT 만료, 상태 변화 없음
        _generate_and_settle(broker, cfg, shadow_env, close_price=50.0)
        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["holdings"] == 3
        assert snap["cash_usd"] == 7610.0
        assert snap["T"] == 3.75  # 미체결 → T 불변

class TestShadowFidelityV13:
    """v1.3 P0 정확도 수정: 매도 T 비율규칙 / 리버스 T / LOC 상한 / effective_seed."""

    def test_quarter_sell_t_decrement(self, shadow_env, monkeypatch):
        """P0-1: 쿼터매도(비율 <0.5) → T × 0.75 — t_target은 매수 전용."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(shadow_env, holdings=4, T=4.0, cash=7800.0, avg=50.0, net_invested=200.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 1, "price": 50.0,
                 "order_type": "MOC", "comment": "쿼터매도", "t_target": 0.9},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        snap = _load_latest(shadow_env)
        assert snap["holdings"] == 3
        assert snap["T"] == 3.0  # 4 × 0.75 (t_target=0.9 무시)
        settlement = _read_ledger_events(shadow_env)[-1]
        assert settlement["t_delta"] == -1.0

    def test_goal_sell_t_decrement(self, shadow_env, monkeypatch):
        """P0-1: 목표매도(비율 ≥0.5) → T × 0.25."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(shadow_env, holdings=4, T=4.0, cash=7800.0, avg=50.0, net_invested=200.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 2, "price": 50.0,
                 "order_type": "MOC", "comment": "목표매도", "t_target": 0.9},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        snap = _load_latest(shadow_env)
        assert snap["holdings"] == 2
        assert snap["T"] == 1.0  # 4 × 0.25 (비율 2/4 = 0.5 → 목표매도)
        settlement = _read_ledger_events(shadow_env)[-1]
        assert settlement["t_delta"] == -3.0

    def test_reverse_sell_uses_reverse_fields(self, shadow_env, monkeypatch):
        """P0-2: 리버스 매도 → T − base×(1−factor) — 일반 비율 규칙보다 우선."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(shadow_env, holdings=10, T=18.0, cash=6000.0, avg=50.0, net_invested=500.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 1, "price": 50.0,
                 "order_type": "MOC", "comment": "리버스 1일차 매도",
                 "t_target": 0.9,
                 "reverse_action": "sell", "reverse_day": 1,
                 "reverse_base_t": 20.0, "reverse_t_factor": 0.9},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        snap = _load_latest(shadow_env)
        # 일반 규칙이면 18×0.75=13.5 — 리버스 규칙: 18 − 20×(1−0.9) = 16.0
        assert snap["T"] == 16.0
        assert snap["holdings"] == 9
        settlement = _read_ledger_events(shadow_env)[-1]
        assert settlement["t_delta"] == -2.0

    def test_reverse_buy_uses_reverse_t_target(self, shadow_env, monkeypatch):
        """P0-2: 리버스 쿼터매수 → T + reverse_t_target — t_target 폴백 무시."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(shadow_env, holdings=0, T=18.0, cash=8000.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "BUY", "quantity": 2, "price": 50.0,
                 "order_type": "LIMIT", "comment": "리버스 쿼터매수",
                 "t_target": 1.0,  # 일반 BUY 폴백이면 +1.0 → 무시 검증
                 "reverse_action": "buy", "reverse_day": 2,
                 "reverse_t_target": 0.25},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        snap = _load_latest(shadow_env)
        assert snap["T"] == 18.25  # 18 + 0.25 (t_target=1.0 무시)
        settlement = _read_ledger_events(shadow_env)[-1]
        assert settlement["t_delta"] == 0.25

    def test_buy_loc_capped_at_current_x119(self, shadow_env, monkeypatch):
        """P0-3(E1): BUY LOC가 현재가 ×1.19 초과 시 상한으로 보정."""
        _seed_snapshot(shadow_env, holdings=0, T=0.0, cash=8000.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "BUY", "quantity": 2, "price": 70.0,
                 "order_type": "LOC", "comment": "고가 LOC", "t_target": 1.0},
            ]),
        )

        broker = _make_broker()
        shadow_ledger.generate_symbol(broker, _symbol_config(seed=8000.0), snapshot_dir=shadow_env)

        # 현재가 50 → 상한 50×1.19 = 59.5 → 70이 59.5로 보정
        snap = _load_latest(shadow_env)
        assert snap["pending_intents"][0]["intent_price"] == 59.5

        events = _read_ledger_events(shadow_env)
        assert events[0]["intent_price"] == 59.5
        assert events[0]["capped"] is True

    def test_loc_cap_applies_only_to_buy_loc(self, shadow_env, monkeypatch):
        """P0-3(E1): 상한 미만 BUY LOC와 SELL LOC는 보정하지 않음."""
        _seed_snapshot(shadow_env, holdings=2, T=2.0, cash=7900.0, avg=50.0, net_invested=100.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "BUY", "quantity": 1, "price": 55.0,
                 "order_type": "LOC", "comment": "상한 미만 매수", "t_target": 1.0},
                {"side": "SELL", "quantity": 1, "price": 70.0,
                 "order_type": "LOC", "comment": "고가 매도 LOC", "t_target": 0.0},
            ]),
        )

        broker = _make_broker()
        shadow_ledger.generate_symbol(broker, _symbol_config(seed=8000.0), snapshot_dir=shadow_env)

        snap = _load_latest(shadow_env)
        assert [p["intent_price"] for p in snap["pending_intents"]] == [55.0, 70.0]
        events = _read_ledger_events(shadow_env)
        assert all("capped" not in e for e in events), \
            "BUY LOC 상한 미만/SELL LOC는 보정되지 않아야 합니다"

    def test_effective_seed_carried_into_next_cycle(self, shadow_env, monkeypatch, session_api):
        """P0-4: 사이클 리셋 후 effective_seed가 다음 전략 시드로 이월됩니다."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        # 1단계: 전량매도 → 리셋 (cash = 7500 + 5×50 = 7750)
        _seed_snapshot(shadow_env, holdings=5, T=5.0, cash=7500.0, avg=50.0,
                       net_invested=250.0, effective_seed=8000.0)
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 5, "price": 50.0,
                 "order_type": "MOC", "comment": "전량매도", "t_target": 0.9},
            ]),
        )

        broker = _make_broker()
        _generate_and_settle(broker, _symbol_config(seed=8000.0), shadow_env, close_price=50.0)

        snap = _load_latest(shadow_env)
        assert snap["holdings"] == 0
        assert snap["T"] == 0.0
        assert snap["cash_usd"] == 7750.0      # cash 불변 (P0-4)
        assert snap["effective_seed"] == 7750.0  # 8000 + (7750 − 8000)

        # 2단계: 다음 사이클 generate — 전략은 effective_seed를 시드로 받음
        captured = {}

        def _recorder(broker_, **kwargs):
            captured["seed"] = kwargs.get("seed")
            return {
                "symbol": kwargs.get("symbol", "TQQQ"),
                "exchange": kwargs.get("exchange_code", "NAS"),
                "tradable": True,
                "open_price": 49.0,
                "last_price": 50.0,
                "position_qty": 0,
                "avg_price": 0.0,
                "orderable_cash": 7750.0,
                "T": kwargs.get("T", 0.0),
                "orders": [],
            }
        monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _recorder)
        # v1.4: 다음 세션 generate (마감 플래그 해제 — 전 세션 정산 후)
        session_api["closed"] = False
        shadow_ledger.generate_symbol(broker, _symbol_config(seed=8000.0), snapshot_dir=shadow_env)

        assert captured["seed"] == 7750.0, \
            "리셋 후 전략 시드는 effective_seed여야 합니다 (config seed 8000 아님)"


class TestReverseContinuity:
    """실전 전략 기반 리버스 모드 연속성 (v1.4: reverse_mode 스냅샷 영속화).

    재현 시나리오 (fix-2 확인 결함): T=20/splits=20/보유100에서
    generate → 리버스 1일차 MOC 매도 → settle → T=18, holdings=90 인데
    reverse_mode가 {"active": false} 그대로면 다음 세션은 일반 모드로
    (LOC/LIMIT 혼합) 내려가 리버스 시퀀스가 붕괴한다.
    """

    D1, D2, D3 = "2026-01-05", "2026-01-06", "2026-01-07"  # 월~수

    def test_reverse_day_sequence_persists_across_sessions(self, real_strategy_env):
        env = real_strategy_env
        snap_dir = env.snapshot_dir
        broker = _make_broker(last=40.0)
        cfg = _symbol_config(seed=8000.0)  # splits=20

        # 사전 상태: T=20 (splits-1=19 초과 → 리버스 진입 조건), 보유 100주, 평단 50
        _seed_snapshot(snap_dir, holdings=100, T=20.0, cash=8000.0,
                       avg=50.0, net_invested=5000.0)

        # ── 세션 1: 리버스 1일차 (실전 전략) ──
        env.session.update({"session": self.D1, "closed": False})
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=snap_dir)

        snap = _load_latest(snap_dir)
        rev = snap["reverse_mode"]
        assert rev.get("active") is True, \
            f"generate 후 reverse_mode.active가 True여야 합니다 (실제: {rev})"
        assert rev.get("day_count") == 1
        assert rev.get("cycle_id"), "cycle_id(리버스 런 고유)가 있어야 합니다"
        assert rev.get("last_planned_session") == self.D1
        pendings = snap["pending_intents"]
        assert pendings, "리버스 1일차 MOC 매도 의도가 있어야 합니다"
        assert all(p.get("reverse_action") == "sell" for p in pendings)
        assert pendings[0]["intent_qty"] == 10  # 100주 // 10 (20분할 1/10)
        assert pendings[0]["order_type"] == "MOC"
        assert pendings[0]["session_date"] == self.D1

        # 세션 마감 후 정산 (종가 40)
        broker.get_daily_candles.return_value = [{"date": self.D1, "close": 40.0}]
        broker.get_daily_closes.return_value = [40.0]
        env.session["closed"] = True
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=snap_dir)

        snap = _load_latest(snap_dir)
        assert snap["T"] == 18.0        # 20 − 20×(1−0.9) — P0-2 리버스 감소
        assert snap["holdings"] == 90
        assert snap["reverse_mode"].get("active") is True, \
            "settle 후에도 리버스 활성 상태가 유지되어야 합니다"
        assert snap["reverse_mode"].get("day_count") == 1
        assert snap["reverse_mode"].get("cumulative_sell_proceeds") == 400.0  # 10주×$40

        # ── 세션 2: T=18 < splits-1 이어도 리버스 유지 (일반 모드로 내려가면 회귀) ──
        env.session.update({"session": self.D2, "closed": False})
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=snap_dir)

        snap = _load_latest(snap_dir)
        assert snap["reverse_mode"].get("day_count") == 2
        pendings = snap["pending_intents"]
        assert pendings, "세션2에서 리버스 주문이 나와야 합니다"
        assert all(p.get("reverse_action") in ("sell", "buy") for p in pendings), \
            f"세션2 주문이 리버스가 아님 (일반 모드로 붕괴): {pendings}"
        assert all(p.get("reverse_day") == 2 for p in pendings)
        assert all(p.get("session_date") == self.D2 for p in pendings)
        # strategy.py:137 중복가드 — 전 세션(last_planned_session=D1)이
        # 다음 세션(D2)을 영구 차단하지 않아야 함 (리버스 2일차 주문이証明)

        # 정산: SELL LOC@40 체결(close 40≥40), BUY LOC@39.99 미체결(40>39.99)
        broker.get_daily_candles.return_value = [{"date": self.D2, "close": 40.0}]
        broker.get_daily_closes.return_value = [40.0]
        env.session["closed"] = True
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=snap_dir)

        snap = _load_latest(snap_dir)
        assert snap["T"] == 16.2  # 18 − 18×0.1 (일반 모드 t_target 규칙이면 다른 값)
        assert snap["T"] < cfg["splits"] - 1  # 하락 T — 그래도 리버스 유지
        assert snap["reverse_mode"].get("active") is True
        assert snap["reverse_mode"].get("day_count") == 2
        assert snap["reverse_mode"].get("cumulative_sell_proceeds") == 760.0  # 400+9×40

        # ── 세션 3: 여전히 리버스 (일반 모드 LOC/LIMIT 혼합으로 바뀌면 회귀) ──
        env.session.update({"session": self.D3, "closed": False})
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=snap_dir)

        snap = _load_latest(snap_dir)
        pendings = snap["pending_intents"]
        assert pendings, "세션3에서 리버스 주문이 나와야 합니다"
        assert all(p.get("reverse_action") in ("sell", "buy") for p in pendings), \
            f"세션3 주문이 리버스가 아님 (일반 모드로 붕괴): {pendings}"
        assert all(p.get("reverse_day") == 3 for p in pendings)
        assert snap["reverse_mode"].get("active") is True
        assert snap["reverse_mode"].get("day_count") == 3


class TestShadowV14SessionRules:
    """v1.4: 세션 날짜 정산 / 멱등 / pending 보호 / REINVEST / 버전 가드."""

    @staticmethod
    def _accounting(snap):
        return (
            snap["cash_usd"], snap["holdings"], snap["T"], snap["net_invested"]
        )

    def test_settle_waits_until_session_closed(self, shadow_env):
        """세션 마감 전에는 정산하지 않고 의도를 pending으로 유지합니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        before = _load_latest(shadow_env)
        # autouse session_api 기본값: closed=False
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)

        after = _load_latest(shadow_env)
        assert after["phase"] == "generating"
        assert [p["event_id"] for p in after["pending_intents"]] == \
            [p["event_id"] for p in before["pending_intents"]]
        assert self._accounting(after) == self._accounting(before)
        events = _read_ledger_events(shadow_env)
        assert [e["event_type"] for e in events] == ["intent"]

    def test_settle_candle_date_mismatch_keeps_pending(self, shadow_env, session_api):
        """세션은 마감됐지만 캔들 date가 다르면 최신 종가로 대체하지 않습니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        before = _load_latest(shadow_env)
        session_api["closed"] = True
        broker.get_daily_candles.return_value = [
            {"date": "2026-01-02", "close": 45.0},  # 의도 세션(2026-01-05)과 불일치
        ]
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)

        after = _load_latest(shadow_env)
        assert after["phase"] == "generating"
        assert len(after["pending_intents"]) == 1
        assert after["pending_intents"][0]["session_date"] == _SESSION
        assert self._accounting(after) == self._accounting(before)
        assert len(_read_ledger_events(shadow_env)) == 1  # intent만, settlement 없음

    def test_settle_uses_matching_dated_close(self, shadow_env, session_api):
        """마감된 세션 + 동일 date 캔들이면 그 세션 종가로 정산합니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        session_api["closed"] = True
        broker.get_daily_candles.return_value = [
            {"date": "2026-01-02", "close": 51.0},
            {"date": _SESSION, "close": 47.0},  # 대상 세션
        ]
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)

        snap = _load_latest(shadow_env)
        assert snap["phase"] == "settled"
        assert snap["pending_intents"] == []
        assert snap["holdings"] == 2
        assert snap["T"] == 1.0
        assert snap["cash_usd"] == pytest.approx(8000.0 - 2 * 47.0 * 1.0025)
        settlement = [e for e in _read_ledger_events(shadow_env)
                      if e["event_type"] == "settlement"][-1]
        assert settlement["session_date"] == _SESSION
        assert settlement["daily_close"] == 47.0

    def test_settle_twice_is_idempotent(self, shadow_env, session_api):
        """같은 정산을 두 번 실행해도 회계가 중복 적용되지 않습니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        session_api["closed"] = True
        broker.get_daily_candles.return_value = [{"date": _SESSION, "close": 50.0}]

        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        first = _load_latest(shadow_env)

        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        second = _load_latest(shadow_env)

        assert self._accounting(second) == self._accounting(first)
        settlements = [e for e in _read_ledger_events(shadow_env)
                       if e["event_type"] == "settlement"]
        assert len(settlements) == 1

    def test_generate_protects_pending_and_force_expires(
        self, shadow_env, monkeypatch
    ):
        """pending이 있으면 generate를 건너뛰고, force 시 expired 후 새로 만듭니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        first_pending = _load_latest(shadow_env)["pending_intents"]
        first_id = first_pending[0]["event_id"]

        # 보호: 재실행해도 pending 유지 + 새 intent 없음
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        protected = _load_latest(shadow_env)["pending_intents"]
        assert [p["event_id"] for p in protected] == [first_id]
        assert len(_read_ledger_events(shadow_env)) == 1

        # 강제: 기존 pending은 expired 이벤트로 감사 가능하게 남긴다
        monkeypatch.setenv("SHADOW_FORCE_NEW_INTENTS", "true")
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)

        events = _read_ledger_events(shadow_env)
        expired = [e for e in events if e["event_type"] == "intent_expired"]
        assert len(expired) == 1
        assert expired[0]["intent_event_id"] == first_id
        assert expired[0]["status"] == "expired"
        intents = [e for e in events if e["event_type"] == "intent"]
        assert len(intents) == 2
        new_pending = _load_latest(shadow_env)["pending_intents"]
        assert len(new_pending) == 1
        assert new_pending[0]["event_id"] != first_id

    def test_reinvest_flag_controls_strategy_seed(self, shadow_env, monkeypatch):
        """REINVEST=false면 config seed, true면 effective_seed를 전략 시드로 씁니다."""
        captured = {}

        def _recorder(broker_, **kwargs):
            captured["seed"] = kwargs.get("seed")
            return {
                "symbol": kwargs.get("symbol", "TQQQ"),
                "exchange": kwargs.get("exchange_code", "NAS"),
                "tradable": True,
                "open_price": 49.0,
                "last_price": 50.0,
                "position_qty": 0,
                "avg_price": 0.0,
                "orderable_cash": 9000.0,
                "T": kwargs.get("T", 0.0),
                "orders": [],
            }

        monkeypatch.setattr(shadow_ledger, "무한매수법_V4", _recorder)
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        dir_true = os.path.join(shadow_env, "reinvest_true")
        _seed_snapshot(dir_true, effective_seed=9000.0)
        monkeypatch.setenv("REINVEST", "true")
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=dir_true)
        assert captured["seed"] == 9000.0  # effective_seed(복리)

        dir_false = os.path.join(shadow_env, "reinvest_false")
        _seed_snapshot(dir_false, effective_seed=9000.0)
        monkeypatch.setenv("REINVEST", "false")
        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=dir_false)
        assert captured["seed"] == 8000.0  # config seed

    def test_version_mismatch_raises_actionable_error(self, shadow_env):
        """가정 버전이 다르면 자동 마이그레이션 없이 명확한 오류로 중단합니다."""
        _seed_snapshot(shadow_env)
        path = os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")
        with open(path) as f:
            snap = json.load(f)
        snap["assumption_version"] = "v1.3"
        with open(path, "w") as f:
            json.dump(snap, f)

        with pytest.raises(shadow_ledger.ShadowVersionMismatchError) as exc:
            shadow_ledger.generate_symbol(
                _make_broker(), _symbol_config(), snapshot_dir=shadow_env
            )
        message = str(exc.value)
        assert "v1.3" in message and "v1.4" in message
        assert "삭제" in message or "archive" in message
        assert "측정 기준" in message

    def test_generate_skips_already_closed_session(self, shadow_env, session_api):
        """이미 마감된 세션에는 generate하지 않고 상태를 그대로 둡니다."""
        _seed_snapshot(
            shadow_env, holdings=2, T=2.0, cash=7900.0,
            avg=50.0, net_invested=100.0,
        )
        path = os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")
        with open(path) as f:
            before_raw = f.read()

        session_api["closed"] = True
        shadow_ledger.generate_symbol(
            _make_broker(), _symbol_config(), snapshot_dir=shadow_env
        )

        with open(path) as f:
            after_raw = f.read()
        assert after_raw == before_raw, "마감 세션 generate는 스냅샷을 변경하면 안 됩니다"
        snap = _load_latest(shadow_env)
        assert snap["pending_intents"] == []
        assert snap["phase"] == "settled"


class TestShadowCrashAndCorruption:
    """v1.4 안정성: 사이클 리셋 crash 복구 / 손상 스냅샷 가드."""

    def _full_sell_strategy(self, monkeypatch):
        monkeypatch.setattr(
            shadow_ledger, "무한매수법_V4",
            _orders_strategy([
                {"side": "SELL", "quantity": 5, "price": 50.0,
                 "order_type": "MOC", "comment": "전량매도", "t_target": 0.9},
            ]),
        )

    def test_cycle_reset_recovered_after_crash(
        self, shadow_env, monkeypatch, session_api
    ):
        """마지막 저장 직전 crash → 다음 settle이 리셋을 정확히 1회 복구한다."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(
            shadow_env, holdings=5, T=5.0, cash=7500.0,
            avg=50.0, net_invested=250.0, effective_seed=8000.0,
        )
        self._full_sell_strategy(monkeypatch)
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)

        real_save = shadow_ledger._save_snapshot
        calls = {"n": 0}

        def _flaky_save(snapshot_dir, symbol, snapshot):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash before final save")
            return real_save(snapshot_dir, symbol, snapshot)

        # 세션 마감 후 정산 (테스트 세션 시계)
        session_api["closed"] = True
        monkeypatch.setattr(shadow_ledger, "_save_snapshot", _flaky_save)
        with pytest.raises(RuntimeError, match="simulated crash"):
            shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        monkeypatch.setattr(shadow_ledger, "_save_snapshot", real_save)

        crashed = _load_latest(shadow_env)
        assert crashed["pending_intents"] == []
        assert crashed["cycle_reset_pending"] is True, \
            "per-intent 체크포인트에 리셋 마커가 저장되어야 합니다"
        assert crashed["effective_seed"] == 8000.0  # 리셋은 아직 미적용

        # 다음 settle 시작에서 마커를 보고 복구
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        recovered = _load_latest(shadow_env)
        assert recovered["effective_seed"] == 7750.0  # 8000 + (7750 − 8000)
        assert recovered["cycle_reset_pending"] is False
        assert recovered["T"] == 0.0
        assert recovered["net_invested"] == 0.0

        # 한 번 더 settle해도 복리/리셋이 중복 적용되지 않는다
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        again = _load_latest(shadow_env)
        assert again["effective_seed"] == 7750.0

        resets = [
            e for e in _read_ledger_events(shadow_env)
            if e["event_type"] == "cycle_reset"
        ]
        assert len(resets) == 1
        assert resets[0]["virtual_state_after"]["effective_seed"] == 7750.0

    def test_cycle_reset_applied_exactly_once(self, shadow_env, monkeypatch):
        """정상 경로에서도 반복 settle 시 리셋은 1회만 적용된다."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")
        _seed_snapshot(
            shadow_env, holdings=5, T=5.0, cash=7500.0,
            avg=50.0, net_invested=250.0, effective_seed=8000.0,
        )
        self._full_sell_strategy(monkeypatch)
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        _generate_and_settle(broker, cfg, shadow_env, close_price=50.0)
        first = _load_latest(shadow_env)
        assert first["effective_seed"] == 7750.0
        assert first["cycle_reset_pending"] is False

        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=shadow_env)
        again = _load_latest(shadow_env)
        assert again["effective_seed"] == 7750.0  # 복리 중복 없음

        resets = [
            e for e in _read_ledger_events(shadow_env)
            if e["event_type"] == "cycle_reset"
        ]
        assert len(resets) == 1

    @pytest.mark.parametrize("phase", ["generate", "settle"])
    def test_corrupt_snapshot_raises_without_overwrite(
        self, shadow_env, phase
    ):
        """손상 스냅샷은 조용히 재시작하지 않고 오류로 중단한다."""
        snap_dir = os.path.join(shadow_env, "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        path = os.path.join(snap_dir, "TQQQ_latest.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json")

        with open(path, encoding="utf-8") as f:
            before_raw = f.read()

        func = (
            shadow_ledger.generate_symbol
            if phase == "generate"
            else shadow_ledger.settle_symbol
        )
        with pytest.raises(shadow_ledger.ShadowSnapshotCorruptError) as exc:
            func(_make_broker(), _symbol_config(), snapshot_dir=shadow_env)

        message = str(exc.value)
        assert "읽을 수 없습니다" in message
        assert "SHADOW_RESET_ON_CORRUPT" in message
        with open(path, encoding="utf-8") as f:
            assert f.read() == before_raw, "오류 시 스냅샷을 덮어쓰면 안 됩니다"

    def test_corrupt_snapshot_force_reset_writes_event(
        self, shadow_env, monkeypatch
    ):
        """명시적 force일 때만 fresh 시작 + snapshot_reset 이벤트를 남긴다."""
        snap_dir = os.path.join(shadow_env, "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        path = os.path.join(snap_dir, "TQQQ_latest.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ broken")

        monkeypatch.setenv("SHADOW_RESET_ON_CORRUPT", "true")
        shadow_ledger.generate_symbol(
            _make_broker(), _symbol_config(), snapshot_dir=shadow_env
        )

        events = _read_ledger_events(shadow_env)
        resets = [e for e in events if e["event_type"] == "snapshot_reset"]
        assert len(resets) == 1
        assert resets[0]["reason"] == "SHADOW_RESET_ON_CORRUPT"

        snap = _load_latest(shadow_env)
        assert snap["assumption_version"] == "v1.4"
        assert len(snap["pending_intents"]) == 1  # fresh 시작 후 정상 generate
