"""
**연속 세션** 리플레이 — 2026-11-02 ~ 11-05 (TQQQ/SOXL, 메리츠 OHLC fixture).

기존 `test_dynamic_replay_1102_1105.py` 는 세션마다 독립된(상태를 이어가지 않는) 미리보기입니다.
이 파일은 **전일 가상 체결을 다음 날 상태에 반영**해 정책별 상태 분기를 비교합니다.

네트워크·state·주문을 일절 건드리지 않습니다.
- 브로커 API 호출 없음 (conftest 가 외부 HTTP/소켓을 차단)
- `state` 모듈 import 없음 → state 파일/orders_meta 미접촉
- 주문은 dict 로만 만들며 전부 `virtual=True` (전송 함수 호출 없음)
- 정책 우열(어느 정책이 더 좋은지)은 **판정하지 않습니다** — 임계값은 최적화되지 않은 후보값입니다.

실패 조건 (그 외는 기록만):
1. look-ahead 위반: 세션 d 의 판단에 d 시점(당일) 이후 데이터가 섞이는 경우
2. T 불변 위반: 추가매수 체결이 T 를 바꾸는 경우
3. 상태 정합성 위반: net_invested ≠ Σ체결금액, R ≠ seed−net_invested, 수량/평단 불일치
4. 가상/실제 분리 위반: 가상 주문에 state 필드가 섞이거나 전송/저장 호출이 생기는 경우
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

# strategy/config import 에는 시드 환경변수가 필요합니다 (config.py 모듈 로드 시 검증).
os.environ.setdefault("TQQQ_SEED", "40000")
os.environ.setdefault("SOXL_SEED", "20000")

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dynamic_levels import (  # noqa: E402
    FIXED_LEVEL_CHOICES,
    MIN_ATR_BARS,
    atr_ratio,
    build_virtual_additional_orders,
    compute_atr14,
    compute_budget_exact,
    decide_levels,
)
from strategy import adjust_price_to_tick, build_additional_loc_prices, cap_buy_loc_price  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "meritz_ohlc_20261106.json"

SESSIONS = ("20261102", "20261103", "20261104", "20261105")
POLICIES = ("A", "B", "C", "D", "E")

# ── 가정 (후보값·미확정, 최적화하지 않음) ──────────────────────────────
KAPPA_ADD = 0.02      # 정책 E 추가매수 전용 예산 비율 — 후보값·미확정
DELTA = 0.02          # 정책 D/E 히스테리시스 폭 — 후보값·미확정
NORMAL_T_FIRST = 1.0  # 사이클 첫 정상매수 체결의 ΔT — strategy 초기진입 1.0 단순화
NORMAL_T_LATER = 0.5  # 이후 정상매수 체결의 ΔT — strategy 전반전 0.5 단순화

ASSUMPTIONS = """
[연속 리플레이 가정 — 실제 운용 결과가 아님]
1) 신규진입 가정: 11/02 를 새 사이클 시작(T=0, qty=0, net_invested=0)으로 보고
   4세션을 **상태를 이어가며** 실행합니다 (전일 가상 체결이 다음 날 예산에 반영).
2) 시드/분할: TQQQ seed=40,000 USD · 40분할, SOXL seed=20,000 USD · 20분할.
   이는 가정값이며 실제 계좌 잔고가 아닙니다.
3) 판단 시점(d): d 이전 확정봉까지만 사용합니다 (당일봉 d 제외 — look-ahead 차단).
   last_price = 직전 확정봉 종가, ATR = 직전 15확정봉 Wilder 14기간(근사).
4) 예산: R = seed − net_invested, B = R (C 가정: 잔고 제약 없음, B=R),
   A = B / (splits − T). T ≥ splits 이면 추가매수 0단계(A=0).
5) 정상매수 1건: qty_n = max(floor(A / last_price), 0),
   price_n = 호가단위 보정 후 현재가×1.19 상한(cap_buy_loc_price) 적용.
   qty_n = 0 이면 정상매수 없음.
6) 추가매수: 기존 가격 공식(build_additional_loc_prices)으로 만든 1주 LOC 매수,
   ΔT = 0 (t_target=0.0). 정책이 단계 수만 결정합니다.
   - D 상한 히스테리시스 prev_level(D 직전 선택)과 E 직전 선택 prev_e 는 분리합니다.
7) 체결 가정: 해당 세션 종가 close_d ≤ 주문가이면 전량 체결 (LOC 매수).
   주문이 1주 단위이므로 0/1 이며 **메인 리플레이에는 부분체결 없음**
   (부분체결 반영 정확성은 별도 합성 테스트에서 검증).
8) ΔT 가정: 사이클 첫 정상매수 체결 1.0, 이후 정상매수 0.5 — strategy 전반전 규칙 단순화.
   (t_target: 초기진입 1.0 / 이후 0.5)
9) 매도·리버스 없음: 매수 국면만 가정합니다 (4세션 모두 신규 매수만).
10) 비용(수수료·슬리피지)과 배당은 반영하지 않습니다.
11) 정책 D/E 임계값(t1/t2)과 kappa_add 는 후보값이며 최적화·검증되지 않았습니다 (미확정).
    TQQQ t1=0.04/t2=0.07, SOXL t1=0.09/t2=0.14, kappa_add=0.02, delta=0.02.
12) E 예산은 2단 구조입니다.
    - 추가매수 전용: Σ추가매수(L) ≤ kappa_add × R (정상매수 제외)
    - 전체: normal + Σ추가매수(L) + 미차감 open_reserved ≤ B_avail (= R, 예약금 없음)
    부족하면 추가매수 단계만 0까지 줄입니다 (정상매수 불변).
"""

SYMBOL_SPECS = {
    "TQQQ": {"seed": 40000.0, "splits": 40, "t1": 0.04, "t2": 0.07},
    "SOXL": {"seed": 20000.0, "splits": 20, "t1": 0.09, "t2": 0.14},
}


# ── fixture / 봉 선택 ─────────────────────────────────────────────────


def load_fixture():
    """메리츠 OHLC fixture 로드 (키·토큰 없는 시장 데이터)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _assert_no_lookahead(bars, session):
    """세션 d 판단에 d 이후(당일 포함) 봉이 섞이면 실패."""
    for bar in bars:
        if str(bar["date"]) >= str(session):
            raise AssertionError(
                f"look-ahead 위반: 세션 {session} 판단에 {bar['date']} 봉이 포함되었습니다"
            )


def bars_before(bars, session):
    """세션 d 이전에 확정된 봉만 반환 (당일봉·이후봉 제외)."""
    confirmed = [bar for bar in bars if str(bar["date"]) < str(session)]
    _assert_no_lookahead(confirmed, session)
    return confirmed


def day_bar(bars, session):
    """세션 d 의 당일봉 (체결 매칭에만 사용 — 판단에는 쓰지 않음)."""
    for bar in bars:
        if str(bar["date"]) == str(session):
            return bar
    raise AssertionError(f"fixture 에 {session} 봉이 없습니다")


# ── 가상 상태 (실제 state 미접촉) ─────────────────────────────────────


def initial_state(spec):
    """종목·정책별 독립 가상 상태. 실제 `.state.json` 과 무관합니다."""
    return {
        "T": 0.0,
        "qty": 0,
        "avg": 0.0,
        "net_invested": 0.0,
        "seed": float(spec["seed"]),
        "splits": int(spec["splits"]),
    }


def _normal_t_target(T_before):
    """정상매수 ΔT: 사이클 첫 체결 1.0, 이후 0.5 (가정)."""
    return NORMAL_T_FIRST if float(T_before) == 0.0 else NORMAL_T_LATER


def _apply_fill(state, order, fill_price, before_T):
    """
    가상 체결 1건을 상태에 반영합니다.

    체결가 = 해당 세션 종가(close_d) — LOC 지정가 이하 체결 가정.
    추가매수는 T 를 바꾸지 않습니다 (assert).
    """
    qty_before = state["qty"]
    amount = float(order["quantity"]) * float(fill_price)

    if order["is_additional"]:
        assert state["T"] == before_T, "추가매수 체결이 T 를 변경했습니다"
    else:
        state["T"] = before_T + float(order["t_target"])

    state["qty"] = qty_before + int(order["quantity"])
    state["net_invested"] = float(state["net_invested"]) + amount
    # 가중평균: (기존 평단×기존수량 + 체결금액) / 체결 후 수량
    state["avg"] = (float(state["avg"]) * qty_before + amount) / state["qty"]
    return amount


# ── 리플레이 엔진 (읽기 전용, 결과는 반환 구조로 수집) ─────────────────


def _run_policy(symbol, spec, bars, policy, kappa_add, delta):
    """한 종목·한 정책의 4세션 연속 리플레이."""
    state = initial_state(spec)
    prev_d_level = None  # D 상한 히스테리시스용 (D 직전 선택)
    prev_e_level = None  # E 직전 선택 (D prev 와 분리)
    sessions = []
    d_level_path = []

    for session in SESSIONS:
        confirmed = bars_before(bars, session)
        last_price = float(confirmed[-1]["c"])
        atr = compute_atr14(confirmed[-MIN_ATR_BARS:])
        ratio = atr_ratio(atr, last_price)
        close = float(day_bar(bars, session)["c"])

        T_before = float(state["T"])
        qty_before = int(state["qty"])
        avg_before = float(state["avg"])
        net_before = float(state["net_invested"])
        R_before = float(state["seed"]) - net_before
        remaining_splits = int(spec["splits"]) - T_before

        # 예산: B = R (잔고 제약 없음), A = B / (splits − T)
        budget_B = R_before
        if remaining_splits > 0 and budget_B > 0:
            unit_amount = budget_B / remaining_splits
        else:
            unit_amount = 0.0

        # 정상매수: qty_n = floor(A / last_price), price_n = 상한 보정 tick(A / qty_n)
        base_qty = max(int(unit_amount // last_price), 0) if unit_amount > 0 else 0
        if base_qty > 0:
            raw_price = adjust_price_to_tick(unit_amount / base_qty)
            normal_price, normal_capped = cap_buy_loc_price(raw_price, last_price)
            normal_amount = float(base_qty) * float(normal_price)
            # E 는 0..D상한 사이 어느 단계도 고를 수 있으므로 0..8 전 단계 가격을 만듭니다
            # (가격 공식은 build_additional_loc_prices 단일 출처 — 여기서는 재사용만).
            prices_by_level = {
                level: [
                    float(price)
                    for _step, price in build_additional_loc_prices(
                        unit_amount, base_qty, level, normal_price
                    )
                ]
                for level in range(0, 9)
            }
        else:
            normal_price, normal_capped, normal_amount = None, False, 0.0
            prices_by_level = {level: [] for level in range(0, 9)}

        # 정책 D 상한(참고 기록) — E 는 내부에서 같은 D 를 다시 계산합니다.
        d_decision = decide_levels(
            "D",
            atr=atr,
            atr_ratio=ratio,
            t1=spec["t1"],
            t2=spec["t2"],
            prev_level=prev_d_level,
            delta=delta,
        )
        d_level = int(d_decision["levels"])
        d_level_path.append({"date": session, "levels": d_level, "ratio": ratio})

        decision = decide_levels(
            policy,
            atr=atr,
            atr_ratio=ratio,
            t1=spec["t1"],
            t2=spec["t2"],
            prev_level=prev_d_level,  # D 상한 히스테리시스 (E 선택값 아님)
            delta=delta,
            levels=8,  # 정책 C 전용 (7 또는 8) — 이 리플레이는 C=8
            remaining_seed=R_before,
            seed=spec["seed"],
            normal_amount=normal_amount,
            additional_prices_by_L=prices_by_level,
            kappa_add=kappa_add,
            budget_available=R_before,  # B_avail = R (잔고 제약 없음), 예약금 없음
            prev_e=prev_e_level,
        )
        levels = int(decision["levels"]) if base_qty > 0 else 0

        orders = []
        if base_qty > 0:
            orders.append(
                {
                    "side": "BUY",
                    "quantity": base_qty,
                    "price": float(normal_price),
                    "order_type": "LOC",
                    "t_target": _normal_t_target(T_before),
                    "is_additional": False,
                    "virtual": True,
                    "policy": policy,
                }
            )
            orders.extend(
                build_virtual_additional_orders(
                    unit_amount, base_qty, levels, normal_price, policy=policy
                )
            )

        fills = []
        fill_amount = 0.0
        for order in orders:
            if close > float(order["price"]):
                continue  # 지정가보다 종가가 높으면 미체결
            before_T = float(state["T"])
            amount = _apply_fill(state, order, close, before_T)
            fill_amount += amount
            fills.append(
                {
                    "kind": "additional" if order["is_additional"] else "normal",
                    "quantity": int(order["quantity"]),
                    "price": float(close),
                    "amount": amount,
                    "T_before": before_T,
                    "T_after": float(state["T"]),
                }
            )

        sessions.append(
            {
                "date": session,
                "confirmed_last_date": str(confirmed[-1]["date"]),
                "confirmed_count": len(confirmed),
                "last_price": last_price,
                "atr": atr,
                "ratio": ratio,
                "close": close,
                # 상태 스냅샷 (세션 시작)
                "T_before": T_before,
                "qty_before": qty_before,
                "avg_before": avg_before,
                "net_invested_before": net_before,
                "R_before": R_before,
                "remaining_splits": remaining_splits,
                # 예산/정상매수
                "budget_B": budget_B,
                "unit_amount": unit_amount,
                "base_qty": base_qty,
                "normal_price": normal_price,
                "normal_capped": normal_capped,
                "normal_amount": normal_amount,
                "normal_order_amount": (base_qty * float(normal_price)) if base_qty > 0 else 0.0,
                # 정책
                "levels": levels,
                "d_level": d_level,
                "reason": decision["reason"],
                "fallback": decision["fallback"],
                "additional_prices": [float(order["price"]) for order in orders if order["is_additional"]],
                "prices_by_level": prices_by_level,
                # 주문/체결
                "orders": orders,
                "fills": fills,
                "order_count": len(orders),
                "fill_count": len(fills),
                "fill_amount": fill_amount,
                # 상태 스냅샷 (세션 종료 = 다음 세션 시작)
                "T": float(state["T"]),
                "qty": int(state["qty"]),
                "avg": float(state["avg"]),
                "net_invested": float(state["net_invested"]),
                "R": float(state["seed"]) - float(state["net_invested"]),
                "exhaustion": (float(state["net_invested"]) / float(state["seed"]))
                if float(state["seed"]) > 0
                else None,
            }
        )

        prev_d_level = d_level
        if policy == "E":
            prev_e_level = int(decision["levels"])

    last = sessions[-1]
    totals = {
        "orders": sum(entry["order_count"] for entry in sessions),
        "fills": sum(entry["fill_count"] for entry in sessions),
        "fill_amount": sum(entry["fill_amount"] for entry in sessions),
        "normal_fills": sum(1 for entry in sessions for fill in entry["fills"] if fill["kind"] == "normal"),
        "additional_fills": sum(
            1 for entry in sessions for fill in entry["fills"] if fill["kind"] == "additional"
        ),
    }
    return {
        "sessions": sessions,
        "d_level_path": d_level_path,
        "final": {
            "T": last["T"],
            "qty": last["qty"],
            "avg": last["avg"],
            "net_invested": last["net_invested"],
            "R": last["R"],
            "exhaustion": last["exhaustion"],
            "seed": float(spec["seed"]),
            "splits": int(spec["splits"]),
        },
        "totals": totals,
    }


def run_replay(kappa_add=KAPPA_ADD, delta=DELTA):
    """
    연속 리플레이를 실행하고 **반환 구조**로 결과를 모읍니다 (테스트 내 print 없음).

    Returns:
        dict: {"assumptions", "sessions", "policies", "kappa_add", "delta", "symbols"}
    """
    data = load_fixture()
    symbols = {}
    for symbol, spec in SYMBOL_SPECS.items():
        bars = sorted(data[symbol], key=lambda bar: str(bar["date"]))
        symbols[symbol] = {
            "spec": dict(spec),
            "policies": {
                policy: _run_policy(symbol, spec, bars, policy, kappa_add, delta)
                for policy in POLICIES
            },
        }
    return {
        "assumptions": ASSUMPTIONS,
        "sessions": SESSIONS,
        "policies": POLICIES,
        "kappa_add": kappa_add,
        "delta": delta,
        "symbols": symbols,
    }


def format_summary(replay):
    """비교표(문자열 목록) — 우열 판정 없이 수치만 나열합니다."""
    lines = []
    for symbol, symbol_result in replay["symbols"].items():
        lines.append(f"[{symbol}] seed={symbol_result['spec']['seed']:.0f} splits={symbol_result['spec']['splits']}")
        for policy in replay["policies"]:
            result = symbol_result["policies"][policy]
            final = result["final"]
            totals = result["totals"]
            lines.append(
                f"  {policy}: T={final['T']:.2f} qty={final['qty']} avg={final['avg']:.4f} "
                f"net={final['net_invested']:.2f} R={final['R']:.2f} "
                f"소진율={final['exhaustion'] * 100:.2f}% "
                f"체결={totals['fills']}건/{totals['fill_amount']:.2f}"
            )
    return lines


@pytest.fixture(scope="module")
def replay():
    return run_replay()


# ── fixture 무결성 ────────────────────────────────────────────────────


def test_fixture_covers_replay_sessions():
    data = load_fixture()
    assert set(data) == {"TQQQ", "SOXL"}
    for symbol, bars in data.items():
        assert len(bars) == 40, f"{symbol} 봉 개수"
        dates = [str(bar["date"]) for bar in bars]
        assert dates == sorted(dates)
        for bar in bars:
            assert set(bar) == {"date", "o", "h", "l", "c"}
            assert bar["h"] >= bar["l"] > 0
        for session in SESSIONS:
            assert session in dates, f"{symbol} fixture 에 {session} 봉이 없습니다"


def test_bars_before_excludes_session_and_later_bars():
    data = load_fixture()
    for symbol, bars in data.items():
        for session in SESSIONS:
            confirmed = bars_before(bars, session)
            assert confirmed, symbol
            assert confirmed[-1]["date"] < session
            assert all(bar["date"] < session for bar in confirmed)
            assert len(confirmed) >= MIN_ATR_BARS


def test_lookahead_helper_rejects_today_bar():
    data = load_fixture()
    bars = data["TQQQ"]
    session = "20261104"
    tampered = [bar for bar in bars if bar["date"] <= session]  # 당일봉 포함
    with pytest.raises(AssertionError):
        _assert_no_lookahead(tampered, session)


# ── 가정 문서화 ───────────────────────────────────────────────────────


def test_assumptions_are_documented():
    for token in (
        "신규진입",
        "B=R",
        "잔고 제약 없음",
        "부분체결 없음",
        "매도·리버스 없음",
        "후보값",
        "미확정",
        "look-ahead",
        "초기진입 1.0",
        "이후 0.5",
    ):
        assert token in ASSUMPTIONS, f"가정 누락: {token}"


def test_candidate_thresholds_are_marked_as_unconfirmed():
    """D/E 임계값은 후보값 — 코드에도 미확정 표기가 있어야 합니다."""
    source = Path(__file__).read_text(encoding="utf-8")
    for token in ("후보값", "미확정", "KAPPA_ADD = 0.02", "DELTA = 0.02"):
        assert token in source


# ── 상태 초기값 / 독립성 ──────────────────────────────────────────────


def test_state_starts_empty_per_symbol_and_policy(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            first = result["sessions"][0]
            assert first["T_before"] == 0.0, symbol + "/" + policy
            assert first["qty_before"] == 0
            assert first["avg_before"] == 0.0
            assert first["net_invested_before"] == 0.0
            assert first["R_before"] == symbol_result["spec"]["seed"]
            assert first["T"] >= 0.0


def test_states_do_not_share_objects(replay):
    """정책별 상태가 같은 dict/리스트를 공유하면 분기가 오염됩니다."""
    for symbol, symbol_result in replay["symbols"].items():
        policies = list(symbol_result["policies"])
        for index, policy in enumerate(policies):
            sessions = symbol_result["policies"][policy]["sessions"]
            for other in policies[index + 1 :]:
                other_sessions = symbol_result["policies"][other]["sessions"]
                assert sessions is not other_sessions
                for left, right in zip(sessions, other_sessions):
                    assert left is not right


# ── look-ahead 없음 ───────────────────────────────────────────────────


def test_no_lookahead_in_every_session_entry(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                assert entry["confirmed_last_date"] < entry["date"], (
                    f"look-ahead: {symbol}/{policy} 세션 {entry['date']} 에 "
                    f"{entry['confirmed_last_date']} 봉 사용"
                )
                assert entry["confirmed_count"] >= MIN_ATR_BARS


# ── 정책별 단계 수 ────────────────────────────────────────────────────


def test_policy_levels_stay_within_definitions(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                levels = entry["levels"]
                if policy == "A":
                    assert levels == 3
                elif policy == "B":
                    assert levels == 5
                elif policy == "C":
                    assert levels == 8
                elif policy == "D":
                    assert levels in (3, 5, 7)
                elif policy == "E":
                    # 새 예산식은 0~D상한(≤7) 사이 값을 냅니다 (0 = 추가매수 없음).
                    assert 0 <= levels <= entry["d_level"], (
                        "E 는 D 상한을 늘릴 수 없습니다"
                    )
                    assert levels <= 7
                else:  # pragma: no cover - 방어
                    raise AssertionError(f"알 수 없는 정책 {policy}")


def test_policy_e_respects_two_budgets(replay):
    """추가매수 전용 예산(κ_add·R)과 전체 예산(B_avail=R)을 각각 확인합니다."""
    kappa_add = replay["kappa_add"]
    eps = 1e-9
    for symbol, symbol_result in replay["symbols"].items():
        for entry in symbol_result["policies"]["E"]["sessions"]:
            levels = entry["levels"]
            if entry["base_qty"] == 0:
                assert levels == 0
                continue
            additional = compute_budget_exact(entry["additional_prices"])
            assert additional <= kappa_add * entry["R_before"] + eps
            assert entry["normal_amount"] + additional <= entry["budget_B"] + eps
            # 선택 단계보다 큰 후보는 두 예산 중 하나를 넘어야 합니다 (최대 선택).
            for level in range(levels + 1, entry["d_level"] + 1):
                bigger = compute_budget_exact(entry["prices_by_level"].get(level) or [])
                kappa_exceeded = bigger > kappa_add * entry["R_before"] + eps
                total_exceeded = entry["normal_amount"] + bigger > entry["budget_B"] + eps
                assert kappa_exceeded or total_exceeded, (
                    f"{symbol} L={levels} 보다 큰 L={level} 이 두 예산 안에 들어갑니다"
                )


# ── T 불변 (추가매수) ─────────────────────────────────────────────────


def test_additional_fills_never_change_T(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                for fill in entry["fills"]:
                    if fill["kind"] == "additional":
                        assert fill["T_before"] == fill["T_after"], (
                            f"{symbol}/{policy} 추가매수 체결이 T 를 변경: "
                            f"{fill['T_before']} → {fill['T_after']}"
                        )


def test_policy_e_levels_are_within_zero_to_seven_and_budget_binds(replay):
    """새 예산식으로 E 가 0~7 을 내고, 실제로 예산이 단계를 깎는 세션이 있어야 합니다."""
    seen = []
    budget_bound = False
    for symbol, symbol_result in replay["symbols"].items():
        for entry in symbol_result["policies"]["E"]["sessions"]:
            assert 0 <= entry["levels"] <= 7
            assert entry["reason"].startswith("budget: L=")
            seen.append((symbol, entry["date"], entry["levels"]))
            if entry["levels"] < entry["d_level"]:
                budget_bound = True
    assert seen, "E 세션 기록이 없습니다"
    assert budget_bound, "예산이 단계를 깎은 세션이 없습니다 (테스트 전제 확인 필요)"


def test_policy_a_b_c_d_results_unchanged_from_baseline(replay):
    """D·ABC 로직은 변경하지 않았습니다 — 기준선(수정 전 리플레이)과 동일해야 합니다."""
    expected_level_paths = {
        "TQQQ": {"A": [3, 3, 3, 3], "B": [5, 5, 5, 5], "C": [8] * 4, "D": [5, 7, 7, 7]},
        "SOXL": {"A": [3, 3, 3, 3], "B": [5, 5, 5, 5], "C": [8] * 4, "D": [7, 7, 7, 7]},
    }
    expected_final_net = {
        "TQQQ": {"A": 2828.53, "B": 2879.00, "C": 2879.00, "D": 2879.00},
        "SOXL": {"A": 2649.37, "B": 2731.11, "C": 2853.72, "D": 2812.85},
    }
    for symbol, symbol_result in replay["symbols"].items():
        for policy in ("A", "B", "C", "D"):
            entries = symbol_result["policies"][policy]["sessions"]
            assert [entry["levels"] for entry in entries] == expected_level_paths[symbol][policy]
            assert symbol_result["policies"][policy]["final"]["net_invested"] == pytest.approx(
                expected_final_net[symbol][policy], abs=0.01
            ), f"{symbol}/{policy} 기준선 불일치"


def test_T_equals_sum_of_normal_fill_targets(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            expected = sum(
                NORMAL_T_FIRST if fill["T_before"] == 0.0 else NORMAL_T_LATER
                for entry in result["sessions"]
                for fill in entry["fills"]
                if fill["kind"] == "normal"
            )
            assert result["final"]["T"] == pytest.approx(expected), symbol + "/" + policy


def test_normal_fill_target_schedule(replay):
    """첫 정상매수는 1.0, 이후는 0.5 (가정 — strategy 전반전 규칙 단순화)."""
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            seen_normal = False
            for entry in result["sessions"]:
                for fill in entry["fills"]:
                    if fill["kind"] != "normal":
                        continue
                    delta = fill["T_after"] - fill["T_before"]
                    assert delta == pytest.approx(NORMAL_T_FIRST if not seen_normal else NORMAL_T_LATER)
                    seen_normal = True


def test_T_is_monotonic_and_within_splits(replay):
    for symbol, symbol_result in replay["symbols"].items():
        splits = symbol_result["spec"]["splits"]
        for policy, result in symbol_result["policies"].items():
            previous = 0.0
            for entry in result["sessions"]:
                assert entry["T"] >= previous
                assert entry["T"] <= splits
                previous = entry["T"]


# ── 가상 주문 형태 ────────────────────────────────────────────────────


def test_additional_orders_are_single_share_loc_and_virtual(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                additional = [order for order in entry["orders"] if order["is_additional"]]
                assert len(additional) == entry["levels"]
                for order in additional:
                    assert order["t_target"] == 0.0
                    assert order["quantity"] == 1
                    assert order["side"] == "BUY"
                    assert order["order_type"] == "LOC"
                    assert order["virtual"] is True
                    assert order["policy"] == policy


def test_order_count_matches_levels(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                expected = 0 if entry["base_qty"] == 0 else entry["levels"] + 1
                assert entry["order_count"] == expected
                assert entry["fill_count"] <= entry["order_count"]


def test_no_sell_or_reverse_orders(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                for order in entry["orders"]:
                    assert order["side"] == "BUY"
                    assert order["order_type"] == "LOC"


def test_all_orders_are_virtual_and_carry_no_state_fields(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                for order in entry["orders"]:
                    assert order["virtual"] is True
                    assert "orders_meta" not in order
                    assert "odno" not in order
                    assert "state" not in order


# ── 예산 / 상태 정합성 (하네스 검산) ──────────────────────────────────


def test_budget_and_unit_follow_state(replay):
    for symbol, symbol_result in replay["symbols"].items():
        splits = symbol_result["spec"]["splits"]
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                assert entry["budget_B"] == pytest.approx(entry["R_before"])
                assert entry["remaining_splits"] == pytest.approx(splits - entry["T_before"])
                if entry["remaining_splits"] > 0 and entry["budget_B"] > 0:
                    assert entry["unit_amount"] == pytest.approx(
                        entry["budget_B"] / entry["remaining_splits"]
                    )
                else:
                    assert entry["unit_amount"] == 0.0
                    assert entry["levels"] == 0


def test_net_invested_equals_sum_of_fill_amounts(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            expected = sum(
                fill["amount"] for entry in result["sessions"] for fill in entry["fills"]
            )
            assert result["final"]["net_invested"] == pytest.approx(expected)
            assert result["final"]["net_invested"] == pytest.approx(result["totals"]["fill_amount"])
            assert result["totals"]["fill_amount"] == pytest.approx(
                sum(entry["fill_amount"] for entry in result["sessions"])
            )


def test_remaining_seed_is_seed_minus_net_invested(replay):
    for symbol, symbol_result in replay["symbols"].items():
        seed = symbol_result["spec"]["seed"]
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                assert entry["R"] == pytest.approx(seed - entry["net_invested"])
            assert result["final"]["R"] == pytest.approx(
                seed - result["final"]["net_invested"]
            )


def test_quantity_equals_sum_of_fill_quantities(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            expected = sum(
                fill["quantity"] for entry in result["sessions"] for fill in entry["fills"]
            )
            assert result["final"]["qty"] == expected


def test_average_price_is_weighted_average_of_fills(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            qty = 0
            total = 0.0
            for entry in result["sessions"]:
                for fill in entry["fills"]:
                    qty += fill["quantity"]
                    total += fill["amount"]
            if qty == 0:
                assert result["final"]["avg"] == 0.0
            else:
                assert result["final"]["avg"] == pytest.approx(total / qty)


def test_exhaustion_ratio_matches_net_invested(replay):
    for symbol, symbol_result in replay["symbols"].items():
        seed = symbol_result["spec"]["seed"]
        for policy, result in symbol_result["policies"].items():
            final = result["final"]
            assert final["exhaustion"] == pytest.approx(final["net_invested"] / seed)


def test_fill_only_when_close_at_or_below_limit_price(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                filled_prices = {fill["price"] for fill in entry["fills"]}
                assert filled_prices <= {entry["close"]}
                for order in entry["orders"]:
                    if entry["close"] <= order["price"]:
                        matching = [
                            fill
                            for fill in entry["fills"]
                            if fill["quantity"] == order["quantity"]
                            and fill["kind"]
                            == ("additional" if order["is_additional"] else "normal")
                        ]
                        assert matching, "체결 조건을 만족한 주문의 체결 기록이 없습니다"


# ── 부분체결 반영 정확성 + processed_filled 멱등 (합성) ────────────────
# 1주 주문은 부분체결이 0/1 뿐이므로, "일부 단계만 체결(깊은 단계 미체결)" 상황으로
# 상태 반영 정확성과 델타 1회 반영(멱등)을 검증합니다. 네트워크·state 미접촉.


def _apply_filled_delta(state, odno, filled_qty, filled_amount):
    """
    주문 1건의 누적 체결 스냅샷을 state 에 반영합니다.

    실제 정산 규칙과 같은 델타 방식: 이미 반영한 수량·금액을 빼고 **새로 늘어난 부분만**
    더합니다. 같은 스냅샷을 두 번 넘겨도 두 번째는 델타 0 입니다 (멱등).
    """
    meta = state["orders_meta"].setdefault(
        odno, {"processed_filled_qty": 0, "processed_filled_amount": 0.0}
    )
    delta_qty = max(int(filled_qty) - int(meta["processed_filled_qty"]), 0)
    delta_amount = max(
        float(filled_amount) - float(meta["processed_filled_amount"]), 0.0
    )
    meta["processed_filled_qty"] += delta_qty
    meta["processed_filled_amount"] += delta_amount
    state["qty"] += delta_qty
    state["net_invested"] += delta_amount
    return delta_qty, delta_amount


def test_partial_level_fills_apply_once_and_processed_fill_is_idempotent():
    orders = build_virtual_additional_orders(1000.0, 16, 7, 101.0, policy="E")
    prices = [order["price"] for order in orders]
    assert len(prices) == 7
    assert all(earlier > later for earlier, later in zip(prices, prices[1:]))

    # 4단계까지만 체결(가격이 높은 앞단계), 깊은 3단계는 미체결.
    close = prices[3]
    filled = [
        (f"sim-{index}", order["quantity"], close)
        for index, order in enumerate(orders, start=1)
        if close <= order["price"]
    ]
    assert len(filled) == 4
    assert all(quantity == 1 for _odno, quantity, _price in filled)

    state = {
        "T": 3.0,
        "qty": 10,
        "net_invested": 550.0,
        "orders_meta": {},
    }
    first_pass = [_apply_filled_delta(state, odno, qty, price) for odno, qty, price in filled]
    assert all(delta == (1, close) for delta in first_pass)
    assert state["qty"] == 10 + 4
    assert state["net_invested"] == pytest.approx(550.0 + 4 * close)
    assert state["T"] == 3.0, "추가매수 체결이 T 를 변경했습니다"

    # 같은 체결 스냅샷을 다시 적용해도 델타는 0 (processed_filled 멱등).
    second_pass = [_apply_filled_delta(state, odno, qty, price) for odno, qty, price in filled]
    assert all(delta == (0, 0.0) for delta in second_pass)
    assert state["qty"] == 14
    assert state["net_invested"] == pytest.approx(550.0 + 4 * close)

    # 깊은 단계 1건이 나중에 체결로 늘어나도 그 1건만 반영됩니다.
    late_odno = f"sim-{len(orders)}"
    late_qty = orders[-1]["quantity"]
    late_price = prices[-1]  # 마지막(가장 깊은) 단계의 지정가로 체결
    assert _apply_filled_delta(state, late_odno, late_qty, late_price) == (1, late_price)
    assert state["qty"] == 15
    assert state["net_invested"] == pytest.approx(550.0 + 4 * close + late_price)
    assert _apply_filled_delta(state, late_odno, late_qty, late_price) == (0, 0.0)
    assert state["T"] == 3.0


# ── 비교 결과 (우열 판정 없음) ────────────────────────────────────────


def test_comparison_covers_all_policies_and_symbols(replay):
    assert set(replay["symbols"]) == set(SYMBOL_SPECS)
    for symbol, symbol_result in replay["symbols"].items():
        assert list(symbol_result["policies"]) == list(POLICIES)
        for policy, result in symbol_result["policies"].items():
            assert [entry["date"] for entry in result["sessions"]] == list(SESSIONS)
            final = result["final"]
            for key in ("T", "qty", "avg", "net_invested", "R", "exhaustion", "seed", "splits"):
                assert key in final
            for key in ("orders", "fills", "fill_amount", "normal_fills", "additional_fills"):
                assert key in result["totals"]


def test_summary_lists_every_policy_without_ranking(replay):
    lines = format_summary(replay)
    text = "\n".join(lines)
    for symbol in SYMBOL_SPECS:
        assert f"[{symbol}]" in text
    for policy in POLICIES:
        assert f"  {policy}: T=" in text
    assert "최적" not in text and "우열" not in text


# ── state / 브로커 미접촉 정적 가드 ───────────────────────────────────

_ALLOWED_STRATEGY_IMPORTS = {
    "adjust_price_to_tick",
    "build_additional_loc_prices",
    "cap_buy_loc_price",
}
_FORBIDDEN_IMPORT_TOKENS = ("state", "broker", "requests", "trading_bot", "config")


def test_module_does_not_import_state_or_broker():
    source = Path(__file__).read_text(encoding="utf-8")
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if re.match(r"^(import|from)\s+\w", line.strip())
    ]
    for line in import_lines:
        for token in _FORBIDDEN_IMPORT_TOKENS:
            assert not re.search(rf"\b{token}\b", line), f"금지 import: {line}"
    # 가격/호가 공식만 strategy 에서 가져옵니다 (단일 출처).
    strategy_lines = [line for line in import_lines if line.startswith("from strategy import")]
    assert strategy_lines, "strategy 가격 공식 import 가 없습니다"
    names = {
        name.split("#", 1)[0].strip()
        for line in strategy_lines
        for name in line.split("import", 1)[1].split(",")
        if name.split("#", 1)[0].strip()
    }
    assert names <= _ALLOWED_STRATEGY_IMPORTS, f"허용되지 않은 strategy import: {names}"


def test_module_has_no_write_calls():
    source = Path(__file__).read_text(encoding="utf-8")
    # 토큰을 문자열 결합으로 만들어, 이 가드 자신의 소스가 검사에 걸리지 않게 합니다.
    forbidden = (
        "save" + "_state",
        "save" + "_states",
        "place" + "_order",
        "cancel" + "_order",
        "open" + "(",
        "write" + "_text(",
    )
    for token in forbidden:
        assert token not in source, f"쓰기/주문 호출 금지 토큰 발견: {token}"
