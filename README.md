# autotrade-basic — 자동매매 봇

KIS(한국투자증권) Open API를 사용한 미국 주식 자동매매 학습용 프로젝트입니다.  
**무한매수법 V4** 전략을 기반으로 동작하며, GitHub Actions에서 `repository_dispatch`로 트리거됩니다.

---

## 프로젝트 구조

```
trading_bot.py        # 전체 파이프라인 실행 (state → 전략 → 주문실행 → 저장)
tests/
  test_dryrun.py      # 전략 미리보기 CLI (상태/주문 비접촉, what-if 검증용)
  test_state_t_updates.py  # T값 업데이트 유닛 테스트
  ...
src/
  strategy.py         # 무한매수법 V4 전략 로직
  trader.py           # KIS API 호출 래퍼 (주문 · 예약주문 · 잔고 · 시세)
  state.py            # 상태 관리 (state.json 로드/저장, T값 추적)
  config.py           # 환경변수 로드
  authentication.py   # 액세스 토큰 발급
  notifier.py         # 알림 헬퍼
  telegram.py         # 텔레그램 전송
.github/workflows/
  trade_base.yml           # 재사용 가능한 공통 실행 베이스
  trade_kis_demo.yml       # KIS 모의투자
  trade_kis_real.yml       # KIS 실전
  trade_kiwoom_demo.yml    # 키움 모의투자
  trade_kiwoom_real.yml    # 키움 실전 (self-hosted runner)
  trade_ls_demo.yml        # LS 모의투자
  trade_ls_real.yml        # LS 실전
  trade_toss_real.yml      # 토스 실전 (real만)
  trade_meritz_real.yml    # 메리츠 실전 (real만, 10/12 신청 개방 확인 전)
.state.json           # 상태 파일 (자동 생성, .gitignore 권장)
```

---

## 전략 개요 — 무한매수법 V4

V4는 **T값(누적 매수 횟수)** 기반으로 매수/매도가 자동 조정되는 전략입니다.

### 핵심 개념

| 용어 | 설명 |
|------|------|
| **분할 (splits)** | 전체 시드를 나눌 횟수 (20 또는 40) |
| **T값** | 현재까지 소진한 분할 횟수 (매수 시 증가, 매도 시 감소) |
| **1회 매수금 (unit_amount)** | `남은자금 / (splits - T)` |
| **별지점 (star point)** | `평단 × (1 + 별%)` — T가 높을수록 평단에 가까워짐 |
| **추가매수 LOC** | 급락 대비 1주씩 N단계 LOC (T 변화 없음) |

### 별지점 공식

| 종목 | 20분할 | 40분할 |
|------|--------|--------|
| TQQQ | 별% = (15 - 1.5×T)% | 별% = (15 - 0.75×T)% |
| SOXL | 별% = (20 - 2×T)% | 별% = (20 - T)% |

### 매수 전략

- **전반전** (T < splits/2): 절반은 별지점 LOC, 절반은 평단 LOC — 각각 체결 시 T += 0.5
- **후반전** (T >= splits/2): 전액 별지점 LOC에 집중 — 체결 시 T += 1.0
- **추가매수 LOC**: 라오어 공식 `unit_amount / (기준수량 + i)`로 N단계 가격 설정, 1주씩 LOC

### 매도 전략 (항상 두 개를 동시 제출)

| 주문 | 수량 | 가격 | 유형 | 체결 시 T |
|------|------|------|------|----------|
| **쿼터매도** | 보유량의 ≈25% | 별지점가 | LOC | T × 0.75 |
| **목표매도** | 잔량 | 익절가 (TQQQ: 평단×1.15, SOXL: 평단×1.20) | LIMIT | T × 0.25 |

### 초기 진입 (T=0, 포지션 없음)

- 현재가 기준 별% 위 가격에 LOC 1회 매수 (t_target=1.0)
- 추가매수 LOC N단계 동시 제출

### 사이클 종료

- 매도 체결로 보유수량 = 0 → `T = 0` 초기화 → 새 사이클 자동 시작
- 종료 시 `generate_cycle_report()`로 수익률 리포트 생성

### 소진

- **일반모드**: `T >= splits` 시 모든 분할을 소진한 것으로 판단, 더 이상 주문을 생성하지 않음
- **리버스모드**: 쿼터매수(잔금의 1/4)로 1개 매수가 불가능해지는 시점에서 소진으로 판단
  - T값이 19나 40을 초과하더라도, 쿼터매수로 1개 매수가 가능하면 별지점 아래에서 매수 지속
  - 별지점 위에서는 기존대로 매도(LOC) 시행
  - 1개 매수가 불가능해지면 매수 시도 없이 MOC 매도만 시행

---

## 실행 방법

### 1. 전략 미리보기 — `tests/test_dryrun.py`

**상태나 API 주문을 전혀 건드리지 않고** `무한매수법_V4()` 전략의 결과만 출력합니다.
파라미터를 조정해 what-if 시나리오를 검증할 때 사용합니다.

```bash
# 기본 설정 (SYMBOLS 첫 번째 종목, 실제 API 잔고 기반)
uv run python tests/test_dryrun.py

# What-if: T=5, 시드 $10,000, 20분할로 테스트
TEST_T=5 TEST_SEED=10000 TEST_SPLITS=20 uv run python tests/test_dryrun.py
```

### 2. 전체 파이프라인 실행 — `trading_bot.py`

실제 운영 파이프라인을 실행합니다:

```bash
# DRY 모드: 주문을 생성만 하고 실행하지 않음
TRADE_MODE=DRY uv run python trading_bot.py

# LIVE 모드: 실제 주문 실행
TRADE_MODE=LIVE uv run python trading_bot.py
```

### 두 실행 방식 비교

| 항목 | `tests/test_dryrun.py` | `trading_bot.py` |
|------|------------------------|------------------|
| 상태 로드/저장 (`state.json`) | ❌ | ✅ |
| 체결 이력 반영 (T 갱신) | ❌ | ✅ |
| 잔고 교차검증 | ❌ | ✅ |
| 사이클 종료 감지 | ❌ | ✅ |
| 실제 주문 실행 | ❌ | ✅ (LIVE) / 출력만 (DRY) |
| Telegram 알림 | ❌ | ✅ |
| 대상 종목 | 1개 (TEST_SYMBOL 또는 SYMBOLS[0]) | SYMBOLS 전체 |
| 목적 | **전략 what-if 검증** | **운영 자동매매** |

### 3. 테스트 — `pytest`

브로커 실 API 통합 테스트는 pytest 마커(`kis`/`kiwoom`/`ls`/`toss`/`nhplug`/`meritz`)로 구분됩니다.
`tests/conftest.py`가 `.env`의 자격증명 유무만으로 실행 여부를 판단하므로,
**BROKER 값과 무관하게** 해당 브로커 키가 설정돼 있으면 실행되고 없으면 자동 skip됩니다.
마커 없는 유닛/모의 테스트는 항상 실행됩니다.

```bash
uv run pytest tests/ -v                          # 키 설정된 브로커 통합 테스트 + 유닛
uv run pytest tests/ -m kis -v                   # 특정 브로커만 (kis/kiwoom/ls/toss/nhplug/meritz)
uv run pytest tests/test_kiwoom_integration.py -v  # 특정 파일도 자격증명 기준으로 동작
```

참고 — 브로커별 필수 자격증명:

| 마커 | 필수 환경변수 |
|------|--------------|
| `kis` | `KIS_APP_KEY` + `KIS_APP_SECRET` + `KIS_ACCOUNT_NO` |
| `kiwoom` | `KIWOOM_APP_KEY` + `KIWOOM_APP_SECRET` |
| `ls` | `LS_APP_KEY` + `LS_APP_SECRET` |
| `toss` | `TOSS_APP_KEY` + `TOSS_APP_SECRET` |
| `nhplug` | `NHPLUG_APP_KEY` + `NHPLUG_APP_SECRET` + `NHPLUG_ACCT_NO` |
| `meritz` | `MERITZ_APP_KEY` + `MERITZ_APP_SECRET` (계좌번호 불필요, 실전전용) |

---

## 상태 관리

각 종목의 상태는 `.state.json` 파일에 저장됩니다.

| 필드 | 타입 | 설명 |
|------|------|------|
| `T` | float | 누적 매수 횟수 |
| `last_updated` | str (ISO) | 마지막으로 T에 반영된 체결 시각 (UTC) |
| `cycle_start_date` | str | 현재 사이클 시작일 (YYYY-MM-DD) |
| `effective_seed` | float | 복리 재투자 적용 시드 (REINVEST 활성 시) |
| `net_invested` | float | 순투입 금액 (Σ 매수체결금액 − Σ 매도체결금액, USD). 시드 캡 기준 |
| `net_invested_status` | str | `valid` 또는 `unresolved`. `unresolved`이고 `net_invested<=0`일 때 seed>0 신규 전략 주문을 보류 |
| `orders_meta` | dict | 주문별 메타 (t_target, is_additional, 부분체결 추적) |
| `last_processed_ordno` | str | 마지막 처리 주문번호 |
| `balance_mismatch` | dict | 잔고 불일치 진단 정보 |
| `reverse_mode` | dict | 리버스모드 진행일과 누적 매도대금 상태 |

`update_T_from_history()`가 체결 이력을 기반으로 T값을 자동 갱신하며, 브로커 잔고와 이력 기반 포지션을 교차검증해 불일치를 감지합니다.

**DRY 모드에서의 불일치 처리**: DRY(프리뷰/진단) 실행에서 이력-잔고 불일치(`balance_mismatch`)가 감지돼도 상태 기록·자동보정·중단 없이 경고와 텔레그램 알림(1회, 비긴급)만 남기고 프리뷰를 계속 진행합니다(exit 0). 사이클 종료 감지 시에도 리포트만 표시하고 캐시(T 리셋/시드) 저장을 생략합니다. 불일치는 **LIVE 실행에서 기존대로 `balance_mismatch`로 기록·중단**(RuntimeError → fatal)되므로, 실전 진입 전 반드시 확인하세요.

리버스모드의 진행일(`reverse_mode.day_count`)과 누적 매도대금 상태는 주문 생성만으로 새로 확정하지 않습니다. DRY 모드에서는 전략 계산에 상태 복사본을 사용하므로 실행 횟수가 캐시 상태를 진행시키지 않으며, 같은 실행에서 제출한 매도 주문의 예정금액도 매수 가능금액에 포함하지 않습니다. 실제 T 갱신은 체결 이력 반영을 기준으로 합니다.

---

## 복리 재투자

`REINVEST` 기본 활성화 (`true`). 사이클 종료 시 순수익(손실 포함)을 `effective_seed`에 합산하여 다음 사이클 시드로 사용합니다.  
해제 시(`REINVEST=false`) 매 사이클 동일한 시드로 운용됩니다.

---

## 실행 모드

| 변수 | 값 | 역할 |
|------|----|------|
| `BROKER` | `kis` / `kiwoom` / `ls` / `toss` / `nhplug` / `meritz` | 증권사 선택. 기본값 `kis`. (하위호환: `KIS_MODE`도 폴백 지원) |
| `BROKER_MODE` | `real` / `demo` | API 환경 선택(실전/모의). 기본값 `demo`. (하위호환: `KIS_MODE`도 폴백 지원) |
| `TRADE_MODE` | `LIVE` / `DRY` | 실제 주문 실행 여부. `DRY`는 주문 정보만 출력합니다. |
| `ORDER_HISTORY_VERBOSE` | `true` / `false` | LIVE 모드에서도 `[주문이력 요약]` 상세 출력. 기본값 `false`. DRY는 항상 출력. |
| `STATE_DIAGNOSTIC_ONLY` | `true` / `false` | 캐시 상태만 출력하고 API/전략/주문을 실행하지 않음. 기본값 `false`. |
| `STATE_REPAIR_ONLY` | `true` / `false` | 지정 fingerprint가 일치할 때만 리버스 상태를 1회 복구. 기본값 `false`. |
| `STATE_CLEAR_FENCE_ONLY` | `true` / `false` | 지정 fingerprint가 일치할 때만 주문 fence를 1회 초기화. 기본값 `false`. |
| `STATE_REVERSE_AUDIT_ONLY` | `true` / `false` | 브로커 리버스 주문과 state를 대조하고 state hash를 출력. 저장/주문 없음. |
| `STATE_REVERSE_RECONCILE_ONLY` | `true` / `false` | 미반영 체결만 state에 반영. 전략/주문 없음. |
| `STATE_NET_INVESTED_REPAIR_ONLY` | `true` / `false` | state hash와 필드 fingerprint가 일치할 때 `net_invested`를 1회 복구. |
| `STATE_ASSUME_REVERSE_EXPIRY_ONLY` | `true` / `false` | **키움 모의 전용**. 이전 세션 zero-fill 리버스 주문을 `terminal_assumed`로 1회 종결. |

> **하위호환**: `BROKER_MODE` 대신 기존 `KIS_MODE`를 사용해도 작동합니다 (`config.py`가 폴백).

---

## 로컬 실행

`.env` 파일을 만들고 아래 항목을 설정하세요:

```env
# 브로커 설정
BROKER=kis                      # kis, kiwoom, ls, toss, nhplug, meritz
BROKER_MODE=demo                # demo 또는 real (기본: demo)

# KIS 인증정보 (BROKER=kis 일 때)
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678

# 거래 설정
TRADE_MODE=DRY                  # DRY 또는 LIVE
ORDER_HISTORY_VERBOSE=false     # true 시 LIVE에서도 주문이력 상세 출력
STATE_DIAGNOSTIC_ONLY=false     # true 시 캐시 상태만 출력하고 종료
STATE_CLEAR_FENCE_ONLY=false    # true 시 주문 fence만 1회 초기화하고 종료
SYMBOLS=TQQQ:NAS,SOXL:AMS       # 브로커별 거래소 코드 주의 (아래 참고)
TQQQ_SPLITS=40
TQQQ_SYMBOL_TYPE=TQQQ
TQQQ_SEED=10000
TQQQ_ADDITIONAL_LOC_LEVELS=3
SOXL_SPLITS=20
SOXL_SYMBOL_TYPE=SOXL
SOXL_SEED=5000
SOXL_ADDITIONAL_LOC_LEVELS=3
ADDITIONAL_LOC_LEVELS=3         # 모든 종목 공통 기본값 (종목별 설정 우선)
```

환경 변수 관련 주요 동작 요약:

- **다중 종목 중심**: `SYMBOLS`에 `TQQQ:NAS,SOXL:AMS` 형태로 종목을 지정합니다. 미설정 시 기본값은 `TQQQ:NAS,SOXL:AMS`입니다.
- **캐시 상태 진단**: GitHub Actions에서 `.state.json`을 직접 확인할 수 없을 때 해당 Environment의 `STATE_DIAGNOSTIC_ONLY=true`로 1회 실행하면 T, 리버스모드 진행일, 누적 매도대금, 마지막 처리 주문번호만 출력합니다. 확인 후 변수를 즉시 `false` 또는 삭제하세요.
- **캐시 상태 복구**: `STATE_REPAIR_ONLY=true`는 API/전략/주문 없이 지정한 fingerprint가 일치할 때만 실행합니다. `STATE_REPAIR_SYMBOL`, `STATE_REPAIR_EXPECT_T`, `STATE_REPAIR_EXPECT_DAY_COUNT`, `STATE_REPAIR_EXPECT_PROCEEDS`, `STATE_REPAIR_EXPECT_LAST_UPDATED`, `STATE_REPAIR_EXPECT_LAST_ORDNO`, `STATE_REPAIR_EXPECT_REVERSE_IDS`, `STATE_REPAIR_EXPECT_REVERSE_SUBMITTED_AT`를 모두 설정하고, 운영 브랜치의 정확한 캐시에서 1회 실행한 뒤 즉시 모든 복구 변수를 삭제하세요.
- **순투입 신뢰성 복구**: `net_invested_status=unresolved`이고 `net_invested<=0`인 동안 seed>0 신규 전략 주문을 차단합니다. 기존 상태에 양수 `net_invested`가 있으면 status 누락만으로 기존 주문을 중단하지 않습니다. `STATE_REVERSE_AUDIT_ONLY`로 주문·체결 상태를 먼저 확인하고, `STATE_REVERSE_RECONCILE_ONLY`로 미반영 체결을 저장한 뒤, `STATE_NET_INVESTED_REPAIR_ONLY`에서 canonical state hash와 기존 값 fingerprint를 검증해 `net_invested`를 복구합니다. 세부 변수(`SYMBOL`, `TARGET`, `EXPECT_HASH`, `EXPECT_NET_INVESTED`, `EXPECT_STATUS`)는 1회 실행 직후 삭제하세요.
- **키움 모의 만료 가정**: 모의 API가 이전 세션 zero-fill LIMIT 주문을 `접수` 상태로 유지할 때만 `STATE_ASSUME_REVERSE_EXPIRY_ONLY`를 사용합니다. `STATE_ASSUME_REVERSE_EXPIRY_SYMBOL`, `_ORDER`, `_EXPECT_HASH`가 필요하며, 주문 메타에 `terminal_assumed=true`와 사유/시각을 기록합니다. **실전 및 다른 브로커에서는 거부**되며, 실전은 API의 취소·만료·체결 상태를 확인할 때까지 주문을 보류합니다.
- **주문 fence 복구**: 불확실 주문(네트워크 오류 등)으로 남은 주문 fence는 **다음 RUN에서 자동 복구**됩니다. 주문이력/잔고 reconciliation이 정상 통과하고 fence가 이전 미국(ET) 세션 기록이면 이력이 정착된 것으로 보고 fence를 해제하고 진행합니다 (해당 종목만, 다른 종목은 영향 없음). 자동 복구가 안 되는 경우 `STATE_CLEAR_FENCE_ONLY=true` + `STATE_CLEAR_FENCE_SYMBOL` + `STATE_CLEAR_FENCE_EXPECT_T` + `STATE_CLEAR_FENCE_EXPECT_LAST_UPDATED` + `STATE_CLEAR_FENCE_EXPECT_INTENT` + `STATE_CLEAR_FENCE_EXPECT_BATCH`로 1회 초기화 후 변수를 즉시 삭제하세요. `EXPECT_INTENT`/`EXPECT_BATCH`는 상태에 남은 fence 값의 정규화 JSON이며, "fence 없음"이면 빈 값입니다 (env var 존재만 필수).
- **종목별 설정**: `{SYMBOL}_SPLITS`, `{SYMBOL}_SYMBOL_TYPE`, `{SYMBOL}_SEED`, `{SYMBOL}_ADDITIONAL_LOC_LEVELS`를 사용합니다.
  - `{SYMBOL}_SEED`는 **필수**입니다. 미설정 시 시작 시 에러가 발생합니다.
  - 시드 캡은 포지션 원가가 아닌 **순투입 금액**(`net_invested` = Σ 매수체결금액 − Σ 매도체결금액) 기준으로 `remaining_seed = seed − net_invested`를 적용합니다. 평단 이하 매도(손절)로 매도 회수액이 원가보다 작아도 시드 여유가 부풀려지지 않습니다.
  - `{SYMBOL}_ADDITIONAL_LOC_LEVELS` 미설정 시 글로벌 `ADDITIONAL_LOC_LEVELS`를 사용하며, 이것도 없으면 기본값 3이 적용됩니다.
- **복리 재투자**: `REINVEST` 기본 활성화 (해제 시 `false`). 사이클 종료 후 순수익을 다음 시드에 합산합니다.

### 브로커별 거래소 코드 차이

SOXL은 브로커마다 상장 거래소 분류가 다릅니다. `SYMBOLS` 설정 시 주의하세요:

| 종목 | KIS | Kiwoom | 비고 |
|------|-----|--------|------|
| TQQQ | `TQQQ:NAS` | `TQQQ:NAS` | 동일 (나스닥) |
| SOXL | `SOXL:AMS` | `SOXL:NYS` | KIS는 AMEX, Kiwoom은 NYSE |

```bash
uv run python trading_bot.py
```

---

## GitHub Actions 설정

워크플로우는 `repository_dispatch`(외부 트리거)와 `workflow_dispatch`(수동 실행)로 실행됩니다.

### 워크플로우 구조

```
.github/workflows/
  trade_base.yml           # 재사용 가능한 공통 실행 베이스 (직접 실행 X)
  trade_kis_demo.yml       # KIS 모의투자
  trade_kis_real.yml       # KIS 실전
  trade_kiwoom_demo.yml    # 키움 모의투자
  trade_kiwoom_real.yml    # 키움 실전 (self-hosted runner — IP 제약)
  trade_ls_demo.yml        # LS 모의투자
  trade_ls_real.yml        # LS 실전
  trade_toss_real.yml      # 토스 실전 (real만 지원)
  trade_meritz_real.yml    # 메리츠 실전 (real만, 10/12 신청 개방 확인 전)
```

### GitHub Environments 설정 (`Settings > Environments`)

demo/real × 브로커별로 **8개의 Environment**를 생성합니다. 각 Environment에 동일한 이름의 secrets와 variables를 등록하면, `trade_base.yml`의 `environment:` 지시자가 자동으로 해당 환경의 값을 주입합니다.

| Environment | 용도 | Runner |
|-------------|------|--------|
| `kis-demo` | KIS 모의투자 | `ubuntu-latest` |
| `kis-real` | KIS 실전 | `ubuntu-latest` |
| `kiwoom-demo` | 키움 모의투자 | `ubuntu-latest` |
| `kiwoom-real` | 키움 실전 | `self-hosted` (접근 IP 제약) |
| `ls-demo` | LS 모의투자 | `ubuntu-latest` |
| `ls-real` | LS 실전 | `ubuntu-latest` |
| `toss-real` | 토스 실전 (real만) | `ubuntu-latest` |
| `meritz-real` | 메리츠 실전 (real만, 모의 없음) | `ubuntu-latest` (유동IP 사용 가능, 고정IP 불필요) |

### Environment별 Secrets

각 Environment에 아래 secrets를 등록합니다. 해당 브로커에만 필요한 값만 채우면 됩니다 (나머지는 빈 값).

| Secret 이름 | kis | kiwoom | ls | toss | nhplug | meritz | 설명 |
|-------------|-----|--------|------|------|--------|--------|------|
| `KIS_APP_KEY` | ✅ | | | | | | KIS 앱 키 |
| `KIS_APP_SECRET` | ✅ | | | | | | KIS 앱 시크릿 |
| `KIS_ACCOUNT_NO` | ✅ | | | | | | KIS 계좌번호 |
| `KIWOOM_APP_KEY` | | ✅ | | | | | 키움 앱 키 |
| `KIWOOM_APP_SECRET` | | ✅ | | | | | 키움 앱 시크릿 |
| `LS_APP_KEY` | | | ✅ | | | | LS 앱 키 |
| `LS_APP_SECRET` | | | ✅ | | | | LS 앱 시크릿 |
| `TOSS_APP_KEY` | | | | ✅ | | | 토스 앱 키 |
| `TOSS_APP_SECRET` | | | | ✅ | | | 토스 앱 시크릿 |
| `NHPLUG_APP_KEY` | | | | | ✅ | | NHPLUG 앱 키 |
| `NHPLUG_APP_SECRET` | | | | | ✅ | | NHPLUG 앱 시크릿 |
| `NHPLUG_ACCT_NO` | | | | | ✅ | | NHPLUG 계좌번호 (필수) |
| `MERITZ_APP_KEY` | | | | | | ✅ | 메리츠 앱 키 (계좌번호 불필요) |
| `MERITZ_APP_SECRET` | | | | | | ✅ | 메리츠 앱 시크릿 |
| `FINNHUB_API_KEY` | | | ✅ | | | | LS 모의투자용 Finnhub API 키 (무료, https://finnhub.io/register) |
| `TELEGRAM_BOT_TOKEN` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 텔레그램 봇 토큰 (환경별 채널 분리 가능) |
| `TELEGRAM_CHAT_ID` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 텔레그램 채팅 ID |

> **demo/real 분리**: 같은 브로커의 demo와 real은 서로 다른 Environment이므로, 각각 다른 계좌번호와 텔레그램 채널을 등록할 수 있습니다.

### Environment별 Variables

각 Environment에 아래 variables를 등록합니다. 브로커별로 거래소 코드가 다르므로 `SYMBOLS`를 각각 설정해야 합니다.

| Variable | kis-demo / kis-real | kiwoom-demo / kiwoom-real | ls-demo / ls-real | 설명 |
|----------|---------------------|---------------------------|-------------------|------|
| `SYMBOLS` | `TQQQ:NAS,SOXL:AMS` | `TQQQ:NAS,SOXL:NYS` | `TQQQ:NAS,SOXL:AMS` | 거래 종목 목록 (거래소 코드 주의) |
| `TRADE_MODE` | `DRY` (demo) / `LIVE` (real) | `DRY` (demo) / `LIVE` (real) | `DRY` (demo) / `LIVE` (real) | 기본 거래 모드 |
| `ORDER_HISTORY_VERBOSE` | `true` / `false` | `true` / `false` | `true` / `false` | LIVE에서도 주문이력 상세 출력 (기본 `false`) |
| `TQQQ_SPLITS` | `40` | `40` | `40` | TQQQ 분할 수 |
| `TQQQ_SYMBOL_TYPE` | `TQQQ` | `TQQQ` | `TQQQ` | TQQQ 별지점 공식 타입 |
| `TQQQ_SEED` | `10000` | `10000` | `10000` | TQQQ 시드 금액 |
| `TQQQ_ADDITIONAL_LOC_LEVELS` | `3` | `3` | `3` | TQQQ 급락 대비 추가 LOC 단계 수 |
| `SOXL_SPLITS` | `20` | `20` | `20` | SOXL 분할 수 |
| `SOXL_SYMBOL_TYPE` | `SOXL` | `SOXL` | `SOXL` | SOXL 별지점 공식 타입 |
| `SOXL_SEED` | `5000` | `5000` | `5000` | SOXL 시드 금액 |
| `SOXL_ADDITIONAL_LOC_LEVELS` | `3` | `3` | `3` | SOXL 급락 대비 추가 LOC 단계 수 |
| `ADDITIONAL_LOC_LEVELS` | `3` | `3` | `3` | 모든 종목 공통 기본값 |
| `QUIET_HOURS` | `true` (demo) / `false` (real) | `true` (demo) / `false` (real) | `true` (demo) / `false` (real) | 조용한 시간대 기능 |
| `COMMISSION_RATE` | `0.0025` | `0.0025` | `0.0025` | 수수료율 |
| `REINVEST` | `true` | `true` | `true` | 복리 재투자 여부 (해제 시 `false`) |

### repository_dispatch 트리거

외부 시스템에서 `repository_dispatch`로 워크플로우를 트리거할 수 있습니다.

| dispatch type | 워크플로우 | 비고 |
|---------------|-----------|------|
| `trigger_trade_demo` | `trade_kis_demo.yml` | 기존 type (하위호환) |
| `trigger_trade_real` | `trade_kis_real.yml` | 기존 type (하위호환) |
| `trigger_trade_kis_demo` | `trade_kis_demo.yml` | 신규 type |
| `trigger_trade_kis_real` | `trade_kis_real.yml` | 신규 type |
| `trigger_trade_kiwoom_demo` | `trade_kiwoom_demo.yml` | |
| `trigger_trade_kiwoom_real` | `trade_kiwoom_real.yml` | |
| `trigger_trade_ls_demo` | `trade_ls_demo.yml` | |
| `trigger_trade_ls_real` | `trade_ls_real.yml` | |
| `trigger_trade_toss_real` | `trade_toss_real.yml` | |
| `trigger_trade_meritz_real` | `trade_meritz_real.yml` | 수동 트리거만 (10/12 신청 개방 확인 전) |

### Self-hosted Runner (Kiwoom Real)

키움증권 실전 API는 사전 등록된 IP에서만 호출할 수 있습니다. `trade_kiwoom_real.yml`은 `self-hosted` runner를 사용합니다.

1. 등록된 IP를 가진 머신에 [GitHub Actions self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners-with-github-actions) 설치
2. runner label이 `self-hosted`로 설정되어 있어야 함
3. `kiwoom-real` Environment의 secrets에 키움 인증정보 등록

---

## 안전 운영 권장

- 실전 배포 전 `TRADE_MODE=DRY`로 먼저 실행해 주문 목록을 확인하세요.
- `BROKER_MODE=real` + `TRADE_MODE=LIVE` 조합은 **실제 주문이 나갑니다.** 설정을 두 번 확인하세요.
- demo/real Environment에 각각 다른 계좌번호를 등록했는지 확인하세요.
- 키움 real은 self-hosted runner의 IP가 키움에 사전 등록되어 있어야 합니다.

---

## ⚖️ 면책 조항

이 프로그램은 교육 및 연구 목적으로 제공됩니다.  
실제 투자에 사용 시 발생하는 모든 손실에 대해 개발자는 책임을 지지 않습니다.  
투자는 본인의 판단과 책임 하에 진행하시기 바랍니다.
