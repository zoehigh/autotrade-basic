"""시각(ord_tmd)이 없는 주문이력(NHPLUG GSB10030)에서도 리버스 주문을 찾는지 검증합니다.

실측(2026-11-06 NHPLUG SOXL): 리버스 1일차 매도(제출 KST 2026-11-06 05:22)가 이력에
ord_dt=20261105, ord_tmd="" 로 내려와 제출일 비교가 어긋남 → "리버스 주문 이력 누락" →
일반 쿼터매도(×0.75)로 오분류되어 T 17.325 가 14.4375 로 저장됐습니다 (verify-settlement 가 검출).
"""
from state import (
    _is_reverse_order,
    _order_matches_submitted_date,
    reconcile_reverse_fills,
)

SUBMITTED = "20261106052250"  # KST 제출 시각


def _nh_row(**kw):
    row = {
        "odno": "2617", "ord_dt": "20261105", "ord_tmd": "",
        "ft_ccld_qty": "4", "ft_ccld_unpr3": "44.88", "ft_ccld_amt3": "179.52",
        "nccs_qty": "0", "prcs_stat_name": "체결",
    }
    row.update(kw)
    return row


def _state(submitted=SUBMITTED, session="2026-11-05"):
    return {
        "T": 19.25,
        "reverse_mode": {"active": True, "day_count": 0, "cycle_id": "C1"},
        "orders_meta": {
            "2617": {
                "side": "SELL", "total_qty": 4, "processed_filled_qty": 0,
                "reverse_action": "sell", "reverse_day": 1, "reverse_base_t": 19.25,
                "reverse_t_factor": 0.9, "cycle_id": "C1",
                "submitted_at": submitted, "submitted_session": session,
            }
        },
    }


# ── 날짜 판정 헬퍼 ──────────────────────────────────────────────

def test_시각없는_행은_제출시각에서_계산한_미국세션과_정확히_일치할때만_인정():
    # KST 11/06 05:22 제출 = ET 11/05 15:22 → 세션 20261105 → ord_dt=20261105 만 인정
    assert _order_matches_submitted_date(_nh_row(), "20261106052250") is True
    # KST 11/05 23:00 제출 = ET 11/05 09:00 → 같은 세션 20261105
    assert _order_matches_submitted_date(_nh_row(), "20261105230000") is True
    # 다음 세션(ET 11/06)에서 제출한 주문의 번호가 같더라도 ord_dt=20261105 행은 다른 세션 → 거부
    assert _order_matches_submitted_date(_nh_row(), "20261107052250") is False
    assert _order_matches_submitted_date(_nh_row(), "20261104041000") is False


def test_연속_세션의_같은_주문번호가_섞이지_않는다():
    """Codex 리뷰 P2: ord_dt/ord_dt+1 을 모두 받아주면 연속 세션 행이 둘 다 후보가 된다."""
    state = _state()  # 제출: KST 11/06 05:22 (세션 11/05)
    other_session = _nh_row(ord_dt="20261106", ft_ccld_qty="99", ft_ccld_amt3="999.00")  # 다음 세션의 같은 번호
    mine = _nh_row()  # ord_dt=20261105, 4주
    reconcile_reverse_fills(state, [other_session, mine])
    assert state["orders_meta"]["2617"]["processed_filled_qty"] == 4
    assert state["T"] == 17.325


def test_기록된_submitted_session을_우선하고_없으면_제출시각에서_계산():
    row = _nh_row()
    assert _order_matches_submitted_date(row, "bad", "2026-11-05") is True
    assert _order_matches_submitted_date(row, "bad", "2026-11-06") is False
    assert _order_matches_submitted_date(row, "bad", "") is False
    # submitted_session 이 없는 오래된 메타: 제출 시각(KST 11/06 05:22 → ET 11/05)으로 계산
    assert _order_matches_submitted_date(row, "20261106052250", "") is True


def test_시각있는_행은_기존_판정_그대로():
    kis = {"ord_dt": "20260731", "ord_tmd": "041046"}  # 실제 KST 2026-08-01
    assert _order_matches_submitted_date(kis, "20260801041046") is True
    assert _order_matches_submitted_date(kis, "20260731041046") is False  # 시각이 있으면 완화하지 않음


def test_KST일자_브로커는_완화하지_않는다():
    toss = {"ord_dt": "20261024", "ord_tmd": "", "_ord_dt_is_kst_date": True}
    assert _order_matches_submitted_date(toss, "20261024010000") is True
    assert _order_matches_submitted_date(toss, "20261025010000") is False


def test_ord_dt_형식이_이상하면_거부():
    assert _order_matches_submitted_date({"ord_dt": "", "ord_tmd": ""}, "20261106052250") is False
    assert _order_matches_submitted_date({"ord_dt": "2026-11-05", "ord_tmd": ""}, "20261106052250") is False


# ── 리버스 정산 경로 ────────────────────────────────────────────

def test_NHPLUG_시각없는_이력에서도_리버스_주문으로_인식():
    assert _is_reverse_order(_state(), "2617", _nh_row()) is True


def test_NHPLUG_리버스_1일차_체결이_배율_0_9로_반영되고_이력누락_경고가_없다():
    state = _state()
    reconcile_reverse_fills(state, [_nh_row()])
    assert state["T"] == 17.325            # 19.25 × 0.9 (쿼터매도 ×0.75 → 14.4375 가 아님)
    assert state["reverse_mode"]["day_count"] == 1
    assert "reconciliation_error" not in state["reverse_mode"]
    assert state["reverse_mode"].get("reconciliation_only") is not True
    assert state["orders_meta"]["2617"]["processed_filled_qty"] == 4


def test_다른_날_제출이면_여전히_이력누락으로_보류():
    # 이력 행 날짜(ord_dt=11/05 → KST 11/05~11/06)와 맞지 않는 제출일(과거 세션)은 기존대로 누락 처리
    state = _state(submitted="20261103052250", session="2026-11-02")
    reconcile_reverse_fills(state, [_nh_row()])
    assert state["reverse_mode"]["reconciliation_error"] == "reverse_order_history_missing"
