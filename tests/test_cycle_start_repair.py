"""
cycle_start_date 일회성 복구 모드(STATE_SET_CYCLE_START_ONLY) 테스트.

배경: 수동 주입(REPAIR CAS) 후 state의 cycle_start_date가 비어 있으면
generate_cycle_report가 전체 이력을 집계해 B안 시드(next_cycle_seed)가 틀어짐.
→ 진단(state_hash) 기준 CAS로 cycle_start_date만 안전하게 1회 설정한다.
"""
import os
import sys
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
