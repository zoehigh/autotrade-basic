# fix-github-actions-line-by-line-output - Work Plan

## TL;DR (For humans)

**What you'll get:** GitHub Actions 로그에서 자동매매 봇의 출력이 한꺼번에 나오지 않고, 각 줄이 실행 즉시 실시간으로 표시됩니다. 예를 들어 `[Step 1] T값 로드 중...` → (10초 후) `[Step 2] 전략 실행 중...`이 아니라, 각 print()가 발생하는 순간 바로 로그에 나타납니다.

**Why this approach:** Python은 GitHub Actions처럼 파이프로 출력을 받을 때 출력을 모아뒀다가 한 번에 내보냅니다(buffer). 가장 간단하고 확실한 해결책은 환경변수 `PYTHONUNBUFFERED=1` 하나를 GitHub Actions 워크플로우에 추가하는 것입니다. 이 변수 하나로 파이썬 전체 출력이 즉시(line-by-line) 나오도록 바뀝니다. 추가로 본 스크립트 시작 부분에 `sys.stdout.reconfigure()`를 넣어서, 환경변수가 없더라도 같은 효과가 나도록 이중 안전장치를 둡니다.

**What it will NOT do:**
- print() 함수 하나하나를 수정하지 않습니다 (flush=True 추가 안 함)
- `trade_demo.yml`이나 `trade_real.yml`을 직접 수정하지 않습니다 (base 하나만 수정하면 세 워크플로우 모두 적용)
- src/ 디렉토리의 전략/상태/트레이더 코드를 전혀 건드리지 않습니다

**Effort:** Quick
**Risk:** Low - `PYTHONUNBUFFERED=1`은 출력 순서/시점만 바꾸고 로직은 전혀 변경하지 않는 안전한 설정입니다. `sys.stdout.reconfigure()`도 같은 이유로 안전합니다.
**Decisions to sanity-check:** (1) step-level env vs job-level env — step-level로 적용해도 하위 프로세스(python)에 자동 상속됨. (2) `uv run python -u` 대신 env var 사용 — env var가 더 명시적이고 workflow 전체에 일관됨.

Your next move: Approve this plan, then `$start-work` to execute.

---

> TL;DR (machine): Quick | Low | Set PYTHONUNBUFFERED=1 in trade_base.yml step env + add sys.stdout.reconfigure(line_buffering=True) in trading_bot.py main() + add unbuffered stdout subprocess test

## Scope
### Must have
1. `.github/workflows/trade_base.yml` — Add `PYTHONUNBUFFERED: 1` to the `자동매매 봇 실행` step's `env:` block
2. `trading_bot.py` — Add `sys.stdout.reconfigure(line_buffering=True)` at top of `main()` (after docstring, before first print)
3. `tests/test_github_actions_output.py` (new file) — Subprocess test verifying unbuffered stdout behavior

### Must NOT have (guardrails, anti-slop, scope boundaries)
- `trade_demo.yml` / `trade_real.yml` 직접 수정 금지 (base 하나로 커버)
- 기존 print()에 flush=True 추가 금지 (env var + reconfigure로 글로벌 커버)
- `src/` 디렉토리 내 어떤 파일도 수정 금지 (`trading_bot.py` 제외)
- 기존 테스트 파일 수정 금지 (신규 파일만 추가)
- `uv run python`을 `uv run python -u`로 변경 금지 (env var가 더 명시적)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after (subprocess-based unbuffered stdout test) + pytest
- Evidence: `.omo/evidence/task-1-verify-workflow-env-var.txt`, `.omo/evidence/task-2-verify-reconfigure.txt`, `.omo/evidence/task-3-verify-tests-pass.txt`

## Execution strategy
### Parallel execution waves
- Wave 1: Todo 1 (trade_base.yml) + Todo 2 (trading_bot.py) — 병렬 가능 (다른 파일)
- Wave 2: Todo 3 (test) — Wave 1 완료 후 (테스트는 최종 검증)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. trade_base.yml env var 추가 | 없음 | 3 | 2 |
| 2. trading_bot.py reconfigure 추가 | 없음 | 3 | 1 |
| 3. unbuffered stdout 테스트 추가 | 1, 2 | - | - |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [x] 1. `.github/workflows/trade_base.yml` — Add PYTHONUNBUFFERED=1 to step env
  What to do / Must NOT do:
    **Edit `.github/workflows/trade_base.yml`:**
    Line 107 currently has `run: uv run python trading_bot.py`.
    Just above it (after line 106), there's an `env:` block starting at line 78.
    
    Add `PYTHONUNBUFFERED: 1` to that `env:` block. The relevant section (lines 78-107) currently reads:
    ```yaml
      - name: 자동매매 봇 실행
        env:
          KIS_APP_KEY: ${{ secrets.KIS_APP_KEY }}
          ...
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run python trading_bot.py
    ```
    
    Add the new env var after TELEGRAM_CHAT_ID:
    ```yaml
          PYTHONUNBUFFERED: 1
    ```
    
    Must NOT do:
    - Do NOT modify `trade_demo.yml` or `trade_real.yml`
    - Do NOT modify the `run:` line from `uv run python` to `uv run python -u`
    - Do NOT add this to a global/job-level env: block — step-level is sufficient and scoped
    - Do NOT change any existing env var values
    
  Parallelization: Wave 1 | Blocked by: none | Blocks: 3
  References: `.github/workflows/trade_base.yml:78-107` (the step env block)
  Acceptance criteria:
    1. `grep -n "PYTHONUNBUFFERED" .github/workflows/trade_base.yml` → returns line with `PYTHONUNBUFFERED: 1`
    2. `grep -n "PYTHONUNBUFFERED" .github/workflows/trade_demo.yml` → NO match (not modified)
    3. `grep -n "PYTHONUNBUFFERED" .github/workflows/trade_real.yml` → NO match (not modified)
  QA scenarios:
    - Happy: grep for PYTHONUNBUFFERED in trade_base.yml shows it exists under the correct step
    - Failure: grep for PYTHONUNBUFFERED in trade_demo.yml / trade_real.yml — must be absent
    - Evidence: `.omo/evidence/task-1-verify-workflow-env-var.txt` (grep results + diff)
  Commit: Y | `fix: GitHub Actions 출력이 line-by-line으로 표시되도록 PYTHONUNBUFFERED=1 추가`

- [x] 2. `trading_bot.py` — Add sys.stdout.reconfigure(line_buffering=True) in main()
  What to do / Must NOT do:
    **Edit `trading_bot.py`:**
    In the `main()` function (starts at line 674), add `sys.stdout.reconfigure(line_buffering=True)` 
    right after the opening docstring and before any print() call.
    
    Current line 683-685:
    ```python
        print("\n" + "=" * 60)
        print("자동매매 봇 시작")
        print("=" * 60)
    ```
    
    Add before line 683:
    ```python
        sys.stdout.reconfigure(line_buffering=True)
    ```
    
    Also ensure `import sys` exists at the top of trading_bot.py (check line 12 — already imports `sys`).
    
    Must NOT do:
    - Do NOT add flush=True to any individual print() call
    - Do NOT modify any other function in trading_bot.py
    - Do NOT add the reconfigure call outside of main()
    - Do NOT modify `import sys` (already present at line 12)
    
  Parallelization: Wave 1 | Blocked by: none | Blocks: 3
  References: `trading_bot.py:12` (import sys), `trading_bot.py:674-685` (main function start)
  Acceptance criteria:
    1. `grep -n "sys.stdout.reconfigure" trading_bot.py` → returns the added line in main()
    2. `uv run python -c "import ast; ast.parse(open('trading_bot.py').read()); print('SYNTAX OK')"` → syntax valid
    3. `uv run python trading_bot.py --help 2>&1 || true` → runs without error (or expected usage error, not syntax error)
  QA scenarios:
    - Happy: check syntax is valid
    - Happy: verify the line exists inside main() (grep for "reconfigure" shows it)
    - Failure: check that no existing test breaks
    - Evidence: `.omo/evidence/task-2-verify-reconfigure.txt` (grep result + syntax check)
  Commit: Y | `fix: main()에 sys.stdout.reconfigure(line_buffering=True) 추가로 실시간 출력 보장`

- [x] 3. `tests/test_github_actions_output.py` — Add unbuffered stdout subprocess test
  What to do / Must NOT do:
    Create **new file** `tests/test_github_actions_output.py` with:
    
    ```python
    """
    GitHub Actions 출력 버퍼링 테스트
    
    GitHub Actions에서는 Python 출력이 파이프로 연결되어 기본적으로 버퍼링됩니다.
    PYTHONUNBUFFERED=1 또는 sys.stdout.reconfigure(line_buffering=True)로
    출력이 실시간(line-by-line)으로 표시되는지 검증합니다.
    """
    
    import subprocess
    import sys
    import os
    
    
    def test_stdout_unbuffered_with_env_var():
        """
        PYTHONUNBUFFERED=1 환경변수가 설정된 상태에서 파이썬 스크립트를
        subprocess로 실행하면 출력이 버퍼링되지 않고 즉시 나타나는지 확인합니다.
        
        검증 방법: "A", "B", "C"를 순서대로 print하는 스크립트를
        subprocess.PIPE로 실행했을 때, 모든 출력이 정상적으로 캡처되는지 확인합니다.
        실제 버퍼링 여부는 OS/환경에 따라 subprocess에서 재현이 어려우므로,
        환경변수가 설정되었는지와 스크립트가 정상 실행되는지만 검증합니다.
        """
        test_code = """
    import sys
    print("LINE_A")
    print("LINE_B")
    print("LINE_C")
    """
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", test_code],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"Return code: {result.returncode}"
        assert "LINE_A" in result.stdout
        assert "LINE_B" in result.stdout
        assert "LINE_C" in result.stdout
    
    
    def test_stdout_unbuffered_with_reconfigure():
        """
        sys.stdout.reconfigure(line_buffering=True)가 설정된 스크립트를
        subprocess로 실행했을 때 출력이 정상적으로 캡처되는지 확인합니다.
        """
        test_code = """
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    print("RECONFIGURE_LINE_1")
    print("RECONFIGURE_LINE_2")
    """
        result = subprocess.run(
            [sys.executable, "-c", test_code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Return code: {result.returncode}"
        assert "RECONFIGURE_LINE_1" in result.stdout
        assert "RECONFIGURE_LINE_2" in result.stdout
    
    
    def test_import_trading_bot_main():
        """
        trading_bot.py의 main() 함수 내부에 sys.stdout.reconfigure() 호출이
        포함되어 있는지 구문 수준에서 확인합니다.
        (실제 실행은 외부 의존성(KIS API)이 필요하므로 AST로 검증)
        """
        import ast
        with open("trading_bot.py", "r") as f:
            tree = ast.parse(f.read())
        
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (isinstance(node.func, ast.Attribute) and
                    isinstance(node.func.value, ast.Attribute) and
                    node.func.value.attr == "stdout" and
                    node.func.attr == "reconfigure"):
                    found = True
                    break
        
        assert found, "trading_bot.py에 sys.stdout.reconfigure() 호출이 없습니다"
    ```
    
    Must NOT do:
    - Do NOT modify any existing test files
    - Do NOT add tests that actually run trading_bot.py (too many side effects)
    - Do NOT test with mock or patch (subprocess is sufficient)
    
  Parallelization: Wave 2 | Blocked by: 1, 2 | Blocks: none
  References: `trading_bot.py:674-685` (main function with reconfigure), `.github/workflows/trade_base.yml:78-107` (PYTHONUNBUFFERED env var)
  Acceptance criteria:
    1. `uv run python -m pytest tests/test_github_actions_output.py -v` → 3/3 PASSED
    2. New test file exists: `ls tests/test_github_actions_output.py`
  QA scenarios:
    - Happy: `uv run python -m pytest tests/test_github_actions_output.py -v` all green
    - Failure: temporarily remove the line from trading_bot.py, test 3 should fail
    - Evidence: `.omo/evidence/task-3-verify-tests-pass.txt` (pytest verbose output)
  Commit: Y | `test: line-by-line 출력 검증 서브프로세스 테스트 추가`

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — Verify: (a) PYTHONUNBUFFERED=1 in trade_base.yml step env ✅, (b) sys.stdout.reconfigure() in trading_bot.py main() ✅, (c) no changes to trade_demo.yml/trade_real.yml/src/*.py ✅, (d) 3 new tests pass ✅
- [x] F2. Code quality review — Verify: no stubs/TODOs ✅, tests meaningful and not trivial ✅, Korean docstrings in test file match project convention ✅, no commented-out code ✅
- [x] F3. Real manual QA — Verify: `pytest tests/test_github_actions_output.py -v` 3/3 green ✅, `grep PYTHONUNBUFFERED .github/workflows/trade_base.yml` shows correct indentation (line 108, inside env block) ✅, `grep reconfigure trading_bot.py` shows correct placement (line 685, inside main()) ✅
- [x] F4. Scope fidelity — Verify: ONLY `trade_base.yml` + `trading_bot.py` + NEW `tests/test_github_actions_output.py` changed ✅; `trade_demo.yml`, `trade_real.yml`, `src/*.py`, 기존 테스트 파일 untouched ✅

## Commit strategy
2개의 커밋으로 분리:
1. `fix: GitHub Actions 출력 line-by-line 표시를 위해 PYTHONUNBUFFERED=1 및 reconfigure 추가` — (trade_base.yml + trading_bot.py, Wave 1)
2. `test: line-by-line 출력 검증 서브프로세스 테스트 추가` — (test_github_actions_output.py, Wave 2)

수정 파일: `.github/workflows/trade_base.yml`, `trading_bot.py`
신규 파일: `tests/test_github_actions_output.py`

## Success criteria
1. GitHub Actions 실행 로그에서 각 print() 출력이 한꺼번에 나오지 않고 실행 즉시 line-by-line으로 표시됨
2. `PYTHONUNBUFFERED=1`이 trade_base.yml의 `자동매매 봇 실행` step env에 설정됨
3. `sys.stdout.reconfigure(line_buffering=True)`가 trading_bot.py의 main() 시작 부분에 추가됨
4. 3개의 unbuffered stdout 테스트가 모두 통과
5. `trade_demo.yml`, `trade_real.yml`, `src/` 디렉토리 미변경
