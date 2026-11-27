#!/usr/bin/env python3
"""Actions Mock 검증 시나리오와 기대값 (단일 출처).

    python3 ops/mock_actions/scenarios.py stub <name>     # 가짜 uv 설정(JSON) 출력
    python3 ops/mock_actions/scenarios.py assert <name>   # 환경변수의 실제 결과를 기대값과 대조

기대값 필드: `trade`(run-trade 결과), `outcome`(허용되는 run_outcome 목록),
`settlement`(허용되는 settlement_status 목록), `cache`(새 state 캐시 저장 여부),
`verify`(허용되는 verify-settlement 결과). 표준 라이브러리만 씁니다.
"""
import json
import os
import sys

RAN = ["success", "failure"]  # 정산 검증이 실행됨 (Mock 근거라 성패는 보지 않음)
FAILED = ["failure"]  # 근거 누락 → 반드시 실패
SKIPPED = ["skipped"]


def _gate(window="open", wait=0, due=True, hold=""):
    return {
        "window": window,
        "session": "2026-11-27",
        "reason": f"mock window={window}",
        "wait_seconds": wait,
        "settlement_due": "true" if due else "false",
        "settled_session": "2026-11-25",
        "state_hold": hold,
    }


def _case(stub, trade, outcome, settlement, cache, verify, **inputs):
    return {
        "stub": stub,
        "inputs": inputs,
        "expect": {
            "trade": trade,
            "outcome": outcome,
            "settlement": settlement,
            "cache": cache,
            "verify": verify,
        },
    }


SCENARIOS = {
    "orders": _case(
        {"gate": _gate()}, "success", ["ORDERS_EXECUTED"], ["ATTEMPTED"], True, RAN
    ),
    "holiday-settle": _case(
        {"gate": _gate("closed")},
        "success", ["SETTLE_ONLY"], ["ATTEMPTED"], True, RAN,
    ),
    "skip": _case(
        {"gate": _gate("closed", due=False)},
        "success", ["SKIPPED"], ["SKIPPED"], False, SKIPPED,
    ),
    "skip-late-failure": _case(
        {"gate": _gate("closed", due=False)},
        "failure", ["SKIPPED"], ["SKIPPED"], False, FAILED,
        fail_after="yes",
    ),
    "dry-holiday": _case(
        {"gate": _gate(), "skip_marker": "mock holiday"},
        "success", ["SKIPPED"], ["SKIPPED"], False, SKIPPED,
        trade_mode="DRY",
    ),
    "hold-blocks-skip": _case(
        {"gate": _gate("closed", due=False, hold="MOCK:pending_order_intent")},
        "failure", ["NOT_STARTED"], [""], False, FAILED,
    ),
    "gate-error": _case(
        {"gate_rc": 1}, "failure", ["NOT_STARTED"], [""], False, FAILED
    ),
    "wait-budget": _case(
        {"gate": _gate(wait=30000)},
        "failure", ["NOT_STARTED"], [""], False, FAILED,
    ),
    "real-review-gate": _case(
        {"gate": _gate()},
        "failure", ["NOT_STARTED"], [""], False, FAILED,
        broker_mode="real",
    ),
    "evidence-missing": _case(
        {"gate": _gate("closed"), "no_evidence": True},
        "success", ["SETTLE_ONLY"], ["ATTEMPTED"], True, FAILED,
    ),
    "settle-failure": _case(
        {"gate": _gate(), "rc": {"SETTLE": 1}},
        "failure", ["FAILED"], ["ATTEMPTED"], True, FAILED,
    ),
    "partial": _case(
        {"gate": _gate(), "plans": ["P1", "P2"],
         "rc": {"EXECUTE:P2": 1, "VERIFY:P2": 1}},
        "failure", ["PARTIAL_FAILURE"], ["ATTEMPTED"], True, RAN,
    ),
    # 아래 두 건은 GitHub 이 중단된 스텝의 output 을 보존하는지 **관측**합니다.
    # 표식이 비어도(""), 캐시 저장과 정산 검증은 반드시 수행돼야 합니다.
    "killed": _case(
        {"gate": _gate(), "kill": "EXECUTE"},
        "failure", ["PARTIAL_FAILURE", ""], ["ATTEMPTED", ""], True, RAN,
    ),
    "step-timeout": _case(
        {"gate": _gate(), "sleep": "EXECUTE", "sleep_seconds": 300},
        "failure", ["PARTIAL_FAILURE", ""], ["ATTEMPTED", ""], True, RAN,
        bot_timeout="1",
    ),
}

# 사람이 `gh run cancel` 로 취소해야 하는 시나리오 (별도 워크플로·브랜치 패턴)
MANUAL_SCENARIOS = {
    "cancel": _case(
        {"gate": _gate(), "sleep": "EXECUTE", "sleep_seconds": 600},
        "cancelled", ["PARTIAL_FAILURE", ""], ["ATTEMPTED", ""], True,
        # 취소된 run 에서 검증 job 이 어떻게 끝나는지는 관측 대상입니다.
        ["success", "failure", "cancelled"],
    ),
}

ALL_SCENARIOS = {**SCENARIOS, **MANUAL_SCENARIOS}


def check(name, trade, outcome, settlement, cache_hit, verify):
    """실제 결과를 기대값과 대조해 문제 목록을 돌려줍니다."""
    expect = ALL_SCENARIOS[name]["expect"]
    problems = []
    if trade != expect["trade"]:
        problems.append(f"run-trade 결과 {trade!r} != {expect['trade']!r}")
    if outcome not in expect["outcome"]:
        problems.append(f"run_outcome {outcome!r} not in {expect['outcome']}")
    if settlement not in expect["settlement"]:
        problems.append(
            f"settlement_status {settlement!r} not in {expect['settlement']}"
        )
    saved = str(cache_hit).lower() == "true"
    if saved is not expect["cache"]:
        problems.append(f"state 캐시 저장 {saved} != {expect['cache']}")
    if verify not in expect["verify"]:
        problems.append(f"verify-settlement 결과 {verify!r} not in {expect['verify']}")
    return problems


def main(argv):
    command, name = argv
    if command == "stub":
        print(json.dumps(ALL_SCENARIOS[name]["stub"]))
        return 0
    if command != "assert":
        raise SystemExit(f"unknown command: {command}")
    observed = {
        "trade": os.environ.get("TRADE_RESULT", ""),
        "outcome": os.environ.get("RUN_OUTCOME", ""),
        "settlement": os.environ.get("SETTLEMENT_STATUS", ""),
        "cache_hit": os.environ.get("CACHE_HIT", ""),
        "verify": os.environ.get("VERIFY_RESULT", ""),
    }
    problems = check(name, **observed)
    lines = [
        f"### Mock 시나리오 `{name}`: {'FAIL' if problems else 'PASS'}",
        f"- 관측: {observed}",
        *[f"- ❌ {problem}" for problem in problems],
    ]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
