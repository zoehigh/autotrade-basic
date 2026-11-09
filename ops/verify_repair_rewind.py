#!/usr/bin/env python3
"""REVERSE_LEDGER_REWIND 복구 결과의 독립 검증기 (src/ 를 import 하지 않고 JSON 만 비교).

복구 전/후 `.state.json` 을 읽어 **허용된 변경만** 일어났는지 확인합니다.
  - 대상 종목 외 모든 종목(가드 포함)은 완전히 동일
  - 대상 종목은 T / net_invested / sell_t_effects / settlement_ledger / 해당 주문의 processed_filled_* 만 변경
  - 변경 값은 복구 전 상태에서 독립적으로 다시 계산해 대조 (T = reverse_base_t, net_invested += 반영 금액 등)

사용: python ops/verify_repair_rewind.py BEFORE.json AFTER.json --symbol SOXL [--odno 2617]  (odno 생략 시 복구 전 상태에서 자동 특정)
종료 코드: 0 = 통과, 1 = 불일치(저장하면 안 됨), 2 = 입력 오류
"""
import argparse
import json
import math
import sys


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _discover_odno(state):
    """odno 를 입력으로 받지 않을 때: 리버스 매도 메타이면서 원장에 일반 sell 항목이 있는 주문(정확히 1개)."""
    sell_odnos = {str(e.get("odno", "")) for e in (state.get("settlement_ledger") or {}).get("entries", []) if e.get("kind") == "sell"}
    found = [str(o) for o, m in (state.get("orders_meta") or {}).items() if m.get("reverse_action") == "sell" and str(o) in sell_odnos]
    return found[0] if len(found) == 1 else None


def verify(before, after, symbol, odno=None):
    """문제 목록(list[str])을 반환합니다. 비어 있으면 통과."""
    problems = []
    if odno is None or str(odno) == "":
        odno = _discover_odno(before.get(symbol) or {})
        if odno is None:
            return ["복구 전 상태에서 되돌릴 대상 주문을 정확히 1개로 특정할 수 없습니다"]
    odno = str(odno)

    if set(before) != set(after):
        problems.append("최상위 종목 집합이 달라졌습니다")
    for sym in before:
        if sym != symbol and before.get(sym) != after.get(sym):
            problems.append(f"대상이 아닌 종목 {sym} 가 변경되었습니다")
    if symbol not in before or symbol not in after:
        problems.append(f"대상 종목 {symbol} 가 없습니다")
        return problems

    b, a = before[symbol], after[symbol]
    changed_keys = {"T", "net_invested", "sell_t_effects", "settlement_ledger", "orders_meta"}
    for key in set(b) | set(a):
        if key not in changed_keys and b.get(key) != a.get(key):
            problems.append(f"허용되지 않은 필드 변경: {key}")

    meta_b = (b.get("orders_meta") or {}).get(odno)
    meta_a = (a.get("orders_meta") or {}).get(odno)
    if not meta_b or meta_a is None:
        problems.append("대상 주문 메타가 없습니다")
        return problems

    # orders_meta: 다른 주문은 동일, 대상 주문은 마커 두 필드만 변경
    if set(b["orders_meta"]) != set(a["orders_meta"]):
        problems.append("orders_meta 주문 집합이 달라졌습니다")
    for k in set(b["orders_meta"]) | set(a["orders_meta"]):
        if k != odno and b["orders_meta"].get(k) != a["orders_meta"].get(k):
            problems.append("다른 주문의 메타가 변경되었습니다")
            break
    for k in set(meta_b) | set(meta_a):
        if k in ("processed_filled_qty", "processed_filled_amount"):
            continue
        if meta_b.get(k) != meta_a.get(k):
            problems.append(f"대상 주문의 허용되지 않은 필드 변경: {k}")
    if meta_a.get("processed_filled_qty") != 0 or meta_a.get("processed_filled_amount") != 0:
        problems.append("대상 주문 마커가 0 이 아닙니다")

    # 복구 전 상태에서 기대값을 독립 계산
    try:
        amount = float(meta_b["processed_filled_amount"])
        base_t = float(meta_b["reverse_base_t"])
        net_before = float(b["net_invested"])
        if not all(math.isfinite(x) for x in (amount, base_t, net_before)) or amount <= 0 or base_t <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        problems.append("복구 전 상태의 기준 값(금액·reverse_base_t·net_invested)이 유효하지 않습니다")
        return problems

    t_after = a.get("T")
    if not isinstance(t_after, (int, float)) or not math.isfinite(t_after) or abs(t_after - base_t) > 1e-4:
        problems.append(f"복구 후 T({t_after})가 reverse_base_t({base_t})와 다릅니다")
    ni_after = a.get("net_invested")
    expected_ni = round(net_before + amount, 2)
    if not isinstance(ni_after, (int, float)) or not math.isfinite(ni_after) or abs(ni_after - expected_ni) > 0.005:
        problems.append(f"복구 후 net_invested({ni_after})가 기대값({expected_ni})과 다릅니다")

    expected_effects = {k: v for k, v in (b.get("sell_t_effects") or {}).items() if k != odno}
    if (b.get("sell_t_effects") or {}).get(odno) is None:
        problems.append("복구 전 sell_t_effects 에 대상 주문이 없었습니다")
    if (a.get("sell_t_effects") or {}) != expected_effects:
        problems.append("sell_t_effects 가 '대상 주문만 제거'와 다릅니다")

    led_b, led_a = b.get("settlement_ledger") or {}, a.get("settlement_ledger") or {}
    ent_b = led_b.get("entries") or []
    mine = [e for e in ent_b if str(e.get("odno", "")) == odno]
    if len(mine) != 1 or mine[0].get("kind") != "sell":
        problems.append("복구 전 원장에 대상 주문의 sell 항목이 정확히 1개가 아니었습니다")
    else:
        seq = mine[0].get("seq")
        if seq != max(e.get("seq", 0) for e in ent_b):
            problems.append("제거 대상 항목이 원장의 마지막이 아니었습니다")
        expected_entries = [e for e in ent_b if e is not mine[0]]
        if (led_a.get("entries") or []) != expected_entries:
            problems.append("원장 항목이 '대상 sell 항목만 제거'와 다릅니다")
        if led_a.get("next_seq") != seq:
            problems.append("원장 next_seq 가 제거된 항목의 seq 로 복귀하지 않았습니다")
    if led_b.get("start_T") != led_a.get("start_T"):
        problems.append("원장 start_T 가 변경되었습니다")
    return problems


def verify_watermark(before, after, symbol, target_updated, target_ordno=None):
    """워터마크 복원 검증: 대상 종목은 last_updated/last_processed_ordno 두 필드만 목표값으로 바뀌고, 나머지는 완전히 동일.

    target_ordno 를 생략하면 복구 전 원장의 마지막 항목 주문번호를 기대값으로 독립 계산합니다.
    """
    problems = []
    if not target_ordno:
        entries = ((before.get(symbol) or {}).get("settlement_ledger") or {}).get("entries") or []
        if not entries:
            return ["복구 전 원장이 비어 있어 기대 last_processed_ordno 를 계산할 수 없습니다"]
        target_ordno = str(max(entries, key=lambda e: e.get("seq", 0)).get("odno", ""))
    if set(before) != set(after):
        problems.append("최상위 종목 집합이 달라졌습니다")
    for sym in before:
        if sym != symbol and before.get(sym) != after.get(sym):
            problems.append(f"대상이 아닌 종목 {sym} 가 변경되었습니다")
    b, a = before.get(symbol), after.get(symbol)
    if b is None or a is None:
        return problems + [f"대상 종목 {symbol} 가 없습니다"]
    for key in set(b) | set(a):
        if key in ("last_updated", "last_processed_ordno"):
            continue
        if b.get(key) != a.get(key):
            problems.append(f"허용되지 않은 필드 변경: {key}")
    if a.get("last_updated") != target_updated:
        problems.append("last_updated 가 목표값과 다릅니다")
    if str(a.get("last_processed_ordno")) != str(target_ordno):
        problems.append("last_processed_ordno 가 목표값과 다릅니다")
    if b.get("last_updated") == a.get("last_updated") and b.get("last_processed_ordno") == a.get("last_processed_ordno"):
        problems.append("워터마크가 변경되지 않았습니다")
    try:
        from datetime import datetime
        if datetime.fromisoformat(a["last_updated"]) >= datetime.fromisoformat(b["last_updated"]):
            problems.append("워터마크가 과거로 이동하지 않았습니다")
    except (KeyError, TypeError, ValueError):
        problems.append("last_updated 를 해석할 수 없습니다")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--odno", default="")
    ap.add_argument("--mode", choices=["ledger-rewind", "watermark"], default="ledger-rewind")
    ap.add_argument("--target-last-updated", default="")
    ap.add_argument("--target-last-ordno", default="")
    args = ap.parse_args(argv)
    try:
        before, after = _load(args.before), _load(args.after)
    except (OSError, ValueError) as exc:
        print(f"[검증] 입력 오류: {exc}", file=sys.stderr)
        return 2
    if args.mode == "watermark":
        problems = verify_watermark(before, after, args.symbol.upper(), args.target_last_updated, args.target_last_ordno)
    else:
        problems = verify(before, after, args.symbol.upper(), args.odno)
    if problems:
        print("[검증 실패] 복구 결과가 허용 범위를 벗어났습니다 — 캐시를 저장하면 안 됩니다")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"[검증 통과] {args.symbol.upper()} 외 종목 불변, 대상 종목은 허용된 필드만 기대값대로 변경 ({args.mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
