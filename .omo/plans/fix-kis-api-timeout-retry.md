# fix-kis-api-timeout-retry - Work Plan

## TL;DR (For humans)

**What you'll get:** GitHub Actions에서 한국투자증권 API 연결 타임아웃으로 토큰 발급이 실패하는 문제를 해결합니다. tokenP 요청의 타임아웃을 30초로 늘리고 재시도를 20회까지 확장해(최대 약 10분간 재시도) Azure의 간헐적 네트워크 문제를 극복합니다. Real+LIVE 모드에서는 프리장 오픈 전이면 KST 기준으로 대기 시간을 계산해 정확히 프리장 오픈 시점에 주문이 실행되도록 합니다. dispatch는 1회만 하므로 중복 주문 위험이 없습니다.

**Why this approach:** API가 완전 차단된 게 아니라 Azure에서 간헐적으로 타임아웃이 발생하는 문제입니다. 한 세션 안에서 20회 재시도(~10분)로 대부분 성공하고, 설령 실패해도 다음 날 자동 재시도됩니다. Real 모드의 프리장 오픈 전 실행 문제는 datetime.now()가 아닌 KST 타임존 기준으로 sleep 시간을 계산해 정확히 제어합니다.

**What it will NOT do:** 토큰 파일 캐시를 도입하지 않습니다. 별도 token-refresh 워크플로우를 만들지 않습니다. dispatch를 2회 하지 않습니다(중복 주문 방지). GitHub Actions schedule을 사용하지 않습니다. Demo 모드의 흐름은 변경하지 않습니다.

**Effort:** Short — 3개 파일 수정
**Risk:** Low — 타임아웃/재시도 숫자 변경 + 프리장 오픈 전 sleep, 기능 변화 없음
**Decisions to sanity-check:** 재시도 20회가 충분한지, 프리장 오픈 전 sleep 로직의 시간 계산이 KST 기준으로 정확한지

Your next move: approve, then execute the plan.

---

> TL;DR (machine): Short effort, Low risk. Modify authentication.py retry constants (3→20, cap 30→60), add KIS_CONNECT_TIMEOUT=30 to trade_base.yml env, add pre-market open wait logic in trading_bot.py using KST timezone. External cron: 1 dispatch per environment.

## Scope
### Must have
- authentication.py: `MAX_RETRIES` = 3 → 20 (network_retry_count 한도), 백오프 최대 30s → 60s
- trade_base.yml: `KIS_CONNECT_TIMEOUT: 30` 환경변수 추가
- trading_bot.py: Real+LIVE 모드에서 프리장 오픈 전 KST 기준 sleep 로직 추가
- 외부 cron: Demo KST 04:10 / Real KST 17:30, 각 1회 dispatch

### Must NOT have (guardrails, anti-slop, scope boundaries)
- ❌ dispatch 2회 금지 — 중복 주문 방지
- ❌ 토큰 파일 캐시(.token_cache.json) 도입하지 않음
- ❌ 별도 token-refresh 워크플로우 생성하지 않음
- ❌ GitHub Actions `schedule` 내장 cron 사용하지 않음
- ❌ repository_dispatch + PAT 기반 자체재시도 도입하지 않음
- ❌ Demo 모드 흐름 변경 (정규장 내 실행, sleep 불필요)
- ❌ rate-limit retry(EGW00201/00215)는 건드리지 않음 (3회 유지)
- ❌ config.py 기본값 변경하지 않음 (env var로 오버라이드)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after
- Evidence: .omo/evidence/task-{N}-fix-kis-api-timeout-retry.{ext}

## Execution strategy
### Parallel execution waves
Wave 1: authentication.py + trade_base.yml (병렬 가능)
Wave 2: trading_bot.py (Wave 1과는 독립적)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. authentication.py | 없음 | — | 2, 3 |
| 2. trade_base.yml | 없음 | — | 1, 3 |
| 3. trading_bot.py | 없음 | 4 | 1, 2 |
| 4. 외부 cron 설정 | 1, 2, 3 완료 | — | — |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [x] 1. authentication.py — 타임아웃 재시도 횟수·백오프 상향
  What to do / Must NOT do:
  - **network_retry_count의 상한만 변경: 3 → 20** (기존 `<= MAX_RETRIES`와 혼동 주의: MAX_RETRIES=3은 EGW00133용)
  - 백오프 공식: `min(30, 2 ** network_retry_count)` → `min(60, 2 ** network_retry_count)`
  - EGW00133(토큰 발급 과다) retry와 rate_limit(EGW00201/00215) retry는 **건드리지 않음** (각각 3회 유지)
  - jitter multiplier `random.uniform(0.75, 1.25)`는 유지
  - 타임아웃 값 자체는 env var로 오버라이드하므로 config.py 변경 불필요
  - **반드시 확인**: network_retry_count(77번째 줄), retry_count(75번째 줄), rate_limit_retry_count(77번째 줄) 3개 변수가 독립적으로 동작하는지 확인
  Parallelization: Wave 1 | Blocked by: 없음 | Blocks: 없음
  References:
  - src/authentication.py:74-77 (상수 선언부 - MAX_RETRIES=3, retry_count=0, network_retry_count=0, rate_limit_retry_count=0)
  - src/authentication.py:121-128 (네트워크 타임아웃 retry 루프)
  - src/authentication.py:124 (백오프 공식: `min(30, 2 ** network_retry_count)`)
  - src/authentication.py:92-114 (EGW00133 / rate_limit retry — 건드리지 말 것)
  - src/config.py:30-32 (KIS_CONNECT_TIMEOUT/KIS_TIMEOUT — env var 오버라이드)
  Acceptance criteria (agent-executable):
  ```bash
  grep -n 'network_retry_count\|min(30' src/authentication.py
  ```
  - `network_retry_count <= MAX_RETRIES` 부분 확인 (MAX_RETRIES는 여전히 3이므로 `network_retry_count <= 20`이 아니라 `network_retry_count <= MAX_RETRIES` 형태를 유지하는지 주의 → `MAX_RETRIES` 값을 20으로 올리면 EGW00133 retry도 20이 되므로 안 됨)
  - **정확한 변경:** `if network_retry_count <= MAX_RETRIES:` 이 부분은 유지하되, `MAX_RETRIES`가 아니라 `network_retry_count`의 비교 대상을 네트워크 전용 상수로 분리하거나 `MAX_RETRIES` 변수명 자체를 그대로 사용하되 모든 retry가 공유하는 구조이므로... 

  ⚠️ **중요: 코드 구조 파악**
  현재 코드:
  ```python
  MAX_RETRIES = 3        # 74: 전역 상수
  retry_count = 0        # 75: EGW00133 전용
  network_retry_count=0  # 76: Timeout 전용
  rate_limit_retry_count=0  # 77: Rate-limit 전용
  ```
  121-128번째 줄:
  ```python
  except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
      network_retry_count += 1
      if network_retry_count <= MAX_RETRIES:    # <= 3
  ```
  `MAX_RETRIES`는 3이고, network_retry_count도 이걸 공유. 즉 MAX_RETRIES를 20으로 올리면 모든 retry(EGW00133, rate_limit 포함)가 20이 되어버림.
  
  **해결: 네트워크 전용 상수 `NETWORK_MAX_RETRIES = 20`을 별도로 선언하고 network_retry_count의 비교에만 사용할 것. `MAX_RETRIES`(=3)는 그대로 유지.**
  
  **수정사항:**
  1. `MAX_RETRIES = 3` 다음 줄에 `NETWORK_MAX_RETRIES = 20` 추가
  2. `network_retry_count <= MAX_RETRIES` → `network_retry_count <= NETWORK_MAX_RETRIES`
  3. `min(30, 2 ** network_retry_count)` → `min(60, 2 ** network_retry_count)`
  Parallelization: Wave 1 | Blocked by: 없음 | Blocks: 없음
  References: src/authentication.py:74-128
  Acceptance criteria:
  ```bash
  grep -n 'NETWORK_MAX_RETRIES\|min(60\|MAX_RETRIES' src/authentication.py
  ```
  - `NETWORK_MAX_RETRIES = 20`이 있고
  - `network_retry_count <= NETWORK_MAX_RETRIES`로 비교하며
  - `min(60, 2 **`이 있고
  - `MAX_RETRIES = 3`은 그대로이며 다른 retry(count, rate_limit_retry_count)는 여전히 `MAX_RETRIES`와 비교
  - `MAX_RETRIES`를 건드리지 않았음을 `git diff`로 확인
  QA scenarios:
  - happy: grep으로 상수 확인
  - failure: `git diff src/authentication.py`로 MAX_RETRIES가 변경되지 않았는지, network_retry_count만 변경되었는지 확인
  Evidence: .omo/evidence/task-1-fix-kis-api-timeout-retry.txt
  Commit: Y | `fix(authentication): tokenP 네트워크 타임아웃 재시도 전용 상수 추가 (3→20회), 백오프 30s→60s`

- [x] 2. trade_base.yml — KIS_CONNECT_TIMEOUT 환경변수 추가
  What to do / Must NOT do:
  - `.github/workflows/trade_base.yml` `자동매매 봇 실행` step의 env 블록에 `KIS_CONNECT_TIMEOUT: 30` 추가
  - 기존 env 변수 순서 유지, `PYTHONUNBUFFERED: 1` 위 또는 아래에 배치
  - `KIS_READ_TIMEOUT`이나 `KIS_TIMEOUT`은 추가하지 않음 (변경 불필요)
  Parallelization: Wave 1 | Blocked by: 없음 | Blocks: 없음
  References: .github/workflows/trade_base.yml:78-109 (전체 env 블록), src/config.py:30 (기본값 10)
  Acceptance criteria:
  ```bash
  grep 'KIS_CONNECT_TIMEOUT' .github/workflows/trade_base.yml
  ```
  → `KIS_CONNECT_TIMEOUT: 30` 출력 확인
  QA scenarios:
  - happy: grep 출력
  - failure: `git diff`로 다른 env var 변경 없는지 확인
  Evidence: .omo/evidence/task-2-fix-kis-api-timeout-retry.txt
  Commit: Y | `fix(ci): tokenP 연결 타임아웃 10s→30s로 상향`

- [x] 3. trading_bot.py — Real+LIVE 프리장 오픈 전 KST 기준 sleep 로직 추가
  What to do / Must NOT do:
  - `trading_bot.py`의 `run_one_symbol()` 함수 내, **주문 실행 루프 직전**(`for i, order in enumerate(orders, 1):` 바로 앞)에 sleep 로직 삽입
  - 조건: `KIS_MODE == "real" and TRADE_MODE == "LIVE"`일 때만 작동
  - **KST 타임존 기준으로 현재 시각 계산:** `trader._get_kst_now()` 사용 (내부적으로 `ZoneInfo("Asia/Seoul")` 사용)
  - **DST 판단:** `trader._is_us_dst()` 사용
  - **프리장 오픈 시간:**
    - DST 적용 시: KST 17:00
    - DST 미적용 시: KST 18:00
  - **sleep 계산:**
    ```python
    if KIS_MODE == "real" and TRADE_MODE == "LIVE":
        now_kst = _get_kst_now()
        is_dst = _is_us_dst()
        pre_market_open_hour = 17 if is_dst else 18
        if now_kst.hour < pre_market_open_hour:
            target = now_kst.replace(hour=pre_market_open_hour, minute=0, second=0, microsecond=0)
            wait_seconds = (target - now_kst).total_seconds()
            print(f"⏳ 프리장 오픈 대기 중... (KST {now_kst.strftime('%H:%M:%S')} → {target.strftime('%H:%M:%S')}, 약 {wait_seconds/60:.0f}분)")
            time.sleep(wait_seconds)
    ```
  - **반드시 확인:** `_get_kst_now()`는 `ZoneInfo("Asia/Seoul")`을 사용해 **러너의 로컬 시간(UTC)과 무관하게 항상 KST 반환**
  - **sleep 후에도** orders 루프는 정상 진행 (별도 추가 조건 불필요)
  - `trading_bot.py` 상단 import에 `from config import KIS_MODE` 추가
  - `from trader import _get_kst_now, _is_us_dst` 추가 (이미 `from trader import ...` 줄이 있으므로 거기에 추가)
  - **Demo 모드는 변경하지 않음** (정규장 내 04:30 실행이므로 sleep 불필요)
  - `time` 모듈은 이미 import되어 있음 (`import time`은 없지만... 확인 필요. `trading_bot.py`에는 `import time`이 없음 → 추가 필요)
  Parallelization: Wave 2 | Blocked by: 없음 | Blocks: 4
  References:
  - trading_bot.py:1-31 (imports)
  - trading_bot.py:476-481 (주문 실행 루프 직전 위치 — `print("\n[Step 4] 주문 실행 중...")` 바로 다음)
  - trader.py:288-292 (`_get_kst_now()` - KST 타임존 반환)
  - trader.py:293-296 (`_is_us_dst()` - 미국 DST 판단)
  - config.py:17-21 (KIS_MODE 상수)
  Acceptance criteria:
  - 코드 리뷰로 sleep 조건과 시간 계산 검증
  ```bash
  grep -n '_get_kst_now\|_is_us_dst\|pre_market_open_hour\|KIS_MODE.*real.*LIVE\|import.*time\|import.*KIS_MODE' trading_bot.py
  ```
  - `_get_kst_now()`, `_is_us_dst()` 호출 코드 확인
  - `pre_market_open_hour` 로직 확인 (DST=17, non-DST=18)
  - `KIS_MODE == "real" and TRADE_MODE == "LIVE"` 조건 확인
  - `import time` 존재 확인
  - `from config import KIS_MODE` 존재 확인
  QA scenarios:
  - happy: grep 출력으로 조건, import, 시간 계산 모두 확인
  - failure: `git diff trading_bot.py`로 불필요한 변경 없는지 확인
  Evidence: .omo/evidence/task-3-fix-kis-api-timeout-retry.txt
  Commit: Y | `fix(trading): Real+LIVE 프리장 오픈 전 KST 기준 sleep 로직 추가`

- [x] 4. 외부 cron 서비스 dispatch 시간 가이드
  What to do / Must NOT do:
  - 이 태스크는 **파일 수정이 아닌 문서 작성.** `.omo/evidence/`에 가이드 기록
  - **dispatch 1회만 설정** (중복 주문 방지)
  - 권장 dispatch 시간:
    ```
    Demo (trigger_trade_demo):
      KST 04:10 (UTC 19:10)
      → tokenP 재시도(최대 ~10분) → 04:20~04:25경 주문 실행
      → 정규장 내 (22:30~06:00 KST)이므로 바로 진행
      → 장 마감(05:00) 전 충분한 여유

    Real (trigger_trade_real):
      KST 17:30 (UTC 08:30)
      → tokenP 재시도(최대 ~10분) → 17:40~17:45경 tokenP 성공 후 전략 실행
      → DST: 프리장 17:00 오픈 → 바로 진행 ✅
      → non-DST: 프리장 18:00 오픈 → 18:00까지 KST 기준 sleep → 진행 ✅
    ```
  - DISPATCH_TYPE 매핑:
    - `trigger_trade_demo` → `.github/workflows/trade_demo.yml`
    - `trigger_trade_real` → `.github/workflows/trade_real.yml`
  Parallelization: Wave 2 | Blocked by: 1, 2, 3 | Blocks: 없음
  References:
  - .github/workflows/trade_demo.yml:4-5 (repository_dispatch types)
  - .github/workflows/trade_real.yml:4-5
  Acceptance criteria:
  - `.omo/evidence/task-4-cron-dispatch-guide.md` 파일 존재
  - dispatch 1회/환경, 중복 없음
  QA scenarios:
  - 가이드 내용과 실제 워크플로우 트리거 타입 일치 확인
  Evidence: .omo/evidence/task-4-cron-dispatch-guide.md
  Commit: N (운영 설정 문서)

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. **authentication.py 상수 검증**: `NETWORK_MAX_RETRIES=20` + `MAX_RETRIES=3` 유지 확인, rate-limit/EGW00133 retry 변경 없음 확인
- [x] F2. **trade_base.yml env 검증**: `KIS_CONNECT_TIMEOUT: 30` 추가 확인, 다른 env var 변경 없음 확인
- [x] F3. **trading_bot.py sleep 로직 검증**: KST 타임존 사용, DST 조건 분기, sleep 전 print 메시지, import 추가 확인
- [x] F4. **외부 cron 가이드 검증**: 1회 dispatch, 타입 일치, 중복 금지 문구 포함 확인
- [x] F5. **전체 git diff 검토**: 의도한 3개 파일만 변경되었는지 확인

## Commit strategy
1. `fix(authentication): tokenP 네트워크 타임아웃 재시도 전용 상수 추가 (3→20회), 백오프 30s→60s`
2. `fix(ci): tokenP 연결 타임아웃 10s→30s로 상향`
3. `fix(trading): Real+LIVE 프리장 오픈 전 KST 기준 sleep 로직 추가`

## Success criteria
- GitHub Actions에서 tokenP 타임아웃으로 인한 거래 누락 0건
- 1회 세션(20회 재시도, ~10분) 내 토큰 발급 성공률 ≥ 99%
- Real+LIVE: 프리장 오픈 전에 주문이 실행되어 API 거부되는 경우 0건
- 중복 주문 0건
