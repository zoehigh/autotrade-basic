"""텔레그램 **주문 접수 상세 알림** 회귀 테스트 (mock 전용 — 실 텔레그램/실 주문 없음).

주문 접수 알림은 파이프라인 EXECUTE 단계(`src/pipeline_execute.py`)에서만 나갑니다.
예전 `trading_bot.run_one_symbol()` 내부 seam 은 제거되었으므로(P0: 주문 루프 우회 경로
금지), 이 파일은 **실제 승인 파일을 만들어 `execute_approved_plan()` 을 통과**시키는
`tests/pipeline_flow.py` 헬퍼로 실행합니다 (evidence mock 대신 실제 커밋 근거 사용).

고정하는 계약:
  (1) 6개 브로커 id + demo/real 계좌 환경 표시
  (2) BUY / SELL 구분 표시
  (3) LIMIT / LOC / MARKET 유형별 가격 표시 (시장가를 "시장가"로 단정하지 않음)
  (4) 예상 주문금액 = 주문가 × 수량 (수수료 제외, "예상" 라벨)
  (5) 가격 None / 0 / 음수 / 문자열 / bool 경계 → 가격 미확정 + 예상금액 줄 없음
  (6) 1달러 미만 주문가의 4자리 정밀도 유지 (반올림·절단 금지)
  (7) 일반 접수 / 예약 접수 상태 문구 구분
  (8) 개인 채팅은 원문 주문번호, (9) 그룹·채널은 가명(`ord#`) 주문번호
  (10) stdout(공개 로그)에는 원문 주문번호가 남지 않음
  (11) DRY 는 승인 실행 단계 자체가 거부되어 접수 알림이 나가지 않음
  (12) 동작 불변 — order dict 미변경, place_order 인자·state fence 저장 순서 불변

정적 가드(`tests/test_odno_log_masking.py`)는 그대로 유지하며, 여기서는
"알림 본문의 원문 주문번호는 라벨 줄 1곳에만 등장" 같은 표시 계약을 추가로 고정합니다.
"""
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import log_mask  # noqa: E402
import notifier  # noqa: E402
import pipeline_flow  # noqa: E402
import trading_bot  # noqa: E402
from broker.base import Balance, OrderResult, PurchaseAmount  # noqa: E402

RAW = "0000039445"
BOT_TOKEN = "123456:FAKE-BOT-TOKEN"
CHAT_ID = "1122334455"
GROUP_ID = "-1001234567890"

BROKER_IDS = ["kis", "kiwoom", "ls", "toss", "nhplug", "meritz"]


class _StubBroker:
    """조회 API 만 흉내내는 브로커 스텁 — 주문은 `place_order` 가 정해진 결과만 돌려줍니다."""

    supports_open_orders_query = False

    def __init__(self, result, balance_qty=0):
        self.result = result
        self.balance_qty = balance_qty
        self.order_calls = []

    # 시장 정보
    def is_trading_day(self):
        return True

    def exchange_code(self, exchange):
        return "NASD"

    # 조회 API
    def get_order_history(self, symbol, exchange, days=30):
        return []

    def get_balance(self, symbol, exchange):
        return Balance(
            quantity=self.balance_qty,
            avg_price=50.0 if self.balance_qty else 0.0,
        )

    def get_purchase_amount(self, symbol, exchange):
        return PurchaseAmount(orderable_cash=1000.0)

    # 주문 API — 실제 주문은 절대 나가지 않습니다.
    def place_order(self, symbol, exchange, side, quantity, price, order_type):
        self.order_calls.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "side": side,
                "quantity": quantity,
                "price": price,
                "order_type": order_type,
            }
        )
        return self.result


def _order(**overrides):
    order = {
        "comment": "1회차 매수",
        "side": "BUY",
        "order_type": "LOC",
        "quantity": 3,
        "price": 12.5,
        "t_target": 1.0,
    }
    order.update(overrides)
    return order


def _symbol_config():
    return {
        "symbol": "TQQQ",
        "exchange": "NAS",
        "splits": 40,
        "symbol_type": "TQQQ",
        "seed": 10000.0,
        "additional_loc_levels": 3,
    }


def _state():
    """EXECUTE 재검증을 통과하는 최소 state (정산 근거/세션은 헬퍼가 채웁니다)."""
    return {
        "T": 0.0,
        "last_updated": "2026-10-20T11:14:03+00:00",
        "cycle_start_date": "",
        "effective_seed": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",
        "last_processed_ordno": "",
        "additional_loc_odno": [],
        "orders_meta": {},
        "balance_mismatch": {},
        "state_version": "v2",
        "close_prices": [],
        "reverse_mode": {},
        "pending_order_intent": None,
        "pending_order_batch": None,
        "position_qty": None,
        "sell_t_effects": {},
    }


def _run(
    monkeypatch,
    tmp_path,
    *,
    orders,
    result=OrderResult(order_id=RAW, order_time="2026-10-29 04:10:00", is_reservation=False),
    trade_mode="LIVE",
    broker_id="kis",
    broker_mode="real",
    real_notify=False,
    refresh=True,
    balance_qty=0,
):
    """승인 파일을 만들고 `execute_approved_plan()` 을 1회 실행하고 관찰값을 돌려줍니다.

    주문 외부 호출(텔레그램 전송·브로커 API·state 파일)은 모두 대체됩니다.
    `real_notify=True` 면 실제 `notifier.notify()`(채널 분리 로직)를 쓰고
    텔레그램 전송만 가로챕니다.
    """
    sent = []
    saved = []
    sleeps = []
    state = _state()
    approved_orders = [dict(order) for order in orders]
    # 승인 계획과 실행 직전 재계산 계획이 같아지도록 전략도 같은 주문을 냅니다.
    strategy_orders = [dict(order) for order in orders]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUN_ID", "run-telegram-details")
    monkeypatch.setenv("RUN_PHASE", "EXECUTE")
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
    monkeypatch.delenv("RUN_LOCK_FD", raising=False)
    monkeypatch.setenv("APPROVALS_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "plans"))
    # LIVE 승인 바인딩(계좌 지문/바인딩 라벨)용 테스트 값 — 실제 계좌번호는 쓰지 않습니다.
    monkeypatch.setenv("ACCOUNT_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("BROKER_ACCOUNT_BINDING", "test-account-binding")
    monkeypatch.setenv("QUIET_HOURS", "false")
    # real+LIVE 는 프리장 오픈까지 대기합니다 — 테스트에서는 실제로 자지 않고 기록만 합니다.
    monkeypatch.setattr(trading_bot.time, "sleep", lambda seconds: sleeps.append(seconds))

    if real_notify:
        monkeypatch.setattr(
            notifier,
            "send_telegram",
            lambda text: sent.append(text) or True,
        )
    else:
        def fake_notify(message, urgent=False, telegram_message=None):
            sent.append(
                {"message": message, "urgent": urgent, "telegram_message": telegram_message}
            )
            return True

        monkeypatch.setattr(trading_bot, "notify", fake_notify)

    monkeypatch.setattr(trading_bot, "TRADE_MODE", trade_mode)
    monkeypatch.setattr(trading_bot, "BROKER", broker_id)
    monkeypatch.setattr(trading_bot, "BROKER_MODE", broker_mode)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [_symbol_config()])
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: deepcopy(state))
    monkeypatch.setattr(
        trading_bot, "save_state", lambda symbol, snapshot: saved.append(deepcopy(snapshot))
    )

    strategy_result = {
        "last_price": 50.0,
        "position_qty": 0,
        "avg_price": 0.0,
        "orderable_cash": 1000.0,
        "star_point": 40.0,
        "T": 0.0,
        "orders": strategy_orders,
        "reverse_exit": False,
        "price_cap_notes": [],
    }
    monkeypatch.setattr(
        trading_bot, "무한매수법_V4", lambda broker, **kwargs: deepcopy(strategy_result)
    )

    broker = _StubBroker(result, balance_qty=balance_qty)
    error = None
    summary = None
    approval_path = None
    try:
        summary, approval_path, _plan = pipeline_flow.run_approved(
            tmp_path,
            broker,
            state,
            approved_orders,
            balance_qty=balance_qty,
            refresh=refresh,
        )
    except RuntimeError as caught:
        error = caught

    return SimpleNamespace(
        sent=sent,
        saved=saved,
        broker=broker,
        summary=summary,
        error=error,
        approval_path=approval_path,
        state=state,
        orders=orders,
        sleeps=sleeps,
    )


def _body(run):
    """주문접수 알림 1건의 개인 채팅용 원문 본문을 돌려줍니다."""
    assert len(run.sent) == 1, f"접수 알림은 1건이어야 합니다: {run.sent}"
    entry = run.sent[0]
    if isinstance(entry, str):
        return entry
    return entry["telegram_message"] or entry["message"]


# ── (1) 증권사 / 계좌 환경 ────────────────────────────────────────────
@pytest.mark.parametrize("broker_id", BROKER_IDS)
def test_broker_id_and_account_env_are_shown(monkeypatch, tmp_path, broker_id):
    for broker_mode in ("demo", "real"):
        run = _run(
            monkeypatch,
            tmp_path,
            orders=[_order()],
            broker_id=broker_id,
            broker_mode=broker_mode,
        )
        assert run.error is None, run.error
        body = _body(run)
        assert f"증권사: {broker_id}" in body
        assert f"계좌 환경: {broker_mode}" in body


# ── (2) 매수 / 매도 ──────────────────────────────────────────────────
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_side_is_shown(monkeypatch, tmp_path, side):
    # 매도는 보유수량 게이트(매도 합계 <= 잔고)를 통과해야 실행 경로에 도달합니다.
    run = _run(
        monkeypatch,
        tmp_path,
        orders=[_order(side=side)],
        balance_qty=3 if side == "SELL" else 0,
    )
    assert run.error is None, run.error
    assert f"매수/매도: {side}" in _body(run)


# ── (3) 주문 유형별 가격 표시 ─────────────────────────────────────────
@pytest.mark.parametrize("order_type", ["LIMIT", "LOC"])
def test_price_quoted_types_show_price(monkeypatch, tmp_path, order_type):
    run = _run(monkeypatch, tmp_path, orders=[_order(order_type=order_type, price=12.5)])
    assert run.error is None, run.error
    body = _body(run)
    assert f"주문 유형: {order_type}" in body
    assert "주문가: $12.5" in body
    assert "가격 미확정" not in body


def test_market_order_is_not_labelled_as_fixed_price(monkeypatch, tmp_path):
    """MARKET 은 체결가를 알 수 없으므로 '시장가'로 단정하지 않고 금액도 추정하지 않습니다."""
    run = _run(monkeypatch, tmp_path, orders=[_order(order_type="MARKET", price=12.5)])
    assert run.error is None, run.error
    body = _body(run)
    assert "주문 유형: MARKET" in body
    assert "가격 미확정(체결가 미정)" in body
    assert "주문가: $" not in body
    assert "예상 주문금액" not in body


# ── (4) 예상 주문금액 ────────────────────────────────────────────────
def test_estimated_amount_is_price_times_quantity(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path, orders=[_order(price=12.5, quantity=4)])
    assert run.error is None, run.error
    body = _body(run)
    assert "수량: 4주" in body
    assert "예상 주문금액(수수료 제외): $50.00" in body


# ── (5) 가격 경계값 ──────────────────────────────────────────────────
@pytest.mark.parametrize("price", [None, 0, 0.0, "12.5"])
def test_unknown_price_omits_estimate(monkeypatch, tmp_path, price):
    """가격 미확정(NULL/0/문자열)은 표시에서 추정하지 않습니다.

    비유한(NaN/Infinity)·음수·불리언 가격은 계획 비교 게이트가 fail-closed 로
    차단하므로(아래 테스트) 여기서는 "값 없음" 표기만 확인합니다.
    """
    run = _run(monkeypatch, tmp_path, orders=[_order(order_type="LIMIT", price=price)])
    assert run.error is None, run.error
    body = _body(run)
    assert "가격 미확정(체결가 미정)" in body
    assert "주문가: $" not in body
    assert "예상 주문금액" not in body
    assert "시장가" not in body


@pytest.mark.parametrize(
    "price", [-3.0, True, float("inf"), float("-inf"), float("nan")]
)
def test_non_finite_or_negative_price_blocks_execution(
    monkeypatch, tmp_path, price
):
    """비유한·음수·불리언 가격은 계획 비교에서 차단되어 주문이 나가지 않습니다.

    회귀: `abs(a-b) > tol` 는 NaN 에서 False 라 NaN 가격이 검증을 통과했습니다.
    """
    run = _run(monkeypatch, tmp_path, orders=[_order(order_type="LIMIT", price=price)])
    assert run.error is not None, "비유한/음수 가격이 검증을 통과하면 안 됩니다"
    message = str(run.error)
    assert "가격이" in message and (
        "유한한 숫자가 아닙니다" in message or "음수입니다" in message
    ), message
    assert run.broker.order_calls == [], "차단 시 주문 API 를 호출하면 안 됩니다"


# ── (6) 1달러 미만 정밀도 ────────────────────────────────────────────
def test_sub_dollar_price_keeps_four_decimals(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path, orders=[_order(order_type="LOC", price=0.9945, quantity=3)])
    assert run.error is None, run.error
    body = _body(run)
    assert "주문가: $0.9945" in body, "1달러 미만 주문가가 반올림/절단되면 안 됩니다"
    assert "예상 주문금액(수수료 제외): $2.98" in body


# ── (7) 일반 접수 vs 예약 접수 ────────────────────────────────────────
def test_normal_order_status_text(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path, orders=[_order()])
    assert run.error is None, run.error
    body = _body(run)
    assert body.startswith("✅ 주문 접수 완료 TQQQ")
    assert f"주문번호: {RAW}" in body
    assert "예약주문번호" not in body
    assert "접수 시각: 2026-10-29 04:10:00" in body
    assert "접수 상태: 체결 여부 미확인" in body
    assert "1회차 매수" in body, "주문 설명(comment) 줄이 유지되어야 합니다"


def test_reservation_order_status_text(monkeypatch, tmp_path):
    run = _run(
        monkeypatch,
        tmp_path,
        orders=[_order()],
        result=OrderResult(
            order_id=RAW, order_time="2026-10-30", is_reservation=True
        ),
    )
    assert run.error is None, run.error
    body = _body(run)
    assert body.startswith("📋 예약주문 접수 TQQQ")
    assert f"예약주문번호: {RAW}" in body
    assert "예약접수일자: 2026-10-30" in body
    assert "접수 상태: 예약 접수 완료, 체결 여부 미확인" in body
    assert "1회차 매수" in body
    # 마스킹된 twin 도 라벨 줄만 가명 토큰으로 바뀌어야 합니다.
    assert RAW not in run.sent[0]["message"]
    assert f"예약주문번호: {log_mask.mask_odno(RAW)}" in run.sent[0]["message"]


def test_body_has_single_labelled_order_id_line(monkeypatch, tmp_path):
    """원문 주문번호는 라벨 줄 1곳에만 등장해야 합니다 (마스킹 twin 이 정확히 동작하도록)."""
    run = _run(monkeypatch, tmp_path, orders=[_order()])
    assert run.error is None, run.error
    body = _body(run)
    assert body.count(RAW) == 1
    assert run.sent[0]["message"].count(RAW) == 0
    assert log_mask.mask_odno(RAW) in run.sent[0]["message"]


# ── (8) 개인 채팅 = 원문 주문번호 ────────────────────────────────────
def test_personal_chat_gets_raw_order_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", CHAT_ID)
    monkeypatch.setenv("QUIET_HOURS", "false")

    run = _run(monkeypatch, tmp_path, orders=[_order()], real_notify=True)
    assert run.error is None, run.error
    assert len(run.sent) == 1
    assert RAW in run.sent[0], "개인 채팅에는 원문 주문번호가 담긴 본문이 가야 합니다"
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream


# ── (9) 그룹·채널 = 가명(ord#) ───────────────────────────────────────
def test_group_channel_gets_masked_order_id(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", GROUP_ID)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", f"{CHAT_ID},{GROUP_ID}")
    monkeypatch.setenv("QUIET_HOURS", "false")

    run = _run(monkeypatch, tmp_path, orders=[_order()], real_notify=True)
    assert run.error is None, run.error
    assert len(run.sent) == 1
    assert RAW not in run.sent[0], "공용 채널에는 원문 주문번호가 가면 안 됩니다"
    assert log_mask.mask_odno(RAW) in run.sent[0]


# ── (10) stdout 에는 원문 주문번호가 없음 ────────────────────────────
def test_stdout_keeps_order_id_masked(monkeypatch, tmp_path, capsys):
    run = _run(monkeypatch, tmp_path, orders=[_order()])
    assert run.error is None, run.error
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream, "공개 로그에 원문 주문번호가 남았습니다"
        assert BOT_TOKEN not in stream
    assert log_mask.mask_odno(RAW) in out.out
    assert run.sent[0]["telegram_message"], "상세 본문은 알림으로만 전달되어야 합니다"


# ── (11) DRY 는 접수 완료 알림을 보내지 않음 ─────────────────────────
def test_dry_mode_sends_no_completion_alert(monkeypatch, tmp_path):
    """DRY 는 승인 실행(EXECUTE) 단계 자체가 거부되므로 접수/예약 알림이 나갈 수 없습니다.

    (파이프라인 불변식: 주문 API 경로는 TRADE_MODE=LIVE + RUN_PHASE=EXECUTE 에서만 열립니다.)
    """
    run = _run(monkeypatch, tmp_path, orders=[_order()], trade_mode="DRY", refresh=False)
    assert isinstance(run.error, RuntimeError)
    # EXECUTE 경계 게이트가 거부한 문구 (pipeline_execute._assert_execute_phase_allowed)
    assert "LIVE" in str(run.error) and "EXECUTE" in str(run.error)
    assert "승인은 소비하지 않았습니다" in str(run.error)
    assert run.sent == [], f"DRY 에서 접수 알림이 발송되었습니다: {run.sent}"
    assert run.broker.order_calls == []
    assert run.saved == []
    assert run.approval_path is None, "거부 시 승인 파일을 만들지 않습니다"


# ── (12) 동작 불변 ───────────────────────────────────────────────────
def test_notification_does_not_change_order_or_fence_state(monkeypatch, tmp_path):
    order = _order(order_type="LOC", price=0.9945, quantity=3)
    before = deepcopy(order)

    run = _run(monkeypatch, tmp_path, orders=[order])
    assert run.error is None, run.error

    # 주문 dict 는 표시 과정에서 절대 변하지 않습니다.
    assert order == before

    # 브로커로 나간 주문 인자는 기존과 동일합니다 (가격 0 처리 포함, 새 호출 없음).
    assert run.broker.order_calls == [
        {
            "symbol": "TQQQ",
            "exchange": "NASD",
            "side": "BUY",
            "quantity": 3,
            "price": 0.9945,
            "order_type": "LOC",
        }
    ]

    # fence 저장 순서 유지: batch 설정 → intent 설정 → intent 해제 → batch 해제
    intents = [snapshot.get("pending_order_intent") for snapshot in run.saved]
    assert intents[0] is None, "주문 batch fence 가 먼저 저장되어야 합니다"
    assert any(intent for intent in intents if intent), "주문 전 intent checkpoint 가 사라졌습니다"
    assert run.saved[-1].get("pending_order_intent") is None
    assert run.saved[-1].get("pending_order_batch") is None

    # orders_meta 키는 원문 주문번호 그대로입니다 (마스킹은 출력 전용).
    assert RAW in run.saved[-1]["orders_meta"]

    # 알림은 접수 1건뿐입니다 (추가 알림 없음).
    assert len(run.sent) == 1


# ── 정적 계약: 상세 줄 도우미는 주문번호를 만들지 않습니다 ─────────────
def test_detail_lines_helper_has_no_order_id():
    """`_order_detail_lines()` 는 표시 문구만 만들고 주문번호를 담지 않습니다."""
    order = _order(price=0.9945)
    before = deepcopy(order)
    lines = trading_bot._order_detail_lines("TQQQ", order)
    assert "주문번호" not in lines
    assert "예약주문번호" not in lines
    assert lines.count("\n") == 7  # 증권사/계좌/종목/구분/유형/수량/주문가/예상금액
    # 도우미에 직접 넘긴 dict 도 변하지 않습니다 (run 경로의 deepcopy 를 우회하는 검증).
    assert order == before
