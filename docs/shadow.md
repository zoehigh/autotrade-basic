# Shadow 운영 가이드 (v1.2, 로컬/VPS 전용)

SIMULATION ONLY — 실제 주문·`state.json`·텔레그램에 절대 닿지 않는 가상 원장.
GH Actions 미지원 (ephemeral 러너 + `.state.json`만 캐시).

## 1. 구조

- **생성(generate)**: 실전 슬롯(프리장)에 전략 입력과 동일 조건으로 주문 의도만 기록. 회계 불변.
- **정산(settle)**: 익일 아침에 전일 종가로 체결 판정 후 가상 회계 반영. 미체결은 소멸, 이월 없음.

| 구분 | BUY LOC | SELL LOC | MOC | LIMIT 당일 |
|---|---|---|---|---|
| 체결 조건 | 종가 ≤ 한도 | 종가 ≥ 한도 | 항상 | 종가가 한도 충족 (C1) |
| 체결가 | 종가 | 종가 | 종가 | 한도가 (C1) |

가정 버전 v1.2 (A1–A6 + C1): 전량/무부분체결, 수수료·슬리피지 반영, 사이클 리셋 포함.
TOSS SELL MOC→$0.01 LOC 프록시는 MOC으로 취급(종가 체결).

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
0 17 * * 1-5 cd /path/to/autotrade-basic && PATH=/home/ubuntu/.local/bin:/usr/bin:/bin TRADE_MODE=DRY flock -n /tmp/shadow-gen.lock /home/ubuntu/.local/bin/uv run python scripts/shadow_runner.py --phase generate >> .shadow/runner.log 2>&1
# 정산: 화~토 07시 (금요일분을 토요일 아침에 정산)
0 7 * * 2-6 cd /path/to/autotrade-basic && PATH=/home/ubuntu/.local/bin:/usr/bin:/bin TRADE_MODE=DRY flock -n /tmp/shadow-settle.lock /home/ubuntu/.local/bin/uv run python scripts/shadow_runner.py --phase settle >> .shadow/runner.log 2>&1
```

- `which uv` 경로·repo 경로 교체. 휴장일은 cron 요일(주말) + runner `is_trading_day()` 조기종료로 커버.
- 미국 휴장 다음날 정산: 종가 API가 직전 종가를 주므로 정상 정산됨.

## 5. 확인 방법

- `.shadow/runner.log`: GENERATE 의도 N건 / SETTLE 체결 x/N건.
- `.shadow/snapshots/<SYM>_latest.json`: `phase`가 settled, `pending_intents`가 [] 인지.
- `.shadow/ledger/<SYM>_YYYY-MM.jsonl`: intent → settlement 1:1 링크 (`intent_event_id`).

## 6. 초기화

`rm -rf .shadow` 1회 후 다음 generate가 새로 만듦. v1.1 원장은 v1.2와 호환 안 되므로 버전 업그레이드 시 필수.

## 7. 트러블슈팅

| 증상 | 원인·대처 |
|---|---|
| 403 허용되지 않은 IP | 토스 WTS 허용 IP에 머신 등록 |
| `prerequisite-required` | 투자성향 앱 등록 (보유 무관, 신규매수 차단) |
| `insufficient-buying-power` | 주문가능금액 부족. shadow 생성에는 지장 없음(가상 현금 기준) |
| `{SYM}_SEED` 에러 | `.env` 시드 누락 |
| pendings가 비워지지 않음 | 정산 cron(화~토) 동작 여부 + 로그 확인 |
| 없는 주식 매도 기록 | v1.1 코드. `git pull` 후 `.shadow` 삭제 |
