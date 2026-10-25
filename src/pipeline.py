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
import os
import tempfile
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

from state import (
    _has_unconfirmed_amount_fills,
    _parse_processed_qty_marker,
    canonical_state_hash,
    compute_position_from_history,
    reset_settlement_ledger,
    update_T_from_history,
)

APPROVALS_DIR = os.getenv("APPROVALS_DIR", "approvals")
PLAN_DIR = os.getenv("PLAN_DIR", "plans")
# v2: created_at_utc/expires_at_utc 를 plan_hash 에 포함하고, account_id 는
#     원문 대신 HMAC-SHA256 지문 + 마스킹 표시값만 저장합니다.
#     v1 파일은 명시적으로 거부합니다. (P2-H / P2-D)
APPROVAL_SCHEMA_VERSION = 2
PLAN_SCHEMA_VERSION = 2
DEFAULT_APPROVAL_TTL_SECONDS = int(os.getenv("APPROVAL_TTL_SECONDS", "32400"))
DEFAULT_MIN_TTL_SECONDS = int(os.getenv("APPROVAL_MIN_TTL_SECONDS", "28800"))


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
            "orders": deepcopy(self.orders),
            # 승인된 상태 변경(close_prices/reverse_mode) — 주문 자체는 orders 에만.
            "state_patch": deepcopy(self.state_patch),
            "consumed": False,
        }


def compute_plan_hash(
    plan_id, symbol, exchange, session, state_hash, settlement_run_id, orders,
    state_patch=None, evidence_id=None, broker=None, broker_mode=None,
    account_id=None, created_at_utc=None, expires_at_utc=None,
    account_display=None,
):
    """승인/계획 파일의 주문 내용 + 실행 바인딩에 대한 안정적 지문(SHA-256).

    `created_at_utc`/`expires_at_utc` 도 포함합니다. 승인 만료시각을 파일에서
    직접 늘려도 plan_hash 가 깨지므로 위조할 수 없습니다 (P2-H).
    `account_display` 는 표시용 값이지만 파일 변조를 막기 위해 해시에 포함합니다.
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


def _broker_identity():
    """승인 파일에 묶을 브로커/계좌 식별자(비밀값 제외)를 반환합니다.

    account_id 는 원문 계좌번호가 아니라 HMAC-SHA256 지문이고,
    account_display 는 사람이 대조하기 위한 **마스킹 표시값**입니다 (P2-D).
    비교는 지문끼리 하므로 승인 파일이 유출되어도 계좌번호가 노출되지 않습니다.
    """
    tb = _tb()
    raw_account = ""
    try:
        from config import BROKER_CONFIG

        cfg = BROKER_CONFIG or {}
        raw_account = str(cfg.get("account_no") or cfg.get("account_seq") or "")
    except Exception:
        raw_account = ""
    return {
        "broker": str(getattr(tb, "BROKER", "") or ""),
        "broker_mode": str(getattr(tb, "BROKER_MODE", "") or ""),
        "account_id": _account_fingerprint(raw_account),
        "account_display": _mask_account_id(raw_account),
    }


def _resolve_ttl(ttl_seconds=None, min_ttl_seconds=None):
    """승인 TTL 을 결정하고 운영 간격보다 짧으면 fail-closed 합니다."""
    ttl = DEFAULT_APPROVAL_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    min_ttl = (
        DEFAULT_MIN_TTL_SECONDS
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
        "orders": deepcopy(plan.orders),
        "state_patch": deepcopy(plan.state_patch),
    }
    path = plan_path(plan.plan_id, plans_dir)
    _atomic_write_json(path, payload)
    print(f"[PREVIEW] {plan.symbol} 계획 기록: {path} (orders={len(plan.orders)})")
    return path


def approve_order_plan(plan, approvals_dir=None, now=None, ttl_seconds=None):
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
    )
    if plan_data.get("plan_hash") != expected:
        raise ValueError(
            "계획 파일 plan_hash 불일치 — 변조되었거나 구버전(v1) 계획입니다. "
            "PREVIEW 를 다시 실행하세요."
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
        assert_reconciliation_current(symbol, state, history, balance)

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

    current_session = _et_today(now)
    if approval.get("session") != current_session:
        problems.append(
            f"세션 불일치: 승인 {approval.get('session')} != 오늘 {current_session}"
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
    )
    if approval.get("plan_hash") != expected:
        problems.append(
            "plan_hash 불일치: 승인 파일의 주문 내용이 변조되었거나 손상되었습니다."
        )
    return problems


EXECUTION_MARKER_SUFFIX = ".executed.json"
OUTCOME_RECORD_SUFFIX = ".recovery.json"
# 실행이 시작된(=이미 소진된) 승인 결과. approval-skipped 는 재시도 가능하므로 제외.
_TERMINAL_APPROVAL_OUTCOMES = {"executed", "execution-failed"}


def execution_marker_path(approval_path):
    """승인 실행 마커 경로 — 한 번 실행된 승인은 다시 실행할 수 없습니다."""
    return f"{approval_path}{EXECUTION_MARKER_SUFFIX}"


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


def _prepare_plan_identity(plan, now=None, ttl_seconds=None):
    """plan_id / session / 만료 / 브로커 바인딩 / plan_hash 를 확정합니다."""
    tb = _tb()
    current = now or tb.datetime.now(ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    ttl = _resolve_ttl(ttl_seconds)
    created = _utc_now_iso(current)
    expires = (current.astimezone(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    session = plan.session or _et_today(current)
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
    plan.plan_id = f"{plan.symbol}-{session}-{run_id}"
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
    flag = os.getenv("HISTORY_SETTLEDNESS_CHECK", "").strip().lower()
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
                f"워터마크 주문 {watermark_ordno} 이 이력에 없습니다 "
                "(이력 누락/정착 지연)"
            )
        else:
            row_ts = _parse_iso(by_odno[watermark_ordno].get("ord_datetime_utc"))
            if row_ts is not None and row_ts.astimezone(timezone.utc) < watermark_ts:
                problems.append(
                    f"워터마크 주문 {watermark_ordno} 의 시각이 state 보다 이전입니다 "
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
                f"orders_meta 주문 {odno} 이 이력에 없습니다 "
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
        return False, f"이력 재조회 실패: {error}"
    if _history_snapshot(repeated) != _history_snapshot(history):
        return False, "이력 반복 조회 결과가 달라졌습니다 (정착 지연 의심)"
    completeness = _verify_history_complete(state, repeated, history_days)
    if completeness:
        return False, " / ".join(completeness[:3])
    if _history_has_recent_open_order(repeated):
        return False, "세션 종료 후 최근 미종결 주문이 이력에 남아 있습니다"
    return True, ""


def settle_positions(broker, symbol_config, *, persist=True):
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
    settlement_run_id = _resolve_settlement_run_id()
    session = _et_today()

    if not broker.is_trading_day():
        today_str = tb.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %A")
        print(f"  📅 오늘({today_str})은 미국 증시 휴장일입니다. 이 종목을 건너뜁니다.")
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
            halt_reason="non_trading_day",
        )

    settlement_evidence = None
    evidence_path = None
    cycle_report = None
    next_cycle_seed = None

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
        )

    def _persist():
        """검증이 끝난 정산 결과를 **마지막에 1회만** 저장합니다 (P1-D).

        - LIVE: 정산 결과 캐시 저장
        - DRY + FORCE_T: 사용자 명시 보정이므로 1회 저장 (기존 동작 유지)
        """
        should_save = persist and (
            tb.TRADE_MODE == "LIVE" or force_t is not None
        )
        if should_save:
            tb.save_state(symbol, state)
            return True
        return False

    # ── Step 1: T값 로드 및 어제 체결 반영 ──────────────────────
    print("\n[Step 1] T값 로드 중...")

    state = tb.load_state(symbol)

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

    # ── T값 강제 설정 (FORCE_T) ──
    if force_t is not None:
        if tb._has_unresolved_reverse_orders(state):
            state.setdefault("reverse_mode", {})["reconciliation_only"] = True
            state["reverse_mode"]["active"] = False
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
            state["last_updated"] = tb.datetime.now(ZoneInfo("UTC")).isoformat()
            state["last_processed_ordno"] = ""
        if force_t == 0:
            state["cycle_start_date"] = ""
            state["net_invested"] = 0.0
            state["net_invested_status"] = "valid"
        print(f"[T 보정] {symbol} FORCE_T={force_t} 적용 (이전 T={old_T}), "
              f"orders_meta/balance_mismatch 초기화, last_updated 갱신")
        # P1-D: 여기서 저장하지 않습니다. 사이클 종료 처리·잔고 교차검증 등
        # 남은 검증이 모두 끝난 뒤 `_persist()` 에서 **1회만** 저장합니다.
        # (조기 저장하면 이후 검증 실패 시 보정된 T 가 캐시에 고정됩니다)

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
    if live_persist:
        state["last_settlement_id"] = settlement_run_id
        state["last_settlement_evidence"] = evidence_path
        state["last_settlement_committed_at_utc"] = _utc_now_iso()
    saved = _persist()
    if saved and live_persist:
        try:
            _mark_evidence_committed(evidence_path, settlement_evidence, True)
        except OSError as commit_error:
            # state 는 저장됐지만 evidence 승격에 실패했습니다. 이 경우 PREVIEW 의
            # settled_from_state() 가 evidence 부재로 거부하므로 fail-closed 입니다.
            print(
                f"[경고] {symbol} 정산 근거 committed 표시 실패 — "
                f"PREVIEW/VERIFY 가 보류됩니다: {str(commit_error)[:160]}"
            )
    else:
        # DRY/미저장 실행(FORCE_T 포함): 근거를 사용할 수 없게 표시합니다.
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
        session=_et_today(),
    )


def build_order_plan(settled, broker):
    """정산 결과로 전략을 실행해 주문 계획을 만듭니다 (저장/주문 없음)."""
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
    session = settled.session or _et_today()
    settlement_run_id = settled.settlement_run_id or _resolve_settlement_run_id()

    # ── Step 2: 전략 실행 ───────────────────────────────────────
    print("\n[Step 2] 전략 실행 중...")

    # 전략은 state 를 읽기만 하지만, 계획 단계가 정산 state 를 변형하면
    # state_hash 가 흔들립니다. 항상 복사본에서 실행하고 변경분은 state_patch 로만
    # 넘깁니다 (PREVIEW/APPROVE/EXECUTE 간 hash 일치 보장).
    strategy_state = deepcopy(state)
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

    session = settled.session or _et_today()
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
