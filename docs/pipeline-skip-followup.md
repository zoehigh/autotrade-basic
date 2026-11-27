# pipeline 휴장·마감 후 SKIP — 후속 검토 메모

> 대상: `src/broker/market_utils.py` `us_order_window()`, `ops/session_gate.py`,
> `.github/workflows/trade_base.yml`. 조사 결과와 후속 과제만 담습니다.
> **cron·dispatch 시각·payload·운영 브랜치는 바꾸지 않았습니다.**

## 1. pipeline 적용에 필요한 cron 변경 검토 (조사만)

### 1-1. 모의 정기 dispatch (외부 스케줄러, UTC)

| 슬롯 | 서머타임 (예: 2026-10-29) | 동절기 (예: 2026-11-30) | 조기폐장 (2026-11-27, 마감 18:00Z) |
|---|---|---|---|
| KIS 19:10 | 정규장 중 (ET 15:10) | 정규장 중 (ET 14:10) | 마감 후 |
| Kiwoom 20:15 | **마감 후** (ET 16:15) | 정규장 중 (ET 15:15) | 마감 후 |
| NHPlug 20:20 | **마감 후** (ET 16:20) | 정규장 중 (ET 15:20) | 마감 후 |

- ⚠️ **서머타임에는 Kiwoom·NHPlug 슬롯이 매일 SKIP(성공)** 이 되고, 애프터아워 종료(ET 20:00) 전이라
  **정산도 실행되지 않습니다.** 변경 전 main 에서도 같은 시각은 시간 예산 게이트에서 실패(주문·정산 없음)했으므로
  매매 동작은 같지만, 빨간 실패가 초록 SKIP 으로 바뀝니다.
  **다음 서머타임 시작(2027-03-14) 전에** 두 슬롯을 정규장 중(서머타임 기준 20:00Z 이전, 마감 직전 여유 포함)으로
  옮기는 결정이 필요합니다. KIS 19:10Z 는 연중 정규장 중입니다.
- 조기폐장일에는 세 슬롯 모두 SKIP 이며 정산은 다음 거래일 슬롯에서 수행됩니다.

### 1-2. 로컬 cron (`ops/cron.example`, KST)

- 로컬 cron 은 Actions 게이트(`session_gate.py`, 대기 상한)를 거치지 않습니다. 주문 창 판정은 봇의
  EXECUTE(및 PREVIEW+AUTO_EXECUTE)에서만 적용됩니다.
- 휴장일 18:30 EXECUTE 는 이제 exit 0 (SKIP)입니다. **18:45 VERIFY 는 기존처럼 실패(비 0)** 합니다
  (승인 실행 세션이 아직 시작 전이거나 미실행). 휴장일에 VERIFY 를 건너뛰는 런처/cron 처리는 후속 과제입니다.

### 1-3. 메리츠 Real LIVE (레거시 계보, 이번 범위 밖)

- `docs/cycle-handover.md` 기준 운영은 KST 18:30 통합 실행 + 18:45 `verify_run.sh` 이며, 이번 변경은 적용되지 않습니다.
- pipeline 으로 옮기려면 단일 18:30 실행을 `ops/cron.example` 의 단계별 `RUN_PHASE` 런
  (SETTLE → PREVIEW → APPROVE → EXECUTE → VERIFY, 또는 AUTO_EXECUTE)으로 바꿔야 합니다. LIVE 는 `RUN_PHASE` 가
  필수이고, VERIFY 는 `verify_run.sh` 대신 `RUN_PHASE=VERIFY` 런(`run_daily.sh`)을 씁니다
  (`docs/cron-dispatch-guide.md`). 계좌 바인딩(`MERITZ_ACCOUNT_BINDING`)도 필요합니다.
- 미확인: 운영 서버의 실제 crontab·배포 코드, 메리츠 LIVE 에서 단계별 런의 실제 호환성.

## 2. 남은 한계·후속 과제

1. 주문 창은 열려 있으나 ET 달력 날짜가 비영업일인 시각(예: KST 월요일 오전 = ET 일요일 저녁)의 EXECUTE 는
   기존대로 `broker.is_trading_day()` 에서 실패합니다.
2. 조기폐장일에도 애프터아워 종료를 ET 20:00 으로 보므로, 그 전 실행은 정산도 다음 실행으로 미룹니다.
3. DRY 의 마감 후(애프터아워 중) 실행은 기존 레거시 동작 그대로입니다.
4. 정기 슬롯이 마감 후 시각으로 설정되면 매일 SKIP(성공)으로 끝나며 Actions 요약·텔레그램의 사유로만
   드러납니다. 연속 SKIP 감시는 후속 과제입니다.
5. 주문 창이 닫힌 날의 LIFECYCLE 은 PREVIEW 를 생략하므로, PREVIEW 에서만 판정되는 보류(리버스 종료 판정 등)는
   다음 거래일에 드러납니다.
6. hosted 러너에서 주문 창이 열려 있고 프리장 대기가 상한을 넘으면 기존대로 실패합니다 (SKIP 아님).
7. step timeout/취소 시 GitHub Actions 의 step output 보존은 실제 실행으로 미검증입니다
   (표식이 비면 캐시를 저장하고 검증하도록 설계).
