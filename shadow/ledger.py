"""섀도우 원장 구현 (v1.2 — 2단계 GENERATE/SETTLE, 로컬 전용 독립 러너).

실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 전혀 닿지 않는
독립 가상 원장입니다. scripts/shadow_runner.py에서 호출합니다.

⚠️  GH Actions 미지원 — .state.json만 캐시하는 ephemeral 러너입니다.
    실계좌 파생값(수수료율 등)이 포함되므로 artifact를 장기보관하지 마세요.

2단계 흐름 (v1.2):
  - generate_symbol(): 전략 호출 → 의도(intent)만 기록 (체결/회계 없음)
  - settle_symbol():   pending 의도를 실제 일봉 종가와 대조해 체결/만료 처리
  - run_shadow_symbol(): generate 래퍼 (v1.1 하위 호환 — 테스트/러너 기본값)

가정(Assumptions) v1.2:
  A1: 주문은 전량 체결(all-or-none)된다 — 부분체결 없음
  A2: 체결 비율(fill_ratio)은 1.0(전량) 또는 0.0(미체결)이다
  A3: 수수료(fee_usd)는 기본 0.0이나 SHADOW_FEE_RATE로 제어 가능하다
  A4: 주문 거부(rejection)는 없다
  A5: 가상 원장은 실제 state.json과 완전히 독립적이다
  A6: 시장 데이터는 읽기 전용 참조이며 실패 시 무시된다
  C1: LIMIT DAY 주문은 close가 limit 조건을 만족하면 의도가격(intent_price)에 체결된다
  (B1 수수료/B2 슬리피지/B4 사이클 리셋 동작은 v1.1과 동일하게 유지 —
   이벤트 assumptions 목록에는 A1-A6+C1만 기록)

체결 규칙 (settle):
  - BUY  LOC: close <= limit 이면 종가 체결
  - SELL LOC: close >= limit 이면 종가 체결
  - MOC:      항상 종가 체결 (TOSS SELL MOC→$0.01 proxy도 MOC로 간주)
  - LIMIT DAY: close가 limit 조건을 만족하면 의도가격 체결 (C1)
  - 미체결은 만료(expire) — 다음 날로 이월하지 않음
"""
import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from shadow.broker import VirtualBroker
from strategy import 무한매수법_V4

_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "C1"]
_ASSUMPTION_VERSION = "v1.2"

_REVERSE_FIELDS = (
    "reverse_action", "reverse_day", "reverse_base_t",
    "reverse_t_factor", "reverse_t_target",
)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _bootstrap_snapshot(symbol_config):
    """신규 가상 스냅샷을 생성합니다. 실제 state.json은 절대 읽지 않습니다."""
    return {
        "symbol": symbol_config["symbol"],
        "schema_version": _ASSUMPTION_VERSION,
        "phase": "settled",  # 대기 의도 없음
        "T": 0.0,
        "cash_usd": float(symbol_config["seed"]),
        "holdings": 0,
        "avg_price": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",  # 신규 부트스트랩은 신뢰 가능
        "reverse_mode": {"active": False},
        "close_prices": [],
        "pending_intents": [],
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


def _decide_fill(order_type, side, intent_price, daily_close):
    """체결 여부/체결가 결정 (v1.2 fill rules).

    Returns:
        tuple[bool, float]: (체결 여부, 체결가). 미체결 시 체결가는 daily_close.
    """
    if order_type == "MOC":
        return True, daily_close  # MOC는 항상 종가 체결
    if order_type == "LOC":
        if side == "BUY":
            return (daily_close <= intent_price), daily_close
        return (daily_close >= intent_price), daily_close
    if order_type == "LIMIT":
        # LIMIT DAY: close가 limit 조건을 만족하면 의도가격 체결 (C1)
        if side == "BUY":
            return (daily_close <= intent_price), intent_price
        return (daily_close >= intent_price), intent_price
    # 그 외 주문 유형(MOO/LOO 등)은 shadow 미지원 → 미체결
    return False, daily_close


def generate_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """1단계 GENERATE: 전략 호출 → 의도(intent)만 기록 (체결/회계 없음).

    - 실제 state.json / save_state / 주문 / 텔레그램에 절대 닿지 않습니다.
    - 전략은 가상 상태 복사본으로 호출하고, 결과 주문을 pending 의도로만
      JSONL 원장 + 스냅샷(pending_intents)에 기록합니다.
    - 주문이 없으면 원장 기록은 생략하고 스냅샷 phase만 갱신합니다.
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
        f"[shadow] ⚠️  SIMULATION ONLY — {symbol} GENERATE "
        f"({_ASSUMPTION_VERSION}, fee={fee_rate*100:.2f}%, slippage={slippage_bps:.0f}bps)"
    )

    snapshot = _load_snapshot(snapshot_dir, symbol_config)

    if snapshot.get("pending_intents"):
        print(
            f"[shadow] {symbol} 기존 대기 의도 {len(snapshot['pending_intents'])}건을 "
            f"새 의도로 대체합니다."
        )

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

    # ── 의도(intent)만 기록 — 체결/회계 없음 ──
    pending = []
    for order in orders:
        qty = int(order.get("quantity", 0))
        price = float(order.get("price", 0.0) or 0.0)
        event_id = uuid.uuid4().hex

        event = {
            "event_id": event_id,
            "timestamp_utc": _utc_now_iso(),
            "event_type": "intent",
            "status": "pending",
            "symbol": symbol,
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "intent_price": price,
            "intent_qty": qty,
            "t_target": order.get("t_target"),
            "assumptions": list(_ASSUMPTIONS),
            "assumption_version": _ASSUMPTION_VERSION,
            "market_snapshot": market_snapshot,
            "virtual_state_before": deepcopy(snapshot),
            "strategy_output_ref": ref,
        }
        # 리버스 필드 passthrough (order에 있으면 그대로 기록)
        for key in _REVERSE_FIELDS:
            if order.get(key) is not None:
                event[key] = order[key]

        _append_ledger(snapshot_dir, symbol, event)

        # pending_intents 최소 필드 (settle에서 사용)
        pending.append({
            "event_id": event_id,
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "intent_price": price,
            "intent_qty": qty,
            "t_target": order.get("t_target"),
            "comment": order.get("comment"),
            **{key: order.get(key) for key in _REVERSE_FIELDS if order.get(key) is not None},
        })

    # ── 스냅샷 저장 (pending 의도 + phase) ──
    snapshot["pending_intents"] = pending
    snapshot["phase"] = "generating" if pending else "settled"
    _save_snapshot(snapshot_dir, symbol, snapshot)
    print(
        f"[shadow] {symbol} GENERATE 완료 — 의도 {len(orders)}건 기록 (체결 대기), "
        f"T={snapshot['T']}, cash=${snapshot['cash_usd']:.2f}, "
        f"holdings={snapshot['holdings']}"
    )
    return snapshot


def settle_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """2단계 SETTLE: pending 의도를 실제 일봉 종가와 대조해 체결/만료 처리.

    - get_daily_closes(days=1) 마지막 값을 일봉 종가로 사용합니다.
    - 체결 규칙은 _decide_fill 참고. 체결 시에만 가상 회계(_apply_order) 적용.
    - 미체결 의도는 만료(expire) — 다음 날로 이월하지 않습니다.
    - 종가를 구할 수 없으면(휴장 등) 의도를 유지하고 종료합니다.
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    seed = float(symbol_config["seed"])

    # ── 수수료/슬리피지 파라미터 (환경변수) ──
    default_commission = float(os.getenv("COMMISSION_RATE", "0.0025"))
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", str(default_commission)))
    slippage_bps = float(os.getenv("SHADOW_SLIPPAGE_BPS", "0"))

    snapshot = _load_snapshot(snapshot_dir, symbol_config)
    pending = snapshot.get("pending_intents", []) or []

    if not pending:
        print(f"[shadow] {symbol} SETTLE — 대기 의도 없음 (스킵)")
        snapshot["phase"] = "settled"
        _save_snapshot(snapshot_dir, symbol, snapshot)
        return snapshot

    # ── 일봉 종가 조회 (days=1 마지막 값) ──
    try:
        closes = broker.get_daily_closes(symbol, exchange, days=1)
    except Exception as e:
        print(f"[shadow] {symbol} SETTLE — 종가 조회 실패, 의도 유지: {e}")
        return snapshot
    if not closes:
        print(f"[shadow] {symbol} SETTLE — 종가 없음 (휴장?), 의도 유지")
        return snapshot
    daily_close = float(closes[-1])

    print(
        f"[shadow] {symbol} SETTLE — 일봉 종가 ${daily_close:.2f}, "
        f"의도 {len(pending)}건"
    )

    total_fee = 0.0
    filled_count = 0
    for intent in pending:
        order_type = intent.get("order_type")
        side = intent.get("side")
        intent_price = float(intent.get("intent_price", 0.0) or 0.0)
        intent_qty = int(intent.get("intent_qty", 0))

        filled, fill_price = _decide_fill(order_type, side, intent_price, daily_close)

        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp_utc": _utc_now_iso(),
            "event_type": "settlement",
            "intent_event_id": intent.get("event_id"),
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "intent_price": intent_price,
            "intent_qty": intent_qty,
            "daily_close": daily_close,
            "filled": filled,
            "assumptions": list(_ASSUMPTIONS),
            "assumption_version": _ASSUMPTION_VERSION,
        }
        # 리버스 필드 passthrough
        for key in _REVERSE_FIELDS:
            if intent.get(key) is not None:
                event[key] = intent[key]

        if filled:
            # 체결: 가상 회계 적용 (fill_price 기준, B1 수수료 + B2 슬리피지)
            order = {
                "side": side,
                "quantity": intent_qty,
                "price": fill_price,
                "order_type": order_type,
                "comment": intent.get("comment"),
                "t_target": intent.get("t_target"),
            }
            for key in _REVERSE_FIELDS:
                if intent.get(key) is not None:
                    order[key] = intent[key]
            fill_result = _apply_order(
                snapshot, order, fee_rate=fee_rate, slippage_bps=slippage_bps
            )
            event["fill_price"] = fill_result["assumed_fill_price"]
            event["fill_qty"] = intent_qty
            event["fill_ratio"] = 1.0            # A2
            event["fee_usd"] = fill_result["fee_usd"]
            event["t_delta"] = fill_result["t_delta"]
            total_fee += fill_result["fee_usd"]
            filled_count += 1
        else:
            # 미체결 → 만료 (상태 변화 없음)
            event["fill_price"] = None
            event["fill_qty"] = 0
            event["fill_ratio"] = 0.0
            event["fee_usd"] = 0.0
            event["t_delta"] = 0.0

        event["virtual_state_after"] = deepcopy(snapshot)
        _append_ledger(snapshot_dir, symbol, event)

    # ── pending 초기화 + 사이클 리셋 (B4) ──
    snapshot["pending_intents"] = []
    snapshot["phase"] = "settled"
    _maybe_reset_cycle(snapshot, seed, fee_rate)

    _save_snapshot(snapshot_dir, symbol, snapshot)
    print(
        f"[shadow] {symbol} SETTLE 완료 — 체결 {filled_count}/{len(pending)}건, "
        f"수수료=${total_fee:.4f}, T={snapshot['T']}, "
        f"cash=${snapshot['cash_usd']:.2f}, holdings={snapshot['holdings']}"
    )
    return snapshot


def run_shadow_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """GENERATE 래퍼 (v1.1 하위 호환 — 테스트/러너 기본값).

    v1.2부터는 generate_symbol()/settle_symbol() 2단계로 분리되었습니다.
    이 함수는 1단계(GENERATE)만 수행합니다.
    """
    return generate_symbol(broker, symbol_config, snapshot_dir)