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
git diff --stat 5e97b11 <APPROVED_SHA>           # 아래 7개 파일 외 변경이 있으면 중단
```

허용 변경 파일 (이 외 파일이 보이면 배포 중단):
`docs/meritz-reprice-hotfix-runbook.md`, `ops/meritz_buy_loc_reprice.py`, `src/meritz_reprice.py`,
`src/run_lock.py`, `src/strategy.py`, `tests/test_meritz_reprice.py`, `trading_bot.py`.
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
     원주문 생존 불명 → 보류) / `UNCERTAIN`·`LINK_FAILED`(즉시 중단, journal 유지).
3. 정정 가능 시간·부분체결 정정 수량은 **문서로 확인되지 않았습니다** (부분체결 주문은 계획에서 제외됩니다).

## 4. 중단·복구

| 증상 | 의미 | 조치 |
|---|---|---|
| `.reprice_journal.json` 존재, 봇이 `정산·주문을 보류합니다` | 정정 접수 후 state 연결 전에 중단됨 | 도구를 `--apply` 없이 실행해 이력 대조 결과 확인 → 일치하면 `--apply --confirm` 로 연결. **journal 을 수동 삭제하지 말 것** |
| `불확실 유지(접수 0/거부 0)` | 이력에 정정 행이 아직 없음 | 앱에서 주문 확인 후 잠시 뒤 재실행. 확인 전 재주문 금지 |
| `정정 거부 확인 ... 보류` | 거부된 정정. 원주문 생존 여부 이력으로 판별 불가 | 앱/HTS 에서 원주문 확인. 원주문 메타는 유지되므로 체결되면 정상 정산됨 |
| `다른 실행이 락을 쥐고 있습니다` | 봇/런처/다른 도구 실행 중 | 끝난 뒤 재시도. 락 파일 삭제 금지 (flock 은 프로세스 종료 시 자동 해제) |

## 5. 알려진 한계 (이 hotfix 가 해결하지 않는 것)

- 이력만으로는 거부된 정정 뒤 원주문이 살아 있는지 알 수 없음 (어댑터가 `정정됨`·잔량 0 으로 표시). 도구는 보류하고,
  운영 봇은 정정을 하지 않으므로 영향 없음. 어댑터 개선은 별도 PR.
- 시세 응답에 시각 필드가 없어 신선도를 검증할 수 없음. 정정 직전 재조회로만 완화.
- 재조회~정정 호출 사이 체결 경쟁은 남음 (정정 거부로 안전하게 끝남).
- 정정 후 가격이 더 오르면 재실행이 필요함 (LOC 는 종가 기준).
- 수량 합산·동적 추가매수·전략 공식은 변경하지 않음.
