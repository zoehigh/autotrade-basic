# 1회성 체결 마커 정정 절차서 (`orders_meta[odno].processed_filled_qty`)

작성: 2026-10-26
대상: `.state.json` 의 `orders_meta[<odno>]` 반영 마커
코드 근거: `src/state.py`(`_parse_processed_qty_marker`, `_fill_delta`), `trading_bot.py`(`_save_settlement_state`)

## 0. 이 절차가 필요한 상황

정산이 아래 보류로 멈췄을 때입니다.

```
[보류] TQQQ 마커 누락·무효 주문 1건(45025)이 있어 T/순투입/마커 저장과 신규 주문을 보류합니다.
       마커 보정 후 재실행하세요.
```

`processed_filled_qty` 는 **"이 체결을 정산에 이미 반영했다"** 는 표식입니다.
값이 없거나(누락) 이상하면(소수·음수·문자열 오타) 봇은 자동 반영하지 않고 **보류**합니다
(자동 반영하면 T·순투입이 이중 가산될 수 있으므로 차단하는 것이 정상 동작입니다).

## 1. 절대 원칙 (이 절차는 "마커만" 고칩니다)

| 항목 | 이 절차에서 |
|------|-------------|
| `orders_meta[odno].processed_filled_qty` | **명시 설정 대상** |
| `orders_meta[odno].processed_filled_amount` | **명시 설정 대상** |
| `T`, `net_invested`, `settlement_ledger`, `sell_t_effects` | **변경 금지 (불변)** |
| `last_updated` / `last_processed_ordno` (워터마크) | **변경 금지 (불변)** |

- 마커를 **올리는** 정정(=이미 T/순투입에 반영된 체결을 표시)만 이 문서의 범위입니다.
- 마커를 **내리는** 정정은 다음 정산에서 그 체결이 **다시 반영**됩니다.
  즉 T·순투입을 함께 되돌려야 하는 별도 복구 건이며, 이 절차로 단독 수행하면 안 됩니다.
- T를 고쳐야 하는 상황이면 FORCE_T 계열·`STATE_REPAIR_ONLY` 문서를 따로 보세요. 두 작업을 한 번에 섞지 않습니다.

## 2. 사전 확인 (읽기 전용)

```bash
cd ~/investment/muhan-v4            # 운영 체크아웃 (경로는 환경에 맞게)
cp -p .state.json ".state.json.bak-$(date -u +%Y%m%dT%H%M%SZ)"   # ① 백업 먼저
sha256sum .state.json >> marker-correction.log                    # ② 원본 해시 기록

# 보류 대상 odno·마커 현재값 확인
TRADE_MODE=DRY uv run python trading_bot.py 2>&1 | grep -E "마커|보류" | tail -20
```

`settlement-<SYMBOL>-<stamp>.json` 의 `pending_marker_odnos` / `hold_reason` 에도 같은 주문번호가 남습니다.
**주문번호(odno)와 그 주문의 실제 누적 체결수량·금액을 브로커 이력에서 먼저 확정**한 뒤 진행하세요.

## 3. 정정 실행 (오프라인 — 브로커 API 호출 없음)

정정 전후 canonical 해시를 남기기 위해 **state 전용 스크립트**로 처리합니다 (LIVE 파이프라인 실행 아님).

```bash
uv run python - <<'PY'
import hashlib, json, sys
sys.path.insert(0, "src")
from state import load_state, save_state, canonical_state_hash

SYMBOL = "TQQQ"
ODNO = "45025"
QTY = 20            # 브로커 이력의 누적 체결수량 (정수, 합격 기준: 0 이상 정수)
AMOUNT = 1450.00    # 브로커 이력의 누적 체결금액 (USD, float)

state = load_state(SYMBOL)
meta = (state.get("orders_meta") or {}).get(ODNO)
assert meta is not None, f"orders_meta[{ODNO}] 없음 — 주문번호를 확인하세요"
print("[before] hash =", canonical_state_hash(state))
print("[before] marker =", meta.get("processed_filled_qty"),
      meta.get("processed_filled_amount"))

# 마커만 명시 설정 (T/net/원장/워터마크는 손대지 않음)
meta["processed_filled_qty"] = int(QTY)
meta["processed_filled_amount"] = float(AMOUNT)
save_state(SYMBOL, state)

after = load_state(SYMBOL)
print("[after ] hash =", canonical_state_hash(after))
print("[after ] T =", after.get("T"), "net =", after.get("net_invested"))
PY
```

기록 항목(로그/티켓에 남기기): **정정 전 sha256 + canonical hash → 정정 후 canonical hash**,
odno, 정정 전/후 마커, `T`·`net_invested` 값(정정 전후 동일해야 정상).

## 4. 직후 reconcile 1회 확인 (여기서 멈춤)

```bash
STATE_REVERSE_RECONCILE_ONLY=true TRADE_MODE=DRY uv run python trading_bot.py
```

확인 포인트:
1. `마커 누락·무효` 보류 메시지가 **사라졌는지**
2. `T` / `net_invested` 가 **정정 전과 같음** (마커만 올렸다면 변하지 않아야 정상)
3. 출력된 canonical hash 를 §3의 [after] hash 와 대조 (§2 ① 백업 해시와는 달라야 정상 — 마커가 바뀌었으므로. ①과 같으면 정정이 반영 안 된 것, [after]와 다르면 롤백)

정상 확인 후 다음 정규 실행(스케줄)부터 신규 주문이 재개됩니다.
reconcile 단계에서 값이 달라지면 **신규 주문은 아직 나가지 않았으므로** 백업으로 되돌리고 원인을 다시 봅니다.

## 5. 롤백

```bash
cp -p ".state.json.bak-<정정시각>" .state.json
sha256sum .state.json     # §2 에서 기록한 원본 해시와 일치 확인
```

## 6. 환경변수는 1회성 (매우 중요)

- `STATE_REVERSE_RECONCILE_ONLY`, `STATE_REVERSE_AUDIT_ONLY`, `STATE_REPAIR_ONLY`,
  `STATE_CLEAR_FENCE_ONLY`, `STATE_NET_INVESTED_REPAIR_ONLY`, `STATE_SET_CYCLE_START_ONLY`,
  `{SYMBOL}_FORCE_T` 는 모두 **1회성 점화용**입니다.
- 확인이 끝나면 **즉시 삭제**하세요. `.env`, `ops/run_daily.sh`, cron, systemd 유닛에 **절대 남기지 않습니다.**
  남아 있으면 매 실행마다 보정/보류 모드로 돌아 신규 주문이 영구 차단됩니다.
- 여러 `STATE_*_ONLY` 를 동시에 켜지 마세요 (배타 그룹).

## 7. 하지 말 것

- LIVE 로 바로 실행해 마커를 확인하지 않기 (실주문 발생) → **DRY/reconcile 전용 모드로 확인**
- 마커와 함께 `T`/`net_invested`/원장을 "대충 맞추기" (검증기 `ops/verify_run.py` 가 적용 내역을 독립 재계산해 실패로 잡습니다)
- `.state.json` 을 손으로 편집한 뒤 백업·해시 없이 넘어가기
- 마커 하향 정정(재반영 유발)을 단독 수행
