"""
cycle_start_date 일회성 복구 모드(STATE_SET_CYCLE_START_ONLY) 테스트.

배경: 수동 주입(REPAIR CAS) 후 state의 cycle_start_date가 비어 있으면
generate_cycle_report가 전체 이력을 집계해 B안 시드(next_cycle_seed)가 틀어짐.
→ 진단(state_hash) 기준 CAS로 cycle_start_date만 안전하게 1회 설정한다.
"""
import os
import sys
from copy import deepcopy
from unittest.mock import patch

import pytest

# repo 루트 + src 경로 추가
_repo_root = os.path.join(os.path.dirname(__file__), "..")
_src_path = os.path.join(_repo_root, "src")
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from trading_bot import (
    _set_cycle_start_only,
    _REPAIR_ACTIONS,
    _REPAIR_LEGACY_MAP,
)
from state import canonical_state_hash


def _make_tqqq_state(**overrides):
    """수동 주입된 TQQQ 상태를 재현합니다 (cycle_start_date 미설정)."""
    state = {
        "T": 9.375,
        "last_updated": "2026-10-14T08:09:04+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "net_invested": 4126.91,
        "net_invested_status": "valid",
        "last_processed_ordno": "20261014:0001650552",
        "additional_loc_odno": [],
        "orders_meta": {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [73.1],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
    }
    state.update(overrides)
    return state


class TestSetCycleStartOnly:
    def _set_fingerprint(self, monkeypatch, state, date="2026-09-23", t="9.375", current=""):
        monkeypatch.setenv("STATE_SET_CYCLE_START_SYMBOL", "TQQQ")
        monkeypatch.setenv("STATE_SET_CYCLE_START_DATE", date)
        monkeypatch.setenv("STATE_SET_CYCLE_START_EXPECT_HASH", canonical_state_hash(state))
        monkeypatch.setenv("STATE_SET_CYCLE_START_EXPECT_T", t)
        monkeypatch.setenv("STATE_SET_CYCLE_START_EXPECT_CURRENT", current)

    def test_env_누락_시_중단(self, monkeypatch):
        monkeypatch.delenv("STATE_SET_CYCLE_START_SYMBOL", raising=False)
        with pytest.raises(RuntimeError, match="fingerprint 환경변수가 없습니다"):
            _set_cycle_start_only()

    def test_날짜_형식_오류_시_중단(self, monkeypatch):
        state = _make_tqqq_state()
        self._set_fingerprint(monkeypatch, state, date="2026/09/23")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="YYYY-MM-DD"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_hash_불일치_시_중단(self, monkeypatch):
        state = _make_tqqq_state()
        self._set_fingerprint(monkeypatch, state)
        monkeypatch.setenv("STATE_SET_CYCLE_START_EXPECT_HASH", "deadbeef")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="hash 불일치"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_T_불일치_시_중단(self, monkeypatch):
        state = _make_tqqq_state()
        self._set_fingerprint(monkeypatch, state, t="1.0")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="T 불일치"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_현재값_불일치_시_중단(self, monkeypatch):
        state = _make_tqqq_state()
        self._set_fingerprint(monkeypatch, state, current="2026-01-01")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="현재값 불일치"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_미종결_리버스_주문_시_거부(self, monkeypatch):
        state = _make_tqqq_state(
            reverse_mode={"active": True, "cycle_id": "C1"},
            orders_meta={
                "X": {"reverse_action": "buy", "cycle_id": "C1", "terminal": False}
            },
        )
        self._set_fingerprint(monkeypatch, state)
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="미종결 리버스 주문"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_pending_fence_시_거부(self, monkeypatch):
        state = _make_tqqq_state(
            pending_order_intent={"side": "BUY", "submitted_session": "2026-10-13"}
        )
        self._set_fingerprint(monkeypatch, state)
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="fence"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_미래_날짜_거부(self, monkeypatch):
        state = _make_tqqq_state()  # last_updated=2026-10-14
        self._set_fingerprint(monkeypatch, state, date="2026-10-20")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            with pytest.raises(RuntimeError, match="미래"):
                _set_cycle_start_only()
        mock_save.assert_not_called()

    def test_정상_복구_성공(self, monkeypatch):
        state = _make_tqqq_state()
        expected_hash = canonical_state_hash(state)
        self._set_fingerprint(monkeypatch, state)
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            _set_cycle_start_only()
        assert state["cycle_start_date"] == "2026-09-23"
        # 다른 필드는 보존
        assert state["T"] == 9.375
        assert state["net_invested"] == 4126.91
        assert state["net_invested_status"] == "valid"
        assert state["last_processed_ordno"] == "20261014:0001650552"
        mock_save.assert_called_once()
        # cycle_start_date가 바뀌면 canonical hash도 바뀜 (저장 후 재계산 보장)
        assert canonical_state_hash(state) != expected_hash

    def test_기존값_덮어쓰기(self, monkeypatch):
        """이미 값이 있어도 현재값 fingerprint가 일치하면 정정할 수 있습니다."""
        state = _make_tqqq_state(cycle_start_date="2026-09-20")
        self._set_fingerprint(monkeypatch, state, date="2026-09-23", current="2026-09-20")
        with patch("trading_bot.load_state", return_value=state), \
             patch("trading_bot.save_state") as mock_save:
            _set_cycle_start_only()
        assert state["cycle_start_date"] == "2026-09-23"
        mock_save.assert_called_once()


class TestRepairActionRegistered:
    def test_REPAIR_ACTION_등록(self):
        assert "CYCLE_START_SET" in _REPAIR_ACTIONS
        assert _REPAIR_LEGACY_MAP["CYCLE_START_SET"] == "_set_cycle_start_only"


# ═══════════════════════════════════════════════════════════════════
# P0-1: 종료 사이클 후처리 분리 (finalize_completed_cycle)
# ═══════════════════════════════════════════════════════════════════
from trading_bot import (  # noqa: E402
    finalize_completed_cycle,
    generate_cycle_report,
)
from state import reset_settlement_ledger  # noqa: E402


def _settled_state():
    state = _make_tqqq_state(T=0.0, net_invested=0.0, cycle_start_date="")
    reset_settlement_ledger(state, 0.0)
    return state


def _fill(odno, side, qty, price, ord_dt, ord_tmd="090000"):
    return {
        "odno": odno,
        "sll_buy_dvsn_cd_name": side,
        "ft_ccld_qty": str(qty),
        "ft_ccld_unpr3": str(price),
        "ft_ccld_amt3": str(qty * price),
        "ord_dt": ord_dt,
        "ord_tmd": ord_tmd,
        "ord_datetime_utc": (
            f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}T"
            f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]}+00:00"
        ),
        "prcs_stat_name": "체결",
    }


def test_finalize_sell_only_report_and_seed():
    state = _settled_state()
    history = [
        _fill("B1", "매수", 10, 10.0, "20260901"),
        _fill("S1", "매도", 10, 12.0, "20260910"),
        _fill("B2", "매수", 5, 10.0, "20260911"),  # 구간 밖
    ]
    result = finalize_completed_cycle(
        "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
        report={"cycle_start_date": "2026-09-01"},
    )
    report = result["report"]
    assert report["total_buy_amount"] == pytest.approx(100.0)
    assert report["total_sell_amount"] == pytest.approx(120.0)
    assert report["net_profit"] == pytest.approx(20.0)
    assert report["cycle_start_date"] == "2026-09-01"
    assert report["cycle_end_date"] == "2026-09-10"
    assert result["next_cycle_seed"] == pytest.approx(1020.0)
    # 후처리는 T/net/원장을 건드리지 않는다
    assert state["T"] == 0.0
    assert state["net_invested"] == 0.0
    assert state["settlement_ledger"]["entries"] == []
    assert state["effective_seed"] == 0.0


def test_finalize_same_day_new_buy_excluded_from_closed_cycle():
    """청산 이벤트 이후 같은 날 재매수는 종료 사이클 리포트에서 제외합니다."""
    state = _settled_state()
    history = [
        _fill("B1", "매수", 10, 10.0, "20260901", "090000"),
        _fill("S1", "매도", 10, 12.0, "20260910", "100000"),
        _fill("B2", "매수", 2, 11.0, "20260910", "110000"),  # 청산 후 새 사이클
    ]
    result = finalize_completed_cycle(
        "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
        report={"cycle_start_date": "2026-09-01", "cycle_end_date": "2026-09-10"},
    )
    report = result["report"]
    assert report["buy_count"] == 1
    assert report["total_buy_amount"] == pytest.approx(100.0)
    assert report["total_sell_amount"] == pytest.approx(120.0)
    assert report["net_profit"] == pytest.approx(20.0)
    assert result["next_cycle_seed"] == pytest.approx(1020.0)
    assert report["liquidation_event"]["odno"] == "S1"
    assert report["cycle_end_date"] == "2026-09-10"


def test_finalize_multi_day_batch_closed_interval():
    state = _settled_state()
    history = [
        _fill("B1", "매수", 10, 10.0, "20260901"),
        _fill("B2", "매수", 10, 11.0, "20260905"),
        _fill("S1", "매도", 20, 12.0, "20260908"),
        _fill("S2", "매도", 1, 12.0, "20260909"),
        _fill("B3", "매수", 1, 9.0, "20260920"),  # 구간 밖
    ]
    result = finalize_completed_cycle(
        "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
        report={"cycle_start_date": "2026-09-01", "cycle_end_date": "2026-09-10"},
    )
    report = result["report"]
    # S1(20주)에서 청산 → 이후 S2/B3 는 종료 사이클 집계에서 제외
    assert report["buy_count"] == 2
    assert report["sell_count"] == 1
    assert report["total_buy_amount"] == pytest.approx(210.0)
    assert report["total_sell_amount"] == pytest.approx(240.0)
    assert report["net_profit"] == pytest.approx(30.0)
    assert report["liquidation_event"]["odno"] == "S1"


def test_finalize_dry_does_not_save_state(monkeypatch):
    state = _settled_state()
    history = [_fill("B1", "매수", 10, 10.0, "20260901"),
               _fill("S1", "매도", 10, 12.0, "20260910")]
    with patch("trading_bot.save_state") as mock_save:
        finalize_completed_cycle(
            "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
            report={"cycle_start_date": "2026-09-01"},
        )
    mock_save.assert_not_called()


def test_finalize_reinvest_off_returns_reference_seed_but_no_state_write():
    state = _settled_state()
    history = [_fill("B1", "매수", 10, 10.0, "20260901"),
               _fill("S1", "매도", 10, 12.0, "20260910")]
    result = finalize_completed_cycle(
        "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
        report={"cycle_start_date": "2026-09-01"},
    )
    # 복리 OFF 판단은 호출자 몫 — finalize 는 참고값만 반환하고 state 를 바꾸지 않는다.
    assert result["next_cycle_seed"] == pytest.approx(1020.0)
    assert state["effective_seed"] == 0.0


def test_finalize_rejects_non_settled_state():
    state = _make_tqqq_state(T=5.0, net_invested=100.0, cycle_start_date="2026-09-01")
    with pytest.raises(ValueError, match="T 가 0이 아닙니다"):
        finalize_completed_cycle(
            "TQQQ", state, [], seed=1000.0, commission_rate=0.0,
            report={"cycle_start_date": "2026-09-01"},
        )


def test_generate_cycle_report_does_not_depend_on_state():
    history = [
        _fill("B1", "매수", 10, 10.0, "20260901"),
        _fill("S1", "매도", 10, 12.0, "20260910"),
        _fill("B2", "매수", 5, 10.0, "20260911"),
    ]
    report = generate_cycle_report(
        "TQQQ", history, "2026-09-01", "2026-09-10", 1000.0, 0.0
    )
    assert report["buy_count"] == 1
    assert report["sell_count"] == 1


def test_finalize_unsettled_new_cycle_preserves_state():
    """같은 실행에서 시작된 새 사이클 상태(T/순투입/보유)는 보존하고 리포트만 만든다."""
    state = _make_tqqq_state(T=1.0, net_invested=200.0, cycle_start_date="2026-09-10")
    state["position_qty"] = 20
    state["settlement_ledger"] = {"start_T": 0.0, "next_seq": 2,
                                  "entries": [{"seq": 1, "kind": "buy",
                                               "odno": "N1", "delta": 1.0}]}
    before = deepcopy(state)
    history = [
        _fill("B1", "매수", 10, 10.0, "20260901", "090000"),
        _fill("S1", "매도", 10, 12.0, "20260910", "100000"),
        _fill("N1", "매수", 20, 10.0, "20260910", "110000"),  # 새 사이클 매수
    ]
    result = finalize_completed_cycle(
        "TQQQ", state, history, seed=1000.0, commission_rate=0.0,
        report={"cycle_start_date": "2026-09-01"}, require_settled=False,
    )
    report = result["report"]
    assert report["cycle_end_date"] == "2026-09-10"
    assert report["buy_count"] == 1
    assert report["net_profit"] == pytest.approx(20.0)
    assert result["next_cycle_seed"] == pytest.approx(1020.0)
    assert state["T"] == before["T"] == 1.0
    assert state["net_invested"] == before["net_invested"] == 200.0
    assert state["position_qty"] == before["position_qty"] == 20
    assert state["cycle_start_date"] == before["cycle_start_date"]
    assert state["settlement_ledger"] == before["settlement_ledger"]
