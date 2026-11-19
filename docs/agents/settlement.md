# 정산·T 갱신·순투입·검증 상세 규칙

> 이 문서는 `AGENTS.md`에서 **내용 변경 없이 그대로 옮긴** 상세 규칙입니다(문서 구조 최적화). 자동으로 로딩되지 않으므로 해당 작업을 할 때만 여세요. 공통 안전 규칙·핵심 운영 정책은 `AGENTS.md`에 남아 있습니다.

## 체결 정산 규칙 (원문: PREFLIGHT 절 하위 단락)

**매도 비율 판정의 보유수량 기준 (2026-10-22 수정)**: 쿼터매도/목표매도/전량매도를 구분하는 비율은
`sell_qty / net_qty` 인데, `net_qty` 를 **주문 이력 조회 구간**에서 계산하면 구간 밖 매수(사이클 이전)가
빠져 **보유를 과소 계산**합니다 → 쿼터매도(25%)가 목표매도(75%)로 오분류되고 T가 1/4배 됩니다.
→ **보유수량 기준선은 3단계로 확보**합니다 (`state.py` `_apply_recent_history_dt`).
  1. **브로커 실제 잔고** 기준 (`balance_qty - 이번 묶음 순변동) — 가장 정확
  2. **state 의 `position_qty`** (워터마크 기준 보유량, 매 실행마다 기록) — 잔고 조회 실패 시
  3. 이력 계산값 — 마지막 폴백
→ **두 기준의 시점이 다릅니다**: 잔고는 "지금"의 보유량이라 이번 묶음 순변동을 빼야 하고,
  `position_qty` 는 "워터마크 시점" 보유량이라 **다시 되감지 않습니다**.
→ 기준선에 브로커 잔고를 쓰면 **리버스 체결이 이미 반영**되어 있으므로 별도로 다시 차감하지 않습니다.
→ 기준선을 끝내 확보하지 못하면(잔고 조회 실패 + 저장값 없음) 매도 분류 후 **정산 저장과 신규 주문을
  모두 보류**합니다 (`state._position_basis_unknown` → `trading_bot` 이 해당 종목을 중단).
  먼저 저장하면 다음 실행에서 재처리될 수 없어 잘못된 T 가 고정되기 때문입니다.
실측(2026-10-22): 실제 81주 → 이력 계산 30주 → 20주 매도가 비율 0.67 로 목표매도 오분류 → T 13.375→3.3438.

**체결 반영은 수량·금액 델타로만** (`processed_filled_qty` + `processed_filled_amount`):
새로 늘어난 부분만 반영하므로 부분체결이 나중에 늘어나도 중복 가산되지 않습니다.
이전 버전 메타에는 반영 금액 표식이 없으므로, **신규분만** 누적금액 비율로 추정해 1회 적용하고 표식을 기록합니다.
신규 주문은 등록 시 `processed_filled_qty=0` + `processed_filled_amount=0.0` + `fill_marker_version=1` 을 함께 기록합니다.
워터마크 이전 주문의 0 마커는 **이 버전 표식이 있을 때만** 미반영으로 신뢰하고(`_marker_proves_unapplied`),
기존 state 의 0 마커(버전 표식 없음)는 미반영의 증거가 아니므로 **반영하지 않고 건너뜁니다**(종목 전체를 보류하지 않아 같은 실행의 신규 체결은 정상 정산). 기존 메타는 일괄 마이그레이션 금지.
마커가 누락·무효인 주문은 자동 반영하지 않고 보류합니다 → 수동 정정 절차는 `docs/marker-correction-runbook.md`
(마커만 명시 설정, T/net/원장·워터마크 불변, 백업+해시, 직후 reconcile 1회 확인, 환경변수 1회성).


**지연 체결(부분체결 증가) 반영**: 전역 워터마크보다 오래된 주문이라도 **이미 반영된 주문**(processed_filled_qty > 0)의
누적 체결이 늘면 주문번호별 델타로 반영합니다 (`_apply_late_fill_deltas`). 워터마크 이전에 있고 반영된 적 없는
주문은 "처리된 범위 밖"이므로 건드리지 않습니다.

**매도 T 효과는 누적 체결 기준으로 판정** (`sell_t_effects` 원장): 주문별 기준선(보유량)·적용 배율을 기록하고,
누적 비율이 바뀌면 `T × (새 배율 ÷ 기존 배율)` 로 재조정합니다. 그래야 **일괄 반영(75주 1회)과
부분 반영(5주 → 70주)이 같은 최종 T 를 냅니다** (10/22 실측: 2.0 vs 6.0 로 갈라졌던 문제).
적용 금액은 실제 체결 증가분만 차감하고, 누적체결금액이 역행하면 차감하지 않습니다(반복 차감 방지).


**자동 검증은 독립 재계산** (`ops/verify_run.py`): 정산이 주문별로 적용한 내역(수량·금액·기준선·배율·T변화)을
`settlement-*.json` 에 기록하고 (`run_id` 로 실행 구분), 검증기는
(1) 기준선을 보유량에서 **독립 계산**해 대조, (2) 매도 분류(배율)를 기준선에서 독립 계산해 적용 배율과 대조,
(3) T 변화·순투입금을 델타로 재계산해 실제 결과와 대조합니다.
근거 파일이 없거나 실행이 다르면 **"검증 불가"로 실패 처리**합니다 (조용히 통과시키지 않습니다).

**전체 재추정과 증분 반영 분리**: `_infer_T_from_full_history` 는 "처음부터" 다시 계산하므로
`processed_filled_qty` 를 빼지 않고 전체 체결로 T 를 계산합니다 (빼면 T 가 다시 낮아집니다).


## 순투입(net_invested)·신뢰성·복구 도구 (원문: NOTES)

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
    `STATE_SET_CYCLE_START_ONLY`도 같은 배타 그룹(`_exclusive_modes`)에 포함됩니다.
