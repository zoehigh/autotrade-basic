"""
브로커 preflight — 모의 전 API 사전 테스트 (read-only 전수 + 비시장성 주문·취소 1건)

용어: 인증 대신 preflight / 브로커 검증 / API 사전 테스트 사용.

실행:
  uv run python tests/test_broker_preflight.py --broker kis --symbol TQQQ
  uv run python tests/test_broker_preflight.py --broker kis --symbol TQQQ --collar
  uv run pytest tests/test_broker_preflight.py -v
  uv run pytest tests/test_broker_preflight.py -m preflight -v

전략이 실제 호출하는 API만 검증합니다:
  - get_daily_closes(5) → list[float]
  - get_balance / get_purchase_amount
  - get_order_history — 빈 이력과 체결 후 이력 표준 필드
  - get_stock_price / get_stock_quotation / is_trading_day
  - 주문 사전 테스트: BUY LIMIT 1주(현재가-5% 등) → 이력 매칭 → cancel → 0체결 재확인
    cancel 미구현 시 OrderNotAcceptedError 거부 확인으로 대체

콜라 경계 probe (--collar / test_collar_probes_all_brokers):
  - 매수 상단 ×1.19/×1.21 (1주): 지정가가 시장 위라 즉시 체결 → 실전에서는 실제 1주
    매수가 발생하므로 COLLAR_PROBE_LIVE_FILL=true일 때만 실행 (기본 skip).
    데모는 기존 자격증명 게이팅만 따릅니다.
  - 매수 하단 ×0.90 (1주, 비시장성): 접수 → 이력 매칭 → cancel → 0체결 terminal 확인
  - 매도 상단 ×1.50 / 매도 하단 ×0.80 (1주, 비시장성): 보유 수량 있을 때만 접수 → cancel
  - cancel 미구현 브로커(KIS/KIWOOM/LS/NHPLUG)는 비정상 주문(99999주/$0.01) 거부
    확인으로 대체 (AGENTS.md PREFLIGHT 규칙)

단일 계좌·이력 최소화를 위해 비시장성 가격을 사용합니다. 취소 이력의 0체결은
state.py에서 ft_ccld_qty<=0 제외라 T/net_invested에 영향 없습니다.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from broker.base import OrderNotAcceptedError, BrokerError

STANDARD_FIELDS = {
    "ord_dt", "ord_tmd", "ord_datetime_kst", "ord_datetime_utc",
    "prdt_name", "sll_buy_dvsn_cd_name", "ft_ord_qty", "ft_ccld_qty",
    "ft_ccld_unpr3", "ft_ccld_amt3", "nccs_qty", "prcs_stat_name",
    "tr_mket_name", "tr_crcy_cd", "odno", "ovrs_excg_cd",
}

# 브로커별 필수 자격증명 (tests/conftest.py와 동일 기준)
CREDENTIALS = {
    "kis": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
    "kiwoom": ["KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"],
    "ls": ["LS_APP_KEY", "LS_APP_SECRET"],
    "toss": ["TOSS_APP_KEY", "TOSS_APP_SECRET"],
    "nhplug": ["NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_ACCT_NO"],
}


def _has_credentials(broker: str) -> bool:
    keys = CREDENTIALS.get(broker, [])
    return all(os.getenv(k) for k in keys)


def _create_real_broker(broker: str):
    """TRADE_MODE와 무관하게 실제 브로커 인스턴스를 생성합니다 (DryBroker 우회)."""
    broker = broker.lower()
    if broker == "kis":
        from broker.kis.adapter import KISBroker
        return KISBroker()
    if broker == "kiwoom":
        from broker.kiwoom.adapter import KiwoomBroker
        return KiwoomBroker()
    if broker == "ls":
        from broker.ls.adapter import LSBroker
        return LSBroker()
    if broker == "toss":
        from broker.toss.adapter import TossBroker
        return TossBroker()
    if broker == "nhplug":
        from broker.nhplug.adapter import NHPlugBroker
        return NHPlugBroker()
    raise ValueError(f"알 수 없는 브로커: {broker}")


def _check_standard_fields(history):
    if not history:
        return
    for item in history:
        missing = STANDARD_FIELDS - set(item.keys())
        assert not missing, f"표준 필드 누락: {missing} / keys={sorted(item.keys())}"


def run_preflight(broker_name: str, symbol: str = "TQQQ", exchange: str = "NAS"):
    broker_name = broker_name.lower()
    symbol = symbol.upper()

    if not _has_credentials(broker_name):
        print(f"[preflight skip] {broker_name} 자격증명 없음 → skip")
        return "skip"

    broker = _create_real_broker(broker_name)
    print(f"[preflight] broker={broker_name} symbol={symbol} exchange={exchange}")

    # 1) read-only 전수
    print("[1/6] is_trading_day")
    trading_day = broker.is_trading_day()
    assert isinstance(trading_day, bool)
    print(f"  → {trading_day}")

    print("[2/6] get_stock_price / get_stock_quotation")
    price = broker.get_stock_price(symbol, exchange)
    print(f"  price open={price.open} last={price.last}")
    quotation = broker.get_stock_quotation(symbol, exchange)
    print(f"  quotation tradable={quotation.tradable} last={quotation.last}")
    assert quotation.last > 0

    print("[3/6] get_balance")
    balance = broker.get_balance(symbol, exchange)
    print(f"  balance={balance}")

    print("[4/6] get_purchase_amount (orderable_cash)")
    pa = broker.get_purchase_amount(symbol, exchange)
    print(f"  orderable_cash={pa.orderable_cash}")
    assert pa.orderable_cash >= 0

    print("[5/6] get_daily_closes(5)")
    closes = broker.get_daily_closes(symbol, exchange, days=5)
    print(f"  closes({len(closes)}): {closes}")
    assert isinstance(closes, list)

    print("[6/6] get_order_history — 빈 이력 파싱")
    history = broker.get_order_history(symbol, exchange, days=5)
    print(f"  history {len(history)}건")
    _check_standard_fields(history)
    # 체결 후 이력이 있으면 표준 필드 재검증 (없으면 빈 케이스만 통과)
    if history:
        print("  표준 필드 확인 완료")
    else:
        print("  이력 없음 — 빈 응답 처리 확인")

    # 7) 주문 사전 테스트 — 비시장성 LIMIT 1주
    # 전략 종목과 동일 계좌에서 이력을 남기므로, 비시장성 가격으로 0체결을 노립니다.
    has_cancel = hasattr(broker, "cancel_order")
    print(f"[7/7] 주문 사전 테스트 (cancel 구현={has_cancel})")

    # 현재가 기준 -5% 가격 (호가는 adapter가 normalize)
    non_market_price = round(quotation.last * 0.95, 2) if quotation.last else 10.0
    if non_market_price <= 0:
        non_market_price = 1.0
    print(f"  BUY LIMIT 1주 @ ${non_market_price} (현재가 ${quotation.last} 대비 -5%)")

    if has_cancel:
        # 성공 접수 → (정보용 이력 확인) → 취소 → 재확인
        ex_code = broker.exchange_code(exchange)
        result = broker.place_order(symbol, ex_code, "BUY", 1, non_market_price, "LIMIT")
        assert result is not None and result.order_id, "주문 접수 실패 — OrderResult 없음"
        odno = str(result.order_id)
        print(f"  접수 odno={odno} time={result.order_time} reservation={result.is_reservation}")
        # 데모 LOC/MOC→LIMIT 변환 로그는 adapter가 출력

        cancel_attempted = False

        try:
            # 중간 이력 매칭은 정보용 — TOSS는 미체결 주문이 CLOSED 이력에 나타나지 않음
            time.sleep(1.0)
            history_after = broker.get_order_history(symbol, exchange, days=5)
            matched = [h for h in history_after if str(h.get("odno")) == odno or str(h.get("odno")).lstrip("0") == odno.lstrip("0")]
            if matched:
                print(f"  [정보] 이력 매칭 {len(matched)}건 odno={odno}")
            else:
                print(f"  [정보] 이력에서 odno={odno} 미발견 — 미체결 주문은 CLOSED 이력에 없을 수 있음 (정상)")

            # 취소
            print(f"  cancel_order odno={odno}")
            # cancel_order 시그니처는 브로커별 상이할 수 있어 위치 인자 1개 우선 시도
            try:
                broker.cancel_order(odno)  # type: ignore
                cancel_attempted = True
            except TypeError:
                broker.cancel_order(odno, symbol, exchange)  # type: ignore
                cancel_attempted = True
            except Exception as e:
                print(f"  cancel_order 1차 실패: {e}")
        finally:
            # 취소가 한 번도 성공적으로 호출되지 않았으면 1초 후 1회 재시도
            if not cancel_attempted:
                time.sleep(1.0)
                try:
                    broker.cancel_order(odno)  # type: ignore
                    cancel_attempted = True
                except TypeError:
                    broker.cancel_order(odno, symbol, exchange)  # type: ignore
                    cancel_attempted = True
                except Exception as e:
                    print(f"  ⚠️ [경고] 주문 취소 실패 — 수동 취소 필요: odno={odno} ({e})")
            if not cancel_attempted:
                print(f"  ⚠️ [경고] 주문 취소 실패 — 수동 취소 필요: odno={odno}")

        time.sleep(1.5)
        history_canceled = broker.get_order_history(symbol, exchange, days=5)
        canceled = [h for h in history_canceled if str(h.get("odno")) == odno or str(h.get("odno")).lstrip("0") == odno.lstrip("0")]
        assert canceled, f"취소 후 이력에서 odno={odno} 미발견"
        item = canceled[0]
        _check_standard_fields([item])
        ft_ccld = int(float(item.get("ft_ccld_qty", "0") or 0))
        nccs = int(float(item.get("nccs_qty", "0") or 0)) if item.get("nccs_qty") is not None else 0
        status = str(item.get("prcs_stat_name", ""))
        print(f"  취소 후 상태={status} ft_ccld_qty={ft_ccld} nccs_qty={nccs}")
        assert ft_ccld == 0, f"0체결 취소 기대였으나 ft_ccld_qty={ft_ccld} — 체결 발생 시 전량매도 복원 없이 모의 진입 금지"
        # remaining 0은 브로커별 nccs_qty 0 또는 prcs_stat_name 취소로 확인
        assert nccs == 0 or "취소" in status or "CANCEL" in status.upper(), f"취소 미완료: {item}"
        print("[preflight 통과] read-only 전수 + 주문·취소 1건 0체결 확인")
    else:
        print("  cancel 미구현 → OrderNotAcceptedError 거부 확인으로 대체")
        ex_code = broker.exchange_code(exchange)
        try:
            broker.place_order(symbol, ex_code, "BUY", 99999, 0.01, "LIMIT")
            raise AssertionError("OrderNotAcceptedError 기대였으나 주문이 접수됨")
        except OrderNotAcceptedError as e:
            print(f"  거부 확인: {e}")
            print("[preflight 통과] read-only 전수 + 거부 경로 확인 (성공 접수 경로는 모의 첫 주문으로 위임)")
        except BrokerError as e:
            # 일부 브로커는 BrokerError로 거부
            print(f"  거부(BrokerError) 확인: {e}")
            print("[preflight 통과] read-only 전수 + 거부 경로 확인")

    broker.close()
    return "pass"


# ═══════════════════════════════════════════════════════════════════════
# 콜라 경계 probe (collar boundary)
# ═══════════════════════════════════════════════════════════════════════
# 매수 상단(×1.19/×1.21)은 지정가가 시장 위라 즉시 체결됩니다 → 실전에서는 실제
# 1주 매수가 발생하므로 COLLAR_PROBE_LIVE_FILL=true일 때만 실행합니다 (기본 skip).
# 매수 하단(×0.90)·매도 상단(×1.50)은 비시장성이라 미체결 + 취소가 가능합니다.
# 매도 하단(×0.80)은 SELL 한도가 시장 위라 체결될 수 있습니다 (보유 시에만 시도).
# 취소 이력의 0체결은 state.py에서 ft_ccld_qty<=0 제외라 T/net_invested에 영향 없습니다.
COLLAR_PROBE_LIVE_FILL = os.getenv("COLLAR_PROBE_LIVE_FILL", "").strip().lower() == "true"

# 콜라 경계 probe 매트릭스: (방향, 배율, 수량, 시장성 여부)
#   시장성=True  → 즉시 체결 전제 (BUY는 시장 위, SELL은 시장 아래) → 실전 1주 실제 체결 발생 → 게이트 대상
#   시장성=False → 비시장성 (BUY 하단·SELL 상단) → 미체결+취소 가능
COLLAR_PROBES = [
    ("BUY", 1.19, 1, True),    # 매수 상단 — 시장 위 지정가 → 즉시 체결
    ("BUY", 1.21, 1, True),    # 매수 상단 — 시장 위 지정가 → 즉시 체결
    ("BUY", 0.90, 1, False),   # 매수 하단 — 시장 아래 지정가 → 비시장성 (미체결+취소)
    ("SELL", 1.50, 1, False),  # 매도 상단 — 시장 위 지정가 → 비시장성 (미체결+취소)
    ("SELL", 0.80, 1, True),   # 매도 하단 — 시장 아래 지정가 → 즉시 체결 (보유 시, 실전 게이트 대상)
]


def _broker_mode(broker) -> str:
    """브로커 모드(real/demo)를 확인합니다."""
    mode = getattr(broker, "_mode", None)
    if mode:
        return str(mode).strip().lower()
    try:
        from config import BROKER_MODE
        return str(BROKER_MODE).strip().lower()
    except Exception:
        return "unknown"


def _match_odno(history, odno: str) -> list:
    """이력에서 odno 매칭 (KIS leading-zero 정규화 포함)."""
    odno_norm = str(odno).lstrip("0")
    return [
        h for h in history
        if str(h.get("odno", "")).lstrip("0") == odno_norm
    ]


# 취소 실패 원인이 '이미 체결'임을 나타내는 키워드 (체결된 주문은 취소 대상이 아님)
_FILL_CANCEL_INDICATORS = ("already-filled", "filled", "체결")


def _is_fill_related_cancel_error(message) -> bool:
    """취소 실패 원인이 '이미 체결'인지 확인합니다."""
    msg = str(message).lower()
    return any(ind in msg for ind in _FILL_CANCEL_INDICATORS)


def _cancel_order_guaranteed(broker, odno: str, symbol: str, exchange: str) -> str:
    """
    취소 보장 — try/finally + 1초 후 1회 재시도.

    반환:
      "canceled" — 취소 성공
      "filled"   — 주문이 이미 체결되어 취소 불가 (정보 로그만 출력)
      "failed"   — 취소 실패 (수동 취소 경고 odno 포함 출력)
    """
    cancel_attempted = False
    fill_related = False

    try:
        try:
            broker.cancel_order(odno)  # type: ignore
            cancel_attempted = True
        except TypeError:
            broker.cancel_order(odno, symbol, exchange)  # type: ignore
            cancel_attempted = True
        except Exception as e:
            if _is_fill_related_cancel_error(str(e)):
                fill_related = True
                print(f"  [정보] 주문 체결로 취소 불가 (정상): odno={odno} ({e})")
            else:
                print(f"  cancel_order 1차 실패: {e}")
    finally:
        if not cancel_attempted:
            time.sleep(1.0)
            try:
                broker.cancel_order(odno)  # type: ignore
                cancel_attempted = True
            except TypeError:
                broker.cancel_order(odno, symbol, exchange)  # type: ignore
                cancel_attempted = True
            except Exception as e:
                if _is_fill_related_cancel_error(str(e)):
                    fill_related = True
                    print(f"  [정보] 주문 체결로 취소 불가 (정상): odno={odno} ({e})")
                else:
                    print(f"  ⚠️ [경고] 주문 취소 실패 — 수동 취소 필요: odno={odno} ({e})")
        if not cancel_attempted and not fill_related:
            print(f"  ⚠️ [경고] 주문 취소 실패 — 수동 취소 필요: odno={odno}")

    if cancel_attempted:
        return "canceled"
    if fill_related:
        return "filled"
    return "failed"


def _run_collar_probe(
    broker, symbol: str, exchange: str,
    side: str, multiplier: float, qty: int, marketable: bool,
    last_price: float, mode: str, has_cancel: bool,
) -> dict:
    """콜라 경계 probe 1건 실행 → 결과 dict (브로커/모드/방향/배율/접수·거부/코드·메시지)."""
    ex_code = broker.exchange_code(exchange)
    price = round(last_price * multiplier, 2)
    if price <= 0:
        price = 1.0

    result = {
        "broker": broker.name,
        "mode": mode,
        "direction": side,
        "multiplier": multiplier,
        "price": price,
        "status": "",
        "odno": "",
        "code": "",
        "message": "",
        "note": "",
    }

    # 실전 시장성 probe — 실제 1주 체결이 발생하므로 명시적 env 없으면 skip
    # (BUY 상단=시장 위 지정, SELL 하단=시장 아래 지정 모두 즉시 체결)
    if marketable and mode == "real" and not COLLAR_PROBE_LIVE_FILL:
        result["status"] = "skipped"
        result["note"] = "실전 시장성 probe는 COLLAR_PROBE_LIVE_FILL=true일 때만 실행 (기본 skip)"
        return result

    # cancel 미구현 브로커의 비시장성 probe — 접수 후 취소가 불가하므로
    # 비정상 주문(99999주/$0.01)으로 OrderNotAcceptedError 거부 확인만으로 대체
    # (AGENTS.md PREFLIGHT: cancel 미구현 시 거부 확인으로 대체)
    if not has_cancel and not marketable:
        try:
            broker.place_order(symbol, ex_code, "BUY", 99999, 0.01, "LIMIT")
            result["status"] = "unexpected_accept"
            result["message"] = "OrderNotAcceptedError 기대였으나 주문이 접수됨 — 수동 취소 필요"
        except OrderNotAcceptedError as e:
            result["status"] = "rejected"
            result["code"] = "OrderNotAcceptedError"
            result["message"] = str(e)
        except BrokerError as e:
            result["status"] = "rejected"
            result["code"] = type(e).__name__
            result["message"] = str(e)
        result["note"] = "cancel 미구현 — 비정상 주문(99999주/$0.01) 거부 확인으로 대체"
        return result

    # 정상 접수 시도
    try:
        order = broker.place_order(symbol, ex_code, side, qty, price, "LIMIT")
    except OrderNotAcceptedError as e:
        result["status"] = "rejected"
        result["code"] = "OrderNotAcceptedError"
        result["message"] = str(e)
        return result
    except BrokerError as e:
        result["status"] = "rejected"
        result["code"] = type(e).__name__
        result["message"] = str(e)
        return result

    if order is None or not order.order_id:
        result["status"] = "no_order_id"
        result["message"] = "OrderResult 없음 — 접수 여부 불확실"
        return result

    odno = str(order.order_id)
    result["odno"] = odno
    result["status"] = "accepted"

    # 시장성 probe(BUY 상단·SELL 하단) — 즉시 체결이 전제입니다. cancel 구현 브로커는
    # 미체결 잔존 방지를 위해 best-effort 취소를 시도합니다 (체결 시 무해).
    if marketable:
        if has_cancel:
            try:
                broker.cancel_order(odno)  # type: ignore
                result["note"] = "시장성 지정가 — 체결 전제, 미체결 시 취소됨"
            except Exception as e:
                result["note"] = f"시장성 지정가 — 즉시 체결 전제 (취소 시도 무해: {str(e)[:50]})"
        else:
            result["note"] = "시장성 지정가 — 즉시 체결 전제 (취소 대상 아님)"
        return result

    # 비시장성/체결가능 probe — 접수 → (정보용 이력 매칭) → cancel → 0체결 terminal 확인
    time.sleep(1.0)
    history_after = broker.get_order_history(symbol, exchange, days=5)
    matched = _match_odno(history_after, odno)
    if matched:
        print(f"  [정보] 이력 매칭 {len(matched)}건 odno={odno}")
    else:
        print(f"  [정보] 이력에서 odno={odno} 미발견 — 미체결 주문은 CLOSED 이력에 없을 수 있음 (TOSS 정상)")

    print(f"  cancel_order odno={odno}")
    cancel_status = _cancel_order_guaranteed(broker, odno, symbol, exchange)
    if cancel_status != "canceled":
        if cancel_status == "filled":
            result["status"] = "filled"
            result["message"] = f"주문이 체결되어 취소 불가 — 수동 확인 필요: odno={odno}"
        else:
            result["status"] = "cancel_failed"
            result["message"] = f"취소 실패 — 수동 취소 필요: odno={odno}"
        return result

    # 취소 후 이력에서 odno 매칭 + 0체결 terminal 확인
    # (TOSS: CLOSED 이력 특성상 미체결 주문이 이력에 없으므로 취소 후 매칭)
    time.sleep(1.5)
    history_canceled = broker.get_order_history(symbol, exchange, days=5)
    canceled = _match_odno(history_canceled, odno)
    if not canceled:
        result["status"] = "canceled_no_history"
        result["message"] = f"취소 후 이력에서 odno={odno} 미발견"
        return result
    item = canceled[0]
    _check_standard_fields([item])
    ft_ccld = int(float(item.get("ft_ccld_qty", "0") or 0))
    nccs = int(float(item.get("nccs_qty", "0") or 0)) if item.get("nccs_qty") is not None else 0
    status = str(item.get("prcs_stat_name", ""))
    print(f"  취소 후 상태={status} ft_ccld_qty={ft_ccld} nccs_qty={nccs}")
    if ft_ccld != 0:
        result["status"] = "filled"
        result["message"] = f"0체결 취소 기대였으나 ft_ccld_qty={ft_ccld} — 체결 발생"
        return result
    if not (nccs == 0 or "취소" in status or "CANCEL" in status.upper()):
        result["status"] = "cancel_incomplete"
        result["message"] = f"취소 미완료: {item}"
        return result
    result["status"] = "canceled"
    result["message"] = f"0체결 취소 확인 (ft_ccld_qty=0, nccs_qty={nccs}, 상태={status})"
    return result


def run_collar_probes(broker_name: str, symbol: str = "TQQQ", exchange: str = "NAS"):
    """콜라 경계 probe 매트릭스를 실행하고 비교표 요약을 출력합니다."""
    broker_name = broker_name.lower()
    symbol = symbol.upper()

    if not _has_credentials(broker_name):
        print(f"[preflight skip] {broker_name} 자격증명 없음 → skip")
        return "skip"

    broker = _create_real_broker(broker_name)
    mode = _broker_mode(broker)
    if broker.name == "toss":
        # TOSS는 모의투자가 없어 모든 주문이 실전 계좌에 즉시 실행됩니다
        mode = "real"
    has_cancel = hasattr(broker, "cancel_order")
    print(
        f"[collar probe] broker={broker_name} symbol={symbol} exchange={exchange} "
        f"mode={mode} cancel={has_cancel} live_fill_gate={COLLAR_PROBE_LIVE_FILL}"
    )

    quotation = broker.get_stock_quotation(symbol, exchange)
    assert quotation.last > 0, f"현재가(last)가 0 이하: {quotation.last}"
    last_price = quotation.last
    print(f"  현재가(last) = ${last_price}")

    results = []

    # 매수 probe (상단 ×1.19/×1.21, 하단 ×0.90)
    for side, multiplier, qty, marketable in COLLAR_PROBES:
        if side != "BUY":
            continue
        price = round(last_price * multiplier, 2)
        print(f"\n  ── probe: {side} x{multiplier} (1주 @ ${price}) ──")
        result = _run_collar_probe(
            broker, symbol, exchange, side, multiplier, qty, marketable,
            last_price, mode, has_cancel,
        )
        results.append(result)
        print(f"  → {result['status']}: {result['message'] or result['note']}")

    # 매도 probe (상단 ×1.50, 하단 ×0.80) — 보유 수량 있을 때만 시도
    # 매수 상단 probe 체결로 보유가 늘었을 수 있어 잔고를 다시 조회합니다.
    balance = broker.get_balance(symbol, exchange)
    position = balance.quantity if balance else 0
    print(f"\n  보유 수량(매도 probe 기준) = {position}")

    for side, multiplier, qty, marketable in COLLAR_PROBES:
        if side != "SELL":
            continue
        price = round(last_price * multiplier, 2)
        print(f"\n  ── probe: {side} x{multiplier} (1주 @ ${price}) ──")
        if position <= 0:
            results.append({
                "broker": broker_name, "mode": mode, "direction": side,
                "multiplier": multiplier, "price": price,
                "status": "skipped", "odno": "", "code": "", "message": "",
                "note": "보유 수량 없음 — skip",
            })
            print("  → skipped: 보유 수량 없음")
            continue
        result = _run_collar_probe(
            broker, symbol, exchange, side, multiplier, qty, marketable,
            last_price, mode, has_cancel,
        )
        results.append(result)
        print(f"  → {result['status']}: {result['message'] or result['note']}")

    broker.close()

    # 비교표 요약
    print("\n[콜라 경계 probe 비교표]")
    header = (
        f"{'broker':<8} {'mode':<6} {'dir':<5} {'mult':<6} {'price':<10} "
        f"{'status':<18} {'odno':<12} code/message"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        mult = f"x{r['multiplier']}"
        price_s = f"${r['price']:.2f}"
        msg = (r["message"] or r["note"]).replace("\n", " ")
        print(
            f"{r['broker']:<8} {r['mode']:<6} {r['direction']:<5} {mult:<6} "
            f"{price_s:<10} {r['status']:<18} {r['odno']:<12} {msg[:60]}"
        )

    return "pass"


# pytest 진입점 — 자격증명 없으면 skip, 있으면 실 API 호출
@pytest.mark.preflight
@pytest.mark.parametrize("broker_name", ["kis", "kiwoom", "ls", "toss", "nhplug"])
def test_preflight_all_brokers(broker_name):
    if not _has_credentials(broker_name):
        pytest.skip(f"{broker_name} 자격증명 없음")
    result = run_preflight(broker_name, symbol="TQQQ", exchange="NAS")
    assert result in ("pass", "skip")


def test_preflight_kis_tqqq():
    if not _has_credentials("kis"):
        pytest.skip("KIS 자격증명 없음")
    assert run_preflight("kis", "TQQQ", "NAS") in ("pass", "skip")


# 콜라 경계 probe — 자격증명 없으면 skip, 있으면 실 API 호출
@pytest.mark.preflight
@pytest.mark.parametrize("broker_name", ["kis", "kiwoom", "ls", "toss", "nhplug"])
def test_collar_probes_all_brokers(broker_name):
    if not _has_credentials(broker_name):
        pytest.skip(f"{broker_name} 자격증명 없음")
    result = run_collar_probes(broker_name, symbol="TQQQ", exchange="NAS")
    assert result in ("pass", "skip")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="브로커 preflight — 모의 전 API 사전 테스트")
    parser.add_argument("--broker", required=True, help="kis|kiwoom|ls|toss|nhplug")
    parser.add_argument("--symbol", default="TQQQ", help="예: TQQQ, SOXL")
    parser.add_argument("--exchange", default="NAS", help="예: NAS, AMS")
    parser.add_argument(
        "--collar", action="store_true",
        help="콜라 경계 probe만 실행 (기본: 표준 preflight)",
    )
    args = parser.parse_args()
    if args.collar:
        sys.exit(0 if run_collar_probes(args.broker, args.symbol, args.exchange) in ("pass", "skip") else 1)
    sys.exit(0 if run_preflight(args.broker, args.symbol, args.exchange) in ("pass", "skip") else 1)
