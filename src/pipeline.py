"""
파이프라인 분리 — SETTLE / PREVIEW / APPROVE 단계.

이 모듈은 **주문 API를 호출하지 않습니다**. 조회/계산/승인 파일 기록만 담당하며,
실제 주문 실행은 `pipeline_execute.py` 로 분리되어 있습니다.

단계:
    settle_positions()    체결 정산 · T/순투입/원장 갱신 (LIVE 1회 저장)
    build_order_plan()    전략 실행 → 주문 계획(OrderPlan) 생성 (저장 없음)
    approve_order_plan()  계획을 approvals/{plan_id}.json 으로 원자적 기록
"""
import hashlib
import hmac
import json
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX 운영 환경 전제
    fcntl = None

import config
from state import (
    _fill_amount,
    _has_unconfirmed_amount_fills,
    _order_us_session_date,
    _parse_processed_qty_marker,
    canonical_state_hash,
    compute_position_from_history,
    mask_odno,
    reset_settlement_ledger,
    update_T_from_history,
)

APPROVALS_DIR = os.getenv("APPROVALS_DIR", "approvals")
# 경로/기본값은 config.py 한 곳에서만 정의합니다 (여기서 env 를 다시 읽지 않습니다).
PLAN_DIR = config.PLAN_DIR
# v2: created_at_utc/expires_at_utc 를 plan_hash 에 포함하고, account_id 는
#     원문 대신 HMAC-SHA256 지문 + 마스킹 표시값만 저장합니다.
#     v1 파일은 명시적으로 거부합니다. (P2-H / P2-D)
# v3: 승인 시점 실행 허용오차(tolerance_policy)를 plan_hash 에 바인딩합니다.
#     v2/v1 파일은 재승인(PREVIEW→APPROVE)이 필요합니다.
APPROVAL_SCHEMA_VERSION = 3
PLAN_SCHEMA_VERSION = 3

# SETTLE 보류 사유: state 는 저장됐지만 evidence `committed=true` 승격에 실패한 경우.
# PREVIEW 는 근거 부재로 fail-closed 되고, SETTLE 디스패처는 halt_reason 으로 보류 집계합니다.
HALT_EVIDENCE_COMMIT_FAILED = "evidence_commit_failed"


def _tb():
    """trading_bot 모듈을 지연 import 합니다 (테스트 monkeypatch 호환)."""
    import trading_bot

    return trading_bot


def _et_today(now=None):
    """미국(ET) 기준 오늘 날짜. aware datetime 은 ET 로 변환해 날짜를 셉니다.

    `now` 는 UTC aware 로 들어오는 경우가 많아(승인 생성/검증) 그대로 date() 를
    쓰면 UTC 자정~ET 자정 사이에 승인 세션과 검증 세션이 하루 어긋납니다.
    """
    if now is None:
        current = _tb().datetime.now(ZoneInfo("America/New_York"))
    else:
        current = now
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(ZoneInfo("America/New_York"))
    return current.date().isoformat()


def settlement_sessions(now=None):
    """(정산 대상 세션, 실행 대상 세션)을 계산합니다.

    - `settled_session`   : SETTLE 이 정산하는 **이미 마감된** 미국 정규 세션
    - `execution_session` : 그 다음 세션 = PREVIEW/APPROVE/EXECUTE 의 주문 대상

    KST 아침에 SETTLE/PREVIEW 를 돌리면 ET 로는 전날 밤이라 `_et_today()` 가
    하루 이릅니다. 세션을 `_et_today()` 로 비교하면 18:30 EXECUTE 와 어긋나므로,
    주말/공휴일/서머타임을 반영하는 XNYS 캘린더로 세션을 분리합니다.

    여기서 `settled_session` 은 **정규장이 마감된** 가장 최근 세션입니다.
    애프터아워 종료 여부는 `us_session_after_hours_end()` 로 SETTLE 이 별도
    확인합니다 (KST 09:30 = 겨울 ET 19:30 처럼 정규장만 닫힌 시각이 있습니다).
    정규장이 **진행 중**인 세션은 절대 대상이 되지 않습니다 (last-closed 만).

    Returns:
        tuple[str, str]: (settled_session, execution_session). 캘린더 범위를
        벗어나면 ("", "") — 호출자는 정산/실행을 보류해야 합니다.
    """
    from broker.market_utils import next_us_session, us_session_last_closed_date

    settled = us_session_last_closed_date(now_utc=now)
    if not settled:
        return "", ""
    execution = next_us_session(settled)
    return settled, (execution or "")


def _utc_now_iso(now=None):
    current = now or _tb().datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class SettledState:
    """정산 단계의 결과. PREVIEW/EXECUTE 는 이 값만 신뢰합니다."""

    state: dict
    symbol: str
    exchange: str
    splits: int
    symbol_type: str
    seed: float
    effective_seed: float
    T: float
    reverse_mode: dict = field(default_factory=dict)
    close_prices: list = field(default_factory=list)
    position_basis: Optional[float] = None
    evidence: Optional[dict] = None
    cycle_report: Optional[dict] = None
    next_cycle_seed: Optional[float] = None
    halt_reason: Optional[str] = None
    order_history: list = field(default_factory=list)
    additional_loc_levels: int = 3
    settlement_run_id: str = ""
    session: str = ""
    # SETTLE 이 정산한(이미 마감된) 세션. `session` 은 실행 대상(다음) 세션입니다.
    settled_session: str = ""
    evidence_id: str = ""
    evidence_path: str = ""


@dataclass
class OrderPlan:
    """PREVIEW 가 만든 주문 계획. 이 객체가 승인 파일의 원본입니다."""

    plan_id: str
    symbol: str
    exchange: str
    session: str
    orders: list
    state_hash: str
    plan_hash: str
    settlement_run_id: str
    created_at_utc: str
    expires_at_utc: str
    state_patch: dict = field(default_factory=dict)
    strategy_result: dict = field(default_factory=dict)
    evidence_id: str = ""
    broker: str = ""
    broker_mode: str = ""
    account_id: str = ""
    # 표시용 마스킹 계좌 id (권한 판정에 쓰지 않음 — P2-D)
    account_display: str = ""
    # 승인 시점에 고정된 실행 허용오차 (plan_hash 에 포함 — v3)
    tolerance_policy: dict = field(default_factory=dict)

    def to_approval(self):
        return {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "session": self.session,
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "state_hash": self.state_hash,
            "settlement_run_id": self.settlement_run_id,
            "evidence_id": self.evidence_id or self.settlement_run_id,
            "broker": self.broker,
            "broker_mode": self.broker_mode,
            "account_id": self.account_id,
            "account_display": self.account_display,
            "plan_hash": self.plan_hash,
            "tolerance_policy": deepcopy(self.tolerance_policy or {}),
            "orders": deepcopy(self.orders),
            # 승인된 상태 변경(close_prices/reverse_mode) — 주문 자체는 orders 에만.
            "state_patch": deepcopy(self.state_patch),
            "consumed": False,
        }


# ── 실행 허용오차 정책 (승인 시점에 고정) ─────────────────────────
# EXECUTE/VERIFY 가 **현재 env** 를 그대로 읽으면, 승인 후 env 를 넓혀 승인 범위
# 밖 주문을 통과시킬 수 있습니다. 그래서 PREVIEW/APPROVE 시점의 유효 정책을
# 계획/승인 파일에 기록하고 plan_hash 에 묶습니다. 이후 env 는 **좁히는 것만**
# 허용합니다 (넓히려면 PREVIEW→APPROVE 재승인 필요).
TOLERANCE_PRICE_PCT_ENV = "EXECUTION_PRICE_TOLERANCE_PCT"
TOLERANCE_PRICE_ABS_ENV = "EXECUTION_PRICE_TOLERANCE_ABS"
TOLERANCE_QTY_ENV = "EXECUTION_QTY_TOLERANCE"
# 기본값(env 미설정 시)은 config.py 의 파이프라인 상수 한 곳에서만 정의합니다.
# ⚠️ env 로 해석된 `config.EXECUTION_*` 이 아니라 **검증된 기본 상수**를 씁니다.
# 이 값은 `_tolerance_float/int` 의 fallback 이므로, 오염값(nan/inf/음수)이
# 새어들면 정책이 NaN 이 되어 모든 플랜이 조용히 거부됩니다.
DEFAULT_TOLERANCE_PRICE_PCT = config.DEFAULT_EXECUTION_PRICE_TOLERANCE_PCT
DEFAULT_TOLERANCE_PRICE_ABS = config.DEFAULT_EXECUTION_PRICE_TOLERANCE_ABS
DEFAULT_TOLERANCE_QTY = config.DEFAULT_EXECUTION_QTY_TOLERANCE


def _tolerance_float(source, name, default):
    try:
        value = float(source.get(name, default))
    except (TypeError, ValueError):
        return default
    # NaN/Infinity 는 비교를 무력화하므로 기본값으로 되돌립니다.
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _tolerance_int(source, name, default):
    try:
        value = int(source.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def execution_tolerance_policy(env=None):
    """현재 env 로 계산한 실행 허용오차 정책을 반환합니다."""
    source = os.environ if env is None else env
    return {
        "price_pct": _tolerance_float(
            source, TOLERANCE_PRICE_PCT_ENV, DEFAULT_TOLERANCE_PRICE_PCT
        ),
        "price_abs": _tolerance_float(
            source, TOLERANCE_PRICE_ABS_ENV, DEFAULT_TOLERANCE_PRICE_ABS
        ),
        "qty": _tolerance_int(source, TOLERANCE_QTY_ENV, DEFAULT_TOLERANCE_QTY),
    }


def normalize_tolerance_policy(policy):
    """파일에서 읽은 정책을 검증해 정규화합니다 (형식 오류면 None).

    NaN/Infinity 는 `x < 0` 비교를 모두 통과해 금액 비교를 무력화하므로
    유한 실수만 허용합니다. 수량은 bool(True/False)과 float 을 거부하고
    음수가 아닌 정수만 허용합니다.
    """
    if not isinstance(policy, dict):
        return None
    keys = ("price_pct", "price_abs", "qty")
    if not all(key in policy for key in keys):
        return None
    qty = policy["qty"]
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
        return None
    try:
        price_pct = float(policy["price_pct"])
        price_abs = float(policy["price_abs"])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(price_pct) and math.isfinite(price_abs)):
        return None
    if price_pct < 0 or price_abs < 0:
        return None
    return {"price_pct": price_pct, "price_abs": price_abs, "qty": qty}


def effective_tolerance_policy(approved, live=None):
    """승인 정책과 현재 env 정책 중 **더 좁은** 값을 씁니다.

    - 승인 정책이 없거나 손상되면 `None` (호출 측 fail-closed).
    - 각 항목은 min() 이므로 env 를 넓혀도 승인 범위를 넘지 못합니다.
    """
    approved_norm = normalize_tolerance_policy(approved)
    if approved_norm is None:
        return None
    live_norm = normalize_tolerance_policy(
        execution_tolerance_policy() if live is None else live
    )
    if live_norm is None:
        live_norm = approved_norm
    return {
        "price_pct": min(approved_norm["price_pct"], live_norm["price_pct"]),
        "price_abs": min(approved_norm["price_abs"], live_norm["price_abs"]),
        "qty": min(approved_norm["qty"], live_norm["qty"]),
    }


def price_tolerance(price, policy):
    """정책 기준 가격 허용오차(±USD)."""
    return max(
        float(policy["price_abs"]), abs(float(price or 0.0)) * float(policy["price_pct"])
    )


# ── 계획 비교 기준 (승인 / 실행 / 사후 검증 공용) ────────────────────
# 승인(APPROVE) → 실행(EXECUTE) → 검증(VERIFY) 세 지점이 **같은 기준**으로 계획을
# 대조해야 합니다. 한 곳만 느슨하면 승인 범위를 벗어난 계획이 실행되고, 한 곳만
# 엄격하면 정상 실행이 실패로 기록됩니다. 그래서 비교 규칙은 이 모듈 한 곳에만
# 두고 `pipeline_execute.compare_order_plans()`(EXECUTE)와
# `transmitted_plan_problems()`(VERIFY)가 함께 씁니다.
#
# 허용오차(승인 시점에 고정된 정책)는 **수량·가격·금액에만** 적용합니다. EXECUTE
# 는 승인 계획을 게이트로만 쓰고 실행 직전 재계산한 계획을 전송하므로, 시세가
# 허용오차 안에서 움직인 것은 **정상 드리프트**이며 이걸 막으면 실행 자체가
# 불가능해집니다.
#
# 반면 아래 필드는 **드리프트 대상이 아니라 정합성 대상**입니다. 시세가 움직여도
# 리버스 단계/배율/T 기준값이 바뀌면 같은 승인으로 다른 주문·다른 정산이 나가므로
# 허용오차 없이 정확히 일치해야 합니다.
COMPARE_EXACT_ORDER_FIELDS = ("side", "order_type", "comment")
COMPARE_EXACT_REVERSE_FIELDS = (
    "reverse_action",    # 리버스 매수/매도 구분
    "reverse_day",       # 리버스 일차 (1일차 MOC / 2일차+ LOC 분기)
    "reverse_base_t",    # 배율 적용 기준 T
    "reverse_t_factor",  # 매도 배율 (T × factor)
    "reverse_t_target",  # 리버스 쿼터매수 T 증가분
)
# T 반영 기준값(t_target)도 허용오차 없이 비교합니다 (주문별 T 가산 근거).
COMPARE_EXACT_NUMERIC_ORDER_FIELDS = ("t_target",)

# state_patch 비교 규칙
# - `reverse_mode` 는 승인에 없으면 실행에서 **추가할 수 없습니다** (승인 없이
#   전략 상태머신·사이클·리버스 단계가 바뀌는 경로를 막습니다).
COMPARE_REQUIRED_PATCH_KEYS = ("reverse_mode",)
# - `close_prices` 의 마지막 원소는 이번 실행의 **현재가**라 정상 드리프트
#   대상입니다. 이력 부분과 길이는 정확 일치, 마지막 원소만 가격 허용오차를
#   적용합니다. 승인 패치에 이 키가 없으면 "바인딩되지 않음"으로 보고 추가를
#   허용합니다 (승인 내용 자체는 plan_hash 가 이미 고정).
PATCH_DRIFT_TAIL_KEY = "close_prices"

# 비교용 "필드 없음" 표식 — 0/빈 문자열과 구분해야 리버스 필드 누락을 잡습니다.
_COMPARE_MISSING = object()


def _compare_field(record, name):
    """비교용 필드 조회 — 키 없음과 `None` 을 같은 "값 없음"으로 정규화합니다.

    `apply_state_patch()` 와 주문 메타가 `None` 을 "값 없음"으로 취급하므로 비교
    에서도 동일하게 봅니다 (None 과 키 부재를 다르게 보면 오탐이 납니다).
    """
    if not isinstance(record, dict):
        return _COMPARE_MISSING
    value = record.get(name)
    return _COMPARE_MISSING if value is None else value


def _finite_number(value, default=0.0):
    """비교 입력 숫자를 **유한한 실수**로 파싱합니다 (해석 불가/비유한이면 `None`).

    `abs(a - b) > tolerance` 는 NaN/Infinity 에서 항상 False 라, 비유한 가격은
    허용오차 비교를 **그대로 통과**합니다. 그래서 비교 입력은 유한한 실수만
    인정하고, 아니면 `None` 을 돌려 호출 측이 fail-closed 로 문제를 보고하게
    합니다 (`float()` 파싱 실패도 같은 취급).

    `None`/빈 문자열은 "값 없음" 표기이므로 `default` 로 봅니다 — 시장가 주문의
    `price` 0, 수량 미지정 같은 기존 케이스를 과차단하지 않기 위함입니다.
    bool 은 숫자로 보지 않습니다 (`True` 를 1주/1달러로 읽으면 안 됩니다).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def strict_equal(left, right):
    """허용오차 없는 비교 — 숫자 표현(int/float) 차이만 허용합니다.

    bool 은 값 타입까지 구분합니다(`True` 와 `1` 은 다릅니다). 리스트/딕셔너리는
    길이·키 집합까지 엄격하게 비교하며 **순서는 유지**합니다 (패치 값은 순서가
    의미를 가지는 배열이므로 무시하면 안 됩니다).
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(strict_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _exact_field_problem(
    index, approved, rebuilt, field, prefix, approved_label, rebuilt_label
):
    """허용오차를 적용하지 않는 주문 필드(리버스/T 기준값)를 비교합니다."""
    approved_value = _compare_field(approved, field)
    rebuilt_value = _compare_field(rebuilt, field)
    if approved_value is _COMPARE_MISSING and rebuilt_value is _COMPARE_MISSING:
        return None
    if approved_value is _COMPARE_MISSING or rebuilt_value is _COMPARE_MISSING:
        # 한쪽에만 있는 경우(예: 일반 주문 → 리버스 주문으로 바뀜)는 정확 일치가
        # 아니므로 그대로 불일치입니다 (허용오차 대상이 아닙니다).
        return (
            f"{prefix}주문 {index} {field} 필드 존재 불일치: "
            f"{approved_label}="
            f"{'없음' if approved_value is _COMPARE_MISSING else repr(approved_value)}, "
            f"{rebuilt_label}="
            f"{'없음' if rebuilt_value is _COMPARE_MISSING else repr(rebuilt_value)} "
            "(리버스/T 기준값은 드리프트 대상이 아닙니다)"
        )
    if strict_equal(approved_value, rebuilt_value):
        return None
    reason = (
        "T 가산 기준값" if field in COMPARE_EXACT_NUMERIC_ORDER_FIELDS
        else "리버스 단계/배율"
    )
    return (
        f"{prefix}주문 {index} {field} 불일치: "
        f"{approved_label}={approved_value!r}, {rebuilt_label}={rebuilt_value!r} "
        f"(허용오차 없음 — {reason}은 정합성 대상입니다)"
    )


def _close_prices_patch_problem(
    approved_prices, rebuilt_prices, policy,
    approved_label="승인", rebuilt_label="재계산",
):
    """close_prices 패치 비교 — 이력은 정확 일치, 마지막(현재가)만 허용오차."""
    approved = list(approved_prices or [])
    rebuilt = list(rebuilt_prices or [])
    if len(approved) != len(rebuilt):
        return (
            f"길이 불일치: {approved_label} {len(approved)}개 vs "
            f"{rebuilt_label} {len(rebuilt)}개"
        )
    if not approved:
        return None
    if not strict_equal(approved[:-1], rebuilt[:-1]):
        return (
            f"이력 종가 불일치: {approved_label} {approved[:-1]} vs "
            f"{rebuilt_label} {rebuilt[:-1]} "
            "(state 가 잠겨 있으므로 이력은 드리프트 대상이 아닙니다)"
        )
    approved_last = _finite_number(approved[-1])
    rebuilt_last = _finite_number(rebuilt[-1])
    if approved_last is None or rebuilt_last is None:
        return (
            f"현재가를 유한한 숫자로 읽을 수 없습니다: {approved_label} {approved[-1]!r}, "
            f"{rebuilt_label} {rebuilt[-1]!r} "
            "(비유한 값은 허용오차 비교를 통과하므로 신뢰할 수 없습니다)"
        )
    if approved_last < 0 or rebuilt_last < 0:
        return (
            f"현재가가 음수입니다: {approved_label}=${approved_last:.4f}, "
            f"{rebuilt_label}=${rebuilt_last:.4f} "
            "(종가는 0 이상이어야 합니다)"
        )
    try:
        tolerance = price_tolerance(approved_last, policy) if policy else 0.0
    except (KeyError, TypeError, ValueError):
        return (
            f"가격 허용오차를 계산할 수 없습니다: policy={policy!r} "
            "(승인 정책이 손상되었으므로 비교 대신 중단합니다)"
        )
    if not math.isfinite(tolerance):
        return (
            f"가격 허용오차가 유효한 숫자가 아닙니다: {tolerance!r} "
            "(정책 값이 손상되었습니다)"
        )
    if abs(approved_last - rebuilt_last) > tolerance:
        return (
            f"현재가 불일치: {approved_label}=${approved_last:.4f}, "
            f"{rebuilt_label}=${rebuilt_last:.4f} (허용 ±${tolerance:.4f})"
        )
    return None


def compare_plan_orders(
    approved_orders, rebuilt_orders, policy,
    *, prefix="", approved_label="승인", rebuilt_label="재계산",
):
    """승인 주문 목록과 재계산(전송/기록) 주문 목록을 **같은 기준**으로 비교합니다.

    - 수량·가격·금액: 승인 시점에 고정된 `policy` 허용오차 안에서만 허용
      (정상 드리프트). 단 **유한한 숫자**만 인정합니다 — NaN/Infinity/파싱 실패는
      허용오차 비교를 무력화하므로(`abs(NaN-a) > tol == False`) 문제로 보고합니다.
      가격·수량의 유효 범위는 0 이상입니다 (0 은 시장가/미지정 표기라 허용).
      수량은 **정수**만 인정합니다 — 소수 수량은 `int()` 로 잘려 3.1 과 3.9 가
      같은 3 으로 비교되므로(비교 무력화) 문제로 보고합니다.
    - side/order_type/comment: 정확 일치.
    - 리버스 필드(reverse_action/day/base_t/factor/t_target)와 t_target: 허용오차
      없이 정확 일치 — 시세 드리프트로 리버스 단계가 바뀌면 안 됩니다.

    `policy` 가 없으면 개수만 확인하고 주문별 비교는 하지 않습니다 (호출 측이
    정책 부재를 이미 fail-closed 로 처리합니다).
    """
    problems = []
    approved_orders = list(approved_orders or [])
    rebuilt_orders = list(rebuilt_orders or [])
    if len(approved_orders) != len(rebuilt_orders):
        problems.append(
            f"{prefix}주문 개수 불일치: {approved_label} {len(approved_orders)} vs "
            f"{rebuilt_label} {len(rebuilt_orders)}"
        )
        return problems
    if not policy:
        return problems
    try:
        qty_tol = int(policy["qty"])
    except (KeyError, TypeError, ValueError):
        qty_tol = 0
    exact_fields = (
        COMPARE_EXACT_REVERSE_FIELDS + COMPARE_EXACT_NUMERIC_ORDER_FIELDS
    )
    for index, (approved, rebuilt) in enumerate(
        zip(approved_orders, rebuilt_orders), 1
    ):
        for field in COMPARE_EXACT_ORDER_FIELDS:
            if str(approved.get(field, "")) != str(rebuilt.get(field, "")):
                problems.append(
                    f"{prefix}주문 {index} {field} 불일치: "
                    f"{approved_label}={approved.get(field)!r}, "
                    f"{rebuilt_label}={rebuilt.get(field)!r}"
                )
        a_qty_value = approved.get("quantity", 0)
        r_qty_value = rebuilt.get("quantity", 0)
        a_price_value = approved.get("price", 0)
        r_price_value = rebuilt.get("price", 0)

        # 수량·가격은 **유한한 숫자**여야 합니다. NaN/Infinity 는 허용오차 비교를
        # 통과하고( abs(a-b) > tol == False ), 파싱 실패는 비교를 예외로 터뜨립니다.
        # 둘 다 조용히 통과시키면 승인 범위를 벗어난 주문이 실행되므로 fail-closed.
        a_qty_number = _finite_number(a_qty_value)
        r_qty_number = _finite_number(r_qty_value)
        a_price = _finite_number(a_price_value)
        r_price = _finite_number(r_price_value)

        qty_ok = True
        if a_qty_number is None or r_qty_number is None:
            problems.append(
                f"{prefix}주문 {index} 수량이 유한한 숫자가 아닙니다: "
                f"{approved_label}={a_qty_value!r}, {rebuilt_label}={r_qty_value!r} "
                "(수량·금액을 비교할 수 없어 실행하지 않습니다)"
            )
            qty_ok = False
        elif a_qty_number < 0 or r_qty_number < 0:
            problems.append(
                f"{prefix}주문 {index} 수량이 음수입니다: "
                f"{approved_label}={a_qty_number!r}, {rebuilt_label}={r_qty_number!r} "
                "(주문 수량은 0 이상이어야 합니다)"
            )
            qty_ok = False
        # 수량은 **정수**여야 합니다. 소수 수량은 아래 `int()` 에서 잘려
        # 3.1 과 3.9 가 둘 다 3 이 되어 "같은 수량"으로 통과합니다 (비교 무력화).
        # 0 은 시장가/미지정 표기라 허용하되, 0.5 같은 소수는 차단합니다.
        elif not (a_qty_number.is_integer() and r_qty_number.is_integer()):
            problems.append(
                f"{prefix}주문 {index} 수량이 정수가 아닙니다: "
                f"{approved_label}={a_qty_number!r}, {rebuilt_label}={r_qty_number!r} "
                "(소수 수량은 정수로 잘려 비교가 무력화되므로 실행하지 않습니다)"
            )
            qty_ok = False
        a_qty = int(a_qty_number) if qty_ok else 0
        r_qty = int(r_qty_number) if qty_ok else 0
        if qty_ok and abs(a_qty - r_qty) > qty_tol:
            problems.append(
                f"{prefix}주문 {index} 수량 불일치: {approved_label}={a_qty}, "
                f"{rebuilt_label}={r_qty} (허용 {qty_tol})"
            )

        price_ok = True
        tolerance = 0.0
        if a_price is None or r_price is None:
            problems.append(
                f"{prefix}주문 {index} 가격이 유한한 숫자가 아닙니다: "
                f"{approved_label}={a_price_value!r}, {rebuilt_label}={r_price_value!r} "
                "(허용오차 비교가 불가능하므로 실행하지 않습니다)"
            )
            price_ok = False
        elif a_price < 0 or r_price < 0:
            problems.append(
                f"{prefix}주문 {index} 가격이 음수입니다: "
                f"{approved_label}={a_price!r}, {rebuilt_label}={r_price!r} "
                "(주문 가격은 0 이상이어야 합니다)"
            )
            price_ok = False
        if price_ok:
            try:
                tolerance = price_tolerance(a_price, policy)
            except (KeyError, TypeError, ValueError):
                tolerance = float("nan")
            if not math.isfinite(tolerance):
                problems.append(
                    f"{prefix}주문 {index} 가격 허용오차가 유효한 숫자가 아닙니다: "
                    f"{tolerance!r} (승인 정책이 손상되어 비교할 수 없습니다)"
                )
                price_ok = False
        if price_ok and abs(a_price - r_price) > tolerance:
            problems.append(
                f"{prefix}주문 {index} 가격 불일치: "
                f"{approved_label}=${a_price:.4f}, "
                f"{rebuilt_label}=${r_price:.4f} (허용 ±${tolerance:.4f})"
            )
        # 금액은 가격×수량 파생값이라, 두 입력이 유한할 때만 계산합니다.
        if qty_ok and price_ok:
            a_amount = a_price * a_qty
            r_amount = r_price * r_qty
            amount_tolerance = max(tolerance * max(a_qty, 1), 0.01)
            if not (
                math.isfinite(a_amount)
                and math.isfinite(r_amount)
                and math.isfinite(amount_tolerance)
            ):
                problems.append(
                    f"{prefix}주문 {index} 금액이 유한한 숫자가 아닙니다: "
                    f"{approved_label}={a_amount!r}, {rebuilt_label}={r_amount!r} "
                    "(금액을 신뢰할 수 없어 실행하지 않습니다)"
                )
            elif abs(a_amount - r_amount) > amount_tolerance:
                problems.append(
                    f"{prefix}주문 {index} 금액 불일치: "
                    f"{approved_label}=${a_amount:.2f}, "
                    f"{rebuilt_label}=${r_amount:.2f} "
                    f"(허용 ±${amount_tolerance:.2f})"
                )
        for field in exact_fields:
            problem = _exact_field_problem(
                index, approved, rebuilt, field, prefix,
                approved_label, rebuilt_label,
            )
            if problem:
                problems.append(problem)
    return problems


def _patch_items(patch, label):
    """state_patch 를 비교용 dict 로 정규화합니다 (`None` 값 키는 "변경 없음")."""
    if patch is None:
        return {}, None
    if not isinstance(patch, dict):
        return {}, (
            f"상태 변경(state_patch) 형식이 올바르지 않습니다: "
            f"{label}={type(patch).__name__}"
        )
    return {key: value for key, value in patch.items() if value is not None}, None


def compare_state_patches(
    approved_patch, rebuilt_patch, policy,
    *, prefix="", approved_label="승인", rebuilt_label="재계산",
):
    """승인 시점 state_patch 와 재계산(전송) state_patch 의 의미적 동등성을 검증합니
    다.

    키 집합으로 비교하고 **순서는 무시**하지만 값은 엄격합니다.
    - `reverse_mode`: 딕셔너리 전체를 허용오차 없이 정확히 비교 (리버스 단계/사이클/
      활성 상태가 승인과 달라지면 실행해서는 안 됩니다).
    - `close_prices`: 길이·이력 부분은 정확 일치, 마지막 원소(이번 실행 현재가)만
      가격 허용오차를 적용합니다 (정상 드리프트).
    - 승인 패치에 `close_prices` 가 없으면 바인딩되지 않은 것으로 보고 추가를
      허용합니다 (구버전 계획 호환 — 승인 내용은 plan_hash 로 이미 고정).
    """
    problems = []
    approved_items, approved_error = _patch_items(approved_patch, approved_label)
    rebuilt_items, rebuilt_error = _patch_items(rebuilt_patch, rebuilt_label)
    if approved_error:
        problems.append(f"{prefix}상태 변경 비교 불가 — {approved_error}")
    if rebuilt_error:
        problems.append(f"{prefix}상태 변경 비교 불가 — {rebuilt_error}")
    if approved_error or rebuilt_error:
        return problems
    for key in sorted(set(approved_items) | set(rebuilt_items)):
        if key in approved_items and key not in rebuilt_items:
            problems.append(
                f"{prefix}상태 변경 {key} 누락: {approved_label} 에 있는 변경이 "
                f"{rebuilt_label} 에 없습니다 "
                "(승인된 상태 변경은 실행에서 제거할 수 없습니다)"
            )
            continue
        if key not in approved_items:
            if key == PATCH_DRIFT_TAIL_KEY:
                # 승인 패치에 없는 close_prices = "바인딩되지 않음" (호환 경로).
                continue
            reason = (
                "전략 상태를 바꾸는 변경은 승인에 없으면 추가할 수 없습니다"
                if key in COMPARE_REQUIRED_PATCH_KEYS
                else "승인에 없는 상태 변경입니다"
            )
            problems.append(
                f"{prefix}상태 변경 {key} 추가: {rebuilt_label} 에만 있는 변경입니다 "
                f"(허용오차 없음 — {reason})"
            )
            continue
        if key == PATCH_DRIFT_TAIL_KEY:
            detail = _close_prices_patch_problem(
                approved_items[key], rebuilt_items[key], policy,
                approved_label=approved_label,
                rebuilt_label=rebuilt_label,
            )
            if detail:
                problems.append(f"{prefix}상태 변경 close_prices 불일치: {detail}")
            continue
        if not strict_equal(approved_items[key], rebuilt_items[key]):
            problems.append(
                f"{prefix}상태 변경 {key} 불일치: "
                f"{approved_label}={approved_items[key]!r}, "
                f"{rebuilt_label}={rebuilt_items[key]!r} (허용오차 없음)"
            )
    return problems


def compute_plan_hash(
    plan_id, symbol, exchange, session, state_hash, settlement_run_id, orders,
    state_patch=None, evidence_id=None, broker=None, broker_mode=None,
    account_id=None, created_at_utc=None, expires_at_utc=None,
    account_display=None, tolerance_policy=None,
):
    """승인/계획 파일의 주문 내용 + 실행 바인딩에 대한 안정적 지문(SHA-256).

    `created_at_utc`/`expires_at_utc` 도 포함합니다. 승인 만료시각을 파일에서
    직접 늘려도 plan_hash 가 깨지므로 위조할 수 없습니다 (P2-H).
    `account_display` 는 표시용 값이지만 파일 변조를 막기 위해 해시에 포함합니다.
    `tolerance_policy` 는 승인 시점의 실행 허용오차를 고정해, 승인 후 env 변경
    으로 위험 범위를 넓히지 못하게 합니다 (v3).
    """
    payload = {
        "plan_id": plan_id,
        "symbol": symbol,
        "exchange": exchange,
        "session": session,
        "state_hash": state_hash,
        "settlement_run_id": settlement_run_id,
        "evidence_id": evidence_id if evidence_id is not None else settlement_run_id,
        "broker": broker or "",
        "broker_mode": broker_mode or "",
        "account_id": account_id or "",
        "account_display": account_display or "",
        "created_at_utc": created_at_utc or "",
        "expires_at_utc": expires_at_utc or "",
        "orders": orders,
        "state_patch": state_patch or {},
        "tolerance_policy": tolerance_policy or {},
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ACCOUNT_FINGERPRINT_SECRET_ENV = "ACCOUNT_FINGERPRINT_SECRET"
# DRY/테스트 기본 키 — LIVE 에서는 절대 쓰지 않습니다 (아래 fail-closed 참고).
# 목적: 승인 파일에 **평문 계좌번호**를 남기지 않으면서, 서버 비밀값이 있으면
#       무차별 대입(짧은 계좌번호)으로 원문을 복원하지 못하게 합니다.
_ACCOUNT_FP_DRY_KEY = b"autotrade-account-fingerprint-dry-only-v1"


def _account_fingerprint_secret():
    """계좌 지문용 서버 비밀값 — LIVE 에서 미설정이면 fail-closed.

    운영 배포는 `ACCOUNT_FINGERPRINT_SECRET` 를 반드시 설정해야 합니다.
    DRY/테스트/REPAIR 에서는 고정 기본 키를 쓰므로 승인 파일 계약 테스트가
    비밀값 없이도 동작합니다 (운영과 다른 지문이 만들어지는 것은 의도된 동작 —
    DRY 산출물은 EXECUTE 대상이 아닙니다).
    """
    secret = os.getenv(ACCOUNT_FINGERPRINT_SECRET_ENV, "").strip()
    if secret:
        return secret.encode("utf-8")
    if str(getattr(_tb(), "TRADE_MODE", "")).upper() == "LIVE":
        raise RuntimeError(
            f"{ACCOUNT_FINGERPRINT_SECRET_ENV} 가 없어 계좌 지문을 만들 수 없습니다 — "
            "LIVE 에서는 승인 파일 계좌 바인딩이 무차별 대입에 노출되므로 중단합니다. "
            "서버 환경변수에 비밀값을 설정하세요."
        )
    return _ACCOUNT_FP_DRY_KEY


def _mask_account_id(value):
    """표시용 마스킹 계좌 id — 승인 파일을 사람이 대조할 때만 씁니다.

    지문(HMAC)만으로는 어느 계좌인지 확인할 수 없으므로 마지막 4자리만 남깁니다.
    (권한 판정에는 쓰지 않습니다 — 표시 전용)
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    return "****" + raw[-4:]


def _account_fingerprint(value):
    """계좌 식별자를 **HMAC-SHA256** 지문으로 바꿉니다 (원문 미저장).

    - 키: `ACCOUNT_FINGERPRINT_SECRET` (서버 비밀값, LIVE 필수)
    - 단순 sha256 은 짧은 계좌번호를 전수 대입으로 복원할 수 있어 사용하지 않습니다.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    digest = hmac.new(
        _account_fingerprint_secret(), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "hmac-sha256:" + digest


ACCOUNT_BINDING_ENV = "BROKER_ACCOUNT_BINDING"
# 계좌번호가 없는 브로커(메리츠 등)는 아래 자격증명/명시 바인딩으로 계좌를 식별합니다.
_ACCOUNTLESS_BROKERS = {"meritz"}


def _account_binding_label(broker_name):
    """운영자가 명시한 계좌 바인딩 라벨 (예: MERITZ_ACCOUNT_BINDING)."""
    specific = f"{str(broker_name or '').upper()}_ACCOUNT_BINDING"
    for name in (specific, ACCOUNT_BINDING_ENV):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _broker_identity():
    """승인 파일에 묶을 브로커/계좌 식별자(비밀값 제외)를 반환합니다.

    account_id 는 원문 계좌번호가 아니라 HMAC-SHA256 지문이고,
    account_display 는 사람이 대조하기 위한 **마스킹 표시값**입니다 (P2-D).
    비교는 지문끼리 하므로 승인 파일이 유출되어도 계좌번호가 노출되지 않습니다.

    계좌번호 필드가 없는 브로커(메리츠)는 계좌 바인딩이 비어 있으면 승인/실행
    계좌를 구분할 수 없습니다. 그래서 다음 순서로 **비민감 식별자**를 만듭니다:
    1. 운영자 명시 env (`MERITZ_ACCOUNT_BINDING` 또는 `BROKER_ACCOUNT_BINDING`)
    2. 설정된 자격증명(app_key/secret)의 SHA-256 해시 (원문·비밀값 미저장)
    둘 다 없으면 LIVE 에서는 fail-closed 로 거부합니다.
    """
    tb = _tb()
    broker_name = str(getattr(tb, "BROKER", "") or "")
    broker_mode = str(getattr(tb, "BROKER_MODE", "") or "")
    raw_account = ""
    cfg = {}
    try:
        from config import BROKER_CONFIG

        cfg = BROKER_CONFIG or {}
        raw_account = str(cfg.get("account_no") or cfg.get("account_seq") or "").strip()
    except Exception:
        raw_account = ""

    display = _mask_account_id(raw_account)
    if not raw_account:
        binding = _account_binding_label(broker_name)
        if binding:
            raw_account = f"binding:{binding}"
            display = _mask_account_id(binding)
        else:
            credentials = [
                str(cfg.get("app_key") or cfg.get("client_id") or ""),
                str(cfg.get("app_secret") or cfg.get("client_secret") or ""),
            ]
            # 지문용 서버 비밀값이 있을 때만 자격증명 해시를 만듭니다. 비밀값이
            # 없으면 LIVE 에서는 해시 자체를 만들 수 없으므로(무차별 대입 방지)
            # 아래에서 메리츠는 거부합니다. (LS/키움/토스는 계좌번호 없이도
            # 운용되므로 기존처럼 빈 바인딩을 허용합니다.)
            has_fp_secret = bool(os.getenv(ACCOUNT_FINGERPRINT_SECRET_ENV, "").strip())
            if any(credentials) and has_fp_secret:
                digest = hashlib.sha256(
                    "|".join([broker_name.lower(), broker_mode.lower(), *credentials]).encode(
                        "utf-8"
                    )
                ).hexdigest()
                raw_account = f"credential:{digest}"
                display = f"{broker_name.lower()}/{broker_mode.lower()}"
            elif broker_name.lower() in _ACCOUNTLESS_BROKERS and str(
                getattr(tb, "TRADE_MODE", "")
            ).upper() == "LIVE":
                raise RuntimeError(
                    f"{broker_name} 는 계좌번호가 없는 브로커라 계좌 바인딩이 필수입니다 — "
                    f"{broker_name.upper()}_ACCOUNT_BINDING (또는 {ACCOUNT_BINDING_ENV}) "
                    "환경변수에 계좌 식별 라벨을 설정하세요. "
                    f"(자격증명 지문을 쓰려면 {ACCOUNT_FINGERPRINT_SECRET_ENV} 도 필요합니다.)"
                )
    return {
        "broker": broker_name,
        "broker_mode": broker_mode,
        "account_id": _account_fingerprint(raw_account),
        "account_display": display,
    }


def _resolve_ttl(ttl_seconds=None, min_ttl_seconds=None):
    """승인 TTL 을 결정하고 운영 간격보다 짧으면 fail-closed 합니다."""
    ttl = config.APPROVAL_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    min_ttl = (
        config.APPROVAL_MIN_TTL_SECONDS
        if min_ttl_seconds is None
        else int(min_ttl_seconds)
    )
    if ttl < min_ttl:
        raise ValueError(
            f"승인 TTL({ttl}s)이 최소 허용치({min_ttl}s)보다 짧습니다 — "
            "ops 스케줄 간격을 덮도록 APPROVAL_TTL_SECONDS 를 늘리세요."
        )
    return ttl


def _resolve_settlement_run_id(now=None):
    """SETTLE 실행 식별자 — env RUN_ID 우선, 없으면 UTC 타임스탬프.

    과거 evidence 의 run_id 를 재사용하지 않습니다. PREVIEW/APPROVE 는
    state.last_settlement_id 로만 바인딩되므로 여기서 최신 파일을 참조할 필요가
    없고, 참조하면 실패한 SETTLE 의 id 를 재사용할 위험이 있습니다 (P1-E).
    """
    env_run_id = os.environ.get("RUN_ID", "").strip()
    if env_run_id:
        return env_run_id
    current = now or _tb().datetime.now(ZoneInfo("UTC"))
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _evidence_candidates(symbol=None):
    pattern = f"settlement-{symbol}-*.json" if symbol else "settlement-*-*.json"
    return sorted(
        (p for p in __import__("pathlib").Path(".").glob(pattern)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _load_evidence_candidate(path):
    """evidence 파일을 읽고 (run_id, committed) 를 반환합니다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", False
    if not isinstance(data, dict):
        return "", False
    return str(data.get("run_id") or "").strip(), data.get("committed") is True


def latest_evidence_run_id(symbol=None):
    """가장 최근 **커밋된** 정산 evidence 파일의 run_id (없으면 '').

    committed=true 는 SETTLE 이 실제로 state 를 저장한 뒤 기록됩니다.
    실패/보류/DRY evidence 는 committed=false 이므로 사용하지 않습니다 (P1-E).
    """
    for path in _evidence_candidates(symbol):
        run_id, committed = _load_evidence_candidate(path)
        if run_id and committed:
            return run_id
    return ""


def evidence_file_for_run(symbol, run_id):
    """run_id 가 일치하는 **커밋된** 정산 evidence 파일 경로 (없으면 '')."""
    if not run_id:
        return ""
    for path in _evidence_candidates(symbol):
        candidate_run_id, committed = _load_evidence_candidate(path)
        if candidate_run_id == str(run_id) and committed:
            return str(path)
    return ""


def _write_settlement_evidence(
    symbol, evidence, *, committed, failure_reason="", now=None
):
    """정산 근거 파일을 기록합니다. `committed` 는 state 저장 성공 여부입니다."""
    tb = _tb()
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"settlement-{symbol}-{stamp}.json"
    payload = dict(evidence)
    payload["committed"] = bool(committed)
    payload["committed_at_utc"] = (
        _utc_now_iso(current) if committed else ""
    )
    if failure_reason:
        payload["failure_reason"] = str(failure_reason)[:400]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def _mark_evidence_committed(path, evidence, committed, failure_reason=""):
    """기존 evidence 파일의 committed 표시만 원자적으로 갱신합니다."""
    payload = dict(evidence or {})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    payload["committed"] = bool(committed)
    payload["committed_at_utc"] = _utc_now_iso() if committed else ""
    if failure_reason:
        payload["failure_reason"] = str(failure_reason)[:400]
    _atomic_write_json(path, payload)
    return path


def _history_new_fill_odnos(state, order_history):
    """state 워터마크 이후 새로 반영될 체결의 odno 집합을 계산합니다.

    VERIFY 에서 "승인된 계획 주문의 체결" 과 "그 밖의 체결" 을 구분하기 위한
    보조 계산입니다. `_apply_recent_history_dt` 의 포함 규칙과 같은 기준
    (ord_datetime_utc > last_updated, 같으면 odno 비교)을 사용하고, 워터마크
    이전 등록 주문의 지연 체결 델타도 포함합니다.
    """
    odnos = set()
    last_updated_dt = _parse_iso(state.get("last_updated"))
    last_ordno = str(state.get("last_processed_ordno") or "")
    for row in order_history or []:
        try:
            filled = int(float(row.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            continue
        if filled <= 0:
            continue
        o_dt = _parse_iso(row.get("ord_datetime_utc"))
        if o_dt is None:
            continue
        o_dt = o_dt.astimezone(timezone.utc)
        if last_updated_dt is not None:
            last_dt = last_updated_dt.astimezone(timezone.utc)
            if o_dt < last_dt:
                continue
            if o_dt == last_dt:
                odno = str(row.get("odno") or "")
                try:
                    newer = int(odno) > int(last_ordno) if last_ordno else bool(odno)
                except (TypeError, ValueError):
                    newer = bool(odno and odno > last_ordno)
                if not newer:
                    continue
        odnos.add(str(row.get("odno") or ""))

    # 워터마크 이전 등록 주문의 지연 체결(부분체결 증가)도 "새 체결" 입니다.
    by_odno = {str(row.get("odno") or ""): row for row in (order_history or [])}
    for odno, meta in (state.get("orders_meta") or {}).items():
        row = by_odno.get(str(odno))
        if row is None:
            continue
        try:
            filled = int(float(row.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            continue
        _valid, processed = _parse_processed_qty_marker(meta)
        if filled > processed:
            odnos.add(str(odno))
    return odnos


def _session_ymd(value):
    """세션 날짜 문자열을 비교 가능한 YYYYMMDD 형태로 정규화합니다."""
    return str(value or "").strip().replace("-", "")


def _row_filled_qty(row):
    """이력 행의 누적 체결수량 (해석 불가 시 0)."""
    try:
        return float((row or {}).get("ft_ccld_qty", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def _split_settlement_session_fills(order_history, settled_session):
    """정산 세션까지의 이력과 **미룬(deferred)** 이후 세션 체결을 분리합니다.

    계약: SETTLE 은 `settled_session` 까지만 확정합니다. 세션 날짜가 그 이후인
    체결은 이번 정산의 T/순투입/원장 계산에서 제외하고 **다음 정산으로 미룹니다**.
    세션을 해석할 수 없는 행(None)은 기존처럼 정산 대상에 포함합니다.

    Returns:
        tuple: (정산 대상 이력, 이후 세션 체결 행, 이후 세션 순변동 수량)
    """
    settled_ymd = _session_ymd(settled_session)
    if not settled_ymd:
        return list(order_history or []), [], 0

    scoped = []
    deferred = []
    deferred_net_qty = 0
    for row in order_history or []:
        row_session = _session_ymd(_order_us_session_date(row))
        if row_session and row_session > settled_ymd:
            deferred.append(row)
            qty = int(_row_filled_qty(row))
            if row.get("sll_buy_dvsn_cd_name") == "매수":
                deferred_net_qty += qty
            elif row.get("sll_buy_dvsn_cd_name") == "매도":
                deferred_net_qty -= qty
            continue
        scoped.append(row)
    return scoped, deferred, deferred_net_qty


def _deferred_buy_spend(state, order_history, deferred_rows=None):
    """SETTLE 이 미룬 **매수** 체결의 확정 금액 합계를 계산합니다.

    EXECUTE 는 state 의 `net_invested` 로 시드 캡을 계산하는데, 미룬 매수는
    그 값에 아직 포함되지 않았습니다. 수량 reconciliation 은 이 달러 초과를
    막지 못하므로, 실행 직전 재계산에서만 쓰는 **오버레이**를 만듭니다.

    `deferred_rows` 를 넘기면 그 집합만 사용합니다. EXECUTE 는 허용 목록에
    실제로 넘긴 **동일한 행**을 넘겨, 기록(.deferred.json)과 지출 계산이
    서로 다른 live 스냅샷을 보지 않게 합니다.

    계약 (P1):
      - 매수만 포함합니다. 미룬 매도는 제외합니다 — 시드 캡을 느슨하게 만들지
        않는 보수적 방향입니다 (매도 회수액을 미리 인정하지 않음).
      - 금액 미확정(`ft_ccld_amt3` 없음/0) 매수가 하나라도 있으면 추정하지 않고
        `pending` 으로 반환합니다. 매수/매도 방향을 해석할 수 없는 체결도
        매수 가능성을 배제할 수 없어 `pending` 으로 처리합니다. 호출자는
        EXECUTE 를 중단해야 합니다.
      - 반환값은 state 에 저장하지 않습니다. 다음 SETTLE 이 유일한 기록자입니다.

    Returns:
        tuple[float, list[str]]: (미룬 매수 확정금액 합계, 금액 미확정 odno 목록)
    """
    if deferred_rows is None:
        _scoped, deferred, _net_qty = _split_settlement_session_fills(
            order_history, state.get("settled_session")
        )
    else:
        deferred = list(deferred_rows)
    spend = 0.0
    pending = []
    for row in deferred:
        if int(_row_filled_qty(row)) <= 0:
            continue
        side = row.get("sll_buy_dvsn_cd_name")
        if side not in ("매수", "매도"):
            # 방향을 해석할 수 없으면 매수일 가능성을 배제할 수 없으므로 fail-closed.
            pending.append(str(row.get("odno") or ""))
            continue
        if side != "매수":
            continue
        amount, estimated = _fill_amount(row)
        if estimated:
            pending.append(str(row.get("odno") or ""))
            continue
        spend += float(amount or 0.0)
    return round(spend, 2), pending


def _deferred_fills_evidence(deferred_rows):
    """SETTLE 이 미룬 체결을 검증용 근거(evidence) 행으로 정규화합니다.

    VERIFY 는 이 행을 live 주문이력과 (odno/side/qty) 대조하고, 기대 잔고에
    순변동을 더합니다. 금액은 **확정된 경우만** 기록합니다 (추정 금지).
    """
    rows = []
    for row in deferred_rows or []:
        qty = int(_row_filled_qty(row))
        if qty <= 0:
            continue
        side = str(row.get("sll_buy_dvsn_cd_name") or "")
        amount, estimated = _fill_amount(row)
        item = {
            "odno": str(row.get("odno") or ""),
            "side": side,
            "qty": qty,
            "session": _session_ymd(_order_us_session_date(row)),
            "amount_confirmed": not bool(estimated),
        }
        if not estimated:
            item["amount"] = round(float(amount or 0.0), 2)
        rows.append(item)
    return rows


def _bot_registered_odnos(state):
    """봇이 접수해 `orders_meta` 에 등록한 주문번호 집합 (문자열로 정규화).

    `register_order_meta_in_state()` 는 `str(odno)` 키로 저장하지만, 손으로 만든
    state·구버전 파일/브로커 응답에 따라 키가 int 일 수 있습니다. 비교는 항상
    문자열로 맞춥니다 (리버스 주문도 같은 `orders_meta` 에 등록됩니다).
    """
    return {str(key) for key in (state.get("orders_meta") or {})}


def _unregistered_deferred_fills(state, deferred_rows):
    """보류 체결 중 **봇 주문이 아닌**(수동) 체결 행을 골라냅니다.

    보류 체결을 재정산 허용 목록에 넣는 판단은 "봇이 낸 주문이 아직 정산되지
    않았다"는 뜻입니다. `orders_meta` 에 없는 주문은 봇 소유가 아니므로, 그
    체결(수동 주문)의 포지션·지출을 봇 실행 결과로 흡수해서는 안 됩니다.
    주문번호를 읽을 수 없는 행도 소유를 증명할 수 없어 같은 취급입니다.
    """
    bot_odnos = _bot_registered_odnos(state)
    manual = []
    for row in deferred_rows or []:
        odno = str((row or {}).get("odno") or "")
        if not odno or odno not in bot_odnos:
            manual.append(row)
    return manual


def _deferred_execution_session_fills(state, order_history):
    """SETTLE 이 실행 세션으로 미룬 체결 행을 반환합니다.

    계약: `state.settled_session` 이후이면서 **실행 세션과 같은 날짜**인 체결은
    SETTLE 이 워터마크를 그 앞에 멈춘 채 T/순투입/원장에서 제외한 체결입니다.
    이 체결은 그 세션의 이후 SETTLE 에서 반영되며, 그 전까지 EXECUTE 의
    재정산 비교에서는 "설명된 새 체결"로 취급해야 합니다.

    ⚠️ 이 함수는 **날짜 조건만** 봅니다. 허용 목록에 넣으려면 그 체결이 봇 주문
    (`state["orders_meta"]`)인지 반드시 별도로 확인해야 합니다
    (`_unregistered_deferred_fills` — 수동 체결은 허용 대상이 아닙니다).
    """
    settled_ymd = _session_ymd(state.get("settled_session"))
    execution_ymd = _session_ymd(state.get("execution_session"))
    if not settled_ymd or not execution_ymd or execution_ymd <= settled_ymd:
        return []

    rows = []
    for row in order_history or []:
        if _row_filled_qty(row) <= 0:
            continue
        row_session = _session_ymd(_order_us_session_date(row))
        if not row_session:
            continue
        if row_session == execution_ymd and row_session > settled_ymd:
            rows.append(row)
    return rows


def _assert_deferred_fills_explained(symbol, state, deferred_rows, balance):
    """미룬 실행 세션 체결이 **현재 잔고 변화를 정확히 설명**하는지 확인합니다.

    재정산 비교에서 미룬 체결을 허용하려면, state 의 워터마크 시점 보유수량
    (`position_qty`)에 그 체결의 순변동을 더한 값이 브로커 현재 잔고와 같아야
    합니다. 다르면 설명되지 않는 포지션 변화가 있다는 뜻이므로 fail-closed 로
    중단합니다 (미룬 체결을 조용히 통과시키지 않습니다).
    """
    if not deferred_rows:
        return
    if balance is None or balance.quantity is None:
        raise _tb().SymbolScopedError(
            f"{symbol} 실행 세션 보류 체결 검증 실패 — 브로커 잔고를 조회할 수 없어 "
            "보류 체결의 포지션 영향을 확인할 수 없습니다."
        )
    stored_qty = state.get("position_qty")
    if stored_qty is None:
        raise _tb().SymbolScopedError(
            f"{symbol} 실행 세션 보류 체결 검증 실패 — 상태에 워터마크 시점 보유수량"
            "(position_qty)이 없어 잔고 교차검증을 할 수 없습니다."
        )

    deferred_net_qty = 0
    for row in deferred_rows:
        qty = int(_row_filled_qty(row))
        if row.get("sll_buy_dvsn_cd_name") == "매수":
            deferred_net_qty += qty
        elif row.get("sll_buy_dvsn_cd_name") == "매도":
            deferred_net_qty -= qty

    expected_qty = int(stored_qty) + deferred_net_qty
    if int(balance.quantity) != expected_qty:
        raise _tb().SymbolScopedError(
            f"{symbol} 실행 세션 보류 체결 검증 실패 — 워터마크 보유 {int(stored_qty)}주 "
            f"+ 보류 체결 순변동 {deferred_net_qty:+d}주 = {expected_qty}주 인데 "
            f"브로커 잔고는 {int(balance.quantity)}주입니다. 설명되지 않는 체결/잔고 "
            "변경이 있어 진행할 수 없습니다."
        )


def _assert_reconciliation_with_deferred(
    symbol, state, order_history, balance, *, context
):
    """미룬 실행 세션 체결을 허용 목록에 넣어 재정산을 확인합니다.

    `assert_reconciliation_current(allowed_fill_odnos=...)` 는 새 체결이 모두
    허용 목록에 속할 때만 통과시킵니다. 그래서 허용 목록에 넣기 전에 두 가지를
    확인합니다:
      1. 그 체결이 **봇 주문**(`state["orders_meta"]` 등록)인지 — 수동 체결은
         봇 실행 결과가 아니므로 허용하면 안 됩니다 (fail-closed 중단).
      2. 그 체결이 현재 잔고 변화를 정확히 설명하는지.

    Returns:
        list[dict]: 허용 목록에 실제로 넘긴 보류 체결 행. EXECUTE 는 이 집합을
        실행 시점에 동결(.deferred.json)해 VERIFY 기준선으로 씁니다.
    """
    deferred_rows = _deferred_execution_session_fills(state, order_history)
    if deferred_rows:
        manual_rows = _unregistered_deferred_fills(state, deferred_rows)
        if manual_rows:
            # 수동 체결은 날짜·잔고만 맞으면 예전에는 허용 목록에 들어가 봇 소유가
            # 아닌 포지션·지출이 승인 실행 결과로 흡수됐습니다. 조용히 통과시키지
            # 않고 중단합니다 (주문번호는 가명 토큰으로만 출력).
            manual_odnos = [str(row.get("odno") or "") for row in manual_rows]
            raise _tb().SymbolScopedError(
                f"{symbol} 실행 세션 보류 체결 {len(manual_rows)}건이 봇 주문"
                "(orders_meta)에 등록되어 있지 않습니다 — 수동 체결은 재정산 허용 "
                "대상이 아니므로 진행할 수 없습니다 "
                f"(odno: {mask_odno(manual_odnos[:5])[1:-1]})."
            )
        _assert_deferred_fills_explained(symbol, state, deferred_rows, balance)
        allowed = {str(row.get("odno") or "") for row in deferred_rows}
        allowed.discard("")
        print(
            f"  [{context}] {symbol} 실행 세션 보류 체결 {len(deferred_rows)}건을 "
            "재정산 허용 목록으로 처리합니다 (T/순투입은 해당 세션의 다음 SETTLE 에서 반영)."
        )
    else:
        allowed = None
    assert_reconciliation_current(
        symbol,
        state,
        order_history,
        balance,
        allowed_fill_odnos=allowed,
    )
    return list(deferred_rows)


def assert_reconciliation_current(
    symbol, state, order_history, balance, allowed_fill_odnos=None
):
    """승인/실행 전 새 체결·잔고 불일치가 없는지 독립 재정산으로 확인합니다.

    `allowed_fill_odnos` 가 주어지면(=이미 실행된 승인 검증), 새 체결이 **모두**
    그 주문들에 속할 때만 T/순투입/잔고 변화를 승인 실행의 정상 결과로 허용합니다.
    승인 계획에 속하지 않는 체결이 하나라도 있으면 기존처럼 실패합니다 (P1-B).
    """
    reconciled = update_T_from_history(
        symbol,
        deepcopy(state),
        order_history,
        balance_qty=(balance.quantity if balance is not None else None),
    )
    holds = (
        reconciled.get("_pending_amount_odnos")
        or reconciled.get("_pending_marker_odnos")
        or reconciled.get("_settlement_hold")
        or reconciled.get("_position_basis_unknown")
    )
    if holds:
        raise _tb().SymbolScopedError(
            f"{symbol} 재정산 보류(체결/정산 미확정)가 있어 진행할 수 없습니다 "
            "— SETTLE 후 다시 시도하세요."
        )
    checks = (
        abs(float(reconciled.get("T", 0.0)) - float(state.get("T", 0.0))) > 1e-9,
        abs(
            float(reconciled.get("net_invested", 0.0))
            - float(state.get("net_invested", 0.0))
        )
        > 0.01,
        str(reconciled.get("last_updated", "")) != str(state.get("last_updated", "")),
        str(reconciled.get("last_processed_ordno", ""))
        != str(state.get("last_processed_ordno", "")),
        (
            state.get("position_qty") is not None
            and reconciled.get("position_qty") is not None
            and reconciled.get("position_qty") != state.get("position_qty")
        ),
    )
    if any(checks):
        if allowed_fill_odnos is not None:
            new_fills = _history_new_fill_odnos(state, order_history)
            unexplained = new_fills - {str(o) for o in allowed_fill_odnos}
            # 새 체결이 없는데 값이 달라졌다면 승인 실행으로 설명할 수 없습니다.
            if new_fills and not unexplained:
                return
        raise _tb().SymbolScopedError(
            f"{symbol} 승인 후 새 체결/정산 변경이 감지되었습니다 — SETTLE 후 다시 시도하세요."
        )



def assert_no_pending_fence(state, symbol=""):
    """미해결 주문 fence / 미확정 정산이 있으면 실행·승인을 거부합니다."""
    problems = []
    if state.get("pending_order_intent"):
        problems.append("pending_order_intent")
    if state.get("pending_order_batch"):
        problems.append("pending_order_batch")
    if state.get("_pending_amount_odnos"):
        problems.append("체결금액 미확정 주문")
    if state.get("_pending_marker_odnos"):
        problems.append("마커 누락·무효 주문")
    if state.get("_settlement_hold"):
        problems.append(
            f"정산 보류({state.get('_settlement_hold_reason', '근거 부족')})"
        )
    if state.get("_position_basis_unknown"):
        problems.append("보유수량 기준 미확보")
    if state.get("balance_mismatch"):
        problems.append("이력-잔고 불일치")
    if problems:
        raise RuntimeError(
            f"{symbol} 미해결 주문/정산이 있어 PREVIEW/APPROVE/EXECUTE 를 중단합니다: "
            + ", ".join(problems)
            + " — SETTLE/fence 복구 후 다시 시도하세요."
        )


def _atomic_write_json(path, payload):
    """mkstemp + fsync + os.replace 로 승인 파일을 원자적으로 기록합니다."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".approval.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def approval_path(plan_id, approvals_dir=None):
    directory = approvals_dir or APPROVALS_DIR
    return os.path.join(directory, f"{plan_id}.json")


def plan_path(plan_id, plans_dir=None):
    directory = plans_dir or PLAN_DIR
    return os.path.join(directory, f"{plan_id}.json")


def read_order_plan_file(plan_id=None, plans_dir=None):
    """PREVIEW 가 기록한 계획 파일을 읽습니다."""
    if plan_id and (plan_id.endswith(".json") or os.path.sep in plan_id):
        path = plan_id
    elif plan_id:
        path = plan_path(plan_id, plans_dir)
    else:
        raise ValueError("PLAN_ID 또는 계획 파일 경로가 필요합니다.")
    with open(path, "r", encoding="utf-8") as fh:
        plan_data = json.load(fh)
    plan_data["_path"] = path
    return plan_data


def write_order_plan(plan, plans_dir=None):
    """PREVIEW 산출물(계획 파일)을 원자적으로 기록합니다 (상태 저장/주문 없음)."""
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "symbol": plan.symbol,
        "exchange": plan.exchange,
        "session": plan.session,
        "created_at_utc": plan.created_at_utc,
        "expires_at_utc": plan.expires_at_utc,
        "state_hash": plan.state_hash,
        "settlement_run_id": plan.settlement_run_id,
        "evidence_id": plan.evidence_id or plan.settlement_run_id,
        "broker": plan.broker,
        "broker_mode": plan.broker_mode,
        "account_id": plan.account_id,
        "account_display": plan.account_display,
        "plan_hash": plan.plan_hash,
        "tolerance_policy": deepcopy(plan.tolerance_policy or {}),
        "orders": deepcopy(plan.orders),
        "state_patch": deepcopy(plan.state_patch),
    }
    path = plan_path(plan.plan_id, plans_dir)
    if os.path.exists(path):
        # 같은 plan_id 재기록 = 운영자가 확인한 계획 내용이 바뀔 수 있음.
        raise RuntimeError(
            f"계획 파일이 이미 존재합니다 (덮어쓰기 금지): {path} — "
            "PREVIEW 를 다시 실행해 새 plan_id 를 받으세요."
        )
    _atomic_write_json(path, payload)
    print(f"[PREVIEW] {plan.symbol} 계획 기록: {path} (orders={len(plan.orders)})")
    return path


def approve_order_plan(plan, approvals_dir=None, now=None, ttl_seconds=None):
    """APPROVE 진입점 — 전역 실행 잠금 안에서 승인 파일을 기록합니다."""
    with global_lock():
        return _approve_order_plan(plan, approvals_dir=approvals_dir, now=now, ttl_seconds=ttl_seconds)


def _approve_order_plan(plan, approvals_dir=None, now=None, ttl_seconds=None):
    """검증된 OrderPlan 을 승인 파일로 기록합니다. 주문은 실행하지 않습니다.

    계획을 새로 만들지 않습니다. PREVIEW 파일에서 읽은 plan_id/orders 를 그대로
    사용하며, 호출자는 state_hash/plan_hash 가 현재 상태와 일치함을 먼저 보장해야
    합니다.
    """
    if not plan.orders:
        print(f"[승인] {plan.symbol} 생성된 주문이 없어 승인 파일을 만들지 않습니다.")
        return None
    if not plan.plan_id or not plan.plan_hash:
        raise ValueError("승인할 계획에 plan_id/plan_hash 가 없습니다 — PREVIEW 를 먼저 실행하세요.")
    if normalize_tolerance_policy(plan.tolerance_policy) is None:
        raise ValueError(
            "승인할 계획에 유효한 허용오차 정책(tolerance_policy)이 없습니다 — "
            "PREVIEW 를 다시 실행하세요."
        )
    expected = compute_plan_hash(
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
        plan.tolerance_policy,
    )
    if expected != plan.plan_hash:
        raise ValueError("승인 파일 작성 중 plan_hash 불일치 — 계획을 신뢰할 수 없습니다.")
    path = approval_path(plan.plan_id, approvals_dir)
    _atomic_write_json(path, plan.to_approval())
    print(f"[승인] {plan.symbol} 주문 계획 승인 기록: {path} (orders={len(plan.orders)})")
    return path


def approve_plan_file(
    plan_ref, approvals_dir=None, plans_dir=None, now=None, broker=None
):
    """APPROVE 진입점 — 전역 실행 잠금 안에서 PREVIEW 계획을 승인합니다."""
    with global_lock():
        return _approve_plan_file(
            plan_ref, approvals_dir=approvals_dir, plans_dir=plans_dir, now=now, broker=broker
        )


def _approve_plan_file(
    plan_ref, approvals_dir=None, plans_dir=None, now=None, broker=None
):
    """APPROVE: PREVIEW 계획 파일을 **그대로** 승인합니다 (재생성 금지).

    - 계획 파일의 plan_hash 재계산이 일치해야 합니다.
    - 현재 state 의 canonical hash 가 계획의 state_hash 와 일치해야 합니다.
    - 승인 파일은 plan_id/orders 를 그대로 복사합니다.
    """
    plan_data = read_order_plan_file(plan_ref, plans_dir)
    if plan_data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError(
            "계획 schema_version 불일치: "
            f"{plan_data.get('schema_version')} != {PLAN_SCHEMA_VERSION}. "
            "구버전 계획(v1)은 만료/계정 지문 바인딩이 없어 승인할 수 없습니다 — "
            "PREVIEW 를 다시 실행하세요."
        )
    expected = compute_plan_hash(
        plan_data.get("plan_id"),
        plan_data.get("symbol"),
        plan_data.get("exchange"),
        plan_data.get("session"),
        plan_data.get("state_hash"),
        plan_data.get("settlement_run_id"),
        plan_data.get("orders"),
        plan_data.get("state_patch"),
        plan_data.get("evidence_id"),
        plan_data.get("broker"),
        plan_data.get("broker_mode"),
        plan_data.get("account_id"),
        plan_data.get("created_at_utc"),
        plan_data.get("expires_at_utc"),
        plan_data.get("account_display"),
        plan_data.get("tolerance_policy"),
    )
    if plan_data.get("plan_hash") != expected:
        raise ValueError(
            "계획 파일 plan_hash 불일치 — 변조되었거나 구버전(v1/v2) 계획입니다. "
            "PREVIEW 를 다시 실행하세요."
        )
    if normalize_tolerance_policy(plan_data.get("tolerance_policy")) is None:
        raise ValueError(
            "계획 파일에 유효한 허용오차 정책(tolerance_policy)이 없습니다 — "
            "구버전 계획이므로 PREVIEW 를 다시 실행하세요."
        )

    symbol = plan_data.get("symbol", "")
    state = _tb().load_state(symbol)
    assert_no_pending_fence(state, symbol)
    actual_hash = canonical_state_hash(state)
    if plan_data.get("state_hash") != actual_hash:
        raise RuntimeError(
            "계획 승인 거부: state 가 PREVIEW 이후 변경되었습니다 "
            f"(plan={plan_data.get('state_hash')}, current={actual_hash}). "
            "새 PREVIEW 를 실행하세요."
        )

    # PREVIEW/APPROVE 는 state 에 **마지막으로 저장된** 정산 실행 id 에만 묶습니다.
    committed_run_id = str(state.get("last_settlement_id") or "")
    if not committed_run_id:
        raise RuntimeError(
            "계획 승인 거부: state 에 커밋된 SETTLE 근거(last_settlement_id)가 없습니다. "
            "SETTLE 을 먼저 실행하세요."
        )
    if str(plan_data.get("settlement_run_id") or "") != committed_run_id:
        raise RuntimeError(
            "계획 승인 거부: 계획의 정산 run_id 가 state 의 마지막 커밋과 다릅니다 "
            f"(plan={plan_data.get('settlement_run_id')}, state={committed_run_id}). "
            "새 SETTLE → PREVIEW 를 실행하세요."
        )
    if str(plan_data.get("evidence_id") or "") != committed_run_id:
        raise RuntimeError(
            "계획 승인 거부: 계획 evidence_id 가 state 의 마지막 커밋 정산과 다릅니다. "
            "새 SETTLE → PREVIEW 를 실행하세요."
        )

    if broker is not None:
        history = broker.get_order_history(symbol, plan_data.get("exchange", ""), days=30)
        balance = broker.get_balance(symbol, plan_data.get("exchange", ""))
        # SETTLE 이 실행 세션으로 미룬 체결은 state 에 반영되지 않은 새 체결입니다.
        # 잔고 교차검증으로 설명되는 경우에만 재정산 허용 목록에 넣습니다.
        _assert_reconciliation_with_deferred(
            symbol, state, history, balance, context="APPROVE"
        )

    identity = _broker_identity()
    for key in ("broker", "broker_mode", "account_id", "account_display"):
        planned = str(plan_data.get(key) or "")
        current = str(identity.get(key) or "")
        if planned and planned != current:
            raise RuntimeError(
                f"계획 승인 거부: {key} 불일치 (plan={planned}, current={current})"
            )

    plan = OrderPlan(
        plan_id=plan_data.get("plan_id", ""),
        symbol=symbol,
        exchange=plan_data.get("exchange", ""),
        session=plan_data.get("session", ""),
        orders=list(plan_data.get("orders") or []),
        state_hash=plan_data.get("state_hash", ""),
        plan_hash=plan_data.get("plan_hash", ""),
        settlement_run_id=plan_data.get("settlement_run_id", ""),
        created_at_utc=plan_data.get("created_at_utc", ""),
        expires_at_utc=plan_data.get("expires_at_utc", ""),
        state_patch=dict(plan_data.get("state_patch") or {}),
        evidence_id=plan_data.get("evidence_id", ""),
        broker=plan_data.get("broker", ""),
        broker_mode=plan_data.get("broker_mode", ""),
        account_id=plan_data.get("account_id", ""),
        account_display=plan_data.get("account_display", ""),
        tolerance_policy=dict(plan_data.get("tolerance_policy") or {}),
    )
    # created_at/expires_at 는 PREVIEW 계획 파일의 값을 그대로 유지합니다.
    # (plan_hash 에 포함되어 있어 승인 시 재생성하면 해시가 깨집니다 — P2-H)
    return approve_order_plan(plan, approvals_dir=approvals_dir, now=now)


def load_approval(plan_id=None, approvals_dir=None):
    """plan_id 또는 파일 경로로 승인 파일을 읽습니다."""
    if plan_id and (plan_id.endswith(".json") or os.path.sep in plan_id):
        path = plan_id
    elif plan_id:
        path = approval_path(plan_id, approvals_dir)
    else:
        raise ValueError("plan_id 또는 승인 파일 경로가 필요합니다.")
    with open(path, "r", encoding="utf-8") as fh:
        approval = json.load(fh)
    approval["_path"] = path
    return approval


def verify_approval(
    approval, state, now=None, require_unconsumed=True, check_state_hash=True
):
    """승인 파일과 현재 상태를 대조해 문제 목록을 반환합니다 (읽기 전용)."""
    problems = []
    if not isinstance(approval, dict):
        return ["승인 파일 형식이 올바르지 않습니다."]
    if approval.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        problems.append(
            f"schema_version 불일치: {approval.get('schema_version')} != "
            f"{APPROVAL_SCHEMA_VERSION} (구버전 승인은 재승인 필요)"
        )
    if approval.get("consumed") and require_unconsumed:
        problems.append("이미 사용된 승인입니다 (consumed=true).")
    if not approval.get("symbol") or not approval.get("exchange"):
        problems.append("승인 파일에 symbol/exchange 가 없습니다.")
    if not isinstance(approval.get("orders"), list):
        problems.append("승인 파일에 orders 목록이 없습니다.")

    identity = _broker_identity()
    for key in ("broker", "broker_mode", "account_id", "account_display"):
        approved = str(approval.get(key) or "")
        current = str(identity.get(key) or "")
        if key not in approval:
            # 계좌 식별자가 없는 브로커(키움/LS/토스/메리츠)는 빈 값이 정상이므로
            # 필드 부재를 실패로 보지 않습니다. 값이 있는데 없는 경우만 거부합니다.
            if current:
                problems.append(f"승인 파일에 {key} 바인딩이 없습니다.")
            continue
        if approved != current:
            problems.append(
                f"{key} 불일치: 승인={approved}, 현재={current}"
            )
    if not approval.get("evidence_id") and not approval.get("settlement_run_id"):
        problems.append("승인 파일에 evidence_id/settlement_run_id 가 없습니다.")
    if normalize_tolerance_policy(approval.get("tolerance_policy")) is None:
        problems.append(
            "승인 파일에 유효한 허용오차 정책(tolerance_policy)이 없습니다 — "
            "실행 허용 범위를 확인할 수 없으므로 재승인이 필요합니다."
        )

    # 세션 검증: `_et_today()`(ET 오늘)는 KST 아침 실행에서 하루 이르므로,
    # state 에 바인딩된 실행 세션(settled_session 의 **다음** 세션)과 비교합니다.
    expected_session = ""
    if isinstance(state, dict):
        expected_session = str(state.get("execution_session") or "")
    if not expected_session:
        _settled_session, expected_session = settlement_sessions(now)
    if not expected_session:
        problems.append("실행 세션을 확정할 수 없습니다 (XNYS 캘린더 범위)")
    else:
        if str(approval.get("session") or "") != expected_session:
            problems.append(
                "세션 불일치: 승인 "
                f"{approval.get('session')} != 실행 대상 세션 {expected_session}"
            )
        # 승인 TTL(기본 9h)을 넘겨 다음 세션까지 살아남은 승인은 실행하지 않습니다.
        current_et = _et_today(now)
        if current_et > expected_session:
            problems.append(
                f"승인 세션이 지났습니다: 승인 {expected_session}, 오늘(ET) {current_et} "
                "— 새 SETTLE→PREVIEW→APPROVE 가 필요합니다."
            )
        elif current_et < expected_session:
            # 실행 대상 세션의 ET 날짜보다 이르면(예: KST 아침 = ET 전날 밤)
            # 아직 그 거래일이 시작되지 않았습니다. 프리장 대기로 해결되는 시간
            # 범위(당일 00:00~프리장)와 구분해 **잘못된 거래일** 실행을 거부합니다.
            problems.append(
                f"승인 실행 세션이 아직 시작되지 않았습니다: 승인 {expected_session}, "
                f"오늘(ET) {current_et} — 해당 거래일에 실행하세요."
            )

    current_now = now or _tb().datetime.now(ZoneInfo("UTC"))
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=timezone.utc)
    expires = _parse_iso(approval.get("expires_at_utc"))
    if expires is None:
        problems.append("expires_at_utc 를 해석할 수 없습니다.")
    elif current_now.astimezone(timezone.utc) >= expires:
        problems.append(f"승인이 만료되었습니다: {approval.get('expires_at_utc')}")

    if state is not None:
        if state.get("_state_unavailable"):
            problems.append(f"상태를 확인할 수 없습니다: {state.get('_state_unavailable')}")
        committed_run_id = str(state.get("last_settlement_id") or "")
        if not committed_run_id:
            problems.append(
                "state 에 커밋된 SETTLE 근거(last_settlement_id)가 없습니다 — "
                "SETTLE 을 먼저 실행하세요."
            )
        else:
            approved_run_id = str(
                approval.get("evidence_id")
                or approval.get("settlement_run_id")
                or ""
            )
            if approved_run_id != committed_run_id:
                problems.append(
                    "정산 run_id 불일치: 승인="
                    f"{approved_run_id or '(없음)'}, state 마지막 커밋={committed_run_id}"
                )
        if check_state_hash:
            actual_hash = canonical_state_hash(state)
            if approval.get("state_hash") != actual_hash:
                problems.append(
                    "상태 해시 불일치: 승인 후 state 가 변경되었습니다 "
                    f"(approved={approval.get('state_hash')}, current={actual_hash})"
                )

    expected = compute_plan_hash(
        approval.get("plan_id"),
        approval.get("symbol"),
        approval.get("exchange"),
        approval.get("session"),
        approval.get("state_hash"),
        approval.get("settlement_run_id"),
        approval.get("orders"),
        approval.get("state_patch"),
        approval.get("evidence_id"),
        approval.get("broker"),
        approval.get("broker_mode"),
        approval.get("account_id"),
        approval.get("created_at_utc"),
        approval.get("expires_at_utc"),
        approval.get("account_display"),
        approval.get("tolerance_policy"),
    )
    if approval.get("plan_hash") != expected:
        problems.append(
            "plan_hash 불일치: 승인 파일의 주문 내용이 변조되었거나 손상되었습니다."
        )
    return problems


EXECUTION_MARKER_SUFFIX = ".executed.json"
OUTCOME_RECORD_SUFFIX = ".recovery.json"
TRANSMITTED_PLAN_SUFFIX = ".transmitted.json"
DEFERRED_RECORD_SUFFIX = ".deferred.json"
# 실행이 시작된(=이미 소진된) 승인 결과. approval-skipped 는 재시도 가능하므로 제외.
_TERMINAL_APPROVAL_OUTCOMES = {"executed", "execution-failed"}


def execution_marker_path(approval_path):
    """승인 실행 마커 경로 — 한 번 실행된 승인은 다시 실행할 수 없습니다."""
    return f"{approval_path}{EXECUTION_MARKER_SUFFIX}"


def transmitted_plan_path(approval_path):
    """EXECUTE 가 **실제로 전송한** 최종 계획 기록 경로.

    승인 파일(승인 시점 값)과 별도로, 실행 직전 재계산한 계획(허용오차 내
    드리프트 반영)을 남깁니다. VERIFY 는 이 기록을 기준으로 주문을 대조해야
    실제 전송값과 승인값의 혼동이 없습니다.
    """
    return f"{approval_path}{TRANSMITTED_PLAN_SUFFIX}"


def read_transmitted_plan(approval_path):
    """실제 전송 계획 기록을 읽습니다 (없거나 손상됐으면 빈 dict)."""
    try:
        with open(transmitted_plan_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def deferred_record_path(approval_path):
    """EXECUTE 가 **동결한** 실행 세션 보류 체결 기록 경로.

    PRE-EXECUTE 에 수용한 보류 체결 집합을 실행 시점에 고정합니다. VERIFY 는
    live 이력만으로는 "EXECUTE 전에 수용된 체결"과 "EXECUTE 후에 도착한 체결"을
    구분할 수 없으므로, 이 기록을 기준선으로 써야 합니다.
    """
    return f"{approval_path}{DEFERRED_RECORD_SUFFIX}"


def read_deferred_record(approval_path):
    """EXECUTE 보류 체결 기록을 읽습니다 (없거나 손상됐으면 빈 dict)."""
    try:
        with open(deferred_record_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if data else {}


def transmitted_plan_problems(approval):
    """소비된 승인의 **실제 전송 기록**을 독립 검증합니다 (VERIFY 용).

    반환된 문제가 하나라도 있으면 검증 실패입니다. 소비(consumed=true)된
    승인은 반드시 유효한 `.transmitted.json` 을 가져야 합니다 — 없거나
    손상/타 승인 기록이면 "실행 내용을 확인할 수 없음"으로 실패합니다.

    검사:
      - 파일 존재/JSON 파싱
      - plan_id/symbol/session/state_hash/settlement_run_id/evidence_id 바인딩
      - approval_plan_hash == 승인 파일 plan_hash
      - transmitted_plan_hash 를 같은 입력으로 **재계산**해 일치
    """
    path = str(approval.get("_path") or "")
    if not path:
        return ["승인 파일 경로가 없어 전송 계획을 검증할 수 없습니다."]
    record_path = transmitted_plan_path(path)
    if not os.path.exists(record_path):
        return [
            "실제 전송 계획 기록(.transmitted.json)이 없습니다 "
            f"— 실행 내용을 확인할 수 없습니다: {record_path}"
        ]
    data = read_transmitted_plan(path)
    if not data:
        return [f"전송 계획 기록을 읽을 수 없습니다 (손상/형식 오류): {record_path}"]

    problems = []
    for key in ("plan_id", "symbol", "session"):
        recorded = str(data.get(key) or "")
        approved = str(approval.get(key) or "")
        if recorded != approved:
            problems.append(
                f"전송 계획 {key} 불일치: 기록={recorded or '(없음)'}, "
                f"승인={approved or '(없음)'}"
            )
    for key in ("state_hash", "settlement_run_id", "evidence_id"):
        recorded = str(data.get(key) or "")
        approved = str(
            approval.get(key)
            or (approval.get("settlement_run_id") if key == "evidence_id" else "")
            or ""
        )
        if recorded != approved:
            problems.append(
                f"전송 계획 {key} 불일치: 기록={recorded or '(없음)'}, "
                f"승인={approved or '(없음)'}"
            )
    approved_hash = str(approval.get("plan_hash") or "")
    if str(data.get("approval_plan_hash") or "") != approved_hash:
        problems.append(
            "전송 계획이 다른 승인에 묶여 있습니다 (approval_plan_hash 불일치)"
        )
    expected_hash = compute_plan_hash(
        data.get("plan_id"),
        data.get("symbol"),
        data.get("exchange"),
        data.get("session"),
        data.get("state_hash"),
        data.get("settlement_run_id"),
        data.get("orders"),
        data.get("state_patch"),
        data.get("evidence_id"),
        data.get("broker"),
        data.get("broker_mode"),
        data.get("account_id"),
        data.get("created_at_utc"),
        data.get("expires_at_utc"),
        data.get("account_display"),
        data.get("tolerance_policy"),
    )
    if str(data.get("transmitted_plan_hash") or "") != expected_hash:
        problems.append(
            "transmitted_plan_hash 불일치: 전송 기록의 주문 내용이 변조되었거나 "
            "손상되었습니다."
        )

    # ── 승인 허용오차 대조 ──────────────────────────────────────────
    # EXECUTE 는 재계산 계획을 전송할 수 있으므로, 전송 기록이 승인 계획의
    # 허용오차(수량/가격/금액) 안에 있어야 합니다. 해시가 유효해도 범위를
    # 벗어나면 재승인 없이 다른 주문을 낸 것이므로 실패입니다.
    #
    # 허용오차는 **승인 파일에 고정된 정책**을 기준으로 하고, 현재 env 는 그
    # 범위를 좁히는 데만 쓸 수 있습니다. 승인 후 env 를 넓혀도 위험 범위는
    # 넓어지지 않습니다 (넓히려면 재승인 필요 — v3).
    policy = effective_tolerance_policy(approval.get("tolerance_policy"))
    if policy is None:
        problems.append(
            "승인 파일의 허용오차 정책을 확인할 수 없습니다 (전송 계획 범위 검증 불가)"
        )
    # 주문 비교 기준은 EXECUTE 와 **같은 함수**를 씁니다 (승인 vs 전송).
    # 수량/가격/금액만 허용오차, 리버스 필드·t_target 은 허용오차 없이 정확 일치.
    problems.extend(
        compare_plan_orders(
            approval.get("orders") or [],
            data.get("orders") or [],
            policy,
            prefix="전송 ",
            approved_label="승인",
            rebuilt_label="전송",
        )
    )
    # 실제 전송 기록의 state_patch 도 같은 기준으로 대조합니다. EXECUTE 가 재계산
    # 계획을 전송하므로, 승인에 없던 리버스 상태 변경이나 다른 close_prices 가
    # 전송됐다면 검증 단계에서 잡아야 합니다 (실행 경로 비교와 동일 기준).
    problems.extend(
        compare_state_patches(
            approval.get("state_patch"),
            data.get("state_patch"),
            policy,
            prefix="전송 ",
            approved_label="승인",
            rebuilt_label="전송",
        )
    )
    return problems


def approval_outcome_path(approval_path):
    """EXECUTE 결과 기록(executed/execution-failed/approval-skipped) 경로."""
    return f"{approval_path}{OUTCOME_RECORD_SUFFIX}"


def read_approval_outcome(approval_path):
    """결과 기록을 읽습니다 (없거나 손상됐으면 빈 dict)."""
    try:
        with open(approval_outcome_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def approval_execution_blockers(path, approval=None):
    """이미 사용/실행된 승인인지 디스크 기록으로 판정합니다.

    - consumed=true (선점됨)
    - 실행 마커 파일 존재 (선점과 동시에 원자적으로 기록)
    - **실행이 시작된** 결과 기록 (executed/execution-failed)

    `approval-skipped` 는 선점 전(잔여 주문 취소 확정 실패 등)에 남는 기록이라
    승인을 소진하지 않으며, 조건이 해소되면 같은 승인으로 재시도할 수 있습니다.
    """
    path = str(path or "")
    if not path:
        return ["승인 파일 경로가 없습니다."]
    data = approval
    if not isinstance(data, dict):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    problems = []
    if data.get("consumed") is True:
        problems.append("이미 사용된 승인입니다 (consumed=true).")
    if os.path.exists(execution_marker_path(path)):
        problems.append("이미 실행된 승인입니다 (실행 마커 존재).")
    outcome = str(read_approval_outcome(path).get("outcome") or "")
    if outcome in _TERMINAL_APPROVAL_OUTCOMES:
        problems.append(f"이미 결과가 기록된 승인입니다 (outcome={outcome}).")
    return problems


@contextmanager
def _approval_lock(path):
    """승인 파일 단위 파일 잠금 — read-check-write 를 직렬화합니다."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ── 전역 실행 잠금 (state read-modify-write 직렬화) ──────────────
# run_daily.sh 는 `.run.lock` 을 flock 으로 잡지만, cron 을 거치지 않는
# **직접 CLI 실행**(SETTLE/EXECUTE/REPAIR)은 그 잠금을 우회합니다. 단일
# `.state.json` 을 여러 프로세스가 read-modify-write 하면 T/워터마크가
# 유실되므로, 셸과 파이썬이 같은 파일(`RUN_LOCK_PATH`, 기본 `.run.lock`)을
# 공유하는 전역 잠금을 둡니다. 승인 파일 잠금(`_approval_lock`)은 그 안쪽의
# 2차 가드로 유지합니다.
RUN_LOCK_PATH_ENV = "RUN_LOCK_PATH"
RUN_LOCK_HELD_ENV = "RUN_LOCK_HELD"
# 런처가 잠금을 잡은 fd 번호. 자식은 이 fd 가 RUN_LOCK_PATH 와 **같은 파일**을
# 가리키는지 확인한 뒤에만 "상속된 잠금"으로 인정합니다 (플래그만 신뢰 금지).
RUN_LOCK_FD_ENV = "RUN_LOCK_FD"
DEFAULT_RUN_LOCK_FD = 9
# 잠금 대기 시간의 env 해석·검증(유한·비음수)은 config 한 곳에서 합니다.
# NaN/Infinity 를 그대로 쓰면 `time.monotonic() >= deadline` 이 영원히 거짓이라
# 잠금 대기 루프가 끝나지 않습니다 (기본값 300초는 config 가 정의합니다).
DEFAULT_RUN_LOCK_TIMEOUT_SECONDS = config.read_run_lock_timeout_seconds()
# 경로별 재진입 깊이 — 같은 프로세스 안에서 상위 래퍼(main/디스패처)가 이미
# 잡은 잠금을 다시 잡으면 flock 은 자기 자신과 교착합니다.
_RUN_LOCKS = {}
# `src/pipeline.py` 의 부모 = 저장소 루트. 런처의 APP_DIR 과 같은 기준입니다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_lock_path():
    """전역 잠금 파일의 **절대 경로** (shell launcher 와 동일 경로여야 함).

    경로 우선순위:
      1. 프로세스 환경변수 `RUN_LOCK_PATH` (런처가 export 한 **해석된 절대 경로**)
      2. `.env` 의 `RUN_LOCK_PATH` (python-dotenv 는 기존 env 를 덮어쓰지 않음)
      3. 기본 `.run.lock`
    상대경로는 저장소 루트(=런처의 APP_DIR) 기준으로 절대화합니다. 그래야
    `.env` 에만 상대경로를 둔 경우에도 셸/파이썬이 같은 잠금 파일을 봅니다.
    `global_lock(path=...)` 에 **명시적으로 넘긴** 경로는 호출자 의도대로
    현재 작업 디렉터리 기준으로 해석합니다 (테스트/도구용).
    """
    raw = os.getenv(RUN_LOCK_PATH_ENV, "").strip() or ".run.lock"
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(_PROJECT_ROOT, raw))


def _lock_key(path):
    return os.path.abspath(path)


def _run_lock_inherited_fd(path):
    """런처가 물려준 잠금 fd 가 `path` 와 **같은 파일**인지 확인합니다.

    `RUN_LOCK_HELD=1` 만으로는 "이 프로세스 트리가 실제로 잠금을 물려받았다"를
    증명하지 못합니다 (예: 플래그만 남은 직접 CLI). 런처는 fd 9 를 flock 한 뒤
    `RUN_LOCK_FD=9` 를 export 하므로, fd 의 st_dev/st_ino 가 잠금 파일과
    일치할 때만 상속된 보유로 인정합니다.

    Returns:
        int | None: 일치하는 fd 번호. 증거가 없으면 None.
    """
    raw = os.getenv(RUN_LOCK_FD_ENV, str(DEFAULT_RUN_LOCK_FD)).strip()
    try:
        fd = int(raw)
    except (TypeError, ValueError):
        return None
    if fd <= 2:
        # stdin/stdout/stderr 는 잠금 fd 로 쓰지 않습니다.
        return None
    try:
        inherited = os.fstat(fd)
        target = os.stat(path)
    except OSError:
        return None
    if (inherited.st_dev, inherited.st_ino) != (target.st_dev, target.st_ino):
        return None
    return fd


def _run_lock_is_held_by_other(path):
    """다른 프로세스(또는 상속된 fd)가 실제로 잠금을 잡고 있는지 확인합니다.

    비파괴 probe 입니다. `RUN_LOCK_HELD=1` 은 **검증 없이 신뢰하지 않습니다**.
    """
    if fcntl is None:
        return False
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        handle.close()


def _inherited_lock_fd(path):
    """이 프로세스가 **런처의 잠금을 실제로 물려받았는지** 검증합니다.

    fd 가 같은 파일을 가리키는 것만으로는 부족합니다 (다른 프로세스가 잡은
    잠금을 가리키는 fd 일 수도 있음). 그래서:
      1) 잠금이 실제로 잡혀 있는지 먼저 확인하고 (probe),
      2) **상속된 fd 자체**에 재-flock 을 시도합니다.
    flock 은 같은 open file description 에서는 idempotent 하므로, 그 fd 가
    잠금을 들고 있는 description 이면 성공하고, 남의 잠금이면 EWOULDBLOCK
    으로 실패합니다. 실패하면 상속 증거가 아니므로 실제 잠금을 잡으러 갑니다.

    Returns:
        int | None: 상속된 잠금 fd. 증거가 없으면 None.
    """
    fd = _run_lock_inherited_fd(path)
    if fd is None or fcntl is None:
        return None
    if not _run_lock_is_held_by_other(path):
        # 아무도 잡고 있지 않다 → 상속이 아니라 그냥 새로 잡아야 합니다.
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd


@contextmanager
def global_lock(path=None, timeout_seconds=None):
    """전역 실행 잠금 — 상태 변경/실행 진입점을 프로세스 간 직렬화합니다.

    - **상속된 잠금**: 런처가 넘긴 fd(`RUN_LOCK_FD`, 기본 9)가 잠금 파일과 같은
      inode 이고 실제로 잠겨 있을 때만 재진입으로 인정합니다. 플래그만으로는
      우회하지 않습니다.
    - **플래그만 남은 직접 CLI**: 실제 잠금을 잡습니다. 다른 실행이 잡고 있으면
      `timeout_seconds` 동안 대기한 뒤 TimeoutError (fail-closed). 절대 동시
      진행하지 않습니다.
    - 같은 프로세스에서 재진입하면 깊이만 증가합니다.
    - `timeout_seconds` 안에 잠금을 얻지 못하면 TimeoutError (fail-closed).
    """
    target = run_lock_path() if path is None else os.path.abspath(path)
    key = _lock_key(target)
    state = _RUN_LOCKS.setdefault(key, {"depth": 0, "handle": None, "pid": None})
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # 같은 프로세스 재진입은 flock 재시도 전에 처리합니다 (자기 교착 방지).
    if state["depth"] > 0 and state["pid"] == os.getpid():
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
        return
    # 상속된 fd 증거가 있고 실제로 잠겨 있을 때만 우회합니다.
    if _inherited_lock_fd(target) is not None:
        yield
        return
    flag_active = os.getenv(RUN_LOCK_HELD_ENV, "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if flag_active:
        print(
            f"[잠금] {RUN_LOCK_HELD_ENV}=1 이지만 이 프로세스가 런처의 잠금"
            f"(fd {os.getenv(RUN_LOCK_FD_ENV, DEFAULT_RUN_LOCK_FD)})을 물려받지 "
            f"않았거나 잠금이 이미 해제되었습니다 — {target} 을 직접 잠급니다. "
            "다른 실행이 잡고 있으면 끝날 때까지 대기합니다."
        )
    if fcntl is None:
        yield
        return

    wait = (
        DEFAULT_RUN_LOCK_TIMEOUT_SECONDS
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    if not math.isfinite(wait):
        # env 와 같은 기준: NaN/Infinity 는 대기 마감을 무력화해 무한 대기가 됩니다.
        raise ValueError(
            f"global_lock timeout_seconds 는 유한한 숫자여야 합니다 (현재 값: {wait!r})"
        )
    handle = open(target, "a+", encoding="utf-8")
    deadline = time.monotonic() + max(wait, 0.0)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"전역 실행 잠금을 {wait:.0f}s 안에 얻지 못했습니다: {target} "
                        "— 다른 실행(SETTLE/EXECUTE/REPAIR)이 진행 중입니다."
                    )
                time.sleep(0.2)
        state.update(depth=1, handle=handle, pid=os.getpid())
        try:
            yield
        finally:
            state.update(depth=0, handle=None, pid=None)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


def claim_approval(approval, now=None):
    """승인을 소비 처리(consumed=true)합니다 — 주문 실행 **직전** 1회.

    파일 잠금으로 read-check-write 를 직렬화하고, 같은 잠금 안에서 **실행 마커**를
    함께 기록합니다. 마커/결과 기록이 있으면 재선점을 거부하므로 프로세스가
    재시작돼도 같은 승인을 다시 실행할 수 없습니다 (P0-A).
    """
    path = approval.get("_path")
    if not path:
        raise ValueError("승인 파일 경로가 없습니다.")
    with _approval_lock(path):
        return _claim_approval_locked(path, now=now)


def _claim_approval_locked(path, now=None):
    """파일 잠금을 **이미 잡은 상태**에서 호출하는 선점 본체.

    `execute_approved_plan()` 이 claim→handle 발급→실행을 같은 잠금 경계에서
    수행할 수 있도록 분리했습니다. 외부에서는 `claim_approval()` 만 사용합니다.
    """
    with open(path, "r", encoding="utf-8") as fh:
        latest = json.load(fh)
    blockers = approval_execution_blockers(path, latest)
    if blockers:
        raise RuntimeError(" / ".join(blockers))
    latest["consumed"] = True
    latest["consumed_at_utc"] = _utc_now_iso(now)
    _atomic_write_json(path, latest)
    # 승인 파일과 별도로 실행 마커를 남깁니다. 승인 파일만 되돌려도(수동 편집)
    # 마커가 남아 재실행을 막습니다.
    _atomic_write_json(
        execution_marker_path(path),
        {
            "plan_id": latest.get("plan_id", ""),
            "approval_path": str(path),
            "consumed_at_utc": latest["consumed_at_utc"],
            "at_utc": _utc_now_iso(now),
        },
    )
    latest["_path"] = path
    print(f"[승인] 사용 처리 완료: {path}")
    return latest


def _plan_id_suffix(created_utc):
    """PREVIEW 산출물마다 **고유**한 접미사 (생성시각 + 짧은 uuid).

    같은 세션/같은 RUN_ID 로 PREVIEW 를 두 번 실행해도 파일과 plan_id 가
    겹치지 않아야 합니다. 겹치면 두 번째 PREVIEW 가 첫 번째 계획 파일을
    덮어써, 운영자가 확인한 plan_id 의 **내용이 조용히 바뀝니다**.
    """
    parsed = _parse_iso(created_utc)
    stamp = (
        parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if parsed is not None
        else "unknown"
    )
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _prepare_plan_identity(plan, now=None, ttl_seconds=None):
    """plan_id / session / 만료 / 브로커 바인딩 / plan_hash 를 확정합니다."""
    tb = _tb()
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    ttl = _resolve_ttl(ttl_seconds)
    created = _utc_now_iso(current)
    expires = (current.astimezone(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    # 기본 세션은 "실행 대상 세션"입니다 (_et_today 는 KST 아침에 ET 전날).
    _settled_session, _execution_session = settlement_sessions(current)
    session = plan.session or _execution_session or _et_today(current)
    run_id = plan.settlement_run_id or _resolve_settlement_run_id(current)
    if not run_id:
        raise RuntimeError(
            "계획에 정산 run_id 가 없습니다 — PREVIEW 는 SETTLE 이 커밋한 "
            "last_settlement_id 에만 묶입니다. SETTLE 을 먼저 실행하세요."
        )
    identity = _broker_identity()

    plan.session = session
    plan.created_at_utc = created
    plan.expires_at_utc = expires
    plan.settlement_run_id = run_id
    plan.evidence_id = plan.evidence_id or run_id
    plan.broker = plan.broker or identity["broker"]
    plan.broker_mode = plan.broker_mode or identity["broker_mode"]
    plan.account_id = plan.account_id or identity["account_id"]
    plan.account_display = plan.account_display or identity.get("account_display", "")
    # 계획 파일은 PREVIEW 1회당 1개, **덮어쓰지 않습니다**. plan_id 에 생성시각과
    # uuid 를 넣어 APPROVE 가 운영자가 확인한 그 파일(그 내용+해시)만 승인하게 합니다.
    plan.plan_id = f"{plan.symbol}-{session}-{run_id}-{_plan_id_suffix(created)}"
    # 승인 시점의 유효 허용오차를 고정합니다. 이후 env 변경은 좁히는 것만 허용.
    plan.tolerance_policy = normalize_tolerance_policy(
        plan.tolerance_policy or execution_tolerance_policy()
    ) or execution_tolerance_policy()
    plan.plan_hash = compute_plan_hash(
        plan.plan_id,
        plan.symbol,
        plan.exchange,
        plan.session,
        plan.state_hash,
        plan.settlement_run_id,
        plan.orders,
        plan.state_patch,
        plan.evidence_id,
        plan.broker,
        plan.broker_mode,
        plan.account_id,
        plan.created_at_utc,
        plan.expires_at_utc,
        plan.account_display,
        plan.tolerance_policy,
    )
    return plan


def _set_approval_window(plan, now=None, ttl_seconds=None):
    """plan_id/orders/hash 는 유지하고 승인 시점 기준 TTL 만 갱신합니다."""
    tb = _tb()
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    ttl = _resolve_ttl(ttl_seconds)
    plan.created_at_utc = _utc_now_iso(current)
    plan.expires_at_utc = (
        current.astimezone(timezone.utc) + timedelta(seconds=ttl)
    ).isoformat()
    return plan


_VERIFY_RUN_MODULE = None


def _load_verify_run_module():
    """ops/verify_run.py 를 파일에서 로드합니다 (import 시 cwd 변경 복원)."""
    global _VERIFY_RUN_MODULE
    if _VERIFY_RUN_MODULE is not None:
        return _VERIFY_RUN_MODULE
    cwd = os.getcwd()
    try:
        import importlib.util
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "_pipeline_verify_run", root / "ops" / "verify_run.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VERIFY_RUN_MODULE = module
        return module
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass


def _verify_settlement_evidence(evidence):
    """ops/verify_run.py 의 독립 재계산으로 정산 근거를 검증합니다."""
    if evidence is None:
        return ["정산 근거가 없습니다."]
    try:
        return list(_load_verify_run_module()._verify_evidence(evidence) or [])
    except Exception as error:
        return [f"독립 검증기 실행 실패: {error}"]


def _verify_settlement_final(evidence_path, evidence, state):
    """**최종 저장 후보**(MAX_T/사이클 종료 후처리 반영)를 근거와 대조합니다.

    evidence 는 후처리 **전** 정산 결과(T_after 등)이고, state 는 실제로 저장할
    후보입니다. ops/verify_run.py 가 evidence 의 `post_processing` 규칙으로 기대
    최종값을 독립 계산해 대조합니다 — 근거와 저장값이 어긋난 채 커밋되는 것을
    막습니다 (P1: evidence-vs-final).
    """
    if not evidence_path:
        return ["정산 근거 파일이 없어 최종 후보를 검증할 수 없습니다."]
    tb = _tb()
    try:
        ok, problems = verify_evidence_file_with_ops(
            evidence_path,
            # 근거에 기록된 브로커/모드와 대조합니다. 이 검증의 목적은 "최종 저장
            # 후보"이므로 브로커 바인딩은 VERIFY 단계에서 실제 브로커로 확인합니다.
            broker=evidence.get("broker"),
            mode=evidence.get("mode") or tb.TRADE_MODE,
            run_id=evidence.get("run_id"),
            broker_mode=evidence.get("broker_mode"),
            final_state=state,
            max_t=evidence.get("max_t"),
        )
    except Exception as error:
        return [f"최종 후보 검증 실행 실패: {error}"]
    return [] if ok else list(problems or ["최종 후보 검증 실패"])


def verify_evidence_file_with_ops(path, **kwargs):
    """ops/verify_run.py 의 verify_evidence_file 을 그대로 호출합니다."""
    cwd = os.getcwd()
    try:
        return _load_verify_run_module().verify_evidence_file(path, **kwargs)
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass


def _history_settledness_enabled():
    flag = config.read_history_settledness_flag()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # 기본: 지연 이력이 알려진 메리츠만 반복 조회합니다.
    return str(getattr(_tb(), "BROKER", "")).lower() == "meritz"


def _history_snapshot(rows):
    snapshot = []
    for row in rows or []:
        snapshot.append(
            (
                str(row.get("odno", "")),
                str(row.get("ft_ccld_qty", "")),
                str(row.get("ft_ccld_amt3", "")),
                str(row.get("nccs_qty", "")),
                str(row.get("prcs_stat_name", "")),
            )
        )
    return sorted(snapshot)


def _history_has_recent_open_order(rows, now=None):
    """세션이 **끝난 뒤에도** 최근 미종결 주문이 남아 있으면 True.

    장중(ET 16:00 이전)에 보이는 미체결 주문은 정상일 수 있으므로 실패로 보지
    않습니다. 세션 종료 후 RECENT_ORDER_GUARD_MINUTES 이내의 미종결 주문은
    이력 정착 지연(또는 취소 누락)으로 판단합니다. 이전 세션의 만료 주문이
    `미체결`로 보이는 브로커(메리츠)는 guard 시간 밖이므로 통과합니다.
    """
    tb = _tb()
    guard_minutes = config.read_recent_order_guard_minutes()
    if guard_minutes <= 0:
        return False
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_utc = current.astimezone(timezone.utc)
    current_et = current_utc.astimezone(ZoneInfo("America/New_York"))
    cutoff = current_utc - timedelta(minutes=guard_minutes)
    open_tokens = {"미체결", "부분체결", "OPEN", "PARTIAL", "PENDING"}
    for row in rows or []:
        status = str(row.get("prcs_stat_name", "") or "").strip().upper()
        if status not in open_tokens:
            continue
        ts = _parse_iso(row.get("ord_datetime_utc"))
        if ts is None:
            continue
        ts_utc = ts.astimezone(timezone.utc)
        ts_et = ts_utc.astimezone(ZoneInfo("America/New_York"))
        session_closed = (
            ts_et.date() < current_et.date()
            or (ts_et.date() == current_et.date() and current_et.hour >= 16)
        )
        if not session_closed:
            continue  # 장중 미체결은 정상
        if ts_utc >= cutoff:
            return True
    return False


def _history_window_start_utc(history_days, now=None):
    current = now or _tb().datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        days = int(history_days)
    except (TypeError, ValueError):
        days = 30
    return current.astimezone(timezone.utc) - timedelta(days=days)


def _meta_order_utc(meta):
    """orders_meta 의 제출 시각을 UTC 로 복원합니다 (없으면 None)."""
    if not isinstance(meta, dict):
        return None
    submitted_at = str(meta.get("submitted_at") or "").strip()
    if submitted_at:
        for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
            try:
                kst = datetime.strptime(submitted_at, fmt).replace(
                    tzinfo=ZoneInfo("Asia/Seoul")
                )
                return kst.astimezone(timezone.utc)
            except ValueError:
                continue
        parsed = _parse_iso(submitted_at)
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    session = str(meta.get("submitted_session") or "").strip()
    if session:
        try:
            et_start = datetime.strptime(session, "%Y-%m-%d").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
            return et_start.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _verify_history_complete(state, history, history_days, now=None):
    """이력이 정산 워터마크/봇 주문 메타를 빠짐없이 포함하는지 확인합니다.

    반복 조회 동일성만으로는 "처음부터 계속 같은 누락"을 잡을 수 없습니다.
    다음을 fail-closed 로 검사합니다 (P1-F):
      1. state.last_updated(워터마크)가 조회 구간 안이고 워터마크 주문번호가 있으면,
         그 주문이 이력에 있어야 하며 이력 행의 시각이 state 보다 이전이면 안 됩니다.
      2. orders_meta 의 주문 중 제출 시각이 조회 구간 안이면 이력에 있어야 합니다.
    제출 시각이 없는 오래된 메타는 판단할 수 없어 건너뜁니다 (문서화된 한계).
    """
    problems = []
    rows = list(history or [])
    by_odno = {str(row.get("odno") or ""): row for row in rows}
    window_start = _history_window_start_utc(history_days, now)

    # 워터마크 **주문번호**가 있으면 그 주문은 반드시 이력에 있어야 합니다.
    # (주문번호 없이 last_updated 만 있는 경우 — FORCE_T 로 현재 시각만 기록한 경우 등 —
    #  는 대조할 행이 없으므로 판단하지 않습니다.)
    watermark_ts = _parse_iso(state.get("last_updated"))
    watermark_ordno = str(state.get("last_processed_ordno") or "")
    if (
        watermark_ordno
        and watermark_ts is not None
        and watermark_ts >= window_start
    ):
        if watermark_ordno not in by_odno:
            problems.append(
                f"워터마크 주문 {mask_odno(watermark_ordno)} 이 이력에 없습니다 "
                "(이력 누락/정착 지연)"
            )
        else:
            row_ts = _parse_iso(by_odno[watermark_ordno].get("ord_datetime_utc"))
            if row_ts is not None and row_ts.astimezone(timezone.utc) < watermark_ts:
                problems.append(
                    f"워터마크 주문 {mask_odno(watermark_ordno)} 의 시각이 state 보다 이전입니다 "
                    f"(history={row_ts.isoformat()}, watermark={state.get('last_updated')})"
                )

    for odno, meta in (state.get("orders_meta") or {}).items():
        if str(odno) in by_odno:
            continue
        submitted_ts = _meta_order_utc(meta)
        if submitted_ts is None:
            continue  # 제출 시각 없음 — 판단 불가 (한계로 문서화)
        if submitted_ts >= window_start:
            problems.append(
                f"orders_meta 주문 {mask_odno(odno)} 이 이력에 없습니다 "
                f"(submitted={submitted_ts.isoformat()})"
            )
    return problems


def _verify_history_settled(broker, state, symbol, exchange, history, history_days):
    """지연 이력(메리츠 등) 정착성 확인.

    검사: (1) 반복 조회 동일성, (2) 워터마크/orders_meta 포함 완전성,
    (3) 세션 종료 후 최근 미종결 주문 잔존. 하나라도 실패하면 fail-closed 로
    정산 저장/주문을 보류합니다.

    한계: 브로커가 조회 구간 밖 주문을 반환하지 않는 것은 정상이며, 제출 시각이
    없는 레거시 orders_meta 는 완전성 검사에서 제외됩니다. 또한 휴장일/세션
    경계는 ET 16:00 기준으로 근사합니다 (조기폐장 미반영).
    """
    if not _history_settledness_enabled():
        return True, ""
    delay = config.read_history_settledness_delay_seconds()
    if delay > 0:
        _tb().time.sleep(delay)
    try:
        repeated = broker.get_order_history(
            symbol,
            exchange,
            days=history_days,
            verbose=False,
        )
    except Exception as error:
        # 브로커 사유는 자유 텍스트라 주문번호가 섞일 수 있습니다 (라벨 참조만 마스킹).
        return False, f"이력 재조회 실패: {_tb()._mask_odno_references(str(error))}"
    if _history_snapshot(repeated) != _history_snapshot(history):
        return False, "이력 반복 조회 결과가 달라졌습니다 (정착 지연 의심)"
    completeness = _verify_history_complete(state, repeated, history_days)
    if completeness:
        return False, " / ".join(completeness[:3])
    if _history_has_recent_open_order(repeated):
        return False, "세션 종료 후 최근 미종결 주문이 이력에 남아 있습니다"
    return True, ""


def _settle_positions(broker, symbol_config, *, persist=True, now=None):
    """체결을 정산하고 T/순투입/원장을 갱신합니다.

    LIVE 이고 persist=True 이면 **마지막에 1회만** 상태를 저장합니다.
    DRY 는 메모리에서만 계산합니다.
    """
    tb = _tb()
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    splits = symbol_config["splits"]
    symbol_type = symbol_config["symbol_type"]
    seed = symbol_config["seed"]
    additional_loc_levels = symbol_config.get("additional_loc_levels", 3)

    # FORCE_T 는 운영자가 이력을 확인한 뒤 쓰는 **수동 보정**이며, REPAIR 단독
    # 경로(`trading_bot._repair_force_t()`)에서만 허용합니다. SETTLE 파이프라인에서
    # 적용하면 남아 있는 env var 가 매 SETTLE 마다 반복 적용되어 T 가 계속
    # 덮어써지므로, 상태/브로커/근거에 손대기 전에 fail-closed 로 거부합니다.
    if symbol_config.get("force_t") is not None:
        raise RuntimeError(
            f"{symbol} {symbol}_FORCE_T 는 SETTLE 단계에서 적용할 수 없습니다. "
            "TRADE_MODE=REPAIR REPAIR_ACTION=FORCE_T 로 단독 실행하세요 "
            "(검증 후 1회만 저장됩니다)."
        )

    settlement_run_id = _resolve_settlement_run_id()
    # 정산 대상(이미 마감된 세션)과 실행 대상(다음 세션)을 분리합니다.
    # `_et_today()` 는 KST 아침 실행에서 ET 기준 하루 이르므로 세션 비교에 부적합합니다.
    settled_session, execution_session = settlement_sessions(now)
    session = execution_session or _et_today(now)

    if not settled_session:
        print(
            f"  📅 {symbol} 정산 대상 세션을 계산할 수 없습니다 (XNYS 캘린더 범위). "
            "정산을 보류합니다."
        )
        return SettledState(
            state={},
            symbol=symbol,
            exchange=exchange,
            splits=splits,
            symbol_type=symbol_type,
            seed=seed,
            effective_seed=0.0,
            T=0.0,
            additional_loc_levels=additional_loc_levels,
            settlement_run_id=settlement_run_id,
            session=session,
            settled_session="",
            halt_reason="session_calendar_unavailable",
        )

    settlement_evidence = None
    evidence_path = None
    cycle_report = None
    next_cycle_seed = None
    # 애프터아워 게이트가 Step 1(이력 조회) 이전에 보류할 수 있으므로 미리 둡니다.
    order_history = []

    def _settled(halt_reason=None):
        return SettledState(
            state=state,
            symbol=symbol,
            exchange=exchange,
            splits=splits,
            symbol_type=symbol_type,
            seed=seed,
            effective_seed=float(state.get("effective_seed", 0.0) or 0.0),
            T=float(state.get("T", 0.0) or 0.0),
            reverse_mode=deepcopy(state.get("reverse_mode", {}) or {}),
            close_prices=list(state.get("close_prices", []) or []),
            position_basis=state.get("position_qty"),
            evidence=settlement_evidence,
            evidence_path=evidence_path,
            cycle_report=cycle_report,
            next_cycle_seed=next_cycle_seed,
            halt_reason=halt_reason,
            order_history=order_history,
            additional_loc_levels=additional_loc_levels,
            settlement_run_id=settlement_run_id,
            evidence_id=settlement_run_id,
            session=session,
            settled_session=settled_session,
        )

    def _persist():
        """검증이 끝난 정산 결과를 **마지막에 1회만** 저장합니다 (P1-D).

        - LIVE: 정산 결과 캐시 저장
        - DRY: 진단/프리뷰이므로 저장하지 않습니다 (FORCE_T 는 REPAIR 단독 경로).
        """
        if persist and tb.TRADE_MODE == "LIVE":
            tb.save_state(symbol, state)
            return True
        return False

    # ── Step 1: T값 로드 및 어제 체결 반영 ──────────────────────
    print("\n[Step 1] T값 로드 중...")

    state = tb.load_state(symbol)
    # 정산 대상/실행 대상 세션을 state 에 바인딩합니다. PREVIEW/APPROVE/EXECUTE 는
    # 이 값으로 세션을 검증하므로 KST 아침↔저녁 실행의 ET 날짜 차이를 흡수합니다.
    state["settled_session"] = settled_session
    state["execution_session"] = execution_session

    # ── 애프터아워 종료 게이트 (정규장 마감과 분리) ──────────────────
    # 정규장 마감(16:00 ET)만 보고 정산하면, KST 09:30(겨울 ET 19:30)처럼
    # 애프터아워(20:00 ET 종료)가 아직 진행 중일 때 시간외 체결을 확정할 수
    # 있습니다. 대상 세션의 애프터아워 창이 끝나기 전에는 저장/주문 없이
    # 보류합니다 (fail-closed).
    from broker.market_utils import after_hours_end_et_hour, us_session_after_hours_end

    if not us_session_after_hours_end(settled_session, now):
        print(
            f"  📅 {symbol} {settled_session} 애프터아워가 아직 종료되지 않았습니다 "
            f"(AFTER_HOURS_END_ET_HOUR={after_hours_end_et_hour()}). "
            "정산을 보류합니다."
        )
        return _settled("after_hours_not_ended")

    # _state_unavailable 플래그만 보관 — 즉시 차단하지 않고 아래에서
    # order_history/balance 조회 후 스마트 부트스트랩 여부를 판단합니다.
    state_unavailable = state.get("_state_unavailable")

    # 첫 실행 워터마크 오염 방지 안내: state 없음 + last_updated 없음 + LIVE면
    # 차단 없이 경고 1줄만 출력합니다 (notify 호출 금지, exit 금지).
    if state_unavailable and tb.TRADE_MODE == "LIVE" and not state.get("last_updated"):
        print("[권장] 첫 실행으로 보입니다. DRY 1회 실행 후 LIVE 전환을 권장합니다.")

    unresolved_zero_invested = (
        state.get("net_invested_status", "unresolved") != "valid"
        and float(state.get("net_invested", 0.0) or 0.0) <= 0
    )
    if unresolved_zero_invested and tb.TRADE_MODE == "LIVE":
        tb.notify(
            f"[상태] {symbol} net_invested 미확정(unresolved) 및 0 이하 → 신규 전략 주문을 보류합니다.\n"
            "STATE_REVERSE_AUDIT_ONLY/reconcile로 상태를 확인하세요."
        )

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
            start_dt = tb.datetime.strptime(cycle_start_date, "%Y-%m-%d")
            days_since_start = (tb.datetime.now() - start_dt).days + 5
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
        verbose=(tb.TRADE_MODE == "DRY" or tb.ORDER_HISTORY_VERBOSE),
    )

    # ── 진행 중(정규장) 세션 체결 차단 ──────────────────────────────
    # 정산 대상은 "직전 마감 세션"이지만, 장중에 SETTLE 을 수동 실행하면
    # 이력에 **진행 중 세션**의 부분 체결이 이미 섞여 있습니다. 그대로 확정하면
    # 진행 중 체결을 이전 세션 정산에 귀속시키게 되므로 저장/주문 없이 보류합니다.
    from broker.market_utils import us_session_in_progress

    in_progress_session = us_session_in_progress(now)
    if in_progress_session:
        in_progress_ymd = in_progress_session.replace("-", "")
        contaminated = [
            row
            for row in order_history
            if (_order_us_session_date(row) or "") >= in_progress_ymd
        ]
        if contaminated:
            print(
                f"[보류] {symbol} 진행 중 세션({in_progress_session}) 체결 "
                f"{len(contaminated)}건이 이력에 있습니다 — 해당 세션 체결을 "
                "직전 세션 정산에 확정하지 않습니다."
            )
            return _settled("in_progress_session_fills")

    # ── **실행 세션 이후** 체결 차단 (경계 정책) ─────────────────────
    # 정산 대상은 애프터아워까지 끝난 `settled_session` 하나뿐이지만, 주문 이력에는
    # 아직 마감되지 않은 **실행 세션(=다음 세션)** 의 프리마켓/장중 체결이 섞여 있을
    # 수 있습니다 (KST 저녁 실행, 수동 SETTLE, 다음 거래일 이른 시각).
    # 실행 세션 체결은 "미래 정산 대상"이므로 이번 SETTLE 에서 **미루고**(아래 필터),
    # 여기서는 실행 세션보다 **엄격히 뒤인** 세션 체결만 미래/범위 밖 데이터로 보고
    # 저장 없이 보류합니다. 해석 불가(None)인 행은 기존 정산/대조 경로에 맡깁니다.
    execution_ymd = _session_ymd(execution_session)
    if not execution_ymd:
        print(
            f"[보류] {symbol} 실행 대상 세션을 계산할 수 없어 정산 범위를 확정하지 "
            "못합니다 — 저장/주문하지 않습니다."
        )
        return _settled("session_calendar_unavailable")
    later_fills = []
    for row in order_history:
        if _row_filled_qty(row) <= 0:
            continue
        row_session = _session_ymd(_order_us_session_date(row))
        if row_session and row_session > execution_ymd:
            later_fills.append((row_session, row))
    if later_fills:
        sessions = sorted({ymd for ymd, _row in later_fills})
        print(
            f"[보류] {symbol} 실행 세션({execution_session}) **이후** 세션 체결 "
            f"{len(later_fills)}건이 이력에 있습니다 "
            f"(세션: {', '.join(sessions)}) — 범위 밖 미래 데이터이므로 "
            "이번 실행은 저장/주문하지 않습니다."
        )
        return _settled("post_settled_session_fills")

    # 지연 이력 정착성 확인 — 불안정하면 정산/저장을 보류합니다 (fail-closed).
    history_ok, history_reason = _verify_history_settled(
        broker, state, symbol, exchange, order_history, history_days
    )
    if not history_ok:
        print(f"[보류] {symbol} 주문 이력이 아직 정착되지 않았습니다: {history_reason}")
        return _settled("history_not_settled")

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
        if tb.LS_DEMO_BYPASS_BUGS:
            print(f"[상태 검증] 브로커 잔고 조회 실패(T 갱신용) - LS 모의투자 버그 우회: {e}")
        else:
            print(f"[상태 검증] 브로커 잔고 조회 실패(T 갱신용): {e}")
        live_balance = None
        live_qty = None
        live_avg = None

    # ── 실잔고 조회 실패 → LIVE 정산 보류 (fail-closed) ──
    # 보유수량 없이 정산하면 매도 비율/T 를 확정할 수 없고, 사이클 종료·잔고
    # 교차검증(아래 `if live_qty is not None`)도 통째로 건너뜁니다. 그래서 LIVE 는
    # 정산을 저장하지 않고 보류합니다 (DRY 는 진단 모드라 계속 진행).
    if live_qty is None and tb.TRADE_MODE != "DRY":
        print(
            f"[보류] {symbol} 브로커 잔고를 조회하지 못해 정산을 보류합니다 "
            "(실잔고 확인 없이 T/순투입을 확정하지 않습니다)."
        )
        tb.notify(
            f"[보류] {symbol} 잔고 조회 실패로 SETTLE 를 보류했습니다. "
            "브로커 API 상태를 확인한 뒤 재실행하세요.",
            urgent=True,
        )
        return _settled("balance_unavailable")

    # ── 실행 세션 체결은 **다음 정산으로 미룹니다** (워터마크 규율) ──
    # 계약: SETTLE 은 `settled_session` 까지만 확정합니다. 세션 날짜가 그 이후인
    # 체결(대표적으로 실행 세션의 프리마켓 체결)은 이번 실행의 T/순투입/원장 계산과
    # 근거(evidence)에서 **제외**합니다. 이력 자체를 잘라 넘기므로 워터마크
    # (last_updated/last_processed_ordno)도 그 체결 앞에서만 전진하고, 체결은
    # **해당 세션의 이후 SETTLE** 에서 정상적으로 다시 조회·반영됩니다 (누락 없음).
    # 잔고는 이미 실행 세션 체결을 포함하므로, 미룬 순변동만큼 되감아
    # "정산 세션 시점" 잔고로 맞춥니다 (매도 비율 기준선/잔고 교차검증 일관성).
    scoped_history, deferred_fills, deferred_net_qty = _split_settlement_session_fills(
        order_history, settled_session
    )
    if deferred_fills:
        print(
            f"  📅 {symbol} 실행 세션({execution_session}) 체결 "
            f"{len(deferred_fills)}건은 이번 정산에서 제외하고 다음 정산으로 "
            f"미룹니다 (순변동 {deferred_net_qty:+d}주, 워터마크 유지)."
        )
        if live_qty is not None:
            live_qty = int(live_qty) - deferred_net_qty
        order_history = scoped_history

    # ── 첫 실행 부트스트랩 (state 없음 + 보유 0 + 이력 0) ──
    # state 파일이 없/손상/미등록이어도, 실제 보유와 이력이 모두 비어 있으면
    # 첫 실행으로 간주해 T=0 LIVE 진행을 허용합니다. 그 외는 기존처럼 차단합니다.
    if tb.TRADE_MODE == "LIVE" and state_unavailable:
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
    settlement_evidence = tb.build_settlement_evidence(
        symbol,
        state,
        order_history,
        mode=tb.TRADE_MODE,
        run_id=settlement_run_id,
        balance_qty=live_qty,
        before=before_snapshot,
        max_t=max_t,
    )
    # SETTLE 이 실행 세션으로 미룬 체결을 근거에 남깁니다 (P2). `balance_qty` 는
    # 이미 미룬 순변동을 되감은 값이므로, VERIFY 는 이 목록을 live 이력과 대조한
    # 뒤 기대 잔고에 순변동을 더해야 합니다. 목록이 없는 구버전 근거는 기존의
    # 엄격한 동작을 그대로 유지합니다.
    settlement_evidence["deferred_fills"] = _deferred_fills_evidence(deferred_fills)

    hold = bool(state.get("_pending_amount_odnos") or state.get("_settlement_hold"))
    # 저장 **전** 독립 재계산 + 잔고 교차검증을 통과해야 합니다.
    evidence_problems = [] if hold else _verify_settlement_evidence(settlement_evidence)
    if evidence_problems:
        raise tb.SymbolScopedError(
            f"{symbol} 정산 근거 독립 검증 실패 — 저장/주문을 보류합니다: "
            + " / ".join(evidence_problems[:3])
        )
    # 근거 파일은 먼저 committed=false 로 남기고, state 저장이 성공한 뒤에만
    # committed=true 로 승격합니다. 실패/보류/DRY 근거는 VERIFY/PREVIEW 에서
    # 사용할 수 없습니다 (P1-E).
    try:
        evidence_path = _write_settlement_evidence(
            symbol,
            settlement_evidence,
            committed=False,
            failure_reason="settlement_hold" if hold else "",
        )
    except OSError as evidence_error:
        raise tb.SymbolScopedError(
            f"{symbol} 정산 근거 기록 실패 — 저장/주문을 보류합니다: "
            f"{str(evidence_error)[:160]}"
        ) from evidence_error

    # 금액 미확정/정산 근거 부족은 evidence 를 남긴 뒤 **즉시** 저장·주문을 보류합니다.
    # DRY 는 저장/주문이 없는 진단 모드이므로, 보유수량 기준이 확보된 경우에는
    # 경고만 남기고 프리뷰를 계속합니다 (AGENTS: DRY 불일치/보류는 기록·중단 없음).
    # LIVE 는 기존대로 fail-closed 로 저장과 주문을 모두 보류합니다.
    if state.get("_pending_amount_odnos") or state.get("_settlement_hold"):
        if tb.TRADE_MODE == "DRY" and not state.get("_position_basis_unknown"):
            reason = state.get("_settlement_hold_reason", "체결/정산 미확정")
            warning = (
                f"[경고] {symbol} {reason} — DRY 모드라 기록/중단 없이 "
                "프리뷰를 계속합니다 (LIVE 는 보류)."
            )
            print(warning)
            tb.notify(warning)
            state.pop("_pending_amount_odnos", None)
            state.pop("_pending_marker_odnos", None)
            state.pop("_settlement_hold", None)
            state.pop("_settlement_hold_reason", None)
        else:
            tb._save_settlement_state(symbol, state)

    # ── 포지션 기반 T 추정 진단 (소액 시드 오추정 시 참고용) ──
    infer_diag = state.get("_inference_diagnostic", {})
    if infer_diag.get("small_seed_days", 0) > 0 and live_qty is not None and live_avg is not None and live_qty > 0 and live_avg > 0:
        _used_seed = state.get("effective_seed", 0.0) or seed
        if _used_seed > 0:
            T_position = round((live_qty * live_avg * splits) / _used_seed, 4)
            T_position = min(T_position, max_t if max_t is not None else splits)
            print(f"  → [참고] 포지션 기반 T 추정: {T_position}")
            print(f"  → 포지션 기반 값으로 보정하려면 {symbol}_FORCE_T={T_position} 환경변수를 설정하고 TRADE_MODE=REPAIR REPAIR_ACTION=FORCE_T 로 단독 실행하세요")

    # ── 후처리 기록 (evidence 는 이 시점의 정산 결과를 담고 있습니다) ──
    # MAX_T/사이클 종료는 evidence 생성 **이후** state 를 바꿉니다.
    # 그 변경을 따로 기록해, 저장 직전에 "최종 후보"를 근거와 독립 검증합니다.
    post_processing = {"applied": False, "steps": []}

    def _post_step(name, **info):
        post_processing["applied"] = True
        post_processing["steps"].append({"step": name, **info})

    # ── T값 상한 적용 (MAX_T) ──
    if max_t is not None and (_force_reinference or _trade_mode == "DRY"):
        # 미리보기 진단: 원시 추정 T 와 상한 적용 후 T 를 함께 보여줍니다.
        print(f"[T 보정] {symbol} 원시 추정 T={state['T']}, 상한 MAX_T={max_t} → 적용 T={min(state['T'], max_t)}")
    if max_t is not None and state["T"] > max_t:
        print(f"[T 보정] {symbol} T={state['T']} > MAX_T={max_t} → T={max_t}로 조정")
        _post_step("max_t", before=float(state["T"]), after=float(max_t))
        state["T"] = max_t
        reset_settlement_ledger(state, max_t)

    # ── 사이클 종료 감지 (전체 재추정 경로) ──
    # _infer_T_from_full_history가 전량매도를 감지해 T=0으로 리셋한 경우,
    # 일반 경로(T > 0 조건)는 작동하지 않으므로 여기서 따로 처리합니다.
    completed_cycle_start = state.pop("_completed_cycle_start", None)
    if completed_cycle_start:
        if tb._has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
            return _settled("unresolved_reverse_orders")
        print(f"\n{'=' * 60}")
        print(f"[사이클 종료] {symbol} — {completed_cycle_start} ~ 완료")
        print(f"{'=' * 60}")
        _used_seed = state.get("effective_seed", 0.0)
        if not (tb.REINVEST and _used_seed > 0):
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
            finalized = tb.finalize_completed_cycle(
                symbol=symbol,
                state=state,
                order_history=order_history,
                seed=_used_seed,
                commission_rate=tb.COMMISSION_RATE,
                report={"cycle_start_date": completed_cycle_start},
                require_settled=False,
            )
        else:
            # 정산은 이미 완료된 상태(T=0/net=0)입니다. 원장만 0 기준으로 확정한 뒤
            # **후처리(리포트/시드)** 만 수행합니다.
            state["cycle_start_date"] = ""
            reset_settlement_ledger(state, 0.0)
            finalized = tb.finalize_completed_cycle(
                symbol=symbol,
                state=state,
                order_history=order_history,
                seed=_used_seed,
                commission_rate=tb.COMMISSION_RATE,
                report={"cycle_start_date": completed_cycle_start},
            )
        report = finalized["report"]
        cycle_report = report
        next_cycle_seed = finalized.get("next_cycle_seed")
        _post_step(
            "cycle_end",
            cycle_start=completed_cycle_start,
            cycle_end=report.get("cycle_end_date", ""),
            next_cycle_seed=finalized.get("next_cycle_seed"),
            seed=float(_used_seed),
            commission_rate=float(tb.COMMISSION_RATE),
            new_cycle_started=bool(new_cycle_started),
            reinvest=bool(tb.REINVEST),
        )
        report_message = tb.format_cycle_report_message(report)
        print(f"\n{report_message}")
        tb.notify(report_message)
        # effective_seed 갱신은 리포트 후 **단 1회**.
        if tb.REINVEST and finalized["next_cycle_seed"] > 0:
            state["effective_seed"] = round(finalized["next_cycle_seed"], 2)
            print(f"  복리 재투자: 다음 사이클 시드 = ${state['effective_seed']:.2f}")
        else:
            state["effective_seed"] = 0.0

    # ── 정산 결과 저장 ──────────────────────────────────────────
    # 보유수량 기준을 확보하지 못한 정산은 **저장하지 않고** 신규 주문도 내지 않습니다.
    # (먼저 저장하면 다음 실행에서 재처리될 수 없어 잘못된 값이 고정됩니다)
    tb._save_settlement_state(symbol, state)

    # 저장은 settle_positions() 종료 시 1회만 수행합니다 (LIVE).

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
            if tb._has_unresolved_reverse_orders(state):
                state.setdefault("reverse_mode", {})["reconciliation_only"] = True
                state["reverse_mode"]["active"] = False
                return _settled("unresolved_reverse_orders")
            msg = (
                f"[불일치] 이력으로는 보유 {comp_qty}주(평단 ${comp_avg:.2f})로 추정되나, 브로커 잔고는 0입니다."
            )
            if tb.TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 기록·자동보정·중단 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} DRY 모드라 기록/보정 없이 프리뷰를 계속합니다. (LIVE 시 자동 보정 후 중단)")
                tb.notify(msg)
            else:
                print(f"{msg} 보수적 자동 보정: T=0으로 초기화합니다.")
                tb.notify(msg)
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
                raise RuntimeError(
                    f"{symbol} 이력 포지션과 브로커 잔고가 불일치하여 주문을 중단합니다. "
                    f"history={comp_qty}, broker={live_qty}"
                )

        elif live_qty != comp_qty:
            # 불일치지만 자동 보정하지 않음 — 관리자 확인 필요
            msg = (
                f"[불일치] 이력 net_qty={comp_qty}, 브로커 잔고={live_qty}. 자동 보정하지 않습니다. 확인 필요."
            )
            if tb.TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 상태 기록 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} (DRY 모드라 balance_mismatch를 기록하지 않습니다)")
                tb.notify(msg)
            else:
                print(msg)
                tb.notify(msg)
                state["balance_mismatch"] = {
                    "computed_net_qty": comp_qty,
                    "computed_avg_price": round(comp_avg, 2),
                    "live_qty": live_qty,
                    "live_avg_price": round(live_avg, 2) if live_avg is not None else None,
                    "note": "requires-attention",
                }
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
            if tb.TRADE_MODE == "DRY":
                # DRY는 프리뷰/진단 모드 — 상태 기록 없이 경고만 남기고 계속 진행합니다.
                print(f"{msg} (DRY 모드라 balance_mismatch를 기록하지 않습니다)")
                tb.notify(msg)
            else:
                print(msg)
                tb.notify(msg)
                state["balance_mismatch"] = {
                    "computed_net_qty": comp_qty,
                    "computed_avg_price": round(comp_avg, 2),
                    "live_qty": live_qty,
                    "live_avg_price": round(live_avg, 2) if live_avg is not None else None,
                    "note": "T-estimation-suspected-low",
                }
                raise RuntimeError(
                    f"{symbol} T=0인데 브로커 잔고 {live_qty}주가 있어 주문을 중단합니다."
                )

        else:
            # 일치하는 경우, 기존 불일치 표시 제거
            if state.get("balance_mismatch"):
                state.pop("balance_mismatch", None)

    # ── 주문 fence 복구 (이전 세션 이력 정착 시 자동 해제) ──
    # 주문이력/잔고 reconciliation이 정상 통과한 뒤에만 도달합니다.
    # fence가 있어도 즉시 차단하지 않고, 전일(이전 세션)이면 자동 해제 후 진행합니다.
    if tb.TRADE_MODE == "LIVE":
        tb._recover_order_fence(state, symbol, live_qty, persist=False)

    # ── T값 강제 설정(FORCE_T)은 이 경로에서 금지 ──
    # REPAIR 단독 경로(`trading_bot._repair_force_t()`)만 FORCE_T 를 적용합니다.
    # 위쪽 진입부에서 force_t 가 있으면 SETTLE 를 거부하므로 여기서는 아무것도
    # 하지 않습니다 (잔여 env var 반복 적용 방지).

    T = state["T"]
    print(f"  현재 T값: {T}")

    # ── 사이클 종료 사전 감지 (전략 실행 전에) ─────────────────────────
    # live 잔고 우선, 없으면 이력 기반 추정값을 사용합니다.
    # 잔고 조회 실패(None) 시에는 종료 감지를 보류합니다 (SOXL 케이스).
    current_qty = live_qty if live_qty is not None else comp_qty

    if T > 0 and current_qty == 0 and live_qty is None:
        # 잔고 조회 실패 → T 유지 (알 수 없음, API 장애 등)
        print(f"\n[경고] {symbol} 사이클 종료 감지 보류 — 브로커 잔고 조회 실패 (T={T} 유지, 보유수량 알 수 없음)")
        tb.notify(f"[경고] {symbol} 잔고 조회 실패로 사이클 종료 감지 보류. T={T} 유지. 확인 필요.")

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
        if not (tb.REINVEST and _used_seed > 0):
            _used_seed = seed
        _cycle_start = state.get("cycle_start_date", "")
        _cycle_orders, _liquidation_event = tb._liquidation_boundary_orders(
            order_history, _cycle_start
        )
        _cycle_end = tb._cycle_end_date_from_history(order_history, _cycle_start)
        report = tb.generate_cycle_report(
            symbol=symbol,
            order_history=order_history,
            cycle_start_date=_cycle_start,
            cycle_end_date=_cycle_end,
            seed=_used_seed,
            commission_rate=tb.COMMISSION_RATE,
            cycle_orders=_cycle_orders,
            liquidation_event=_liquidation_event,
        )
        cycle_report = report
        next_cycle_seed = report.get("next_cycle_seed")
        _post_step(
            "cycle_end",
            cycle_start=_cycle_start,
            cycle_end=report.get("cycle_end_date", ""),
            next_cycle_seed=next_cycle_seed,
            seed=float(_used_seed),
            commission_rate=float(tb.COMMISSION_RATE),
            new_cycle_started=False,
            reinvest=bool(tb.REINVEST),
            # DRY 는 캐시 저장을 생략하며 effective_seed 를 0으로만 계산합니다.
            dry=bool(tb.TRADE_MODE == "DRY"),
        )

        report_message = tb.format_cycle_report_message(report)
        print(f"\n{report_message}")
        tb.notify(report_message)

        if tb.TRADE_MODE == "DRY":
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
            if tb.REINVEST and report["next_cycle_seed"] is not None:
                state["effective_seed"] = round(report["next_cycle_seed"], 2)
                print(f"  복리 재투자: 다음 사이클 시드 = ${state['effective_seed']:.2f}")
            else:
                state["effective_seed"] = 0.0

            if tb._has_unresolved_reverse_orders(state):
                state.setdefault("reverse_mode", {})["reconciliation_only"] = True
                state["reverse_mode"]["active"] = False
                return _settled("unresolved_reverse_orders")

            # T 초기화 및 사이클 시작일 리셋
            state["T"] = 0.0
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, 0.0)

            print("  T값 초기화 완료. 새 사이클을 즉시 시작합니다.")
            T = 0.0
            # ── Step 2로 계속 진행 (새 사이클 즉시 시작) ──

    if current_qty == 0 and state.get("reverse_mode", {}).get("active"):
        if tb._has_unresolved_reverse_orders(state):
            state["reverse_mode"]["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
        else:
            state["reverse_mode"] = {}

    # ── seed 적용 (복리 재투자) ────────────────────────────────
    if tb.REINVEST:
        effective_seed = state.get("effective_seed", 0.0)
        if effective_seed > 0:
            seed = effective_seed
            print(f"  복리 재투자 적용: 이번 사이클 시드 = ${seed:.2f}")

    # ── 정산 결과 저장 (검증이 모두 끝난 뒤 1회) ────────────────────
    # state 에 마지막 커밋 정산 id 를 함께 기록해야 PREVIEW/APPROVE 가
    # "이번 SETTLE" 에만 묶입니다 (P1-E).
    live_persist = bool(persist and tb.TRADE_MODE == "LIVE")
    will_persist = live_persist
    if live_persist:
        state["last_settlement_id"] = settlement_run_id
        state["last_settlement_evidence"] = evidence_path
        state["last_settlement_committed_at_utc"] = _utc_now_iso()

    # ── 최종 저장 후보 검증 (evidence-vs-final) ─────────────────────
    # 후처리(MAX_T/사이클 종료)로 evidence 의 T_after/net_invested_after 와
    # 저장값이 달라질 수 있습니다. 그 변경을 근거에 기록하고, **저장 직전** 최종
    # 후보를 ops/verify_run.py 로 독립 검증합니다. 실패하면 저장/승격하지 않습니다.
    post_processing["final_candidate"] = {
        "T": float(state.get("T", 0.0) or 0.0),
        "net_invested": float(state.get("net_invested", 0.0) or 0.0),
        "effective_seed": float(state.get("effective_seed", 0.0) or 0.0),
        "cycle_start_date": state.get("cycle_start_date", ""),
    }
    settlement_evidence["post_processing"] = post_processing
    if will_persist:
        try:
            _mark_evidence_committed(
                evidence_path, settlement_evidence, False,
                "settlement_hold" if hold else "",
            )
        except OSError as evidence_error:
            raise tb.SymbolScopedError(
                f"{symbol} 정산 근거 후처리 기록 실패 — 저장/주문을 보류합니다: "
                f"{str(evidence_error)[:160]}"
            ) from evidence_error
        final_problems = _verify_settlement_final(evidence_path, settlement_evidence, state)
        if final_problems:
            raise tb.SymbolScopedError(
                f"{symbol} 최종 저장 후보가 정산 근거와 다릅니다 — 저장/주문을 보류합니다: "
                + " / ".join(final_problems[:3])
            )
    saved = _persist()
    if saved and live_persist:
        try:
            _mark_evidence_committed(evidence_path, settlement_evidence, True)
        except OSError as commit_error:
            # state 는 **이미 저장**됐습니다. 되돌리면 실제 거래 상태/워터마크가
            # 유실돼 더 위험하므로 state 는 그대로 둡니다.
            # 다만 근거 승격에 실패했다면 이 SETTLE 은 확정이 아닙니다:
            # - PREVIEW 의 settled_from_state() 는 committed 근거가 없어 거부(fail-closed)
            # - 상위 SETTLE 디스패처는 halt_reason 이 없으면 **성공으로 집계**하므로,
            #   경고만 남기고 성공으로 반환하면 "정산 완료" 오판이 됩니다.
            # 그래서 명시적 halt_reason 으로 보류/실패 집계되게 합니다 (리포트도
            # 보류 경로 — 성공 리포트 금지 + 사유 알림).
            print(
                f"[경고] {symbol} 정산 근거 committed 표시 실패 — "
                f"SETTLE 을 보류로 집계합니다 (PREVIEW/VERIFY 는 근거 부재로 거부): "
                f"{str(commit_error)[:160]}"
            )
            try:
                # 왜 커밋되지 않았는지 근거 파일에 남깁니다 (실패해도 무시).
                _mark_evidence_committed(
                    evidence_path,
                    settlement_evidence,
                    False,
                    HALT_EVIDENCE_COMMIT_FAILED,
                )
            except OSError:
                pass
            return _settled(HALT_EVIDENCE_COMMIT_FAILED)
    else:
        # DRY/미저장 실행: 근거를 사용할 수 없게 표시합니다.
        try:
            _mark_evidence_committed(
                evidence_path,
                settlement_evidence,
                False,
                "not_persisted",
            )
        except OSError:
            pass
    return _settled(None)


def settle_positions(broker, symbol_config, *, persist=True, now=None):
    """SETTLE 진입점 — 전역 실행 잠금 안에서 정산합니다.

    `.state.json` read-modify-write 를 프로세스 간 직렬화합니다 (셸 런처와
    같은 `.run.lock`). 직접 CLI 로 SETTLE/EXECUTE 를 동시에 돌려도 T/워터마크가
    유실되지 않습니다.
    """
    with global_lock():
        return _settle_positions(broker, symbol_config, persist=persist, now=now)


def settled_from_state(symbol_config, state=None, *, settlement_run_id=None):
    """저장된 state 로 PREVIEW/APPROVE/EXECUTE 재계산용 SettledState 를 만듭니다.

    PREVIEW/APPROVE 는 state 에 **마지막으로 커밋된** 정산 id 에만 묶입니다.
    커밋된 근거(evidence)가 없으면 fail-closed 로 거부합니다 (P1-E).
    """
    symbol = symbol_config["symbol"]
    state = state if state is not None else _tb().load_state(symbol)
    if state.get("_state_unavailable"):
        raise RuntimeError(
            f"{symbol} 상태를 확인할 수 없습니다: {state.get('_state_unavailable')}"
        )
    assert_no_pending_fence(state, symbol)
    committed_run_id = str(state.get("last_settlement_id") or "")
    if not committed_run_id:
        raise RuntimeError(
            f"{symbol} 커밋된 정산 근거가 없습니다 (state.last_settlement_id 없음) — "
            "SETTLE 을 먼저 실행하세요."
        )
    if settlement_run_id and str(settlement_run_id) != committed_run_id:
        raise RuntimeError(
            f"{symbol} 정산 run_id 불일치: 요청={settlement_run_id}, "
            f"state 마지막 커밋={committed_run_id} — SETTLE 후 다시 시도하세요."
        )
    if not evidence_file_for_run(symbol, committed_run_id):
        raise RuntimeError(
            f"{symbol} 커밋된 정산 근거 파일을 찾을 수 없습니다 "
            f"(run_id={committed_run_id}) — SETTLE 을 다시 실행하세요."
        )
    # 세션 분리: SETTLE 이 저장한 정산/실행 세션을 그대로 씁니다. 구버전 state
    # (필드 없음)는 같은 규칙(직전 마감 세션 → 다음 세션)으로 계산합니다.
    settled_session = str(state.get("settled_session") or "")
    execution_session = str(state.get("execution_session") or "")
    if not settled_session or not execution_session:
        settled_session, execution_session = settlement_sessions()
        if not settled_session or not execution_session:
            raise RuntimeError(
                f"{symbol} 정산/실행 세션을 확정할 수 없습니다 — SETTLE 을 다시 실행하세요."
            )
        print(
            f"[경고] {symbol} state 에 세션 정보가 없어 캘린더로 계산합니다 "
            f"(settled={settled_session}, execution={execution_session})."
        )
    return SettledState(
        state=state,
        symbol=symbol,
        exchange=symbol_config["exchange"],
        splits=symbol_config["splits"],
        symbol_type=symbol_config["symbol_type"],
        seed=float(symbol_config.get("seed", 0.0) or 0.0),
        effective_seed=float(state.get("effective_seed", 0.0) or 0.0),
        T=float(state.get("T", 0.0) or 0.0),
        reverse_mode=deepcopy(state.get("reverse_mode", {}) or {}),
        close_prices=list(state.get("close_prices", []) or []),
        position_basis=state.get("position_qty"),
        additional_loc_levels=symbol_config.get("additional_loc_levels", 3),
        settlement_run_id=committed_run_id,
        evidence_id=committed_run_id,
        evidence_path=evidence_file_for_run(symbol, committed_run_id),
        session=execution_session,
        settled_session=settled_session,
    )


def build_order_plan(settled, broker, *, net_invested_overlay=None):
    """정산 결과로 전략을 실행해 주문 계획을 만듭니다 (저장/주문 없음).

    `net_invested_overlay` 는 **EXECUTE 재계산 전용** 시드 캡 보정입니다.
    SETTLE 이 실행 세션으로 미룬 매수 금액을 전략의 `net_invested` 에만 더해
    달러 기준 시드 캡을 지키게 합니다. 원본 state/해시/저장값에는 반영하지
    않습니다 (다음 SETTLE 이 유일한 기록자).
    """
    tb = _tb()
    symbol = settled.symbol
    exchange = settled.exchange
    splits = settled.splits
    symbol_type = settled.symbol_type
    seed = settled.seed
    # 복리 재투자: SETTLE 이 확정한 effective_seed 를 PREVIEW/APPROVE 에서도 사용합니다.
    if tb.REINVEST and (settled.effective_seed or 0.0) > 0:
        seed = settled.effective_seed
    additional_loc_levels = settled.additional_loc_levels
    state = settled.state
    T = settled.T
    assert_no_pending_fence(state, symbol)
    # 전략이 state(close_prices/reverse_mode)를 바꾸기 **전** 지문을 사용합니다.
    state_hash_at_plan = canonical_state_hash(state)
    session = settled.session or settlement_sessions()[1] or _et_today()
    settlement_run_id = settled.settlement_run_id or _resolve_settlement_run_id()

    # ── Step 2: 전략 실행 ───────────────────────────────────────
    print("\n[Step 2] 전략 실행 중...")

    # 전략은 state 를 읽기만 하지만, 계획 단계가 정산 state 를 변형하면
    # state_hash 가 흔들립니다. 항상 복사본에서 실행하고 변경분은 state_patch 로만
    # 넘깁니다 (PREVIEW/APPROVE/EXECUTE 간 hash 일치 보장).
    strategy_state = deepcopy(state)
    if net_invested_overlay is not None:
        # 실행 직전 재계산에서만 적용하는 오버레이 — 원본 state/hash 는 그대로.
        strategy_state["net_invested"] = float(net_invested_overlay)
        print(
            f"  [EXECUTE] 시드 캡 보정: 미룬 매수 금액 반영 "
            f"net_invested ${float(state.get('net_invested', 0.0) or 0.0):.2f} → "
            f"${float(net_invested_overlay):.2f} (저장하지 않음)"
        )
    reverse_before = deepcopy(state.get("reverse_mode", {}) or {})

    strategy_result = tb.무한매수법_V4(
        broker,
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=seed,
        T=T,
        additional_loc_levels=additional_loc_levels,
        state=strategy_state,
        # 리버스모드 중복방지는 **벽시계(ET 오늘)가 아니라 실행 대상 세션**으로
        # 판정합니다. KST 아침 PREVIEW 시점의 ET 벽시계는 아직 전일이라, 벽시계로
        # 비교하면 진행 중인 리버스가 0건으로 조용히 건너뛰어집니다.
        session_date=session,
    )

    if strategy_result.get("reverse_exit") and tb.TRADE_MODE == "LIVE":
        if tb._has_unresolved_reverse_orders(strategy_state):
            strategy_state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            strategy_state["reverse_mode"]["active"] = False
        else:
            strategy_state["reverse_mode"] = {}

    last_price = strategy_result['last_price']
    close_prices = list(state.get("close_prices", []) or [])
    if last_price > 0:
        close_prices = [p for p in close_prices if isinstance(p, (int, float)) and p > 0]
        close_prices.append(round(last_price, 2))
    new_close_prices = close_prices[-5:]

    print("✓ 전략 실행 완료")
    print(f"  현재가: ${last_price}")
    print(f"  보유 수량: {strategy_result['position_qty']}주")
    print(f"  평단가: ${strategy_result['avg_price']}")
    print(f"  주문 가능 금액: ${strategy_result['orderable_cash']:.2f}")
    print(f"  T값: {T} / {splits}")
    if strategy_result['star_point']:
        print(f"  별지점: ${strategy_result['star_point']:.2f}")

    rev = strategy_state.get("reverse_mode", {})
    if rev.get("day_count", 0) > 0:
        print(f"  [리버스모드] {rev['day_count']}일차 진행 중")

    orders = strategy_result["orders"]

    # BUY LOC 1.19 상한 보정과 추가매수 계단 배치는 전략 단계에서 이미 반영되어 있습니다.
    # (trading_bot에서 다시 보정하면 추가매수 가격이 정상매수와 같아질 수 있습니다)
    for note in strategy_result.get("price_cap_notes") or []:
        tb.notify(
            f"{symbol} ⚠️ LOC 가격 보정\n원가격: ${note['from']:.2f}\n"
            f"현재가 대비 상한: ${note['to']:.2f}"
        )

    print(f"\n[Step 3] 생성된 주문 목록 ({len(orders)}개)")
    print("-" * 60)

    if len(orders) == 0:
        print("생성된 주문이 없습니다.")

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

    session = settled.session or settlement_sessions()[1] or _et_today()
    plan = OrderPlan(
        plan_id="",
        symbol=symbol,
        exchange=exchange,
        session=session,
        orders=orders,
        state_hash=state_hash_at_plan,
        plan_hash="",
        settlement_run_id=settlement_run_id,
        created_at_utc="",
        expires_at_utc="",
        evidence_id=settled.evidence_id or settlement_run_id,
        state_patch={
            "close_prices": new_close_prices,
        },
        strategy_result=strategy_result,
    )
    reverse_after = strategy_state.get("reverse_mode", {}) or {}
    if reverse_after != reverse_before:
        plan.state_patch["reverse_mode"] = deepcopy(reverse_after)
    _prepare_plan_identity(plan)
    return plan
