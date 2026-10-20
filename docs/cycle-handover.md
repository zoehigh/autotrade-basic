# 손매매 계좌 인계 → 자동매매 시딩 → 사이클 종료(B안) 런북

작성: 2026-10-15 · 갱신: 2026-10-19
대상: TQQQ, SOXL (메리츠 실전)
코드 근거: `trading_bot.py`, `src/state.py`, `src/strategy.py`

## 전제 (2026-10-19 확정)

- **이 코드는 메리츠 실전에 적용된 적이 없습니다.** 계좌는 사용자가 **손으로 직접 매매**해 왔고,
  코드는 preflight / DRY / 명시적 보정으로만 사용됐습니다.
- **운영 호스트: 별도 전용 VPS + `cron` + `flock`.** GH Actions는 사용하지 않습니다
  (외부 cron dispatch도 꺼져 있음).
- **개발 머신은 preflight / DRY / 보정 전용 — 실주문 금지.**
- 운영 상태는 **운영 VPS의 `.state.json`** 이 권위입니다. (GH Actions 캐시 아님)

---

## 0. 현재 상태 (2026-10-19 read-only 실측)

`.state.json`은 **10/14 스냅샷으로 stale**이며, 10/15·10/16 손매매 체결이 미반영입니다.
아래 "실제" 값은 메리츠 실계좌를 read-only로 조회한 reconciliation 결과입니다.

| 항목 | 로컬 state (10/14) | **실제 (2026-10-19)** |
|---|---|---|
| TQQQ T | 9.375 | **11.375** |
| TQQQ net_invested | $4,126.91 | **$5,121.12** |
| SOXL T | 9.65625 | **11.65625** |
| SOXL net_invested | $3,996.61 | **$4,880.68** |
| net_invested_status | valid | valid |
| **effective_seed** | **0.0** | **0.0** (→ config `{SYMBOL}_SEED` 폴백) |
| **cycle_start_date** | **`""` ⚠️** | **`""` ⚠️** (→ §2에서 설정) |
| watermark(last_updated) | 2026-10-14T08:09:04Z / 08:05:55Z | 10/16 체결로 전진 예정 |
| last_processed_ordno | `20261014:0001650552` / `20261014:0001647177` | 10/16 최신 체결로 전진 예정 |
| 보유 | — | TQQQ 67주 @ $77.0777 / SOXL 39주 @ $128.091 |
| 최근가 | — | TQQQ $68.21 / SOXL $93.67 |
| 확정 시드(운영) | — | **$9,696.18 / $9,627.46** |

**미반영 체결**: 10/15 · 10/16 (TQQQ 3+2건, SOXL 3+3건).

**검증 로그 요약 (방법 A, `TRADE_MODE=DRY` + 메리츠 실계좌 read-only)**
- exit 0, `.state.json` sha256 **불변** → DRY는 상태를 저장하지 않음 확인
- `[불일치]` 경고 **없음** → 이력 기반 포지션과 브로커 잔고 일치 (상태 무결성 양호)
- 문제는 **최신성(stale)** 과 **시드 값**뿐

**미추적 백업**: `.state.json.bak-20261013`, `.state.json.bak-20261013-effix`
(10/13 백업의 `effective_seed`는 TQQQ 10,231.81 / SOXL 11,792.01 → 이후 0으로 초기화됨)

---

## 1. 확인 완료 사항

### (a) 시드 소스

- `effective_seed = 0.0` → 시드는 **config `{SYMBOL}_SEED`로 폴백**
  (`trading_bot.py:365-367`, `571-573`, `635-639`).
- **운영 VPS `.env`에 확정 시드를 설정해야 합니다**: `TQQQ_SEED=9696.18`, `SOXL_SEED=9627.46`.
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

### (d) `cycle_start_date` — BLOCKER, 수단 구현됨

- 증상: `generate_cycle_report`가 `cycle_start_date`가 비면 **전체 이력**을 집계
  (`trading_bot.py:86-102`) → `net_profit`이 전 기간 누적이 되어 **B안 시드가 틀어짐**.
- 수단: `STATE_SET_CYCLE_START_ONLY` (CAS repair, `_set_cycle_start_only`) — 구현·테스트 완료
  (`tests/test_cycle_start_repair.py`, 11건).
- 설정 순서는 **§2 Step 5** (reconcile 이후 해시로 CAS).

### (e) 기타

- **텔레그램**: 로컬 `.env`에 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 없음 → 알림 미발송.
  운영 VPS에는 설정 필요.
- **×1.19 BUY 캡 재현**: 별지점 매수 `$82.05 → $81.16` (`현재가 $68.21 × 1.19`).
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
uv run python tests/test_broker_preflight.py --broker meritz --symbol TQQQ
```

### Step 2 — `.state.json` 이관

개발 머신의 `.state.json`(10/14 스냅샷)을 운영 VPS로 복사(`scp`/`rsync`).
**이후 모든 변경은 VPS에서만** 수행합니다.

### Step 3 — 주문 없이 상태 동기화 (핵심)

```bash
TRADE_MODE=DRY STATE_REVERSE_RECONCILE_ONLY=true uv run python trading_bot.py
```

이름과 달리 **일반 reconciliation 전체를 실행하고 저장**합니다
(`trading_bot.py:1233-1272`: `update_T_from_history` → `save_state`, DRY 강제, 전략/주문 없음).

기대 출력:

```
[reconcile-only] TQQQ 저장 완료 → T=11.375, net_invested=$5121.12 (valid), state_hash=<H_TQQQ>
[reconcile-only] SOXL 저장 완료 → T=11.65625, net_invested=$4880.68 (valid), state_hash=<H_SOXL>
```

### Step 4 — 진단 검증 + 해시 확보

```bash
STATE_DIAGNOSTIC_ONLY=true uv run python trading_bot.py
```

확인: T=11.375 / 11.65625, net_invested=$5,121.12 / $4,880.68, status=`valid`,
`cycle_start_date=""`, `pending_*`=`null`, `reverse_mode={}` → **해시 2개 기록**

### Step 5 — `cycle_start_date` 설정 (CAS, 종목별 개별 실행)

```bash
STATE_SET_CYCLE_START_ONLY=true \
STATE_SET_CYCLE_START_SYMBOL=TQQQ \
STATE_SET_CYCLE_START_DATE=2026-09-23 \
STATE_SET_CYCLE_START_EXPECT_HASH=<H_TQQQ> \
STATE_SET_CYCLE_START_EXPECT_T=11.375 \
STATE_SET_CYCLE_START_EXPECT_CURRENT= \
uv run python trading_bot.py
```

SOXL: `_SYMBOL=SOXL`, `_DATE=2026-09-22`, `_EXPECT_T=11.65625`, `_EXPECT_HASH=<H_SOXL>`, `_EXPECT_CURRENT=`

- 보존: T, net_invested, watermark, `orders_meta`, `reverse_mode`, fence
- 거부: hash/T/현재값 불일치, 미종결 리버스 주문, pending fence, 날짜가 기준일(last_updated 날짜) 이후
- `REPAIR_ACTION=CYCLE_START_SET`으로도 실행 가능

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

- 실행 시각: **KST 17:30** (프리장 대기는 코드가 자동 처리, `trading_bot.py:713-722`; DST 무관)
- `flock -n`으로 중복 실행 방지 (`docs/shadow.md` 패턴 참고)
- run 전 state 백업 + 로그 append/회전

### 순서 근거 (Step 3 → Step 5)

- reconcile은 `last_updated` 기준 증분이라 `cycle_start_date`에 의존하지 않음 → 순서 무관
- **reconcile을 먼저** 두면 `cycle_start` CAS의 `EXPECT_T`가 "실제 운용할 T"와 일치해 실수 여지가 적음
- `cycle_start_date` 설정은 해시를 바꾸므로 **반드시 reconcile 이후 해시로 CAS**

### 검증 게이트

| 시점 | 통과 조건 | 실패 시 |
|---|---|---|
| Step 3 후 | T=11.375 / 11.65625, net_invested 일치, status=valid | 중단 → `STATE_REVERSE_AUDIT_ONLY`로 이력 대조 |
| Step 4 후 | `[불일치]` 없음, fence/리버스 없음 | LIVE 금지, 원인 조사 |
| Step 5 후 | `cycle_start_date` 설정, T 불변 | 해시 재획득 후 재실행 |
| Step 7 후 | SOXL 매수 생략 해소, 주문 목록 정상 | 진행 중단 |

### 실패 / 롤백

- 되돌리기: `.state.json.bak-*` 복원 (파일 단위라 즉시 롤백)
- `cycle_start_date` 설정은 비가역에 가까움(해시 변경) → **Step 0 백업 필수**
- Step 3의 T/net_invested는 **그 시점 계좌 기준 재계산**입니다. 확인 이후 손매매가 있었다면
  값이 달라지므로 **항상 출력값**을 CAS에 넣으세요.

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
| 로컬 `.state.json` stale (10/14) | 🔴 **해결 절차 확정** — §2 Step 3 reconcile-only |
| `cycle_start_date=""` | 🟠 **수단 구현됨** — §2 Step 5에서 설정 |
| 운영 시드 소스 | 🟠 VPS `.env`에 확정값 설정 필요 (미설정 시 SOXL 매수 생략) |
| 메리츠 이력 ~1일 후행 | ⚠️ `net_profit` 불완전 가능 → 익일 재확인 |
| 미체결/예약 조건 | ❌ 코드 미검증 → 수동 게이트 |
| 리비전 고정 | ⚠️ `trading_bot.py`, 본 문서, `tests/test_cycle_start_repair.py` **미커밋** → 운영 배포 전 고정 필요 |
| 텔레그램 | ⚠️ 로컬 미설정, 운영 VPS 설정 필요 |

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
