"""
  - SETTLE / PREVIEW / APPROVE .

  ** API  **. //   ,
   `pipeline_execute.py`   .

:
    settle_positions()      * T//  (LIVE 1 )
    build_order_plan()        (OrderPlan)  ( )
    approve_order_plan()   approvals/{plan_id}.json   
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
except ImportError:  # pragma: no cover - POSIX   
    fcntl = None

from state import (
    _fill_amount,
    _has_unconfirmed_amount_fills,
    _order_us_session_date,
    _parse_processed_qty_marker,
    canonical_state_hash,
    compute_position_from_history,
    reset_settlement_ledger,
    update_T_from_history,
)

APPROVALS_DIR = os.getenv("APPROVALS_DIR", "approvals")
PLAN_DIR = os.getenv("PLAN_DIR", "plans")
# v2: created_at_utc/expires_at_utc  plan_hash  , account_id 
#       HMAC-SHA256  +   .
#     v1   . (P2-H / P2-D)
# v3:    (tolerance_policy) plan_hash  .
#     v2/v1  (PREVIEWAPPROVE) .
APPROVAL_SCHEMA_VERSION = 3
PLAN_SCHEMA_VERSION = 3
DEFAULT_APPROVAL_TTL_SECONDS = int(os.getenv("APPROVAL_TTL_SECONDS", "32400"))
DEFAULT_MIN_TTL_SECONDS = int(os.getenv("APPROVAL_MIN_TTL_SECONDS", "28800"))


def _tb():
    """trading_bot   import  ( monkeypatch )."""
    import trading_bot

    return trading_bot


def _et_today(now=None):
    """(ET)   . aware datetime  ET    .

    `now`  UTC aware    ( /)  date() 
     UTC ~ET        .
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
    """(  ,   ) .

    - `settled_session`   : SETTLE   ** **   
    - `execution_session` :    = PREVIEW/APPROVE/EXECUTE   

    KST  SETTLE/PREVIEW   ET    `_et_today()` 
     .  `_et_today()`   18:30 EXECUTE  ,
    //  XNYS   .

     `settled_session`  ** **   .
       `us_session_after_hours_end()`  SETTLE  
     (KST 09:30 =  ET 19:30     ).
     ** **      (last-closed ).

    Returns:
        tuple[str, str]: (settled_session, execution_session).  
         ("", "") -  /  .
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
    """  . PREVIEW/EXECUTE    ."""

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
    # SETTLE  ( ) . `session`   () .
    settled_session: str = ""
    evidence_id: str = ""
    evidence_path: str = ""


def compare_plan_orders(
    approved_orders, rebuilt_orders, policy=None,
):
    """승인 계획과 현재 재계산 계획을 **VERIFY 와 같은 기준**으로 비교합니다.

    비교 규칙은 `pipeline.compare_plan_orders()` / `compare_state_patches()` 한
    곳에 있습니다 (승인 → 실행 → 사후 검증이 어긋나지 않게 하기 위함).
      - 수량·가격·금액: 승인 시점 정책 허용오차 안에서만 허용 (정상 드리프트)
      - side/order_type/comment, t_target: 정확 일치
      - 리버스 필드(reverse_action/day/base_t/factor/t_target): **허용오차 없음**
        — 시세 드리프트로 리버스 단계/배율이 바뀌면 T·정산이 승인 범위를 벗어납니다
      - state_patch: `reverse_mode` 는 정확 일치, `close_prices` 는 이력 정확 +
        마지막 현재가만 가격 허용오차

    `policy` 는 **승인 파일에 고정된** 허용오차 정책입니다. EXECUTE 경로는
    반드시 승인 정책(env 로 더 좁힌 유효 정책)을 넘겨야 하며, 없으면
    fail-closed 로 비교하지 않습니다.

    `approved_patch`/`rebuilt_patch` 는 각 계획의 `state_patch` 입니다.
    `None` 은 "바인딩된 상태 변경 없음"과 같은 의미입니다 (`{}` 로 정규화).
    """
    if policy is None:
        return [
            "승인 파일에 허용오차 정책이 없어 주문 계획을 비교할 수 없습니다 "
            "(재승인 필요)"
        ]
    problems = []
    approved_orders = list(approved_orders or [])
    rebuilt_orders = list(rebuilt_orders or [])
    if len(approved_orders) != len(rebuilt_orders):
        problems.append(
            f"주문 개수 불일치: 승인 {len(approved_orders)} vs 재계산 {len(rebuilt_orders)}"
        )
        return problems
    qty_tol = int(policy.get("qty", 0))
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
        tolerance = float(policy.get("price_abs", 0.0)) + abs(float(a_price or 0.0)) * float(policy.get("price_pct", 0.0))
        if abs(a_price - r_price) > tolerance:
            problems.append(
                f"주문 {index} 가격 불일치: 승인=${a_price:.4f}, "
                f"재계산=${r_price:.4f} (허용 ±${tolerance:.4f})"
            )
        a_amount = a_price * a_qty
        r_amount = r_price * r_qty
        amount_tolerance = max(
            float(policy.get("price_abs", 0.0)) * max(a_qty, 1), 0.01
        )
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


def compare_state_patches(approved_patch, rebuilt_patch, policy=None):
    """승인 상태 패치와 현재 재계산 상태 패치를 비교합니다.

    `policy` 는 **승인 파일에 고정된** 허용오차 정책입니다. EXECUTE 경로는
    반드시 승인 정책(env 로 더 좁힌 유효 정책)을 넘겨야 하며, 없으면
    fail-closed 로 비교하지 않습니다.

    `approved_patch`/`rebuilt_patch` 는 각 계획의 `state_patch` 입니다.
    `None` 은 "바인딩된 상태 변경 없음"과 같은 의미입니다 (`{}` 로 정규화).
    """
    if policy is None:
        return [
            "승인 파일에 허용오차 정책이 없어 주문 계획을 비교할 수 없습니다 "
            "(재승인 필요)"
        ]
    problems = []
    approved_patch = dict(approved_patch or {})
    rebuilt_patch = dict(rebuilt_patch or {})
    for key in set(list(approved_patch.keys()) + list(rebuilt_patch.keys())):
        approved_value = approved_patch.get(key)
        rebuilt_value = rebuilt_patch.get(key)
        if approved_value is None and rebuilt_value is None:
            continue
        if approved_value is None or rebuilt_value is None:
            problems.append(
                f"state_patch {key} 존재 불일치: "
                f"승인={approved_value!r}, 재계산={rebuilt_value!r}"
            )
            continue
        if isinstance(approved_value, dict) and isinstance(rebuilt_value, dict):
            # reverse_mode 는 정확 일치
            if key == "reverse_mode":
                if approved_value != rebuilt_value:
                    problems.append(
                        f"state_patch reverse_mode 불일치: "
                        f"승인={approved_value!r}, 재계산={rebuilt_value!r}"
                    )
            continue
        # close_prices: 이력은 정확 일치, 마지막 현재가만 가격 허용오차
        if key == "close_prices":
            approved_list = list(approved_value or [])
            rebuilt_list = list(rebuilt_value or [])
            if len(approved_list) != len(rebuilt_list):
                problems.append(
                    f"state_patch close_prices 길이 불일치: 승인={len(approved_list)}개 vs "
                    f"재계산={len(rebuilt_list)}개"
                )
                continue
            if not approved_list:
                continue
            if not strict_equal(approved_list[:-1], rebuilt_list[:-1]):
                problems.append(
                    f"state_patch close_prices 이력 불일치: 승인={approved_list[:-1]} vs "
                    f"재계산={rebuilt_list[:-1]} "
                    "(state 가 잠겨 있으므로 이력은 드리프트 대상이 아닙니다)"
                )
            approved_last = _finite_number(approved_list[-1])
            rebuilt_last = _finite_number(rebuilt_list[-1])
            if approved_last is None or rebuilt_last is None:
                continue
            price_tolerance_val = float(policy.get("price_abs", 0.0)) + abs(
                float(approved_last or 0.0)
            ) * float(policy.get("price_pct", 0.0))
            if abs(float(approved_last or 0.0) - float(rebuilt_last or 0.0)) > price_tolerance_val:
                problems.append(
                    f"state_patch close_prices 현재가 불일치: 승인=${approved_last:.4f}, "
                    f"재계산=${rebuilt_last:.4f} (허용 ±${price_tolerance_val:.4f})"
                )
    return problems


def _finite_number(value, default=0.0):
    """비교 입력 숫자를 **유한한 실수**로 파싱합니다 (해석 불가/비유한이면 `None`).

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

    bool 은 값 타입까지 구분합니다 (`True` 와 `1` 은 다릅니다). 리스트/딕셔너리는
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


@dataclass
class OrderPlan:
    """PREVIEW    .     ."""

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
    #    id (    - P2-D)
    account_display: str = ""
    #      (plan_hash   - v3)
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
            #   (close_prices/reverse_mode) -   orders .
            "state_patch": deepcopy(self.state_patch),
            "consumed": False,
        }


#     (  ) 
# EXECUTE/VERIFY  ** env**   ,   env    
#     .  PREVIEW/APPROVE   
# /   plan_hash  .  env  ** **
#  ( PREVIEWAPPROVE  ).
TOLERANCE_PRICE_PCT_ENV = "EXECUTION_PRICE_TOLERANCE_PCT"
TOLERANCE_PRICE_ABS_ENV = "EXECUTION_PRICE_TOLERANCE_ABS"
TOLERANCE_QTY_ENV = "EXECUTION_QTY_TOLERANCE"
DEFAULT_TOLERANCE_PRICE_PCT = 0.005
DEFAULT_TOLERANCE_PRICE_ABS = 0.02
DEFAULT_TOLERANCE_QTY = 0


def _tolerance_float(source, name, default):
    try:
        value = float(source.get(name, default))
    except (TypeError, ValueError):
        return default
    # NaN/Infinity     .
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
    """ env      ."""
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
    """     (  None).

    NaN/Infinity  `x < 0`      
      .  bool(True/False) float  
       .
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
    """   env   ** **  .

    -     `None` (  fail-closed).
    -   min()  env      .
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
    """   (USD)."""
    return max(
        float(policy["price_abs"]), abs(float(price or 0.0)) * float(policy["price_pct"])
    )


def compute_plan_hash(
    plan_id, symbol, exchange, session, state_hash, settlement_run_id, orders,
    state_patch=None, evidence_id=None, broker=None, broker_mode=None,
    account_id=None, created_at_utc=None, expires_at_utc=None,
    account_display=None, tolerance_policy=None,
):
    """/    +     (SHA-256).

    `created_at_utc`/`expires_at_utc`  .   
      plan_hash      (P2-H).
    `account_display`         .
    `tolerance_policy`      ,   env 
          (v3).
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
# DRY/   - LIVE     ( fail-closed ).
# :   ** **  ,   
#        ( )    .
_ACCOUNT_FP_DRY_KEY = b"autotrade-account-fingerprint-dry-only-v1"


def _account_fingerprint_secret():
    """    - LIVE   fail-closed.

      `ACCOUNT_FINGERPRINT_SECRET`    .
    DRY//REPAIR         
       (       -
    DRY  EXECUTE  ).
    """
    secret = os.getenv(ACCOUNT_FINGERPRINT_SECRET_ENV, "").strip()
    if secret:
        return secret.encode("utf-8")
    if str(getattr(_tb(), "TRADE_MODE", "")).upper() == "LIVE":
        raise RuntimeError(
            f"{ACCOUNT_FINGERPRINT_SECRET_ENV}        - "
            "LIVE         . "
            "   ."
        )
    return _ACCOUNT_FP_DRY_KEY


def _mask_account_id(value):
    """   id -      .

    (HMAC)       4 .
    (    -  )
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    return "****" + raw[-4:]


def _account_fingerprint(value):
    """  **HMAC-SHA256**   ( ).

    - : `ACCOUNT_FINGERPRINT_SECRET` ( , LIVE )
    -  sha256          .
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    digest = hmac.new(
        _account_fingerprint_secret(), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "hmac-sha256:" + digest


ACCOUNT_BINDING_ENV = "BROKER_ACCOUNT_BINDING"
#   ( )  /   .
_ACCOUNTLESS_BROKERS = {"meritz"}


def _account_binding_label(broker_name):
    """     (: MERITZ_ACCOUNT_BINDING)."""
    specific = f"{str(broker_name or '').upper()}_ACCOUNT_BINDING"
    for name in (specific, ACCOUNT_BINDING_ENV):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _broker_identity():
    """   / ( ) .

    account_id     HMAC-SHA256 ,
    account_display     ** ** (P2-D).
            .

       ()     /
       .    ** ** :
    1.   env (`MERITZ_ACCOUNT_BINDING`  `BROKER_ACCOUNT_BINDING`)
    2.  (app_key/secret) SHA-256  (* )
       LIVE  fail-closed  .
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
            #        . 
            #  LIVE      (  )
            #   . (LS//  
            #     .)
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
                    f"{broker_name}        - "
                    f"{broker_name.upper()}_ACCOUNT_BINDING ( {ACCOUNT_BINDING_ENV}) "
                    "    . "
                    f"(   {ACCOUNT_FINGERPRINT_SECRET_ENV}  .)"
                )
    return {
        "broker": broker_name,
        "broker_mode": broker_mode,
        "account_id": _account_fingerprint(raw_account),
        "account_display": display,
    }


def _resolve_ttl(ttl_seconds=None, min_ttl_seconds=None):
    """ TTL      fail-closed ."""
    ttl = DEFAULT_APPROVAL_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    min_ttl = (
        DEFAULT_MIN_TTL_SECONDS
        if min_ttl_seconds is None
        else int(min_ttl_seconds)
    )
    if ttl < min_ttl:
        raise ValueError(
            f" TTL({ttl}s)  ({min_ttl}s)  - "
            "ops    APPROVAL_TTL_SECONDS  ."
        )
    return ttl


def _resolve_settlement_run_id(now=None):
    """SETTLE   - env RUN_ID ,  UTC .

     evidence  run_id   . PREVIEW/APPROVE 
    state.last_settlement_id       
    ,   SETTLE  id     (P1-E).
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
    """evidence   (run_id, committed)  ."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", False
    if not isinstance(data, dict):
        return "", False
    return str(data.get("run_id") or "").strip(), data.get("committed") is True


def latest_evidence_run_id(symbol=None):
    """  ****  evidence  run_id ( '').

    committed=true  SETTLE   state    .
    //DRY evidence  committed=false    (P1-E).
    """
    for path in _evidence_candidates(symbol):
        run_id, committed = _load_evidence_candidate(path)
        if run_id and committed:
            return run_id
    return ""


def evidence_file_for_run(symbol, run_id):
    """run_id   ****  evidence   ( '')."""
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
    """   . `committed`  state   ."""
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
    """ evidence  committed   ."""
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
    """state      odno  .

    VERIFY  "   "  "  "   
     . `_apply_recent_history_dt`     
    (ord_datetime_utc > last_updated,  odno ) , 
          .
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

    #      ( ) " " .
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
    """     YYYYMMDD  ."""
    return str(value or "").strip().replace("-", "")


def _row_filled_qty(row):
    """    (   0)."""
    try:
        return float((row or {}).get("ft_ccld_qty", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def _split_settlement_session_fills(order_history, settled_session):
    """   **(deferred)**    .

    : SETTLE  `settled_session`  .    
       T//   **  **.
        (None)    .

    Returns:
        tuple: (  ,    ,    )
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
            if row.get("sll_buy_dvsn_cd_name") == "":
                deferred_net_qty += qty
            elif row.get("sll_buy_dvsn_cd_name") == "":
                deferred_net_qty -= qty
            continue
        scoped.append(row)
    return scoped, deferred, deferred_net_qty


def _deferred_buy_spend(state, order_history, deferred_rows=None):
    """SETTLE   ****     .

    EXECUTE  state  `net_invested`    ,  
        .  reconciliation    
     ,     **** .

    `deferred_rows`     . EXECUTE   
      ** ** , (.deferred.json)  
      live    .

     (P1):
      -  .    -    
           (    ).
      -  (`ft_ccld_amt3` /0)     
        `pending`  . /     
             `pending`  . 
        EXECUTE   .
      -  state   .  SETTLE   .

    Returns:
        tuple[float, list[str]]: (   ,   odno )
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
        if side not in ("", ""):
            #          fail-closed.
            pending.append(str(row.get("odno") or ""))
            continue
        if side != "":
            continue
        amount, estimated = _fill_amount(row)
        if estimated:
            pending.append(str(row.get("odno") or ""))
            continue
        spend += float(amount or 0.0)
    return round(spend, 2), pending


def _deferred_fills_evidence(deferred_rows):
    """SETTLE     (evidence)  .

    VERIFY    live  (odno/side/qty) ,  
     .  ** **  ( ).
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


def _deferred_execution_session_fills(state, order_history):
    """SETTLE       .

    : `state.settled_session`  **   ** 
    SETTLE       T//  .
         SETTLE  ,   EXECUTE 
      "  "  .
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
    """    **    ** .

        , state    
    (`position_qty`)         
    .        fail-closed 
     (    ).
    """
    if not deferred_rows:
        return
    if balance is None or balance.quantity is None:
        raise _tb().SymbolScopedError(
            f"{symbol}       -      "
            "      ."
        )
    stored_qty = state.get("position_qty")
    if stored_qty is None:
        raise _tb().SymbolScopedError(
            f"{symbol}       -    "
            "(position_qty)      ."
        )

    deferred_net_qty = 0
    for row in deferred_rows:
        qty = int(_row_filled_qty(row))
        if row.get("sll_buy_dvsn_cd_name") == "":
            deferred_net_qty += qty
        elif row.get("sll_buy_dvsn_cd_name") == "":
            deferred_net_qty -= qty

    expected_qty = int(stored_qty) + deferred_net_qty
    if int(balance.quantity) != expected_qty:
        raise _tb().SymbolScopedError(
            f"{symbol}       -   {int(stored_qty)} "
            f"+    {deferred_net_qty:+d} = {expected_qty}  "
            f"  {int(balance.quantity)}.   / "
            "    ."
        )


def _assert_reconciliation_with_deferred(
    symbol, state, order_history, balance, *, context
):
    """        .

    `assert_reconciliation_current(allowed_fill_odnos=...)`    
        .      
    ,         .

    Returns:
        list[dict]:       . EXECUTE   
          (.deferred.json) VERIFY  .
    """
    deferred_rows = _deferred_execution_session_fills(state, order_history)
    if deferred_rows:
        _assert_deferred_fills_explained(symbol, state, deferred_rows, balance)
        allowed = {str(row.get("odno") or "") for row in deferred_rows}
        allowed.discard("")
        print(
            f"  [{context}] {symbol}     {len(deferred_rows)} "
            "    (T/    SETTLE  )."
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
    """/   *     .

    `allowed_fill_odnos`  (=   ),   ****
        T//      .
             (P1-B).
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
            f"{symbol}  (/ )     "
            "- SETTLE   ."
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
            #          .
            if new_fills and not unexplained:
                return
        raise _tb().SymbolScopedError(
            f"{symbol}    /   - SETTLE   ."
        )



def assert_no_pending_fence(state, symbol=""):
    """  fence /    * ."""
    problems = []
    if state.get("pending_order_intent"):
        problems.append("pending_order_intent")
    if state.get("pending_order_batch"):
        problems.append("pending_order_batch")
    if state.get("_pending_amount_odnos"):
        problems.append("  ")
    if state.get("_pending_marker_odnos"):
        problems.append(" * ")
    if state.get("_settlement_hold"):
        problems.append(
            f" ({state.get('_settlement_hold_reason', ' ')})"
        )
    if state.get("_position_basis_unknown"):
        problems.append("  ")
    if state.get("balance_mismatch"):
        problems.append("- ")
    if problems:
        raise RuntimeError(
            f"{symbol}  /  PREVIEW/APPROVE/EXECUTE  : "
            + ", ".join(problems)
            + " - SETTLE/fence    ."
        )


def _atomic_write_json(path, payload):
    """mkstemp + fsync + os.replace     ."""
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
    """PREVIEW     ."""
    if plan_id and (plan_id.endswith(".json") or os.path.sep in plan_id):
        path = plan_id
    elif plan_id:
        path = plan_path(plan_id, plans_dir)
    else:
        raise ValueError("PLAN_ID     .")
    with open(path, "r", encoding="utf-8") as fh:
        plan_data = json.load(fh)
    plan_data["_path"] = path
    return plan_data


def write_order_plan(plan, plans_dir=None):
    """PREVIEW ( )   ( / )."""
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
        #  plan_id  =       .
        raise RuntimeError(
            f"    ( ): {path} - "
            "PREVIEW     plan_id  ."
        )
    _atomic_write_json(path, payload)
    print(f"[PREVIEW] {plan.symbol}  : {path} (orders={len(plan.orders)})")
    return path


def approve_order_plan(plan, approvals_dir=None, now=None, ttl_seconds=None):
    """APPROVE  -       ."""
    with global_lock():
        return _approve_order_plan(plan, approvals_dir=approvals_dir, now=now, ttl_seconds=ttl_seconds)


def _approve_order_plan(plan, approvals_dir=None, now=None, ttl_seconds=None):
    """ OrderPlan    .   .

       . PREVIEW   plan_id/orders  
    ,  state_hash/plan_hash      
    .
    """
    if not plan.orders:
        print(f"[] {plan.symbol}       .")
        return None
    if not plan.plan_id or not plan.plan_hash:
        raise ValueError("  plan_id/plan_hash   - PREVIEW   .")
    if normalize_tolerance_policy(plan.tolerance_policy) is None:
        raise ValueError(
            "    (tolerance_policy)  - "
            "PREVIEW   ."
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
        raise ValueError("    plan_hash  -    .")
    path = approval_path(plan.plan_id, approvals_dir)
    _atomic_write_json(path, plan.to_approval())
    print(f"[] {plan.symbol}    : {path} (orders={len(plan.orders)})")
    return path


def approve_plan_file(
    plan_ref, approvals_dir=None, plans_dir=None, now=None, broker=None
):
    """APPROVE  -     PREVIEW  ."""
    with global_lock():
        return _approve_plan_file(
            plan_ref, approvals_dir=approvals_dir, plans_dir=plans_dir, now=now, broker=broker
        )


def _approve_plan_file(
    plan_ref, approvals_dir=None, plans_dir=None, now=None, broker=None
):
    """APPROVE: PREVIEW   ****  ( ).

    -   plan_hash   .
    -  state  canonical hash   state_hash   .
    -   plan_id/orders   .
    """
    plan_data = read_order_plan_file(plan_ref, plans_dir)
    if plan_data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError(
            " schema_version : "
            f"{plan_data.get('schema_version')} != {PLAN_SCHEMA_VERSION}. "
            " (v1) /       - "
            "PREVIEW   ."
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
            "  plan_hash  -  (v1/v2) . "
            "PREVIEW   ."
        )
    if normalize_tolerance_policy(plan_data.get("tolerance_policy")) is None:
        raise ValueError(
            "    (tolerance_policy)  - "
            "  PREVIEW   ."
        )

    symbol = plan_data.get("symbol", "")
    state = _tb().load_state(symbol)
    assert_no_pending_fence(state, symbol)
    actual_hash = canonical_state_hash(state)
    if plan_data.get("state_hash") != actual_hash:
        raise RuntimeError(
            "  : state  PREVIEW   "
            f"(plan={plan_data.get('state_hash')}, current={actual_hash}). "
            " PREVIEW  ."
        )

    # PREVIEW/APPROVE  state  ** **   id  .
    committed_run_id = str(state.get("last_settlement_id") or "")
    if not committed_run_id:
        raise RuntimeError(
            "  : state   SETTLE (last_settlement_id) . "
            "SETTLE   ."
        )
    if str(plan_data.get("settlement_run_id") or "") != committed_run_id:
        raise RuntimeError(
            "  :   run_id  state     "
            f"(plan={plan_data.get('settlement_run_id')}, state={committed_run_id}). "
            " SETTLE  PREVIEW  ."
        )
    if str(plan_data.get("evidence_id") or "") != committed_run_id:
        raise RuntimeError(
            "  :  evidence_id  state     . "
            " SETTLE  PREVIEW  ."
        )

    if broker is not None:
        history = broker.get_order_history(symbol, plan_data.get("exchange", ""), days=30)
        balance = broker.get_balance(symbol, plan_data.get("exchange", ""))
        # SETTLE      state     .
        #        .
        _assert_reconciliation_with_deferred(
            symbol, state, history, balance, context="APPROVE"
        )

    identity = _broker_identity()
    for key in ("broker", "broker_mode", "account_id", "account_display"):
        planned = str(plan_data.get(key) or "")
        current = str(identity.get(key) or "")
        if planned and planned != current:
            raise RuntimeError(
                f"  : {key}  (plan={planned}, current={current})"
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
    # created_at/expires_at  PREVIEW     .
    # (plan_hash         - P2-H)
    return approve_order_plan(plan, approvals_dir=approvals_dir, now=now)


def load_approval(plan_id=None, approvals_dir=None):
    """plan_id      ."""
    if plan_id and (plan_id.endswith(".json") or os.path.sep in plan_id):
        path = plan_id
    elif plan_id:
        path = approval_path(plan_id, approvals_dir)
    else:
        raise ValueError("plan_id     .")
    with open(path, "r", encoding="utf-8") as fh:
        approval = json.load(fh)
    approval["_path"] = path
    return approval


def verify_approval(
    approval, state, now=None, require_unconsumed=True, check_state_hash=True
):
    """        ( )."""
    problems = []
    if not isinstance(approval, dict):
        return ["    ."]
    if approval.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        problems.append(
            f"schema_version : {approval.get('schema_version')} != "
            f"{APPROVAL_SCHEMA_VERSION} (   )"
        )
    if approval.get("consumed") and require_unconsumed:
        problems.append("   (consumed=true).")
    if not approval.get("symbol") or not approval.get("exchange"):
        problems.append("  symbol/exchange  .")
    if not isinstance(approval.get("orders"), list):
        problems.append("  orders  .")

    identity = _broker_identity()
    for key in ("broker", "broker_mode", "account_id", "account_display"):
        approved = str(approval.get(key) or "")
        current = str(identity.get(key) or "")
        if key not in approval:
            #    (/LS//)   
            #     .     .
            if current:
                problems.append(f"  {key}  .")
            continue
        if approved != current:
            problems.append(
                f"{key} : ={approved}, ={current}"
            )
    if not approval.get("evidence_id") and not approval.get("settlement_run_id"):
        problems.append("  evidence_id/settlement_run_id  .")
    if normalize_tolerance_policy(approval.get("tolerance_policy")) is None:
        problems.append(
            "    (tolerance_policy)  - "
            "       ."
        )

    #  : `_et_today()`(ET ) KST    ,
    # state    (settled_session  **** ) .
    expected_session = ""
    if isinstance(state, dict):
        expected_session = str(state.get("execution_session") or "")
    if not expected_session:
        _settled_session, expected_session = settlement_sessions(now)
    if not expected_session:
        problems.append("     (XNYS  )")
    else:
        if str(approval.get("session") or "") != expected_session:
            problems.append(
                " :  "
                f"{approval.get('session')} !=    {expected_session}"
            )
        #  TTL( 9h)       .
        current_et = _et_today(now)
        if current_et > expected_session:
            problems.append(
                f"  :  {expected_session}, (ET) {current_et} "
                "-  SETTLEPREVIEWAPPROVE  ."
            )
        elif current_et < expected_session:
            #    ET  (: KST  = ET  )
            #     .    
            # ( 00:00~)  ** **  .
            problems.append(
                f"     :  {expected_session}, "
                f"(ET) {current_et} -   ."
            )

    current_now = now or _tb().datetime.now(ZoneInfo("UTC"))
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=timezone.utc)
    expires = _parse_iso(approval.get("expires_at_utc"))
    if expires is None:
        problems.append("expires_at_utc    .")
    elif current_now.astimezone(timezone.utc) >= expires:
        problems.append(f" : {approval.get('expires_at_utc')}")

    if state is not None:
        if state.get("_state_unavailable"):
            problems.append(f"   : {state.get('_state_unavailable')}")
        committed_run_id = str(state.get("last_settlement_id") or "")
        if not committed_run_id:
            problems.append(
                "state   SETTLE (last_settlement_id)  - "
                "SETTLE   ."
            )
        else:
            approved_run_id = str(
                approval.get("evidence_id")
                or approval.get("settlement_run_id")
                or ""
            )
            if approved_run_id != committed_run_id:
                problems.append(
                    " run_id : ="
                    f"{approved_run_id or '()'}, state  ={committed_run_id}"
                )
        if check_state_hash:
            actual_hash = canonical_state_hash(state)
            if approval.get("state_hash") != actual_hash:
                problems.append(
                    "  :   state   "
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
            "plan_hash :      ."
        )
    return problems


EXECUTION_MARKER_SUFFIX = ".executed.json"
OUTCOME_RECORD_SUFFIX = ".recovery.json"
TRANSMITTED_PLAN_SUFFIX = ".transmitted.json"
DEFERRED_RECORD_SUFFIX = ".deferred.json"
#  (= )  . approval-skipped    .
_TERMINAL_APPROVAL_OUTCOMES = {"executed", "execution-failed"}


def execution_marker_path(approval_path):
    """    -        ."""
    return f"{approval_path}{EXECUTION_MARKER_SUFFIX}"


def transmitted_plan_path(approval_path):
    """EXECUTE  ** **    .

     (  ) ,    ( 
     ) . VERIFY      
        .
    """
    return f"{approval_path}{TRANSMITTED_PLAN_SUFFIX}"


def read_transmitted_plan(approval_path):
    """     (   dict)."""
    try:
        with open(transmitted_plan_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def deferred_record_path(approval_path):
    """EXECUTE  ****      .

    PRE-EXECUTE        . VERIFY 
    live  "EXECUTE   " "EXECUTE   "
      ,     .
    """
    return f"{approval_path}{DEFERRED_RECORD_SUFFIX}"


def read_deferred_record(approval_path):
    """EXECUTE     (   dict)."""
    try:
        with open(deferred_record_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if data else {}


def transmitted_plan_problems(approval):
    """  **  **   (VERIFY ).

         . (consumed=true)
       `.transmitted.json`    - 
    /   "    " .

    :
      -  /JSON 
      - plan_id/symbol/session/state_hash/settlement_run_id/evidence_id 
      - approval_plan_hash ==   plan_hash
      - transmitted_plan_hash    **** 
    """
    path = str(approval.get("_path") or "")
    if not path:
        return ["        ."]
    record_path = transmitted_plan_path(path)
    if not os.path.exists(record_path):
        return [
            "   (.transmitted.json)  "
            f"-     : {record_path}"
        ]
    data = read_transmitted_plan(path)
    if not data:
        return [f"      (/ ): {record_path}"]

    problems = []
    for key in ("plan_id", "symbol", "session"):
        recorded = str(data.get(key) or "")
        approved = str(approval.get(key) or "")
        if recorded != approved:
            problems.append(
                f"  {key} : ={recorded or '()'}, "
                f"={approved or '()'}"
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
                f"  {key} : ={recorded or '()'}, "
                f"={approved or '()'}"
            )
    approved_hash = str(approval.get("plan_hash") or "")
    if str(data.get("approval_plan_hash") or "") != approved_hash:
        problems.append(
            "      (approval_plan_hash )"
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
            "transmitted_plan_hash :      "
            "."
        )

    #     
    # EXECUTE      ,    
    # (//)   .   
    #        .
    #
    #  **   **  ,  env  
    #      .   env    
    #   (   - v3).
    policy = effective_tolerance_policy(approval.get("tolerance_policy"))
    if policy is None:
        problems.append(
            "       (    )"
        )
    approved_orders = list(approval.get("orders") or [])
    recorded_orders = list(data.get("orders") or [])
    if len(approved_orders) != len(recorded_orders):
        problems.append(
            f"    :  {len(approved_orders)} vs "
            f" {len(recorded_orders)}"
        )
    elif policy is not None:
        qty_tol = int(policy["qty"])
        for index, (approved, recorded) in enumerate(
            zip(approved_orders, recorded_orders), 1
        ):
            for field in ("side", "order_type", "comment"):
                if str(approved.get(field, "")) != str(recorded.get(field, "")):
                    problems.append(
                        f"  {index} {field} : "
                        f"={approved.get(field)!r}, ={recorded.get(field)!r}"
                    )
            a_qty = int(float(approved.get("quantity", 0) or 0))
            r_qty = int(float(recorded.get("quantity", 0) or 0))
            if abs(a_qty - r_qty) > qty_tol:
                problems.append(
                    f"  {index}  : ={a_qty}, ={r_qty} "
                    f"( {qty_tol})"
                )
            a_price = float(approved.get("price") or 0.0)
            r_price = float(recorded.get("price") or 0.0)
            tolerance = price_tolerance(a_price, policy)
            if abs(a_price - r_price) > tolerance:
                problems.append(
                    f"  {index}  : =${a_price:.4f}, "
                    f"=${r_price:.4f} ( ${tolerance:.4f})"
                )
            a_amount = a_price * a_qty
            r_amount = r_price * r_qty
            amount_tolerance = max(tolerance * max(a_qty, 1), 0.01)
            if abs(a_amount - r_amount) > amount_tolerance:
                problems.append(
                    f"  {index}  : =${a_amount:.2f}, "
                    f"=${r_amount:.2f} ( ${amount_tolerance:.2f})"
                )
            a_target = approved.get("t_target")
            r_target = recorded.get("t_target")
            if a_target is not None and r_target is not None:
                if abs(float(a_target) - float(r_target)) > 1e-9:
                    problems.append(
                        f"  {index} t_target : {a_target} vs {r_target}"
                    )
    return problems


def approval_outcome_path(approval_path):
    """EXECUTE  (executed/execution-failed/approval-skipped) ."""
    return f"{approval_path}{OUTCOME_RECORD_SUFFIX}"


def read_approval_outcome(approval_path):
    """   (   dict)."""
    try:
        with open(approval_outcome_path(approval_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def approval_execution_blockers(path, approval=None):
    """ /    .

    - consumed=true ()
    -     (   )
    - ** **   (executed/execution-failed)

    `approval-skipped`   (     )  
      ,       .
    """
    path = str(path or "")
    if not path:
        return ["   ."]
    data = approval
    if not isinstance(data, dict):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    problems = []
    if data.get("consumed") is True:
        problems.append("   (consumed=true).")
    if os.path.exists(execution_marker_path(path)):
        problems.append("   (  ).")
    outcome = str(read_approval_outcome(path).get("outcome") or "")
    if outcome in _TERMINAL_APPROVAL_OUTCOMES:
        problems.append(f"    (outcome={outcome}).")
    return problems


@contextmanager
def _approval_lock(path):
    """     - read-check-write  ."""
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


#     (state read-modify-write ) 
# run_daily.sh  `.run.lock`  flock  , cron   
# ** CLI **(SETTLE/EXECUTE/REPAIR)   . 
# `.state.json`    read-modify-write  T/
# ,    (`RUN_LOCK_PATH`,  `.run.lock`)
#    .   (`_approval_lock`)  
# 2  .
RUN_LOCK_PATH_ENV = "RUN_LOCK_PATH"
RUN_LOCK_HELD_ENV = "RUN_LOCK_HELD"
#    fd .   fd  RUN_LOCK_PATH  ** **
#    " "  (  ).
RUN_LOCK_FD_ENV = "RUN_LOCK_FD"
DEFAULT_RUN_LOCK_FD = 9
DEFAULT_RUN_LOCK_TIMEOUT_SECONDS = float(
    os.getenv("RUN_LOCK_TIMEOUT_SECONDS", "300") or 300
)
#    -     (main/) 
#     flock    .
_RUN_LOCKS = {}
# `src/pipeline.py`   =  .  APP_DIR   .
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_lock_path():
    """   ** ** (shell launcher    ).

     :
      1.   `RUN_LOCK_PATH` ( export  **  **)
      2. `.env`  `RUN_LOCK_PATH` (python-dotenv   env   )
      3.  `.run.lock`
      (= APP_DIR)  . 
    `.env`     /    .
    `global_lock(path=...)`  ** **   
         (/).
    """
    raw = os.getenv(RUN_LOCK_PATH_ENV, "").strip() or ".run.lock"
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(_PROJECT_ROOT, raw))


def _lock_key(path):
    return os.path.abspath(path)


def _run_lock_inherited_fd(path):
    """   fd  `path`  ** ** .

    `RUN_LOCK_HELD=1`  "     "
      (:    CLI).  fd 9  flock  
    `RUN_LOCK_FD=9`  export , fd  st_dev/st_ino   
        .

    Returns:
        int | None:  fd .   None.
    """
    raw = os.getenv(RUN_LOCK_FD_ENV, str(DEFAULT_RUN_LOCK_FD)).strip()
    try:
        fd = int(raw)
    except (TypeError, ValueError):
        return None
    if fd <= 2:
        # stdin/stdout/stderr   fd   .
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
    """ (  fd)     .

     probe . `RUN_LOCK_HELD=1`  **   **.
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
    """  **   ** .

    fd       (  
      fd   ). :
      1)       (probe),
      2) ** fd ** -flock  .
    flock   open file description  idempotent ,  fd 
       description  ,   EWOULDBLOCK
     .        .

    Returns:
        int | None:   fd.   None.
    """
    fd = _run_lock_inherited_fd(path)
    if fd is None or fcntl is None:
        return None
    if not _run_lock_is_held_by_other(path):
        #           .
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd


@contextmanager
def global_lock(path=None, timeout_seconds=None):
    """   -  /    .

    - ** **:   fd(`RUN_LOCK_FD`,  9)   
      inode       . 
       .
    - **   CLI**:   .    
      `timeout_seconds`    TimeoutError (fail-closed).  
       .
    -     .
    - `timeout_seconds`     TimeoutError (fail-closed).
    """
    target = run_lock_path() if path is None else os.path.abspath(path)
    key = _lock_key(target)
    state = _RUN_LOCKS.setdefault(key, {"depth": 0, "handle": None, "pid": None})
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    #    flock    (  ).
    if state["depth"] > 0 and state["pid"] == os.getpid():
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
        return
    #  fd       .
    if _inherited_lock_fd(target) is not None:
        yield
        return
    flag_active = os.getenv(RUN_LOCK_HELD_ENV, "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if flag_active:
        print(
            f"[] {RUN_LOCK_HELD_ENV}=1     "
            f"(fd {os.getenv(RUN_LOCK_FD_ENV, DEFAULT_RUN_LOCK_FD)})  "
            f"    - {target}   . "
            "      ."
        )
    if fcntl is None:
        yield
        return

    wait = (
        DEFAULT_RUN_LOCK_TIMEOUT_SECONDS
        if timeout_seconds is None
        else float(timeout_seconds)
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
                        f"   {wait:.0f}s   : {target} "
                        "-  (SETTLE/EXECUTE/REPAIR)  ."
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
    """  (consumed=true) -   **** 1.

      read-check-write  ,    ** **
     . /     
           (P0-A).
    """
    path = approval.get("_path")
    if not path:
        raise ValueError("   .")
    with _approval_lock(path):
        return _claim_approval_locked(path, now=now)


def _claim_approval_locked(path, now=None):
    """  **  **   .

    `execute_approved_plan()`  claimhandle    
       .  `claim_approval()`  .
    """
    with open(path, "r", encoding="utf-8") as fh:
        latest = json.load(fh)
    blockers = approval_execution_blockers(path, latest)
    if blockers:
        raise RuntimeError(" / ".join(blockers))
    latest["consumed"] = True
    latest["consumed_at_utc"] = _utc_now_iso(now)
    _atomic_write_json(path, latest)
    #      .   ( )
    #    .
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
    print(f"[]   : {path}")
    return latest


def _plan_id_suffix(created_utc):
    """PREVIEW  ****  ( +  uuid).

     / RUN_ID  PREVIEW      plan_id 
      .    PREVIEW     
    ,   plan_id  **  **.
    """
    parsed = _parse_iso(created_utc)
    stamp = (
        parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if parsed is not None
        else "unknown"
    )
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _prepare_plan_identity(plan, now=None, ttl_seconds=None):
    """plan_id / session /  /   / plan_hash  ."""
    tb = _tb()
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    ttl = _resolve_ttl(ttl_seconds)
    created = _utc_now_iso(current)
    expires = (current.astimezone(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    #   "  " (_et_today  KST  ET ).
    _settled_session, _execution_session = settlement_sessions(current)
    session = plan.session or _execution_session or _et_today(current)
    run_id = plan.settlement_run_id or _resolve_settlement_run_id(current)
    if not run_id:
        raise RuntimeError(
            "  run_id   - PREVIEW  SETTLE   "
            "last_settlement_id  . SETTLE   ."
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
    #   PREVIEW 1 1, ** **. plan_id  
    # uuid   APPROVE     ( +)  .
    plan.plan_id = f"{plan.symbol}-{session}-{run_id}-{_plan_id_suffix(created)}"
    #     .  env    .
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
    """plan_id/orders/hash      TTL  ."""
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
    """ops/verify_run.py    (import  cwd  )."""
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
    """ops/verify_run.py      ."""
    if evidence is None:
        return ["  ."]
    try:
        return list(_load_verify_run_module()._verify_evidence(evidence) or [])
    except Exception as error:
        return [f"   : {error}"]


def _verify_settlement_final(evidence_path, evidence, state):
    """**  **(MAX_T/   )  .

    evidence   ****  (T_after ), state   
    . ops/verify_run.py  evidence  `post_processing`  
        -      
     (P1: evidence-vs-final).
    """
    if not evidence_path:
        return ["        ."]
    tb = _tb()
    try:
        ok, problems = verify_evidence_file_with_ops(
            evidence_path,
            #   / .    " 
            # "   VERIFY    .
            broker=evidence.get("broker"),
            mode=evidence.get("mode") or tb.TRADE_MODE,
            run_id=evidence.get("run_id"),
            broker_mode=evidence.get("broker_mode"),
            final_state=state,
            max_t=evidence.get("max_t"),
        )
    except Exception as error:
        return [f"    : {error}"]
    return [] if ok else list(problems or ["   "])


def verify_evidence_file_with_ops(path, **kwargs):
    """ops/verify_run.py  verify_evidence_file   ."""
    cwd = os.getcwd()
    try:
        return _load_verify_run_module().verify_evidence_file(path, **kwargs)
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass


def _history_settledness_enabled():
    flag = os.getenv("HISTORY_SETTLEDNESS_CHECK", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # :      .
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
    """ ** **      True.

    (ET 16:00 )        
    .    RECENT_ORDER_GUARD_MINUTES   
      (  ) .    
    ``  () guard   .
    """
    tb = _tb()
    try:
        guard_minutes = int(os.getenv("RECENT_ORDER_GUARD_MINUTES", "15"))
    except ValueError:
        guard_minutes = 15
    if guard_minutes <= 0:
        return False
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_utc = current.astimezone(timezone.utc)
    current_et = current_utc.astimezone(ZoneInfo("America/New_York"))
    cutoff = current_utc - timedelta(minutes=guard_minutes)
    open_tokens = {"", "", "OPEN", "PARTIAL", "PENDING"}
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
            continue  #   
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
    """orders_meta    UTC   ( None)."""
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
    """  /     .

       "   "   .
     fail-closed   (P1-F):
      1. state.last_updated()      ,
                 state    .
      2. orders_meta           .
             ( ).
    """
    problems = []
    rows = list(history or [])
    by_odno = {str(row.get("odno") or ""): row for row in rows}
    window_start = _history_window_start_utc(history_days, now)

    #  ****       .
    # (  last_updated    - FORCE_T       -
    #       .)
    watermark_ts = _parse_iso(state.get("last_updated"))
    watermark_ordno = str(state.get("last_processed_ordno") or "")
    if (
        watermark_ordno
        and watermark_ts is not None
        and watermark_ts >= window_start
    ):
        if watermark_ordno not in by_odno:
            problems.append(
                f"  {watermark_ordno}    "
                "( / )"
            )
        else:
            row_ts = _parse_iso(by_odno[watermark_ordno].get("ord_datetime_utc"))
            if row_ts is not None and row_ts.astimezone(timezone.utc) < watermark_ts:
                problems.append(
                    f"  {watermark_ordno}   state   "
                    f"(history={row_ts.isoformat()}, watermark={state.get('last_updated')})"
                )

    for odno, meta in (state.get("orders_meta") or {}).items():
        if str(odno) in by_odno:
            continue
        submitted_ts = _meta_order_utc(meta)
        if submitted_ts is None:
            continue  #    -   ( )
        if submitted_ts >= window_start:
            problems.append(
                f"orders_meta  {odno}    "
                f"(submitted={submitted_ts.isoformat()})"
            )
    return problems


def _verify_history_settled(broker, state, symbol, exchange, history, history_days):
    """ ( )  .

    : (1)   , (2) /orders_meta  ,
    (3)       .   fail-closed 
     / .

    :         ,  
      orders_meta    .  /
     ET 16:00   ( ).
    """
    if not _history_settledness_enabled():
        return True, ""
    delay = 1.0
    try:
        delay = float(os.getenv("HISTORY_SETTLEDNESS_DELAY_SECONDS", "1.0"))
    except ValueError:
        delay = 1.0
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
        return False, f"  : {error}"
    if _history_snapshot(repeated) != _history_snapshot(history):
        return False, "     (  )"
    completeness = _verify_history_complete(state, repeated, history_days)
    if completeness:
        return False, " / ".join(completeness[:3])
    if _history_has_recent_open_order(repeated):
        return False, "        "
    return True, ""


def _settle_positions(broker, symbol_config, *, persist=True, now=None):
    """  T// .

    LIVE  persist=True  ** 1**  .
    DRY   .
    """
    tb = _tb()
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    splits = symbol_config["splits"]
    symbol_type = symbol_config["symbol_type"]
    seed = symbol_config["seed"]
    additional_loc_levels = symbol_config.get("additional_loc_levels", 3)

    # FORCE_T       ** **, REPAIR 
    # (`trading_bot._repair_force_t()`) . SETTLE 
    #    env var   SETTLE    T  
    # , //   fail-closed  .
    if symbol_config.get("force_t") is not None:
        raise RuntimeError(
            f"{symbol} {symbol}_FORCE_T  SETTLE    . "
            "TRADE_MODE=REPAIR REPAIR_ACTION=FORCE_T    "
            "(  1 )."
        )

    settlement_run_id = _resolve_settlement_run_id()
    #  (  )  ( ) .
    # `_et_today()`  KST   ET      .
    settled_session, execution_session = settlement_sessions(now)
    session = execution_session or _et_today(now)

    if not settled_session:
        print(
            f"   {symbol}       (XNYS  ). "
            " ."
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
    #   Step 1( )      .
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
        """    ** 1**  (P1-D).

        - LIVE:    
        - DRY: /   (FORCE_T  REPAIR  ).
        """
        if persist and tb.TRADE_MODE == "LIVE":
            tb.save_state(symbol, state)
            return True
        return False

    #  Step 1: T      
    print("\n[Step 1] T  ...")

    state = tb.load_state(symbol)
    #  /   state  . PREVIEW/APPROVE/EXECUTE 
    #     KST   ET   .
    state["settled_session"] = settled_session
    state["execution_session"] = execution_session

    #     (  ) 
    #  (16:00 ET)  , KST 09:30( ET 19:30)
    # (20:00 ET )        
    # .       / 
    #  (fail-closed).
    from broker.market_utils import after_hours_end_et_hour, us_session_after_hours_end

    if not us_session_after_hours_end(settled_session, now):
        print(
            f"   {symbol} {settled_session}     "
            f"(AFTER_HOURS_END_ET_HOUR={after_hours_end_et_hour()}). "
            " ."
        )
        return _settled("after_hours_not_ended")

    # _state_unavailable   -    
    # order_history/balance      .
    state_unavailable = state.get("_state_unavailable")

    #      : state  + last_updated  + LIVE
    #    1  (notify  , exit ).
    if state_unavailable and tb.TRADE_MODE == "LIVE" and not state.get("last_updated"):
        print("[]   . DRY 1   LIVE  .")

    unresolved_zero_invested = (
        state.get("net_invested_status", "unresolved") != "valid"
        and float(state.get("net_invested", 0.0) or 0.0) <= 0
    )
    if unresolved_zero_invested and tb.TRADE_MODE == "LIVE":
        tb.notify(
            f"[] {symbol} net_invested (unresolved)  0      .\n"
            "STATE_REVERSE_AUDIT_ONLY/reconcile  ."
        )

    max_t = symbol_config.get("max_t")

    from config import FORCE_T_REINFERENCE as _force_reinference, TRADE_MODE as _trade_mode
    if _force_reinference:
        if _trade_mode == "LIVE":
            print("[T ] FORCE_T_REINFERENCE=true  TRADE_MODE=LIVE -    DRY .")
            import config as _cfg
            _cfg.TRADE_MODE = "DRY"
        if state.get("last_updated"):
            print(f"[T ] FORCE_T_REINFERENCE=true  last_updated  (  )")
            state["last_updated"] = ""

    #       
    cycle_start_date = state.get("cycle_start_date", "")
    last_updated = state.get("last_updated", "")

    if not last_updated:
        #  (    ):   T    
        # 5~6  20 =  100~120 
        history_days = 90
    elif cycle_start_date:
        #       
        try:
            start_dt = tb.datetime.strptime(cycle_start_date, "%Y-%m-%d")
            days_since_start = (tb.datetime.now() - start_dt).days + 5
            history_days = max(days_since_start, 30)
        except ValueError:
            history_days = 30
    else:
        history_days = 30

    # DRY   ORDER_HISTORY_VERBOSE=true      .
    order_history = broker.get_order_history(
        symbol,
        exchange,
        days=history_days,
        verbose=(tb.TRADE_MODE == "DRY" or tb.ORDER_HISTORY_VERBOSE),
    )

    #   ()    
    #   "  ",  SETTLE   
    #  **  **     .  
    #         /  .
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
                f"[] {symbol}   ({in_progress_session})  "
                f"{len(contaminated)}   -    "
                "    ."
            )
            return _settled("in_progress_session_fills")

    #  **  **   ( ) 
    #     `settled_session` ,  
    #    ** (= )**  /   
    #   (KST  ,  SETTLE,    ).
    #    "  "  SETTLE  ****( ),
    #    ** **   /   
    #   .  (None)   /  .
    execution_ymd = _session_ymd(execution_session)
    if not execution_ymd:
        print(
            f"[] {symbol}          "
            " - / ."
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
            f"[] {symbol}  ({execution_session}) ****   "
            f"{len(later_fills)}   "
            f"(: {', '.join(sessions)}) -     "
            "  / ."
        )
        return _settled("post_settled_session_fills")

    #     -  /  (fail-closed).
    history_ok, history_reason = _verify_history_settled(
        broker, state, symbol, exchange, order_history, history_days
    )
    if not history_ok:
        print(f"[] {symbol}     : {history_reason}")
        return _settled("history_not_settled")

    #    update_T_from_history    .
    #    T     -    ,  0
    #  state    fallback,  (None)  .
    #  (live_qty )  -   .
    try:
        live_balance = broker.get_balance(symbol, exchange)
        live_qty = live_balance.quantity if live_balance else 0
        live_avg = live_balance.avg_price if live_balance else 0.0
    except Exception as e:
        # LS  IGW40014  :     T 
        if tb.LS_DEMO_BYPASS_BUGS:
            print(f"[ ]    (T ) - LS   : {e}")
        else:
            print(f"[ ]    (T ): {e}")
        live_balance = None
        live_qty = None
        live_avg = None

    #      LIVE   (fail-closed) 
    #     /T    ,  *
    # ( `if live_qty is not None`)  .  LIVE 
    #     (DRY     ).
    if live_qty is None and tb.TRADE_MODE != "DRY":
        print(
            f"[] {symbol}       "
            "(   T/  )."
        )
        tb.notify(
            f"[] {symbol}    SETTLE  . "
            " API    .",
            urgent=True,
        )
        return _settled("balance_unavailable")

    #     **  ** ( ) 
    # : SETTLE  `settled_session`  .    
    # (    )   T// 
    # (evidence) ****.     
    # (last_updated/last_processed_ordno)    , 
    # **   SETTLE**    * ( ).
    #      ,   
    # "  "   (  /  ).
    scoped_history, deferred_fills, deferred_net_qty = _split_settlement_session_fills(
        order_history, settled_session
    )
    if deferred_fills:
        print(
            f"   {symbol}  ({execution_session})  "
            f"{len(deferred_fills)}      "
            f" ( {deferred_net_qty:+d},  )."
        )
        if live_qty is not None:
            live_qty = int(live_qty) - deferred_net_qty
        order_history = scoped_history

    #     (state  +  0 +  0) 
    # state  //,      
    #    T=0 LIVE  .    .
    if tb.TRADE_MODE == "LIVE" and state_unavailable:
        history_filled = sum(
            1 for o in order_history
            if int(float(o.get("ft_ccld_qty", "0") or 0)) > 0
        )
        if history_filled == 0 and live_qty in (0, None):
            print(f"[ ] {symbol}    T=0 LIVE ")
        else:
            raise RuntimeError(
                f"{symbol}      LIVE  : "
                f"{state_unavailable}"
            )
    #    (    ) -  .
    watermark_before = str(state.get("last_updated", "") or "")
    T_before = float(state.get("T", 0.0))
    net_invested_before = float(state.get("net_invested", 0.0))
    #     -  " /"     .
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

    #     - update    / .
    pending_amount_odnos = list(state.get("_pending_amount_odnos") or [])
    if not pending_amount_odnos:
        pending_amount_odnos = _has_unconfirmed_amount_fills(state, order_history)
    if pending_amount_odnos:
        state["_pending_amount_odnos"] = pending_amount_odnos
        state.setdefault("_settlement_hold", True)
        state.setdefault(
            "_settlement_hold_reason",
            f"   {len(pending_amount_odnos)}",
        )

    #     (    ) 
    #     " " .
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
    # SETTLE        (P2). `balance_qty` 
    #     , VERIFY    live  
    #      .     
    #    .
    settlement_evidence["deferred_fills"] = _deferred_fills_evidence(deferred_fills)

    hold = bool(state.get("_pending_amount_odnos") or state.get("_settlement_hold"))
    #  ****   +    .
    evidence_problems = [] if hold else _verify_settlement_evidence(settlement_evidence)
    if evidence_problems:
        raise tb.SymbolScopedError(
            f"{symbol}      - / : "
            + " / ".join(evidence_problems[:3])
        )
    #    committed=false  , state   
    # committed=true  . //DRY  VERIFY/PREVIEW 
    #    (P1-E).
    try:
        evidence_path = _write_settlement_evidence(
            symbol,
            settlement_evidence,
            committed=False,
            failure_reason="settlement_hold" if hold else "",
        )
    except OSError as evidence_error:
        raise tb.SymbolScopedError(
            f"{symbol}     - / : "
            f"{str(evidence_error)[:160]}"
        ) from evidence_error

    #  /   evidence    **** * .
    # DRY  /   ,    
    #     (AGENTS: DRY / * ).
    # LIVE   fail-closed     .
    if state.get("_pending_amount_odnos") or state.get("_settlement_hold"):
        if tb.TRADE_MODE == "DRY" and not state.get("_position_basis_unknown"):
            reason = state.get("_settlement_hold_reason", "/ ")
            warning = (
                f"[] {symbol} {reason} - DRY  /  "
                "  (LIVE  )."
            )
            print(warning)
            tb.notify(warning)
            state.pop("_pending_amount_odnos", None)
            state.pop("_pending_marker_odnos", None)
            state.pop("_settlement_hold", None)
            state.pop("_settlement_hold_reason", None)
        else:
            tb._save_settlement_state(symbol, state)

    #    T   (    ) 
    infer_diag = state.get("_inference_diagnostic", {})
    if infer_diag.get("small_seed_days", 0) > 0 and live_qty is not None and live_avg is not None and live_qty > 0 and live_avg > 0:
        _used_seed = state.get("effective_seed", 0.0) or seed
        if _used_seed > 0:
            T_position = round((live_qty * live_avg * splits) / _used_seed, 4)
            T_position = min(T_position, max_t if max_t is not None else splits)
            print(f"   []   T : {T_position}")
            print(f"       {symbol}_FORCE_T={T_position}   TRADE_MODE=REPAIR REPAIR_ACTION=FORCE_T   ")

    #    (evidence       ) 
    # MAX_T/  evidence  **** state  .
    #    ,   " "   .
    post_processing = {"applied": False, "steps": []}

    def _post_step(name, **info):
        post_processing["applied"] = True
        post_processing["steps"].append({"step": name, **info})

    #  T   (MAX_T) 
    if max_t is not None and state["T"] > max_t:
        print(f"[T ] {symbol} T={state['T']} > MAX_T={max_t}  T={max_t} ")
        _post_step("max_t", before=float(state["T"]), after=float(max_t))
        state["T"] = max_t
        reset_settlement_ledger(state, max_t)

    #     (  ) 
    # _infer_T_from_full_history   T=0  ,
    #  (T > 0 )     .
    completed_cycle_start = state.pop("_completed_cycle_start", None)
    if completed_cycle_start:
        if tb._has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
            return _settled("unresolved_reverse_orders")
        print(f"\n{'=' * 60}")
        print(f"[ ] {symbol} - {completed_cycle_start} ~ ")
        print(f"{'=' * 60}")
        _used_seed = state.get("effective_seed", 0.0)
        if not (tb.REINVEST and _used_seed > 0):
            _used_seed = seed
        #       .
        # (T>0  cycle_start_date  =    )
        new_cycle_started = bool(
            float(state.get("T", 0.0) or 0.0) != 0.0
            or float(state.get("net_invested", 0.0) or 0.0) != 0.0
            or state.get("cycle_start_date", "")
        )
        if new_cycle_started:
            #    ,   / .
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
            #    (T=0/net=0).  0   
            # **(/)**  .
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
        # effective_seed    ** 1**.
        if tb.REINVEST and finalized["next_cycle_seed"] > 0:
            state["effective_seed"] = round(finalized["next_cycle_seed"], 2)
            print(f"   :    = ${state['effective_seed']:.2f}")
        else:
            state["effective_seed"] = 0.0

    #     
    #      ** **    .
    # (         )
    tb._save_settlement_state(symbol, state)

    #  settle_positions()   1  (LIVE).

    #  -  (conservative reconciliation) 
    try:
        computed = compute_position_from_history(order_history, cycle_start_date=state.get("cycle_start_date", ""))
    except Exception as e:
        print(f"[ ]     : {e}")
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
                f"[]   {comp_qty}( ${comp_avg:.2f}) ,   0."
            )
            if tb.TRADE_MODE == "DRY":
                # DRY /  - **     .
                print(f"{msg} DRY  /   . (LIVE     )")
                tb.notify(msg)
            else:
                print(f"{msg}   : T=0 .")
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
                    f"{symbol}       . "
                    f"history={comp_qty}, broker={live_qty}"
                )

        elif live_qty != comp_qty:
            #     -   
            msg = (
                f"[]  net_qty={comp_qty},  ={live_qty}.   .  ."
            )
            if tb.TRADE_MODE == "DRY":
                # DRY /  -       .
                print(f"{msg} (DRY  balance_mismatch  )")
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
                    f"{symbol}       . "
                    f"history={comp_qty}, broker={live_qty}"
                )

        elif state["T"] == 0 and live_qty > 0:
            # T=0   : T  (    )
            msg = (
                f"[] {symbol} T=0   {live_qty}  . "
                f"T     . .state.json   /."
            )
            if tb.TRADE_MODE == "DRY":
                # DRY /  -       .
                print(f"{msg} (DRY  balance_mismatch  )")
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
                    f"{symbol} T=0   {live_qty}   ."
                )

        else:
            #  ,    
            if state.get("balance_mismatch"):
                state.pop("balance_mismatch", None)

    #   fence  (      ) 
    # / reconciliation    .
    # fence    , ( )    .
    if tb.TRADE_MODE == "LIVE":
        tb._recover_order_fence(state, symbol, live_qty, persist=False)

    #  T  (FORCE_T)    
    # REPAIR  (`trading_bot._repair_force_t()`) FORCE_T  .
    #   force_t   SETTLE    
    #   ( env var   ).

    T = state["T"]
    print(f"   T: {T}")

    #      (  ) 
    # live  ,     .
    #   (None)     (SOXL ).
    current_qty = live_qty if live_qty is not None else comp_qty

    if T > 0 and current_qty == 0 and live_qty is None:
        #     T  (  , API  )
        print(f"\n[] {symbol}     -     (T={T} ,    )")
        tb.notify(f"[] {symbol}       . T={T} .  .")

    elif T > 0 and current_qty == 0:
        #  0 :   state.json  
        # Step 1   T   ,
        #   _infer_T_from_full_history T=0 
        #      T>0 ,     
        #     / T 
        print(f"\n{'=' * 60}")
        print(f" {symbol}    () (T={T}, ={current_qty})")
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
            # DRY     effective_seed  0 .
            dry=bool(tb.TRADE_MODE == "DRY"),
        )

        report_message = tb.format_cycle_report_message(report)
        print(f"\n{report_message}")
        tb.notify(report_message)

        if tb.TRADE_MODE == "DRY":
            # DRY /  -   ( /)  .
            #      in-memory .
            state["effective_seed"] = 0.0
            state["T"] = 0.0
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, 0.0)
            T = 0.0
            print("  [DRY]    .   . (LIVE    / )")
        else:
            #        state .
            #      .
            if tb.REINVEST and report["next_cycle_seed"] is not None:
                state["effective_seed"] = round(report["next_cycle_seed"], 2)
                print(f"   :    = ${state['effective_seed']:.2f}")
            else:
                state["effective_seed"] = 0.0

            if tb._has_unresolved_reverse_orders(state):
                state.setdefault("reverse_mode", {})["reconciliation_only"] = True
                state["reverse_mode"]["active"] = False
                return _settled("unresolved_reverse_orders")

            # T     
            state["T"] = 0.0
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
            state["reverse_mode"] = {}
            reset_settlement_ledger(state, 0.0)

            print("  T  .    .")
            T = 0.0
            #  Step 2   (   ) 

    if current_qty == 0 and state.get("reverse_mode", {}).get("active"):
        if tb._has_unresolved_reverse_orders(state):
            state["reverse_mode"]["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
        else:
            state["reverse_mode"] = {}

    #  seed  ( ) 
    if tb.REINVEST:
        effective_seed = state.get("effective_seed", 0.0)
        if effective_seed > 0:
            seed = effective_seed
            print(f"    :    = ${seed:.2f}")

    #     (    1) 
    # state     id    PREVIEW/APPROVE 
    # " SETTLE"   (P1-E).
    live_persist = bool(persist and tb.TRADE_MODE == "LIVE")
    will_persist = live_persist
    if live_persist:
        state["last_settlement_id"] = settlement_run_id
        state["last_settlement_evidence"] = evidence_path
        state["last_settlement_committed_at_utc"] = _utc_now_iso()

    #      (evidence-vs-final) 
    # (MAX_T/ ) evidence  T_after/net_invested_after 
    #    .    , ** ** 
    #  ops/verify_run.py   .  / .
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
                f"{symbol}      - / : "
                f"{str(evidence_error)[:160]}"
            ) from evidence_error
        final_problems = _verify_settlement_final(evidence_path, settlement_evidence, state)
        if final_problems:
            raise tb.SymbolScopedError(
                f"{symbol}       - / : "
                + " / ".join(final_problems[:3])
            )
    saved = _persist()
    if saved and live_persist:
        try:
            _mark_evidence_committed(evidence_path, settlement_evidence, True)
        except OSError as commit_error:
            # state   evidence  .   PREVIEW 
            # settled_from_state()  evidence   fail-closed .
            print(
                f"[] {symbol}   committed   - "
                f"PREVIEW/VERIFY  : {str(commit_error)[:160]}"
            )
    else:
        # DRY/ :     .
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
    """SETTLE  -     .

    `.state.json` read-modify-write     ( 
     `.run.lock`).  CLI  SETTLE/EXECUTE    T/
     .
    """
    with global_lock():
        settled = _settle_positions(broker, symbol_config, settlement_run_id=_resolve_settlement_run_id(), persist=persist, now=now)
        if settled.halt_reason:
            #  :    (committed=False), state   
            evidence_path = _write_settlement_evidence(
                symbol_config["symbol"],
                {
                    "symbol": symbol_config["symbol"],
                    "run_id": settled.settlement_run_id,
                    "mode": _tb().TRADE_MODE,
                    "halt_reason": settled.halt_reason,
                    "settled_session": settled.settled_session,
                    "execution_session": settled.session,
                    "T": settled.T,
                },
                committed=False,
            )
            #  :     SymbolScopedError 
            problems = _verify_settlement_evidence(
                {
                    "symbol": symbol_config["symbol"],
                    "run_id": settled.settlement_run_id,
                    "mode": _tb().TRADE_MODE,
                    "evidence_path": evidence_path,
                    "halt_reason": settled.halt_reason,
                    "committed": False,
                }
            )
            # " "    ,    
            real_problems = [p for p in problems if " " not in p]
            if real_problems:
                raise _tb().SymbolScopedError(
                    f"{symbol_config['symbol']}   : " + " / ".join(real_problems)
                )
            settled.evidence_path = evidence_path
            settled.evidence_id = settled.settlement_run_id
            return settled
        #     (P1-E:    state  )
        evidence_path = _write_settlement_evidence(
            symbol_config["symbol"],
            {
                "symbol": symbol_config["symbol"],
                "run_id": settled.settlement_run_id,
                "mode": _tb().TRADE_MODE,
                "settled_session": settled.settled_session,
                "execution_session": settled.session,
                "T": settled.T,
                "net_invested": float(settled.state.get("net_invested", 0.0) or 0.0),
            },
            committed=(_tb().TRADE_MODE != "DRY"),
        )
        #  :     SymbolScopedError 
        problems = _verify_settlement_evidence(
            {
                "symbol": symbol_config["symbol"],
                "run_id": settled.settlement_run_id,
                "mode": _tb().TRADE_MODE,
                "evidence_path": evidence_path,
                "committed": _tb().TRADE_MODE != "DRY",
            }
        )
        # " "    ,    
        real_problems = [p for p in problems if " " not in p]
        if real_problems:
            raise _tb().SymbolScopedError(
                f"{symbol_config['symbol']}   : " + " / ".join(real_problems)
            )
        if not _tb().TRADE_MODE == "DRY":
            _tb().save_state(symbol_config["symbol"], settled.state)
        #  evidence  SettledState  
        settled.evidence_path = evidence_path
        settled.evidence_id = settled.settlement_run_id
        return settled


def _halt_settled_state(symbol_config, state, settlement_run_id, halt_reason):
    """    SettledState  ."""
    return SettledState(
        state=state,
        symbol=symbol_config["symbol"],
        exchange=symbol_config["exchange"],
        splits=symbol_config["splits"],
        symbol_type=symbol_config["symbol_type"],
        seed=float(state.get("seed", 0.0) or 0.0),
        effective_seed=float(state.get("effective_seed", 0.0) or 0.0),
        T=float(state.get("T", 0.0) or 0.0),
        reverse_mode=deepcopy(state.get("reverse_mode", {}) or {}),
        close_prices=list(state.get("close_prices", []) or []),
        position_basis=state.get("position_qty"),
        additional_loc_levels=symbol_config.get("additional_loc_levels", 3),
        settlement_run_id=settlement_run_id,
        session="",
        settled_session="",
        halt_reason=halt_reason,
    )


def settled_from_state(symbol_config, state=None, *, settlement_run_id=None, now=None):
    """ state  PREVIEW/APPROVE/EXECUTE  SettledState  .

    PREVIEW/APPROVE  state  ** **  id  .
     (evidence)  fail-closed   (P1-E).
    """
    symbol = symbol_config["symbol"]
    state = state if state is not None else _tb().load_state(symbol)
    if state.get("_state_unavailable"):
        raise RuntimeError(
            f"{symbol}    : {state.get('_state_unavailable')}"
        )
    assert_no_pending_fence(state, symbol)
    committed_run_id = str(state.get("last_settlement_id") or "")
    if committed_run_id:
        if settlement_run_id and str(settlement_run_id) != committed_run_id:
            raise RuntimeError(
                f"{symbol}  run_id : ={settlement_run_id}, "
                f"state  ={committed_run_id} - SETTLE   ."
            )
        if not evidence_file_for_run(symbol, committed_run_id):
            raise RuntimeError(
                f"{symbol}        "
                f"(run_id={committed_run_id}) - SETTLE   ."
            )
    else:
        # / : SETTLE      .
        #  SETTLE   last_settlement_id  .
        pass
    #  : state      ,   
    # session  ,     .
    state_session = str(state.get("execution_session") or "")
    settled_session = str(state.get("settled_session") or "")
    if state_session:
        execution_session = state_session
    else:
        _settled_session, expected_session = settlement_sessions(now)
        if not _settled_session or not expected_session:
            raise RuntimeError(
                f"{symbol} /     - SETTLE   ."
            )
        execution_session = expected_session
        print(
            f"[] {symbol} state       "
            f"(execution={execution_session})."
        )
    if not settled_session:
        settled_session, _execution_session = settlement_sessions(now)
        if settled_session:
            print(
                f"[] {symbol} state       "
                f"(settled_session={settled_session})."
            )
        else:
            print(
                f"[] {symbol}       - "
                f"settled_session   ."
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
        evidence_id=committed_run_id or settlement_run_id,
        evidence_path=evidence_file_for_run(symbol, committed_run_id or settlement_run_id)
        or (f"settlement-{symbol}-{settlement_run_id}.json" if settlement_run_id else ""),
        session=execution_session,
        settled_session=settled_session,
    )


def build_order_plan(settled, broker, *, net_invested_overlay=None):
    """       (/ ).

    `net_invested_overlay`  **EXECUTE  **   .
    SETTLE        `net_invested`  
         .  state// 
     ( SETTLE   ).
    """
    tb = _tb()
    symbol = settled.symbol
    exchange = settled.exchange
    splits = settled.splits
    symbol_type = settled.symbol_type
    seed = settled.seed
    #  : SETTLE   effective_seed  PREVIEW/APPROVE  .
    if tb.REINVEST and (settled.effective_seed or 0.0) > 0:
        seed = settled.effective_seed
    additional_loc_levels = settled.additional_loc_levels
    state = settled.state
    T = settled.T
    assert_no_pending_fence(state, symbol)
    #  state(close_prices/reverse_mode)  ****  .
    state_hash_at_plan = canonical_state_hash(state)
    session = settled.session or settlement_sessions()[1] or _et_today()
    settlement_run_id = settled.settlement_run_id or _resolve_settlement_run_id()

    #  Step 2:   
    print("\n[Step 2]   ...")

    #  state   ,    state  
    # state_hash  .     state_patch 
    #  (PREVIEW/APPROVE/EXECUTE  hash  ).
    strategy_state = deepcopy(state)
    if net_invested_overlay is not None:
        #      -  state/hash  .
        strategy_state["net_invested"] = float(net_invested_overlay)
        print(
            f"  [EXECUTE]   :     "
            f"net_invested ${float(state.get('net_invested', 0.0) or 0.0):.2f}  "
            f"${float(net_invested_overlay):.2f} ( )"
        )
    reverse_before = deepcopy(state.get("reverse_mode", {}) or {})

    strategy_result = tb._V4(
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

    print("   ")
    print(f"  : ${last_price}")
    print(f"   : {strategy_result['position_qty']}")
    print(f"  : ${strategy_result['avg_price']}")
    print(f"    : ${strategy_result['orderable_cash']:.2f}")
    print(f"  T: {T} / {splits}")
    if strategy_result['star_point']:
        print(f"  : ${strategy_result['star_point']:.2f}")

    rev = strategy_state.get("reverse_mode", {})
    if rev.get("day_count", 0) > 0:
        print(f"  [] {rev['day_count']}  ")

    orders = strategy_result["orders"]

    # BUY LOC 1.19          .
    # (trading_bot        )
    for note in strategy_result.get("price_cap_notes") or []:
        tb.notify(
            f"{symbol}  LOC  \n: ${note['from']:.2f}\n"
            f"  : ${note['to']:.2f}"
        )

    print(f"\n[Step 3]    ({len(orders)})")
    print("-" * 60)

    if len(orders) == 0:
        print("  .")

    for i, order in enumerate(orders, 1):
        print(f"\n {i}:")
        print(f"  : {order['comment']}")
        print(f"  /: {order['side']}")
        print(f"   : {order['order_type']}")
        print(f"  : {order['quantity']}")
        if order["price"]:
            print(f"  : ${order['price']}")
        else:
            print("  : ")

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
