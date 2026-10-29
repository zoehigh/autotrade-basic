# 실행 트리거 가이드 (로컬 cron · GitHub Actions dispatch)

> 최신 갱신: 2026-10-29
>
> 이 문서는 **실제로 존재하는 트리거만** 다룹니다. 삭제된 워크플로(`trade_demo.yml` /
> `trade_real.yml`), 폐기된 event type 의 분리형 이름, `KIS_MODE` 컬럼은 모두 제거했습니다.
> 실행 단계(5단계)의 정의와 스케줄 원문은 `ops/cron.example`,
> dispatch type ↔ 워크플로 매핑은 `README.md` 의 "repository_dispatch 트리거" 표를
> 기준으로 맞춥니다.

---

## 1. 실행 경로는 두 가지 (동시에 켜지 마세요)

| 경로 | 트리거 | 단계 진행 | 승인 |
|------|--------|-----------|------|
| **로컬 cron (권장)** | `ops/cron.example` 의 cron 5줄 | SETTLE → PREVIEW → APPROVE → EXECUTE → VERIFY 를 **각각 별도 실행** | 사람이 PREVIEW 로그의 `plan_id` 를 확인해 APPROVE 에 전달 |
| **GitHub Actions** | `repository_dispatch` (아래 표) | `run_phase=LIFECYCLE`(기본) 이 한 job 안에서 SETTLE → PREVIEW → APPROVE → EXECUTE | 승인 파일은 자동으로 이어지지만, 봇의 승인 게이트(plan_hash·세션·상태해시 바인딩, 허용오차, TTL)를 통과해야만 주문 |

- **LIVE 는 `RUN_PHASE` 가 필수**입니다. 값이 없으면 봇이 브로커 생성 **전에** 차단합니다
  (fail-closed). `run_phase` 를 비운 Actions 실행은 DRY 미리보기 전용입니다.
- 사람이 승인하는 **정식 경로는 로컬 cron 5단계**입니다. Actions 는 실행마다 새
  체크아웃이라 `plans/`·`approvals/` 가 실행 사이에 이어지지 않습니다(self-hosted runner 예외).
- 두 경로를 동시에 켜면 같은 `.run.lock`/`.state.json` 을 두 주체가 갱신합니다. 하나만 켜세요.

---

## 2. 로컬 cron 5단계 (사람 승인)

`ops/cron.example` 원문 기준 (서버 TZ=Asia/Seoul, KST):

| 시각 (KST) | 단계 | 명령 요지 |
|------------|------|-----------|
| 10:05 | `SETTLE` | `RUN_PHASE=SETTLE LAUNCH_MODE=LIVE ops/run_daily.sh` |
| 10:15 | `PREVIEW` | `RUN_PHASE=PREVIEW LAUNCH_MODE=LIVE ops/run_daily.sh` |
| 18:20 | `APPROVE` | `RUN_PHASE=APPROVE PLAN_ID=<PREVIEW plan_id> LAUNCH_MODE=LIVE …` |
| 18:30 | `EXECUTE` | `RUN_PHASE=EXECUTE APPROVAL_PLAN_ID=<plan_id> LAUNCH_MODE=LIVE …` |
| 18:45 | `VERIFY` | `RUN_PHASE=VERIFY APPROVAL_PLAN_ID=<plan_id> LAUNCH_MODE=LIVE …` |

- `AUTO_EXECUTE=true`(opt-in)이면 PREVIEW 런이 자동 승인·실행까지 이어집니다. VERIFY 는
  여전히 별도 런(18:45)입니다.
- `SETTLE` 은 정규장 마감(16:00 ET)이 아니라 **애프터아워 종료(20:00 ET)** 이후에만 정산합니다.
  10:05 KST 는 동절기(ET 20:05) 기준 여유 시각이며 `AFTER_HOURS_END_ET_HOUR` 로 조정합니다.
- `SETTLE` 대상은 **직전 마감 세션**, 주문 대상은 그 **다음 세션**입니다. 그래서 KST 아침
  PREVIEW 와 KST 18:30 EXECUTE 의 세션이 일치합니다.
- `PREVIEW`/`EXECUTE` 는 프리장 개장(KST 17:00 DST / 18:00 비DST) 전이면 **자동 대기**합니다
  (real/demo 공통). 대기 중에는 전역 잠금을 잡지 않습니다 — §5 참고.
- `VERIFY` 는 `ops/verify_run.py` 를 직접 부르지 않습니다. `run_daily.sh` 가
  `RUN_PHASE=VERIFY` 로 `trading_bot.py` 를 실행해 승인 파일 + 커밋된 SETTLE evidence 독립
  재계산 + 브로커 read-only 대조를 한 번에 수행합니다 (`ops/verify_run.sh` 는 레거시).
- `VERIFY` 종료 코드: `0`=정상, `1`=실행 실패/검증 실패, `2`=승인만 되고 미실행.
  `run_daily.sh` 는 LIVE 에서 2 를 **1 과 다른 문구**로 알립니다(긴급 아님, 운영자 확인 필요).

설치/되돌리기 명령은 `ops/cron.example` 하단 주석을 그대로 사용하세요.

---

## 3. GitHub Actions `repository_dispatch` 매핑

브로커별로 워크플로 파일이 분리되어 있습니다. `KIS_MODE` 는 더 이상 쓰지 않습니다
(현재는 `BROKER_MODE=demo|real` + `BROKER`).

| dispatch type | 워크플로 | 브로커 모드 | 비고 |
|---------------|----------|-------------|------|
| `trigger_trade_demo` | `trade_kis_demo.yml` | kis / demo | **레거시 type** (하위호환) |
| `trigger_trade_real` | `trade_kis_real.yml` | kis / real | **레거시 type** (하위호환) |
| `trigger_trade_kis_demo` | `trade_kis_demo.yml` | kis / demo | |
| `trigger_trade_kis_real` | `trade_kis_real.yml` | kis / real | |
| `trigger_trade_kiwoom_demo` | `trade_kiwoom_demo.yml` | kiwoom / demo | |
| `trigger_trade_kiwoom_real` | `trade_kiwoom_real.yml` | kiwoom / real | self-hosted runner (등록 IP 필요) |
| `trigger_trade_ls_demo` | `trade_ls_demo.yml` | ls / demo | |
| `trigger_trade_ls_real` | `trade_ls_real.yml` | ls / real | |
| `trigger_trade_toss_real` | `trade_toss_real.yml` | toss / real | 토스는 real 전용 |
| `trigger_trade_nhplug_demo` | `trade_nhplug_demo.yml` | nhplug / demo | |
| `trigger_trade_nhplug_real` | `trade_nhplug_real.yml` | nhplug / real | |
| `trigger_trade_meritz_real` | `trade_meritz_real.yml` | meritz / real | 수동 트리거만 (오픈API 신청 개방 확인 전) |

`trade_*.yml` 은 모두 `./.github/workflows/trade_base.yml`(reusable)을 호출합니다.
단계·대상 지정은 `workflow_dispatch` 입력 또는 dispatch payload 로 넘깁니다.

### payload 예시

```json
{
  "event_type": "trigger_trade_kis_real",
  "client_payload": {
    "run_phase": "LIFECYCLE",
    "trade_mode": "LIVE",
    "symbols": "TQQQ:NAS,SOXL:AMS"
  }
}
```

- `run_phase`: `LIFECYCLE`(기본) | `SETTLE` | `PREVIEW` | `APPROVE` | `EXECUTE` | `VERIFY`.
  빈 값은 레거시 단일 실행(DRY 전용)입니다.
- `trade_mode`: 비우면 워크플로 기본 규칙을 따릅니다(real 은 `DRY`, demo 는 `LIVE` 등
  `trade_base.yml` Resolve 단계 기준). **LIVE 를 켜기 전에 `DRY` 로 1회 확인하세요.**
- `plan_id`(APPROVE) / `approval_plan_id`(EXECUTE·VERIFY) / `auto_execute` 도 payload 로
  넘길 수 있습니다.

---

## 4. 권장 dispatch 시각 (Actions 경로)

`run_phase=LIFECYCLE` 은 한 job 에서 SETTLE → PREVIEW → APPROVE → EXECUTE 를 이어서
실행하므로 **하루 1회 dispatch** 면 충분합니다.

| 항목 | 값 |
|------|-----|
| 빈도 | **브로커·모드 조합당 1일 1회** (2회 dispatch 는 중복 주문 위험) |
| 권장 시각 (KST) | **10:10 이후 ~ 17:00 이전** |
| 이유 | ① `SETTLE` 은 애프터아워 종료(20:00 ET = KST 09:00 DST / 10:00 비DST) **이후**에만 정산합니다 — 그 전에 dispatch 하면 SETTLE 이 보류로 끝나 체인이 중단됩니다(fail-closed). ② PREVIEW 는 직전 마감 세션의 **다음 세션**을 대상으로 계획을 만들고, EXECUTE 는 프리장 개장(KST 17:00 DST / 18:00 비DST)까지 스스로 대기합니다 |
| 프리장 직후 dispatch | 프리장 개장 무렵(KST 17:00/18:00)에 dispatch 해도 동작합니다(대기 없이 바로 실행). 다만 그 시각엔 이미 대기 구간이 끝나 있으므로 위 여유 시간대가 안전합니다 |
| DST | 미국 DST 전환(3월 둘째 일요일 / 11월 첫째 일요일)에도 cron 은 UTC 고정, KST 기준 시각은 자동 조정됩니다 |

- `SETTLE` 이 보류(애프터아워 미종료·정산 미확정·휴장일 등)를 반환하면 SETTLE 단계가
  실패로 끝나 **체인이 중단**됩니다. 이는 정상적인 fail-closed 동작이며, 시각을 늦춰
  다시 dispatch 하면 됩니다.
- Actions 캐시는 **브랜치별 격리**입니다. T 보정 등 state 변경은 운영 브랜치(`main`)에서만 반영됩니다.

---

## 5. 잠금(동시 실행 보호)

- 셸 런처(`ops/run_daily.sh`)는 **단계와 무관하게 항상** `RUN_LOCK_PATH`(기본
  `$APP_DIR/.run.lock`)를 `flock` 합니다. DRY 는 `flock -n`(즉시 종료), LIVE 는
  `flock -w 300`(최대 5분 대기)입니다.
- "대기 단계는 잠그지 않는다" 판정의 **단일 출처는 `trading_bot.py`
  `_phase_runs_without_outer_lock()`** 입니다. 셸은 이 규칙을 다시 구현하지 않습니다.
  대기 단계(EXECUTE, PREVIEW+AUTO_EXECUTE)면 자식 파이썬이 기동 직후
  `_release_inherited_outer_lock()` 으로 상속 fd 의 flock 만 해제합니다.
- 따라서 잠금이 비는 구간은 **프리장 대기뿐**이고, 계획 생성·승인 재검증·선점·state 저장·
  주문 전송은 항상 `pipeline.global_lock()` 안에서 직렬화됩니다 (fail-open 아님).
- `RUN_LOCK_HELD=1` 플래그만으로는 절대 우회하지 않습니다 — 자식 파이썬은 fd 가 잠금 파일과
  **같은 inode** 이고 그 fd 가 실제 잠금 보유자일 때만 상속으로 인정합니다. 직접 CLI 실행은
  실제 잠금을 잡고, 다른 실행이 잡고 있으면 끝날 때까지 대기합니다.

---

## 6. 검증 방법

1. Actions 실행 로그 확인: `https://github.com/{owner}/{repo}/actions`
2. KIS 토큰 재시도 로그: `"network_retry"` 문자열 검색
3. EXECUTE 대기/재검증 로그: `"프리장 오픈 대기 중"`, `[잠금]` 문자열 검색
   (대기 단계면 `[잠금] 대기 단계 → 런처 잠금 … 을 해제했습니다` 가 1회 출력됩니다)
4. 승인/실행 게이트: PREVIEW 로그의 `plan_id`, APPROVE·EXECUTE 의 `plan_hash`/세션/상태해시
   일치 여부 확인
5. 같은 환경에서 **중복 dispatch 가 없었는지** 확인 (브로커·모드당 1회)

---

## 7. 주의사항

- **2회 dispatch 금지** — 같은 브로커·모드 조합에서 중복 주문이 발생합니다.
- PAT(Personal Access Token)가 노출되지 않도록 GitHub Secrets 또는 cron 서비스의 secure
  variable 을 쓰세요. 권한은 `Actions: Read/Write`, `Contents: Read` 로 최소화합니다.
- cron 서비스 중단 시 거래 누락 가능 — 서비스 health check 를 권장합니다.
- GitHub Token 만료 시 refresh 필요.
- 계좌 지문을 쓰는 브로커(KIS/NHPLUG 등 `account_no` 설정)는 `ACCOUNT_FINGERPRINT_SECRET`
  이 **필수**입니다. 없으면 승인/실행이 fail-closed 로 중단됩니다.
- 계좌번호가 없는 브로커(메리츠)는 `MERITZ_ACCOUNT_BINDING`(또는 `BROKER_ACCOUNT_BINDING`)
  을 설정해야 LIVE 승인/실행이 가능합니다.
- `TRADE_MODE=LIVE` 전환 전에는 항상 `DRY` 로 주문 목록을 한 번 확인하세요.
