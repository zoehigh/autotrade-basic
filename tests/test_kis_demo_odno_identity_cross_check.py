"""KIS 모의 주문번호 재사용 — 정산 처리기(PR #30)와 독립 검증기(PR #29)의 주문 인스턴스 식별 교차 검증.

실제 `pipeline.settle_positions()` 가 만든 정산 근거(Evidence)를 `ops/verify_run.py` 로 검증합니다.
처리기는 (번호 + 제출일/세션) 으로, 검증기는 (번호 + 방향 + 세션) 으로 **서로 독립적으로** 주문 인스턴스를 가립니다.
같은 입력에서 두 구현이 같은 결론(통과/보류)을 내야 하고, 처리기가 반영한 값을 검증기가 거부하거나
처리기가 막은 값을 검증기가 통과시키는 불일치가 없어야 합니다.
"""
import glob
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[1] / "ops"))
import verify_run  # noqa: E402
import pipeline  # noqa: E402
import trading_bot  # noqa: E402
from broker.base import ReadOnlyBroker  # noqa: E402

from test_pipeline_phases import _base_state, _make_broker, _symbol_config, io  # noqa: E402,F401
from test_kis_demo_odno_reuse_general_orders import (  # noqa: E402,F401
    N, _kis_demo, _meta, _new_row, _old_row,
)

@pytest.fixture(autouse=True)
def _kis_demo_env(monkeypatch):
    """정산 근거(Evidence)는 환경변수의 브로커 모드를 기록하므로, 실행 환경(.env/CI)과 무관하게 KIS 모의로 고정합니다."""
    monkeypatch.setenv("BROKER", "kis")
    monkeypatch.setenv("BROKER_MODE", "demo")


MID_WM = "2026-11-20T10:00:00+00:00"        # 과거 행(11/17 UTC)은 워터마크 이전, 새 행(11/23 UTC)만 이후
EARLY_WM = "2026-11-10T10:00:00+00:00"      # 두 행 모두 워터마크 이후


def _state(meta, last_updated=MID_WM, **kw):
    state = _base_state(
        T=5.0, net_invested=520.0, position_qty=15, last_updated=last_updated,
        cycle_start_date="2026-11-01", orders_meta={N: meta}, **kw,
    )
    return state


def _settle(io, state, history, qty=15):
    io["load"]("TQQQ", deepcopy(state))
    return pipeline.settle_positions(ReadOnlyBroker(_make_broker(balance_qty=qty, history=history)), _symbol_config())


def _verify(settled):
    path = sorted(glob.glob("settlement-TQQQ-*.json"))[-1]
    evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    ok, problems = verify_run.verify_evidence_file(
        path, broker=evidence["broker"], mode="LIVE", run_id=evidence.get("run_id"), broker_mode="demo",
    )
    return ok, problems, evidence


def test_구분_가능한_번호_재사용_처리기_반영을_검증기가_같은_값으로_통과시킨다(io):
    settled = _settle(io, _state(_meta(2, 20.0)), [_old_row(10, 50.0), _new_row(5, 10.0)])
    assert settled.halt_reason is None
    assert settled.state["orders_meta"][N]["processed_filled_qty"] == 5
    ok, problems, evidence = _verify(settled)
    assert ok, problems
    # 검증기가 본 적용 델타는 새 주문의 3주/$30 뿐 (과거 주문 10주/$500 이 섞이지 않음)
    deltas = [d for d in evidence.get("applied_orders", evidence.get("orders", [])) if str(d.get("odno")) == N]
    assert [(d.get("qty_delta"), round(float(d.get("amount_delta")), 2)) for d in deltas] == [(3, 30.0)]


def test_신규_주문의_지연_체결은_두_구현이_같은_델타로_합의한다(io):
    first = _settle(io, _state(_meta(2, 20.0)), [_old_row(10, 50.0), _new_row(4, 10.0)], qty=14)
    ok, problems, _ = _verify(first)
    assert ok, problems
    second = _settle(io, first.state, [_old_row(10, 50.0), _new_row(5, 10.0)], qty=15)
    assert second.halt_reason is None
    assert second.state["orders_meta"][N]["processed_filled_qty"] == 5
    ok, problems, _ = _verify(second)
    assert ok, problems


@pytest.mark.parametrize("old_first", [True, False])
def test_같은_번호_다른_날짜_체결이_한_묶음이면_처리기는_보류하고_상태를_바꾸지_않으며_일시_보류로_안내하지_않는다(io, old_first):
    state = _state(_meta(0, 0.0), last_updated=EARLY_WM)
    history = [_old_row(10, 50.0), _new_row(5, 10.0)]
    if not old_first:
        history.reverse()
    with pytest.raises(trading_bot.SymbolScopedError) as raised:
        _settle(io, state, history)
    message = str(raised.value)
    assert "[보류]" in message and "구분할 수 없" in message
    assert "금액 확정 후 재실행" not in message            # 번호 재사용 구분 불가는 기다린다고 풀리지 않음
    assert "전용 복구 도구는 아직 없습니다" in message
    assert io["saved"] == []                               # 정산 결과 저장 없음
    assert any("구분할 수 없" in n for n in io["notifications"])   # 긴급 알림도 같은 사유


def test_방향이_다른_같은_번호의_과거_체결은_두_구현_모두_새_주문에_섞지_않는다(io):
    old_sell = _old_row(10, 50.0)
    old_sell["sll_buy_dvsn_cd_name"] = "매도"          # 같은 번호·다른 날짜·반대 방향
    settled = _settle(io, _state(_meta(2, 20.0)), [old_sell, _new_row(5, 10.0)], qty=5)
    assert settled.halt_reason is None
    assert settled.state["orders_meta"][N]["processed_filled_qty"] == 5
    assert settled.state["net_invested"] == pytest.approx(520.0 + 30.0)
    ok, problems, _ = _verify(settled)
    assert ok, problems


def test_날짜_정보가_없는_레거시_메타는_처리기_반영과_검증기_판정이_어긋나면_저장_전에_보류한다(io):
    """처리기는 기존 호환(체결 행 우선)을 유지하지만, 검증기는 번호가 재사용된 날짜 없는 메타를 확정할 수 없어 fail-closed 입니다.
    파이프라인의 저장 전 독립 검증 게이트가 이를 막아 어긋난 값이 저장되지 않습니다 (알려진 한계: 보류 반복)."""
    meta = _meta(2, 20.0)
    del meta["submitted_session"]
    with pytest.raises(trading_bot.SymbolScopedError) as raised:
        _settle(io, _state(meta), [_old_row(10, 50.0), _new_row(5, 10.0)])
    assert "정산 근거 독립 검증 실패" in str(raised.value)
    assert io["saved"] == []
