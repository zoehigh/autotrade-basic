"""
키움 TR 레지스트리 단위 테스트.
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from broker.kiwoom.tr_registry import (
    get_endpoint,
    TR_BUY,
    TR_SELL,
    TR_BALANCE,
    TR_PRICE,
)


class TestTrRegistry:
    def test_buy_order_endpoint(self):
        method, path = get_endpoint(TR_BUY)
        assert method == "POST"
        assert path == "/api/us/ordr"

    def test_sell_order_endpoint(self):
        method, path = get_endpoint(TR_SELL)
        assert method == "POST"
        assert path == "/api/us/ordr"

    def test_balance_endpoint(self):
        method, path = get_endpoint(TR_BALANCE)
        assert method == "POST"
        assert path == "/api/us/acnt"

    def test_price_endpoint(self):
        method, path = get_endpoint(TR_PRICE)
        assert method == "POST"
        assert path == "/api/us/mrkcond"

    def test_unknown_tr_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_endpoint("unknown_tr_id")
