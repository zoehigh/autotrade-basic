"""섀도우 원장 구현 (v1.4 — 2단계 GENERATE/SETTLE, 로컬 전용 독립 러너).

실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 전혀 닿지 않는
독립 가상 원장입니다. scripts/shadow_runner.py에서 호출합니다.

⚠️  GH Actions 미지원 — .state.json만 캐시하는 ephemeral 러너입니다.
    실계좌 파생값(수수료율 등)이 포함되므로 artifact를 장기보관하지 마세요.

2단계 흐름 (v1.4):
  - generate_symbol(): 전략 호출 → 의도(intent)만 기록 (체결/회계 없음)
  - settle_symbol():   pending 의도를 **세션 날짜가 일치하는** 일봉 종가와
                       대조해 체결/만료 처리 (최신 종가 추정 금지)
  - run_shadow_symbol(): generate 래퍼 (v1.1 하위 호환 — 테스트/러너 기본값)

가정(Assumptions) v1.4:
  A1: 주문은 전량 체결(all-or-none)된다 — 부분체결 없음
  A2: 체결 비율(fill_ratio)은 1.0(전량) 또는 0.0(미체결)이다
  A3: 수수료(fee_usd)는 COMMISSION_RATE(기본 0.0025)를 따르며 SHADOW_FEE_RATE로 제어 가능하다
  A4: 주문 거부(rejection)는 없다
  A5: 가상 원장은 실제 state.json과 완전히 독립적이다
  A6: 시장 데이터는 읽기 전용 참조이며 실패 시 무시된다
  C1: LIMIT DAY 주문은 close가 limit 조건을 만족하면 의도가격(intent_price)에 체결된다
  E1: BUY LOC 주문가는 현재가 ×1.19 상한을 넘지 않는다 (trading_bot과 동일 보정)
  S1: 정산(session-dated settle) — 의도에 기록된 session_date가 마감되었고
      브로커 일봉의 date가 그 session_date와 정확히 일치할 때만 체결/만료를
      확정한다. 날짜 없는 캔들/최신 캔들로 대체하지 않는다 (미정산 유지).
  R1: 리버스 연속성(reverse continuity) — reverse_mode.active가 영속되면
      T가 splits-1 미만으로 내려가도 리버스 시퀀스를 유지한다. 전략의
      reverse_exit/전량매도/명시적 정리에서만 해제한다.
  (B1 수수료/B2 슬리피지/B4 사이클 리셋 동작은 v1.1과 동일하게 유지 —
   이벤트 assumptions 목록에는 A1-A6+C1+E1+S1+R1을 기록)

v1.4 신규 규칙:
  - 세션 날짜 기반 정산(S1): generate가 us_session_date_now()의 세션 날짜를
    의도/스냅샷에 기록하고, settle은 us_session_closed(session_date)와
    get_daily_candles()의 동일 date 캔들에서만 체결을 확정한다.
  - 이미 마감된 세션에는 generate하지 않는다 (휴장일 = 직전 세션 반환 대비).
    마감 세션에 generate하면 다음 settle이 과거 종가와 매칭될 수 있기 때문.
  - 리버스 연속성(R1): reverse_mode를 스냅샷에 영속화하고 전략에 그대로
    전달해 다음 세션의 reverse_day가 이어지도록 한다.
  - 미정산 pending 보호: pending이 있으면 generate를 건너뛴다.
    SHADOW_FORCE_NEW_INTENTS=true이면 기존 pending에 expired 이벤트를 남기고
    새 의도를 만든다.
  - 멱등 정산/원자적 스냅샷: 의도별로 회계 적용 → pending에서 제거 →
    스냅샷 원자적 저장(os.replace) → 정산 이벤트 추가. 스냅샷이 권위이며
    JSONL 원장은 projection이다 (두 단계 사이 crash 시 원장에 gap 가능).
    **저장 → 원장 추가 순서를 의도적으로 유지한다.** 원장을 먼저 쓰면
    crash 시 pending이 남아 다음 settle이 회계를 두 번 적용할 수 있다.
    이 순서는 at-most-once 회계를 보장하고, 대신 원장 gap 가능성을 감수한다.
  - 사이클 리셋 crash-safe: 전량매도 등 사이클 종료가 감지되면 per-intent
    체크포인트 스냅샷에 `cycle_reset_pending` 마커를 함께 저장하고,
    settle 시작 시(그리고 정산 루프 종료 후) 마커/스냅샷 상태를 보고 리셋을
    멱등 적용한다. 리셋 1회당 `cycle_reset` 원장 이벤트를 남긴다.
  - 손상된 스냅샷은 조용히 새로 시작하지 않는다. 읽기 실패 시
    ShadowSnapshotCorruptError로 중단하며, 운영자가 명시적으로
    SHADOW_RESET_ON_CORRUPT=true를 준 경우에만 fresh 시작 +
    `snapshot_reset` 원장 이벤트를 기록한다.
  - REINVEST=false이면 사이클 시드로 config seed를 사용한다 (effective_seed
    복리 미적용). 가상 cash는 시뮬레이션 결과를 그대로 반영한다.
  - 스냅샷 assumption_version이 현재 버전과 다르면 자동 마이그레이션 없이
    명확한 오류로 중단한다 (측정 기준 혼합 방지).
  - 운영 타이밍: GENERATE는 미국 장 마감 **전**(프리장)에, SETTLE은 대상
    세션 마감 **후**에 실행한다. generate가 마감 후에 돌면 가드(C)가 그
    세션을 건너뛰어 그날 의도가 생성되지 않는다. pending이 2세션 이상
    남으면 날짜 일치 캔들을 못 찾은 것이므로 API 지연/휴장 갭/세션 날짜
    불일치를 점검한다.

v1.3 정확도(P0) 수정 — 실제 봇(state.py/trading_bot.py) 규칙과 일치:
  P0-1: 일반 매도 T 감소는 t_target이 아니라 보유수량 비율 기준
        (비율 ≥1.0 전량 → T=0, ≥0.5 목표 → ×0.25, <0.5 쿼터 → ×0.75 —
         state.py:1318-1350와 동일)
  P0-2: 리버스 주문은 reverse_base_t/reverse_t_factor/reverse_t_target 기준
        (state.py reconcile_reverse_fills와 동일, 일반 규칙보다 우선)
  P0-3: BUY LOC 가격을 현재가 ×1.19 상한으로 보정 (E1)
  P0-4: 사이클 리셋은 effective_seed 기준 — 스냅샷에 영속화하고 cash_usd는 불변

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

from broker.market_utils import us_session_closed, us_session_date_now
from shadow.broker import VirtualBroker
from strategy import adjust_price_to_tick, 무한매수법_V4

_ASSUMPTIONS = ["A1", "A2", "A3", "A4", "A5", "A6", "C1", "E1", "S1", "R1"]
_ASSUMPTION_VERSION = "v1.4"

_REVERSE_FIELDS = (
    "reverse_action", "reverse_day", "reverse_base_t",
    "reverse_t_factor", "reverse_t_target",
)

# 정산 시 조회할 일봉 수 — 세션 날짜 매칭을 위해 여유 있게 조회
_SETTLE_CANDLE_DAYS = 10


class ShadowVersionMismatchError(RuntimeError):
    """스냅샷 가정 버전이 현재 코드와 달라 자동 마이그레이션을 거부할 때."""


class ShadowSnapshotCorruptError(RuntimeError):
    """스냅샷 파일을 읽을 수 없어 조용한 재시작을 거부할 때."""


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _reinvest_enabled():
    """REINVEST 환경변수 (기본 true, src/config.py와 동일한 해석)."""
    raw = (os.getenv("REINVEST") or "true").strip().lower()
    return raw == "true"


def _bootstrap_snapshot(symbol_config):
    """신규 가상 스냅샷을 생성합니다. 실제 state.json은 절대 읽지 않습니다."""
    return {
        "symbol": symbol_config["symbol"],
        "schema_version": _ASSUMPTION_VERSION,
        "phase": "settled",  # 대기 의도 없음
        "T": 0.0,
        "cash_usd": float(symbol_config["seed"]),
        "effective_seed": float(symbol_config["seed"]),  # P0-4: 사이클 리셋 기준 시드
        "holdings": 0,
        "avg_price": 0.0,
        "net_invested": 0.0,
        "net_invested_status": "valid",  # 신규 부트스트랩은 신뢰 가능
        "reverse_mode": {"active": False},
        "close_prices": [],
        "pending_intents": [],
        "session_date": None,  # 마지막 generate 기준 미국 세션 날짜
        "cycle_reset_pending": False,  # 전량매도 후 리셋 미적용 마커
        "assumption_version": _ASSUMPTION_VERSION,
        "as_of": _utc_now_iso(),
    }


def _load_snapshot(snapshot_dir, symbol_config):
    """최신 스냅샷을 로드하거나 없으면 부트스트랩합니다.

    가정 버전(assumption_version)이 현재 코드와 다르면 자동 마이그레이션하지
    않고 명확한 오류로 중단합니다. 버전 간에는 측정 기준(체결 규칙/리버스
    연속성 등)이 달라 수치를 섞으면 해석이 불가능해지기 때문입니다.
    """
    symbol = symbol_config["symbol"]
    path = os.path.join(snapshot_dir, "snapshots", f"{symbol}_latest.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            if not isinstance(snap, dict):
                raise ValueError("스냅샷 JSON이 객체(dict)가 아닙니다")
        except Exception as exc:
            # 손상된 스냅샷을 조용히 새로 시작하면 측정 연속성이 사라진다.
            # 운영자가 명시적으로 허용한 경우에만 fresh 시작한다.
            force_reset = (
                (os.getenv("SHADOW_RESET_ON_CORRUPT") or "").strip().lower()
                == "true"
            )
            if not force_reset:
                raise ShadowSnapshotCorruptError(
                    f"[shadow] {symbol} 스냅샷을 읽을 수 없습니다: {path} "
                    f"({exc}). 자동으로 새로 시작하면 기존 측정 연속성이 "
                    f"조용히 사라지므로 중단합니다. 파일을 보관(archive)/점검한 "
                    f"뒤 복구하거나, 의도적으로 새로 시작하려면 "
                    f"SHADOW_RESET_ON_CORRUPT=true로 1회 실행하세요 "
                    f"(이 경우 snapshot_reset 이벤트가 원장에 기록됩니다)."
                ) from exc
            print(
                f"[shadow] ⚠️ {symbol} 손상된 스냅샷을 "
                f"SHADOW_RESET_ON_CORRUPT=true로 새로 시작합니다: {exc}"
            )
            fresh = _bootstrap_snapshot(symbol_config)
            # 손실 사실을 원장에 남긴다 (스냅샷이 권위, 원장은 projection).
            _save_snapshot(snapshot_dir, symbol, fresh)
            _append_ledger(snapshot_dir, symbol, {
                "event_id": uuid.uuid4().hex,
                "timestamp_utc": _utc_now_iso(),
                "event_type": "snapshot_reset",
                "status": "reset",
                "symbol": symbol,
                "snapshot_path": path,
                "reason": "SHADOW_RESET_ON_CORRUPT",
                "error": str(exc)[:500],
                "assumptions": list(_ASSUMPTIONS),
                "assumption_version": _ASSUMPTION_VERSION,
            })
            return fresh

        loaded_version = snap.get("assumption_version")
        if loaded_version != _ASSUMPTION_VERSION:
            raise ShadowVersionMismatchError(
                f"[shadow] {symbol} 스냅샷 가정 버전 불일치: "
                f"저장={loaded_version!r}, 현재={_ASSUMPTION_VERSION!r}. "
                f"자동 마이그레이션하지 않습니다 — 계속하면 서로 다른 측정 기준이 "
                f"섞여 수치 해석이 불가능해집니다. "
                f"기존 스냅샷을 보관(archive)한 뒤 `{snapshot_dir}` 디렉터리를 "
                f"삭제하고 새로 시작하세요."
            )

        # 누락 키 보정 (같은 버전 내 하위 호환)
        defaults = _bootstrap_snapshot(symbol_config)
        for key, value in defaults.items():
            snap.setdefault(key, value)
        return snap
    return _bootstrap_snapshot(symbol_config)


def _write_json_atomic(path, payload):
    """같은 디렉터리의 임시 파일에 쓴 뒤 os.replace로 원자적 교체합니다."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _save_snapshot(snapshot_dir, symbol, snapshot):
    """최신 + 일자별 스냅샷을 원자적으로 저장합니다 (일자별은 덮어쓰기)."""
    snap_dir = os.path.join(snapshot_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    snapshot["as_of"] = _utc_now_iso()
    latest_path = os.path.join(snap_dir, f"{symbol}_latest.json")
    dated_path = os.path.join(
        snap_dir,
        f"{symbol}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json",
    )
    for path in (latest_path, dated_path):
        _write_json_atomic(path, snapshot)


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

    v1.3 (P0 정확도 수정):
      - P0-1 일반 매도: 보유수량 비율 기준 T 감소 (state.py:1318-1350와 동일)
      - P0-2 리버스 매도/매수: reverse_* 필드 기준 — 일반 규칙보다 우선
        (state.py reconcile_reverse_fills와 동일)
      - t_delta: 전후 T 증감(부호 있음, 소수 4자리)
      - full_sell: 전량매도(비율 ≥1.0) 감지 → settle의 사이클 리셋 트리거

    Returns:
        dict: {
            "t_delta": 이 주문으로 변한 T (부호 있음, 소수 4자리),
            "full_sell": 전량매도 여부 (매도 비율 >= 1.0),
            "fee_usd": 실제 부과된 수수료,
            "assumed_fill_price": 슬리피지 적용된 체결가,
        }
    """
    result = {
        "t_delta": 0.0, "full_sell": False,
        "fee_usd": 0.0, "assumed_fill_price": 0.0,
    }

    side = order.get("side", "BUY")
    qty = int(order.get("quantity", 0))
    price = float(order.get("price", 0.0) or 0.0)
    if qty <= 0 or price <= 0:
        return result  # 체결 없음

    # P0-1 비율 판정용: 매도 반영 "전" 보유수량과 T
    holdings_before = int(snapshot.get("holdings", 0) or 0)
    old_T = float(snapshot.get("T", 0.0))

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

    # ── T 갱신 (P0-1/P0-2) ──
    reverse_action = order.get("reverse_action")
    if side == "SELL" and reverse_action == "sell":
        # P0-2: 리버스 매도 → T − base×(1−factor)×fill_ratio(=1.0, A1/A2)
        # (state.py reconcile_reverse_fills:469와 동일, 일반 비율 규칙보다 우선)
        base_t = order.get("reverse_base_t")
        base_t = old_T if base_t is None else float(base_t)
        factor = order.get("reverse_t_factor")
        factor = 1.0 if factor is None else float(factor)
        snapshot["T"] = round(max(old_T - base_t * (1.0 - factor), 0.0), 4)
    elif side == "BUY" and reverse_action == "buy":
        # P0-2: 리버스 쿼터매수 → T + reverse_t_target × fill_ratio(=1.0, A1/A2)
        # (state.py reconcile_reverse_fills:482와 동일, t_target 폴백 미사용)
        reverse_t_target = order.get("reverse_t_target")
        reverse_t_target = (
            0.0 if reverse_t_target is None else float(reverse_t_target)
        )
        snapshot["T"] = round(old_T + reverse_t_target, 4)
    elif side == "SELL":
        # P0-1: 일반 매도 — 보유수량 비율 기준 (state.py:1318-1350와 동일)
        #   전량(비율 ≥1.0) → T=0(사이클 종료) / 목표(≥0.5) → ×0.25 / 쿼터(<0.5) → ×0.75
        # t_target(0.9 등)은 매수 전용 — 매도에는 적용하지 않음
        if holdings_before > 0:
            ratio = qty / holdings_before
            if ratio >= 1.0:
                snapshot["T"] = 0.0
                result["full_sell"] = True
            elif ratio >= 0.5:
                snapshot["T"] = round(old_T * 0.25, 4)
            else:
                snapshot["T"] = round(old_T * 0.75, 4)
        else:
            # 보유수량 불명 → 쿼터매도 (state.py:1346-1349)
            snapshot["T"] = round(old_T * 0.75, 4)
    else:
        # 일반 BUY — t_target 없으면 추가매수 0 / 일반 1 (trading_bot과 동일 fallback)
        t_target = order.get("t_target")
        if t_target is None:
            is_additional = "[추가매수]" in order.get("comment", "")
            t_target = 0.0 if is_additional else 1.0
        snapshot["T"] = round(old_T + float(t_target), 4)

    snapshot["T"] = round(float(snapshot["T"]), 4)
    result["t_delta"] = round(float(snapshot["T"]) - old_T, 4)
    return result


def _maybe_reset_cycle(snapshot, seed, commission_rate, cycle_completed=False,
                       reinvest=True):
    """전량매도 후 사이클 리셋 (B4, v1.3 P0-4, v1.4 REINVEST).

    trading_bot.py 사이클 종료 로직과 동일하게 shadow 스냅샷에만 적용합니다:
      - 보유량=0 + (T>0 또는 전량매도 완료) → effective_seed 갱신,
        T=0, net_invested=0, avg=0, reverse_mode 해제
      - 순수익 = cash_usd − effective_seed (원장 누적 매도-매수-수수료 잔액)
      - cash_usd는 건드리지 않음 (다음 사이클의 cash basis 유지)
      - cycle_start_date는 초기화하지 않음 (shadow는 추적 안 함)
      - REINVEST=false이면 다음 사이클 시드를 config seed로 되돌린다
        (가상 cash는 시뮬레이션 결과 그대로 유지).

    Args:
        cycle_completed: 이 정산 배치에서 전량매도(비율 ≥1.0)가 있었는지
            (_apply_order의 full_sell 플래그). P0-1 전량매도는 T를 0으로
            만드므로 T>0 조건만으로는 리셋이 누락되어 이 플래그가 필요.
        reinvest: REINVEST 환경변수 해석값. False면 복리 시드 미적용.

    Returns:
        bool: 리셋 실행 여부
    """
    if snapshot["holdings"] > 0:
        return False
    if not (float(snapshot.get("T", 0.0)) > 0 or cycle_completed):
        return False

    # P0-4: 수익 기준은 effective_seed (config seed 아님)
    effective_seed = float(snapshot.get("effective_seed") or seed)
    profit = snapshot["cash_usd"] - effective_seed
    if reinvest:
        next_seed = round(effective_seed + profit, 2) if effective_seed > 0 else 0.0
    else:
        # REINVEST=false: 다음 사이클은 설정 시드에서 다시 시작
        next_seed = round(float(seed), 2)

    snapshot["T"] = 0.0
    snapshot["net_invested"] = 0.0
    snapshot["avg_price"] = 0.0
    snapshot["effective_seed"] = next_seed
    # 전량매도 = 사이클 종료 → 리버스 상태도 명시적으로 정리
    snapshot["reverse_mode"] = {"active": False}
    # cash_usd는 그대로 유지 (다음 사이클의 cash basis)

    print(
        f"[shadow] 🏁 사이클 리셋 — 순수익=${profit:.2f}, "
        f"다음 시드=${next_seed:.2f} (REINVEST={reinvest}), T=0"
    )
    return True


def _cycle_reset_needed(snapshot):
    """사이클 리셋이 필요한 상태인지 판단합니다 (멱등 판정).

    - `cycle_reset_pending` 마커가 있으면 필요.
    - 마커가 없어도 보유 0 + (T>0 또는 net_invested>0)이면 사이클 종료 상태.
    """
    if snapshot.get("cycle_reset_pending"):
        return True
    if int(snapshot.get("holdings", 0) or 0) > 0:
        return False
    return (
        float(snapshot.get("T", 0.0) or 0.0) > 0
        or float(snapshot.get("net_invested", 0.0) or 0.0) > 0
    )


def _apply_cycle_reset_if_needed(snapshot, seed, fee_rate, reinvest):
    """필요하면 사이클 리셋을 1회 적용하고 `cycle_reset` 이벤트를 반환합니다.

    스냅샷을 저장/원장 추가하지 않는다 — 호출 측이 저장 → 원장 순서를 지킨다.
    리셋이 이미 적용된 상태(마커 없음, T=0, net=0)에서는 None을 반환하므로
    반복 호출해도 effective_seed가 두 번 복리되지 않는다.
    """
    if not _cycle_reset_needed(snapshot):
        return None
    # 보유가 남아 있으면 사이클 종료가 아니다 (마커는 유지해 나중에 재판정).
    if int(snapshot.get("holdings", 0) or 0) > 0:
        return None

    marker = bool(snapshot.get("cycle_reset_pending"))
    # 마커 없는 "상태 기반" 리셋은 미정산 pending이 없을 때만 신뢰한다.
    # (예: 리버스 쿼터매수 직전 T>0/holdings=0 상태를 조기 리셋하지 않도록)
    if not marker and snapshot.get("pending_intents"):
        return None
    effective_seed_before = float(snapshot.get("effective_seed", 0.0) or 0.0)
    snapshot["cycle_reset_pending"] = False
    applied = _maybe_reset_cycle(
        snapshot, seed, fee_rate,
        cycle_completed=True,  # 마커/상태 기반 강제 판정
        reinvest=reinvest,
    )
    if not applied:
        return None

    return {
        "event_id": uuid.uuid4().hex,
        "timestamp_utc": _utc_now_iso(),
        "event_type": "cycle_reset",
        "status": "applied",
        "symbol": snapshot.get("symbol"),
        "trigger": "marker" if marker else "state",
        "effective_seed_before": round(effective_seed_before, 6),
        "effective_seed_after": round(
            float(snapshot.get("effective_seed", 0.0) or 0.0), 6
        ),
        "assumptions": list(_ASSUMPTIONS),
        "assumption_version": _ASSUMPTION_VERSION,
        "virtual_state_after": deepcopy(snapshot),
    }


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
    """체결 여부/체결가 결정 (v1.3 fill rules).

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

    v1.4 규칙:
      - session_date = us_session_date_now()를 스냅샷/의도에 기록한다.
      - 이미 마감된 세션(us_session_closed=True)에는 generate하지 않는다
        (휴장일에 직전 세션 날짜가 반환되는 경우 과거 종가와 매칭되는 것을 방지).
      - 미정산 pending이 있으면 건너뛴다. SHADOW_FORCE_NEW_INTENTS=true이면
        기존 pending에 expired 이벤트를 남기고 새 의도를 만든다.
      - 리버스 주문이 나오면 reverse_mode를 스냅샷에 영속화한다(R1).

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
    reinvest = _reinvest_enabled()

    # ── 수수료/슬리피지 파라미터 (환경변수) ──
    default_commission = float(os.getenv("COMMISSION_RATE", "0.0025"))
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", str(default_commission)))
    slippage_bps = float(os.getenv("SHADOW_SLIPPAGE_BPS", "0"))

    # ── 시뮬레이션 경고 ──
    print(
        f"[shadow] ⚠️  SIMULATION ONLY — {symbol} GENERATE "
        f"({_ASSUMPTION_VERSION}, fee={fee_rate*100:.2f}%, "
        f"slippage={slippage_bps:.0f}bps, reinvest={reinvest})"
    )

    snapshot = _load_snapshot(snapshot_dir, symbol_config)

    # ── 세션 날짜 확정 (S1) ──
    session_date = us_session_date_now()
    if not session_date:
        print(
            f"[shadow] {symbol} GENERATE — 미국 세션 날짜를 확정할 수 없음 "
            f"→ 상태 변경 없이 건너뜁니다."
        )
        return snapshot

    # ── (C) 이미 마감된 세션에는 생성 금지 ──
    if us_session_closed(session_date):
        print(
            f"[shadow] {symbol} GENERATE — 세션 {session_date}은 이미 마감됨 "
            f"(휴장일/장 종료 후) → 새 의도를 만들지 않고 상태를 유지합니다."
        )
        return snapshot

    # ── (D) 미정산 pending 보호 ──
    existing_pending = list(snapshot.get("pending_intents") or [])
    if existing_pending:
        force_new = os.getenv(
            "SHADOW_FORCE_NEW_INTENTS", ""
        ).strip().lower() in ("1", "true", "yes", "on")
        if not force_new:
            print(
                f"[shadow] {symbol} GENERATE — 미정산 대기 의도 "
                f"{len(existing_pending)}건이 있어 건너뜁니다 "
                f"(settle 먼저 실행; 강제하려면 SHADOW_FORCE_NEW_INTENTS=true)."
            )
            return snapshot
        for old in existing_pending:
            _append_ledger(snapshot_dir, symbol, {
                "event_id": uuid.uuid4().hex,
                "timestamp_utc": _utc_now_iso(),
                "event_type": "intent_expired",
                "status": "expired",
                "intent_event_id": old.get("event_id"),
                "symbol": symbol,
                "session_date": old.get("session_date"),
                "reason": "SHADOW_FORCE_NEW_INTENTS",
                "assumptions": list(_ASSUMPTIONS),
                "assumption_version": _ASSUMPTION_VERSION,
            })
        print(
            f"[shadow] {symbol} GENERATE — SHADOW_FORCE_NEW_INTENTS: 기존 대기 의도 "
            f"{len(existing_pending)}건을 expired로 기록하고 새 의도를 생성합니다."
        )

    # ── crash 복구: 리셋 마커가 남아 있으면 generate 전에 먼저 적용 ──
    #    (settle crash 후 generate가 먼저 돌아도 낡은 effective_seed로
    #     주문 크기를 계산하지 않도록 한다. 마커가 있을 때만 동작.)
    if snapshot.get("cycle_reset_pending"):
        reset_event = _apply_cycle_reset_if_needed(
            snapshot, seed, fee_rate, reinvest
        )
        if reset_event:
            _save_snapshot(snapshot_dir, symbol, snapshot)
            _append_ledger(snapshot_dir, symbol, reset_event)
            print(
                f"[shadow] {symbol} GENERATE — 이전 settle의 사이클 리셋을 "
                f"복구했습니다 (seed=${reset_event['effective_seed_after']:.2f})"
            )

    # P0-4: REINVEST=true면 전략 시드는 effective_seed 기준 (사이클 리셋 후 이월).
    # REINVEST=false면 설정 시드(config seed)에서 사이클을 시작한다.
    effective_seed = float(snapshot.get("effective_seed") or seed)
    strategy_seed = effective_seed if reinvest else seed

    # ── 리버스 상태 정리 (보유 0 + active면 전략 진입 불가 → 해제) ──
    #    pending이 있는 동안에는 절대 해제하지 않는다 — 보유 0은 정산이
    #    끝난 뒤에만 신뢰할 수 있다 (미정산 매도가 있을 수 있음).
    if (
        not existing_pending
        and snapshot.get("holdings", 0) <= 0
        and (snapshot.get("reverse_mode") or {}).get("active")
    ):
        print(f"[shadow] {symbol} 보유 0 + 리버스 활성 → 리버스 상태를 해제합니다.")
        snapshot["reverse_mode"] = {"active": False}

    snapshot["session_date"] = session_date

    # ── 가상 브로커: 전략이 읽는 잔고/주문가능금액을 스냅샷 기준으로 제공 ──
    #    실제 브로커 잔고를 그대로 읽으면 가상 포트폴리오에 없는 주식의
    #    팬텀 매도(무료 가상 현금)가 발생하고 매수가 항상 생략됩니다.
    #    VirtualBroker는 스냅샷 dict를 live 참조합니다.
    virtual_broker = VirtualBroker(broker, snapshot)

    # ── 전략 호출 (가상 상태 복사본, T/reverse_mode는 스냅샷 기준) ──
    virtual_state = deepcopy(snapshot)
    result = 무한매수법_V4(
        virtual_broker,
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=strategy_seed,
        T=float(snapshot["T"]),
        additional_loc_levels=additional_loc_levels,
        state=virtual_state,
        # 리버스 중복방지 기준은 **이번 generate 의 대상 세션**입니다. 넘기지 않으면
        # 전략이 ET 벽시계 오늘로 폴백하는데, 섀도우 GENERATE 는 KST 아침(ET 전일 밤)에
        # 돌기 때문에 벽시계 날짜가 세션보다 하루 앞서 리버스 주문이 "이미 실행됨"으로
        # 오판되어 0건이 됩니다 (실운영 pipeline.py 와 동일하게 세션을 명시 전달).
        session_date=session_date,
    )

    orders = result.get("orders", []) or []
    last_price = float(result.get("last_price", 0.0) or 0.0)

    # ── 리버스 상태 영속화 (R1) ──
    reverse_orders = [o for o in orders if o.get("reverse_action")]
    if result.get("reverse_exit"):
        # 전략의 명시적 리버스 종료 신호 — 상태 정리
        if (snapshot.get("reverse_mode") or {}).get("active"):
            print(f"[shadow] {symbol} 전략 reverse_exit → 리버스 상태를 해제합니다.")
        snapshot["reverse_mode"] = {"active": False}
    elif reverse_orders:
        prev_rev = snapshot.get("reverse_mode") or {}
        prev_active = bool(prev_rev.get("active"))
        cycle_id = prev_rev.get("cycle_id") if prev_active else None
        cycle_id = cycle_id or uuid.uuid4().hex
        reverse_days = [
            int(o.get("reverse_day") or 0) for o in reverse_orders
        ]
        day_count = max(reverse_days) if max(reverse_days) > 0 else (
            int(prev_rev.get("day_count", 0) or 0) + 1
        )
        cumulative = 0.0
        if prev_active:
            cumulative = float(prev_rev.get("cumulative_sell_proceeds", 0.0) or 0.0)
        # 전략의 중복가드(strategy.py:137)는 last_planned_session == 오늘(ET)이면
        # 같은 세션의 두 번째 배치를 차단한다. 새 세션일 때만 갱신한다.
        last_planned = prev_rev.get("last_planned_session") if prev_active else None
        if last_planned != session_date:
            last_planned = session_date
        snapshot["reverse_mode"] = {
            "active": True,
            "day_count": day_count,
            "cycle_id": cycle_id,
            "last_planned_session": last_planned,
            "cumulative_sell_proceeds": round(cumulative, 6),
        }
        print(
            f"[shadow] {symbol} 리버스 {day_count}일차 계획 영속화 "
            f"(세션 {session_date}, 누적 매도대금=${cumulative:.2f})"
        )

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

        # P0-3(E1): BUY LOC 가격 상한 — 현재가 ×1.19 초과 시 보정
        # (trading_bot.py:753-762와 동일: 브로커가 +20% 초과 주문을 거부)
        # BUY LOC만 해당 — SELL/MOC/LIMIT는 보정하지 않음
        capped = False
        if (
            order.get("side") == "BUY"
            and order.get("order_type") == "LOC"
            and price > 0
            and last_price > 0
        ):
            max_allowed_price = adjust_price_to_tick(last_price * 1.19)
            if price > max_allowed_price:
                original_price = price
                price = max_allowed_price
                capped = True
                print(
                    f"[shadow] ⚠️ LOC 가격 보정: ${original_price:.2f} → "
                    f"${price:.2f} (현재가 ${last_price:.2f} × 1.19 기준)"
                )

        event = {
            "event_id": event_id,
            "timestamp_utc": _utc_now_iso(),
            "event_type": "intent",
            "status": "pending",
            "symbol": symbol,
            "session_date": session_date,
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
        if capped:
            event["capped"] = True  # E1 상한 보정 발생 (이벤트에만 표기)
        # 리버스 필드 passthrough (order에 있으면 그대로 기록)
        for key in _REVERSE_FIELDS:
            if order.get(key) is not None:
                event[key] = order[key]

        _append_ledger(snapshot_dir, symbol, event)

        # pending_intents 최소 필드 (settle에서 사용)
        pending.append({
            "event_id": event_id,
            "session_date": session_date,
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "intent_price": price,
            "intent_qty": qty,
            "t_target": order.get("t_target"),
            "comment": order.get("comment"),
            **{key: order.get(key) for key in _REVERSE_FIELDS if order.get(key) is not None},
        })

    # ── 스냅샷 저장 (pending 의도 + phase + session_date) ──
    snapshot["pending_intents"] = pending
    snapshot["phase"] = "generating" if pending else "settled"
    _save_snapshot(snapshot_dir, symbol, snapshot)
    print(
        f"[shadow] {symbol} GENERATE 완료 — 의도 {len(orders)}건 기록 (체결 대기), "
        f"세션={session_date}, T={snapshot['T']}, cash=${snapshot['cash_usd']:.2f}, "
        f"holdings={snapshot['holdings']}"
    )
    return snapshot


def settle_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """2단계 SETTLE: 의도에 기록된 세션 날짜의 일봉 종가와 대조해 체결/만료.

    v1.4 세션 기반 정산(S1):
      - 각 의도의 session_date가 us_session_closed()로 마감 확인되어야 한다.
      - broker.get_daily_candles()에서 date == session_date인 캔들만 사용한다.
      - 마감 전/캔들 없음/날짜 불일치면 그 의도는 pending으로 유지한다
        (최신 종가로 대체하지 않음).
      - 마감 + 날짜 일치 캔들이 있으면 체결 또는 미체결 만료를 확정한다.

    멱등/원자성/상태머신:
      1) 시작 시 `cycle_reset_pending` 마커 또는 스냅샷 상태(보유 0 +
         T>0/net_invested>0)를 보고 사이클 리셋을 멱등 적용한다.
         → 스냅샷 저장 → `cycle_reset` 원장 이벤트.
      2) 대기 의도가 없으면 phase만 정리하고 종료.
      3) 의도별: 회계 적용 → (전량매도면 마커 set) → pending 제거 →
         스냅샷 저장(체크포인트) → settlement 원장 이벤트.
      4) 루프 종료 후 필요하면 사이클 리셋을 다시 멱등 적용.
         (crash로 1)이 유실된 경우 다음 settle 시작에서 복구된다.)
      - **저장 → 원장 순서를 유지한다.** 원장을 먼저 쓰면 crash 시 pending이
        남아 다음 settle이 회계를 두 번 적용할 수 있다. 대신 원장 gap은
        감수한다 (스냅샷이 권위, JSONL은 projection).
      - 리셋은 정확히 1회만 적용된다 (마커 해제 + T/net 0 → 재판정 false).
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    seed = float(symbol_config["seed"])
    reinvest = _reinvest_enabled()

    # ── 수수료/슬리피지 파라미터 (환경변수) ──
    default_commission = float(os.getenv("COMMISSION_RATE", "0.0025"))
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", str(default_commission)))
    slippage_bps = float(os.getenv("SHADOW_SLIPPAGE_BPS", "0"))

    snapshot = _load_snapshot(snapshot_dir, symbol_config)

    # ── (1) 시작 시 사이클 리셋 복구/적용 (crash-safe, exactly-once) ──
    reset_event = _apply_cycle_reset_if_needed(
        snapshot, seed, fee_rate, reinvest
    )
    if reset_event:
        _save_snapshot(snapshot_dir, symbol, snapshot)
        _append_ledger(snapshot_dir, symbol, reset_event)
        print(
            f"[shadow] {symbol} SETTLE — 사이클 리셋 적용 "
            f"(trigger={reset_event['trigger']}, "
            f"seed=${reset_event['effective_seed_after']:.2f})"
        )

    pending = list(snapshot.get("pending_intents") or [])

    if not pending:
        print(f"[shadow] {symbol} SETTLE — 대기 의도 없음 (스킵)")
        if snapshot.get("phase") != "settled":
            snapshot["phase"] = "settled"
            _save_snapshot(snapshot_dir, symbol, snapshot)
        return snapshot

    # ── 날짜 있는 일봉 캔들 조회 (세션 날짜 매칭용) ──
    try:
        fetch_candles = getattr(broker, "get_daily_candles")
        candles = fetch_candles(symbol, exchange, days=_SETTLE_CANDLE_DAYS)
    except AttributeError:
        print(
            f"[shadow] {symbol} SETTLE — 브로커가 날짜 있는 일봉(get_daily_candles)을 "
            f"지원하지 않아 정산할 수 없습니다. 의도 유지 (S1)."
        )
        return snapshot
    except Exception as e:
        print(f"[shadow] {symbol} SETTLE — 일봉 조회 실패, 의도 유지: {e}")
        return snapshot

    close_by_date = {}
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        candle_date = candle.get("date")
        candle_close = candle.get("close")
        if candle_date is None or candle_close is None:
            continue
        try:
            close_by_date[str(candle_date)] = float(candle_close)
        except (TypeError, ValueError):
            continue

    if not close_by_date:
        print(
            f"[shadow] {symbol} SETTLE — 날짜 있는 일봉이 없어 정산 보류 "
            f"(의도 {len(pending)}건 유지, S1)."
        )
        return snapshot

    print(
        f"[shadow] {symbol} SETTLE — 의도 {len(pending)}건, "
        f"가용 세션 캔들 {len(close_by_date)}건"
    )

    total_fee = 0.0
    filled_count = 0
    settled_count = 0

    for intent in list(pending):
        session_date = intent.get("session_date")
        if not session_date:
            print(
                f"[shadow] {symbol} SETTLE — session_date 없는 의도 "
                f"{intent.get('event_id')} 유지 (S1)."
            )
            continue
        if not us_session_closed(session_date):
            print(
                f"[shadow] {symbol} SETTLE — 세션 {session_date} 아직 마감 전 "
                f"→ 의도 유지."
            )
            continue
        if session_date not in close_by_date:
            print(
                f"[shadow] {symbol} SETTLE — 세션 {session_date}과 일치하는 일봉 "
                f"없음(날짜 불일치/누락) → 의도 유지 (최신 종가 대체 금지, S1)."
            )
            continue

        daily_close = close_by_date[session_date]
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
            "session_date": session_date,
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
            if fill_result.get("full_sell"):
                # per-intent 체크포인트와 함께 마커를 영속화한다.
                # crash 후 다음 settle이 리셋을 복구할 수 있게 하기 위함.
                snapshot["cycle_reset_pending"] = True
            total_fee += fill_result["fee_usd"]
            filled_count += 1

            # R1: 리버스 매도 체결대금 누적 (fill_price × fill_qty)
            if intent.get("reverse_action") == "sell":
                rev = snapshot.get("reverse_mode") or {}
                if rev.get("active"):
                    proceeds = float(
                        rev.get("cumulative_sell_proceeds", 0.0) or 0.0
                    ) + event["fill_price"] * intent_qty
                    rev["cumulative_sell_proceeds"] = round(proceeds, 6)
                    snapshot["reverse_mode"] = rev
        else:
            # 미체결 → 만료 (상태 변화 없음)
            event["fill_price"] = None
            event["fill_qty"] = 0
            event["fill_ratio"] = 0.0
            event["fee_usd"] = 0.0
            event["t_delta"] = 0.0

        event["virtual_state_after"] = deepcopy(snapshot)

        # ── 멱등 체크포인트: pending 제거 → 스냅샷 저장 → 원장 이벤트 ──
        pending.remove(intent)
        snapshot["pending_intents"] = pending
        snapshot["phase"] = "generating" if pending else "settled"
        _save_snapshot(snapshot_dir, symbol, snapshot)
        _append_ledger(snapshot_dir, symbol, event)
        settled_count += 1

    if settled_count == 0:
        print(f"[shadow] {symbol} SETTLE — 정산 가능한 의도 없음 (모두 보류).")
        return snapshot

    snapshot["pending_intents"] = pending
    snapshot["phase"] = "generating" if pending else "settled"

    # ── (4) 루프 종료 후 리셋 (정상 경로: 즉시 적용) ──
    reset_event = _apply_cycle_reset_if_needed(
        snapshot, seed, fee_rate, reinvest
    )
    if reset_event:
        _save_snapshot(snapshot_dir, symbol, snapshot)
        _append_ledger(snapshot_dir, symbol, reset_event)
        print(
            f"[shadow] {symbol} SETTLE — 사이클 리셋 적용 "
            f"(trigger={reset_event['trigger']}, "
            f"seed=${reset_event['effective_seed_after']:.2f})"
        )
    else:
        # 리셋 대상은 아니지만 보유 0 + 리버스 활성인 경우 방어적 정리
        if (
            snapshot.get("holdings", 0) <= 0
            and (snapshot.get("reverse_mode") or {}).get("active")
        ):
            print(f"[shadow] {symbol} 보유 0 → 리버스 상태를 해제합니다.")
            snapshot["reverse_mode"] = {"active": False}
        _save_snapshot(snapshot_dir, symbol, snapshot)

    print(
        f"[shadow] {symbol} SETTLE 완료 — 체결 {filled_count}/{settled_count}건, "
        f"수수료=${total_fee:.4f}, T={snapshot['T']}, "
        f"cash=${snapshot['cash_usd']:.2f}, holdings={snapshot['holdings']}, "
        f"pending={len(pending)}"
    )
    return snapshot


def run_shadow_symbol(broker, symbol_config, snapshot_dir=".shadow"):
    """GENERATE 래퍼 (v1.1 하위 호환 — 테스트/러너 기본값).

    v1.2부터는 generate_symbol()/settle_symbol() 2단계로 분리되었습니다.
    이 함수는 1단계(GENERATE)만 수행합니다.
    """
    return generate_symbol(broker, symbol_config, snapshot_dir)