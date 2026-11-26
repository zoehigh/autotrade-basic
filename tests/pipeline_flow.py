"""파이프라인 EXECUTE 테스트 공용 헬퍼.

주문 루프를 직접 호출하는 우회 경로가 없으므로(P0), 테스트도 **실제 승인 파일**을
만들어 `execute_approved_plan()` 을 통해서만 주문을 실행합니다.

전제:
  - 호출자가 cwd 를 tmp_path 로 바꿔 둡니다 (evidence/approval glob 기준).
  - 호출자가 trading_bot 의 TRADE_MODE/BROKER/BROKER_MODE/SYMBOLS/전략을 패치합니다.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pipeline
import pipeline_execute
import trading_bot
from state import canonical_state_hash


def write_committed_evidence(symbol="TQQQ", run_id="run-test", balance_qty=0, mode="LIVE"):
    """커밋된 SETTLE 근거 파일을 cwd 에 만듭니다 (PREVIEW/EXECUTE 바인딩용)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(f"settlement-{symbol}-{stamp}-{uuid.uuid4().hex[:6]}.json")
    path.write_text(
        json.dumps(
            {
                "symbol": symbol,
                "run_id": run_id,
                "evidence_version": 2,
                "mode": mode,
                "committed": True,
                "balance_qty": balance_qty,
            }
        ),
        encoding="utf-8",
    )
    return path


def execution_now():
    """테스트용 실행 시각 — 승인된 실행 세션(ET)의 프리장(05:30 ET).

    실제 벽시계가 주말/휴장/정규장 진행 중이어도 "그 실행 세션 당일"의
    유효한 EXECUTE 시각을 만들어 세션 게이트를 결정적으로 통과시킵니다.
    """
    _settled, execution = pipeline.settlement_sessions()
    if not execution:
        return datetime.now(timezone.utc)
    return (
        datetime.fromisoformat(f"{execution}T05:30:00")
        .replace(tzinfo=ZoneInfo("America/New_York"))
        .astimezone(timezone.utc)
    )


def prepare_approval(
    tmp_path,
    state,
    orders,
    *,
    symbol="TQQQ",
    exchange="NAS",
    balance_qty=0,
    run_id=None,
    ttl_seconds=32400,
    now=None,
):
    """PREVIEW 계획 + APPROVE 승인 파일을 실제 파일로 만듭니다.

    state 에 `last_settlement_id`/`last_settlement_evidence` 를 설정한 뒤
    canonical hash 를 계산하므로, `execute_approved_plan()` 의 바인딩 검증을
    그대로 통과합니다.
    """
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    evidence = write_committed_evidence(
        symbol=symbol, run_id=run_id, balance_qty=balance_qty
    )
    state["last_settlement_id"] = run_id
    state["last_settlement_evidence"] = str(evidence)

    plan = pipeline.OrderPlan(
        plan_id="",
        symbol=symbol,
        exchange=exchange,
        session="",
        orders=[dict(order) for order in orders],
        state_hash=canonical_state_hash(state),
        plan_hash="",
        settlement_run_id=run_id,
        created_at_utc="",
        expires_at_utc="",
        state_patch={"close_prices": [50.0]},
    )
    now = now or execution_now()
    pipeline._prepare_plan_identity(plan, now=now, ttl_seconds=ttl_seconds)
    approvals_dir = Path(tmp_path) / "approvals"
    path = pipeline.approve_order_plan(plan, approvals_dir=str(approvals_dir))
    return path, plan, evidence


def run_approved(
    tmp_path,
    broker,
    state,
    orders,
    *,
    symbol="TQQQ",
    exchange="NAS",
    balance_qty=0,
    refresh=True,
    now=None,
):
    """승인 파일을 만들고 execute_approved_plan() 으로 1회 실행합니다.

    `refresh=False` 는 DRY 테스트 전용입니다. LIVE 에서 이 값을 넘기면 승인
    파일을 만들기 **전에** 거부합니다 (stale 계획 전송 금지).

    execute_approved_plan() 의 경계 게이트(LIVE+RUN_PHASE=EXECUTE)를 통과하도록
    호출 동안 RUN_PHASE 를 EXECUTE 로 고정하고 원래 값으로 복원합니다.
    """
    if not refresh and str(getattr(trading_bot, "TRADE_MODE", "")).strip().upper() != "DRY":
        raise RuntimeError(
            "run_approved(refresh=False) 는 DRY 테스트에서만 허용됩니다 — "
            "LIVE 에서는 승인 계획을 반드시 다시 계산해야 합니다."
        )
    now = now or execution_now()
    path, plan, _evidence = prepare_approval(
        tmp_path,
        state,
        orders,
        symbol=symbol,
        exchange=exchange,
        balance_qty=balance_qty,
        now=now,
    )
    previous_phase = os.environ.get("RUN_PHASE")
    os.environ["RUN_PHASE"] = "EXECUTE"
    try:
        result = pipeline_execute.execute_approved_plan(
            broker, str(path), now=now, refresh=refresh, state_override=state
        )
    finally:
        if previous_phase is None:
            os.environ.pop("RUN_PHASE", None)
        else:
            os.environ["RUN_PHASE"] = previous_phase
    return result, path, plan
