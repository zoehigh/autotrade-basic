# 작업 히스토리

## 2026-06-23 — `_apply_recent_history_dt` odno 타입 버그 수정

- **문제:** SOXL T값이 1.0이 아닌 0.5로 계산됨. 전량매도 시 사이클 종료 리포트 미생성.
- **원인:**
  - `_apply_recent_history_dt()`에서 `odno`를 `str()` 변환 없이 `orders_meta` 딕셔너리 키로 조회 → int 타입 odno 오면 key miss
  - 전량매도 감지 시 `_completed_cycle_start` 플래그 미설정
- **수정:** `src/state.py` 5개 라인 `str()` 변환 + `_completed_cycle_start` 플래그 추가
- **테스트:** 3개 추가 (int odno 매칭, 전량매도 플래그, 초기모드 플래그), 27/27 통과
- **커밋:** `381ccaa`

---

## 2026-06-25 — GitHub Actions line-by-line 출력

- **문제:** GitHub Actions 로그에서 Python 출력이 한꺼번에 나옴 (버퍼링)
- **원인:** Python stdout이 파이프 연결 시 block-buffered 모드로 전환됨
- **수정:**
  - `.github/workflows/trade_base.yml`: `PYTHONUNBUFFERED: 1` 환경변수 추가
  - `trading_bot.py`: `main()` 시작부에 `sys.stdout.reconfigure(line_buffering=True)` 추가 (이중 안전장치)
- **테스트:** `tests/test_github_actions_output.py` 신규 추가, 3/3 통과
- **커밋:** `e4150e4`

---

## 2026-06-25 — KIS API 타임아웃 재시도 확장

- **문제:** GitHub Actions Azure 러너에서 KIS API(`openapi.koreainvestment.com:9443`) 간헐적 타임아웃 → 토큰 발급 실패 → 거래 누락
- **원인:** 연결 타임아웃 10s + 재시도 3회(총 ~15s)로는 Azure 간헐적 네트워크 지연 대응 부족
- **수정:**
  - `src/authentication.py`: `NETWORK_MAX_RETRIES = 20` 전용 상수 분리 (기존 `MAX_RETRIES=3`은 EGW00133/rate-limit 유지), 백오프 cap 30s → 60s
  - `.github/workflows/trade_base.yml`: `KIS_CONNECT_TIMEOUT: 30` 환경변수 추가
  - `trading_bot.py`: Real+LIVE 모드에서 프리장 오픈 전 KST 기준 sleep 로직 추가 (DST 17:00 / non-DST 18:00)
- **운영 가이드:** 외부 cron 1회 dispatch — Demo KST 04:10, Real KST 17:30 (→ `docs/cron-dispatch-guide.md` 참조)
- **커밋:** `d291d6a`, `57c7565`, `3656151`
