# src/ — 핵심 모듈

## OVERVIEW
KIS Open API 호출, 무한매수법 V4 전략 로직, 상태 관리 등 자동매매 봇의 모든 핵심 기능 구현.

## WHERE TO LOOK
| Task | File | Notes |
|------|------|-------|
| 전략 이해/수정 | strategy.py | `무한매수법_V4()` — 별지점, 전/후반전, 추가매수 LOC |
| 브로커 구현 | broker/{kis,kiwoom,ls,toss,nhplug,meritz}/ | 각 브로커별 API 어댑터 |
| 공통 인터페이스 | broker/base.py | `Broker`, `OrderResult`, `BrokerError`, `OrderNotAcceptedError` |
| 일봉 종가 조회 | broker/base.py `get_daily_closes()` | TR: KIS=HHDFS76240000, LS=g3204, KIWOOM=usa06012, TOSS=GET /api/v1/candles |
| state.json I/O | state.py | `load_state()`, `save_state()`, `update_T_from_history()` |
| 설정 파싱 | config.py | `_parse_symbols()` — SYMBOLS 환경변수 → 종목 설정 dict |
| Telegram 발송 | telegram.py | `send_telegram()` — 텔레그램 봇 API 호출 |
| 알림 라우팅 | notifier.py | `notify()` — 조용한 시간 제어, 긴급 메시지 즉시 전송 |
| 시간/시장 유틸 | broker/market_utils.py | `get_kst_now()`, `is_us_trading_day()`, `normalize_order_price()` |

## CONVENTIONS (src 전용)
- **broker/{kis,kiwoom,ls,toss,nhplug,meritz}/**: 각 브로커별 `_request_with_rate_retry()` 래퍼 — 재시도/rate-limit 처리 (meritz는 `_request()` + `_check_rsp_cd()`)
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
| NHPLUG | `rsp_cd in {"00000","00166","00221","13578","XA102","00048"} or "완료" in rsp_msg/usr_msg` | `rsp_cd`/`msg_code` | `rsp_msg`/`usr_msg` | `_request_with_rate_retry()` |
| MERITZ | 주문 `rsp_cd=="0001"` AND `data.warn_cls_code==0`, 조회 계열별 상이 (시세 `0000` / 잔고·매수가능 `5766` / 이력·당일 `5766`·`5762`(다음페이지) / 취소 `0001`) — 5820은 data 유무로 판단, EGW00200은 조회만 제한 재시도 | `rsp_cd` | `rsp_msg`+`rsp_sub_msg` | `_check_rsp_cd()` |

**참고**: 주문 시각은 모든 브로커에서 `get_kst_now()` 사용 (API 응답 시간 미사용)
- **MERITZ**: real 전용(모의 없음) — 생성자에서 `BROKER_MODE != real` 즉시 실패. 계좌번호 불필요. 주문번호는 합성 `YYYYMMDD:제로패딩주문번호`. 주문이력 `oder_exec_ymdh/cntg_ymdh`는 KST 그대로 파싱(`_ord_dt_is_kst_date=True`). 주문 POST 네트워크 재시도 금지 (EGW00121/123 토큰 1회 재발급 재시도만 허용). 경고 확인(`warn_cnfr_yn=A`)은 `MERITZ_CONFIRM_WARNINGS`에 명시된 등급만 1회, 기본 OFF.
- **MERITZ 10/12 신청 게이트 (2026-10-02 기준)**: 모바일앱 신청 차단 + 포털 미개방 → 실검증 보류. 유동IP 사용 가능 확인됨 (고정IP 불필요). MAC 사전등록도 별도 절차 없음 (요청 헤더에 12자리 HEX를 넣어 전송하면 됨). preflight 주문 단계는 `--live-order-probe`로만 실행.
- **`OrderNotAcceptedError`** (`broker/base.py`): 주문이 **확정적으로 미접수**됨이 보장되는 경우만 사용 — 사전검증 실패, 브로커 거부 응답(`rt_cd`/`return_code`/`rsp_cd`/`error` envelope). 네트워크 타임아웃/연결 오류 등 접수 여부가 불확실하면 기존 `OrderError` 유지. trading_bot.py는 미접수 확정 시 fence 해제 후 다음 종목 진행, 불확실 시 fence 유지 + 해당 종목만 중단하고 다음 종목 계속(`SymbolScopedError`) + 종목별 복구(`_recover_order_fence`, 다음 RUN에서 이전 세션 이력 정착 시 자동 해제). state 저장 실패는 공통 장애로 전체 중단(`GlobalScopedError`)
- **주문가 정규화**: 각 브로커 `place_order()`는 `normalize_order_price()`(broker/market_utils.py)로 호가 단위 규칙($1+ → 소수점 2자리, $1 미만 → 4자리, 버림)에 맞춰 가격을 정규화 후 전송

## ANTI-PATTERNS
- `from config import ...` 지연 임포트 (일부 함수에서 순환참조 방지용)
- API 키/시크릿을 소스코드에 하드코딩 금지 — 반드시 `.env` 또는 환경변수 사용
- `_request_with_rate_retry()` 우회 금지 — 모든 API 호출은 이 래퍼를 통해야 rate-limit 안전
- `__pycache__/` — 절대 커밋 금지 (`.gitignore`에 있음)
- `.state.json` — 커밋 금지 (GH Actions 캐시로만 관리)
- `KIS_ACCOUNT_NO` / `NHPLUG_ACCT_NO` 없는 상태로 KIS/NHPLUG API 호출 금지 (KIWOOM/LS/TOSS/MERITZ는 계좌번호 불필요)
- 모의투자 미지원 주문 유형(LOC/LOO/MOC/MOO) → 자동 LIMIT 변환 (broker별 adapter)
- `TRADE_MODE` 무단 LIVE 전환 금지 (DRY 먼저 확인)
- `.venv` 의존성 직접 수정 금지 — 항상 `uv` 사용

## NOTES
- 시드(seed) 설정은 필수: 모든 종목에 `{SYMBOL}_SEED` 달러 금액만 허용
- FULL은 지원하지 않음 (계좌 잔고 실시간 변동으로 예측 불가능)
- **텔레그램 재시도**: `send_telegram()`은 타임아웃/연결 오류/서버 5xx 시 최대 3회 재시도(1초 간격). 4xx(설정 오류 등)는 재시도 없이 즉시 실패
