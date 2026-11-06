#!/usr/bin/env bash
# 운영 런처 — cron 에서 하루 여러 번(단계별) LIVE 실행합니다.
#
# ⚠️ 1일 1회 단일 실행이 아닙니다. SETTLE/PREVIEW 는 KST 아침, APPROVE/EXECUTE/VERIFY 는
#    KST 18:20~18:45 에 각각 별도 런으로 실행합니다 (스케줄 예시: ops/cron.example).
#
# 사용법:
#   ./run_daily.sh                          # .env 설정 그대로 LIVE (LIVE 는 RUN_PHASE 필수 — 아래 참고)
#   FORCE_SYMBOLS=TQQQ:NAS ./run_daily.sh   # 단일 종목만
#   LAUNCH_MODE=DRY ./run_daily.sh          # 주문 없이 프리뷰
#
# ⚠️ LIVE 는 RUN_PHASE 를 반드시 지정해야 합니다 (없으면 봇이 브로커 생성 전에 차단).
#    단계: SETTLE → PREVIEW → APPROVE → EXECUTE → VERIFY (스케줄 예시는 ops/cron.example).
#    예) RUN_PHASE=PREVIEW LAUNCH_MODE=LIVE ./run_daily.sh
#    GitHub Actions 는 워크플로가 같은 단계를 자동으로 이어서 실행합니다
#    (.github/workflows/trade_base.yml 의 run_phase=LIFECYCLE — 승인 게이트는 유지).
#
# 종료 코드
#   0  정상 (주문 접수 성공 또는 휴장일 등)
#   1  실패 (주문 미접수/취소 미확정/치명 오류 등) — 텔레그램 긴급 알림 발송
#   2  승인만 되고 실행되지 않음 (VERIFY approval-skipped) — 실행 실패와 구분해
#      별도 안내 알림을 보냅니다 (긴급 실패 아님, 운영자 확인 필요).
set -uo pipefail

APP_DIR="$HOME/investment/muhan-v4"
cd "$APP_DIR" || exit 1

# cron은 PATH가 최소이므로 uv 경로를 직접 넣습니다.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

# 실행 식별자 — 정산 근거 파일과 로그를 묶는 키 (자동 검증이 사용)
export RUN_ID="$(basename "$LOG" .log)"

# 호출자가 넘긴 오버라이드는 .env보다 우선해야 하므로 먼저 보관합니다.
OVERRIDE_SYMBOLS="${FORCE_SYMBOLS:-}"
OVERRIDE_MODE="${LAUNCH_MODE:-}"
# 프로세스 환경변수 RUN_LOCK_PATH 는 .env 보다 우선합니다 (python-dotenv 와 동일).
OVERRIDE_LOCK_PATH="${RUN_LOCK_PATH:-}"

# AUTO_EXECUTE 도 호출자 값이 .env 보다 우선해야 합니다 (외부 리뷰 P1).
# 아래 `.env` 로드(`set -a` 로 소싱)는 **셸 변수 대입**이므로 export 된 호출자 값까지 덮어씁니다.
# 그래서 SYMBOLS/LAUNCH_MODE 처럼 미리 보관했다가 .env 로드 뒤 되돌립니다.
# 호출자가 지정하지 않은 경우(unset)는 그대로 .env 값을 씁니다 — 값을 **없는 것**과
# **빈 값**으로 구분해야 하므로 `${AUTO_EXECUTE+x}` sentinel 을 씁니다
# (빈 값 `AUTO_EXECUTE=` 도 "호출자가 끄겠다"는 의도이므로 보존).
# 값 해석(1/true/yes/on, strip+lower)은 봇(trading_bot.py)이 하므로 런처는 보존만 합니다.
if [[ -n "${AUTO_EXECUTE+x}" ]]; then
    OVERRIDE_AUTO_EXECUTE_SET=1
    OVERRIDE_AUTO_EXECUTE="$AUTO_EXECUTE"
else
    OVERRIDE_AUTO_EXECUTE_SET=0
    OVERRIDE_AUTO_EXECUTE=""
fi

# RUN_PHASE 도 같은 결함이 있습니다 (외부 리뷰 P1 — 조건부).
# `.env` 에 RUN_PHASE=EXECUTE 가 있으면 오전 cron 의 RUN_PHASE=PREVIEW 가 뒤집혀
# 진단용 PREVIEW 가 주문 단계로 바뀝니다. AUTO_EXECUTE 와 동일한 sentinel 패턴으로
# 호출자 값을 보존합니다 — unset 이면 `.env` 값을 그대로 쓰고, 빈 값(`RUN_PHASE=`)도
# "호출자가 단계를 비우겠다"는 의도이므로 보존합니다.
# 단계 값 정규화(strip + 대문자, 유효 단계 판정)는 봇(`config.read_run_phase()`)이
# 하므로 런처는 값을 **보존만** 합니다 (셸에 두 번째 판정 규칙을 만들지 않습니다).
if [[ -n "${RUN_PHASE+x}" ]]; then
    OVERRIDE_RUN_PHASE_SET=1
    OVERRIDE_RUN_PHASE="$RUN_PHASE"
else
    OVERRIDE_RUN_PHASE_SET=0
    OVERRIDE_RUN_PHASE=""
fi

# 같은 시각에 두 번 돌지 않도록 잠급니다.
# - DRY: 다른 RUN이 있으면 즉시 종료합니다 (조회성 작업이라 지연할 이유가 없습니다).
# - LIVE: 다른 RUN이 있으면 최대 5분 기다립니다. 즉시 종료하면 "성공한 것처럼 조용히
#   건너뛰어" 실제 주문이 나가지 않으므로, LIVE는 반드시 실행될 때까지 기다립니다.
# ── 잠금 경로 단일화 (경로 우선순위) ───────────────────────────────
# 1) `.env` 를 **먼저** 읽습니다. RUN_LOCK_PATH 가 .env 에만 있어도 셸과 파이썬이
#    같은 값을 보게 하기 위해서입니다. (프로세스 환경변수는 python-dotenv 가
#    덮어쓰지 않으므로, 이미 export 된 값이 .env 보다 우선합니다.)
# 2) 상대경로는 APP_DIR 기준으로 절대화합니다.
# 3) 최종 절대 경로를 RUN_LOCK_PATH 로 export 합니다 — **런처가 해석한 이 값이
#    자식 파이썬의 기준값**입니다 (직접 CLI 도 .env 를 읽으므로 같은 값).
# 4) 런처는 **단계와 무관하게 항상** 이 파일을 fd 9 로 flock 합니다 (fail-closed).
#    "대기 단계는 잠그지 않는다" 판정은 셸에서 다시 구현하지 않습니다 —
#    **판정 규칙의 단일 출처는 trading_bot.py `_phase_runs_without_outer_lock()`**
#    입니다 (셸/파이썬 이중 구현은 drift 시 보호가 조용히 약해집니다).
#    - 일반 단계: 런처가 잡은 잠금을 자식이 그대로 사용합니다. 자식 파이썬은
#      fd 9 가 실제로 같은 파일을 가리킬 때만 상속된 잠금으로 인정합니다
#      (플래그만으로는 우회하지 않음).
#    - 대기 단계(EXECUTE, PREVIEW+AUTO_EXECUTE): 자식 파이썬이 기동 직후
#      `_release_inherited_outer_lock()` 으로 **상속 fd 의 flock 만** 풉니다.
#      프리장 개장 대기(최대 ~7.5시간)를 셸이 잠금을 든 채 보내면 그동안 다른
#      단계(SETTLE/PREVIEW/APPROVE/VERIFY)가 전부 `flock -w 300` 타임아웃으로
#      실패하기 때문입니다. 동시실행 보호는 그 뒤 임계구간(계획 생성·승인 재검증·
#      선점·state 저장·주문 전송)에서 파이썬 `pipeline.global_lock()` 이 같은
#      파일로 계속 수행합니다. 즉 잠금이 **사라지는 구간은 대기뿐**이고,
#      주문·상태변경 구간은 항상 잠금 안입니다 (fail-open 아님).
set -a
. ./.env
set +a
LOCK_PATH="${OVERRIDE_LOCK_PATH:-${RUN_LOCK_PATH:-$APP_DIR/.run.lock}}"
case "$LOCK_PATH" in
    /*) ;;
    *) LOCK_PATH="$APP_DIR/$LOCK_PATH" ;;
esac
export RUN_LOCK_PATH="$LOCK_PATH"

export RUN_LOCK_FD=9
mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>"$LOCK_PATH"
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

# 파이썬 쪽 전역 잠금(pipeline.global_lock)도 같은 .run.lock 을 씁니다.
# 런처가 이미 fd 9 로 잡고 있으므로, 자식 프로세스가 다시 flock 을 시도하면
# 자기 자신과 교착합니다 → 상속된 fd(9)와 플래그를 env 로 알립니다.
# 파이썬은 fd 9 가 RUN_LOCK_PATH 와 **같은 파일**일 때만 상속으로 인정하고,
# 대기 단계로 판정되면 이 상속 잠금만 스스로 해제합니다.
export RUN_LOCK_HELD=1

# `.env` 는 위에서 잠금 경로를 정하기 전에 이미 읽었습니다 (RUN_LOCK_PATH 포함).
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

# AUTO_EXECUTE: 호출자가 export 했을 때만 복원합니다 (unset 이면 .env 값 유지).
# 복원하지 않으면 `.env` 의 AUTO_EXECUTE=true 가 호출자의 `AUTO_EXECUTE=false` 를
# 이겨 아침 진단 PREVIEW 가 자동 주문(PREVIEW→APPROVE→EXECUTE)으로 이어집니다.
if [[ "$OVERRIDE_AUTO_EXECUTE_SET" == "1" ]]; then
    export AUTO_EXECUTE="$OVERRIDE_AUTO_EXECUTE"
fi

# RUN_PHASE: 호출자가 넘겼을 때만 복원합니다 (unset 이면 .env 값 유지).
# 아래 [launcher] start 로그의 phase= 필드는 이 복원 **후** 값입니다 — 로그만 보고
# "어느 단계로 돌았는지" 판단하므로 .env 값이 찍히면 안 됩니다.
if [[ "$OVERRIDE_RUN_PHASE_SET" == "1" ]]; then
    export RUN_PHASE="$OVERRIDE_RUN_PHASE"
fi

# 일회성 복구 플래그가 남아 있으면 사고가 나므로 항상 지웁니다.
unset STATE_REPAIR_ONLY STATE_DIAGNOSTIC_ONLY STATE_CLEAR_FENCE_ONLY \
      STATE_REVERSE_AUDIT_ONLY STATE_REVERSE_RECONCILE_ONLY \
      STATE_NET_INVESTED_REPAIR_ONLY STATE_ASSUME_REVERSE_EXPIRY_ONLY \
      STATE_SET_CYCLE_START_ONLY STATE_REPAIR_ONLY REPAIR_ACTION FORCE_T_REINFERENCE

echo "[launcher] start $(date '+%F %T %Z') mode=$TRADE_MODE phase=${RUN_PHASE:-} symbols=${SYMBOLS:-} auto=${AUTO_EXECUTE:-} rev=$(git rev-parse --short HEAD 2>/dev/null)" >> "$LOG"

# 알림 채널(텔레그램) 생존 확인 — 거래는 막지 않고 상태만 기록합니다.
# (토큰이 죽으면 실패 알림도 함께 사라지므로, 여기서 드러내야 합니다)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    tg=$(curl -sS --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>&1 || true)
    if echo "$tg" | grep -q '"ok":true'; then
        echo "[launcher] 알림 채널 정상 (telegram getMe ok)" >> "$LOG"
    else
        echo "[launcher] ⚠️ 알림 채널 응답 이상 — 실패 알림이 전달되지 않을 수 있습니다: ${tg:0:120}" >> "$LOG"
    fi
fi

uv run python trading_bot.py >> "$LOG" 2>&1
rc=$?

echo "[launcher] exit=$rc $(date '+%F %T %Z')" >> "$LOG"

# 실패/미실행 시 봇이 알림을 보내기 전에 죽었을 수 있으므로 런처에서 한 번 더 알립니다.
# (LIVE 만 — DRY 점검 실패는 로그로 충분하며 알림이 과해집니다)
# VERIFY 의 exit 2(approval-skipped)는 "승인만 되고 실행 안 됨"이라는 **정상적인
# 운영 상태**입니다. 실행 실패(exit 1)와 같은 긴급 알림을 보내면 운영자가 둘을
# 구분할 수 없으므로 문구/이모지를 분리합니다.
if [[ "$rc" != "0" && "$TRADE_MODE" == "LIVE" \
      && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    if [[ "$rc" == "2" ]]; then
        ALERT_TEXT="⚠️ 자동매매 미실행 (exit=2, 승인만 됨 — 실행 실패 아님) $(date '+%F %T %Z')
승인 파일은 있으나 EXECUTE 가 실행되지 않았습니다. PREVIEW/APPROVE/EXECUTE 상태를 확인하세요.
로그: ${LOG}"
    else
        ALERT_TEXT="🚨 자동매매 실행 실패 (exit=${rc}) $(date '+%F %T %Z')
로그: ${LOG}"
    fi
    curl -sS --max-time 15 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${ALERT_TEXT}" \
        >> "$LOG" 2>&1 || true
fi

# 14일 지난 로그는 정리합니다.
find "$LOG_DIR" -name 'run-*.log' -mtime +14 -delete 2>/dev/null

exit "$rc"
