#!/usr/bin/env python3
"""복구 감사용 해시 계산 (읽기 전용 — state 를 쓰지 않고 브로커도 호출하지 않습니다).

복구 전 사본과 복구 후 `.state.json` 의 **파일 SHA-256** 과, 대상·가드 종목의 **canonical hash**
(`state.canonical_state_hash`, 복구 입력의 CAS 값과 같은 정의)를 계산해 JSON 으로 저장합니다.
복구 워크플로의 감사 요약이 "입력값"과 "실제 계산값"을 구분해 적을 수 있게 하는 용도입니다.

이 계산은 복구·독립 검증 결과를 바꾸지도 가리지도 않아야 하므로 워크플로에서 별도 단계
(`continue-on-error`)로 실행합니다. 실패하면 출력 파일을 만들지 않고 종료 코드 1 로 끝납니다.

사용: python ops/repair_audit_hashes.py --before B.json --after A.json --symbol SOXL --guard TQQQ --out OUT.json
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _canonical_hashes(path, symbols):
    """파일 `path` 를 상태 파일로 읽어 종목별 canonical hash 를 계산합니다 (복구 입력 CAS 와 같은 경로)."""
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    import state  # noqa: E402  (src 경로 설정 뒤에 import)

    previous = state._STATE_FILE
    state._STATE_FILE = path
    try:
        result = {}
        for symbol in symbols:
            # load_state 가 진단 출력을 하므로 공개 로그에 섞이지 않게 삼킵니다 (값은 JSON 으로만 남깁니다).
            with contextlib.redirect_stdout(io.StringIO()):
                loaded = state.load_state(symbol)
            if loaded.get("_state_unavailable"):
                raise ValueError(f"{symbol}: 상태를 읽을 수 없습니다 ({loaded['_state_unavailable']})")
            result[symbol] = state.canonical_state_hash(loaded)
        return result
    finally:
        state._STATE_FILE = previous


def compute(before_path, after_path, symbol, guard):
    symbols = [symbol, guard]
    return {
        "symbol": symbol,
        "guard_symbol": guard,
        "file_sha256": {"before": _file_sha256(before_path), "after": _file_sha256(after_path)},
        "canonical_hash": {
            "before": _canonical_hashes(before_path, symbols),
            "after": _canonical_hashes(after_path, symbols),
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--guard", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        data = compute(args.before, args.after, args.symbol.upper(), args.guard.upper())
    except (OSError, ValueError, KeyError) as exc:
        print(f"[감사 해시] 계산 실패(복구·검증 결과와 무관): {exc}", file=sys.stderr)
        return 1
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    print("[감사 해시] 계산 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
