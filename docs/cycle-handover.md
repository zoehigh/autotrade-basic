# 손매매 계좌 인계 → 자동매매 시딩 → 사이클 종료(B안) 런북

작성: 2026-10-15 · 갱신: 2026-10-20
대상: TQQQ, SOXL (메리츠 실전)
코드 근거: `trading_bot.py`, `src/state.py`, `src/strategy.py`

## 전제 (2026-10-20 확정)

- **이 코드는 메리츠 실전에 적용된 적이 없습니다.** 계좌는 사용자가 **손으로 직접 매매**해 왔고,
  코드는 preflight / DRY / 명시적 보정으로만 사용됐습니다.
- **운영 호스트: 별도 전용 VPS + `cron` + `flock`.** GH Actions는 사용하지 않습니다
  (외부 cron dispatch도 꺼져 있음).
- 배포 위치: 운영 VPS `~/investment/muhan-v4` (commit `5e48e69`)
- **개발 머신은 preflight / DRY / 보정 전용 — 실주문 금지.**
- 운영 상태는 **운영 VPS의 `.state.json`** 이 권위입니다. (GH Actions 캐시 아님)

---

## 0. 현재 상태 (2026-10-20 VPS 동기화 완료)

`.state.json`은 10/14 스냅샷이었고, **2026-10-20 VPS에서 `STATE_REVERSE_RECONCILE_ONLY`로 동기화를 완료**했습니다.
아래 "VPS (동기화 후)" 값이 현재 운영 기준입니다.

| 항목 | 로컬 state (10/14, stale) | **VPS (동기화 후)** |
|---|---|---|
| TQQQ T | 9.375 | **12.375** (수동 보정, §2-보정 참고) |
| **TQQQ 분할수** | 40 | **20** (2026-10-20 정정, §0-20분할 전환 참고) |
| TQQQ net_invested | $4,126.91 | **$5,615.60** |
| SOXL T | 9.65625 | **12.6562** |
| SOXL net_invested | $3,996.61 | **$5,373.93** |
| net_invested_status | valid | valid |
| **effective_seed** | **0.0** | **0.0** (→ config `{SYMBOL}_SEED` 폴백) |
| **cycle_start_date** | **`""`** | **2026-09-23 / 2026-09-22** (설정 완료) |
| watermark(last_updated) | 2026-10-14T08:09:04Z / 08:05:55Z | 2026-10-19T08:06:49Z / 08:04:19Z |
| last_processed_ordno | `20261014:0001650552` / `20261014:0001647177` | `20261019:0000076920` / `20261019:0000073422` |
| 보유 | — | TQQQ 74주 @ $76.4687 / SOXL 44주 @ $124.7454 |
| 최근가 | — | TQQQ $70.64 / SOXL $98.65 |
| 확정 시드(운영) | — | **$9,696.18 / $9,627.46** |
| state_hash | — | `0a146fd5…` / `72937302…` |

**반영된 손매매 체결**: 10/15 · 10/16 · 10/19 (TQQQ 3+2+1건, SOXL 3+3+2건).

**TQQQ T 보정 (2026-10-20)**: reconcile 결과 11.875는 **과소값**이었습니다.
`orders_meta`가 없는 수동매수 폴백(`src/state.py:1419`)이 `1건 → +0.5`로 계산했으나,
10/13→10/14 실적(3주×2건 → **+2.0**)과 `src/strategy.py:618-621` 규칙(1건 주문 = 1회차)은 `1건 → +1.0`입니다.
10/19 1건을 1회차로 보고 `STATE_REPAIR_TARGET_T=12.375`로 보정했습니다.

**검증 로그 요약 (2026-10-20, 운영 VPS)**
- preflight 1~6단계 통과 (read-only, 주문 단계는 `--live-order-probe` 없이 skip)
- reconcile-only: TQQQ 6건 / SOXL 8건 체결 반영, `[불일치]` 없음
- DRY 실행 전후 `.state.json` sha256 **불변** → DRY 미저장 확인
- 주의: 브로커 주문가능금액($11,752.21)은 시드 기준 잔여($4,080.58)보다 큼 → 봇은 시드 캡 적용
- 수수료: 메리츠 계좌 **2026-12-31까지 수수료 면제** → `COMMISSION_RATE=0.0000` 사용 중.
  `COMMISSION_RATE`는 `generate_cycle_report()`의 순수익 계산에만 쓰이므로 **전략 신호에는 영향 없음**.
  **2027-01-01부터 실제 요율(예: `0.0025`)로 반드시 변경** — 변경 전까지 사이클 리포트 순수익이 과대표시됩니다.

**미추적 백업**: `.state.json.bak-20261013`, `.state.json.bak-20261013-effix`,
`.state.json.bak-20261020-090926`, `.state.json.bak-20261020-091421-pre-cycle-start`

**운영 실행 방식 (2026-10-20 준비 완료)**

| 항목 | 값 |
|---|---|
| 런처 | `~/investment/muhan-v4/run_daily.sh` (flock + 로그 14일 보관 + 일회성 복구 플래그 unset) |
| cron (현재) | `20 17 * * 1-5 FORCE_SYMBOLS=TQQQ:NAS LAUNCH_MODE=DRY .../run_daily.sh` (검증용, 주문 없음) |
| 자동 실행 | **보류** — 사용자 승인 후 `30 17 * * 1-5 .../run_daily.sh` 등록 |
| 서버 TZ | `Asia/Seoul` → `CRON_TZ` 불필요 (이미 설정돼 있음) |
| 텔레그램 | `.env` 설정 완료, 발송 테스트 성공 |
| 로그 | `logs/run-YYYYMMDD-HHMMSS.log` |

- 같은 VPS에 기존 인스턴스 `~/investment/autotrade-basic`(rev `c8a5efa`, `BROKER=toss`,
  `TRADE_MODE=DRY`, shadow_runner 17:00/07:00)과 KIS 봇이 함께 돌고 있습니다.
  디렉터리·`.state.json`·lock 파일이 분리되어 **주문 충돌 없음**.
- `LAUNCH_MODE`/`FORCE_SYMBOLS`를 지정하지 않으면 `.env`의 `TRADE_MODE=LIVE` + 양종목으로 실행됩니다.

**LIVE 1세션 검증 (2026-10-20 17:37 KST, TQQQ 단일)**

- 1차 시도(17:32): 쿼터매도 LOC가 `IGW50002`로 거부 → **접수 0건**, fence 자동 해제(리스크 없음).
  원인: `oder_cond_cls_code` 하드코딩 `"0"`. **LOC(32)는 `7`**이어야 함.
- 수정: `_ODER_COND_CLS_CODE` 매핑 추가(`441838c`), 주문유형 6종 단위 테스트 추가 → 79 passed.
- 2차 시도(17:37): **주문 6/6 접수 성공, 실패 0건**, `orders_meta` 6건 기록.

| 주문 | 유형 | 수량 | 가격 | 주문번호 |
|---|---|---|---|---|
| 쿼터매도 | LOC(32) | 18 | $80.84 | `20261020:0000759959` |
| 목표매도 | LIMIT(01) | 56 | $87.93 | `20261020:0000759963` |
| 전반전 평단 | LOC(32) | 1 | $76.46 | `20261020:0000759969` |
| 추가 1단계 | LOC(32) | 1 | $73.85 | `20261020:0000759978` |
| 추가 2단계 | LOC(32) | 1 | $49.23 | `20261020:0000759984` |
| 추가 3단계 | LOC(32) | 1 | $36.92 | `20261020:0000759988` |

- **SOXL은 아직 LIVE 미검증** (cron 교체 전 판단 필요).

**TQQQ 20분할 전환 정정 (2026-10-20 18:00~18:10 KST)**

`TQQQ_SPLITS`가 40으로 잘못 설정되어 17:37 LIVE에서 40분할 기준 주문 6건이 접수됐습니다.
TQQQ도 **20분할**이므로 `TQQQ_SPLITS=20`으로 바꾸고 기존 주문을 **정정**했습니다.

| 항목 | 40분할(잘못됨) | 20분할(정정 후) |
|---|---|---|
| 구간 | 전반전 | **후반전** |
| 1회 매수금 | $147.71 | **$535.16** |
| 쿼터매도 LOC | 18주 @$80.84 | **18주 @$73.74** |
| 목표매도 LIMIT | 56주 @$87.93 | 56주 @$87.93 (유지, 평단×1.15) |
| 정상매수 LOC | 평단 1주 @$76.46 | **별지점 7주 @$73.73** |
| 추가매수 LOC | $73.85 / $49.23 / $36.92 | **$66.89 / $59.46 / $53.51** |

최종 유효 주문 6건 (모두 미체결):

| 주문 | 유형 | 수량 | 가격 | 주문번호 |
|---|---|---|---|---|
| 쿼터매도 | LOC(32) | 18 | $73.74 | `20261020:0000771667` |
| 목표매도 | LIMIT(01) | 56 | $87.93 | `20261020:0000759963` (정정 없음) |
| 정상매수 | LOC(32) | 7 | $73.73 | `20261020:0000774127` |
| 추가매수 1단계 | LOC(32) | 1 | $66.89 | `20261020:0000771672` |
| 추가매수 2단계 | LOC(32) | 1 | $59.46 | `20261020:0000771674` |
| 추가매수 3단계 | LOC(32) | 1 | $53.51 | `20261020:0000771675` |

**메리츠 정정 API 실측 결론 (중요)**

- 정정은 `POST /trading/v1/overseas/orders/modify` 사용. **가격은 변경되지만 수량 증가는 반영되지 않습니다.**
  (odqt 1→7을 보냈는데 접수 응답 `rsp_cd=0001`·경고 없음으로 통과했지만 실제 `oder_dqty`는 1주 그대로였습니다.)
  → 수량 변경이 필요하면 **취소 후 신규 주문**으로 처리해야 합니다.
- 해외는 **재정정·취소 시 직전 정정 건의 주문번호**를 원주문번호로 씁니다 (국내와 반대).
- **취소 확인은 주문 상세조회(`/orders/detail`)로 불가**입니다. 상세조회는 해당 주문의 행만 보여주고
  취소가 반영되지 않습니다. 취소는 **이력에 별도 행**(`rctf_cncl_cls_name="취소"`, `orgl_oder_no=취소 대상`)으로
  남으며, 원주문은 잔량 0·`취소`로 갱신됩니다. → **취소 확인은 이력 기준**입니다.
- 원주문은 정정 후에도 이력에 `미체결`로 남는 문제가 있어, 어댑터가 `rctf=1` 정정 행을
  근거로 원주문을 `정정됨`(잔량 0)으로 표시하도록 보완했습니다(`f75e759`).

**수동 매매 이력 T 폴백의 한계**: `src/state.py`의 미등록매수 폴백(`1건 → +0.5`)은 봇이 직접 낸
주문이 아니라 손매매 이력에만 적용됩니다. 인계 이후에는 `orders_meta`가 있으므로 이 경로를 타지 않습니다.

**SOXL LIVE 1세션 (2026-10-20 18:17 KST)** — TQQQ와 동일하게 6/6 접수 성공, 실패 0건.

| 주문 | 유형 | 수량 | 가격 | 주문번호 |
|---|---|---|---|---|
| 쿼터매도 | LOC(32) | 11 | $118.11 | `20261020:0000780733` |
| 목표매도 | LIMIT(01) | 33 | $149.69 | `20261020:0000780737` |
| 후반전 별지점 매수 | LOC(32) | 4 | **$111.51** | `20261020:0000780745` |
| 추가매수 1단계 | LOC(32) | 1 | **$111.51** | `20261020:0000780754` |
| 추가매수 2단계 | LOC(32) | 1 | $96.53 | `20261020:0000780761` |
| 추가매수 3단계 | LOC(32) | 1 | $82.74 | `20261020:0000780768` |

- **×1.19 가격 캡이 매수 2건에 적용**됐습니다 (현재가 $93.71 기준):
  별지점 매수 $118.10 → $111.51, 추가매수 1단계 $115.84 → $111.51.
  → 두 건이 같은 가격($111.51)이 됐습니다. 캡은 더 낮은 가격으로 내리므로 체결은 더 보수적입니다.
- SOXL은 T=12.6562 / 20분할 → 후반전입니다 (설정 변경 없음).
- 두 종목 모두 `orders_meta` 6건, fence 없음, T·순투입금 불변.

**런처 락 경합 수정 (2026-10-20)**: `run_daily.sh`가 DRY는 `flock -n`(즉시 종료), **LIVE는 `flock -w 300`**으로
대기합니다. 이전에는 cron(17:20 DRY)과 수동 LIVE가 겹치면 수동 LIVE가 **exit 0으로 조용히 건너뛰어질**
위험이 있었습니다. LIVE는 이제 5분까지 기다린 뒤에도 못 얻으면 exit 1로 실패합니다.

**추가매수 계단식 재배치 (2026-10-20, `c995412`)**

1.19 상한 보정을 `trading_bot`의 주문 전송 직전 → **전략 주문 생성 단계**로 옮겼습니다.
그 결과 상한에 걸린 추가매수 후보가 정상매수와 **같은 가격으로 뭉치는** 문제가 생겼습니다
(SOXL: 정상매수 $118.10 과 추가매수 1단계 $115.84 가 둘 다 $111.51).

이제 규칙은 다음과 같습니다.

1. 정상매수(BUY LOC)에 현재가 ×1.19 상한을 적용합니다.
2. 추가매수 후보는 기존 공식 `1회 매수금 ÷ (기준 수량 + i)` 을 그대로 사용합니다.
3. **정상매수 보정가 이상인 후보는 건너뜁니다.**
4. 앞 단계보다 엄격히 낮은 가격만 선택하고, 설정된 단계 수를 채웁니다.
5. 캡이 걸리지 않으면 기존 배치가 그대로 유지됩니다.

SOXL 정정 결과 (현재가 $94.89, 상한 $112.91):

| 주문 | 정정 전 | 정정 후 | 주문번호 |
|---|---:|---:|---|
| 정상매수 4주 | $111.51 | $111.51 (유지) | `20261020:0000780745` |
| 추가매수 1단계 | $111.51 | **$96.53** | `20261020:0000787976` |
| 추가매수 2단계 | $96.53 | **$82.74** | `...0000787974` |
| 추가매수 3단계 | $82.74 | **$72.40** | `...0000787973` |

- TQQQ는 캡이 걸리지 않아 **변경 없음** ($73.73 / $66.89 / $59.46 / $53.51).
- 두 종목 모두 매수 가격 중복 없음, state `orders_meta` 6건 일치.
- 주의: **가격이 계단식으로 내려가도 종가가 여러 지정가 이하이면 여러 LOC이 함께 체결될 수 있습니다.**
  이 변경은 중복 가격 방지와 가격 간격 유지를 목적지, 동시 체결 자체를 막지는 않습니다.

**이전 세션 잔여 주문 정리 (2026-10-20, `9400c8d` / `d2f1820`)**

봇은 매 세션마다 새 주문 전체를 새로 냅니다. 그런데 이전 세션에 낸 주문이 아직 살아 있으면
(특히 **목표매도 LIMIT**) 새 주문과 중복되어 매도 수량이 보유량을 초과할 수 있습니다.
이를 막기 위해 신규 주문 제출 **직전**에 봇 소유의 미체결 주문을 정리합니다.

- 대상: `state.orders_meta` 에 있는 **봇이 직접 낸 주문** 중 `미체결`/`부분체결`이고 잔량 > 0
- 제외: 수동 주문(`orders_meta` 에 없음), 리버스 주문(`reverse_action`), 리버스 활성 상태
- 취소 실패 시: 나머지 취소는 모두 시도한 뒤 **신규 주문을 내지 않고 해당 종목만 중단**(`SymbolScopedError`)
- 취소를 지원하지 않는 브로커(KIS/LS/키움/토스/나무)는 오류 없이 건너뜀
- DRY 모드에서는 실행되지 않음

실측(2026-10-20 18:43 KST, TQQQ·SOXL): 기존 12건 취소 → 신규 12건 제출, 실패 0건.
정리 후 상태: TQQQ 매도 합계 74주(보유 74), SOXL 매도 합계 44주(보유 44) — 중복 매도 없음.

> 참고: `orders_meta` 는 취소된 주문도 남겨 두므로 실행할수록 쌓입니다(세션당 6건).
> 부분체결 주문의 meta 를 지우면 재계산 때 T 가 어긋날 수 있어 자동 정리하지 않습니다.

**수동 실행 (자동 실행 보류 중 — 매 거래일 KST 17:20~17:40)**

```bash
~/investment/muhan-v4/run_daily.sh
```

- LOC 주문은 **당일 마감 기준**이라 매 거래일 재제출해야 합니다. 실행을 건너뛰면 그날 전략이 진행되지 않습니다.
- 결과 확인: `ls -1t ~/investment/muhan-v4/logs/ | head -1` → `vi <로그경로>` (`/주문` 검색, `:q` 종료)
- 상태 확인: `STATE_DIAGNOSTIC_ONLY=true ~/.local/bin/uv run python trading_bot.py` (저장소 루트에서)

---

## 1. 확인 완료 사항

### (a) 시드 소스

- `effective_seed = 0.0` → 시드는 **config `{SYMBOL}_SEED`로 폴백**
  (`trading_bot.py:365-367`, `571-573`, `635-639`).
- **운영 VPS `.env`에 확정 시드를 설정해야 합니다**: `TQQQ_SEED=9696.18`, `SOXL_SEED=9627.46`. ✅ 설정 완료
- 🔴 **실측 확인된 문제**: 로컬 `.env` 값(`10000` / `5000`)으로 DRY를 돌리면
  SOXL은 `seed 5000 − net_invested 4880.68 = 잔여 $119.32` → **"1회 매수금 부족"으로 매수가 전면 생략**됩니다.
  시드는 장식이 아니라 **주문 규모를 결정**합니다.
- 함의(B안): `next_cycle_seed = seed + net_profit`(`trading_bot.py:133`)이므로
  운영 VPS `.env` 시드가 곧 B안의 기준값입니다.

### (b) 운용 브로커 = 메리츠(real) — 확정

- 로컬 `.env`는 `BROKER=ls`(stale)이지만, 셸 override(`BROKER=meritz`)로 **실계좌 조회 성공**.
- `.state.json`의 `last_processed_ordno`가 메리츠 합성 형식(`YYYYMMDD:제로패딩`)과 일치.
- 메리츠 이력은 ~1일 후행 → 종료 시 `net_profit`이 불완전할 수 있음(§6).

### (c) 거래소 코드 — 확인

- TQQQ → `OQ` (NAS), SOXL → `AX` (AMS). **SOXL은 AMS가 맞음**(NYS 아님).

### (d) `cycle_start_date` — ✅ 설정 완료

- 증상: `generate_cycle_report`가 `cycle_start_date`가 비면 **전체 이력**을 집계
  (`trading_bot.py:86-102`) → `net_profit`이 전 기간 누적이 되어 **B안 시드가 틀어짐**.
- 2026-10-20 VPS에서 `STATE_SET_CYCLE_START_ONLY`(CAS)로 **TQQQ `2026-09-23` / SOXL `2026-09-22` 설정 완료**.
- 손매매가 다시 발생해 재동기화가 필요하면 **§2 Step 3 → Step 5**를 새 해시로 다시 수행합니다.

### (e) 기타

- **텔레그램**: 로컬/운영 VPS `.env` 모두 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 비어 있음 → 알림 미발송.
- **×1.19 BUY 캡 재현**: 별지점 매수 `$82.05 → $81.16`, SOXL `$118.10 → $117.39`.
  AGENTS.md에 기록된 알려진 동작.

---

## 2. 인계 시딩 절차 (확정) — 운영 VPS에서 실행

> 모든 상태 쓰기 전에 **백업**합니다. 각 repair 모드는 배타(`_exclusive_modes`)이므로 **1회 1개**만 실행합니다.
> env var는 **1회성** — `.env`/cron에 넣지 마세요.

### Step 0 — 백업

```bash
cp .state.json .state.json.bak-$(date +%Y%m%d-%H%M%S)
```

### Step 1 — preflight

```bash
set -a && . ./.env && set +a
uv run python tests/test_broker_preflight.py --broker meritz --symbol TQQQ
```

> ⚠️ `tests/test_broker_preflight.py`의 `__main__`은 `.env`를 로드하지 않습니다.
> 위처럼 환경변수를 주입하지 않으면 **자격증명 없음으로 조용히 skip**됩니다.

### Step 2 — `.state.json` 이관

개발 머신의 `.state.json`을 운영 VPS로 복사(`scp`/`rsync`). **이후 모든 변경은 VPS에서만** 수행합니다.

### Step 3 — 주문 없이 상태 동기화 (핵심)

```bash
TRADE_MODE=DRY STATE_REVERSE_RECONCILE_ONLY=true uv run python trading_bot.py
```

이름과 달리 **일반 reconciliation 전체를 실행하고 저장**합니다
(`trading_bot.py:1233-1272`: `update_T_from_history` → `save_state`, DRY 강제, 전략/주문 없음).

2026-10-20 실측 출력:

```
[reconcile-only] TQQQ 저장 완료 → T=11.875, net_invested=$5615.60 (valid), state_hash=<H_TQQQ>
[reconcile-only] SOXL 저장 완료 → T=12.6562, net_invested=$5373.93 (valid), state_hash=<H_SOXL>
```

> 값은 **실행 시점 계좌 기준**으로 달라집니다. 항상 출력값을 그대로 사용하세요.

### Step 4 — 진단 검증 + 해시 확보

```bash
STATE_DIAGNOSTIC_ONLY=true uv run python trading_bot.py
```

확인: T=11.875 / 12.6562, net_invested=$5,615.60 / $5,373.93, status=`valid`,
`pending_*`=`null`, `reverse_mode={}` → **해시 2개 기록**

### Step 5 — `cycle_start_date` 설정 (CAS, 종목별 개별 실행)

```bash
STATE_SET_CYCLE_START_ONLY=true \
STATE_SET_CYCLE_START_SYMBOL=TQQQ \
STATE_SET_CYCLE_START_DATE=2026-09-23 \
STATE_SET_CYCLE_START_EXPECT_HASH=<H_TQQQ> \
STATE_SET_CYCLE_START_EXPECT_T=11.875 \
STATE_SET_CYCLE_START_EXPECT_CURRENT= \
uv run python trading_bot.py
```

SOXL: `_SYMBOL=SOXL`, `_DATE=2026-09-22`, `_EXPECT_T=12.6562`, `_EXPECT_HASH=<H_SOXL>`, `_EXPECT_CURRENT=`

- 보존: T, net_invested, watermark, `orders_meta`, `reverse_mode`, fence
- 거부: hash/T/현재값 불일치, 미종결 리버스 주문, pending fence, 날짜가 기준일(last_updated 날짜) 이후
- `REPAIR_ACTION=CYCLE_START_SET`으로도 실행 가능
- 이미 설정된 뒤 재실행 시 `_EXPECT_CURRENT`에 기존 날짜를 넣습니다

### Step 6 — 재검증

```bash
STATE_DIAGNOSTIC_ONLY=true uv run python trading_bot.py
```

확인: `cycle_start_date` 설정됨, T/net_invested 불변

### Step 7 — `.env` 시드 정정 후 DRY 프리뷰

`.env`에 `TQQQ_SEED=9696.18`, `SOXL_SEED=9627.46` 반영 후:

```bash
TRADE_MODE=DRY uv run python trading_bot.py
```

확인:
- `시드 적용: $9696.18` / `$9627.46`
- **SOXL "1회 매수금 부족" 매수 생략이 사라졌는가**
- 주문 목록/가격/수량이 의도와 맞는가

### Step 8 — 단일종목 소액 LIVE 1세션 (별도 승인)

`SYMBOLS=TQQQ:NAS`로 1세션 실행 → 접수/체결/이력/state 정합 확인 → SOXL 추가.

### Step 9 — cron + flock 활성화

- 실행 시각: **KST 17:30** (`docs/cron-dispatch-guide.md:38`; 프리장 오픈 직후, DST는 코드가 자동 처리)
- `flock -n`으로 중복 실행 방지
- run 전 state 백업 + 로그 append/회전

```cron
CRON_TZ=Asia/Seoul
30 17 * * 1-5 cd ~/investment/muhan-v4 && PATH=$HOME/.local/bin:/usr/bin:/bin flock -n /tmp/autotrade.lock $HOME/.local/bin/uv run python trading_bot.py >> run.log 2>&1
```

### 순서 근거 (Step 3 → Step 5)

- reconcile은 `last_updated` 기준 증분이라 `cycle_start_date`에 의존하지 않음 → 순서 무관
- **reconcile을 먼저** 두면 `cycle_start` CAS의 `EXPECT_T`가 "실제 운용할 T"와 일치해 실수 여지가 적음
- `cycle_start_date` 설정은 해시를 바꾸므로 **반드시 reconcile 이후 해시로 CAS**

### 검증 게이트

| 시점 | 통과 조건 | 실패 시 |
|---|---|---|
| Step 3 후 | T=11.875 / 12.6562, net_invested 일치, status=valid | 중단 → `STATE_REVERSE_AUDIT_ONLY`로 이력 대조 |
| Step 4 후 | `[불일치]` 없음, fence/리버스 없음 | LIVE 금지, 원인 조사 |
| Step 5 후 | `cycle_start_date` 설정, T 불변 | 해시 재획득 후 재실행 |
| Step 7 후 | SOXL 매수 생략 해소, 주문 목록 정상 | 진행 중단 |

### 실패 / 롤백

- 되돌리기: `.state.json.bak-*` 복원 (파일 단위라 즉시 롤백)
- `cycle_start_date` 설정은 비가역에 가까움(해시 변경) → **Step 0 백업 필수**
- Step 3의 T/net_invested는 **그 시점 계좌 기준 재계산**입니다. 확인 이후 손매매가 있었다면
  값이 달라지므로 **항상 출력값**을 CAS에 넣으세요.
- 손매매가 계속되면 Step 3을 다시 실행해 재동기화하고, **새 해시로 Step 5를 다시** 수행합니다
  (이미 설정된 경우 `EXPECT_CURRENT`에 그 값을 넣습니다).

---

## 3. 종료 4조건 체크리스트

| # | 조건 | 코드 검증 | 확인 방법 |
|---|---|---|---|
| 1 | 전량매도 **체결** | 간접 (체결 `ft_ccld_qty>0`만 집계, `trading_bot.py:95,101`) | 브로커 매도 이력 |
| 2 | **보유 0** | ✅ 종료 판정의 실제 근거 (`current_qty==0`, `trading_bot.py:561`) | `get_balance` |
| 3 | **미체결 0** | ❌ 미검증 | 브로커 미체결 주문 조회(수동) |
| 4 | **예약 0** | ❌ 미검증 | 예약주문 조회(수동) |

**핵심 리스크**: 잔고 0이어도 **미체결 BUY가 살아있으면** 봇은 종료로 판단해 새 사이클을 시작하고,
이후 그 주문이 체결되면 예상 밖 포지션이 생깁니다. 3·4번은 **자동화 불가 → 사람이 게이트**.

종료 판정 우선순위(`trading_bot.py:551-566`):
- `T>0 and current_qty==0` → 종료 처리
- `T>0 and current_qty==0 and live_qty is None` → **보류**(잔고 조회 실패, `556-559`)

---

## 4. 사이클 종료 전 사전 준비

### A. `cycle_start_date` 정정
§2 Step 5로 **이미 설정 완료**되어 있어야 합니다. 미설정이면 B안 시드가 틀어집니다.

### B. 시드 소스 확정
- 운영 VPS `.env`의 `TQQQ_SEED=9696.18` / `SOXL_SEED=9627.46` 및 `{SYMBOL}_SPLITS`(40/20) 확인.

### C. fence / 미체결 / 예약 정리
- `.state.json`의 `pending_order_intent` / `pending_order_batch`가 `null`인지 확인.
- 종료 예상일 전일에 브로커 미체결/예약 주문 0 확인.

### D. 메리츠 이력 후행 대비
- 종료 감지 RUN **다음 RUN**에서 `generate_cycle_report` 결과(총 매수/매도, `net_profit`)를 재확인.
- 이력이 늦게 정착하면 `net_profit`이 부분값일 수 있음 → **익일 재확인 필수**(§6).

---

## 5. 종료 시 절차 (T>0 & 보유 0 발생 시)

1. 4조건(§3)을 브로커에서 직접 확인 — 특히 미체결·예약 0.
2. `generate_cycle_report`의 `net_profit`을 **실계좌 확정 순손익**과 대조.
3. `next_cycle_seed = seed + net_profit` 값이 **B안**(기존 시드 + 확정 순손익)과 일치하는지 확인.
4. DRY 프리뷰로 먼저 확인:
   ```bash
   TRADE_MODE=DRY uv run python trading_bot.py
   ```
   ※ DRY는 **리포트만 표시하고 캐시 저장을 생략**(`trading_bot.py:586-596`) →
   **시드 확정은 LIVE RUN에서만** 이루어짐.
5. LIVE RUN에서 종료 처리 → `effective_seed` 저장, `T=0`, `cycle_start_date=""`, `net_invested=0`
   (`trading_bot.py:600-620`). **같은 RUN에서 새 사이클 즉시 시작**(`620-622`).

---

## 6. 익일 반영 확인 체크리스트

- [ ] `effective_seed`가 의도한 B안 값으로 저장됐는가
- [ ] 새 사이클 첫 주문이 정상 접수됐는가
- [ ] `T`/`net_invested`/`cycle_start_date` 리셋이 정상인가
- [ ] 리포트 `net_profit` == 실계좌 확정 순손익 (메리츠 이력 후행 반영 후 재확인)
- [ ] 미체결/예약 주문 0 유지
- [ ] 텔레그램 사이클 리포트 수신 확인

---

## 7. 리스크 / 미확정

| 항목 | 상태 |
|---|---|
| 로컬 `.state.json` stale (10/14) | ✅ **해결** — 2026-10-20 VPS reconcile-only 완료 |
| `cycle_start_date=""` | ✅ **해결** — TQQQ 2026-09-23 / SOXL 2026-09-22 |
| 운영 시드 소스 | ✅ VPS `.env`에 9,696.18 / 9,627.46 설정 완료 |
| 손매매 지속 시 state 재-stale | ⚠️ 컷오버(수동 주문 중단) 후 §2 Step 3 재실행 |
| 수동매수 T 폴백 과소계산 | ⚠️ `src/state.py:1419` (`1건 → 0.5`). TQQQ는 12.375로 보정, **SOXL 재검토 필요** |
| 메리츠 이력 ~1일 후행 | ⚠️ `net_profit` 불완전 가능 → 익일 재확인 |
| 미체결/예약 조건 | ❌ 코드 미검증 → 수동 게이트 |
| 텔레그램 | ⚠️ 운영 VPS에도 미설정 → 알림 없음 |
| preflight `.env` 미로드 | ⚠️ `set -a && . ./.env`로 주입 필요 |

---

## 8. 관련 도구

```bash
# 캐시/state 상태만 출력 (API/전략/주문 없음)
STATE_DIAGNOSTIC_ONLY=true uv run python trading_bot.py

# 주문 없이 미반영 체결을 state에 반영·저장 (일반 reconciliation 포함)
TRADE_MODE=DRY STATE_REVERSE_RECONCILE_ONLY=true uv run python trading_bot.py

# 브로커 주문이력 vs state orders_meta 대조 (read-only)
STATE_REVERSE_AUDIT_ONLY=true uv run python trading_bot.py

# cycle_start_date CAS 설정
STATE_SET_CYCLE_START_ONLY=true STATE_SET_CYCLE_START_SYMBOL=<SYM> \
STATE_SET_CYCLE_START_DATE=<YYYY-MM-DD> STATE_SET_CYCLE_START_EXPECT_HASH=<hash> \
STATE_SET_CYCLE_START_EXPECT_T=<T> STATE_SET_CYCLE_START_EXPECT_CURRENT= \
uv run python trading_bot.py

# 전략 what-if (API/상태 미접촉)
uv run python tests/test_dryrun.py
```
