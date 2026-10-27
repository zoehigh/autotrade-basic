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


def evaluate(expr, ctx, status="success", raw=False):
    """GitHub Actions `if:` 표현식(부분집합) 평가. 반환값은 Python 진리값 (raw=True 면 `||`/`&&` 의 값 그대로)."""
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
    return result if raw else bool(result)


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


# ── Resolve 단계: env 로만 외부 입력을 받고, 봇(config.py)과 같은 정규화, REPAIR 는 차단 ──
def _run_resolve(tmp_path, **env):
    """Resolve 단계의 run 스크립트를 실제로 실행합니다 (외부 입력은 env 로만 전달)."""
    script = _step("Resolve and log inputs")["run"]
    out = tmp_path / "gh_output"
    out.write_text("")
    full_env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), **env}
    result = subprocess.run(
        ["bash", "-c", script], env=full_env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    lines = out.read_text().splitlines()
    return result, lines


def _resolve_output(mode_value, tmp_path):
    result, lines = _run_resolve(tmp_path, IN_TRADE_MODE=mode_value)
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    return values["trade_mode"]


@pytest.mark.parametrize(
    "raw,expected",
    [("live", "LIVE"), (" LIVE ", "LIVE"), ("dry", "DRY"), ("DRY", "DRY"), ("bogus", "BOGUS"),
     ("LI VE", "INVALID"), ("DRY-RUN", "INVALID"), ("L1VE", "INVALID")],
)
def test_resolve_normalizes_like_bot_config(raw, expected, tmp_path):
    assert _resolve_output(raw, tmp_path) == expected


@pytest.mark.parametrize("raw", ["live", " LIVE "])
def test_normalized_value_matches_what_the_bot_runs(raw, tmp_path, monkeypatch):
    """워크플로가 저장을 허용한 값은 봇이 실제로 LIVE 로 해석하는 값과 같아야 합니다."""
    import importlib
    import sys

    resolved = _resolve_output(raw, tmp_path)
    assert _save_decision(resolved, "success") is True
    monkeypatch.setenv("TRADE_MODE", resolved)
    sys.path.insert(0, str(ROOT / "src"))
    import config
    importlib.reload(config)
    assert config.TRADE_MODE == "LIVE"
    monkeypatch.delenv("TRADE_MODE")
    importlib.reload(config)


@pytest.mark.parametrize("raw", ["bogus", "LI VE", "DRY-RUN"])
def test_invalid_value_is_dry_in_bot_and_blocked_in_workflow(raw, tmp_path, monkeypatch):
    import importlib
    import sys

    resolved = _resolve_output(raw, tmp_path)
    assert _save_decision(resolved, "success") is False
    monkeypatch.setenv("TRADE_MODE", resolved)
    sys.path.insert(0, str(ROOT / "src"))
    import config
    importlib.reload(config)
    assert config.TRADE_MODE == "DRY"
    monkeypatch.delenv("TRADE_MODE")
    importlib.reload(config)


# 옛 표현식과의 동등성: 우선순위(inputs → vars → real 이면 DRY → LIVE)와 GitHub 의 대소문자 무시 비교를 보존
_OLD_MODE_EXPR = "inputs.trade_mode || vars.TRADE_MODE || (inputs.broker_mode == 'real' && 'DRY') || 'LIVE'"


def _normalize_like_script(value):
    return value.strip().upper()


@pytest.mark.parametrize("in_mode", ["", "LIVE", " ", "dry"])
@pytest.mark.parametrize("var_mode", ["", "DRY", "Live"])
@pytest.mark.parametrize("broker_mode", ["demo", "real", "REAL", ""])
def test_resolve_keeps_old_expression_precedence(in_mode, var_mode, broker_mode, tmp_path):
    ctx = {"inputs.trade_mode": in_mode, "vars.TRADE_MODE": var_mode, "inputs.broker_mode": broker_mode}
    expected = _normalize_like_script(evaluate(_OLD_MODE_EXPR, ctx, raw=True))
    result, lines = _run_resolve(
        tmp_path, IN_TRADE_MODE=in_mode, VAR_TRADE_MODE=var_mode, IN_BROKER_MODE=broker_mode,
    )
    assert result.returncode == 0, result.stdout
    assert lines == [f"trade_mode={expected}"]


# ── A안: Actions 에서의 REPAIR 차단 ──────────────────────────────────
@pytest.mark.parametrize("raw", ["REPAIR", "repair", " Repair "])
@pytest.mark.parametrize("source", ["IN_TRADE_MODE", "VAR_TRADE_MODE"])
def test_repair_is_blocked_before_any_output(raw, source, tmp_path):
    result, lines = _run_resolve(tmp_path, **{source: raw})
    assert result.returncode == 1
    assert "::error::" in result.stdout and "REPAIR" in result.stdout
    assert lines == [], "차단된 실행은 trade_mode 출력을 쓰면 안 됩니다 (이후 캐시 저장 가드가 저장하지 않도록)"


def test_repair_in_inputs_wins_over_var_and_real_default(tmp_path):
    result, lines = _run_resolve(tmp_path, IN_TRADE_MODE="REPAIR", VAR_TRADE_MODE="DRY", IN_BROKER_MODE="real")
    assert result.returncode == 1 and lines == []


# ── 외부 입력이 셸·출력 파일에 주입되지 않음 ────────────────────────
_HOSTILE = [
    "$(touch PWNED)", "`touch PWNED`", 'x"; touch PWNED; echo "', "x'; touch PWNED; echo '",
    "LIVE\nextra_output=pwned", "LIVE\n::error::injected", "a\r::set-env name=X::y",
]


def _decode(value):
    return value.encode().decode("unicode_escape")


@pytest.mark.parametrize("hostile", _HOSTILE)
@pytest.mark.parametrize("field", ["IN_TRADE_MODE", "VAR_TRADE_MODE", "IN_BROKER_MODE", "IN_BROKER", "IN_SYMBOLS", "VAR_SYMBOLS"])
def test_resolve_does_not_execute_or_inject_external_inputs(hostile, field, tmp_path):
    value = _decode(hostile)
    result, lines = _run_resolve(tmp_path, **{field: value})
    assert not (tmp_path / "PWNED").exists(), "외부 입력이 셸 명령으로 실행됐습니다"
    # GITHUB_OUTPUT 에는 trade_mode 한 줄만 (다른 출력 키 주입 불가)
    assert len(lines) == 1 and lines[0].startswith("trade_mode="), lines
    # 로그에 외부 값이 줄 머리의 워크플로 명령(::)으로 나오지 않음 (우리가 쓴 ::warning::/::error:: 제외)
    injected = [
        l for l in result.stdout.splitlines()
        if l.startswith("::") and not l.startswith(("::warning::유효하지 않은 TRADE_MODE", "::error::Actions 에서의"))
    ]
    assert injected == [], injected


def test_resolve_step_has_no_expression_interpolation_in_shell(tmp_path):
    step = _step("Resolve and log inputs")
    assert "${{" not in step["run"], "run 스크립트에 GitHub 표현식이 직접 들어 있습니다"
    assert set(step["env"]) == {
        "IN_TRADE_MODE", "VAR_TRADE_MODE", "IN_BROKER", "IN_BROKER_MODE", "IN_SYMBOLS", "VAR_SYMBOLS",
    }
    assert step["env"]["IN_TRADE_MODE"] == "${{ inputs.trade_mode }}"
    assert step["env"]["VAR_TRADE_MODE"] == "${{ vars.TRADE_MODE }}"


# ── 차단된 REPAIR 실행의 단계별 시뮬레이션: 봇 미실행 · 신규 state 캐시 미저장 ──
def _simulate_job(tmp_path, **resolve_env):
    """run-trade 잡의 단계를 순서대로 평가합니다 (Resolve 는 실제 실행, 나머지는 성공으로 가정)."""
    status, outputs, executed = "success", {}, []
    for step in _steps():
        ctx = {CONTEXT_KEY: outputs.get("trade_mode", "")}
        cond = step.get("if", "success()")
        if not evaluate(cond, ctx, status):
            continue
        executed.append(step.get("name"))
        if step.get("id") == "resolve":
            result, lines = _run_resolve(tmp_path, **resolve_env)
            outputs = dict(l.split("=", 1) for l in lines if "=" in l)
            if result.returncode != 0:
                status = "failure"
    return executed, status


BOT_STEP = "자동매매 봇 실행"


def test_blocked_repair_runs_no_bot_and_saves_no_new_cache(tmp_path):
    executed, status = _simulate_job(tmp_path, IN_TRADE_MODE="REPAIR")
    assert status == "failure"
    assert RESTORE in executed, "복원은 state 를 바꾸지 않으므로 그대로 실행됩니다"
    assert BOT_STEP not in executed, "차단된 REPAIR 에서 봇이 실행되면 안 됩니다"
    assert SAVE not in executed, "차단된 REPAIR 에서 신규 state 캐시가 저장되면 안 됩니다"
    assert NOTICE in executed


@pytest.mark.parametrize("source", ["IN_TRADE_MODE", "VAR_TRADE_MODE"])
def test_blocked_repair_via_repository_variable_also_saves_nothing(source, tmp_path):
    executed, _ = _simulate_job(tmp_path, **{source: "repair"})
    assert BOT_STEP not in executed and SAVE not in executed


def test_non_repair_modes_keep_their_behavior(tmp_path):
    live, _ = _simulate_job(tmp_path, IN_TRADE_MODE="LIVE")
    dry, _ = _simulate_job(tmp_path, IN_TRADE_MODE="DRY")
    assert BOT_STEP in live and SAVE in live
    assert BOT_STEP in dry and SAVE not in dry


def test_nothing_runs_between_resolve_and_bot_step():
    """Resolve 와 봇 실행 사이에 다른 단계가 끼어들면(부작용 가능) 차단 보장이 약해지므로 막습니다."""
    names = [s.get("name") for s in _steps()]
    assert names.index(BOT_STEP) == names.index("Resolve and log inputs") + 1


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


# ── 이식성: self-hosted runner(오래된 bash 가능)에서도 동작하는 문법만 사용 ──
def test_resolve_script_avoids_bash4_only_syntax_and_pins_locale():
    script = _step("Resolve and log inputs")["run"]
    for banned in (",,", "^^", "declare -A", "mapfile", "readarray", "&>>", "|&"):
        assert banned not in script, f"bash 4+ 전용 문법 {banned!r} 사용 — 오래된 bash 에서 실패할 수 있습니다"
    assert "export LC_ALL=C" in script, "로케일에 따라 [A-Z]/대소문자 변환이 달라지지 않도록 고정해야 합니다"


@pytest.mark.parametrize("locale_env", [{}, {"LANG": "C.UTF-8"}, {"LC_ALL": "C.UTF-8"}, {"LC_ALL": "POSIX"}])
def test_resolve_result_does_not_depend_on_caller_locale(locale_env, tmp_path):
    result, lines = _run_resolve(tmp_path, IN_TRADE_MODE=" live ", IN_BROKER_MODE="REAL", **locale_env)
    assert result.returncode == 0 and lines == ["trade_mode=LIVE"]
    result, lines = _run_resolve(tmp_path, IN_BROKER_MODE="Real", **locale_env)
    assert lines == ["trade_mode=DRY"], "broker_mode 대소문자 무시 비교가 로케일과 무관해야 합니다"
