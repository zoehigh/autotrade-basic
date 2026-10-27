"""state 캐시 저장 가드 테스트 (trade_base.yml).

DRY·미확정·잘못된 값 실행은 새 state 캐시를 만들지 않고, LIVE/REPAIR 만 저장해야 합니다.
- 판단은 입력값/BROKER_MODE 가 아니라 **실제 확정된** `steps.resolve.outputs.trade_mode` 로만 합니다.
- `always()` 는 유지합니다: LIVE 실행이 중간에 실패해도 state 를 보존해야 하므로 `success()` 로 바꾸면 안 됩니다.
- 복원 단계는 모든 모드에서 허용합니다.

GitHub 표현식 엔진을 로컬에서 쓸 수 없으므로, 워크플로의 `if:` 문자열을 이 테스트의 작은 평가기로
직접 평가합니다 (always/success/failure/cancelled, ==, !=, &&, ||, !, 괄호, 문자열 리터럴만 지원 —
그 밖의 문법이 들어오면 평가기가 실패해 변경을 알려줍니다). `==` 는 GitHub 처럼 대소문자를 무시합니다.
"""
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "trade_base.yml"
CONTEXT_KEY = "steps.resolve.outputs.trade_mode"

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^']*')|(?P<op>&&|\|\||==|!=|!|\(|\))|(?P<id>[A-Za-z_][A-Za-z0-9_.]*(?:\(\))?))"
)


def _tokens(expr):
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"지원하지 않는 표현식 문법: {expr[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


def evaluate(expr, ctx, status="success"):
    """GitHub Actions `if:` 표현식(부분집합) 평가. 반환값은 Python 진리값."""
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    toks = _tokens(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def primary():
        kind, val = take()
        if kind == "op" and val == "(":
            v = or_expr()
            assert take() == ("op", ")"), "닫는 괄호 없음"
            return v
        if kind == "str":
            return val[1:-1]
        if kind == "id":
            if val.endswith("()"):
                return {
                    "always()": True,
                    "success()": status == "success",
                    "failure()": status == "failure",
                    "cancelled()": status == "cancelled",
                }[val]
            return ctx.get(val, "")  # 없는 컨텍스트는 null → 빈 문자열
        raise ValueError(f"예상치 못한 토큰: {kind} {val}")

    def eq(a, b):
        if isinstance(a, str) and isinstance(b, str):
            return a.lower() == b.lower()  # GitHub 문자열 비교는 대소문자 무시
        return a == b

    def cmp_expr():
        left = primary()
        kind, val = peek()
        if kind == "op" and val in ("==", "!="):
            take()
            right = primary()
            return eq(left, right) if val == "==" else not eq(left, right)
        return left

    def not_expr():
        if peek() == ("op", "!"):
            take()
            return not not_expr()
        return cmp_expr()

    def and_expr():
        v = not_expr()
        while peek() == ("op", "&&"):
            take()
            r = not_expr()
            v = v and r
        return v

    def or_expr():
        v = and_expr()
        while peek() == ("op", "||"):
            take()
            r = and_expr()
            v = v or r
        return v

    result = or_expr()
    assert pos == len(toks), "표현식 일부가 해석되지 않았습니다"
    return bool(result)


def _steps():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["run-trade"]["steps"]


def _step(name):
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"'{name}' 단계가 없습니다")


SAVE = "state 캐시 저장"
RESTORE = "state 캐시 복원"
NOTICE = "state 캐시 저장 생략 안내"


def _save_decision(mode, status):
    ctx = {CONTEXT_KEY: mode} if mode is not None else {}
    return evaluate(_step(SAVE)["if"], ctx, status)


# ── 4가지 실제 실행 모드 × 거래 job 상태 ─────────────────────────────
@pytest.mark.parametrize("status", ["success", "failure", "cancelled"])
def test_dry_never_saves_new_cache(status):
    assert _save_decision("DRY", status) is False


@pytest.mark.parametrize("status", ["success", "failure", "cancelled"])
def test_live_saves_even_when_run_failed_or_cancelled(status):
    """LIVE 는 일부 주문 처리 후 실패해도 state 를 보존해야 합니다 (always 유지)."""
    assert _save_decision("LIVE", status) is True


@pytest.mark.parametrize("status", ["success", "failure"])
def test_repair_saves(status):
    assert _save_decision("REPAIR", status) is True


@pytest.mark.parametrize("mode", [None, "", "BOGUS", "LIVEX", "DRY-RUN", "TRUE", "0"])
@pytest.mark.parametrize("status", ["success", "failure"])
def test_unresolved_or_invalid_mode_never_saves(mode, status):
    """resolve 단계가 실패했거나(출력 없음) 잘못된 값이면 저장하지 않습니다."""
    assert _save_decision(mode, status) is False


def test_live_failure_is_saved_but_success_only_would_not():
    """회귀 방지: if 를 success() 로 바꾸면 LIVE 실패 시 state 가 사라집니다."""
    cond = _step(SAVE)["if"]
    assert "always()" in cond
    assert "success()" not in cond and "failure()" not in cond


# ── 구조 ─────────────────────────────────────────────────────────────
def test_guard_uses_resolved_mode_not_inputs():
    cond = _step(SAVE)["if"]
    assert CONTEXT_KEY in cond
    for forbidden in ("inputs.", "vars.", "env.", "broker_mode", "github.event"):
        assert forbidden not in cond, f"저장 가드가 {forbidden} 에 의존합니다 (실제 실행 모드와 다를 수 있음)"


def test_restore_is_unconditional_for_all_modes():
    restore = _step(RESTORE)
    assert "if" not in restore, "복원 단계에 조건이 붙었습니다 (DRY 복원은 허용해야 함)"
    assert restore["uses"].startswith("actions/cache/restore")


def test_save_keeps_exact_key_and_path():
    save = _step(SAVE)
    assert save["uses"].startswith("actions/cache/save")
    assert save["with"]["path"] == ".state.json"
    assert "github.run_id" in save["with"]["key"]


def test_resolve_runs_before_save():
    names = [s.get("name") for s in _steps()]
    assert names.index("Resolve and log inputs") < names.index(SAVE)
    assert _step("Resolve and log inputs")["id"] == "resolve"


@pytest.mark.parametrize("mode", [None, "DRY", "LIVE", "REPAIR", "BOGUS", ""])
@pytest.mark.parametrize("status", ["success", "failure"])
def test_notice_is_exact_complement_of_save(mode, status):
    ctx = {CONTEXT_KEY: mode} if mode is not None else {}
    save = evaluate(_step(SAVE)["if"], ctx, status)
    notice = evaluate(_step(NOTICE)["if"], ctx, status)
    assert save != notice


# ── Resolve 단계: 봇(config.py)과 같은 정규화 ────────────────────────
def _resolve_output(mode_value, tmp_path):
    script = _step("Resolve and log inputs")["run"]
    # MODE 대입식만 테스트 값으로 바꾸고, 나머지 ${{ }} 는 자리표시자로 치환
    script = re.sub(r'MODE="\$\{\{.*?\}\}"', f'MODE="{mode_value}"', script, count=1)
    script = re.sub(r"\$\{\{.*?\}\}", "x", script)
    out = tmp_path / "gh_output"
    out.write_text("")
    subprocess.run(
        ["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out)},
        capture_output=True, text=True, timeout=30, check=True,
    )
    values = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return values["trade_mode"]


@pytest.mark.parametrize(
    "raw,expected",
    [("live", "LIVE"), (" LIVE ", "LIVE"), ("Repair", "REPAIR"), ("dry", "DRY"), ("DRY", "DRY"),
     ("bogus", "BOGUS"), ("LI VE", "LI VE")],
)
def test_resolve_normalizes_like_bot_config(raw, expected, tmp_path):
    assert _resolve_output(raw, tmp_path) == expected


@pytest.mark.parametrize("raw", ["live", " LIVE ", "Repair"])
def test_normalized_value_matches_what_the_bot_runs(raw, tmp_path, monkeypatch):
    """워크플로가 저장을 허용한 값은 봇이 실제로 LIVE/REPAIR 로 해석하는 값과 같아야 합니다."""
    import importlib
    import sys

    resolved = _resolve_output(raw, tmp_path)
    assert _save_decision(resolved, "success") is True
    monkeypatch.setenv("TRADE_MODE", resolved)
    sys.path.insert(0, str(ROOT / "src"))
    import config
    importlib.reload(config)
    assert config.TRADE_MODE in ("LIVE", "REPAIR")
    monkeypatch.delenv("TRADE_MODE")
    importlib.reload(config)


def test_invalid_value_is_dry_in_bot_and_blocked_in_workflow(tmp_path, monkeypatch):
    import importlib
    import sys

    resolved = _resolve_output("bogus", tmp_path)
    assert _save_decision(resolved, "success") is False
    monkeypatch.setenv("TRADE_MODE", resolved)
    sys.path.insert(0, str(ROOT / "src"))
    import config
    importlib.reload(config)
    assert config.TRADE_MODE == "DRY"
    monkeypatch.delenv("TRADE_MODE")
    importlib.reload(config)


# ── 표현식 주입 방지: 외부 입력(trade_mode)을 셸 스크립트에 직접 끼워 넣지 않습니다 ──
def test_notice_step_does_not_interpolate_expression_into_shell():
    step = _step(NOTICE)
    assert "${{" not in step["run"], "run 스크립트에 GitHub 표현식이 직접 들어 있습니다 (셸 주입 위험)"
    assert step["env"]["RESOLVED_MODE"] == "${{ steps.resolve.outputs.trade_mode }}"


@pytest.mark.parametrize(
    "hostile",
    ["$(touch PWNED)", "`touch PWNED`", "x'; touch PWNED; echo '", "LIVE\n::error::injected", '"; touch PWNED; "'],
)
def test_notice_step_does_not_execute_hostile_mode_value(hostile, tmp_path):
    """악의적 trade_mode 값이 와도 알림 단계가 명령을 실행하거나 워크플로 명령을 주입하지 않습니다."""
    step = _step(NOTICE)
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PATH": "/usr/bin:/bin", "RESOLVED_MODE": hostile},
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "PWNED").exists(), "주입된 명령이 실행됐습니다"
    # 값에 개행이 있어도 별도 워크플로 명령(::error:: 등)이 줄 머리에 나오면 안 됩니다
    assert [l for l in result.stdout.splitlines() if l.startswith("::") and not l.startswith("::notice::")] == []
