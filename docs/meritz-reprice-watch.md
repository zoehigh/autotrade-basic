# 메리츠 가격 정정 감시기

`ops/meritz_reprice_watch.py` — 크론이 주기적으로 호출하면 한 번 점검하고 종료합니다.
보정된 일반모드 BUY LOC 를 가격 정정 도구(`src/meritz_reprice.py`)의 규칙으로 계획·정정합니다.
**기본은 DRY**(조회·계획·알림만). 크론 등록과 APPLY 활성화는 사람이 결정합니다 (이 PR 은 등록하지 않음).

## 실행 방식
- 런처 `ops/run_reprice_watch.sh` (예: `*/10 19-23,0-5 * * 1-6`, `ops/cron.example` 에 주석으로만 수록)
- 크론은 KST 고정으로 자주 불러도 됩니다. **감시 창은 코드가 ET 로 판단**하므로 서머타임/동절기를 따로 관리하지 않습니다.
  - 미국(NYSE) 영업일만, 기본 ET 06:00 ~ min(15:30, 정규장 마감 − 30분) — 조기 마감일은 창이 같이 줄어듭니다.
  - 창 밖·휴장일·달력 오류(fail-closed)는 API 호출 없이 즉시 종료(0).
  - 서머타임 19:00~04:30 KST / 동절기 20:00~05:30 KST 가 창에 해당 (봇의 18:30 주문 이후, 마감 전).

## 설정 (환경변수, 잘못된 값은 오류로 중단 — 종료 코드 2)
| 이름 | 기본 | 의미 |
|---|---|---|
| `REPRICE_WATCH_MODE` | `DRY` | `DRY` / `APPLY` |
| `REPRICE_WATCH_START_ET` / `_END_ET` | `06:00` / `15:30` | 감시 창 (ET) |
| `REPRICE_WATCH_CLOSE_BUFFER_MIN` | `30` | 정규장 마감 몇 분 전에 끝낼지 |
| `REPRICE_WATCH_MIN_STEP` | `0.05` | 이 금액(USD) 미만의 변경은 건너뜀 |
| `REPRICE_WATCH_MIN_INTERVAL_MIN` | `20` | 같은 종목 정정 시도 사이 최소 간격 |
| `REPRICE_WATCH_MAX_AMENDS_PER_DAY` | `3` | 종목별 하루(ET 세션) 정정 한도 |
| `REPRICE_WATCH_SYMBOLS` | (전체) | 쉼표로 종목 제한 |

## 실주문(APPLY) 활성화 — 3개가 모두 필요
`REPRICE_WATCH_MODE=APPLY` + `TRADE_MODE=LIVE` + `ALLOW_BROKER_ORDERS=1` (+ `BROKER=meritz`).
하나라도 없으면 **DRY 로 낮추지 않고 실행을 거부**합니다(종료 2). 런처는 `.env` 의 같은 이름을 무시하고
**크론 줄에서 명시한 값만** 사용하므로 `.env` 실수로 실주문이 켜지지 않습니다.

## 안전장치
- **중복 실행 방지:** `.watch.lock` + 운영 런처 `.run.lock` + 봇 `.bot.lock` 을 모두 non-blocking 으로 잡고, 하나라도 사용 중이면 조용히 건너뜁니다(0). 정정 도구와도 상호 배제.
- **정정 직전 재조회·재계획**, 최소 변경폭, 종목별 최소 간격·일일 한도(시도 직후 `.watch_state.json` 에 기록).
- **journal 복구:** APPLY 는 미해결 journal 을 이력으로 확인해 정확히 일치할 때만 연결(그 외는 중단). DRY 는 복사본으로 확인만 하고 아무것도 쓰지 않으며 긴급 알림.
- **중단(halt):** `REJECTED_LATE` / `UNCERTAIN` / `LINK_FAILED` / 해소 불가 journal → `.watch_halt.json` 기록 + 긴급 알림 + 이후 APPLY 는 정정하지 않음(재알림 6시간 간격).
  자동 재주문은 하지 않습니다. 원인(앱에서 주문 상태 확인, journal 해소)을 확인한 뒤 **사람이** `.watch_halt.json` 을 삭제해야 재개됩니다.
- **알림 중복 방지:** 같은 계획/오류는 반복 알리지 않습니다(오류 60분, 보류 안내 6시간). 알림 실패는 감시를 죽이지 않습니다.
- 조회 오류(정정 전)는 중단 표식 없이 긴급 알림 + 종료 코드 1.

## 도입 순서 (권장)
1. 배포 후 DRY 로 며칠 돌려 로그(`logs/reprice-watch-*.log`)·알림이 의도대로인지 확인 — 후보가 실제 앱 시세와 맞는지 대조.
2. APPLY 는 한 종목(`REPRICE_WATCH_SYMBOLS=SOXL`)으로 시작, 첫 정정 결과를 런북 7절 절차로 확인.

## 제한
- `original_price` 메타가 없는 주문(핫픽스 배포 전 접수분)은 감시기가 정정하지 않습니다(`--original` 수동 도구 사용).
- 거부된 정정 행이 있는 주문은 생존 여부를 판별할 수 없어 보류+알림. 정정 가능 시간·부분체결 정정 수량은 문서로 확인되지 않아
  부분체결 주문은 제외, 시간 제한은 브로커 거부에 의존. 시세 응답에 시각이 없어 신선도는 직전 재조회로만 완화.
- 합산 주문(PR "수량 합산")은 정정 대상이 아닙니다.
