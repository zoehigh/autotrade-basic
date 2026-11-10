"""보정된 정상매수 + 보정가 이상 추가매수 수량 합산 테스트 (MERGE_CAPPED_ADDITIONAL).

검증 범위
  1) 전략: 원래 주문표 기준 합산, 추가매수와 중복 없음, 기본 OFF 는 기존과 동일
  2) 정산: 합산 주문의 T·순투입이 '정상 + 추가 분리 주문' 기준선과 같은지
     (완전체결 / 부분체결 / 지연 체결 / 이중 가산 없음 / 추가매수 단독 체결)
"""
import os
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import strategy
from state import update_T_from_history
from test_buy_price_cap import _soxl_late_half_orders

PRICE = 111.51


def _normals(result):
    return [o for o in result["orders"] if o["side"] == "BUY" and "[추가매수]" not in o["comment"]]


def _additionals(result):
    return [o for o in result["orders"] if "[추가매수]" in o["comment"]]


# ── 1) 전략 ──────────────────────────────────────────────────────────

def test_default_off_keeps_existing_behavior(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", False)
    result = _soxl_late_half_orders(monkeypatch)
    normal = _normals(result)[0]
    assert normal["quantity"] == 4 and "merged_additional_qty" not in normal
    assert [o["price"] for o in _additionals(result)] == [96.53, 82.74, 72.40]


def test_on_folds_levels_at_or_above_cap_into_normal_order(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    result = _soxl_late_half_orders(monkeypatch)
    normal = _normals(result)[0]
    # 원래 주문표: 정상 4주 @118.10 + 추가 [115.84, 96.53, 82.74] → 115.84(>= 보정가 111.51)만 합산
    assert (normal["price"], normal["normal_qty"], normal["merged_additional_qty"], normal["quantity"]) == (PRICE, 4, 1, 5)
    assert [o["price"] for o in _additionals(result)] == [96.53, 82.74]
    # 총 수량은 원래 주문표(정상 4 + 추가 3 = 7)와 같고 깊은 단계(72.40)를 더 만들지 않음 (중복 없음)
    total = normal["quantity"] + sum(o["quantity"] for o in _additionals(result))
    assert total == 4 + 3
    # 합산 주문은 정상매수로 남아야 T 가 오름 — 추가매수 표식이 없어야 함
    assert "[추가매수]" not in normal["comment"] and normal["t_target"] == 1.0


def test_no_cap_means_no_merge(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    result = _soxl_late_half_orders(monkeypatch, last_price=120.0)  # 상한 보정 없음
    normal = _normals(result)[0]
    assert "merged_additional_qty" not in normal and normal["quantity"] == 4
    assert [o["price"] for o in _additionals(result)] == [115.84, 96.53, 82.74]


def test_ladder_prices_stay_strictly_below_merged_order_and_unique(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    result = _soxl_late_half_orders(monkeypatch)
    buys = [o["price"] for o in result["orders"] if o["side"] == "BUY"]
    assert buys == sorted(buys, reverse=True) and len(set(buys)) == len(buys)


def _orders(*normals):
    return [{"side": "BUY", "quantity": q, "price": p, "original_price": op,
             "comment": "전반전 매수", "order_type": "LOC"} for q, p, op in normals]


def test_first_half_merges_into_lowest_priced_normal_order(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    orders = _orders((3, 111.51, 118.10), (2, 100.0, None))  # 별지점(보정) + 평단
    ladder = strategy._additional_ladder_with_merge(orders, 579.2, 4, 3, 100.0)
    # 원래 천장 = min(118.10, 100.0) = 100.0 = 보정 천장 → 합산 없음, 기존 계단과 동일
    assert "merged_additional_qty" not in orders[0] and "merged_additional_qty" not in orders[1]
    assert ladder == strategy.build_additional_loc_prices(579.2, 4, 3, 100.0)


def test_merge_target_is_the_ceiling_order_when_both_normals_capped(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    orders = _orders((3, 111.51, 118.10), (2, 105.0, 117.0))
    ladder = strategy._additional_ladder_with_merge(orders, 579.2, 4, 3, 105.0)
    # 원래 천장 117.0 → 원래 계단 [115.84, 96.53, 82.74]; 115.84 >= 105 만 합산, 나머지는 계단로 남김
    assert "merged_additional_qty" not in orders[0]          # 가장 낮은 가격(105) 주문에만 합산
    assert (orders[1]["normal_qty"], orders[1]["merged_additional_qty"], orders[1]["quantity"]) == (2, 1, 3)
    assert [p for _l, p in ladder] == [96.53, 82.74]
    assert 2 + 3 + 1 + len(ladder) == (2 + 3) + 3             # 총 수량 = 정상 + levels (중복 없음)


# ── 2) 정산: 합산 주문 vs 분리 주문 기준선 ────────────────────────────

def _state(meta):
    return {
        "T": 5.0, "last_updated": "2026-05-27 00:00:00", "cycle_start_date": "",
        "effective_seed": 0.0, "net_invested": 1000.0, "net_invested_status": "valid",
        "last_processed_ordno": "", "additional_loc_odno": [], "orders_meta": deepcopy(meta),
        "balance_mismatch": {}, "reverse_mode": {}, "state_version": "v2",
    }


def _fill(odno, qty, price=PRICE, day="20260528"):
    return {
        "odno": odno, "ord_dt": day, "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": str(qty), "ft_ccld_amt3": str(round(qty * price, 2)),
        "ft_ccld_unpr3": str(price), "ord_datetime_utc": "2026-05-28T10:00:00+00:00",
    }


def _meta(total, t_target, additional=False, **extra):
    base = {"side": "BUY", "total_qty": total, "t_target": t_target, "is_additional": additional,
            "processed_filled_qty": 0, "processed_filled_amount": 0.0, "fill_marker_version": 1}
    base.update(extra)
    return base


SPLIT = {"N": _meta(4, 1.0), "A": _meta(1, 0.0, additional=True)}
MERGED = {"M": _meta(5, 1.0, merged_additional_qty=1, normal_qty=4)}


def test_full_fill_merged_equals_split_baseline_for_T_and_net_invested():
    split = update_T_from_history("SOXL", _state(SPLIT), [_fill("N", 4), _fill("A", 1)])
    merged = update_T_from_history("SOXL", _state(MERGED), [_fill("M", 5)])
    assert split["T"] == merged["T"] == 6.0       # 추가 수량은 T 를 올리지 않음
    assert round(split["net_invested"], 2) == round(merged["net_invested"], 2) == round(1000 + 5 * PRICE, 2)


def test_partial_fill_is_pro_rata_and_late_fill_does_not_double_count():
    state = _state(MERGED)
    first = update_T_from_history("SOXL", state, [_fill("M", 2)])
    assert first["T"] == 5.4                              # 2/5 × 1.0
    assert round(first["net_invested"], 2) == round(1000 + 2 * PRICE, 2)
    assert first["orders_meta"]["M"]["processed_filled_qty"] == 2
    # 같은 이력으로 다시 정산해도 변화 없음 (재시작/중복 실행)
    again = update_T_from_history("SOXL", deepcopy(first), [_fill("M", 2)])
    assert again["T"] == 5.4 and round(again["net_invested"], 2) == round(first["net_invested"], 2)
    # 나머지가 늦게 체결: 누적 5주 → 델타 3주만 반영, 최종 T 는 완전체결과 같음
    late = update_T_from_history("SOXL", deepcopy(first), [_fill("M", 5)])
    assert late["T"] == 6.0
    assert round(late["net_invested"], 2) == round(1000 + 5 * PRICE, 2)
    assert late["orders_meta"]["M"]["processed_filled_qty"] == 5


def test_ladder_additional_filled_alone_does_not_move_T():
    meta = {**MERGED, "L": _meta(1, 0.0, additional=True)}
    result = update_T_from_history("SOXL", _state(meta), [_fill("L", 1, price=96.53)])
    assert result["T"] == 5.0 and round(result["net_invested"], 2) == round(1000 + 96.53, 2)


def test_merged_and_ladder_both_fill_when_close_is_low():
    meta = {**MERGED, "L": _meta(1, 0.0, additional=True)}
    result = update_T_from_history("SOXL", _state(meta), [_fill("M", 5, price=80.0), _fill("L", 1, price=80.0)])
    assert result["T"] == 6.0 and round(result["net_invested"], 2) == round(1000 + 6 * 80.0, 2)


def test_half_target_merged_order_adds_only_half_T():
    """전반전 별지점/평단 분할(각 0.5)에 합산된 경우도 정상 몫만큼만 T 가 오름."""
    meta = {"M": _meta(5, 0.5, merged_additional_qty=1, normal_qty=4)}
    result = update_T_from_history("SOXL", _state(meta), [_fill("M", 5)])
    assert result["T"] == 5.5


def test_reprice_tool_skips_merged_orders():
    import meritz_reprice as rp
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    et = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    row = {"odno": f"{now:%Y%m%d}:0000100", "ord_dt": f"{now:%Y%m%d}", "ord_tmd": f"{now:%H%M%S}",
           "_ord_dt_is_kst_date": True, "sll_buy_dvsn_cd_name": "매수", "ft_ord_qty": "5",
           "ft_ccld_qty": "0", "nccs_qty": "5", "prcs_stat_name": "미체결", "_order_price": 100.0,
           "_oder_cls_code": "32", "_rctf_code": "0", "_plain_orgl_oder_no": "", "_oder_date": f"{now:%Y%m%d}",
           "_is_rejected": False, "_is_cancel": False, "_ordered_qty": 5}
    meta = _meta(5, 1.0, merged_additional_qty=1, normal_qty=4, original_price=118.10)
    item = rp.plan_order(row["odno"], meta, row, 100.0, {}, et)
    assert item["action"] == rp.ACTION_SKIP and "합산" in item["reason"]


def test_partial_fill_equivalence_with_split_orders_is_not_guaranteed():
    """완전체결만 분리 주문과 동등하다는 점을 고정: 2주 체결 시 합산은 0.4, 정상 2주만 체결된 분리 주문은 0.5."""
    merged = update_T_from_history("SOXL", _state(MERGED), [_fill("M", 2)])
    split = update_T_from_history("SOXL", _state(SPLIT), [_fill("N", 2)])
    assert merged["T"] == 5.4 and split["T"] == 5.5
    # 순투입은 체결금액 기준이라 같음
    assert round(merged["net_invested"], 2) == round(split["net_invested"], 2)


# ── "큰수 이상" 경계: 보정가와 같은 가격의 단계는 합산, 바로 아래 단계는 추가매수로 유지 ────────

def _capped_order(price=110.0, original=118.10, qty=4):
    return [{"side": "BUY", "quantity": qty, "price": price, "original_price": original,
             "comment": "후반전 별지점 매수", "order_type": "LOC"}]


def test_level_exactly_equal_to_cap_price_is_merged(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    orders = _capped_order()
    ladder = strategy._additional_ladder_with_merge(orders, 550.0, 4, 3, 110.0)   # 550/5 = 110.00 (== 보정가)
    assert orders[0]["merged_additional_qty"] == 1 and orders[0]["quantity"] == 5
    assert [p for _l, p in ladder] == [91.66, 78.57]                              # 같은 가격 단계는 별도 추가매수로 남지 않음


def test_level_just_below_cap_price_is_kept_as_additional_not_merged(monkeypatch):
    monkeypatch.setattr(strategy, "MERGE_CAPPED_ADDITIONAL", True)
    orders = _capped_order()
    ladder = strategy._additional_ladder_with_merge(orders, 545.0, 4, 3, 110.0)   # 545/5 = 109.00 (< 보정가)
    assert "merged_additional_qty" not in orders[0] and orders[0]["quantity"] == 4
    assert [p for _l, p in ladder][0] == 109.0 and len(ladder) == 3
