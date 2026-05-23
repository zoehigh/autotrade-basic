# autotrade-basic — 자동매매 봇

KIS(한국투자증권) Open API를 사용한 미국 주식 자동매매 학습용 프로젝트입니다.  
무상태 무한매수법 전략을 기반으로 동작하며, GitHub Actions에서 자동 실행됩니다.

---

## 프로젝트 구조

```
trading_bot.py        # 메인 실행 파일 (주문 루프 · 결과 요약)
src/
  strategy.py         # 무상태 무한매수법 전략 로직
  trader.py           # KIS API 호출 래퍼 (주문 · 예약주문 · 잔고 · 시세)
  authentication.py   # 액세스 토큰 발급
  config.py           # 환경변수 로드
  notifier.py         # 알림 헬퍼
  telegram.py         # 텔레그램 전송
.github/workflows/
  trade_base.yml      # 재사용 가능한 공통 실행 베이스
  trade_real.yml      # 실전 계좌 워크플로우 (기본 TRADE_MODE=LIVE)
  trade_demo.yml      # 모의 계좌 워크플로우 (기본 TRADE_MODE=DRY)
```

---

## 전략 개요 — 무상태 무한매수법

1. **포지션 없음**: 현재가로 초기 진입 (2 × 단위수량, LIMIT 주문)
2. **포지션 있음 — 익절 조건 충족**: 전량 매도 (LIMIT 주문)
3. **포지션 있음 — 추가 매수**:
   - 평단가에 LOC 매수 (단위수량)
   - 큰수 기준가에 LOC 매수 (단위수량)

> 전략은 `trading_bot.py`에서 주문 목록만 생성하고, 실제 API 호출은 `trader.py`가 담당합니다.

---

## 주문 흐름

```
place_overseas_order()         # /trading/order (일반 주문)
  └─ 모의 + 정규장 외 시간
       └─ ReservationOrderRequired 예외 발생
            └─ place_overseas_reservation_order()  # /trading/order-resv (예약주문)
```

실행 후 주문은 4가지로 집계됩니다:

| 구분 | 설명 |
|------|------|
| 체결 성공 | `/trading/order` 정상 접수 |
| 예약 접수 | `/trading/order-resv` 예약 완료 (다음 정규장 시작 시 체결) |
| 실패 | 일반/예약 주문 모두 실패 |
| 건너뜀 | 매도 주문 (현재 미지원) |

---

## 실행 모드

| 변수 | 값 | 역할 |
|------|----|------|
| `KIS_MODE` | `real` / `demo` | KIS Open API 환경 선택(실전/모의). 기본값은 `demo`입니다. `real` 선택 시 실계좌(`KIS_ACCOUNT_NO`)를 사용하고 API 도메인은 `https://openapi.koreainvestment.com:9443`로 설정됩니다. `demo`는 모의 도메인 `https://openapivts.koreainvestment.com:29443`과 `KIS_ACCOUNT_NO_DEMO`를 사용합니다. |
| `TRADE_MODE` | `LIVE` / `DRY` | 실제 주문 실행 여부. `DRY`는 주문 정보만 출력합니다. 입력값이 유효하지 않으면 경고를 출력하고 기본값 `DRY`로 동작합니다. |

---

## 로컬 실행

`.env` 파일을 만들고 아래 항목을 설정하세요:

```env
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678        # 실계좌 번호
KIS_ACCOUNT_NO_DEMO=87654321   # 모의계좌 번호
KIS_MODE=demo                  # demo 또는 real (기본: demo)
TRADE_MODE=DRY                 # DRY 또는 LIVE (잘못된 값은 DRY로 대체됨)
SYMBOL=TQQQ
EXCHANGE=NAS
SPLITS=40
TAKE_PROFIT=0.10
BIG_BUY_RANGE=0.10
```

환경 변수 관련 주요 동작 요약:

- **다중 종목 지원**: `SYMBOLS`에 `TQQQ:NAS,SOXL:AMS` 형태로 복수 종목을 지정할 수 있습니다. 지정하지 않으면 `SYMBOL` + `EXCHANGE`를 사용하고, 둘 다 없으면 기본으로 `TQQQ:NAS`와 `SOXL:AMS`가 사용됩니다.
- **종목별 설정 우선순위**: `{SYMBOL}_SPLITS`, `{SYMBOL}_TAKE_PROFIT`, `{SYMBOL}_BIG_BUY_RANGE`, `{SYMBOL}_SEED` 같은 종목 전용 환경변수를 사용하면 전역 값(`SPLITS`, `TAKE_PROFIT`, `BIG_BUY_RANGE`)보다 우선합니다. 전용 변수가 없으면 전역 기본값을 사용합니다.

```bash
uv run python trading_bot.py
```

---

## GitHub Actions 설정

### Secrets (`Settings > Secrets and variables > Actions > Secrets`)

| 이름 | 필수 | 설명 |
|------|------|------|
| `KIS_APP_KEY` | ✅ | KIS Open API 앱 키 |
| `KIS_APP_SECRET` | ✅ | KIS Open API 앱 시크릿 |
| `KIS_ACCOUNT_NO` | ✅ | 실전 계좌번호 |
| `KIS_ACCOUNT_NO_DEMO` | ✅ | 모의 계좌번호 |
| `TELEGRAM_BOT_TOKEN_DEMO` | 선택 | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID_DEMO` | 선택 | 텔레그램 채팅 ID |

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
  KIS_ACCOUNT_NO: ${{ secrets.KIS_ACCOUNT_NO }}
  KIS_ACCOUNT_NO_DEMO: ${{ secrets.KIS_ACCOUNT_NO_DEMO }}
```

이렇게 하면 동일한 재사용 워크플로우(`.github/workflows/trade_base.yml`)를 사용하더라도, 데모 실행 시 데모 전용 키/계좌가 전달되어 실계좌 사용 실수를 방지할 수 있습니다.

`trade_real.yml`은 실전 키(`KIS_APP_KEY`, `KIS_APP_SECRET`)를 사용하도록 설정되어 있으니, 실전 배포 전 키 설정을 반드시 확인하세요.

### Repository Variables (`Settings > Secrets and variables > Actions > Variables`)

민감하지 않은 기본값은 Variables에 저장해 워크플로우에서 공유합니다.

| 이름 | 예시 값 | 설명 |
|------|---------|------|
| `SYMBOL` | `TQQQ` | 거래 종목 코드 |
| `EXCHANGE` | `NAS` | 거래소 코드 (`NAS`, `NYS`, `AMS` 등) |
| `SPLITS` | `40` | 분할 수 |
| `TAKE_PROFIT` | `0.10` | 익절률 (10% = 0.10) |
| `BIG_BUY_RANGE` | `0.10` | 큰수 상승률 |

---

## 워크플로우 스케줄

### `trade_real.yml` — 실전 계좌

ET(미국 동부시간) 기준 프리마켓 오전 4시에 자동 실행됩니다.

| 기간 | UTC cron | 설명 |
|------|----------|------|
| 4월~10월 (EDT) | `0 8 * 4-10 1-5` | ET+4 = UTC 08:00 |
| 11월~3월 (EST) | `0 9 * 11-3 1-5` | ET+5 = UTC 09:00 |

기본 `TRADE_MODE=LIVE` — 수동 실행 시 DRY 선택 가능.

### `trade_demo.yml` — 모의 계좌

ET 기준 오전 10시 10분(장중)에 자동 실행됩니다. 예약주문 검증에 활용합니다.

기본 `TRADE_MODE=DRY` — `Variables`의 `SYMBOL` 등을 자동으로 적용합니다.

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