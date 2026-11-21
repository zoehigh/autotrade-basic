# PROJECT KNOWLEDGE BASE

**Generated:** 2026-06-25
**Updated:** 2026-08-08
**Branch:** `develop`

## OVERVIEW
미국 주식 자동매매 봇 (Python). 6개 증권사(KIS, KIWOOM, LS, TOSS, NHPLUG, MERITZ) API를 지원하며, 무한매수법 V4 전략을 실행. GitHub Actions에서 `repository_dispatch`로 트리거됨.

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
│   │   ├── meritz/      # 메리츠증권 (real 전용)
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
| 브로커 구현 | src/broker/{kis,kiwoom,ls,toss,nhplug,meritz}/ | 각 브로커별 API 어댑터 |
| 공통 인터페이스 | src/broker/base.py | `Broker`, `OrderResult`, `BrokerError`, `get_daily_closes()` |
| 일봉 종가 조회 | src/broker/base.py `get_daily_closes()` | TR: KIS=HHDFS76240000, LS=g3204, KIWOOM=usa06012, TOSS=GET /api/v1/candles, MERITZ=GET /market/v1/overseas/candles/days |
| 날짜 포함 일봉 | src/broker/base.py `get_daily_candles()` | `[{"date": "YYYY-MM-DD"\|None, "close": float}]` (오래된 순). 6개 브로커 모두 날짜 제공(`provides_daily_candle_dates=True`) — 리버스 종료 판정은 확정된 직전 XNYS 세션 종가만 사용 |
| 상태 파일 관리 | src/state.py | state.json 로드/저장, T 갱신, reverse_mode 상태 |
| 환경변수 설정 | src/config.py | 종목 설정, 모드, 수수료, T 보정 env var |
| 시세 데이터 | src/market_data.py | Finnhub 5일 이동평균 조회 |
| 파이프라인 흐름 | trading_bot.py | state→전략→주문→저장 전체 순서 |

## 상세 문서 — 작업 유형별로 필요한 것만 여세요

아래 문서는 **자동으로 로딩되지 않습니다**(`@` 가져오기·링크 자동 포함 없음). 해당 작업을 시작할 때 파일을 직접 열어 읽으세요.
안전 규칙은 이 파일(ANTI-PATTERNS, state 저장 원칙, 정산 핵심 규칙, COMMUNICATION RULES)에 남아 있으며, 세부 문서는 그 규칙을 대체하지 않고 보완합니다.

| 작업 유형 | 읽을 문서 |
|-----------|-----------|
| 브로커 어댑터·API 에러 코드·주문이력/시각 컨벤션·preflight·MERITZ 만료/정정·`get_daily_closes` | `docs/agents/brokers.md` |
| 체결 정산·T 갱신·`net_invested`·마커/델타·`ops/verify_run.py`·로그 주문번호 마스킹 세부 | `docs/agents/settlement.md` |
| 리버스모드·쿼터매수·추가매수·종료 조건·BUY LOC 가격 보정 | `docs/agents/reverse-mode.md` |
| state 복구·`TRADE_MODE=REPAIR`·`FORCE_T`·`MAX_T`·`STATE_*_ONLY`·복구 워크플로·T 보정 명령 (**이런 작업 전에는 반드시 먼저 읽기**) | `docs/agents/state-repair.md` + `STATE_*_ONLY` 사용 시 `docs/agents/settlement.md`(감사·reconcile·net_invested·만료 모드의 CAS/적용 조건/상호배타 규칙)도 함께 |
| 주문 접수·취소 로직 수정·주문 fence·이전 세션 잔여 주문 정리·취소 확정·collar probe | `docs/agents/orders.md` |
| 운영 절차(크론·사이클 인수인계 등) | 기존 `docs/` 문서(`docs/cron-dispatch-guide.md`, `docs/cycle-handover.md`, `docs/marker-correction-runbook.md`) |

한 작업이 여러 유형에 걸치면(예: 리버스 + 정산, 브로커 + 주문) 해당 문서를 **모두** 읽으세요.

위 표에 없는 일반 작업(코드 읽기·테스트 실행 등)은 이 파일만으로 충분합니다.

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
- `KIS_ACCOUNT_NO` / `NHPLUG_ACCT_NO` 없는 상태로 KIS/NHPLUG API 호출 금지 (KIWOOM/LS/TOSS/MERITZ는 계좌번호 불필요)
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
- **DRY 불일치/사이클 종료 처리**: DRY에서는 이력-잔고 불일치(`balance_mismatch`)가 감지돼도 **기록·자동보정·중단 없이 경고 + 텔레그램 1회(비긴급)만** 출력하고 프리뷰를 계속 진행(exit 0). 사이클 종료 감지 시 리포트만 표시하고 **캐시 저장(T 리셋/시드)을 생략**. LIVE는 기존대로 기록 + 중단(RuntimeError → fatal). `{SYMBOL}_FORCE_T`는 DRY에서 **적용하지 않고 오류로 중단**(저장 없음, T 보정은 `TRADE_MODE=REPAIR`), `FORCE_T_REINFERENCE`는 DRY에서 **미리보기만**(저장 없음)
- **실행 모드별 state 저장 원칙**: **state 복구·보정은 `TRADE_MODE=REPAIR`로 실행한다.**

  | 모드 | 목적 | state 저장 | Actions 새 캐시 저장 |
  |------|------|-----------|----------------------|
  | `DRY` | 전략·정산 미리보기 | 원칙적으로 금지 | **금지** |
  | `REPAIR` | 명시적 state 복구·보정 (주문 없음, **로컬 전용** — Actions 에서는 차단) | 허용 | 허용 (Actions 에서는 REPAIR 자체가 차단됨) |
  | `LIVE` | 실제 주문·정산 (**`{SYMBOL}_FORCE_T` 금지**) | 허용 | 허용 |

  - `{SYMBOL}_FORCE_T` + `TRADE_MODE=DRY` 또는 `LIVE` → state 저장·브로커 호출·주문 **없이 명시적 오류로 중단**(`sys.exit(1)`, 브로커 생성 전). **FORCE_T는 `TRADE_MODE=REPAIR`에서만 허용**. LIVE를 막는 이유: FORCE_T는 T·`orders_meta`·`reverse_mode`·정산 원장·워터마크를 덮어쓰는데, LIVE에서는 같은 실행에서 주문 단계까지 이어져 복구와 주문이 결합됨. `FORCE_T_REINFERENCE=true`로 LIVE가 DRY로 고정된 경우도 동일하게 차단
  - REPAIR 재추정(`_repair_reinference`): 재추정 T가 `{SYMBOL}_MAX_T`(기본 분할수)를 **초과하면 저장을 거부**하고 종료 코드 1 (T를 상한으로 **자르지 않음** — 자르면 정산 오류가 숨겨짐). 여러 종목은 **전부 재추정·검증한 뒤에만** 저장하므로 일부 종목만 먼저 저장되지 않음. T/MAX_T가 유한한 유효 숫자가 아니어도 거부. 일반 실행 경로(`run_one_symbol`)는 기존대로 상한으로 조정
  - ⚠️ **Actions의 `TRADE_MODE=REPAIR`는 차단됩니다** (`trade_base.yml` Resolve 단계, 봇 실행·state 변경·캐시 저장 전 `exit 1`). 이유: REPAIR 핸들러는 `settlement-*.json`(정산 근거)을 만들지 않아 `verify-settlement`가 "정산 근거 파일이 없습니다 — 검증 불가"로 항상 실패하는데, state 캐시는 검증 job보다 앞선 job에서 이미 저장되기 때문입니다. `repository_dispatch` payload·`workflow_dispatch` 입력·`vars.TRADE_MODE` 어디서 지정해도 동일하게 차단됩니다. **REPAIR 전용 독립 검증(후속 PR)이 준비되기 전까지 state 복구·보정은 로컬에서만** 실행하세요 (`docs/marker-correction-runbook.md` 절차)
  - ⚠️ 예외 — **레거시 `STATE_*_ONLY`는 DRY여도 대부분 state를 저장**합니다: 읽기 전용은 `STATE_DIAGNOSTIC_ONLY`·`STATE_REVERSE_AUDIT_ONLY`뿐이고, `STATE_REPAIR_ONLY`·`STATE_CLEAR_FENCE_ONLY`·`STATE_REVERSE_RECONCILE_ONLY`·`STATE_NET_INVESTED_REPAIR_ONLY`·`STATE_ASSUME_REVERSE_EXPIRY_ONLY`·`STATE_SET_CYCLE_START_ONLY`는 저장합니다. 따라서 이들은 **운영 사용을 제한**합니다(`TRADE_MODE=DRY`를 읽기 전용으로 오해하지 말 것 — 사용 전 백업·승인 필수, "확인용" 실행 금지). 후속 작업에서 REPAIR 전용으로 통합 예정
  - ⚠️ REPAIR라고 자동으로 안전한 것은 아닙니다. FORCE_T는 `orders_meta`·`reverse_mode`·정산 원장·워터마크까지 바꿉니다 → **실행 전 state 백업(저장소 밖) + 브로커 체결 이력 대조 + 작업자≠승인자 승인**은 그대로 유지 (`docs/marker-correction-runbook.md` §2~§4 참고)
  - (분리됨) `FORCE_T_REINFERENCE`·REPAIR 재추정 세부·Actions 캐시 저장 가드·Resolve 입력 처리 규칙 → `docs/agents/state-repair.md`
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

# state 복구·보정은 TRADE_MODE=REPAIR 로 실행합니다 (주문 없음). 실행 전 state 백업·체결 이력 대조·승인 필수.

# 테스트 실행
# 기본 `pytest tests/` 는 외부 브로커 API·주문을 **원천 차단**합니다 (유닛/모의 테스트만 실행).
#  - 외부 HTTP: conftest autouse fixture 가 `requests.Session.request` 를 막음
#  - 브로커 마커(kis/kiwoom/ls/toss/nhplug/meritz)·preflight·live_order 테스트: 기본 skip
#  - 실 API 통합 테스트: RUN_BROKER_API_TESTS=1 (opt-in, 자격증명도 필요)
#  - 주문이 발생하는 테스트(live_order): 위에 더해 ALLOW_BROKER_ORDERS=1 (추가 opt-in)
#  - 주문 함수 진입 직전에도 `_assert_order_allowed()` 가 ALLOW_BROKER_ORDERS 를 재검증 (CLI 포함)
#  - 테스트는 conftest autouse fixture 로 `state._STATE_FILE` 을 임시 경로로 격리하고, 세션 전후 저장소 `.state.json` 해시가 달라지면 세션을 실패 처리합니다
uv run pytest tests/ -v                                        # 안전: 유닛/모의만
RUN_BROKER_API_TESTS=1 uv run pytest tests/ -m kis -v          # 읽기 전용 통합 테스트
RUN_BROKER_API_TESTS=1 ALLOW_BROKER_ORDERS=1 uv run pytest tests/test_broker_preflight.py -v  # 주문 포함

```
T값 보정 명령 예시 → `docs/agents/state-repair.md`, 브로커별 필수 자격증명 → `docs/agents/brokers.md`.

## 정산 핵심 규칙 (공통 안전)

**공개 로그의 주문번호는 가명 식별자로만 출력**: 정산·체결 복구·보류 경로(`src/state.py`, `trading_bot.py`)의 stdout·예외·알림과
검증기(`ops/verify_run.py`) 실패 메시지는 `mask_odno()` = `ord#` + HMAC-SHA256(**실행별 무작위 salt**, 주문번호) 앞 10자리만 씁니다
(단순 해시는 짧은 숫자 주문번호가 전수 대입으로 복원됨). salt(`log_salt`)와 원문 주문번호는 **비공개(암호화) Evidence** 에만 남습니다.
정적 가드 `tests/test_odno_log_masking.py`. 일반 주문 접수·취소·브로커 어댑터 로그는 미적용 → `docs/odno-log-masking-followup.md`.
**Evidence artifact 는 gpg 암호화본만 업로드**합니다 (tar→gpg 스트리밍, 실패 시 부분 암호문·평문 tgz 잔존 없음, 패스프레이즈 없으면 업로드 없이 실패).

**불확실한 정산은 어떤 저장 경로에서도 확정하지 않습니다**: `_save_settlement_state()` 가
`state._position_basis_unknown` 를 확인해 저장과 신규 주문을 함께 막습니다 (reconcile-only 경로 포함).
먼저 저장하면 다음 실행에서 재처리될 수 없어 잘못된 값이 고정되기 때문입니다.

세부 정산 규칙(매도 비율 기준선·체결 델타·지연 체결·검증기·전체 재추정) → `docs/agents/settlement.md`.

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
- 브로커·이력·fence·취소·리버스·복구 세부 NOTES는 위 `상세 문서` 표의 파일로 옮겨졌습니다(내용 동일).
