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


def _load_settlement_evidence(symbol, run_id):
    """
    **검증 대상 실행의** 정산 근거 파일을 읽습니다 (없으면 None).

    가장 최신 파일을 쓰면 다른 실행(예: 이후 DRY)의 근거로 잘못 검증할 수 있으므로,
    실행 식별자(run_id)로 정확히 일치하는 파일만 사용합니다.
    """
    if not run_id:
        return None
    for path in sorted(Path(".").glob(f"settlement-{symbol}-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("run_id") or "") == run_id:
            return data
    return None


def _classify_sell_ratio(ratio: float) -> float:
    """정산 로직과 같은 분류 규칙 (독립 구현)."""
    if ratio >= 1.0:
        return 0.0
    if ratio >= 0.5:
        return 0.25
    return 0.75


def _classify_sell_ratio(ratio: float) -> float:
    """정산 로직과 같은 분류 규칙 (독립 구현)."""
    if ratio >= 1.0:
        return 0.0
    if ratio >= 0.5:
        return 0.25
    return 0.75


def _recompute_expected(evidence):
    """
    정산 근거를 **독립적으로 대조**합니다.

    2026-10-22 T 가 13.375 → 3.3438 로 잘못 갱신됐는데 검증은 "이상 없음"을 보고했습니다.
    근거에 기록된 주문별 적용 내역을 원시 체결 데이터와 대조하고,
    (1) 매도 분류(배율)를 기준선에서 **독립 계산**해 적용 배율과 비교하고,
    (2) T 변화·순투입금 산술을 기록된 델타로 **재계산**해 실제 결과와 비교합니다.

    Returns:
        tuple: (문제 목록, 예상 T, 예상 순투입금)
    """
    problems: list[str] = []
    if not evidence:
        return ["정산 근거 파일 없음 — 검증 불가"], None, None
    if evidence.get("basis_unknown"):
        problems.append("보유수량 기준 미확보로 정산이 보류됐습니다 (검증 불가)")

    t_before = evidence.get("T_before")
    money_before = evidence.get("net_invested_before")
    if t_before is None or money_before is None:
        problems.append("정산 전 T/순투입금 기준이 근거에 없습니다 (검증 불가)")
        return problems, None, None

    t_value = float(t_before)
    money = float(money_before)
    orders = evidence.get("orders") or []
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}

    # 묶음 직전 보유량 (독립 계산) — 매도 기준선 대조에 사용
    position = None
    balance = evidence.get("balance_qty")
    if isinstance(balance, int):
        net_batch = 0
        for o in orders:
            q = int(o.get("qty_delta") or 0)
            net_batch += q if o.get("side") == "매수" else -q
        position = balance - net_batch

    for o in orders:
        odno = str(o.get("odno"))
        side = o.get("side")
        qty_delta = int(o.get("qty_delta") or 0)
        amount_delta = float(o.get("amount_delta") or 0)
        raw = fills.get(odno) or {}
        raw_qty = int(raw.get("qty") or 0)
        raw_amount = float(raw.get("amount") or 0)

        # ① 적용 델타는 원시 누적 체결을 넘을 수 없습니다
        if qty_delta > raw_qty:
            problems.append(
                f"{odno} 적용 수량 {qty_delta}주 가 누적 체결 {raw_qty}주 를 초과합니다"
            )
        if side == "매수" and amount_delta > raw_amount + 0.01:
            problems.append(
                f"{odno} 적용 금액 ${amount_delta:.2f} 가 누적 체결금액 "
                f"${raw_amount:.2f} 를 초과합니다"
            )

        money += amount_delta if side == "매수" else -amount_delta

        if side == "매수":
            # T 변화 독립 재계산
            t_target = float(o.get("t_target") or 0.0)
            total_qty = int(o.get("total_qty") or 0)
            if not o.get("is_additional") and total_qty > 0 and t_target > 0:
                t_value += (qty_delta / total_qty) * t_target
        else:
            # ② 매도 분류를 기준선에서 독립 계산해 적용 배율과 대조
            basis = int(o.get("basis") or 0)
            cumulative = int(o.get("cumulative_qty") or 0)
            applied = float(o.get("factor") or 1.0)
            # 첫 체결이 이번 묶음에 있으면 기준선을 독립 계산해 대조합니다.
            # (이전 실행에서 일부 체결된 주문은 기준선이 이미 확정되어 있으므로 기록값을 씁니다)
            prev_cumulative = int(o.get("prev_cumulative_qty") or 0)
            check_basis = basis
            if prev_cumulative == 0 and position is not None:
                check_basis = int(position)
                if basis and abs(basis - check_basis) > 1:
                    problems.append(
                        f"{odno} 보유수량 기준 불일치: 기록 {basis}주 vs 독립 계산 {check_basis}주"
                    )
            if check_basis > 0 and cumulative > 0:
                expected_factor = _classify_sell_ratio(cumulative / check_basis)
                if abs(expected_factor - applied) > 1e-6:
                    problems.append(
                        f"{odno} 매도 분류 불일치: 적용 배율 {applied} "
                        f"vs 기준 {check_basis}주·누적 {cumulative}주 기준 {expected_factor} "
                        f"— T 분류 오류 가능"
                    )
            if position is not None:
                position = max(0, position - cumulative)
            # T 변화는 기록된 델타로 대조 (산술 검증)
            t_value += float(o.get("t_delta") or 0.0)
    return problems, round(t_value, 4), round(money, 2)


def _verify_evidence(evidence):
    """정산 근거로 예상 T/순투입금을 계산해 실제 값과 대조합니다."""
    problems, expected_t, expected_money = _recompute_expected(evidence)
    if expected_t is None:
        return problems
    actual_t = evidence.get("T_after")
    actual_money = evidence.get("net_invested_after")
    if actual_t is not None and abs(float(actual_t) - expected_t) > 0.01:
        problems.append(
            f"T 불일치: 정산 결과 {float(actual_t):.4f} vs 근거 재계산 {expected_t:.4f}"
        )
    if actual_money is not None and abs(float(actual_money) - expected_money) > 0.05:
        problems.append(
            f"순투입금 불일치: 정산 결과 ${float(actual_money):.2f} "
            f"vs 근거 재계산 ${expected_money:.2f}"
        )
    return problems


def _verify_t_transitions(text, symbol, rows, balance_qty):
    """
    로그의 T 변화가 실제 체결 내용과 일치하는지 검사합니다.

    2026-10-22 실제로 T 가 13.375 → 3.3438 로 잘못 갱신됐는데 검증은 "이상 없음"을
    보고했습니다. 로그의 매도 비율을 브로커 잔고로 산출한 실제 분모와 대조해 잡습니다.
    """
    problems: list[str] = []
    m_before = re.search(
        rf"\[상태\] {re.escape(symbol)} 상태 로드 완료 → T=([0-9.]+), 마지막 갱신: (\S+)", text
    )
    m_after = re.search(
        rf"\[상태\] {re.escape(symbol)} T값 업데이트 완료 → T=([0-9.]+)", text
    )
    if not m_before or not m_after:
        return problems
    t_before = float(m_before.group(1))
    t_after = float(m_after.group(1))
    watermark = m_before.group(2)

    # 종목별 섹션만 봅니다 (체결 전이 로그에는 종목명이 없어 섹션으로 구분합니다).
    section = text
    marker = f"종목 처리 시작: {symbol} "
    if marker in text:
        section = text[text.index(marker):]
        nxt = section.find("종목 처리 시작: ", len(marker))
        if nxt != -1:
            section = section[:nxt]

    # 로그에 기록된 전이를 따라 T 을 재현합니다 (매수 ΔT / 매도 후 T).
    logged_ratios: list[float] = []
    t_running = t_before
    for line in section.splitlines():
        m_buy = re.search(r"→ 매수 체결 \(\d+\): .*ΔT=([0-9.]+) → T=([0-9.]+)", line)
        if m_buy:
            t_running = float(m_buy.group(2))
            continue
        m_sell = re.search(
            r"→ 매도 체결 \(\d+\): \S+ \(비율=([0-9.]+)(?:, 보유 (\d+)주)?\) → T=([0-9.]+)", line
        )
        if m_sell:
            logged_ratios.append(float(m_sell.group(1)))
            t_running = float(m_sell.group(3))
    if abs(t_running - t_after) > 0.0001:
        problems.append(
            f"{symbol} 로그 내 T 전이({t_running}) ≠ 최종 T({t_after})"
        )

    # 실제 분모(보유수량)로 로그의 매도 비율을 대조합니다.
    if logged_ratios and balance_qty is not None:
        net = 0
        for r in rows:
            ts = r.get("ord_datetime_utc") or ""
            if not ts or ts <= watermark:
                continue
            q = int(float(r.get("ft_ccld_qty", "0") or 0))
            if r.get("sll_buy_dvsn_cd_name") == "매수":
                net += q
            elif r.get("sll_buy_dvsn_cd_name") == "매도":
                net -= q
        position = balance_qty - net
        sells = sorted(
            [
                r for r in rows
                if (r.get("ord_datetime_utc") or "") > watermark
                and r.get("sll_buy_dvsn_cd_name") == "매도"
                and int(float(r.get("ft_ccld_qty", "0") or 0)) > 0
            ],
            key=lambda x: x.get("ord_datetime_utc", ""),
        )
        for idx, r in enumerate(sells):
            if idx >= len(logged_ratios):
                break
            q = int(float(r.get("ft_ccld_qty", "0") or 0))
            if position > 0:
                expected = q / position
                if abs(expected - logged_ratios[idx]) > 0.05:
                    problems.append(
                        f"{symbol} 매도 비율 불일치: 로그 {logged_ratios[idx]:.2f} "
                        f"vs 실제 보유 {position}주 기준 {expected:.2f} — T 분류 오류 가능"
                    )
            position = max(0, position - q)
    return problems


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    expect_live = "--expect-live" in sys.argv
    target = args[0] if args else datetime.now(KST).strftime("%Y%m%d")
    logs = sorted(Path("logs").glob(f"run-{target}-*.log"))
    if not logs:
        print(f"logs/run-{target}-*.log 없음 — 실행되지 않았습니다.")
        sys.exit(2)
    # **가장 최근 로그**를 검사합니다. (이전 LIVE 로그를 대신 검사하면
    #  이번 예약 실행이 실패했는데 조용히 통과될 수 있습니다)
    log_path = logs[-1]

    print(f"=== 실행 로그 검증 ({target}) ===")
    text = log_path.read_text()
    ok(f"로그: logs/{log_path.name}")
    mode = "DRY" if "mode=DRY" in text else "LIVE"
    if expect_live and mode != "LIVE":
        problem(
            f"예약 LIVE 실행 로그가 없습니다 (가장 최근 로그: {log_path.name}, mode={mode})"
        )
    exit_code = None
    m_run = re.search(r"\[launcher\] start .*", text)
    run_id = ""
    if m_run:
        run_id = Path(log_path).stem
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
        for msg in _verify_t_transitions(text, sym, rows, bal.quantity):
            problem(msg)
        evidence = _load_settlement_evidence(sym, run_id)
        if evidence is None:
            problem(
                f"{sym} 정산 근거 파일 없음 (run_id={run_id or '없음'}) — T 검산 불가"
            )
        else:
            for msg in _verify_evidence(evidence):
                problem(msg)
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
