# 1회성 체결 마커 정정 절차서 (`orders_meta[odno].processed_filled_qty`)

작성: 2026-10-26
대상: `.state.json` 의 `orders_meta[<odno>]` 반영 마커
코드 근거: `src/state.py`(`_parse_processed_qty_marker`, `_fill_delta`), `trading_bot.py`(`_save_settlement_state`)

## 0. 이 절차가 필요한 상황

정산이 아래 보류로 멈췄을 때입니다.

```
[보류] TQQQ 마커 누락·무효 주문 1건(ord#3fa91c07be)이 있어 T/순투입/마커 저장과 신규 주문을 보류합니다.
       마커 보정 후 재실행하세요.
```

공개 Actions 로그에는 **실제 주문번호 대신 가명 식별자(`ord#…`)** 만 나옵니다
(`ord#` + HMAC-SHA256(실행별 무작위 salt, 주문번호) 앞 10자리). 원문 주문번호는 **비공개(암호화) Evidence** 의
`pending_marker_odnos` 에 있고, 같은 파일의 `log_salt` 로 `ord#…` 와 대조할 수 있습니다 (§2).

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

### 2-1. 백업·로그 보관 위치 — Git 저장소 **밖**의 접근 제한 디렉터리

수동 백업(`.state.json.bak-*`)과 작업 로그(`marker-correction.log`)에는 주문번호·체결·잔고 정보가 들어 있습니다.
체크아웃 폴더 안에 두면 실수로 커밋·공유될 수 있으므로 **저장소 밖**에 소유자 전용으로 보관하세요.

```bash
umask 077
AUDIT_DIR="$HOME/.local/state/marker-audit/$(date -u +%Y%m%dT%H%M%SZ)"   # 저장소 밖
install -d -m 700 "$AUDIT_DIR"

cd ~/investment/muhan-v4            # 운영 체크아웃 (경로는 환경에 맞게)
cp -p .state.json "$AUDIT_DIR/state.before.json"                  # ① 백업 먼저 (권한 600 유지)
sha256sum .state.json | tee -a "$AUDIT_DIR/marker-correction.log" # ② 원본 해시 기록
```

- 백업·로그를 이슈/PR/채팅/공개 Artifact 에 **원문 그대로 올리지 않습니다.** 공유가 필요하면 `gpg --symmetric` 으로 암호화한 파일만.
- 작업이 끝나고 보관 기간이 지나면 안전하게 삭제(`shred -u`)합니다.

### 2-2. 보류 대상 확인 (state 저장 없음)

```bash
# 일반 DRY — state 저장 생략. 공개 로그와 같은 가명 식별자(ord#…)가 출력됩니다.
TRADE_MODE=DRY uv run python trading_bot.py 2>&1 | grep -E "마커|보류" | tail -20
```

가명 식별자를 원문 주문번호로 되돌리려면 **비공개 Evidence** 를 복호화합니다
(`gpg --decrypt evidence.tgz.gpg | tar xz` → `settlement-<SYMBOL>-*.json`).
`pending_marker_odnos` 가 원문이고, 같은 파일의 `log_salt` 로 후보를 HMAC 해 `ord#…` 와 대조합니다
(`ops/verify_run.py` 의 `_independent_odno_hash(odno, salt_hex)` 와 같은 식).
**주문번호(odno)와 그 주문의 실제 누적 체결수량·금액을 브로커 이력에서 먼저 확정**한 뒤 진행하세요.

## 3. 정정 실행 (오프라인 — 브로커 API 호출 없음)

### 3-0. ⚠️ 브로커 체결 확인만으로는 마커를 올릴 수 없습니다

브로커 이력의 누적 체결수량·금액은 **"그 체결이 있었다"** 는 증거일 뿐, **"이미 정산(T·순투입)에 반영했다"** 는
증거가 아닙니다. 마커는 후자를 뜻합니다. 근거 없이 올리면 두 가지 사고가 납니다.

- 실제로는 **미반영**인 체결의 마커를 올리면 → 그 체결이 영구히 **누락**됩니다 (T·순투입 과소).
- 반대로 이미 반영된 체결의 마커를 내리면 → **이중 가산**됩니다 (2026-10-26 과거 72건 재반영 사고).

따라서 마커를 올리려면 **"기존 정산이 이 체결을 반영했다"는 독립 근거**가 필요합니다. 아래 중 **하나 이상**을
확보하고, 스크립트에 근거 파일을 지정해 검증을 통과해야 합니다 (통과 못 하면 정정하지 말고 중단·에스컬레이션).

| 근거 | 확인 방법 |
|------|-----------|
| 과거 실행의 **Evidence** (`orders[]` 에 해당 odno 의 반영 수량·금액) | 암호화 artifact 복호화 후 `orders[].odno` 일치, `qty_delta`/`amount_delta` 가 정정할 QTY/AMOUNT 와 일치 |
| 반영 **이후** 시점의 state 백업/캐시 스냅샷 | 해당 odno 마커 > 0 이고 워터마크가 그 체결 시각 이후 |
| T·순투입 **산술 재현** | 백업 `net_invested` + 반영 체결 순액 == 현재 `net_invested` (±반올림) |

근거가 없거나 서로 모순이면 **마커를 올리지 않습니다.** 증명 불가 과거 주문은 코드가 자동으로 건너뛰므로
(`_marker_proves_unapplied`) 정정하지 않아도 신규 체결 정산은 막히지 않습니다.
**두 사람이 근거를 교차 확인**하세요 (작업자 ≠ 승인자).

### 3-1. 정정 스크립트

정정 전후 canonical 해시를 남기기 위해 **state 전용 스크립트**로 처리합니다 (LIVE 파이프라인 실행 아님).

```bash
uv run python - <<'PY'
import hashlib, json, os, sys
sys.path.insert(0, "src")
from state import load_state, save_state, canonical_state_hash

SYMBOL = "TQQQ"
ODNO = "45025"      # 원문 주문번호 (비공개 Evidence 의 pending_marker_odnos 에서 확인)
QTY = 20            # 브로커 이력의 누적 체결수량 (정수, 합격 기준: 0 이상 정수)
AMOUNT = 1450.00    # 브로커 이력의 누적 체결금액 (USD, float)
# 3-0 의 독립 근거: 복호화한 과거 실행 Evidence 파일 경로 (비우면 중단)
APPLIED_EVIDENCE = os.environ.get("APPLIED_EVIDENCE", "")

# ── 독립 근거 검증 (브로커 체결 확인만으로는 통과 불가) ──
assert APPLIED_EVIDENCE, "기존 정산 반영의 독립 근거(Evidence)가 없습니다 — 3-0 참고, 정정하지 마세요"
raw = open(APPLIED_EVIDENCE, "rb").read()
print("[evidence] sha256 =", hashlib.sha256(raw).hexdigest())   # 로그에 남겨 승인자가 대조
ev = json.loads(raw)
assert ev.get("symbol") == SYMBOL, "Evidence 종목이 다릅니다"
applied = [o for o in ev.get("orders", []) if str(o.get("odno")) == ODNO]
assert applied, f"Evidence 의 반영 내역(orders)에 {ODNO} 이 없습니다 — 반영 근거 없음, 정정 금지"
# 정정할 마커는 "기존 정산이 실제로 반영한 누적 수량·금액" 과 같아야 합니다 (브로커 누적치와 달라도 근거가 우선)
applied_qty = max(int(o.get("cumulative_qty", 0)) for o in applied)
assert applied_qty == int(QTY), f"Evidence 반영 누적수량({applied_qty}) != QTY({QTY}) — 근거 불일치, 정정 금지"
ev_marker = (ev.get("orders_meta_after") or {}).get(ODNO) or {}
if "processed_filled_amount" in ev_marker:
    assert abs(float(ev_marker["processed_filled_amount"]) - float(AMOUNT)) <= 0.02, \
        "Evidence 반영 금액 마커와 AMOUNT 가 다릅니다 — 근거 불일치, 정정 금지"

state = load_state(SYMBOL)
meta = (state.get("orders_meta") or {}).get(ODNO)
assert meta is not None, "orders_meta 에 해당 주문이 없습니다 — 주문번호를 확인하세요"
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

기록 항목(`$AUDIT_DIR/marker-correction.log`): **정정 전 sha256 + canonical hash → 정정 후 canonical hash**,
odno, 정정 전/후 마커, `T`·`net_invested` 값(정정 전후 동일해야 정상), 근거 Evidence 의 sha256, 작업자·승인자.

## 4. 직후 확인 — 읽기 전용 / 리버스 / 실제 저장을 **분리**합니다

⚠️ 세 가지는 목적도 위험도 다릅니다. 하나로 뭉뚱그려 "reconcile 한 번 돌려 확인"하지 마세요.

| 구분 | 명령 | state 저장 | 무엇을 확인하나 |
|------|------|-----------|-----------------|
| **4-A-1 일반 체결 마커 검증** | 일반 DRY + canonical hash 조회 | 안 함 | §3 에서 고친 **일반 체결 마커**·보류 해소 |
| **4-A-2 리버스 전용 감사** | `STATE_REVERSE_AUDIT_ONLY=true` | 안 함 | **리버스 주문**의 브로커 이력 ↔ `orders_meta` 일치 (일반 체결 마커는 검증하지 않음) |
| **4-B 리버스 정산 저장** | `STATE_REVERSE_RECONCILE_ONLY=true` | **함** | 리버스 체결을 state 에 **반영·저장** (마커 정정과 별개 복구 건) |

### 4-A-1. 일반 체결 마커 검증 (읽기 전용 — 마커 정정의 확인은 여기서만)

```bash
sha256sum .state.json | tee -a "$AUDIT_DIR/marker-correction.log"        # ① 실행 전 해시
TRADE_MODE=DRY uv run python trading_bot.py 2>&1 | grep -E "마커|보류|건너뜁" | tail -20   # 일반 DRY: 저장 생략
sha256sum .state.json | tee -a "$AUDIT_DIR/marker-correction.log"        # ② 실행 후 해시 — ①과 **같아야** 합니다
uv run python - <<'PY'      # canonical hash 조회 (읽기 전용: load_state 만 사용)
import sys; sys.path.insert(0, "src")
from state import load_state, canonical_state_hash
print("TQQQ", canonical_state_hash(load_state("TQQQ")))
PY
```

확인 포인트:
1. `마커 누락·무효` 보류 메시지가 **사라졌는지**
2. 조회한 canonical hash 가 §3 의 [after] hash 와 같음 (§2 백업 해시와는 달라야 정상 — 마커가 바뀌었으므로)
3. ①과 ②의 `sha256sum` 이 같음 (읽기 전용이었다는 증거 — 다르면 즉시 중단하고 백업으로 롤백)
4. `T` / `net_invested` 는 §3 의 정정 전후 값과 같아야 함 (마커만 올렸다면 변하지 않아야 정상)
5. DRY 가 남긴 `settlement-<SYMBOL>-*.json` 을 독립 검증기로 확인 — 마커·건너뜀 감사 검사 포함
   (`ops/verify_run.py` 의 `main()` 은 로그 검증용이라 근거 파일에는 아래 함수를 씁니다. 실패 메시지의 주문번호는 `ord#…` 로 마스킹됩니다)

```bash
uv run python - <<'PY'
import glob, sys
sys.path.insert(0, "ops")
import verify_run
for path in sorted(glob.glob("settlement-*.json")):
    ok, problems = verify_run.verify_evidence_file(path, "kis", "DRY")   # broker/mode 는 실제 값으로
    print(path, "OK" if ok else "FAIL", *problems, sep="\n  ")
PY
```

### 4-A-2. 리버스 전용 감사 (읽기 전용 — 리버스 주문이 얽힌 경우에만)

```bash
STATE_REVERSE_AUDIT_ONLY=true TRADE_MODE=DRY uv run python trading_bot.py   # canonical state_hash 출력, 저장 없음
```

이 감사는 브로커 주문이력을 **읽기 전용으로 조회**해 **리버스 주문**만 대조합니다. 통과해도 **일반 체결 마커가 맞다는 뜻이 아닙니다** (4-A-1 로 따로 확인).
리버스 주문이 없는 마커 정정에는 필요하지 않습니다.

### 4-B. 리버스 정산의 실제 저장 — 일반 마커 정정과 **섞지 않습니다**

`STATE_REVERSE_RECONCILE_ONLY=true` 는 `TRADE_MODE=DRY` 여도 **state 를 실제로 저장**합니다.
내부적으로 `update_T_from_history` 전체를 돌리므로 리버스 체결뿐 아니라 일반 체결도 정산하며,
`T` · `net_invested` · 워터마크 · 마커가 **변합니다** → §1 의 "마커만 고친다" 원칙과 충돌합니다.

- 마커 정정 절차의 일부로 실행하지 않습니다. **별도 복구 건**으로 따로 승인받아 진행합니다.
- 실행 직전에 §2-1 의 백업·해시를 **새로** 뜨고(별도 `$AUDIT_DIR`), 4-A-2 리버스 감사를 먼저 통과시킵니다.
- 실행 후에는 변경된 `T`/`net_invested`/워터마크를 리버스 체결 내역과 대조해 기록하고, 달라지면 §5 로 롤백합니다.

```bash
STATE_REVERSE_RECONCILE_ONLY=true TRADE_MODE=DRY uv run python trading_bot.py   # 저장됨!
```

### 4-C. 운영 재개는 **명시적 승인 후에만** (자동 재개 없음)

4-A 를 통과해도 봇은 스스로 재개되지 않습니다. 아래를 모두 충족하고 승인자가 기록한 뒤에만 재개합니다.

- [ ] §3-0 독립 근거 확인 완료 (근거 파일 sha256 기록)
- [ ] 4-A-1 모든 확인 포인트 통과, 전후 `sha256sum` 동일
- [ ] 리버스 주문이 있었다면 4-A-2 통과 + 4-B 는 별도 승인 완료
- [ ] 1회성 환경변수(§6) 전부 제거 확인, `.env`·스케줄에 남아 있지 않음
- [ ] 승인자(작업자와 다른 사람)·일시·근거를 `$AUDIT_DIR/marker-correction.log` 에 기록

재개는 **스케줄/디스패치를 사람이 다시 켜는 것**으로만 합니다. 확인 직후 자동으로 신규 주문이 나가지 않습니다.
값이 달라지거나 승인이 나지 않으면 신규 주문은 나가지 않은 상태이므로 백업으로 되돌립니다(§5).

## 5. 롤백

```bash
cp -p "$AUDIT_DIR/state.before.json" .state.json
sha256sum .state.json     # §2-1 에서 기록한 원본 해시와 일치 확인
```

## 6. 환경변수는 1회성 (매우 중요)

- `STATE_REVERSE_RECONCILE_ONLY`, `STATE_REVERSE_AUDIT_ONLY`, `STATE_REPAIR_ONLY`,
  `STATE_CLEAR_FENCE_ONLY`, `STATE_NET_INVESTED_REPAIR_ONLY`, `STATE_SET_CYCLE_START_ONLY`,
  `{SYMBOL}_FORCE_T` 는 모두 **1회성 점화용**입니다.
- 확인이 끝나면 **즉시 삭제**하세요. `.env`, `ops/run_daily.sh`, cron, systemd 유닛에 **절대 남기지 않습니다.**
  남아 있으면 매 실행마다 보정/보류 모드로 돌아 신규 주문이 영구 차단됩니다.
- 여러 `STATE_*_ONLY` 를 동시에 켜지 마세요 (배타 그룹).

## 7. 하지 말 것

- LIVE 로 바로 실행해 마커를 확인하지 않기 (실주문 발생) → **DRY 또는 `STATE_REVERSE_AUDIT_ONLY`(읽기 전용)로 확인**
- `STATE_REVERSE_RECONCILE_ONLY` 를 "확인용"으로 쓰기 (DRY 여도 state 가 **저장**됨 → §4-B 별도 복구 건 전용)
- 브로커 체결 확인만으로 마커를 올리기 (§3-0: 기존 정산 반영의 독립 근거 필수)
- 백업·로그를 Git 체크아웃 안이나 공개 위치에 두기 (§2-1: 저장소 밖 700 디렉터리)
- 4-A 통과 직후 자동 재개 (§4-C: 명시적 승인 후 사람이 재개)
- 마커와 함께 `T`/`net_invested`/원장을 "대충 맞추기" (검증기 `ops/verify_run.py` 가 적용 내역을 독립 재계산해 실패로 잡습니다)
- `.state.json` 을 손으로 편집한 뒤 백업·해시 없이 넘어가기
- 마커 하향 정정(재반영 유발)을 단독 수행
