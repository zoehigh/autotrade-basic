"""trade_base.yml 실행 결과별 캐시 저장·정산 검증·요약 동작 (Mock E2E).

워크플로의 **실제 run 스크립트**를 bash 로 실행합니다. `uv` 는 가짜로 바꿔
세션 게이트 출력과 봇 단계별 종료코드/산출물을 주입합니다 (브로커·네트워크 없음).
캐시 저장 단계와 verify-settlement job 의 `if:` 는 `test_cache_save_guard` 의
평가기로 그대로 평가합니다.
"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_repo_root = Path(__file__).resolve().parents[1]
for _p in (_repo_root, _repo_root / "src", _repo_root / "ops", _repo_root / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import session_gate
from test_cache_save_guard import NOTICE, SAVE, evaluate

WORKFLOWS = _repo_root / ".github" / "workflows"
BASE = yaml.safe_load((WORKFLOWS / "trade_base.yml").read_text(encoding="utf-8"))
VERIFY_WF = yaml.safe_load((WORKFLOWS / "verify.yml").read_text(encoding="utf-8"))
RUN_TRADE = BASE["jobs"]["run-trade"]
BOT_STEP = "자동매매 봇 실행"
SUMMARY_STEP = "실행 결과 요약"


def _step(name, job=RUN_TRADE):
    return next(s for s in job["steps"] if s.get("name") == name)


FAKE_UV = r'''#!/usr/bin/env python3
import json, os, signal, sys
from pathlib import Path

args = sys.argv[1:]
assert args[:2] == ["run", "python"], args
args = args[2:]
config = json.loads(os.environ["STUB_CONFIG"])
log = Path(os.environ["STUB_LOG"])

def record(entry):
    with log.open("a") as fh:
        fh.write(entry + "\n")

if args and args[0] == "ops/session_gate.py":
    record("GATE")
    if config.get("gate_rc", 0):
        sys.exit(config["gate_rc"])
    for key, value in config["gate"].items():
        print(f"{key}={value}")
    sys.exit(0)
if args and args[0] == "-c":
    record("SUMMARY")
    Path(os.environ["STUB_SENT"]).write_text(os.environ["RUN_SUMMARY_MESSAGE"])
    sys.exit(config.get("send_rc", 0))
if args[0] != "trading_bot.py":
    os.execv(sys.executable, [sys.executable, *args])

phase = os.environ.get("RUN_PHASE", "") or "LEGACY"
target = os.environ.get("PLAN_ID", "") if phase == "APPROVE" else ""
if phase in ("EXECUTE", "VERIFY"):
    target = os.environ.get("APPROVAL_PLAN_ID", "")
key = f"{phase}:{target}" if f"{phase}:{target}" in config.get("rc", {}) else phase
record(f"{phase} {target}".strip())
if config.get("kill") == phase:
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(1)
rc = config.get("rc", {}).get(key, 0)
if rc == 0 and phase == "SETTLE":
    Path("settlement-TQQQ-x.json").write_text("{}")
if rc == 0 and phase == "PREVIEW":
    Path("plans").mkdir(exist_ok=True)
    for plan_id in config.get("plans", ["P1"]):
        Path("plans", f"{plan_id}.json").write_text(json.dumps({"plan_id": plan_id}))
if rc == 0 and phase == "APPROVE":
    Path("approvals").mkdir(exist_ok=True)
    Path("approvals", f"{target}.json").write_text("{}")
marker = os.environ.get("RUN_SKIP_MARKER", "")
if phase == "LEGACY" and config.get("skip_marker"):
    Path(marker).write_text(config["skip_marker"] + "\n")
# 봇의 EXECUTE SKIP: 창이 닫혀 있고 승인 검증을 통과(exit 0)한 대상만 표식을 남깁니다.
if phase == "EXECUTE" and marker and rc == 0 and target not in config.get("no_skip", []):
    with open(marker, "a") as fh:
        fh.write(f"{target} skip\n")
sys.exit(rc)
'''


def _gate(window="open", wait=0, due=True, hold="", session="2026-11-27"):
    return {
        "window": window,
        "session": session,
        "reason": "휴장 사유" if window == "closed" else "주문 가능",
        "wait_seconds": wait,
        "settlement_due": "true" if due else "false",
        "settled_session": "2026-11-25",
        "state_hold": hold,
    }


def _fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return bin_dir


class Run:
    def __init__(self, result, outputs, calls, mode):
        self.result = result
        self.outputs = outputs
        self.calls = calls
        self.mode = mode
        self.bot_outcome = "success" if result.returncode == 0 else "failure"

    def phases(self):
        return [c.split()[0] for c in self.calls if c != "GATE"]

    def _ctx(self):
        ctx = {
            "steps.resolve.outputs.trade_mode": self.mode,
            "steps.bot.outcome": self.bot_outcome,
        }
        ctx.update({f"steps.bot.outputs.{k}": v for k, v in self.outputs.items()})
        return ctx

    def saves_cache(self):
        save = evaluate(_step(SAVE)["if"], self._ctx(), self.bot_outcome)
        notice = evaluate(_step(NOTICE)["if"], self._ctx(), self.bot_outcome)
        assert save != notice, "저장과 생략 안내는 정확히 반대여야 합니다"
        return save

    def runs_settlement_verify(self, job_result=None):
        ctx = {
            "needs.run-trade.result": job_result or self.bot_outcome,
            "needs.run-trade.outputs.settlement_status": self.outputs.get(
                "settlement_status", ""
            ),
        }
        return evaluate(BASE["jobs"]["verify-settlement"]["if"], ctx, self.bot_outcome)

    @property
    def outcome(self):
        return self.outputs.get("run_outcome", "")


def run_bot_step(
    tmp_path,
    *,
    phase="LIFECYCLE",
    mode="LIVE",
    broker="kis",
    broker_mode="demo",
    runner="github-hosted",
    env=None,
    **config,
):
    config.setdefault("gate", _gate())
    out = tmp_path / "gh_output"
    out.write_text("")
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = {
        "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(out),
        "STUB_CONFIG": json.dumps(config),
        "STUB_LOG": str(log),
        "RUN_PHASE": phase,
        "TRADE_MODE": mode,
        "BROKER": broker,
        "BROKER_MODE": broker_mode,
        "RUNNER_ENVIRONMENT": runner,
        **(env or {}),
    }
    result = subprocess.run(
        ["bash", "-c", _step(BOT_STEP)["run"]],
        env=full_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    outputs = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    return Run(result, outputs, log.read_text().splitlines(), mode)


def _assert_run(run, *, rc, outcome, cache, verify, phases=None):
    detail = run.result.stdout + run.result.stderr
    assert run.result.returncode == rc, detail
    assert run.outcome == outcome, detail
    assert run.saves_cache() is cache, detail
    assert run.runs_settlement_verify() is verify, detail
    if phases is not None:
        assert run.phases() == phases, detail


FULL = ["SETTLE", "PREVIEW", "APPROVE", "EXECUTE", "VERIFY"]


# ── 정상 거래일 ──────────────────────────────────────────────────


@pytest.mark.parametrize("broker", ["kis", "kiwoom", "nhplug"])
def test_normal_session_lifecycle_executes_orders(tmp_path, broker):
    run = run_bot_step(tmp_path, broker=broker)
    _assert_run(run, rc=0, outcome="ORDERS_EXECUTED", cache=True, verify=True, phases=FULL)
    assert run.outputs["settlement_status"] == "ATTEMPTED"
    assert run.outputs["order_verify_status"] == "PASS"


def test_no_new_plans_is_settle_only(tmp_path):
    run = run_bot_step(tmp_path, plans=[])
    _assert_run(
        run, rc=0, outcome="SETTLE_ONLY", cache=True, verify=True,
        phases=["SETTLE", "PREVIEW"],
    )


# ── 휴장일·마감 후 ───────────────────────────────────────────────


@pytest.mark.parametrize("broker", ["kis", "kiwoom", "nhplug"])
def test_closed_window_with_due_settlement_runs_settle_only(tmp_path, broker):
    run = run_bot_step(tmp_path, broker=broker, gate=_gate("closed", due=True))
    _assert_run(
        run, rc=0, outcome="SETTLE_ONLY", cache=True, verify=True, phases=["SETTLE"]
    )
    assert run.outputs["order_verify_status"] == "SKIPPED"


@pytest.mark.parametrize("broker", ["kis", "kiwoom", "nhplug"])
def test_closed_window_without_due_settlement_is_normal_skip(tmp_path, broker):
    run = run_bot_step(tmp_path, broker=broker, gate=_gate("closed", due=False))
    _assert_run(run, rc=0, outcome="SKIPPED", cache=False, verify=False, phases=[])
    assert run.outputs["state_may_change"] == "false"
    assert run.outputs["settlement_status"] == "SKIPPED"
    assert run.outputs["order_verify_status"] == "SKIPPED"
    assert "애프터아워" in run.outputs["run_reason"]


def test_closed_window_settle_failure_is_not_hidden(tmp_path):
    run = run_bot_step(tmp_path, gate=_gate("closed"), rc={"SETTLE": 1})
    _assert_run(run, rc=1, outcome="FAILED", cache=True, verify=True, phases=["SETTLE"])


def test_state_hold_is_not_hidden_by_skip(tmp_path):
    run = run_bot_step(
        tmp_path, gate=_gate("closed", due=False, hold="TQQQ:pending_order_intent")
    )
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])
    assert "pending_order_intent" in run.outputs["run_reason"]


def _execute_closed(tmp_path, refs="P1", **config):
    config.setdefault("gate", _gate("closed"))
    return run_bot_step(
        tmp_path, phase="EXECUTE", runner="self-hosted",
        env={"APPROVAL_PLAN_ID": refs}, **config,
    )


@pytest.mark.parametrize("refs,count", [("P1", 1), ("P1, P2,", 2)])
def test_standalone_execute_on_closed_window_is_validated_then_skipped(
    tmp_path, refs, count
):
    run = _execute_closed(tmp_path, refs)
    _assert_run(
        run, rc=0, outcome="SKIPPED", cache=False, verify=False,
        phases=["EXECUTE"] * count,
    )
    assert run.outputs["order_verify_status"] == "SKIPPED"


def test_invalid_approval_is_not_hidden_by_closed_window(tmp_path):
    """없는·사용된 승인, 검토 해시 불일치는 봇이 exit 1 → SKIP 이 아닙니다."""
    run = _execute_closed(tmp_path, rc={"EXECUTE": 1, "VERIFY": 1})
    _assert_run(
        run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True,
        phases=["EXECUTE", "VERIFY"],
    )


def test_one_invalid_target_among_skips_is_a_failure(tmp_path):
    run = _execute_closed(
        tmp_path, "P1,P2", rc={"EXECUTE:P2": 1, "VERIFY": 2}
    )
    _assert_run(run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True)


def test_execute_success_without_skip_marker_is_still_verified(tmp_path):
    """봇이 SKIP 을 확인해 주지 않으면 기존대로 주문 VERIFY 를 수행합니다."""
    run = _execute_closed(tmp_path, no_skip=["P1"])
    _assert_run(
        run, rc=0, outcome="ORDERS_EXECUTED", cache=True, verify=True,
        phases=["EXECUTE", "VERIFY"],
    )


def test_open_window_execute_never_uses_skip_marker(tmp_path):
    run = _execute_closed(tmp_path, gate=_gate(), rc={"VERIFY": 2})
    _assert_run(run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True)


def test_execute_without_target_is_config_error_even_when_closed(tmp_path):
    run = _execute_closed(tmp_path, "", rc={"EXECUTE": 1, "VERIFY": 1})
    assert run.result.returncode == 1
    assert run.outcome == "PARTIAL_FAILURE"
    assert run.saves_cache() is True


def test_state_hold_blocks_closed_execute(tmp_path):
    run = _execute_closed(tmp_path, gate=_gate("closed", hold="TQQQ:balance_mismatch"))
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])


@pytest.mark.parametrize("broker", ["kis", "kiwoom", "nhplug", "meritz"])
def test_real_review_gate_still_applies_on_closed_window(tmp_path, broker):
    run = run_bot_step(
        tmp_path, broker=broker, broker_mode="real", gate=_gate("closed", due=False)
    )
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])


def test_real_plan_hash_gate_still_applies_on_closed_window(tmp_path):
    run = run_bot_step(
        tmp_path, phase="EXECUTE", broker_mode="real", runner="self-hosted",
        gate=_gate("closed"), env={"REVIEWED": "true", "APPROVAL_PLAN_ID": "P1"},
    )
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])


def test_real_reviewed_lifecycle_executes(tmp_path):
    run = run_bot_step(tmp_path, broker_mode="real", env={"REVIEWED": "true"})
    _assert_run(run, rc=0, outcome="ORDERS_EXECUTED", cache=True, verify=True, phases=FULL)


# ── 게이트 실패 (봇 미시작) ──────────────────────────────────────


def test_gate_failure_fails_without_bot_or_cache(tmp_path):
    run = run_bot_step(tmp_path, gate_rc=1)
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])


@pytest.mark.parametrize(
    "gate",
    [_gate(window="maybe"), _gate(wait="soon"), {}],
)
def test_malformed_gate_output_fails_closed(tmp_path, gate):
    run = run_bot_step(tmp_path, gate=gate)
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])


def test_wait_budget_exceeded_is_failure_not_skip(tmp_path):
    run = run_bot_step(tmp_path, gate=_gate(wait=6 * 3600))
    _assert_run(run, rc=1, outcome="NOT_STARTED", cache=False, verify=True, phases=[])
    assert "settlement_status" not in run.outputs


@pytest.mark.parametrize(
    "runner,env,wait",
    [
        ("self-hosted", {}, 8 * 3600),
        ("github-hosted", {}, 4 * 3600),
        ("github-hosted", {"LIFECYCLE_MAX_WAIT_SECONDS": "30000"}, 8 * 3600),
    ],
)
def test_next_session_wait_is_preserved(tmp_path, runner, env, wait):
    run = run_bot_step(tmp_path, runner=runner, env=env, gate=_gate(wait=wait))
    _assert_run(run, rc=0, outcome="ORDERS_EXECUTED", cache=True, verify=True, phases=FULL)


def test_preview_phase_does_not_consult_gate(tmp_path):
    run = run_bot_step(
        tmp_path, phase="PREVIEW", runner="self-hosted", gate=_gate("closed", due=False)
    )
    _assert_run(
        run, rc=0, outcome="COMPLETED", cache=True, verify=True, phases=["PREVIEW"]
    )
    assert "GATE" not in run.calls


# ── 주문 접수 전후 실패·부분 접수·중단 ───────────────────────────


def test_settle_failure_stops_chain(tmp_path):
    run = run_bot_step(tmp_path, rc={"SETTLE": 1})
    _assert_run(run, rc=1, outcome="FAILED", cache=True, verify=True, phases=["SETTLE"])


def test_preview_failure_after_settle_is_partial(tmp_path):
    run = run_bot_step(tmp_path, rc={"PREVIEW": 1})
    _assert_run(
        run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True,
        phases=["SETTLE", "PREVIEW"],
    )


def test_execute_failure_still_verifies_orders(tmp_path):
    run = run_bot_step(tmp_path, rc={"EXECUTE": 1, "VERIFY": 1})
    _assert_run(
        run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True, phases=FULL
    )
    assert run.outputs["order_verify_status"] == "FAIL"


def test_partial_submission_across_symbols(tmp_path):
    run = run_bot_step(
        tmp_path, plans=["P1", "P2"], rc={"EXECUTE:P2": 1, "VERIFY:P2": 1}
    )
    _assert_run(run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True)
    assert [c for c in run.calls if c.startswith("VERIFY")] == ["VERIFY P1", "VERIFY P2"]
    assert "EXECUTE 1건" in run.outputs["run_reason"]


def test_market_closed_between_gate_and_execute_is_not_hidden(tmp_path):
    """봇이 실행 직전 SKIP(exit 0)해도 주문 VERIFY(exit 2)가 실패로 드러냅니다."""
    run = run_bot_step(tmp_path, rc={"VERIFY": 2})
    _assert_run(run, rc=1, outcome="PARTIAL_FAILURE", cache=True, verify=True)
    assert run.outputs["order_verify_status"] == "NOT_EXECUTED"


@pytest.mark.parametrize(
    "phase,outcome",
    [("SETTLE", "FAILED"), ("EXECUTE", "PARTIAL_FAILURE"), ("VERIFY", "PARTIAL_FAILURE")],
)
def test_killed_run_keeps_conservative_cache_save(tmp_path, phase, outcome):
    """강제 종료(타임아웃 등)에도 직전 표식이 남아 캐시를 저장하고 검증합니다."""
    run = run_bot_step(tmp_path, kill=phase)
    assert run.result.returncode != 0
    assert run.outcome == outcome
    assert run.saves_cache() is True
    assert run.runs_settlement_verify() is True


def test_missing_outputs_after_start_save_cache_and_verify():
    """표식이 하나도 남지 않으면 상태 변경 가능성이 있다고 봅니다."""
    run = Run(subprocess.CompletedProcess([], 1, "", ""), {}, [], "LIVE")
    assert run.saves_cache() is True
    assert run.runs_settlement_verify() is True


@pytest.mark.parametrize("mode", ["LIVE", "REPAIR"])
def test_skipped_bot_step_never_saves(mode):
    ctx = {"steps.resolve.outputs.trade_mode": mode, "steps.bot.outcome": "skipped"}
    assert evaluate(_step(SAVE)["if"], ctx, "failure") is False


def test_settlement_verify_runs_when_skip_run_later_fails(tmp_path):
    run = run_bot_step(tmp_path, gate=_gate("closed", due=False))
    assert run.runs_settlement_verify(job_result="failure") is True
    assert run.runs_settlement_verify(job_result="cancelled") is True
    assert run.runs_settlement_verify(job_result="success") is False


def test_verify_job_keeps_always():
    assert "always()" in BASE["jobs"]["verify-settlement"]["if"]
    assert "always()" in _step(SAVE)["if"]


def test_job_outputs_expose_markers():
    for key in ("run_outcome", "run_reason", "settlement_status"):
        assert RUN_TRADE["outputs"][key] == f"${{{{ steps.bot.outputs.{key} }}}}"


# ── DRY·레거시 ───────────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["LIFECYCLE", ""])
def test_dry_holiday_marker_excludes_settlement_verify(tmp_path, phase):
    run = run_bot_step(tmp_path, phase=phase, mode="DRY", skip_marker="휴장일")
    _assert_run(run, rc=0, outcome="SKIPPED", cache=False, verify=False, phases=["LEGACY"])


@pytest.mark.parametrize("phase", ["LIFECYCLE", ""])
def test_dry_trading_day_is_verified(tmp_path, phase):
    run = run_bot_step(tmp_path, phase=phase, mode="DRY")
    _assert_run(run, rc=0, outcome="COMPLETED", cache=False, verify=True, phases=["LEGACY"])
    assert "GATE" not in run.calls


@pytest.mark.parametrize("phase", ["LIFECYCLE", ""])
def test_dry_failure_with_marker_is_not_a_skip(tmp_path, phase):
    run = run_bot_step(
        tmp_path, phase=phase, mode="DRY", skip_marker="휴장일", rc={"LEGACY": 1}
    )
    _assert_run(run, rc=1, outcome="FAILED", cache=False, verify=True)


def test_standalone_settle_on_self_hosted(tmp_path):
    run = run_bot_step(tmp_path, phase="SETTLE", runner="self-hosted")
    _assert_run(
        run, rc=0, outcome="SETTLE_ONLY", cache=True, verify=True, phases=["SETTLE"]
    )


# ── 게이트 ↔ 워크플로 계약 ───────────────────────────────────────


def test_workflow_reads_only_keys_the_gate_emits(tmp_path):
    emitted = set(session_gate.evaluate(state_path=tmp_path / "none.json"))
    script = _step(BOT_STEP)["run"]
    used = set(re.findall(r"gate_value ([a-z_]+)", script))
    assert used and used <= emitted


def test_bot_step_script_stays_under_expression_limit():
    script = _step(BOT_STEP)["run"]
    assert "${{" not in script
    assert len(script) < 21000


# ── 정산 Evidence 누락은 실패 ────────────────────────────────────


@pytest.mark.parametrize("files", [[], ["final-settlement-1.json"]])
def test_missing_settlement_evidence_fails_verification(tmp_path, files):
    script = _step("정산 근거 독립 검산", VERIFY_WF["jobs"]["verify"])["run"]
    (tmp_path / "evidence").mkdir()
    for name in files:
        (tmp_path / "evidence" / name).write_text("{}")
    (tmp_path / "ops").symlink_to(_repo_root / "ops")
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\nshift 2\nexec "{sys.executable}" "$@"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "MODE": "LIVE", "BROKER": "kis"},
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "검증 불가" in result.stdout


# ── 실행 결과 요약 (Actions 요약 + 텔레그램) ─────────────────────


def _run_summary(tmp_path, send_rc=0, **env):
    summary = tmp_path / "summary.md"
    sent = tmp_path / "sent.txt"
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["bash", "-c", _step(SUMMARY_STEP)["run"]],
        env={
            "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin",
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_CONFIG": json.dumps({"send_rc": send_rc}),
            "STUB_LOG": str(log),
            "STUB_SENT": str(sent),
            "BROKER": "kis",
            "BROKER_MODE": "demo",
            "TRADE_MODE": "LIVE",
            "JOB_STATUS": "success",
            **env,
        },
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return summary.read_text(encoding="utf-8"), sent.read_text(encoding="utf-8"), result


@pytest.mark.parametrize(
    "outcome,label",
    [
        ("NOT_STARTED", "봇 미시작"),
        ("SKIPPED", "정상 SKIP"),
        ("SETTLE_ONLY", "정산 실행 (주문 없음)"),
        ("ORDERS_EXECUTED", "주문 실행"),
        ("COMPLETED", "단계 완료"),
        ("FAILED", "실패"),
        ("PARTIAL_FAILURE", "부분 실패"),
    ],
)
def test_summary_shows_outcome_and_reason(tmp_path, outcome, label):
    summary, sent, _ = _run_summary(
        tmp_path, RUN_OUTCOME=outcome, RUN_REASON="휴장 사유", BOT_OUTCOME="success"
    )
    for text in (summary, sent):
        assert f"실행 결과: {label}" in text
        assert "휴장 사유" in text and outcome in text


@pytest.mark.parametrize(
    "bot_outcome,expected", [("skipped", "NOT_STARTED"), ("", "NOT_STARTED"), ("failure", "UNKNOWN")]
)
def test_summary_without_markers(tmp_path, bot_outcome, expected):
    _summary, sent, _ = _run_summary(
        tmp_path, BOT_OUTCOME=bot_outcome, JOB_STATUS="failure"
    )
    assert expected in sent


def test_summary_flags_later_step_failure(tmp_path):
    _summary, sent, _ = _run_summary(
        tmp_path, RUN_OUTCOME="ORDERS_EXECUTED", JOB_STATUS="failure"
    )
    assert "이후 단계 실패" in sent


def test_summary_send_failure_does_not_fail_step(tmp_path):
    _summary, _sent, result = _run_summary(tmp_path, send_rc=1, RUN_OUTCOME="SKIPPED")
    assert "::warning::" in result.stdout
    step = _step(SUMMARY_STEP)
    assert step["if"] == "always()" and step["continue-on-error"] is True
    assert "${{" not in step["run"]


def test_summary_strips_control_characters_from_reason(tmp_path):
    _summary, sent, result = _run_summary(
        tmp_path, RUN_OUTCOME="SKIPPED", RUN_REASON="a\n::error::injected"
    )
    assert not any(line.startswith("::error::") for line in result.stdout.splitlines())
