"""LS 주문이력 정규화 단위 테스트 (API 호출 없음).

원주문 수량(`ft_ord_qty`)은 응답에 있을 때만 보존합니다. 없으면 "0"을
합성하지 않고 키를 생략해 VERIFY 가 "필드 없음(관대)"으로 판정하도록 합니다.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from broker.ls.adapter import LSBroker  # noqa: E402


class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _normalized_row(raw_item):
    """LS get_order_history 1페이지 응답을 정규화한 행을 반환합니다."""
    response = _FakeResponse(
        {"rsp_cd": "00000", "COSAQ00102OutBlock1": [raw_item]},
        headers={"tr_cont": "N"},
    )
    broker = object.__new__(LSBroker)
    setattr(broker, "_post", lambda *args, **kwargs: response)
    return broker.get_order_history("TQQQ", "NAS")[0]


def _raw_item(**overrides):
    item = {
        "ord_dt": "20260615",
        "ord_tmd": "093000",
        "prdt_name": "TQQQ",
        "sll_buy_dvsn_cd_name": "02",
        "ft_ccld_qty": "1",
        "ft_ccld_unpr3": "54.00",
        "ft_ccld_amt3": "54.00",
        "nccs_qty": "0",
        "prcs_stat_name": "체결완료",
        "tr_mket_name": "NASDAQ",
        "tr_crcy_cd": "USD",
        "odno": "36267",
        "ovrs_excg_cd": "NASD",
    }
    item.update(overrides)
    return item


def test_missing_qty_field_omits_ft_ord_qty():
    row = _normalized_row(_raw_item())

    assert "ft_ord_qty" not in row
    assert row["ft_ccld_qty"] == "1"


def test_present_zero_qty_keeps_ft_ord_qty():
    row = _normalized_row(_raw_item(ft_ord_qty="0"))

    assert row["ft_ord_qty"] == "0"
