# PROJECT KNOWLEDGE BASE

**Generated:** 2026-06-25
**Commit:** `dcbe118`
**Branch:** `develop`

## OVERVIEW
미국 주식 자동매매 봇 (Python). KIS(한국투자증권) Open API로 무한매수법 V4 전략을 실행. GitHub Actions에서 `repository_dispatch`로 트리거됨.

## STRUCTURE
```
autotrade-basic/
├── src/             # 핵심 모듈 (전략, API 호출, 상태 관리)
├── tests/           # pytest 기반 테스트 스크립트
├── .github/workflows/  # GitHub Actions (trade_real, trade_demo, trade_base)
└── trading_bot.py   # 메인 파이프라인 진입점
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| 전략 로직 이해 | src/strategy.py | 무한매수법_V4(), T값/별지점 계산 |
| API 주문/잔고 호출 | src/trader.py | KIS REST API 래퍼 전부 |
| 상태 파일 관리 | src/state.py | state.json 로드/저장, T 갱신 |
| 환경변수 설정 | src/config.py | 종목 설정, 모드, 수수료 |
| 인증 토큰 | src/authentication.py | OAuth2 토큰 발급/캐싱 |
| 파이프라인 흐름 | trading_bot.py | state→전략→주문→저장 전체 순서 |
| CI/CD 설정 | .github/workflows/ | `trade_real.yml`, `trade_demo.yml` |

## CODE MAP
| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `무한매수법_V4()` | func | src/strategy.py | V4 전략 실행 (매수/매도 주문 생성) |
| `calculate_star_point()` | func | src/strategy.py | 별지점 가격 계산 |
| `adjust_price_to_tick()` | func | src/strategy.py | 호가 단위 보정 |
| `place_overseas_order()` | func | src/trader.py | 실제/DRY 주문 실행 |
| `get_overseas_balance()` | func | src/trader.py | 보유 잔고 조회 |
| `get_overseas_order_history()` | func | src/trader.py | 체결 이력 조회 (연속조회) |
| `get_overseas_stock_price()` | func | src/trader.py | 현재가 조회 |
| `get_access_token()` | func | src/authentication.py | OAuth2 토큰 발급/캐싱 |
| `load_state()`/`save_state()` | funcs | src/state.py | state.json I/O |
| `update_T_from_history()` | func | src/state.py | 체결 이력 기반 T 갱신 |
| `send_telegram()` | func | src/telegram.py | 텔레그램 발송 |
| `notify()` | func | src/notifier.py | 알림 발송 (조용한 시간 제어) |
| `KISSession` | class | src/kis_session.py | HTTP 세션 관리 |
| `generate_cycle_report()` | func | trading_bot.py | 사이클 종료 리포트 생성 |

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
- `KIS_ACCOUNT_NO` 없는 상태로 API 호출 금지
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (trader.py)
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
- 복리 재투자: `REINVEST` 기본 활성화 (해제 시 `false`)
