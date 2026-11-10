# 메리츠 BUY LOC 사후 재조정 hotfix — 배포·복구 런북

기준: 운영 `5e97b1180fb73b5749954b226bc1e19a539fcb9b` (`muhan-v4`, main).
**main 병합은 배포의 조건이 아닙니다.** 이 브랜치는 운영 커밋 위에 쌓였으므로 지정 SHA 를
그대로 체크아웃해 적용합니다. 최신 main 반영은 별도 작업입니다.

## 0. 적용 대상 확인 (배포 전, 읽기 전용)

적용 SHA 는 승인된 PR #12 head 입니다 (`<APPROVED_SHA>` — 보고서에 기재된 값만 사용).

```bash
cd ~/investment/muhan-v4
git status -sb                                   # main, 변경 없음이어야 함
git rev-parse HEAD                               # 5e97b1180fb73b5749954b226bc1e19a539fcb9b
git fetch origin hotfix/meritz-buy-loc-reprice
git merge-base --is-ancestor 5e97b11 <APPROVED_SHA> && echo OK   # 운영 커밋 위에 쌓였는지
git diff --stat 5e97b11 <APPROVED_SHA>           # 아래 8개 파일 외 변경이 있으면 중단
```

허용 변경 파일 (이 외 파일이 보이면 배포 중단):
`.gitignore`, `docs/meritz-reprice-hotfix-runbook.md`, `ops/meritz_buy_loc_reprice.py`, `src/meritz_reprice.py`,
`src/run_lock.py`, `src/strategy.py`, `tests/test_meritz_reprice.py`, `trading_bot.py`.
`.gitignore` 는 `.bot.lock`·`.reprice_journal.json` 을 무시하도록 추가한 것입니다 (없으면 첫 LIVE 실행 뒤
운영 `git status` 가 더러워져 다음 배포·점검의 "변경 없음" 확인이 깨집니다).
`ops/run_daily.sh`·크론·`pyproject.toml`·`uv.lock` 은 변경되지 않습니다 (의존성 추가 없음).

## 1. 배포 (승인 후, 사람이 수행)

시각: 18:30 LIVE 실행과 18:45 검증이 끝난 뒤 ~ 다음 날 18:25 전. 장중 정정이 필요한 날은 3절을 먼저 읽을 것.

```bash
cd ~/investment/muhan-v4
pgrep -af "trading_bot|run_daily"                # 비어 있어야 함
flock -n .run.lock true && echo "락 비어 있음"
cp -p .state.json ~/backup/state-$(date +%Y%m%d-%H%M%S).json   # 저장소 밖 백업 + sha256 기록
sha256sum .state.json
git checkout --detach <APPROVED_SHA>             # main 포인터는 건드리지 않음
git rev-parse HEAD                               # <APPROVED_SHA> 와 일치 확인
```

배포 직후 확인 (모두 읽기 전용, 주문 없음):
```bash
.venv/bin/python -I -c "import sys; sys.path[:0]=['src','ops']; import run_lock, meritz_reprice; print('import OK')"
.venv/bin/python -I ops/meritz_buy_loc_reprice.py --symbol SOXL     # 계획만 출력, 파일 생성 없음
sha256sum .state.json                            # 배포 전과 동일해야 함
```
- 조회 모드는 `.bot.lock`·journal·state 를 만들거나 바꾸지 않습니다.
- 첫 LIVE 실행(18:30) 후 확인: `.bot.lock` 생성, 18:45 `verify_run` 통과, 새로 접수된
  보정 주문의 `orders_meta` 에 `original_price`/`order_price` 기록.

## 2. 롤백

```bash
cd ~/investment/muhan-v4
ls .reprice_journal.json 2>&1                    # 있으면 먼저 4절로 해소 (구버전 봇은 journal 을 보지 않음)
pgrep -af "trading_bot|run_daily"                # 비어 있어야 함
git checkout main                                # 5e97b11
git rev-parse HEAD
```
`.bot.lock`/`.reprice.lock` 은 빈 파일이라 남겨도 무해합니다. `orders_meta` 의 추가 키
(`original_price`, `order_price`, `amended_from`)는 구버전이 무시합니다. 정정으로 바뀐 주문번호는
이미 새 키로 옮겨져 있으므로 state 를 되돌리지 말 것 (정정 이력 없이 백업을 복원하면 새 주문번호가 미등록이 됨).

## 3. 실제 정정 절차 (AMEND 계획이 나왔을 때만, 별도 승인)

전제: 운영 체크아웃, 봇 미실행, `.run.lock`·`.bot.lock` 비어 있음, 미해결 journal 없음.

1. 계획 확인 (조회 전용): `ops/meritz_buy_loc_reprice.py --symbol SOXL --original <주문번호>=<원래 매수가>`
   - 오늘 이미 접수된 주문은 `original_price` 가 없으므로 `--original` 필수.
     원래 가격 근거는 전략 재계산 결과(`price_cap_notes` 의 from 값)와 대조해 확인.
   - 계획에 `거부된 정정 행` 보류가 보이면 해당 주문은 건드리지 않고 앱/HTS 에서 직접 확인.
2. 실행: `TRADE_MODE=LIVE ALLOW_BROKER_ORDERS=1 BROKER=meritz ... --apply --confirm "REPRICE SOXL"`
   - 정정 직전 이력·시세를 다시 조회해 재계획하고, 정정 직후 이력으로 새 주문 접수를 확인한 뒤에만
     `orders_meta` 키를 옮깁니다.
   - 결과: `AMENDED`(정상) / `REJECTED`(미접수 확정, 원주문 유지) / `REJECTED_LATE`(이력에 거부로 남음, 메타 이동 안 함,
     원주문 생존 불명 → **보류**) / `UNCERTAIN`·`LINK_FAILED`(접수 불확실 → 보류). 세 보류 결과는 모두
     **즉시 전체 중단**(다른 주문·종목도 더 정정하지 않음)하고 journal 항목을 **유지**하며, `REJECTED_LATE` 는
     `review_required` 표식을 남깁니다. journal 이 남아 있는 동안 **봇은 해당 종목의 정산·신규 주문을 보류**합니다
     (어댑터가 거부된 정정 행도 원주문을 `정정됨`·잔량 0 으로 보여 주어, 살아 있는 원주문을 봇의 잔여 주문 취소가
     건너뛰고 신규 주문과 중복될 수 있기 때문).
3. 정정 가능 시간·부분체결 정정 수량은 **문서로 확인되지 않았습니다** (부분체결 주문은 계획에서 제외됩니다).

## 4. 중단·복구

| 증상 | 의미 | 조치 |
|---|---|---|
| `.reprice_journal.json` 존재, 봇이 `정산·주문을 보류합니다` | 정정 접수 후 state 연결 전에 중단됨 | 도구를 `--apply` 없이 실행해 이력 대조 결과 확인 → 일치하면 `--apply --confirm` 로 연결. **journal 을 수동 삭제하지 말 것** |
| `불확실 유지(접수 0/거부 0)` | 이력에 정정 행이 아직 없음 | 앱에서 주문 확인 후 잠시 뒤 재실행. 확인 전 재주문 금지 |
| `정정 거부 확인 ... 보류` | 거부된 정정. 원주문 생존 여부 이력으로 판별 불가 | 앱/HTS 에서 원주문 확인. 원주문 메타는 유지되므로 체결되면 정상 정산됨 |
| `원주문 확인이 필요한 항목 ...` (`review_required`) | `REJECTED_LATE` 뒤 journal 표식. 봇이 해당 종목 보류 중 | ① 앱/HTS 에서 원주문이 살아 있는지 확인, 살아 있으면 직접 취소(또는 의도적으로 유지) ② `--apply --confirm "REPRICE <종목>" --original-checked` 로 정리 — **확인 없이 `--original-checked` 를 붙이지 말 것.** 정리해야 봇이 해당 종목을 재개 |
| `다른 실행이 락을 쥐고 있습니다` | 봇/런처/다른 도구 실행 중 | 끝난 뒤 재시도. 락 파일 삭제 금지 (flock 은 프로세스 종료 시 자동 해제) |
| `[중단] ...: 미해결 정정 journal ... 정산·저장을 하지 않습니다` (`exit 1`) | 유지보수 경로(reconcile-only, REPAIR:REINFERENCE, REPAIR:FORCE_T, net_invested repair)가 journal 보류 종목을 대상에 포함 | **여러 종목을 대상으로 하면 하나만 보류돼도 실행 전체가 중단**됩니다 (부분 저장 방지). 보류되지 않은 종목만 따로 지정해 실행하거나, 위 첫 행의 절차로 journal 을 먼저 해소 |

## 5. 알려진 한계 (이 hotfix 가 해결하지 않는 것)

후속 과제로 기록 (이번 범위에 포함하지 않음):
- 레거시 `STATE_*_ONLY` 복구 경로(`_repair_state_only`, `_clear_fence_only`, `_set_cycle_start_only`,
  `_assume_reverse_expiry_only`)에는 journal 보류 게이트가 없음. `_repair_state_only` 는 설정에 따라 T 와 정산 원장도
  바꾸므로 "정산하지 않아 안전"하다고 가정하지 말 것. 기존 백업·승인 절차를 그대로 지키고, 후속에서 게이트 확장.
- 어댑터가 거부된 정정 행에도 원주문을 `정정됨`·잔량 0 으로 표시하는 문제 개선.
- 시세 응답 시각 검증.


- 이력만으로는 거부된 정정 뒤 원주문이 살아 있는지 알 수 없음 (어댑터가 `정정됨`·잔량 0 으로 표시). 도구는 보류하고,
  운영 봇은 정정을 하지 않으므로 영향 없음. 어댑터 개선은 별도 PR.
- 시세 응답에 시각 필드가 없어 신선도를 검증할 수 없음. 정정 직전 재조회로만 완화.
- 재조회~정정 호출 사이 체결 경쟁은 남음 (정정 거부로 안전하게 끝남).
- 정정 후 가격이 더 오르면 재실행이 필요함 (LOC 는 종가 기준).
- 수량 합산·동적 추가매수·전략 공식은 변경하지 않음.

## 6. 보류 조건 — 도구가 정정하지 않거나 사람이 중단해야 하는 경우

| 항목 | 한계 | 도구 동작 | 사람의 판단 기준 |
|---|---|---|---|
| 부분체결·체결 있는 주문 | 부분체결 후 정정 수량의 의미를 문서로 확인하지 못함 (이력에도 사례 없음) | 계획에서 SKIP — 정정하지 않음 | 수동 판단. 이 도구로 정정하지 말 것 |
| 거부된 정정 행이 있는 원주문 | 이력은 원주문을 `정정됨`·잔량 0 으로 보여 주어 생존 여부 판별 불가 | 계획에서 SKIP(보류), 정정 직후 이력이 `거부`면 메타를 옮기지 않고 원주문 메타 유지 + journal 표식·봇 종목 보류·전체 중단 | 앱/HTS 에서 원주문 확인·정리 후 `--original-checked` 로 해소 (생존 여부를 확정할 수 없으면 보류 유지). 살아 있으면 체결이 원주문 메타로 정상 정산됨 |
| 시세 신선도 | 시세 응답에 시각 필드가 없음 (3초 간격 샘플에서 값이 갱신되는 것만 확인) | 정정 직전 재조회 1회만 수행, 신선도 검증 불가 | 실행 전에 앱 시세와 대조해 큰 차이(예: 1% 이상)가 있으면 중단 |
| 정정 가능 시간 | 문서로 확인하지 못함. 접수된 이력은 17:12~18:32 KST(프리마켓)뿐 | 시간 제한 판정 없음 — 브로커 거부(`REJECTED`/`REJECTED_LATE`)에 의존 | 위 시간대 밖(정규장 등)에서는 첫 시도를 1건으로 제한하고 결과 확인 후 진행 |
| 재조회~정정 사이 체결 | 조회와 호출 사이 경쟁 | 정정 거부로 안전하게 종료, 메타 불변 | 거부 후 이력 재확인 |

## 7. 최초 적용 후 확인 절차

배포 직후 (이미 1절에 있는 항목 포함):
1. `git rev-parse HEAD` 가 승인 SHA 와 일치, `git status -sb` 에 변경 없음 (`.gitignore` 반영 확인).
2. 조회 모드 실행 → 오류 없음, `.state.json` 해시 불변, `.bot.lock`·journal 미생성.

첫 LIVE 실행(18:30) 후:
1. `.bot.lock` 생성, 로그에 `[중단]` 락 메시지 없음, 18:45 `verify_run` 통과.
2. 보정된 일반 BUY LOC 의 `orders_meta` 에 `original_price`·`order_price` 기록 (텔레그램의 LOC 가격 보정 알림과 대조).
3. `git status -sb` 가 여전히 깨끗함.

첫 실제 정정 직후 (정정이 필요한 날에만):
1. 결과가 `AMENDED` 이고 이력에 정정 행(정정 코드 1)이 접수 상태로 남음, 원주문은 `정정됨`.
2. `orders_meta` 의 키가 새 주문번호로 이동, `amended_from`·`original_price` 존재, `.reprice_journal.json` 이 비어 있거나 없음.
3. 앱에서 정정 가격·수량(수량 불변) 확인.
4. 다음 18:30 정산 후: 체결이 있었다면 T·`net_invested` 가 **한 번만** 반영됐는지, 18:45 `verify_run` 통과 확인.
5. `REJECTED_LATE`/`UNCERTAIN`/`LINK_FAILED` 가 한 번이라도 나오면 추가 정정을 중단하고 4절 절차로 해소.
