# 메리츠 정정 계열 적용 계획 (#12 + #15 → #14 → #13)

현재 운영: `muhan-v4` **detached HEAD `342a9f1`** (PR #12 이전 head). 이 문서는 계획일 뿐이며,
각 단계는 **사람이 별도로 승인한 뒤** 수행합니다. 이 문서의 어떤 명령도 아직 실행하지 않았습니다.

## 0. 커밋 지도 — 단계마다 "이전 패치를 모두 포함한" SHA 만 체크아웃

| 단계 | 포함 내용 | 체크아웃할 SHA | 이전 단계 포함 확인 |
|---|---|---|---|
| (현재 운영) | #12 구버전 | `342a9f1` | — |
| **1단계** | #12 보완(`5d8d20e`,`22b007c`) + #15 어댑터 | **`6044141`** | `342a9f1 ⊂ 22b007c ⊂ 6044141` |
| **2단계** | 1단계 + #14 감시기 | **`0b3dec0`** | `6044141 ⊂ 0b3dec0`, `7465a96 ⊂ 0b3dec0` |
| **3단계** | 2단계 + #13 합산(기본 OFF) | **`6d3a6c9`** | `0b3dec0 ⊂ 6d3a6c9`, `c2e2bbf ⊂ 6d3a6c9` |

⚠️ **PR head 를 그대로 체크아웃하지 마세요.** #14 head(`7465a96`)에는 #15(`6044141`)가 **없고**,
#13 head(`c2e2bbf`)에는 #14·#15 가 없습니다. 위 표의 SHA 만 사용합니다 (`integration/meritz-all` 브랜치의 병합 이력).
모든 단계 공통 확인: `git merge-base --is-ancestor <이전SHA> <적용SHA> && echo OK`.

검증 결과 (mock·격리 테스트): 1단계 `6044141` 1234 passed / 2단계 `0b3dec0` 1310 passed / 3단계 전체 1325 passed + 통합 호환성 4개.
플래그 OFF(기본)에서 3단계의 주문표는 `22b007c` 와 3개 시세에서 바이트 단위로 동일합니다.

공통 사전 조건 (매 단계): 18:30 LIVE 실행·18:45 `verify_run` 종료 후 ~ 다음 날 18:25 이전, `pgrep -af "trading_bot|run_daily|reprice"` 비어 있음,
`.reprice_journal.json`·`.watch_halt.json` 없음, 운영 `git status` 깨끗, 크론 변경 없음.

---

## 1. 1단계 배포: #12 + #15 (`6044141`)

변경 파일 (이 외가 보이면 중단): `docs/meritz-reprice-hotfix-runbook.md`, `ops/meritz_buy_loc_reprice.py`,
`src/broker/meritz/adapter.py`, `src/meritz_reprice.py`, `tests/test_meritz_rejected_amend_status.py`, `tests/test_meritz_reprice.py`.
크론·`pyproject.toml`·`uv.lock`·`.env` 무변경.

**사전 확인 (읽기 전용)**
```bash
cd ~/investment/muhan-v4
git status -sb && git rev-parse HEAD                 # detached, 342a9f1...
git fetch origin hotfix/meritz-buy-loc-reprice fix/meritz-adapter-rejected-amend-status
git merge-base --is-ancestor 342a9f1 6044141 && echo OK
git diff --stat 342a9f1 6044141                      # 위 6개 파일뿐인지
pgrep -af "trading_bot|run_daily"; ls .bot.lock .reprice_journal.json .watch_halt.json 2>&1 | head
flock -n .run.lock true && echo "락 비어 있음"
crontab -l | sha256sum                               # 기록 (변경 없음 확인용, 기대값 dbf46f3f...)
sha256sum .state.json                                # 기록 (기대값 699598893c58...)
```

**백업 (저장소 밖) → 배포**
```bash
mkdir -p ~/backup && cp -p .state.json ~/backup/state-$(date +%Y%m%d-%H%M%S).json
crontab -l > ~/backup/crontab-$(date +%Y%m%d-%H%M%S).txt
git checkout --detach 6044141 && git rev-parse HEAD  # 6044141...
```

**배포 직후 확인 (모두 읽기 전용)**
```bash
.venv/bin/python -I -c "import sys; sys.path[:0]=['src','ops']; import run_lock, meritz_reprice; print('import OK')"
.venv/bin/python -I ops/meritz_buy_loc_reprice.py --symbol SOXL      # 계획만. 주문·파일 생성 없음
git status --short; sha256sum .state.json; crontab -l | sha256sum      # 모두 사전 기록과 동일
```
합격 기준: import OK, 계획 출력 정상, 상태·크론 해시 불변, `git status` 깨끗.

**첫 LIVE 실행(18:30) 후 확인**
- 18:45 `verify_run` 통과, `.bot.lock` 생성, 새 보정 주문의 `orders_meta` 에 `original_price`/`order_price`.
- #15 영향: 과거에 **거부된 정정 행이 있던 원주문**이 이제 `미체결`로 보여 봇이 취소 대상으로 삼습니다. 이미 죽은 주문이면
  취소가 `5636`/`4062` 로 거절되고 "미확정"으로 그 종목이 하루 보류될 수 있습니다(의도된 보수 동작). 로그에서
  `미확정`·`취소` 문구와 알림을 확인하고, 보류되면 앱/HTS 로 해당 주문 상태를 확인합니다. 일반 날(거부된 정정 없음)은 변화 없음.

**롤백 (1단계 → `342a9f1`)**
```bash
cd ~/investment/muhan-v4
ls .reprice_journal.json 2>&1        # 있으면 먼저 런북 4절로 해소 (구버전 봇도 journal 게이트는 있으나 REJECTED_LATE 표식은 모름)
pgrep -af "trading_bot|run_daily"    # 비어 있어야 함
git checkout --detach 342a9f1 && git rev-parse HEAD
```
state 백업을 **맹목적으로 복원하지 않습니다**: 정정으로 주문번호가 바뀌었거나 uncertain journal 이 있으면 복원 시 새 주문번호가 미등록이 됩니다.
복원이 필요하면 journal·정정 이력부터 대조한 뒤 별도 승인.

---

## 2. 2단계: #14 감시기 — 코드 배포(`0b3dec0`) 후 **조회 전용 DRY**

2단계 변경: `.gitignore`, `docs/meritz-reprice-watch.md`, `ops/cron.example`, `ops/meritz_reprice_watch.py`, `ops/run_reprice_watch.sh`,
`src/meritz_reprice.py`, `src/meritz_watch.py`, `src/run_lock.py`, `tests/test_meritz_reprice_watch.py` (+ 1단계 파일).
**배포만으로는 아무것도 실행되지 않습니다** (크론 미등록). 배포 절차는 1단계와 같고 SHA 만 `0b3dec0`, 비교 기준은 `6044141`.

**(a) 크론 없이 수동 1회 DRY** (감시 창 안인 시각, 예: 서머타임 19:00~04:30 KST / 동절기 20:00~05:30 KST)
```bash
cd ~/investment/muhan-v4
./ops/run_reprice_watch.sh; echo "exit=$?"       # 기본 DRY (REPRICE_WATCH_MODE/TRADE_MODE/ALLOW_BROKER_ORDERS 는 .env 무시)
tail -30 logs/reprice-watch-$(date +%Y%m%d).log
sha256sum .state.json                              # 불변 (DRY 는 매매 state 를 쓰지 않음)
ls -la .watch.lock .watch_state.json .watch_halt.json 2>&1   # 감시 기록 파일만 생길 수 있음 (.gitignore 대상)
```
- 감시 창 밖/휴장일이면 API 호출 없이 `exit=0` — 정상입니다. 창 안에서 후보가 있으면 `[DRY] 정정 후보` 로그와 비긴급 텔레그램 1건.
- 합격: `exit=0`, `.state.json` 해시 불변, `.watch_halt.json` 없음, `git status` 깨끗, 후보가 있으면 앱 시세·원래 매수가와 일치.
- 오늘 이미 접수된 구 주문은 `original_price` 가 없어 후보가 나오지 않을 수 있습니다(제한). 이 경우 AMEND 경로는 mock 으로만 검증된 상태입니다.

**(b) DRY 크론 등록 (사람이 결정; 이 계획은 등록하지 않음)**
```bash
crontab -l > ~/backup/crontab-$(date +%Y%m%d-%H%M%S).txt          # 등록 전 백업
( crontab -l; echo '*/10 19-23,0-5 * * 1-6 /home/ubuntu/investment/muhan-v4/ops/run_reprice_watch.sh' ) | crontab -
crontab -l | tail -3
```
- 줄에 `REPRICE_WATCH_MODE`·`TRADE_MODE`·`ALLOW_BROKER_ORDERS` 를 **넣지 않으면** DRY 입니다. 기본 설정(`.env` 에 `REPRICE_WATCH_*` 없음)으로 시작:
  창 ET 06:00~15:30(마감−30분), 최소 변경폭 $0.05, 최소 간격 20분, 일일 한도 3, 실행당 1건.
- 필요하면 `REPRICE_WATCH_SYMBOLS=SOXL` 로 종목 제한. 18:30 LIVE 와 겹쳐도 `.run.lock`/`.bot.lock` 으로 조용히 건너뜁니다.
- 일일 점검: `logs/reprice-watch-*.log`, 텔레그램(후보·보류·오류), 종료 코드 1/2 가 없는지.
  종료 1 = 오류·중단(알림 발송), 2 = 설정 오류.
- 며칠 DRY 로 확인할 것: 알림이 중복되지 않는지(같은 후보 1회), 실패한 알림이 다음 실행에 재시도되는지, 창 밖에서 조용한지,
  휴장일·조기마감일 동작, `.state.json` 해시가 감시기 때문에 바뀌지 않는지.

**(c) DRY 중지·복구**
```bash
( crontab -l | grep -v run_reprice_watch.sh ) | crontab -         # 감시 줄만 제거 (백업과 diff 로 확인)
crontab -l | diff - ~/backup/crontab-<등록 전 백업>.txt && echo "크론 원복 확인"
```
| 증상 | 조치 |
|---|---|
| `.watch_halt.json` 생김 (`RECORD_CORRUPT` 등) | 원인 확인(`.watch_state.json.corrupt-*` 보존됨) 후 사람이 `.watch_halt.json` 삭제. DRY 는 계속 동작 |
| `미해결 정정 journal` DRY 알림 | 자동 복구 없음. 런북 4절대로 수동 도구로 이력 확인 |
| `git status` 에 `.watch_state.json.corrupt-*` 가 보임 | 손상 기록 격리 파일은 `.gitignore` 에 없습니다(알려진 한계 — 후속 수정 후보). 내용을 확인한 뒤 `~/backup/` 으로 옮기면 `git status` 가 다시 깨끗해집니다 |
| `.watch.lock` 잔존 | 빈 파일이라 무해 (flock 기반) |
| 알림이 오지 않음 | 로그의 `알림 발송 실패/발송되지 않았습니다` 확인 — 다음 실행에서 재시도됨 |

**2단계 롤백 (→ `6044141`)**: 크론 줄 제거 → `pgrep` 확인 → `git checkout --detach 6044141`. 감시 파일(`.watch_*`)은 지워도 되고 남겨도 무해합니다.

**APPLY(실정정)는 이 계획 범위 밖입니다.** 켜려면 DRY 관찰 결과를 보고 별도 승인 후:
크론 줄에 `REPRICE_WATCH_MODE=APPLY TRADE_MODE=LIVE ALLOW_BROKER_ORDERS=1` 세 값을 모두 명시하고 `REPRICE_WATCH_SYMBOLS=SOXL` 로 한 종목부터,
첫 정정 결과를 런북 3·4절 절차로 확인합니다. 미검증 항목: 실제 정정 응답 형태, 부분체결 정정 수량·정정 가능 시간, 시세 신선도(응답에 시각 없음).

---

## 3. 3단계: #13 합산 (`6d3a6c9`, 기본 OFF)

배포만 하면 동작이 바뀌지 않습니다 (`MERGE_CAPPED_ADDITIONAL` 미설정 = OFF; `22b007c` 와 주문표 동일 검증).
켜기 전: `.env` 에 플래그를 **넣지 않은 채** 배포 → 하루 이상 관찰 → 별도 승인으로 `MERGE_CAPPED_ADDITIONAL=true` + 먼저 DRY 프리뷰로 주문표 확인
(보정 시 정상매수 수량이 합산되는지, 추가매수 계단과 중복 없는지) → 첫 체결 후 `verify_run` 으로 정산이 한 번만 반영됐는지 확인.
합산 주문은 가격 정정 도구·감시기가 건드리지 않습니다 (통합 테스트로 고정).
롤백: 플래그 제거(즉시 OFF) 또는 `0b3dec0` 로 체크아웃.

---

## 4. 하지 않는 것 (이 계획의 경계)

main 병합, 크론 변경(2(b) 는 승인 후 사람이), 실제 주문·정정·취소, `MERGE_CAPPED_ADDITIONAL`·APPLY 활성화, `.env`·state 수정.
