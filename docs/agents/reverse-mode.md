# 리버스모드 상세 규칙

> 이 문서는 `AGENTS.md`에서 **내용 변경 없이 그대로 옮긴** 상세 규칙입니다(문서 구조 최적화). 자동으로 로딩되지 않으므로 해당 작업을 할 때만 여세요. 공통 안전 규칙·핵심 운영 정책은 `AGENTS.md`에 남아 있습니다.

## 리버스모드 (원문: NOTES › 리버스모드)

- **리버스모드** (`src/strategy.py execute_reverse_mode()`):
  - 발동: `T > splits - 1` + position > 0 (20분할 T>19, 40분할 T>39 — 원본 규칙: 마지막 1회 매수도 불가능한 상태)
  - 1일차: MOC 매도 (보유량 1/10(20분할) or 1/20(40분할)) → T × 0.9/0.95 (MOC 가격은 `adjust_price_to_tick()`으로 호가 단위 보정 후 전달)
  - 2일차+ (비소진): LOC 매도 @5일MA + 실제 주문가능금액 기준 쿼터매수 @별지점-0.01 + **쿼터매수 아래 1주씩 계단식 추가매수 LOC**(`_build_reverse_additional_buys`, 원문 주문표 기준 — 사용자 제공 PDF 요약, 저장소에 사본 없음). 가격은 일반모드와 같은 `build_additional_loc_prices`(쿼터매수금액 ÷ (쿼터매수 수량+i)), 쿼터매수 가격 아래·단조 감소, 쿼터매수+추가매수 합계 ≤ 주문가능금액(시드 캡 반영). 단계 수는 일반모드와 같은 `{SYMBOL}_ADDITIONAL_LOC_LEVELS`(기본 8, 0=끔). 주문은 `reverse_action=buy` + `reverse_t_target=0` + `[추가매수]` 표식으로 접수되어 **체결돼도 T는 오르지 않고 `net_invested`만 증가**(`state.reconcile_reverse_fills`·`ops/verify_run.py` 독립 검증 포함). 체결되지 않은 추가매수가 대부분이므로 **미체결 주문을 다음 세션에 종결하는 경로가 있는 환경에서만 겁니다**: 데모 전체(자동 만료 가정)와 메리츠 실전(이전 세션 zero-fill 만료 가정, PR #21 선행 조건). KIS·LS·KIWOOM·TOSS·NHPLUG **실전**은 그 경로가 없어(KIS 실전은 체결 전용 이력이라 미체결이 이력에서 빠짐) 추가매수를 걸지 않습니다(`_reverse_ladder_supported`)
  - 2일차+ (소진): 쿼터매수(잔금의 1/4)로 1개 매수가 불가능하면 매수 시도 없이 MOC 매도만 수행
  - 리버스모드 날짜는 실행당 1회만 증가하며, DRY 실행으로 저장 상태를 진행시키지 않음
  - 같은 실행에서 제출한 매도 주문의 예정 매도대금은 매수 가능금액으로 선반영하지 않음
  - **쿼터매수 잔금**: broker `orderable_cash`(주문가능금액)만 사용하며 `reverse_mode.cumulative_sell_proceeds`를 합산하지 않음 — **의도적 보수 설계** (정산 매도대금은 broker가 `orderable_cash`에 반영함을 전제, `test_reverse_mode.py:50-76`로 고정). 원본(매도금 누적 합산)과 달리 하락장에서 덜 매수 = 더 안전한 쪽
  - T 갱신은 실제 체결 이력 반영을 기준으로 하며, 전략의 T 계산값만으로 저장하지 않음
  - **쿼터매수 T 효과는 체결 반영 시점의 '직전 T' 기준** (원문 4~5쪽: 매도 `T=직전 T×0.9`, 매수 `T=직전 T+(분할수−직전 T)×0.25`). 주문 생성 때 `reverse_t_target` 은 '같은 날 매도 전량 체결' 가정의 **계획 추정값**일 뿐이고, 신규 주문(`reverse_t_rule=2`, `reverse_splits` 기록)은 정산이 **첫 체결을 반영할 때** 그 시점 T(같은 세션 매도 체결 반영분 포함)로 목표값을 확정해 `reverse_t_target` 에 덮어씁니다(계획값은 `reverse_t_target_planned`, 기준 T 는 `reverse_t_target_basis_t`). 이후 부분체결은 확정값 × 누적 체결비율만 적용해 체결을 쪼개도 결과가 같습니다. 정산 순서의 시각(`ord_datetime_utc`)은 브로커 이력의 **주문(접수) 시각**(메리츠는 주문일시 우선)이며 체결 시각이 아닙니다 — 봇이 매도를 먼저 접수하므로 매도→매수 순이 되고, 초 단위로 같으면 매도를 먼저 정산합니다. 분할수를 넘는 T(`T>분할수`)는 매수 효과가 음수가 되어 오류를 덮을 수 있으므로 자르지 않고 정산을 보류합니다. 분할수·기준 T 가 무효면 추정하지 않고 정산을 보류하고, 표식이 없는 기존 주문은 기존 규칙(계획값)을 그대로 씁니다. 독립 검증기(`ops/verify_run.py`)는 직전 T 를 원장 재생으로 구해 목표값을 재계산합니다. 배경: 2026-11-17 SOXL 매도 미체결인데 쿼터매수가 +0.747(정상 +0.274) 반영됨
  - 종료 조건: 종가 > 평단×(1-0.15)(TQQQ) or ×(1-0.20)(SOXL) — 판정은 `last_price`(실시간/최신가) 기준. 런 타이밍: **실전=pre장**(직전 종가 근사), **모의=장 후반**(당일 종가 근사). pre-market이 라이브 시세를 반환하면 종가와 달라질 수 있어 broker별 1회 DRY 검증 필요
  - 별지점: Finnhub 5일 MA → close_prices(state) → last_price fallback (fallback 시 `[경고]` 로그 출력)
  - **TOSS MOC 변환**: 토스는 MOC(장마감시장가)를 지원하지 않음. 리버스 1일차 SELL MOC는 `toss/adapter.py place_order()`에서 LOC(장마감지정가) `$0.01`로 자동 변환되어 종가 체결 (MOC와 동일 동작, `_MOC_SELL_PROXY_PRICE`). 변환은 adapter 한 곳에서만 일어나므로 전략은 계속 `order_type="MOC"`를 생성하며, fence(`pending_order_intent/batch`)에는 원본 MOC/현재가가 기록됨 — fence 복구는 세션 날짜만 사용하므로 영향 없음. **BUY MOC는 변환하지 않음** ($0.01 매수 LOC는 체결 불가) → `OrderNotAcceptedError` 유지
  - **BUY LOC +19% 보정과 쿼터매수 상호작용**: `trading_bot.py`의 BUY LOC 가격 보정(`last_price × 1.19` 초과 시 `last×1.19`로 교정)이 리버스 쿼터매수(`star-0.01`, 별지점=5일MA)에도 적용됨. 하락장이 전제인 리버스에서는 별지점이 현재가보다 19% 이상 높아 **일반모드보다 훨씬 자주 발동** → notify 다발 + 매수가가 의도(별지점 대기)와 다른 `last×1.19`로 보정될 수 있음. 미해결 리스크로 기록 (기능 실패는 아님)
  - **BUY LOC 1.19 상한 + 추가매수 계단 배치는 `src/strategy.py`에서 처리** (`cap_buy_loc_price` / `build_additional_loc_prices`, 2026-10-20 `c995412`). `trading_bot.py`의 주문 전송 직전 보정은 제거했습니다(이중 보정 방지). 상한에 걸린 추가매수 후보는 **건너뛰고** 정상매수 보정가보다 낮은 단계부터 배치합니다 — 안 하면 정상매수와 같은 가격으로 뭉칩니다. DRY 프리뷰·fence·실제 전송 가격이 모두 이 단계의 결과입니다.
  - **데모 모드 MOC/LOC→LIMIT 변환**: KIS/KIWOOM 데모는 MOC/LOC/LOO/MOO를 `LIMIT`으로 자동 변환(LS는 `DEMO_UNSUPPORTED_ORDER_TYPES`가 비어 있어 변환 없음). 리버스 "마감 체결" 전제가 깨져 데모(장중 체결 가능)와 실전(마감 체결) 결과가 달라질 수 있음. T 반영은 체결 이력 기준이라 정합성은 유지
  - **실전 pre-market MOC/LOC 제출 미실증**: `trading_bot.py`는 실전에서 pre-market(ET ~04:00)까지 대기 후 주문. MOC/LOC(마감가 주문)는 통상 정규장에만 접수되므로 ET 04:00 제출 시 브로커가 거부/보류할 수 있음. KIS(33)/LS(M4)/KIWOOM(33) 실전의 "MOC+가격" 전송 및 TOSS CLS 주문의 pre-market 접수 여부는 **실전 배포 전 1회 검증 필요**
  - **TOSS `prerequisite-required` 실측**: `ETF 거래를 위한 요건이 부족합니다` — 신규 매수 주문에 걸리며 기존 보유와 무관(보유 중이어도 매수 차단). 투자성향(자금투자성향) 앱 등록으로 해소됨 확인 (교육 이수 아님). 해소 후 다음 관문은 `insufficient-buying-power`(주문가능금액 부족). 4xx 응답은 에러 envelope 선파싱 후 `OrderNotAcceptedError` 확정거부로 처리 (`toss/adapter.py _request_with_rate_retry()`)
  - **LS `08507` 실측**: `ETF/ETN 교육이수 확인 후 매매 가능 합니다` — 레버리지 ETF(TQQQ 등) 매수 시 교육이수 요구. **거래확인서 등록으로 해소됨 확인** (교육이수 불필요). preflight 거부 경로(`99999주/$0.01`)에서도 동일 코드로 확인 가능하며 이력 오염 없음
