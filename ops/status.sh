#!/usr/bin/env bash
# 운영 상태 한눈에 보기 (read-only — 주문/상태 변경 없음)
#
#   ./status.sh
#
# 1) 마지막 실행 결과  2) 예약 cron  3) state 진단
set -uo pipefail

APP_DIR="$HOME/investment/muhan-v4"
cd "$APP_DIR" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "=== 현재 시각 ==="
date "+%F %T %Z"

echo
echo "=== 마지막 실행 로그 ==="
last=$(ls -1t logs/run-*.log 2>/dev/null | head -1)
if [[ -z "$last" ]]; then
    echo "(실행 로그 없음)"
else
    echo "파일: $last"
    grep -E "^\[launcher\]|\[실행 결과\]|합계:|잔여 주문 정리|취소 확정|이전 세션 주문|✗ |\[불일치\]|\[주의\]|\[경고\]|체결 [0-9]+건 발견" "$last" | head -40
fi

echo
echo "=== 예약 cron (muhan) ==="
crontab -l 2>/dev/null | grep muhan-v4 || echo "(없음 — 자동 실행 꺼짐)"

echo
echo "=== 실행 중 프로세스 ==="
pgrep -af "trading_bot.py|run_daily.sh" || echo "(없음)"

echo
echo "=== state 진단 ==="
set -a
. ./.env
set +a
STATE_DIAGNOSTIC_ONLY=true uv run python trading_bot.py 2>&1 | grep "상태 진단" | sed 's/, order_meta_ids=\[[^]]*\]//'

echo
echo "=== 최근 5회 실행 ==="
for f in $(ls -1t logs/run-*.log 2>/dev/null | head -5); do
    printf "  %-34s %s\n" "$f" "$(grep -oE '\[launcher\] exit=[0-9]+.*' "$f" | head -1)"
done
