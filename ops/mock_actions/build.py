#!/usr/bin/env python3
"""운영 워크플로(trade_base.yml)에서 **격리된 Mock 검증 워크플로**를 생성합니다.

    uv run --with pyyaml python ops/mock_actions/build.py --out .github/workflows

생성 파일은 main/PR 에 커밋하지 않습니다 (`ops/mock_actions/README.md` 절차).
스텝 스크립트와 `if:` 조건은 trade_base.yml·verify.yml 의 것을 그대로 복사하고, 아래만 바꿉니다.
  - secrets/vars/environment 제거, hosted 러너 고정, 별도 concurrency 그룹
  - state 캐시는 키(`mock-skip-…`)와 **저장 경로**(`.mock-cache/…`)를 모두 Mock 전용으로 교체
    (restore-keys 없음 — 경로가 다르면 캐시 version 도 달라 운영 캐시와 섞일 수 없습니다)
  - uv 의 Actions 캐시 사용 중지 (저장소 캐시 용량에 영향을 주지 않도록)
  - 봇 실행 스텝의 `uv` 를 가짜(ops/mock_actions/bin/uv)로 교체, `trading_bot.py` 는 실행 불가
  - artifact 이름에 시나리오 접미사, 암호는 비밀이 아닌 고정 Mock 값
치환 대상이 원본 워크플로에서 사라지면 생성이 실패합니다.
"""
import argparse
import copy
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios import MANUAL_SCENARIOS, SCENARIOS  # noqa: E402

RUN_FILE = "mock_session_skip_run.yml"
VERIFY_FILE = "mock_session_skip_verify.yml"
CALLER_FILE = "mock_session_skip.yml"
CANCEL_FILE = "mock_session_cancel.yml"
BRANCHES = ["mock-verify/**"]
# 같은 브랜치에서 커밋 메시지로 수동 취소 시나리오만 고릅니다.
CANCEL_TOKEN = "[mock-cancel]"
MAX_PARALLEL = 4  # 운영 실행이 러너를 기다리지 않도록 제한
CACHE_PATH = ".mock-cache/mock-state.json"
CACHE_PREPARE_STEP = {
    "name": "Mock 캐시 파일 준비",
    "if": "always()",
    # 실패를 숨기지 않습니다: 파일이 없으면 "저장 안 함" 이 거짓 통과할 수 있습니다.
    "run": f"mkdir -p .mock-cache\ncp -f .state.json {CACHE_PATH}\n",
}
MOCK_PASSPHRASE = "mock-passphrase-not-a-secret"
CACHE_KEY = "mock-skip-${{ inputs.scenario }}-${{ github.run_id }}-${{ github.run_attempt }}"
ARTIFACT = "mock-evidence-${{ github.run_id }}-${{ inputs.scenario }}"
BOT_STEP = "자동매매 봇 실행"
BOT_PRELUDE = """# ── Mock 격리: 가짜 uv 만 사용합니다 ──
export PATH="$GITHUB_WORKSPACE/ops/mock_actions/bin:$PATH"
STUB_CONFIG="$(python3 ops/mock_actions/scenarios.py stub "$MOCK_SCENARIO")"
export STUB_CONFIG STUB_LOG="$RUNNER_TEMP/mock-calls.log"
"""
ISOLATION_STEP = {
    "name": "Mock 격리 (실제 봇 실행 불가, Mock state 준비)",
    "run": (
        "printf '%s\\n' 'raise SystemExit(\"mock workflow: real bot is disabled\")' "
        "> trading_bot.py\n"
        "chmod +x ops/mock_actions/bin/uv\n"
        "printf '%s' '{\"MOCK\": {}}' > .state.json\n"
    ),
}
# 호출자 matrix 가 넘기는 trade_base 입력과 기본값
INPUT_DEFAULTS = {
    "broker": "kis",
    "broker_mode": "demo",
    "trade_mode": "LIVE",
    "run_phase": "LIFECYCLE",
    "bot_timeout": "",
    "fail_after": "false",
}


def _scrub(value):
    """모든 문자열에서 secrets/vars 참조를 빈 문자열 리터럴로 바꿉니다."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and "${{" in value:
        value = value.replace("vars.BOT_STEP_TIMEOUT_MINUTES", "inputs.bot_timeout")
        return re.sub(r"\b(?:secrets|vars)\.[A-Za-z0-9_]+", "''", value)
    return value


def _step(steps, name):
    found = [step for step in steps if step.get("name") == name]
    if len(found) != 1:
        raise ValueError(f"trade_base.yml 에서 '{name}' 스텝을 찾을 수 없습니다")
    return found[0]


def build_run_workflow(base):
    jobs = base["jobs"]
    trade = copy.deepcopy(jobs["run-trade"])
    trade.pop("environment")
    trade["runs-on"] = "ubuntu-latest"
    trade["timeout-minutes"] = 20
    trade["concurrency"] = {
        "group": "mock-skip-${{ inputs.scenario }}-${{ github.run_id }}",
        "cancel-in-progress": False,
    }
    steps = trade["steps"]
    steps.insert(steps.index(_step(steps, "코드 체크아웃")) + 1, dict(ISOLATION_STEP))
    _step(steps, "state 캐시 복원")["with"] = {"path": CACHE_PATH, "key": CACHE_KEY}
    save = _step(steps, "state 캐시 저장")
    save["with"] = {"path": CACHE_PATH, "key": CACHE_KEY}
    steps.insert(steps.index(save), dict(CACHE_PREPARE_STEP))
    _step(steps, "uv 설치")["with"]["enable-cache"] = False
    bot = _step(steps, BOT_STEP)
    bot["run"] = BOT_PRELUDE + bot["run"]
    bot["env"]["MOCK_SCENARIO"] = "${{ inputs.scenario }}"
    for name in ("정산 근거 암호화",):
        _step(steps, name)["env"]["PASSPHRASE"] = MOCK_PASSPHRASE
    _step(steps, "정산 근거 업로드 (암호화본만)")["with"]["name"] = ARTIFACT
    summary_index = steps.index(_step(steps, "실행 결과 요약"))
    steps.insert(
        summary_index,
        {
            "name": "Mock 사후 단계 실패 주입",
            "if": "${{ inputs.fail_after == 'true' }}",
            "run": "echo 'mock: later step failure'; exit 1",
        },
    )

    verify = copy.deepcopy(jobs["verify-settlement"])
    verify.pop("secrets")  # 원본의 `secrets: inherit` 제거 — 비밀값을 넘기지 않습니다.
    verify["uses"] = f"./.github/workflows/{VERIFY_FILE}"
    verify["with"]["artifact_name"] = ARTIFACT

    trade, verify_with = _scrub(trade), _scrub(verify["with"])
    verify["with"] = verify_with

    assertion = {
        "name": "assert",
        "needs": ["run-trade", "verify-settlement"],
        "if": "always()",
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {
                "name": "Mock state 캐시 저장 여부 조회",
                "id": "lookup",
                "uses": "actions/cache/restore@v4",
                "with": {"path": CACHE_PATH, "key": CACHE_KEY, "lookup-only": True},
            },
            {
                "name": "기대값 대조",
                "env": {
                    "SCENARIO": "${{ inputs.scenario }}",
                    "TRADE_RESULT": "${{ needs.run-trade.result }}",
                    "RUN_OUTCOME": "${{ needs.run-trade.outputs.run_outcome }}",
                    "SETTLEMENT_STATUS": "${{ needs.run-trade.outputs.settlement_status }}",
                    "CACHE_HIT": "${{ steps.lookup.outputs.cache-hit }}",
                    "VERIFY_RESULT": "${{ needs.verify-settlement.result }}",
                },
                "run": 'python3 ops/mock_actions/scenarios.py assert "$SCENARIO"',
            },
        ],
    }
    inputs = {
        name: {"required": False, "type": "string", "default": default}
        for name, default in INPUT_DEFAULTS.items()
    }
    # trade_base 가 참조하는 나머지 입력은 빈 문자열 기본값으로 선언합니다.
    declared = base[True]["workflow_call"]["inputs"]
    for name in declared:
        inputs.setdefault(name, {"required": False, "type": "string", "default": ""})
    inputs["scenario"] = {"required": True, "type": "string"}
    return {
        "name": "Mock 세션 SKIP 검증 (재사용)",
        "on": {"workflow_call": {"inputs": inputs}},
        "permissions": {"contents": "read"},
        "jobs": {"run-trade": trade, "verify-settlement": verify, "assert": assertion},
    }


def build_verify_workflow(verify):
    """verify.yml 사본 — uv 캐시를 끄고 비밀값 참조를 Mock 암호로 바꿉니다."""
    workflow = copy.deepcopy(verify)
    workflow["name"] = "Mock 정산 근거 검증 (verify.yml 사본)"
    workflow["on"] = workflow.pop(True)
    workflow["on"]["workflow_call"].pop("secrets")
    workflow["permissions"] = {"contents": "read"}
    steps = workflow["jobs"]["verify"]["steps"]
    _step(steps, "uv 설치")["with"]["enable-cache"] = False
    decrypt = _step(steps, "정산 근거 복호화")
    if "secrets." not in decrypt["env"]["PASSPHRASE"]:
        raise ValueError("verify.yml 의 복호화 암호 참조가 예상과 다릅니다")
    decrypt["env"]["PASSPHRASE"] = MOCK_PASSPHRASE
    return workflow


def build_caller(name, scenarios, *, cancel):
    condition = f"contains(github.event.head_commit.message, '{CANCEL_TOKEN}')"
    if not cancel:
        condition = "!" + condition
    matrix = [
        {"scenario": key, **INPUT_DEFAULTS, **case["inputs"]}
        for key, case in scenarios.items()
    ]
    return {
        "name": name,
        "on": {"push": {"branches": list(BRANCHES)}},
        "permissions": {"contents": "read"},
        "jobs": {
            "mock": {
                "if": "${{ " + condition + " }}",
                "strategy": {
                    "fail-fast": False,
                    "max-parallel": MAX_PARALLEL,
                    "matrix": {"include": matrix},
                },
                "uses": f"./.github/workflows/{RUN_FILE}",
                "with": {
                    key: "${{ matrix.%s }}" % key
                    for key in ("scenario", *INPUT_DEFAULTS)
                },
            }
        },
    }


def build(
    base_path=ROOT / ".github" / "workflows" / "trade_base.yml",
    verify_path=ROOT / ".github" / "workflows" / "verify.yml",
):
    """생성할 {파일명: 워크플로 dict} 를 돌려줍니다."""
    base = yaml.safe_load(Path(base_path).read_text(encoding="utf-8"))
    verify = yaml.safe_load(Path(verify_path).read_text(encoding="utf-8"))
    return {
        RUN_FILE: build_run_workflow(base),
        VERIFY_FILE: build_verify_workflow(verify),
        CALLER_FILE: build_caller("Mock 세션 SKIP 검증", SCENARIOS, cancel=False),
        CANCEL_FILE: build_caller(
            "Mock 세션 취소 검증 (수동 취소)", MANUAL_SCENARIOS, cancel=True
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="생성 파일을 쓸 디렉터리")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for filename, workflow in build().items():
        text = "# 생성 파일 (ops/mock_actions/build.py) — main/PR 에 커밋하지 마세요.\n"
        text += yaml.safe_dump(
            workflow, allow_unicode=True, sort_keys=False, width=10000
        )
        (out / filename).write_text(text, encoding="utf-8")
        print(out / filename)
    return 0


if __name__ == "__main__":
    sys.exit(main())
