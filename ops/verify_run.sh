#!/usr/bin/env bash
# 예약 실행 결과 검증 — 이상이 있을 때만 텔레그램으로 알립니다.
#
#   ./verify_run.sh [YYYYMMDD]
#
# - 정상이라면 조용히 종료합니다 (성공은 봇의 주문 알림이 이미 알립니다).
# - 이상이 있으면 로그 경로와 함께 긴급 알림을 보냅니다.
# - 봇이 시작 전에 죽어 알림이 전혀 없었던 경우도 여기서 잡힙니다.
set -uo pipefail

APP_DIR="$HOME/investment/muhan-v4"
cd "$APP_DIR" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TARGET="${1:-$(TZ=Asia/Seoul date +%Y%m%d)}"
LOG="$APP_DIR/logs/verify-${TARGET}-$(date +%H%M%S).log"
mkdir -p "$APP_DIR/logs"

set -a
. ./.env
set +a

uv run python ops/verify_run.py "$TARGET" > "$LOG" 2>&1
rc=$?

if [[ "$rc" != "0" ]]; then
    echo "[verify] 이상 판정 (exit=$rc) → 텔레그램 알림" >> "$LOG"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        summary=$(grep -E '^\s+\[!! \]' "$LOG" | head -5 | sed 's/^ *//' | tr '\n' ';')
        if [[ -z "$summary" ]]; then
            # [!!] 항목이 없으면 사유를 로그에서 직접 뽑습니다 (예: 실행 로그 자체가 없는 경우)
            summary=$(grep -E "로그 없음|Traceback|Error" "$LOG" | head -3 | sed 's/^ *//' | tr '\n' ';')
        fi
        [[ -z "$summary" ]] && summary="원인은 로그를 확인하세요"
        curl -sS --max-time 15 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=🚨 예약 실행 검증 이상 (${TARGET})
${summary}
로그: ${LOG}" \
            >> "$LOG" 2>&1 || true
    fi
else
    echo "[verify] 이상 없음" >> "$LOG"
fi

# 검증 로그도 14일 지난 것은 정리합니다.
find "$APP_DIR/logs" -name 'verify-*.log' -mtime +14 -delete 2>/dev/null

exit "$rc"
