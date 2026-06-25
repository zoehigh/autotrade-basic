# fix-apply-recent-history-ty-bugs - Work Plan

## TL;DR (For humans)

**What you'll get:** SOXL의 T값이 0.5가 아닌 1.0으로 정상 계산되고, 사이클 종료(전량매도) 발생 시 텔레그램/로그로 수익률 리포트가 출력됩니다.

**Why this approach:** `_apply_recent_history_dt()`가 API에서 받은 주문번호(odno)를 `str()` 변환 없이 `orders_meta` 딕셔너리에서 조회하여 실패하는 것이 근본 원인입니다. `_infer_T_from_full_history()`는 이미 `str()` 변환을 하고 있어 정상 동작하므로, 동일 패턴으로 일관성 있게 수정합니다.

**What it will NOT do:** T값 계산 공식이나 전략 로직 자체는 변경하지 않습니다. `_infer_T_from_full_history()`는 이미 정상이므로 손대지 않습니다.

**Effort:** Quick
**Risk:** Low - 변경 범위가 `_apply_recent_history_dt()` 함수 내 5개 라인 + 테스트 추가에 국한됨
**Decisions to sanity-check:** odno 비교 시 일괄 `str()` 변환 적용 (기존 `_infer_T_from_full_history` 패턴과 일관성)

Your next move: approve this plan, then `$start-work` to execute.

---

> TL;DR (machine): Quick | Low | Fix 2 bugs in _apply_recent_history_dt: (A) odno type mismatch causing orders_meta lookup failure → T=0.5 instead of T=1.0, (B) missing _completed_cycle_start flag on full-sell → cycle report never generated

## Scope
### Must have
1. `_apply_recent_history_dt()`에서 odno를 `str()`로 변환하여 orders_meta/additional_loc_odno 조회 (5개 라인: 674, 678, 700, 701, 704)
2. `_apply_recent_history_dt()` 전량매도 감지 시 `_completed_cycle_start` 플래그 설정
3. int odno로 API 응답이 왔을 때도 정상 동작하는지 검증하는 유닛 테스트 추가
4. 전량매도 시 `_completed_cycle_start`가 설정되는지 검증하는 유닛 테스트 추가

### Must NOT have (guardrails, anti-slop, scope boundaries)
- `_infer_T_from_full_history` 수정 금지 (정상 동작 중)
- `trading_bot.py` 수정 금지
- `register_order_meta_in_state` 수정 금지 (이미 str() 변환 중)
- 추가매수/정상매수 분류 로직 변경 금지

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: TDD + pytest
- Evidence: `.omo/evidence/task-1-verify-odno-type-fix.txt`

## Execution strategy
### Parallel execution waves
- Wave 1: 두 소스코드 수정은 동일 파일(state.py)이라 순차 실행
- Wave 2: 테스트 추가 (소스 수정 후)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. 소스코드 수정 | 없음 | 2 | - |
| 2. 테스트 추가 | 1 | - | - |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [x] 1. Fix 2 bugs in `_apply_recent_history_dt()` — odno str() 변환 + _completed_cycle_start 플래그
  What to do / Must NOT do:
    **Fix A — odno type mismatch (5 lines in src/state.py):**
    1. Line 674: `odno in additional_loc_odno or orders_meta.get(odno, {}).get("is_additional")` 
       → `str(odno) in additional_loc_odno or orders_meta.get(str(odno), {}).get("is_additional")`
    2. Line 678: `odno not in additional_loc_odno and not orders_meta.get(odno, {}).get("is_additional")` 
       → `str(odno) not in additional_loc_odno and not orders_meta.get(str(odno), {}).get("is_additional")`
    3. Line 700: `if orders_meta.get(odno)` 
       → `if orders_meta.get(str(odno))`
    4. Line 701: `if not orders_meta.get(odno)` 
       → `if not orders_meta.get(str(odno))`
    5. Line 704 (meta_buys 본문): `meta = orders_meta[odno]` 
       → `meta = orders_meta[str(odno)]`
    
    **Fix B — _completed_cycle_start flag (line 648-651):**
    Before the `T = 0.0` and `state["cycle_start_date"] = ""` lines, add:
    ```python
    completed_start = state.get("cycle_start_date", "")
    if T > 0 and completed_start:
        state["_completed_cycle_start"] = completed_start
    ```
    This mirrors the exact pattern in `_infer_T_from_full_history()` (line 329-333).
    
    Must NOT do: 
    - `_infer_T_from_full_history` 수정 금지
    - `register_order_meta_in_state` 수정 금지
    - 전략 로직 변경 금지

  Parallelization: Wave 1 | Blocked by: none | Blocks: 2
  References: `src/state.py:648-651, 674, 678, 700-701, 704` | `src/state.py:329-333` (참고용 정상 구현)
  Acceptance criteria:
    1. `uv run python -m pytest tests/test_state_t_updates.py -v` ALL GREEN (기존 테스트와 동일한 통과 기준)
    2. `grep -n "str(odno)" src/state.py`로 5개 라인 모두 str() 변환 확인
    3. `grep -n "_completed_cycle_start" src/state.py`로 2개 라인 확인 (line 331의 기존 + line 649 부근 새로 추가)
  QA scenarios:
    - 기존 테스트 전면 통과 확인
    - `src/state.py`의 해당 라인들에 `str()` 변환이 적용되었는지 grep으로 확인
    - Evidence: `.omo/evidence/task-1-verify-odno-type-fix.txt` (pytest 결과 + grep 결과)
  Commit: Y | `fix: _apply_recent_history_dt odno 타입 불일치 버그 수정 및 전량매도 플래그 추가`

- [x] 2. Add tests for int odno matching and _completed_cycle_start flag
  What to do / Must NOT do:
    **Add to `tests/test_state_t_updates.py`:**
    
    1. `test_apply_recent_history` 클래스에 int odno로 meta 조회 테스트 추가:
       - `_make_buy_order`의 odno를 int `36267`로 전달
       - `register_order_meta_in_state`로 `"36267"` 키로 메타 등록
       - t_target=1.0, 1주 매수 → T가 1.0 증가하는지 검증 (0.5가 아니라!)
       - 이름 예: `test_odno_int_타입_메타_t1_정상반영`
    
    2. `test_apply_recent_history` 클래스에 전량매도 `_completed_cycle_start` 테스트 추가:
       - state: T=4.0, cycle_start_date="2026-06-03", last_updated 적절히 설정
       - 매도 8주 주문 생성 (전량매도, ratio >= 1.0)
       - `update_T_from_history` 실행 후 result에 `_completed_cycle_start` == `"2026-06-03"` 검증
       - T=0.0 검증
       - 이름 예: `test_전량매도_완료_사이클_플래그_설정`
    
    3. `test_infer_T_from_full_history` 클래스에 유사 테스트 추가:
       - 초기 모드(last_updated 없음)에서 전량매도 후 `_completed_cycle_start` 플래그 검증
       - 이름 예: `test_전량매도_초기모드_사이클_플래그`
    
    Must NOT do:
    - 기존 테스트 수정 금지 (추가만 할 것)
    - `_make_buy_order` / `_make_sell_order` 헬퍼 수정 금지 (이미 odno를 `str()`로 저장하지 않음 — raw 값 유지)

  Parallelization: Wave 2 | Blocked by: 1 | Blocks: none
  References: `tests/test_state_t_updates.py` (전체, 특히 line 39-62의 헬퍼 함수, line 154-192의 소액시드 테스트 패턴), `src/state.py:329-333` (플래그 설정 참조 패턴)
  Acceptance criteria:
    1. `uv run python -m pytest tests/test_state_t_updates.py -v` ALL GREEN (기존 + 신규)
    2. 새 테스트 3개가 추가되었는지 확인
    3. int odno 테스트에서 T=1.0 검증 (0.5가 아니라)
  QA scenarios:
    - pytest 전면 통과
    - `grep -n "def test_" tests/test_state_t_updates.py | tail -10`로 새 테스트 3개 확인
    - Evidence: `.omo/evidence/task-2-verify-int-odno-tests.txt` (pytest 결과)

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — APPROVE (All 5 str() conversions applied, _completed_cycle_start flag before T=0, 3 tests added, no scope creep)
- [x] F2. Code quality review — APPROVE (Logic correct, no stubs/TODOs, tests meaningful, Korean naming convention followed)
- [x] F3. Real manual QA — APPROVE (pytest 27/27 passed, str(odno)=7, _completed_cycle_start=2)
- [x] F4. Scope fidelity — APPROVE (Only src/state.py + tests/test_state_t_updates.py changed, no forbidden files touched)

## Commit strategy
1개의 커밋으로 묶음: `fix: _apply_recent_history_dt odno 타입 불일치 버그 수정 및 전량매도 플래그 추가`  
수정 파일: `src/state.py`, `tests/test_state_t_updates.py`

## Success criteria
1. SOXL 초기진입(t_target=1.0) 1주 매수 체결 시 T가 0.5가 아닌 1.0으로 증가
2. 전량매도 발생 시 사이클 종료 리포트가 텔레그램/로그에 출력
3. 기존 모든 T값 업데이트 테스트가 영향 없이 통과
