"""정산 기준선(사이클 경계) 회귀 테스트 — C1/C2.

배경
----
매도 비율 분류(쿼터/목표/전량)의 **분모(기준선)** 가 잘못된 시점의 보유수량이면
T 가 왜곡됩니다. 2026-10-22 사고: 실제 81주를 30주로 계산 → 20주 매도가
쿼터매도(×0.75)가 아니라 목표매도(×0.25)로 오분류 → T 13.375 → 3.3438.

검증 항목
---------
(a) 과거 체결 72건(이미 반영 마커 있음) 재실행 → 델타 0 · T/순투입 불변
(b) 신규 매수 추가 → 정상 반영 (T 증가 · 순투입 증가)
(c) 마커 없는 과거 매도 → 자동 반영 없이 보류 (스냅샷 복구, T 불변)
(d) 이전 사이클 매도를 현재 잔고·position_qty 로 분류 시도 → 보류 (C1 가드)
(e) 같은 사이클 매도는 기존대로 분류 (정상 케이스 통과 — 과차단 방지)

브로커별 이력 행 형식 차이는 어댑터 실측 근거가 있는 3곳만 mock 으로 재현합니다.
  - kis   : ord_dt = 미국(ET) 영업일, ord_tmd = KST 시각 (숫자 주문번호)
  - toss  : ord_dt = KST 날짜 (`_ord_dt_is_kst_date=True`)
  - meritz: ord_dt = KST 날짜 + 합성 주문번호 `YYYYMMDD:제로패딩`

⚠️ 네트워크/실 API 를 쓰지 않습니다. 주문 이력은 dict 로만 구성합니다.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from state import update_T_from_history  # noqa: E402

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"

BROKERS = ("kis", "toss", "meritz")
SYMBOLS = ("TQQQ", "SOXL")
COMBOS = [(b, s) for b in BROKERS for s in SYMBOLS]

_KST = ZoneInfo("Asia/Seoul")
_UTC = ZoneInfo("UTC")


# ─────────────────────────────────────────────────────────
# 브로커별 주문 이력 행 (adapter 정규화 결과와 같은 필드)
# ─────────────────────────────────────────────────────────

def _odno_for(broker, day, seq):
    """브로커별 주문번호 형식."""
    if broker == "kis":
        # KIS 는 leading zero 없는 trimmed 숫자 주문번호
        return str(seq)
    if broker == "toss":
        return f"toss-{seq}"
    # MERITZ: 합성 주문번호 `YYYYMMDD:제로패딩주문번호`
    return f"{day}:{str(seq).zfill(7)}"


def _row(broker, seq, side, qty, price, day, tmd="043000"):
    """브로커 이력 행 1건.

    Args:
        day: 브로커 `ord_dt` 컨벤션의 날짜(YYYYMMDD).
             kis=미국영업일, toss/meritz=KST 일자.
        tmd: KST 시각(HHMMSS) — 세 브로커 모두 ord_tmd 는 KST 입니다.
    """
    kst = datetime.strptime(day + tmd, "%Y%m%d%H%M%S").replace(tzinfo=_KST)
    row = {
        "ord_dt": day,
        "ord_tmd": tmd,
        "ord_datetime_kst": kst.isoformat(),
        "ord_datetime_utc": kst.astimezone(_UTC).isoformat(),
        "prdt_name": "TQQQ" if broker == "kis" else "",
        "sll_buy_dvsn_cd_name": side,
        "ft_ord_qty": str(qty),
        "ft_ccld_qty": str(qty),
        "ft_ccld_unpr3": str(price),
        "ft_ccld_amt3": str(round(qty * price, 2)),
        "nccs_qty": "0",
        "prcs_stat_name": "체결",
        "tr_mket_name": "",
        "tr_crcy_cd": "USD",
        "odno": _odno_for(broker, day, seq),
        "ovrs_excg_cd": "NASD" if broker == "kis" else "",
    }
    if broker in ("toss", "meritz"):
        row["_ord_dt_is_kst_date"] = True
    return row


def _meta(side, qty, t_target=1.0, is_additional=False, processed=True, price=10.0):
    """orders_meta 항목.

    - ``processed=True``  : 이미 반영 완료 (마커 = 누적 체결)
    - ``processed=False`` : 등록 직후 미반영 (마커 0 — 유효한 마커)

    마커를 **누락**시키는 경우(레거시 메타)는 ``processed=False`` 와 다릅니다.
    마커 누락은 자동 반영 없이 보류 대상이므로 테스트에서 직접 dict 로 만듭니다.
    """
    meta = {
        "side": "BUY" if side == "매수" else "SELL",
        "total_qty": qty,
        "t_target": t_target,
        "is_additional": is_additional,
    }
    if processed:
        meta["processed_filled_qty"] = qty
        meta["processed_filled_amount"] = round(qty * price, 2)
    else:
        meta["processed_filled_qty"] = 0
        meta["processed_filled_amount"] = 0.0
    return meta


def _state(T=10.0, net=1000.0, cycle_start="2026-10-01", **kw):
    st = {
        "T": T,
        "net_invested": net,
        "last_updated": "2026-10-14T00:00:00+00:00",
        "last_processed_ordno": "",
        "orders_meta": {},
        "cycle_start_date": cycle_start,
        "additional_loc_odno": [],
        "balance_mismatch": {},
        "net_invested_status": "valid",
        "sell_t_effects": {},
        "position_qty": 480,
    }
    st.update(kw)
    return st


def _reflected_history(broker, count=72):
    """이미 반영 마커가 있는 과거 체결 72건 (매수 60 · 매도 12).

    매도 12건은 20주 중 5주씩(누적 60/480 → 쿼터) 처럼 보이도록 단순화했습니다.
    핵심은 **전부 반영 완료 마커** 라는 점입니다 — 재실행 시 델타가 0 이어야 합니다.
    """
    days = ["20261015", "20261016", "20261017", "20261018", "20261019", "20261020"]
    rows, metas = [], {}
    for i in range(count):
        side = "매수" if i < 60 else "매도"
        day = days[i % len(days)]
        tmd = f"{9 + (i // len(days)):02d}{(i % 60) * 60 % 60:02d}30"
        row = _row(broker, 1000 + i, side, 10, 10.0, day, tmd)
        rows.append(row)
        metas[row["odno"]] = _meta(side, 10, price=10.0, processed=True)
    return rows, metas


# ═══════════════════════════════════════════════════════════════════
# (a) 과거 체결 72건(마커 있음) 재실행 → 델타 0 · T/순투입 불변
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_reexecution_of_72_reflected_fills_is_noop(broker, symbol):
    rows, metas = _reflected_history(broker)
    state = _state(orders_meta=metas)

    result = update_T_from_history(symbol, state, rows, balance_qty=480)

    assert result["T"] == pytest.approx(10.0), f"{broker}/{symbol} T={result['T']}"
    assert result["net_invested"] == pytest.approx(1000.0)
    # 반영 내역이 비어 있어야 합니다 (재반영 차단).
    assert result["_settlement_deltas"] == [], result["_settlement_deltas"][:3]
    # 이미 반영된 매도는 재분류 대상이 아닙니다 → 기준선 보류 신호 금지.
    assert result.get("_position_basis_unknown") is not True
    assert result.get("_settlement_hold") is not True
    assert result.get("_pending_marker_odnos") in (None, [])


# ═══════════════════════════════════════════════════════════════════
# (b) 신규 매수 추가 → 정상 반영 (T 증가 · 순투입 증가)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_new_buy_after_72_reflected_fills_is_applied(broker, symbol):
    rows, metas = _reflected_history(broker)
    new_row = _row(broker, 2000, "매수", 10, 50.0, "20261022", "043000")
    rows.append(new_row)
    metas[new_row["odno"]] = _meta("매수", 10, t_target=1.0, price=50.0, processed=False)
    state = _state(orders_meta=metas)

    result = update_T_from_history(symbol, state, rows, balance_qty=490)

    assert result["T"] == pytest.approx(11.0), f"{broker}/{symbol} T={result['T']}"
    assert result["net_invested"] == pytest.approx(1500.0)
    # 새 매수 1건만 반영되어야 합니다 (과거 72건은 델타 0).
    applied = [d for d in result["_settlement_deltas"] if d.get("qty_delta")]
    assert len(applied) == 1, applied
    assert applied[0]["odno"] == new_row["odno"]
    assert applied[0]["qty_delta"] == 10
    assert result.get("_position_basis_unknown") is not True


# ═══════════════════════════════════════════════════════════════════
# (c) 마커 없는 과거 매도 → 자동 반영 없이 보류
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_markerless_past_sell_is_held_without_applying(broker, symbol):
    """워터마크 이전 매도인데 processed_filled_qty 마커가 없으면 반영하지 않고 보류합니다."""
    day = "20261015"
    sell_row = _row(broker, 3000, "매도", 20, 70.0, day, "043000")
    # 마커가 없는 레거시 메타 (side/total_qty 만 존재)
    meta = {"side": "SELL", "total_qty": 20, "t_target": 0.0}
    state = _state(
        orders_meta={sell_row["odno"]: meta},
        # 매도 이후 시점이 워터마크 (지연 체결 후보 경로)
        last_updated="2026-10-21T00:00:00+00:00",
        position_qty=480,
    )

    result = update_T_from_history(symbol, state, [sell_row], balance_qty=480)

    # 스냅샷 복구 — T/순투입/매도 효과가 하나도 확정되지 않습니다.
    assert result["T"] == pytest.approx(10.0), f"{broker}/{symbol} T={result['T']}"
    assert result["net_invested"] == pytest.approx(1000.0)
    assert result.get("_settlement_hold") is True
    assert sell_row["odno"] in (result.get("_pending_marker_odnos") or [])
    assert sell_row["odno"] in (result.get("_pending_amount_odnos") or [])
    # 매도 효과가 기록되지 않았습니다 (분류 자체가 확정되지 않음)
    assert result["sell_t_effects"] == {}


# ═══════════════════════════════════════════════════════════════════
# (d) 이전 사이클 매도를 현재 잔고/position_qty 로 분류 시도 → 보류 (C1)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
@pytest.mark.parametrize("basis_source", ["position_qty", "balance"])
def test_previous_cycle_sell_is_held_not_reclassified(broker, symbol, basis_source):
    """사이클 시작(2026-10-20) 이전 매도(10-15)는 현재 보유 100주 기준으로 재분류하지 않습니다.

    잘못 분류되면 20/100 = 0.20 → 쿼터매도(×0.75) → T 7.5 로 확정됩니다.
    가드가 동작하면 T 는 10.0 그대로이고 저장·신규 주문이 보류됩니다.
    """
    sell_row = _row(broker, 4000, "매도", 20, 70.0, "20261015", "043000")
    state = _state(
        cycle_start="2026-10-20",
        last_updated="2026-10-14T00:00:00+00:00",
        orders_meta={sell_row["odno"]: _meta("매도", 20, t_target=0.0, price=70.0, processed=False)},
        sell_t_effects={},  # 이전 사이클 종료로 매도 효과 기록이 비었습니다
    )
    if basis_source == "position_qty":
        # 워터마크 시점 보유량 (매도 전 100주)
        state["position_qty"] = 100
        balance_qty = None
    else:
        # 현재 잔고는 매도 후 80주 → 기준선 = 80 - (-20) = 100주
        state["position_qty"] = None
        balance_qty = 80

    result = update_T_from_history(symbol, state, [sell_row], balance_qty=balance_qty)

    # ① T 가 재분류로 바뀌지 않았습니다 (7.5 가 되면 회귀).
    assert result["T"] == pytest.approx(10.0), f"{broker}/{symbol} T={result['T']}"
    # ② 저장·신규 주문 보류 신호 + 사유 토큰
    assert result.get("_position_basis_unknown") is True
    assert "이전사이클매도" in (result.get("_position_basis_unknown_reason") or "")
    # ③ 순투입금도 차감되지 않았습니다 (매도 반영 자체를 보류)
    assert result["net_invested"] == pytest.approx(1000.0)
    # ④ 반영 마커가 진행되지 않았습니다 → 다음 실행에서 재처리 가능
    assert result["orders_meta"][sell_row["odno"]].get("processed_filled_qty") == 0
    assert result["sell_t_effects"] == {}
    assert result["_settlement_deltas"] == []


@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_previous_cycle_sell_guard_uses_recorded_basis_when_present(broker, symbol):
    """이미 기록된 기준선(sell_t_effects.basis)이 있으면 사이클 경계 가드는 발동하지 않습니다.

    기록된 기준선으로 누적 비율을 판정하므로 현재 잔고에 의존하지 않습니다.
    """
    sell_row = _row(broker, 4100, "매도", 20, 70.0, "20261015", "043000")
    state = _state(
        cycle_start="2026-10-20",
        last_updated="2026-10-14T00:00:00+00:00",
        orders_meta={sell_row["odno"]: _meta("매도", 20, t_target=0.0, price=70.0, processed=False)},
        # 기록된 기준선 80주 → 20/80 = 0.25 → 쿼터매도(×0.75)
        sell_t_effects={sell_row["odno"]: {"basis": 80, "factor": 0.75}},
    )

    result = update_T_from_history(symbol, state, [sell_row], balance_qty=80)

    assert result.get("_position_basis_unknown") is not True
    assert result.get("_settlement_hold") is not True
    # 이미 반영된 같은 분류(0.75)이므로 T 를 다시 곱하지 않습니다.
    assert result["T"] == pytest.approx(10.0), f"{broker}/{symbol} T={result['T']}"


# ═══════════════════════════════════════════════════════════════════
# (e) 같은 사이클 매도는 기존대로 분류 (정상 케이스 통과 · 과차단 방지)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_same_cycle_sell_is_still_classified(broker, symbol):
    """사이클 시작 이후 매도는 기존대로 기준선 비율로 분류됩니다 (쿼터 → ×0.75)."""
    sell_row = _row(broker, 5000, "매도", 20, 70.0, "20261021", "043000")
    state = _state(
        cycle_start="2026-10-20",
        last_updated="2026-10-20T00:00:00+00:00",
        orders_meta={sell_row["odno"]: _meta("매도", 20, t_target=0.0, price=70.0, processed=False)},
        sell_t_effects={},
        position_qty=100,
        net=5000.0,
    )

    # 현재 잔고 80주(매도 후) → 기준선 100주
    result = update_T_from_history(symbol, state, [sell_row], balance_qty=80)

    assert result.get("_position_basis_unknown") is not True
    # 20/100 = 0.20 → 쿼터매도(×0.75)
    assert result["T"] == pytest.approx(7.5), f"{broker}/{symbol} T={result['T']}"
    assert result["net_invested"] == pytest.approx(5000.0 - 1400.0)
    assert result["sell_t_effects"][sell_row["odno"]]["basis"] == 100


# ═══════════════════════════════════════════════════════════════════
# (f) 사이클 경계를 알 수 없으면 과차단하지 않습니다 (보수적 기본값)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_unknown_cycle_start_keeps_legacy_classification(broker, symbol):
    """cycle_start_date 가 비어 있는 레거시 상태는 기존 분류를 유지합니다."""
    sell_row = _row(broker, 6000, "매도", 20, 70.0, "20261015", "043000")
    state = _state(
        cycle_start="",
        last_updated="2026-10-14T00:00:00+00:00",
        orders_meta={sell_row["odno"]: _meta("매도", 20, t_target=0.0, price=70.0, processed=False)},
        sell_t_effects={},
        position_qty=100,
    )

    result = update_T_from_history(symbol, state, [sell_row], balance_qty=80)

    assert result.get("_position_basis_unknown") is not True
    assert result["T"] == pytest.approx(7.5), f"{broker}/{symbol} T={result['T']}"


# ═══════════════════════════════════════════════════════════════════
# (g) 10/22~23 신규 매수 16건 보존 + 과거 72건 재반영 차단 동시 검증
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_sixteen_new_buys_applied_while_72_past_stay_blocked(broker, symbol):
    rows, metas = _reflected_history(broker)
    new_odnos = []
    for i in range(16):
        day = "20261022" if i < 8 else "20261023"
        new_row = _row(broker, 2100 + i, "매수", 10, 50.0, day, f"{9 + i % 8:02d}0000")
        rows.append(new_row)
        metas[new_row["odno"]] = _meta("매수", 10, t_target=1.0, price=50.0, processed=False)
        new_odnos.append(new_row["odno"])
    state = _state(orders_meta=metas)

    result = update_T_from_history(symbol, state, rows, balance_qty=640)

    assert result["T"] == pytest.approx(26.0), f"{broker}/{symbol} T={result['T']}"
    assert result["net_invested"] == pytest.approx(1000.0 + 16 * 500.0)
    applied = {d["odno"] for d in result["_settlement_deltas"] if d.get("qty_delta")}
    assert applied == set(new_odnos), applied
    assert result.get("_position_basis_unknown") is not True
    assert result.get("_settlement_hold") is not True


# ═══════════════════════════════════════════════════════════════════
# (h) 워터마크 이전 주문의 0 마커: 버전 표식이 있는 신규 등록분만 복구 반영
# ═══════════════════════════════════════════════════════════════════

def _old_buy_state(broker, meta_extra):
    old_row = _row(broker, 7100, "매수", 10, 10.0, "20261015", "043000")
    meta = {"side": "BUY", "total_qty": 10, "t_target": 1.0, "processed_filled_qty": 0}
    meta.update(meta_extra)
    state = _state(
        orders_meta={old_row["odno"]: meta},
        last_updated="2026-10-21T00:00:00+00:00",
    )
    return old_row, state


@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_legacy_zero_marker_old_order_is_skipped_not_reapplied(broker, symbol):
    """기존 state 의 processed_filled_qty=0 (버전 표식 없음)은 재정산하지 않습니다."""
    old_row, state = _old_buy_state(broker, {})

    result = update_T_from_history(symbol, state, [old_row], balance_qty=490)

    # 보류가 아니라 건너뜀 — 같은 실행의 신규 체결이 막히지 않아야 합니다.
    assert result["T"] == pytest.approx(10.0)
    assert result["net_invested"] == pytest.approx(1000.0)
    assert result.get("_settlement_hold") is not True
    assert result["_settlement_deltas"] == []
    assert old_row["odno"] in (result.get("_unverified_skipped_odnos") or [])


@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_legacy_zero_marker_with_amount_but_no_version_is_still_skipped(broker, symbol):
    """금액 0.0 만으로는 부족합니다 — 과거 데이터에도 같은 값이 존재할 수 있습니다."""
    old_row, state = _old_buy_state(broker, {"processed_filled_amount": 0.0})

    result = update_T_from_history(symbol, state, [old_row], balance_qty=490)

    assert result["T"] == pytest.approx(10.0)
    assert result.get("_settlement_hold") is not True
    assert old_row["odno"] in (result.get("_unverified_skipped_odnos") or [])


@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_versioned_zero_marker_old_order_is_recovered(broker, symbol):
    old_row, state = _old_buy_state(
        broker, {"processed_filled_amount": 0.0, "fill_marker_version": 1}
    )

    result = update_T_from_history(symbol, state, [old_row], balance_qty=490)

    assert result["T"] == pytest.approx(11.0)
    assert result.get("_settlement_hold") is not True


# ═══════════════════════════════════════════════════════════════════
# (i) 신규 등록 메타 형식 — trading_bot 이 쓰는 키와 state 판정이 일치해야 합니다
# ═══════════════════════════════════════════════════════════════════

def test_trading_bot_registers_versioned_markers():
    text = (Path(__file__).resolve().parents[1] / "trading_bot.py").read_text(encoding="utf-8")
    assert '"processed_filled_amount": 0.0,' in text
    assert '"fill_marker_version": FILL_MARKER_VERSION,' in text
    from state import FILL_MARKER_VERSION, _marker_proves_unapplied
    assert _marker_proves_unapplied({
        "processed_filled_qty": 0, "processed_filled_amount": 0.0,
        "fill_marker_version": FILL_MARKER_VERSION,
    })


@pytest.mark.parametrize("broker,symbol", COMBOS)
def test_legacy_zero_marker_old_order_does_not_block_new_buys(broker, symbol):
    """증명 불가한 과거 주문은 건너뛰되, 같은 실행의 신규 매수는 정상 정산됩니다."""
    old_row, state = _old_buy_state(broker, {})
    new_row = _row(broker, 7200, "매수", 10, 50.0, "20261022", "043000")
    state["orders_meta"][new_row["odno"]] = _meta("매수", 10, t_target=1.0, price=50.0, processed=False)

    result = update_T_from_history(symbol, state, [old_row, new_row], balance_qty=500)

    assert result["T"] == pytest.approx(11.0)
    assert result["net_invested"] == pytest.approx(1500.0)
    assert result.get("_settlement_hold") is not True
    applied = {d["odno"] for d in result["_settlement_deltas"] if d.get("qty_delta")}
    assert applied == {new_row["odno"]}
