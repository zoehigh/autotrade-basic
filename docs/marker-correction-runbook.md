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

## 8. Actions 복구 워크플로 (리버스 원장 되돌리기·워터마크 복원) 실행 절차

대상: `.github/workflows/repair_reverse_ledger_rewind.yml` (모의 전용, `main` 에서만, 주문·브로커 호출 없음).
이 워크플로는 마커 정정(§3)과 다른 도구입니다 — 리버스 매도가 일반 매도로 오분류 정산된 종목의 state 를 되돌립니다.

### 8-1. 두 번 실행합니다 (한 번에 끝나지 않습니다)

`confirm` 은 실행을 **시작할 때** 입력하는 값이라, 같은 실행 안에서 미리보기를 보고 나서 정할 수 없습니다.
(Environment `state-repair` 에 Required reviewers 가 없으면 승인 단계가 대기 없이 통과합니다.
지금은 **독립된 승인자가 없고, 실행자가 직접 대조하는 구조**입니다 — 승인이 있는 것으로 간주하지 마세요.)

`confirm` 입력으로 실행 범위를 정합니다 (정확히 일치해야 하며, 다르면 미리보기만 합니다).

| `confirm` | 실행 범위 | state 변경 |
|---|---|---|
| (비움) | preview 만 | 없음 (백업도 없음) |
| `BACKUP` (종목 생략 가능, `BACKUP <종목>` 도 허용) | preview → backup (+ hashes) | **없음** — 시크릿·암호화·왕복 검증·업로드 경로 확인과 복원 state 지문 계산 |
| `REWIND <종목>` | preview → approve → backup → apply | 있음 (복구 적용) |

**`BACKUP` 모드는 복구 대상이 없는 정상 state 에서도 돌아갑니다.**
- 필요한 입력은 `broker` 와 `restore_key`(해당 브로커의 최신 캐시) 뿐입니다. 복구 대상·`expect_t`·hash·`symbol` 은 요구하지 않고 쓰지도 않습니다.
- **봇의 복구 미리보기는 실행하지 않습니다.** 미리보기는 복구 대상(오분류된 리버스 매도 등)이 있어야 성공하므로, 정상 state 에서 실행하면 백업 전에 실패합니다.
- **파일 SHA-256 은 생략할 수 있습니다** (`BACKUP` 에서만). 입력했다면 반드시 복원본과 대조해 다르면 실패하고, 생략했다면 요약에
  **"사전 지문 대조 없이 백업"** 으로 표시됩니다. `REWIND`·미리보기에서는 SHA 가 필수입니다.
- 시크릿이 없는 별도 `hashes` 잡이 복원본의 **파일 SHA-256 과 파일에 든 모든 종목의 canonical hash·T** 를 요약에 출력합니다.
  이 값이 이어서 `REWIND` 를 실행할 때 넣을 입력값(`expect_file_sha256`·`expect_hash`·`expect_t`·`guard_hash`)입니다.
  (canonical hash 는 저장소 코드로 계산하므로, 백업 passphrase 가 있는 `backup` 잡이 아니라 시크릿 없는 잡에서 계산합니다.)

⚠️ 비운 미리보기로는 `backup`·`apply` 가 생략되어 **백업 경로를 확인할 수 없습니다.** 실제 복구 전에 `BACKUP` 으로 한 번 실행해
백업 시크릿·암호화·artifact 업로드가 되는지 먼저 확인하세요 (`apply` 의 저장 경로까지 확인되는 것은 아니며, 그 경로는 실제 복구 실행에서 처음 검증됩니다).

1. **사전 백업(선택이지만 권장)**: `Backup state caches (encrypted)` 워크플로로 복구 대상 캐시를 따로 백업해 둡니다.
   (복구 워크플로도 적용 직전에 자체 암호화 백업을 만들지만, 그 백업은 적용 단계에 들어간 뒤에만 생깁니다.
   `BACKUP` 으로 실행하면 이 워크플로의 백업 잡만 단독으로 돌려 볼 수 있고, 요약에서 REWIND 입력값(SHA·hash·T)도 얻을 수 있습니다.)
2. **1차 실행 — 미리보기**: `confirm` 을 **비우고** 실행합니다. 저장하지 않으며, 실행 요약(Summary)에
   현재값(복원본)과 목표값이 나란히 표시됩니다.
3. **대조 (실행자)**: 미리보기 출력과 **근거 실행의 Evidence** 를 비교합니다. 워터마크 복원이라면
   목표 `last_updated` 가 그 Evidence 의 `watermark_before` 와 같은지 직접 확인합니다.
   (복구 함수·검증기는 목표가 "현재보다 과거"인지만 확인할 뿐 값의 정확성은 확인하지 못합니다.)
4. **2차 실행 — 적용**: **같은 입력**에 `confirm`=`REWIND <종목>` 을 넣어 다시 실행합니다.
   `최신 캐시·hash 재검증 → 암호화 백업 → 복구 → 독립 검증 → 통과 시에만 캐시 저장 → 새 캐시 존재 확인` 순서로 진행됩니다.
5. **확인**: 실행 요약의 감사 기록에서 "독립 검증", "캐시 저장 단계", "새 캐시 존재 확인" 결과를 **각각** 확인합니다
   (검증은 통과했는데 저장이 실패했거나, 저장 단계는 성공으로 끝났지만 새 캐시가 만들어지지 않은 경우를 구분하기 위함 —
   `actions/cache/save` 는 캐시 서비스 장애·예약 충돌에도 경고만 남기고 성공할 수 있습니다). 그 뒤 DRY 1건으로 재정산 결과를 확인합니다.

### 8-2. 거래 슬롯과 겹치지 않게 합니다

- 1차와 2차 실행 **사이**에 거래가 돌아 새 캐시(`state-<broker>-demo-<run_id>`)가 생기면, 2차 실행의
  "최신 캐시 확인"에서 중단됩니다. 그러면 새 `restore_key`·`expect_file_sha256`·hash 로 **처음부터** 다시 해야 합니다.
  → 두 번의 실행은 **다음 거래 슬롯 전에** 끝냅니다.
- 복구 잡은 거래와 같은 concurrency 그룹(`trade-<broker>-demo`)을 씁니다. 같은 그룹에서 **대기 중인 실행은
  새 실행이 들어오면 취소될 수 있으므로** (`cancel-in-progress: false` 여도 기존 대기 작업의 교체는 막지 못함)
  거래 슬롯(Demo 19:10~20:30Z 안팎) 시간대에는 복구를 돌리지 않습니다.

### 8-3. 백업·감사 기록

- **백업 artifact**: `repair-backup-<broker>-<run_id>-<시도 번호>` (gpg 대칭 암호화본 + 원본/암호문 SHA-256 파일). **30일 보관**.
  GitHub "Re-run all jobs" 는 `run_id` 가 같고 시도 번호만 올라가며 artifact 는 덮어쓸 수 없으므로, 이름에 시도 번호를 넣어 재실행이 막히지 않게 했습니다.
  공개 저장소의 artifact 는 누구나 받을 수 있으므로 **평문 state 는 올리지 않습니다.** 복호화는 `STATE_BACKUP_PASSPHRASE` 로 합니다.
  백업(암호화·왕복 검증·업로드)이 실패하면 **복구는 시작되지 않습니다.**
- **passphrase 범위**: `STATE_BACKUP_PASSPHRASE` 는 백업 잡의 암호화 단계에만 주입됩니다. 봇을 실행하는 preview/apply 잡에는 시크릿이 없습니다.
- **감사 기록**: 적용 잡의 실행 요약에 워크플로 커밋, 복원 캐시, 파일 SHA-256(입력값 / 복구 전 사본 계산값 / 복구 후 계산값),
  대상·가드 종목 canonical hash(입력 CAS / 복구 전·후 계산값, 가드 종목 불변 여부), T·net_invested·워터마크 변화,
  근거 Evidence 실행 번호, 단계별 결과를 남깁니다. 실패해도 남으며 주문번호 원문은 쓰지 않습니다(`last_processed_ordno` 는 변경 여부만).
  해시는 별도 단계(`continue-on-error`)가 계산하므로 계산에 실패해도 "(계산 실패)"로만 표시되고 복구·검증·저장 판정에는 영향이 없습니다.
- **최종 요약 잡**: 마지막 `summary` 잡이 preview·approve·backup·apply 의 결과를 한곳에 모아 최종 판정을 남깁니다.
  백업이 실패해 `apply` 가 생략된 경우처럼 적용 잡 요약이 만들어지지 않는 흐름도 여기에 기록됩니다.
- **복구 전 상태로 되돌리기**: 복구가 만든 새 캐시(`state-<broker>-demo-<run_id>`)를 **삭제하면** 복원 키 접두어 규칙상
  직전 캐시(= 복구 전 `restore_key`)가 다시 최신이 됩니다. 캐시 삭제는 되돌릴 수 없는 작업이므로 **별도 승인 후** 진행합니다.
  단, 직전 캐시는 7일 동안 접근이 없으면 만료됩니다 — 만료됐다면 백업 artifact 를 복호화해
  SHA-256 이 입력의 `expect_file_sha256` 과 같은지 확인한 뒤 §5 의 롤백 절차(로컬 복원)를 따르세요.
  (복구 워크플로에는 "백업에서 캐시로 되돌리는" 동작이 없습니다.)

### 8-4. `evidence_run_id` (워터마크 복원에서 필수)

목표값의 근거로 삼은 Evidence 가 들어 있는 **실행 번호(숫자)** 를 입력합니다. 값이 맞다는 증명은 아니고
"무엇을 근거로 이 값을 넣었는지"를 감사 기록에 남기기 위한 것입니다. 되돌리기(`REVERSE_LEDGER_REWIND`)에서는 선택입니다.
