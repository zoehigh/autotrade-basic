#!/usr/bin/env python3
"""복원한 state 파일의 지문 계산 (읽기 전용 — state 를 쓰지 않고 브로커도 호출하지 않습니다).

복구 워크플로의 `BACKUP` 모드에서 쓰입니다. 파일 전체의 SHA-256 과, 파일에 들어 있는 **모든 종목**의
canonical hash(`state.canonical_state_hash`, 복구 입력 CAS 와 같은 정의)·T·net_invested·last_updated 를 출력합니다.
이 값들은 이어서 `REWIND` 를 실행할 때 넣을 입력값(expect_file_sha256·expect_hash·expect_t·guard_hash)이 됩니다.

- 입력 SHA(`--expect-sha`)가 있으면 반드시 대조하고, 다르면 값을 모두 출력한 뒤 종료 코드 1 로 끝납니다.
- 입력 SHA 가 없으면 "사전 지문 대조 없이" 계산했다고 표시합니다 (BACKUP 에서만 허용되는 경로).
- 이 스크립트는 시크릿이 없는 잡에서 실행합니다. 주문번호 원문은 출력하지 않습니다.

종료 코드: 0 = 계산 완료(입력 SHA 가 있으면 일치), 1 = 불일치 또는 계산 실패
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repair_audit_hashes import _canonical_hashes  # noqa: E402  (같은 읽기 전용 계산을 재사용)


def _symbols(data):
    """최상위 항목 중 종목 상태(dict)인 것만 종목으로 봅니다."""
    return sorted(k for k, v in data.items() if isinstance(v, dict))


def fingerprint(path, expect_sha=""):
    with open(path, "rb") as f:
        raw = f.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("state 파일의 최상위가 객체가 아닙니다")
    symbols = _symbols(data)
    if not symbols:
        raise ValueError("종목 상태가 없습니다")
    return {
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "expect_sha": expect_sha,
        "symbols": {
            sym: {
                "canonical_hash": h,
                "T": data[sym].get("T"),
                "net_invested": data[sym].get("net_invested"),
                "last_updated": data[sym].get("last_updated", ""),
            }
            for sym, h in _canonical_hashes(path, symbols).items()
        },
    }


def render(result):
    """GITHUB_STEP_SUMMARY 용 마크다운 (주문번호 원문 없음)."""
    sha, expect = result["file_sha256"], result["expect_sha"]
    if not expect:
        verdict = "**사전 지문 대조 없이** 계산했습니다 (입력 SHA 를 생략했으므로 복원본이 기대한 파일인지는 확인하지 못했습니다)."
    elif expect == sha:
        verdict = "입력 SHA-256 과 **일치**합니다."
    else:
        verdict = f"❌ 입력 SHA-256 과 **다릅니다** (입력: `{expect}`)."
    lines = [
        "## 복원 state 지문 (BACKUP 모드)",
        "",
        f"- 파일 SHA-256: `{sha}`",
        f"- {verdict}",
        "- 아래 값은 이어서 `REWIND` 를 실행할 때 입력값(expect_file_sha256 · expect_hash · expect_t · guard_hash)으로 쓸 수 있습니다.",
        "",
        "| 종목 | canonical hash | T | net_invested | last_updated |",
        "|---|---|---|---|---|",
    ]
    for sym, v in result["symbols"].items():
        lines.append(f"| `{sym}` | `{v['canonical_hash']}` | {v['T']} | {v['net_invested']} | {v['last_updated']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--expect-sha", default="")
    ap.add_argument("--summary", default="", help="마크다운을 이어 쓸 파일(GITHUB_STEP_SUMMARY). 없으면 출력만")
    args = ap.parse_args(argv)
    try:
        result = fingerprint(args.state, args.expect_sha.strip())
    except (OSError, ValueError) as exc:
        print(f"[지문] 계산 실패: {exc}", file=sys.stderr)
        return 1
    text = render(result)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as out:
            out.write(text)
    print(text)
    if args.expect_sha.strip() and args.expect_sha.strip() != result["file_sha256"]:
        print("[지문] 입력 SHA-256 과 복원본이 다릅니다", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
