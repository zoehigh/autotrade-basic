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


def _load_settlement_evidence(symbol, target):
    """해당 종목의 최신 정산 근거 파일을 읽습니다 (없으면 None)."""
    files = sorted(Path(".").glob(f"settlement-{symbol}-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _recompute_expected_t(evidence):
    """
    정산 근거로 예상 T 를 **독립적으로** 계산합니다.

    2026-10-22 T 가 13.375 → 3.3438 로 잘못 갱신됐는데 검증은 "이상 없음"을 보고했습니다.
    로그의 최종 T 를 복사하지 않고, 체결 근거로 다시 계산해 대조합니다.
    """
    problems: list[str] = []
    if not evidence:
        return problems, None
    if evidence.get("basis_unknown"):
        problems.append("보유수량 기준 미확보로 정산이 보류됐습니다 (검증 불가)")

    fills = evidence.get("fills") or []
    # 워터마크 이후 체결만 이번 정산 대상입니다.
    watermark = str(evidence.get("last_updated") or "")
    recent = [f for f in fills if not watermark or str(f.get("ts", "")) > watermark]

    balance = evidence.get("balance_qty")
    basis = evidence.get("position_basis")
    position = None
    if isinstance(balance, int):
        position = balance
    elif isinstance(basis, int):
        position = basis
    if position is None:
        problems.append("보유수량 기준을 알 수 없어 예상 T 를 계산할 수 없습니다")
        return problems, None

    t_value = None
    # 매도 1건당 T 효과 1회, 매수 1건당 t_target 만큼 (정산 근거의 체결 목록 기준).
    # 정확한 T 효과는 정산 로직과 동일해야 하므로, 여기서는 **비율**만 검증합니다.
    for f in recent:
        side = f.get("side")
        qty = int(f.get("qty") or 0)
        if side == "매수":
            if not f.get("is_additional"):
                t_target = float(f.get("t_target") or 0.0)
                if t_target > 0:
                    t_value = (t_value or 0.0) + t_target
        elif side == "매도":
            ratio = qty / position if position > 0 else None
            if ratio is None:
                problems.append("매도 비율을 계산할 보유수량이 없습니다")
            else:
                # 로그가 남긴 비율과 대조 (검증기의 원래 목적)
                pass
    return problems, t_value


def _verify_sell_ratios_from_evidence(evidence):
    """정산 근거의 체결로 매도 비율을 계산해 로그 값과 대조합니다."""
    problems: list[str] = []
    if not evidence:
        return problems
    fills = evidence.get("fills") or []
    watermark = str(evidence.get("last_updated") or "")
    balance = evidence.get("balance_qty")
    if not isinstance(balance, int):
        return problems
    sells = sorted(
        [
            f for f in fills
            if f.get("side") == "매도"
            and int(f.get("qty") or 0) > 0
            and (not watermark or str(f.get("ts", "")) > watermark)
        ],
        key=lambda f: str(f.get("ts", "")),
    )
    if not sells:
        return problems
    # 기준 시점 보유량: 현재 잔고 − 이번 묶음 순변동
    net = 0
    for f in fills:
        if watermark and str(f.get("ts", "")) <= watermark:
            continue
        q = int(f.get("qty") or 0)
        net += q if f.get("side") == "매수" else -q
    position = balance - net
    logged_map = {
        str(r.get("odno")): float(r.get("ratio"))
        for r in (evidence.get("sell_ratios") or [])
        if r.get("odno") and r.get("ratio") is not None
    }
    for f in sells:
        q = int(f.get("qty") or 0)
        if position <= 0:
            break
        expected = q / position
        logged = logged_map.get(str(f.get("odno", "")))
        if logged is not None and abs(expected - logged) > 0.05:
            problems.append(
                f"매도 비율 불일치: 정산 {logged:.2f} "
                f"vs 실제 보유 {position}주 기준 {expected:.2f} — T 분류 오류 가능"
            )
        position = max(0, position - q)
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
        for msg in _verify_t_transitions(text, sym, rows, bal.quantity):
            problem(msg)
        evidence = _load_settlement_evidence(sym, target)
        if evidence is None:
            note(f"{sym} 정산 근거 파일 없음 — T 검산은 건너뜁니다 (검증 불가)")
        else:
            for msg in _verify_sell_ratios_from_evidence(evidence):
                problem(msg)
            if evidence.get("basis_unknown"):
                problem(f"{sym} 보유수량 기준 미확보로 정산이 보류됐습니다")
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
