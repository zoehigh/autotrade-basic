"""
`src/dynamic_preview.py` — DRY 전용 동적 단계 통합 미리보기 테스트.

검증 범위 (네트워크·state·주문 접촉 없음):
- 기본 비활성: `DYNAMIC_LOC_PREVIEW` 플래그가 없거나 DRY 가 아니면 후크는 아무 것도 하지 않음
- 메리츠 실측 OHLC fixture 로 고정 3 vs 동적(D/E) 주문가·수량 비교 구조 확인
- 미리보기가 state 객체를 받지 않음 (정적 가드 + 런타임 불변)
- 미체결 예약금 미확정/부분체결/조회실패/ATR부족/API오류 → fallback 3 + 사유
- 같은 세션 반복 호출 멱등성(동일 입력 → 동일 출력)
- 예약금 이중차감 방지(reserved_already_deducted)와 예약금 합산식
"""

import ast
import copy
import json
import os
import sys
from pathlib import Path

import pytest

# config/strategy import 전에 시드가 필요합니다 (config.py 가 모듈 로드 시 검증).
os.environ.setdefault("TQQQ_SEED", "40000")
os.environ.setdefault("SOXL_SEED", "20000")

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
for _path in (str(_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


from dynamic_preview import (  # noqa: E402
    PREVIEW_FIXED_LEVELS,
    compare_dynamic_preview,
    compute_open_reserved_amount,
    format_preview_report,
    get_prev_levels,
    reset_preview_state,
)
from strategy import build_additional_loc_prices  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "meritz_ohlc_20261106.json"


@pytest.fixture(autouse=True)
def _clean_preview_state():
    """세션 prev 메모리를 테스트마다 비웁니다 (테스트 간 간섭 방지)."""
    reset_preview_state()
    yield
    reset_preview_state()


def _bars(symbol="TQQQ"):
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data[symbol]


def _preview(bars, **overrides):
    params = {
        "symbol": "TQQQ",
        "bars": bars,
        "last_close": bars[-1]["c"],
        "unit_amount": 2000.0,
        "base_qty": 24,
        "ceiling": 82.5,
        "remaining_seed": 20000.0,
        "budget_available": 18000.0,
        "open_reserved": 0.0,
        "normal_amount": 1980.0,
        "session_date": "20261106",
        "prev_levels": None,
    }
    params.update(overrides)
    return compare_dynamic_preview(**params)


# ── 1. 기본 비활성 (플래그 off → 후크 미동작) ───────────────────────────


class _FakeBroker:
    """
    실제 브로커 계약(`base.py`)을 따르는 stub.

    - `get_daily_closes() → list[float]` (종가만)
    - `get_daily_candles()` 는 base 기본 구현과 동일하게 `date`+`close` 만 채웁니다
      → OHLC(o/h/l/c) 없음 = 실제 DRY 실행과 같은 조건.
    """

    def __init__(self, closes=None, raises=None, candles=None):
        self.closes = closes or []
        self.raises = raises
        self.candles = candles
        self.closed = False
        self.order_calls = []

    def get_daily_closes(self, symbol, exchange, days=5):
        if self.raises is not None:
            raise self.raises
        return [float(close) for close in self.closes]

    def get_daily_candles(self, symbol, exchange, days=5):
        # 실제 base.py 기본 구현과 동일: 종가만 있고 OHLC/날짜는 없음
        if self.candles is not None:
            return list(self.candles)
        return [{"date": None, "close": close} for close in self.get_daily_closes(
            symbol, exchange, days
        )]

    def place_order(self, *args, **kwargs):  # pragma: no cover - 호출되면 실패
        self.order_calls.append((args, kwargs))
        raise AssertionError("미리보기에서 주문을 전송하면 안 됩니다")

    def cancel_order(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("미리보기에서 주문을 취소하면 안 됩니다")


class _OhlcBroker(_FakeBroker):
    """`get_daily_candles()` 를 OHLC 로 오버라이드한 브로커 (후속 배선 시나리오)."""

    def get_daily_candles(self, symbol, exchange, days=5):
        return [
            {
                "date": bar["date"],
                "o": bar["o"],
                "h": bar["h"],
                "l": bar["l"],
                "c": bar["c"],
            }
            for bar in _bars()
        ]


def _strategy_result(**overrides):
    result = {
        "symbol": "TQQQ",
        "last_price": 69.6,
        "unit_amount": 2000.0,
        "unit_qty": 24.0,
        "star_buy_price": 82.5,
        "remaining_seed": 20000.0,
        "orderable_cash": 18000.0,
        "orders": [
            {
                "side": "BUY",
                "quantity": 23,
                "price": 82.5,
                "order_type": "LOC",
                "comment": "전반전 별지점 매수",
            },
            {
                "side": "BUY",
                "quantity": 1,
                "price": 80.0,
                "order_type": "LOC",
                "comment": "추가매수 1단계 (급락 대비) [추가매수]",
            },
        ],
    }
    result.update(overrides)
    return result


def test_flag_off_hook_does_nothing(monkeypatch, capsys):
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.delenv("DYNAMIC_LOC_PREVIEW", raising=False)

    broker = _FakeBroker(closes=[bar["c"] for bar in _bars()])
    trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert trading_bot._dynamic_preview_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "yes", "off", "2"])
def test_enabled_only_for_explicit_truthy_flag(monkeypatch, value):
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", value)
    assert trading_bot._dynamic_preview_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " true "])
def test_enabled_for_truthy_flag_in_dry(monkeypatch, value):
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", value)
    assert trading_bot._dynamic_preview_enabled() is True


def test_hook_is_disabled_outside_dry(monkeypatch, capsys):
    import trading_bot

    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", "1")
    for mode in ("LIVE", "REPAIR"):
        monkeypatch.setattr(trading_bot, "TRADE_MODE", mode)
        broker = _FakeBroker(closes=[bar["c"] for bar in _bars()])
        trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())
    assert capsys.readouterr().out == ""


def test_hook_prints_preview_when_enabled(monkeypatch, capsys):
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", "1")

    # 실제 계약: get_daily_candles 는 date+close 만 (OHLC 없음) → ATR 불가
    broker = _FakeBroker(
        closes=[bar["c"] for bar in _bars()],
        candles=[{"date": bar["date"], "close": bar["c"]} for bar in _bars()],
    )
    trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())

    out = capsys.readouterr().out
    assert "[동적 LOC 미리보기] TQQQ" in out
    assert "고정 3" in out
    assert "ATR 계산 불가" in out
    assert "fallback:ohlc-unavailable" in out  # 현실 반영: OHLC 미확보 → 고정 3
    assert "예약금: 미확정" in out  # DRY 기본값은 undetermined (fail-closed)
    assert broker.order_calls == []


def test_hook_never_synthesizes_ohlc_from_closes(monkeypatch, capsys):
    """TOSS 처럼 date+close 만 주는 소스에서도 OHLC 를 합성하지 않습니다."""
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", "1")

    broker = _FakeBroker(
        closes=[100.0, 101.0, 102.0],
        candles=[{"date": "2026-11-05", "close": 101.0}],
    )
    trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())

    out = capsys.readouterr().out
    assert "ohlc-unavailable:daily_candles" in out
    assert "ATR 계산 불가" in out
    assert broker.order_calls == []


def test_hook_uses_ohlc_candles_when_broker_provides_them(monkeypatch, capsys):
    """OHLC 를 제공하는 오버라이드가 있으면 ATR 계산이 실제로 동작합니다."""
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", "1")

    broker = _OhlcBroker()
    trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())

    out = capsys.readouterr().out
    assert "지표: ATR=" in out
    assert "세션 20261106" in out
    # 예약금 미확정(fail-closed)은 OHLC 가 있어도 유지됩니다
    assert "fallback:reservation-undetermined" in out
    assert broker.order_calls == []


def test_hook_survives_ohlc_query_failure(monkeypatch, capsys):
    import trading_bot

    monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
    monkeypatch.setenv("DYNAMIC_LOC_PREVIEW", "1")

    broker = _FakeBroker(raises=RuntimeError("boom"))
    trading_bot._log_dynamic_loc_preview(broker, "TQQQ", "NAS", _strategy_result())

    out = capsys.readouterr().out
    assert "[동적 LOC 미리보기]" in out
    assert "fallback:api-error" in out
    assert "ohlc-query-failed:RuntimeError" in out
    assert broker.order_calls == []


# ── 2. 메리츠 fixture 기반 비교 구조 ─────────────────────────────────────


def test_fixed3_vs_dynamic_structure_meritz_fixture():
    bars = _bars("TQQQ")
    preview = _preview(bars)

    assert preview["virtual"] is True
    assert preview["indicator"]["available"] is True
    assert preview["indicator"]["bars"] == len(bars)

    fixed = preview["fixed"]
    dynamic = preview["dynamic"]
    assert fixed["levels"] == PREVIEW_FIXED_LEVELS
    assert fixed["order_count"] == len(fixed["orders"]) == 3
    for order in fixed["orders"] + dynamic["orders"]:
        assert order["virtual"] is True
        assert order["side"] == "BUY"
        assert order["order_type"] == "LOC"
        assert order["quantity"] == 1
        assert order["t_target"] == 0.0

    # 가격 공식 단일 출처: 양쪽 모두 build_additional_loc_prices 결과와 일치
    expected_fixed = [
        price for _lvl, price in build_additional_loc_prices(2000.0, 24, 3, 82.5)
    ]
    assert [order["price"] for order in fixed["orders"]] == expected_fixed
    expected_dynamic = [
        price
        for _lvl, price in build_additional_loc_prices(
            2000.0, 24, dynamic["levels"], 82.5
        )
    ]
    assert [order["price"] for order in dynamic["orders"]] == expected_dynamic

    comparison = preview["comparison"]
    assert comparison["fixed_levels"] == 3
    assert comparison["dynamic_levels"] == dynamic["levels"]
    assert comparison["level_delta"] == dynamic["levels"] - 3
    assert comparison["fixed_order_count"] == 3
    assert comparison["dynamic_order_count"] == dynamic["order_count"]
    assert comparison["fixed_prices"] == expected_fixed
    assert comparison["shared_prefix_match"] is True
    assert comparison["budget_delta"] == pytest.approx(
        comparison["additional_total_delta"]
    )

    # 예약금 0(측정값)이면 총액 = 정상매수 + 추가매수
    assert preview["budget"]["open_reserved_state"] == "measured"
    assert fixed["total"] == pytest.approx(1980.0 + fixed["additional_total"])
    assert dynamic["total"] == pytest.approx(1980.0 + dynamic["additional_total"])

    report = format_preview_report(preview)
    assert "고정 3" in report
    assert "동적" in report


def test_dynamic_levels_change_with_volatility_bucket():
    """ATR 비율이 높을수록 동적 상한(고정 3 vs 5/7)이 커지는지 확인."""
    bars = _bars("TQQQ")
    high_vol = _preview(bars)
    # 같은 봉에서 종가를 크게 키워 ATR 비율을 낮춥니다 (버킷 하향)
    low_vol = _preview(bars, last_close=bars[-1]["c"] * 8, prev_levels={"D": None, "E": None})

    assert high_vol["indicator"]["atr_ratio"] > low_vol["indicator"]["atr_ratio"]
    assert high_vol["dynamic"]["levels"] >= low_vol["dynamic"]["levels"]


# ── 3. state 객체를 받지 않음 (정적 가드 + 런타임 불변) ──────────────────


def _module_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_preview_module_has_no_state_or_broker_imports():
    allowed = {"math", "datetime", "dynamic_levels", "strategy"}
    imported = _module_imports(_SRC / "dynamic_preview.py")
    assert imported <= allowed, f"허용되지 않은 import: {sorted(imported - allowed)}"


def test_preview_signature_has_no_state_parameter():
    import inspect

    from dynamic_preview import compare_dynamic_preview as compare

    parameters = inspect.signature(compare).parameters
    assert "state" not in parameters
    assert all("state" not in name for name in parameters)


def test_trading_bot_hook_never_passes_state_to_preview():
    tree = ast.parse((_ROOT / "trading_bot.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "compare_dynamic_preview"
    ]
    assert calls, "trading_bot 후크가 compare_dynamic_preview 를 호출해야 합니다"
    for call in calls:
        for keyword in call.keywords:
            assert keyword.arg != "state", "미리보기에 state 객체를 넘기면 안 됩니다"
        # 위치 인자는 symbol 하나만 허용
        assert len(call.args) == 1


def test_preview_does_not_mutate_state_like_payload():
    state = {
        "T": 13.25,
        "net_invested": 4321.0,
        "orders_meta": {"123": {"total_qty": 3, "t_target": 1.0}},
        "reverse_mode": {},
    }
    before = copy.deepcopy(state)
    bars = _bars("TQQQ")
    _preview(bars, remaining_seed=20000.0 - state["net_invested"])
    assert state == before


# ── 4. 미확정/실패 시나리오 → fallback 3 + 사유 ─────────────────────────


@pytest.mark.parametrize(
    "note, expected",
    [
        ("open-unfilled", "reservation-undetermined"),
        ("open-partial-fill", "reservation-undetermined"),
        ("open-order-query-failed", "reservation-undetermined"),
    ],
)
def test_undetermined_reservation_falls_back_to_fixed3(note, expected):
    preview = _preview(_bars(), open_reserved=None, reservation_note=note)

    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert expected in preview["dynamic"]["reason"]
    assert note in preview["dynamic"]["reason"]
    assert expected in preview["reasons"]
    # 미확정은 0 으로 간주하지 않습니다
    assert preview["budget"]["open_reserved"] is None
    assert preview["budget"]["open_reserved_state"] == "undetermined"
    assert preview["budget"]["reserved_applied"] is None
    assert preview["fixed"]["total"] is None
    assert preview["dynamic"]["total"] is None


def test_data_error_falls_back_to_fixed3():
    preview = _preview(_bars(), data_errors=["ohlc-query-failed:BrokerError"])

    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "api-error" in preview["dynamic"]["reason"]
    assert "ohlc-query-failed:BrokerError" in preview["dynamic"]["reason"]


def test_ohlc_unavailable_error_token():
    preview = _preview(_bars(), data_errors=["ohlc-unavailable:daily_candles"])

    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "fallback:ohlc-unavailable" in preview["dynamic"]["reason"]
    assert "ohlc-unavailable" in preview["reasons"]


def test_undetermined_session_date_falls_back_without_utc():
    """날짜 없는 봉 + session_date 없음 → UTC 대체 없이 fallback."""
    bars = [{k: bar[k] for k in ("o", "h", "l", "c")} for bar in _bars()]
    preview = compare_dynamic_preview(
        "TQQQ",
        bars=bars,
        last_close=bars[-1]["c"],
        unit_amount=2000.0,
        base_qty=24,
        ceiling=82.5,
        remaining_seed=20000.0,
        budget_available=18000.0,
        open_reserved=0.0,
        normal_amount=1980.0,
        session_date=None,
    )

    assert preview["session_date"] is None
    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "session-date-undetermined" in preview["dynamic"]["reason"]
    # 세션일 미확정 → prev 기록 없음
    assert get_prev_levels("TQQQ") == {"D": None, "E": None}


@pytest.mark.parametrize("field", ["remaining_seed", "budget_available"])
def test_unknown_budget_falls_back_to_fixed3(field):
    preview = _preview(_bars(), **{field: None})

    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "budget-undetermined" in preview["dynamic"]["reason"]
    assert field in preview["dynamic"]["reason"]


def test_insufficient_bars_falls_back_to_fixed3():
    bars = _bars()[-5:]
    preview = _preview(bars)

    assert preview["indicator"]["available"] is False
    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "no-indicator" in preview["dynamic"]["reason"]
    assert "atr-insufficient" in preview["reasons"]


def test_invalid_thresholds_fall_back_to_fixed3():
    preview = _preview(_bars(), t1=0.2, t2=0.1)

    assert preview["dynamic"]["levels"] == 3
    assert preview["dynamic"]["fallback"] is True
    assert "invalid-input" in preview["dynamic"]["reason"]


# ── 5. 예약금 합산 · 이중차감 방지 ──────────────────────────────────────


def test_measured_reserved_is_added_when_not_deducted():
    preview = _preview(_bars(), open_reserved=500.0, reserved_already_deducted=False)

    assert preview["budget"]["open_reserved_state"] == "measured"
    assert preview["budget"]["reserved_applied"] == pytest.approx(500.0)
    assert preview["dynamic"]["total"] == pytest.approx(
        1980.0 + preview["dynamic"]["additional_total"] + 500.0
    )


def test_measured_reserved_is_not_double_deducted():
    preview = _preview(_bars(), open_reserved=500.0, reserved_already_deducted=True)

    assert preview["budget"]["reserved_applied"] == 0.0
    assert preview["dynamic"]["total"] == pytest.approx(
        1980.0 + preview["dynamic"]["additional_total"]
    )


def test_reserved_consumes_budget_and_shrinks_levels():
    wide = _preview(_bars(), budget_available=2400.0, open_reserved=0.0)
    tight = _preview(_bars(), budget_available=2400.0, open_reserved=400.0)

    assert tight["dynamic"]["levels"] <= wide["dynamic"]["levels"]


def test_compute_open_reserved_amount_formula():
    rows = [
        {"side": "BUY", "price": 10.0, "quantity": 5, "filled_qty": 2},
        {"side": "BUY", "price": 4.0, "quantity": 3, "filled_qty": 0},
        {"side": "SELL", "price": 99.0, "quantity": 9, "filled_qty": 0},
    ]
    amount, detail = compute_open_reserved_amount(rows)
    assert amount == pytest.approx(10.0 * 3 + 4.0 * 3)
    assert detail["state"] == "measured"
    assert detail["count"] == 2
    assert detail["sell_count"] == 1


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [{"side": "BUY", "price": "n/a", "quantity": 1}],
        [{"side": "BUY", "price": 10.0, "quantity": 1, "filled_qty": 2}],
        ["not-a-dict"],
    ],
)
def test_compute_open_reserved_amount_undetermined(rows):
    amount, detail = compute_open_reserved_amount(rows)
    assert amount is None
    assert detail["state"] == "undetermined"


# ── 6. 세션 prev · 멱등성 ───────────────────────────────────────────────


def test_same_session_repeat_is_idempotent():
    bars = _bars()
    first = _preview(bars)
    second = _preview(bars)
    assert first == second
    assert first["prev"] == {"D": None, "E": None}


def test_prev_levels_come_from_earlier_session_only():
    bars = _bars()
    # 1세션: 고변동 → D 상한 7
    first = _preview(bars, session_date="20261105")
    assert first["indicator"]["atr_ratio"] > 0.06
    assert get_prev_levels("TQQQ", "20261105")["D"] == 7

    # 2세션: r 이 버킷 경계(5)지만 delta 안쪽이면 7 유지 (히스테리시스)
    #    목표 r = 0.058 → last_close = atr / 0.058
    atr = first["indicator"]["atr"]
    second = _preview(bars, last_close=atr / 0.058, session_date="20261106")
    assert second["dynamic"]["reason"].startswith("budget:")
    assert "L_D=7" in second["dynamic"]["reason"]
    assert second["prev"]["D"] == 7

    # 같은 세션 반복은 여전히 동일 (prev 는 이전 세션만 참조)
    assert _preview(bars, last_close=atr / 0.058, session_date="20261106") == second


def test_prev_override_is_honored():
    bars = _bars()
    preview = _preview(bars, prev_levels={"D": 7, "E": 5}, last_close=bars[-1]["c"])
    assert preview["prev"] == {"D": 7, "E": 5}
    assert "L_D=7" in preview["dynamic"]["reason"]


def test_reset_preview_state_clears_memory():
    _preview(_bars(), session_date="20261105")
    assert get_prev_levels("TQQQ", "20261105")["D"] == 7
    reset_preview_state("TQQQ")
    assert get_prev_levels("TQQQ", "20261105") == {"D": None, "E": None}


# ── 7. 가상 주문에는 전송 경로가 없음 ───────────────────────────────────


def test_virtual_orders_are_plain_data():
    preview = _preview(_bars())
    for side in ("fixed", "dynamic"):
        for order in preview[side]["orders"]:
            assert set(order) == {
                "side",
                "quantity",
                "price",
                "order_type",
                "t_target",
                "is_additional",
                "virtual",
                "policy",
                "level",
            }
