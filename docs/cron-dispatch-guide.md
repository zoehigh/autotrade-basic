# 외부 Cron 서비스 Dispatch 설정 가이드

> KIS API 타임아웃 재시도 대응 + 중복 주문 방지를 위한 1회 dispatch 설정

---

## 핵심 원칙: 1회 dispatch만 설정

- **각 환경(Demo/Real)당 1회만 dispatch** — 2회 dispatch 시 중복 주문 발생 위험
- GitHub Actions 내장 `schedule` cron을 사용하지 않고 **외부 cron 서비스**로 `repository_dispatch` 트리거
- 외부 cron 서비스 예시: cron-job.org, EasyCron, AWS EventBridge

---

## 권장 Dispatch 시간

### Demo (`trigger_trade_demo`)

| 항목 | 값 |
|------|-----|
| Dispatch type | `trigger_trade_demo` |
| Dispatch 시각 (KST) | **04:10** |
| Dispatch 시각 (UTC) | **19:10** (KST - 9h) |
| 대상 워크플로우 | `.github/workflows/trade_demo.yml` |

**타이밍 설명:**
- KST 04:10 dispatch → GitHub Actions runner 시작 (~1-2분)
- tokenP 재시도 최대 ~10분 (20회, backoff 최대 60s)
- KST 04:20~04:25경 tokenP 성공 → 전략 실행
- 정규장 내 (KST 22:30~05:00)이므로 바로 진행 가능
- 장 마감(KST 05:00) 전 충분한 여유

### Real (`trigger_trade_real`)

| 항목 | 값 |
|------|-----|
| Dispatch type | `trigger_trade_real` |
| Dispatch 시각 (KST) | **17:30** |
| Dispatch 시각 (UTC) | **08:30** (KST - 9h) |
| 대상 워크플로우 | `.github/workflows/trade_real.yml` |

**타이밍 설명:**
- KST 17:30 dispatch → GitHub Actions runner 시작 (~1-2분)
- tokenP 재시도 최대 ~10분 → KST 17:40~17:45경 tokenP 성공
- **DST 적용 시:** 프리장 17:00 오픈 → dispatch 시각에 이미 오픈 → 바로 진행
- **DST 미적용 시:** 프리장 18:00 오픈 → trading_bot.py가 KST 기준으로 18:00까지 sleep → 진행
- KST 17:30은 프리장 오픈(DST 17:00 / non-DST 18:00) 직후이므로 tokenP 재시도 시간이 충분함

---

## DISPATCH_TYPE 매핑

| Dispatch Type | 대상 Workflow | KIS_MODE |
|---------------|---------------|----------|
| `trigger_trade_demo` | `trade_demo.yml` | `demo` |
| `trigger_trade_real` | `trade_real.yml` | `real` |

---

## 외부 Cron 서비스 설정 예시 (cron-job.org)

1. [cron-job.org](https://cron-job.org)에 접속 후 회원가입
2. **Create Cron Job** 클릭
3. 설정:

| 필드 | Demo | Real |
|------|------|------|
| Title | `AutoTrade Demo` | `AutoTrade Real` |
| URL | `https://api.github.com/repos/{owner}/{repo}/dispatches` | 동일 |
| Method | `POST` | `POST` |
| Headers | `Authorization: Bearer {PAT}`<br>`Accept: application/vnd.github+json` | 동일 |
| Body | `{"event_type": "trigger_trade_demo"}` | `{"event_type": "trigger_trade_real"}` |
| Cron Expression | `10 19 * * 1-5` (UTC) | `30 8 * * 1-5` (UTC) |
| Timezone | UTC | UTC |

**참고:**
- PAT(Personal Access Token)은 GitHub Settings > Developer settings > Personal access tokens > Fine-grained tokens에서 생성
- PAT 권한: `Actions: Read/Write`, `Contents: Read`
- Cron 표현식: `분 시 일 월 요일` (UTC 기준)
  - Demo: `10 19 * * 1-5` = KST 04:10, 월-금
  - Real: `30 8 * * 1-5` = KST 17:30, 월-금

---

## 검증 방법

1. 실제 dispatch 후 GitHub Actions 실행 로그 확인
   ```
   https://github.com/{owner}/{repo}/actions
   ```
2. tokenP 재시도 로그 확인: `"network_retry"` 문자열 검색
3. Real+LIVE: sleep 메시지 확인: `"프리장 오픈 대기 중"` 문자열 검색
4. 중복 dispatch 발생하지 않았는지 확인 (동일 환경에서 1회만 실행)

---

## 주의사항

- **절대 2회 dispatch하지 않음** — Demo/Real 각 1회만 설정
- PAT가 노출되지 않도록 주의 (GitHub Secrets 또는 cron 서비스의 secure variable 사용)
- cron 서비스 중단 시 거래 누락 가능 — 서비스 health check 권장
- GitHub Token 만료 시 refresh 필요
- 미국 DST 전환일(3월 둘째 주 일요일 / 11월 첫째 주 일요일)에도 동일한 UTC 시간 유지 (KST 기준 시각은 자동 조정)
