# 동적 추가매수 — 예산 계약 (B_avail · R · 예약금)

**상태**: 후보 실험 단계 (기본 비활성) · **범위**: `src/dynamic_preview.py` DRY 미리보기 + `src/dynamic_levels.py` 정책 E
**최종 갱신**: 2026-11-09

이 문서는 동적 단계(정책 D/E)가 **어떤 숫자를 예산으로 믿는지** 고정합니다.
여기 정의된 값을 추정해서 채우지 않습니다 — 확정할 수 없으면 동적을 끄고 고정 3으로 갑니다(fail-closed).

---

## 1. 용어

| 이름 | 의미 | 누가 확정하나 |
|------|------|----------------|
| `R` (remaining_seed) | `seed − net_invested` (음수는 0으로) | 호출자 (`strategy.무한매수법_V4` → `remaining_seed`) |
| `B_avail` (budget_available) | **이번 실행에서 실제로 쓸 수 있는 가용예산** | 호출자 (브로커 구매가능액 + 예약금 계약 반영) |
| `orderable_cash` | **브로커가 발표한 구매가능액** (원값) | 브로커 API (`get_purchase_amount()`) |
| `open_reserved` | 이미 살아 있는 미체결 **매수** 주문에 잡혀 있는 금액 | 호출자 (측정 불가하면 `None`) |
| `reserved_already_deducted` | `B_avail` 이 이미 `open_reserved` 를 차감한 값인지 여부 | 호출자 (기본 `False`) |

핵심 구분:

- `R` 은 **시드 캡**입니다 (순투입 기준 잔여 시드).
- `B_avail` 은 **이번 실행 예산**입니다. `R` 과 같은 값일 수도 있지만, 반드시 같지는 않습니다.
- `orderable_cash` 는 **브로커 발표값**입니다. 봇이 미체결 예약금을 직접 빼지 않습니다(§3).

> ⚠️ `orderable_cash` 를 그대로 `B_avail` 로 쓰려면 §4 의 이중차감 계약을 반드시 확인하세요.

---

## 2. R = seed − net_invested

- `net_invested` 는 `Σ 매수체결금액 − Σ 매도체결금액` (USD)입니다.
- 시드 캡은 **원가(평단×수량) 기준이 아니라 순투입 기준**입니다.
  평단 이하 매도(손절) 시 원가 기준은 시드 여유를 부풀려 누적 투입이 시드를 넘길 수 있었습니다
  (실제 사례: 시드 $50,000 vs 누적 사용 $56,299.66).
- `R = max(seed − net_invested, 0)` 이며, `net_invested_status != "valid"` 이고 `net_invested <= 0`
  이면 전략이 신규 주문을 보류합니다 (`strategy.무한매수법_V4` 게이트). 미리보기도 이 경우
  `remaining_seed` 가 0 으로 넘어와 정책 E 가 `L=0` 을 고릅니다.
- `R` 을 확정할 수 없으면(`None`) 미리보기는 동적을 켜지 않습니다
  → `fallback:budget-undetermined(remaining_seed)`.

---

## 3. broker `orderable_cash` — 봇이 예약금을 직접 차감하지 않습니다

**코드 조사 근거** (2026-11-09, `develop` 기준):

| 위치 | 내용 |
|------|------|
| `src/strategy.py:452-463` | `orderable_cash = psamount.orderable_cash` → `min(orderable_cash, remaining_seed)` (시드 캡만 적용, 미체결 차감 없음) |
| `src/broker/kis/adapter.py:358` | `ord_psbl_frcr_amt` (주문가능외화금액) 그대로 반환 |
| `src/broker/kiwoom/adapter.py:585` | `fc_ord_alowa` 그대로 반환 |
| `src/broker/ls/adapter.py:622,670` | `COSOQ02701` 외화주문가능금액(`FcurrOrdAbleAmt`) 그대로 반환 |
| `src/broker/nhplug/adapter.py:633` | `orr_pbl_amt` 그대로 반환 |
| `src/broker/toss/adapter.py:597` | `cashBuyingPower` 그대로 반환 |
| `src/broker/meritz/adapter.py:644` | `oder_*_able_camt` 계열 그대로 반환 |
| `src/trading_bot.py` `_cancel_open_bot_orders()` | LIVE 신규 주문 직전 미체결 주문을 취소·확정하지만 **예약금을 계산해 빼지는 않음** |

즉 봇 코드에는 "미체결 예약금만큼 `orderable_cash` 를 줄이는" 로직이 **없습니다**.
브로커가 내부적으로 미체결 주문을 구매가능액에서 빼는지는 **브로커·계좌 유형별로 다르고
문서화되어 있지 않습니다**. 그래서 계약을 호출자가 명시합니다:

- `reserved_already_deducted=False` (기본): `B_avail` 에 예약금이 **아직 반영되지 않았다** →
  정책 E 의 전체예산 분자에 `open_reserved` 를 **더합니다**.
- `reserved_already_deducted=True`: `B_avail` 이 **이미 예약금을 차감한 값**이다 →
  분자에서 `open_reserved` 를 **제외**합니다 (이중차감 방지).

### 브로커별 실측 필요 사항

`reserved_already_deducted=True` 로 넘기려면 **그 브로커에서 미체결 주문이 구매가능액을
실제로 줄이는지 1회 실측**해야 합니다. 실측 전에는 기본값(`False`)을 유지하세요.
(미리보기는 `False` 기준으로만 계산하고, 이 값은 로그에 그대로 남깁니다.)

---

## 4. `open_reserved` — 미확정이면 `undetermined`

- 값은 **금액(USD)** 입니다. `None` 은 "미확정"이고 **0 이 아닙니다**.
- `0.0` 은 **명시적으로 측정된 0** 일 때만 넘깁니다 (살아 있는 매수 주문이 0건임을 확인한 경우).
- 미확정(`None`)이면 미리보기는 **동적 정책을 강제 fallback 3** 으로 내리고
  `reason` 에 `reservation-undetermined` 를 남깁니다 (`dynamic_preview.compare_dynamic_preview`).

### 미체결 존재 시 예약금 계산식

```
open_reserved = Σ (max(0, 주문수량 − 누적체결수량) × 실제 전송 주문가)   # BUY 주문만
```

- **매도 주문은 제외**합니다 (현금이 아니라 주식 수량을 예약).
- 주문가는 **호가 보정 후 실제 전송가**입니다 (`adjust_price_to_tick`, BUY LOC 은 1.19 상한 적용 후 값).
- 계산·검증 구현: `dynamic_preview.compute_open_reserved_amount(open_orders)`
  - 주문가·수량이 숫자가 아니거나, 음수이거나, 체결수량 > 주문수량이면 → **미확정(None)**
  - 목록 자체가 `None`(조회 실패/미수행)이면 → **미확정(None)**

### 취소·만료가 확인되지 않은 주문

- 주문이 취소·만료·정정되었는지 **확인되지 않았으면** 그 주문은 "살아 있는 것"으로 보고
  예약금에 포함하거나, 금액을 확정할 수 없으면 `None` 으로 넘깁니다.
- **신규 예산으로 계산하지 않습니다.** 취소/만료 미확인 상태에서 새 예산을 쓰면
  중복 주문·과매수로 이어집니다.
- LIVE 는 기존 규칙을 그대로 따릅니다: `_cancel_open_bot_orders()` → 취소 확정 확인 →
  재계획. 미리보기는 주문을 취소하지 않습니다(조회만).

### 왜 DRY 미리보기의 기본값이 `None` 인가

DRY 미리보기 후크(`trading_bot._log_dynamic_loc_preview`)는 `open_reserved=None` 을 넘깁니다.
이유: 현재 주문이력 표준 필드에는 **주문가가 없고**(`ft_ord_qty`, `ft_ccld_qty`, `nccs_qty`,
`ft_ccld_unpr3` 만 존재), `state.orders_meta` 에는 `total_qty` 만 있어 **가격×잔량을 확정할 수
없기** 때문입니다. 그래서 미리보기는 fail-closed 로 고정 3을 보여주고, 로그에
`예약금: 미확정 → 동적 비활성(고정 3)` 을 남깁니다.

### 실 DRY 후크의 OHLC 소스 (현재 미배선)

- 후크는 `broker.get_daily_candles()` **오버라이드 결과만** OHLC 소스로 사용합니다.
- `src/broker/base.py` 의 기본 구현은 `get_daily_closes()` 로 `date`+`close` 만 채우므로
  **OHLC(o/h/l/c)가 없습니다**. TOSS 처럼 `date`+`close` 만 주는 소스도 동일합니다.
- 이 경우 미리보기는 **OHLC 를 합성하지 않고**(합성 금지) `fallback:ohlc-unavailable` 로
  내려가 **항상 고정 3** 을 보여줍니다 (안전하지만 D/E 비교는 비어 있음).
- 따라서 **D/E 주문가·수량 비교는 OHLC(o/h/l/c)를 가진 bars 를 넘기는 호출자에서만 동작**합니다
  (단위 테스트의 fixture, 또는 후속 단계에서 브로커별 OHLC 소스를 배선한 뒤).
- 세션 날짜도 봉 `date` 또는 명시 인자로만 확정합니다. 확정 불가면 UTC/로컬 날짜로
  **조용히 대체하지 않고** `session-date-undetermined` fallback 으로 처리합니다.

---

## 5. LOC/LIMIT 최대 필요금액 합산식

정책 E 의 전체예산(①)은 **동시에 묶일 수 있는 최대 자본**입니다.

```
최대필요금액 =
    정상매수금액
  + Σ(추가매수 1주 가격)            # 각 1주, LOC
  + (reserved_already_deducted ? 0 : open_reserved)

조건: 최대필요금액 ≤ B_avail
추가매수 전용: Σ(추가매수 1주 가격) ≤ kappa_add × R
```

- **가격×수량** 기준입니다. LOC 도 지정가와 동일하게 `가격 × 수량` 으로 계산합니다.
- BUY LOC 가격은 **1.19 상한가**(`현재가 × 1.19`, 호가 보정 후)를 넘지 않습니다
  (`strategy.cap_buy_loc_price`). 상한이 적용된 가격으로 합산합니다
  (미리보기도 실제 전략과 같은 `build_additional_loc_prices()` 결과 가격을 더합니다).
- 수수료·환전 스프레드는 포함하지 않습니다 (기존 전략과 동일한 보수적 근사).
- 정상매수는 **줄이지 않습니다.** 예산이 빠듯하면 추가매수 단계만 0까지 줄입니다.

---

## 6. 조회 실패는 fail-closed (동적 off → 고정 3)

다음 중 하나라도 확정할 수 없으면 **동적 정책을 켜지 않고 고정 3** 으로 표시합니다.

| 상황 | 미리보기 `dynamic.reason` | `reasons` 토큰 |
|------|---------------------------|----------------|
| OHLC/시세 등 조회 실패 (`data_errors`) | `fallback:api-error(...)` | `api-error` |
| OHLC 키(o/h/l/c) 미비·빈 목록 | `fallback:ohlc-unavailable(...)` | `ohlc-unavailable` |
| 세션 날짜 확정 불가 (봉 date·명시값 없음) | `fallback:session-date-undetermined` | `session-date-undetermined` |
| `open_reserved is None` (미체결 예약금 미확정) | `fallback:reservation-undetermined(...)` | `reservation-undetermined` |
| `R` 미확정 | `fallback:budget-undetermined(remaining_seed)` | `budget-undetermined` |
| `B_avail` 미확정 | `fallback:budget-undetermined(budget_available)` | `budget-undetermined` |
| 추가매수 여력 없음 (수량 0·상한 없음) | `fallback:no-additional-capacity` | `no-additional-capacity` |
| 확정봉 부족으로 ATR 불가 | `fallback:no-indicator` | `atr-insufficient` |
| 입력값이 숫자가 아님 | `fallback:invalid-input(...)` | `invalid-input` |

- 확정봉 15개 미만이면 ATR 을 **추정하지 않습니다** (`dynamic_levels.MIN_ATR_BARS`).
- fallback 으로 내려간 단계는 **세션 prev 히스테리시스에 기록하지 않습니다**
  (데이터 결손이 다음 세션 판단을 오염시키지 않도록).

---

## 7. 미리보기 출력 계약

`compare_dynamic_preview()` 반환 dict:

```
{
  "symbol", "session_date", "virtual": True, "policy",
  "indicator": {bars, atr, close, atr_ratio, available},
  "budget": {remaining_seed, budget_available, normal_amount, open_reserved,
             open_reserved_state: "measured"|"undetermined",
             reserved_already_deducted, reserved_applied, reservation_note},
  "fixed":   {policy, levels, orders[virtual], prices, order_count, additional_total, total, reason, fallback},
  "dynamic": {policy, levels, orders[virtual], prices, order_count, additional_total, total, reason, fallback},
  "comparison": {fixed_levels, dynamic_levels, level_delta, fixed_order_count,
                 dynamic_order_count, fixed_additional_total, dynamic_additional_total,
                 additional_total_delta, fixed_prices, dynamic_prices,
                 shared_prefix_match, budget_delta},
  "reasons": [...],
  "prev": {"D", "E"}
}
```

- `fixed.total` / `dynamic.total` 은 예약금이 미확정이면 `None` 입니다 (0 으로 표시하지 않음).
- `session_date` 는 확정 불가 시 `None` 입니다 (UTC/로컬 대체 없음, fallback 사유에 기록).
- 주문 dict 는 `virtual=True` 이며 전송 함수가 없습니다.
- `shared_prefix_match`: 동적 사다리의 앞부분이 고정 3과 같은 가격인지(같은 가격 공식 사용 확인).
- 같은 세션·같은 입력이면 **완전히 같은 dict** 를 돌려줍니다(prev 는 이전 세션만 참조).

---

## 8. 활성화 · 안전 규칙

- 기본 **비활성**. `TRADE_MODE=DRY` + `DYNAMIC_LOC_PREVIEW=1|true` 일 때만 후크가 동작합니다.
- **실 DRY 후크는 현재 OHLC 소스 미배선으로 fallback 3 고정** (`get_daily_candles` 기본 구현이
  date+close 만 제공) → D/E 비교는 OHLC(o/h/l/c)를 측정해 넘기는 호출자에서만 동작합니다.
- 미리보기는 **state 를 인자로 받지 않습니다** → T·net_invested·orders_meta 를 바꿀 수 없습니다.
- LIVE 경로·주문·저장 코드는 이 기능과 무관합니다 (변경 없음).
- `_PREV_LEVELS` 는 프로세스 메모리에만 존재합니다 (state 스키마 변경 없음, 재시작 시 초기화).
