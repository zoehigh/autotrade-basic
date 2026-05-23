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
"""

import os
from datetime import datetime, timezone, timedelta

from telegram import send_telegram

# 한국 표준시 (UTC+9)
KST = timezone(timedelta(hours=9))

# 조용한 시간 범위: 밤 10시(22시) ~ 아침 8시
QUIET_START_HOUR = 22
QUIET_END_HOUR = 8


def _is_quiet_hours() -> bool:
    """현재 한국 시간이 조용한 시간대(밤 10시 ~ 아침 8시)인지 확인합니다."""
    now_kst = datetime.now(KST)
    hour = now_kst.hour
    # 22:00 이상이거나 08:00 미만이면 조용한 시간
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def notify(message: str, urgent: bool = False) -> bool:
    """
    알림 메시지를 발송합니다.

    조용한 시간(밤 10시 ~ 아침 8시)에는 일반 메시지를 건너뜁니다.
    urgent=True인 에러 메시지는 시간에 관계없이 즉시 발송합니다.

    Args:
        message: 전송할 메시지 내용
        urgent: True이면 조용한 시간에도 즉시 발송 (에러 메시지용)

    Returns:
        bool: 전송 성공 시 True, 건너뜀이나 실패 시 False
    """
    quiet_hours_enabled = os.getenv("QUIET_HOURS", "false").strip().lower() == "true"

    if quiet_hours_enabled and not urgent and _is_quiet_hours():
        now_kst = datetime.now(KST)
        print(f"[조용한 시간] 메시지 발송 건너뜀 (KST {now_kst.strftime('%H:%M')}): {message[:50]}...")
        return False

    return send_telegram(message)
