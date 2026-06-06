"""
T값 업데이트 로직 유닛 테스트

테스트 대상:
  - orders_meta의 t_target 기반 T 증가 (전반전/후반전 구분)
  - 추가매수(is_additional=True)는 T 변화 없음
  - 레거시 폴백 (meta 없을 때 건수 기반: 1건→+0.5, 2건→+1.0)
  - _apply_recent_history_dt, _infer_T_from_full_history 모두 검증
"""

import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from state import update_T_from_history, register_order_meta_in_state


# ─────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────

def _make_state(T=0.0, last_updated="", orders_meta=None, additional_loc_odno=None):
    return {
        "T": T,
        "last_updated": last_updated,
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "last_processed_ordno": "",
        "additional_loc_odno": additional_loc_odno or [],
        "orders_meta": orders_meta or {},
        "balance_mismatch": {},
        "state_version": "v2",
    }


def _make_buy_order(odno, ord_dt, qty, utc_dt=None):
    """체결 완료된 매수 주문 레코드 생성."""
    if utc_dt is None:
        utc_dt = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T10:00:00+00:00"
    return {
        "odno": odno,
        "ord_dt": ord_dt,
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": str(qty),
        "ord_datetime_utc": utc_dt,
    }


def _make_sell_order(odno, ord_dt, qty, utc_dt=None):
    """체결 완료된 매도 주문 레코드 생성."""
    if utc_dt is None:
        utc_dt = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T15:00:00+00:00"
    return {
        "odno": odno,
        "ord_dt": ord_dt,
        "sll_buy_dvsn_cd_name": "매도",
        "ft_ccld_qty": str(qty),
        "ord_datetime_utc": utc_dt,
    }


# ─────────────────────────────────────────────────────────
# 테스트: _apply_recent_history_dt (일반 모드 — last_updated 있음)
# ─────────────────────────────────────────────────────────

class TestApplyRecentHistory:
    """last_updated가 설정된 일반 모드 T 업데이트 테스트."""

    def test_후반전_단일매수_meta_t1(self):
        """후반전: meta t_target=1.0 → T += 1.0"""
        state = _make_state(T=2.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD001", {
            "side": "BUY",
            "total_qty": 3,
            "t_target": 1.0,
            "is_additional": False,
            "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD001", "20260528", qty=3)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 3.0, f"T={result['T']} (expected 3.0)"

    def test_전반전_별지점_meta_t05(self):
        """전반전 별지점: meta t_target=0.5 → T += 0.5"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD002", {
            "side": "BUY",
            "total_qty": 2,
            "t_target": 0.5,
            "is_additional": False,
            "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD002", "20260528", qty=2)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.5, f"T={result['T']} (expected 1.5)"

    def test_전반전_분할매수_2건_각각_t05(self):
        """전반전 별지점+평단 두 건 각각 t_target=0.5 → T += 1.0"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD003", {
            "side": "BUY", "total_qty": 2, "t_target": 0.5,
            "is_additional": False, "processed_filled_qty": 0,
        })
        register_order_meta_in_state(state, "ORD004", {
            "side": "BUY", "total_qty": 1, "t_target": 0.5,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [
            _make_buy_order("ORD003", "20260528", qty=2, utc_dt="2026-05-28T10:00:00+00:00"),
            _make_buy_order("ORD004", "20260528", qty=1, utc_dt="2026-05-28T10:01:00+00:00"),
        ]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_추가매수_is_additional_T_변화없음(self):
        """is_additional=True인 주문은 T 변화 없음."""
        state = _make_state(T=2.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD005", {
            "side": "BUY", "total_qty": 1, "t_target": 0.0,
            "is_additional": True, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD005", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_추가매수_additional_loc_odno_레거시_T_변화없음(self):
        """additional_loc_odno 목록에 있는 주문은 T 변화 없음 (레거시)."""
        state = _make_state(T=2.0, last_updated="2026-05-27 00:00:00",
                            additional_loc_odno=["ORD006"])
        orders = [_make_buy_order("ORD006", "20260528", qty=3)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_레거시_폴백_2건_plus1(self):
        """meta 없는 주문 2건 → 레거시 폴백 +1.0"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        orders = [
            _make_buy_order("ORD007", "20260528", qty=3, utc_dt="2026-05-28T10:00:00+00:00"),
            _make_buy_order("ORD008", "20260528", qty=2, utc_dt="2026-05-28T10:01:00+00:00"),
        ]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_레거시_폴백_1건_plus05(self):
        """meta 없는 주문 1건 → 레거시 폴백 +0.5"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        orders = [_make_buy_order("ORD009", "20260528", qty=3)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.5, f"T={result['T']} (expected 1.5)"

    def test_전반전_별지점만_소액시드_t1(self):
        """소액 시드: 별지점만 있고 평단 없음 → t_target=1.0 (1회분 전액)"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD020", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD020", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_전반전_평단만_소액시드_t1(self):
        """소액 시드: 평단만 있고 별지점 없음 → t_target=1.0 (1회분 전액)"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD021", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD021", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_전반전_소액시드_추가매수_함께_t1(self):
        """소액 시드: 평단 1주(T+1.0) + 추가매수(T+0.0) → T += 1.0"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD022", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        register_order_meta_in_state(state, "ORD023", {
            "side": "BUY", "total_qty": 1, "t_target": 0.0,
            "is_additional": True, "processed_filled_qty": 0,
        })
        orders = [
            _make_buy_order("ORD022", "20260528", qty=1, utc_dt="2026-05-28T10:00:00+00:00"),
            _make_buy_order("ORD023", "20260528", qty=1, utc_dt="2026-05-28T10:01:00+00:00"),
        ]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_부분체결_비례_반영(self):
        """주문 total_qty=4, 체결 qty=2 (50%) → ΔT = 0.5 * 1.0 = 0.5"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD010", {
            "side": "BUY", "total_qty": 4, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD010", "20260528", qty=2)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.5, f"T={result['T']} (expected 1.5)"

    def test_부분체결_processed_filled_qty_반영(self):
        """processed_filled_qty=2, 이번 체결 qty=2 → 추가 new_filled=0 → T 변화 없음"""
        state = _make_state(T=1.5, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD011", {
            "side": "BUY", "total_qty": 4, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 2,  # 이미 절반 반영됨
        })
        orders = [_make_buy_order("ORD011", "20260528", qty=2)]
        result = update_T_from_history("TQQQ", state, orders)
        # new_filled = 2 - 2 = 0 → ΔT = 0
        assert result["T"] == 1.5, f"T={result['T']} (expected 1.5)"

    def test_이력_없으면_T_변화없음(self):
        """매수 이력이 없으면 T는 그대로."""
        state = _make_state(T=3.0, last_updated="2026-05-27 00:00:00")
        result = update_T_from_history("TQQQ", state, [])
        assert result["T"] == 3.0

    def test_last_updated_이전_이력_무시(self):
        """last_updated(2026-05-27) 이전 주문(20260526)은 무시."""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD012", {
            "side": "BUY", "total_qty": 3, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD012", "20260526", qty=3,
                                  utc_dt="2026-05-26T10:00:00+00:00")]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.0, f"T={result['T']} (expected 1.0)"


# ─────────────────────────────────────────────────────────
# 테스트: _infer_T_from_full_history (초기 모드 — last_updated 없음)
# ─────────────────────────────────────────────────────────

class TestInferTFromFullHistory:
    """last_updated가 비어 있는 초기 모드 T 추정 테스트."""

    def test_후반전_단일매수_meta_t1_초기모드(self):
        """초기 모드에서 meta t_target=1.0 → T=1.0"""
        state = _make_state(T=0.0, last_updated="")
        register_order_meta_in_state(state, "ORD101", {
            "side": "BUY", "total_qty": 3, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD101", "20260528", qty=3)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.0, f"T={result['T']} (expected 1.0)"

    def test_추가매수_is_additional_초기모드_T_변화없음(self):
        """초기 모드: is_additional=True → T=0"""
        state = _make_state(T=0.0, last_updated="")
        register_order_meta_in_state(state, "ORD102", {
            "side": "BUY", "total_qty": 1, "t_target": 0.0,
            "is_additional": True, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD102", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 0.0, f"T={result['T']} (expected 0.0)"

    def test_레거시_폴백_qty1_initial_mode(self):
        """초기 모드 레거시: qty=1 매수는 추가매수로 분류 → T=0"""
        state = _make_state(T=0.0, last_updated="")
        orders = [_make_buy_order("ORD103", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 0.0, f"T={result['T']} (expected 0.0)"

    def test_레거시_폴백_qty2_초기모드_plus05(self):
        """초기 모드 레거시: qty=2 단일 매수 → +0.5"""
        state = _make_state(T=0.0, last_updated="")
        orders = [_make_buy_order("ORD104", "20260528", qty=2)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 0.5, f"T={result['T']} (expected 0.5)"

    def test_전반전_2건_분할매수_meta_초기모드(self):
        """초기 모드: 전반전 t_target=0.5 두 건 → T=1.0"""
        state = _make_state(T=0.0, last_updated="")
        register_order_meta_in_state(state, "ORD105", {
            "side": "BUY", "total_qty": 2, "t_target": 0.5,
            "is_additional": False, "processed_filled_qty": 0,
        })
        register_order_meta_in_state(state, "ORD106", {
            "side": "BUY", "total_qty": 1, "t_target": 0.5,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [
            _make_buy_order("ORD105", "20260528", qty=2, utc_dt="2026-05-28T10:00:00+00:00"),
            _make_buy_order("ORD106", "20260528", qty=1, utc_dt="2026-05-28T10:01:00+00:00"),
        ]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.0, f"T={result['T']} (expected 1.0)"

    def test_전반전_소액시드_1건_meta_t1_초기모드(self):
        """초기 모드: 소액 시드 전반전 1건 t_target=1.0 → T=1.0"""
        state = _make_state(T=0.0, last_updated="")
        register_order_meta_in_state(state, "ORD107", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD107", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 1.0, f"T={result['T']} (expected 1.0)"

    def test_이력_없으면_T_0(self):
        """초기 모드: 이력 없으면 T=0."""
        state = _make_state(T=0.0, last_updated="")
        result = update_T_from_history("TQQQ", state, [])
        assert result["T"] == 0.0


# ─────────────────────────────────────────────────────────
# 테스트: register_order_meta_in_state / get_order_meta
# ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_register_and_get(self):
        from state import get_order_meta
        state = _make_state()
        register_order_meta_in_state(state, "ORD999", {
            "side": "BUY", "total_qty": 5, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        meta = get_order_meta(state, "ORD999")
        assert meta is not None
        assert meta["t_target"] == 1.0
        assert meta["is_additional"] is False

    def test_odno_를_str로_변환(self):
        from state import get_order_meta
        state = _make_state()
        register_order_meta_in_state(state, 12345, {"t_target": 0.5})
        meta = get_order_meta(state, "12345")
        assert meta is not None
        meta2 = get_order_meta(state, 12345)
        assert meta2 is not None

    def test_없는_odno_None_반환(self):
        from state import get_order_meta
        state = _make_state()
        assert get_order_meta(state, "NOTEXIST") is None
