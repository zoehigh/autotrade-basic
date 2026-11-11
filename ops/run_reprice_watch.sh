#!/usr/bin/env bash
# 메리츠 가격 정정 감시기 런처 — 크론이 주기적으로 호출합니다 (등록은 사람이 합니다).
#
# 기본은 DRY (조회·계획·알림만). 실제 정정은 크론 줄에서 아래 3개를 **명시**해야만 켜집니다.
#   REPRICE_WATCH_MODE=APPLY TRADE_MODE=LIVE ALLOW_BROKER_ORDERS=1
# .env 에 같은 이름이 있어도 무시합니다 (.env 로 실수로 실주문이 켜지는 것을 막기 위해).
#
# 감시 시간 창(ET, 서머타임·조기마감 반영)은 파이썬이 판단하므로, 크론은 KST 고정으로 자주 호출해도 됩니다.
# 창 밖이면 API 호출 없이 즉시 종료합니다. 종료 코드: 0 정상/건너뜀, 1 오류·중단(알림 발송), 2 설정 오류.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reprice-watch-$(date +%Y%m%d).log"

# 호출자(크론)가 넘긴 값만 신뢰합니다: .env 로드 전에 보관하고, 로드 후 .env 값은 지우고 복원합니다.
_mode="${REPRICE_WATCH_MODE-}"; _trade="${TRADE_MODE-}"; _allow="${ALLOW_BROKER_ORDERS-}"
set -a
. ./.env
set +a
unset REPRICE_WATCH_MODE TRADE_MODE ALLOW_BROKER_ORDERS
export REPRICE_WATCH_MODE="${_mode:-DRY}"
export TRADE_MODE="${_trade:-DRY}"
# ALLOW_BROKER_ORDERS 는 unset 만으로는 부족합니다: 파이썬의 load_dotenv() 가 .env 에서 다시 읽어 오므로
# 값을 명시적으로 내보내 (기본 0) .env 가 덮어쓰지 못하게 합니다. 다른 두 값도 위에서 같은 방식으로 고정합니다.
export ALLOW_BROKER_ORDERS="${_allow:-0}"

uv run python ops/meritz_reprice_watch.py >> "$LOG" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
    echo "[watch-launcher] exit=$rc $(date '+%F %T %Z') mode=$REPRICE_WATCH_MODE rev=$(git rev-parse --short HEAD 2>/dev/null)" >> "$LOG"
fi
exit $rc
