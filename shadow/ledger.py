"""섀도우 원장 구현 (v1.1 — 로컬 전용 독립 러너).

실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 전혀 닿지 않는
독립 가상 원장입니다. scripts/shadow_runner.py에서 호출합니다.

⚠️  GH Actions 미지원 — .state.json만 캐시하는 ephemeral 러너입니다.
    실계좌 파생값(수수료율 등)이 포함되므로 artifact를 장기보관하지 마세요.

가정(Assumptions) v1.1:
  A1: 주문은 의도가격(intent_price)에 전량 체결된다
  A2: 체결 비율(fill_ratio)은 항상 1.0이다
  A3: 수수료(fee_usd)는 기본 0.0이나 SHADOW_FEE_RATE로 제어 가능하다
  A4: 주문 거부(rejection)는 없다
  A5: 가상 원장은 실제 state.json과 완전히 독립적이다
  A6: 시장 데이터는 읽기 전용 참조이며 실패 시 무시된다
  B1: 수수료(fee)는 매수·매도 양방향에 fee_rate × qty × price로 부과된다
  B2: 슬리피지(slippage)는 slippage_bps/10000로 가격을 보정한다
  B3: 미체결/부분체결은 없다 (A2와 동일, future 확장용 명시)
  B4: 전량매도(보유=0) 후 T>0이면 사이클 리셋 — 시드=시드+순수익, T/net_invested/avg=0
"""
import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from shadow.broker import VirtualBroker
from strategy import 무한매수법_V4

_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "B3", "B4"]
_ASSUMPTION_VERSION = "v1.1"


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _bootstrap_snapshot(symbol_config):
    """신규 가상 스냅샷을 생성합니다. 실제 state.json은 절대 읽지 않습니다."""
    return {
        "symbol": symbol_config["symbol"],
        "T": 0.0,
        "cash_usd": float(symbol_config["seed"]),
        "holdings": 0,
        "avg_price": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",  # 신규 부트스트랩은 신뢰 가능
        "reverse_mode": {"active": False},
        "close_prices": [],
        "assumption_version": _ASSUMPTION_VERSION,
        "as_of": _utc_now_iso(),
    }


def _load_snapshot(snapshot_dir, symbol_config):
    """최신 스냅샷을 로드하거나 없으면 부트스트랩합니다."""
    symbol = symbol_config["symbol"]
    path = os.path.join(snapshot_dir, "snapshots", f"{symbol}_latest.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            # 누락 키 보정 (이전 버전 스냅샷 호환)
            defaults = _bootstrap_snapshot(symbol_config)
            for key, value in defaults.items():
                snap.setdefault(key, value)
            return snap
        except Exception:
            # 손상된 스냅샷은 부트스트랩으로 대체 (경고만)
            print(f"[shadow] {symbol} 스냅샷 손상 — 새로 시작합니다.")
    return _bootstrap_snapshot(symbol_config)


def _save_snapshot(snapshot_dir, symbol, snapshot):
    """최신 + 일자별 스냅샷을 저장합니다 (일자별은 매일 덮어쓰기)."""
    snap_dir = os.path.join(snapshot_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    snapshot["as_of"] = _utc_now_iso()
    latest_path = os.path.join(snap_dir, f"{symbol}_latest.json")
    dated_path = os.path.join(
        snap_dir,
        f"{symbol}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json",
    )
    for path in (latest_path, dated_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)


def _append_ledger(snapshot_dir, symbol, event):
    """JSONL 원장에 이벤트 1건을 추가합니다."""
    ledger_dir = os.path.join(snapshot_dir, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = os.path.join(ledger_dir, f"{symbol}_{month}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _apply_order(snapshot, order, fee_rate=0.0, slippage_bps=0.0):
    """가상 회계: 주문 1건을 전량 체결 가정(A1/A2)으로 반영합니다.

    v1.1 추가:
      - fee_rate: 매수·매도 양방향 부과 (B1)
      - slippage_bps: 매수는 +, 매도는 - 보정 (B2)
      - qty<=0 또는 price<=0 → 0.0 반환 (변경 없음)

    Returns:
        dict: {
            "t_delta": 이 주문으로 증가한 T,
            "fee_usd": 실제 부과된 수수료,
            "assumed_fill_price": 슬리피지 적용된 체결가,
        }
    """
    result = {"t_delta": 0.0, "fee_usd": 0.0, "assumed_fill_price": 0.0}

    side = order.get("side", "BUY")
    qty = int(order.get("quantity", 0))
    price = float(order.get("price", 0.0) or 0.0)
    if qty <= 0 or price <= 0:
        return result  # 체결 없음

    slip = slippage_bps / 10000.0
    if side == "BUY":
        fill_price = price * (1.0 + slip)
        fee_usd = qty * fill_price * fee_rate
        snapshot["cash_usd"] -= qty * fill_price + fee_usd
        old_holdings = snapshot["holdings"]
        old_avg = snapshot["avg_price"]
        snapshot["holdings"] += qty
        snapshot["net_invested"] += qty * fill_price
        if snapshot["holdings"] > 0:
            snapshot["avg_price"] = round(
                (old_avg * old_holdings + qty * fill_price) / snapshot["holdings"], 4
            )
    elif side == "SELL":
        fill_price = price * (1.0 - slip)
        fee_usd = qty * fill_price * fee_rate
        snapshot["cash_usd"] += qty * fill_price - fee_usd
        snapshot["holdings"] = max(snapshot["holdings"] - qty, 0)
        snapshot["net_invested"] -= qty * fill_price
        if snapshot["holdings"] <= 0:
            snapshot["avg_price"] = 0.0
    else:
        result["assumed_fill_price"] = price
        return result

    result["fee_usd"] = round(fee_usd, 6)
    result["assumed_fill_price"] = round(fill_price, 6)

    # T 갱신: t_target 없으면 추가매수 0 / 일반 1 (trading_bot과 동일 fallback)
    t_target = order.get("t_target")
    if t_target is None:
        is_additional = "[추가매수]" in order.get("comment", "")
        t_target = 0.0 if is_additional else 1.0
    snapshot["T"] = round(float(snapshot["T"]) + float(t_target) * 1.0, 4)
    result["t_delta"] = float(t_target)
    return result


def _maybe_reset_cycle(snapshot, seed, commission_rate):
    """전량매도 후 사이클 리셋 (B4).

    trading_bot.py:569-579와 동일 로직을 shadow 스냅샷에만 적용합니다:
      - 보유량=0 + T>0 → 시드 갱신(seed+순수익), T=0, net_invested=0, avg=0
      - 순수익 = cash_usd - seed (원장에서 누적된 매도-매수-수수료 잔액)
      - cycle_start_date는 초기화하지 않음 (shadow는 추적 안 함)

    Returns:
        bool: 리셋 실행 여부
    """
    if snapshot["holdings"] > 0 or snapshot["T"] <= 0:
        return False

    # 시드 대비 순수익 계산
    profit = snapshot["cash_usd"] - seed
    next_seed = round(seed + profit, 2) if seed > 0 else 0.0

    snapshot["T"] = 0.0
    snapshot["net_invested"] = 0.0
    snapshot["avg_price"] = 0.0
    # cash는 그대로 유지 (다음 사이클의 cash basis)
    # effective_seed는 shadow에 없으므로 cash_usd를 다음 시드 기준으로 리셋
    snapshot["cash_usd"] = round(next_seed, 2) if next_seed > 0 else snapshot["cash_usd"]

    print(
        f"[shadow] 🏁 사이클 리셋 — 순수익=${profit:.2f}, "
        f"다음 시드=${snapshot['cash_usd']:.2f}, T=0"
    )
    return True


def _strategy_output_ref(result):
    """전략 출력의 짧은 참조 문자열 (핵심 필드 해시)."""
    key_fields = {
        "last_price": result.get("last_price"),
        "star_point": result.get("star_point"),
        "orderable_cash": result.get("orderable_cash"),
        "position_qty": result.get("position_qty"),
        "avg_price": result.get("avg_price"),
    }
    raw = json.dumps(key_fields, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def run_shadow_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """섀도우 원장 1회 실행 (로컬 전용 독립 러너).

    ⚠️  GH Actions 미지원 — .state.json만 캐시하는 ephemeral 러너입니다.
        실계좌 파생값(수수료율 등)이 포함되므로 artifact를 장기보관하지 마세요.

    - 실제 state.json / save_state / 주문 / 텔레그램에 절대 닿지 않습니다.
    - 전략은 가상 상태 복사본으로 호출하고, 결과 주문을 fee+slippage 반영
      가상 회계에 반영한 뒤 JSONL 원장 + 스냅샷을 기록합니다.
    - 주문이 없으면 원장 기록은 생략하고 스냅샷 as_of만 갱신합니다.
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    splits = symbol_config["splits"]
    symbol_type = symbol_config["symbol_type"]
    seed = float(symbol_config["seed"])
    additional_loc_levels = symbol_config.get("additional_loc_levels", 3)

    # ── 수수료/슬리피지 파라미터 (환경변수) ──
    default_commission = float(os.getenv("COMMISSION_RATE", "0.0025"))
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", str(default_commission)))
    slippage_bps = float(os.getenv("SHADOW_SLIPPAGE_BPS", "0"))

    # ── 시뮬레이션 경고 ──
    print(
        f"[shadow] ⚠️  SIMULATION ONLY — {symbol} "
        f"({_ASSUMPTION_VERSION}, fee={fee_rate*100:.2f}%, slippage={slippage_bps:.0f}bps)"
    )

    snapshot = _load_snapshot(snapshot_dir, symbol_config)

    # ── 가상 브로커: 전략이 읽는 잔고/주문가능금액을 스냅샷 기준으로 제공 ──
    #    실제 브로커 잔고를 그대로 읽으면 가상 포트폴리오에 없는 주식의
    #    팬텀 매도(무료 가상 현금)가 발생하고 매수가 항상 생략됩니다.
    #    VirtualBroker는 스냅샷 dict를 live 참조하므로 아래 가상 체결 반영이
    #    다음 전략 호출에 즉시 보입니다.
    virtual_broker = VirtualBroker(broker, snapshot)

    # ── 전략 호출 (가상 상태 복사본, T는 스냅샷 기준) ──
    virtual_state = deepcopy(snapshot)
    result = 무한매수법_V4(
        virtual_broker,
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=seed,
        T=float(snapshot["T"]),
        additional_loc_levels=additional_loc_levels,
        state=virtual_state,
    )

    orders = result.get("orders", []) or []
    last_price = float(result.get("last_price", 0.0) or 0.0)

    # ── 시장 스냅샷 (읽기 전용 참조, 실패 시 무시) ──
    market_snapshot = {
        "last_price": last_price,
        "open_price": result.get("open_price"),
        "star_point": result.get("star_point"),
        "orderable_cash": result.get("orderable_cash"),
        "position_qty": result.get("position_qty"),
        "avg_price": result.get("avg_price"),
    }
    try:
        psamount = virtual_broker.get_purchase_amount(symbol, exchange)
        market_snapshot["purchase_amount"] = psamount.orderable_cash
    except Exception:
        market_snapshot["purchase_amount"] = None

    ref = _strategy_output_ref(result)

    # ── 주문별 가상 체결 + 원장 기록 ──
    total_fee = 0.0
    for order in orders:
        qty = int(order.get("quantity", 0))
        price = float(order.get("price", 0.0) or 0.0)
        fill_result = _apply_order(snapshot, order, fee_rate=fee_rate, slippage_bps=slippage_bps)
        t_delta = fill_result["t_delta"]
        fee_usd = fill_result["fee_usd"]
        fill_price = fill_result["assumed_fill_price"]
        total_fee += fee_usd

        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp_utc": _utc_now_iso(),
            "symbol": symbol,
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "intent_price": price,
            "intent_qty": qty,
            "assumed_fill_price": fill_price,
            "fill_ratio": 1.0,            # A2
            "fee_usd": fee_usd,
            "t_target": order.get("t_target"),
            "t_delta": t_delta,
            "assumptions": list(_ASSUMPTIONS),
            "assumption_version": _ASSUMPTION_VERSION,
            "market_snapshot": market_snapshot,
            "virtual_state_after": deepcopy(snapshot),
            "strategy_output_ref": ref,
        }
        # 리버스 필드 passthrough (order에 있으면 그대로 기록)
        for key in (
            "reverse_action", "reverse_day", "reverse_base_t",
            "reverse_t_factor", "reverse_t_target",
        ):
            if order.get(key) is not None:
                event[key] = order[key]

        _append_ledger(snapshot_dir, symbol, event)

    # ── 사이클 리셋 (B4): 전량매도 후 T>0이면 시드 갱신 ──
    cycle_reset = _maybe_reset_cycle(snapshot, seed, fee_rate)

    # ── 스냅샷 저장 (주문이 없어도 as_of 갱신) ──
    _save_snapshot(snapshot_dir, symbol, snapshot)
    print(
        f"[shadow] {symbol} 실행 완료 — 주문 {len(orders)}건, "
        f"수수료=${total_fee:.4f}, T={snapshot['T']}, "
        f"cash=${snapshot['cash_usd']:.2f}, holdings={snapshot['holdings']}"
    )
