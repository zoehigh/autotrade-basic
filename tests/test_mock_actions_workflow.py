"""Actions Mock 검증 워크플로(`ops/mock_actions/`)의 격리 조건과 시나리오 기대값.

GitHub 에서 실행하기 **전에** 로컬에서 확인합니다.
- 생성 워크플로가 secrets·vars·environment·운영 캐시 키·실제 봇을 쓰지 않는지
- 저장/검증 `if:` 와 봇 스크립트가 trade_base.yml 과 같은지
- 시나리오 기대값이 실제 스텝 스크립트의 로컬 동작과 맞는지
스텝 타임아웃·취소의 output 보존은 GitHub 에서만 관측할 수 있어 여기서 덮지 않습니다.
"""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_repo_root = Path(__file__).resolve().parents[1]
for _p in (_repo_root / "tests", _repo_root / "ops" / "mock_actions"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build
import scenarios
from test_cache_save_guard import evaluate

BASE = yaml.safe_load(
    (_repo_root / ".github" / "workflows" / "trade_base.yml").read_text(encoding="utf-8")
)
GENERATED = build.build()
RUN = GENERATED[build.RUN_FILE]
TRADE = RUN["jobs"]["run-trade"]


def _step(name, job=TRADE):
    return next(s for s in job["steps"] if s.get("name") == name)


def _walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


# ── 격리 ─────────────────────────────────────────────────────────


def test_no_secret_or_repository_variable_references():
    for workflow in GENERATED.values():
        for text in _walk(workflow):
            for expression in re.findall(r"\$\{\{(.*?)\}\}", text, re.S):
                assert not re.search(r"\b(secrets|vars)\.", expression), expression
            if "\n" not in text:
                assert text != "inherit", "secrets: inherit 금지"


def test_no_environment_hosted_runner_and_read_only_token():
    for workflow in GENERATED.values():
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert "environment" not in job
            assert job.get("runs-on", "ubuntu-latest") == "ubuntu-latest"
    assert TRADE["concurrency"]["group"].startswith("mock-skip-")


def test_only_dedicated_push_branches_trigger():
    triggers = {name: wf["on"] for name, wf in GENERATED.items()}
    assert triggers[build.RUN_FILE].keys() == {"workflow_call"}
    assert triggers[build.VERIFY_FILE].keys() == {"workflow_call"}
    for name in (build.CALLER_FILE, build.CANCEL_FILE):
        assert triggers[name] == {"push": {"branches": ["mock-verify/**"]}}


def test_cancel_scenario_is_selected_only_by_commit_message():
    normal = GENERATED[build.CALLER_FILE]["jobs"]["mock"]
    cancel = GENERATED[build.CANCEL_FILE]["jobs"]["mock"]
    key = "github.event.head_commit.message"
    for message, expected in [("ci(mock): run", True), ("x [mock-cancel] y", False)]:
        contains = "[mock-cancel]" in message
        assert (not contains) is expected
    assert normal["if"] == "${{ !contains(%s, '[mock-cancel]') }}" % key
    assert cancel["if"] == "${{ contains(%s, '[mock-cancel]') }}" % key
    for job in (normal, cancel):
        assert job["strategy"]["max-parallel"] <= 4
    assert [row["scenario"] for row in cancel["strategy"]["matrix"]["include"]] == ["cancel"]


def test_no_actions_cache_other_than_mock_state():
    for workflow in GENERATED.values():
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv"):
                    assert step["with"]["enable-cache"] is False


def test_verify_copy_differs_only_in_cache_and_passphrase():
    original = yaml.safe_load(
        (_repo_root / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    )
    mock = GENERATED[build.VERIFY_FILE]
    expected = copy.deepcopy(original["jobs"])
    steps = expected["verify"]["steps"]
    next(s for s in steps if s.get("name") == "uv 설치")["with"]["enable-cache"] = False
    next(s for s in steps if s.get("name") == "정산 근거 복호화")["env"][
        "PASSPHRASE"
    ] = build.MOCK_PASSPHRASE
    assert mock["jobs"] == expected
    assert mock["on"]["workflow_call"]["inputs"] == original[True]["workflow_call"]["inputs"]


def test_cache_keys_never_touch_operating_state_caches():
    cache_steps = [
        step
        for workflow in GENERATED.values()
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/cache")
    ]
    assert len(cache_steps) == 3  # 복원, 저장, 저장 여부 조회
    for step in cache_steps:
        assert step["with"]["key"].startswith("mock-skip-")
        assert step["with"]["path"] == build.CACHE_PATH
        assert "restore-keys" not in step["with"]
    operating = [
        step["with"]["path"]
        for step in BASE["jobs"]["run-trade"]["steps"]
        if str(step.get("uses", "")).startswith("actions/cache")
    ]
    assert operating and build.CACHE_PATH not in operating
    names = [s.get("name") for s in TRADE["steps"]]
    assert names.index(build.CACHE_PREPARE_STEP["name"]) == names.index("state 캐시 저장") - 1
    assert "||" not in build.CACHE_PREPARE_STEP["run"]


def test_real_bot_is_disabled_and_fake_uv_comes_first():
    names = [step.get("name") for step in TRADE["steps"]]
    isolation = build.ISOLATION_STEP["name"]
    assert names.index(isolation) == names.index("코드 체크아웃") + 1
    assert "> trading_bot.py" in _step(isolation)["run"]
    script = _step(build.BOT_STEP)["run"]
    original = _step(build.BOT_STEP, BASE["jobs"]["run-trade"])["run"]
    assert script == build.BOT_PRELUDE + original
    assert len(script) < 21000 and "${{" not in script


def test_conditions_are_copied_verbatim():
    base_trade = BASE["jobs"]["run-trade"]
    for name in (
        "state 캐시 저장", "state 캐시 저장 생략 안내", "최종 정산 스냅샷 생성",
        "정산 근거 암호화", "실행 결과 요약",
    ):
        assert _step(name).get("if") == _step(name, base_trade).get("if")
    assert RUN["jobs"]["verify-settlement"]["if"] == BASE["jobs"]["verify-settlement"]["if"]
    assert RUN["jobs"]["verify-settlement"]["uses"].endswith(build.VERIFY_FILE)
    assert "secrets" not in RUN["jobs"]["verify-settlement"]
    assert TRADE["outputs"] == base_trade["outputs"]


def test_build_fails_when_a_replaced_step_disappears(tmp_path):
    broken = copy.deepcopy(BASE)
    broken["jobs"]["run-trade"]["steps"] = [
        step for step in broken["jobs"]["run-trade"]["steps"]
        if step.get("name") != "state 캐시 저장"
    ]
    path = tmp_path / "trade_base.yml"
    path.write_text(
        yaml.safe_dump({True: broken.pop(True), **broken}, allow_unicode=True),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="state 캐시 저장"):
        build.build(path)


def test_cli_writes_files_outside_repository_workflows(tmp_path):
    assert build.main(["--out", str(tmp_path)]) == 0
    for name in GENERATED:
        assert yaml.safe_load((tmp_path / name).read_text(encoding="utf-8"))
    committed = {p.name for p in (_repo_root / ".github" / "workflows").iterdir()}
    assert not committed & set(GENERATED), "생성 파일은 커밋하지 않습니다"


# ── 시나리오 기대값 ↔ 로컬 동작 ──────────────────────────────────

LOCAL = [name for name in scenarios.SCENARIOS if name != "step-timeout"]


def _simulate(name, tmp_path):
    case = scenarios.SCENARIOS[name]
    inputs = {**build.INPUT_DEFAULTS, **case["inputs"]}
    (tmp_path / "ops").symlink_to(_repo_root / "ops")
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_WORKSPACE": str(_repo_root),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "RUNNER_TEMP": str(tmp_path),
        "RUNNER_ENVIRONMENT": "github-hosted",
        "MOCK_SCENARIO": name,
        "RUN_PHASE": inputs["run_phase"],
        "TRADE_MODE": inputs["trade_mode"],
        "BROKER": inputs["broker"],
        "BROKER_MODE": inputs["broker_mode"],
    }
    (tmp_path / "out").write_text("")
    for step_name in (build.ISOLATION_STEP["name"], build.BOT_STEP):
        result = subprocess.run(
            ["bash", "-e", "-c", _step(step_name)["run"]],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )
        if step_name != build.BOT_STEP:
            assert result.returncode == 0, result.stderr
    outputs = dict(
        line.split("=", 1)
        for line in (tmp_path / "out").read_text().splitlines()
        if "=" in line
    )
    bot = "success" if result.returncode == 0 else "failure"
    trade = "failure" if inputs["fail_after"] == "yes" else bot
    ctx = {
        "steps.resolve.outputs.trade_mode": inputs["trade_mode"],
        "steps.bot.outcome": bot,
        **{f"steps.bot.outputs.{k}": v for k, v in outputs.items()},
    }
    saved = evaluate(_step("state 캐시 저장")["if"], ctx, trade)
    verify_runs = evaluate(
        RUN["jobs"]["verify-settlement"]["if"],
        {
            "needs.run-trade.result": trade,
            "needs.run-trade.outputs.settlement_status": outputs.get(
                "settlement_status", ""
            ),
        },
        trade,
    )
    # 정산 검증 job 은 근거(settlement-*.json)가 없으면 실패합니다 (verify.yml).
    has_evidence = any(tmp_path.glob("settlement-*.json"))
    verify = (
        "skipped" if not verify_runs else ("success" if has_evidence else "failure")
    )
    return {
        "trade": trade,
        "outcome": outputs.get("run_outcome", ""),
        "settlement": outputs.get("settlement_status", ""),
        "cache_hit": "true" if saved else "",
        "verify": verify,
    }, result


@pytest.mark.parametrize("name", LOCAL)
def test_scenario_expectation_matches_local_behaviour(name, tmp_path):
    observed, result = _simulate(name, tmp_path)
    assert scenarios.check(name, **observed) == [], result.stdout + result.stderr


def test_disabled_bot_stub_refuses_to_run(tmp_path):
    subprocess.run(
        ["bash", "-e", "-c", build.ISOLATION_STEP["run"].replace(
            "chmod +x ops/mock_actions/bin/uv\n", "")],
        cwd=tmp_path, check=True,
    )
    result = subprocess.run(
        [sys.executable, "trading_bot.py"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0 and "disabled" in result.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("trade", "failure"), ("outcome", "SETTLE_ONLY"), ("settlement", "ATTEMPTED"),
        ("cache_hit", "true"), ("verify", "success"),
    ],
)
def test_assertion_detects_each_mismatch(field, value):
    observed = {
        "trade": "success", "outcome": "SKIPPED", "settlement": "SKIPPED",
        "cache_hit": "", "verify": "skipped",
    }
    assert scenarios.check("skip", **observed) == []
    assert len(scenarios.check("skip", **{**observed, field: value})) == 1


def test_assert_command_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    for key, value in {
        "TRADE_RESULT": "success", "RUN_OUTCOME": "SKIPPED",
        "SETTLEMENT_STATUS": "SKIPPED", "CACHE_HIT": "",
        "VERIFY_RESULT": "skipped",
    }.items():
        monkeypatch.setenv(key, value)
    assert scenarios.main(["assert", "skip"]) == 0
    monkeypatch.setenv("CACHE_HIT", "true")
    assert scenarios.main(["assert", "skip"]) == 1
    assert "FAIL" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    json.loads(json.dumps(scenarios.ALL_SCENARIOS))  # stub 은 JSON 직렬화 가능
