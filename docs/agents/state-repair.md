# state 복구·보정(REPAIR)·STATE_*_ONLY·복구 워크플로

> 이 문서는 `AGENTS.md`에서 **내용 변경 없이 그대로 옮긴** 상세 규칙입니다(문서 구조 최적화). 자동으로 로딩되지 않으므로 해당 작업을 할 때만 여세요. 공통 안전 규칙·핵심 운영 정책은 `AGENTS.md`에 남아 있습니다.

## 실행 모드별 state 저장 원칙 하위 항목 (원문: UNIQUE STYLES)

  - `FORCE_T_REINFERENCE`: **DRY = 미리보기(저장 없음), REPAIR = 실제 저장**. LIVE + `FORCE_T_REINFERENCE=true`는 주문 방지를 위해 **DRY 미리보기로 고정**되며 state는 저장되지 않음(로그에 명시). 실제 저장은 `TRADE_MODE=REPAIR`
  - REPAIR 재추정은 **정산 보류 표식**(`_settlement_hold`·`_pending_amount_odnos`·`_pending_marker_odnos`·`_position_basis_unknown`)이 있으면 저장을 거부하고 종료 코드 1로 끝납니다. 재추정이 `last_updated`를 비운 뒤 `update_T_from_history`를 부르기 때문에, 보류 시 되돌려진 스냅샷도 `last_updated=""`라 그대로 저장하면 워터마크가 지워집니다
  - REPAIR 재추정은 전 종목 검증 후 `.state.json`을 **파일 쓰기 1회(`save_states`, 임시 파일→`os.replace`)** 로만 저장합니다. 종료 코드: **1 = 검증 실패**(MAX_T 초과·숫자 오류·보류, 저장 시도 없음), **2 = 파일 저장 실패**(검증은 통과, 원본 파일은 변경되지 않음)
  - Actions는 확정된 실행 모드(`steps.resolve.outputs.trade_mode`, 봇과 같은 strip+upper 정규화)가 `LIVE`/`REPAIR`일 때만 새 state 캐시를 저장(Actions REPAIR 는 위처럼 Resolve 에서 이미 차단되므로 현재는 LIVE 만 해당)(`always()` 유지 — LIVE가 중간 실패해도 보존). 복원은 모든 모드에서 허용. 잘못된 값은 봇이 DRY로 처리하고 저장도 차단
  - Resolve 단계의 외부 모드 입력(`inputs.trade_mode`·`vars.TRADE_MODE`·`broker_mode` 등)은 셸 문자열에 `${{ }}`로 삽입하지 않고 **env로만 전달**합니다. 영문 대문자가 아닌 값(개행·특수문자 포함)은 `GITHUB_OUTPUT`에 쓰지 않고 `INVALID`로 치환(봇은 DRY, 캐시 저장 안 함)

## T값 보정 명령 예시 (원문: COMMANDS)

```bash
# T값 보정: 전체 이력 재추정 — 미리보기 (저장하지 않음)
TRADE_MODE=DRY FORCE_T_REINFERENCE=true uv run python trading_bot.py

# T값 보정: 전체 이력 재추정 — 결과를 state에 실제 저장
TRADE_MODE=REPAIR FORCE_T_REINFERENCE=true uv run python trading_bot.py

# T값 강제 설정 — 실제 T 보정 및 state 저장, 주문은 실행하지 않음 (1회성 — 보정 RUN 후 env var 즉시 삭제)
TRADE_MODE=REPAIR TQQQ_FORCE_T=29 SOXL_FORCE_T=20 uv run python trading_bot.py
# ※ TRADE_MODE=DRY 또는 LIVE 와 {SYMBOL}_FORCE_T 를 함께 쓰면 브로커 생성 전에 오류(종료 코드 1)로 중단됩니다.
# ※ REPAIR 는 작업 소스가 정확히 1개여야 합니다. {SYMBOL}_FORCE_T 와 REPAIR_ACTION=FORCE_T 를 함께 설정하지 마세요
#    (작업 소스 2개로 중단). REPAIR_ACTION=FORCE_T 단독은 값을 읽을 곳이 없어 명시적으로 실패합니다(종료 코드 1).

# T값 상한 설정 (기본값은 분할수) — DRY 미리보기는 "원시 추정 T"와 "상한 적용 후 T"를 함께 출력
TRADE_MODE=DRY SOXL_MAX_T=19 FORCE_T_REINFERENCE=true uv run python trading_bot.py
# ※ REPAIR 재추정은 추정 T 가 MAX_T 를 넘으면 T 를 자르지 않고 저장을 거부합니다(종료 코드 1, state 변경 없음).
#    초과 원인(정산 오류 등)을 확인한 뒤 필요하면 별도로 {SYMBOL}_FORCE_T 보정을 승인받아 진행하세요.
```

## T 보정 환경변수 (원문: NOTES)

- **T 보정 환경변수**:
  - `FORCE_T_REINFERENCE=true`: `last_updated` 초기화 → 전체 이력(90일)에서 T 재추정. **DRY=미리보기(저장 없음) / REPAIR=실제 저장** (LIVE는 주문 방지를 위해 DRY로 자동 전환)
  - `{SYMBOL}_FORCE_T={value}` (**`TRADE_MODE=REPAIR` 전용** — DRY·LIVE에서는 브로커 생성 전 오류로 중단): state.json의 T를 강제 덮어쓰기 (orders_meta/balance_mismatch 초기화, `last_updated`/`last_processed_ordno`를 이력 최신 주문 시각으로 갱신 → 이중 가산 방지, `FORCE_T=0`이면 `cycle_start_date` 초기화)
  - `{SYMBOL}_MAX_T={value}`: T 자동추정 결과의 상한선 (기본값: `{SYMBOL}_SPLITS` — 리버스모드 진입 위해 `T=splits` 허용, 초과만 방지)
  - FORCE_T_REINFERENCE 실행 시 small_seed_days(소액 시드 오추정)가 감지되면 포지션 기반 T 추정값이 함께 출력되며, 해당 값을 `{SYMBOL}_FORCE_T`로 설정하여 직접 보정 가능
  - ⚠️ `{SYMBOL}_FORCE_T`는 **1회성 점화용**입니다. 보정 RUN 1회 실행 후 env var를 **즉시 삭제**하세요. 리버스모드 진행은 `day_count`(state `reverse_mode`)와 체결 이력 기반 T 갱신이 주도하므로 FORCE_T 불필요. 유지하면 리버스모드 종료 → 새 사이클(T=0)에서 T를 다시 `splits`로 강제해 **새 사이클 매수가 영구 차단**됩니다.
  - ⚠️ GH Actions의 새 state 캐시는 **`LIVE`/`REPAIR` 실행에서만** 저장됩니다. DRY(또는 DRY로 고정된 실행)의 결과는 캐시에 남지 않으므로 Actions에서는 REPAIR 가 차단되므로 T 보정은 로컬에서 `TRADE_MODE=REPAIR`로 실행하세요 (`TRADE_MODE=DRY`/`LIVE` + `FORCE_T` 같은 기존 방식은 더 이상 동작하지 않음).
  - ⚠️ GH Actions 캐시는 **브랜치별 격리**입니다. `develop` 수동 RUN에서 보정해도 `main`(repository_dispatch) RUN의 캐시에는 반영되지 않습니다. T 보정은 반드시 운영 브랜치(`main`)에서 실행하세요.

## STATE_*_ONLY·REPAIR_ACTION·복구 워크플로 (원문: NOTES)

- **STATE_DIAGNOSTIC_ONLY=true**: GitHub Actions 캐시의 T/reverse_mode 상태만 출력하고 브로커 API, 전략, 주문을 실행하지 않음. 일회성 진단 후 즉시 해제
- **STATE_REPAIR_ONLY=true**: 지정 fingerprint가 일치할 때만 API/전략/주문 없이 리버스 상태를 1회 초기화. `STATE_REPAIR_*` 변수는 실행 직후 삭제
  - `STATE_REPAIR_TARGET_T={value}` (선택): 기본 초기화 대신 **T만 보정**하고 리버스 cycle/orders_meta를 보존. ord_dt vs submitted_at 날짜 컨벤션 버그로 오반영된 T(예: 20→15)를 되돌려 다음 RUN의 `reconcile_reverse_fills`가 체결 기반으로 재계산(예: 20→18)하도록 하는 용도. fingerprint는 기존과 동일하게 필요
- **STATE_CLEAR_FENCE_ONLY=true**: 지정 fingerprint가 일치할 때만 API/전략/주문 없이 주문 fence(`pending_order_intent`/`pending_order_batch`)를 1회 초기화. `STATE_CLEAR_FENCE_SYMBOL` + `STATE_CLEAR_FENCE_EXPECT_T`/`EXPECT_LAST_UPDATED`/`EXPECT_INTENT`/`EXPECT_BATCH` 필요 (intent/batch는 빈 값 = "fence 없음" 의미, env var 존재만 필수). 실행 직후 변수 삭제
- **STATE_SET_CYCLE_START_ONLY=true**: 지정 fingerprint가 일치할 때만 API/전략/주문 없이 `cycle_start_date`를 1회 설정. `STATE_SET_CYCLE_START_SYMBOL` + `STATE_SET_CYCLE_START_DATE`(YYYY-MM-DD) + `STATE_SET_CYCLE_START_EXPECT_HASH`/`EXPECT_T`/`EXPECT_CURRENT` 필요 (EXPECT_CURRENT는 빈 값 허용 = "현재 값 없음"). `REPAIR_ACTION=CYCLE_START_SET`으로도 실행 가능. 미종결 리버스 주문 또는 pending fence가 있으면 거부하고, date가 기준일(last_updated 날짜, 없으면 오늘)보다 미래면 거부. 실행 직후 변수 삭제
  - 배경: 수동 주입(REPAIR CAS) 후 `cycle_start_date`가 비면 `generate_cycle_report`가 **전체 이력**을 집계해 B안 시드(`next_cycle_seed = seed + net_profit`)가 틀어짐. `cycle_start_date`는 full-history 추정 경로에서만 자동 설정되므로(현재 T>0인 진행 중 사이클은 미설정), 이 액션으로 정정
- **REPAIR_ACTION=REVERSE_LEDGER_REWIND** (로컬/복구 워크플로 전용, 주문·브로커 호출 없음): 리버스 매도가 일반 매도(×factor)로 **오분류 정산**된 종목을 재정산 직전으로 되돌립니다(`state.rewind_misclassified_reverse_sell`). 원장의 해당 `sell` 항목(마지막 항목일 때만)·`sell_t_effects`를 제거하고 T를 `reverse_base_t`로, `net_invested`를 반영 전으로, 해당 주문 `processed_filled_*` 마커를 0으로 되돌립니다. 워터마크·`position_qty`·`reverse_mode`는 불변. `REPAIR_REWIND_SYMBOL/ODNO/EXPECT_HASH/EXPECT_T` + **가드 종목** `REPAIR_REWIND_GUARD_SYMBOL/GUARD_HASH`(다른 종목 canonical hash, 저장 전후 불변 검증)가 필요하고, `REPAIR_REWIND_APPLY=true`가 없으면 **미리보기만**(저장 없음). 전제가 하나라도 어긋나면 아무것도 바꾸지 않고 중단. 실행 직후 환경변수 삭제. 배경: 2026-11-06 NHPlug SOXL(odno 2617) T 17.325 → 14.4375 오분류
- **복구 워크플로** (`.github/workflows/repair_reverse_ledger_rewind.yml`, 수동 `workflow_dispatch`, main·demo 전용): 위 REVERSE_LEDGER_REWIND 를 Actions 캐시에 적용하는 유일한 경로. preview(저장 없음) → approve(Environment `state-repair` — **Required reviewers 는 선택 사항**: 지정하면 apply 직전에 승인 대기, 미지정이면 대기 없이 통과. 정기 거래 실행과 무관) → **backup**(복구 전 state 를 gpg 로 암호화·왕복 검증해 artifact `repair-backup-<broker>-<run_id>-<시도 번호>` 로 30일 보관("Re-run all jobs" 는 run_id 가 같아 이름에 `run_attempt` 를 넣음) — **이 잡이 성공해야 apply 시작**) → apply(복구 → `ops/verify_repair_rewind.py` 독립 검증 → 통과 시에만 새 캐시 `state-<broker>-demo-<run_id>` 저장 → **새 키가 실제로 존재하는지 확인**: `actions/cache/save` 는 장애·예약 충돌에도 경고만 남기고 성공할 수 있어 step 결과만으로는 저장 성공을 알 수 없음). 복원 대상이 해당 브로커 main 의 **최신 캐시**여야 하고 복원본 SHA-256 이 입력과 같아야 하며(승인 대기 중 거래가 돌았으면 apply 가 중단), 거래와 같은 concurrency 그룹을 씁니다(백업 잡은 캐시를 읽기만 하므로 제외). 브로커/텔레그램 시크릿은 주입하지 않고, 백업 passphrase(`STATE_BACKUP_PASSPHRASE`)는 **백업 잡의 암호화 단계에만** 주입합니다(봇을 실행하는 preview/apply 잡에는 시크릿 없음 — 평문 state 는 공개 artifact 로 올리지 않음). 주문번호 원문은 공개 입력으로 받지 않고 봇이 후보 1개를 자동 특정(여러 개면 중단)
  - **`confirm` 으로 실행 범위를 정함**: (비움) preview 만 / `BACKUP`(종목 생략 가능) preview → backup + hashes(**state 변경 없음**, 시크릿·암호화·왕복 검증·업로드 경로 점검과 복원 state 지문 계산) / `REWIND <종목>` preview → approve → backup → apply. 정확히 일치하지 않으면 미리보기만. 비운 미리보기로는 backup·apply 가 생략되므로 **실제 복구 전에 `BACKUP` 으로 백업 경로부터 확인** (apply 저장 경로는 실제 복구에서 처음 검증). **`BACKUP` 은 복구 대상·T·hash 없이 broker·restore_key 만으로 실행하고, 봇의 복구 미리보기는 건너뜁니다**(미리보기는 복구 대상이 있어야 성공해 정상 state 에서는 실패). 파일 SHA-256 은 `BACKUP` 에서만 생략 가능(입력하면 반드시 대조, 생략하면 "사전 지문 대조 없이 백업"으로 표시). 시크릿 없는 `hashes` 잡이 복원본의 파일 SHA·종목별 canonical hash·T 를 요약에 출력해 REWIND 입력값으로 쓰게 하며, 백업 잡(passphrase 보유)은 저장소 코드를 실행하지 않습니다
  - **절차는 두 번 실행**: `confirm` 은 실행 시작 시 입력하는 값이라 같은 실행에서 미리보기를 본 뒤 정할 수 없습니다(Required reviewers 없이는 approve 가 즉시 통과). ① `confirm` 비우고 실행 → 미리보기(Summary 에 현재값·목표값 표시) ② 출력을 근거 실행 Evidence 와 **실행자가 직접 대조**(독립된 승인자가 있는 것이 아님) ③ 같은 입력 + `confirm`=`REWIND <종목>` 으로 재실행. ①~③ 사이에 거래가 돌아 새 캐시가 생기면 최신 캐시 검사에서 중단되므로 **다음 거래 슬롯 전에** 끝내고, 거래 슬롯 시간대는 피하세요(같은 concurrency 그룹에서 대기 중 실행은 새 실행에 밀려 취소될 수 있음)
  - 실행 요약(Summary)에 **복구 적용·독립 검증·캐시 저장·새 캐시 존재 확인 결과를 따로** 기록하며(실패해도 기록, 주문번호 원문 없음, `last_processed_ordno` 는 변경 여부만) "검증 통과 + 저장 실패/미확인"을 구분합니다(저장 성공은 존재 확인 뒤에만 보고). 파일 SHA·canonical hash 는 **입력값과 계산값을 구분**해 복구 전후로 기록하고(별도 `continue-on-error` 단계 — 계산 실패가 복구·검증 판정을 가리지 않음), 마지막 `summary` 잡이 백업 실패로 apply 가 생략된 경우까지 전체 흐름의 최종 결과를 남깁니다
- **REPAIR_ACTION=REVERSE_WATERMARK_RESTORE** (REVERSE_LEDGER_REWIND 직후 후속, 같은 복구 워크플로의 `action` 선택): 되돌리기 뒤에도 오분류 실행이 올려 둔 워터마크(`last_updated`/`last_processed_ordno`)가 남아 있으면 독립 검증기가 그 리버스 체결을 "워터마크 이전 = 필수 아님"으로 보고 정상 정산을 '과다 반영'으로 실패 처리합니다(2026-11-09 NHPlug SOXL DRY 실측). 이 두 필드만 오분류 직전 값으로 되돌립니다. 전제: 미정산 리버스 매도(마커 0/0, 원장 항목 없음)가 정확히 1개, 목표 시각은 **현재보다 과거**(앞으로 감기 금지), CAS(대상 hash·T)+가드 종목 hash. 목표 `last_processed_ordno` 는 공개 입력으로 받지 않고 **원장 마지막 항목의 주문번호**로 도출합니다(Evidence 의 `watermark_before`/`last_processed_ordno_before` 와 대조). 목표 시각은 Evidence `watermark_before` 값. 워크플로 입력 `evidence_run_id`(숫자)는 **이 동작에서 필수**입니다 — 목표값의 근거가 된 Evidence 가 있는 실행 번호를 남기는 추적용 기록이며 값의 정확성을 증명하지는 않습니다(복구 함수·검증기는 목표가 현재보다 과거인지만 확인하므로, 목표값은 실행자가 Evidence 와 직접 대조해야 합니다)
