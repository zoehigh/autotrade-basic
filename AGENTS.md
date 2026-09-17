# PROJECT KNOWLEDGE BASE

**Generated:** 2026-06-25
**Updated:** 2026-08-08
**Branch:** `develop`

## OVERVIEW
미국 주식 자동매매 봇 (Python). 5개 증권사(KIS, KIWOOM, LS, TOSS, NHPLUG) API를 지원하며, 무한매수법 V4 전략을 실행. GitHub Actions에서 `repository_dispatch`로 트리거됨.

## STRUCTURE
```
autotrade-basic/
├── src/
│   ├── broker/          # 브로커별 API 구현체
│   │   ├── base.py      # 공통 에러/데이터 클래스
│   │   ├── kis/         # 한국투자증권
│   │   ├── kiwoom/      # 키움증권
│   │   ├── ls/          # LS증권
│   │   ├── toss/        # 토스증권
│   │   ├── nhplug/      # NH투자증권 나무
│   │   └── market_utils.py  # 시간/시장 유틸리티
│   ├── market_data.py   # 시세 데이터 (Finnhub 5일 MA)
│   ├── strategy.py      # 전략 로직 (+ 리버스모드)
│   ├── state.py         # 상태 관리 (+ reverse_mode/close_prices)
│   ├── config.py        # 환경변수 설정
│   ├── telegram.py      # 텔레그램 발송
│   └── notifier.py      # 알림 라우팅
├── tests/               # pytest 기반 테스트
├── .github/workflows/   # GitHub Actions
└── trading_bot.py       # 메인 파이프라인
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| 전략 로직 이해 | src/strategy.py | `무한매수법_V4()`, T값/별지점 계산, `execute_reverse_mode()` |
| 브로커 구현 | src/broker/{kis,kiwoom,ls,toss,nhplug}/ | 각 브로커별 API 어댑터 |
| 공통 인터페이스 | src/broker/base.py | `Broker`, `OrderResult`, `BrokerError`, `get_daily_closes()` |
| 일봉 종가 조회 | src/broker/base.py `get_daily_closes()` | TR: KIS=HHDFS76240000, LS=g3204, KIWOOM=usa06012, TOSS=GET /api/v1/candles |
| 상태 파일 관리 | src/state.py | state.json 로드/저장, T 갱신, reverse_mode 상태 |
| 환경변수 설정 | src/config.py | 종목 설정, 모드, 수수료, T 보정 env var |
| 시세 데이터 | src/market_data.py | Finnhub 5일 이동평균 조회 |
| 파이프라인 흐름 | trading_bot.py | state→전략→주문→저장 전체 순서 |

## BROKER ERROR HANDLING
| 브로커 | 성공 코드 | 에러 코드 | 에러 메시지 | 검증 위치 |
|--------|----------|----------|------------|----------|
| KIS | `rt_cd == "0"` | `msg_cd` | `msg1` | 각 메서드 |
| KIWOOM | `return_code == 0` | `return_code` | `return_msg` | `_check_response()` |
| LS | `rsp_cd == "00000"` | `rsp_cd` | `rsp_msg` | 각 메서드 |
| TOSS | `error` 없음 | `error.code` | `error.message` | `_request_with_rate_retry()` |
| NHPLUG | `rsp_cd in {"00000","00166","00221","13578","XA102","00048"} or "완료" in rsp_msg/usr_msg` | `rsp_cd`/`msg_code` | `rsp_msg`/`usr_msg` | `_request_with_rate_retry()` |

**참고**: 주문 시각은 모든 브로커에서 `get_kst_now()` 로컬 시간 사용 (API 응답 시간 미사용)

## CONVENTIONS
- **패키지 의존성**: `requests`, `python-dotenv`, `exchange-calendars` (`uv` 관리)
- **실행**: `uv run python trading_bot.py` / `uv run python tests/test_dryrun.py`
- **환경변수**: `.env` 파일, 대문자 snake_case (예: `KIS_APP_KEY`, `TRADE_MODE`)
- **KoCra v4**: 변수/함수명은 영어, 주석/로그는 한국어
- **한 함수 한 역할**: 함수당 단일 책임 원칙
- **김작가님 규칙**: 초보자 가독성 우선, 과도한 추상화 금지
- **Finnhub API**: 리버스모드 5일 MA 별지점 계산용 (선택, 없으면 state close_prices fallback)

## ANTI-PATTERNS (THIS PROJECT)
- `__pycache__/` — 절대 커밋 금지 (`.gitignore`에 있음)
- `.state.json` — 커밋 금지 (GH Actions 캐시로만 관리)
- `KIS_ACCOUNT_NO` / `NHPLUG_ACCT_NO` 없는 상태로 KIS/NHPLUG API 호출 금지 (KIWOOM/LS/TOSS는 계좌번호 불필요)
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (broker별 adapter)
- 주문가 소수점 초과(예: $1+ 3자리) 전송 금지 — 각 broker `place_order()`가 `normalize_order_price()`로 호가 단위($1+ → 2자리, $1 미만 → 4자리, 버림) 정규화 후 전송
- `TRADE_MODE` 무단 LIVE 전환 금지 (DRY 먼저 확인)
- `.venv` 의존성 직접 수정 금지 — 항상 `uv` 사용
- `FINNHUB_API_KEY` 없어도 동작은 하나, 리버스모드 MA(5) 별지점 정확도를 위해 등록 권장

## UNIQUE STYLES
- 유니코드 함수명 `무한매수법_V4()` — 전략 함수만 한글명
- T값(float)이 누적 매수 횟수를 나타내는 독특한 상태 관리
- 모의/실전 TR_ID를 KIS_MODE에 따라 동적 전환
- DRY 모드에서는 주문 출력만 하고 실행하지 않으며, 리버스모드 상태도 복사본으로 계산해 캐시에 진행 상태를 저장하지 않음
- **DRY 불일치/사이클 종료 처리**: DRY에서는 이력-잔고 불일치(`balance_mismatch`)가 감지돼도 **기록·자동보정·중단 없이 경고 + 텔레그램 1회(비긴급)만** 출력하고 프리뷰를 계속 진행(exit 0). 사이클 종료 감지 시 리포트만 표시하고 **캐시 저장(T 리셋/시드)을 생략**. LIVE는 기존대로 기록 + 중단(RuntimeError → fatal). `{SYMBOL}_FORCE_T`/`FORCE_T_REINFERENCE`는 가드 아래에 있어 DRY에서 불일치가 있어도 정상 적용됨
- 리버스모드(`execute_reverse_mode()`): T>분할수-1 시 발동, 5일 MA 별지점 기반 무한매도+쿼터매수
- T값 환경변수 보정: `FORCE_T_REINFERENCE`, `{SYMBOL}_FORCE_T`, `{SYMBOL}_MAX_T` (기본 MAX_T=분할수, 리버스모드 진입용 `T=splits` 허용)

## COMMANDS
```bash
# 전략 what-if 검증 (API/상태 미접촉)
uv run python tests/test_dryrun.py

# DRY 모드 실행
TRADE_MODE=DRY uv run python trading_bot.py

# LIVE 모드 실행 (실제 주문)
TRADE_MODE=LIVE uv run python trading_bot.py

# T값 보정: 전체 이력 재추정 (DRY 자동 전환)
FORCE_T_REINFERENCE=true uv run python trading_bot.py

# T값 강제 설정 (1회성 — 보정 RUN 후 env var 즉시 삭제)
TQQQ_FORCE_T=29 SOXL_FORCE_T=20 TRADE_MODE=DRY uv run python trading_bot.py

# T값 상한 설정 (재추정 시 상한 제한, 기본값은 분할수)
SOXL_MAX_T=19 FORCE_T_REINFERENCE=true uv run python trading_bot.py

# 테스트 실행
# 브로커 실 API 통합 테스트는 pytest 마커(kis/kiwoom/ls/toss/nhplug)로 구분됩니다.
# tests/conftest.py가 .env 자격증명만으로 실행 여부를 판단합니다 — BROKER 값과 무관하게
# 해당 브로커 키가 설정돼 있으면 실행되고, 없으면 자동 skip됩니다.
# 마커 없는 유닛/모의 테스트는 항상 실행됩니다.
uv run pytest tests/ -v                          # 키 설정된 브로커 통합 테스트 + 유닛
uv run pytest tests/ -m kis -v                   # 특정 브로커만 (kis/kiwoom/ls/toss/nhplug)
uv run pytest tests/test_kiwoom_integration.py -v  # 특정 파일도 자격증명 기준으로 동작

# 참고: 브로커별 필수 자격증명
#   kis   = KIS_APP_KEY + KIS_APP_SECRET + KIS_ACCOUNT_NO
#   kiwoom= KIWOOM_APP_KEY + KIWOOM_APP_SECRET
#   ls    = LS_APP_KEY + LS_APP_SECRET
#   toss  = TOSS_APP_KEY + TOSS_APP_SECRET
#   nhplug= NHPLUG_APP_KEY + NHPLUG_APP_SECRET + NHPLUG_ACCT_NO
# 유닛/모의 테스트(test_broker_contract, test_reverse_mode, test_state_t_updates 등)는
# API를 호출하지 않아 자격증명 없이 항상 실행됩니다.
```

## PREFLIGHT (브로커 검증) — 모의 전 사전 테스트

신규/변경 브로커는 모의 전략 실행 전에 문서 체크리스트 기반 preflight 검증을 1회 통과해야 합니다. 코드 차단(마커)이 아니라 문서·스크립트 기반 사전 점검입니다.

**용어**: `인증` 대신 `preflight` / `브로커 검증` / `API 사전 테스트`를 사용합니다.

**범위 (전략이 실제 호출하는 API만)**:
- `get_daily_closes(symbol, exchange, days=5) → list[float]` (오래된 순) — 리버스 별지점 fallback
- `get_balance(symbol, exchange) → Balance | None`
- `get_purchase_amount(symbol, exchange) → PurchaseAmount` — 리버스 쿼터매수 기준
- `get_order_history(symbol, exchange, days=30) → list[dict]` — `ord_dt/ord_tmd/odno/ft_ccld_qty` 등 표준 필드. 빈 이력과 체결 후 이력 모두에서 파싱 검증
- `get_stock_price` / `get_stock_quotation` / `is_trading_day` — 시세/거래일

**주문 사전 테스트 (단일 계좌, 이력 최소화)**:
- 1순위: 비시장성 `BUY LIMIT 1주(현재가-5% 등)` 제출 → 당일 이력 매칭(`ord_dt/odno` 확인) → `cancel` → `ft_ccld_qty=0, remaining=0, terminal canceled` 재확인. `0체결 취소`는 T/`net_invested`에 영향 없음.
- 대체: `cancel` 미구현 시 비정상 주문(예: `99999주/$0.01`)으로 `OrderNotAcceptedError` 거부 확인만으로 대체. 성공 접수 경로는 모의 첫 주문으로 위임.
- 전량매도 복원(`매수 체결→다음날 매도해 잔고 0`)은 사전 필수 아님 — 모의 진입 후 보정으로 위임.
- 데모 `LOC/MOC→LIMIT` 변환은 어댑터 로그(`요청 타입 vs 실제 전송 타입`)로만 확인. 종가 체결 차이는 실전 소액 LIVE에서 확인.
- TOSS: 접수 직후 CLOSED 이력에 열린 주문이 잡히지 않음 → `odno` 매칭은 취소 후 이력에서 검증. 취소는 try/finally로 보장하며(실주문 방치 금지), 취소 실패 시 odno를 경고 출력해 수동 취소.

**실행**:
```bash
uv run python tests/test_broker_preflight.py --broker kis --symbol TQQQ
# 또는 pytest
uv run pytest tests/test_broker_preflight.py -v
```
- 자격증명이 설정된 브로커만 실행, 없으면 skip. 이력 오염을 막기 위해 비시장성 가격 사용.
- 통과 로그를 남기고 모의(`TRADE_MODE=DRY → LIVE demo`) 진입. 중간 운영 중 API 응답 변경은 `balance_mismatch`/`T 경고`/`STATE_REVERSE_AUDIT_ONLY`로 감지·보정.

**실전 소액 LIVE 1세션**: 별도 테스트가 아니라 전략의 첫 실전 주문 자체를 smoke 테스트로 봅니다. `LOC/MOC` 실전 접수 여부와 파이프라인 1회 정상 동작만 확인.

## COMMUNICATION RULES

- **질문과 수정 요청 구분**: 사용자가 물음표(?)로 끝내면 "질문"으로 간주한다.
  - 질문에는 **분석/답변만** 하고, 코드 수정을 하지 않는다.
  - 수정이 필요하면 사용자가 명시적으로 "수정해줘", "진행해줘", "적용해줘" 등으로 요청해야 한다.
  - 답변 중 수정이 필요하다고 판단되면 "수정할까요?"라고 먼저 물어본다.
- **검증 요청은 수정 아님**: "확인해줘", "맞는지 봐줘" 등은 검증만 수행하고 결과만 보고한다.

## NOTES
- GitHub Actions: `repository_dispatch`로만 트리거 (cron 없음)
- 기본 거래소: TQQQ(NAS), SOXL(AMS)
- KIS 모의투자는 초당 1회, 실전은 초당 20회 rate-limit
- LS 조회 TR(g3101 등)은 초당 1회, 주문 TR은 초당 10회 rate-limit (모의/실전 동일)
- 복리 재투자: `REINVEST` 기본 활성화 (해제 시 `false`)
- **시드 설정 필수**: 모든 종목에 `{SYMBOL}_SEED` 설정 필수 (달러 금액만 허용, FULL 미지원)
- **순투입(net_invested) 시드 캡** (`state.json`의 `net_invested`): 시드 캡은
  `remaining_seed = seed − net_invested` (net_invested = Σ 매수체결금액 − Σ 매도체결금액, USD) 기준.
  - 배경: 기존 `seed − position_qty×avg_price`(원가 기준)는 **평단 이하 매도(손절)** 시 매도 회수액이
    원가보다 작아 시드 여유가 부풀려져 누적 투입이 시드를 초과할 수 있었음
    (실제 사례: $50,000 시드 vs $56,299.66 사용).
  - 누적 경로: 일반모드 `_apply_recent_history_dt`(증분), 초기모드 `_infer_T_from_full_history`(재계산),
    리버스 `reconcile_reverse_fills`(델타). 전량매도(사이클 종료) 시 0.0 리셋.
  - 마이그레이션: `load_state`가 `net_invested` 키 없음 감지(`_net_invested_missing`) →
    `update_T_from_history`에서 `_compute_non_reverse_net_invested(cutoff_dt=last_updated)`로 1회 백필.
    cutoff는 `_apply`의 최근 윈도우와 상보적이어서 **이중 가산 없음**.
  - 리버스 주문은 `_compute_non_reverse_net_invested`에서 제외, reconcile이 델타 반영.
- **net_invested 신뢰성(`net_invested_status`)** — SOXL `net_invested=0.00` 손상 사고 대응:
  - 상태머신: `"valid"`(신뢰) | `"unresolved"`(신뢰 불가). 필드가 없으면 `unresolved`로 처리
    (기존/마이그레이션 상태는 자동 `unresolved` → **TQQQ도 1회 audit 필요**).
  - `unresolved`이고 `net_invested<=0`이면 `무한매수법_V4`가 **신규 전략 주문을 차단**합니다
    (`strategy.py` 게이트, `seed>0`일 때만). 기존 상태에 양수 `net_invested`가 있으면 status 누락만으로
    기존 운용을 차단하지 않습니다. reconciliation(체결 반영)은 이전처럼 계속 실행됩니다.
  - `valid` 자동 설정 지점: 신규 상태(파일 없음/종목 미등록), 사이클 종료·전량매도 리셋, `FORCE_T=0` 리셋,
    `STATE_NET_INVESTED_REPAIR_ONLY` 복구 성공 시.
  - 도구:
    - `STATE_REVERSE_AUDIT_ONLY=true`: 브로커 주문이력 vs state `orders_meta` 대조 (read-only,
      `state_hash` 출력, state 미저장).
    - `STATE_REVERSE_RECONCILE_ONLY=true`: 기존 reconciliation으로 미반영 체결만 state에 반영·저장
      (전략/주문 없음). `net_invested_status`는 승격하지 않음.
    - `STATE_NET_INVESTED_REPAIR_ONLY=true`: canonical `state_hash` + 필드 fingerprint(CAS)가
      일치할 때만 `net_invested`를 명시 값으로 복구하고 `valid` 전환. 미종결 리버스 주문/fence 있으면 거부.
      `STATE_NET_INVESTED_REPAIR_SYMBOL`/`_TARGET`/`_EXPECT_HASH`/`_EXPECT_NET_INVESTED`/`_EXPECT_STATUS` 필요.
    - `STATE_ASSUME_REVERSE_EXPIRY_ONLY=true`: **키움 모의 등 수동 강제 처리용**. 이전 미국 세션의
      zero-fill 리버스 주문을 `terminal_assumed=true`로 1회성 종결 처리한 뒤 reconcile/repair를
      진행할 때 사용합니다. `BROKER_MODE=demo`에서는 reconcile이 `_auto_assume_reverse_expiry()`
      (`src/state.py`)로 **자동** 처리하므로, 이 도구는 실전(real)/비데모, 부분체결·이력 누락 등
      자동 처리 예외 케이스에만 필요합니다. 실전/다른 브로커에서는 거부되며
      `STATE_ASSUME_REVERSE_EXPIRY_SYMBOL`/`_ORDER`/`_EXPECT_HASH` 필요.
  - `canonical_state_hash()` (`src/state.py`): `save_state()`가 기록하는 필드만 SHA-256 — 복구 CAS/audit용.
    네 복구 모드(`STATE_REVERSE_AUDIT_ONLY`/`RECONCILE_ONLY`/`NET_INVESTED_REPAIR_ONLY`/`ASSUME_REVERSE_EXPIRY_ONLY`)는 **동시 설정 금지**.
- **KIWOOM/LS/TOSS**: `BROKER_CONFIG`에 `account_no` 불필요 (AppKey/Secret만으로 API 호출 가능), **KIS/NHPLUG는 계좌번호 필수** (`KIS_ACCOUNT_NO` / `NHPLUG_ACCT_NO` 없으면 호출 금지)
- **주문 시각**: 모든 브로커에서 `get_kst_now()` 사용 (API 응답 시간 미사용)
- **KIS ODNO 정규화**: 주문 접수 API는 leading zero 10자리(`0000052248`), 체결 조회는 trimmed(`52248`) 반환 → `kis/adapter.py`에서 `str(int(odno))`로 정규화 후 반환 (다른 브로커는 해당 없음)
- **KIWOOM 모의투자 주문이력(ust21150)**: 날짜별 개별 조회, 빈 결과/`501724` 에러 시 `[정보]` 로그 출력 (line 534-545)
- **해외주식 주문이력 날짜/시각 컨벤션 (`ord_dt` vs KST)** — `kis/ls/kiwoom/toss` 어댑터의
  `[주문이력 요약]` 출력용 참고사항. 상태 체인(`ord_datetime_utc`/`last_updated`)은 **절대 수정하지 말 것**.
  - KIS/LS/KIWOOM(실전·모의) 해외주문 조회 응답의 `ord_dt`는 **미국(ET) 영업일**, `ord_tmd`는 **한국(KST) 시각**.
    봇은 KST 장중(대개 04:10경, 현지 시간으로는 전일 15:xx) 주문하므로 실제 KST 날짜 = `ord_dt+1`.
    → `[주문이력 요약]`은 `미국영업일 YYYY-MM-DD | 한국시각 YYYY-MM-DD HH:MM:SS KST | odno=…` 형태로 복원 표시.
  - **상태 체인 비변경 (display-only fix)**: `ord_datetime_kst_iso`/`ord_datetime_utc_iso`(상태 저장용)는
    기존 식(ord_dt를 그대로 KST로 해석 → +24h shift)을 유지. `last_updated`는 항상 `ord_datetime_utc`에서
    파생되므로 모든 시간 비교는 동일 convention 간 비교 → 순서·포함 여부 정확, **T 계산에 영향 없음**.
    (검증: `trading_bot.py`에 fill-vs-`now()` 비교 없음; `state.py:143`의 `datetime.now(UTC)`는
    `last_updated` 빈 값(초기 모드) fallback 전용.)
  - `resolve_real_kst_from_ord` (`broker/market_utils.py`): ord_dt(미국영업일)+ord_tmd(KST) → 실제 KST 복원.
    KST 후보를 ET로 round-trip해 날짜가 ord_dt와 일치하면 선택, 후보 없으면 offset=0(KST) fallback, 실패 시 None.
  - **브로커별 검증 상태**:
    - KIS: ✅ 실전 로그 검증 완료 (odno=45025: `20260731 041046` → 실제 KST `2026-08-01 04:10:46`).
    - TOSS: ✅ `orderedAt`이 timezone-aware ISO8601(`+09:00`) → 이미 정확한 KST → 보정 불필요.
    - KIWOOM 모의(ust21150): ✅ 실측 검증 완료 (2026-08-20 04:17 KST 제출 주문이 이력에
      `ord_dt=20260819`로 기록 확인) — **모의도 `ord_dt`는 미국영업일 컨벤션**.
      기존 "봇 루프가 KST 날짜 주입" 가설은 폐기, 실전과 동일하게 `resolve_real_kst_from_ord`
      표시 보정 적용 (`_normalize_ust21150_item` → `_ord_dt_is_us_trading_date=True`).
    - LS 실전 / KIWOOM 실전(ust21100): ⚠️ **미실증** — KIS·LS·KIOM 공통 해외주식 API 컨벤션에서
      `ord_dt=미국영업일`을 추정 중이나, CI가 실제 체결(fill)을 생산하지 않음.
      `resolve_real_kst_from_ord` 보정은 display-only이므로 안전하나, 실전 배포 전
      반드시 실전 API 응답으로 `ord_dt` 의미를 1회 검증할 것.
  - **리버스 reconciliation 날짜 매칭** (`state.py` `_order_real_kst_date`): `reconcile_reverse_fills._order_for_meta`와
    `_is_reverse_order`가 `ord_dt == submitted_at[:8]`로 **제출일(KST) vs 이력일(ET영업일)**을 비교하던 것을,
    `resolve_real_kst_from_ord(ord_dt, ord_tmd)`로 실제 KST 날짜를 복원해 비교하도록 수정.
    버그 사례(SOXL 모의 odno=000004615): 8/6 04:16 KST 제출(`submitted_at=20260806041650`)한 리버스 1일차
    MOC→LIMIT 매도가 이력에 `ord_dt=20260805`(ET영업일)로 기록 → 날짜 하루 차이로 "리버스 주문 이력 누락" 오판 →
    신규 주문 영구 차단 + 리버스 매도가 일반 쿼터매도(×0.75)로 오분류되어 T=15 오반영(정상 ×0.9 → 18).
    복구: `STATE_REPAIR_TARGET_T=20`으로 T만 보정 후 다음 RUN의 reconciliation이 재계산.
  - **데모 zero-fill 리버스 주문 자동 만료** (`state.py` `_auto_assume_reverse_expiry`):
    `BROKER_MODE=demo`에서는 이전 미국 세션의 zero-fill(이력 `ft_ccld_qty`=0 && `processed_filled_qty`=0
    && `processed_filled_amount`=0) 리버스 주문(매수/매도 모두)이 `reconcile_reverse_fills`에서
    자동으로 `terminal=true, terminal_assumed=true` 처리되어 "전일 리버스 주문 미종결 → 신규 주문 보류"
    동결이 발생하지 않습니다. 실전(real) 또는 부분체결·이력 누락 주문은 기존대로 보류 유지.
    늦은 체결은 odno 단위 `processed_filled_qty` 델타로 다음 RUN에 그대로 반영됩니다(상태 소실 없음).
- **ORDER_HISTORY_VERBOSE=true**: LIVE 모드에서도 `[주문이력 요약]` 상세 출력 (DRY는 항상 출력). KIS/KIWOOM/LS/TOSS 공통
- **텔레그램 재시도**: `send_telegram()`은 타임아웃/연결 오류/서버 5xx 시 최대 3회 재시도(1초 간격). 4xx(설정 오류 등)는 재시도 없이 즉시 실패
- **T 보정 환경변수**:
  - `FORCE_T_REINFERENCE=true`: `last_updated` 초기화 → 전체 이력(90일)에서 T 재추정 (LIVE→DRY 자동 전환)
  - `{SYMBOL}_FORCE_T={value}`: state.json의 T를 강제 덮어쓰기 (orders_meta/balance_mismatch 초기화, `last_updated`/`last_processed_ordno`를 이력 최신 주문 시각으로 갱신 → 이중 가산 방지, `FORCE_T=0`이면 `cycle_start_date` 초기화)
  - `{SYMBOL}_MAX_T={value}`: T 자동추정 결과의 상한선 (기본값: `{SYMBOL}_SPLITS` — 리버스모드 진입 위해 `T=splits` 허용, 초과만 방지)
  - FORCE_T_REINFERENCE 실행 시 small_seed_days(소액 시드 오추정)가 감지되면 포지션 기반 T 추정값이 함께 출력되며, 해당 값을 `{SYMBOL}_FORCE_T`로 설정하여 직접 보정 가능
  - ⚠️ `{SYMBOL}_FORCE_T`는 **1회성 점화용**입니다. 보정 RUN 1회 실행 후 env var를 **즉시 삭제**하세요. 리버스모드 진행은 `day_count`(state `reverse_mode`)와 체결 이력 기반 T 갱신이 주도하므로 FORCE_T 불필요. 유지하면 리버스모드 종료 → 새 사이클(T=0)에서 T를 다시 `splits`로 강제해 **새 사이클 매수가 영구 차단**됩니다.
  - ⚠️ GH Actions 캐시는 **브랜치별 격리**입니다. `develop` 수동 RUN에서 보정해도 `main`(repository_dispatch) RUN의 캐시에는 반영되지 않습니다. T 보정은 반드시 운영 브랜치(`main`)에서 실행하세요.
- **리버스모드** (`src/strategy.py execute_reverse_mode()`):
  - 발동: `T > splits - 1` + position > 0 (20분할 T>19, 40분할 T>39 — 원본 규칙: 마지막 1회 매수도 불가능한 상태)
  - 1일차: MOC 매도 (보유량 1/10(20분할) or 1/20(40분할)) → T × 0.9/0.95 (MOC 가격은 `adjust_price_to_tick()`으로 호가 단위 보정 후 전달)
  - 2일차+ (비소진): LOC 매도 @5일MA + 실제 주문가능금액 기준 쿼터매수 @별지점-0.01
  - 2일차+ (소진): 쿼터매수(잔금의 1/4)로 1개 매수가 불가능하면 매수 시도 없이 MOC 매도만 수행
  - 리버스모드 날짜는 실행당 1회만 증가하며, DRY 실행으로 저장 상태를 진행시키지 않음
  - 같은 실행에서 제출한 매도 주문의 예정 매도대금은 매수 가능금액으로 선반영하지 않음
  - **쿼터매수 잔금**: broker `orderable_cash`(주문가능금액)만 사용하며 `reverse_mode.cumulative_sell_proceeds`를 합산하지 않음 — **의도적 보수 설계** (정산 매도대금은 broker가 `orderable_cash`에 반영함을 전제, `test_reverse_mode.py:50-76`로 고정). 원본(매도금 누적 합산)과 달리 하락장에서 덜 매수 = 더 안전한 쪽
  - T 갱신은 실제 체결 이력 반영을 기준으로 하며, 전략의 T 계산값만으로 저장하지 않음
  - 종료 조건: 종가 > 평단×(1-0.15)(TQQQ) or ×(1-0.20)(SOXL) — 판정은 `last_price`(실시간/최신가) 기준. 런 타이밍: **실전=pre장**(직전 종가 근사), **모의=장 후반**(당일 종가 근사). pre-market이 라이브 시세를 반환하면 종가와 달라질 수 있어 broker별 1회 DRY 검증 필요
  - 별지점: Finnhub 5일 MA → close_prices(state) → last_price fallback (fallback 시 `[경고]` 로그 출력)
  - **TOSS MOC 변환**: 토스는 MOC(장마감시장가)를 지원하지 않음. 리버스 1일차 SELL MOC는 `toss/adapter.py place_order()`에서 LOC(장마감지정가) `$0.01`로 자동 변환되어 종가 체결 (MOC와 동일 동작, `_MOC_SELL_PROXY_PRICE`). 변환은 adapter 한 곳에서만 일어나므로 전략은 계속 `order_type="MOC"`를 생성하며, fence(`pending_order_intent/batch`)에는 원본 MOC/현재가가 기록됨 — fence 복구는 세션 날짜만 사용하므로 영향 없음. **BUY MOC는 변환하지 않음** ($0.01 매수 LOC는 체결 불가) → `OrderNotAcceptedError` 유지
  - **BUY LOC +19% 보정과 쿼터매수 상호작용**: `trading_bot.py`의 BUY LOC 가격 보정(`last_price × 1.19` 초과 시 `last×1.19`로 교정)이 리버스 쿼터매수(`star-0.01`, 별지점=5일MA)에도 적용됨. 하락장이 전제인 리버스에서는 별지점이 현재가보다 19% 이상 높아 **일반모드보다 훨씬 자주 발동** → notify 다발 + 매수가가 의도(별지점 대기)와 다른 `last×1.19`로 보정될 수 있음. 미해결 리스크로 기록 (기능 실패는 아님)
  - **데모 모드 MOC/LOC→LIMIT 변환**: KIS/KIWOOM 데모는 MOC/LOC/LOO/MOO를 `LIMIT`으로 자동 변환(LS는 `DEMO_UNSUPPORTED_ORDER_TYPES`가 비어 있어 변환 없음). 리버스 "마감 체결" 전제가 깨져 데모(장중 체결 가능)와 실전(마감 체결) 결과가 달라질 수 있음. T 반영은 체결 이력 기준이라 정합성은 유지
  - **실전 pre-market MOC/LOC 제출 미실증**: `trading_bot.py`는 실전에서 pre-market(ET ~04:00)까지 대기 후 주문. MOC/LOC(마감가 주문)는 통상 정규장에만 접수되므로 ET 04:00 제출 시 브로커가 거부/보류할 수 있음. KIS(33)/LS(M4)/KIWOOM(33) 실전의 "MOC+가격" 전송 및 TOSS CLS 주문의 pre-market 접수 여부는 **실전 배포 전 1회 검증 필요**
  - **TOSS `prerequisite-required` 실측**: `ETF 거래를 위한 요건이 부족합니다` — 신규 매수 주문에 걸리며 기존 보유와 무관(보유 중이어도 매수 차단). 투자성향(자금투자성향) 앱 등록으로 해소됨 확인 (교육 이수 아님). 해소 후 다음 관문은 `insufficient-buying-power`(주문가능금액 부족). 4xx 응답은 에러 envelope 선파싱 후 `OrderNotAcceptedError` 확정거부로 처리 (`toss/adapter.py _request_with_rate_retry()`)
- **STATE_DIAGNOSTIC_ONLY=true**: GitHub Actions 캐시의 T/reverse_mode 상태만 출력하고 브로커 API, 전략, 주문을 실행하지 않음. 일회성 진단 후 즉시 해제
- **STATE_REPAIR_ONLY=true**: 지정 fingerprint가 일치할 때만 API/전략/주문 없이 리버스 상태를 1회 초기화. `STATE_REPAIR_*` 변수는 실행 직후 삭제
  - `STATE_REPAIR_TARGET_T={value}` (선택): 기본 초기화 대신 **T만 보정**하고 리버스 cycle/orders_meta를 보존. ord_dt vs submitted_at 날짜 컨벤션 버그로 오반영된 T(예: 20→15)를 되돌려 다음 RUN의 `reconcile_reverse_fills`가 체결 기반으로 재계산(예: 20→18)하도록 하는 용도. fingerprint는 기존과 동일하게 필요
- **STATE_CLEAR_FENCE_ONLY=true**: 지정 fingerprint가 일치할 때만 API/전략/주문 없이 주문 fence(`pending_order_intent`/`pending_order_batch`)를 1회 초기화. `STATE_CLEAR_FENCE_SYMBOL` + `STATE_CLEAR_FENCE_EXPECT_T`/`EXPECT_LAST_UPDATED`/`EXPECT_INTENT`/`EXPECT_BATCH` 필요 (intent/batch는 빈 값 = "fence 없음" 의미, env var 존재만 필수). 실행 직후 변수 삭제
- **LIVE 주문 fence**: 주문 전 `pending_order_batch`/`pending_order_intent`를 캐시에 저장하며, fence가 남아 있어도 **전체 중단하지 않고** 해당 종목만 복구를 시도합니다 (다른 종목은 계속 진행)
  - **미접수 확정(`OrderNotAcceptedError`)**: 브로커가 명시적으로 거부(사전검증 실패 또는 거부 응답)해 주문이 접수되지 않았음이 보장되면 fence를 해제하고 해당 종목의 남은 주문만 중단 → 다음 종목 계속 진행
  - **불확실(네트워크 타임아웃/연결 오류, 주문번호 누락)**: fence 유지 → 해당 종목만 중단, 다음 종목은 계속 진행 (`SymbolScopedError`). **다음 RUN에서 자동 복구**를 시도합니다. state 저장 실패(intent/checkpoint)는 공통 장애로 전체 중단 (`GlobalScopedError`, 단일 `.state.json` 공유 때문)
  - **이전 세션 자동 복구** (`_recover_order_fence`, `trading_bot.py`): 주문이력/잔고 reconciliation(Step 1~3)을 정상 통과한 뒤, fence의 `submitted_session`(intent)/`session`(batch)이 오늘 미국(ET) 세션보다 과거면 이력이 정착된 것으로 보고 fence를 해제하고 정상 진행. 잔고 조회 실패(None) 또는 같은 세션/세션 정보 없음이면 보수적으로 fence 유지 + 해당 종목만 중단
  - fence 복구는 1일 1회 실행 + 당일 유효 주문(LOC/MOC/LIMIT) 특성상 전일 미확정 주문이 하루 뒤 이력으로 판별 가능하므로 안전합니다
  - `STATE_CLEAR_FENCE_ONLY`로 수동 해소 가능 (자동 복구가 안 되는 경우)
  - **collar 경계 probe** (`tests/test_broker_preflight.py`, `COLLAR_PROBES`): 시장성 probe(매수 상단 ×1.19/×1.21, 매도 하단 ×0.80)는 실전에서 실제 체결이 발생하므로 `COLLAR_PROBE_LIVE_FILL=true`일 때만 실행(기본 skip). 비시장성(매수 하단 ×0.90, 매도 상단 ×1.50)만 접수→이력 매칭→취소→0체결 확인. 매도 probe는 보유 수량 있을 때만 시도. KIS 운용 기준: 매수 현재가 −97%~+20%, 매도 −20%~+200% (현지 브로커별 변동, LS FAQ는 브로커별 10~20% 명시)
- **close_prices**: state.json에 최근 5거래일 종가 저장 (Finnhub fallback용)
- **`get_daily_closes()`** (`src/broker/base.py`):
  - Broker 추상 메서드: `(symbol, exchange, days=5) → list[float]` (오래된 종가순)
  - KIS: HHDFS76240000 → `GET /uapi/overseas-price/v1/quotations/dailyprice` (최신순 → 역순)
  - LS: g3204 → `POST /overseas-stock/market-data` (최신순 → 역순) + Finnhub candle fallback
  - KIWOOM: usa06012 → `POST /api/us/chart` (최신순 → 역순)
  - TOSS: `GET /api/v1/candles?interval=1d` (최신순 → `reversed()`)
  - 리버스모드 `_get_reverse_star_point()`에서 close_prices 보강용으로 사용 가능
