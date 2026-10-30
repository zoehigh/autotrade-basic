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
   pending 항목은 **생성 시각**(`created_at`, 불변)과 **마지막 시도 시각**(`at`)을 나눠
   기록하고, 재전송 순서·최신 스냅샷 선택은 생성 시각을 씁니다. 마지막 시도 시각으로
   정렬하면 오래된 payload 재시도 실패가 그 payload 를 "최신"으로 둔갑시켜 다음 델타 0
   실행이 오정정을 냅니다. 구버전 항목은 `created_at` 이 없어 `at` 으로 물러납니다.
6. **정산 세션 바인딩**: 보류/실패 알림의 억제 키에는 **호출측이 의도한 정산 세션**이 들어갑니다.
   `settled=None`(정산 예외)이나 `session_calendar_unavailable` 처럼 결과 객체에 세션이
   없는 경로에서 세션이 비면, 같은 사유가 **날짜를 넘어 영구 억제**되어 운영자는 첫 실패만
   보게 됩니다. 세션을 계산할 수 없을 때는 UTC 날짜 버킷(`unknown:YYYY-MM-DD`)으로
   **하루 단위** 억제합니다.
7. **과거 세션 미발송분 이월 재전송(backlog)**: `pending` 키에는 정산 세션이 들어 있으므로,
   전날 실패한 payload 는 **다음날 리포트 키로 조회되지 않습니다**. 그래서 새 일일 리포트를
   발송하기 **전에** 그 종목 state 의 `pending` 을 직접 훑어, (broker, 계좌지문, 종목)이
   일치하고 **정산 세션이 이번 실행과 다른** 항목을 모아 **원래 정산 세션을 명시한 별도
   메시지**(`📨 미발송 정산 재전송`)로 **오래된 세션부터** 보냅니다. 오늘 리포트와 섞으면
   "오늘 정산"으로 오해되므로 메시지를 분리합니다. 한 실행에서 보내는 세션 그룹 수는
   `MAX_BACKLOG_SESSIONS_PER_RUN` 으로 제한합니다(오래된 것부터 처리 → 다음 실행에서 이어짐).
   같은 세션의 미발송분은 기존 경로(현재 리포트 재시도)가 처리하므로 backlog 대상이 아닙니다.
   과거 세션의 pending 이라도 **발송 이력과 내용 지문이 같으면** 잔여물이므로 건너뛰고,
   지문이 다르면(정정 알림이 실패해 남은 추가 반영분) 재전송합니다 — 키 존재만으로
   건너뛰면 세션이 넘어간 뒤 그 반영분을 영영 알릴 수 없습니다.
8. **같은 세션 정정 알림(correction)**: 정산은 세션 단위로 1회지만, 발송 뒤에 **뒤늦은 추가
   체결**이 반영되면 같은 세션의 정산 결과가 달라집니다. 성공 이력만 보고 무조건 건너뛰면
   그 반영분을 영영 알릴 수 없습니다. 그래서 발송 이력에 **내용 지문**(`delta_hash` = 이번
   반영분, `state_hash` = 확정 T·순투입·보유)과 **리비전 번호**를 함께 남기고, 지문이
   실질적으로 달라졌을 때만 `📌 정산 정정` 메시지를 별도 발송합니다. 반영분이 없고 확정
   상태도 같으면(델타 0 재보고) 기존처럼 억제합니다. 지문이 없는 기존 발송 이력은 비교가
   불가능하므로 억제(= 기존 동작)로 처리합니다. "새 내용인가"를 판단할 때 비교 기준은
   **이미 알려진 모든 내용**(발송한 payload 이력 + 같은 세션 `pending`)입니다 — 하나만 고르면
   반대쪽에 이미 알려진 내용을 델타 0 정정으로 다시 알리게 됩니다.
   발송 이력은 **`payloads`(무엇을 보냈는가 = 지문 이력)** 와 **`delta_hash`/`state_hash`
   (가장 최근 스냅샷 기준선)** 로 **분리 기록**합니다. 과거 세션 이월 재전송이나 정정 실패분
   재전송은 **오래된 payload** 를 성공시키는 경로인데, 그 성공이 기준선을 과거로 되돌리면
   다음 델타 0 실행이 이미 알려진 최신 내용을 "새 정보"로 오판해 정정을 냅니다. 그래서
   되살린 payload 는 `payloads` 에만 남기고 기준선은 최신 스냅샷으로 유지합니다
   (동일 내용 재발송 억제는 그대로).
   확정 상태 지문에는 **지속적 정산값만** 넣습니다(T·순투입·보유). `full_reinference`
   처럼 "이번 실행이 어땠는지"를 나타내는 per-run 플래그를 넣으면, 초기 전체 재추정
   SETTLE 뒤 같은 세션의 다음 실행(델타 0·상태 동일)이 플래그만 바뀌어 정정으로 나갑니다.
   리비전은 상수로 박지 않고 persist 규칙(`previous.revision+1`)과 같은 식으로 계산해
   메시지 표기와 state 기록이 어긋나지 않게 합니다.
9. **메시지 길이 분할**: 여러 종목·다중 정정이 한 메시지에 합쳐져 텔레그램 한도를 넘으면
   `_clip()` 이 뒷부분을 잘라냅니다. 잘린 채 "전량 발송 완료"로 기록하면 **잘려 나간
   반영분이 영영 사라지므로**, 리포트를 **메시지 크기 기준으로 나눠** 각각 보내고
   **실제로 전송된 덩어리의 payload 만** 완료로 표시합니다(실패한 덩어리는 `pending` 유지).

멱등 식별자
----------
    broker + account_fingerprint + symbol + (settled_session 또는 세션 버킷) + report_version

`pending` 항목은 이 멱등 키에 **내용 지문**을 붙인 자리(`{멱등 키}:{내용 지문}`)를 dict 키로
쓰고, 함께 `broker`/`account`/`session_key`/`report_id` 를 남깁니다 — 다음 실행에서
**계좌·종목별로** 과거 미발송분을 골라내기 위해서입니다(계좌 구분이 없는 레거시 항목은 다른
계좌로 새지 않도록 재전송하지 않고 건너뜁니다). 키를 나누는 이유: 같은 세션의 **서로 다른
payload**(1차 실패분 + 정정분, 정정 #N + #N+1)가 하나의 키를 공유하면 나중 것이 먼저 것을
덮어써 그 반영분이 영영 사라집니다. 같은 내용이면 같은 자리를 재사용해 시도 횟수만 누적합니다.

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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from notifier import notify

# 리포트 포맷/집계 규칙이 바뀌면 올립니다 (멱등 식별자에 포함 — 구버전 리포트와 구분).
# v2: 미발송 payload 재전송(pending)·전체 재추정/추정 금액 요약 표기·세션 버킷 도입.
#     이후 과거 세션 이월 재전송(backlog)·같은 세션 정정 알림(correction)을 추가했지만
#     **버전은 유지**합니다. 올리면 이미 발송한 세션이 "미발송"으로 보여 같은 세션
#     리포트가 한 번 더 나가기 때문입니다(구버전 발송 이력은 지문이 없어 억제로 처리).
REPORT_VERSION = 2

# state 안에서 발송 상태를 보관하는 전용 키 (거래 원장과 분리).
REPORT_STATE_KEY = "settlement_report"

# state 파일이 무한정 커지지 않도록 보관 개수를 제한합니다.
MAX_SENT_ENTRIES = 20
MAX_HOLD_ENTRIES = 10
# 발송 이력 한 건이 기억하는 **발송된 payload 지문**(내용) 개수 상한.
# 마지막 1건만 남기면 오래된 payload 재전송 성공이 기준선을 과거로 되돌립니다.
MAX_SENT_PAYLOAD_HISTORY = 8
# 미발송(pending) payload — 재시도 전에 사라지면 안 되지만 무한정 쌓이지도 않게 합니다.
# ⚠️ 정리 기준은 **"이미 발송된 내용의 잔여물"** 뿐입니다(미발송분 보존 불변식).
#    슬롯 개수만 보고 지우면 아직 못 보낸 정산 반영분이 조용히 사라지므로,
#    아래 두 값은 "목표치(경고)"와 "마지노선(경고 후 정리)"으로만 씁니다.
MAX_PENDING_ENTRIES = 10  # 목표치 — 넘으면 경고만 하고 **보존**합니다.
MAX_PENDING_HARD_CAP = 40  # 마지노선 — 넘으면 오래된 것부터 정리(경고 필수).

# 한 실행에서 재전송하는 **과거 세션** 그룹 수 상한 (오래된 세션부터).
# 발송이 여러 날 실패해 쌓여도 한 번에 몰아 보내지 않고, 다음 실행에서 이어서 처리합니다.
MAX_BACKLOG_SESSIONS_PER_RUN = 3

# 텔레그램 메시지 길이 제한(4096)보다 여유 있게 자릅니다.
# 단일 종목 메시지가 이 한도를 넘는 것은 **이론적 한계**입니다 — 사유 300자·수량/금액
# 필드 상한 때문에 실제로는 도달하지 않지만, 도달하면 더 나눌 수 없어 앞부분만 나갑니다
# (`_send_chunks()` 가 경고를 남깁니다).
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


# ── 내용 지문 (같은 세션 재보고 판정) ─────────────────────────────
def _amount_key(value):
    return f"{_as_float(value):.2f}"


def report_delta_hash(report):
    """이 리포트가 알리는 **반영분(델타)** 의 지문.

    부분체결이 늘어나면 델타 합계가 달라지므로 지문도 달라집니다 — 같은 세션의
    추가 반영분을 "새 정보"로 판정하는 근거입니다.
    """
    raw = "|".join(
        [
            str(_as_int(getattr(report, "buy_qty", 0), 0)),
            _amount_key(getattr(report, "buy_amount", 0.0)),
            str(_as_int(getattr(report, "sell_qty", 0), 0)),
            _amount_key(getattr(report, "sell_amount", 0.0)),
            str(_as_int(getattr(report, "late_fill_count", 0), 0)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def report_state_hash(report):
    """확정 상태(T·순투입·보유)의 지문.

    델타가 0인 재보고(정산 반영분 없음)를 "새 정보 없음"으로 억제하기 위한 축입니다.
    미확정 건수 같은 부가 표기는 지문에서 제외합니다 — 값이 흔들려도 정정 알림이
    남발되면 안 되기 때문입니다.
    **실행 방식 플래그(`full_reinference`)도 제외**합니다. 그 값은 "이번 실행이 전체
    재추정이었는지"라는 per-run 정보이지 지속적 정산값이 아닙니다. 넣으면 초기 전체
    재추정 SETTLE 뒤 같은 세션의 다음 실행(델타 0, T·순투입·보유 동일)이 플래그만
    true→false 로 바뀌어 델타 0 정정이 나갑니다. 반영분이 실제로 있으면 델타 지문이
    달라지므로 전체 재추정 여부도 그때 함께 알려집니다.
    """
    position = getattr(report, "position_qty", None)
    raw = "|".join(
        [
            f"{_as_float(getattr(report, 't_after', 0.0)):.4f}",
            _amount_key(getattr(report, "net_after", 0.0)),
            "" if position is None else str(_as_int(position, 0)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _report_hashes(report):
    """발송 이력/pending payload 에 남길 지문 묶음."""
    return {"delta_hash": report_delta_hash(report), "state_hash": report_state_hash(report)}


def report_has_new_content(report, known):
    """이미 알려진 내용(발송 이력/보관 payload)과 **실질적으로 다른** 내용인지 (정정 대상 판정).

    Args:
        known: `sent` 항목 dict **또는** `_report_hashes()` 결과. 두 형태 모두
            `state_hash`/`delta_hash` 키만 읽으므로 어느 쪽을 넘겨도 같은 판정입니다
            (호출부에 따라 "발송 이력"이 오기도 하고 "지문 묶음"이 오기도 합니다).

    - 확정 상태(T·순투입·보유)가 달라졌으면 정정 대상입니다.
    - 반영분(델타)이 있는데 델타 지문이 다르면 정정 대상입니다.
      델타 0인 재보고는 상태가 같다면 새 정보가 아니므로 억제합니다.
    - 지문이 없는 레거시 항목은 비교할 수 없으므로 "새 내용 없음"으로 봅니다.
    """
    known = known if isinstance(known, dict) else {}
    state_hash = known.get("state_hash")
    if state_hash and state_hash != report_state_hash(report):
        return True
    delta_hash = known.get("delta_hash")
    if delta_hash and report.has_activity() and delta_hash != report_delta_hash(report):
        return True
    return False


def _entry_known_hashes(entry):
    """발송 이력 항목이 기억하는 **알려진 내용 지문 전부** (마지막 1건 + 발송 payload 이력).

    왜 1건이 아닌가: 과거 세션 backlog 재전송이나 정정 실패분 재전송은 **오래된 payload**
    를 성공시키는 경로입니다. 발송 이력을 "마지막에 보낸 payload" 1건만 남기면 그 성공이
    최신 스냅샷 지문을 과거로 되돌리고, 다음 델타 0 실행이 이미 알려진 최신 내용을
    "새 정보"로 오판해 정정을 냅니다. 그래서 **발송한 payload 기록**(`payloads`)을 따로
    쌓고 비교 기준선은 이 전체를 씁니다 (중복 방지는 그대로).
    """
    if not isinstance(entry, dict):
        return []
    knowns = []
    primary = {
        "delta_hash": entry.get("delta_hash"),
        "state_hash": entry.get("state_hash"),
    }
    if primary["delta_hash"] or primary["state_hash"]:
        knowns.append(primary)
    for item in entry.get("payloads") or []:
        if not isinstance(item, dict):
            continue
        hashes = {
            "delta_hash": item.get("delta_hash"),
            "state_hash": item.get("state_hash"),
        }
        if hashes["delta_hash"] or hashes["state_hash"]:
            knowns.append(hashes)
    return knowns


def _is_new_against_hashes(report, knowns):
    """이 리포트가 **알려진 어떤 내용과도** 다른지 (지문 목록 기준).

    비교할 지문이 하나도 없으면 `False` 입니다 — 비교 근거가 없을 때는 기존처럼
    "새 내용 없음"으로 보고 억제합니다 (`report_has_new_content()` 와 같은 규칙).
    """
    usable = [
        known
        for known in (knowns or [])
        if isinstance(known, dict)
        and (known.get("delta_hash") or known.get("state_hash"))
    ]
    if not usable:
        return False
    return all(report_has_new_content(report, known) for known in usable)


def _baseline_hashes(entry):
    """발송 이력 항목에 기록된 **최신 스냅샷 기준선** (지문이 없으면 빈 dict)."""
    baseline = {
        "delta_hash": (entry or {}).get("delta_hash"),
        "state_hash": (entry or {}).get("state_hash"),
    }
    if not baseline["delta_hash"] and not baseline["state_hash"]:
        return {}
    return baseline


def _is_restored_payload(report):
    """이 리포트가 **보관해 둔 payload 를 되살린 것**인지 (최신 스냅샷이 아닐 수 있음).

    되살리는 경로는 셋입니다.
      - 같은 세션 재시도 (`restore_pending()` → `retried=True`)
      - 정정 발송 실패분 재전송 (`_unreported_pending()` → `pending_key`)
      - 과거 세션 이월 재전송 (`pending_backlog()` → `backlog=True`)
    셋 다 **과거에 만들어진 내용**이므로, 발송에 성공해도 최신 스냅샷 기준선을
    대표하지 않습니다.
    """
    return bool(
        str(getattr(report, "pending_key", "") or "").strip()
        or getattr(report, "retried", False)
        or getattr(report, "backlog", False)
    )


def _append_payload_history(previous, hashes, at_iso):
    """**발송한 payload 지문 이력**을 갱신합니다 (같은 내용은 1회, 최근 것 위주 상한).

    기준선(`delta_hash`/`state_hash`)과 분리해 두는 이유: 기준선은 "가장 최근 스냅샷"
    한 건이지만, 발송 기록은 "무엇을 이미 보냈는가"의 집합입니다. 둘을 한 필드로 합치면
    오래된 payload 재전송이 기준선을 과거로 되돌립니다.
    """
    history = [
        item for item in ((previous or {}).get("payloads") or []) if isinstance(item, dict)
    ]
    fingerprint = f"{hashes.get('delta_hash')}{hashes.get('state_hash')}"
    history = [
        item
        for item in history
        if f"{item.get('delta_hash')}{item.get('state_hash')}" != fingerprint
    ]
    history.append({**hashes, "at": at_iso})
    return history[-MAX_SENT_PAYLOAD_HISTORY:]


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
    # **과거 세션**의 미발송분을 재전송하는지 (오늘 정산과 구분해 표시)
    backlog: bool = False
    # 같은 세션에 뒤늦게 반영된 내용을 알리는 **정정** 메시지인지 + 리비전 번호
    correction: bool = False
    revision: int = 0
    # state `pending` 항목 키 (재전송 성공 시 정리 대상) — payload 로 저장하지 않습니다
    pending_key: str = ""
    # 계좌 지문 (과거 pending 을 계좌별로 구분) — payload 로 저장하지 않습니다
    account_fingerprint: str = ""

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
def build_daily_report(reports, now=None, *, include_already_sent=True):
    """종목별 리포트를 일일 요약으로 묶습니다 (실현손익·평가손익은 다루지 않습니다).

    `include_already_sent=False` 이면 **이미 발송된 리포트**(중복 억제분·정정 대상)를
    합계·건수 축에서 제외합니다. 일일 리포트 본문은 그런 종목을 그리지 않으므로, 합계에
    넣으면 같은 세션 반영분이 두 번 더해집니다(본문 1회 + 합계 1회).
    정정 알림은 자기 델타를 별도로 합산해야 하므로 호출부에서 `include_already_sent=True`
    로 구분해 넘깁니다.
    """
    reports = list(reports or [])
    sessions = sorted({r.settled_session for r in reports if r.settled_session})
    visible = [
        r for r in reports if include_already_sent or not getattr(r, "already_sent", False)
    ]
    committed = [r for r in visible if not r.is_hold]
    holds = [r for r in visible if r.is_hold]
    return {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _now_iso(now),
        "settled_session": sessions[0] if len(sessions) == 1 else ", ".join(sessions),
        "reports": reports,
        "completed_count": len(committed),
        "hold_count": len(holds),
        "unconfirmed_amount_count": sum(r.unconfirmed_amount_count for r in visible),
        "unconfirmed_marker_count": sum(r.unconfirmed_marker_count for r in visible),
        # 하루치 매매처럼 보이지 않도록 요약에도 "전체 재추정 포함"을 남깁니다.
        "full_reinference_symbols": [r.symbol for r in committed if r.full_reinference],
        "estimated_amount_count": sum(
            getattr(r, "estimated_amount_count", 0) or 0 for r in committed
        ),
        "retried_symbols": [r.symbol for r in visible if getattr(r, "retried", False)],
        "buy_qty": sum(r.buy_qty for r in committed),
        "buy_amount": round(sum(r.buy_amount for r in committed), 2),
        "sell_qty": sum(r.sell_qty for r in committed),
        "sell_amount": round(sum(r.sell_amount for r in committed), 2),
    }


# ── 메시지 포맷 ───────────────────────────────────────────────────
def _merge_reports_for_display(reports):
    """같은 종목 리포트를 **섹션 하나**로 합칩니다 (중복 섹션 정리).

    정정 알림에는 같은 종목의 payload 가 여러 번 실릴 수 있습니다(정정 #N 실패분 +
    정정 #N+1, 1차 실패분 + 추가 반영분). 그대로 그리면 같은 종목 섹션이 반복돼
    읽기 어렵고 어느 숫자가 최종인지 알 수 없습니다.

    병합은 **덧셈만** 하므로 합계(델타) 정합은 그대로입니다. 합계 줄은 병합 결과가
    아니라 `build_daily_report()` 가 계산한 값을 쓰므로 이중 집계도 없습니다.
      - 수량/금액/건수: 합산
      - T·순투입·보유: **마지막(가장 최근)** 값 — 상태는 누적 스냅샷이라 더할 수 없습니다
      - `t_before`: 첫(가장 오래된) 값 — 이번 정정 묶음이 시작된 지점
    """
    merged = {}
    order = []
    for report in reports or []:
        key = str(getattr(report, "symbol", "") or "")
        first = merged.get(key)
        if first is None:
            merged[key] = replace(report)
            order.append(key)
            continue
        first.buy_qty = _as_int(first.buy_qty, 0) + _as_int(report.buy_qty, 0)
        first.buy_amount = round(
            _as_float(first.buy_amount, 0.0) + _as_float(report.buy_amount, 0.0), 2
        )
        first.sell_qty = _as_int(first.sell_qty, 0) + _as_int(report.sell_qty, 0)
        first.sell_amount = round(
            _as_float(first.sell_amount, 0.0) + _as_float(report.sell_amount, 0.0), 2
        )
        first.late_fill_count = _as_int(first.late_fill_count, 0) + _as_int(
            report.late_fill_count, 0
        )
        first.unconfirmed_amount_count = _as_int(
            first.unconfirmed_amount_count, 0
        ) + _as_int(report.unconfirmed_amount_count, 0)
        first.unconfirmed_marker_count = _as_int(
            first.unconfirmed_marker_count, 0
        ) + _as_int(report.unconfirmed_marker_count, 0)
        first.estimated_amount_count = _as_int(
            getattr(first, "estimated_amount_count", 0), 0
        ) + _as_int(getattr(report, "estimated_amount_count", 0), 0)
        # 상태 스냅샷은 합산할 수 없으므로 가장 최근 값을 씁니다.
        first.t_after = _as_float(getattr(report, "t_after", 0.0), 0.0)
        first.net_after = _as_float(getattr(report, "net_after", 0.0), 0.0)
        first.position_qty = getattr(report, "position_qty", None)
        first.full_reinference = bool(
            getattr(first, "full_reinference", False)
            or getattr(report, "full_reinference", False)
        )
        first.revision = max(
            _as_int(getattr(first, "revision", 0), 0),
            _as_int(getattr(report, "revision", 0), 0),
        )
    return [merged[key] for key in order]


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
    # 정정 알림은 "직전 리포트 이후 추가분"만 담으므로 문구를 구분합니다.
    label = "추가 반영분" if getattr(report, "correction", False) else "이번 정산 반영분"
    # ⚠️ `ord_dt` 는 **주문일**(미국 영업일)이라 "전일 체결"로 단정할 수 없습니다.
    lines.append(
        f"  {label}: 매수 {report.buy_qty}주 / {_money(report.buy_amount)}"
        f" · 매도 {report.sell_qty}주 / {_money(report.sell_amount)}"
    )
    lines.append(
        f"  T {report.t_before:g} → {report.t_after:g}"
        f" · 순투입 {_money(report.net_before)} → {_money(report.net_after)}"
    )
    if report.position_qty is not None:
        lines.append(f"  확정 보유: {report.position_qty}주")
    notes = []
    if getattr(report, "correction", False):
        revision = _as_int(getattr(report, "revision", 0), 0)
        notes.append(
            f"정정 #{revision} (같은 세션 재보고)" if revision else "같은 세션 재보고"
        )
    if getattr(report, "backlog", False):
        origin = report.settled_session or getattr(report, "session_key", "") or "미확인"
        notes.append(f"원래 정산 세션 {origin} 미발송분")
    elif getattr(report, "retried", False):
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


def format_report_message(daily, *, clip=True):
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
    omitted = [r for r in already if not getattr(r, "correction", False)]
    corrected = [r for r in already if getattr(r, "correction", False)]
    for report in reports:
        # 이미 발송된 종목은 같은 세션에서 다시 그리지 않습니다 (재발송 혼동 방지).
        if getattr(report, "already_sent", False):
            continue
        sections.append("")
        sections.append(_format_symbol_section(report))
    if omitted:
        sections.append("")
        sections.append(
            f"(이미 발송된 종목 {len(omitted)}건은 표시를 생략: "
            + ", ".join(r.symbol for r in omitted)
            + ")"
        )
    if corrected:
        sections.append(
            f"(같은 세션 추가 반영분 {len(corrected)}건은 '정산 정정' 알림으로 별도 발송: "
            + ", ".join(r.symbol for r in corrected)
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

    message = "\n".join(sections)
    return _clip(message) if clip else message.strip()


def _clip(message):
    """텔레그램 길이 제한에 맞춰 자릅니다 (조용히 잘리지 않게 표시를 남깁니다)."""
    message = str(message or "").strip()
    if len(message) > MAX_MESSAGE_CHARS:
        return message[: MAX_MESSAGE_CHARS - 20].rstrip() + "\n…(생략)"
    return message


def format_backlog_message(daily, *, clip=True):
    """**과거 세션** 미발송분 재전송 메시지 (오늘 정산과 섞이지 않게 분리).

    전날 발송에 실패한 정산 내용은 원래 정산 세션이 다르므로, 같은 메시지에 오늘
    정산과 함께 실으면 "오늘 반영분"으로 오해됩니다. 그래서 원래 세션을 명시한
    별도 메시지로 보냅니다.
    """
    daily = daily or {}
    reports = _merge_reports_for_display(daily.get("reports"))
    session = str(daily.get("settled_session") or "").strip() or "미확인"
    lines = [
        "📨 미발송 정산 재전송",
        f"정산 세션: {session} (원래 정산 세션)",
        "※ 이전 실행에서 발송에 실패해 state 에 보관해 둔 정산 내용입니다 (오늘 정산 아님)",
    ]
    for report in reports:
        lines.append("")
        lines.append(_format_symbol_section(report))
    lines.append("")
    lines.append(
        f"합계: 매수 {daily.get('buy_qty', 0)}주 / {_money(daily.get('buy_amount', 0.0))}"
        f" · 매도 {daily.get('sell_qty', 0)}주 / {_money(daily.get('sell_amount', 0.0))}"
    )
    if daily.get("full_reinference_symbols"):
        lines.append(
            "  ※ 전체 재추정 포함: "
            + ", ".join(daily["full_reinference_symbols"])
            + " (전 기간 반영분이 합계에 포함)"
        )
    message = "\n".join(lines)
    return _clip(message) if clip else message.strip()


def format_correction_message(daily, *, clip=True):
    """같은 세션 **추가 반영분** 정정 메시지.

    이미 발송한 세션의 정산 결과가 뒤늦은 체결로 달라졌을 때 보냅니다. 본문은
    "직전 리포트 이후 추가 반영분"이며 누적 합계가 아님을 명시합니다.
    같은 종목이 여러 번 실리면 **섹션 하나로 병합**해 그립니다(`_merge_reports_for_display`).
    """
    daily = daily or {}
    reports = _merge_reports_for_display(daily.get("reports"))
    session = str(daily.get("settled_session") or "").strip() or "미확인"
    revisions = sorted(
        {
            _as_int(getattr(report, "revision", 0), 0)
            for report in reports
            if _as_int(getattr(report, "revision", 0), 0)
        }
    )
    lines = ["📌 정산 정정", f"정산 세션: {session} (이미 발송된 세션의 추가 반영분)"]
    if revisions:
        lines.append("리비전: " + ", ".join(f"#{revision}" for revision in revisions))
    lines.append("※ 직전 리포트 이후 새로 반영된 내용만 담습니다 (누적 아님)")
    for report in reports:
        lines.append("")
        lines.append(_format_symbol_section(report))
    lines.append("")
    lines.append(
        f"합계: 매수 {daily.get('buy_qty', 0)}주 / {_money(daily.get('buy_amount', 0.0))}"
        f" · 매도 {daily.get('sell_qty', 0)}주 / {_money(daily.get('sell_amount', 0.0))}"
    )
    message = "\n".join(lines)
    return _clip(message) if clip else message.strip()


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
    """오래된 항목을 잘라 state 가 무한정 커지지 않게 합니다 (최근 것만 보관).

    ⚠️ `pending`(**미발송 내용**)은 다르게 다룹니다. 재시도 근거이므로 **슬롯 개수만
    보고** 지우면 아직 못 보낸 정산 반영분이 조용히 사라집니다. 그래서 pending 은
    "이미 발송된 내용의 잔여물"만 정리하고(`_prune_pending()`), 미발송분은 보존합니다.
    """
    for key, limit in (("sent", MAX_SENT_ENTRIES), ("holds", MAX_HOLD_ENTRIES)):
        entries = store.get(key) or {}
        if len(entries) <= limit:
            continue
        ordered = sorted(
            entries.items(),
            key=lambda kv: str((kv[1] or {}).get("at", "")),
            reverse=True,
        )
        store[key] = dict(ordered[:limit])
    return _prune_pending(store)


def _prune_pending(store):
    """`pending` 정리 — **미발송 내용 보존**이 우선입니다.

    지우는 대상은 두 가지뿐입니다.
      1. 이미 같은 내용이 발송된 잔여물 (지문 비교로 확인 — 다시 보내면 중복)
      2. payload 가 없거나 깨져 **재전송이 불가능한** 항목

    그래도 하드 상한(`MAX_PENDING_HARD_CAP`)을 넘으면(발송이 오래 실패한 극단 상황)
    오래된 것부터 줄이되 **경고를 출력**합니다 — 조용히 사라지면 그 반영분은 영영
    확인할 수 없기 때문입니다. 목표치(`MAX_PENDING_ENTRIES`) 초과는 경고만 합니다.
    """
    pending = store.get("pending") or {}
    if not pending:
        store["pending"] = {}
        return store

    kept = {}
    duplicates = 0
    unreadable = 0
    for key, entry in pending.items():
        if not isinstance(entry, dict):
            unreadable += 1
            continue
        restored = _report_from_payload(entry.get("payload"))
        if restored is None:
            unreadable += 1
            continue
        sent = _sent_for_pending_entry(store, entry, key)
        if isinstance(sent, dict) and not _is_new_against_hashes(
            restored, _entry_known_hashes(sent)
        ):
            # 같은 내용이 이미 발송된 잔여물 — 남겨 두면 다음 실행이 중복 발송합니다.
            # 비교는 **발송한 payload 이력 전체**입니다 (마지막 1건만 보면 오래된
            # payload 재전송분을 중복으로 오판해 지워 버립니다).
            duplicates += 1
            continue
        kept[key] = entry

    if duplicates or unreadable:
        print(
            f"[정산 리포트] 미발송 payload 정리 — 이미 발송된 내용 {duplicates}건, "
            f"복원 불가 {unreadable}건 (미발송 내용은 보존)"
        )
    if len(kept) > MAX_PENDING_HARD_CAP:
        # 나이는 **생성 시각** 기준입니다 (재시도로 `at` 이 갱신돼도 항목이 젊어지지 않음).
        ordered = sorted(
            kept.items(), key=lambda kv: _pending_created_at(kv[1]), reverse=True
        )
        dropped = len(kept) - MAX_PENDING_HARD_CAP
        kept = dict(ordered[:MAX_PENDING_HARD_CAP])
        print(
            f"[경고] 미발송 payload 가 상한({MAX_PENDING_HARD_CAP}건)을 넘어 오래된 "
            f"{dropped}건을 정리합니다 — 발송 실패가 길게 이어졌습니다 "
            "(해당 정산 내용은 브로커 체결 이력에서 확인하세요)"
        )
    elif len(kept) > MAX_PENDING_ENTRIES:
        print(
            f"[정보] 미발송 payload {len(kept)}건 보관 중 — 재시도 전에는 지우지 않습니다"
        )
    store["pending"] = kept
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


def sent_entry(store, report):
    """같은 세션의 **발송 성공 이력** (보류 알림은 별도 키라 여기서는 None)."""
    if report.is_hold:
        return None
    entry = _normalized_store(store)["sent"].get(report.report_id)
    return entry if isinstance(entry, dict) else None


def report_already_sent(store, report):
    """이미 발송한 리포트인지 (성공 리포트와 보류 알림을 분리해 판정).

    성공 리포트는 **내용 지문까지 같을 때만** 억제합니다. 같은 세션에 뒤늦은 추가
    체결이 반영되면 내용(반영분/확정 T·순투입)이 달라지므로, 그때는 억제하지 않고
    `📌 정산 정정` 으로 별도 발송합니다 (`report_has_new_content()`).
    """
    store = _normalized_store(store)
    if report.is_hold:
        return hold_key(report) in store["holds"]
    entry = store["sent"].get(report.report_id)
    if not isinstance(entry, dict):
        return False
    knowns = _entry_known_hashes(entry)
    if not knowns:
        # 지문이 없는 기존 발송 이력 — 비교가 불가능하므로 기존처럼 억제합니다.
        return True
    return not _is_new_against_hashes(report, knowns)


# ── 미발송 payload 자리 (같은 세션 다중 payload 보존) ──────────────
# pending 키 = `{멱등 키}{구분자}{내용 지문}` 입니다. 같은 세션의 **서로 다른 payload**
# (1차 실패분 + 정정분, 정정 #N + #N+1)는 각자 자리를 가집니다 — 하나의 키에 덮어쓰면
# 먼저 보관한 반영분이 영영 사라집니다. 같은 내용이면 같은 자리를 재사용해 시도 횟수만
# 누적합니다. 구버전 항목은 멱등 키 자체를 키로 쓰므로 접두사 조회로 함께 찾습니다.
_PENDING_SLOT_SEP = ":"


def _payload_fingerprint(report):
    """pending payload 의 **내용 지문** — 같은 내용이면 같은 자리, 다르면 다른 자리."""
    hashes = _report_hashes(report)
    return f"{hashes['delta_hash']}{hashes['state_hash']}"


def _pending_base_key(report):
    """이 리포트의 멱등 키 (내용 자리 접미사가 붙어 있으면 떼어냅니다)."""
    base = str(getattr(report, "pending_key", "") or getattr(report, "report_id", "") or "")
    return base.split(_PENDING_SLOT_SEP, 1)[0]


def _pending_created_at(entry):
    """pending 항목이 담은 payload 의 **생성 시각** (레거시 항목은 `at` 로 fallback).

    왜 `at` 과 분리하는가: `at` 은 **마지막 발송 시도 시각**이라 재시도마다 갱신됩니다.
    정렬·최신 스냅샷 선택에 `at` 을 쓰면, 오래된 payload(A)의 재시도 실패가 A 를
    "가장 최근 내용"으로 둔갑시킵니다. 그러면 다음 델타 0 실행이 최신 내용(B) 대신
    A 와 비교해 **오정정**을 냅니다. 그래서 **변하지 않는 생성 시각**을 기준으로
    정렬하고, `at` 은 "언제 마지막으로 시도했는가"로만 씁니다.

    구버전 항목에는 `created_at` 이 없으므로 `at`(그 시점의 유일한 시각)으로 물러납니다.
    """
    entry = entry if isinstance(entry, dict) else {}
    created = str(entry.get("created_at") or "").strip()
    if created:
        return created
    return str(entry.get("at") or "")


def _pending_entries(store, base_key):
    """멱등 키(base)에 딸린 pending 항목 전부 — **생성 시각 오래된 순**."""
    base_key = str(base_key or "")
    if not base_key:
        return []
    entries = []
    for key, entry in _normalized_store(store)["pending"].items():
        key = str(key)
        if key != base_key and not key.startswith(base_key + _PENDING_SLOT_SEP):
            continue
        if isinstance(entry, dict):
            entries.append((key, entry))
    entries.sort(key=lambda kv: _pending_created_at(kv[1]))
    return entries


def _entry_fingerprint(entry):
    """pending 항목이 담은 payload 의 지문 (내용 지문이 없는 구버전 항목은 복원해 계산)."""
    stored = str((entry or {}).get("payload_key") or "")
    if stored:
        return stored
    restored = _report_from_payload((entry or {}).get("payload"))
    return _payload_fingerprint(restored) if restored is not None else ""


def _pending_slot_key(store, report):
    """이 payload 를 보관할 pending 키.

    같은 내용의 기존 항목이 있으면 그 자리를 재사용해 **시도 횟수**를 누적하고,
    내용이 다르면 새 자리를 씁니다 (덮어쓰기로 반영분이 사라지지 않게).
    """
    base_key = _pending_base_key(report)
    fingerprint = _payload_fingerprint(report)
    for key, entry in _pending_entries(store, base_key):
        if _entry_fingerprint(entry) == fingerprint:
            return key
    return f"{base_key}{_PENDING_SLOT_SEP}{fingerprint}"


def _sent_for_pending_entry(store, entry, key):
    """pending 항목과 짝이 되는 발송 이력 (멱등 키 기준, 없으면 구버전 키로 조회)."""
    report_key = str(
        (entry or {}).get("report_id") or str(key).split(_PENDING_SLOT_SEP, 1)[0]
    )
    sent = _normalized_store(store)["sent"]
    found = sent.get(report_key)
    if not isinstance(found, dict):
        found = sent.get(str(key))
    return found if isinstance(found, dict) else None


def restore_pending(store, report):
    """같은 세션의 **가장 오래된** 미발송 payload 로 리포트를 되살립니다 (없으면 None).

    정산은 이미 커밋됐으므로 재시도 실행의 리포트는 델타가 0입니다. 그대로 보내면
    "재시도 성공"만 남고 **실제 반영분이 사라집니다**. 그래서 1차 실패 시 저장해 둔
    재료를 그대로 다시 보냅니다. 보류(hold) 알림은 재시도 때 같은 사유로 재생성되므로
    되살리지 않습니다 (보류가 해소되면 새 성공 리포트를 보내는 것이 맞습니다).

    같은 세션에 서로 다른 payload 가 여러 자리에 남아 있으면 **오래된 것부터** 보냅니다
    (반영분이 시간 순서로 알려지고, 남은 내용은 다음 실행의 정정 경로가 이어서 보냅니다).
    동일 `created_at` 은 dict 삽입 순서(stable sort)로 결정됩니다 — 시각만으로 순서를
    기대하지 마세요.

    여기서 되살리는 것은 **같은 세션**의 미발송분입니다. 다른 세션의 미발송분은
    `pending_backlog()` 가 골라 별도 메시지로 재전송합니다.
    """
    if report.is_hold:
        return None
    for key, entry in _pending_entries(store, _pending_base_key(report)):
        restored = _report_from_payload(entry.get("payload"))
        if restored is None:
            continue
        if not restored.committed or restored.symbol != report.symbol:
            # 손상/불일치 payload — 엉뚱한 종목·보류 화면으로 나가면 안 됩니다.
            print(
                f"[정산 리포트] {report.symbol} 미발송 payload 가 유효하지 않아 "
                "저장 내용 재전송을 건너뜁니다 (이번 실행 내용으로 발송)"
            )
            continue
        # 멱등 키/세션은 이번 실행 기준을 유지합니다 (저장값이 손상돼도 키가 흔들리지 않게).
        restored.report_id = report.report_id
        restored.session_key = report.session_key
        restored.pending_key = str(key)
        restored.account_fingerprint = str(getattr(report, "account_fingerprint", "") or "")
        if not restored.settled_session:
            restored.settled_session = report.settled_session
        restored.retried = True
        restored.already_sent = False
        return restored
    return None


def latest_pending_report(store, report):
    """같은 세션 pending 중 **가장 최근** payload 를 되살립니다 (없으면 None).

    재전송 순서는 오래된 것부터지만(`restore_pending()`), "이번 실행 내용이 **새
    정보**인가"를 판단할 때 **가장 오래된** 것과 비교하면 이미 알려진 내용(그 뒤에
    보관된 최신 payload)을 델타 0 정정으로 다시 알리게 됩니다. 그래서 비교 기준은
    가장 최근 보관분입니다. "최근"은 **payload 생성 시각**(`_pending_created_at()`) 기준이며,
    마지막 시도 시각(`at`)이 아닙니다 — 재시도 실패로 `at` 이 갱신된 오래된 payload 가
    최신으로 둔갑하면 비교 기준이 뒤집힙니다. (발송 이력이 더 최근일 수 있으므로 `unsent`
    경로는 `_is_new_against_known()` 으로 발송 이력까지 함께 비교합니다.)
    동일 `created_at` 은 dict 삽입 순서(stable sort)의 역순으로 결정됩니다 — 시각만으로
    순서를 기대하지 마세요.
    """
    if report.is_hold:
        return None
    entries = _pending_entries(store, _pending_base_key(report))
    for key, entry in reversed(entries):
        restored = _report_from_payload((entry or {}).get("payload"))
        if restored is None or not restored.committed:
            continue
        if restored.symbol != report.symbol:
            continue
        return restored
    return None


def _known_contents(store, report, sent):
    """이미 알려진 내용(발송 이력 + 같은 세션 pending) **전부**의 지문 목록.

    비교 기준을 하나만 고르면 반대쪽에 이미 알려진 내용을 델타 0 정정으로 다시
    알리게 됩니다 — 발송 이력이 더 최근일 수도(1차 실패분이 남은 채 정정 성공),
    보관 payload 가 더 최근일 수도(정정 발송 실패) 있습니다. `at` 비교는 같은 초에
    기록되면 순서를 가릴 수 없으므로(테스트·재시도 실행에서 흔함) 시간 대신 **내용**을
    기준으로 삼습니다.

    Returns:
        list[dict]: `_report_hashes()` 결과 목록 (지문이 있는 항목만).
    """
    sent = sent if isinstance(sent, dict) else {}
    # 발송 이력은 **발송한 payload 이력 전체**를 기준으로 합니다. 마지막 1건만 보면
    # 오래된 payload 재전송 성공 뒤 최신 내용이 "새 정보"로 오판됩니다.
    knowns = _entry_known_hashes(sent)
    for key, entry in _pending_entries(store, _pending_base_key(report)):
        restored = _report_from_payload((entry or {}).get("payload"))
        if restored is None or not restored.committed:
            continue
        if restored.symbol != report.symbol:
            continue
        knowns.append(_report_hashes(restored))
    return knowns


def _is_new_against_known(store, report, sent):
    """이번 리포트가 **새 정보**인지 — 발송 이력·보관 payload **어느 것과도** 다를 때만.

    `unsent`(정정 실패 잔여물)가 남아 있는 경로에서 "현재 리포트를 정정으로 더 보낼지"를
    판단합니다. 이미 알려진 내용과 같으면 그 내용은 재전송 경로가 그대로 전달하므로
    델타 0 정정을 덧붙이면 소음입니다.
    """
    knowns = _known_contents(store, report, sent)
    if not knowns:
        return False  # 비교할 지문이 없으면 기존처럼 억제합니다.
    return all(report_has_new_content(report, known) for known in knowns)


def pending_backlog(store, symbol, *, broker="", account_fingerprint="", current_keys=()):
    """현재 세션이 **아닌** 과거 미발송 payload 를 세션별로 묶어 오래된 순서로 돌려줍니다.

    왜 필요한가: `pending` 키는 멱등 해시(broker·계좌지문·종목·정산세션·버전)에 내용 지문이
    붙은 형태라, **다른 세션**의 payload 는 다음날 리포트의 키로는 절대 조회되지 않습니다
    (`restore_pending()` 은 같은 세션만 봅니다). 그래서 오늘 리포트를 보내기 전에 종목
    state 의 `pending` 을 직접 훑어 **원래 정산 세션을 명시해** 재전송합니다.

    Args:
        store: 종목 state 의 `settlement_report` 키
        symbol: 종목 코드 (payload 종목과 일치해야 합니다)
        broker/account_fingerprint: 이번 실행의 계좌 식별자 — **다른 계좌의 미발송분을
            이 계좌 채널로 보내지 않기 위해** 일치하는 항목만 고릅니다. 계좌 정보가 없는
            레거시 항목은 구분할 수 없으므로 재전송하지 않고 건너뜁니다.
        current_keys: 이번 실행 리포트의 멱등 키/세션 키 — 같은 세션 미발송분은
            현재 리포트 재시도 경로가 처리하므로 제외합니다.

    Returns:
        list[dict]: [{"session_key", "session", "at", "reports": [SymbolReport]}]
            (오래된 세션 순)
    """
    store = _normalized_store(store)
    current_keys = {str(key or "") for key in (current_keys or ())}
    symbol = str(symbol or "").upper()
    groups = {}
    unverifiable = 0
    for key, entry in store["pending"].items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol") or "").upper() != symbol:
            continue
        if str(key) in current_keys:
            continue
        if str(entry.get("session_key") or "") in current_keys:
            continue
        if str(entry.get("broker") or "") != str(broker or ""):
            continue
        if str(entry.get("account") or "") != str(account_fingerprint or ""):
            # 계좌 구분이 없는(또는 다른 계좌의) 항목 — 섞으면 다른 계좌 내용이 노출됩니다.
            unverifiable += 1
            continue
        restored = _report_from_payload(entry.get("payload"))
        if restored is None or not restored.committed or restored.symbol != symbol:
            print(
                f"[정산 리포트] {symbol} 과거 미발송 payload 가 유효하지 않아 "
                "재전송을 건너뜁니다"
            )
            continue
        sent_before = _sent_for_pending_entry(store, entry, key)
        if isinstance(sent_before, dict) and not _is_new_against_hashes(
            restored, _entry_known_hashes(sent_before)
        ):
            # 이미 발송된 세션의 잔여 pending — **내용 지문이 같을 때만** 중복입니다.
            # 정정 알림이 실패해 남은 payload 는 발송 이력과 지문이 다르므로(추가 반영분)
            # 반드시 재전송해야 합니다. 키 존재만으로 건너뛰면 그 반영분이 영영 사라집니다.
            # 비교는 발송한 payload 이력 전체입니다 — 마지막 1건만 보면, 오래된 payload
            # 재전송이 끝난 뒤 최신 내용이 남은 항목을 중복으로 오판해 지워 버립니다.
            continue
        session_key = str(entry.get("session_key") or restored.session_key or "")
        # 멱등 키는 **원래 세션의 키**를 유지합니다 (보낸 이력도 그 키로 남겨야 다음 실행이
        # 같은 내용을 다시 보내지 않습니다). pending 자리(내용 지문 접미사)는 따로 기억합니다.
        restored.report_id = str(
            entry.get("report_id") or str(key).split(_PENDING_SLOT_SEP, 1)[0]
        )
        restored.session_key = session_key or restored.session_key
        restored.settled_session = restored.settled_session or str(entry.get("session") or "")
        restored.pending_key = str(key)
        restored.account_fingerprint = str(account_fingerprint or "")
        restored.backlog = True
        restored.retried = False
        restored.already_sent = False
        group = groups.get(session_key)
        if group is None:
            # 그룹 정렬은 **payload 생성 시각** 기준입니다 — 재시도로 `at`(마지막 시도)이
            # 갱신돼도 세션 순서가 뒤집히지 않아야 합니다.
            group = {
                "session_key": session_key,
                "session": restored.settled_session,
                "at": _pending_created_at(entry),
                "reports": [],
            }
            groups[session_key] = group
        group["reports"].append(restored)
        if _pending_created_at(entry) < group["at"]:
            group["at"] = _pending_created_at(entry)
    if unverifiable:
        print(
            f"[정산 리포트] {symbol} 계좌 구분이 없는 미발송 payload {unverifiable}건은 "
            "재전송하지 않습니다 (다른 계좌 내용 혼입 방지)"
        )
    return sorted(groups.values(), key=lambda group: str(group.get("at") or ""))


def _unreported_pending(store, report, sent):
    """같은 세션 **정정 발송이 실패해** 남은 payload 목록 (이미 발송된 내용과 다를 때만).

    정정 알림 자체가 실패하면 그 payload 가 `pending` 에 남습니다. 다음 실행의 정산은
    델타가 0일 수 있으므로, 남은 payload 를 먼저 되살려야 그 반영분이 사라지지 않습니다.
    발송 이력과 **내용 지문이 같은** 항목은 이미 나간 내용이므로 건너뜁니다.
    여러 자리에 남아 있으면 **오래된 순서**로 모두 돌려줍니다 (정정 메시지 하나에 담습니다).
    """
    pending = []
    for key, entry in _pending_entries(store, _pending_base_key(report)):
        restored = _report_from_payload(entry.get("payload"))
        if restored is None or not restored.committed or restored.symbol != report.symbol:
            continue
        if not _is_new_against_hashes(restored, _entry_known_hashes(sent)):
            # 이미 발송한 내용이 남아 있을 뿐 — 새 정보가 없습니다.
            # (비교는 발송한 payload 이력 전체 — 마지막 1건만 보면 오래된 payload 를
            #  재전송한 뒤 남은 최신 내용을 중복으로 오판해 영영 버립니다.)
            continue
        restored.report_id = report.report_id
        restored.session_key = report.session_key or restored.session_key
        restored.settled_session = restored.settled_session or report.settled_session
        restored.pending_key = str(key)
        restored.account_fingerprint = str(getattr(report, "account_fingerprint", "") or "")
        restored.retried = False
        restored.already_sent = False
        pending.append(restored)
    return pending


def _next_revision(store, report):
    """이 리포트를 발송 이력에 기록할 때 올라갈 리비전 — `_apply_mark()` 와 **같은 식**.

    메시지에 표기한 리비전과 state 에 기록된 리비전이 어긋나면 다음 정정의 번호가
    건너뛴(또는 되돌아간) 것처럼 보입니다. 그래서 정정 리비전은 상수를 쓰지 않고
    이 식으로 계산합니다.
    """
    previous = _normalized_store(store)["sent"].get(report.report_id)
    previous = previous if isinstance(previous, dict) else {}
    return _as_int(previous.get("revision"), 0) + 1


def _mark_correction(report, revision):
    """리포트를 **정정 알림** 대상으로 표시합니다 (일일 리포트 본문에서는 제외)."""
    report.correction = True
    report.revision = _as_int(revision, 0)
    report.already_sent = True
    return report


def _apply_mark(store, report, *, ok, at_iso, reason=""):
    """state 발송 상태에 이번 발송 결과를 반영합니다 (거래 필드는 손대지 않음).

    실패 시에는 보류 알림을 제외하고 **본문 재료(payload)를 `pending` 에 저장**합니다
    (재시도가 빈 리포트를 보내지 않도록). `last_failure` 는 **감사용 요약**이며 재시도
    근거는 `pending` 입니다 (소비처는 `restore_pending()`/`pending_backlog()`).

    저장 자리는 **payload 내용별**로 나뉩니다(`_pending_slot_key()`). 같은 내용이면 기존
    자리에 시도 횟수만 누적하고, 내용이 다르면 새 자리를 씁니다 — 하나의 키에 덮어쓰면
    1차 실패분과 정정분(또는 정정 #N + #N+1) 중 하나가 영영 사라집니다.

    시간은 **두 축으로 분리 기록**합니다: `created_at` = payload **생성 시각**(불변),
    `at` = **마지막 시도 시각**(시도마다 갱신). 정렬·최신 스냅샷 선택은 `created_at` 을
    씁니다. `at` 하나만 쓰면 오래된 payload(A)의 재시도 실패가 A 를 "가장 최근 내용"으로
    둔갑시켜, 다음 델타 0 실행이 최신 내용(B) 대신 A 와 비교해 오정정을 냅니다.

    성공 항목에는 **내용 지문(delta_hash/state_hash)과 리비전**을 함께 남깁니다 —
    같은 세션의 추가 반영분을 다음 실행에서 `📌 정산 정정` 으로 구분하기 위해서입니다.
    지문은 두 축으로 **분리 기록**합니다: `payloads` = 실제로 발송한 payload 지문 이력,
    `delta_hash`/`state_hash` = **최신 보고 스냅샷 기준선**(되살린 오래된 payload 로는
    되돌리지 않음). 하나로 합치면 오래된 payload 재전송 성공이 기준선을 과거로 회귀시켜
    다음 델타 0 실행이 오정정을 냅니다.
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
            previous = store["sent"].get(report.report_id)
            previous = previous if isinstance(previous, dict) else {}
            revision = _as_int(previous.get("revision"), 0) + 1
            if getattr(report, "correction", False):
                # 정정 메시지는 본문에 리비전을 **표기**합니다. 앞선 덩어리(같은 멱등 키의
                # 일일 리포트) 발송이 실패하면 규칙만으로는 번호가 내려가 표기와 state 가
                # 어긋나므로, 표기값 이상으로만 올립니다.
                revision = max(revision, _as_int(getattr(report, "revision", 0), 0))
            hashes = _report_hashes(report)
            entry = {
                "at": at_iso,
                "symbol": report.symbol,
                "session": report.settled_session,
                "session_key": getattr(report, "session_key", ""),
                "broker": report.broker,
                "account": str(getattr(report, "account_fingerprint", "") or ""),
                "retried": bool(getattr(report, "retried", False)),
                "backlog": bool(getattr(report, "backlog", False)),
                "correction": bool(getattr(report, "correction", False)),
                "version": REPORT_VERSION,
                # 같은 세션 재보고 판정용 — 리비전/내용 해시를 발송 이력에 함께 남깁니다.
                "revision": revision,
            }
            baseline = _baseline_hashes(previous)
            if baseline and _is_restored_payload(report):
                # **오래된 payload 재전송 성공**이 최신 스냅샷 기준선을 과거로 되돌리면
                # 다음 델타 0 실행이 이미 알려진 최신 내용을 "새 정보"로 오판해 정정을
                # 냅니다. 그래서 되살린 payload 는 `payloads`(발송한 payload 기록)에만
                # 남기고 기준선(`delta_hash`/`state_hash`)은 그대로 둡니다.
                entry.update(baseline)
            else:
                # 이번 실행에서 새로 계산한 스냅샷 — 항상 최신 기준선입니다.
                entry.update(hashes)
            # 발송한 payload 기록은 **별도로 쌓습니다** — 기준선 1건만 남기면 오래된 내용을
            # 다시 보낸 사실이 사라져 중복 판정이 흔들립니다 (중복 방지는 유지).
            entry["payloads"] = _append_payload_history(previous, hashes, at_iso)
            store["sent"][report.report_id] = entry
        # 발송됐으므로 미발송 payload 는 지웁니다 (다음 세션에서 다시 보내지 않도록).
        # ⚠️ 보류 알림 성공으로는 지우지 않습니다 — 같은 report_id 를 공유하기 때문에
        #    지우면 아직 못 보낸 **커밋된 정산 내용**이 사라집니다.
        if not report.is_hold:
            sent_key = str(getattr(report, "pending_key", "") or "") or _pending_slot_key(
                store, report
            )
            store["pending"].pop(sent_key, None)
        store.pop("last_failure", None)
    else:
        if not report.is_hold:
            # 같은 세션의 **다른 내용** payload 를 덮어쓰지 않도록 내용별 자리에 보관합니다
            # (1차 실패분 + 정정분, 정정 #N + #N+1 이 서로를 지우면 반영분이 사라집니다).
            key = _pending_slot_key(store, report)
            previous = store["pending"].get(key) or {}
            store["pending"][key] = {
                # payload **생성 시각** — 재시도해도 바뀌지 않습니다. 정렬·최신 스냅샷
                # 선택이 이 값을 씁니다 (아래 `at` 을 쓰면 재시도가 순서를 뒤집습니다).
                "created_at": _pending_created_at(previous) or at_iso,
                # **마지막 시도 시각** — 시도마다 갱신됩니다 (감사·디버깅용).
                "at": at_iso,
                "symbol": report.symbol,
                "session": report.settled_session,
                "session_key": getattr(report, "session_key", ""),
                # 과거 세션 미발송분을 **계좌·종목별로** 골라내기 위한 식별자
                "broker": str(getattr(report, "broker", "") or ""),
                "account": str(getattr(report, "account_fingerprint", "") or ""),
                # 키에 붙은 내용 자리와 별개로 멱등 키를 남깁니다 (발송 이력 대조용).
                "report_id": report.report_id,
                "payload_key": _payload_fingerprint(report),
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


def persist_marks(batches, now=None):
    """발송 결과를 state 에 **한 번의 파일 쓰기**로 기록합니다.

    Args:
        batches: `[{"symbol": str, "reports": [SymbolReport], "ok": bool, "reason": str}]`
            **메시지 단위 목록**입니다 (순서 보존). 같은 종목이 여러 메시지에 나오면
            (일일 리포트 + 정정 알림) 적용 순서대로 리비전이 올라가야 하므로 dict 가 아니라
            순서 있는 목록을 받습니다. `{symbol: {...}}` 형태(구 호출)도 허용합니다.

    실패해도 예외를 던지지 않습니다 (리포트는 정산의 부수 기능).
    """
    if not batches:
        return False
    if isinstance(batches, dict):
        batches = [dict(mark, symbol=symbol) for symbol, mark in batches.items()]
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
            for batch in batches:
                symbol = str((batch or {}).get("symbol") or "")
                if not symbol:
                    continue
                loaded = updates.get(symbol)
                if loaded is None:
                    loaded = load_state(symbol)
                    if not isinstance(loaded, dict) or loaded.get("_state_unavailable"):
                        # 상태 파일에 없는 종목은 새 레코드를 만들지 않습니다.
                        continue
                store = loaded.get(REPORT_STATE_KEY)
                for report in batch.get("reports") or []:
                    store = _apply_mark(
                        store,
                        report,
                        ok=bool(batch.get("ok")),
                        at_iso=at_iso,
                        reason=batch.get("reason", ""),
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


def _group_by_symbol(reports):
    """리포트 목록을 종목별로 묶습니다 (발송 상태 기록 단위)."""
    grouped = {}
    for report in reports or []:
        grouped.setdefault(report.symbol, []).append(report)
    return grouped


def _suppressed_reports(reports, corrections):
    """일일 리포트에서 빠진(=이미 발송·정정 대상) 리포트 목록 — 안내 문구용.

    `reports` 만 보면 `restore_pending()` 경로의 안내가 빠집니다. 그 경로는 현재
    리포트를 보관 payload 로 **교체**(`reports[index] = restored`)하기 때문에, 정정
    대상이 된 이번 실행 리포트가 `reports` 에 남지 않습니다. 그래서 정정 목록까지
    합쳐 "정산 정정으로 별도 발송" 안내가 항상 나가게 합니다.
    같은 객체가 양쪽에 있으면 한 번만 셉니다 (정정 메시지는 따로 나갑니다).
    """
    found = []
    for candidate in list(reports or []) + list(corrections or []):
        if not getattr(candidate, "already_sent", False):
            continue
        if any(candidate is kept for kept in found):
            continue
        found.append(candidate)
    return found


def _chunk_reports(reports, render, limit=None):
    """리포트 목록을 **메시지 크기 기준**으로 나눕니다 (잘림 유실 방지).

    한 메시지가 텔레그램 한도를 넘으면 `_clip()` 이 뒷부분을 잘라내는데, 그대로
    "전량 발송 완료"로 기록하면 **잘려 나간 반영분이 영영 사라집니다**. 그래서 한
    메시지에 담기는 만큼만 묶고 나머지는 다음 메시지로 보냅니다.

    Args:
        render: `render(reports, clip=False)` → **자르지 않은** 메시지 문자열
        limit: 한 메시지 최대 글자 수 (기본 `MAX_MESSAGE_CHARS`)

    Returns:
        list[list[SymbolReport]] — 각 덩어리가 메시지 하나가 됩니다 (빈 덩어리 없음).
    """
    limit = MAX_MESSAGE_CHARS if limit is None else int(limit)
    chunks = []
    pending = []
    for report in reports or []:
        if pending and len(render(pending + [report], clip=False)) > limit:
            chunks.append(pending)
            pending = [report]
        else:
            pending.append(report)
    if pending:
        chunks.append(pending)
    return chunks


def _send_chunks(chunks, render, send):
    """덩어리별로 **따로** 발송하고 결과를 그대로 돌려줍니다 (순서 보존).

    Returns:
        list[tuple[list[SymbolReport], bool]] — (덩어리, 발송 성공 여부)
    """
    outcomes = []
    for chunk in chunks:
        if len(chunk) == 1 and len(render(chunk, clip=False)) > MAX_MESSAGE_CHARS:
            # 한 종목만으로도 한도를 넘으면 더 나눌 수 없습니다 — 잘림을 경고로 남깁니다.
            print(
                f"[정산 리포트] {getattr(chunk[0], 'symbol', '')} 단일 메시지가 길이 제한"
                f"({MAX_MESSAGE_CHARS}자)을 넘어 일부가 생략됩니다 "
                "(종목 하나로는 더 나눌 수 없음)"
            )
        outcomes.append((chunk, send(render(chunk))))
    return outcomes


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
        dict: {"sent": [...], "failed": [...], "retried": [...], "skipped": [사유...],
               "corrected": [...], "corrections_failed": [...],
               "resent_previous": [...], "resent_previous_failed": [...]}
            - sent/failed: 이번 세션 일일 리포트 결과
            - corrected: 같은 세션 추가 반영분(`📌 정산 정정`) 발송 종목
            - resent_previous: **과거 세션** 미발송분(`📨 미발송 정산 재전송`) 발송 종목

    발송 순서(모두 서로 다른 메시지):
        1) 과거 세션 미발송분 — 오래된 세션부터 (`📨 미발송 정산 재전송`)
        2) 이번 세션 일일 리포트 (`📊 일일 정산 리포트`)
        3) 이번 세션 추가 반영분 (`📌 정산 정정`)
    각 메시지는 **길이 한도를 넘으면 크기 기준으로 나눠** 여러 번 보냅니다. 완료 표시는
    **실제로 전송된 덩어리의 payload 만** 하므로, 실패한 덩어리는 그대로 `pending` 에 남아
    다음 실행에서 재시도됩니다 (잘린 메시지를 "전량 발송 완료"로 기록하지 않습니다).
    한 메시지의 실패가 다른 메시지 발송을 막지 않습니다 (실패분은 pending 으로 남아 재시도).
    """
    result = {
        "sent": [],
        "failed": [],
        "retried": [],
        "skipped": [],
        "corrected": [],
        "corrections_failed": [],
        "resent_previous": [],
        "resent_previous_failed": [],
    }
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
        account_key = str(account_fingerprint or "")
        current = []  # 이번 세션 일일 리포트에 실을 리포트
        corrections = []  # 같은 세션 추가 반영분 (정정 알림)
        stores = {}  # 종목별 발송 상태 (state 읽기 1회)

        def _send(message):
            """발송 1회 — 예외는 여기서 흡수합니다 (정산 롤백 금지)."""
            try:
                return bool(sender(message))
            except Exception as error:
                print(
                    f"[정산 리포트] 발송 예외({type(error).__name__}) — "
                    "정산 결과/주문에는 영향 없습니다"
                )
                return False

        # 1) 이번 실행 리포트 판정 — 발송 / 억제 / **같은 세션 정정**
        #    (종목별 state 의 전용 키만 읽습니다 — 거래 상태는 읽기만.)
        for index, report in enumerate(reports):
            if not str(getattr(report, "session_key", "") or "").strip():
                report.session_key = session_bucket(
                    getattr(report, "settled_session", "") or settled_session, now
                )
            if not report.report_id:
                report.report_id = report_id(
                    broker, account_fingerprint, report.symbol, report.session_key
                )
            if not str(getattr(report, "account_fingerprint", "") or "").strip():
                report.account_fingerprint = account_key
            store = stores.get(report.symbol)
            if store is None:
                store = load_report_store(report.symbol)
                stores[report.symbol] = store

            if report.is_hold:
                # 보류 알림은 사유별 1회 — 기존 동작 그대로입니다.
                if report_already_sent(store, report):
                    report.already_sent = True
                    continue
                current.append(report)
                continue

            sent = sent_entry(store, report)
            if sent is None:
                # 1차 발송에 실패한 리포트는 저장해 둔 내용으로 되살립니다 (빈 리포트 방지).
                restored = restore_pending(store, report)
                if restored is None:
                    current.append(report)
                    continue
                reports[index] = restored
                current.append(restored)
                # 되살린 내용 **이후에** 이번 실행에서 새로 반영된 정산이 있으면 정정으로
                # 따로 알립니다 (그대로 두면 이번 실행의 반영분이 어디에도 남지 않습니다).
                # 비교 기준은 **가장 최근** 보관분입니다 — 가장 오래된 것과 비교하면 이미
                # 알려진 내용을 델타 0 정정으로 다시 알리게 됩니다.
                baseline = latest_pending_report(store, report) or restored
                if report_has_new_content(report, _report_hashes(baseline)):
                    # 리비전은 상수 대신 persist 규칙(previous.revision+1)에서 계산합니다.
                    # 되살린 payload 가 같은 멱등 키로 먼저 기록되므로 그 다음 번호입니다.
                    corrections.append(
                        _mark_correction(report, _next_revision(store, report) + 1)
                    )
                continue

            base_revision = _as_int(sent.get("revision"), 0)
            # 정정 알림 자체가 실패해 남은 payload 가 있으면 그 내용을 먼저 다시 보냅니다.
            unsent = _unreported_pending(store, report, sent)
            if unsent:
                revision = base_revision
                for pending_report in unsent:
                    revision += 1
                    corrections.append(_mark_correction(pending_report, revision))
                # 비교 기준은 **이미 알려진 모든 내용**(발송 이력 + 보관 payload)입니다.
                # 보관 payload 만 보면 발송 이력이 더 최신인 경우(1차 실패분이 남은 채
                # 정정 성공) 이미 알려진 내용을 델타 0 정정으로 다시 알립니다.
                if _is_new_against_known(store, report, sent):
                    corrections.append(_mark_correction(report, revision + 1))
                continue
            if _is_new_against_known(store, report, sent):
                # 같은 세션인데 내용이 실질적으로 달라졌습니다 (뒤늦은 추가 체결 등).
                # 비교는 **이미 알려진 모든 내용**(발송한 payload 이력 + 보관 payload)
                # 기준입니다 — 마지막 1건만 보면, 오래된 payload 재전송 성공 뒤 이미 알려진
                # 최신 내용을 델타 0 정정으로 다시 알립니다.
                corrections.append(_mark_correction(report, _next_revision(store, report)))
                continue
            # 반영분도 확정 상태도 그대로 — 같은 세션 중복 발송을 억제합니다.
            report.already_sent = True

        # 2) 과거 세션 미발송분 — pending 키에 세션이 들어 있어 현재 키로는 조회되지 않습니다.
        #    오늘 리포트보다 **먼저**, 원래 정산 세션을 명시한 별도 메시지로 보냅니다.
        current_keys = {str(r.report_id or "") for r in reports}
        current_keys |= {str(getattr(r, "session_key", "") or "") for r in reports}
        backlog_groups = []
        seen_symbols = set()
        for report in reports:
            if report.symbol in seen_symbols:
                continue
            seen_symbols.add(report.symbol)
            backlog_groups.extend(
                pending_backlog(
                    stores.get(report.symbol) or {},
                    report.symbol,
                    broker=broker,
                    account_fingerprint=account_key,
                    current_keys=current_keys,
                )
            )
        backlog_groups.sort(key=lambda group: str(group.get("at") or ""))
        backlog_groups = backlog_groups[:MAX_BACKLOG_SESSIONS_PER_RUN]

        if not current and not corrections and not backlog_groups:
            result["skipped"].append("already_sent")
            print(
                "[정산 리포트] 이번 정산 세션 리포트는 이미 발송되었습니다 "
                "(broker+계좌지문+종목+세션 멱등 키 + 내용 지문 — 중복 방지)"
            )
            return result

        # 3) 발송 + 상태 기록 목록 (메시지 순서 = 기록 순서 → 리비전이 순서대로 올라갑니다)
        batches = []

        for group in backlog_groups:
            items = list(group.get("reports") or [])
            if not items:
                continue
            session_label = str(group.get("session") or group.get("session_key") or "")

            def render_backlog(chunk, clip=True, _now=now):
                return format_backlog_message(
                    build_daily_report(chunk, now=_now), clip=clip
                )

            delivered, failed = [], []
            for chunk, ok in _send_chunks(
                _chunk_reports(items, render_backlog), render_backlog, _send
            ):
                for symbol, symbol_reports in _group_by_symbol(chunk).items():
                    batches.append(
                        {
                            "symbol": symbol,
                            "reports": symbol_reports,
                            "ok": ok,
                            "reason": "" if ok else "notify_failed",
                        }
                    )
                # 실제로 전송된 덩어리의 payload 만 발송 완료로 표시합니다.
                (delivered if ok else failed).extend(chunk)
            result["resent_previous"].extend(item.symbol for item in delivered)
            result["resent_previous_failed"].extend(item.symbol for item in failed)
            if delivered:
                print(
                    f"[정산 리포트] 과거 미발송 정산 재전송 — 세션 {session_label or '미확인'} "
                    f"{len(delivered)}건 ({', '.join(item.symbol for item in delivered)})"
                )
            if failed:
                print(
                    f"[정산 리포트] 과거 미발송 정산 재전송 실패 — 세션 "
                    f"{session_label or '미확인'} {len(failed)}건은 state 에 보관해 다음 실행에서 "
                    "다시 시도합니다"
                )

        if current:
            # 본문에서 생략한 종목(이미 발송·정정 대상)은 **합계에서도** 빼야 합니다.
            # 넣으면 같은 세션 반영분이 본문 1회 + 합계 1회로 두 번 더해집니다.
            # 정정 목록까지 합치는 이유: restore 경로는 현재 리포트를 되살린 payload 로
            # 교체하므로 정정 대상이 `reports` 에 남지 않아 안내가 빠집니다.
            already = _suppressed_reports(reports, corrections)

            def render_daily(chunk, clip=True, _now=now):
                # 생략 안내(이미 발송/정정)는 전체 기준을 유지하고, 본문 섹션과 합계는
                # 이 덩어리 기준입니다 (덩어리 합계를 모으면 전체 합계가 됩니다).
                return format_report_message(
                    build_daily_report(
                        list(chunk) + already,
                        now=_now,
                        include_already_sent=False,
                    ),
                    clip=clip,
                )

            delivered, failed = [], []
            for chunk, ok in _send_chunks(
                _chunk_reports(current, render_daily), render_daily, _send
            ):
                for report in chunk:
                    batches.append(
                        {
                            "symbol": report.symbol,
                            "reports": [report],
                            "ok": ok,
                            "reason": "" if ok else "notify_failed",
                        }
                    )
                (delivered if ok else failed).extend(chunk)
            retried = [r.symbol for r in delivered if getattr(r, "retried", False)]
            result["sent"] = [r.symbol for r in delivered]
            result["retried"] = retried
            result["failed"] = [r.symbol for r in failed]
            if delivered:
                print(
                    f"[정산 리포트] 발송 완료 — {len(delivered)}건 "
                    f"({', '.join(r.symbol for r in delivered)})"
                )
                if retried:
                    print(
                        f"[정산 리포트] 1차 발송 실패분 재전송: {', '.join(retried)} "
                        "(저장해 둔 정산 내용으로 발송)"
                    )
            if failed:
                print(
                    f"[정산 리포트] 발송 실패 — {len(failed)}건은 다음 실행에서 재시도합니다 "
                    "(정산 내용은 state 에 보관 — 재시도는 그 내용으로 발송)"
                )

        if corrections:
            # 정정 알림은 **정정 델타만** 합산해야 하므로 이미 발송 표시된 리포트를 그대로
            # 합계에 넣습니다 (일일 리포트와 집합이 다릅니다).
            def render_correction(chunk, clip=True, _now=now):
                return format_correction_message(
                    build_daily_report(chunk, now=_now), clip=clip
                )

            delivered, failed = [], []
            for chunk, ok in _send_chunks(
                _chunk_reports(corrections, render_correction), render_correction, _send
            ):
                for report in chunk:
                    batches.append(
                        {
                            "symbol": report.symbol,
                            "reports": [report],
                            "ok": ok,
                            "reason": "" if ok else "notify_failed",
                        }
                    )
                # 잘려 나가는 대신 **전송된 덩어리만** 완료로 남깁니다 (실패분은 pending).
                (delivered if ok else failed).extend(chunk)
            result["corrected"] = [r.symbol for r in delivered]
            result["corrections_failed"] = [r.symbol for r in failed]
            if delivered:
                print(
                    f"[정산 리포트] 정산 정정 알림 — {len(delivered)}건 "
                    f"({', '.join(r.symbol for r in delivered)}) — "
                    "같은 세션에 추가 반영분이 확인되었습니다"
                )
            if failed:
                print(
                    f"[정산 리포트] 정산 정정 알림 발송 실패 — {len(failed)}건은 내용을 state 에 "
                    "보관해 다음 실행에서 재시도합니다"
                )

        # 4) 발송 상태 기록 (성공했을 때만 '발송됨' — 실패는 재시도 가능 상태)
        persist_marks(batches, now=now)
        return result
    except Exception as error:  # 어떤 경우에도 정산 흐름을 깨지 않습니다
        print(
            f"[정산 리포트] 처리 중 예외({type(error).__name__}) — "
            "정산 결과/주문에는 영향 없습니다"
        )
        result["failed"].append("error")
        return result
