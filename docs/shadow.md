# Shadow 운영 가이드 (v1.4, 로컬/VPS 전용)

SIMULATION ONLY — 실제 주문·`state.json`·텔레그램에 절대 닿지 않는 가상 원장.
GH Actions 미지원 (ephemeral 러너 + `.state.json`만 캐시).

## 1. 구조

- **생성(generate)**: **미국 장 마감 전(프리장)**에 전략 입력과 동일 조건으로 주문 의도만 기록. 회계 불변.
  마감 후에 실행하면 가드(C)가 그 세션을 건너뛰어 의도가 하나도 생기지 않습니다
  (로그: "세션이 이미 마감됨").
- **정산(settle)**: **대상 세션 마감 후**에 그 세션 날짜의 종가로 체결 판정 후 가상 회계 반영.
  미체결은 소멸, 이월 없음. 마감 전/날짜 불일치면 pending 유지.

| 구분 | BUY LOC | SELL LOC | MOC | LIMIT 당일 |
|---|---|---|---|---|
| 체결 조건 | 종가 ≤ 한도 | 종가 ≥ 한도 | 항상 | 종가가 한도 충족 (C1) |
| 체결가 | 종가 | 종가 | 종가 | 한도가 (C1) |

가정 버전 v1.4 (A1–A6 + C1 + E1 + S1 + R1): 전량/무부분체결, 수수료·슬리피지 반영,
사이클 리셋 포함, BUY LOC 현재가 ×1.19 상한 보정(E1), 세션 날짜 기반 정산(S1),
리버스 연속성(R1). 정확도(P0) 반영 — 매도 T는 보유수량 비율 기준
(전량 0 / 목표 ×0.25 / 쿼터 ×0.75), 리버스 주문은 reverse_* 필드 기준,
리셋 시드는 `effective_seed` 영속화(cash 불변). TOSS SELL MOC→$0.01 LOC 프록시는 MOC으로 취급(종가 체결).

### v1.4 신규 규칙

- **세션 날짜 기반 정산(S1)**: generate가 `us_session_date_now()`의 세션 날짜를
  의도와 스냅샷(`session_date`)에 기록합니다. settle은 의도의 `session_date`가
  `us_session_closed()`로 마감 확인되고, `get_daily_candles()`에 **같은 date**의
  캔들이 있을 때만 체결/만료를 확정합니다. 날짜 없는 캔들/최신 종가로 대체하지
  않으며, 조건이 안 되면 의도를 pending으로 유지합니다. 금요일 세션을 토요일에
  정산해도 금요일 종가를 사용합니다.
- **마감 세션 generate 금지**: generate 시점에 해당 세션이 이미 마감됐으면
  (휴장일에는 `us_session_date_now()`가 직전 세션을 반환) 새 의도를 만들지 않고
  상태를 그대로 둡니다. 러너의 `is_trading_day()`는 1차 방어선일 뿐입니다.
- **미정산 pending 보호**: pending이 남아 있으면 generate를 건너뜁니다.
  `SHADOW_FORCE_NEW_INTENTS=true`이면 기존 pending을 `intent_expired`
  (`status=expired`) 원장 이벤트로 남긴 뒤 새 의도를 생성합니다.
- **리버스 연속성(R1)**: `reverse_mode = {active, day_count, cycle_id,
  last_planned_session, cumulative_sell_proceeds}`를 스냅샷에 영속화하고
  전략에 그대로 전달합니다. 활성 상태에서는 T가 `splits-1` 미만으로 내려가도
  리버스 시퀀스가 유지되며, 전략의 `reverse_exit`·전량매도(사이클 종료)·
  보유 0 정리에서만 해제됩니다. 정산 시 리버스 매도 체결대금이
  `cumulative_sell_proceeds`에 누적됩니다.
- **멱등 정산 + 원자적 스냅샷**: 의도별로 (1) 회계 적용 → (2) pending에서 제거
  → (3) 같은 디렉터리 temp + `os.replace`로 스냅샷 저장 → (4) settlement 이벤트
  추가. **스냅샷이 권위**이고 JSONL 원장은 projection입니다. 저장→원장 순서를
  유지하므로 (3)과 (4) 사이 crash 시 원장에 gap이 남을 수 있지만, 재실행해도
  이중 회계는 없습니다 (at-most-once 회계).
- **사이클 리셋 crash-safe**: 전량매도 등 사이클 종료가 감지되면 per-intent
  체크포인트 스냅샷에 `cycle_reset_pending` 마커를 함께 저장합니다. settle은
  **시작 시마다** 마커(또는 보유 0 + T>0/net_invested>0)를 확인해 리셋을
  멱등 적용하므로, 마지막 저장 직전 crash가 나도 다음 settle이 복구합니다.
  settle crash 직후 generate가 먼저 실행되는 경우에도, generate가 마커를 보고
  전략 계산 전에 리셋을 먼저 복구합니다 (낡은 effective_seed로 주문 크기를
  계산하지 않도록).
  리셋이 적용될 때 `cycle_reset` 원장 이벤트(사후 `virtual_state_after` 포함)가
  1회 기록되며, 반복 settle해도 effective_seed가 두 번 복리되지 않습니다.
- **손상 스냅샷 가드**: 스냅샷 JSON을 읽을 수 없으면 조용히 새로 시작하지 않고
  `ShadowSnapshotCorruptError`로 중단합니다. 운영자가 의도적으로 새로 시작할
  때만 `SHADOW_RESET_ON_CORRUPT=true`로 1회 실행하며, 이때 `snapshot_reset`
  원장 이벤트에 손실 사실이 기록됩니다.
- **REINVEST**: 기본 true. false면 다음 사이클 시드로 config seed를 사용하고
  (`effective_seed` 복리 미적용), 가상 cash는 시뮬레이션 결과를 그대로 반영합니다.
- **버전 가드**: 스냅샷 `assumption_version`이 코드와 다르면 자동 마이그레이션
  없이 오류로 중단합니다. 서로 다른 측정 기준이 섞이는 것을 막기 위함입니다.
  기존 `.shadow`를 보관(archive)한 뒤 디렉터리를 삭제하고 새로 시작하세요.
  버전 불일치/손상 스냅샷은 러너가 친절한 한국어 메시지를 출력하고 **exit 2**로
  종료합니다 (휴장일 조기 종료는 기존대로 exit 0).
- **리버스 자동 정리**: generate의 보유 0 + reverse active 정리는 **미정산
  pending이 없을 때만** 동작합니다. 보유 0은 정산이 끝난 뒤에만 신뢰할 수 있기
  때문입니다. `last_planned_session`은 새 세션일 때만 갱신됩니다.

## 2. 설치 (새 VPS)

```bash
git clone <repo> && cd autotrade-basic
curl -LsSf astral.sh/uv/install.sh | sh
uv sync
cp .env.sample .env   # 값 채우기: TOSS 키 3종, TQQQ_SEED/SOXL_SEED, BROKER=toss, BROKER_MODE=real, TRADE_MODE=DRY
```

- `{SYMBOL}_SPLITS`: 미설정 시 40. 전략 의도(20)와 다르면 명시.
- 토스 허용 IP에 VPS 등록 (미등록 시 403).
- 시간대 KST 확인 (`timedatectl | grep zone`, 아니면 crontab 첫 줄에 `CRON_TZ=Asia/Seoul`).

## 3. 수동 실행

```bash
TRADE_MODE=DRY uv run python scripts/shadow_runner.py --phase generate [--symbol TQQQ --exchange NAS]
TRADE_MODE=DRY uv run python scripts/shadow_runner.py --phase settle
```

`--symbol` 생략 시 `SYMBOLS` 전체. 첫 generate가 T=0 fresh 부트스트랩.

## 4. cron (평일, KST)

```
# 생성: 월~금 17시(서머) / 동절기는 18시로 변경
0 17 * * 1-5 cd /path/to/autotrade-basic && mkdir -p .shadow && PATH=/home/ubuntu/.local/bin:/usr/bin:/bin TRADE_MODE=DRY flock -n /tmp/shadow-gen.lock /home/ubuntu/.local/bin/uv run python scripts/shadow_runner.py --phase generate >> .shadow/runner.log 2>&1
# 정산: 화~토 07시 (금요일분을 토요일 아침에 정산)
0 7 * * 2-6 cd /path/to/autotrade-basic && mkdir -p .shadow && PATH=/home/ubuntu/.local/bin:/usr/bin:/bin TRADE_MODE=DRY flock -n /tmp/shadow-settle.lock /home/ubuntu/.local/bin/uv run python scripts/shadow_runner.py --phase settle >> .shadow/runner.log 2>&1
```

- `which uv` 경로·repo 경로 교체. 휴장일은 cron 요일(주말) + runner `is_trading_day()` 조기종료로 커버.
- 미국 휴장일 전후에도 settle은 의도의 세션 날짜와 일치하는 캔들만 사용하므로 날짜가 어긋나면
  의도를 pending으로 유지합니다. 휴장 다음 generate는 마감 세션 가드로 건너뜁니다.

## 5. 확인 방법

- `.shadow/runner.log`: GENERATE 의도 N건 / SETTLE 체결 x/N건.
- `.shadow/snapshots/<SYM>_latest.json`: `phase`가 settled, `pending_intents`가 [] 인지.
- `.shadow/ledger/<SYM>_YYYY-MM.jsonl`: intent → settlement 1:1 링크 (`intent_event_id`),
  `cycle_reset`/`snapshot_reset`/`intent_expired` 이벤트 확인.
- **pending 감시**: pending intent가 **2세션 이상** 남아 있으면 대상 세션 날짜와
  일치하는 일봉 캔들을 찾지 못한 것입니다. API 지연, 휴장 갭, 세션 날짜 불일치를
  점검하세요 (최신 종가로 대체하지 않습니다).

## 6. 초기화

`rm -rf .shadow` 1회 후 다음 generate가 새로 만듦. v1.1~v1.3 원장/스냅샷은 v1.4와 호환되지 않으며, 버전 가드가 자동 마이그레이션을 거부합니다. 버전 업그레이드 시 필수.

## 7. 트러블슈팅

| 증상 | 원인·대처 |
|---|---|
| 403 허용되지 않은 IP | 토스 WTS 허용 IP에 머신 등록 |
| `prerequisite-required` | 투자성향 앱 등록 (보유 무관, 신규매수 차단) |
| `insufficient-buying-power` | 주문가능금액 부족. shadow 생성에는 지장 없음(가상 현금 기준) |
| `{SYM}_SEED` 에러 | `.env` 시드 누락 |
| pendings가 비워지지 않음 | 정산 cron(화~토) 동작 여부 + 로그 확인. 2세션 이상이면 날짜 일치 캔들/휴장 갭 점검 |
| `스냅샷 가정 버전 불일치` (exit 2) | 코드/스냅샷 버전 다름. `.shadow` archive 후 삭제하고 새로 시작 |
| `스냅샷을 읽을 수 없습니다` (exit 2) | 파일 손상. archive/점검 후 복구하거나, 의도적 재시작은 `SHADOW_RESET_ON_CORRUPT=true` 1회 |
| generate 후 의도 0건 ("세션이 이미 마감됨") | generate를 장 마감 후에 실행함. 프리장 슬롯으로 옮길 것 |
| 없는 주식 매도 기록 | v1.1 코드. `git pull` 후 `.shadow` 삭제 |
