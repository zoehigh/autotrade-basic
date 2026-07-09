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
  trade_ls_demo.yml        # LS 모의투자 (구현 예정)
  trade_ls_real.yml        # LS 실전 (구현 예정)
  trade_toss_real.yml      # 토스 실전 (real만, 구현 예정)
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
| `BROKER` | `kis` / `kiwoom` / `ls` / `toss` | 증권사 선택. 기본값 `kis`. (하위호환: `KIS_MODE`도 폴백 지원) |
| `BROKER_MODE` | `real` / `demo` | API 환경 선택(실전/모의). 기본값 `demo`. (하위호환: `KIS_MODE`도 폴백 지원) |
| `TRADE_MODE` | `LIVE` / `DRY` | 실제 주문 실행 여부. `DRY`는 주문 정보만 출력합니다. |

> **하위호환**: `BROKER_MODE` 대신 기존 `KIS_MODE`를 사용해도 작동합니다 (`config.py`가 폴백).

---

## 로컬 실행

`.env` 파일을 만들고 아래 항목을 설정하세요:

```env
# 브로커 설정
BROKER=kis                      # kis, kiwoom, ls, toss
BROKER_MODE=demo                # demo 또는 real (기본: demo)

# KIS 인증정보 (BROKER=kis 일 때)
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678

# 거래 설정
TRADE_MODE=DRY                  # DRY 또는 LIVE
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
- **종목별 설정**: `{SYMBOL}_SPLITS`, `{SYMBOL}_SYMBOL_TYPE`, `{SYMBOL}_SEED`, `{SYMBOL}_ADDITIONAL_LOC_LEVELS`를 사용합니다.
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
  trade_ls_demo.yml        # LS 모의투자 (브로커 구현 예정)
  trade_ls_real.yml        # LS 실전 (브로커 구현 예정)
  trade_toss_real.yml      # 토스 실전 (real만 지원, 브로커 구현 예정)
```

### GitHub Environments 설정 (`Settings > Environments`)

demo/real × 브로커별로 **7개의 Environment**를 생성합니다. 각 Environment에 동일한 이름의 secrets와 variables를 등록하면, `trade_base.yml`의 `environment:` 지시자가 자동으로 해당 환경의 값을 주입합니다.

| Environment | 용도 | Runner |
|-------------|------|--------|
| `kis-demo` | KIS 모의투자 | `ubuntu-latest` |
| `kis-real` | KIS 실전 | `ubuntu-latest` |
| `kiwoom-demo` | 키움 모의투자 | `ubuntu-latest` |
| `kiwoom-real` | 키움 실전 | `self-hosted` (접근 IP 제약) |
| `ls-demo` | LS 모의투자 | `ubuntu-latest` |
| `ls-real` | LS 실전 | `ubuntu-latest` |
| `toss-real` | 토스 실전 (real만) | `ubuntu-latest` |

### Environment별 Secrets

각 Environment에 아래 secrets를 등록합니다. 해당 브로커에만 필요한 값만 채우면 됩니다 (나머지는 빈 값).

| Secret 이름 | kis | kiwoom | ls | toss | 설명 |
|-------------|-----|--------|----|------|------|
| `KIS_APP_KEY` | ✅ | | | | KIS 앱 키 |
| `KIS_APP_SECRET` | ✅ | | | | KIS 앱 시크릿 |
| `KIS_ACCOUNT_NO` | ✅ | | | | KIS 계좌번호 |
| `KIWOOM_APP_KEY` | | ✅ | | | 키움 앱 키 |
| `KIWOOM_APP_SECRET` | | ✅ | | | 키움 앱 시크릿 |
| `LS_APP_KEY` | | | ✅ | | LS 앱 키 |
| `LS_APP_SECRET` | | | ✅ | | LS 앱 시크릿 |
| `TOSS_APP_KEY` | | | | ✅ | 토스 앱 키 |
| `TOSS_APP_SECRET` | | | | ✅ | 토스 앱 시크릿 |
| `TELEGRAM_BOT_TOKEN` | ✅ | ✅ | ✅ | ✅ | 텔레그램 봇 토큰 (환경별 채널 분리 가능) |
| `TELEGRAM_CHAT_ID` | ✅ | ✅ | ✅ | ✅ | 텔레그램 채팅 ID |

> **demo/real 분리**: 같은 브로커의 demo와 real은 서로 다른 Environment이므로, 각각 다른 계좌번호와 텔레그램 채널을 등록할 수 있습니다.

### Environment별 Variables

각 Environment에 아래 variables를 등록합니다. 브로커별로 거래소 코드가 다르므로 `SYMBOLS`를 각각 설정해야 합니다.

| Variable | kis-demo / kis-real | kiwoom-demo / kiwoom-real | 설명 |
|----------|---------------------|---------------------------|------|
| `SYMBOLS` | `TQQQ:NAS,SOXL:AMS` | `TQQQ:NAS,SOXL:NYS` | 거래 종목 목록 (거래소 코드 주의) |
| `TRADE_MODE` | `DRY` (demo) / `LIVE` (real) | `DRY` (demo) / `LIVE` (real) | 기본 거래 모드 |
| `TQQQ_SPLITS` | `40` | `40` | TQQQ 분할 수 |
| `TQQQ_SYMBOL_TYPE` | `TQQQ` | `TQQQ` | TQQQ 별지점 공식 타입 |
| `TQQQ_SEED` | `10000` | `10000` | TQQQ 시드 금액 |
| `TQQQ_ADDITIONAL_LOC_LEVELS` | `3` | `3` | TQQQ 급락 대비 추가 LOC 단계 수 |
| `SOXL_SPLITS` | `20` | `20` | SOXL 분할 수 |
| `SOXL_SYMBOL_TYPE` | `SOXL` | `SOXL` | SOXL 별지점 공식 타입 |
| `SOXL_SEED` | `5000` | `5000` | SOXL 시드 금액 |
| `SOXL_ADDITIONAL_LOC_LEVELS` | `3` | `3` | SOXL 급락 대비 추가 LOC 단계 수 |
| `ADDITIONAL_LOC_LEVELS` | `3` | `3` | 모든 종목 공통 기본값 |
| `QUIET_HOURS` | `true` (demo) / `false` (real) | `true` (demo) / `false` (real) | 조용한 시간대 기능 |
| `COMMISSION_RATE` | `0.0025` | `0.0025` | 수수료율 |
| `REINVEST` | `true` | `true` | 복리 재투자 여부 (해제 시 `false`) |

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
