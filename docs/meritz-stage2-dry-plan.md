# 2단계 계획 — #14 감시기 배포 + 조회 전용 DRY 운영

상태: **계획만 있음. 2단계 배포·크론 등록은 아직 하지 않았습니다** (별도 승인 필요).
1단계(#12+#15, `604414111fc22c0f0d1dd588dc9c04090a8d13a6`)는 2026-11-11 운영 `muhan-v4` 에 배포 완료
(배포 전 HEAD `342a9f1`, 상태 백업 `~/backup/state-20261111-134545.json`). 첫 LIVE·`verify_run` 확인 결과는 별도 기록.

## 1. 정확한 대상

| 항목 | 값 |
|---|---|
| 2단계 적용 SHA (전체) | **`23f7ce76fe15c423b999eaf0b4d8e728c35b14f6`** (= 1단계 `6044141` + #14 `c70d3dd`) |
| 포함 확인 | `6044141 ⊂ 23f7ce7`, `c70d3dd ⊂ 23f7ce7` |
| 가져올 원격 ref | `integration/meritz-all` (head `bf948a7f928a64878f55fa2e185477ec4012209c`; 위 SHA 는 그 조상) |
| 허용 변경 파일 (1단계 대비) | `.gitignore`, `docs/meritz-reprice-watch.md`, `ops/cron.example`, `ops/meritz_reprice_watch.py`, `ops/run_reprice_watch.sh`, `src/meritz_reprice.py`, `src/meritz_watch.py`, `src/run_lock.py`, `tests/test_meritz_reprice_watch.py` |
| 테스트 | 2단계 SHA 전체 1312 passed |

⚠️ #14 PR head(`c70d3dd`)를 체크아웃하면 #15 가 빠집니다. 반드시 위 SHA 로 고정합니다.

## 2. 배포 전제와 절차 (승인 후)

전제: 1단계 첫 LIVE(18:30)·`verify_run`(18:45) 확인이 합격, 운영 HEAD `6044141`·clean, 프로세스·`.reprice_journal.json`·`.watch_halt.json` 없음.
시각: 18:45 이후 ~ 다음 날 18:25 이전 (크론 등록 전이므로 감시 창과는 무관).

```bash
cd ~/investment/muhan-v4
WANT=23f7ce76fe15c423b999eaf0b4d8e728c35b14f6; OLD=604414111fc22c0f0d1dd588dc9c04090a8d13a6
[ "$(git rev-parse HEAD)" = "$OLD" ] && [ -z "$(git status --porcelain)" ] || echo "ABORT: HEAD/clean 불일치"
pgrep -af "trading_bot|run_daily|reprice|verify_run" | grep -v pgrep
ls .reprice_journal.json .watch_halt.json 2>&1                       # 둘 다 없어야 함
[ -e .run.lock ] && flock -n .run.lock true && echo "run.lock free"   # 파일이 없으면 flock 이 만들므로 -e 먼저
sha256sum .state.json | cut -c1-12; crontab -l | md5sum | cut -c1-8    # 기록 (크론 기대값 dbf46f3f)
git fetch origin integration/meritz-all
git merge-base --is-ancestor $WANT FETCH_HEAD && git cat-file -e $WANT^{commit} && echo "SHA 확인"
git diff --name-only $OLD $WANT                                        # 위 허용 9개 외 파일이 있으면 중단
mkdir -p ~/backup; TS=$(date +%Y%m%d-%H%M%S)
cp -p .state.json ~/backup/state-$TS.json; crontab -l > ~/backup/crontab-$TS.txt
git checkout --detach $WANT && git rev-parse HEAD && git status --porcelain | wc -l   # 전체 SHA 일치, 0
```
배포 직후(읽기 전용): `.venv/bin/python -I -c "import sys; sys.path[:0]=['src','ops']; import meritz_watch, run_lock; print('import OK')"`,
`.venv/bin/python -I ops/meritz_buy_loc_reprice.py` (계획만), state 해시·크론 md5 불변.
**배포만으로는 아무것도 실행되지 않습니다** (크론 미등록).

## 3. 수동 1회 DRY (크론 등록 전, 감시 창 안에서)

감시 창은 **코드가 ET 로 판단**합니다 (미국 NYSE 영업일, ET 06:00 ~ min(15:30, 정규장 마감−30분); 조기 마감일은 같이 줄어듦).

| 시기 | ET 06:00 → KST | 창 종료(15:30 ET) → KST |
|---|---|---|
| 동절기(현재, EST, ~2027-03-07) | 20:00 | 다음 날 05:30 |
| 서머타임(2027-03-08~) | 19:00 | 다음 날 04:30 |

```bash
cd ~/investment/muhan-v4
./ops/run_reprice_watch.sh; echo "exit=$?"
tail -20 logs/reprice-watch-$(date +%Y%m%d).log            # 첫 줄: "모드=DRY 감시 창 안 ..."
sha256sum .state.json | cut -c1-12                           # 배포 전과 동일
git status --porcelain | wc -l                               # 0
ls -la .watch.lock .watch_state.json 2>&1                    # 새로 생김 (.gitignore 대상, 매매 state 아님)
```
합격: `exit=0`, `모드=DRY`, state 해시 불변, `.watch_halt.json` 없음, `git status` 0건.
(오늘 주문처럼 `original_price` 없는 구 주문은 후보가 나오지 않음 — 정상. AMEND 경로 실데이터 검증은 새 보정 주문이 생긴 날에만 가능.)

## 4. 스케줄 (등록은 승인 후, 지금은 하지 않음)

**권장: `*/20 19-23,0-5 * * 1-6`** (서버 `CRON_TZ=Asia/Seoul`, 이미 crontab 에 설정됨)
- 실행 시각: 19–23시·0–5시의 :00/:20/:40. 실제로 API 를 부르는 시각은 위 감시 창 안뿐이고, 창 밖·휴장일은 로그 1줄만 남기고 즉시 종료.
  동절기 첫 유효 20:00 / 마지막 05:20, 서머타임 첫 19:00 / 마지막 04:20.
- 요일 `1-6`(월~토): 한국 새벽(0~5시)은 미국 전일 세션이므로 토요일 새벽(금요일 세션)까지 포함. 일요일은 제외.
- 18:30 LIVE·18:45 검증과 겹치지 않음 (창 시작이 19:00 이후). 겹쳐도 `.run.lock`/`.bot.lock` 비차단 락으로 조용히 건너뜀.
- **왜 10분이 아니라 20분인가:** 감시기는 실행마다 새 프로세스라 메리츠 **토큰을 매번 새로 발급**합니다(토큰 캐시는 프로세스 메모리뿐).
  10분 주기면 세션당 약 57회(+봇 1회)로 토큰 발급 횟수 제한(미확인)에 걸릴 위험이 있어, 먼저 20분(약 29회)으로 시작해
  인증 오류 없이 며칠 지나면 10분으로 좁힙니다. (`REPRICE_WATCH_MIN_INTERVAL_MIN` 기본 20분과도 일치)
- 알림: 운영 `.env` 의 `QUIET_HOURS=false` 이므로 야간에도 알림이 갑니다. 같은 후보는 1회, 발송 실패는 다음 실행에서 재시도.

등록·확인 (사람이, 승인 후):
```bash
cd ~/investment/muhan-v4
TS=$(date +%Y%m%d-%H%M%S); crontab -l > ~/backup/crontab-pre-watch-$TS.txt        # 등록 전 백업
( crontab -l; echo '*/20 19-23,0-5 * * 1-6 /home/ubuntu/investment/muhan-v4/ops/run_reprice_watch.sh' ) | crontab -
crontab -l | tail -3                                                                  # 기존 매매·검증 줄 그대로 + 새 줄 1개
diff <(crontab -l) ~/backup/crontab-pre-watch-$TS.txt                                 # 차이는 새 줄 1개뿐이어야 함
```
**줄에 `REPRICE_WATCH_MODE`·`TRADE_MODE`·`ALLOW_BROKER_ORDERS` 를 넣지 않습니다.** (넣지 않으면 DRY. 런처는 `.env` 값을 무시하고 `DRY`/`DRY`/`0` 으로 고정)

## 5. 경로

| 용도 | 경로 (`~/investment/muhan-v4/` 기준) | 비고 |
|---|---|---|
| 매매 state | `.state.json` | 감시기 DRY 는 **읽기만** (해시 불변이 합격 기준) |
| 감시 기록 | `.watch_state.json` (+ `.tmp`, `.corrupt-<시각>`) | 일일 횟수·알림 중복 방지. 매매 state 와 별개. `.gitignore` 대상 |
| 중단 표식 | `.watch_halt.json` | 있으면 APPLY 가 정정하지 않음. DRY 에서도 `RECORD_CORRUPT` 로 생길 수 있음. 사람이 원인 확인 후 삭제 |
| 락 | `.watch.lock`, `.run.lock`, `.bot.lock` | 모두 `flock` 비차단. **파일 존재만으로 판단하지 말 것** — 실행 중 프로세스·`flock -n` 결과·로그 메시지로 판단 |
| journal | `.reprice_journal.json` | DRY 는 만들지 않음. 있으면 이전 정정 중단의 흔적 → 런북 4절 |
| 로그 | `logs/reprice-watch-YYYYMMDD.log` | 일별 파일. 창 밖 실행은 1줄씩이라 증가량 미미 (현재 `logs/` 1.3MB·여유 디스크 34GB). 오류 시 `[watch-launcher] exit=…` 줄 추가 |
| 크론 백업 | `~/backup/crontab-*.txt`, state 백업 `~/backup/state-*.json` | 저장소 밖 |

## 6. 주문 POST 차단 방식 — 현재 방어선과 **빈틈**

DRY 에서 주문(정정) POST 가 나가지 않도록 막는 층:
1. **코드 경로:** 감시기 DRY 분기는 `apply_amend` 를 호출하지 않고 알림 후 `return "ok"` 합니다 (`ops/meritz_reprice_watch.py`, `if not apply:`).
   mock 테스트가 DRY 에서 `broker.calls == []`(정정 호출 0)와 매매 state 불변을 고정합니다.
2. **런처 고정:** `ops/run_reprice_watch.sh` 가 `REPRICE_WATCH_MODE=DRY`, `TRADE_MODE=DRY`, `ALLOW_BROKER_ORDERS=0` 을 **명시 export** 하고 `.env` 값을 무시합니다
   (파이썬 `load_dotenv()` 가 `.env` 로 되살리는 경로도 export 로 막음 — e2e 로 확인).
3. **게이트:** `REPRICE_WATCH_MODE=APPLY` 인데 `TRADE_MODE=LIVE`·`ALLOW_BROKER_ORDERS=1`·`BROKER=meritz` 중 하나라도 없으면 DRY 로 낮추지 않고 **종료 2 로 거부**합니다.
4. **조회 전용 사전 점검:** 같은 시각에 도는 수동 도구 기본 모드도 계획만 출력합니다.

⚠️ **빈틈 (실측으로 확인):** 운영 어댑터 `MeritzBroker.modify_order` / `place_order` / `cancel_order` 자체에는 `ALLOW_BROKER_ORDERS` **런타임 방어선이 없습니다**.
AGENTS.md 가 말하는 `_assert_order_allowed()` 는 `tests/test_broker_preflight.py` 안의 함수이며 운영 코드 경로에 없습니다.
따라서 DRY 의 주문 차단은 **1~3번(코드 구조·환경 고정)에 의존**합니다 — 층이 얇지 않지만 독립된 마지막 방어선은 아닙니다.

운영 중 **POST 가 나가지 않았다는 사후 증거** (매일):
```bash
cd ~/investment/muhan-v4
grep -c "모드=APPLY" logs/reprice-watch-*.log          # 0 이어야 함
grep -cE "정정 완료|정정 미접수|정정 접수 여부 불확실|정정 후 이력에서" logs/reprice-watch-*.log   # 0 이어야 함
ls .reprice_journal.json 2>&1                           # 없어야 함 (정정을 시도하면 먼저 journal 을 씀)
.venv/bin/python -I ops/meritz_buy_loc_reprice.py       # 이력에 새 '정정' 행이 생기지 않았는지(주문 이력 건수 변화는 봇 주문 외 없음)
```

**권장 보강 (선택, 코드 변경 → 새 SHA·재검증·재리뷰 필요, 이 계획에는 미포함):** 감시기 DRY 에서 브로커를 읽기 전용 프록시(`modify_order`/`place_order`/`cancel_order` 호출 시 예외)로 감싸
어댑터 수준의 마지막 차단선을 만드는 소규모 PR. 크론 등록 전에 넣는 것을 권장하며, 승인하시면 별도 작업으로 진행합니다.

## 7. 일일 점검 · 중단 조건 · 제거

일일 점검 (조회 전용): `logs/reprice-watch-*.log` 에 `exit=` 줄(오류) 유무, 텔레그램 알림 중복 여부, 위 6절 사후 증거 3개, `.state.json` 해시가
LIVE 정산 외 이유로 바뀌지 않았는지, `git status --porcelain` 0건, 인증 오류(토큰 발급 실패·rate limit) 유무.

**즉시 크론 줄 제거**해야 하는 경우: `모드=APPLY` 가 한 번이라도 보임 / 정정 관련 로그 · `.reprice_journal.json` 발생 / 종료 2 반복 / 토큰·rate-limit 오류 반복 /
`git status` 가 더러워짐 / 매매 state 해시가 LIVE 외 시점에 변함.

제거 (감시 줄만, 기존 매매·검증 줄은 그대로):
```bash
cd ~/investment/muhan-v4
crontab -l > ~/backup/crontab-before-remove-$(date +%Y%m%d-%H%M%S).txt
( crontab -l | grep -vF 'ops/run_reprice_watch.sh' ) | crontab -
crontab -l | grep -c run_reprice_watch                      # 0
crontab -l | md5sum | cut -c1-8                              # 줄이 정확히 추가·제거됐다면 dbf46f3f (1단계 직후 값)
pgrep -af run_reprice_watch | grep -v pgrep || echo "실행 중 없음"   # 이미 도는 실행은 1회 끝나고 종료됨 (kill 불필요)
```
감시 파일(`.watch_*`)은 지워도 되고 남겨도 무해합니다.

**2단계 코드 롤백 (→ `6044141`)**: 크론 줄 제거 → 프로세스 없음 확인 → `.reprice_journal.json` 이 있으면 **먼저 런북 4절**로 해소 →
`git checkout --detach 604414111fc22c0f0d1dd588dc9c04090a8d13a6`. journal·주문 연결이 있으면 이전 state 백업을 복원하지 않습니다.

## 8. DRY 졸업 기준 (APPLY 검토 전)

최소 3개 거래 세션 관찰, 그중 가능하면 보정 주문(`original_price` 기록)이 있는 날 1회 이상에서 후보 가격이 앱 시세·원래 매수가와 일치,
오류·중단 0, 알림 중복 0, 토큰 오류 0. 그래도 **APPLY 는 별도 승인** — 크론 줄에 세 값을 모두 명시하고 `REPRICE_WATCH_SYMBOLS=SOXL` 로 한 종목부터.
미검증: 실제 정정 응답 형태, 부분체결 정정 수량·정정 가능 시간, 시세 신선도(응답에 시각 없음).
