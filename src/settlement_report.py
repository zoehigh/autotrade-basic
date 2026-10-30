"""
일일 정산 리포트 — SETTLE 결과를 사람이 읽는 요약으로 만들고 텔레그램으로 1회 발송합니다.

설계 원칙
--------
1. **읽기 전용 집계**: 정산 계산에는 전혀 관여하지 않습니다. 입력은 이미 커밋된
   SETTLE 결과(`SettledState`)와 정산 근거(evidence)뿐이고, 출력은 딕셔너리/문자열입니다.
2. **집계 단위는 "이번 정산 반영분(델타)"**: 정산은 주문별로 `qty_delta`/`amount_delta`
   (이번 정산이 **새로 반영한 증가분**)를 남깁니다. `cumulative_qty`(누적 체결)를 합산하면
   부분체결이 중복 집계되므로 이 모듈은 델타만 더합니다.
   - **초기 전체 재추정**(`full_reinference=true`): 델타가 곧 "전 기간 반영분"이므로 그대로
     합산하되 메시지에 "전체 재추정" 표시를 남깁니다 (하루치 매매처럼 보이지 않게).
   - **지연 체결**(`late=true`): 이미 반영된 주문의 누적 체결이 늘어난 경우로, 역시 델타만
     반영됩니다. 합산하되 "지연 체결 N건"으로 따로 표시합니다.
3. **발송은 정산의 부수 기능**: 텔레그램 장애가 T·순투입금·state 저장/롤백을 유발하면 안
   됩니다. 공개 함수는 예외를 던지지 않고, 실패는 "발송 실패(재시도 가능)" 상태로만 남깁니다.
4. **중복 방지 = state 내 전용 키**: GitHub hosted 러너는 실행마다 새 작업 디렉터리라 로컬
   파일은 사라지고, 재시작 후에도 유지되는 저장소는 `.state.json`(Actions 캐시)뿐입니다.
   그래서 발송 상태를 종목별 state 의 `settlement_report` 키에 남깁니다.
   이 키는 거래 원장(T/orders_meta/정산 원장)과 완전히 분리되어 있고
   `canonical_state_hash()` 계산에도 **포함하지 않습니다** — 발송 때문에 복구용 CAS 지문이
   흔들리면 안 되기 때문입니다.
5. **미발송 내용 보존 = `pending`**: 발송에 실패하면 그 리포트의 **본문 재료(payload)** 를
   state 의 `pending` 에 남기고, 다음 재시도 실행에서 **그 내용을 그대로** 보냅니다.
   왜 필요한가: 정산은 이미 커밋됐으므로 다음 SETTLE 은 델타가 0인 "빈 리포트"를 만듭니다.
   저장해 두지 않으면 "재시도했다"는 사실만 남고 운영자는 실제 반영분을 영영 못 봅니다.
   보류(hold) 알림은 재시도 시에도 같은 사유로 재생성되므로 저장하지 않습니다
   (보류가 해소돼 커밋되면 새 성공 리포트가 나가는 것이 맞습니다).
6. **정산 세션 바인딩**: 보류/실패 알림의 억제 키에는 **호출측이 의도한 정산 세션**이 들어갑니다.
   `settled=None`(정산 예외)이나 `session_calendar_unavailable` 처럼 결과 객체에 세션이
   없는 경로에서 세션이 비면, 같은 사유가 **날짜를 넘어 영구 억제**되어 운영자는 첫 실패만
   보게 됩니다. 세션을 계산할 수 없을 때는 UTC 날짜 버킷(`unknown:YYYY-MM-DD`)으로
   **하루 단위** 억제합니다.

멱등 식별자
----------
    broker + account_fingerprint + symbol + (settled_session 또는 세션 버킷) + report_version

계좌 지문(`account_fingerprint`)이 비면 계좌 구분이 사라져 서로 다른 계좌가 같은 멱등 키를
공유합니다. 그래서 **지문이 없으면 발송도 상태 기록도 하지 않고** 명시적으로 경고합니다
(조용히 섞이면 한 계좌의 리포트가 다른 계좌의 리포트를 억제합니다).

조용한 시간
----------
정산 리포트는 하루 1회 운영 요약이고 SETTLE 은 KST 새벽(=조용한 시간)에 실행됩니다.
`notify()` 는 조용한 시간에 건너뛰어도 `False` 를 돌려주므로 **건너뜀과 실패를 구분할 수
없습니다**. 그대로 두면 "항상 실패 → 영원한 재시도"가 됩니다. 그래서 기본 발송자는
`urgent=True` 로 보냅니다 (건너뜀 없음 → `False` 는 실제 실패 = 재시도 대상).

Telegram Bot API 에는 멱등 키가 없습니다. 그래서 "전송 성공 → 발송 상태 저장" 사이에
프로세스가 죽으면 같은 세션에서 한 번 더 발송될 수 있습니다. 창이 매우 좁고(전송 직후
저장) 피해는 중복 알림 1건이라 문서화된 한계로 남깁니다.

보류/실패 시에는 **성공 리포트를 만들지 않습니다**. 대신 사유와 "신규 주문 차단" 상태를
알립니다 (정산이 확정되지 않았는데 완료처럼 보이면 안 됩니다). 휴장(`non_trading_day`)은
정산 대상이 없으므로 호출측에서 리포트 대상에서 제외합니다.

채널은 `notifier.notify()` 가 결정합니다(개인 채팅/공용 채널 구분). 메시지에 원문
주문번호·계좌번호가 없으므로 두 채널에 같은 본문을 씁니다.

개인정보(주문번호·계좌번호)는 메시지에 넣지 않습니다. 사유 문자열에 주문번호가 섞일 수
있으므로 호출측이 `mask_odno` 계열로 마스킹한 뒤 넘깁니다 (이 모듈은 집계·표기만 담당).
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from notifier import notify

# 리포트 포맷/집계 규칙이 바뀌면 올립니다 (멱등 식별자에 포함 — 구버전 리포트와 구분).
# v2: 미발송 payload 재전송(pending)·전체 재추정/추정 금액 요약 표기·세션 버킷 도입.
REPORT_VERSION = 2

# state 안에서 발송 상태를 보관하는 전용 키 (거래 원장과 분리).
REPORT_STATE_KEY = "settlement_report"

# state 파일이 무한정 커지지 않도록 보관 개수를 제한합니다.
MAX_SENT_ENTRIES = 20
MAX_HOLD_ENTRIES = 10
# 미발송(pending) payload — 재시도 전에 사라지면 안 되지만 무한정 쌓이지도 않게 제한합니다.
MAX_PENDING_ENTRIES = 10

# 텔레그램 메시지 길이 제한(4096)보다 여유 있게 자릅니다.
MAX_MESSAGE_CHARS = 3900

REASON_UNCOMMITTED = "정산 미확정 (커밋된 근거 없음)"
REASON_ERROR = "정산 실행 실패"


# ── 작은 숫자 유틸 ────────────────────────────────────────────────
def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _now_iso(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(amount):
    return f"${_as_float(amount):,.2f}"


def session_bucket(settled_session, now=None):
    """멱등/억제 키에 쓰는 세션값 — 세션을 모르면 **UTC 날짜 버킷**으로 대체합니다.

    `settled=None`(정산 예외)이나 `session_calendar_unavailable` 처럼 정산 세션이
    비는 경로가 있습니다. 그때 키를 그대로 비워 두면 같은 사유가 **날짜를 넘어 영구
    억제**되어 운영자는 첫 실패만 보게 됩니다. 그래서 하루 단위 버킷으로 묶습니다
    (`unknown:YYYY-MM-DD` — 같은 날 재실행은 억제, 다음 날은 다시 알림).
    """
    session = str(settled_session or "").strip()
    if session:
        return session
    return f"unknown:{_now_iso(now)[:10]}"


def _order_amount_estimated(order, orders_meta, estimated_odnos):
    """이 주문의 `amount_delta` 가 **추정치일 수 있는지** (보수적으로 판정).

    정산은 금액 미확정 체결을 추정하지 않고 보류하지만, 다음 예외 경로가 있습니다.
      1. `evidence.orders` 에 추정 플래그가 실린 경우 (`amount_estimated`)
      2. `evidence.fills` 가 금액 추정으로 표시한 주문 (`amount_confirmed=false`)
      3. 레거시 메타(반영금액 표식 없음) — 신규분을 누적금액 비율로 1회 추정
    확인할 수 없는 추정(FORCE_T_REINFERENCE 의 내부 추정값)은 여기서 잡히지 않으므로,
    이 표기는 "추정일 수 있는 것이 확인된 경우"만 알립니다.
    """
    if not isinstance(order, dict):
        return False
    if order.get("amount_estimated") or order.get("amount_inferred"):
        return True
    odno = str(order.get("odno") or "")
    if odno and odno in estimated_odnos:
        return True
    meta = (orders_meta or {}).get(odno)
    if isinstance(meta, dict) and "processed_filled_amount" not in meta:
        # 이전 버전 메타 — 반영금액 표식이 없어 신규분을 비율로 추정합니다.
        return True
    return False


def _estimated_odnos(evidence):
    """`evidence.fills` 에서 금액이 추정으로 표시된 주문번호를 모읍니다."""
    odnos = set()
    for fill in (evidence or {}).get("fills") or []:
        if not isinstance(fill, dict):
            continue
        if fill.get("amount_estimated") or fill.get("amount_confirmed") is False:
            odnos.add(str(fill.get("odno") or ""))
    return odnos


# ── 정산 델타 집계 ────────────────────────────────────────────────
def order_totals(orders):
    """evidence `orders`(= `state["_settlement_deltas"]`)를 델타 기준으로 합산합니다.

    **`qty_delta`/`amount_delta` 만** 씁니다. `cumulative_qty` 는 누적 체결이라
    부분체결(예: 누적 7주 중 이번 2주만 새로 반영)에서 중복 집계됩니다.

    Returns:
        dict: buy_qty/buy_amount/sell_qty/sell_amount/order_count/
              late_fill_count/full_reinference
    """
    totals = {
        "buy_qty": 0,
        "buy_amount": 0.0,
        "sell_qty": 0,
        "sell_amount": 0.0,
        "order_count": 0,
        "late_fill_count": 0,
        "full_reinference": False,
    }
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        qty = _as_int(order.get("qty_delta"), 0)
        amount = _as_float(order.get("amount_delta"), 0.0)
        if qty <= 0 and amount <= 0:
            continue
        totals["order_count"] += 1
        # 정산은 `side` 를 "매수"/"매도" 한글 라벨로 남깁니다 (영문 라벨도 허용).
        side = str(order.get("side") or "").strip().upper()
        if side.startswith("매도") or side in ("SELL", "S"):
            totals["sell_qty"] += qty
            totals["sell_amount"] += amount
        else:
            totals["buy_qty"] += qty
            totals["buy_amount"] += amount
        if order.get("late"):
            totals["late_fill_count"] += 1
        if order.get("full_reinference"):
            totals["full_reinference"] = True
    totals["buy_amount"] = round(totals["buy_amount"], 2)
    totals["sell_amount"] = round(totals["sell_amount"], 2)
    return totals


# ── 멱등 식별자 ───────────────────────────────────────────────────
def report_id(broker, account_fingerprint, symbol, settled_session):
    """(broker, 계좌지문, 종목, 정산세션, 리포트 버전)의 SHA-256 앞 16자리.

    `settled_session` 에는 `session_bucket()` 결과(세션 또는 날짜 버킷)를 넘깁니다.
    계좌 원문은 쓰지 않습니다 (지문은 이미 HMAC-SHA256). 길이를 줄여 state 를
    가볍게 유지하고, 구성 요소는 state 에 함께 남겨 디버깅할 수 있게 합니다.
    """
    raw = "|".join(
        [
            str(broker or "").strip().lower(),
            str(account_fingerprint or "").strip(),
            str(symbol or "").strip().upper(),
            str(settled_session or "").strip(),
            f"v{REPORT_VERSION}",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _reason_hash(reason):
    return hashlib.sha256(str(reason or "").encode("utf-8")).hexdigest()[:8]


def hold_key(report):
    """보류 알림용 키 — 같은 사유는 세션당 1회, 사유가 바뀌면 다시 알립니다."""
    return f"{report.report_id}:{_reason_hash(report.halt_reason)}"


# ── 종목별 리포트 ─────────────────────────────────────────────────
@dataclass
class SymbolReport:
    """한 종목의 정산 리포트 한 줄 묶음 (읽기 전용 스냅샷)."""

    symbol: str
    exchange: str = ""
    # 화면 표시용 정산 세션 (모르면 "" → "미확인")
    settled_session: str = ""
    # 멱등/억제 키용 세션값 — `session_bucket()` 결과 (세션 또는 날짜 버킷)
    session_key: str = ""
    report_id: str = ""
    broker: str = ""
    # 정산이 확정(커밋)됐는지 / 보류·실패인지
    committed: bool = False
    halted: bool = False
    halt_reason: str = ""
    # T / 순투입 변화 (정산 전 → 확정 후)
    t_before: float = 0.0
    t_after: float = 0.0
    net_before: float = 0.0
    net_after: float = 0.0
    # 이번 정산 반영분 (델타 합산)
    buy_qty: int = 0
    buy_amount: float = 0.0
    sell_qty: int = 0
    sell_amount: float = 0.0
    # 확정 보유수량 (브로커 잔고 기준, 없으면 state 기준)
    position_qty: Optional[int] = None
    late_fill_count: int = 0
    full_reinference: bool = False
    # 미확정 체결금액/마커 건수 (요약에 표시)
    unconfirmed_amount_count: int = 0
    unconfirmed_marker_count: int = 0
    # 금액이 추정일 수 있는 반영분 건수 (표기용 — "(일부 추정)" 보강)
    estimated_amount_count: int = 0
    # 이번 실행에서 이미 발송했는지 (중복 방지 판정 결과)
    already_sent: bool = False
    # 1차 발송 실패분(pending)을 이번에 재전송하는지 (본문에 표시)
    retried: bool = False

    @property
    def is_hold(self):
        """성공 리포트를 보내면 안 되는 상태(보류·미확정·실패)인지."""
        return bool(self.halted or not self.committed)

    @property
    def outcome(self):
        return "hold" if self.is_hold else "committed"

    def has_activity(self):
        return bool(self.buy_qty or self.sell_qty or self.buy_amount or self.sell_amount)


def _committed_values(settled, evidence):
    """확정 T/순투입 — 후처리(MAX_T·사이클 종료)가 반영된 최종 후보를 우선합니다.

    우선순위: 후처리 최종 후보(`post_processing.final_candidate`) → 정산 결과 객체
    (`settled`) 값 → 근거의 정산 시점 값. 후처리가 없으면 객체 값이 이미 최종값이고,
    객체에 필드가 없으면(테스트/구버전) 근거 값으로 물러납니다.
    """
    evidence = evidence or {}
    final = (evidence.get("post_processing") or {}).get("final_candidate") or {}
    state = getattr(settled, "state", None) or {}

    if "T" in final:
        t_after = _as_float(final.get("T"))
    elif hasattr(settled, "T"):
        t_after = _as_float(getattr(settled, "T"))
    else:
        t_after = _as_float(evidence.get("T_after"))

    if "net_invested" in final:
        net_after = _as_float(final.get("net_invested"))
    elif "net_invested" in state:
        net_after = _as_float(state.get("net_invested"))
    else:
        net_after = _as_float(evidence.get("net_invested_after"))
    return t_after, net_after


def build_symbol_report(
    settled=None,
    *,
    symbol="",
    exchange="",
    error="",
    committed=False,
    broker="",
    account_fingerprint="",
    settled_session=None,
    now=None,
):
    """SETTLE 결과 하나를 리포트 객체로 변환합니다 (계산/저장 없음).

    `settled` 가 None 이면 정산 예외(실패)로 취급합니다. `committed` 는 호출측이
    **커밋된 근거(committed=true)** 를 확인한 뒤 넘겨야 합니다 — 커밋되지 않은
    정산으로 성공 리포트를 만들면 안 되기 때문입니다.

    `settled_session` 은 **호출측이 의도한 정산 세션**입니다 (독립 인자). 정산 예외나
    `session_calendar_unavailable` 처럼 결과 객체에 세션이 없을 때, 이 값이 없으면
    멱등/억제 키가 비어 같은 사유가 날짜를 넘어 영구 억제됩니다. 결과 객체의 세션이
    있으면 그쪽(실제 정산 세션)을 우선하고, 둘 다 없으면 UTC 날짜 버킷을 씁니다.
    """
    session_hint = str(settled_session or "").strip()
    symbol = str(symbol or getattr(settled, "symbol", "") or "").strip().upper()
    exchange = str(exchange or getattr(settled, "exchange", "") or "")
    actual_session = str(getattr(settled, "settled_session", "") or "").strip()
    # 키에 쓰는 세션: 실제 정산 세션 → 호출측 의도 세션 → UTC 날짜 버킷
    session_key = session_bucket(actual_session or session_hint, now)
    evidence = (getattr(settled, "evidence", None) if settled is not None else None) or {}
    halt_reason = str(getattr(settled, "halt_reason", "") or "")

    report = SymbolReport(
        symbol=symbol,
        exchange=exchange,
        settled_session=actual_session,
        session_key=session_key,
        broker=str(broker or ""),
        report_id=report_id(broker, account_fingerprint, symbol, session_key),
    )

    if settled is None and error:
        report.halted = True
        report.halt_reason = f"{REASON_ERROR}: {error}"[:300]
        return report

    if halt_reason:
        report.halted = True
        report.halt_reason = halt_reason[:300]
        return report

    if not committed:
        # 근거가 커밋되지 않았습니다 (DRY/보류/승격 실패) → 성공 리포트 금지.
        report.halted = True
        report.halt_reason = (
            evidence.get("hold_reason") or REASON_UNCOMMITTED
        )[:300]
        return report

    totals = order_totals(evidence.get("orders"))
    t_after, net_after = _committed_values(settled, evidence)
    position_qty = evidence.get("balance_qty")
    if position_qty is None:
        position_qty = (getattr(settled, "state", None) or {}).get("position_qty")

    report.committed = True
    report.buy_qty = totals["buy_qty"]
    report.buy_amount = totals["buy_amount"]
    report.sell_qty = totals["sell_qty"]
    report.sell_amount = totals["sell_amount"]
    report.late_fill_count = totals["late_fill_count"]
    report.full_reinference = totals["full_reinference"]
    report.t_before = _as_float(evidence.get("T_before"), getattr(settled, "T", 0.0))
    report.t_after = t_after
    report.net_before = _as_float(evidence.get("net_invested_before"), 0.0)
    report.net_after = net_after
    report.position_qty = (
        None if position_qty is None else _as_int(position_qty, 0)
    )
    report.unconfirmed_amount_count = len(evidence.get("pending_amount_odnos") or []) + len(
        evidence.get("skipped_fills") or []
    )
    report.unconfirmed_marker_count = len(evidence.get("pending_marker_odnos") or [])
    # 금액이 추정일 수 있는 반영분 — 표기용(일부 추정) 판정. evidence 에 표식이 없는
    # 추정 경로는 확인할 수 없으므로 확인 가능한 경우만 셉니다.
    estimated_odnos = _estimated_odnos(evidence)
    orders_meta_before = evidence.get("orders_meta_before") or {}
    estimated_count = 0
    for order in evidence.get("orders") or []:
        if not isinstance(order, dict):
            continue
        qty = _as_int(order.get("qty_delta"), 0)
        amount = _as_float(order.get("amount_delta"), 0.0)
        if qty <= 0 and amount <= 0:
            continue
        if _order_amount_estimated(order, orders_meta_before, estimated_odnos):
            estimated_count += 1
    report.estimated_amount_count = estimated_count
    return report


# ── 전체 요약 ─────────────────────────────────────────────────────
def build_daily_report(reports, now=None):
    """종목별 리포트를 일일 요약으로 묶습니다 (실현손익·평가손익은 다루지 않습니다)."""
    reports = list(reports or [])
    sessions = sorted({r.settled_session for r in reports if r.settled_session})
    committed = [r for r in reports if not r.is_hold]
    holds = [r for r in reports if r.is_hold]
    return {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _now_iso(now),
        "settled_session": sessions[0] if len(sessions) == 1 else ", ".join(sessions),
        "reports": reports,
        "completed_count": len(committed),
        "hold_count": len(holds),
        "unconfirmed_amount_count": sum(r.unconfirmed_amount_count for r in reports),
        "unconfirmed_marker_count": sum(r.unconfirmed_marker_count for r in reports),
        # 하루치 매매처럼 보이지 않도록 요약에도 "전체 재추정 포함"을 남깁니다.
        "full_reinference_symbols": [r.symbol for r in committed if r.full_reinference],
        "estimated_amount_count": sum(
            getattr(r, "estimated_amount_count", 0) or 0 for r in committed
        ),
        "retried_symbols": [r.symbol for r in reports if getattr(r, "retried", False)],
        "buy_qty": sum(r.buy_qty for r in committed),
        "buy_amount": round(sum(r.buy_amount for r in committed), 2),
        "sell_qty": sum(r.sell_qty for r in committed),
        "sell_amount": round(sum(r.sell_amount for r in committed), 2),
    }


# ── 메시지 포맷 ───────────────────────────────────────────────────
def _format_symbol_section(report):
    head = f"■ {report.symbol}"
    if report.exchange:
        head += f" ({report.exchange})"
    if report.is_hold:
        lines = [f"{head} — 보류 (신규 주문 차단)"]
        if report.halt_reason:
            lines.append(f"  사유: {report.halt_reason}")
        return "\n".join(lines)

    lines = [f"{head} — 반영 완료"]
    # ⚠️ `ord_dt` 는 **주문일**(미국 영업일)이라 "전일 체결"로 단정할 수 없습니다.
    lines.append(
        f"  이번 정산 반영분: 매수 {report.buy_qty}주 / {_money(report.buy_amount)}"
        f" · 매도 {report.sell_qty}주 / {_money(report.sell_amount)}"
    )
    lines.append(
        f"  T {report.t_before:g} → {report.t_after:g}"
        f" · 순투입 {_money(report.net_before)} → {_money(report.net_after)}"
    )
    if report.position_qty is not None:
        lines.append(f"  확정 보유: {report.position_qty}주")
    notes = []
    if getattr(report, "retried", False):
        notes.append("1차 발송 실패분 재전송")
    if report.late_fill_count:
        notes.append(f"지연 체결 {report.late_fill_count}건 반영")
    if report.full_reinference:
        notes.append("전체 재추정(전 기간 반영 포함, 하루치 매매 아님)")
    if getattr(report, "estimated_amount_count", 0):
        notes.append(
            f"금액 일부 추정({report.estimated_amount_count}건 — 확정 금액 아님)"
        )
    if report.unconfirmed_amount_count:
        notes.append(f"미확정 체결금액 {report.unconfirmed_amount_count}건")
    if report.unconfirmed_marker_count:
        notes.append(f"마커 미확정 {report.unconfirmed_marker_count}건")
    if notes:
        lines.append("  * " + ", ".join(notes))
    return "\n".join(lines)


def format_report_message(daily):
    """일일 리포트 메시지 (주문번호·계좌번호 없음, 집계·가격/수량만)."""
    daily = daily or {}
    reports = list(daily.get("reports") or [])
    session = str(daily.get("settled_session") or "").strip()
    header = ["📊 일일 정산 리포트"]
    header.append(f"정산 세션: {session}" if session else "정산 세션: 미확인")
    if daily.get("generated_at_utc"):
        header.append(f"생성: {daily['generated_at_utc']} (UTC)")
    header.append("※ 금액은 '이번 정산 반영분' 기준입니다 (실현/평가손익 제외)")

    sections = [line for line in header if line]
    already = [r for r in reports if getattr(r, "already_sent", False)]
    for report in reports:
        # 이미 발송된 종목은 같은 세션에서 다시 그리지 않습니다 (재발송 혼동 방지).
        if getattr(report, "already_sent", False):
            continue
        sections.append("")
        sections.append(_format_symbol_section(report))
    if already:
        sections.append("")
        sections.append(
            f"(이미 발송된 종목 {len(already)}건은 표시를 생략: "
            + ", ".join(r.symbol for r in already)
            + ")"
        )

    summary = [
        "",
        f"합계: 완료 {daily.get('completed_count', 0)} / "
        f"보류 {daily.get('hold_count', 0)} 종목",
        f"  매수 {daily.get('buy_qty', 0)}주 / {_money(daily.get('buy_amount', 0.0))}"
        f" · 매도 {daily.get('sell_qty', 0)}주 / {_money(daily.get('sell_amount', 0.0))}",
        f"  미확정 체결금액 {daily.get('unconfirmed_amount_count', 0)}건",
    ]
    # 합계는 "이번 정산 반영분"의 합입니다. 초기 전체 재추정이 섞이면 하루치 매매처럼
    # 보이므로 어떤 종목이 전 기간 반영분인지 요약에서 한 번 더 밝힙니다.
    if daily.get("full_reinference_symbols"):
        summary.append(
            "  ※ 전체 재추정 포함: "
            + ", ".join(daily["full_reinference_symbols"])
            + " (전 기간 반영분이 합계에 포함)"
        )
    if daily.get("estimated_amount_count"):
        summary.append(
            f"  ※ 금액 일부 추정 {daily['estimated_amount_count']}건 "
            "(확정 체결금액이 아닌 추정치 포함)"
        )
    if daily.get("retried_symbols"):
        summary.append(
            "  ※ 1차 발송 실패분 재전송: "
            + ", ".join(daily["retried_symbols"])
            + " (이전 실행의 정산 내용)"
        )
    if daily.get("unconfirmed_marker_count"):
        summary.append(f"  마커 미확정 {daily['unconfirmed_marker_count']}건")
    if daily.get("hold_count"):
        summary.append("  보류 종목은 신규 주문이 차단된 상태입니다 — 확인 후 재실행하세요.")
    sections.extend(summary)

    message = "\n".join(sections).strip()
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 20].rstrip() + "\n…(생략)"
    return message


# ── 발송 상태 (state 내 전용 키) ─────────────────────────────────
def _normalized_store(raw):
    store = dict(raw) if isinstance(raw, dict) else {}
    for key in ("sent", "holds", "pending"):
        entries = store.get(key)
        store[key] = dict(entries) if isinstance(entries, dict) else {}
    store["version"] = REPORT_VERSION
    return store


# `pending` 에 저장하는 리포트 필드 — 재시도 시 본문을 그대로 되살리는 데 필요한 최소 집합.
_PAYLOAD_FIELDS = (
    "symbol",
    "exchange",
    "settled_session",
    "session_key",
    "report_id",
    "broker",
    "committed",
    "halted",
    "halt_reason",
    "t_before",
    "t_after",
    "net_before",
    "net_after",
    "buy_qty",
    "buy_amount",
    "sell_qty",
    "sell_amount",
    "position_qty",
    "late_fill_count",
    "full_reinference",
    "unconfirmed_amount_count",
    "unconfirmed_marker_count",
    "estimated_amount_count",
)


def _report_payload(report):
    """미발송 리포트의 본문 재료를 state 에 넣을 수 있는 dict 로 만듭니다.

    본문(텔레그램 메시지)이 아니라 **재료**를 저장합니다 — 나중에 포맷이 바뀌어도
    재전송이 새 포맷으로 렌더되고, 미리보기/디버깅도 그대로 됩니다.
    """
    return {field: getattr(report, field, None) for field in _PAYLOAD_FIELDS}


def _report_from_payload(payload):
    """`_report_payload()` 결과를 SymbolReport 로 되살립니다 (형식이 깨졌으면 None)."""
    if not isinstance(payload, dict):
        return None
    report = SymbolReport(symbol=str(payload.get("symbol") or ""))
    for field in _PAYLOAD_FIELDS:
        if field in payload:
            try:
                setattr(report, field, payload[field])
            except Exception:
                continue
    if report.position_qty is not None:
        report.position_qty = _as_int(report.position_qty, 0)
    return report


def _prune(store):
    """오래된 항목을 잘라 state 가 무한정 커지지 않게 합니다 (최근 것만 보관)."""
    limits = (
        ("sent", MAX_SENT_ENTRIES),
        ("holds", MAX_HOLD_ENTRIES),
        ("pending", MAX_PENDING_ENTRIES),
    )
    for key, limit in limits:
        entries = store.get(key) or {}
        if len(entries) <= limit:
            continue
        ordered = sorted(
            entries.items(),
            key=lambda kv: str((kv[1] or {}).get("at", "")),
            reverse=True,
        )
        store[key] = dict(ordered[:limit])
    return store


def load_report_store(symbol):
    """종목 state 의 발송 상태를 읽습니다 (실패하면 빈 상태 — 발송 로직은 계속)."""
    try:
        from state import load_state

        loaded = load_state(symbol)
    except Exception:
        return {}
    if not isinstance(loaded, dict) or loaded.get("_state_unavailable"):
        return {}
    return _normalized_store(loaded.get(REPORT_STATE_KEY))


def report_already_sent(store, report):
    """이미 발송한 리포트인지 (성공 리포트와 보류 알림을 분리해 판정)."""
    store = _normalized_store(store)
    if report.is_hold:
        return hold_key(report) in store["holds"]
    return report.report_id in store["sent"]


def restore_pending(store, report):
    """미발송 payload 가 있으면 그 내용으로 리포트를 되살립니다 (없으면 None).

    정산은 이미 커밋됐으므로 재시도 실행의 리포트는 델타가 0입니다. 그대로 보내면
    "재시도 성공"만 남고 **실제 반영분이 사라집니다**. 그래서 1차 실패 시 저장해 둔
    재료를 그대로 다시 보냅니다. 보류(hold) 알림은 재시도 때 같은 사유로 재생성되므로
    되살리지 않습니다 (보류가 해소되면 새 성공 리포트를 보내는 것이 맞습니다).
    """
    if report.is_hold:
        return None
    store = _normalized_store(store)
    entry = store["pending"].get(report.report_id)
    if not isinstance(entry, dict):
        return None
    restored = _report_from_payload(entry.get("payload"))
    if restored is None:
        return None
    if not restored.committed or restored.symbol != report.symbol:
        # 손상/불일치 payload — 엉뚱한 종목·보류 화면으로 나가면 안 됩니다.
        print(
            f"[정산 리포트] {report.symbol} 미발송 payload 가 유효하지 않아 "
            "저장 내용 재전송을 건너뜁니다 (이번 실행 내용으로 발송)"
        )
        return None
    # 멱등 키/세션은 이번 실행 기준을 유지합니다 (저장값이 손상돼도 키가 흔들리지 않게).
    restored.report_id = report.report_id
    restored.session_key = report.session_key
    if not restored.settled_session:
        restored.settled_session = report.settled_session
    restored.retried = True
    restored.already_sent = False
    return restored


def _apply_mark(store, report, *, ok, at_iso, reason=""):
    """state 발송 상태에 이번 발송 결과를 반영합니다 (거래 필드는 손대지 않음).

    실패 시에는 보류 알림을 제외하고 **본문 재료(payload)를 `pending` 에 저장**합니다
    (재시도가 빈 리포트를 보내지 않도록). `last_failure` 는 **감사용 요약**이며 재시도
    근거는 `pending` 입니다 (소비처는 `restore_pending()`).
    """
    store = _normalized_store(store)
    if ok:
        if report.is_hold:
            store["holds"][hold_key(report)] = {
                "at": at_iso,
                "symbol": report.symbol,
                "session": report.settled_session,
                "session_key": getattr(report, "session_key", ""),
                "reason": report.halt_reason[:120],
                "version": REPORT_VERSION,
            }
        else:
            store["sent"][report.report_id] = {
                "at": at_iso,
                "symbol": report.symbol,
                "session": report.settled_session,
                "session_key": getattr(report, "session_key", ""),
                "broker": report.broker,
                "retried": bool(getattr(report, "retried", False)),
                "version": REPORT_VERSION,
            }
        # 발송됐으므로 미발송 payload 는 지웁니다 (다음 세션에서 다시 보내지 않도록).
        # ⚠️ 보류 알림 성공으로는 지우지 않습니다 — 같은 report_id 를 공유하기 때문에
        #    지우면 아직 못 보낸 **커밋된 정산 내용**이 사라집니다.
        if not report.is_hold:
            store["pending"].pop(report.report_id, None)
        store.pop("last_failure", None)
    else:
        if not report.is_hold:
            previous = store["pending"].get(report.report_id) or {}
            store["pending"][report.report_id] = {
                "at": at_iso,
                "symbol": report.symbol,
                "session": report.settled_session,
                "session_key": getattr(report, "session_key", ""),
                "attempts": _as_int(previous.get("attempts"), 0) + 1,
                "payload": _report_payload(report),
            }
        # 감사용 요약 — 재시도 근거는 위 `pending` 입니다.
        store["last_failure"] = {
            "at": at_iso,
            "symbol": report.symbol,
            "session": report.settled_session,
            "session_key": getattr(report, "session_key", ""),
            "report_id": report.report_id,
            "reason": str(reason or "notify_failed")[:120],
            "retryable": True,
            "pending_saved": not report.is_hold,
        }
    return _prune(store)


def persist_marks(marks, now=None):
    """발송 결과를 state 에 **한 번의 파일 쓰기**로 기록합니다.

    marks: {symbol: {"reports": [...], "ok": bool, "reason": str}}
    실패해도 예외를 던지지 않습니다 (리포트는 정산의 부수 기능).
    """
    if not marks:
        return False
    at_iso = _now_iso(now)
    try:
        from state import load_state, save_states
        from pipeline import global_lock
    except Exception as error:  # pragma: no cover - 임포트 실패는 환경 문제
        print(f"[정산 리포트] 발송 상태 기록 모듈 로드 실패({type(error).__name__}) — 생략")
        return False

    updates = {}
    try:
        with global_lock():
            for symbol, mark in marks.items():
                loaded = load_state(symbol)
                if not isinstance(loaded, dict) or loaded.get("_state_unavailable"):
                    # 상태 파일에 없는 종목은 새 레코드를 만들지 않습니다.
                    continue
                store = loaded.get(REPORT_STATE_KEY)
                for report in mark.get("reports") or []:
                    store = _apply_mark(
                        store,
                        report,
                        ok=bool(mark.get("ok")),
                        at_iso=at_iso,
                        reason=mark.get("reason", ""),
                    )
                loaded[REPORT_STATE_KEY] = store
                updates[symbol] = loaded
            if updates:
                save_states(updates)
    except Exception as error:
        print(
            f"[정산 리포트] 발송 상태 기록 실패({type(error).__name__}) — "
            "다음 실행에서 중복 발송될 수 있습니다"
        )
        return False
    return bool(updates)


# ── 발송 오케스트레이션 ───────────────────────────────────────────
def _telegram_configured():
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_ID"))


def _default_sender(message):
    """기본 발송자 — 정산 리포트는 **조용한 시간에도** 보냅니다(`urgent=True`).

    `notify()` 는 조용한 시간에 건너뛰어도 `False` 를 돌려주므로 건너뜀과 실패를
    구분할 수 없습니다. 리포트를 일반 메시지로 보내면 (SETTLE 은 KST 새벽 실행)
    조용한 시간 설정에서 **항상** 건너뛰어 = "항상 실패 → 영원한 재시도"가 됩니다.
    하루 1회 운영 요약이라 소음 위험이 없으므로 urgent 로 보내
    **`False` = 실제 실패(재시도 대상)** 를 보장합니다.
    """
    return notify(message, urgent=True)


def send_settlement_report(
    reports,
    *,
    trade_mode,
    broker="",
    account_fingerprint="",
    settled_session="",
    now=None,
    notify_func=None,
):
    """일일 정산 리포트를 발송하고 발송 상태를 기록합니다. **예외를 던지지 않습니다.**

    Args:
        reports: `build_symbol_report()` 결과 목록 (이번 SETTLE 실행의 종목들)
        trade_mode: 호출측 TRADE_MODE. LIVE 가 아니면 발송하지 않습니다
            (DRY/REPAIR 는 state 를 저장하지 않으므로 중복 방지 근거가 남지 않습니다).
        broker/account_fingerprint: 멱등 식별자 구성 요소.
            **`account_fingerprint` 가 비면 발송하지 않습니다** — 계좌 구분이 사라져
            서로 다른 계좌의 리포트가 같은 멱등 키를 공유하기 때문입니다.
        settled_session: 세션이 비어 있는 리포트(예외/캘린더 미확인)에 바인딩할 세션.
        notify_func: 테스트/대체 채널 주입용 (기본 `notifier.notify(urgent=True)`)

    Returns:
        dict: {"sent": [...], "failed": [...], "retried": [...], "skipped": [사유...]}
    """
    result = {"sent": [], "failed": [], "retried": [], "skipped": []}
    try:
        reports = list(reports or [])
        if str(trade_mode or "").strip().upper() != "LIVE":
            result["skipped"].append("not_live")
            print("[정산 리포트] LIVE 실행이 아니므로 발송을 생략합니다 (DRY/REPAIR)")
            return result
        if not reports:
            result["skipped"].append("no_symbols")
            return result
        if not str(account_fingerprint or "").strip():
            # 지문이 없으면 멱등 키의 계좌 차원이 사라져 계좌 간 리포트가 서로를 억제합니다.
            result["skipped"].append("account_fingerprint_missing")
            print(
                "[정산 리포트] 계좌 지문이 없어 발송을 생략합니다 "
                "(계좌 구분 없는 멱등 키로 다른 계좌 리포트를 억제하지 않도록 — "
                "정산 결과에는 영향 없음, 발송 상태도 기록하지 않음)"
            )
            return result
        if not _telegram_configured() and notify_func is None:
            result["skipped"].append("telegram_not_configured")
            print(
                "[정산 리포트] 텔레그램 설정(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)이 없어 "
                "발송을 생략합니다 (발송 상태도 기록하지 않음)"
            )
            return result

        sender = notify_func or _default_sender

        # 1) 중복 판정 + 미발송 payload 복원
        #    — 종목별 state 의 전용 키만 읽습니다 (거래 상태는 읽기만).
        #    — 1차 발송에 실패한 리포트는 저장해 둔 내용으로 되살립니다 (빈 리포트 방지).
        todo = []
        for index, report in enumerate(reports):
            if not str(getattr(report, "session_key", "") or "").strip():
                report.session_key = session_bucket(
                    getattr(report, "settled_session", "") or settled_session, now
                )
            if not report.report_id:
                report.report_id = report_id(
                    broker, account_fingerprint, report.symbol, report.session_key
                )
            store = load_report_store(report.symbol)
            if report_already_sent(store, report):
                report.already_sent = True
                continue
            restored = restore_pending(store, report)
            if restored is not None:
                reports[index] = restored
                todo.append(restored)
            else:
                todo.append(report)

        if not todo:
            result["skipped"].append("already_sent")
            print(
                "[정산 리포트] 이번 정산 세션 리포트는 이미 발송되었습니다 "
                "(broker+계좌지문+종목+세션 멱등 키 — 중복 방지)"
            )
            return result

        # 2) 발송 — 실패해도 정산 결과에는 영향이 없습니다.
        message = format_report_message(build_daily_report(reports, now=now))
        try:
            sent_ok = bool(sender(message))
        except Exception as error:
            # 발송 예외는 여기서 흡수합니다 (정산 롤백 금지).
            print(
                f"[정산 리포트] 발송 예외({type(error).__name__}) — "
                "정산 결과/주문에는 영향 없습니다"
            )
            sent_ok = False

        # 3) 발송 상태 기록 (성공했을 때만 '발송됨' — 실패는 재시도 가능 상태)
        marks = {}
        for report in todo:
            marks.setdefault(report.symbol, {"reports": [], "ok": sent_ok,
                                             "reason": "" if sent_ok else "notify_failed"})
            marks[report.symbol]["reports"].append(report)
        persist_marks(marks, now=now)

        retried = [r.symbol for r in todo if getattr(r, "retried", False)]
        if sent_ok:
            result["sent"] = [r.symbol for r in todo]
            result["retried"] = retried
            print(
                f"[정산 리포트] 발송 완료 — {len(todo)}건 "
                f"({', '.join(r.symbol for r in todo)})"
            )
            if retried:
                print(
                    f"[정산 리포트] 1차 발송 실패분 재전송: {', '.join(retried)} "
                    "(저장해 둔 정산 내용으로 발송)"
                )
        else:
            result["failed"] = [r.symbol for r in todo]
            print(
                f"[정산 리포트] 발송 실패 — {len(todo)}건은 다음 실행에서 재시도합니다 "
                "(정산 내용은 state 에 보관 — 재시도는 그 내용으로 발송)"
            )
        return result
    except Exception as error:  # 어떤 경우에도 정산 흐름을 깨지 않습니다
        print(
            f"[정산 리포트] 처리 중 예외({type(error).__name__}) — "
            "정산 결과/주문에는 영향 없습니다"
        )
        result["failed"].append("error")
        return result
