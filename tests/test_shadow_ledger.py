"""
섀도우 원장(shadow/) 테스트.

검증 대상:
- (a) 연속 2회 전체 사이클(GENERATE+SETTLE): 가상 T/cash 이월 + 원장 4건.
- (b) 실제 state.json에 닿지 않습니다 (save_state 호출 0회).
- (c) broker.place_order를 호출하지 않습니다.
- (d) 전량매도 후 T>0 → 사이클 리셋 (T=0, 시드 갱신).
- (e) SHADOW_FEE_RATE 수수료가 매수/매도에 반영됩니다.
- (f) SHADOW_SLIPPAGE_BPS 슬리피지가 체결가에 반영됩니다.
- 2단계(v1.2): generate는 의도만 기록, settle은 종가 대조 체결/만료.
"""
import json
import os
import sys
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
from broker.base import Balance, PurchaseAmount, StockPrice, StockQuotation
from shadow.broker import VirtualBroker

# v1.2 가정 목록 (A1-A6 + C1)
_EXPECTED_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "C1"]


def _symbol_config(seed=8000.0):
    return {
        "symbol": "TQQQ",
        "exchange": "NAS",
        "splits": 20,
        "symbol_type": "TQQQ",
        "seed": seed,
        "additional_loc_levels": 3,
    }


def _make_broker():
    broker = MagicMock()
    broker.get_stock_quotation.return_value = StockQuotation(tradable=True, last=50.0)
    broker.get_stock_price.return_value = StockPrice(open=49.0, last=50.0)
    broker.get_balance.return_value = Balance(quantity=0, avg_price=0.0)
    broker.get_purchase_amount.return_value = PurchaseAmount(orderable_cash=8000.0)
    broker.get_daily_closes.return_value = [50.0]
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


def _seed_snapshot(snapshot_dir, holdings=0, T=0.0, cash=8000.0, avg=0.0, net_invested=0.0):
    """사전 상태 스냅샷을 생성합니다 (v1.2 스키마)."""
    snap_dir = os.path.join(snapshot_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, "TQQQ_latest.json"), "w") as f:
        json.dump({
            "symbol": "TQQQ",
            "schema_version": "v1.2",
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
            "assumption_version": "v1.2",
            "as_of": "2026-01-01T00:00:00+00:00",
        }, f)


def _generate_and_settle(broker, cfg, snapshot_dir, close_price):
    """GENERATE → SETTLE 전체 사이클 (fake broker 종가 고정)."""
    shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=snapshot_dir)
    broker.get_daily_closes.return_value = [close_price]
    shadow_ledger.settle_symbol(broker, cfg, snapshot_dir=snapshot_dir)


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
        assert snap["assumption_version"] == "v1.2"
        assert snap["schema_version"] == "v1.2"
        assert snap["phase"] == "settled"
        assert snap["pending_intents"] == []

        # 원장: 4건 (intent 2 + settlement 2, JSONL 1파일)
        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        assert len(ledger_files) == 1
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 4
        assert all(e["assumption_version"] == "v1.2" for e in lines)
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
        """(d) 전량매도 후 T>0 → 사이클 리셋 (T=0, 시드 갱신)."""
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
        # cash = 7500 + 5*50 = 7750 (수수료 0) → 다음 시드로 갱신
        assert snap["cash_usd"] == 7750.0


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
        assert ev["assumption_version"] == "v1.2"
        assert ev["assumptions"] == _EXPECTED_ASSUMPTIONS
        assert "virtual_state_before" in ev
        assert "virtual_state_after" not in ev
        assert ev["virtual_state_before"]["cash_usd"] == 8000.0

    def test_settle_buy_loc_fills_at_close(self, shadow_env):
        """(b) SETTLE: BUY LOC는 close<=limit이면 종가에 체결됩니다."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        broker.get_daily_closes.return_value = [45.0]  # close 45 <= limit 50
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

    def test_unfilled_expires_no_state_change(self, shadow_env):
        """(c) SETTLE: BUY LOC 미체결(close>limit) → 만료, 상태 변화 없음."""
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.generate_symbol(broker, cfg, snapshot_dir=shadow_env)
        broker.get_daily_closes.return_value = [55.0]  # close 55 > limit 50 → 미체결
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
        assert snap["T"] == 5.9
        assert snap["cash_usd"] == 7560.0  # 7500 + 1*60 (수수료 0)
        assert snap["net_invested"] == 190.0  # 250 - 60

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            events = [json.loads(line) for line in f if line.strip()]
        settlement = events[1]
        assert settlement["filled"] is True
        assert settlement["fill_price"] == 60.0  # 종가 체결
        assert settlement["t_delta"] == 0.9

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
        assert snap["T"] == 5.0  # t_target=0

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
        assert snap["T"] == 5.0