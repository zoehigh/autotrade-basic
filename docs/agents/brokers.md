# 브로커별 규칙 (에러 코드·preflight·이력/시각 컨벤션)

> 이 문서는 `AGENTS.md`에서 **내용 변경 없이 그대로 옮긴** 상세 규칙입니다(문서 구조 최적화). 자동으로 로딩되지 않으므로 해당 작업을 할 때만 여세요. 공통 안전 규칙·핵심 운영 정책은 `AGENTS.md`에 남아 있습니다.

## 브로커 에러 처리 (원문: BROKER ERROR HANDLING)

| 브로커 | 성공 코드 | 에러 코드 | 에러 메시지 | 검증 위치 |
|--------|----------|----------|------------|----------|
| KIS | `rt_cd == "0"` | `msg_cd` | `msg1` | 각 메서드 |
| KIWOOM | `return_code == 0` | `return_code` | `return_msg` | `_check_response()` |
| LS | 조회 `rsp_cd in {"00000","00001","00136"}` / 주문 `{"00000","00040","00050"}`(00050 미실측 추정) / 취소 `{"00000","00156"}` | `rsp_cd` | `rsp_msg` | 조회: `LS_SUCCESS_RSP_CDS`, 주문/취소: 각 메서드 |
| TOSS | `error` 없음 | `error.code` | `error.message` | `_request_with_rate_retry()` |
| NHPLUG | `rsp_cd in {"00000","00166","00221","13578","XA102","00048"} or "완료" in rsp_msg/usr_msg` | `rsp_cd`/`msg_code` | `rsp_msg`/`usr_msg` | `_request_with_rate_retry()` |
| MERITZ | 주문 `rsp_cd=="0001"` AND `data.warn_cls_code==0`, 조회 계열별 상이 (시세 `0000` / 잔고·매수가능 `5766` / 이력·당일 `5766`·`5762`(다음페이지) / 취소 `0001`) — 5820은 data 유무로 판단, EGW00200은 조회만 제한 재시도 | `rsp_cd` | `rsp_msg`+`rsp_sub_msg` | `_check_rsp_cd()` |

**참고**: 주문 시각은 모든 브로커에서 `get_kst_now()` 로컬 시간 사용 (API 응답 시간 미사용)
**MERITZ 주문 시각**: 합성 주문번호 `YYYYMMDD:제로패딩주문번호`, `oder_exec_ymdh/cntg_ymdh`는 KST 그대로 파싱 (resolve_real_kst_from_ord 미적용, `_ord_dt_is_kst_date=True`)

## 브로커별 필수 자격증명 (원문: COMMANDS 주석)

```bash
# 참고: 브로커별 필수 자격증명
#   kis   = KIS_APP_KEY + KIS_APP_SECRET + KIS_ACCOUNT_NO
#   kiwoom= KIWOOM_APP_KEY + KIWOOM_APP_SECRET
#   ls    = LS_APP_KEY + LS_APP_SECRET
#   toss  = TOSS_APP_KEY + TOSS_APP_SECRET
#   nhplug= NHPLUG_APP_KEY + NHPLUG_APP_SECRET + NHPLUG_ACCT_NO
#   meritz= MERITZ_APP_KEY + MERITZ_APP_SECRET (계좌번호 불필요, real 전용, 모의 없음)
# 유닛/모의 테스트(test_broker_contract, test_reverse_mode, test_state_t_updates 등)는
# API를 호출하지 않아 자격증명 없이 항상 실행됩니다.
```

## PREFLIGHT·MERITZ 규칙 (원문: PREFLIGHT 절 — 제목 `PREFLIGHT (브로커 검증) — 모의 전 사전 테스트`)


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

**MERITZ 주문조건구분(`oder_cond_cls_code`)**: 값이 `oder_cls_code`에 따라 고정됩니다 — `01`·`05`·`35~38` → `0`, `31`·`33` → `2`, `32`·`34` → `7`. 위반 시 주문이 `IGW50002`로 거부됩니다 (2026-10-20 LOC 쿼터매도 실측 미접수). 매핑은 `src/broker/meritz/adapter.py`의 `_ODER_COND_CLS_CODE`.

**MERITZ 주문 정정** (`POST /trading/v1/overseas/orders/modify`): `MeritzBroker.modify_order()` 구현. 필수 6필드 `oder_date`/`orgl_oder_no`/`oder_cls_code`/`odqt`/`ovrs_stck_oder_prc`/`oder_cond_cls_code`. **해외는 재정정 시 직전 정정 건의 주문번호를 씁니다** (국내는 최초 주문번호 유지 — 기준이 반대). 정정하면 주문번호가 바뀌므로 `state.json`의 `orders_meta` 키도 새 번호로 옮기고 `total_qty`를 정정 수량으로 갱신해야 합니다.

**MERITZ 정정은 수량을 바꾸지 않습니다**: 가격만 반영되고, `odqt` 증가는 접수(`rsp_cd=0001`·경고 없음)돼도 실제 `oder_dqty`는 원주문 수량 그대로입니다 (2026-10-20 1주→7주 실측). **수량 변경이 필요하면 취소 후 신규 주문**으로 처리하세요. 취소 확인은 상세조회(`/orders/detail`)가 아니라 **이력 기준**입니다 — 취소는 이력에 별도 행(`rctf_cncl_cls_name="취소"`, `orgl_oder_no=취소 대상`)으로 남습니다.

**MERITZ 경고확인값**: 해외주식의 `warn_cnfr_yn` 재전송 값은 **`"Y"`** 입니다 (공식 안내 기준). 응답 설명의 `"A"`는 국내 관례이므로 사용하지 않습니다.

**MERTMS 해외주식 주문 만료/취소 판별 코드** (2026-10-22 실측):
- `5636` "정정,취소 가능 수량이 없습니다" → **만료됨**(세션 종료) 또는 체결/잔량 0. 취소 대상이 아닙니다.
- `4062` "원주문이 취소주문으로 정정,취소 주문이 불가능합니다" → **이미 취소됨**(취소 행 존재).
→ 둘 다 "살아있는 주문이 아님"을 뜻합니다. **이전 세션 주문은 만료이므로 취소 시도 대상이 아닙니다**
  (시도하면 위 코드로 거절되어 "미확정"으로 잘못 판정되어 그날 주문이 차단됩니다).
→ 기간 이력은 만료 주문도 `미체결` + 잔량=주문수량 으로 표시합니다 (잔량 필드 없음 → 주문수량−체결수량 계산).
  그래서 이력만 보면 살아있는 것처럼 보이지만, 실제로는 만료되어 있습니다 (위 코드로 판별).

**MERTMS 해외주식 만료 판정 — 기간 이력은 잔량을 알려주지 않습니다**: 만료된(세션 종료) 주문도
**기간 이력(`/orders/history`)에서는 `미체결` + 잔량=주문수량 으로 표시**됩니다 (공식 잔량 필드 없음 → `주문수량−체결수량` 계산).
→ 실제 생존 여부를 판별하는 유일한 방법은 **취소 시도**이며, 만료 주문은 `5636: 정정,취소 가능 수량이 없습니다` 로 거절됩니다 (2026-10-21 실측 10건 전량).
→ 그래서 봇의 잔여 주문 정리는 **오늘 세션 주문만** 취소 대상으로 삼습니다 (이전 세션 주문은 만료이므로 취소 시도 불필요).
→ **주의**: 브로커 주문을 날짜 필터 없이 조회하면 수십 건의 만료 주문이 "열린 주문"으로 보입니다. 반드시 주문일자로 필터하세요.
→ 당일 이력(`/orders/today`)은 해외주식에서 0건을 반환하므로 잔량 확인용으로 쓸 수 없습니다.


## MERITZ 10/12 신청 게이트 (원문)

**MERITZ 10/12 신청 게이트 (2026-10-02 기준)**: 메리츠 오픈API는 모바일앱 신청이 차단되고 포털도 미개방 상태라 **실검증 보류**. 유동IP 사용 가능 확인됨 (고정IP 불필요, ubuntu-latest 그대로 사용). MAC 사전등록도 별도 절차 없음 (요청 헤더에 12자리 HEX를 넣어 전송하면 됨). 자격증명 발급 전까지 preflight 주문 단계는 `--live-order-probe`로만 실행하고, 기본 실행은 read-only 조회(1~6단계) + skip 출력으로 통과 처리합니다. 신청 개방 확인 후 preflight 1회 통과 + 실전 소액 LIVE 1세션 검증 순서로 진행.

## 브로커·주문이력 관련 NOTES 항목 (원문: NOTES)

- **KIWOOM/LS/TOSS/MERITZ**: `BROKER_CONFIG`에 `account_no` 불필요 (AppKey/Secret만으로 API 호출 가능), **KIS/NHPLUG는 계좌번호 필수** (`KIS_ACCOUNT_NO` / `NHPLUG_ACCT_NO` 없으면 호출 금지)
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
- **KIS 체결 전용 이력과 미체결 리버스 주문** (`state._assume_missing_reverse_order_zero_fill`): KIS 주문이력 조회는 `CCLD_NCCS_DVSN="01"`(체결만)이라 **체결되지 않은 주문은 이력에 행 자체가 없습니다**. 그래서 이전 세션 리버스 주문(예: 체결 안 된 쿼터매수)이 "이력 누락 → 신규 주문 보류"로 오판돼 SOXL 이 멈췄습니다(2026-11-12 KIS 데모 실측). `BROKER=kis` + `BROKER_MODE=demo` 에서 이전 세션·미반영(`processed_filled_qty`·`processed_filled_amount` 마커가 **명시된 유한한 숫자 0**이고 `applied_fill_fraction` 은 없거나 0 — 누락·None·문자열·소수 수량은 불명확하므로 보류)·미종결 주문이 행 없이 빠졌고, 이력이 비어 있지 않으며 그 세션 날짜 이전·이후(같은 날 포함)의 행이 모두 있을 때만(조회 구간이 그 세션을 덮는다는 증거) 0체결 만료로 가정해 `terminal`/`terminal_assumed`(사유 `missing_from_filled_only_history_after_session`)로 종결합니다. 이후 체결 행이 나타나면 odno 델타로 반영됩니다. 다른 브로커(이력에 미체결 행 포함)·실전은 기존대로 보류(KIS 실전은 검증 전까지 보수적 유지). **공통 변경(브로커·모드 무관)**: 이미 `terminal` 인 주문이 이력에서 빠진 것은 더 이상 보류하지 않으며, 리버스 이력 행이 없는 실행에서도 현재 cycle 의 모든 리버스 주문이 종결이면 `reconciliation_only` 표식을 해제합니다
- **ORDER_HISTORY_VERBOSE=true**: LIVE 모드에서도 `[주문이력 요약]` 상세 출력 (DRY는 항상 출력). KIS/KIWOOM/LS/TOSS 공통
- **텔레그램 재시도**: `send_telegram()`은 타임아웃/연결 오류/서버 5xx 시 최대 3회 재시도(1초 간격). 4xx(설정 오류 등)는 재시도 없이 즉시 실패

## close_prices / get_daily_closes / LS 취소 (원문: NOTES)

- **close_prices**: state.json에 최근 5거래일 종가 저장 (Finnhub fallback용)
- **`get_daily_closes()`** (`src/broker/base.py`):
  - Broker 추상 메서드: `(symbol, exchange, days=5) → list[float]` (오래된 종가순)
  - KIS: HHDFS76240000 → `GET /uapi/overseas-price/v1/quotations/dailyprice` (최신순 → 역순)
  - LS: g3204 → `POST /overseas-stock/chart` (차트 경로, `gubun="2"` 일봉, 종가 `close`, 오래된순 그대로) + Finnhub candle fallback. 시세 경로(`/market-data`)로 보내면 `IGW00215` 거부됨 (421baf0 실측)
  - KIWOOM: usa06012 → `POST /api/us/chart` (최신순 → 역순)
  - TOSS: `GET /api/v1/candles?interval=1d` (최신순 → `reversed()`)
  - 리버스모드 `_get_reverse_star_point()`에서 close_prices 보강용으로 사용 가능
- **LS 해외주식 취소** (`src/broker/ls/adapter.py::cancel_order`, 2026-11-03 실측): COSAT00301 + OrdPtnCode=`08`(취소) + OrgOrdNo=원주문번호(필수). 성공 `{"00000","00156"}` (00156="취소주문이 완료되었습니다"). 취소주문도 신규 주문번호를 발급받으며(OrgOrdNo로 원주문 연결), 원주문은 잔량 0·0체결로 확정. **COSMT00300은 "해외증권 매도상환주문"으로 취소와 무관 — 사용 금지**. Place_order 성공 `{"00000","00040","00050"}` (00040 실측="매수 주문이 완료되었습니다", 00050은 매도 대칭 추정 — 첫 실전 매도 시 rsp_cd 확인 필요). 주문번호는 OutBlock2(결과) 우선·OutBlock1(에코) 폴백으로 읽기.
