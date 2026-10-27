"""
자동매매 봇 메인 실행 파일

이 프로그램은 다음 작업을 순서대로 수행합니다:
1. 환경변수에서 설정값을 읽어옵니다 (.env 파일)
2. 전략 함수를 실행하여 주문 목록을 생성하고 출력합니다
3. 생성된 주문을 실행합니다

프로그램 실행 중 발생하는 모든 에러는 catch되어 출력됩니다.
"""

import json
import os
import sys
import time
from copy import deepcopy

sys.path.append("src")

from datetime import datetime
from zoneinfo import ZoneInfo

from config import SYMBOLS, TRADE_MODE, COMMISSION_RATE, REINVEST, BROKER, BROKER_MODE, LS_DEMO_BYPASS_BUGS, ORDER_HISTORY_VERBOSE, REPAIR_ACTION
from broker import create_broker
from broker.base import Broker, OrderResult, OrderNotAcceptedError
from broker.market_utils import get_kst_now, is_us_dst, is_us_trading_day
from strategy import 무한매수법_V4, adjust_price_to_tick
from state import FILL_MARKER_VERSION, log_salt_hex, mask_odno, load_state, save_state, update_T_from_history, compute_position_from_history, register_order_meta_in_state, canonical_state_hash, reset_settlement_ledger, record_settlement_t_adjustment, _order_real_kst_date, _order_us_session_date, _fill_amount, _has_unconfirmed_amount_fills
from notifier import notify


class SymbolScopedError(RuntimeError):
    """해당 종목만 중단하고 다음 종목으로 계속 진행하는 예외입니다.

    브로커 불확실 실패(네트워크 오류, 주문번호 누락, 접수 응답 없음)로
    주문 fence를 유지해야 하는 경우에 사용합니다. main()은 이 예외를 받으면
    로그/알림만 남기고 다음 종목으로 계속 진행합니다.
    """


class GlobalScopedError(RuntimeError):
    """공통 장애로 전체 실행을 중단하는 예외입니다.

    state 파일 저장 실패 등 모든 종목에 영향을 주는 공통 장애에 사용합니다.
    main()은 이 예외를 받으면 치명 에러로 처리해 전체 실행을 중단합니다.
    """


STATE_DIAGNOSTIC_ONLY = os.getenv("STATE_DIAGNOSTIC_ONLY", "").strip().lower() == "true"
STATE_REPAIR_ONLY = os.getenv("STATE_REPAIR_ONLY", "").strip().lower() == "true"
STATE_CLEAR_FENCE_ONLY = os.getenv("STATE_CLEAR_FENCE_ONLY", "").strip().lower() == "true"
STATE_REVERSE_AUDIT_ONLY = os.getenv("STATE_REVERSE_AUDIT_ONLY", "").strip().lower() == "true"
STATE_REVERSE_RECONCILE_ONLY = os.getenv("STATE_REVERSE_RECONCILE_ONLY", "").strip().lower() == "true"
STATE_NET_INVESTED_REPAIR_ONLY = os.getenv("STATE_NET_INVESTED_REPAIR_ONLY", "").strip().lower() == "true"
STATE_ASSUME_REVERSE_EXPIRY_ONLY = os.getenv("STATE_ASSUME_REVERSE_EXPIRY_ONLY", "").strip().lower() == "true"
STATE_SET_CYCLE_START_ONLY = os.getenv("STATE_SET_CYCLE_START_ONLY", "").strip().lower() == "true"


def generate_cycle_report(
    symbol,
    order_history,
    cycle_start_date,
    cycle_end_date,
    seed,
    commission_rate,
    cycle_orders=None,
    liquidation_event=None,
):
    """
    **종료된 한 사이클 구간**의 수익 리포트를 생성합니다.

    전체 이력이 아니라 `[cycle_start_date, cycle_end_date]` **닫힌 구간**의
    체결만 집계합니다 (ord_dt 기준, 양 끝 포함). 사이클 종료 후처리에서
    호출되며 state 에 의존하지 않습니다 — 구간은 호출자가 명시합니다.

    Parameters:
        symbol (str): 종목 코드
        order_history (list): get_overseas_order_history()의 반환값
        cycle_start_date (str): 사이클 시작일 (YYYY-MM-DD 또는 YYYYMMDD)
        cycle_end_date (str): 사이클 종료일 (YYYY-MM-DD 또는 YYYYMMDD)
        seed (float): 이번 사이클에 사용된 시드 금액 (달러)
        commission_rate (float): 매매 수수료율 (예: 0.0025 = 0.25%)

    Returns:
        dict: 리포트 결과
    """
    start_yyyymmdd = str(cycle_start_date or "").replace("-", "")
    end_yyyymmdd = str(cycle_end_date or "").replace("-", "")
    if not end_yyyymmdd:
        end_yyyymmdd = datetime.now().strftime("%Y%m%d")

    if cycle_orders is not None:
        # 청산 이벤트 경계로 이미 잘라낸 체결 목록입니다. 같은 날 새 사이클
        # 매수가 섞이지 않도록 날짜 필터를 다시 적용하지 않습니다.
        selected_orders = []
        for order in cycle_orders:
            try:
                filled = int(float(order.get("ft_ccld_qty", "0") or 0))
            except (TypeError, ValueError):
                continue
            if filled > 0:
                selected_orders.append(order)
        cycle_orders = selected_orders
    else:
        cycle_orders = []
        for order in order_history or []:
            try:
                filled = int(float(order.get("ft_ccld_qty", "0") or 0))
            except (TypeError, ValueError):
                continue
            if filled <= 0:
                continue
            ord_dt = str(order.get("ord_dt", "") or "").replace("-", "")[:8]
            if not ord_dt:
                continue
            if start_yyyymmdd and ord_dt < start_yyyymmdd:
                continue
            if ord_dt > end_yyyymmdd:
                continue
            cycle_orders.append(order)

    total_buy_amount = 0.0
    total_sell_amount = 0.0
    buy_count = 0
    sell_count = 0

    for order in cycle_orders:
        qty = float(order.get("ft_ccld_qty", "0"))
        price = float(order.get("ft_ccld_unpr3", "0"))
        amount = qty * price

        if order.get("sll_buy_dvsn_cd_name") == "매수":
            total_buy_amount += amount
            buy_count += 1
        elif order.get("sll_buy_dvsn_cd_name") == "매도":
            total_sell_amount += amount
            sell_count += 1

    # 수수료는 매수와 매도 양방향 모두 부과됩니다
    estimated_commission = (total_buy_amount + total_sell_amount) * commission_rate
    net_profit = total_sell_amount - total_buy_amount - estimated_commission

    operational_return_pct = None
    if total_buy_amount > 0:
        operational_return_pct = (net_profit / total_buy_amount) * 100

    seed_return_pct = None
    if seed > 0:
        seed_return_pct = (net_profit / seed) * 100

    next_cycle_seed = (seed + net_profit) if seed > 0 else None

    def _display_date(value, fallback):
        value = str(value or "")
        if len(value.replace("-", "")) == 8:
            ymd = value.replace("-", "")
            return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        return value or fallback

    return {
        "symbol": symbol,
        "cycle_start_date": _display_date(cycle_start_date, "(기록 없음)"),
        "cycle_end_date": _display_date(end_yyyymmdd, datetime.now().strftime("%Y-%m-%d")),
        "total_buy_amount": total_buy_amount,
        "total_sell_amount": total_sell_amount,
        "estimated_commission": estimated_commission,
        "commission_rate_pct": commission_rate * 100,
        "net_profit": net_profit,
        "operational_return_pct": operational_return_pct,
        "seed_return_pct": seed_return_pct,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "seed": seed,
        "next_cycle_seed": next_cycle_seed,
        # 청산 이벤트 경계 (같은 날 재매수 제외 근거)
        "liquidation_event": liquidation_event,
    }


def _order_event_key(order):
    """정렬·경계 식별용 (시각, 주문번호) 키. 시각이 없으면 ord_dt+ord_tmd."""
    odno = str(order.get("odno", "") or "")
    ts = str(order.get("ord_datetime_utc", "") or "")
    if not ts:
        ord_dt = str(order.get("ord_dt", "") or "").replace("-", "")[:8]
        ord_tmd = str(order.get("ord_tmd", "") or "").zfill(6)
        ts = f"{ord_dt}{ord_tmd}"
    return (ts, odno)


def _liquidation_boundary_orders(order_history, cycle_start_date):
    """사이클 시작 이후 **첫 전량매도(청산) 이벤트**까지의 체결 목록을 반환합니다.

    날짜 구간이 아니라 청산 체결의 시각·주문번호를 경계로 사용하므로,
    청산 후 같은 날 발생한 새 사이클 매수는 리포트에서 제외됩니다.

    Returns:
        tuple: (cycle_orders, event) — event 는 청산 주문 dict 또는 None
    """
    start = str(cycle_start_date or "").replace("-", "")
    rows = []
    for index, order in enumerate(order_history or []):
        try:
            filled = int(float(order.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            continue
        if filled <= 0:
            continue
        ord_dt = str(order.get("ord_dt", "") or "").replace("-", "")[:8]
        if not ord_dt or (start and ord_dt < start):
            continue
        side = order.get("sll_buy_dvsn_cd_name", "")
        if side not in ("매수", "매도"):
            continue
        rows.append((_order_event_key(order), index, side, filled, order))

    rows.sort(key=lambda item: (item[0], item[1]))
    net_qty = 0
    selected = []
    for _key, _index, side, qty, order in rows:
        selected.append(order)
        if side == "매수":
            net_qty += qty
        else:
            before = net_qty
            net_qty = max(0, net_qty - qty)
            if before > 0 and net_qty == 0:
                # 이 체결이 청산 이벤트입니다. 이후 행(같은 날 재매수 포함)은 제외.
                return selected, order
    return selected, None


def _cycle_end_date_from_history(order_history, cycle_start_date):
    """청산 이벤트의 ord_dt(표시용)를 반환합니다. 없으면 마지막 체결일 폴백."""
    selected, event = _liquidation_boundary_orders(order_history, cycle_start_date)
    chosen = ""
    if event is not None:
        chosen = str(event.get("ord_dt", "") or "").replace("-", "")[:8]
    if not chosen and selected:
        chosen = str(selected[-1].get("ord_dt", "") or "").replace("-", "")[:8]
    if chosen:
        return f"{chosen[:4]}-{chosen[4:6]}-{chosen[6:8]}"
    return datetime.now().strftime("%Y-%m-%d")


def finalize_completed_cycle(
    symbol,
    state,
    order_history,
    seed,
    commission_rate,
    report=None,
    require_settled=True,
):
    """**이미 정산이 끝난** 사이클의 후처리만 수행합니다 (종료 전용 상태머신).

    호출 시점 불변조건:
      - T == 0, net_invested == 0, cycle_start_date == ""
      - settlement_ledger.entries == [] (reset_settlement_ledger(state, 0.0) 선행)

    이 함수는 리포트를 만들고 `next_cycle_seed` 만 반환합니다.
    T / net_invested / ledger / apply marker / position_qty 는 건드리지 않습니다.

    Parameters:
        report (dict|None): 사이클 구간 힌트. `cycle_start_date`/`cycle_end_date` 를
            담을 수 있습니다. 없으면 state 의 `_completed_cycle_start` 를 사용합니다.

    Returns:
        dict: {"next_cycle_seed": float, "report": dict}
    """
    hint = report if isinstance(report, dict) else {}
    cycle_start = (
        hint.get("cycle_start_date")
        or state.pop("_completed_cycle_start", "")
        or ""
    )
    boundary_orders, liquidation_event = _liquidation_boundary_orders(
        order_history, cycle_start
    )
    if liquidation_event is not None:
        raw_end = str(liquidation_event.get("ord_dt", "") or "").replace("-", "")[:8]
        cycle_end = (
            f"{raw_end[:4]}-{raw_end[4:6]}-{raw_end[6:8]}"
            if raw_end else hint.get("cycle_end_date")
        )
    else:
        cycle_end = hint.get("cycle_end_date") or _cycle_end_date_from_history(
            order_history, cycle_start
        )

    # 불변조건 — 위반된 상태로 후처리하면 다른 사이클의 리포트/시드를 만듭니다.
    # (같은 실행에서 새 사이클 매수가 시작된 경우에는 require_settled=False 로
    #  리포트만 만들고 새 사이클 상태는 그대로 보존합니다)
    if require_settled:
        if float(state.get("T", 0.0) or 0.0) != 0.0:
            raise ValueError("finalize_completed_cycle: T 가 0이 아닙니다")
        if float(state.get("net_invested", 0.0) or 0.0) != 0.0:
            raise ValueError("finalize_completed_cycle: net_invested 가 0이 아닙니다")
        if state.get("cycle_start_date", ""):
            raise ValueError("finalize_completed_cycle: cycle_start_date 가 비어있지 않습니다")
        ledger = state.get("settlement_ledger") or {}
        if ledger.get("entries"):
            raise ValueError("finalize_completed_cycle: settlement_ledger.entries 가 비어있지 않습니다")

    cycle_report = generate_cycle_report(
        symbol=symbol,
        order_history=order_history,
        cycle_start_date=cycle_start,
        cycle_end_date=cycle_end,
        seed=seed,
        commission_rate=commission_rate,
        cycle_orders=boundary_orders,
        liquidation_event=liquidation_event,
    )
    next_seed = cycle_report.get("next_cycle_seed")
    return {
        "next_cycle_seed": float(next_seed) if next_seed is not None else 0.0,
        "report": cycle_report,
    }


def format_cycle_report_message(report):
    """
    사이클 리포트 dict를 텔레그램 메시지 문자열로 변환합니다.

    Parameters:
        report (dict): generate_cycle_report()의 반환값

    Returns:
        str: 텔레그램에 전송할 메시지 문자열
    """
    symbol = report["symbol"]
    start = report["cycle_start_date"]
    end = report["cycle_end_date"]
    buy_amt = report["total_buy_amount"]
    sell_amt = report["total_sell_amount"]
    commission = report["estimated_commission"]
    commission_pct = report["commission_rate_pct"]
    net = report["net_profit"]
    buy_cnt = report["buy_count"]
    sell_cnt = report["sell_count"]
    seed = report["seed"]
    next_seed = report["next_cycle_seed"]
    op_return = report["operational_return_pct"]
    seed_return = report["seed_return_pct"]

    profit_sign = "+" if net >= 0 else ""
    lines = [
        f"🏁 사이클 종료 — {symbol}",
        "",
        f"기간: {start} ~ {end}",
        f"매수 {buy_cnt}회 / 매도 {sell_cnt}회",
        "",
        f"총 매수금액:  ${buy_amt:>10,.2f}",
        f"총 매도금액:  ${sell_amt:>10,.2f}",
        f"추정 수수료:  ${commission:>10,.2f}  ({commission_pct:.2f}% × 매수·매도)",
        "─" * 36,
        f"순수익금:     ${profit_sign}{net:>9,.2f}",
    ]

    if op_return is not None:
        lines.append(f"운용 수익률:  {profit_sign}{op_return:.2f}%")

    if seed > 0 and seed_return is not None:
        lines.append(f"시드 대비:    {profit_sign}{seed_return:.2f}%  (시드 ${seed:,.0f})")

    if next_seed is not None:
        reinvest_label = "복리 적용" if REINVEST else "참고값"
        lines.append("")
        lines.append(f"다음 사이클 시드: ${next_seed:,.2f}  ({reinvest_label})")

    return "\n".join(lines)


def _save_settlement_state(symbol: str, state: dict) -> bool:
    """
    정산 결과를 저장합니다. **보유수량 기준이 없으면 저장하지 않습니다.**

    매도 분류에 필요한 보유수량을 브로커 잔고나 저장된 보유수량에서 확보하지 못하면
    쿼터매도/목표매도 구분을 신뢰할 수 없습니다 (T 가 과소 계산될 수 있음).
    이 경우 T·워터마크·반영 마커를 먼저 확정하면 다음 실행에서 재처리될 수 없어
    잘못된 값이 고정됩니다. → 계산 결과를 버리고 확인을 요청합니다.

    Returns:
        bool: 저장했으면 True
    Raises:
        SymbolScopedError: 기준 미확보로 저장하지 못한 경우
    """
    basis_unknown = state.pop("_position_basis_unknown", False)
    # 사유 키도 **항상** 함께 소비합니다 (남으면 .state.json 으로 새는 것을 방지).
    basis_reason = state.pop("_position_basis_unknown_reason", "")
    skipped_unverified = state.pop("_unverified_skipped_odnos", [])
    skipped_audit = state.pop("_unverified_skipped_audit", [])
    if skipped_unverified:
        # 공개 로그에는 실제 주문번호 대신 해시만 남깁니다 (원문은 Evidence 에만 기록)
        hashes = [e.get("odno_hash", "") for e in skipped_audit][:5]
        print(
            f"[정보] {symbol} 미반영 증명 불가 과거 주문 {len(skipped_unverified)}건은 "
            f"반영하지 않고 건너뛰었습니다 ({', '.join(hashes)})"
        )
    if basis_unknown:
        msg = (
            f"[보류] {symbol} 보유수량 기준을 확보하지 못해 정산 결과와 신규 주문을 "
            f"보류합니다. 사유: {basis_reason or '잔고조회실패 + 저장보유수량없음'} "
            f"(브로커 잔고 조회 실패 + 저장 보유수량 없음) — 확인 후 재실행하세요."
        )
        print(msg)
        notify(msg, urgent=True)
        raise SymbolScopedError(
            f"{symbol} 보유수량 기준 미확보로 정산 저장과 신규 주문을 보류합니다. "
            f"브로커 잔고 조회와 state.position_qty 를 확인하세요."
        )
    pending_amount_odnos = list(state.get("_pending_amount_odnos") or [])
    if pending_amount_odnos:
        pending_marker_odnos = [str(o) for o in (state.get("_pending_marker_odnos") or [])]
        amount_only = [o for o in pending_amount_odnos if o not in pending_marker_odnos]
        parts = []
        if amount_only:
            parts.append(
                f"체결금액 미확정 주문 {len(amount_only)}건({mask_odno(amount_only[:5])[1:-1]})"
            )
        if pending_marker_odnos:
            parts.append(
                f"마커 누락·무효 주문 {len(pending_marker_odnos)}건({mask_odno(pending_marker_odnos[:5])[1:-1]})"
            )
        hold_detail = " + ".join(parts) if parts else f"주문 {len(pending_amount_odnos)}건"
        followup = []
        if amount_only:
            followup.append("금액 확정 후 재실행하세요")
        if pending_marker_odnos:
            followup.append("마커 보정 후 재실행하세요")
        msg = (
            f"[보류] {symbol} {hold_detail}이 있어 "
            f"T/순투입/마커 저장과 신규 주문을 보류합니다. {' / '.join(followup)}."
        )
        print(msg)
        notify(msg, urgent=True)
        raise SymbolScopedError(msg)
    if state.get("_settlement_hold"):
        reason = state.get("_settlement_hold_reason", "적용 순서 근거 부족")
        msg = (
            f"[보류] {symbol} 정산 근거가 부족해 T/순투입/마커 저장과 신규 주문을 "
            f"보류합니다: {reason}"
        )
        print(msg)
        notify(msg, urgent=True)
        raise SymbolScopedError(msg)
    return True


def build_settlement_evidence(
    symbol,
    state,
    order_history,
    *,
    mode,
    run_id="",
    balance_qty=None,
    before=None,
    settlement_mode=None,
    max_t=None,
):
    """정산 근거(evidence v2)를 만듭니다 — 운영/검증/테스트가 **같은 함수**를 씁니다.

    `before` 는 정산 직전 스냅샷입니다:
        T, net_invested, ledger, sell_t_effects, position_qty, watermark,
        orders_meta, applied markers
    검증기는 이 스냅샷 + 원시 체결만으로 기대 원장을 독립 재구성합니다.
    """
    before = before or {}
    ledger_before = deepcopy(before.get("settlement_ledger", {}) or {})
    effects_before = deepcopy(before.get("sell_t_effects", {}) or {})
    meta_before = deepcopy(before.get("orders_meta", {}) or {})
    evidence = {
        "symbol": symbol,
        "run_id": run_id,
        "retry_num": os.environ.get("RETRY_NUM", ""),
        "mode": mode,
        "broker": os.environ.get("BROKER", BROKER),
        "broker_mode": os.environ.get("BROKER_MODE", BROKER_MODE),
        "code_version": os.environ.get("CODE_VERSION") or os.environ.get("GIT_SHA", ""),
        "max_t": max_t,
        "evidence_version": 2,
        "settlement_mode": (
            settlement_mode
            or state.get("_settlement_mode")
            or ("full_reinference" if not before.get("last_updated") else "incremental")
        ),
        "balance_qty": balance_qty,
        "position_basis": state.get("position_qty"),
        "position_qty_before": before.get("position_qty"),
        "basis_unknown": bool(
            state.get("_position_basis_unknown") or state.get("_settlement_hold")
        ),
        "hold_reason": state.get("_settlement_hold_reason", ""),
        "pending_amount_odnos": list(state.get("_pending_amount_odnos") or []),
        "pending_marker_odnos": list(state.get("_pending_marker_odnos") or []),
        "T_before": before.get("T", 0.0),
        "net_invested_before": before.get("net_invested", 0.0),
        "T_after": float(state.get("T", 0.0)),
        "net_invested_after": float(state.get("net_invested", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "watermark_before": before.get("last_updated", ""),
        "last_processed_ordno_before": before.get("last_processed_ordno", ""),
        "sell_ratios": state.get("_settlement_sell_ratios", []),
        "sell_t_effects_before": effects_before,
        "sell_t_effects": deepcopy(state.get("sell_t_effects", {}) or {}),
        "orders_meta_before": meta_before,
        "orders_meta_after": deepcopy(state.get("orders_meta", {}) or {}),
        "ledger_before": ledger_before,
        "ledger_after": deepcopy(state.get("settlement_ledger", {}) or {}),
        "applied_seq": [
            o.get("ledger_seq")
            for o in state.get("_settlement_deltas", [])
            if o.get("ledger_seq") is not None
        ],
        "orders": deepcopy(state.get("_settlement_deltas", [])),
        "fills": [],
        "skipped_fills": [],
        # 미반영 증명 불가로 건너뛴 과거(레거시 0 마커) 주문 — 감사용 (원문 odno + 해시)
        "skipped_unverified": deepcopy(state.get("_unverified_skipped_audit") or []),
        # 공개 로그의 가명 식별자(ord#…)를 원문 주문번호로 되돌려 대조하기 위한 실행별 salt.
        # 비공개(암호화) Evidence 에만 기록합니다 — 공개 로그에는 절대 출력하지 않습니다.
        "log_salt": log_salt_hex(),
    }
    for row in order_history or []:
        try:
            filled = int(float(row.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            continue
        if filled <= 0:
            # 금액 계산 불가로 브로커 경계에서 제외된 체결도 근거에 남깁니다.
            skipped_qty = 0
            try:
                skipped_qty = int(float(row.get("_skipped_fill_qty", "0") or 0))
            except (TypeError, ValueError):
                skipped_qty = 0
            if skipped_qty > 0:
                evidence["skipped_fills"].append({
                    "odno": str(row.get("odno", "")),
                    "qty": skipped_qty,
                    "reason": "amount_unavailable",
                })
            continue
        odno = str(row.get("odno", ""))
        row_meta = meta_before.get(odno) or {}
        fill_amount, amount_estimated = _fill_amount(row)
        evidence["fills"].append({
            "odno": odno,
            "side": row.get("sll_buy_dvsn_cd_name", ""),
            "qty": filled,
            "amount": fill_amount,
            "amount_confirmed": not bool(amount_estimated),
            "amount_source": row.get("_ft_ccld_amt_source", ""),
            "amount_estimated": bool(row.get("_ft_ccld_amt_estimated", False)),
            "ts": row.get("ord_datetime_utc", ""),
            "t_target": float(row_meta.get("t_target", 0.0) or 0.0),
            "total_qty": int(row_meta.get("total_qty", 0) or 0),
            "is_additional": bool(row_meta.get("is_additional", False)),
            "reverse_action": row_meta.get("reverse_action", ""),
            "reverse_base_t": float(row_meta.get("reverse_base_t", 0.0) or 0.0),
            "reverse_t_factor": float(
                row_meta.get("reverse_t_factor", 1.0)
                if row_meta.get("reverse_t_factor") is not None else 1.0
            ),
            "reverse_t_target": float(row_meta.get("reverse_t_target", 0.0) or 0.0),
            "processed_filled_qty": int(row_meta.get("processed_filled_qty", 0) or 0),
            "processed_filled_amount": float(
                row_meta.get("processed_filled_amount", 0.0) or 0.0
            ),
            "applied_fill_fraction": min(
                1.0, float(row_meta.get("applied_fill_fraction", 0.0) or 0.0)
            ),
        })
    return evidence


def run_one_symbol(broker: Broker, symbol_config):
    """
    단일 종목에 대해 전략을 실행하고 주문을 넣는 함수입니다.

    Parameters:
        symbol_config (dict): 종목별 설정
            - symbol (str): 종목 코드 (예: "TQQQ")
            - exchange (str): 거래소 코드 (예: "NAS")
            - splits (int): 분할 수
            - symbol_type (str): 종목 타입 (예: "TQQQ", "SOXL")
            - seed (float): 투입 시드 금액 (달러, 필수)
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    splits = symbol_config["splits"]
    symbol_type = symbol_config["symbol_type"]
    seed = symbol_config["seed"]
    additional_loc_levels = symbol_config.get("additional_loc_levels", 3)

    print(f"\n{'=' * 60}")
    print(f"종목 처리 시작: {symbol} ({exchange})")
    print(f"{'=' * 60}")

    # ── 휴장일 체크 ───────────────────────────────────────────
    if not broker.is_trading_day():
        today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %A")
        print(f"  📅 오늘({today_str})은 미국 증시 휴장일입니다. 이 종목을 건너<skip>니다.")
        return

    # ── Step 1: T값 로드 및 어제 체결 반영 ──────────────────────
    print("\n[Step 1] T값 로드 중...")

    state = load_state(symbol)

    # _state_unavailable 플래그만 보관 — 즉시 차단하지 않고 아래에서
    # order_history/balance 조회 후 스마트 부트스트랩 여부를 판단합니다.
    state_unavailable = state.get("_state_unavailable")

    # 첫 실행 워터마크 오염 방지 안내: state 없음 + last_updated 없음 + LIVE면
    # 차단 없이 경고 1줄만 출력합니다 (notify 호출 금지, exit 금지).
    if state_unavailable and TRADE_MODE == "LIVE" and not state.get("last_updated"):
        print("[권장] 첫 실행으로 보입니다. DRY 1회 실행 후 LIVE 전환을 권장합니다.")

    unresolved_zero_invested = (
        state.get("net_invested_status", "unresolved") != "valid"
        and float(state.get("net_invested", 0.0) or 0.0) <= 0
    )
    if unresolved_zero_invested and TRADE_MODE == "LIVE":
        notify(
            f"[상태] {symbol} net_invested 미확정(unresolved) 및 0 이하 → 신규 전략 주문을 보류합니다.\n"
            "STATE_REVERSE_AUDIT_ONLY/reconcile로 상태를 확인하세요."
        )

    force_t = symbol_config.get("force_t")
    max_t = symbol_config.get("max_t")

    from config import FORCE_T_REINFERENCE as _force_reinference, TRADE_MODE as _trade_mode
    if _force_reinference:
        if _trade_mode == "LIVE":
            print("[T 보정] FORCE_T_REINFERENCE=true 이지만 TRADE_MODE=LIVE입니다 — 주문 방지를 위해 DRY로 전환합니다.")
            import config as _cfg
            _cfg.TRADE_MODE = "DRY"
        if state.get("last_updated"):
            print(f"[T 보정] FORCE_T_REINFERENCE=true → last_updated 초기화 (전체 이력 재추정)")
            state["last_updated"] = ""

    # 주문 이력 조회 기간을 상황에 맞게 계산합니다
    cycle_start_date = state.get("cycle_start_date", "")
    last_updated = state.get("last_updated", "")

    if not last_updated:
        # 초기 상태(처음 실행 또는 업그레이드 직후): 이전 이력에서 T를 추정하기 위해 넉넉하게 조회합니다
        # 5~6페이지 × 20건 = 약 100~120건을 확보합니다
        history_days = 90
    elif cycle_start_date:
        # 사이클 시작일 기준으로 전체 사이클 이력을 조회합니다
        try:
            start_dt = datetime.strptime(cycle_start_date, "%Y-%m-%d")
            days_since_start = (datetime.now() - start_dt).days + 5
            history_days = max(days_since_start, 30)
        except ValueError:
            history_days = 30
    else:
        history_days = 30

    # DRY 모드 또는 ORDER_HISTORY_VERBOSE=true 시 주문 이력 요약을 로그에 출력합니다.
    order_history = broker.get_order_history(
        symbol,
        exchange,
        days=history_days,
        verbose=(TRADE_MODE == "DRY" or ORDER_HISTORY_VERBOSE),
    )

    # 잔고를 미리 조회하여 update_T_from_history의 잔고 교차 검증에 사용합니다.
    # 이력이 비었을 때 T 리셋 여부를 잔고로 판별 — 보유 중이면 리셋 보류, 잔고 0이면
    # 잘못된 state 복구를 위해 리셋 fallback, 조회 실패(None)면 보수적 보류.
    # 이 결과(live_qty 등)는 아래 상태-잔고 교차검증에서도 그대로 재사용합니다.
    try:
        live_balance = broker.get_balance(symbol, exchange)
        live_qty = live_balance.quantity if live_balance else 0
        live_avg = live_balance.avg_price if live_balance else 0.0
    except Exception as e:
        # LS 모의투자 IGW40014 버그 우회: 예외 발생 시에도 보수적 T 유지
        if LS_DEMO_BYPASS_BUGS:
            print(f"[상태 검증] 브로커 잔고 조회 실패(T 갱신용) - LS 모의투자 버그 우회: {e}")
        else:
            print(f"[상태 검증] 브로커 잔고 조회 실패(T 갱신용): {e}")
        live_balance = None
        live_qty = None
        live_avg = None

    # ── 첫 실행 부트스트랩 (state 없음 + 보유 0 + 이력 0) ──
    # state 파일이 없/손상/미등록이어도, 실제 보유와 이력이 모두 비어 있으면
    # 첫 실행으로 간주해 T=0 LIVE 진행을 허용합니다. 그 외는 기존처럼 차단합니다.
    if TRADE_MODE == "LIVE" and state_unavailable:
        history_filled = sum(
            1 for o in order_history
            if int(float(o.get("ft_ccld_qty", "0") or 0)) > 0
        )
        if history_filled == 0 and live_qty in (0, None):
            print(f"[초기 부트스트랩] {symbol} 첫 실행 → T=0 LIVE 허용")
        else:
            raise RuntimeError(
                f"{symbol} 상태 파일을 확인할 수 없어 LIVE 주문을 중단합니다: "
                f"{state_unavailable}"
            )
    # 정산 전 워터마크 (이번 정산이 처리한 구간의 시작점) — 검증기가 사용합니다.
    watermark_before = str(state.get("last_updated", "") or "")
    T_before = float(state.get("T", 0.0))
    net_invested_before = float(state.get("net_invested", 0.0))
    # 정산 전 원장 스냅샷 — 검증기가 "적용 전/후"를 독립 재생할 수 있게 남깁니다.
    ledger_before = deepcopy(state.get("settlement_ledger", {}))
    sell_t_effects_before = deepcopy(state.get("sell_t_effects", {}))
    position_qty_before = state.get("position_qty")
    orders_meta_before = deepcopy(state.get("orders_meta", {}))
    before_snapshot = {
        "T": T_before,
        "net_invested": net_invested_before,
        "settlement_ledger": ledger_before,
        "sell_t_effects": sell_t_effects_before,
        "position_qty": position_qty_before,
        "last_updated": watermark_before,
        "last_processed_ordno": state.get("last_processed_ordno", ""),
        "orders_meta": orders_meta_before,
    }

    state = update_T_from_history(symbol, state, order_history, balance_qty=live_qty)

    # 금액 미확정 체결 게이트 — update 경로가 놓친 경우에도 저장/주문을 막습니다.
    pending_amount_odnos = list(state.get("_pending_amount_odnos") or [])
    if not pending_amount_odnos:
        pending_amount_odnos = _has_unconfirmed_amount_fills(state, order_history)
    if pending_amount_odnos:
        state["_pending_amount_odnos"] = pending_amount_odnos
        state.setdefault("_settlement_hold", True)
        state.setdefault(
            "_settlement_hold_reason",
            f"체결금액 미확정 주문 {len(pending_amount_odnos)}건",
        )

    # ── 정산 근거 기록 (자동 검증이 독립 검산에 사용) ────────────────
    # 이 근거가 없으면 검증기는 "검증 불가"로 판단합니다.
    settlement_evidence = build_settlement_evidence(
        symbol,
        state,
        order_history,
        mode=TRADE_MODE,
        run_id=os.environ.get("RUN_ID", ""),
        balance_qty=live_qty,
        before=before_snapshot,
        max_t=max_t,
    )
    try:
        stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
        with open(f"settlement-{symbol}-{stamp}.json", "w", encoding="utf-8") as fh:
            json.dump(settlement_evidence, fh, ensure_ascii=False, indent=2)
    except OSError as evidence_error:
        print(f"[경고] 정산 근거 기록 실패: {str(evidence_error)[:100]}")

    # 금액 미확정/정산 근거 부족은 evidence 를 남긴 뒤 **즉시** 저장·주문을 보류합니다.
    if state.get("_pending_amount_odnos") or state.get("_settlement_hold"):
        _save_settlement_state(symbol, state)

    # ── 포지션 기반 T 추정 진단 (소액 시드 오추정 시 참고용) ──
    infer_diag = state.get("_inference_diagnostic", {})
    if infer_diag.get("small_seed_days", 0) > 0 and live_qty is not None and live_avg is not None and live_qty > 0 and live_avg > 0:
        _used_seed = state.get("effective_seed", 0.0) or seed
        if _used_seed > 0:
            T_position = round((live_qty * live_avg * splits) / _used_seed, 4)
            T_position = min(T_position, max_t if max_t is not None else splits)
            print(f"  → [참고] 포지션 기반 T 추정: {T_position}")
            print(f"  → 포지션 기반 값으로 설정하려면 {symbol}_FORCE_T={T_position} 환경변수를 추가하고 재실행하세요")

    # ── T값 상한 적용 (MAX_T) ──
    if max_t is not None and state["T"] > max_t:
        print(f"[T 보정] {symbol} T={state['T']} > MAX_T={max_t} → T={max_t}로 조정")
        state["T"] = max_t
        reset_settlement_ledger(state, max_t)

    # ── 사이클 종료 감지 (전체 재추정 경로) ──
    # _infer_T_from_full_history가 전량매도를 감지해 T=0으로 리셋한 경우,
    # 일반 경로(T > 0 조건)는 작동하지 않으므로 여기서 따로 처리합니다.
    completed_cycle_start = state.pop("_completed_cycle_start", None)
    if completed_cycle_start:
        if _has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
            # [DRY READONLY] cycle-end unresolved-reverse save: LIVE only
            if TRADE_MODE == "LIVE":
                save_state(symbol, state)
            return
        print(f"\n{'=' * 60}")
        print(f"[사이클 종료] {symbol} — {completed_cycle_start} ~ 완료")
        print(f"{'=' * 60}")
        _used_seed = state.get("effective_seed", 0.0)
        if not (REINVEST and _used_seed > 0):
            _used_seed = seed
        # 같은 실행에서 새 사이클 매수가 시작됐는지 판별합니다.
        # (T>0 또는 cycle_start_date 설정 = 새 사이클 진행 중)
        new_cycle_started = bool(
            float(state.get("T", 0.0) or 0.0) != 0.0
            or float(state.get("net_invested", 0.0) or 0.0) != 0.0
            or state.get("cycle_start_date", "")
        )
        if new_cycle_started:
            # 새 사이클 상태는 보존하고, 종료된 사이클의 리포트/시드만 갱신합니다.
            state.pop("_completed_cycle_start", None)
            finalized = finalize_completed_cycle(
                symbol=symbol,
                state=state,
                order_history=order_history,
                seed=_used_seed,
                commission_rate=COMMISSION_RATE,
                report={"cycle_start_date": completed_cycle_start},
                require_settled=False,
            )
        else:
            # 정산은 이미 완료된 상태(T=0/net=0)입니다. 원장만 0 기준으로 확정한 뒤
            # **후처리(리포트/시드)** 만 수행합니다.
            state["cycle_start_date"] = ""
            reset_settlement_ledger(state, 0.0)
            finalized = finalize_completed_cycle(
                symbol=symbol,
                state=state,
                order_history=order_history,
                seed=_used_seed,
                commission_rate=COMMISSION_RATE,
                report={"cycle_start_date": completed_cycle_start},
            )
        report = finalized["report"]
        report_message = format_cycle_report_message(report)
        print(f"\n{report_message}")
        notify(report_message)
        # effective_seed 갱신은 리포트 후 **단 1회**.
        if REINVEST and finalized["next_cycle_seed"] > 0:
            state["effective_seed"] = round(finalized["next_cycle_seed"], 2)
            print(f"  복리 재투자: 다음 사이클 시드 = ${state['effective_seed']:.2f}")
        else:
            state["effective_seed"] = 0.0

    # ── 정산 결과 저장 ──────────────────────────────────────────
    # 보유수량 기준을 확보하지 못한 정산은 **저장하지 않고** 신규 주문도 내지 않습니다.
    # (먼저 저장하면 다음 실행에서 재처리될 수 없어 잘못된 값이 고정됩니다)
    _save_settlement_state(symbol, state)

    # [DRY READONLY] T 갱신 후 상태 저장: DRY에서는 절대 저장하지 않습니다.
    if TRADE_MODE == "LIVE":
        save_state(symbol, state)

    # ── 상태-잔고 교차검증 (conservative reconciliation) ──
    try:
        computed = compute_position_from_history(order_history, cycle_start_date=state.get("cycle_start_date", ""))
    except Exception as e:
        print(f"[상태 검증] 이력 기반 포지션 계산 실패: {e}")
        computed = {"net_qty": 0, "avg_price": 0.0}

    comp_qty = int(computed.get("net_qty", 0))
    comp_avg = float(computed.get("avg_price", 0.0))

    if live_qty is not None:
        if live_qty == 0 and comp_qty > 0:
            if _has_unresolved_reverse_orders(state):
                state.setdefault("reverse_mode", {})["reconciliation_only"] = True
                state["reverse_mode"]["active"] = False
                # [DRY READONLY] unresolved-reverse balance=0 save: LIVE only
                if TRADE_MODE == "LIVE":
                    save_state(symbol, state)
                return
            msg = (
                f"[불일치] 이력으로는 보유 {comp_qty}주(평단 ${comp_avg:.2f})로 추정되나, 브로커 잔고는 0입니다."
            )
            if TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 기록·자동보정·중단 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} DRY 모드라 기록/보정 없이 프리뷰를 계속합니다. (LIVE 시 자동 보정 후 중단)")
                notify(msg)
            else:
                print(f"{msg} 보수적 자동 보정: T=0으로 초기화합니다.")
                notify(msg)
                state["balance_mismatch"] = {
                    "computed_net_qty": comp_qty,
                    "computed_avg_price": round(comp_avg, 2),
                    "live_qty": live_qty,
                    "live_avg_price": round(live_avg, 2) if live_avg is not None else None,
                    "note": "auto-corrected-to-zero",
                }
                state["T"] = 0.0
                state["cycle_start_date"] = ""
                state["net_invested"] = 0.0
                state["net_invested_status"] = "valid"
                state["reverse_mode"] = {}
                reset_settlement_ledger(state, 0.0)
                save_state(symbol, state)
                raise RuntimeError(
                    f"{symbol} 이력 포지션과 브로커 잔고가 불일치하여 주문을 중단합니다. "
                    f"history={comp_qty}, broker={live_qty}"
                )

        elif live_qty != comp_qty:
            # 불일치지만 자동 보정하지 않음 — 관리자 확인 필요
            msg = (
                f"[불일치] 이력 net_qty={comp_qty}, 브로커 잔고={live_qty}. 자동 보정하지 않습니다. 확인 필요."
            )
            if TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 상태 기록 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} (DRY 모드라 balance_mismatch를 기록하지 않습니다)")
                notify(msg)
            else:
                print(msg)
                notify(msg)
                state["balance_mismatch"] = {
                    "computed_net_qty": comp_qty,
                    "computed_avg_price": round(comp_avg, 2),
                    "live_qty": live_qty,
                    "live_avg_price": round(live_avg, 2) if live_avg is not None else None,
                    "note": "requires-attention",
                }
                save_state(symbol, state)
                raise RuntimeError(
                    f"{symbol} 이력 포지션과 브로커 잔고가 불일치하여 주문을 중단합니다. "
                    f"history={comp_qty}, broker={live_qty}"
                )

        elif state["T"] == 0 and live_qty > 0:
            # T=0인데 실제 보유가 있음: T 오추정 (소액 시드로 인한 추가매수 오분류)
            msg = (
                f"[경고] {symbol} T=0이지만 브로커 잔고에 {live_qty}주 보유 중입니다. "
                f"T가 실제보다 낮게 추정되었을 수 있습니다. .state.json 에서 직접 확인/수정하세요."
            )
            if TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 상태 기록 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} (DRY 모드라 balance_mismatch를 기록하지 않습니다)")
                notify(msg)
            else:
                print(msg)
                notify(msg)
                state["balance_mismatch"] = {
                    "computed_net_qty": comp_qty,
                    "computed_avg_price": round(comp_avg, 2),
                    "live_qty": live_qty,
                    "live_avg_price": round(live_avg, 2) if live_avg is not None else None,
                    "note": "T-estimation-suspected-low",
                }
                save_state(symbol, state)
                raise RuntimeError(
                    f"{symbol} T=0인데 브로커 잔고 {live_qty}주가 있어 주문을 중단합니다."
                )

        else:
            # 일치하는 경우, 기존 불일치 표시 제거
            if state.get("balance_mismatch"):
                state.pop("balance_mismatch", None)
                # [DRY READONLY] balance_mismatch removal save: LIVE only
                if TRADE_MODE == "LIVE":
                    save_state(symbol, state)

    # ── 주문 fence 복구 (이전 세션 이력 정착 시 자동 해제) ──
    # 주문이력/잔고 reconciliation이 정상 통과한 뒤에만 도달합니다.
    # fence가 있어도 즉시 차단하지 않고, 전일(이전 세션)이면 자동 해제 후 진행합니다.
    if TRADE_MODE == "LIVE":
        _recover_order_fence(state, symbol, live_qty)

    # ── T값 강제 설정 (FORCE_T) ──
    if force_t is not None:
        if _has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
            # TODO [REPAIR MIGRATION] FORCE_T save in DRY: 명시적 보정용으로 유지, REPAIR로 이관 예정.
            # For now kept as-is (FORCE_T is user-initiated state repair).
            # Migrate to STATE_NET_INVESTED_REPAIR_ONLY or similar read-only tool.
            save_state(symbol, state)
            raise RuntimeError("미해결 리버스 주문이 있어 FORCE_T 적용을 중단합니다.")
        old_T = state["T"]
        state["T"] = force_t
        state.pop("balance_mismatch", None)
        state["orders_meta"] = {}
        state["additional_loc_odno"] = []
        state["reverse_mode"] = {}
        reset_settlement_ledger(state, force_t)
        # FORCE_T 이후 이력 조회가 stale last_updated 기준으로 이미 반영된 주문을
        # 다시 가산(이중 가산)하는 것을 방지하기 위해, 이번 RUN에서 조회된 최신
        # 주문 시각으로 last_updated를 갱신합니다. 이력이 없으면 현재 UTC를 사용합니다.
        latest_ord_dt = ""
        for o in order_history:
            odt = o.get("ord_datetime_utc", "")
            if odt and odt > latest_ord_dt:
                latest_ord_dt = odt
        if latest_ord_dt:
            state["last_updated"] = latest_ord_dt
            latest_order = next(
                o for o in order_history
                if o.get("ord_datetime_utc") == latest_ord_dt
            )
            state["last_processed_ordno"] = latest_order.get("odno", "")
        else:
            state["last_updated"] = datetime.now(ZoneInfo("UTC")).isoformat()
            state["last_processed_ordno"] = ""
        if force_t == 0:
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
        print(f"[T 보정] {symbol} FORCE_T={force_t} 적용 (이전 T={old_T}), "
              f"orders_meta/balance_mismatch 초기화, last_updated 갱신")
        # TODO [REPAIR MIGRATION] FORCE_T save in DRY: allowed for explicit correction.
        save_state(symbol, state)

    T = state["T"]
    print(f"  현재 T값: {T}")

    # ── 사이클 종료 사전 감지 (전략 실행 전에) ─────────────────────────
    # live 잔고 우선, 없으면 이력 기반 추정값을 사용합니다.
    # 잔고 조회 실패(None) 시에는 종료 감지를 보류합니다 (SOXL 케이스).
    current_qty = live_qty if live_qty is not None else comp_qty

    if T > 0 and current_qty == 0 and live_qty is None:
        # 잔고 조회 실패 → T 유지 (알 수 없음, API 장애 등)
        print(f"\n[경고] {symbol} 사이클 종료 감지 보류 — 브로커 잔고 조회 실패 (T={T} 유지, 보유수량 알 수 없음)")
        notify(f"[경고] {symbol} 잔고 조회 실패로 사이클 종료 감지 보류. T={T} 유지. 확인 필요.")

    elif T > 0 and current_qty == 0:
        # 잔고 0 확정: 정상 전량매도이거나 state.json이 잘못된 상태
        # Step 1에서 이력 기반 T 재추정을 이미 시도했으며,
        # 이력이 없으면 _infer_T_from_full_history가 T=0으로 리셋했으므로
        # 이 조건에 도달한 것은 진짜 T>0이 맞거나, 이력 복원 불가로 판단된 경우
        # → 사이클 종료 처리 / T 리셋
        print(f"\n{'=' * 60}")
        print(f"🏁 {symbol} 사이클 종료 감지 (사전) (T={T}, 보유수량={current_qty})")
        print(f"{'=' * 60}")

        _used_seed = state.get("effective_seed", 0.0)
        if not (REINVEST and _used_seed > 0):
            _used_seed = seed
        _cycle_start = state.get("cycle_start_date", "")
        _cycle_orders, _liquidation_event = _liquidation_boundary_orders(
            order_history, _cycle_start
        )
        _cycle_end = _cycle_end_date_from_history(order_history, _cycle_start)
        report = generate_cycle_report(
            symbol=symbol,
            order_history=order_history,
            cycle_start_date=_cycle_start,
            cycle_end_date=_cycle_end,
            seed=_used_seed,
            commission_rate=COMMISSION_RATE,
            cycle_orders=_cycle_orders,
            liquidation_event=_liquidation_event,
        )

        report_message = format_cycle_report_message(report)
        print(f"\n{report_message}")
        notify(report_message)

        if TRADE_MODE == "DRY":
            # DRY는 프리뷰/진단 모드 — 리포트만 표시하고 캐시(사이클 리셋/시드) 저장을 생략합니다.
            # 프리뷰가 새 사이클 기준으로 보이도록 in-memory로만 리셋합니다.
            state["effective_seed"] = 0.0
            state["T"] = 0.0
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, 0.0)
            T = 0.0
            print("  [DRY] 사이클 종료 리포트 표시. 캐시 저장은 생략합니다. (LIVE 실행 시 실제 리셋/시드 갱신)")
        else:
            # 복리 재투자가 활성화된 경우 다음 사이클 시드를 state에 저장합니다.
            # 손실이 발생한 경우에도 변경된 시드를 저장합니다.
            if REINVEST and report["next_cycle_seed"] is not None:
                state["effective_seed"] = round(report["next_cycle_seed"], 2)
                print(f"  복리 재투자: 다음 사이클 시드 = ${state['effective_seed']:.2f}")
            else:
                state["effective_seed"] = 0.0

            if _has_unresolved_reverse_orders(state):
                state.setdefault("reverse_mode", {})["reconciliation_only"] = True
                state["reverse_mode"]["active"] = False
                save_state(symbol, state)
                return

            # T 초기화 및 사이클 시작일 리셋
            state["T"] = 0.0
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, 0.0)
            save_state(symbol, state)

            print("  T값 초기화 완료. 새 사이클을 즉시 시작합니다.")
            T = 0.0
            # ── Step 2로 계속 진행 (새 사이클 즉시 시작) ──

    if current_qty == 0 and state.get("reverse_mode", {}).get("active"):
        if _has_unresolved_reverse_orders(state):
            state["reverse_mode"]["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
        else:
            state["reverse_mode"] = {}
        # [DRY READONLY] reverse_mode cleanup save: LIVE only
        if TRADE_MODE == "LIVE":
            save_state(symbol, state)

    # ── seed 적용 (복리 재투자) ────────────────────────────────
    if REINVEST:
        effective_seed = state.get("effective_seed", 0.0)
        if effective_seed > 0:
            seed = effective_seed
            print(f"  복리 재투자 적용: 이번 사이클 시드 = ${seed:.2f}")

    # ── Step 2: 전략 실행 ───────────────────────────────────────
    print("\n[Step 2] 전략 실행 중...")

    # DRY 모드는 주문뿐 아니라 리버스모드 진행일/누적값도 시뮬레이션해야
    # 실제 캐시 상태를 오염시키지 않습니다.
    strategy_state = deepcopy(state) if TRADE_MODE == "DRY" else state

    strategy_result = 무한매수법_V4(
        broker,
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=seed,
        T=T,
        additional_loc_levels=additional_loc_levels,
        state=strategy_state,
    )

    if strategy_result.get("reverse_exit") and TRADE_MODE == "LIVE":
        if _has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
        else:
            state["reverse_mode"] = {}
        save_state(symbol, state)

    last_price = strategy_result['last_price']
    # [DRY READONLY] close_prices: in-memory only — no save_state() reachable in DRY
    close_prices = state.get("close_prices", [])
    if last_price > 0:
        close_prices = [p for p in close_prices if isinstance(p, (int, float)) and p > 0]
        close_prices.append(round(last_price, 2))
        state["close_prices"] = close_prices[-5:]

    print("✓ 전략 실행 완료")
    print(f"  현재가: ${last_price}")
    print(f"  보유 수량: {strategy_result['position_qty']}주")
    print(f"  평단가: ${strategy_result['avg_price']}")
    print(f"  주문 가능 금액: ${strategy_result['orderable_cash']:.2f}")
    print(f"  T값: {T} / {splits}")
    if strategy_result['star_point']:
        print(f"  별지점: ${strategy_result['star_point']:.2f}")

    rev = state.get("reverse_mode", {})
    if rev.get("day_count", 0) > 0:
        print(f"  [리버스모드] {rev['day_count']}일차 진행 중")

    orders = strategy_result["orders"]

    # BUY LOC 1.19 상한 보정과 추가매수 계단 배치는 전략 단계에서 이미 반영되어 있습니다.
    # (trading_bot에서 다시 보정하면 추가매수 가격이 정상매수와 같아질 수 있습니다)
    for note in strategy_result.get("price_cap_notes") or []:
        notify(
            f"{symbol} ⚠️ LOC 가격 보정\n원가격: ${note['from']:.2f}\n"
            f"현재가 대비 상한: ${note['to']:.2f}"
        )

    print(f"\n[Step 3] 생성된 주문 목록 ({len(orders)}개)")
    print("-" * 60)

    if len(orders) == 0:
        print("생성된 주문이 없습니다.")
        return

    for i, order in enumerate(orders, 1):
        print(f"\n주문 {i}:")
        print(f"  설명: {order['comment']}")
        print(f"  매수/매도: {order['side']}")
        print(f"  주문 유형: {order['order_type']}")
        print(f"  수량: {order['quantity']}주")
        if order["price"]:
            print(f"  가격: ${order['price']}")
        else:
            print("  가격: 시장가")

    print("\n[Step 4] 주문 실행 중...")
    print("-" * 60)

    # Real+LIVE: 프리장 오픈 전이면 KST 기준으로 대기
    if BROKER_MODE == "real" and TRADE_MODE == "LIVE":
        now_kst = get_kst_now()
        is_dst = is_us_dst()
        pre_market_open_hour = 17 if is_dst else 18
        if now_kst.hour < pre_market_open_hour:
            target = now_kst.replace(hour=pre_market_open_hour, minute=0, second=0, microsecond=0)
            wait_seconds = (target - now_kst).total_seconds()
            print(f"⏳ 프리장 오픈 대기 중... (KST {now_kst.strftime('%H:%M:%S')} → {target.strftime('%H:%M:%S')}, 약 {wait_seconds/60:.0f}분)")
            time.sleep(wait_seconds)

    order_exchange_code = broker.exchange_code(exchange)

    # ── 잔여 주문 정리 ────────────────────────────────────────────
    # 매 세션마다 새 주문 전체를 새로 냅니다. 이전에 낸 주문이 아직 살아 있으면
    # (특히 목표매도 LIMIT) 새 주문과 중복되어 매도 수량이 보유량을 넘을 수 있습니다.
    # 새 주문을 내기 전에 봇 소유의 미체결 주문을 정리합니다.
    if TRADE_MODE == "LIVE":
        _cancel_open_bot_orders(broker, symbol, exchange, state, order_history)

    if TRADE_MODE == "LIVE":
        state["pending_order_batch"] = {
            "session": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
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
        save_state(symbol, state)

    executed_orders = []
    reserved_orders = []
    failed_orders = []
    fatal_order_error = False

    for i, order in enumerate(orders, 1):
        print(f"\n주문 {i}/{len(orders)} 실행: {order['comment']}")

        try:
            order_price = order["price"] if order["price"] else 0

            if TRADE_MODE == "LIVE":
                state["pending_order_intent"] = {
                    "symbol": symbol,
                    "side": order["side"],
                    "quantity": int(order["quantity"]),
                    "price": order_price,
                    "order_type": order["order_type"],
                    "comment": order["comment"],
                    "submitted_session": datetime.now(
                        ZoneInfo("America/New_York")
                    ).date().isoformat(),
                }
                try:
                    save_state(symbol, state)
                except Exception as intent_error:
                    raise GlobalScopedError(
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
                    raise SymbolScopedError(
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
                    # 신규 등록분만 "아직 한 번도 반영되지 않음"을 증명하는 버전 표식
                    # (기존 메타는 마이그레이션하지 않습니다 — 0 만으로는 미반영을 단정할 수 없음)
                    "processed_filled_amount": 0.0,
                    "fill_marker_version": FILL_MARKER_VERSION,
                }
                if order.get("reverse_action"):
                    reverse_mode = state.setdefault("reverse_mode", {})
                    cycle_id = reverse_mode.get("cycle_id") or datetime.now(
                        ZoneInfo("UTC")
                    ).isoformat()
                    order_meta.update({
                        "reverse_action": order["reverse_action"],
                        "reverse_day": int(order.get("reverse_day", 0)),
                        "cycle_id": cycle_id,
                        "submitted_at": result.order_time,
                        "submitted_session": datetime.now(
                            ZoneInfo("America/New_York")
                        ).date().isoformat(),
                    })
                    for key in ("reverse_base_t", "reverse_t_factor", "reverse_t_target"):
                        if key in order:
                            order_meta[key] = float(order[key])
                    reverse_mode["active"] = True
                    reverse_mode["cycle_id"] = cycle_id
                    reverse_mode["last_planned_session"] = datetime.now(
                        ZoneInfo("America/New_York")
                    ).date().isoformat()
                register_order_meta_in_state(state, result.order_id, order_meta)
                state["pending_order_intent"] = None
                if TRADE_MODE == "LIVE":
                    # 주문 응답을 받은 즉시 메타데이터를 checkpoint하여
                    # 이후 주문 실패/프로세스 중단 시에도 체결 reconciliation이 가능합니다.
                    try:
                        save_state(symbol, state)
                    except Exception as checkpoint_error:
                        raise GlobalScopedError(
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
                    notify(message)
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
                    notify(message)
            else:
                if TRADE_MODE == "LIVE":
                    raise SymbolScopedError(
                        "주문 접수 응답이 없어 pending intent를 해소하지 못했습니다. 주문 fence를 유지합니다."
                    )
                print("✓ 주문 정보 출력 완료")

        except OrderNotAcceptedError as error:
            print(f"✗ 주문 실패 (미접수 확정): {str(error)}")
            if TRADE_MODE == "LIVE":
                # 브로커가 명시적으로 거부해 주문이 접수되지 않았음이 보장되므로
                # 해소할 주문이 없는 fence만 남기지 않습니다.
                state["pending_order_intent"] = None
                state["pending_order_batch"] = None
                try:
                    save_state(symbol, state)
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
            notify(message, urgent=True)
            break
        except Exception as error:
            print(f"✗ 주문 실패: {str(error)}")
            if TRADE_MODE == "LIVE":
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
            notify(message, urgent=True)
            if isinstance(error, GlobalScopedError):
                # 공통 장애(상태 저장 실패)는 전체 실행을 중단합니다.
                raise
            if isinstance(error, SymbolScopedError):
                # 해당 종목만 중단하고 다음 종목으로 계속 진행합니다.
                raise
            if fatal_order_error:
                break
            continue

    if fatal_order_error:
        raise SymbolScopedError(
            "주문 결과를 확정할 수 없어 주문 fence를 유지합니다. "
            "해당 종목의 남은 주문을 중단합니다 (fence는 종목별로 복구되며 다른 종목은 계속 진행)."
        )

    if TRADE_MODE == "LIVE":
        state["pending_order_batch"] = None
        save_state(symbol, state)

    print("\n" + "=" * 60)
    print(f"{symbol} 처리 완료")
    print("=" * 60)

    # ── Step 4: T값 저장 (LIVE 주문 성공 or 리버스모드 or 상태 변경) ──
    should_save = False
    if TRADE_MODE == "LIVE" and (len(executed_orders) > 0 or len(reserved_orders) > 0):
        should_save = True
    if state.get("reverse_mode", {}).get("active"):
        should_save = True
    if should_save:
        save_state(symbol, state)

    if TRADE_MODE == "DRY":
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
    }


def _order_remaining_qty(row: dict):
    """이력 행의 미체결(잔량) 수량을 읽습니다. 파싱 불가면 None 을 반환합니다."""
    raw = row.get("nccs_qty")
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _order_filled_qty(row: dict):
    """이력 행의 체결 수량을 읽습니다. 파싱 불가면 None 을 반환합니다."""
    raw = row.get("ft_ccld_qty")
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


# 주문 상태 구분 — 종료 상태와 진행 중 상태를 명시합니다.
# (알 수 없는 상태는 종료로 간주하지 않습니다)
_TERMINAL_ORDER_STATUSES = {
    "취소", "체결", "거부", "만료", "정정됨", "종료", "완료",
    "CANCELED", "CANCELLED", "FILLED", "REJECTED", "EXPIRED", "CLOSED",
}
_OPEN_ORDER_STATUSES = {
    "미체결", "부분체결", "접수", "대기", "취소거부",
    "OPEN", "PARTIAL", "PENDING", "NEW",
}


def _is_terminal_order_status(status) -> bool:
    """종료 상태(취소/체결/거부/만료 등)인지 — 대소문자 무관."""
    token = str(status or "").strip().upper()
    return token in {s.upper() for s in _TERMINAL_ORDER_STATUSES}


def _is_open_order_status(status) -> bool:
    """진행 중(미체결/부분체결) 상태인지 — TOSS 영어 상태 포함."""
    token = str(status or "").strip().upper()
    return token in {s.upper() for s in _OPEN_ORDER_STATUSES}


def _open_orders_query_target(broker):
    """`get_open_orders` 를 실제로 지원하는 브로커 객체인지 판별합니다.

    Broker 기본 구현(빈 목록 + 경고)은 지원으로 보지 않습니다.
    DryBroker 는 내부 real 브로커를 기준으로 판단합니다.
    """
    target = getattr(broker, "_real", None)
    if not isinstance(target, Broker):
        target = broker
    if getattr(target, "supports_open_orders_query", False):
        return target
    owner = getattr(type(target), "get_open_orders", None)
    if owner is not None and owner is not Broker.get_open_orders:
        return target
    return None


def _load_open_orders_for_cancel(broker, symbol, exchange):
    """열린 주문 조회 — (rows, error) 3상태.

    Returns:
        tuple: (list[dict], Exception|None)
          - 지원하지 않는 브로커/정상 0건: ([], None) → 기존 경로로 진행
          - 404/405(엔드포인트 미지원): ([], None) + 경고 → CLOSED 만으로 진행
          - 그 외 실패: ([], error) → 호출부가 신규 주문을 보류
    """
    target = _open_orders_query_target(broker)
    if target is None:
        return [], None
    fetch = getattr(target, "get_open_orders", None)
    if not callable(fetch):
        return [], None
    try:
        try:
            rows = fetch(symbol, exchange)
        except TypeError:
            rows = fetch(symbol, exchange, days=30)
        return list(rows or []), None
    except Exception as error:
        text = str(error)
        if "404" in text or "405" in text:
            print(
                f"  ⚠️ 열린 주문 조회 미지원(HTTP 404/405) — CLOSED 이력만 사용합니다: "
                f"{text[:120]}"
            )
            return [], None
        print(f"  ⚠️ 열린 주문 조회 실패 — 신규 주문을 보류합니다: {text[:160]}")
        return [], error


def _merge_open_orders_into_history(broker, symbol, exchange, history):
    """열린 주문을 이력에 합칩니다 (조회 실패 시 기존 이력 유지 — 하위호환)."""
    open_rows, _error = _load_open_orders_for_cancel(broker, symbol, exchange)
    if not open_rows:
        return history
    merged = {str(row.get("odno")): row for row in (history or [])}
    for row in open_rows:
        odno = str(row.get("odno") or "")
        if odno:
            # 열린 주문의 현재 상태/잔량이 CLOSED 스냅샷보다 최신입니다.
            merged[odno] = row
    return list(merged.values())


def _cancel_open_bot_orders(
    broker,
    symbol: str,
    exchange: str,
    state: dict,
    order_history: list,
) -> int:
    """
    봇이 직접 낸 미체결 주문을 취소하고 **취소 확정까지 확인**합니다.

    매 세션마다 새 주문 전체를 새로 내므로, 이전에 낸 주문이 아직 살아 있으면
    목표매도 같은 주문이 중복되어 매도 수량이 보유량을 초과할 수 있습니다.

    처리 순서
      1. 대상 선정 — state.orders_meta 의 봇 소유 주문 중 미체결/부분체결(잔량>0)
      2. 취소 요청 — 실패해도 나머지 취소는 계속 (부분 취소 상태 방지)
      3. **취소 확정 확인** — 이력을 재조회해 잔량 0 / 취소 / 체결을 확인
         (이력 반영 지연 대비 재시도)
      4. 취소 도중 체결이 발생했거나 확정이 안 되면 신규 주문을 내지 않고 중단

    - 수동 주문은 건드리지 않습니다 (orders_meta 에 있는 것만)
    - 리버스 주문/리버스 활성 상태는 건드리지 않습니다 (기간 관리 대상)
    - 이전 세션 주문은 만료 대상이라 취소하지 않습니다 (당일 주문 규칙)
    - 취소 미지원 브로커는 조용히 건너뜀

    Returns:
        int: 취소하고 확정한 주문 수
    """
    # ── OPEN 조회는 대상 선정보다 **먼저** 항상 시도합니다. ──────────
    # 메타가 비었거나 리버스모드여도 조회 실패 자체는 신규 주문 보류 사유입니다.
    # (취소 대상 필터링만 메타/리버스 여부에 따라 달라집니다)
    open_rows, open_error = _load_open_orders_for_cancel(broker, symbol, exchange)
    if open_error is not None:
        raise SymbolScopedError(
            f"{symbol} 열린 주문 조회에 실패해 신규 주문을 보류합니다: {str(open_error)[:160]}"
        )

    orders_meta = state.get("orders_meta") or {}
    if not orders_meta:
        return 0

    cancel = getattr(broker, "cancel_order", None)
    if cancel is None:
        return 0  # 취소를 지원하지 않는 브로커는 건너뜁니다

    reverse_state = state.get("reverse_mode") or {}
    if reverse_state.get("active"):
        print("[잔여 주문 정리] 리버스모드 활성 — 주문을 건드리지 않습니다.")
        return 0

    # CLOSED 이력만 넘어온 경우에도 열린 주문을 취소 대상에 포함합니다.
    # 3상태: 정상(0건 포함)→진행 / 조회 실패→신규 주문 보류 / 404·405→CLOSED만 진행.
    if open_rows:
        merged = {str(row.get("odno")): row for row in (order_history or [])}
        for row in open_rows:
            odno = str(row.get("odno") or "")
            if odno:
                merged[odno] = row
        order_history = list(merged.values())
    by_odno = {str(o.get("odno")): o for o in (order_history or [])}
    today_kst = get_kst_now().strftime("%Y%m%d")
    today_session = datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y%m%d")
    targets: list[tuple[str, str, int | None]] = []
    expired_candidates: list[str] = []
    for odno, meta in list(orders_meta.items()):
        if (meta or {}).get("reverse_action"):
            continue  # 리버스 주문은 기간 관리 대상
        row = by_odno.get(str(odno))
        if row is None:
            continue
        status = str(row.get("prcs_stat_name", "") or "")
        # 토스는 OPEN/PARTIAL 같은 영어 상태를 반환하고, 어댑터가 한국어로
        # 매핑하지 못한 경우에도 취소 대상에서 누락되지 않도록 함께 판정합니다.
        if not _is_open_order_status(status):
            continue
        remaining_row = _order_remaining_qty(row)
        if remaining_row is not None and remaining_row <= 0:
            continue
        # 브로커별 ord_dt 컨벤션과 무관하게 실제 미국(ET) 세션으로 비교합니다
        # (KST 자정 경계에서도 올바른 세션 판정).
        order_session = _order_us_session_date(row)
        if order_session is not None:
            is_today_session = (order_session == today_session)
        else:
            # 시각 정보가 없는 행은 KST 날짜 비교로 호환합니다.
            order_day = _order_real_kst_date(row) or str(row.get("ord_dt", "") or "")
            is_today_session = (order_day == today_kst)
        if is_today_session:
            # 오늘 세션 주문 — 아직 체결될 수 있으므로 반드시 취소해야 합니다.
            targets.append((str(odno), status, remaining_row))
        else:
            # 이전 세션 주문 — 메리츠 주문 API에는 유효기간(GTC) 파라미터가 없어
            # 모든 주문이 당일 주문입니다. 세션이 종료되면 자동으로 만료되므로
            # 취소하지 않습니다 (만료 주문 취소 거부를 오탐으로 보지 않기 위함).
            expired_candidates.append(str(odno))

    if expired_candidates:
        print(
            f"[잔여 주문 정리] 이전 세션 주문 {len(expired_candidates)}건은 만료 대상이라 "
            f"취소하지 않습니다 (당일 주문 규칙)."
        )

    if not targets:
        return 0

    print(f"\n[잔여 주문 정리] 봇 소유 미체결 주문 {len(targets)}건 취소")
    for odno, status, remaining in targets:
        print(f"  - {odno} ({status}, 잔량 {remaining}주)")

    rejected: list[tuple[str, str]] = []
    for odno, _status, _remaining in targets:
        try:
            cancel(odno, symbol=symbol, exchange=exchange)
        except Exception as error:
            # 실패해도 나머지 취소는 계속합니다 (부분 취소 상태로 남지 않도록).
            rejected.append((odno, str(error)))
            print(f"  ✗ 취소 요청 실패: {odno} — {str(error)[:160]}")

    # ── 취소 확정 확인 ────────────────────────────────────────────
    # 메리츠 상세조회에는 잔량 정보가 없고(문서상 ncnt_odqt 항상 0),
    # 취소는 이력에 별도 행으로 남습니다. 따라서 이력 재조회가 유일한 확인 방법입니다.
    #
    # 확정 조건 (모두 만족해야 확정):
    #   - 이력 재조회가 성공했고
    #   - 해당 주문 행이 이력에 존재하며
    #   - 체결수량·잔량이 **유효한 숫자로 읽히고**
    #   - 취소 전 대비 체결수가 증가하지 않았고
    #   - 주문이 명시적인 종료 상태이거나 잔량이 0
    # 이력 누락·조회 실패·값 파싱 실패·알 수 없는 상태는 "확정"이 아니라
    # "미확정"으로 처리합니다 (조회 누락을 확정으로 보면 중복 주문이 됩니다).
    before_filled = {
        odno: (_order_filled_qty(by_odno.get(odno) or {}) or 0)
        for odno, _s, _r in targets
    }
    confirmed: set[str] = set()
    # 조회 실패 시에도 "미확정"으로 남도록 처음부터 모든 대상을 미확정으로 둡니다.
    still_open: list[str] = [odno for odno, _s, _r in targets]
    filled_during_cancel: list[str] = []
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(2)
        try:
            history = broker.get_order_history(symbol, exchange, days=30, verbose=False)
        except Exception as error:
            print(f"  ⚠️ 이력 재조회 실패 (확인 시도 {attempt}): {str(error)[:120]}")
            continue
        current = {str(o.get("odno")): o for o in (history or [])}
        still_open = []
        filled_during_cancel = []
        for odno, _status, _remaining in targets:
            row = current.get(odno)
            if row is None:
                # 이력에서 사라진 주문은 확정할 수 없습니다 (조회 누락 가능)
                still_open.append(odno)
                continue
            filled_now = _order_filled_qty(row)
            remaining_now = _order_remaining_qty(row)
            if filled_now is None or remaining_now is None:
                # 체결수량·잔량을 유효한 숫자로 읽지 못했습니다 → 미확정
                still_open.append(odno)
                continue
            if filled_now > before_filled.get(odno, 0):
                # 취소 도중 체결 → 포지션이 바뀌므로 기존 계획으로 재주문하면 안 됩니다
                filled_during_cancel.append(odno)
                still_open.append(odno)
                continue
            status = str(row.get("prcs_stat_name", "") or "")
            if _is_terminal_order_status(status):
                confirmed.add(odno)
                continue
            if not _is_open_order_status(status):
                # 알 수 없는 상태는 종료로 간주하지 않습니다
                still_open.append(odno)
                continue
            if remaining_now <= 0:
                confirmed.add(odno)
                continue
            still_open.append(odno)
        if not still_open:
            break
        print(f"  · 취소 확인 시도 {attempt}: 미확정 {len(still_open)}건")

    if filled_during_cancel:
        raise SymbolScopedError(
            "취소 과정에서 체결이 발생해 포지션이 바뀝니다. "
            "신규 주문을 생략하고 다음 실행에서 재계획합니다: "
            + ", ".join(filled_during_cancel)
        )

    if still_open:
        detail = []
        for odno in still_open:
            reason = next((m for o, m in rejected if o == odno), "취소 접수 후에도 잔량 유지")
            detail.append(f"{odno}({reason[:60]})")
        raise SymbolScopedError(
            "이전 주문 취소를 확정하지 못해 신규 주문을 생략합니다: " + ", ".join(detail)
        )

    print(f"[잔여 주문 정리] 취소 확정 {len(confirmed)}건 — 신규 주문을 제출합니다.")
    return len(confirmed)


def _print_state_diagnostic():
    """캐시 상태만 출력하고 브로커/API를 건드리지 않습니다."""
    print("\n[상태 진단 모드] API/전략/주문을 실행하지 않습니다.")
    for symbol_config in SYMBOLS:
        symbol = symbol_config["symbol"]
        state = load_state(symbol)
        reverse_mode = state.get("reverse_mode") or {}
        reverse_order_ids = sorted(
            odno for odno, meta in state.get("orders_meta", {}).items()
            if meta.get("reverse_action")
        )
        pending_intent = state.get("pending_order_intent")
        pending_batch = state.get("pending_order_batch")
        print(
            f"[상태 진단] {symbol} → "
            f"T={state.get('T', 0.0)}, "
            f"day_count={reverse_mode.get('day_count', 0)}, "
            f"cumulative_sell_proceeds=${reverse_mode.get('cumulative_sell_proceeds', 0.0):.2f}, "
            f"net_invested=${state.get('net_invested', 0.0):.2f} "
            f"({state.get('net_invested_status', 'unresolved')}), "
            f"effective_seed=${state.get('effective_seed', 0.0):.2f}, "
            f"cycle_start_date={state.get('cycle_start_date', '') or '(없음)'}, "
            f"pending_intent={bool(pending_intent)}, pending_batch={bool(pending_batch)}, "
            f"reverse_order_ids={reverse_order_ids}, "
            f"order_meta_ids={sorted(state.get('orders_meta', {}).keys())}, "
            f"last_updated={state.get('last_updated', '')}, "
            f"last_processed_ordno={state.get('last_processed_ordno', '')}, "
            f"state_hash={canonical_state_hash(state)}"
        )
    print("[상태 진단] 종료합니다. 상태 파일은 변경하지 않았습니다.")


def _fence_session(state):
    """pending fence가 기록된 미국 세션(YYYY-MM-DD)을 반환합니다. 없으면 ''."""
    intent = state.get("pending_order_intent")
    if intent:
        return intent.get("submitted_session", "")
    batch = state.get("pending_order_batch")
    if batch:
        return batch.get("session", "")
    return ""


def _recover_order_fence(state, symbol, live_qty):
    """이전 세션에 남은 주문 fence를 이력 정착 기준으로 해제합니다.

    run_one_symbol()의 reconciliation(주문이력/잔고 교차검증)이 정상 통과한 뒤에
    호출됩니다. 전일(이전 세션)의 미확정 주문은 하루 뒤 이력으로 판별할 수 있으므로
    fence를 해제하고 정상 진행합니다.

    보수적으로 유지하는 경우:
    - 잔고를 확인할 수 없음(live_qty None): 불확실성이 남아 있어 fence 유지 + 종목 중단
    - 같은 세션이거나 세션 정보 없음: 미확정 주문이 방금 발생한 상태 → fence 유지 + 종목 중단

    Returns:
        bool: fence를 해제했으면 True, 남아 있지 않았으면 False
    """
    if not (state.get("pending_order_intent") or state.get("pending_order_batch")):
        return False
    if live_qty is None:
        raise RuntimeError(
            f"이전 주문 fence가 남아 있고 잔고를 확인할 수 없어 신규 주문을 중단합니다."
        )
    today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    session = _fence_session(state)
    if not session or session >= today_session:
        raise RuntimeError(
            f"이전 주문 fence가 남아 있어 신규 주문을 중단합니다: "
            f"intent={state.get('pending_order_intent')}, batch={state.get('pending_order_batch')}"
        )
    state["pending_order_intent"] = None
    state["pending_order_batch"] = None
    save_state(symbol, state)
    print(f"  → [fence 복구] {symbol} 이전 세션({session}) 주문 fence를 이력 정착으로 해제하고 진행합니다.")
    notify(f"[fence 복구] {symbol} 이전 세션({session}) 미확정 주문 fence를 이력 정착으로 해제하고 진행합니다.")
    return True


def _has_unresolved_reverse_orders(state):
    """현재 리버스 cycle에 아직 terminal 확정되지 않은 주문이 있는지 확인합니다."""
    cycle_id = state.get("reverse_mode", {}).get("cycle_id", "")
    return any(
        meta.get("reverse_action")
        and meta.get("cycle_id") == cycle_id
        and not meta.get("terminal")
        for meta in state.get("orders_meta", {}).values()
    )


def _repair_state_only():
    """기대 fingerprint가 일치할 때만 오염된 리버스 상태를 초기화합니다.

    STATE_REPAIR_TARGET_T가 설정되면 reverse_mode 초기화 대신 **T만 보정**하고
    리버스 cycle을 유지합니다. 이는 날짜 컨벤션 버그로 오반영된 T(예: 20→15)를
    되돌려, 다음 RUN의 reconciliation이 체결 기반으로 정상 재계산(예: 20→18)하도록
    하기 위한 일회성 복구입니다. 실행 직후 변수 삭제 필수.
    """
    symbol = os.getenv("STATE_REPAIR_SYMBOL", "").strip().upper()
    expected_t = os.getenv("STATE_REPAIR_EXPECT_T", "").strip()
    expected_day = os.getenv("STATE_REPAIR_EXPECT_DAY_COUNT", "").strip()
    expected_proceeds = os.getenv("STATE_REPAIR_EXPECT_PROCEEDS", "").strip()
    expected_updated = os.getenv("STATE_REPAIR_EXPECT_LAST_UPDATED", "").strip()
    expected_ordno = os.getenv("STATE_REPAIR_EXPECT_LAST_ORDNO", "").strip()
    expected_reverse_ids_raw = os.getenv("STATE_REPAIR_EXPECT_REVERSE_IDS", "").strip()
    expected_reverse_submitted_at = os.getenv("STATE_REPAIR_EXPECT_REVERSE_SUBMITTED_AT", "").strip()
    target_t = os.getenv("STATE_REPAIR_TARGET_T", "").strip()
    expected_reverse_ids = sorted(filter(None, expected_reverse_ids_raw.split(",")))
    required = {
        "STATE_REPAIR_SYMBOL": symbol,
        "STATE_REPAIR_EXPECT_T": expected_t,
        "STATE_REPAIR_EXPECT_DAY_COUNT": expected_day,
        "STATE_REPAIR_EXPECT_PROCEEDS": expected_proceeds,
        "STATE_REPAIR_EXPECT_LAST_UPDATED": expected_updated,
        "STATE_REPAIR_EXPECT_LAST_ORDNO": expected_ordno,
        "STATE_REPAIR_EXPECT_REVERSE_IDS": expected_reverse_ids_raw,
        "STATE_REPAIR_EXPECT_REVERSE_SUBMITTED_AT": expected_reverse_submitted_at,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"복구 fingerprint 환경변수가 없습니다: {', '.join(missing)}")

    state = load_state(symbol)
    reverse_mode = state.get("reverse_mode") or {}
    actual = {
        "T": float(state.get("T", 0.0)),
        "day_count": int(reverse_mode.get("day_count", 0)),
        "proceeds": float(reverse_mode.get("cumulative_sell_proceeds", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "last_ordno": str(state.get("last_processed_ordno", "")),
        "reverse_ids": sorted(
            set(expected_reverse_ids)
            .intersection(state.get("orders_meta", {}).keys())
            .union({
                odno for odno, meta in state.get("orders_meta", {}).items()
                if meta.get("reverse_action")
            })
        ),
    }
    expected = {
        "T": float(expected_t),
        "day_count": int(expected_day),
        "proceeds": float(expected_proceeds),
        "last_updated": expected_updated,
        "last_ordno": expected_ordno,
        "reverse_ids": expected_reverse_ids,
    }
    if actual != expected:
        raise RuntimeError(f"복구 fingerprint 불일치: actual={actual}, expected={expected}")

    if target_t:
        # T만 보정하고 리버스 cycle/orders_meta를 보존합니다.
        # 단순 T 대입은 다음 정산의 원장 재생에서 덮어써지므로, 복구 전/목표/조정량을
        # adjust 항목으로 기록해 원장 재생값을 복구 T와 일치시킵니다.
        print(f"[상태 복구] {symbol} T 보정: {state['T']} → {float(target_t)} (리버스 cycle 유지)")
        record_settlement_t_adjustment(
            state, float(target_t),
            source="STATE_REPAIR_TARGET_T",
            at=datetime.now(ZoneInfo("UTC")).isoformat(),
        )
        save_state(symbol, state)
        print(f"[상태 복구] {symbol} T={target_t}로 보정 완료. 다음 RUN의 reconciliation이 체결 기반으로 재계산합니다.")
        return

    print(f"[상태 복구] {symbol} reverse_mode 초기화: {reverse_mode}")
    state["reverse_mode"] = {}
    reverse_order_ids = expected_reverse_ids
    if reverse_order_ids:
        print(f"[상태 복구] 리버스 주문 메타데이터 보존(archived): {reverse_order_ids}")
        for odno in reverse_order_ids:
            meta = state["orders_meta"][odno]
            meta.setdefault("reverse_action", "sell")
            meta["submitted_at"] = expected_reverse_submitted_at
            meta["repair_archived"] = True
            meta["terminal"] = True
    save_state(symbol, state)
    print(f"[상태 복구] {symbol} T/체결 watermark를 유지하고 리버스 상태만 초기화했습니다.")


def _reverse_audit_only():
    """브로커 주문이력과 state orders_meta를 대조해 리버스 주문 상태만 출력합니다.

    STATE_REVERSE_AUDIT_ONLY=true 로 사용합니다.
    read-only 모드 — 상태 파일을 저장하지 않으며 전략/주문도 실행하지 않습니다.
    주문 시각은 통일적으로 KST 기준으로 표시합니다.
    """
    import config as runtime_config
    if runtime_config.TRADE_MODE == "LIVE":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[audit] 검증 모드 — 브로커 생성 전 DRY로 고정합니다.")

    broker = create_broker()
    try:
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            exchange = symbol_config["exchange"]
            print(f"\n[audit] {symbol} 리버스 주문 상태 감사 (read-only)")
            state = load_state(symbol)
            reverse_meta = sorted(
                (odno, meta) for odno, meta in state.get("orders_meta", {}).items()
                if meta.get("reverse_action")
            )
            if not reverse_meta:
                print("[audit] 리버스 주문 메타가 없습니다.")
            order_history = broker.get_order_history(symbol, exchange, days=90, verbose=False)
            history_by_odno = {}
            for order in order_history:
                history_by_odno.setdefault(str(order.get("odno", "")), []).append(order)

            for odno, meta in reverse_meta:
                candidates = history_by_odno.get(str(odno), [])
                settled = None
                if candidates:
                    settled = max(
                        candidates,
                        key=lambda o: float(o.get("ft_ccld_qty", "0") or 0),
                    )
                broker_qty = int(float(settled.get("ft_ccld_qty", "0"))) if settled else 0
                broker_amt = float(settled.get("ft_ccld_amt3", "0") or 0) if settled else 0.0
                state_qty = int(meta.get("processed_filled_qty", 0))
                state_amt = float(meta.get("processed_filled_amount", 0.0))
                terminal = bool(meta.get("terminal"))
                delta_amt = max(0.0, broker_amt - state_amt)
                print(
                    f"  odno={mask_odno(odno)} side={meta.get('reverse_action')} day={meta.get('reverse_day')} "
                    f"submitted={meta.get('submitted_at', '')} session={meta.get('submitted_session', '')} "
                    f"| 브로커: qty={broker_qty} amt=${broker_amt:.2f} "
                    f"remaining={settled.get('nccs_qty', '') if settled else ''} "
                    f"status={settled.get('prcs_stat_name', '') if settled else ''} "
                    f"cancel_qty={settled.get('cncl_qty', '') if settled else ''} "
                    f"text1={settled.get('text1', '') if settled else ''} "
                    f"| state: qty={state_qty} amt=${state_amt:.2f} "
                    f"| delta_amt=${delta_amt:.2f} terminal={terminal}"
                )
            net_invested = float(state.get("net_invested", 0.0))
            status = state.get("net_invested_status", "unresolved")
            print(f"[audit] {symbol} net_invested=${net_invested:.2f} ({status})")
            print(f"[audit] {symbol} canonical state_hash={canonical_state_hash(state)}")
    finally:
        broker.close()
    print("[audit] read-only 모드 — 상태 파일은 변경하지 않았습니다.")


def _reverse_reconcile_only():
    """리버스 주문의 실제 체결 상태만 상태에 반영하고 주문은 생성하지 않습니다.

    STATE_REVERSE_RECONCILE_ONLY=true 로 사용합니다.
    기존 reconciliation(update_T_from_history → reconcile_reverse_fills)을 실행해
    미반영 체결(예: 5315 SELL/$3,910.05, 5316 BUY/$0)을 state에 반영하고 저장합니다.
    전략/주문은 실행하지 않으며, 한번 더 잘못된 주문이 나가지 않도록 DRY로 고정합니다.
    """
    import config as runtime_config
    if runtime_config.TRADE_MODE == "LIVE":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[reconcile-only] 검증 모드 — 브로커 생성 전 DRY로 고정합니다.")

    broker = create_broker()
    try:
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            exchange = symbol_config["exchange"]
            print(f"\n[reconcile-only] {symbol} 리버스 체결 상태 조정 (주문 없음)")
            state = load_state(symbol)
            order_history = broker.get_order_history(symbol, exchange, days=90, verbose=True)
            try:
                live_balance = broker.get_balance(symbol, exchange)
                live_qty = live_balance.quantity if live_balance else 0
            except Exception as e:
                print(f"[reconcile-only] 잔고 조회 실패(참고): {e}")
                live_qty = None
            state = update_T_from_history(symbol, state, order_history, balance_qty=live_qty)
            # reconcile은 net_invested를 valid로 승격하지 않습니다 (audit/repair가 담당)
            # 보유수량 기준이 없으면 정산을 확정하지 않고 보류합니다.
            _save_settlement_state(symbol, state)
            save_state(symbol, state)
            print(
                f"[reconcile-only] {symbol} 저장 완료 → T={state['T']}, "
                f"net_invested=${float(state.get('net_invested', 0.0)):.2f} "
                f"({state.get('net_invested_status', 'unresolved')}), "
                f"state_hash={canonical_state_hash(state)}"
            )
    finally:
        broker.close()
    print("[reconcile-only] 주문을 생성하지 않고 상태만 반영했습니다.")


def _net_invested_repair_only():
    """net_invested를 명시 값으로 1회 복구합니다 (CAS, 상태 파일만 수정).

    사용:
        STATE_NET_INVESTED_REPAIR_ONLY=true
        STATE_NET_INVESTED_REPAIR_SYMBOL=SOXL
        STATE_NET_INVESTED_REPAIR_TARGET=<target>
        STATE_NET_INVESTED_REPAIR_EXPECT_HASH=<audit에서 출력한 해시>
        STATE_NET_INVESTED_REPAIR_EXPECT_NET_INVESTED=0.00
        STATE_NET_INVESTED_REPAIR_EXPECT_STATUS=unresolved

    - 브로커 API/전략/주문은 실행하지 않습니다.
    - audit 직후의 canonical hash가 일치할 때만 적용합니다 (CAS).
    - T, watermark, orders_meta, reverse_mode, fence는 보존합니다.
    - 미종결 리버스 주문 또는 pending fence가 남아 있으면 거부합니다.
    """
    def _required(name):
        raw = os.environ.get(name)
        if not raw or not raw.strip():
            raise RuntimeError(f"net_invested 복구 fingerprint 환경변수가 없습니다: {name}")
        return raw.strip()

    symbol = _required("STATE_NET_INVESTED_REPAIR_SYMBOL").upper()
    target = float(_required("STATE_NET_INVESTED_REPAIR_TARGET"))
    expect_hash = _required("STATE_NET_INVESTED_REPAIR_EXPECT_HASH")
    expect_net_invested = float(_required("STATE_NET_INVESTED_REPAIR_EXPECT_NET_INVESTED"))
    expect_status = _required("STATE_NET_INVESTED_REPAIR_EXPECT_STATUS")

    state = load_state(symbol)
    actual_hash = canonical_state_hash(state)
    actual_net_invested = float(state.get("net_invested", 0.0))
    actual_status = state.get("net_invested_status", "unresolved")

    if actual_hash != expect_hash:
        raise RuntimeError(
            f"net_invested 복구 hash 불일치: actual={actual_hash}, expected={expect_hash}"
        )
    if abs(actual_net_invested - expect_net_invested) > 0.005:
        raise RuntimeError(
            f"net_invested 복구 필드 불일치: actual={actual_net_invested:.2f}, expected={expect_net_invested:.2f}"
        )
    if actual_status != expect_status:
        raise RuntimeError(
            f"net_invested 복구 status 불일치: actual={actual_status}, expected={expect_status}"
        )
    if _has_unresolved_reverse_orders(state):
        raise RuntimeError(
            "net_invested 복구 거부: 미종결 리버스 주문이 남아 있습니다. reconcile/audit을 먼저 진행하세요."
        )
    if state.get("pending_order_intent") or state.get("pending_order_batch"):
        raise RuntimeError(
            "net_invested 복구 거부: pending 주문 fence가 남아 있습니다. fence 확인 필요."
        )

    print(
        f"[net_invested 복구] {symbol} ${actual_net_invested:.2f}({actual_status}) "
        f"→ ${target:.2f}(valid)로 복구합니다."
    )
    state["net_invested"] = round(target, 2)
    state["net_invested_status"] = "valid"
    save_state(symbol, state)
    print(f"[net_invested 복구] {symbol} net_invested=${target:.2f}/valid 저장 완료.")


def _set_cycle_start_only():
    """cycle_start_date를 명시 값으로 1회 복구합니다 (CAS, 상태 파일만 수정).

    사용:
        STATE_SET_CYCLE_START_ONLY=true
        STATE_SET_CYCLE_START_SYMBOL=TQQQ
        STATE_SET_CYCLE_START_DATE=2026-09-23
        STATE_SET_CYCLE_START_EXPECT_HASH=<진단에서 출력한 state_hash>
        STATE_SET_CYCLE_START_EXPECT_T=9.375
        STATE_SET_CYCLE_START_EXPECT_CURRENT=          # 현재 값(비어 있으면 빈 문자열)

    - 브로커 API/전략/주문은 실행하지 않습니다.
    - 진단 직후의 canonical hash가 일치할 때만 적용합니다 (CAS).
    - T, net_invested, watermark, orders_meta, reverse_mode, fence는 보존합니다.
    - 미종결 리버스 주문 또는 pending fence가 남아 있으면 거부합니다.
    - date는 YYYY-MM-DD 형식이며 기준일(last_updated 날짜, 없으면 오늘) 이하여야 합니다.
    """
    def _required(name, allow_empty=False):
        raw = os.environ.get(name)
        if raw is None:
            raise RuntimeError(f"cycle_start_date 복구 fingerprint 환경변수가 없습니다: {name}")
        if not allow_empty and not raw.strip():
            raise RuntimeError(f"cycle_start_date 복구 fingerprint 환경변수가 비어 있습니다: {name}")
        return raw.strip()

    symbol = _required("STATE_SET_CYCLE_START_SYMBOL").upper()
    date_str = _required("STATE_SET_CYCLE_START_DATE")
    expect_hash = _required("STATE_SET_CYCLE_START_EXPECT_HASH")
    expect_t = float(_required("STATE_SET_CYCLE_START_EXPECT_T"))
    # 현재 cycle_start_date는 "없음"(빈 문자열)을 의미할 수 있으므로 빈 값 허용
    expect_current = _required("STATE_SET_CYCLE_START_EXPECT_CURRENT", allow_empty=True)

    # 날짜 형식 검증 (YYYY-MM-DD)
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise RuntimeError(f"cycle_start_date 복구 거부: 날짜 형식이 YYYY-MM-DD가 아닙니다: {date_str}")

    state = load_state(symbol)
    actual_hash = canonical_state_hash(state)
    actual_t = float(state.get("T", 0.0))
    actual_current = state.get("cycle_start_date", "") or ""

    if actual_hash != expect_hash:
        raise RuntimeError(
            f"cycle_start_date 복구 hash 불일치: actual={actual_hash}, expected={expect_hash}"
        )
    if abs(actual_t - expect_t) > 0.0005:
        raise RuntimeError(
            f"cycle_start_date 복구 T 불일치: actual={actual_t}, expected={expect_t}"
        )
    if actual_current != expect_current:
        raise RuntimeError(
            f"cycle_start_date 복구 현재값 불일치: actual='{actual_current}', expected='{expect_current}'"
        )
    if _has_unresolved_reverse_orders(state):
        raise RuntimeError(
            "cycle_start_date 복구 거부: 미종결 리버스 주문이 남아 있습니다. reconcile/audit을 먼저 진행하세요."
        )
    if state.get("pending_order_intent") or state.get("pending_order_batch"):
        raise RuntimeError(
            "cycle_start_date 복구 거부: pending 주문 fence가 남아 있습니다. fence 확인 필요."
        )

    # 사이클 시작일은 과거여야 합니다: 기준일(last_updated 날짜, 없으면 오늘) 이하만 허용.
    last_updated = str(state.get("last_updated", ""))
    ref_date = None
    if len(last_updated) >= 10:
        try:
            ref_date = datetime.strptime(last_updated[:10], "%Y-%m-%d").date()
        except ValueError:
            ref_date = None
    if ref_date is None:
        ref_date = datetime.now(ZoneInfo("UTC")).date()
    if target_date > ref_date:
        raise RuntimeError(
            f"cycle_start_date 복구 거부: date({date_str})가 기준일({ref_date.isoformat()})보다 미래입니다."
        )

    print(
        f"[cycle_start_date 복구] {symbol} '{actual_current or '(없음)'}' → '{date_str}'로 복구합니다."
    )
    state["cycle_start_date"] = date_str
    save_state(symbol, state)
    print(f"[cycle_start_date 복구] {symbol} cycle_start_date={date_str} 저장 완료.")


def _assume_reverse_expiry_only():
    """키움 모의의 이전 세션 zero-fill 주문을 정책상 종결 처리합니다.

    API가 ``접수``/잔량을 유지하는 모의투자 LIMIT 주문에만 사용하는 일회성 모드입니다.
    실전이나 다른 브로커에서는 실행하지 않으며, state hash와 주문 메타를 확인합니다.
    """
    def _required(name):
        raw = os.environ.get(name)
        if not raw or not raw.strip():
            raise RuntimeError(f"리버스 만료 가정 환경변수가 없습니다: {name}")
        return raw.strip()

    if BROKER != "kiwoom" or BROKER_MODE != "demo":
        raise RuntimeError("리버스 만료 가정은 키움 모의투자에서만 허용됩니다.")

    symbol = _required("STATE_ASSUME_REVERSE_EXPIRY_SYMBOL").upper()
    odno = _required("STATE_ASSUME_REVERSE_EXPIRY_ORDER")
    expect_hash = _required("STATE_ASSUME_REVERSE_EXPIRY_EXPECT_HASH")
    state = load_state(symbol)
    actual_hash = canonical_state_hash(state)
    if actual_hash != expect_hash:
        raise RuntimeError(
            f"리버스 만료 가정 hash 불일치: actual={actual_hash}, expected={expect_hash}"
        )
    if state.get("pending_order_intent") or state.get("pending_order_batch"):
        raise RuntimeError("리버스 만료 가정 거부: pending 주문 fence가 남아 있습니다.")

    meta = state.get("orders_meta", {}).get(odno)
    if not meta or not meta.get("reverse_action"):
        raise RuntimeError(f"리버스 만료 가정 거부: 주문 메타가 없습니다: {mask_odno(odno)}")
    if meta.get("terminal"):
        print(f"[리버스 만료 가정] {symbol} odno={mask_odno(odno)} 이미 terminal입니다.")
        return
    if int(meta.get("processed_filled_qty", 0) or 0) != 0:
        raise RuntimeError("리버스 만료 가정 거부: 이미 체결된 수량이 있습니다.")
    if float(meta.get("processed_filled_amount", 0.0) or 0.0) != 0.0:
        raise RuntimeError("리버스 만료 가정 거부: 이미 체결금액이 있습니다.")

    submitted_session = str(meta.get("submitted_session", ""))
    today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if not submitted_session or submitted_session >= today_session:
        raise RuntimeError("리버스 만료 가정 거부: 이전 거래 세션 주문이 아닙니다.")

    meta["terminal"] = True
    meta["terminal_assumed"] = True
    meta["terminal_assumed_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    meta["terminal_assumption_reason"] = "kiwoom_demo_day_order_expired_after_session"
    save_state(symbol, state)
    print(f"[리버스 만료 가정] {symbol} odno={mask_odno(odno)}를 terminal_assumed로 저장했습니다.")


def _clear_fence_only():
    """기대 fingerprint가 일치할 때만 주문 fence를 1회 초기화합니다.

    불확실 주문(네트워크 오류 등)으로 남은 pending_order_intent/pending_order_batch를
    관리자가 브로커 주문이력을 직접 확인한 뒤 제거할 때 사용합니다.
    API/전략/주문은 실행하지 않으며, 순수 상태 파일만 수정합니다.
    """
    def _required(name, allow_empty=False):
        raw = os.environ.get(name)
        if raw is None:
            raise RuntimeError(f"fence 복구 fingerprint 환경변수가 없습니다: {name}")
        if not allow_empty and not raw.strip():
            raise RuntimeError(f"fence 복구 fingerprint 환경변수가 비어 있습니다: {name}")
        return raw.strip()

    symbol = _required("STATE_CLEAR_FENCE_SYMBOL").upper()
    expected_t = _required("STATE_CLEAR_FENCE_EXPECT_T")
    expected_updated = _required("STATE_CLEAR_FENCE_EXPECT_LAST_UPDATED")
    # intent/batch는 "fence 없음"을 의미하는 빈 값이 허용됩니다 (env var 존재만 필수)
    expected_intent = _required("STATE_CLEAR_FENCE_EXPECT_INTENT", allow_empty=True)
    expected_batch = _required("STATE_CLEAR_FENCE_EXPECT_BATCH", allow_empty=True)

    state = load_state(symbol)
    actual_intent = state.get("pending_order_intent")
    actual_batch = state.get("pending_order_batch")

    def _fmt(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else ""

    actual = {
        "T": float(state.get("T", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "intent": _fmt(actual_intent),
        "batch": _fmt(actual_batch),
    }
    expected = {
        "T": float(expected_t),
        "last_updated": expected_updated,
        "intent": expected_intent,
        "batch": expected_batch,
    }
    if actual != expected:
        raise RuntimeError(f"fence 복구 fingerprint 불일치: actual={actual}, expected={expected}")
    if not actual_intent and not actual_batch:
        print(f"[fence 복구] {symbol} 남아 있는 fence가 없습니다. (이미 해소됨)")
        return

    print(f"[fence 복구] {symbol} 주문 fence 초기화: intent={actual_intent}, batch={actual_batch}")
    state["pending_order_intent"] = None
    state["pending_order_batch"] = None
    save_state(symbol, state)
    print(f"[fence 복구] {symbol} 주문 fence를 해제했습니다.")


# ── REPAIR 모드 핸들러 ──
# REPAIR_ACTION이 지정되면 기존 STATE_*_ONLY 핸들러를 재사용합니다.
# REINFERENCE/FORCE_T는 REPAIR 전용 핸들러로 별도 구현합니다.

# REPAIR_ACTION → 기존 핸들러 매핑 (DIAGNOSTIC除外 — 별도 함수)
_REPAIR_LEGACY_MAP = {
    "REVERSE_AUDIT": "_reverse_audit_only",
    "REVERSE_RECONCILE": "_reverse_reconcile_only",
    "REVERSE_RESET": "_repair_state_only",
    "REVERSE_T_FIX": "_repair_state_only",
    "FENCE_CLEAR": "_clear_fence_only",
    "NET_INVESTED_REPAIR": "_net_invested_repair_only",
    "CYCLE_START_SET": "_set_cycle_start_only",
    "ASSUME_EXPIRY": "_assume_reverse_expiry_only",
}

# REPAIR 모드에서 허용되는 액션 목록
_REPAIR_ACTIONS = {"DIAGNOSTIC", "REVERSE_AUDIT", "REVERSE_RECONCILE", "REVERSE_RESET",
                   "REVERSE_T_FIX", "FENCE_CLEAR", "NET_INVESTED_REPAIR",
                   "CYCLE_START_SET", "ASSUME_EXPIRY", "REINFERENCE", "FORCE_T"}


def _repair_reinference():
    """REPAIR 모드 전용: FORCE_T_REINFERENCE를 단독 실행합니다.

    기존 main()의 FORCE_T_REINFERENCE 처리를 모방하지만, REPAIR 모드 전용으로
    브로커/전략/주문 없이 T 재추정만 수행합니다.
    """
    import config as runtime_config
    # REPAIR는 LIVE이더라도 DRY로 고정 (T 재추정 중 주문 방지)
    if runtime_config.TRADE_MODE != "DRY":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[REPAIR] REINFERENCE — 브로커 생성 전 DRY 모드로 고정합니다.")

    broker = create_broker()
    try:
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            exchange = symbol_config["exchange"]
            print(f"\n[REPAIR:REINFERENCE] {symbol} T 재추정 시작")
            state = load_state(symbol)

            # last_updated 초기화 → update_T_from_history가 전체 이력에서 T 재추정
            if state.get("last_updated"):
                state["last_updated"] = ""

            history_days = 90
            order_history = broker.get_order_history(symbol, exchange, days=history_days, verbose=False)
            try:
                live_balance = broker.get_balance(symbol, exchange)
                live_qty = live_balance.quantity if live_balance else 0
            except Exception as e:
                print(f"[REPAIR:REINFERENCE] {symbol} 잔고 조회 실패(참고): {e}")
                live_qty = None

            state = update_T_from_history(symbol, state, order_history, balance_qty=live_qty)
            save_state(symbol, state)
            print(
                f"[REPAIR:REINFERENCE] {symbol} T 재추정 완료 → T={state['T']}, "
                f"state_hash={canonical_state_hash(state)}"
            )
    finally:
        broker.close()
    print("[REPAIR:REINFERENCE] 모든 종목 T 재추정 완료. 주문은 실행하지 않았습니다.")


def _repair_force_t():
    """REPAIR 모드 전용: {SYMBOL}_FORCE_T를 단독 실행합니다.

    기존 run_one_symbol()의 FORCE_T 처리를 REPAIR 전용으로 재현합니다.
    주문은 생성하지 않습니다.
    """
    broker = create_broker()
    try:
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            exchange = symbol_config["exchange"]
            force_t = symbol_config.get("force_t")
            if force_t is None:
                print(f"[REPAIR:FORCE_T] {symbol} FORCE_T 미설정 — 건너뜁니다.")
                continue

            print(f"\n[REPAIR:FORCE_T] {symbol} FORCE_T={force_t} 적용 시작")
            state = load_state(symbol)

            if _has_unresolved_reverse_orders(state):
                print(f"[REPAIR:FORCE_T] {symbol} 미해결 리버스 주문이 있어 FORCE_T 적용 불가 — 건너뜁니다.")
                continue

            old_T = state["T"]
            state["T"] = force_t
            state.pop("balance_mismatch", None)
            state["orders_meta"] = {}
            state["additional_loc_odno"] = []
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, force_t)

            # FORCE_T 이후 이력 조회가 stale last_updated 기준으로 이미 반영된 주문을
            # 다시 가산(이중 가산)하는 것을 방지하기 위해, 이번 RUN에서 조회된 최신
            # 주문 시각으로 last_updated를 갱신합니다.
            order_history = broker.get_order_history(symbol, exchange, days=90, verbose=False)
            latest_ord_dt = ""
            for o in order_history:
                odt = o.get("ord_datetime_utc", "")
                if odt and odt > latest_ord_dt:
                    latest_ord_dt = odt
            if latest_ord_dt:
                state["last_updated"] = latest_ord_dt
                latest_order = next(
                    o for o in order_history
                    if o.get("ord_datetime_utc") == latest_ord_dt
                )
                state["last_processed_ordno"] = latest_order.get("odno", "")
            else:
                state["last_updated"] = datetime.now(ZoneInfo("UTC")).isoformat()
                state["last_processed_ordno"] = ""

            if force_t == 0:
                state["cycle_start_date"] = ""
                state["net_invested"] = 0.0
                state["net_invested_status"] = "valid"

            save_state(symbol, state)
            print(f"[REPAIR:FORCE_T] {symbol} FORCE_T={force_t} 적용 (이전 T={old_T}), 저장 완료.")
    finally:
        broker.close()
    print("[REPAIR:FORCE_T] 모든 종목 FORCE_T 적용 완료. 주문은 실행하지 않았습니다.")


def _dispatch_repair():
    """REPAIR 모드의 메인 진입점.

    액션 소스(REPAIR_ACTION 또는 레거시 STATE_*_ONLY/FORCE_T*)가 **정확히
    하나**일 때만 실행합니다. 0개 또는 2개 이상이면 sys.exit(1)로 중단합니다.
    단일 소스가 레거시 변수면 기존 핸들러로, REPAIR_ACTION이면 매핑된
    핸들러(REINFERENCE/FORCE_T는 REPAIR 전용)로 디스패치합니다.
    """
    import config as runtime_config

    # ── 액션 소스 집계 ──
    # FORCE_T*는 "FORCE_T 계열"을 하나의 소스로 봅니다 (여러 종목 동시 설정 허용).
    sources = []
    if REPAIR_ACTION:
        sources.append(f"REPAIR_ACTION={REPAIR_ACTION}")
    legacy_flags = {
        "STATE_DIAGNOSTIC_ONLY": STATE_DIAGNOSTIC_ONLY,
        "STATE_REPAIR_ONLY": STATE_REPAIR_ONLY,
        "STATE_CLEAR_FENCE_ONLY": STATE_CLEAR_FENCE_ONLY,
        "STATE_REVERSE_AUDIT_ONLY": STATE_REVERSE_AUDIT_ONLY,
        "STATE_REVERSE_RECONCILE_ONLY": STATE_REVERSE_RECONCILE_ONLY,
        "STATE_NET_INVESTED_REPAIR_ONLY": STATE_NET_INVESTED_REPAIR_ONLY,
        "STATE_ASSUME_REVERSE_EXPIRY_ONLY": STATE_ASSUME_REVERSE_EXPIRY_ONLY,
        "STATE_SET_CYCLE_START_ONLY": STATE_SET_CYCLE_START_ONLY,
    }
    for name, enabled in legacy_flags.items():
        if enabled:
            sources.append(name)
    if runtime_config.FORCE_T_REINFERENCE:
        sources.append("FORCE_T_REINFERENCE")
    if any(cfg.get("force_t") is not None for cfg in SYMBOLS):
        sources.append("{SYMBOL}_FORCE_T")

    if len(sources) != 1:
        print(f"[REPAIR] 액션 소스가 {len(sources)}개 감지되었습니다. 정확히 1개여야 합니다.")
        for s in sources:
            print(f"  - {s}")
        print("  REPAIR_ACTION 또는 레거시 STATE_*_ONLY/FORCE_T* 중 하나만 설정하세요.")
        sys.exit(1)

    source = sources[0]
    print(f"[REPAIR] 액션 소스: {source}")

    # ── 디스패치 ──
    if source.startswith("REPAIR_ACTION="):
        action = REPAIR_ACTION
        if action not in _REPAIR_ACTIONS:
            print(f"[REPAIR] 잘못된 REPAIR_ACTION: '{action}'. 허용 값: {sorted(_REPAIR_ACTIONS)}")
            sys.exit(1)
        if action == "DIAGNOSTIC":
            _print_state_diagnostic()
        elif action == "REINFERENCE":
            _repair_reinference()
        elif action == "FORCE_T":
            _repair_force_t()
        elif action in _REPAIR_LEGACY_MAP:
            globals()[_REPAIR_LEGACY_MAP[action]]()
        else:
            # 방어적 — 위 검증에서 걸러지지만 추가 안전장치
            print(f"[REPAIR] 처리할 수 없는 액션: {action}")
            sys.exit(1)
    else:
        # 레거시 단일 소스 → 기존 핸들러로 디스패치
        if source == "STATE_DIAGNOSTIC_ONLY":
            _print_state_diagnostic()
        elif source == "STATE_REPAIR_ONLY":
            _repair_state_only()
        elif source == "STATE_CLEAR_FENCE_ONLY":
            _clear_fence_only()
        elif source == "STATE_REVERSE_AUDIT_ONLY":
            _reverse_audit_only()
        elif source == "STATE_REVERSE_RECONCILE_ONLY":
            _reverse_reconcile_only()
        elif source == "STATE_NET_INVESTED_REPAIR_ONLY":
            _net_invested_repair_only()
        elif source == "STATE_ASSUME_REVERSE_EXPIRY_ONLY":
            _assume_reverse_expiry_only()
        elif source == "FORCE_T_REINFERENCE":
            _repair_reinference()
        elif source == "{SYMBOL}_FORCE_T":
            _repair_force_t()
        else:
            # 방어적 — 위 집계에서 걸러지지만 추가 안전장치
            print(f"[REPAIR] 처리할 수 없는 소스: {source}")
            sys.exit(1)

    print(f"[REPAIR] {source} 완료.")


def main():
    """
    자동매매 봇의 메인 실행 함수입니다.

    SYMBOLS 설정에 있는 종목을 순서대로 처리합니다.
    한 종목이 실패해도 나머지 종목은 계속 처리됩니다.
    """
    # ── 모드 불변식 ──
    # TRADE_MODE는 DRY(프리뷰, 상태 저장 없음) / LIVE(실주문) / REPAIR(유지보수) 중 하나입니다.
    # REPAIR는 액션 소스(REPAIR_ACTION 또는 레거시 STATE_*_ONLY/FORCE_T*)가 정확히
    # 하나일 때만 실행하며, 0개 또는 2개 이상이면 _dispatch_repair()가 sys.exit(1)로 중단합니다.
    # REPAIR 체크를 레거시 STATE_*_ONLY보다 먼저 수행해 REPAIR 모드의 단일 액션
    # 불변식을 항상 강제합니다.
    if TRADE_MODE == "REPAIR":
        _dispatch_repair()
        return

    if STATE_DIAGNOSTIC_ONLY:
        _print_state_diagnostic()
        return

    if STATE_REPAIR_ONLY:
        _repair_state_only()
        return
    if STATE_CLEAR_FENCE_ONLY:
        _clear_fence_only()
        return

    # 리버스 주문 감사/체결 반영/reverse net_invested 복구는 서로 동시에 설정할 수 없습니다.
    _exclusive_modes = {
        "STATE_REVERSE_AUDIT_ONLY": STATE_REVERSE_AUDIT_ONLY,
        "STATE_REVERSE_RECONCILE_ONLY": STATE_REVERSE_RECONCILE_ONLY,
        "STATE_NET_INVESTED_REPAIR_ONLY": STATE_NET_INVESTED_REPAIR_ONLY,
        "STATE_ASSUME_REVERSE_EXPIRY_ONLY": STATE_ASSUME_REVERSE_EXPIRY_ONLY,
        "STATE_SET_CYCLE_START_ONLY": STATE_SET_CYCLE_START_ONLY,
    }
    _enabled = [name for name, enabled in _exclusive_modes.items() if enabled]
    if len(_enabled) > 1:
        raise RuntimeError(
            f"상호 배타적인 복구 모드가 동시에 설정되었습니다: {', '.join(_enabled)}"
        )
    if STATE_REVERSE_AUDIT_ONLY:
        _reverse_audit_only()
        return
    if STATE_REVERSE_RECONCILE_ONLY:
        _reverse_reconcile_only()
        return
    if STATE_NET_INVESTED_REPAIR_ONLY:
        _net_invested_repair_only()
        return
    if STATE_ASSUME_REVERSE_EXPIRY_ONLY:
        _assume_reverse_expiry_only()
        return
    if STATE_SET_CYCLE_START_ONLY:
        _set_cycle_start_only()
        return

    # 전체 T 재추정은 주문을 절대 발생시키지 않아야 합니다.
    # 브로커 생성 전 config와 이 모듈의 모드를 함께 DRY로 고정합니다.
    import config as runtime_config
    if runtime_config.FORCE_T_REINFERENCE and runtime_config.TRADE_MODE == "LIVE":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[T 보정] FORCE_T_REINFERENCE=true → 브로커 생성 전 DRY 모드로 고정합니다.")

    broker = create_broker()
    try:
        # stdout 버퍼링 해제: GitHub Actions 또는 로컬에서 출력이 한꺼번에 나오지 않고
        # 줄 단위로 실시간 표시되도록 합니다. (PYTHONUNBUFFERED 환경변수와 동일한 효과)
        sys.stdout.reconfigure(line_buffering=True)

        print("\n" + "=" * 60)
        print("자동매매 봇 시작")
        print("=" * 60)

        notify("🚀 무한매수 자동매매 시작")

        # ========================================
        # 휴장일 체크 — 조기종료
        # ========================================
        if not broker.is_trading_day():
            today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %A")
            print(f"\n📅 오늘({today_str})은 미국 증시 휴장일입니다. 프로그램을 종료합니다.")
            notify("📅 오늘은 미국 증시 휴장일입니다. 자동매매를 실행하지 않습니다.")
            return

        # ========================================
        # 설정 정보 출력
        # ========================================
        print("\n[설정 정보]")
        print(f"거래 모드: {TRADE_MODE}")
        print("종목 목록:")
        for cfg in SYMBOLS:
            seed_info = f", 시드: ${cfg['seed']:.0f}"
            print(f"  - {cfg['symbol']}({cfg['exchange']}): "
                  f"분할={cfg['splits']}, "
                  f"타입={cfg['symbol_type']}"
                  f"{seed_info}")

        if TRADE_MODE == "LIVE":
            for cfg in SYMBOLS:
                preflight_state = load_state(cfg["symbol"])
                if preflight_state.get("_state_unavailable"):
                    # 첫 실행 부트스트랩: state가 없어도 보유 0 + 이력 0이면 LIVE 허용
                    _pf_symbol = cfg["symbol"]
                    _pf_exchange = cfg["exchange"]
                    try:
                        _pf_balance = broker.get_balance(_pf_symbol, _pf_exchange)
                        _pf_qty = _pf_balance.quantity if _pf_balance else 0
                        _pf_history = broker.get_order_history(
                            _pf_symbol, _pf_exchange, days=30, verbose=False
                        )
                        _pf_filled = sum(
                            1 for o in _pf_history
                            if int(float(o.get("ft_ccld_qty", "0") or 0)) > 0
                        )
                    except Exception as _pf_err:
                        raise RuntimeError(
                            f"LIVE preflight 실패: {_pf_symbol} state를 확인할 수 없습니다. "
                            f"보유/이력 조회 실패({_pf_err}) → DRY+FORCE_T_REINFERENCE 필요"
                        )
                    if _pf_qty in (0, None) and _pf_filled == 0:
                        print(
                            f"[preflight] {_pf_symbol} 첫 실행 확인(보유 0, 이력 0) "
                            f"→ LIVE 부트스트랩 허용"
                        )
                    else:
                        raise RuntimeError(
                            f"LIVE preflight 실패: {_pf_symbol} state를 확인할 수 없습니다. "
                            f"보유/이력이 있어 DRY+FORCE_T_REINFERENCE 필요"
                        )
                # 주문 fence는 종목별 복구 대상입니다. 전체 중단 없이
                # run_one_symbol()이 이전 세션 이력 reconciliation으로 복구를 시도합니다.
                if preflight_state.get("pending_order_intent") or preflight_state.get("pending_order_batch"):
                    print(
                        f"[fence 감지] {cfg['symbol']} 이전 주문 fence가 남아 있어 "
                        f"해당 종목 reconciliation/복구를 시도합니다. (다른 종목은 진행)"
                    )
                if preflight_state.get("balance_mismatch"):
                    raise RuntimeError(
                        f"LIVE preflight 실패: {cfg['symbol']} balance_mismatch가 남아 있습니다."
                    )

        # ========================================
        # 종목별 순차 처리
        # ========================================
        run_results: list[dict] = []
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            try:
                result = run_one_symbol(broker, symbol_config)
                if result:
                    run_results.append(result)

            except GlobalScopedError:
                # 공통 장애(상태 파일 저장 실패 등)는 전체 실행을 중단합니다.
                raise

            except SymbolScopedError as error:
                # 해당 종목만 중단하고 다음 종목으로 계속 진행합니다.
                print(f"\n✗ {symbol} 처리 중 오류 발생: {str(error)}")
                if TRADE_MODE != "DRY" or "잔고 부족:" not in str(error):
                    notify(f"⚠️ {symbol} 오류\n\n{str(error)}", urgent=True)
                run_results.append({
                    "symbol": symbol, "executed": 0, "reserved": 0,
                    "failed": 1, "errors": [str(error)],
                })

            except Exception as error:
                # 한 종목이 실패해도 나머지 종목은 계속 처리합니다
                print(f"\n✗ {symbol} 처리 중 오류 발생: {str(error)}")
                if TRADE_MODE != "DRY" or "잔고 부족:" not in str(error):
                    notify(f"⚠️ {symbol} 오류\n\n{str(error)}", urgent=True)
                run_results.append({
                    "symbol": symbol, "executed": 0, "reserved": 0,
                    "failed": 1, "errors": [str(error)],
                })
                # 공통/치명 사안(상태 파일, T 불일치, preflight 실패 등)은 전체 중단합니다.
                if any(marker in str(error) for marker in (
                    "checkpoint 실패",
                    "상태 파일을 확인할 수 없어",
                    "T=0인데 브로커 잔고",
                    "이력 포지션과 브로커 잔고가 불일치",
                    "LIVE preflight 실패",
                )):
                    raise

            if len(SYMBOLS) > 1:
                time.sleep(1)

        # ── 실행 결과 집계 ────────────────────────────────────────
        # 자동 운영에서는 exit 0만 보고 성공으로 판단하면 안 됩니다.
        # 종목 실패/주문 실패가 있으면 종료코드 1 로 알립니다.
        total_executed = sum(r.get("executed", 0) for r in run_results)
        total_reserved = sum(r.get("reserved", 0) for r in run_results)
        total_failed = sum(r.get("failed", 0) for r in run_results)
        print("\n" + "=" * 60)
        print("[실행 결과]")
        print("=" * 60)
        for r in run_results:
            print(
                f"  {r.get('symbol')}: 성공 {r.get('executed', 0)}건 / "
                f"예약 {r.get('reserved', 0)}건 / 실패 {r.get('failed', 0)}건"
            )
        print(
            f"  합계: 성공 {total_executed}건 / 예약 {total_reserved}건 / 실패 {total_failed}건"
        )

        if total_failed > 0:
            summary = "\n".join(
                f"- {r.get('symbol')}: {r.get('failed', 0)}건 실패"
                + (f" — {r['errors'][0][:120]}" if r.get("errors") else "")
                for r in run_results if r.get("failed", 0)
            )
            message = f"""⚠️ 자동매매 일부 실패
{summary}
성공 {total_executed}건 / 예약 {total_reserved}건 / 실패 {total_failed}건"""
            notify(message, urgent=True)
            print(f"\n✗ 실패가 있어 종료코드 1 로 종료합니다.")
            sys.exit(1)

        print("\n프로그램을 정상적으로 종료합니다.")

    except Exception as error:
        print("\n" + "=" * 60)
        print("✗ 프로그램 실행 중 치명적 에러 발생")
        print("=" * 60)
        print(f"에러: {str(error)}")

        message = f"""🚨 치명적 에러 발생

{str(error)}"""
        notify(message, urgent=True)

        import traceback

        print("\n[상세 에러 정보]")
        print(traceback.format_exc())

        print("\n프로그램을 에러와 함께 종료합니다.")
        sys.exit(1)
    finally:
        broker.close()


if __name__ == "__main__":
    main()
