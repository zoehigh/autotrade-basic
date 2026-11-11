# 2단계 계획 — #14 감시기 배포 + 조회 전용 DRY 운영

상태: **계획만 있음. 2단계 배포·크론 등록은 아직 하지 않았습니다** (별도 승인 필요).
1단계(#12+#15, `604414111fc22c0f0d1dd588dc9c04090a8d13a6`)는 2026-11-11 운영 `muhan-v4` 에 배포 완료
(배포 전 HEAD `342a9f1`, 상태 백업 `~/backup/state-20261111-134545.json`). 첫 LIVE·`verify_run` 확인 결과는 별도 기록.

## 1. 정확한 대상

| 항목 | 값 |
|---|---|
| 2단계 적용 SHA (전체) | **`8c1c3b05cb0a68df7a8a8a6d9fede980082d3e8e`** (= 1단계 `6044141` + #14 `4cf182d`) |
| 포함 확인 | `6044141 ⊂ 8c1c3b0`, `4cf182d ⊂ 8c1c3b0` |
| 가져올 원격 ref | `integration/meritz-all` (브랜치 head 는 문서 커밋으로 계속 바뀌므로 head 가 아니라 **위 SHA 가 그 조상인지**로 확인) |
| 허용 변경 파일 (1단계 대비, 12개) | `.gitignore`, `docs/meritz-reprice-watch.md`, `ops/cron.example`, `ops/meritz_reprice_watch.py`, `ops/run_reprice_watch.sh`, `src/meritz_readonly.py`, `src/meritz_reprice.py`, `src/meritz_watch.py`, `src/run_lock.py`, `tests/test_meritz_readonly.py`, `tests/test_meritz_reprice_watch.py`, `tests/test_meritz_watch_schedule.py` |
| 테스트 | 2단계 SHA 전체 1356 passed (읽기 전용 방어선 20 + 스케줄 24 포함) |

⚠️ #14 PR head(`4cf182d`)를 체크아웃하면 #15 가 빠집니다. 반드시 위 SHA 로 고정합니다.

## 2. 배포 전제와 절차 (승인 후)

전제: 1단계 첫 LIVE(18:30)·`verify_run`(18:45) 확인이 합격, 운영 HEAD `6044141`·clean, 프로세스·`.reprice_journal.json`·`.watch_halt.json` 없음.
시각: 18:45 이후 ~ 다음 날 18:25 이전 (크론 등록 전이므로 감시 창과는 무관).

```bash
cd ~/investment/muhan-v4
WANT=8c1c3b05cb0a68df7a8a8a6d9fede980082d3e8e; OLD=604414111fc22c0f0d1dd588dc9c04090a8d13a6
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
| 동절기(현재, EST, ~2027-03-13) | 20:00 | 다음 날 05:30 |
| 서머타임(2027-03-14 일요일 ~ 2027-11-06) | 19:00 | 다음 날 04:30 |

```bash
cd ~/investment/muhan-v4
./ops/run_reprice_watch.sh; echo "exit=$?"
tail -20 logs/reprice-watch-$(date +%Y%m%d).log            # 첫 줄: "모드=DRY 감시 창 안 ..."
grep 'state 불변 확인' logs/reprice-watch-$(date +%Y%m%d).log | tail -1   # 이 실행이 락을 잡은 동안 전후 동일 (9절)
git status --porcelain | wc -l                               # 0
ls -la .watch.lock .watch_state.json 2>&1                    # 새로 생김 (.gitignore 대상, 매매 state 아님)
```
합격: `exit=0`, `모드=DRY`, 로그에 `state 불변 확인`, `.watch_halt.json` 없음, `git status` 0건.
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
| 매매 state | `.state.json` | 감시기 DRY 는 **읽기만**. 불변 검사는 락을 잡은 실행 전후 비교(9절) — 날짜 간 해시 비교는 LIVE 갱신 때문에 무의미 |
| 감시 기록 | `.watch_state.json` (+ `.tmp`, `.corrupt-<시각>`) | 일일 횟수·알림 중복 방지. 매매 state 와 별개. `.gitignore` 대상 |
| 중단 표식 | `.watch_halt.json` | 있으면 APPLY 가 정정하지 않음. DRY 에서도 `RECORD_CORRUPT` 로 생길 수 있음. 사람이 원인 확인 후 삭제 |
| 락 | `.watch.lock`, `.run.lock`, `.bot.lock` | 모두 `flock` 비차단. **파일 존재만으로 판단하지 말 것** — 실행 중 프로세스·`flock -n` 결과·로그 메시지로 판단 |
| journal | `.reprice_journal.json` | DRY 는 만들지 않음. 있으면 이전 정정 중단의 흔적 → 런북 4절 |
| 로그 | `logs/reprice-watch-YYYYMMDD.log` | 일별 파일. 창 밖 실행은 1줄씩이라 증가량 미미 (현재 `logs/` 1.3MB·여유 디스크 34GB). 오류 시 `[watch-launcher] exit=…` 줄 추가 |
| 크론 백업 | `~/backup/crontab-*.txt`, state 백업 `~/backup/state-*.json` | 저장소 밖 |

## 6. 주문 POST 차단 방식 — DRY 읽기 전용 방어선 (구현됨, `src/meritz_readonly.py`)

감시기 DRY 는 브로커를 `ReadOnlyBroker` 로 감싸서만 사용합니다. **실제 브로커 호출 전에** 막는 서로 독립된 두 겹:

| 층 | 막는 것 | 방식 |
|---|---|---|
| ① 메서드 허용 목록 | `place_order`·`modify_order`·`cancel_order`, 그 밖에 허용 목록에 없는 **모든** 속성(미래에 추가될 주문 변경 메서드·`_request`·`_session` 포함) | 허용 4개(`exchange_code`·`get_order_history`·`get_stock_price`·`close`) 외 접근은 즉시 `ReadOnlyViolation` — 내부 브로커 메서드는 호출되지 않음 |
| ② 전송 계층 | 프록시를 우회해 내부 브로커를 직접 써도 나가는 주문 POST | 브로커 HTTP 세션(`_session`)의 `post` 를 차단. 어댑터의 주문·정정·취소는 모두 `_session.post` 경유 |

- **인증 토큰 발급은 허용:** 토큰은 `broker/meritz/auth.py` 가 `requests.post` 로 세션과 **별개로** 요청하므로 ② 의 대상이 아닙니다 (모든 POST 를 막으면 인증까지 막혀 조회 불가).
  테스트가 "토큰 POST 는 나가고 세션 POST 는 차단"을 함께 고정합니다.
- **fail-closed:** ② 를 설치하지 못하면 조회도 하지 않고 중단(`READONLY_VIOLATION`). 위반 시 중단 표식 + 긴급 알림(종료 1).
- **변경 범위:** DRY 분기에만 적용. **APPLY 경로·LIVE 봇·어댑터 코드는 그대로**입니다 (APPLY 는 기존 3중 게이트: 모드·`TRADE_MODE=LIVE`·`ALLOW_BROKER_ORDERS=1`).
- 이 밖의 층: 감시기 DRY 분기가 `apply_amend` 를 호출하지 않음, 런처가 `DRY`/`DRY`/`ALLOW_BROKER_ORDERS=0` 을 명시 export(`.env` 무시), APPLY 게이트 부족 시 종료 2.
- **남는 한계:** 어댑터 자체(`modify_order` 등)에는 `ALLOW_BROKER_ORDERS` 런타임 방어선이 없습니다 (AGENTS.md 의 `_assert_order_allowed` 는 preflight 테스트 스크립트 전용). LIVE 경로를 건드리지 않기 위해 이번에도 추가하지 않았으며,
  따라서 **APPLY 의 보호는 게이트에 의존**합니다 — DRY 는 위 두 겹으로 별도 보호됩니다.

운영 중 **POST 가 나가지 않았다는 사후 증거** (매일):
```bash
cd ~/investment/muhan-v4
grep -c "모드=APPLY" logs/reprice-watch-*.log          # 0 이어야 함
grep -cE "읽기 전용 위반|READONLY_VIOLATION|STATE_CHANGED_IN_DRY" logs/reprice-watch-*.log    # 0 이어야 함
grep -cE "정정 완료|정정 미접수|정정 접수 여부 불확실|정정 후 이력에서" logs/reprice-watch-*.log   # 0 이어야 함
ls .reprice_journal.json .watch_halt.json 2>&1          # 둘 다 없어야 함
```

## 7. 일일 점검 · 중단 조건 · 제거

일일 점검 (조회 전용): `logs/reprice-watch-*.log` 에 `exit=` 줄(오류) 유무, 텔레그램 알림 중복 여부, 위 6절 사후 증거 4개, 로그의 `state 불변 확인` 줄(실행마다),
`git status --porcelain` 0건, 인증 오류(토큰 발급 실패·rate limit) 유무.

**즉시 크론 줄 제거**해야 하는 경우: `모드=APPLY` 가 한 번이라도 보임 / 정정 관련 로그 · `.reprice_journal.json` 발생 / 종료 2 반복 / 토큰·rate-limit 오류 반복 /
`git status` 가 더러워짐 / `STATE_CHANGED_IN_DRY`·`READONLY_VIOLATION` 발생.

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

## 9. state 불변 검사 방식

DRY 불변 검사는 **감시기가 `.watch.lock`·`.run.lock`·`.bot.lock` 을 모두 잡은 동안**의 `.state.json` 해시를 실행 전후로 비교합니다(로그 `state 불변 확인 (락 보유 중 전후 동일, <해시>)`).
락을 잡고 있는 동안에는 LIVE 봇(18:30)·수동 도구·정정 도구가 state 를 쓸 수 없으므로, 전후 차이는 DRY(또는 락을 무시한 무언가)의 변경으로 단정할 수 있습니다.
반대로 **정상 LIVE 가 18:30 에 state 를 갱신한 것은 락이 풀린 시간의 변화라 DRY 의 변경으로 오인되지 않습니다** — 그래서 "어제 해시와 오늘 해시 비교"는 쓰지 않습니다.
락을 잡은 동안 바뀌면 `STATE_CHANGED_IN_DRY` 중단 표식 + 긴급 알림(종료 1)이고, 크론 줄 제거 기준에 해당합니다.

## 10. 스케줄 검증 결과 (서버 실측 + 실제 NYSE 달력 테스트)

| 점검 | 결과 |
|---|---|
| 서버 크론 시간대 | `Asia/Seoul`(KST +0900), NTP 동기, crontab 에 `CRON_TZ=Asia/Seoul` 명시 (실측). 서버 `TZ` 환경변수 없음 |
| KST 자정 이후 세션 날짜 | 감시기 세션 = **ET 날짜**, 18:30 KST 주문의 세션(`_order_us_session_date`: KST 일시→ET 날짜)과 동일. 예) KST 11-12 18:30 주문 ↔ KST 11-12 21:00·23:40·**11-13 00:20·03:00** 실행 모두 세션 `20261112` (동절기·서머타임 4개 날짜 검증). 감시 기록(일일 횟수)은 자정에 초기화되지 않고 다음 세션 20:00(KST)에 새로 시작 |
| 서머타임 | 창 시작 ET 06:00 = 동절기 20:00 / 서머타임 19:00 KST, 종료 15:30 ET = 05:30 / 04:30 KST. 전환일(2027-03-14, 2027-11-07) 전후 영업일 5개 날짜를 정각·1분 전 경계로 검증. **문서의 이전 날짜(2027-03-08)는 오기였고 2027-03-14 가 맞음** |
| 휴장 | 추수감사절·성탄절·MLK·주말은 저녁·자정 이후 모두 닫힘 (`미국 휴장일 또는 주말`), 달력 조회 실패는 fail-closed |
| 조기 폐장 | 2026-11-27·2026-12-24 (13:00 ET 폐장): 창 종료 12:30 ET 로 단축, 12:29 열림/12:30 닫힘 |
| 크론 커버리지 | **실제 크론 줄(`ops/cron.example`)을 파싱**해 2026-11-12~2027-09-30 의 모든 세션(200+개, 조기폐장·전환일 포함)을 검사: 창 시작 후 20분 안에 첫 실행, 실행 간 간격 ≤ 20분, 종료 전 20분 안에 마지막 실행. 크론 줄을 일부러 망가뜨리면(새벽 시간 축소·토요일 제외·시작 늦춤) 실패함을 확인 |
| 달력 범위 | 달력은 실행 시점 기준 약 1년 앞까지 열려 있어(2027-12-31 은 범위 밖 오류) 운영에는 영향 없음 — 범위 밖이면 fail-closed(감시 안 함) |
