"""
토스 주문 정규화/열린 주문 조회 단위 테스트 (API 호출 없음).

배경 (2026-10-23 실측):
- 토스 주문의 실제 체결 필드는 execution.averageFilledPrice / filledAmount 이고
  기존 코드는 averagePrice 를 읽어 FILLED 1건의 체결금액이 0 으로 반영됐습니다.
- status=CLOSED 고정 조회는 OPEN/PARTIAL 주문을 누락해 취소 대상에서 빠졌습니다.
  읽기 전용 실측 결과 status=OPEN 은 HTTP 200 으로 지원됩니다.
"""
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from broker.toss.adapter import TossBroker, map_order_status  # noqa: E402


def _broker():
    """__init__(config/네트워크) 없이 정규화 메서드만 쓰는 인스턴스."""
    return object.__new__(TossBroker)


def _raw(
    order_id="OD1",
    status="FILLED",
    side="BUY",
    quantity="1",
    filled_qty="1",
    avg_price=None,
    filled_amount=None,
    price="55.38",
):
    execution = {"filledQuantity": filled_qty}
    if avg_price is not None:
        execution["averageFilledPrice"] = avg_price
    if filled_amount is not None:
        execution["filledAmount"] = filled_amount
    return {
        "orderId": order_id,
        "symbol": "TQQQ",
        "side": side,
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "status": status,
        "price": price,
        "quantity": quantity,
        "orderedAt": "2026-09-17T13:26:52.029+09:00",
        "execution": execution,
    }


def test_map_order_status_english_to_korean():
    assert map_order_status("OPEN") == "미체결"
    assert map_order_status("PENDING") == "미체결"
    assert map_order_status("PARTIAL") == "부분체결"
    assert map_order_status("FILLED") == "체결"
    assert map_order_status("CANCELED") == "취소"
    assert map_order_status("REJECTED") == "거부"
    # 알 수 없는 상태는 종료로 간주하지 않도록 원본 유지
    assert map_order_status("SOMETHING_NEW") == "SOMETHING_NEW"


def test_normalize_reads_actual_toss_execution_fields():
    """averageFilledPrice/filledAmount 를 읽어 FILLED 금액이 0 이 아니어야 한다."""
    row = _broker()._normalize_order_item(
        _raw(avg_price="69.21", filled_amount="69.21")
    )
    assert row["ft_ccld_qty"] == "1"
    assert float(row["ft_ccld_unpr3"]) == pytest.approx(69.21)
    assert float(row["ft_ccld_amt3"]) == pytest.approx(69.21)
    assert row["prcs_stat_name"] == "체결"
    assert row["prcs_stat_name_raw"] == "FILLED"
    assert row["_ft_ccld_amt_estimated"] is False


def test_normalize_does_not_estimate_amount_from_average_price():
    """금액 누락 시 평균가로 추정하지 않고 수량을 보존해 공통 보류 로직으로 넘깁니다."""
    row = _broker()._normalize_order_item(
        _raw(status="PARTIAL", quantity="5", filled_qty="2",
             avg_price="10.5", filled_amount="0")
    )
    assert row["ft_ccld_qty"] == "2"
    assert row["ft_ccld_amt3"] == "0"
    assert row["_ft_ccld_amt_source"] == ""
    assert row["_ft_ccld_amt_estimated"] is False
    assert row["_fill_amount_unavailable"] is True
    assert row["prcs_stat_name"] == "부분체결"
    assert row["nccs_qty"] == "3"


def test_normalize_does_not_fall_back_to_last_price():
    """현재가 fallback 을 제거 — 금액 미확정으로 남겨 정산을 보류합니다."""
    row = _broker()._normalize_order_item(
        _raw(status="PARTIAL", quantity="5", filled_qty="2", filled_amount="0"),
        last_price=7.5,
    )
    assert row["ft_ccld_qty"] == "2"
    assert row["ft_ccld_amt3"] == "0"
    assert row["_ft_ccld_amt_source"] == ""
    assert row["_fill_amount_unavailable"] is True
    assert float(row["ft_ccld_unpr3"]) == 0.0


def test_normalize_preserves_quantity_when_amount_unresolvable(capsys):
    """수량을 0으로 만들면 보류 대상에서 사라져 영구 누락되므로 그대로 보존합니다."""
    row = _broker()._normalize_order_item(
        _raw(status="PARTIAL", quantity="5", filled_qty="2", filled_amount="0")
    )
    out = capsys.readouterr().out
    assert "정산 보류" in out
    assert row["ft_ccld_qty"] == "2"
    assert row["_skipped_fill_qty"] == "0"
    assert row["_fill_amount_unavailable"] is True
    assert row["ft_ccld_amt3"] == "0"
    assert row["prcs_stat_name"] == "부분체결"


def test_normalize_open_order_maps_to_unfilled_and_keeps_remaining():
    row = _broker()._normalize_order_item(
        _raw(status="OPEN", quantity="3", filled_qty="0")
    )
    assert row["prcs_stat_name"] == "미체결"
    assert row["nccs_qty"] == "3"
    assert row["ft_ccld_qty"] == "0"


def test_normalize_missing_quantity_omits_ft_ord_qty():
    """원주문 수량(quantity)이 없으면 "0"을 합성하지 않고 키를 생략합니다."""
    raw = _raw()
    raw.pop("quantity")

    row = _broker()._normalize_order_item(raw)

    assert "ft_ord_qty" not in row


def test_normalize_present_zero_quantity_keeps_ft_ord_qty():
    """필드가 있으면 값이 "0"이어도 기존처럼 전달합니다."""
    row = _broker()._normalize_order_item(_raw(quantity="0"))

    assert row["ft_ord_qty"] == "0"


def _mock_broker(status_orders, calls=None):
    broker = _broker()
    broker._get_token = lambda: "token"
    broker._rate_limit_wait = 0

    def fake_request(method, path, token, params=None, json_body=None,
                     extra_headers=None, return_headers=False, retry_network=True):
        if calls is not None:
            calls.append(dict(params or {}))
        status = (params or {}).get("status", "")
        return {"result": {"orders": status_orders.get(status, [])}}, {}

    broker._request_with_rate_retry = fake_request
    return broker


def test_get_open_orders_uses_open_status():
    calls = []
    broker = _mock_broker(
        {"OPEN": [_raw(order_id="OPEN1", status="OPEN", quantity="2", filled_qty="0")]},
        calls,
    )
    rows = broker.get_open_orders("TQQQ", "NAS", days=30)

    assert [c["status"] for c in calls] == ["OPEN"]
    assert len(rows) == 1
    assert rows[0]["odno"] == "OPEN1"
    assert rows[0]["prcs_stat_name"] == "미체결"


def test_order_history_merges_closed_and_open_orders():
    calls = []
    broker = _mock_broker(
        {
            "CLOSED": [_raw(order_id="CLOSED1", status="FILLED",
                            avg_price="10", filled_amount="10")],
            "OPEN": [
                _raw(order_id="OPEN1", status="OPEN", quantity="2", filled_qty="0"),
                # CLOSED 와 중복되면 CLOSED(최종 상태)를 우선합니다.
                _raw(order_id="CLOSED1", status="OPEN", quantity="1", filled_qty="0"),
            ],
        },
        calls,
    )
    rows = broker.get_order_history("TQQQ", "NAS", days=30)

    assert [c["status"] for c in calls] == ["CLOSED", "OPEN"]
    assert [row["odno"] for row in rows] == ["CLOSED1", "OPEN1"]
    assert rows[0]["prcs_stat_name"] == "체결"
    assert rows[1]["prcs_stat_name"] == "미체결"


def test_normalize_marks_ord_dt_as_kst_date():
    """TOSS ord_dt 는 KST 날짜 — state 의 ET round-trip 복원을 건너뜁니다."""
    row = _broker()._normalize_order_item(
        _raw(avg_price="10", filled_amount="10")
    )
    assert row["_ord_dt_is_kst_date"] is True
    assert row["ord_dt"] == "20260917"  # orderedAt 2026-09-17T13:26:52+09:00
