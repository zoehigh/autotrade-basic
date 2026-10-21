#!/usr/bin/env bash
# 운영 런처 — cron(KST 17:20)에서 1일 1회 LIVE 실행합니다.
#
# 사용법:
#   ./run_daily.sh                          # .env 설정 그대로 LIVE
#   FORCE_SYMBOLS=TQQQ:NAS ./run_daily.sh   # 단일 종목만
#   LAUNCH_MODE=DRY ./run_daily.sh          # 주문 없이 프리뷰
#
# 종료 코드
#   0  정상 (주문 접수 성공 또는 휴장일 등)
#   1  실패 (주문 미접수/취소 미확정/치명 오류 등) — 텔레그램 긴급 알림 발송
set -uo pipefail

APP_DIR="$HOME/investment/muhan-v4"
cd "$APP_DIR" || exit 1

# cron은 PATH가 최소이므로 uv 경로를 직접 넣습니다.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

# 호출자가 넘긴 오버라이드는 .env보다 우선해야 하므로 먼저 보관합니다.
OVERRIDE_SYMBOLS="${FORCE_SYMBOLS:-}"
OVERRIDE_MODE="${LAUNCH_MODE:-}"

# 같은 시각에 두 번 돌지 않도록 잠급니다.
# - DRY: 다른 RUN이 있으면 즉시 종료합니다 (조회성 작업이라 지연할 이유가 없습니다).
# - LIVE: 다른 RUN이 있으면 최대 5분 기다립니다. 즉시 종료하면 "성공한 것처럼 조용히
#   건너뛰어" 실제 주문이 나가지 않으므로, LIVE는 반드시 실행될 때까지 기다립니다.
exec 9>"$APP_DIR/.run.lock"
if [[ "${OVERRIDE_MODE:-LIVE}" == "DRY" ]]; then
    if ! flock -n 9; then
        echo "[launcher] 다른 RUN이 실행 중입니다. 종료합니다. $(date '+%F %T %Z')" >> "$LOG"
        exit 0
    fi
else
    if ! flock -w 300 9; then
        echo "[launcher] 5분 안에 잠을 얻지 못했습니다. 실행하지 않습니다. $(date '+%F %T %Z')" >> "$LOG"
        exit 1
    fi
fi

set -a
. ./.env
set +a

# 수수료 면제 종료 안내 — 2027-01-01 부터는 실제 요율로 되돌려야 합니다.
# (0 으로 두는 동안 사이클 리포트 순수익이 약 0.25% 과대 표시됩니다)
if [[ "$(date +%Y%m%d)" -ge "20270101" ]]; then
    case "${COMMISSION_RATE:-0}" in
        0|0.0|0.00|0.000|0.0000)
            echo "[launcher] ⚠️ 수수료 면제 종료 기준(2027-01-01) 을 지났습니다. COMMISSION_RATE 를 실제 요율(예: 0.0025) 로 변경하세요." >> "$LOG"
            ;;
    esac
fi

[[ -n "$OVERRIDE_SYMBOLS" ]] && export SYMBOLS="$OVERRIDE_SYMBOLS"
export TRADE_MODE="${OVERRIDE_MODE:-LIVE}"

# 일회성 복구 플래그가 남아 있으면 사고가 나므로 항상 지웁니다.
unset STATE_REPAIR_ONLY STATE_DIAGNOSTIC_ONLY STATE_CLEAR_FENCE_ONLY \
      STATE_REVERSE_AUDIT_ONLY STATE_REVERSE_RECONCILE_ONLY \
      STATE_NET_INVESTED_REPAIR_ONLY STATE_ASSUME_REVERSE_EXPIRY_ONLY \
      STATE_SET_CYCLE_START_ONLY STATE_REPAIR_ONLY REPAIR_ACTION FORCE_T_REINFERENCE

echo "[launcher] start $(date '+%F %T %Z') mode=$TRADE_MODE symbols=$SYMBOLS rev=$(git rev-parse --short HEAD 2>/dev/null)" >> "$LOG"

uv run python trading_bot.py >> "$LOG" 2>&1
rc=$?

echo "[launcher] exit=$rc $(date '+%F %T %Z')" >> "$LOG"

# 실패 시 봇이 알림을 보내기 전에 죽었을 수 있으므로 런처에서 한 번 더 알립니다.
# (LIVE 만 — DRY 점검 실패는 로그로 충분하며 알림이 과해집니다)
if [[ "$rc" != "0" && "$TRADE_MODE" == "LIVE" \
      && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -sS --max-time 15 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=🚨 자동매매 실행 실패 (exit=${rc}) $(date '+%F %T %Z')
로그: ${LOG}" \
        >> "$LOG" 2>&1 || true
fi

# 14일 지난 로그는 정리합니다.
find "$LOG_DIR" -name 'run-*.log' -mtime +14 -delete 2>/dev/null

exit "$rc"
