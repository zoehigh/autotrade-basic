"""
키움 거래소 코드 매핑 단위 테스트.
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from broker.kiwoom.exchange import get_api_exchange_code, get_currency


class TestKiwoomExchange:
    def test_nas_to_nd(self):
        assert get_api_exchange_code("NAS") == "ND"

    def test_nys_to_ny(self):
        assert get_api_exchange_code("NYS") == "NY"

    def test_ams_to_na(self):
        assert get_api_exchange_code("AMS") == "NA"

    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError):
            get_api_exchange_code("XXX")

    def test_currency_usd(self):
        assert get_currency("NAS") == "USD"
