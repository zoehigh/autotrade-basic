"""
메리츠 OHLC 연동 — `get_daily_candles()` 오버라이드 + DRY 미리보기 비교 테스트.

범위:
- 메리츠 `get_daily_candles()`: API 파라미터/필드 매핑/정렬/dedupe/OHLC 관계 검증
- 확정봉 필터: `us_session_closed()` 기반 (휴장일·미래 세션 제외)
- `get_daily_closes()` 동작 불변 (list[float], 오늘 ET 봉 제외)
- 조회 실패 → BrokerError
- 동적 미리보기 fallback 3 (ohlc-query-failed / ohlc-unavailable)
- **실측(조회 전용)**: 메리츠 실 계정으로 TQQQ/SOXL 40봉 OHLC 확보 + DRY 비교 출력
  (`.env` 자격증명 + RUN_BROKER_API_TESTS=1 필요, 없으면 skip)
- 주문/state 저장 호출 0건 (정적 AST 가드 + 런타임 불변)

경계:
- 실측 테스트는 **조회 API만** 호출합니다 (`get_daily_candles`/`get_daily_closes`).
- 미리보기는 `virtual=True` 가상 주문만 만들며 state·브로커 주문에 접촉하지 않습니다.
"""

import ast
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

# config/strategy import 전에 시드가 필요합니다 (config.py 가 모듈 로드 시 검증).
os.environ.setdefault("TQQQ_SEED", "40000")
os.environ.setdefault("SOXL_SEED", "20000")

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
for _path in (str(_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from dotenv import load_dotenv  # noqa: E402

# .env 는 gitignore 대상이며 작업 디렉터리에 없을 수 있어 명시 경로도 시도합니다.
load_dotenv()
load_dotenv(_ROOT / ".env")

from broker.base import BrokerError  # noqa: E402
from broker.market_utils import (  # noqa: E402
    us_session_closed,
    us_session_last_closed_date,
)
from broker.meritz.adapter import MeritzBroker, PATH_DAILY_CANDLES  # noqa: E402
from dynamic_preview import (  # noqa: E402
    PREVIEW_FIXED_LEVELS,
    compare_dynamic_preview,
    format_preview_report,
    reset_preview_state,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "meritz_ohlc_20261106.json"
_ET = ZoneInfo("America/New_York")

# 실측 비교 후보값 (**후보·미확정** — 확정 정책이 아닙니다)
_MERITZ_THRESHOLDS = {
    "TQQQ": {"exchange": "NAS", "t1": 0.04, "t2": 0.07},
    "SOXL": {"exchange": "AMS", "t1": 0.09, "t2": 0.14},
}
_CANDIDATE_KAPPA_ADD = 0.02
_CANDIDATE_DELTA = 0.02
_REAL_DAYS = 40


@pytest.fixture(autouse=True)
def _clean_preview_state():
    """세션 prev 메모리를 테스트마다 비웁니다."""
    reset_preview_state()
    yield
    reset_preview_state()


# ── 헬퍼 ───────────────────────────────────────────────────────────────


def _row(date, o, h, l, c):
    """메리츠 일봉 응답 행 (실측 필드명)."""
    return {"date": date, "oprc": o, "hprc": h, "lprc": l, "prpr": c}


def _make_broker(rows, captured=None):
    """
    네트워크 없이 `_request` 만 대체한 MeritzBroker.

    `_check_rsp_cd`/`_extract_rows` 등 나머지 로직은 실제 구현을 그대로 씁니다.
    """
    broker = MeritzBroker.__new__(MeritzBroker)
    broker._last_candle_dropped_bars = 0

    def fake_request(method, path, params=None, json_body=None, is_query=True):
        if captured is not None:
            captured.append({"method": method, "path": path, "params": dict(params or {})})
        return {"rsp_cd": "0000", "data": {"list": rows}}

    broker._request = fake_request
    return broker


def _fixture_bars(symbol="TQQQ"):
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data[symbol]


def _preview(bars, symbol="TQQQ", **overrides):
    last_bar = bars[-1] if bars else {"c": 0.0, "date": None}
    params = {
        "symbol": symbol,
        "bars": bars,
        "last_close": last_bar["c"],
        "unit_amount": 2000.0,
        "base_qty": 38,
        "ceiling": last_bar["c"],
        "remaining_seed": 20000.0,
        "budget_available": 18000.0,
        "open_reserved": 0.0,
        "normal_amount": 1980.0,
        "policy": "E",
        "session_date": last_bar["date"],
        "prev_levels": {"D": None, "E": None},
    }
    params.update(overrides)
    return compare_dynamic_preview(**params)


# ── 1. get_daily_candles 오버라이드 (네트워크 없음) ─────────────────────


def test_candles_params_match_closes_params():
    """get_daily_candles 는 get_daily_closes 와 동일한 5개 query 를 씁니다."""
    rows = [_row("20261106", 50.0, 51.0, 49.0, 50.5)]
    candle_calls, close_calls = [], []

    _make_broker(rows, captured=candle_calls).get_daily_candles("tqqq", "NAS")
    _make_broker(rows, captured=close_calls).get_daily_closes("tqqq", "NAS")

    assert candle_calls[0]["method"] == "GET"
    assert candle_calls[0]["path"] == PATH_DAILY_CANDLES
    assert candle_calls[0]["params"] == close_calls[0]["params"]
    assert set(candle_calls[0]["params"]) == {
        "mrkt_div_code",
        "mrkt_cls_code",
        "iscd",
        "dely_rltm_cls_code",
        "mod_stpr_cls_code",
    }
    assert candle_calls[0]["params"]["iscd"] == "TQQQ"


def test_candles_map_fields_and_order():
    """date/oprc/hprc/lprc/prpr → date(ISO)+o/h/l/c, 오래된 순."""
    rows = [
        _row("20261105", 20.0, 21.0, 19.5, 20.5),
        _row("20261103", 18.0, 19.0, 17.5, 18.5),
        _row("20261104", 19.0, 20.0, 18.0, 19.5),
    ]
    bars = _make_broker(rows).get_daily_candles("TQQQ", "NAS", days=40)

    assert [bar["date"] for bar in bars] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert bars[0] == {"date": "2026-11-03", "o": 18.0, "h": 19.0, "l": 17.5, "c": 18.5}


def test_candles_dedupe_duplicate_dates():
    """같은 날짜 행이 여러 개면 1개만 남습니다 (뒤 행이 우선)."""
    rows = [
        _row("20261104", 19.0, 20.0, 18.0, 19.5),
        _row("20261104", 19.0, 20.0, 18.0, 19.9),
    ]
    bars = _make_broker(rows).get_daily_candles("TQQQ", "NAS", days=40)
    assert len(bars) == 1
    assert bars[0]["c"] == 19.9


def test_candles_skip_invalid_ohlc_rows():
    """음수/0 가격, h<max(o,c), l>min(o,c), 비dict, 잘못된 날짜는 조용히 스킵."""
    rows = [
        _row("20261106", 50.0, 51.0, 49.0, 50.5),   # 정상
        _row("20261105", 10.0, 10.5, 9.0, 11.0),    # h < max(o,c)
        _row("20261104", 10.0, 11.0, 9.9, 9.8),     # l > min(o,c)
        _row("20261103", 0.0, 11.0, 9.0, 10.0),     # 시가 0
        "not-a-dict",                                # 비dict
        _row("2026-11", 10.0, 11.0, 9.0, 10.0),     # 날짜 불량
    ]
    broker = _make_broker(rows)
    bars = broker.get_daily_candles("TQQQ", "NAS", days=40)

    assert [bar["date"] for bar in bars] == ["2026-11-06"]
    assert broker._last_candle_dropped_bars == 5


def test_candles_calendar_out_of_bounds_row_does_not_abort_whole_query():
    """
    캘린더 범위 밖 날짜 행("00000000" 등)이 섞여도 40봉 조회 전체가 실패하지 않습니다.

    `us_session_closed()` 는 이런 날짜에서 예외를 던지므로, 행별로 잡지 않으면
    유효한 봉까지 전부 잃습니다 (P2 회귀 방지).
    """
    rows = [
        _row("20261104", 10.0, 11.0, 9.0, 10.5),   # 정상
        _row("00000000", 10.0, 11.0, 9.0, 10.0),   # 캘린더 범위 밖 (ValueError)
        _row("20261105", 10.0, 11.0, 9.0, 10.7),   # 정상
        _row("19000101", 10.0, 11.0, 9.0, 10.0),   # 캘린더 첫 세션 이전 (DateOutOfBounds)
        _row("20261106", 10.0, 11.0, 9.0, 10.9),   # 정상
    ]
    broker = _make_broker(rows)
    bars = broker.get_daily_candles("TQQQ", "NAS", days=40)

    assert [bar["date"] for bar in bars] == [
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
    ]
    assert broker._last_candle_dropped_bars == 2
    assert broker.get_last_candle_drop_reasons().count("calendar_out_of_bounds") == 2


def test_candles_drop_reasons_cover_each_token():
    """제외 사유 토큰이 행별로 정확히 1개씩 기록됩니다 (결과 dict 에는 미포함)."""
    rows = [
        _row("20261106", 50.0, 51.0, 49.0, 50.5),   # 정상
        "not-a-dict",                                # non_dict
        _row("2026-11", 10.0, 11.0, 9.0, 10.0),     # bad_date_format
        _row("20261103", 0.0, 11.0, 9.0, 10.0),     # non_positive_price
        _row("20261102", 10.0, 10.5, 9.0, 11.0),    # ohlc_relation_violation
        _row("00000000", 10.0, 11.0, 9.0, 10.0),    # calendar_out_of_bounds
        _row("20261126", 10.0, 11.0, 9.0, 10.0),    # non_finalized (추수감사절)
    ]
    broker = _make_broker(rows)
    bars = broker.get_daily_candles("TQQQ", "NAS", days=40)

    reasons = broker.get_last_candle_drop_reasons()
    assert [bar["date"] for bar in bars] == ["2026-11-06"]
    assert reasons == [
        "non_dict",
        "bad_date_format",
        "non_positive_price",
        "ohlc_relation_violation",
        "calendar_out_of_bounds",
        "non_finalized",
    ]
    assert broker._last_candle_dropped_bars == len(reasons)
    for bar in bars:
        assert set(bar) == {"date", "o", "h", "l", "c"}


def test_candles_drop_reasons_getter_returns_copy():
    """getter 는 복사본을 돌려줍니다 (호출 측 변형이 내부 진단값을 바꾸지 않음)."""
    broker = _make_broker([_row("00000000", 10.0, 11.0, 9.0, 10.0)])
    broker.get_daily_candles("TQQQ", "NAS", days=40)

    reasons = broker.get_last_candle_drop_reasons()
    reasons.append("mutated")
    assert broker.get_last_candle_drop_reasons() == ["calendar_out_of_bounds"]


def test_candles_all_rows_invalid_returns_empty_list():
    """
    유효 행이 0개면 빈 리스트를 반환합니다 (예외 아님).

    동적 프리뷰 측(`trading_bot._dynamic_ohlc_bars`)이 빈 리스트를
    `ohlc-unavailable` 로 보고 고정 3 fallback 을 타는 계약입니다.
    """
    rows = [
        _row("00000000", 10.0, 11.0, 9.0, 10.0),
        _row("19000101", 10.0, 11.0, 9.0, 10.0),
        _row("2026-11", 10.0, 11.0, 9.0, 10.0),
        "not-a-dict",
    ]
    broker = _make_broker(rows)
    bars = broker.get_daily_candles("TQQQ", "NAS", days=40)

    assert bars == []
    assert broker._last_candle_dropped_bars == 4
    assert "calendar_out_of_bounds" in broker.get_last_candle_drop_reasons()


def test_candles_finalized_filter_uses_xnys_calendar():
    """
    확정봉 필터는 us_session_closed 기준입니다.

    - 추수감사절(2025-11-27, 2026-11-26): 세션이 아님 → 제외
    - 미래 세션(2027-01-04): 아직 마감 전 → 제외
    - 지난 세션(2026-11-06): 포함
    """
    rows = [
        _row("20261126", 10.0, 11.0, 9.0, 10.0),   # 2026 Thanksgiving
        _row("20251127", 10.0, 11.0, 9.0, 10.0),   # 2025 Thanksgiving
        _row("20270104", 10.0, 11.0, 9.0, 10.0),   # 미래 세션
        _row("20261106", 50.0, 51.0, 49.0, 50.5),  # 확정 세션
    ]
    bars = _make_broker(rows).get_daily_candles("TQQQ", "NAS", days=40)

    assert [bar["date"] for bar in bars] == ["2026-11-06"]
    assert us_session_closed("2026-11-26") is False
    assert us_session_closed("2025-11-27") is False


def test_candles_respect_days_limit():
    rows = [
        _row("20261102", 10.0, 11.0, 9.0, 10.0),
        _row("20261103", 10.0, 11.0, 9.0, 10.0),
        _row("20261104", 10.0, 11.0, 9.0, 10.0),
        _row("20261105", 10.0, 11.0, 9.0, 10.0),
        _row("20261106", 10.0, 11.0, 9.0, 10.0),
    ]
    bars = _make_broker(rows).get_daily_candles("TQQQ", "NAS", days=2)
    assert [bar["date"] for bar in bars] == ["2026-11-05", "2026-11-06"]


def test_candles_raise_broker_error_on_request_failure():
    broker = MeritzBroker.__new__(MeritzBroker)
    broker._last_candle_dropped_bars = 0

    def boom(*args, **kwargs):
        raise requests.exceptions.Timeout("network down")

    broker._request = boom
    with pytest.raises(BrokerError):
        broker.get_daily_candles("TQQQ", "NAS")


def test_candles_raise_broker_error_on_rsp_cd():
    broker = MeritzBroker.__new__(MeritzBroker)
    broker._last_candle_dropped_bars = 0
    broker._request = lambda *a, **k: {"rsp_cd": "9999", "rsp_msg": "실패"}
    with pytest.raises(BrokerError):
        broker.get_daily_candles("TQQQ", "NAS")


def test_closes_format_unchanged_and_excludes_today_et():
    """get_daily_closes: list[float], 오늘(ET) 봉 제외 — 기존 동작 불변."""
    today_et = datetime.now(_ET).strftime("%Y%m%d")
    rows = [
        _row("20261104", 10.0, 11.0, 9.0, 10.5),
        _row("20261105", 10.0, 11.0, 9.0, 10.7),
        _row(today_et, 10.0, 11.0, 9.0, 99.9),  # 오늘 봉 (미확정/제외 대상)
    ]
    closes = _make_broker(rows).get_daily_closes("TQQQ", "NAS", days=40)

    assert isinstance(closes, list)
    assert all(isinstance(value, float) for value in closes)
    assert closes == [10.5, 10.7]  # 오늘 봉(99.9) 제외


# ── 2. fallback 3 (OHLC 미확보/조회 실패) ──────────────────────────────


def test_preview_falls_back_to_fixed3_on_ohlc_unavailable():
    preview = _preview(
        _fixture_bars(),
        data_errors=["ohlc-unavailable:daily_candles"],
    )
    assert preview["fixed"]["levels"] == PREVIEW_FIXED_LEVELS
    assert preview["dynamic"]["levels"] == PREVIEW_FIXED_LEVELS
    assert preview["dynamic"]["fallback"] is True
    assert "ohlc-unavailable" in preview["dynamic"]["reason"]
    assert preview["virtual"] is True


def test_preview_falls_back_to_fixed3_on_ohlc_query_failed():
    preview = _preview(
        _fixture_bars(),
        data_errors=["ohlc-query-failed:BrokerError"],
    )
    assert preview["dynamic"]["levels"] == PREVIEW_FIXED_LEVELS
    assert preview["dynamic"]["fallback"] is True
    assert "ohlc-query-failed:BrokerError" in preview["dynamic"]["reason"]


def test_preview_falls_back_when_bars_are_empty():
    preview = _preview([], data_errors=["ohlc-unavailable:daily_candles"])
    assert preview["dynamic"]["levels"] == PREVIEW_FIXED_LEVELS
    assert preview["indicator"]["available"] is False


# ── 3. 주문/state 저장 0건 가드 ────────────────────────────────────────


def test_source_has_no_order_or_state_calls():
    """정적 AST 가드: 이 테스트 모듈은 주문·state 저장·봇 실행을 호출하지 않습니다."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    called_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "place_order" not in called_attrs
    assert "save_state" not in called_attrs
    assert "save_states" not in called_attrs
    assert "run" not in called_attrs


def test_preview_runtime_touches_no_state_and_no_orders(monkeypatch):
    """런타임 불변: 미리보기/조회 경로는 state 저장·주문 함수에 닿지 않습니다."""
    import state
    import broker.meritz.adapter as meritz_adapter

    def _boom(*args, **kwargs):
        raise AssertionError("주문/state 저장 호출 금지")

    monkeypatch.setattr(state, "save_state", _boom)
    monkeypatch.setattr(state, "save_states", _boom)
    monkeypatch.setattr(meritz_adapter.MeritzBroker, "place_order", _boom)

    rows = [
        _row("20261104", 10.0, 11.0, 9.0, 10.0),
        _row("20261105", 10.0, 11.5, 9.5, 11.0),
        _row("20261106", 11.0, 12.0, 10.0, 11.5),
    ]
    broker = _make_broker(rows)
    bars = broker.get_daily_candles("TQQQ", "NAS", days=20)
    preview = _preview(bars, prev_levels={"D": None, "E": None})

    assert preview["virtual"] is True
    assert preview["dynamic"]["levels"] >= PREVIEW_FIXED_LEVELS


# ── 4. 메리츠 실측 (조회 전용, opt-in) ─────────────────────────────────


def _reload_config_as_meritz():
    """config 를 BROKER=meritz, BROKER_MODE=real 로 인프로세스 교체합니다."""
    import config

    saved = {key: os.environ.get(key) for key in ("BROKER", "BROKER_MODE")}
    os.environ["BROKER"] = "meritz"
    os.environ["BROKER_MODE"] = "real"
    importlib.reload(config)
    return config, saved


def _restore_config(saved):
    import config

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(config)


@pytest.fixture(scope="module")
def meritz_broker():
    """실 메리츠 어댑터 (조회 전용). 자격증명 없으면 skip."""
    if not (os.getenv("MERITZ_APP_KEY") and os.getenv("MERITZ_APP_SECRET")):
        pytest.skip("MERITZ_APP_KEY/MERITZ_APP_SECRET 미설정 — 실측 생략")
    _config, saved = _reload_config_as_meritz()
    try:
        yield MeritzBroker()
    finally:
        _restore_config(saved)


def _sessions_between(first_date, last_date):
    """XNYS 세션 수 (누락 검증용)."""
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(first_date, last_date)
    return [session.strftime("%Y-%m-%d") for session in sessions]


@pytest.mark.meritz
def test_meritz_real_candles_40_rows_ohlc(meritz_broker):
    """TQQQ/SOXL 원시 40행 → 확정봉 OHLC + XNYS 세션 대조 + OHLC 관계 검증."""
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    for symbol, meta in _MERITZ_THRESHOLDS.items():
        exchange = meta["exchange"]
        bars = meritz_broker.get_daily_candles(symbol, exchange, days=_REAL_DAYS)
        closes = meritz_broker.get_daily_closes(symbol, exchange, days=_REAL_DAYS)

        # API 는 최신 40행을 주며, 장중이면 오늘 미확정 봉 1개가 제외됩니다.
        assert len(bars) in (39, 40), f"{symbol}: 확정봉 {len(bars)}개"
        assert len(bars) + meritz_broker._last_candle_dropped_bars == _REAL_DAYS
        assert meritz_broker._last_candle_dropped_bars in (0, 1)

        dates = [bar["date"] for bar in bars]
        assert dates == sorted(dates)
        assert len(dates) == len(set(dates))  # 중복 없음

        # XNYS 세션과 1:1 대조 (누락 없음)
        assert _sessions_between(dates[0], dates[-1]) == dates

        # 확정봉 + OHLC 관계
        for bar in bars:
            assert us_session_closed(bar["date"]) is True
            assert set(bar) == {"date", "o", "h", "l", "c"}
            assert min(bar["o"], bar["h"], bar["l"], bar["c"]) > 0
            assert bar["h"] >= max(bar["o"], bar["c"])
            assert bar["l"] <= min(bar["o"], bar["c"])
            assert bar["h"] >= bar["l"]

        assert dates[-1] == us_session_last_closed_date()

        # get_daily_closes 불변: list[float], 오늘(ET) 봉 제외
        assert isinstance(closes, list)
        assert all(isinstance(value, float) for value in closes)
        expected = sum(1 for bar in bars if bar["date"] < today_et)
        assert len(closes) == expected
        assert closes == [bar["c"] for bar in bars if bar["date"] < today_et]
        # 요청 상한(40)까지 주되, 당일 봉 포함 여부만큼만 39/40 이 됩니다
        assert len(closes) in (39, 40)


@pytest.mark.meritz
def test_meritz_real_dry_preview_comparison(meritz_broker, capsys):
    """실측 OHLC 로 고정 3 vs D vs E 를 비교 출력합니다 (후보 임계값, 미확정)."""
    results = {}
    for symbol, meta in _MERITZ_THRESHOLDS.items():
        exchange = meta["exchange"]
        bars = meritz_broker.get_daily_candles(symbol, exchange, days=20)
        assert len(bars) >= 15, f"{symbol}: ATR 계산에 필요한 확정봉 부족"

        last_close = bars[-1]["c"]
        base_qty = max(1, int(2000.0 // last_close))
        common = {
            "bars": bars,
            "last_close": last_close,
            "unit_amount": 2000.0,
            "base_qty": base_qty,
            "ceiling": last_close,
            "remaining_seed": 20000.0,
            "budget_available": 18000.0,
            "open_reserved": 0.0,
            "normal_amount": 1980.0,
            "t1": meta["t1"],
            "t2": meta["t2"],
            "delta": _CANDIDATE_DELTA,
            "kappa_add": _CANDIDATE_KAPPA_ADD,
            "session_date": bars[-1]["date"],
            "prev_levels": {"D": None, "E": None},
        }
        d_preview = compare_dynamic_preview(symbol, policy="D", **common)
        e_preview = compare_dynamic_preview(symbol, policy="E", **common)
        results[symbol] = {"D": d_preview, "E": e_preview}

        for preview in (d_preview, e_preview):
            assert preview["virtual"] is True
            assert preview["indicator"]["available"] is True
            assert preview["fixed"]["levels"] == PREVIEW_FIXED_LEVELS
            assert preview["dynamic"]["levels"] in (3, 5, 7)
            for order in preview["fixed"]["orders"] + preview["dynamic"]["orders"]:
                assert order["virtual"] is True
                assert order["side"] == "BUY"
                assert order["order_type"] == "LOC"

        # 출력: ATR(14), ATR%, 선택 단계, 후보 임계값, 예산 근거, 단계별 주문
        print(format_preview_report(e_preview))
        indicator = e_preview["indicator"]
        print(
            f"  [DRY 비교 요약] {symbol} 세션 {e_preview['session_date']} "
            f"ATR14={indicator['atr']:.4f} ATR%={indicator['atr_ratio'] * 100:.3f}% "
            f"(확정봉 {indicator['bars']}개) "
            f"후보 t1={meta['t1']:.2%} t2={meta['t2']:.2%} "
            f"delta={_CANDIDATE_DELTA:.2%} kappa_add={_CANDIDATE_KAPPA_ADD:.2f} (후보·미확정)"
        )
        for label in ("fixed", "dynamic"):
            side = e_preview[label]
            steps = ", ".join(
                f"L{order['level']}=${order['price']:.2f}x{order['quantity']}"
                for order in side["orders"]
            )
            print(f"    {label}({side['policy']}): 단계 {side['levels']} · {steps}")
        for name in ("D", "E"):
            preview = results[symbol][name]
            print(
                f"    {name} 단계={preview['dynamic']['levels']} "
                f"추가합계=${preview['dynamic']['additional_total']:.2f} · "
                f"근거: {preview['dynamic']['reason']}"
            )
        print(f"    사유: {', '.join(e_preview['reasons'])}")

    out = capsys.readouterr().out
    assert "ATR14=" in out
    assert "후보·미확정" in out
    assert len(results) == 2
