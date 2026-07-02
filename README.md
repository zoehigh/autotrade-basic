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
  trade_base.yml      # 재사용 가능한 공통 실행 베이스
  trade_real.yml      # 실전 계좌
  trade_demo.yml      # 모의 계좌
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

- `T >= splits` 시 모든 분할을 소진한 것으로 판단, 더 이상 주문을 생성하지 않음

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

---

## 상태 관리

각 종목의 상태는 `.state.json` 파일에 저장됩니다.

| 필드 | 타입 | 설명 |
|------|------|------|
| `T` | float | 누적 매수 횟수 |
| `last_updated` | str (ISO) | 마지막으로 T에 반영된 체결 시각 (UTC) |
| `cycle_start_date` | str | 현재 사이클 시작일 (YYYY-MM-DD) |
| `effective_seed` | float | 복리 재투자 적용 시드 (REINVEST 활성 시) |
| `orders_meta` | dict | 주문별 메타 (t_target, is_additional, 부분체결 추적) |
| `last_processed_ordno` | str | 마지막 처리 주문번호 |
| `balance_mismatch` | dict | 잔고 불일치 진단 정보 |

`update_T_from_history()`가 체결 이력을 기반으로 T값을 자동 갱신하며, 브로커 잔고와 이력 기반 포지션을 교차검증해 불일치를 감지합니다.

---

## 복리 재투자

`REINVEST` 기본 활성화 (`true`). 사이클 종료 시 순수익(손실 포함)을 `effective_seed`에 합산하여 다음 사이클 시드로 사용합니다.  
해제 시(`REINVEST=false`) 매 사이클 동일한 시드로 운용됩니다.

---

## 실행 모드

| 변수 | 값 | 역할 |
|------|----|------|
| `KIS_MODE` | `real` / `demo` | KIS Open API 환경 선택(실전/모의). 기본값은 `demo`. |
| `TRADE_MODE` | `LIVE` / `DRY` | 실제 주문 실행 여부. `DRY`는 주문 정보만 출력합니다. |

---

## 로컬 실행

`.env` 파일을 만들고 아래 항목을 설정하세요:

```env
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678        # 실계좌 번호
KIS_ACCOUNT_NO_DEMO=87654321   # 모의계좌 번호
KIS_MODE=demo                  # demo 또는 real (기본: demo)
TRADE_MODE=DRY                 # DRY 또는 LIVE
SYMBOLS=TQQQ:NAS,SOXL:AMS
TQQQ_SPLITS=40
TQQQ_SYMBOL_TYPE=TQQQ
TQQQ_SEED=10000
TQQQ_ADDITIONAL_LOC_LEVELS=3
SOXL_SPLITS=20
SOXL_SYMBOL_TYPE=SOXL
SOXL_SEED=5000
SOXL_ADDITIONAL_LOC_LEVELS=3
ADDITIONAL_LOC_LEVELS=3        # 모든 종목 공통 기본값 (종목별 설정 우선)
```

환경 변수 관련 주요 동작 요약:

- **다중 종목 중심**: `SYMBOLS`에 `TQQQ:NAS,SOXL:AMS` 형태로 종목을 지정합니다. 미설정 시 기본값은 `TQQQ:NAS,SOXL:AMS`입니다.
- **종목별 설정**: `{SYMBOL}_SPLITS`, `{SYMBOL}_SYMBOL_TYPE`, `{SYMBOL}_SEED`, `{SYMBOL}_ADDITIONAL_LOC_LEVELS`를 사용합니다.
  - `{SYMBOL}_ADDITIONAL_LOC_LEVELS` 미설정 시 글로벌 `ADDITIONAL_LOC_LEVELS`를 사용하며, 이것도 없으면 기본값 3이 적용됩니다.
- **복리 재투자**: `REINVEST` 기본 활성화 (해제 시 `false`). 사이클 종료 후 순수익을 다음 시드에 합산합니다.

```bash
uv run python trading_bot.py
```

---

## GitHub Actions 설정

워크플로우는 `repository_dispatch`(외부 트리거)와 `workflow_dispatch`(수동 실행)로 실행됩니다.

### Secrets (`Settings > Secrets and variables > Actions > Secrets`)

| 이름 | 필수 | 설명 |
|------|------|------|
| `KIS_APP_KEY` | ✅ | KIS Open API 앱 키 |
| `KIS_APP_SECRET` | ✅ | KIS Open API 앱 시크릿 |
| `KIS_ACCOUNT_NO` | ✅ | 실전 계좌번호 |
| `KIS_ACCOUNT_NO_DEMO` | ✅ | 모의 계좌번호 |
| `TELEGRAM_BOT_TOKEN_DEMO` | 선택 | 모의 텔레그램 봇 토큰 |
| `TELEGRAM_BOT_TOKEN_REAL` | 선택 | 실전 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID_DEMO` | 선택 | 모의 텔레그램 채팅 ID |
| `TELEGRAM_CHAT_ID_REAL` | 선택 | 실전 텔레그램 채팅 ID |

### 워크플로우별 비밀키(권장 네이밍)

워크플로우는 실전/모의용 키를 구분해서 사용하도록 설계되어 있습니다. 실전 키와 모의 키를 별도로 등록해 사용하는 것을 권장합니다:

- `KIS_APP_KEY`: 실전(라이브) 앱 키
- `KIS_APP_SECRET`: 실전 앱 시크릿
- `KIS_APP_KEY_DEMO`: 모의(데모) 앱 키
- `KIS_APP_SECRET_DEMO`: 모의 앱 시크릿
- `KIS_ACCOUNT_NO`: 실전 계좌번호
- `KIS_ACCOUNT_NO_DEMO`: 모의 계좌번호

예를 들어 `.github/workflows/trade_demo.yml`은 데모 전용 시크릿을 재사용 워크플로우로 전달합니다:

```yaml
secrets:
  KIS_APP_KEY: ${{ secrets.KIS_APP_KEY_DEMO }}
  KIS_APP_SECRET: ${{ secrets.KIS_APP_SECRET_DEMO }}
  KIS_ACCOUNT_NO: ${{ secrets.KIS_ACCOUNT_NO_DEMO }}
```

`trade_real.yml`은 실전 키(`KIS_APP_KEY`, `KIS_APP_SECRET`)를 사용하도록 설정되어 있습니다.

### Repository Variables (`Settings > Secrets and variables > Actions > Variables`)

민감하지 않은 기본값은 Variables에 저장해 워크플로우에서 공유합니다.

| 이름 | 예시 값 | 설명 |
|------|---------|------|
| `SYMBOLS` | `TQQQ:NAS,SOXL:AMS` | 거래 종목 목록 |
| `TQQQ_SPLITS` | `40` | TQQQ 분할 수 |
| `TQQQ_SYMBOL_TYPE` | `TQQQ` | TQQQ 별지점 공식 타입 |
| `TQQQ_SEED` | `10000` | TQQQ 시드 금액 |
| `TQQQ_ADDITIONAL_LOC_LEVELS` | `3` | TQQQ 급락 대비 추가 LOC 단계 수 |
| `SOXL_SPLITS` | `20` | SOXL 분할 수 |
| `SOXL_SYMBOL_TYPE` | `SOXL` | SOXL 별지점 공식 타입 |
| `SOXL_SEED` | `5000` | SOXL 시드 금액 |
| `SOXL_ADDITIONAL_LOC_LEVELS` | `3` | SOXL 급락 대비 추가 LOC 단계 수 |
| `ADDITIONAL_LOC_LEVELS` | `3` | 모든 종목 공통 기본값 |
| `TRADE_MODE_DEMO` | `DRY` | 데모 워크플로우 기본 거래 모드 |
| `REINVEST_REAL` / `REINVEST_DEMO` | `true` | 복리 재투자 여부 (해제 시 `false`) |

---

## 안전 운영 권장

- 실전 배포 전 `TRADE_MODE=DRY`로 먼저 실행해 주문 목록을 확인하세요.
- `KIS_MODE=real` + `TRADE_MODE=LIVE` 조합은 **실제 주문이 나갑니다.** 설정을 두 번 확인하세요.
- 실전/모의 계좌번호(`KIS_ACCOUNT_NO` / `KIS_ACCOUNT_NO_DEMO`)가 뒤바뀌지 않도록 주의하세요.

---

## ⚖️ 면책 조항

이 프로그램은 교육 및 연구 목적으로 제공됩니다.  
실제 투자에 사용 시 발생하는 모든 손실에 대해 개발자는 책임을 지지 않습니다.  
투자는 본인의 판단과 책임 하에 진행하시기 바랍니다.
