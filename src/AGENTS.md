# src/ — 핵심 모듈

## OVERVIEW
KIS Open API 호출, 무한매수법 V4 전략 로직, 상태 관리 등 자동매매 봇의 모든 핵심 기능 구현.

## WHERE TO LOOK
| Task | File | Notes |
|------|------|-------|
| 전략 이해/수정 | strategy.py | `무한매수법_V4()` — 별지점, 전/후반전, 추가매수 LOC |
| KIS API 호출 | trader.py | `place_overseas_order()`, `get_overseas_balance()`, `get_overseas_order_history()` |
| state.json I/O | state.py | `load_state()`, `save_state()`, `update_T_from_history()` |
| 설정 파싱 | config.py | `_parse_symbols()` — SYMBOLS 환경변수 → 종목 설정 dict |
| OAuth2 토큰 | authentication.py | `get_access_token()` — 캐싱, 재시도 로직 포함 |
| Telegram 발송 | telegram.py | `send_telegram()` — 텔레그램 봇 API 호출 |
| 알림 라우팅 | notifier.py | `notify()` — 조용한 시간 제어, 긴급 메시지 즉시 전송 |
| HTTP 세션 | kis_session.py | `KISSession` — 공통 헤더, verify, timeout 설정 |

## CONVENTIONS (src 전용)
- **trader.py**: `_request_with_rate_retry()` 래퍼로 모든 API 호출 — 재시도/rate-limit 처리 통일
- **config.py**: `_parse_symbols()`는 모듈 로드 시점에 실행되어 `SYMBOLS` 전역 상수 생성
- **authentication.py**: `_cached_token` 전역 변수로 토큰 캐싱 (프로세스 생명주기)
- **strategy.py**: 유니코드 함수 `무한매수법_V4()`만 전략 함수, 나머지는 영어 snake_case
- **trader.py**: DRY 모드에서 주문 정보만 print, LIVE 모드에서만 실제 API POST

## ANTI-PATTERNS
- `from config import ...` 지연 임포트 (trader.py 일부 함수에서 순환참조 방지용)
- API 키/시크릿을 소스코드에 하드코딩 금지 — 반드시 `.env` 또는 환경변수 사용
- `_request_with_rate_retry()` 우회 금지 — 모든 API 호출은 이 래퍼를 통해야 rate-limit 안전
- `__pycache__/` — 절대 커밋 금지 (`.gitignore`에 있음)
- `.state.json` — 커밋 금지 (GH Actions 캐시로만 관리)
- `KIS_ACCOUNT_NO` 없는 상태로 KIS API 호출 금지 (KIWOOM/LS/TOSS는 계좌번호 불필요)
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (trader.py)
- `TRADE_MODE` 무단 LIVE 전환 금지 (DRY 먼저 확인)
- `.venv` 의존성 직접 수정 금지 — 항상 `uv` 사용

## NOTES
- 시드(seed) 설정은 필수: 모든 종목에 `{SYMBOL}_SEED` 달러 금액만 허용
- FULL은 지원하지 않음 (계좌 잔고 실시간 변동으로 예측 불가능)
