"""
T값 업데이트 로직 유닛 테스트

테스트 대상:
  - orders_meta의 t_target 기반 T 증가 (전반전/후반전 구분)
  - 추가매수(is_additional=True)는 T 변화 없음
  - 미등록매수 폴백 (meta 없을 때 건수 기반: 1건→+0.5, 2건→+1.0)
  - _apply_recent_history_dt, _infer_T_from_full_history 모두 검증
"""

import sys
import os

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
        "ft_ccld_amt3": str(qty * 10.0),
        "ft_ccld_unpr3": "10.0",
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
        "ft_ccld_amt3": str(qty * 10.0),
        "ft_ccld_unpr3": "10.0",
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

    def test_추가매수_additional_loc_odno_미등록_T_변화없음(self):
        """additional_loc_odno 목록에 있는 주문은 T 변화 없음 (미등록)."""
        state = _make_state(T=2.0, last_updated="2026-05-27 00:00:00",
                            additional_loc_odno=["ORD006"])
        orders = [_make_buy_order("ORD006", "20260528", qty=3)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_미등록매수_폴백_2건_plus1(self):
        """meta 없는 주문 2건 → 미등록매수 폴백 +1.0"""
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        orders = [
            _make_buy_order("ORD007", "20260528", qty=3, utc_dt="2026-05-28T10:00:00+00:00"),
            _make_buy_order("ORD008", "20260528", qty=2, utc_dt="2026-05-28T10:01:00+00:00"),
        ]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"

    def test_미등록매수_폴백_1건_plus05(self):
        """meta 없는 주문 1건 → 미등록매수 폴백 +0.5"""
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
        """이력이 없는데 보유 중이면 T는 그대로 (조회 누락/정지 복귀 보호)."""
        state = _make_state(T=3.0, last_updated="2026-05-27 00:00:00")
        result = update_T_from_history("TQQQ", state, [], balance_qty=10)
        assert result["T"] == 3.0

    def test_이력_없음_리버스모드면_경고_대신_안내(self, capsys):
        """리버스모드 진행 중이면 일반모드 체결 없음이 정상 → [경고] 없이 안내만 출력."""
        state = _make_state(T=3.0, last_updated="2026-05-27 00:00:00")
        state["reverse_mode"] = {"active": True, "day_count": 7}
        result = update_T_from_history("TQQQ", state, [], balance_qty=10)
        assert result["T"] == 3.0
        out = capsys.readouterr().out
        assert "[경고]" not in out, f"리버스모드 중 불필요한 경고 출력:\n{out}"
        assert "리버스 체결은 reconciliation에서 반영" in out

    def test_이력_없음_잔고0이면_T_리셋(self):
        """이력 없음 + 잔고 0 + T>0 → 잘못된 state 복구를 위해 T=0 리셋 (Case B)."""
        state = _make_state(T=3.0, last_updated="2026-05-27 00:00:00")
        result = update_T_from_history("TQQQ", state, [], balance_qty=0)
        assert result["T"] == 0.0

    def test_이력_없음_잔고_미확인이면_T_유지(self):
        """이력 없음 + 잔고 조회 실패(None) → 보수적으로 T 유지 (사고 방지)."""
        state = _make_state(T=3.0, last_updated="2026-05-27 00:00:00")
        result = update_T_from_history("TQQQ", state, [], balance_qty=None)
        assert result["T"] == 3.0

    def test_T_오추정_재추정_이력없고_보유중이면_보류(self):
        """T 오추정 재추정 경로도 이력 없음 + 보유 중이면 mismatch를 유지하고 보류."""
        state = _make_state(T=0.0, last_updated="2026-06-15T00:00:00+00:00")
        state["balance_mismatch"] = {"note": "T-estimation-suspected-low"}
        register_order_meta_in_state(state, "ORD030", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        result = update_T_from_history("SOXL", state, [], balance_qty=3)
        assert result["T"] == 0.0
        assert result["balance_mismatch"]["note"] == "T-estimation-suspected-low"

    def test_T_오추정_재추정_이력있으면_복구하고_mismatch_제거(self):
        """T 오추정 재추정 경로에서 orders_meta 이력이 있으면 T를 복구하고 mismatch를 제거."""
        state = _make_state(T=0.0, last_updated="2026-06-15T00:00:00+00:00")
        state["balance_mismatch"] = {"note": "T-estimation-suspected-low"}
        register_order_meta_in_state(state, "ORD031", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        orders = [_make_buy_order("ORD031", "20260614", qty=1)]
        result = update_T_from_history("SOXL", state, orders, balance_qty=1)
        assert result["T"] == 1.0
        assert result.get("balance_mismatch") is None

    def test_미반영_등록주문은_워터마크_이전이어도_복구(self):
        """등록된 봇 주문이 미반영(processed=0)이면 워터마크 이전이어도 복구합니다.

        2026-10-24 수정: 금액 미확정으로 보류된 체결이 워터마크 뒤로 밀려
        영구 누락되던 문제를 막기 위해, **등록 주문**의 미반영 체결은
        지연 체결 경로에서 1회 반영합니다. (미등록 수동 이력은 계속 무시)
        """
        state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
        register_order_meta_in_state(state, "ORD012", {
            "side": "BUY", "total_qty": 3, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
            "processed_filled_amount": 0.0, "fill_marker_version": 1,
        })
        orders = [_make_buy_order("ORD012", "20260526", qty=3,
                                  utc_dt="2026-05-26T10:00:00+00:00")]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 2.0, f"T={result['T']} (expected 2.0)"
        assert result["orders_meta"]["ORD012"]["processed_filled_qty"] == 3

    def test_odno_int_타입_메타_t1_정상반영(self):
        """int odno로 API 응답이 와도 str() 변환으로 orders_meta 조회 성공 → T=1.0"""
        state = _make_state(T=0.0, last_updated="2026-06-15T00:00:00+00:00")
        register_order_meta_in_state(state, "36267", {
            "side": "BUY", "total_qty": 1, "t_target": 1.0,
            "is_additional": False, "processed_filled_qty": 0,
        })
        # odno를 int 36267로 전달 (KIS API가 int로 반환하는 상황 시뮬레이션)
        orders = [_make_buy_order(36267, "20260616", qty=1)]
        result = update_T_from_history("SOXL", state, orders)
        assert result["T"] == 1.0, f"T={result['T']} (expected 1.0, not 0.5)"

    def test_전량매도_완료_사이클_플래그_설정(self):
        """일반 모드: 전량매도 시 _completed_cycle_start 플래그가 설정되어야 함"""
        state = _make_state(T=4.0, last_updated="2026-06-02T00:00:00+00:00")
        state["cycle_start_date"] = "2026-06-03"
        # 06-01 매수 8주 + 06-14 전량매도 8주
        orders = [
            _make_buy_order("ORD1", "20260601", qty=8, utc_dt="2026-06-01T10:00:00+00:00"),
            _make_sell_order("ORD2", "20260614", qty=8, utc_dt="2026-06-14T15:00:00+00:00"),
        ]
        result = update_T_from_history("SOXL", state, orders)
        assert result.get("_completed_cycle_start") == "2026-06-03", f"got {result.get('_completed_cycle_start')}"
        assert result["T"] == 0.0, f"T={result['T']} (expected 0.0)"


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

    def test_미등록매수_폴백_qty1_initial_mode(self):
        """초기 모드 미등록매수: qty=1 첫 매수(avg_price=0) → 정상매수 → T+=0.5"""
        state = _make_state(T=0.0, last_updated="")
        orders = [_make_buy_order("ORD103", "20260528", qty=1)]
        result = update_T_from_history("TQQQ", state, orders)
        assert result["T"] == 0.5, f"T={result['T']} (expected 0.5)"

    def test_미등록매수_폴백_qty2_초기모드_plus05(self):
        """초기 모드 미등록매수: qty=2 단일 매수 → +0.5"""
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

    def test_전량매도_초기모드_사이클_플래그(self):
        """초기 모드: _infer_T_from_full_history가 전량매도 시 _completed_cycle_start 설정"""
        state = _make_state(T=0.0, last_updated="")
        # qty>1 매수로 첫 매수(T += 0.5) 후 전량매도
        orders = [
            _make_buy_order("ORD1", "20260604", qty=3, utc_dt="2026-06-04T10:00:00+00:00"),
            _make_sell_order("ORD2", "20260614", qty=3, utc_dt="2026-06-14T15:00:00+00:00"),
        ]
        result = update_T_from_history("SOXL", state, orders)
        assert result.get("_completed_cycle_start") == "2026-06-04", f"got {result.get('_completed_cycle_start')}"
        assert result["T"] == 0.0, f"T={result['T']} (expected 0.0)"


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


# ─────────────────────────────────────────────────────────
# 테스트: 체결 없는 정상 상태의 경고 억제
# ─────────────────────────────────────────────────────────

def _make_unfilled_order(odno, ord_dt, qty, utc_dt=None):
    """체결 없는(미체결) 주문 레코드 생성."""
    if utc_dt is None:
        utc_dt = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T10:00:00+00:00"
    return {
        "odno": odno,
        "ord_dt": ord_dt,
        "sll_buy_dvsn_cd_name": "매수",
        "ft_ccld_qty": "0",
        "ord_datetime_utc": utc_dt,
    }


def test_bot_orders_visible_no_warning(capsys):
    """봇 주문이 이력에 보이면 정상 미체결 상태 → 경고 없이 안내만."""
    state = _make_state(
        T=12.375,
        last_updated="2026-10-19 08:06:49+00:00",
        orders_meta={"OD1": {"side": "BUY", "total_qty": 7, "t_target": 1.0,
                           "processed_filled_qty": 0}},
    )
    orders = [_make_unfilled_order("OD1", "20261020", 7)]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=74)

    assert result["T"] == 12.375
    out = capsys.readouterr().out
    assert "[경고]" not in out
    assert "아직 체결 안 됨" in out


def test_bot_orders_missing_keeps_warning(capsys):
    """봇 주문이 이력에 없으면 조회 누락 가능성이 있으므로 경고를 유지."""
    state = _make_state(
        T=12.375,
        last_updated="2026-10-19 08:06:49+00:00",
        orders_meta={"OD1": {"side": "BUY", "total_qty": 7, "t_target": 1.0}},
    )
    orders = [_make_unfilled_order("OTHER", "20261020", 7)]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=74)

    assert result["T"] == 12.375
    out = capsys.readouterr().out
    assert "[경고]" in out
    assert "이력이 없는데 보유 중" in out


def test_empty_history_keeps_warning(capsys):
    """이력이 비면 조회 누락 → 경고 유지."""
    state = _make_state(
        T=12.375,
        last_updated="2026-10-19 08:06:49+00:00",
        orders_meta={"OD1": {"side": "BUY", "total_qty": 7, "t_target": 1.0}},
    )

    result = update_T_from_history("TQQQ", state, [], balance_qty=74)

    assert result["T"] == 12.375
    out = capsys.readouterr().out
    assert "[경고]" in out


# ─────────────────────────────────────────────────────────
# 테스트: 매도 비율 판정의 보유수량 기준 (2026-10-21 실측 버그)
# ─────────────────────────────────────────────────────────

def test_sell_ratio_uses_broker_balance_not_windowed_history():
    """이력 구간이 보유를 못 담으면 쿼터매도가 목표매도로 오분류됩니다.

    2026-10-21 실측: 실제 81주 보유 중 20주(24.7%) 매도 → 쿼터매도(T×0.75).
    이력 계산값이 30주라 비율 0.67 → 목표매도(T×0.25)로 잘못 분류됐습니다.
    브로커 잔고(61주)를 기준선으로 보정하면 비율 20/81 = 0.25 → 쿼터매도가 됩니다.
    """
    state = _make_state(
        T=13.375,
        last_updated="2026-10-20 11:14:03+00:00",
    )
    # 사이클 이전 매도/매수가 이력 구간 계산에 섞여 순보유가 과소 계산되는 상황
    orders = [
        _make_sell_order("OLD1", "20260922", 23),
        _make_sell_order("OLD2", "20260918", 12),
        _make_buy_order("OLD3", "20260924", 2),
        _make_sell_order("NEWSELL", "20261021", 20),
    ]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=61)

    # 13.375 × 0.75 = 10.0312 (쿼터매도) — 목표매도(×0.25 = 3.3438) 가 아님
    assert abs(result["T"] - 10.0312) < 0.001, f"T={result['T']}"


def test_sell_ratio_without_balance_falls_back_to_history():
    """잔고 조회 실패(None)면 이력 계산값으로 폴백하고 예외가 없어야 합니다."""
    state = _make_state(
        T=13.375,
        last_updated="2026-10-20 11:14:03+00:00",
    )
    orders = [_make_sell_order("NEWSELL", "20261021", 20)]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=None)

    assert result["T"] is not None


def test_full_sell_detected_with_balance_anchor():
    """보유 전량 매도 시 사이클 종료(T=0)로 판정되어야 합니다."""
    state = _make_state(
        T=13.375,
        last_updated="2026-10-20 11:14:03+00:00",
    )
    orders = [_make_sell_order("FULLSELL", "20261021", 61)]

    result = update_T_from_history("TQQQ", state, orders, balance_qty=0)

    assert result["T"] == 0.0


# ─────────────────────────────────────────────────────────
# 테스트: 보유수량 기준 미확보 → 보류 사유 기록 (A2)
# ─────────────────────────────────────────────────────────

def test_기준선_미확보_사유가_state에_기록된다():
    """잔고·저장 보유수량이 모두 없으면 두 사유 토큰이 조합되어 기록됩니다."""
    state = _make_state(T=10.0, last_updated="2026-10-20 11:14:03+00:00")
    orders = [_make_sell_order("S", "20261021", qty=20,
                               utc_dt="2026-10-21T09:30:16+00:00")]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=None)
    assert result.get("_position_basis_unknown") is True
    reason = result.get("_position_basis_unknown_reason") or ""
    assert "잔고조회실패" in reason
    assert "저장보유수량없음" in reason


def test_저장_보유수량만_있으면_보류사유가_없다():
    """state.position_qty 로 기준선을 잡으면 보류 플래그도 사유도 남지 않습니다."""
    state = _make_state(T=10.0, last_updated="2026-10-20 11:14:03+00:00")
    state["position_qty"] = 100
    orders = [_make_sell_order("S", "20261021", qty=20,
                               utc_dt="2026-10-21T09:30:16+00:00")]
    result = update_T_from_history("TQQQ", state, orders, balance_qty=None)
    assert result.get("_position_basis_unknown") is not True
    assert "_position_basis_unknown_reason" not in result


def test_기존_0마커_워터마크_이전_주문은_버전표식_없이_복구하지_않고_건너뛴다():
    """fill_marker_version 이 없는 기존 0 마커는 미반영의 증거가 아니므로 반영하지 않고 건너뜁니다."""
    state = _make_state(T=1.0, last_updated="2026-05-27 00:00:00")
    register_order_meta_in_state(state, "ORD013", {
        "side": "BUY", "total_qty": 3, "t_target": 1.0,
        "is_additional": False, "processed_filled_qty": 0,
    })
    orders = [_make_buy_order("ORD013", "20260526", qty=3,
                              utc_dt="2026-05-26T10:00:00+00:00")]
    result = update_T_from_history("TQQQ", state, orders)
    assert result["T"] == 1.0
    assert result.get("_settlement_hold") is not True
    assert "ORD013" in (result.get("_unverified_skipped_odnos") or [])
