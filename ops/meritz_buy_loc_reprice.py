#!/usr/bin/env python3
"""메리츠 보정된 일반모드 BUY LOC 사후 재조정 도구 (긴급 hotfix).

기본 동작은 **계획 출력만** 합니다 (조회 전용, 주문·정정·state 저장 없음).
실제 정정은 아래를 모두 만족할 때만 실행합니다.
  --apply  --confirm "REPRICE <종목>"  + ALLOW_BROKER_ORDERS=1 + TRADE_MODE=LIVE
  + BROKER=meritz + trading_bot 프로세스 없음 + 미해결 journal 없음

사용 예
  uv run python ops/meritz_buy_loc_reprice.py --symbol SOXL
  uv run python ops/meritz_buy_loc_reprice.py --symbol SOXL --original 20261110:0000123=73.69
"""
import argparse
import fcntl
from copy import deepcopy
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import meritz_reprice as rp  # noqa: E402
import run_lock  # noqa: E402
import state as state_module  # noqa: E402
from state import load_state, save_state, mask_odno  # noqa: E402



def parse_overrides(items):
    """--original ODNO=PRICE 를 {odno: price} 로 바꿉니다."""
    result = {}
    for item in items or []:
        odno, _, price = item.rpartition("=")
        if not odno or not rp.is_valid_price(price):
            raise SystemExit(f"--original 형식 오류: {item!r} (유한한 양수 가격, 예: 20261110:0000123=73.69)")
        result[odno] = float(price)
    return result


def bot_is_running() -> bool:
    out = subprocess.run(["pgrep", "-f", "trading_bot.py"], capture_output=True, text=True)
    return bool(out.stdout.strip())


def print_plan(symbol, plan):
    """계획 출력 — 정정/유지/보류 사유는 개별 출력, 그 밖의 SKIP 은 사유별 건수로 요약합니다."""
    print(f"\n[{symbol}] 정정 계획 (state 주문 {len(plan)}건)")
    quiet = {}
    for item in plan:
        tag = mask_odno(item["odno"])
        if item["action"] in (rp.ACTION_AMEND, rp.ACTION_NOOP):
            print(f"  {item['action']:<17} {tag} {item['qty']}주 "
                  f"현재 ${item['current_price']:.2f} → 목표 ${item['target_price']:.2f} "
                  f"(원래 ${item['original_price']:.2f}, 현재가 ${item['last_price']:.2f}) — {item['reason']}")
        elif item["action"] == rp.ACTION_SKIP and not any(
            word in item["reason"] for word in ("보류", "정정", "원래 전략 매수가", "부분체결", "상태 불확실")
        ):
            quiet[item["reason"]] = quiet.get(item["reason"], 0) + 1
        else:
            print(f"  {item['action']:<17} {tag} — {item['reason']}")
    for reason, count in sorted(quiet.items(), key=lambda kv: -kv[1]):
        print(f"  SKIP(요약) {count:>3}건 — {reason}")


def main(argv=None, broker=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--symbol", action="append", help="대상 종목 (기본: SYMBOLS 설정 전체)")
    parser.add_argument("--original", action="append", help="원래 매수가 지정 ODNO=PRICE (메타에 없을 때)")
    parser.add_argument("--apply", action="store_true", help="실제 정정 실행 (기본은 계획만)")
    parser.add_argument("--confirm", default="", help='실행 확인 문구: "REPRICE <종목>"')
    parser.add_argument("--original-checked", action="store_true",
                        help="앱/HTS 에서 원주문 상태(생존 여부)를 확인했음을 명시 — REJECTED_LATE 표식 journal 을 정리할 때만 필요")
    args = parser.parse_args(argv)

    import config
    overrides = parse_overrides(args.original)
    configs = [c for c in config.SYMBOLS
               if not args.symbol or c["symbol"] in {s.upper() for s in args.symbol}]
    if not configs:
        raise SystemExit("대상 종목이 없습니다")

    if args.apply:
        problems = []
        if config.BROKER != "meritz":
            problems.append("BROKER=meritz 가 아님")
        if config.TRADE_MODE != "LIVE":
            problems.append("TRADE_MODE=LIVE 가 아님")
        if os.getenv("ALLOW_BROKER_ORDERS") != "1":
            problems.append("ALLOW_BROKER_ORDERS=1 이 아님")
        if bot_is_running():
            problems.append("trading_bot 프로세스가 실행 중")
        if problems:
            raise SystemExit("정정 실행 거부: " + ", ".join(problems))

    # 실제 정정(--apply)은 운영 런처와 같은 .run.lock 을 잡아야만 진행합니다.
    # (pgrep 확인만으로는 확인 직후 시작하는 크론과의 경쟁을 막을 수 없음)
    # .run.lock = 런처 시작 구간, .bot.lock = 봇 본체 실행 구간 (직접 실행 포함).
    # 둘 다 .state.json 과 같은 디렉터리의 파일이라 항상 같은 체크아웃을 가리킵니다.
    locks = []
    journal_file = run_lock.journal_path()
    if args.apply:
        print(f"[대상 확인] state={os.path.abspath(state_module._STATE_FILE)} journal={journal_file}")
        for path in (run_lock.run_lock_path(), run_lock.bot_lock_path()):
            try:
                locks.append(run_lock.acquire_nonblocking(path))
            except RuntimeError as error:
                raise SystemExit(str(error))

    if broker is None:
        from broker.meritz.adapter import MeritzBroker
        broker = MeritzBroker()  # 조회에는 DryBroker 불필요 — 이 도구는 apply 가 없으면 조회만 호출

    today_session = datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y%m%d")
    exit_code = 0
    for cfg in configs:
        symbol, exchange = cfg["symbol"], cfg["exchange"]
        exchange_code = broker.exchange_code(exchange)
        state = load_state(symbol)
        history = broker.get_order_history(symbol, exchange_code, days=30, verbose=False)

        # 미해결 journal 먼저 처리 (재시작/중복 실행 대비)
        journal = [e for e in rp.read_journal(journal_file) if e["symbol"] == symbol]
        # 복구(state 저장·journal 정리)도 쓰기 작업이므로 종목별 확인 문구가 맞을 때만 수행합니다
        confirmed = args.apply and args.confirm == f"REPRICE {symbol}"
        if journal:
            print(f"[{symbol}] 미해결 정정 journal {len(journal)}건 — 이력으로 확인합니다")
            if args.apply and not confirmed:
                print(f'  확인 문구 불일치 — 복구는 "REPRICE {symbol}" 일 때만 수행합니다 (조회만 합니다)')
            review = [e for e in journal if e.get("review_required")]
            if confirmed and review and not args.original_checked:
                # 원주문 생존 여부를 이력으로 알 수 없는 항목은, 사람이 확인했다고 명시하기 전에는 정리하지 않습니다.
                print(f"  원주문 확인이 필요한 항목 {len(review)}건({review[0].get('review_required')}) — "
                      f"앱/HTS 에서 원주문 상태를 확인하고 필요하면 취소한 뒤 --original-checked 를 함께 지정하세요. 정리하지 않습니다")
                exit_code = 1
                continue
            if confirmed:
                left, messages = rp.resolve_journal(journal, history, state)
                for message in messages:
                    print("  " + message)
                if len(left) < len(journal):
                    save_state(symbol, state)  # state 저장 성공 후에만 journal 을 줄입니다
                others = [e for e in rp.read_journal(journal_file) if e["symbol"] != symbol]
                rp.write_journal(journal_file, others + left)
            else:
                # 조회 모드: 사본에서만 확인하고 state·journal 은 절대 바꾸지 않습니다
                left, messages = rp.resolve_journal(journal, history, deepcopy(state))
                for message in messages:
                    print("  [조회 모드, 저장 안 함] " + message)
            if left or (journal and not confirmed):
                print(f"[{symbol}] 미해결 journal 이 남아 있어 이 종목은 중단합니다 (자동 재주문 안 함)")
                exit_code = 1
                continue

        last_price = broker.get_stock_price(symbol, exchange_code).last
        unmatched = [o for o in overrides if o not in (state.get("orders_meta") or {})]
        if unmatched:
            print(f"[{symbol}] --original 대상 주문번호가 state 에 없습니다: "
                  f"{[mask_odno(o) for o in unmatched]}")
        plan = rp.build_plan(state, history, last_price, overrides, today_session)
        print_plan(symbol, plan)

        targets = [item for item in plan if item["action"] == rp.ACTION_AMEND]
        if not args.apply:
            print(f"[{symbol}] 계획만 출력했습니다 (정정 대상 {len(targets)}건, 실행하지 않음)")
            continue
        if args.confirm != f"REPRICE {symbol}":
            print(f'[{symbol}] 확인 문구 불일치 — "REPRICE {symbol}" 필요. 실행하지 않습니다')
            exit_code = 1
            continue

        for item in targets:
            # 정정 직전 재조회·재계획: 그 사이 체결/시세 변동이 있으면 최신 값 기준으로만 실행
            fresh_history = broker.get_order_history(symbol, exchange_code, days=30, verbose=False)
            fresh_last = broker.get_stock_price(symbol, exchange_code).last
            fresh_plan = rp.build_plan(state, fresh_history, fresh_last, overrides, today_session)
            fresh = next((p for p in fresh_plan if p["odno"] == item["odno"]), None)
            if not fresh or fresh["action"] != rp.ACTION_AMEND:
                print(f"  - 재확인에서 정정 불필요/불가: {mask_odno(item['odno'])}")
                continue
            history_fn = lambda: broker.get_order_history(symbol, exchange_code, days=30, verbose=False)
            result = rp.apply_amend(broker, symbol, exchange_code, fresh, state, journal_file,
                                    save_state, history_fn)
            if result in ("REJECTED_LATE", "UNCERTAIN", "LINK_FAILED"):
                # 원주문 생존 불명/접수 불확실: 다른 주문·종목도 더 정정하지 않고 즉시 전체 중단합니다 (journal 유지).
                print("  → 즉시 전체 중단합니다. 재주문하지 않고 journal 을 유지합니다 (봇은 해당 종목을 보류)")
                return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
