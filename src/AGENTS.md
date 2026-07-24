# src/ — 핵심 모듈

## OVERVIEW
KIS Open API 호출, 무한매수법 V4 전략 로직, 상태 관리 등 자동매매 봇의 모든 핵심 기능 구현.

## WHERE TO LOOK
| Task | File | Notes |
|------|------|-------|
| 전략 이해/수정 | strategy.py | `무한매수법_V4()` — 별지점, 전/후반전, 추가매수 LOC |
| 브로커 구현 | broker/{kis,kiwoom,ls,toss}/ | 각 브로커별 API 어댑터 |
| 공통 인터페이스 | broker/base.py | `Broker`, `OrderResult`, `BrokerError` |
| state.json I/O | state.py | `load_state()`, `save_state()`, `update_T_from_history()` |
| 설정 파싱 | config.py | `_parse_symbols()` — SYMBOLS 환경변수 → 종목 설정 dict |
| Telegram 발송 | telegram.py | `send_telegram()` — 텔레그램 봇 API 호출 |
| 알림 라우팅 | notifier.py | `notify()` — 조용한 시간 제어, 긴급 메시지 즉시 전송 |
| 시간/시장 유틸 | broker/market_utils.py | `get_kst_now()`, `is_us_trading_day()` |

## CONVENTIONS (src 전용)
- **broker/{kis,kiwoom,ls,toss}/**: 각 브로커별 `_request_with_rate_retry()` 래퍼 — 재시도/rate-limit 처리
- **config.py**: `_parse_symbols()`는 모듈 로드 시점에 실행되어 `SYMBOLS` 전역 상수 생성
- **strategy.py**: 유니코드 함수 `무한매수법_V4()`만 전략 함수, 나머지는 영어 snake_case
- **broker/adapter.py**: DRY 모드에서 주문 정보만 print, LIVE 모드에서만 실제 API POST

## BROKER ERROR HANDLING
| 브로커 | 성공 코드 | 에러 코드 | 에러 메시지 | 검증 위치 |
|--------|----------|----------|------------|----------|
| KIS | `rt_cd == "0"` | `msg_cd` | `msg1` | 각 메서드 |
| KIWOOM | `return_code == 0` | `return_code` | `return_msg` | `_check_response()` |
| LS | `rsp_cd == "00000"` | `rsp_cd` | `rsp_msg` | 각 메서드 |
| TOSS | `error` 없음 | `error.code` | `error.message` | `_request_with_rate_retry()` |

**참고**: 주문 시각은 모든 브로커에서 `get_kst_now()` 사용 (API 응답 시간 미사용)

## ANTI-PATTERNS
- `from config import ...` 지연 임포트 (일부 함수에서 순환참조 방지용)
- API 키/시크릿을 소스코드에 하드코딩 금지 — 반드시 `.env` 또는 환경변수 사용
- `_request_with_rate_retry()` 우회 금지 — 모든 API 호출은 이 래퍼를 통해야 rate-limit 안전
- `__pycache__/` — 절대 커밋 금지 (`.gitignore`에 있음)
- `.state.json` — 커밋 금지 (GH Actions 캐시로만 관리)
- `KIS_ACCOUNT_NO` 없는 상태로 KIS API 호출 금지 (KIWOOM/LS/TOSS는 계좌번호 불필요)
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (broker별 adapter)
- `TRADE_MODE` 무단 LIVE 전환 금지 (DRY 먼저 확인)
- `.venv` 의존성 직접 수정 금지 — 항상 `uv` 사용

## NOTES
- 시드(seed) 설정은 필수: 모든 종목에 `{SYMBOL}_SEED` 달러 금액만 허용
- FULL은 지원하지 않음 (계좌 잔고 실시간 변동으로 예측 불가능)
