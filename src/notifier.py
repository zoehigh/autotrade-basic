"""
알림을 보내는 진입점 모듈

이 파일은 알림 발송의 비즈니스 로직을 담당합니다.
텔레그램 API 호출은 telegram.py가 담당하고,
이 파일은 "언제 보낼지"를 결정합니다.

조용한 시간(한국 시간 밤 10시 ~ 아침 8시)에는
일반 메시지 발송을 건너뜁니다.
에러 메시지(urgent=True)는 시간에 관계없이 즉시 발송합니다.

QUIET_HOURS 환경 변수로 이 기능을 켜고 끌 수 있습니다.
  QUIET_HOURS=true  → 조용한 시간 기능 활성화
  QUIET_HOURS=false → 항상 즉시 발송 (기본값)

**메시지 채널 분리 (주문번호 노출 방지)**

같은 알림이라도 "개인 채팅"과 "공용 채널"에 보여줄 내용이 다를 수 있습니다.
주문번호 같은 원문 식별자는 개인 채팅에만 보내고, 공용 채널에는 가명(`ord#`)으로
마스킹한 `message`만 보냅니다. 대상이 개인 채팅인지 판정하는 근거는
`TELEGRAM_PERSONAL_CHAT_IDS`(쉼표 구분 chat id 목록)뿐입니다.

  TELEGRAM_PERSONAL_CHAT_IDS=123456789,-1001234567890

판정할 수 없으면(환경변수 없음/비어 있음, 현재 chat id가 목록에 없음) **개인 아님**으로
보고 보수적으로 마스킹된 `message`를 보냅니다.
"""

import os
from datetime import datetime, timezone, timedelta

from telegram import send_telegram

# 한국 표준시 (UTC+9)
KST = timezone(timedelta(hours=9))

# 조용한 시간 범위: 밤 10시(22시) ~ 아침 8시
QUIET_START_HOUR = 22
QUIET_END_HOUR = 8

# 개인 채팅으로 취급할 chat id 목록 (쉼표 구분)
PERSONAL_CHAT_IDS_ENV = "TELEGRAM_PERSONAL_CHAT_IDS"


def _env_chat_id() -> str:
    """현재 알림을 받을 chat id를 반환합니다 (앞뒤 공백 제거, 없으면 빈 문자열)."""
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _personal_chat_ids() -> set[str]:
    """`TELEGRAM_PERSONAL_CHAT_IDS` 를 chat id 집합으로 파싱합니다.

    쉼표로 나누고 앞뒤 공백을 지운 뒤 빈 항목은 버립니다.
    환경변수가 없거나 비어 있으면 빈 집합이 되어, 어떤 채팅도 개인 채팅이 아닙니다.
    """
    raw = os.getenv(PERSONAL_CHAT_IDS_ENV, "")
    chat_ids = set()
    for part in raw.split(","):
        chat_id = part.strip()
        if chat_id:
            chat_ids.add(chat_id)
    return chat_ids


def is_personal_chat() -> bool:
    """현재 알림 대상(chat id)이 개인 채팅인지 판정합니다.

    **판정 불가는 "개인 아님"으로 처리합니다** (주문번호 원문 노출을 막는 쪽이 안전).
      - `TELEGRAM_CHAT_ID` 가 없으면 → False
      - `TELEGRAM_PERSONAL_CHAT_IDS` 가 목록에 없거나 비어 있으면 → False

    Returns:
        bool: 개인 채팅이면 True, 그 외(공용 채널/판정 불가)는 False
    """
    chat_id = _env_chat_id()
    if not chat_id:
        return False
    return chat_id in _personal_chat_ids()


def _is_quiet_hours() -> bool:
    """현재 한국 시간이 조용한 시간대(밤 10시 ~ 아침 8시)인지 확인합니다."""
    now_kst = datetime.now(KST)
    hour = now_kst.hour
    # 22:00 이상이거나 08:00 미만이면 조용한 시간
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def notify(
    message: str,
    urgent: bool = False,
    telegram_message: str | None = None,
) -> bool:
    """
    알림 메시지를 발송합니다.

    조용한 시간(밤 10시 ~ 아침 8시)에는 일반 메시지를 건너뜁니다.
    urgent=True인 에러 메시지는 시간에 관계없이 즉시 발송합니다.

    **채널 분리**: 대상이 개인 채팅(`is_personal_chat()`)이고 `telegram_message`가
    주어지면 `telegram_message`(원문 주문번호 포함 가능)를 보내고, 그 외에는
    `message`(가명 `ord#` 로 마스킹된 본문)를 보냅니다.

    Args:
        message: 공용 채널용 메시지 내용 (마스킹된 본문)
        urgent: True이면 조용한 시간에도 즉시 발송 (에러 메시지용)
        telegram_message: 개인 채팅에만 보낼 메시지 (없으면 `message` 사용)

    Returns:
        bool: 전송 성공 시 True, 건너뜀이나 실패 시 False
    """
    quiet_hours_enabled = os.getenv("QUIET_HOURS", "false").strip().lower() == "true"

    if quiet_hours_enabled and not urgent and _is_quiet_hours():
        now_kst = datetime.now(KST)
        # 건너뛴 메시지의 본문·주문번호는 절대 출력하지 않습니다 (길이만 남깁니다).
        print(
            f"[조용한 시간] 메시지 발송 건너뜀 (KST {now_kst.strftime('%H:%M')}, "
            f"본문 {len(message)}자) — 본문은 출력하지 않습니다"
        )
        return False

    text = message
    if telegram_message is not None and is_personal_chat():
        text = telegram_message

    return send_telegram(text)
