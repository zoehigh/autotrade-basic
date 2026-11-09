"""
fixture 기반 **읽기 전용** 리플레이 — 2026-11-02 ~ 11-05 (TQQQ/SOXL, 메리츠 OHLC 40거래일).

이 테스트는 네트워크·state·주문을 일절 건드리지 않습니다.
- 브로커 API 호출 없음 (conftest 가 외부 HTTP 를 차단)
- `state` 모듈 import 없음 → state 파일/orders_meta 미접촉
- 주문은 dict 로만 만들어지며 전부 `virtual=True` (전송 함수 호출 없음)

실패 조건 (그 외는 기록만):
1. look-ahead 위반: 세션 t 의 판단에 t 시점 봉(당일봉) 이후 데이터가 섞이는 경우
2. T 불변 위반: 추가매수가 T 를 바꾸는 경우 (정책 간 T 경로가 달라지는 경우)
3. ΔT 위반: 가상 추가매수 주문의 t_target != 0.0 또는 수량 != 1
   (+ 하네스 자체 정합성: 주문 수/체결 합계 계산이 맞는지)

정책 우열(어느 정책이 더 좋은지)은 **판정하지 않습니다** — 임계값은 최적화되지 않은 후보값입니다.
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
    select_dynamic_d,
)
from strategy import adjust_price_to_tick, build_additional_loc_prices  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "meritz_ohlc_20261106.json"

SESSIONS = ("20261102", "20261103", "20261104", "20261105")
POLICIES = ("A", "B", "C", "D", "E")

# ── 가정 (후보값·미확정, 최적화하지 않음) ──────────────────────────────
KAPPA_ADD = 0.02      # 정책 E 추가매수 전용 예산 비율 — 후보값·미확정
DELTA = 0.02          # 정책 D/E 히스테리시스 폭 — 후보값·미확정
KAPPA_SENSITIVITY = (0.02, 0.10, 0.20)  # 민감도 확인용 후보값 (우열 판정 아님)

ASSUMPTIONS = """
[리플레이 가정 — 실제 운용 결과가 아님]
1) 신규진입 가정: 11/02 를 새 사이클 시작(T=0)으로 보고 4세션을 연속 실행합니다.
2) 시드/분할/unit: TQQQ seed=40,000 USD · 40분할, SOXL seed=20,000 USD · 20분할
   → unit_amount = seed/분할 = 1,000 USD (양쪽 동일).
3) 판단 시점(t): t-1 확정봉까지만 사용합니다 (당일봉 t 제외 — look-ahead 차단).
   last_price = 직전 확정봉 종가, ATR = 직전 15확정봉 Wilder 14기간.
4) 정상매수: 당일 LOC 매수 1건, 가격 = 직전 확정종가(호가단위 보정), 수량 = floor(unit/가격).
5) 추가매수: 기존 가격 공식(build_additional_loc_prices)으로 만든 1주 LOC 매수,
   ΔT = 0 (t_target=0.0). 정책이 단계 수만 결정합니다.
6) 체결 가정: 당일 종가로 매칭합니다 (LOC 매수 → 종가 <= 주문가일 때 종가 체결).
7) 비용(수수료·슬리피지)과 배당은 반영하지 않습니다.
8) 정책 D/E 임계값(t1/t2)과 kappa_add 는 후보값이며 최적화·검증되지 않았습니다.
   TQQQ t1=0.04/t2=0.07, SOXL t1=0.09/t2=0.14, kappa_add=0.02, delta=0.02.
9) E 예산은 2단 구조입니다 — 추가매수 전용(Σ추가 ≤ kappa_add×R, 정상매수 제외)과
   전체(normal + Σ추가 + 미차감 예약금 ≤ B_avail=R). 부족하면 추가매수 단계만
   0까지 줄이고 정상매수는 건드리지 않습니다.
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
    """세션 t 판단에 t 이후(당일 포함) 봉이 섞이면 실패."""
    for bar in bars:
        if str(bar["date"]) >= str(session):
            raise AssertionError(
                f"look-ahead 위반: 세션 {session} 판단에 {bar['date']} 봉이 포함되었습니다"
            )


def bars_before(bars, session):
    """세션 t 이전에 확정된 봉만 반환 (당일봉·이후봉 제외)."""
    confirmed = [bar for bar in bars if str(bar["date"]) < str(session)]
    _assert_no_lookahead(confirmed, session)
    return confirmed


def day_bar(bars, session):
    """세션 t 의 당일봉 (체결 매칭에만 사용 — 판단에는 쓰지 않음)."""
    for bar in bars:
        if str(bar["date"]) == str(session):
            return bar
    raise AssertionError(f"fixture 에 {session} 봉이 없습니다")


# ── 리플레이 실행 (읽기 전용, 결과는 반환 구조로 수집) ─────────────────


def _session_inputs(spec, confirmed):
    last_price = float(confirmed[-1]["c"])
    atr = compute_atr14(confirmed[-MIN_ATR_BARS:])
    ratio = atr_ratio(atr, last_price)
    unit_amount = spec["seed"] / spec["splits"]
    normal_price = adjust_price_to_tick(last_price)
    base_qty = int(unit_amount // normal_price)
    if base_qty <= 0:
        raise AssertionError(f"기준 수량이 0 입니다 (unit={unit_amount}, price={normal_price})")
    normal_amount = base_qty * normal_price
    # E 는 0..D상한 사이 어느 단계도 고를 수 있으므로 0..8 전 단계 가격을 만듭니다
    # (가격 공식은 build_additional_loc_prices 단일 출처 — 여기서는 재사용만).
    prices_by_level = {
        level: [price for _step, price in build_additional_loc_prices(unit_amount, base_qty, level, normal_price)]
        for level in range(0, 9)
    }
    return {
        "last_price": last_price,
        "atr": atr,
        "ratio": ratio,
        "unit_amount": unit_amount,
        "normal_price": normal_price,
        "base_qty": base_qty,
        "normal_amount": normal_amount,
        "prices_by_level": prices_by_level,
    }


def _normal_order(inputs, policy):
    """정상매수 1건 (가상). ΔT = 1.0 — 추가매수(0.0)와 구분합니다."""
    return {
        "side": "BUY",
        "quantity": inputs["base_qty"],
        "price": inputs["normal_price"],
        "order_type": "LOC",
        "t_target": 1.0,
        "is_additional": False,
        "virtual": True,
        "policy": policy,
    }


def _run_symbol(symbol, spec, bars, kappa_add, delta):
    policies = {
        policy: {"spent": 0.0, "T": 0.0, "sessions": [], "prev_e": None}
        for policy in POLICIES
    }
    d_level_path = []
    prev_d_level = None

    for session in SESSIONS:
        confirmed = bars_before(bars, session)
        inputs = _session_inputs(spec, confirmed)
        close = float(day_bar(bars, session)["c"])

        d_level, _d_reason = select_dynamic_d(
            inputs["ratio"], spec["t1"], spec["t2"], prev_level=prev_d_level, delta=delta
        )
        d_level_path.append({"date": session, "levels": d_level, "ratio": inputs["ratio"]})

        for policy in POLICIES:
            state = policies[policy]
            remaining_before = spec["seed"] - state["spent"]
            decision = decide_levels(
                policy,
                atr=inputs["atr"],
                atr_ratio=inputs["ratio"],
                t1=spec["t1"],
                t2=spec["t2"],
                prev_level=prev_d_level,
                delta=delta,
                levels=8,  # 정책 C 전용 (7 또는 8)
                remaining_seed=remaining_before,
                seed=spec["seed"],
                normal_amount=inputs["normal_amount"],
                additional_prices_by_L=inputs["prices_by_level"],
                kappa_add=kappa_add,
                budget_available=remaining_before,  # B_avail = R (예약금 없음)
                prev_e=state["prev_e"],
            )
            levels = decision["levels"]
            state["prev_e"] = levels  # E 직전 선택값 (다음 세션 prev_e)
            orders = [_normal_order(inputs, policy)] + build_virtual_additional_orders(
                inputs["unit_amount"], inputs["base_qty"], levels, inputs["normal_price"], policy=policy
            )

            for order in orders:
                assert order["virtual"] is True, "가상 주문이 아닌 dict 가 섞였습니다"
                if order["is_additional"]:
                    assert order["t_target"] == 0.0, "추가매수 ΔT 위반"
                    assert order["quantity"] == 1
                assert "orders_meta" not in order and "odno" not in order

            filled = [order for order in orders if close <= order["price"]]
            fill_amount = sum(order["quantity"] * close for order in filled)
            delta_t = sum(order["t_target"] for order in filled)

            state["spent"] += fill_amount
            state["T"] += delta_t
            prices = inputs["prices_by_level"].get(levels, [])
            state["sessions"].append(
                {
                    "date": session,
                    "policy": policy,
                    "levels": levels,
                    "reason": decision["reason"],
                    "fallback": decision["fallback"],
                    "ratio": inputs["ratio"],
                    "atr": inputs["atr"],
                    "last_price": inputs["last_price"],
                    "normal_price": inputs["normal_price"],
                    "base_qty": inputs["base_qty"],
                    "normal_amount": inputs["normal_amount"],
                    "remaining_seed_before": remaining_before,
                    "additional_prices": list(prices),
                    "prices_by_level": inputs["prices_by_level"],
                    "close": close,
                    "capital_use_ratio": (
                        (inputs["normal_amount"] + compute_budget_exact(prices)) / remaining_before
                        if remaining_before > 0
                        else None
                    ),
                    "d_level": d_level,
                    "order_count": len(orders),
                    "fill_count": len(filled),
                    "fill_amount": fill_amount,
                    "remaining_seed": spec["seed"] - state["spent"],
                    "exhaustion": state["spent"] / spec["seed"],
                    "T": state["T"],
                    "orders": orders,
                    "filled_orders": filled,
                }
            )

        prev_d_level = d_level

    result = {}
    for policy, state in policies.items():
        entries = state["sessions"]
        total_amount = sum(entry["fill_amount"] for entry in entries)
        total_fills = sum(entry["fill_count"] for entry in entries)
        result[policy] = {
            "sessions": entries,
            "T_path": [entry["T"] for entry in entries],
            "totals": {
                "orders": sum(entry["order_count"] for entry in entries),
                "fills": total_fills,
                "fill_amount": total_amount,
                "avg_price": (total_amount / total_fills) if total_fills else None,
                "remaining_seed": entries[-1]["remaining_seed"],
                "exhaustion": entries[-1]["exhaustion"],
            },
        }
    return {"spec": dict(spec), "policies": result, "d_level_path": d_level_path}


def run_replay(kappa_add=KAPPA_ADD, delta=DELTA):
    """
    리플레이를 실행하고 **반환 구조**로 결과를 모읍니다 (테스트 내 print 없음).

    Returns:
        dict: {"assumptions", "sessions", "policies", "kappa_add", "delta", "symbols"}
    """
    data = load_fixture()
    symbols = {}
    for symbol, spec in SYMBOL_SPECS.items():
        bars = sorted(data[symbol], key=lambda bar: str(bar["date"]))
        symbols[symbol] = _run_symbol(symbol, spec, bars, kappa_add, delta)
    return {
        "assumptions": ASSUMPTIONS,
        "sessions": SESSIONS,
        "policies": POLICIES,
        "kappa_add": kappa_add,
        "delta": delta,
        "symbols": symbols,
    }


@pytest.fixture(scope="module")
def replay():
    return run_replay()


# ── fixture 무결성 ────────────────────────────────────────────────────


def test_fixture_has_sessions_and_ohlc_columns():
    data = load_fixture()
    assert set(data) == {"TQQQ", "SOXL"}
    for symbol, bars in data.items():
        assert len(bars) == 40, f"{symbol} 봉 개수"
        for bar in bars:
            assert set(bar) == {"date", "o", "h", "l", "c"}
            assert bar["h"] >= bar["l"] > 0
        dates = [str(bar["date"]) for bar in bars]
        assert dates == sorted(dates)
        assert all(bar["date"] < "20261107" for bar in bars)


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
    assert "신규진입" in ASSUMPTIONS
    assert "unit_amount" in ASSUMPTIONS
    assert "종가" in ASSUMPTIONS
    assert "후보값" in ASSUMPTIONS
    assert "look-ahead" in ASSUMPTIONS


def test_candidate_thresholds_are_marked_as_unconfirmed():
    """D/E 임계값은 후보값 — 코드에도 미확정 표기가 있어야 합니다."""
    source = Path(__file__).read_text(encoding="utf-8")
    for token in ("후보값", "미확정", "KAPPA_ADD = 0.02", "DELTA = 0.02"):
        assert token in source


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


# ── T 불변 (정책 무관) ────────────────────────────────────────────────


def test_T_path_is_identical_across_policies(replay):
    """추가매수는 ΔT=0 이므로 T 경로가 정책에 따라 달라지면 안 됩니다."""
    for symbol, symbol_result in replay["symbols"].items():
        paths = {policy: result["T_path"] for policy, result in symbol_result["policies"].items()}
        baseline = paths["A"]
        for policy, path in paths.items():
            assert path == baseline, f"{symbol}/{policy} T 경로 불일치: {path} vs {baseline}"
        # T 는 단조 비감소 (정상매수 체결 시 +1)
        for previous, current in zip(baseline, baseline[1:]):
            assert current >= previous


def test_T_path_counts_only_normal_fills(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            normal_fills = sum(
                1 for entry in result["sessions"] if entry["filled_orders"]
                and any(not order["is_additional"] for order in entry["filled_orders"])
            )
            assert result["T_path"][-1] == pytest.approx(float(normal_fills)), symbol + "/" + policy


# ── ΔT 위반 / 가상 주문 ───────────────────────────────────────────────


def test_additional_orders_are_always_delta_T_zero(replay):
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


def test_all_orders_are_virtual_and_carry_no_state_fields(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                for order in entry["orders"]:
                    assert order["virtual"] is True
                    assert order["policy"] == policy
                    assert "orders_meta" not in order
                    assert "odno" not in order
                    assert "state" not in order


# ── 지표 정합성 (하네스 검산, 우열 판정 아님) ─────────────────────────


def test_session_metrics_are_internally_consistent(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            for entry in result["sessions"]:
                assert entry["order_count"] == entry["levels"] + 1
                assert entry["fill_count"] <= entry["order_count"]
                expected_amount = sum(
                    order["quantity"] * entry["close"] for order in entry["filled_orders"]
                )
                assert entry["fill_amount"] == pytest.approx(expected_amount)
                assert entry["fill_count"] == len(entry["filled_orders"])
                assert entry["remaining_seed"] >= -1e-9
                assert 0 <= entry["exhaustion"] <= 1 + 1e-9
                assert entry["T"] == pytest.approx(
                    sum(
                        order["t_target"]
                        for previous in result["sessions"]
                        for order in (previous["filled_orders"] if previous["date"] <= entry["date"] else [])
                    )
                )


def test_totals_match_session_sums(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            sessions = result["sessions"]
            assert result["totals"]["orders"] == sum(entry["order_count"] for entry in sessions)
            assert result["totals"]["fills"] == sum(entry["fill_count"] for entry in sessions)
            assert result["totals"]["fill_amount"] == pytest.approx(
                sum(entry["fill_amount"] for entry in sessions)
            )
            if result["totals"]["fills"]:
                assert result["totals"]["avg_price"] == pytest.approx(
                    result["totals"]["fill_amount"] / result["totals"]["fills"]
                )
            assert result["totals"]["remaining_seed"] == pytest.approx(
                symbol_result["spec"]["seed"] - result["totals"]["fill_amount"]
            )


def test_policy_c_orders_are_prefix_extension_of_policy_a():
    """8단계 가격은 3단계 가격의 앞부분을 그대로 포함합니다 (가격 공식 prefix 성질)."""
    data = load_fixture()
    for symbol, bars in data.items():
        spec = SYMBOL_SPECS[symbol]
        for session in SESSIONS:
            inputs = _session_inputs(spec, bars_before(bars, session))
            three = inputs["prices_by_level"][3]
            eight = inputs["prices_by_level"][8]
            assert eight[: len(three)] == three


# ── 정책 E 시드 사용률 ────────────────────────────────────────────────


def test_policy_e_respects_two_budgets(replay):
    """추가매수 전용 예산(κ_add·R)과 전체 예산(B_avail=R)을 각각 확인합니다."""
    kappa_add = replay["kappa_add"]
    eps = 1e-9
    for symbol, symbol_result in replay["symbols"].items():
        for entry in symbol_result["policies"]["E"]["sessions"]:
            levels = entry["levels"]
            additional = compute_budget_exact(entry["additional_prices"])
            assert additional <= kappa_add * entry["remaining_seed_before"] + eps
            assert entry["normal_amount"] + additional <= entry["remaining_seed_before"] + eps
            # 선택 단계보다 큰 후보는 두 예산 중 하나를 넘어야 합니다 (최대 선택).
            for level in range(levels + 1, entry["d_level"] + 1):
                bigger = compute_budget_exact(entry["prices_by_level"].get(level) or [])
                kappa_exceeded = (
                    bigger > kappa_add * entry["remaining_seed_before"] + eps
                )
                total_exceeded = (
                    entry["normal_amount"] + bigger
                    > entry["remaining_seed_before"] + eps
                )
                assert kappa_exceeded or total_exceeded, (
                    f"{symbol} L={levels} 보다 큰 L={level} 이 두 예산 안에 들어갑니다"
                )


def test_kappa_sensitivity_is_monotonic():
    """kappa_add 가 커지면 선택 단계는 줄어들지 않습니다 (단조성 — 우열 판정 아님)."""
    runs = {kappa_add: run_replay(kappa_add=kappa_add) for kappa_add in KAPPA_SENSITIVITY}
    for symbol in SYMBOL_SPECS:
        for session_index in range(len(SESSIONS)):
            levels = [
                runs[kappa_add]["symbols"][symbol]["policies"]["E"]["sessions"][session_index]["levels"]
                for kappa_add in KAPPA_SENSITIVITY
            ]
            for smaller, larger in zip(levels, levels[1:]):
                assert larger >= smaller, f"{symbol} kappa_add 민감도 단조성 위반: {levels}"


def test_replay_window_uses_expected_sessions(replay):
    for symbol, symbol_result in replay["symbols"].items():
        for policy, result in symbol_result["policies"].items():
            assert [entry["date"] for entry in result["sessions"]] == list(SESSIONS)


# ── state / 브로커 미접촉 정적 가드 ───────────────────────────────────


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
    # 가격 공식만 strategy 에서 가져옵니다 (단일 출처).
    assert any(line.startswith("from strategy import") for line in import_lines)


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
