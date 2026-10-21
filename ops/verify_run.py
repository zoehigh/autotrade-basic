"""
무인 실행 결과 전용 검증 도구 (read-only — 주문/상태 변경 없음).

    python ops/verify_run.py [YYYYMMDD]

검사 항목
  1) 해당 날짜의 실행 로그 — 모드, exit code, 실행 결과 집계
  2) 브로커 — 실제로 접수된 주문 (수량·가격), 체결
  3) state — T / 순투입 / 보유 / fence
  4) 정합성 — 매도 합계 = 보유량, 매수 가격 중복 없음, 주문 수 일치
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "src"))
os.chdir(APP_DIR)

from broker.meritz.adapter import MeritzBroker  # noqa: E402
from state import load_state  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
problems: list[str] = []
notes: list[str] = []


def problem(msg):
    problems.append(msg)
    print(f"  [!! ] {msg}")


def note(msg):
    notes.append(msg)
    print(f"  [참고] {msg}")


def ok(msg):
    print(f"  [OK ] {msg}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now(KST).strftime("%Y%m%d")
    logs = sorted(Path("logs").glob(f"run-{target}-*.log"))
    if not logs:
        print(f"logs/run-{target}-*.log 없음 — 실행되지 않았습니다.")
        sys.exit(2)
    # 같은 날짜에 DRY 도 함께 있으면 LIVE 실행 로그를 우선합니다.
    live_logs = [p for p in logs if "mode=LIVE" in p.read_text(errors="ignore")]
    log_path = live_logs[-1] if live_logs else logs[-1]

    print(f"=== 실행 로그 검증 ({target}) ===")
    text = log_path.read_text()
    ok(f"로그: logs/{log_path.name}")
    mode = "DRY" if "mode=DRY" in text else "LIVE"
    exit_code = None
    m = re.search(r"\[launcher\] exit=(\d+)", text)
    if m:
        exit_code = int(m.group(1))
        (ok if exit_code == 0 else problem)(f"런처 exit={exit_code}")
    else:
        problem("런처 종료 코드 기록이 없습니다 (비정상 종료 가능)")

    total = re.search(r"합계: 성공 (\d+)건 / 예약 (\d+)건 / 실패 (\d+)건", text)
    executed = reserved = failed = 0
    if total:
        executed, reserved, failed = (int(g) for g in total.groups())
        (ok if failed == 0 else problem)(
            f"실행 결과: 성공 {executed}건 / 예약 {reserved}건 / 실패 {failed}건"
        )
    else:
        note("실행 결과 집계 없음 (휴장일 조기 종료 등)")

    if mode == "LIVE" and exit_code == 0 and executed == 0 and reserved == 0:
        if "신규 주문을 생성하지 않습니다" in text:
            note("주문 0건 — 리버스 정산 대기(정상)")
        else:
            problem("LIVE 인데 접수된 주문이 0건 — 원인 확인 필요")

    print(f"\n=== 브로커·state 검증 ===")
    if mode == "DRY":
        note("DRY 실행 — 주문 접수가 없으므로 브로커 주문 검증은 LIVE 실행 대상입니다")
    broker = MeritzBroker()
    for sym, exch in (("TQQQ", "NAS"), ("SOXL", "AMS")):
        print(f"\n[{sym}]")
        st = load_state(sym)
        bal = broker.get_balance(sym, exch)
        rows = broker.get_order_history(sym, exch, days=5)
        live = [
            r for r in rows
            if str(r.get("ord_dt", "")).startswith(target)
            and r.get("prcs_stat_name") == "미체결"
        ]
        live.sort(key=lambda r: r.get("_order_price") or 0)
        for r in live:
            print(
                f"    {r.get('odno')} {r.get('sll_buy_dvsn_cd_name')} "
                f"{r.get('ft_ord_qty'):>3s}주 @{r.get('_order_price')}"
            )
        filled = [
            r for r in rows
            if str(r.get("ord_dt", "")).startswith(target)
            and int(float(r.get("ft_ccld_qty", "0") or 0)) > 0
        ]
        for r in filled:
            print(
                f"    체결: {r.get('odno')} {r.get('sll_buy_dvsn_cd_name')} "
                f"{r.get('ft_ccld_qty')}주 @{r.get('ft_ccld_unpr3')}"
            )

        buys = [r.get("_order_price") for r in live if r.get("sll_buy_dvsn_cd_name") == "매수"]
        sells = sum(int(float(r.get("ft_ord_qty", 0) or 0))
                    for r in live if r.get("sll_buy_dvsn_cd_name") == "매도")
        if len(buys) != len(set(buys)):
            problem(f"{sym} 매수 가격 중복: {sorted(buys)}")
        else:
            ok(f"{sym} 매수 가격 중복 없음 ({len(buys)}건)")
        if sells > bal.quantity:
            problem(f"{sym} 매도 합계 {sells}주 > 보유 {bal.quantity}주")
        else:
            ok(f"{sym} 매도 합계 {sells}주 ≤ 보유 {bal.quantity}주")
        if mode == "LIVE" and executed and len(live) + len(filled) == 0:
            problem(f"{sym} 접수된 주문이 이력에 없습니다")
        print(
            f"    state: T={st.get('T')} net_invested=${st.get('net_invested')} "
            f"({st.get('net_invested_status')}) 보유={bal.quantity}주 "
            f"fence={'있음' if (st.get('pending_order_intent') or st.get('pending_order_batch')) else '없음'}"
        )
        if st.get("pending_order_intent") or st.get("pending_order_batch"):
            problem(f"{sym} 주문 fence가 남아 있습니다")
        if st.get("balance_mismatch"):
            problem(f"{sym} balance_mismatch: {st.get('balance_mismatch')}")
        if st.get("net_invested_status") != "valid":
            problem(f"{sym} net_invested_status={st.get('net_invested_status')}")

    print("\n" + "=" * 66)
    if problems:
        print(f"판정: 이상 {len(problems)}건 — 확인 필요")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("판정: 이상 없음")
    for n in notes:
        print(f"  - {n}")
    sys.exit(0)


if __name__ == "__main__":
    main()
