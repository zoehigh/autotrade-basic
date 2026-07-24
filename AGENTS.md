# PROJECT KNOWLEDGE BASE

**Generated:** 2026-06-25
**Updated:** 2026-07-24
**Branch:** `develop`

## OVERVIEW
미국 주식 자동매매 봇 (Python). 4개 증권사(KIS, KIWOOM, LS, TOSS) API를 지원하며, 무한매수법 V4 전략을 실행. GitHub Actions에서 `repository_dispatch`로 트리거됨.

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
│   │   └── market_utils.py  # 시간/시장 유틸리티
│   ├── strategy.py      # 전략 로직
│   ├── state.py         # 상태 관리
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
| 전략 로직 이해 | src/strategy.py | `무한매수법_V4()`, T값/별지점 계산 |
| 브로커 구현 | src/broker/{kis,kiwoom,ls,toss}/ | 각 브로커별 API 어댑터 |
| 공통 인터페이스 | src/broker/base.py | `Broker`, `OrderResult`, `BrokerError` |
| 상태 파일 관리 | src/state.py | state.json 로드/저장, T 갱신 |
| 환경변수 설정 | src/config.py | 종목 설정, 모드, 수수료 |
| 파이프라인 흐름 | trading_bot.py | state→전략→주문→저장 전체 순서 |

## BROKER ERROR HANDLING
| 브로커 | 성공 코드 | 에러 코드 | 에러 메시지 | 검증 위치 |
|--------|----------|----------|------------|----------|
| KIS | `rt_cd == "0"` | `msg_cd` | `msg1` | 각 메서드 |
| KIWOOM | `return_code == 0` | `return_code` | `return_msg` | `_check_response()` |
| LS | `rsp_cd == "00000"` | `rsp_cd` | `rsp_msg` | 각 메서드 |
| TOSS | `error` 없음 | `error.code` | `error.message` | `_request_with_rate_retry()` |

**참고**: 주문 시각은 모든 브로커에서 `get_kst_now()` 로컬 시간 사용 (API 응답 시간 미사용)

## CONVENTIONS
- **패키지 의존성**: `requests`, `python-dotenv`, `exchange-calendars` (`uv` 관리)
- **실행**: `uv run python trading_bot.py` / `uv run python tests/test_dryrun.py`
- **환경변수**: `.env` 파일, 대문자 snake_case (예: `KIS_APP_KEY`, `TRADE_MODE`)
- **KoCra v4**: 변수/함수명은 영어, 주석/로그는 한국어
- **한 함수 한 역할**: 함수당 단일 책임 원칙
- **김작가님 규칙**: 초보자 가독성 우선, 과도한 추상화 금지

## ANTI-PATTERNS (THIS PROJECT)
- `__pycache__/` — 절대 커밋 금지 (`.gitignore`에 있음)
- `.state.json` — 커밋 금지 (GH Actions 캐시로만 관리)
- `KIS_ACCOUNT_NO` 없는 상태로 KIS API 호출 금지 (KIWOOM/LS/TOSS는 계좌번호 불필요)
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (broker별 adapter)
- `TRADE_MODE` 무단 LIVE 전환 금지 (DRY 먼저 확인)
- `.venv` 의존성 직접 수정 금지 — 항상 `uv` 사용

## UNIQUE STYLES
- 유니코드 함수명 `무한매수법_V4()` — 전략 함수만 한글명
- T값(float)이 누적 매수 횟수를 나타내는 독특한 상태 관리
- 모의/실전 TR_ID를 KIS_MODE에 따라 동적 전환
- DRY 모드에서는 주문 출력만 하고 실행하지 않음

## COMMANDS
```bash
# 전략 what-if 검증 (API/상태 미접촉)
uv run python tests/test_dryrun.py

# DRY 모드 실행
TRADE_MODE=DRY uv run python trading_bot.py

# LIVE 모드 실행 (실제 주문)
TRADE_MODE=LIVE uv run python trading_bot.py

# 테스트 실행
uv run pytest tests/ -v
```

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
- **KIWOOM/LS/TOSS**: `BROKER_CONFIG`에 `account_no` 불필요 (AppKey/Secret만으로 API 호출 가능)
- **주문 시각**: 모든 브로커에서 `get_kst_now()` 사용 (API 응답 시간 미사용)
