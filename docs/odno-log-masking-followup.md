# 주문번호 로그 마스킹 — 후속 작업 기록

PR #1(A안)은 **정산·체결 복구·보류 경로**의 공개 출력(stdout / 예외 메시지 / 알림 / 검증기 메시지)만 마스킹했습니다.
저장소가 PUBLIC 이라 GitHub Actions 로그에 남은 주문번호는 공개됩니다. 아래는 **이번 PR 범위 밖**이라 남긴 항목입니다
(별도 PR 에서 처리).

## 이번 PR 에서 한 것

- 가명 식별자: `ord#` + HMAC-SHA256(salt, 주문번호) 앞 10자리. **salt 는 실행(프로세스)마다 무작위**
  → 짧은 숫자 주문번호를 전수 대입으로 복원할 수 없고, 다른 실행의 로그와 연결되지도 않습니다.
- salt 는 공개 로그에 출력하지 않고 **비공개(암호화) Evidence** 의 `log_salt` 에만 기록합니다.
  Evidence 는 독립 검증을 위해 **원문 주문번호를 유지**합니다.
- 로그의 `ord#…` 를 원문으로 되돌리려면 복호화한 Evidence 의 `log_salt` 로 후보 주문번호를 HMAC 해 대조합니다
  (`ops/verify_run.py` `_independent_odno_hash(odno, salt_hex)` 와 동일).
- 검증기(`verify_evidence_file`)의 실패 메시지는 Evidence 안의 모든 주문번호를 같은 방식으로 치환해 반환합니다.
- 회귀 방지: `tests/test_odno_log_masking.py` 의 정적 가드가 `src/state.py`, `trading_bot.py` 의
  `print`/`notify`/예외 f-string 에 원문 주문번호가 들어가면 실패시킵니다.

## 후속 PR 대상 (미처리)

| 위치 | 내용 |
|------|------|
| `src/state.py` `register_order_meta_in_state` | `[orders_meta 등록] odno=… (원본=…)` — 일반 주문 접수 등록 로그 |
| `trading_bot.py` `_cancel_open_bot_orders` | 이전 세션 잔여 주문 취소 목록·실패 로그 |
| `src/broker/base.py` | DRY 취소 무시 로그 |
| `src/broker/kis/adapter.py` | `[주문이력 요약]` 출력 (`odno=…`) |
| `src/broker/kiwoom/adapter.py`, `ls/adapter.py`, `nhplug/adapter.py` | 주문이력·접수 로그 |
| `src/broker/toss/adapter.py` | 주문 취소 성공/이미 취소됨 로그 (4곳) |
| `src/broker/meritz/adapter.py` | 취소 성공 로그, `OrderNotAcceptedError` 메시지 (예외 4곳) |
| `ops/verify_run.py` 하단 CLI(`main` 이후 조회 유틸) | 브로커 이력 조회 결과 출력 (`r.get('odno')`) |

## 후속 PR 설계 메모

- 운영자가 브로커 앱에서 주문을 찾아야 하는 **접수·취소 로그**는 마스킹하면 추적이 어려워집니다.
  접수 시점에는 마스킹 토큰을 함께 출력하고, 원문 조회는 비공개 Evidence 로 하도록 절차를 정한 뒤 적용하세요.
- 어댑터 단은 `mask_odno` 를 `src/state.py` 에서 import 하면 순환 의존이 생길 수 있으므로
  `src/broker/base.py` 또는 별도 유틸 모듈로 옮기는 것을 검토하세요 (salt 는 모듈 전역 하나만 유지).
- 같은 정적 가드(`tests/test_odno_log_masking.py`)를 어댑터 파일에도 확대 적용하세요.
