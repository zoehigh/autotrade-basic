"""
자동매매 봇 메인 실행 파일

이 프로그램은 다음 작업을 순서대로 수행합니다:
1. 환경변수에서 설정값을 읽어옵니다 (.env 파일)
2. 전략 함수를 실행하여 주문 목록을 생성하고 출력합니다
3. 생성된 주문을 실행합니다

프로그램 실행 중 발생하는 모든 에러는 catch되어 출력됩니다.
"""

import hmac
import json
import math
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.append("src")

from datetime import datetime
from zoneinfo import ZoneInfo

from config import SYMBOLS, TRADE_MODE, COMMISSION_RATE, REINVEST, BROKER, BROKER_MODE, LS_DEMO_BYPASS_BUGS, ORDER_HISTORY_VERBOSE, REPAIR_ACTION
from broker import create_broker, validate_run_phase
from broker.base import Broker, OrderResult, OrderNotAcceptedError, ReadOnlyBroker
from pipeline import (
    SettledState,
    _session_ymd,
    approve_plan_file,
    assert_no_pending_fence,
    assert_reconciliation_current,
    build_order_plan,
    deferred_record_path,
    effective_tolerance_policy,
    evidence_file_for_run,
    global_lock,
    load_approval,
    price_tolerance,
    read_deferred_record,
    read_order_plan_file,
    read_transmitted_plan,
    settle_positions,
    transmitted_plan_problems,
    settled_from_state,
    verify_approval,
    verify_evidence_file_with_ops,
    write_order_plan,
)
from pipeline_execute import execute_approved_plan
from broker.market_utils import get_kst_now, is_us_dst, is_us_trading_day
from strategy import 무한매수법_V4, adjust_price_to_tick
from state import FILL_MARKER_VERSION, log_salt_hex, mask_odno, load_state, save_state, save_states, update_T_from_history, compute_position_from_history, register_order_meta_in_state, canonical_state_hash, reset_settlement_ledger, record_settlement_t_adjustment, _order_real_kst_date, _order_us_session_date, _fill_amount, _has_unconfirmed_amount_fills
# 주문번호 매핑 원장은 state 를 거치지 않고 log_mask 에서 직접 가져옵니다
# (state 와 **같은 모듈 객체**여야 salt·원장이 하나로 유지됩니다 — `src.log_mask` import 금지).
from log_mask import flush_odno_ledger
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

# AUTO_EXECUTE(opt-in): PREVIEW 후 자동 APPROVE → EXECUTE 연쇄 실행.
# 디폴트 false(수동 승인 유지). true 로 설정하면 사람이 plan_id 를 복사하지 않아도
# 정산·전략·승인·실행을 연속으로 수행하며, 차단 사유(hold/mismatch/tolerance/세션
# 드리프트 등)가 있으면 해당 종목만 AUTO_BLOCKED 로 중단하고 다음 종목은 계속 진행합니다.
# VERIFY 는 별도 런(18:45 등) 으로 유지합니다.
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "").strip().lower() in ("1", "true", "yes", "on")


# ── 공개 로그 주문번호 마스킹 도우미 ──────────────────────────────────
# 주문 접수·취소·요약 로그는 공개(GitHub Actions 로그/일반 알림)에 남으므로
# 주문번호 원문을 쓰지 않습니다. 단, 마스킹이 진단 정보를 지우면 안 되므로
# **주문번호 참조만** 바꾸고 나머지 문구(수량·가격·사유)는 그대로 둡니다.
# 라벨 뒤 구분자(`:`/`=`)는 필수입니다. 구분자 없이 라벨과 영단어가 나란히 오는
# 문장(`ordno is required`)은 주문번호 참조가 아닙니다.
# 값은 **숫자를 1개 이상** 포함해야 합니다. 실제 6개 브로커 주문번호(KIS/KIWOOM/LS/
# NHPLUG 숫자, TOSS 영숫자, MERITZ `YYYYMMDD:NNNN`)는 모두 숫자를 포함하므로,
# 숫자가 없는 값(`odno=odno`, `odno=None`, `odno=unknown`)은 코드 식별자·문장으로
# 보고 그대로 둡니다.
# 공백은 스페이스·탭만 허용합니다. `\s*` 는 줄바꿈까지 삼켜서 다음 줄의 숫자를
# 값으로 오인할 수 있습니다 (`주문번호\n123`).
_ODNO_REFERENCE = re.compile(
    r"(?P<label>(?:odno|ordno|주문[ \t]*번호|예약[ \t]*주문[ \t]*번호|예약[ \t]*번호)[ \t]*[:=][ \t]*)"
    # 라벨이 붙은 값은 **최소 길이 제한을 두지 않습니다**. KIS 처럼 앞자리 0 을 떼면
    # 주문번호가 `1`·`12` 같은 1~3자리가 되는데, `{4,}` 로 막으면 그 참조가 그대로
    # 공개 로그에 남습니다 (`odno=1`, `주문번호: 123`).
    r"(?P<id>(?=[0-9A-Za-z:_-]*[0-9])[0-9A-Za-z:_-]+)",
    re.IGNORECASE,
)

# 라벨 없이 등장하는 **합성 주문번호**(메리츠 `YYYYMMDD:주문번호`)도 주문 식별자입니다.
# 예: "20261028:1234 주문이 거부되었습니다". 8자리 날짜 뒤 콜론이 있어야만 매칭해
# 수량·가격·에러코드 같은 일반 숫자는 건드리지 않습니다.
_SYNTHETIC_ODNO_REFERENCE = re.compile(r"(?<![\dA-Za-z:])(?P<id>\d{8}:\d+)(?![\dA-Za-z:])")


def _mask_one_reference(match):
    """`_ODNO_REFERENCE` 매치 하나를 마스킹합니다 (멱등).

    이미 마스킹된 토큰(`odno=ord#…`)이 다시 들어오면 손대지 않고 돌려줍니다.
    `ord` 뒤에 `#`이 오거나 값이 `ord#…`으로 시작하면 기존 토큰이므로,
    `mask_odno("ord")` 같은 엉뚱한 토큰을 만들지 않습니다 (원장 대조 깨짐 방지).
    """
    label = match.group("label")
    oid = match.group("id")
    lowered = oid.lower()
    if lowered.startswith("ord#"):
        return match.group(0)
    if lowered == "ord" and match.string[match.end():match.end() + 1] == "#":
        return match.group(0)
    return label + mask_odno(oid)


def _mask_odno_references(text):
    """자유 텍스트(브로커 에러 사유 등)에서 주문번호 참조만 마스킹합니다.

    라벨이 붙은 참조(`odno=…`, `주문번호: …`)와 라벨 없는 합성번호(`YYYYMMDD:NNNN`)를
    같은 가명 토큰으로 바꿉니다. 수량·가격·사유 같은 나머지 진단 정보는 그대로 둡니다.
    이미 마스킹된 텍스트에 다시 걸어도 토큰이 바뀌지 않습니다 (멱등).
    """
    if not text:
        return text

    masked = _ODNO_REFERENCE.sub(_mask_one_reference, str(text))
    return _SYNTHETIC_ODNO_REFERENCE.sub(
        lambda match: mask_odno(match.group("id")),
        masked,
    )


def _mask_token_in(text, odno):
    """접수 알림 twin 메시지의 **라벨 있는 주문번호 필드만** 가명 토큰으로 치환합니다.

    `str.replace(odno)` 같은 무제한 치환은 주문번호가 수량·가격·시각과 겹칠 때
    그 값까지 망가뜨립니다 (예: KIS는 앞자리 0을 떼므로 주문번호 `1`이 `수량: 1주`를
    오염시킴). `예약주문번호:`·`주문번호:` 라벨이 붙은 자리만 바꿉니다 (긴 라벨 우선).
    """
    if not text or not odno:
        return text

    token = mask_odno(odno)
    raw = str(odno)
    masked = str(text)
    for label in ("예약주문번호", "주문번호"):
        masked = masked.replace(f"{label}: {raw}", f"{label}: {token}")
    return masked


def _safe_error_text(error, limit=120):
    """예외 메시지를 공개 로그용으로 정리합니다 (주문번호 참조 마스킹 + 길이 제한)."""
    return _mask_odno_references(str(error))[:limit]


# 주문번호 매핑 원장 flush 여부 (프로세스당 1회 — 종목 수와 무관하게 한 파일에 누적).
_ODNO_LEDGER_FLUSHED = False


def _flush_odno_ledger_once():
    """이번 실행의 주문번호 매핑 원장을 프로세스당 1회만 기록합니다.

    정산 근거(`settlement-*.json`)와 같은 시점(실행 종료)에 남기므로 게이팅이 같습니다.
    원장이 비어 있으면 `flush_odno_ledger()` 가 파일을 만들지 않습니다.
    두 번 불려도(예: 실패 경로 + finally) 중복 기록하지 않습니다.
    """
    global _ODNO_LEDGER_FLUSHED
    if _ODNO_LEDGER_FLUSHED:
        return None
    _ODNO_LEDGER_FLUSHED = True
    return flush_odno_ledger()


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
        "effective_seed": float(state.get("effective_seed", 0.0) or 0.0),
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
            # 사이클 리포트 재계산용 원시값 — 검증기가 날짜 경계/금액을 독립
            # 재계산할 수 있도록 ord_dt(미국 영업일)와 체결단가를 함께 남깁니다.
            "ord_dt": str(row.get("ord_dt", "") or "").replace("-", "")[:8],
            # ord_datetime_utc 가 없는 행도 검증기가 (시각, 주문번호) 순서를
            # 복원할 수 있도록 원시 주문시각을 함께 남깁니다.
            "ord_tmd": str(row.get("ord_tmd", "") or ""),
            "price": float(row.get("ft_ccld_unpr3", "0") or 0.0),
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


def _force_t_mode_block_message(mode, symbol_configs):
    """DRY·LIVE 에서 `{SYMBOL}_FORCE_T` 가 설정돼 있으면 중단 사유를 반환합니다 (없으면 빈 문자열).

    FORCE_T 는 T·orders_meta·reverse_mode·정산 원장·워터마크를 덮어쓰고 저장하는 명시적 복구입니다.
    - DRY 는 state 를 저장하지 않는 읽기 전용 미리보기라 적용하면 로컬 .state.json 이 바뀝니다.
    - LIVE 는 보정이 적용된 **같은 실행에서 주문 단계까지** 진행할 수 있어 복구와 주문이 결합됩니다.
    복구·보정은 TRADE_MODE=REPAIR 로만 실행합니다 (주문 없음).
    """
    names = [str(c.get("symbol")) for c in symbol_configs if c.get("force_t") is not None]
    if not names or mode not in ("DRY", "LIVE"):
        return ""
    keys = ", ".join(f"{n}_FORCE_T" for n in names)
    if mode == "LIVE":
        reason = "LIVE 에서는 보정과 주문이 한 실행에 결합되므로 허용하지 않습니다"
    else:
        reason = "DRY 는 state 를 저장하지 않는 읽기 전용 미리보기입니다"
    return (
        f"TRADE_MODE={mode} 에서는 {keys} 를 적용할 수 없습니다 ({reason}). "
        "state 저장·브로커 호출·주문 없이 중단합니다. FORCE_T 는 TRADE_MODE=REPAIR 에서만 허용됩니다."
    )


def _validate_reinference_t(symbol, new_t, max_t):
    """REPAIR 재추정 결과를 저장해도 되는지 검증합니다 (문제가 없으면 빈 문자열).

    - 추정 T 와 MAX_T 는 유한한 유효 숫자여야 합니다 (None 인 MAX_T 는 상한 없음으로 건너뜁니다).
    - T > MAX_T 이면 **저장을 거부**합니다. 상한으로 잘라 저장하면 T 가 그만큼 부풀려진 원인
      (정산 오류 등)을 숨길 수 있으므로 원인 확인을 요구합니다.
    """
    def _is_number(value):
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        )

    if not _is_number(new_t):
        return f"{symbol} 재추정 T 가 유효한 숫자가 아닙니다: {new_t!r}"
    if max_t is None:
        return ""
    if not _is_number(max_t) or max_t < 0:
        return f"{symbol} MAX_T 가 유효한 숫자가 아닙니다: {max_t!r}"
    if new_t > max_t:
        return (
            f"{symbol} 재추정 T={new_t} 가 설정된 상한 MAX_T={max_t} 를 초과합니다 — 저장을 거부합니다 "
            f"(상한으로 잘라 저장하면 정산 오류를 숨길 수 있습니다. 초과 원인을 확인하세요)"
        )
    return ""


def _reinference_hold_reasons(symbol, state):
    """재추정 결과가 '정산 보류' 상태인지 확인하고 사유 목록을 반환합니다 (정상이면 빈 목록).

    보류 상태에서는 `update_T_from_history` 가 메모리를 보류 직전 스냅샷으로 되돌리는데, REPAIR 재추정은
    그 스냅샷을 찍기 전에 `last_updated` 를 비워 두었으므로 **그대로 저장하면 워터마크가 빈 값으로 바뀝니다**.
    따라서 보류 표식이 하나라도 있으면 어떤 종목도 저장하지 않습니다. 주문번호는 마스킹해 출력합니다.
    """
    reasons = []
    if state.get("_settlement_hold"):
        reasons.append(f"정산 보류({state.get('_settlement_hold_reason') or '사유 없음'})")
    if state.get("_pending_amount_odnos"):
        reasons.append(f"체결금액 미확정 주문 {mask_odno(list(state['_pending_amount_odnos'])[:5])}")
    if state.get("_pending_marker_odnos"):
        reasons.append(f"마커 누락·무효 주문 {mask_odno(list(state['_pending_marker_odnos'])[:5])}")
    if state.get("_position_basis_unknown"):
        reasons.append(f"보유수량 기준 미확보({state.get('_position_basis_unknown_reason') or '사유 없음'})")
    return [f"{symbol} {reason}" for reason in reasons]


# ── 주문 접수 알림 표시 도우미 (표시 전용) ──────────────────────────────
# 알림 본문 문구만 만듭니다. 브로커 호출·state 변경·주문은 하지 않으며,
# 넘겨받은 `order` dict 를 절대 수정하지 않습니다.
# 지정가(주문가가 확정된) 유형만 주문가·예상금액을 표시합니다.
_PRICE_QUOTED_ORDER_TYPES = {"LIMIT", "LOC"}


def _is_positive_number(value) -> bool:
    """가격·수량으로 표시할 수 있는 유효한 양수인지 판정합니다 (bool·비유한수 제외)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _format_estimate_amount(amount: float) -> str:
    """예상 주문금액 표기 — 1달러 미만 금액은 소수 4자리까지 유지합니다."""
    if abs(amount) < 1:
        return f"{amount:.4f}"
    return f"{amount:,.2f}"


def _order_detail_lines(symbol, order) -> str:
    """주문 접수 알림의 공통 상세 줄을 만듭니다 (증권사/계좌 환경/종목/구분/유형/수량/주문가/예상금액).

    - 주문가: 지정가 계열(LIMIT/LOC) + 유효한 양수 가격일 때만 원값 그대로 표시합니다
      (`str()` 표기라 1달러 미만 4자리 정밀도가 유지됩니다).
    - 그 외(시장가·가격 없음·0·숫자 아님)는 `가격 미확정(체결가 미정)` 으로만 표시하고
      **예상금액 줄은 넣지 않습니다** (체결가를 알 수 없으므로 시장가로 단정하지 않습니다).
    - 예상 주문금액 = 주문가 × 수량 (수수료 제외, 표시용 추정값).

    표시 전용 — 네트워크 호출도, `order` dict 변경도 없습니다.
    """
    price = order.get("price")
    order_type = str(order.get("order_type") or "")
    quantity = order.get("quantity")
    price_known = (
        order_type.strip().upper() in _PRICE_QUOTED_ORDER_TYPES
        and _is_positive_number(price)
    )

    lines = [
        f"증권사: {BROKER}",
        f"계좌 환경: {BROKER_MODE}",
        f"종목: {symbol}",
        f"매수/매도: {order.get('side')}",
        f"주문 유형: {order_type}",
        f"수량: {quantity}주",
    ]
    if not price_known:
        lines.append("주문가: 가격 미확정(체결가 미정)")
        return "\n".join(lines)

    lines.append(f"주문가: ${price}")
    if _is_positive_number(quantity):
        lines.append(f"예상 주문금액(수수료 제외): ${_format_estimate_amount(price * quantity)}")
    return "\n".join(lines)


def run_one_symbol(broker: Broker, symbol_config, approval=None):
    """단일 종목 통합 실행 (레거시 경로).

    DRY: SETTLE → PREVIEW 만 수행합니다 (기존과 동일, 저장/주문 없음).
    LIVE: **명시적 승인 없이는 실행하지 않습니다** (P0-A). `APPROVAL_PLAN_ID`/
    `APPROVAL_FILE` 또는 `approval` 인자로 승인 파일을 받아야 하며, 이 경로는
    RUN_PHASE 없는 LIVE 를 main()/create_broker() 가 이미 차단하므로 테스트·수동
    호출용입니다.
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]

    # DRY(저장 금지)·LIVE(복구와 주문 결합)에서 FORCE_T 는 어떤 조회·변경보다 먼저 막습니다.
    _force_t_block = _force_t_mode_block_message(TRADE_MODE, [symbol_config])
    if _force_t_block:
        raise RuntimeError(_force_t_block)

    print(f"\n{'=' * 60}")
    print(f"종목 처리 시작: {symbol} ({exchange})")
    print(f"{'=' * 60}")

    # 정산/계획 단계는 조회 전용 브로커만 받습니다.
    read_only_broker = ReadOnlyBroker(broker)

    settled = settle_positions(read_only_broker, symbol_config)
    if settled.halt_reason:
        if settled.halt_reason == "non_trading_day":
            return None
        # halt_reason 에는 주문번호가 섞일 수 있습니다 (예: `_verify_history_complete`
        # 의 "orders_meta 주문 {odno} 이 이력에 없습니다"). 공개 로그 규칙에 따라
        # 출력 직전에 가명 식별자로 바꿉니다 (원문은 비공개 Evidence 에만).
        print(f"  [{symbol}] 정산 보류: {_mask_odno_references(settled.halt_reason)}")
        return None

    plan = build_order_plan(settled, read_only_broker)
    if not plan.orders:
        # 주문이 없어도 리버스 종료 상태 변경은 기존처럼 저장합니다.
        if TRADE_MODE == "LIVE" and plan.strategy_result.get("reverse_exit"):
            save_state(symbol, settled.state)
        return None

    if TRADE_MODE != "LIVE":
        # DRY: 프리뷰만. 승인/주문으로 진행하지 않습니다.
        return None

    plans_dir = os.getenv("PLAN_DIR", "plans")
    approvals_dir = os.getenv("APPROVALS_DIR", "approvals")
    approval_ref = approval or os.getenv("APPROVAL_PLAN_ID") or os.getenv("APPROVAL_FILE")
    # 승인 없이 실행하려는 경우에만 PREVIEW 계획을 남깁니다. 승인 파일이 이미
    # 있으면 PREVIEW 산출물을 덮어쓰지 않습니다 (자동 승인 금지, P0-A).
    plan_file = ""
    if not approval_ref:
        plan_file = write_order_plan(plan, plans_dir=plans_dir)
    if not approval_ref:
        raise RuntimeError(
            f"{symbol} LIVE 실행에는 명시적 승인이 필요합니다 "
            f"(APPROVAL_PLAN_ID/APPROVAL_FILE 또는 approval 인자). "
            f"PREVIEW 계획을 확인한 뒤 APPROVE 하세요: plan={plan.plan_id}, file={plan_file}"
        )

    approval_data = (
        approval_ref
        if isinstance(approval_ref, dict)
        else load_approval(approval_ref, approvals_dir)
    )
    if str(approval_data.get("plan_id") or "") != plan.plan_id:
        raise RuntimeError(
            f"{symbol} 승인 파일이 현재 계획과 일치하지 않습니다 "
            f"(approval={approval_data.get('plan_id')}, plan={plan.plan_id}). "
            "새 PREVIEW → APPROVE 를 실행하세요."
        )
    return execute_approved_plan(
        broker,
        approval_data,
        approvals_dir=approvals_dir,
        state_override=settled.state,
    )


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
        # 브로커 사유에 주문번호 참조가 섞여도 공개 로그에는 원문을 남기지 않습니다.
        safe_text = _mask_odno_references(text)
        if "404" in text or "405" in text:
            print(
                f"  ⚠️ 열린 주문 조회 미지원(HTTP 404/405) — CLOSED 이력만 사용합니다: "
                f"{safe_text[:120]}"
            )
            return [], None
        print(f"  ⚠️ 열린 주문 조회 실패 — 신규 주문을 보류합니다: {safe_text[:160]}")
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
    - 취소 API 미지원 브로커(KIS/KIWOOM/LS/NHPLUG)는 **오늘 세션으로 확정되는**
      미체결 봇 주문이 남아 있으면 신규 주문을 보류합니다 (fail-closed, 중복 방지).
      그런 주문이 없으면 그대로 진행합니다.
      ⚠️ 운영 제약: 잔여 주문이 남은 채 중단된 뒤 재실행하면 **브로커 앱에서 직접
      미체결 주문을 취소**해야 그 종목의 신규 주문이 재개됩니다 (취소 API 가 없어
      봇이 정리할 수단이 없음). 세션이 끝나면 주문이 만료되므로 다음 세션은 정상입니다.

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
        # 메타가 비어 있어도 열린 주문이 있으면 소유/상태를 확정할 수 없으므로
        # 신규 주문을 내지 않습니다 (이전 early-return 으로 미체결을 덮어쓰던 경로).
        if open_rows:
            raise SymbolScopedError(
                f"{symbol} 봇 주문 메타 없이 열린 주문이 남아 있어 신규 주문을 보류합니다 "
                f"({len(open_rows)}건). 주문 상태를 확인/정리한 뒤 재실행하세요."
            )
        return 0

    cancel = getattr(broker, "cancel_order", None)

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

    # ── 취소 API 미지원 브로커 — fail-closed ──────────────────────────
    # `cancel_order` 가 없는 브로커(KIS/KIWOOM/LS/NHPLUG — 어댑터 전수 확인)는
    # 잔여 주문을 정리할 방법이 **없습니다**. 예전에는 여기서 조용히 0 을 반환해
    # 신규 주문을 그대로 냈기 때문에, 같은 세션에서 EXECUTE 가 두 번 돌거나
    # (재시도·수동 재실행) 이전 실행이 주문을 남긴 채 끝나면 **중복 주문**이
    # 접수될 수 있었습니다 (매도는 보유량 초과, 매수는 시드 초과 위험).
    # 그래서 "오늘 세션으로 확정되는" 미체결 봇 주문이 남아 있으면 신규 주문을
    # 보류합니다 (취소 수단도, 중복을 막을 다른 방법도 없으므로 fail-closed).
    if not callable(cancel):
        session_confirmed = []
        session_unknown = []
        for odno, _status, _remaining in targets:
            row = by_odno.get(odno) or {}
            if _order_us_session_date(row) == today_session:
                session_confirmed.append(odno)
            else:
                # 위 대상 선정이 KST 날짜 호환 경로로만 고른 행 — 세션을 확정할 수 없습니다.
                session_unknown.append(odno)
        if session_confirmed:
            raise SymbolScopedError(
                f"{symbol} 취소 API 미지원 브로커인데 오늘 세션 미체결 봇 주문이 남아 "
                f"있어 신규 주문을 보류합니다 "
                f"({mask_odno(sorted(session_confirmed))}). 이전 주문이 살아 있으면 "
                "중복 접수되므로, 브로커에서 미체결 주문을 직접 취소한 뒤 재실행하세요."
            )
        if session_unknown:
            # ── 세션 미확정 행은 **막지 않습니다** (의도적, 아래 근거) ────────────
            # 취소 지원 브로커(TOSS/MERITZ)는 같은 행을 그냥 취소하므로 동작이 다르지만,
            # 여기서 fail-closed 로 바꾸면 다음 두 가지가 깨집니다.
            #  1) 세션을 확정할 수 없으면 "아직 살아 있는 오늘 주문"과 "이미 만료된 이전
            #     주문"을 구분할 수 없습니다. 구분 없이 막으면 실제로는 만료된 주문 때문에
            #     그 종목의 정상 재시도가 영구 차단됩니다.
            #  2) 6개 어댑터 모두 주문이력에 ord_tmd 를 채우므로 이 경로는 **방어적 경로**
            #     입니다(비정상·레거시 행에서만 발생). 계약 테스트
            #     `test_broker_without_cancel_support_is_ignored` 도 "세션 정보가 없는 행은
            #     건너뛴다"를 고정하고 있어, 코드를 계약 쪽에 맞춥니다.
            # 대신 **조용히** 넘어가지 않도록 경고를 남깁니다 (운영자가 상태를 알 수 있게).
            print(
                f"[경고] {symbol} 취소 API 미지원 브로커 — 세션을 확정할 수 없는 미체결 "
                f"후보 {len(session_unknown)}건은 취소도 보류도 하지 않고 건너뜁니다 "
                f"({mask_odno(sorted(session_unknown))}). 오늘 주문이라면 브로커 앱에서 "
                "직접 취소하세요 (중복 접수 위험)."
            )
        print(
            "[잔여 주문 정리] 취소 API 미지원 브로커 — 오늘 세션으로 확정되는 미체결 "
            "주문이 없어 그대로 진행합니다."
        )
        return 0

    print(f"\n[잔여 주문 정리] 봇 소유 미체결 주문 {len(targets)}건 취소")
    for odno, status, remaining in targets:
        print(f"  - {mask_odno(odno)} ({status}, 잔량 {remaining}주)")

    rejected: list[tuple[str, str]] = []
    for odno, _status, _remaining in targets:
        try:
            cancel(odno, symbol=symbol, exchange=exchange)
        except Exception as error:
            # 실패해도 나머지 취소는 계속합니다 (부분 취소 상태로 남지 않도록).
            rejected.append((odno, str(error)))
            # 브로커 사유에 섞인 주문번호 참조만 마스킹합니다 (나머지 진단 정보 유지).
            masked_reason = _mask_odno_references(str(error))[:160]
            print(f"  ✗ 취소 요청 실패: {mask_odno(odno)} — {masked_reason}")

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
            print(f"  ⚠️ 이력 재조회 실패 (확인 시도 {attempt}): {_safe_error_text(error, 120)}")
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
            + mask_odno(filled_during_cancel)
        )

    if still_open:
        detail = []
        for odno in still_open:
            reason = next((m for o, m in rejected if o == odno), "취소 접수 후에도 잔량 유지")
            # 주문번호는 가명 토큰으로, 브로커 사유는 주문번호 참조만 마스킹해 그대로 남깁니다.
            masked_reason = _mask_odno_references(reason)[:60]
            detail.append(f"{mask_odno(odno)}({masked_reason})")
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
        last_ordno = state.get("last_processed_ordno", "")
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
            f"reverse_order_ids={mask_odno(reverse_order_ids)}, "
            f"order_meta_ids={mask_odno(sorted(state.get('orders_meta', {}).keys()))}, "
            f"last_updated={state.get('last_updated', '')}, "
            f"last_processed_ordno={mask_odno(last_ordno) if last_ordno else ''}, "
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


def _recover_order_fence(state, symbol, live_qty, persist=True):
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
    if persist:
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


# FORCE_T 저장 전에 반드시 해소돼 있어야 하는 상태 (balance_mismatch 는 FORCE_T 가
# 의도적으로 지우는 값이라 제외).
_FORCE_T_BLOCKING_KEYS = (
    ("pending_order_intent", "주문 intent fence"),
    ("pending_order_batch", "주문 batch fence"),
    ("_pending_amount_odnos", "체결금액 미확정 주문"),
    ("_pending_marker_odnos", "마커 누락·무효 주문"),
    ("_settlement_hold", "정산 보류"),
    ("_position_basis_unknown", "보유수량 기준 미확보"),
)


def _force_t_precheck_problems(state):
    """FORCE_T 적용 전 차단 조건을 문자열 목록으로 돌려줍니다."""
    return [label for key, label in _FORCE_T_BLOCKING_KEYS if state.get(key)]


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
        print(f"[상태 복구] 리버스 주문 메타데이터 보존(archived): {mask_odno(reverse_order_ids)}")
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

    **전 종목을 먼저 재추정·검증한 뒤, `.state.json` 을 한 번만(원자적으로) 저장합니다.**
    - 검증 실패(재추정 T 가 MAX_T 초과, 숫자 오류, 정산 보류 표식)가 한 종목이라도 있으면
      어떤 종목도 저장하지 않고 종료 코드 1 로 끝납니다 — 일부 종목만 먼저 저장되는 부분 복구 방지.
    - 검증은 통과했지만 파일 저장이 실패하면 종료 코드 2 로 끝납니다. 임시 파일에 쓴 뒤 교체하므로
      원본 상태 파일은 그대로입니다 (검증 실패와 구분).
    - T 를 MAX_T 로 잘라 저장하지 않습니다(정산 오류 은폐 방지).
    """
    import config as runtime_config
    # REPAIR는 LIVE이더라도 DRY로 고정 (T 재추정 중 주문 방지)
    if runtime_config.TRADE_MODE != "DRY":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[REPAIR] REINFERENCE — 브로커 생성 전 DRY 모드로 고정합니다.")

    broker = create_broker()
    try:
        planned = {}   # {symbol: 재추정된 state}
        failures = []
        for symbol_config in SYMBOLS:
            symbol = symbol_config["symbol"]
            exchange = symbol_config["exchange"]
            max_t = symbol_config.get("max_t")
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
            print(f"[REPAIR:REINFERENCE] {symbol} 재추정 T={state['T']}, 상한 MAX_T={max_t}")
            failures.extend(_reinference_hold_reasons(symbol, state))
            problem = _validate_reinference_t(symbol, state["T"], max_t)
            if problem:
                failures.append(problem)
            planned[symbol] = state

        if failures:
            print("\n[REPAIR:REINFERENCE] 검증 실패 — 어떤 종목도 저장하지 않습니다 (state 변경 없음, 주문 0건):")
            for problem in failures:
                print(f"  - {problem}")
            sys.exit(1)

        # 전 종목 검증 통과 → 파일 쓰기 1회 (원자적)
        try:
            save_states(planned)
        except Exception as save_error:
            print(
                f"\n[REPAIR:REINFERENCE] 저장 실패(파일 쓰기) — 검증은 통과했지만 상태 파일을 저장하지 못했습니다. "
                f"임시 파일 교체 방식이라 기존 상태 파일은 변경되지 않았습니다: {str(save_error)[:200]}"
            )
            sys.exit(2)
        for symbol, state in planned.items():
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

    FORCE_T 는 운영자가 이력을 확인한 뒤 쓰는 수동 보정이지만, 저장 전에
    fence/미확정 정산/재정산 보류가 없는지 검증합니다. 검증에 실패하면
    해당 종목은 저장하지 않고 건너뜁니다 (조기 저장 금지).
    """
    # 적용할 종목별 값이 하나도 없으면 "성공처럼" 끝내지 않고 명시적으로 실패합니다.
    # (예: REPAIR_ACTION=FORCE_T 단독 — 값은 {SYMBOL}_FORCE_T 에서만 읽는데 함께 설정하면 작업 소스 2개로 중단)
    if not any(c.get("force_t") is not None for c in SYMBOLS):
        print(
            "[REPAIR:FORCE_T] 적용할 {SYMBOL}_FORCE_T 값이 없습니다 — 아무것도 적용되지 않았습니다 (실패).\n"
            "  REPAIR_ACTION=FORCE_T 단독 호출은 지원되지 않습니다. 사용법: TRADE_MODE=REPAIR TQQQ_FORCE_T=29 "
            "(REPAIR_ACTION 은 비워 둡니다)."
        )
        sys.exit(1)

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

            blocking = _force_t_precheck_problems(state)
            if blocking:
                print(
                    f"[REPAIR:FORCE_T] {symbol} 미해결 상태가 있어 FORCE_T 적용 불가 — "
                    f"저장하지 않고 건너뜁니다: {', '.join(blocking)}"
                )
                continue

            old_T = state["T"]
            candidate = deepcopy(state)
            candidate["T"] = force_t
            candidate.pop("balance_mismatch", None)
            candidate["orders_meta"] = {}
            candidate["additional_loc_odno"] = []
            candidate["reverse_mode"] = {}
            reset_settlement_ledger(candidate, force_t)

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
                candidate["last_updated"] = latest_ord_dt
                latest_order = next(
                    o for o in order_history
                    if o.get("ord_datetime_utc") == latest_ord_dt
                )
                candidate["last_processed_ordno"] = latest_order.get("odno", "")
            else:
                candidate["last_updated"] = datetime.now(ZoneInfo("UTC")).isoformat()
                candidate["last_processed_ordno"] = ""

            if force_t == 0:
                candidate["cycle_start_date"] = ""
                candidate["net_invested"] = 0.0
                candidate["net_invested_status"] = "valid"

            # ── 저장 전 검증 (조기 저장 금지) ──
            # FORCE_T 로 T 를 덮기 전에 이력/잔고 재정산이 보류를 만들지 않는지
            # 확인합니다. 검증에 실패하면 어떤 값도 저장하지 않습니다.
            try:
                assert_no_pending_fence(candidate, symbol)
                try:
                    live_balance = broker.get_balance(symbol, exchange)
                    # 브로커 계약상 None 은 "조회 실패"가 아니라 **보유 0주 확정**입니다
                    # (실패는 예외). 0 으로 정규화해야 전체 이력 재추정이 돌아
                    # 정산 보류를 저장 전에 발견할 수 있습니다 (다른 정산 경로와 동일).
                    live_qty = live_balance.quantity if live_balance else 0
                except Exception as balance_error:
                    # 잔고를 확인하지 못하면 position_qty 같은 저장값으로
                    # _position_basis_unknown 보류를 피해갈 수 있습니다.
                    # 보유수량 미확인 상태로 강제 T 를 확정하지 않습니다 (fail-closed).
                    print(
                        f"[REPAIR:FORCE_T] {symbol} 잔고 조회 실패 — 보유수량을 "
                        f"확인할 수 없어 FORCE_T 를 저장하지 않습니다: {balance_error}"
                    )
                    continue
                reconciled = update_T_from_history(
                    symbol, deepcopy(candidate), order_history, balance_qty=live_qty
                )
                holds = (
                    reconciled.get("_pending_amount_odnos")
                    or reconciled.get("_pending_marker_odnos")
                    or reconciled.get("_settlement_hold")
                    or reconciled.get("_position_basis_unknown")
                )
                if holds:
                    raise RuntimeError(
                        "재정산 보류(체결/정산 미확정): "
                        f"{reconciled.get('_settlement_hold_reason', '근거 부족')}"
                    )
            except Exception as validation_error:
                print(
                    f"[REPAIR:FORCE_T] {symbol} 저장 전 검증 실패 — FORCE_T 를 "
                    f"저장하지 않습니다: {validation_error}"
                )
                continue

            save_state(symbol, candidate)
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



# ═══════════════════════════════════════════════════════════════════════
# RUN_PHASE 단계 디스패처 (SETTLE / PREVIEW / APPROVE / EXECUTE / VERIFY)
# ═══════════════════════════════════════════════════════════════════════

def _settled_from_state(symbol_config):
    """SETTLE 가 저장해 둔 상태로 PREVIEW/APPROVE 용 SettledState 를 만듭니다."""
    return settled_from_state(symbol_config)


def _approval_recovery_outcome(approval):
    """EXECUTE 가 남긴 결과 파일의 outcome 을 읽습니다 (없으면 '').

    outcome 은 executed(정상 실행) / execution-failed(실행 실패·부분실패) /
    approval-skipped(승인만 되고 실행 안 됨) 중 하나입니다 (P1-C).
    """
    path = str(approval.get("_path") or "")
    if not path:
        return ""
    try:
        data = json.loads(Path(path + ".recovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("outcome") or "")


def _parse_row_int(value, default=None):
    """브로커 이력의 문자열 수량을 int 로 파싱합니다 (실패 시 default)."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _row_side(row):
    """이력 행의 매수/매도 구분을 표준값(BUY/SELL)으로 정규화합니다."""
    token = str(
        row.get("sll_buy_dvsn_cd_name") or row.get("side") or ""
    ).strip()
    if token in ("매수", "BUY", "buy"):
        return "BUY"
    if token in ("매도", "SELL", "sell"):
        return "SELL"
    return ""


def _plan_order_metas(state, plan_id):
    """state.orders_meta 에서 승인 계획(plan_id)으로 실행된 주문을 순번별로 모읍니다."""
    found = {}
    for odno, meta in (state.get("orders_meta") or {}).items():
        if str((meta or {}).get("plan_id") or "") != str(plan_id or ""):
            continue
        index = _parse_row_int((meta or {}).get("plan_order_index"), -1)
        if index is None or index < 1:
            continue
        found.setdefault(index, (str(odno), meta))
    return found


def _approved_plan_odnos(state, plan_id):
    """승인 계획으로 실행된 주문번호 집합 (VERIFY 의 체결 귀속 판정용)."""
    return {
        odno for odno, _meta in _plan_order_metas(state, plan_id).values()
    }


# 브로커 이력이 명시적인 주문가/주문유형을 함께 반환하는 경우 비교합니다.
_ROW_ORDER_PRICE_KEYS = (
    "_order_price",  # MERITZ 정규화 필드 (ovrs_stck_oder_prc)
    "ord_unpr",
    "ord_price",
    "order_price",
    "price",
)
_ROW_ORDER_TYPE_KEYS = ("order_type", "ord_type", "orderType")
_ROW_ORIGINAL_QTY_KEYS = ("ft_ord_qty", "ord_qty", "order_qty", "qty")
_KNOWN_ORDER_TYPES = {"LIMIT", "LOC", "MOC", "MOO", "LOO", "MARKET", "MKT"}
_MERITZ_ORDER_TYPE_BY_CLASS = {
    "01": "LIMIT",
    "05": "MKT",
    "31": "LOO",
    "32": "LOC",
    "33": "MOO",
    "34": "MOC",
}


def _row_order_price(row):
    """이력 행의 **주문가**(체결가 아님)가 있으면 float 로 반환합니다."""
    for key in _ROW_ORDER_PRICE_KEYS:
        if key not in row:
            continue
        try:
            value = float(str(row.get(key)).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _row_order_type(row):
    """이력 행의 주문유형이 표준값으로 해석되면 반환합니다 (아니면 '')."""
    for key in _ROW_ORDER_TYPE_KEYS:
        token = str(row.get(key) or "").strip().upper()
        if token in _KNOWN_ORDER_TYPES:
            return token
    # MERITZ 어댑터는 원본 주문구분을 내부 필드 `_oder_cls_code` 로 보존합니다.
    # 공개 표준 주문유형이 없는 경우에만 코드 역매핑으로 비교합니다.
    for key in ("_oder_cls_code", "oder_cls_code"):
        cls_code = str(row.get(key) or "").strip()
        order_type = _MERITZ_ORDER_TYPE_BY_CLASS.get(cls_code)
        if order_type:
            return order_type
    return ""


def _row_original_qty(row):
    """이력 행의 **원주문 수량**과 필드 존재 여부를 반환합니다.

    브로커마다 키 이름이 달라 `ft_ord_qty` → `ord_qty` → `order_qty` → `qty`
    순서로 첫 번째 양수 값을 사용합니다.

    Returns:
        tuple(int | None, bool): (원주문 수량, 원주문 수량 필드 존재 여부)
        필드는 있으나 0/파싱 불가면 (None, True), 필드 자체가 없으면 (None, False).
    """
    present = False
    for key in _ROW_ORIGINAL_QTY_KEYS:
        if key not in row:
            continue
        present = True
        value = _parse_row_int(row.get(key), None)
        if value is not None and value > 0:
            return value, True
    return None, present


def _verify_orders_against_plan(approval, state, history):
    """승인 주문별 접수/잔량/체결을 state·브로커 이력과 독립 대조합니다 (P1-B).

    Returns:
        tuple(list[str], int, int): (문제 목록, 매수 체결 합, 매도 체결 합)

    판정 기준
      - 접수: orders_meta 에 승인 plan_id + 순번이 있어야 합니다 (주문 API 접수 증거).
      - 메타: orders_meta 의 side/total_qty 가 승인 주문과 일치해야 합니다.
      - 이력 행이 없으면: **같은 세션**이면 아직 CLOSED 이력에 안 잡힌 것일 수 있어
        실패로 보지 않고, 이전 세션 주문이면 확인 불가로 실패 처리합니다.
      - 잔량>0 은 실패가 아닙니다 (VERIFY 는 장중에 돌 수 있음).
      - 이력이 주문가/주문유형을 제공하면 승인값과 허용오차 내에서 비교합니다.
        주문가 허용오차는 **승인 시점에 고정된 정책**(env 는 더 좁힐 수만 있음)을
        쓰며, 승인 정책이 없거나 손상되면 fail-closed 로 실패합니다. env 를 직접
        읽으면 승인 후 env 확대로 브로커 이력 검증까지 느슨해지기 때문입니다.
      - 이력이 원주문 수량(`ft_ord_qty`/`ord_qty`/`order_qty`/`qty`)을 제공하면
        승인 수량과 정확히 일치해야 합니다. 원주문 수량 < 체결수량도 실패입니다.
      - **원주문 수량 필드가 아예 없으면**(일부 브로커) 실패로 보지 않습니다.
        "원주문 수량 필드 없음" 을 정보 로그로 남기고, 체결/잔량이 승인 수량과
        일치하는지만 확인합니다 (lenient 정책).
      - 체결수량/잔량 파싱 실패, side 불일치, 체결>주문수량,
        체결+잔량<주문수량, 체결+잔량>주문수량 은 실패.
      - 같은 날 다른 수동 주문이 섞이면 잔고 기대값이 어긋날 수 있어, 잔고는
        "SETTLE 근거 + 승인 주문 체결 델타" 로 계산해 대조합니다 (SETTLE 시점 값과
        직접 비교하지 않음).
    """
    problems = []
    # 주문가 비교는 승인 시점 정책을 기준으로 합니다. env 는 좁히는 것만 허용되고,
    # 정책이 없거나 손상되면 아래에서 fail-closed 로 실패 처리합니다.
    tolerance_policy = effective_tolerance_policy(approval.get("tolerance_policy"))
    if tolerance_policy is None:
        problems.append(
            "승인 허용오차 정책이 없거나 유효하지 않아 브로커 주문가를 대조할 수 "
            "없습니다 (fail-closed — 재승인이 필요합니다)"
        )
    # EXECUTE 는 승인 계획을 허용오차 게이트로만 쓰고 **재계산 계획**을 전송합니다.
    # 실제 전송 기록이 있으면 그 값으로 대조해야 승인값과 전송값의 혼동이 없습니다.
    transmitted = read_transmitted_plan(str(approval.get("_path") or ""))
    approved = list(transmitted.get("orders") or approval.get("orders") or [])
    metas = _plan_order_metas(state, approval.get("plan_id"))
    by_odno = {str(row.get("odno") or ""): row for row in (history or [])}
    today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    buy_filled = 0
    sell_filled = 0

    for index, order in enumerate(approved, 1):
        comment = str(order.get("comment") or "")
        side = str(order.get("side") or "").upper()
        qty = _parse_row_int(order.get("quantity"), 0) or 0
        if index not in metas:
            problems.append(
                f"주문 {index}({comment}) 승인 실행 흔적이 state.orders_meta 에 없습니다 "
                f"(plan_id={approval.get('plan_id')})"
            )
            continue
        odno, meta = metas[index]

        # ── 접수 메타가 승인 내용과 같은지 (수량/방향) ──
        meta_side = str((meta or {}).get("side") or "").upper()
        if not meta_side:
            problems.append(
                f"주문 {index}({comment}) orders_meta.side 가 없습니다 (odno={mask_odno(odno)})"
            )
        elif meta_side != side:
            problems.append(
                f"주문 {index}({comment}) orders_meta side 불일치: "
                f"승인={side}, meta={meta_side}"
            )
        meta_qty = _parse_row_int((meta or {}).get("total_qty"), None)
        if meta_qty is None:
            problems.append(
                f"주문 {index}({comment}) orders_meta.total_qty 를 읽을 수 없습니다 "
                f"(odno={mask_odno(odno)})"
            )
        elif meta_qty != qty:
            problems.append(
                f"주문 {index}({comment}) orders_meta 수량 불일치: "
                f"승인={qty}, meta={meta_qty}"
            )

        row = by_odno.get(odno)
        if row is None:
            submitted_session = str((meta or {}).get("submitted_session") or "")
            if submitted_session and submitted_session < today_session:
                problems.append(
                    f"주문 {index}({comment}) 가 브로커 이력에 없습니다 (odno={mask_odno(odno)})"
                )
            continue
        row_side = _row_side(row)
        if row_side and side and row_side != side:
            problems.append(
                f"주문 {index}({comment}) side 불일치: 승인={side}, 이력={row_side}"
            )

        # ── 이력이 주문가/주문유형을 제공하면 승인값과 대조 ──
        row_type = _row_order_type(row)
        approved_type = str(order.get("order_type") or "").upper()
        type_keys_present = any(
            str(row.get(key) or "").strip()
            for key in (
                *_ROW_ORDER_TYPE_KEYS,
                "_oder_cls_code",
                "oder_cls_code",
            )
        )
        if row_type and approved_type and row_type != approved_type:
            problems.append(
                f"주문 {index}({comment}) 주문유형 불일치: "
                f"승인={approved_type}, 이력={row_type}"
            )
        elif not row_type and approved_type:
            # 해석 실패를 조용히 통과시키면 실제 주문유형이 승인과 달라도
            # 검증이 성공합니다. 키가 있는데 매핑되지 않았거나, MERITZ 처럼
            # 이력에서 주문유형을 확인해야 하는 브로커는 실패로 처리합니다.
            broker_is_meritz = str(BROKER or "").strip().lower() == "meritz"
            if broker_is_meritz or type_keys_present:
                type_fields = {
                    key: row.get(key)
                    for key in (
                        *_ROW_ORDER_TYPE_KEYS,
                        "_oder_cls_code",
                        "oder_cls_code",
                    )
                    if key in row
                }
                problems.append(
                    f"주문 {index}({comment}) 주문유형 필드 없음/해석 불가: "
                    f"승인={approved_type}, 이력 필드={type_fields}"
                )
        row_price = _row_order_price(row)
        approved_price = float(order.get("price") or 0.0)
        if (
            tolerance_policy is not None
            and row_price is not None
            and approved_price > 0
        ):
            tolerance = price_tolerance(approved_price, tolerance_policy)
            if abs(row_price - approved_price) > tolerance:
                problems.append(
                    f"주문 {index}({comment}) 주문가 불일치: "
                    f"승인=${approved_price:.4f}, 이력=${row_price:.4f} "
                    f"(허용 ±${tolerance:.4f})"
                )

        # ── 브로커가 실제 접수한 원주문 수량을 승인 수량과 대조 ──
        original_qty, original_qty_present = _row_original_qty(row)
        if original_qty is not None:
            if original_qty != qty:
                problems.append(
                    f"주문 {index}({comment}) 주문 수량 불일치: "
                    f"승인={qty}, 브로커 원주문={original_qty}"
                )
        elif original_qty_present:
            problems.append(
                f"주문 {index}({comment}) 원주문 수량을 읽을 수 없습니다 (odno={mask_odno(odno)})"
            )
        else:
            # 일부 브로커는 원주문 수량을 이력에 노출하지 않습니다.
            # 필드 자체가 없는 것은 실패가 아니며, 체결/잔량으로만 대조합니다.
            print(
                f"  [정보] 주문 {index}({comment}) 원주문 수량 필드 없음 "
                f"— 체결/잔량으로만 대조합니다 (odno={mask_odno(odno)})"
            )

        filled = _parse_row_int(row.get("ft_ccld_qty"), None)
        remaining = _parse_row_int(row.get("nccs_qty"), None)
        if filled is None:
            problems.append(
                f"주문 {index}({comment}) 체결수량을 읽을 수 없습니다 (odno={mask_odno(odno)})"
            )
        elif filled > qty:
            problems.append(
                f"주문 {index}({comment}) 체결수량이 승인 수량을 넘습니다: "
                f"{filled} > {qty}"
            )
        else:
            if original_qty is not None and filled > original_qty:
                problems.append(
                    f"주문 {index}({comment}) 체결수량이 원주문 수량을 넘습니다: "
                    f"{filled} > {original_qty}"
                )
            if side == "BUY":
                buy_filled += filled
            elif side == "SELL":
                sell_filled += filled
        if remaining is None:
            problems.append(
                f"주문 {index}({comment}) 잔량을 읽을 수 없습니다 (odno={mask_odno(odno)})"
            )
        elif filled is not None and filled + remaining < qty:
            problems.append(
                f"주문 {index}({comment}) 체결+잔량이 승인 수량보다 적습니다: "
                f"{filled}+{remaining} < {qty}"
            )
        elif filled is not None and filled + remaining > qty:
            problems.append(
                f"주문 {index}({comment}) 체결+잔량이 승인 수량을 넘습니다: "
                f"{filled}+{remaining} > {qty}"
            )
    return problems, buy_filled, sell_filled


def _verify_deferred_evidence_fills(evidence, history, frozen=None):
    """SETTLE 이 미룬 체결 근거를 live 주문이력과 대조합니다 (P2).

    SETTLE 은 실행 세션 체결을 T/순투입/잔고 기준에서 제외하면서
    `evidence["deferred_fills"]` 에 (odno, side, qty, session, amount) 를 남깁니다.
    VERIFY 는 이 행들이 **브로커 이력에 그대로 존재**하는지 확인해야 합니다.
    근거를 신뢰만 하면 변조/누락된 행을 정상 체결처럼 허용하게 됩니다.

    `frozen` 은 EXECUTE 가 동결한 (유효한) `.deferred.json` 행입니다. 동결 기록이
    있는 odno 는 SETTLE 스냅샷 이후 **EXECUTE 시점까지 누적 체결이 늘어날 수**
    있으므로 live 수량과의 동일성을 요구하지 않고, 동결된 누적 수량을 기준선으로
    씁니다. 대신 근거 수량이 동결 수량을 넘으면 fail-closed 입니다.
    동결 기록이 없는 odno/실행은 기존처럼 live 수량과 정확히 일치해야 합니다.

    Returns:
        tuple(list[str], set[str], int): (문제 목록, 허용할 odno 집합, 순변동 수량)
        `deferred_fills` 키가 없는 구버전 근거는 ([], set(), 0) — 기존 동작 유지.
    """
    problems = []
    rows = (evidence or {}).get("deferred_fills")
    if rows is None:
        return problems, set(), 0
    if not isinstance(rows, list):
        return (["SETTLE 근거의 deferred_fills 형식이 올바르지 않습니다"], set(), 0)

    frozen = frozen or {}
    by_odno = {str(row.get("odno") or ""): row for row in (history or [])}
    odnos = set()
    net_qty = 0
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            problems.append(f"SETTLE 근거 deferred_fills[{index}] 가 객체가 아닙니다")
            continue
        odno = str(item.get("odno") or "").strip()
        side = _row_side({"side": item.get("side", "")})
        qty = _parse_row_int(item.get("qty"), None)
        if not odno:
            problems.append(f"SETTLE 근거 deferred_fills[{index}] 에 odno 가 없습니다")
            continue
        if odno in odnos:
            problems.append(f"SETTLE 근거에 보류 체결 odno={mask_odno(odno)} 가 중복 기록되었습니다")
            continue
        odnos.add(odno)
        if not side:
            problems.append(
                f"SETTLE 근거 보류 체결 odno={mask_odno(odno)} 의 side 를 해석할 수 없습니다 "
                f"(side={item.get('side')!r})"
            )
            continue
        if qty is None or qty <= 0:
            problems.append(
                f"SETTLE 근거 보류 체결 odno={mask_odno(odno)} 의 수량이 올바르지 않습니다 "
                f"(qty={item.get('qty')!r})"
            )
            continue

        frozen_row = frozen.get(odno)
        if frozen_row is not None:
            # EXECUTE 동결 기록이 기준선입니다. SETTLE→EXECUTE 증가는 정상이며,
            # 동결 수량 대비 live 수량은 `_verify_execute_deferred_record` 가
            # 정확히 대조합니다 (EXECUTE 이후 증가 = fail-closed).
            if frozen_row["side"] != side:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} side 불일치: "
                    f"근거={side}, EXECUTE={frozen_row['side']}"
                )
            if qty > frozen_row["qty"]:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} 수량 불일치: 근거={qty}, "
                    f"EXECUTE 동결={frozen_row['qty']} (근거가 동결 수량을 넘습니다)"
                )
            if side == "BUY":
                net_qty += frozen_row["qty"]
            elif side == "SELL":
                net_qty -= frozen_row["qty"]
            continue

        row = by_odno.get(odno)
        if row is None:
            problems.append(
                f"SETTLE 근거 보류 체결 odno={mask_odno(odno)} 가 브로커 주문이력에 없습니다 "
                "(누락/변조 의심)"
            )
        else:
            live_side = _row_side(row)
            live_qty = _parse_row_int(row.get("ft_ccld_qty"), None)
            if live_side != side:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} side 불일치: "
                    f"근거={side}, 이력={live_side or '(해석 불가)'}"
                )
            if live_qty is None:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} 의 브로커 체결수량을 읽을 수 없습니다"
                )
            elif live_qty != qty:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} 수량 불일치: 근거={qty}, 이력={live_qty}"
                )

        if side == "BUY":
            net_qty += qty
        elif side == "SELL":
            net_qty -= qty
    return problems, odnos, net_qty


def _verify_execute_deferred_record(approval, state, history):
    """EXECUTE 가 동결한 보류 체결 기록(.deferred.json)을 검증합니다.

    EXECUTE 는 SETTLE 이후·EXECUTE 이전에 도착한 실행 세션 체결을 승인 소비
    직후 sidecar 로 남깁니다. VERIFY 는 live 이력만으로 그 체결과 "EXECUTE
    이후 도착한 체결"을 구분할 수 없으므로 이 기록을 기준선으로 씁니다.

    Returns:
        tuple(list[str], dict[str, dict], bool):
            (문제 목록, odno→정규화 행, 기록 파일 존재 여부)
        파일이 없으면 (기존 승인) 빈 결과 — 호출자는 기존 엄격 동작을 유지합니다.
    """
    approval_path = str(approval.get("_path") or "")
    if not approval_path:
        return [], {}, False
    record_path = deferred_record_path(approval_path)
    if not os.path.exists(record_path):
        return [], {}, False

    data = read_deferred_record(approval_path)
    if not data:
        return [f"EXECUTE 보류 체결 기록을 읽을 수 없습니다 (손상/형식 오류): {record_path}"], {}, True

    problems = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("deferred_fills")
        # 선택 바인딩: 기록에 있으면 승인과 일치해야 합니다 (다른 승인 기록 혼입 방지).
        for key in ("plan_id", "symbol"):
            recorded = str(data.get(key) or "")
            expected = str(approval.get(key) or "")
            if recorded and expected and recorded != expected:
                problems.append(
                    f"EXECUTE 보류 체결 기록 {key} 불일치: 기록={recorded}, 승인={expected}"
                )
    else:
        return ["EXECUTE 보류 체결 기록 형식이 올바르지 않습니다"], {}, True
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        return ["EXECUTE 보류 체결 기록의 deferred_fills 형식이 올바르지 않습니다"], {}, True

    settled_ymd = _session_ymd(state.get("settled_session"))
    execution_ymd = _session_ymd(state.get("execution_session"))
    sessions_known = bool(settled_ymd and execution_ymd)
    if not sessions_known:
        problems.append(
            "EXECUTE 보류 체결 기록을 검증할 세션 정보가 state 에 없습니다 "
            "(settled_session/execution_session)"
        )

    by_odno = {str(row.get("odno") or ""): row for row in (history or [])}
    normalized = {}
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            problems.append(f"EXECUTE 보류 체결 기록[{index}] 가 객체가 아닙니다")
            continue
        odno = str(item.get("odno") or "").strip()
        side = _row_side({"side": item.get("side", "")})
        qty = _parse_row_int(item.get("qty"), None)
        session = _session_ymd(item.get("session"))
        if not odno:
            problems.append(f"EXECUTE 보류 체결 기록[{index}] 에 odno 가 없습니다")
            continue
        if odno in normalized:
            problems.append(
                f"EXECUTE 보류 체결 기록에 odno={mask_odno(odno)} 가 중복 기록되었습니다"
            )
            continue
        if not side:
            problems.append(
                f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 side 를 해석할 수 없습니다 "
                f"(side={item.get('side')!r})"
            )
            continue
        if qty is None or qty <= 0:
            problems.append(
                f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 수량이 올바르지 않습니다 "
                f"(qty={item.get('qty')!r})"
            )
            continue
        if sessions_known:
            if session != execution_ymd:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 세션이 실행 세션과 다릅니다: "
                    f"기록={session or '(없음)'}, execution={execution_ymd}"
                )
            elif session <= settled_ymd:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 세션이 정산 세션 이전/같습니다: "
                    f"기록={session}, settled={settled_ymd}"
                )

        row = by_odno.get(odno)
        if row is None:
            problems.append(
                f"EXECUTE 보류 체결 odno={mask_odno(odno)} 가 브로커 주문이력에 없습니다 "
                "(누락/변조 의심)"
            )
        else:
            live_side = _row_side(row)
            live_qty = _parse_row_int(row.get("ft_ccld_qty"), None)
            live_session = _session_ymd(_order_us_session_date(row))
            if live_side != side:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} side 불일치: "
                    f"기록={side}, 이력={live_side or '(해석 불가)'}"
                )
            if live_qty is None:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 브로커 체결수량을 읽을 수 없습니다"
                )
            elif live_qty != qty:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 수량 불일치: "
                    f"기록={qty}, 이력={live_qty}"
                )
            # live 이력 행이 정말 **이 실행 세션의 동결 체결**인지 확인합니다.
            # side/qty 만 같으면 다른 세션(다음 거래일 등)의 체결을 동결 체결로
            # 오인할 수 있으므로, 기록 세션·실행 세션·정산 세션 경계를 모두 봅니다.
            if not live_session:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 이력 세션을 해석할 수 없습니다 "
                    f"(기록={session or '(없음)'}) — 세션 검증 불가로 중단합니다"
                )
            elif live_session != session:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 세션 불일치: "
                    f"기록={session or '(없음)'}, 이력={live_session}"
                )
            elif sessions_known and live_session != execution_ymd:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 이력 세션이 실행 세션과 다릅니다: "
                    f"이력={live_session}, execution={execution_ymd}"
                )
            elif sessions_known and live_session <= settled_ymd:
                problems.append(
                    f"EXECUTE 보류 체결 odno={mask_odno(odno)} 의 이력 세션이 정산 세션 이전/같습니다: "
                    f"이력={live_session}, settled={settled_ymd}"
                )

        normalized[odno] = {
            "side": side,
            "qty": qty,
            "amount": item.get("amount"),
            "amount_confirmed": bool(item.get("amount_confirmed", "amount" in item)),
        }
    return problems, normalized, True


def _deferred_rows_from_evidence(evidence):
    """SETTLE 근거의 보류 체결 행을 odno→정규화 행으로 변환합니다.

    검증(문제 판정)은 `_verify_deferred_evidence_fills` 가 담당하고, 여기서는
    EXECUTE 기록과의 odno-union 병합에 쓸 정규화 값만 만듭니다.
    """
    rows = (evidence or {}).get("deferred_fills")
    if not isinstance(rows, list):
        return {}
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        odno = str(item.get("odno") or "").strip()
        side = _row_side({"side": item.get("side", "")})
        qty = _parse_row_int(item.get("qty"), None)
        if not odno or not side or qty is None or qty <= 0:
            continue
        result[odno] = {
            "side": side,
            "qty": qty,
            "amount": item.get("amount"),
            "amount_confirmed": bool(item.get("amount_confirmed", "amount" in item)),
        }
    return result


def _verify_transmitted_plan(approval):
    """VERIFY: 실제 전송 기록(.transmitted.json)의 무결성과 승인 허용오차를 봅니다.

    소비된 승인은 반드시 유효한 전송 기록을 가져야 합니다. 기록이 없거나
    손상/타 승인 기록/해시 불일치면 실행 내용을 확인할 수 없으므로 실패입니다.
    """
    return list(transmitted_plan_problems(approval))


def _verify_broker_readonly(approval, state, broker):
    """VERIFY 의 read-only 브로커 확인 — 주문/취소 API 를 호출하지 않습니다.

    실행된 승인(consumed=true)은 **승인 주문별** 접수/잔량/체결을 먼저 대조하고,
    재정산 불일치는 승인 계획에 속하지 않는 체결이 있을 때만 실패로 봅니다
    (승인 실행으로 인한 정상 T/잔고 변화는 허용). 실행되지 않은 승인은 잔고가
    SETTLE 근거와 같은지만 확인합니다 (P1-B).
    """
    symbol = approval.get("symbol", "")
    exchange = approval.get("exchange", "")
    try:
        history = broker.get_order_history(symbol, exchange, days=30, verbose=False)
        balance = broker.get_balance(symbol, exchange)
    except Exception as error:
        return [f"브로커 read-only 조회 실패: {error}"]
    problems = []
    consumed = bool(approval.get("consumed"))
    buy_filled = 0
    sell_filled = 0

    # 0) EXECUTE 가 동결한 보류 체결 기록을 **먼저** 검증합니다. 소비된 승인의
    #    live 이력만으로는 "EXECUTE 전 수용"과 "EXECUTE 후 도착"을 구분할 수
    #    없으므로, 실행 시점에 동결한 집합이 VERIFY 의 기준선입니다. 기록이
    #    없는 구버전 승인은 기존 엄격 동작을 그대로 유지합니다.
    #    EXECUTE **이후** 도착한 체결은 이 기록에도 승인 계획에도 없으므로
    #    allowed/기대잔고에 포함되지 않습니다 — 소급 허용하지 않고 fail-closed.
    record_problems = []
    record_rows = {}
    record_present = False
    if consumed:
        record_problems, record_rows, record_present = _verify_execute_deferred_record(
            approval, state, history
        )
        problems.extend(record_problems)

    # 0b) SETTLE 이 미룬 체결 근거를 live 이력과 대조합니다. 유효한 EXECUTE
    #     동결 기록이 있으면 그 odno 는 **동결된 누적 수량**을 기준선으로 씁니다.
    #     SETTLE 스냅샷(1주) → EXECUTE 수용(2주) 증가는 정상 흐름이지만,
    #     동결 기록 이후 live 증가는 record 검증에서 fail-closed 됩니다.
    evidence = {}
    deferred_odnos = set()
    deferred_net_qty = 0
    evidence_path = approval.get("_evidence_path")
    if evidence_path:
        try:
            evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            evidence = {}
        if isinstance(evidence, dict):
            frozen_baseline = (
                record_rows if record_present and not record_problems else None
            )
            deferred_problems, deferred_odnos, deferred_net_qty = (
                _verify_deferred_evidence_fills(
                    evidence, history, frozen=frozen_baseline
                )
            )
            problems.extend(deferred_problems)

    # 0c) SETTLE 근거와 EXECUTE 동결 기록을 odno 기준으로 합칩니다 (중복 계산 금지).
    #     같은 odno 는 동결된 누적 수량이 최종 기준선이며, SETTLE→EXECUTE 증가는
    #     허용하되 감소/방향 변경은 변조 의심으로 fail-closed 입니다.
    merged_deferred = _deferred_rows_from_evidence(evidence)
    if record_present:
        for odno, record_row in (record_rows or {}).items():
            existing = merged_deferred.get(odno)
            if existing is None:
                merged_deferred[odno] = record_row
                continue
            if existing["side"] != record_row["side"]:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} 기록 불일치: "
                    f"SETTLE={existing['side']} {existing['qty']}주, "
                    f"EXECUTE={record_row['side']} {record_row['qty']}주"
                )
                continue
            if record_row["qty"] < existing["qty"]:
                problems.append(
                    f"보류 체결 odno={mask_odno(odno)} 수량 불일치: "
                    f"SETTLE={existing['qty']}주, EXECUTE 동결={record_row['qty']}주 "
                    "(동결 수량이 SETTLE 근거보다 적습니다)"
                )
                continue
            # SETTLE→EXECUTE 누적 증가는 정상 — 동결 수량을 기준선으로 삼습니다.
            merged = dict(record_row)
            if existing.get("amount_confirmed") and existing.get("amount") is not None:
                # 금액은 SETTLE 근거의 확정값을 우선합니다.
                merged["amount"] = existing["amount"]
                merged["amount_confirmed"] = True
            merged_deferred[odno] = merged
        deferred_odnos = set(deferred_odnos) | set(record_rows or {})
    if deferred_odnos:
        deferred_net_qty = sum(
            row["qty"] if row["side"] == "BUY" else -row["qty"]
            for row in merged_deferred.values()
        )

    # 1) 승인 주문 대조를 **먼저** 수행합니다. 이 결과로 어떤 체결이 승인된
    #    계획에 속하는지(odno) 확정되고, 재정산 불일치 허용 범위가 정해집니다.
    if consumed:
        fill_problems, buy_filled, sell_filled = _verify_orders_against_plan(
            approval, state, history
        )
        problems.extend(fill_problems)

    # 2) 재정산 대조 — 승인 계획 + 미룬 체결에 속하지 않는 새 체결만 실패 사유입니다.
    allowed_odnos = set()
    if consumed:
        allowed_odnos.update(_approved_plan_odnos(state, approval.get("plan_id")))
    allowed_odnos.update(deferred_odnos)
    try:
        assert_reconciliation_current(
            symbol,
            state,
            history,
            balance,
            allowed_fill_odnos=allowed_odnos or None,
        )
    except Exception as error:
        problems.append(f"브로커 주문이력/잔고 재정산 불일치: {error}")

    if evidence_path:
        expected_qty = evidence.get("balance_qty")
        broker_qty = int(balance.quantity) if balance is not None else None
        if balance is None:
            problems.append("브로커 잔고를 확인할 수 없습니다 (SETTLE 근거 대조 불가)")
        elif expected_qty is not None:
            # SETTLE 의 balance_qty 는 미룬 체결을 되감은 "정산 세션 시점" 값입니다.
            # 미룬 순변동을 더한 뒤, 실행된 승인 주문의 체결 델타를 반영합니다.
            baseline_after = int(expected_qty) + deferred_net_qty
            if consumed:
                expected_after = baseline_after + buy_filled - sell_filled
                if broker_qty != expected_after:
                    problems.append(
                        "브로커 잔고가 기대 결과와 다릅니다: "
                        f"SETTLE={int(expected_qty)}, "
                        f"보류체결={deferred_net_qty:+d}, "
                        f"승인주문 체결(매수+{buy_filled}/매도-{sell_filled}), "
                        f"기대={expected_after}, broker={broker_qty}"
                    )
            elif broker_qty != baseline_after:
                problems.append(
                    "브로커 잔고가 SETTLE 근거와 다릅니다(미실행 승인): "
                    f"evidence={int(expected_qty)}, "
                    f"보류체결={deferred_net_qty:+d}, "
                    f"기대={baseline_after}, broker={broker_qty}"
                )
    return problems


def _run_verify_phase(broker=None):
    """VERIFY: 승인 파일 + SETTLE evidence 를 읽기 전용으로 독립 검증합니다.

    (1) ops/verify_run.py 의 독립 재계산, (2) 승인 주문별 접수/잔량/체결 +
    기대 잔고 read-only 대조.

    outcome 구분 (P1-C):
      - executed           EXECUTE 가 정상 실행 후 남긴 명시적 성공 기록 → exit 0
      - execution-failed   실행 실패/부분실패, 또는 소비됐지만 기록 없음 → exit 1
      - approval-skipped   승인만 되고 실행되지 않음 → exit 2 (검증 통과와 구분)
    """
    approval_ref = os.getenv("APPROVAL_PLAN_ID") or os.getenv("APPROVAL_FILE")
    if not approval_ref:
        print("[VERIFY] APPROVAL_PLAN_ID 또는 APPROVAL_FILE 이 필요합니다.")
        return 1
    approval = load_approval(approval_ref)
    symbol = approval.get("symbol", "")
    state = load_state(symbol)

    problems = verify_approval(
        approval,
        state,
        require_unconsumed=False,
        check_state_hash=False,
    )
    # 소비된 승인은 실제 전송 기록까지 독립 검증합니다 (P1: transmitted integrity).
    if approval.get("consumed"):
        problems.extend(_verify_transmitted_plan(approval))
    if state.get("pending_order_intent") or state.get("pending_order_batch"):
        problems.append(
            "미종결 주문 fence 가 남아 있습니다 "
            f"(intent={bool(state.get('pending_order_intent'))}, "
            f"batch={bool(state.get('pending_order_batch'))})"
        )

    # 승인 파일의 evidence_id(정산 run_id)로 **커밋된** SETTLE 근거만 찾습니다.
    run_id = approval.get("evidence_id") or approval.get("settlement_run_id")
    evidence_path = evidence_file_for_run(symbol, run_id)
    approval["_evidence_path"] = evidence_path
    if not evidence_path:
        problems.append(
            f"커밋된 SETTLE 근거 파일을 찾을 수 없습니다 (symbol={symbol}, run_id={run_id})."
        )
    else:
        try:
            evidence_mode = json.loads(
                Path(evidence_path).read_text(encoding="utf-8")
            ).get("mode", TRADE_MODE)
            evidence_ok, evidence_problems = verify_evidence_file_with_ops(
                evidence_path,
                broker=BROKER,
                mode=evidence_mode,
                run_id=run_id,
                broker_mode=BROKER_MODE,
                final_state=state,
            )
        except Exception as error:
            evidence_ok, evidence_problems = False, [f"독립 검증 실행 실패: {error}"]
        if not evidence_ok:
            problems.extend(evidence_problems or ["정산 근거 검증 실패"])

    if broker is not None:
        problems.extend(_verify_broker_readonly(approval, state, broker))

    recovery_outcome = _approval_recovery_outcome(approval)
    # 소비된 승인인데 결과 기록이 없으면(구버전/강제 종료) 보수적으로 실행 실패로 봅니다.
    # 정상 실행은 EXECUTE 가 outcome="executed" 를 남기므로 여기서 성공으로 판정됩니다.
    if approval.get("consumed"):
        outcome = recovery_outcome or "execution-failed"
        if not recovery_outcome:
            problems.append(
                "소비된 승인에 실행 결과 기록(.recovery.json)이 없습니다 "
                "— 실행 성공을 확인할 수 없습니다."
            )
    else:
        outcome = recovery_outcome or "approval-skipped"

    if problems:
        label = outcome
        if outcome == "executed":
            label = "executed(실행 후 검증 실패)"
        print(f"[VERIFY] 실패 ({label}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    # 실행 실패/부분실패는 검증 실패입니다 (성공으로 조용히 끝나면 안 됩니다).
    if outcome == "execution-failed":
        print(
            "[VERIFY] 실패 (execution-failed): EXECUTE 가 실행 실패를 기록했습니다 "
            f"— plan_id={approval.get('plan_id')}"
        )
        return 1
    # 승인만 되고 실행되지 않은 상태는 "검증 통과"와 구분되는 자체 종료코드(2)를
    # 씁니다. 실행은 없었지만 운영자가 인지해야 하는 상태입니다.
    if outcome == "approval-skipped":
        print(
            "[VERIFY] 실행 안 됨 (approval-skipped): 승인만 존재하고 실행 기록이 "
            f"없습니다 — plan_id={approval.get('plan_id')}, exit=2"
        )
        return 2
    print(
        f"[VERIFY] 통과 — outcome={outcome}, "
        f"plan_id={approval.get('plan_id')}, "
        f"evidence={evidence_path}, orders={len(approval.get('orders') or [])}, "
        f"consumed={approval.get('consumed')}"
    )
    return 0


def _preview_symbol_plan(symbol_config, read_only_broker, plans_dir):
    """PREVIEW: 계획 생성 → (AUTO_EXECUTE 면) 승인까지 **잠금 안**에서 수행합니다.

    대기(프리장 개장)는 여기서 하지 않습니다. 계획 생성과 승인 파일 쓰기만
    state 를 읽고 쓰므로 이 구간만 직렬화하면 충분합니다.

    Returns:
        tuple: `(plan, approval_file)`. 자동 실행 대상이 아니면 `approval_file`
               은 None 입니다. 승인 실패 예외는 호출자가 AUTO_BLOCKED 로 처리합니다.
    """
    with global_lock():
        settled = _settled_from_state(symbol_config)
        plan = build_order_plan(settled, read_only_broker)
        plan_file = write_order_plan(plan, plans_dir=plans_dir)
        print(
            f"[PREVIEW] {plan.symbol} — plan_id={plan.plan_id}, "
            f"orders={len(plan.orders)}, state_hash={plan.state_hash[:12]}, "
            f"file={plan_file}"
        )

        # AUTO_EXECUTE(opt-in): PREVIEW 후 자동 APPROVE → EXECUTE.
        # 차단 사유(hold/mismatch/tolerance/세션 드리프트 등)가 있으면
        # 해당 종목만 AUTO_BLOCKED 로 중단하고 다음 종목은 계속 진행합니다.
        # DRY 는 주문/상태 저장을 하지 않으므로 승인·실행을 건너뛰고
        # PREVIEW 산출물만 남깁니다 (DRY 검증용).
        if not AUTO_EXECUTE:
            return plan, None
        if str(TRADE_MODE).strip().upper() == "DRY":
            print(
                f"[AUTO_EXECUTE] {plan.symbol} DRY 모드라 승인·실행을 건너뜁니다 "
                f"(PREVIEW 산출물만 저장)."
            )
            return plan, None

        approval_file = approve_plan_file(
            plan_file,
            approvals_dir=os.getenv("APPROVALS_DIR", "approvals"),
            plans_dir=plans_dir,
            broker=read_only_broker,
        )
        if approval_file:
            notify(
                f"🤖 [AUTO_APPROVE] {plan.symbol} 승인 완료 — "
                f"plan_id={plan.plan_id}, orders={len(plan.orders)}"
            )
        return plan, approval_file


def _execute_auto_approved_plan(approval_file, broker):
    """AUTO_EXECUTE: 자동 승인된 계획을 **잠금 밖에서** 실행합니다.

    `execute_approved_plan()` 은 RUN_PHASE=EXECUTE 만 허용하므로 이 호출
    범위에서만 일시 변경하고 호출 후 복구합니다. 프리장 대기(최대 ~7.5시간)는
    그 함수 안에서 잠금을 잡기 **전에** 끝나므로, 여기서 잠금을 잡으면 안 됩니다.
    """
    _prev_phase = os.environ.get("RUN_PHASE", "")
    os.environ["RUN_PHASE"] = "EXECUTE"
    try:
        return execute_approved_plan(broker, approval_file)
    finally:
        os.environ["RUN_PHASE"] = _prev_phase


def _phase_runs_without_outer_lock(run_phase):
    """이 단계가 프리장 대기(최대 ~7.5시간)를 포함해 **바깥 잠금 없이** 실행돼야 하는지.

    대기 중에 전역 실행 잠금(`.run.lock`)을 들고 있으면 그동안 다른 실행
    (SETTLE/PREVIEW/APPROVE)이 전부 막히므로, 해당하는 경우에만 바깥 잠금을
    생략하고 각 단계/`execute_approved_plan()` 이 임계구간만 잡습니다.
    (기본값은 "잠근다" — fail-closed)

    **판정 규칙의 단일 출처입니다.** 셸 런처(`ops/run_daily.sh`)는 이 규칙을
    다시 구현하지 않고 항상 잠금을 시도하며, 위임 판정이면 이 프로세스가
    `_release_inherited_outer_lock()` 으로 상속 잠금만 풉니다. 셸/파이썬에 같은
    규칙이 이중 구현되면 drift 로 보호가 조용히 약해집니다.
    """
    phase = str(run_phase or "").strip().upper()
    if phase == "EXECUTE":
        return True
    if phase == "PREVIEW" and AUTO_EXECUTE:
        # DRY 는 승인·실행을 건너뛰고 PREVIEW 산출물만 남기므로 대기가 없습니다.
        return str(TRADE_MODE).strip().upper() != "DRY"
    return False


def _dispatch_pipeline_phase(run_phase, broker):
    """RUN_PHASE 단계별 실행 — 임계구간만 전역 실행 잠금으로 직렬화합니다.

    cron 을 거치지 않는 직접 CLI 실행도 `.state.json` read-modify-write 를
    공유하므로, 셸 런처와 같은 `.run.lock` 을 파이썬에서도 잡습니다.

    예외는 프리장 개장 대기(최대 ~7.5시간)를 포함하는 구간입니다. 잠금을 든 채
    자면 그동안 다른 실행(SETTLE/PREVIEW/APPROVE)이 전부 막히므로 대기는
    잠금 **밖**에서 합니다 (`_phase_runs_without_outer_lock`).

      - EXECUTE: 여기서 잠그지 않고 `execute_approved_plan()` 에 위임합니다.
        그 함수가 대기를 마친 뒤 같은 `.run.lock` 을 스스로 잡고 검증·선점·
        전송을 직렬화합니다.
      - PREVIEW + AUTO_EXECUTE: 계획 생성·승인까지만 잠그고
        (`_preview_symbol_plan`), 자동 실행은 `_execute_auto_approved_plan()`
        이 잠금 밖에서 호출합니다 (내부에서 대기 후 잠금).
    """
    if _phase_runs_without_outer_lock(run_phase):
        return _dispatch_pipeline_phase_locked(run_phase, broker)
    with global_lock():
        return _dispatch_pipeline_phase_locked(run_phase, broker)


def _reviewed_plan_hash():
    """사람이 검토한 계획 해시(`REVIEWED_PLAN_HASH`)를 정규화해 반환합니다.

    비어 있으면 "바인딩 없음"입니다 (자동 경로 — LIFECYCLE/AUTO_EXECUTE).
    """
    return str(os.getenv("REVIEWED_PLAN_HASH", "") or "").strip().lower()


def _assert_reviewed_plan_hash_matches(actual_hash, context):
    """사람이 검토한 plan_hash 와 실제 계획/승인 파일의 해시를 대조합니다.

    배경(외부 리뷰 P1): `reviewed=true` 는 "사람이 검토했다"는 **선언**일 뿐
    "무엇을" 검토했는지 증명하지 않습니다. PREVIEW 보다 먼저 전달되면 사람이
    가격·수량을 보지 않고도 승인·실행까지 자동으로 진행됩니다. 그래서 수동
    승인 경로에서는 사람이 본 `plan_id` + `plan_hash` 를 함께 넘겨, 승인 파일을
    쓰기 **전에** 계획 파일의 plan_hash 와 일치하는지 확인합니다 (불일치 = 중단).

    - `REVIEWED_PLAN_HASH` 가 비어 있으면 아무것도 하지 않습니다 (기존 동작).
      필수 여부는 워크플로(운영 계좌 real LIVE 수동 APPROVE/EXECUTE)가 강제합니다.
    - 비교는 대소문자 무시 + constant-time 입니다.
    """
    expected = _reviewed_plan_hash()
    if not expected:
        return
    actual = str(actual_hash or "").strip().lower()
    if not actual:
        raise RuntimeError(
            f"{context}: 계획/승인 파일에 plan_hash 가 없어 사람이 검토한 값과 대조할 수 "
            "없습니다 — PREVIEW 부터 다시 실행하세요."
        )
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError(
            f"{context}: plan_hash 불일치 — 사람이 검토한 값({expected[:12]}…)과 계획 "
            f"파일 값({actual[:12]}…)이 다릅니다. 가격·수량이 바뀐 계획은 승인·실행하지 "
            "않습니다 (PREVIEW 를 다시 실행하고 새 plan_hash 로 승인하세요)."
        )
    print(f"{context}: 사람이 검토한 plan_hash 일치 확인 ({expected[:12]}…)")


def _dispatch_pipeline_phase_locked(run_phase, broker):
    """RUN_PHASE 단계별 실행. SETTLE/PREVIEW/APPROVE 는 ReadOnlyBroker 만 받습니다."""
    read_only_broker = ReadOnlyBroker(broker)
    failures = 0

    if run_phase == "SETTLE":
        for symbol_config in SYMBOLS:
            try:
                settled = settle_positions(read_only_broker, symbol_config)
                if settled.halt_reason:
                    # 정산이 확정되지 않았는데 성공(exit 0)으로 끝나면, 운영자는
                    # 정산이 끝난 줄 알고 다음 단계를 진행합니다. 보류는 실패입니다.
                    print(
                        f"[SETTLE] {settled.symbol} 보류: {settled.halt_reason} "
                        "(정산 미확정 → 실패로 집계)"
                    )
                    failures += 1
                else:
                    print(
                        f"[SETTLE] {settled.symbol} 완료 — T={settled.T}, "
                        f"seed=${settled.seed:.2f}, run_id={settled.settlement_run_id}"
                    )
            except GlobalScopedError:
                # 상태 파일 공통 장애 — 다른 종목을 계속 진행하면 안 됩니다.
                raise
            except Exception as error:
                print(f"[SETTLE] {symbol_config['symbol']} 실패: {error}")
                failures += 1
        return failures

    if run_phase == "PREVIEW":
        plans_dir = os.getenv("PLAN_DIR", "plans")
        for symbol_config in SYMBOLS:
            try:
                # 계획 생성·승인은 잠금 안 (`_preview_symbol_plan`).
                plan, approval_file = _preview_symbol_plan(
                    symbol_config, read_only_broker, plans_dir
                )
                if not approval_file:
                    continue
                # 자동 실행은 **잠금 밖**에서: 프리장 개장 대기(최대 ~7.5시간)를
                # 잠금을 든 채 하면 그동안 다른 실행이 전부 막힙니다.
                # 대기 후 게이트 재검증·선점·전송은 execute_approved_plan() 이
                # 자체 잠금 안에서 다시 수행하므로 순서는 그대로 유지됩니다.
                try:
                    result = _execute_auto_approved_plan(approval_file, broker)
                except GlobalScopedError:
                    # 공통 장애(상태 저장 실패 등)는 삼키지 않고 전체 실행을 중단합니다.
                    raise
                except Exception as auto_error:
                    msg = f"[AUTO_BLOCKED] {plan.symbol} {str(auto_error)}"
                    print(msg)
                    notify(f"🚫 {msg}", urgent=True)
                    failures += 1
                else:
                    failed = int((result or {}).get("failed", 0))
                    if failed:
                        notify(
                            f"⚠️ [AUTO_EXECUTE] {plan.symbol} 실행 실패 "
                            f"({failed}건)",
                            urgent=True,
                        )
                        failures += 1
                    else:
                        notify(f"✅ [AUTO_EXECUTE] {plan.symbol} 실행 완료")
            except GlobalScopedError:
                raise
            except Exception as error:
                print(f"[PREVIEW] {symbol_config['symbol']} 실패: {error}")
                failures += 1
        return failures

    if run_phase == "APPROVE":
        plan_ref = os.getenv("PLAN_ID") or os.getenv("PLAN_FILE")
        if not plan_ref:
            print("[APPROVE] PLAN_ID 또는 PLAN_FILE 이 필요합니다 (PREVIEW 산출물).")
            return 1
        plans_dir = os.getenv("PLAN_DIR", "plans")
        approvals_dir = os.getenv("APPROVALS_DIR", "approvals")
        try:
            # 사람이 검토한 plan_hash 바인딩 검증 (외부 리뷰 P1).
            # 승인 파일을 쓰기 **전에** 대조해야 합니다 — PREVIEW 이후 계획이 바뀌었거나
            # 사람이 보지 않은 계획을 승인하는 경로를 막습니다 (값이 없으면 기존 동작).
            plan_data = read_order_plan_file(plan_ref, plans_dir)
            _assert_reviewed_plan_hash_matches(
                plan_data.get("plan_hash"), f"[APPROVE] {plan_ref}"
            )
            approval_file = approve_plan_file(
                plan_ref,
                approvals_dir=approvals_dir,
                plans_dir=plans_dir,
                broker=read_only_broker,
            )
            if approval_file:
                print(f"[APPROVE] 승인 파일: {approval_file}")
        except Exception as error:
            print(f"[APPROVE] 실패: {error}")
            failures += 1
        return failures

    if run_phase == "EXECUTE":
        plan_ref = os.getenv("APPROVAL_PLAN_ID") or os.getenv("APPROVAL_FILE")
        if not plan_ref:
            print("[EXECUTE] APPROVAL_PLAN_ID 또는 APPROVAL_FILE 이 필요합니다.")
            return 1
        if not broker.is_trading_day():
            print("[EXECUTE] 미국 증시 휴장일입니다. 주문을 실행하지 않습니다.")
            return 1
        # 승인 파일도 같은 해시로 대조합니다 (사람이 검토한 계획 == 실제 실행 계획).
        # APPROVE 에서 쓴 plan_hash 가 승인 파일에 그대로 복사되므로, 두 단계 모두에
        # 같은 값을 넘기면 검토→승인→실행 사슬이 하나의 해시로 묶입니다.
        try:
            approval_data = load_approval(
                plan_ref, os.getenv("APPROVALS_DIR", "approvals")
            )
            _assert_reviewed_plan_hash_matches(
                approval_data.get("plan_hash"), f"[EXECUTE] {plan_ref}"
            )
        except Exception as error:
            print(f"[EXECUTE] 실패: {error} — 주문을 실행하지 않습니다.")
            return 1
        result = execute_approved_plan(broker, plan_ref)
        return int((result or {}).get("failed", 0))

    raise RuntimeError(f"알 수 없는 RUN_PHASE: {run_phase}")


def _phase_waits_outside_lock():
    """env 의 RUN_PHASE 기준으로 대기 단계인지 판별합니다 (`main()` 진입점용).

    부수효과가 없습니다 (env 읽기만). RUN_PHASE 정규화는 `config.read_run_phase()`
    한 곳만 사용하고, 판정 규칙은 `_phase_runs_without_outer_lock()` 한 곳에만
    둡니다.
    """
    import config as runtime_config

    return _phase_runs_without_outer_lock(runtime_config.read_run_phase())


def _release_inherited_outer_lock():
    """대기 단계에서 셸 런처가 잡은 `.run.lock` 을 **즉시 해제**합니다.

    잠금 보유 판정의 **단일 출처**는 `_phase_runs_without_outer_lock()` 입니다.
    셸 런처(`ops/run_daily.sh`)는 단계 규칙을 다시 구현하지 않고 **항상** 잠금을
    시도한 뒤(fail-closed) 이 값을 상속 fd 로 넘깁니다. 여기서 위임 판정이면
    상속된 flock 만 풀어, 프리장 개장 대기(최대 ~7.5시간) 동안 다른 단계
    (SETTLE/PREVIEW/APPROVE/VERIFY)가 진행될 수 있게 합니다.

    해제는 **런처가 실제로 물려준 fd** 로만 합니다 — `pipeline._inherited_lock_fd`
    가 fd 의 st_dev/st_ino 일치와 "그 fd 가 실제 잠금 보유자" 임을 모두 확인한
    경우에만 풉니다. 상속 증거가 없으면(직접 CLI 실행) 아무것도 하지 않고,
    잠금은 뒤이어 `global_lock()` 이 정상적으로 잡습니다 (동시 진행 금지).

    Returns:
        int | None: 해제한 fd 번호. 해제할 상속 잠금이 없으면 None.
    """
    try:
        import fcntl
    except ImportError:  # fcntl 이 없는 플랫폼에는 위임할 잠금도 없습니다.
        return None
    # 순환 참조를 피하려고 함수 안에서 임포트합니다 (모듈 로드 시점 의존 없음).
    import pipeline as pipeline_module

    target = pipeline_module.run_lock_path()
    fd = pipeline_module._inherited_lock_fd(target)
    if fd is None:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as error:
        # 해제 실패 = 런처 잠금이 그대로 유지됩니다. fail-closed 방향이라 안전합니다.
        print(
            f"[잠금] 런처 잠금 해제 실패 (fd {fd}): {error} — 잠금을 유지한 채 진행합니다."
        )
        return None
    # 이 프로세스는 더 이상 바깥 잠금을 들고 있지 않습니다. 플래그를 남기면
    # global_lock 이 "상속받았는데 증거가 없다" 는 잘못된 안내를 출력하므로 지웁니다
    # (실제 보유 판정은 fd inode 검증이 담당하므로 플래그 제거로 잠금이 약해지지 않음).
    os.environ.pop("RUN_LOCK_HELD", None)
    os.environ.pop("RUN_LOCK_FD", None)
    print(
        f"[잠금] 대기 단계 → 런처 잠금(fd {fd}, {target})을 해제했습니다. "
        "프리장 대기 동안 다른 단계가 진행할 수 있습니다 "
        "(임계구간은 대기 후 global_lock 이 같은 파일로 다시 잡습니다)."
    )
    return fd


def main():
    """자동매매 봇 메인 진입점.

    SETTLE/PREVIEW(자동 실행 제외)/APPROVE/VERIFY/REPAIR 와 레거시 통합 실행은
    모두 `.state.json` 을 읽고 쓰므로 같은 `.run.lock` 으로 프로세스 간
    직렬화합니다. 셸 런처가 이미 잡은 경우(`RUN_LOCK_HELD=1` + 상속 fd)에는
    재진입합니다.

    프리장 개장 대기(최대 ~7.5시간)를 포함하는 단계(EXECUTE, PREVIEW+
    AUTO_EXECUTE)는 **잠금 밖에서** 대기합니다 (`_phase_waits_outside_lock`).
    런처는 단계와 무관하게 항상 잠그므로, 이 경우 상속된 잠금을 여기서
    해제합니다 (`_release_inherited_outer_lock`). 잠금은 대기 후의 임계구간
    (승인 재검증·선점·state 저장·주문)에서만 잡습니다.
    """
    if _phase_waits_outside_lock():
        _release_inherited_outer_lock()
        return _main_locked()
    with global_lock():
        return _main_locked()


def _run_mode_dispatch():
    """진단·복구 전용 모드를 실행합니다 (브로커 생성 없음).

    Returns:
        bool: 전용 모드가 실행을 처리했으면 True (호출자는 원장 확정 후 종료),
              일반 실행(DRY/LIVE)이 계속되어야 하면 False.
    """
    if TRADE_MODE == "REPAIR":
        _dispatch_repair()
        return True

    if STATE_DIAGNOSTIC_ONLY:
        _print_state_diagnostic()
        return True

    if STATE_REPAIR_ONLY:
        _repair_state_only()
        return True
    if STATE_CLEAR_FENCE_ONLY:
        _clear_fence_only()
        return True

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
        return True
    if STATE_REVERSE_RECONCILE_ONLY:
        _reverse_reconcile_only()
        return True
    if STATE_NET_INVESTED_REPAIR_ONLY:
        _net_invested_repair_only()
        return True
    if STATE_ASSUME_REVERSE_EXPIRY_ONLY:
        _assume_reverse_expiry_only()
        return True
    if STATE_SET_CYCLE_START_ONLY:
        _set_cycle_start_only()
        return True
    return False


def _main_locked():
    """
    자동매매 봇의 메인 실행 함수입니다.

    Returns:
        bool: 전용 모드가 실행을 처리했으면 True (호출자는 원장 확정 후 종료),
              일반 실행(DRY/LIVE)이 계속되어야 하면 False.
    """
    # 진단·복구 전용 모드는 브로커 생성 없이 여기서 끝나며, 공개 로그에 마스킹
    # 토큰을 남길 수 있습니다. 전용 모드가 처리했으면 원장을 확정하고 main()도
    # 종료합니다 (계속 진행하면 읽기 전용 진단 뒤에 실주문이 나갈 수 있음).
    # flush 는 once-flag 을 세우지 않는 원시 호출이라, 일반 실행이 뒤이어
    # 종료 시점에 남기는 전체 원장에 영향을 주지 않습니다 (덮어쓰기 구조).
    # 또한 raise·sys.exit 경로에서도 finally 가 동작해 부분 매핑을 남깁니다.
    # 전용 모드는 state 를 저장하므로 main() 이 잠금을 생략한 경우(대기 단계)에도
    # 반드시 잠금 안에서 실행합니다 (fail-closed).
    try:
        with global_lock():
            _dispatch_handled = _run_mode_dispatch()
    finally:
        flush_odno_ledger()
    if _dispatch_handled:
        return


    # 전체 T 재추정은 주문을 절대 발생시키지 않아야 합니다.
    # 브로커 생성 전 config와 이 모듈의 모드를 함께 DRY로 고정합니다.
    import config as runtime_config
    if runtime_config.FORCE_T_REINFERENCE and runtime_config.TRADE_MODE == "LIVE":
        runtime_config.TRADE_MODE = "DRY"
        globals()["TRADE_MODE"] = "DRY"
        print("[T 보정] FORCE_T_REINFERENCE=true + LIVE → 주문 방지를 위해 브로커 생성 전 DRY(미리보기)로 고정합니다. "
              "state 는 저장되지 않습니다 (실제 저장은 TRADE_MODE=REPAIR FORCE_T_REINFERENCE=true).")

    # DRY·LIVE(DRY 로 고정된 경우 포함)에서는 {SYMBOL}_FORCE_T 를 적용하지 않습니다 —
    # 브로커 생성·state 변경·주문 전에 종료 코드 1 로 중단합니다. FORCE_T 는 TRADE_MODE=REPAIR 에서만 허용.
    # FORCE_T_REINFERENCE 는 DRY 에서 미리보기(저장 없음), REPAIR 에서 실제 저장으로 구분이 유지됩니다.
    _force_t_block = _force_t_mode_block_message(TRADE_MODE, SYMBOLS)
    if _force_t_block:
        print(f"[중단] {_force_t_block}")
        sys.exit(1)

    run_phase = validate_run_phase(os.getenv("RUN_PHASE", ""), TRADE_MODE)
    if run_phase == "VERIFY":
        # VERIFY 도 브로커를 **읽기 전용**으로만 사용합니다 (주문 API 없음).
        # 대기 단계가 아니지만, main() 이 잠금을 생략한 경우에도 state/근거
        # 파일을 다른 실행과 동시에 읽지 않도록 잠금 안에서 수행합니다.
        verify_broker = create_broker()
        try:
            with global_lock():
                sys.exit(_run_verify_phase(broker=verify_broker))
        finally:
            verify_broker.close()

    broker = create_broker()
    try:
        # stdout 버퍼링 해제: GitHub Actions 또는 로컬에서 출력이 한꺼번에 나오지 않고
        # 줄 단위로 실시간 표시되도록 합니다. (PYTHONUNBUFFERED 환경변수와 동일한 효과)
        sys.stdout.reconfigure(line_buffering=True)

        print("\n" + "=" * 60)
        print("자동매매 봇 시작")
        print("=" * 60)

        notify("🚀 무한매수 자동매매 시작")

        # ── RUN_PHASE 분리 실행 ──
        # SETTLE/PREVIEW/APPROVE 는 ReadOnlyBroker, EXECUTE 만 실제 브로커를 받습니다.
        if run_phase:
            failures = _dispatch_pipeline_phase(run_phase, broker)
            print(f"\n[{run_phase}] 완료 — 실패 {failures}건")
            if failures:
                sys.exit(1)
            return

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
                # 공개 로그/알림에는 주문번호 참조 원문을 남기지 않습니다 (진단 정보는 유지).
                error_text = _mask_odno_references(str(error))
                print(f"\n✗ {symbol} 처리 중 오류 발생: {error_text}")
                if TRADE_MODE != "DRY" or "잔고 부족:" not in str(error):
                    notify(f"⚠️ {symbol} 오류\n\n{error_text}", urgent=True)
                run_results.append({
                    "symbol": symbol, "executed": 0, "reserved": 0,
                    "failed": 1, "errors": [error_text],
                })

            except Exception as error:
                # 한 종목이 실패해도 나머지 종목은 계속 처리합니다
                error_text = _mask_odno_references(str(error))
                print(f"\n✗ {symbol} 처리 중 오류 발생: {error_text}")
                if TRADE_MODE != "DRY" or "잔고 부족:" not in str(error):
                    notify(f"⚠️ {symbol} 오류\n\n{error_text}", urgent=True)
                run_results.append({
                    "symbol": symbol, "executed": 0, "reserved": 0,
                    "failed": 1, "errors": [error_text],
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
        # 공개 로그/알림에는 주문번호 참조 원문을 남기지 않습니다 (진단 정보는 유지).
        error_text = _mask_odno_references(str(error))
        print(f"에러: {error_text}")

        message = f"""🚨 치명적 에러 발생

{error_text}"""
        notify(message, urgent=True)

        import traceback

        print("\n[상세 에러 정보]")
        print(_mask_odno_references(traceback.format_exc()))

        print("\n프로그램을 에러와 함께 종료합니다.")
        sys.exit(1)
    finally:
        # 정산 근거와 같은 시점(실행 종료)에 주문번호 매핑 원장을 1회 기록합니다.
        # 정산 근거 파일을 남기는 실행(DRY 포함)과 게이팅이 같고, 원장이 비어 있으면
        # 아무 파일도 만들지 않습니다. 암호화 Evidence 묶음에만 포함됩니다.
        _flush_odno_ledger_once()
        broker.close()


if __name__ == "__main__":
    main()
