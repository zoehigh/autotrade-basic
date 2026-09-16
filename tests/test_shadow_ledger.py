"""
섀도우 원장(shadow/) 테스트.

검증 대상:
- (a) 연속 2회 실행 시 가상 T/cash가 이월되고 원장에 2건이 추가됩니다.
- (b) 실제 state.json에 닿지 않습니다 (save_state 호출 0회).
- (c) broker.place_order를 호출하지 않습니다.
- (d) 전량매도 후 T>0 → 사이클 리셋 (T=0, 시드 갱신).
- (e) SHADOW_FEE_RATE 수수료가 매수/매도에 반영됩니다.
- (f) SHADOW_SLIPPAGE_BPS 슬리피지가 체결가에 반영됩니다.
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

# v1.1 가정 목록 (A1-A6 + B1-B4)
_EXPECTED_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "B3", "B4"]


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
    return broker


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
        """(a) 연속 2회 실행: T/cash 이월 + 원장 2건.

        기본 수수료(COMMISSION_RATE=0.0025)가 적용됩니다:
          BUY 2주 @50 → cash -= 2*50*1.0025 = 100.25
        """
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.run_shadow_symbol(broker, cfg, snapshot_dir=shadow_env)
        shadow_ledger.run_shadow_symbol(broker, cfg, snapshot_dir=shadow_env)

        # 스냅샷: T=2.0, cash=8000-100.25*2, holdings=4, net_invested=200
        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["T"] == 2.0
        assert snap["cash_usd"] == 7799.5
        assert snap["holdings"] == 4
        assert snap["net_invested"] == 200.0
        assert snap["assumption_version"] == "v1.1"

        # 원장: 2건 (JSONL 1파일)
        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        assert len(ledger_files) == 1
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 2
        assert all(e["assumption_version"] == "v1.1" for e in lines)
        assert all(e["fill_ratio"] == 1.0 for e in lines)
        assert all(e["fee_usd"] == 0.25 for e in lines)  # 2*50*0.0025
        assert all(e["assumptions"] == _EXPECTED_ASSUMPTIONS for e in lines)
        assert lines[0]["virtual_state_after"]["T"] == 1.0
        assert lines[1]["virtual_state_after"]["T"] == 2.0
        assert lines[0]["virtual_state_after"]["cash_usd"] == 7899.75
        assert lines[1]["virtual_state_after"]["cash_usd"] == 7799.5

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
        """(e) SHADOW_FEE_RATE 수수료가 매수에 반영됩니다 (fee 1%)."""
        monkeypatch.setenv("SHADOW_FEE_RATE", "0.01")
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.run_shadow_symbol(broker, cfg, snapshot_dir=shadow_env)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        # BUY 2주 @50, fee 1% → cash = 8000 - 2*50*1.01 = 7899
        assert snap["cash_usd"] == 7899.0
        assert snap["holdings"] == 2
        assert snap["net_invested"] == 100.0  # fee는 net_invested에 미포함

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            event = json.loads(f.readline())
        assert event["fee_usd"] == 1.0
        assert event["assumed_fill_price"] == 50.0  # 슬리피지 없음

    def test_slippage_applied(self, shadow_env, monkeypatch):
        """(f) SHADOW_SLIPPAGE_BPS 슬리피지가 매수 체결가에 반영됩니다 (2%)."""
        monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "200")
        monkeypatch.setenv("SHADOW_FEE_RATE", "0")  # 슬리피지만 검증
        broker = _make_broker()
        cfg = _symbol_config(seed=8000.0)

        shadow_ledger.run_shadow_symbol(broker, cfg, snapshot_dir=shadow_env)

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        # BUY 2주 @50, slippage 2% → fill 51.0 → cash = 8000 - 2*51 = 7898
        assert snap["cash_usd"] == 7898.0
        assert snap["avg_price"] == 51.0
        assert snap["net_invested"] == 102.0

        ledger_files = os.listdir(os.path.join(shadow_env, "ledger"))
        with open(os.path.join(shadow_env, "ledger", ledger_files[0])) as f:
            event = json.loads(f.readline())
        assert event["assumed_fill_price"] == 51.0
        assert event["fee_usd"] == 0.0  # 수수료 미설정

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
        snap_dir = os.path.join(shadow_env, "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, "TQQQ_latest.json"), "w") as f:
            json.dump({
                "symbol": "TQQQ", "T": 5.0, "cash_usd": 7500.0, "holdings": 5,
                "avg_price": 50.0, "net_invested": 250.0,
                "net_invested_status": "valid",
                "reverse_mode": {"active": False}, "close_prices": [],
                "assumption_version": "v1.1",
                "as_of": "2026-01-01T00:00:00+00:00",
            }, f)

        shadow_ledger.run_shadow_symbol(
            _make_broker(), _symbol_config(seed=8000.0), snapshot_dir=shadow_env
        )

        with open(os.path.join(shadow_env, "snapshots", "TQQQ_latest.json")) as f:
            snap = json.load(f)
        assert snap["T"] == 0.0
        assert snap["holdings"] == 0
        assert snap["net_invested"] == 0.0
        assert snap["avg_price"] == 0.0
        # cash = 7500 + 5*50 = 7750 (수수료 0) → 다음 시드로 갱신
        assert snap["cash_usd"] == 7750.0