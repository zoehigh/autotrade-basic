"""
파이프라인 EXECUTE 단계 — 실제 주문 API 호출은 **이 모듈에만** 존재합니다.

SETTLE / PREVIEW / APPROVE 모듈(pipeline.py)은 주문 API 문자열을 포함하지 않으며,
`tests/test_pipeline_phases.py` 의 정적 가드가 이를 검증합니다.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pipeline import (
    _approval_lock,
    _atomic_write_json,
    _claim_approval_locked,
    _TERMINAL_APPROVAL_OUTCOMES,
    approval_execution_blockers,
    approval_outcome_path,
    assert_no_pending_fence,
    assert_reconciliation_current,
    _assert_reconciliation_with_deferred,
    build_order_plan,
    compute_plan_hash,
    _deferred_buy_spend,
    _deferred_fills_evidence,
    deferred_record_path,
    effective_tolerance_policy,
    evidence_file_for_run,
    global_lock,
    load_approval,
    normalize_tolerance_policy,
    price_tolerance,
    read_approval_outcome,
    settled_from_state,
    transmitted_plan_path,
    verify_approval,
)


def _tb():
    import trading_bot

    return trading_bot


def _wait_until_pre_market(tb, now=None):
    """프리장 개장 전이면 **먼저** 대기하고, 재검증에 쓸 기준 시각을 반환합니다.

    EXECUTE 는 승인 검증/선점/전송 **전에** 대기해야 합니다. 대기 후 시세·체결이
    바뀌었을 수 있으므로 모든 게이트(승인/세션/만료/fence/재정산/계획 재계산/
    매도<=보유)를 대기 **뒤에** 다시 실행합니다.

    Returns:
        datetime | None: 재검증 기준 시각. None 이면 실제 현재 시각을 사용합니다.
        테스트/재생처럼 `now` 가 주어지면 대기하지 않고 그대로 반환합니다.
    """
    if now is not None:
        return now
    if not (tb.BROKER_MODE == "real" and tb.TRADE_MODE == "LIVE"):
        return None
    now_kst = tb.get_kst_now()
    pre_market_open_hour = 17 if tb.is_us_dst() else 18
    if now_kst.hour < pre_market_open_hour:
        target = now_kst.replace(
            hour=pre_market_open_hour, minute=0, second=0, microsecond=0
        )
        wait_seconds = (target - now_kst).total_seconds()
        print(
            f"⏳ 프리장 오픈 대기 중... (KST {now_kst.strftime('%H:%M:%S')} → "
            f"{target.strftime('%H:%M:%S')}, 약 {wait_seconds/60:.0f}분)"
        )
        tb.time.sleep(wait_seconds)
        # 대기 후에는 최신 시각으로 전체 게이트를 다시 통과해야 합니다.
        return None
    return None


# ── 승인 재검증 ────────────────────────────────────────────────
# 주문 계획은 **디스크의 consumed 승인 파일**에서만 다시 읽습니다. 모듈에 실행
# 함수/핸들을 공개하지 않으므로, 같은 프로세스에서 임의 계획/state/이력을
# 주문 루프에 주입할 수 있는 API 는 존재하지 않습니다 (P0).
def _reload_approved_plan(path, now=None):
    """디스크의 승인 파일 + state 를 다시 읽어 소비/해시/바인딩을 재검증합니다.

    메모리 승인 dict 나 호출자 제공 state 를 신뢰하지 않습니다. 실행 직전의
    디스크 state 로 state_hash 까지 다시 확인하므로, state 를 주입해도 승인
    내용을 바꿀 수 없습니다. 실패하면 주문 API 는 호출되지 않습니다.
    """
    approval = load_approval(path)
    if approval.get("consumed") is not True:
        raise RuntimeError(
            "승인 파일이 consumed=true 가 아닙니다 — 승인 선점 없이 주문할 수 없습니다."
        )
    state = _tb().load_state(approval.get("symbol", ""))
    problems = verify_approval(
        approval,
        state,
        now=now,
        require_unconsumed=False,
        check_state_hash=True,
    )
    if problems:
        raise RuntimeError(
            "승인 파일 재검증 실패 — 주문을 실행하지 않습니다: "
            + " / ".join(problems)
        )
    return approval, state


def apply_state_patch(state, patch):
    """승인된 계획에 포함된 상태 변경(close_prices/reverse_mode)을 적용합니다."""
    if not patch:
        return state
    if "close_prices" in patch and patch["close_prices"] is not None:
        state["close_prices"] = list(patch["close_prices"])
    if "reverse_mode" in patch and patch["reverse_mode"] is not None:
        state["reverse_mode"] = patch["reverse_mode"]
    return state


def _symbol_config(symbol):
    tb = _tb()
    for config in getattr(tb, "SYMBOLS", []) or []:
        if config.get("symbol") == symbol:
            return config
    raise RuntimeError(f"{symbol} 종목 설정을 찾을 수 없습니다.")


def _price_tolerance(price, policy):
    """정책 기준 가격 허용오차. 정책은 승인 시점에 고정된 값입니다."""
    return price_tolerance(price, policy)


def _qty_tolerance(policy):
    try:
        return int(policy["qty"])
    except (KeyError, TypeError, ValueError):
        return 0


def compare_order_plans(approved_orders, rebuilt_orders, policy=None):
    """승인 계획과 현재 재계산 계획의 주문을 허용 오차 내에서 비교합니다.

    `policy` 는 **승인 파일에 고정된** 허용오차 정책입니다. EXECUTE 경로는
    반드시 승인 정책(env 로 더 좁힌 유효 정책)을 넘겨야 하며, 없으면
    fail-closed 로 비교하지 않습니다.
    """
    problems = []
    approved_orders = list(approved_orders or [])
    rebuilt_orders = list(rebuilt_orders or [])
    if policy is None:
        problems.append(
            "승인 파일에 허용오차 정책이 없어 주문 계획을 비교할 수 없습니다 "
            "(재승인 필요)"
        )
        return problems
    if len(approved_orders) != len(rebuilt_orders):
        problems.append(
            f"주문 개수 불일치: 승인 {len(approved_orders)} vs 재계산 {len(rebuilt_orders)}"
        )
        return problems
    qty_tol = _qty_tolerance(policy)
    for index, (approved, rebuilt) in enumerate(
        zip(approved_orders, rebuilt_orders), 1
    ):
        for field in ("side", "order_type", "comment"):
            if str(approved.get(field, "")) != str(rebuilt.get(field, "")):
                problems.append(
                    f"주문 {index} {field} 불일치: "
                    f"승인={approved.get(field)!r}, 재계산={rebuilt.get(field)!r}"
                )
        a_qty = int(float(approved.get("quantity", 0) or 0))
        r_qty = int(float(rebuilt.get("quantity", 0) or 0))
        if abs(a_qty - r_qty) > qty_tol:
            problems.append(
                f"주문 {index} 수량 불일치: 승인={a_qty}, 재계산={r_qty} "
                f"(허용 {qty_tol})"
            )
        a_price = float(approved.get("price") or 0.0)
        r_price = float(rebuilt.get("price") or 0.0)
        tolerance = _price_tolerance(a_price, policy)
        if abs(a_price - r_price) > tolerance:
            problems.append(
                f"주문 {index} 가격 불일치: 승인=${a_price:.4f}, "
                f"재계산=${r_price:.4f} (허용 ±${tolerance:.4f})"
            )
        a_amount = a_price * a_qty
        r_amount = r_price * r_qty
        amount_tolerance = max(_price_tolerance(a_price, policy) * max(a_qty, 1), 0.01)
        if abs(a_amount - r_amount) > amount_tolerance:
            problems.append(
                f"주문 {index} 금액 불일치: 승인=${a_amount:.2f}, "
                f"재계산=${r_amount:.2f} (허용 ±${amount_tolerance:.2f})"
            )
        a_target = approved.get("t_target")
        r_target = rebuilt.get("t_target")
        if a_target is not None and r_target is not None:
            if abs(float(a_target) - float(r_target)) > 1e-9:
                problems.append(
                    f"주문 {index} t_target 불일치: {a_target} vs {r_target}"
                )
    return problems


def _assert_sell_within_holdings(orders, balance, symbol):
    sell_total = sum(
        int(float(order.get("quantity", 0) or 0))
        for order in orders or []
        if str(order.get("side", "")).upper() == "SELL"
    )
    if sell_total <= 0:
        return
    if balance is None:
        raise RuntimeError(
            f"{symbol} 잔고를 확인할 수 없어 매도 주문을 실행할 수 없습니다 (fail-closed)."
        )
    if sell_total > int(balance.quantity):
        raise RuntimeError(
            f"{symbol} 매도 합계 {sell_total}주 > 보유 {balance.quantity}주 — "
            "승인 계획을 실행할 수 없습니다."
        )


def _write_outcome_record(approval, outcome, reason, now=None):
    """승인 스킵/실행 성공/실행 실패를 approval 옆 결과 파일로 남깁니다.

    VERIFY 는 이 파일의 outcome 으로 "승인만 되고 실행 안 됨(approval-skipped)" 과
    "실행했지만 실패/부분실패(execution-failed)", "정상 실행(executed)" 을 구분합니다.
    파일명은 하위호환을 위해 `.recovery.json` 을 유지합니다 (P1-C).
    """
    path = str(approval.get("_path") or "")
    if not path:
        return None
    record_path = approval_outcome_path(path)
    # 실행이 시작된 결과(executed/execution-failed)는 덮어쓰지 않습니다. 재실행
    # 시도가 기존 성공 기록을 approval-skipped 로 오염시키는 것을 막습니다 (P0-A).
    # 단, approval-skipped 는 승인을 소진하지 않은 "시도 전 중단" 기록이므로
    # 조건 해소 후 재시도가 성공하면 executed 로 승격할 수 있습니다.
    existing = str(read_approval_outcome(path).get("outcome") or "")
    if existing in _TERMINAL_APPROVAL_OUTCOMES or existing == outcome:
        print(f"[EXECUTE] 기존 결과 기록 유지 ({existing}): {record_path}")
        return record_path
    _atomic_write_json(
        record_path,
        {
            "plan_id": approval.get("plan_id", ""),
            "outcome": outcome,
            "reason": str(reason)[:1000],
            "at_utc": (
                (now or datetime.now(timezone.utc))
                .astimezone(timezone.utc)
                .isoformat()
            ),
        },
    )
    print(f"[EXECUTE] {outcome} 기록: {record_path}")
    return record_path


def confirm_open_orders_cancelled(broker, symbol, exchange, state, order_history):
    """신규 주문 전 잔여 주문 취소를 확정합니다 (LIVE 전용)."""
    tb = _tb()
    if tb.TRADE_MODE != "LIVE":
        return 0
    return tb._cancel_open_bot_orders(broker, symbol, exchange, state, order_history)


def _write_execute_deferred_record(approval_path, approval, deferred_rows, now=None):
    """EXECUTE 가 수용한 실행 세션 보류 체결 집합을 **동결**해 기록합니다.

    VERIFY 는 live 이력만으로 "EXECUTE 전에 수용된 체결"과 "EXECUTE 후에
    도착한 체결"을 구분할 수 없습니다. 그래서 승인 소비(claim) 직후, 주문
    전송과 같은 잠금 경계에서 이 집합을 sidecar 로 남깁니다.

    행 형식은 `{odno, side, qty, session, amount?}` 이며, 금액은 확정된
    경우에만 기록합니다 (추정 금지). 기록 실패는 주문 전에 fail-closed 로
    중단되어야 하므로 호출자는 예외를 그대로 전파합니다.
    """
    payload = {
        "plan_id": str(approval.get("plan_id") or ""),
        "symbol": str(approval.get("symbol") or ""),
        "session": str(approval.get("session") or ""),
        "settlement_run_id": str(approval.get("settlement_run_id") or ""),
        "deferred_fills": _deferred_fills_evidence(deferred_rows),
        "recorded_at_utc": (
            (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        ),
    }
    path = deferred_record_path(approval_path)
    _atomic_write_json(path, payload)
    print(
        f"[EXECUTE] 실행 세션 보류 체결 기록: {path} "
        f"({len(payload['deferred_fills'])}건)"
    )
    return path


def _write_transmitted_plan(approval_path, plan, approval, drift=None, now=None):
    """EXECUTE 가 실제로 전송하는 최종 계획을 승인 파일 옆에 기록합니다.

    승인 파일은 "승인 시점" 값이고, 전송 계획은 재계산으로 허용오차 내에서
    달라질 수 있습니다. VERIFY 는 이 기록(transmitted)을 기준으로 주문을
    대조해야 실제 전송값과 승인값을 혼동하지 않습니다.
    """
    path = transmitted_plan_path(approval_path)
    policy = normalize_tolerance_policy(plan.tolerance_policy) or {}
    transmitted_hash = compute_plan_hash(
        plan.plan_id,
        plan.symbol,
        plan.exchange,
        plan.session,
        plan.state_hash,
        plan.settlement_run_id,
        plan.orders,
        plan.state_patch,
        plan.evidence_id or plan.settlement_run_id,
        plan.broker,
        plan.broker_mode,
        plan.account_id,
        plan.created_at_utc,
        plan.expires_at_utc,
        plan.account_display,
        policy,
    )
    payload = {
        "plan_id": plan.plan_id,
        "symbol": plan.symbol,
        "exchange": plan.exchange,
        "session": plan.session,
        "approval_plan_hash": approval.get("plan_hash", ""),
        "transmitted_plan_hash": transmitted_hash,
        "state_hash": plan.state_hash,
        "settlement_run_id": plan.settlement_run_id,
        "evidence_id": plan.evidence_id or plan.settlement_run_id,
        "orders": list(plan.orders or []),
        "state_patch": dict(plan.state_patch or {}),
        # VERIFY 가 transmitted_plan_hash 를 **독립 재계산**할 수 있도록 해시
        # 입력 필드를 모두 남깁니다 (approval hash 와 같은 입력 집합).
        "broker": plan.broker,
        "broker_mode": plan.broker_mode,
        "account_id": plan.account_id,
        "account_display": plan.account_display,
        "created_at_utc": plan.created_at_utc,
        "expires_at_utc": plan.expires_at_utc,
        # 승인 시점에 고정된 정책(해시 입력) + 실행 시점에 실제 적용한(더 좁힌)
        # 유효 정책(감사용, 해시 비입력).
        "tolerance_policy": policy,
        "effective_tolerance_policy": effective_tolerance_policy(
            approval.get("tolerance_policy")
        )
        or {},
        "drift_within_tolerance": list(drift or []),
        "source": "rebuilt" if drift is not None else "approval",
        "recorded_at_utc": (
            (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        ),
    }
    _atomic_write_json(path, payload)
    print(f"[EXECUTE] 실제 전송 계획 기록: {path}")
    return path


def _assert_refresh_allowed(refresh):
    """계획 재계산 생략(`refresh=False`)은 DRY/테스트에서만 허용합니다.

    파이프라인의 핵심 보장은 "승인 시점 계획이 아니라 **실행 직전 재계산한**
    계획만 전송한다" 입니다. LIVE 에서 재계산을 생략하면 승인 후 바뀐
    시세·체결을 반영하지 않은 stale 주문이 그대로 나가므로, 브로커 조회/주문/
    승인 소비 같은 **어떤 부수효과보다 먼저** fail-closed 로 거부합니다.
    """
    if refresh:
        return
    tb = _tb()
    if str(getattr(tb, "TRADE_MODE", "")).strip().upper() == "DRY":
        return
    raise RuntimeError(
        "refresh=False 는 DRY 모드에서만 허용됩니다 — LIVE 에서는 승인 계획을 "
        "실행 직전에 반드시 다시 계산해야 합니다 (stale 주문 전송 금지)."
    )


def _assert_execute_phase_allowed():
    """실행 함수 자체의 경계 게이트 — 호출자와 무관하게 LIVE+EXECUTE 만 허용합니다.

    `main()`/`create_broker()` 의 RUN_PHASE 검증을 우회해 이 함수를 직접 호출해도
    (DRY + 오래된 LIVE 승인 + 실제 브로커) 주문 API 에 도달할 수 없어야 합니다.
    승인 로드/잠금/브로커 조회/주문 **이전에** 거부하므로 승인은 소비되지 않습니다.

    RUN_PHASE 해석은 `broker.validate_run_phase()` 와 같습니다
    (env, strip().upper(), 미설정="").
    """
    tb = _tb()
    mode = str(getattr(tb, "TRADE_MODE", "")).strip().upper()
    phase = os.getenv("RUN_PHASE", "").strip().upper()
    if mode == "LIVE" and phase == "EXECUTE":
        return
    raise RuntimeError(
        "execute_approved_plan() 은 TRADE_MODE=LIVE 이고 RUN_PHASE=EXECUTE 일 때만 "
        f"실행할 수 있습니다 (현재 TRADE_MODE={mode or '미설정'}, "
        f"RUN_PHASE={phase or '미설정'}) — 승인은 소비하지 않았습니다."
    )


def execute_approved_plan(
    broker, approval_or_id, approvals_dir=None, now=None, refresh=True,
    state_override=None,
):
    """EXECUTE 진입점 — 전역 실행 잠금 안에서 승인 계획을 실행합니다."""
    # 승인 로드/잠금/브로커 호출 전에 검사합니다 (승인은 소비되지 않습니다).
    _assert_execute_phase_allowed()
    _assert_refresh_allowed(refresh)
    with global_lock():
        return _execute_approved_plan(
            broker,
            approval_or_id,
            approvals_dir=approvals_dir,
            now=now,
            refresh=refresh,
            state_override=state_override,
        )


def _execute_approved_plan(
    broker, approval_or_id, approvals_dir=None, now=None, refresh=True,
    state_override=None,
):
    """승인 파일을 검증·새로고침·취소확정한 뒤 1회 실행합니다.

    `refresh=False` 는 DRY 전용 escape hatch 이며, LIVE 에서는 진입 즉시
    거부됩니다 (`_assert_refresh_allowed`).

    순서:
      1) 승인 파일 로드 + state 재로드 + hash/session/expiry/broker 바인딩 검증
      2) fence/미확정 정산 거부
      3) 이력·잔고·열린주문 재조회 + 현재 state 로 계획 재계산/허용오차 비교
      4) 매도 합계 <= 보유수량 확인
      5) 잔여 주문 취소 확정 (실패 시 승인을 소비하지 않고 approval-skipped 기록)
      6) **파일 잠금 안에서** consumed=true 선점 + 실행 마커 + 디스크 재검증 + 주문 실행
         (실행 루프는 이 함수 안의 내부 함수 — 외부에서 호출할 수 없습니다)
    """
    # 공개 진입점과 같은 fail-closed 검사를 여기서도 수행합니다 (직접 호출 방어).
    # 위상 게이트는 승인/브로커/주문 그 무엇보다 먼저입니다.
    _assert_execute_phase_allowed()
    _assert_refresh_allowed(refresh)
    tb = _tb()
    approval = (
        approval_or_id
        if isinstance(approval_or_id, dict)
        else load_approval(approval_or_id, approvals_dir)
    )
    if not approval.get("_path"):
        raise ValueError("승인 파일 경로가 없어 단일 사용 처리를 할 수 없습니다.")
    path = str(approval.get("_path"))

    # ── 0) 이미 사용/실행된 승인은 어떤 부수효과도 일으키지 않고 거부 ──
    blockers = approval_execution_blockers(path, approval)
    if blockers:
        raise RuntimeError(
            "이미 사용/실행된 승인입니다 — 재실행할 수 없습니다: "
            + " / ".join(blockers)
        )

    # ── 0.5) 프리장 대기 → **대기 후** 전체 게이트 재실행 ─────────────
    # 대기 전에 검증/재조회/전송을 하면 대기 중 바뀐 시세·체결을 반영하지 못합니다.
    # (대기 후에는 추가 대기 없이 검증→선점→전송→주문을 연속 수행합니다.)
    effective_now = _wait_until_pre_market(tb, now)
    # 대기 중 다른 프로세스가 승인을 선점/변경했을 수 있으므로 디스크에서 다시 읽습니다.
    approval = load_approval(path)

    symbol = approval.get("symbol", "")
    exchange = approval.get("exchange", "")
    state = state_override if state_override is not None else tb.load_state(symbol)

    problems = verify_approval(approval, state, now=effective_now)
    if problems:
        raise RuntimeError(
            "승인 검증 실패 — 주문을 실행하지 않습니다: " + " / ".join(problems)
        )
    # 승인 시점에 고정된 정책 + 현재 env 중 **더 좁은** 범위만 사용합니다.
    # env 를 넓혀도 승인 범위를 넘을 수 없습니다 (넓히려면 재승인).
    tolerance_policy = effective_tolerance_policy(approval.get("tolerance_policy"))
    if tolerance_policy is None:
        raise RuntimeError(
            "승인 파일의 허용오차 정책을 확인할 수 없어 주문을 실행하지 않습니다 "
            "— 재승인(PREVIEW→APPROVE)이 필요합니다."
        )
    assert_no_pending_fence(state, symbol)

    evidence_id = str(
        approval.get("evidence_id") or approval.get("settlement_run_id") or ""
    )
    if not evidence_id or not evidence_file_for_run(symbol, evidence_id):
        raise RuntimeError(
            f"{symbol} 커밋된 SETTLE 근거를 찾을 수 없습니다 (evidence_id={evidence_id}) "
            "— 주문을 실행하지 않습니다."
        )
    order_history = broker.get_order_history(symbol, exchange, days=30)
    balance = broker.get_balance(symbol, exchange)
    open_rows, open_error = tb._load_open_orders_for_cancel(broker, symbol, exchange)
    if open_error is not None:
        raise tb.SymbolScopedError(
            f"{symbol} 열린 주문 조회 실패 — 주문을 실행하지 않습니다: {open_error}"
        )
    meta_odnos = {str(k) for k in (state.get("orders_meta") or {})}
    unknown_open = [
        str(row.get("odno") or "")
        for row in (open_rows or [])
        if str(row.get("odno") or "") not in meta_odnos
    ]
    if unknown_open:
        raise tb.SymbolScopedError(
            f"{symbol} 봇 소유가 아닌 미체결 주문이 있어 주문을 실행하지 않습니다: "
            + ", ".join(unknown_open[:5])
        )

    # ── 3b) 승인 후 새 체결/잔고 불일치 재확인 (계획 재계산보다 먼저) ──
    # SETTLE 이 실행 세션으로 **미룬** 체결(프리마켓 등)은 state 워터마크 앞이라
    # 새 체결처럼 보이지만, 그 세션의 다음 SETTLE 에서 반영될 예정입니다.
    # 잔고 교차검증으로 포지션 변화가 정확히 설명될 때만 허용 목록에 넣고,
    # 그 외 새 체결/불일치는 기존처럼 fail-closed 로 중단합니다.
    accepted_deferred_rows = _assert_reconciliation_with_deferred(
        symbol, state, order_history, balance, context="EXECUTE"
    )

    # 승인 계획은 **허용오차 게이트**일 뿐, 실제 전송 대상이 아닙니다.
    # 실행 직전 재계산한 계획(허용오차 내 드리프트 반영)을 전송합니다.
    rebuilt = None
    drift = []
    if refresh:
        # SETTLE 이 실행 세션으로 미룬 매수는 state.net_invested 에 아직 없습니다.
        # 재계산 계획의 **시드 캡에만** 그 금액을 더합니다 (수량 reconciliation 은
        # 달러 초과를 막지 못함 — P1). 금액 미확정 매수가 있으면 추정하지 않고
        # 정산 전까지 실행을 차단합니다 (fail-closed).
        deferred_spend, pending_deferred = _deferred_buy_spend(
            state, order_history, deferred_rows=accepted_deferred_rows
        )
        if pending_deferred:
            raise tb.SymbolScopedError(
                f"{symbol} 미룬 매수 체결의 금액이 확정되지 않았거나 방향을 "
                f"해석할 수 없습니다 (odno: {', '.join(pending_deferred[:5])}) — "
                "시드 캡을 정확히 계산할 수 없어 SETTLE 전까지 실행을 중단합니다."
            )
        net_invested_overlay = None
        if deferred_spend > 0:
            net_invested_overlay = (
                float(state.get("net_invested", 0.0) or 0.0) + deferred_spend
            )
        try:
            symbol_config = _symbol_config(symbol)
            settled = settled_from_state(
                symbol_config,
                state=state,
                settlement_run_id=approval.get("settlement_run_id", ""),
            )
            rebuilt = build_order_plan(
                settled, broker, net_invested_overlay=net_invested_overlay
            )
        except Exception as error:
            raise RuntimeError(
                f"{symbol} 실행 전 계획 재계산 실패 — 주문을 실행하지 않습니다: {error}"
            ) from error
        drift = compare_order_plans(
            approval.get("orders") or [], rebuilt.orders, policy=tolerance_policy
        )
        if drift:
            raise RuntimeError(
                f"{symbol} 승인 계획과 현재 계획이 허용 오차를 벗어났습니다 — "
                "재승인(PREVIEW→APPROVE)이 필요합니다: " + " / ".join(drift[:4])
            )

    # ── 4) 매도 합계 <= 보유수량 (전송할 계획 기준) ──
    transmission_orders = rebuilt.orders if rebuilt is not None else (
        approval.get("orders") or []
    )
    _assert_sell_within_holdings(transmission_orders, balance, symbol)

    # ── 5) 취소 확정 (실패 시 승인 소비하지 않음) ──
    try:
        confirm_open_orders_cancelled(broker, symbol, exchange, state, order_history)
    except Exception as error:
        _write_outcome_record(
            approval, "approval-skipped", f"cancel-confirm: {error}", now=effective_now
        )
        raise

    # ── 6-7) 선점 + 실행을 **하나의 파일 잠금 경계**에서 수행 ──
    # 임의 계획/state 를 주입할 수 있는 공개 실행 경로는 없습니다.
    # state/주문 내용은 모두 디스크에서 다시 읽습니다 (P0).
    with _approval_lock(path):
        blockers = approval_execution_blockers(path)
        if blockers:
            raise RuntimeError(
                "이미 사용/실행된 승인입니다 — 재실행할 수 없습니다: "
                + " / ".join(blockers)
            )
        try:
            claimed_latest = _claim_approval_locked(path, now=effective_now)
        except Exception as error:
            # 선점 경합/실패는 기존 결과 기록을 덮어쓰지 않습니다.
            if not approval_execution_blockers(path):
                _write_outcome_record(
                    approval, "approval-skipped", f"claim: {error}", now=effective_now
                )
            raise
        approval["consumed"] = True
        approval["consumed_at_utc"] = claimed_latest.get("consumed_at_utc", "")

        # 주문 루프는 이 잠금 경계 안의 **내부 함수**로만 존재합니다. 모듈에
        # 실행 함수/핸들을 두지 않으므로, 같은 프로세스에서 import 해 임의
        # 계획/state 로 주문 루프에 진입할 수 없습니다 (P0).
        try:
            approval, state = _reload_approved_plan(path, now=effective_now)
            plan = _plan_from_approval(approval)
            if rebuilt is not None:
                # 재로드한 승인(디스크)과 재계산 계획을 다시 대조합니다. 이 시점의
                # state 는 state_hash 로 승인과 동일함이 보장됩니다 (fail-closed).
                drift = compare_order_plans(
                    approval.get("orders") or [],
                    rebuilt.orders,
                    policy=tolerance_policy,
                )
                if drift:
                    raise RuntimeError(
                        f"{symbol} 승인 계획과 현재 계획이 허용 오차를 벗어났습니다 — "
                        "재승인(PREVIEW→APPROVE)이 필요합니다: " + " / ".join(drift[:4])
                    )
                # 승인 id/바인딩은 유지하고, 전송 내용만 재계산 계획으로 교체합니다.
                plan.orders = list(rebuilt.orders)
                plan.state_patch = dict(getattr(rebuilt, "state_patch", {}) or {})
                plan.strategy_result = dict(getattr(rebuilt, "strategy_result", {}) or {})
            state = apply_state_patch(state, getattr(plan, "state_patch", None))
            _assert_sell_within_holdings(plan.orders, balance, symbol)
            # 선점(consumed=true) 이후에만 기록합니다. approval-skipped 는 선점 전
            # 중단이므로 아무 sidecar 도 남기지 않습니다. 이 기록은 전송/결과
            # 기록과 같은 잠금 경계 안에서 원자적으로 남깁니다.
            _write_execute_deferred_record(
                path, approval, accepted_deferred_rows, now=effective_now
            )
            _write_transmitted_plan(path, plan, approval, drift=drift, now=effective_now)

            def _run_orders():
                tb = _tb()
                symbol = plan.symbol
                exchange = plan.exchange
                orders = plan.orders
                assert_no_pending_fence(state, symbol)
                # 잔여 주문 취소 확정은 execute_approved_plan() 이 **이미** 마쳤습니다.
                # 여기서 다시 취소하면 방금 낸 주문을 건드릴 위험이 있어 호출하지 않습니다.

                print("\n[Step 4] 주문 실행 중...")
                print("-" * 60)

                # 프리장 대기는 execute_approved_plan() 진입 시 **이미** 끝냈습니다.
                # (대기 후 전체 게이트를 다시 통과한 뒤 선점/전송하므로 여기서 또
                #  기다리면 검증과 주문 사이에 다시 시차가 생깁니다.)
                order_exchange_code = broker.exchange_code(exchange)

                # ── 잔여 주문 정리 ────────────────────────────────────────────
                # 매 세션마다 새 주문 전체를 새로 냅니다. 이전에 낸 주문이 아직 살아 있으면
                # (특히 목표매도 LIMIT) 새 주문과 중복되어 매도 수량이 보유량을 넘을 수 있습니다.
                # 새 주문을 내기 전에 봇 소유의 미체결 주문을 정리합니다.
                if tb.TRADE_MODE == "LIVE":
                    state["pending_order_batch"] = {
                        "session": tb.datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                        "orders": [
                            {
                                "side": item["side"],
                                "quantity": int(item["quantity"]),
                                "price": item["price"],
                                "order_type": item["order_type"],
                                "comment": item["comment"],
                            }
                            for item in orders
                        ],
                    }
                    tb.save_state(symbol, state)

                executed_orders = []
                reserved_orders = []
                failed_orders = []
                fatal_order_error = False

                for i, order in enumerate(orders, 1):
                    print(f"\n주문 {i}/{len(orders)} 실행: {order['comment']}")

                    try:
                        order_price = order["price"] if order["price"] else 0

                        if tb.TRADE_MODE == "LIVE":
                            state["pending_order_intent"] = {
                                "symbol": symbol,
                                "side": order["side"],
                                "quantity": int(order["quantity"]),
                                "price": order_price,
                                "order_type": order["order_type"],
                                "comment": order["comment"],
                                "submitted_session": tb.datetime.now(
                                    ZoneInfo("America/New_York")
                                ).date().isoformat(),
                            }
                            try:
                                tb.save_state(symbol, state)
                            except Exception as intent_error:
                                raise tb.GlobalScopedError(
                                    f"주문 전 intent checkpoint 실패: {intent_error}"
                                ) from intent_error

                        result = broker.place_order(
                            symbol,
                            order_exchange_code,
                            order["side"],
                            order["quantity"],
                            order_price,
                            order["order_type"],
                        )

                        if result:
                            if not str(result.order_id or "").strip():
                                raise tb.SymbolScopedError(
                                    "주문 접수 응답에 유효한 주문번호가 없습니다. 주문 fence를 유지합니다."
                                )
                            is_additional = "[추가매수]" in order.get("comment", "")

                            # 기존: additional_loc_odno 유지 (미등록 호환)
                            if is_additional:
                                state.setdefault("additional_loc_odno", []).append(result.order_id)

                            # 신규: orders_meta에 t_target 등록
                            # strategy가 t_target을 제공하면 그대로 사용, 없으면 fallback
                            t_target = order.get("t_target")
                            if t_target is None:
                                t_target = 0.0 if is_additional else 1.0

                            order_meta = {
                                "side": order["side"],
                                "total_qty": int(order["quantity"]),
                                "t_target": float(t_target),
                                "is_additional": bool(is_additional),
                                "processed_filled_qty": 0,
                                # 승인 계획과 1:1 대조용 (VERIFY P1-B). 주문 내용은 여기에
                                # 복제하지 않고 계획 id/순번만 남겨 대조 기준으로 씁니다.
                                "plan_id": str(plan.plan_id or ""),
                                "plan_order_index": i,
                                # VERIFY 가 "같은 세션 미노출(정상)" 과 "이전 세션 누락(실패)"
                                # 을 구분할 수 있게 제출 세션을 남깁니다.
                                "submitted_session": tb.datetime.now(
                                    ZoneInfo("America/New_York")
                                ).date().isoformat(),
                            }
                            if order.get("reverse_action"):
                                reverse_mode = state.setdefault("reverse_mode", {})
                                cycle_id = reverse_mode.get("cycle_id") or tb.datetime.now(
                                    ZoneInfo("UTC")
                                ).isoformat()
                                order_meta.update({
                                    "reverse_action": order["reverse_action"],
                                    "reverse_day": int(order.get("reverse_day", 0)),
                                    "cycle_id": cycle_id,
                                    "submitted_at": result.order_time,
                                    "submitted_session": tb.datetime.now(
                                        ZoneInfo("America/New_York")
                                    ).date().isoformat(),
                                })
                                for key in ("reverse_base_t", "reverse_t_factor", "reverse_t_target"):
                                    if key in order:
                                        order_meta[key] = float(order[key])
                                reverse_mode["active"] = True
                                reverse_mode["cycle_id"] = cycle_id
                                reverse_mode["last_planned_session"] = tb.datetime.now(
                                    ZoneInfo("America/New_York")
                                ).date().isoformat()
                            tb.register_order_meta_in_state(state, result.order_id, order_meta)
                            state["pending_order_intent"] = None
                            if tb.TRADE_MODE == "LIVE":
                                # 주문 응답을 받은 즉시 메타데이터를 checkpoint하여
                                # 이후 주문 실패/프로세스 중단 시에도 체결 reconciliation이 가능합니다.
                                try:
                                    tb.save_state(symbol, state)
                                except Exception as checkpoint_error:
                                    raise tb.GlobalScopedError(
                                        f"주문은 접수됐지만 상태 checkpoint에 실패했습니다: {checkpoint_error}"
                                    ) from checkpoint_error

                            if result.is_reservation:
                                reserved_orders.append({
                                    "comment": order["comment"],
                                    "order_id": result.order_id,
                                    "order_time": result.order_time,
                                })
                                print(f"✓ 예약주문 접수 완료 (주문번호: {result.order_id})")
                                message = f"""📋 예약주문 접수 {symbol}

            {order['comment']}
            수량: {order['quantity']}주
            예약주문번호: {result.order_id}
            접수일자: {result.order_time}"""
                                tb.notify(message)
                            else:
                                executed_orders.append({
                                    "comment": order["comment"],
                                    "order_id": result.order_id,
                                    "order_time": result.order_time,
                                })
                                print("✓ 주문 성공")
                                message = f"""✅ 주문 성공 {symbol}

            {order['comment']}
            수량: {order['quantity']}주
            주문번호: {result.order_id}
            시각: {result.order_time}"""
                                tb.notify(message)
                        else:
                            if tb.TRADE_MODE == "LIVE":
                                raise tb.SymbolScopedError(
                                    "주문 접수 응답이 없어 pending intent를 해소하지 못했습니다. 주문 fence를 유지합니다."
                                )
                            print("✓ 주문 정보 출력 완료")

                    except tb.OrderNotAcceptedError as error:
                        print(f"✗ 주문 실패 (미접수 확정): {str(error)}")
                        if tb.TRADE_MODE == "LIVE":
                            # 브로커가 명시적으로 거부해 주문이 접수되지 않았음이 보장되므로
                            # 해소할 주문이 없는 fence만 남기지 않습니다.
                            state["pending_order_intent"] = None
                            state["pending_order_batch"] = None
                            try:
                                tb.save_state(symbol, state)
                                print("  → 미접수 확정: 주문 fence를 해제하고 해당 종목의 남은 주문을 중단합니다.")
                            except Exception as save_error:
                                print(f"✗ fence 해제 상태 저장 실패: {save_error}")
                                fatal_order_error = True
                        failed_orders.append(
                            {
                                "comment": order["comment"],
                                "error": str(error),
                            }
                        )

                        message = f"""⚠️ 주문 미접수 확정 {symbol}

            {order['comment']}
            에러: {str(error)}
            접수 여부: 미접수 확정
            fence: 해제"""
                        tb.notify(message, urgent=True)
                        break
                    except Exception as error:
                        print(f"✗ 주문 실패: {str(error)}")
                        if tb.TRADE_MODE == "LIVE":
                            fatal_order_error = True
                        failed_orders.append(
                            {
                                "comment": order["comment"],
                                "error": str(error),
                            }
                        )

                        message = f"""⚠️ 주문 실패 {symbol}

            {order['comment']}
            에러: {str(error)}
            접수 여부: 불확실
            fence: 유지"""
                        tb.notify(message, urgent=True)
                        if isinstance(error, tb.GlobalScopedError):
                            # 공통 장애(상태 저장 실패)는 전체 실행을 중단합니다.
                            raise
                        if isinstance(error, tb.SymbolScopedError):
                            # 해당 종목만 중단하고 다음 종목으로 계속 진행합니다.
                            raise
                        if fatal_order_error:
                            break
                        continue

                if fatal_order_error:
                    raise tb.SymbolScopedError(
                        "주문 결과를 확정할 수 없어 주문 fence를 유지합니다. "
                        "해당 종목의 남은 주문을 중단합니다 (fence는 종목별로 복구되며 다른 종목은 계속 진행)."
                    )

                if tb.TRADE_MODE == "LIVE":
                    state["pending_order_batch"] = None
                    tb.save_state(symbol, state)

                print("\n" + "=" * 60)
                print(f"{symbol} 처리 완료")
                print("=" * 60)

                # ── Step 4: T값 저장 (LIVE 주문 성공 or 리버스모드 or 상태 변경) ──
                should_save = False
                if tb.TRADE_MODE == "LIVE" and (len(executed_orders) > 0 or len(reserved_orders) > 0):
                    should_save = True
                if state.get("reverse_mode", {}).get("active"):
                    should_save = True
                if should_save:
                    tb.save_state(symbol, state)

                if tb.TRADE_MODE == "DRY":
                    print("\n💡 DRY 모드로 실행되었습니다.")
                    print("   실제 주문은 실행되지 않았으며, 주문 정보만 출력되었습니다.")
                    print(f"   총 {len(orders)}개 주문이 처리되었습니다.")
                    print("\n   실제 주문을 하려면 환경변수 또는 .env 파일에서 TRADE_MODE=LIVE 로 설정하세요.")
                else:
                    print("\n✓ LIVE 모드로 실행되었습니다.")
                    print(f"   총 {len(orders)}개 주문 중:")
                    print(f"   - 주문 성공: {len(executed_orders)}개")
                    print(f"   - 예약 접수: {len(reserved_orders)}개")
                    print(f"   - 실패:     {len(failed_orders)}개")

                if executed_orders:
                    print("\n[성공한 주문]")
                    for order in executed_orders:
                        print(f"  ✓ {order['comment']}: 주문번호 {order['order_id']} (시각: {order['order_time']})")

                if reserved_orders:
                    print("\n[예약 주문] - 다음 정규장 시작 시 체결됩니다")
                    for order in reserved_orders:
                        print(
                            f"  📋 {order['comment']}: 예약번호 {order['order_id']} "
                            f"(접수일자: {order['order_time']})"
                        )

                if failed_orders:
                    print("\n[실패한 주문]")
                    for order in failed_orders:
                        print(f"  ✗ {order['comment']}: {order['error']}")

                return {
                    "symbol": symbol,
                    "executed": len(executed_orders),
                    "reserved": len(reserved_orders),
                    "failed": len(failed_orders),
                    "errors": [order["error"] for order in failed_orders],
                    "transmitted_orders": list(orders),
                }

            result = _run_orders()
        except Exception as error:
            _write_outcome_record(
                approval, "execution-failed", str(error), now=effective_now
            )
            raise
        # 부분 실패(일부 주문 미접수/불확실)도 성공으로 기록하지 않습니다.
        if int(result.get("failed", 0) or 0) > 0:
            reason = " / ".join(result.get("errors") or []) or "일부 주문 실패"
            result["outcome"] = "execution-failed"
            _write_outcome_record(approval, "execution-failed", reason, now=effective_now)
            return result
        # 깨끗한 실행 — VERIFY 가 성공으로 판정할 수 있게 **명시적** 기록을 남깁니다.
        result["outcome"] = "executed"
        _write_outcome_record(
            approval,
            "executed",
            f"orders={len(result.get('transmitted_orders') or approval.get('orders') or [])}",
            now=effective_now,
        )
        return result


def _plan_from_approval(approval):
    from pipeline import OrderPlan

    return OrderPlan(
        plan_id=approval.get("plan_id", ""),
        symbol=approval.get("symbol", ""),
        exchange=approval.get("exchange", ""),
        session=approval.get("session", ""),
        orders=list(approval.get("orders") or []),
        state_hash=approval.get("state_hash", ""),
        plan_hash=approval.get("plan_hash", ""),
        settlement_run_id=approval.get("settlement_run_id", ""),
        created_at_utc=approval.get("created_at_utc", ""),
        expires_at_utc=approval.get("expires_at_utc", ""),
        state_patch=dict(approval.get("state_patch") or {}),
        evidence_id=approval.get("evidence_id", ""),
        broker=approval.get("broker", ""),
        broker_mode=approval.get("broker_mode", ""),
        account_id=approval.get("account_id", ""),
        account_display=approval.get("account_display", ""),
        tolerance_policy=dict(approval.get("tolerance_policy") or {}),
    )
