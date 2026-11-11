# 메리츠 가격 정정 감시기

`ops/meritz_reprice_watch.py` — 크론이 주기적으로 호출하면 한 번 점검하고 종료합니다.
보정된 일반모드 BUY LOC 를 가격 정정 도구(`src/meritz_reprice.py`)의 규칙으로 계획·정정합니다.
**기본은 DRY**(조회·계획·알림만). 크론 등록과 APPLY 활성화는 사람이 결정합니다 (이 PR 은 등록하지 않음).

## 실행 방식
- 런처 `ops/run_reprice_watch.sh` (예: `*/20 19-23,0-5 * * 1-6`, `ops/cron.example` 에 주석으로만 수록)
- 크론은 KST 고정으로 자주 불러도 됩니다. **감시 창은 코드가 ET 로 판단**하므로 서머타임/동절기를 따로 관리하지 않습니다.
  - 미국(NYSE) 영업일만, 기본 ET 06:00 ~ min(15:30, 정규장 마감 − 30분) — 조기 마감일은 창이 같이 줄어듭니다.
  - 창 밖·휴장일·달력 오류(fail-closed)는 API 호출 없이 즉시 종료(0).
  - 서머타임(2027-03-14~11-06) 19:00~04:30 KST / 동절기 20:00~05:30 KST 가 창에 해당 (봇의 18:30 주문 이후, 마감 전).

## 설정 (환경변수, 잘못된 값은 오류로 중단 — 종료 코드 2)
| 이름 | 기본 | 의미 |
|---|---|---|
| `REPRICE_WATCH_MODE` | `DRY` | `DRY` / `APPLY` |
| `REPRICE_WATCH_START_ET` / `_END_ET` | `06:00` / `15:30` | 감시 창 (ET) |
| `REPRICE_WATCH_CLOSE_BUFFER_MIN` | `30` | 정규장 마감 몇 분 전에 끝낼지 |
| `REPRICE_WATCH_MIN_STEP` | `0.05` | 이 금액(USD) 미만의 변경은 건너뜀 |
| `REPRICE_WATCH_MIN_INTERVAL_MIN` | `20` | 같은 종목 정정 시도 사이 최소 간격 |
| `REPRICE_WATCH_MAX_PER_RUN` | `1` | 한 번의 실행에서 종목당 시도할 정정 수 (후보가 여럿이면 가격 차이가 큰 것부터, 나머지는 다음 실행). 기본 1 이면 모든 시도가 최소 간격만큼 떨어짐 |
| `REPRICE_WATCH_MAX_AMENDS_PER_DAY` | `3` | 종목별 하루(ET 세션) 정정 한도 |
| `REPRICE_WATCH_SYMBOLS` | (전체) | 쉼표로 종목 제한 |

## 실주문(APPLY) 활성화 — 3개가 모두 필요
`REPRICE_WATCH_MODE=APPLY` + `TRADE_MODE=LIVE` + `ALLOW_BROKER_ORDERS=1` (+ `BROKER=meritz`).
하나라도 없으면 **DRY 로 낮추지 않고 실행을 거부**합니다(종료 2). 런처는 `.env` 의 같은 이름을 무시하고
**크론 줄에서 명시한 값만** 사용하므로 `.env` 실수로 실주문이 켜지지 않습니다.

## 안전장치
- **중복 실행 방지:** `.watch.lock` + 운영 런처 `.run.lock` + 봇 `.bot.lock` 을 모두 non-blocking 으로 잡고, 하나라도 사용 중이면 조용히 건너뜁니다(0). 정정 도구와도 상호 배제.
- **DRY 읽기 전용 방어선** (`src/meritz_readonly.py`): DRY 는 브로커를 `ReadOnlyBroker` 로 감싸서만 씁니다. ① 허용 목록(`exchange_code`·`get_order_history`·
  `get_stock_price`·`close`) 밖의 모든 접근(`place_order`/`modify_order`/`cancel_order`, 미래에 추가될 변경 메서드 포함)은 **실제 브로커 호출 전에** 예외,
  ② 브로커 HTTP 세션의 POST 를 막아 프록시를 우회해도 요청이 나가지 못함. **인증 토큰 발급은 허용**됩니다(토큰은 `auth.py` 가 세션과 별개로 `requests.post` 호출 —
  모든 POST 를 막으면 인증까지 막혀 조회가 불가능하므로 구분). 차단을 설치하지 못하면 조회도 하지 않고 중단(fail-closed). 위반이 발생하면
  `READONLY_VIOLATION` 중단 표식 + 긴급 알림(종료 1). APPLY 경로와 LIVE 봇·어댑터는 변경하지 않습니다.
- **state 불변 검사(DRY):** 감시기가 `.watch/.run/.bot` 락을 모두 잡은 동안의 **전후 해시만** 비교합니다(로그 `state 불변 확인`). 락이 풀려 있던 때 정상 LIVE 봇이 갱신한 것은
  DRY 의 변경으로 오인하지 않으며, 락을 잡은 동안 바뀌면 `STATE_CHANGED_IN_DRY` 중단 표식 + 긴급 알림.
- **정정 직전 재조회·재계획**, 최소 변경폭, 종목별 최소 간격·일일 한도(시도 직후 `.watch_state.json` 에 기록).
- **journal 복구:** APPLY 는 미해결 journal 을 이력으로 확인해 정확히 일치할 때만 연결(그 외는 중단). 복구된 정정은 일일 횟수와 마지막 시도 시각(journal 의 실제 시도 시각)에 반영합니다. **횟수 기록 → state 저장 → journal 제거** 순서이고 항목 id 로 한 번만 세므로, 어느 단계에서 종료돼도 재시작 시 이중 가산·누락이 없습니다.
  정상 정정 성공 경로도 같습니다: 감시기가 `apply_amend(defer_journal_cleanup=True)` 로 journal 항목을 유지한 채 받아 횟수를 기록·저장한 **뒤에** 직접 지웁니다(수동 도구는 기존대로 즉시 정리). 정정 직후 종료돼 구 주문번호가 이미 사라진 상태에서 재시작해도 이동된 메타의 `amended_from` 으로 정정을 인식해 횟수를 셉니다. DRY 는 복사본으로 확인만 하고 아무것도 쓰지 않으며 긴급 알림.
- **중단(halt):** `REJECTED_LATE` / `UNCERTAIN` / `LINK_FAILED` / 해소 불가 journal / `RECORD_SAVE_FAILED` / `RECORD_CORRUPT` → `.watch_halt.json` 기록 + 긴급 알림 + 이후 APPLY 는 정정하지 않음(재알림 6시간 간격).
  **중단 표식은 감시 기록 저장보다 먼저** 만들어, 기록 저장이 실패해도 중단은 유지됩니다.
  위험 결과로 남은 journal 항목에는 `review_required` 표식도 함께 남깁니다 — `.watch_halt.json` 저장이 실패해도 다음 APPLY 는
  이 항목을 **자동 복구하지 않고** 중단 표식(`JOURNAL_REVIEW_REQUIRED`)을 다시 만듭니다. 사람이 앱에서 확인한 뒤
  `ops/meritz_buy_loc_reprice.py --apply --confirm "REPRICE <종목>"` 로 journal 을 해소하세요(수동 복구는 감시 횟수에 반영되지 않음).
  `REJECTED_LATE`(API 는 접수했으나 이력에 거부)도 같은 방식으로 journal 항목을 **지우지 않고** 표식을 남깁니다. 정산 중복 위험은 없지만,
  어댑터가 거부된 정정 행도 원주문을 `정정됨`·잔량 0 으로 표시해 **살아 있는 원주문을 봇의 잔여 주문 취소가 건너뛰므로**
  이 항목도 **봇의 정산·신규 주문을 막습니다**(해당 종목). 앱/HTS 에서 원주문이 살아 있는지 확인해 필요하면 직접 취소한 뒤,
  수동 도구가 "정정 거부 확인"으로 항목을 정리해야 봇이 재개됩니다(정리 전에 반드시 원주문 상태를 확인하세요).
  자동 재주문은 하지 않습니다. 원인(앱에서 주문 상태 확인, journal 해소)을 확인한 뒤 **사람이** `.watch_halt.json` 을 삭제해야 재개됩니다.
- **감시 기록 손상:** `.watch_state.json` 을 읽을 수 없으면 기존 정정 횟수를 알 수 없으므로 손상 파일을 `.watch_state.json.corrupt-<시각>` 으로 보존하고 **DRY·APPLY 모두 `RECORD_CORRUPT` 중단 표식을 남깁니다**(DRY 가 먼저 만나 새 기록을 저장해도 이후 APPLY 가 한도 초기화 상태로 정정하지 못하게). DRY 자체는 계속 동작하고 APPLY 만 보류합니다. 사람이 이전 횟수를 확인한 뒤 `.watch_halt.json` 을 삭제하면 재개됩니다.
- **알림 중복 방지:** 같은 계획/오류는 반복 알리지 않습니다(오류 60분, 보류 안내 6시간). 알림 실패는 감시를 죽이지 않습니다.
- 조회 오류(정정 전)는 중단 표식 없이 긴급 알림 + 종료 코드 1.

## 도입 순서 (권장)
1. 배포 후 DRY 로 며칠 돌려 로그(`logs/reprice-watch-*.log`)·알림이 의도대로인지 확인 — 후보가 실제 앱 시세와 맞는지 대조.
2. APPLY 는 한 종목(`REPRICE_WATCH_SYMBOLS=SOXL`)으로 시작, 첫 정정 결과를 런북 7절 절차로 확인.

## 제한
- `REPRICE_WATCH_MAX_PER_RUN` > 1 이면 같은 실행에서 여러 시도가 연속 수행되어 최소 간격이 시도 사이에 적용되지 않습니다(기본 1 권장).
- `original_price` 메타가 없는 주문(핫픽스 배포 전 접수분)은 감시기가 정정하지 않습니다(`--original` 수동 도구 사용).
- 거부된 정정 행이 있는 주문은 생존 여부를 판별할 수 없어 보류+알림. 정정 가능 시간·부분체결 정정 수량은 문서로 확인되지 않아
  부분체결 주문은 제외, 시간 제한은 브로커 거부에 의존. 시세 응답에 시각이 없어 신선도는 직전 재조회로만 완화.
- 합산 주문(PR "수량 합산")은 정정 대상이 아닙니다.
