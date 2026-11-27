#!/usr/bin/env python3
"""Actions 실행 전 세션 게이트 — 봇을 실행하기 **전에** 주문/정산 가능 여부를 판정합니다.

읽기 전용입니다 (브로커 호출·state 저장 없음). 판정은 봇과 같은 XNYS 캘린더 함수
(`broker.market_utils`)를 씁니다. 표준출력에 `key=value` 줄을 냅니다:

    window=open|closed      주문 대상 세션에 주문할 수 있는지
    session=YYYY-MM-DD      주문 대상 거래일(ET)
    reason=...              판정 사유 (한 줄)
    wait_seconds=N          프리장 개장까지 예상 대기 (closed 면 0)
    settlement_due=true|false   직전 마감 세션의 애프터아워가 끝나 정산할 수 있는지
    settled_session=YYYY-MM-DD  그 직전 마감 세션
    state_hold=...          복원한 state 에 남은 보류 표식 (없으면 빈 값)

캘린더 오류·손상된 state 파일은 **0 이 아닌 종료코드**로 끝냅니다. 판정 불가를
SKIP 으로 바꾸면 장애가 정상으로 숨기 때문입니다.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from broker.market_utils import (  # noqa: E402
    pre_market_wait_seconds,
    us_order_window,
    us_session_after_hours_end,
    us_session_in_progress,
    us_session_last_closed_date,
)

# 이전 실행이 남긴 보류 — 남아 있으면 "정상 SKIP" 으로 끝내지 않습니다.
HOLD_KEYS = ("pending_order_intent", "pending_order_batch", "balance_mismatch")


def state_hold(state_path, symbols=""):
    """state 파일의 보류 표식을 `SYMBOL:key` 목록 문자열로 돌려줍니다 (읽기 전용).

    파일이 없으면 판단할 대상이 없어 빈 문자열입니다. 파일이 있는데 읽을 수 없으면
    예외를 그대로 올립니다.

    Parameters:
        state_path: `.state.json` 경로.
        symbols: `SYMBOLS` 형식("TQQQ:NAS,SOXL:AMS"). 비면 파일의 모든 종목.
    """
    path = Path(state_path)
    if not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("state 파일 형식이 올바르지 않습니다 (객체가 아님)")
    wanted = {
        item.split(":")[0].strip().upper()
        for item in str(symbols or "").split(",")
        if item.strip()
    }
    holds = []
    for symbol, record in data.items():
        if wanted and str(symbol).upper() not in wanted:
            continue
        if not isinstance(record, dict):
            raise ValueError(f"state 파일의 {symbol} 기록이 손상됐습니다 (객체가 아님)")
        holds.extend(f"{symbol}:{key}" for key in HOLD_KEYS if record.get(key))
        # 전략의 신규 주문 보류 조건(strategy.py "net_invested 신뢰성 게이트")과 같습니다:
        # status 가 valid 가 아니고(누락 포함) 순투입금이 0 이하일 때만 보류입니다.
        if (
            record.get("net_invested_status") != "valid"
            and float(record.get("net_invested", 0.0) or 0.0) <= 0
        ):
            holds.append(f"{symbol}:net_invested_status")
    return ",".join(holds)


def evaluate(now_utc=None, state_path=".state.json", symbols=""):
    """게이트 판정 결과를 dict 로 돌려줍니다."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    window = us_order_window(now_utc)
    wait_seconds = 0
    if window.is_open and us_session_in_progress(now_utc) is None:
        now_kst = now_utc.astimezone(ZoneInfo("Asia/Seoul"))
        et_dst = now_utc.astimezone(ZoneInfo("America/New_York")).dst()
        wait_seconds = int(pre_market_wait_seconds(now_kst, bool(et_dst)))
    settled_session = us_session_last_closed_date(now_utc)
    if not settled_session:
        raise RuntimeError("직전 마감 세션을 계산할 수 없습니다 (XNYS 캘린더)")
    due = us_session_after_hours_end(settled_session, now_utc)
    return {
        "window": "open" if window.is_open else "closed",
        "session": window.session,
        "reason": window.reason,
        "wait_seconds": str(wait_seconds),
        "settlement_due": "true" if due else "false",
        "settled_session": settled_session,
        "state_hold": state_hold(state_path, symbols),
    }


def main():
    try:
        result = evaluate(symbols=os.getenv("SYMBOLS", ""))
    except Exception as error:
        print(f"[세션 게이트] 판정 실패: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}={' '.join(str(value).split())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
