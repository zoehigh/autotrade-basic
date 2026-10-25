"""파이프라인 EXECUTE 테스트 공용 헬퍼.

주문 루프를 직접 호출하는 우회 경로가 없으므로(P0), 테스트도 **실제 승인 파일**을
만들어 `execute_approved_plan()` 을 통해서만 주문을 실행합니다.

전제:
  - 호출자가 cwd 를 tmp_path 로 바꿔 둡니다 (evidence/approval glob 기준).
  - 호출자가 trading_bot 의 TRADE_MODE/BROKER/BROKER_MODE/SYMBOLS/전략을 패치합니다.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pipeline
import pipeline_execute
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
    now = datetime.now(timezone.utc)
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
):
    """승인 파일을 만들고 execute_approved_plan() 으로 1회 실행합니다."""
    path, plan, _evidence = prepare_approval(
        tmp_path,
        state,
        orders,
        symbol=symbol,
        exchange=exchange,
        balance_qty=balance_qty,
    )
    result = pipeline_execute.execute_approved_plan(
        broker, str(path), refresh=refresh, state_override=state
    )
    return result, path, plan
