# T값(매수 횟수)을 파일에 저장하고 불러오는 코드
# 무한매수법에서 T값은 "지금까지 몇 번이나 매수했는가"를 나타내는 숫자입니다.
# 프로그램이 종료되어도 T값을 잃지 않도록 JSON 파일에 보관합니다.
import hashlib
import json
import math
import os
import tempfile
from copy import deepcopy
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from broker.market_utils import resolve_real_kst_from_ord

# 상태 파일 위치: 프로젝트 루트의 .state.json
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", ".state.json")


def load_state(symbol):
    """
    종목의 현재 상태(T값 등)를 파일에서 읽어옵니다.

    처음 실행하거나 파일이 없으면 T=0인 초기 상태를 반환합니다.

    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "SOXL")

    Returns:
        dict: 상태 정보
            - T (float): 누적 매수 횟수 (1회 매수=1, 절반 매수=0.5)
            - last_updated (str): 마지막 저장 일시 (ISO 형식)
    """
    symbol = symbol.upper()

    if not os.path.exists(_STATE_FILE):
        print(f"[상태] {symbol} 상태 파일 없음 → T=0으로 시작합니다")
        return {
            "T": 0.0,
            "last_updated": "",
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
            "_state_unavailable": "missing",
        }

    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            all_states = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[상태] {symbol} 상태 파일 읽기 실패 ({e}) → T=0으로 시작합니다")
        return {
            "T": 0.0,
            "last_updated": "",
            "cycle_start_date": "",
            "effective_seed": 0.0,
            "net_invested": 0.0,
            "net_invested_status": "unresolved",
            "last_processed_ordno": "",
            "_state_unavailable": "corrupt",
        }

    if symbol not in all_states:
        print(f"[상태] {symbol} 상태 기록 없음 → T=0으로 시작합니다")
        return {
            "T": 0.0,
            "last_updated": "",
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
            "_state_unavailable": "symbol_missing",
        }

    state = all_states[symbol]
    T = float(state.get("T", 0.0))
    last_updated = state.get("last_updated", "")
    cycle_start_date = state.get("cycle_start_date", "")
    effective_seed = float(state.get("effective_seed", 0.0))
    net_invested = float(state.get("net_invested", 0.0))
    # net_invested 필드가 없는 기존 상태(마이그레이션)는 이력 기반 백필을 위해 표시합니다.
    net_invested_missing = "net_invested" not in state
    # net_invested_status: "valid"(신뢰 가능) | "unresolved"(신뢰 불가, 신규 주문 보류).
    # 값이 없으면(unresolved) 신뢰할 수 없다고 보고 — SOXL net_invested=0.00 손상 사고 방지.
    net_invested_status = state.get("net_invested_status", "")
    if net_invested_status not in ("valid", "unresolved"):
        net_invested_status = "unresolved"
    last_processed_ordno = state.get("last_processed_ordno", "")
    additional_loc_odno = state.get("additional_loc_odno", [])
    orders_meta = state.get("orders_meta", {})
    balance_mismatch = state.get("balance_mismatch", {})
    state_version = state.get("state_version", "v1")
    close_prices = state.get("close_prices", [])
    reverse_mode = state.get("reverse_mode", {})
    pending_order_intent = state.get("pending_order_intent")
    pending_order_batch = state.get("pending_order_batch")
    # position_qty: 워터마크 시점의 보유수량. 매도 비율 판정의 기준선으로 쓰이며,
    # 잔고 조회가 실패한 실행에서도 이 값으로 분류할 수 있게 합니다.
    position_qty = state.get("position_qty")
    # sell_t_effects: 매도 T 효과 요약 (주문별 기준선/적용 배율).
    # 정본은 settlement_ledger 이며, 이 필드는 기존 상태/도구 호환용입니다.
    sell_t_effects = state.get("sell_t_effects", {})
    # settlement_ledger: T 의 덧셈(매수)/곱셈(매도)을 적용 순서대로 담은 원장.
    # 재시작 후에도 반드시 함께 로드해야 부분체결 재분류가 같은 결과를 냅니다.
    settlement_ledger = state.get("settlement_ledger", {})

    print(f"[상태] {symbol} 상태 로드 완료 → T={T}, 마지막 갱신: {last_updated}")
    return {
        "T": T,
        "last_updated": last_updated,
        "cycle_start_date": cycle_start_date,
        "effective_seed": effective_seed,
        "net_invested": net_invested,
        "net_invested_status": net_invested_status,
        "last_processed_ordno": last_processed_ordno,
        "additional_loc_odno": additional_loc_odno,
        "orders_meta": orders_meta,
        "balance_mismatch": balance_mismatch,
        "state_version": state_version,
        "close_prices": close_prices,
        "reverse_mode": reverse_mode,
        "pending_order_intent": pending_order_intent,
        "pending_order_batch": pending_order_batch,
        "position_qty": position_qty,
        "sell_t_effects": sell_t_effects,
        "settlement_ledger": settlement_ledger,
        "_net_invested_missing": net_invested_missing,
    }


def save_state(symbol, state_dict):
    """
    종목의 상태(T값 등)를 파일에 저장합니다.

    기존 파일의 다른 종목 정보는 유지하고, 해당 종목 정보만 덮어씁니다.

    Parameters:
        symbol (str): 종목 코드 (예: "TQQQ", "SOXL")
        state_dict (dict): 저장할 상태 정보 (T 포함)
    """
    symbol = symbol.upper()

    # 기존 전체 상태 읽기
    all_states = {}
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                all_states = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"상태 파일이 손상되어 덮어쓰기를 중단합니다: {e}") from e

    # 이 종목 상태 업데이트
    # state_dict에 이미 'last_updated' 값이 있으면 그 값을 우선 사용합니다 (UTC ISO 권장).
    last_updated_val = state_dict.get("last_updated")
    if not last_updated_val:
        # 빈이력 첫RUN 워터마크 오염 방지: last_updated가 ""/None이면 now(UTC)로 채우지 않고
        # "" 그대로 저장합니다. 다음RUN도 초기모드(전체스캔)로 유지되어, 캐시삭제 후 첫RUN의
        # 빈 이력이 now(UTC) 워터마크로 오염되어 진짜 체결(백데이트 UTC)이 기간외 제외되는
        # 사고(T=0 vs 잔고 LIVE fatal)를 막습니다.
        last_updated_val = ""

    # Allow optional new fields to be persisted for diagnostic purposes
    all_states[symbol] = {
        "T": float(state_dict.get("T", 0.0)),
        "last_updated": last_updated_val,
        "cycle_start_date": state_dict.get("cycle_start_date", ""),
        "effective_seed": float(state_dict.get("effective_seed", 0.0)),
        "net_invested": float(state_dict.get("net_invested", 0.0)),
        "net_invested_status": state_dict.get("net_invested_status", "unresolved"),
        "last_processed_ordno": state_dict.get("last_processed_ordno", ""),
        "additional_loc_odno": state_dict.get("additional_loc_odno", []),
        "orders_meta": state_dict.get("orders_meta", {}),
        "balance_mismatch": state_dict.get("balance_mismatch", {}),
        "state_version": state_dict.get("state_version", "v2"),
        "close_prices": state_dict.get("close_prices", []),
        "reverse_mode": state_dict.get("reverse_mode", {}),
        "pending_order_intent": state_dict.get("pending_order_intent"),
        "pending_order_batch": state_dict.get("pending_order_batch"),
        # 워터마크 시점 보유수량 — 매도 비율 판정 기준선 (잔고 조회 실패 시 대체)
        "position_qty": state_dict.get("position_qty"),
        # 매도 T 효과 원장 — 주문별 기준선/적용 배율 (일괄·부분 반영 결과 일치)
        "sell_t_effects": state_dict.get("sell_t_effects", {}),
        # 순서 원장 — 적용 순서(seq)를 가진 T 연산 기록 (정본)
        "settlement_ledger": state_dict.get("settlement_ledger", {}),
    }

    state_dir = os.path.dirname(_STATE_FILE)
    fd, temp_path = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(all_states, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, _STATE_FILE)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    T = all_states[symbol]["T"]
    effective_seed = all_states[symbol]["effective_seed"]
    last_upd = all_states[symbol].get("last_updated", "")
    last_ordno = all_states[symbol].get("last_processed_ordno", "")
    mismatch = all_states[symbol].get("balance_mismatch")
    mismatch_flag = "YES" if mismatch else "NO"
    print(f"[상태] {symbol} 상태 저장 완료 → T={T}, effective_seed=${effective_seed:.2f}, last_updated={last_upd}, last_processed_ordno={last_ordno}, balance_mismatch={mismatch_flag}")


def canonical_state_hash(state):
    """save_state()가 저장하는 필드만으로 계산한 SHA-256 해시를 반환합니다.

    상태 파일의 canonical 형태(저장 관점)에 대한 지문으로, 일회성 복구(fence/T/net_invested)
    CAS 검증과 audit 출력에 사용합니다. 상태 저장 후 재계산하면 값이 달라짐을 보장합니다.
    """
    payload = {
        "T": float(state.get("T", 0.0)),
        "last_updated": state.get("last_updated", ""),
        "cycle_start_date": state.get("cycle_start_date", ""),
        "effective_seed": float(state.get("effective_seed", 0.0)),
        "net_invested": float(state.get("net_invested", 0.0)),
        "net_invested_status": state.get("net_invested_status", "unresolved"),
        "last_processed_ordno": state.get("last_processed_ordno", ""),
        "additional_loc_odno": state.get("additional_loc_odno", []),
        "orders_meta": state.get("orders_meta", {}),
        "balance_mismatch": state.get("balance_mismatch", {}),
        "state_version": state.get("state_version", "v2"),
        "close_prices": state.get("close_prices", []),
        "reverse_mode": state.get("reverse_mode", {}),
        "pending_order_intent": state.get("pending_order_intent"),
        "pending_order_batch": state.get("pending_order_batch"),
        "position_qty": state.get("position_qty"),
        "sell_t_effects": state.get("sell_t_effects", {}),
        "settlement_ledger": state.get("settlement_ledger", {}),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_order_meta_in_state(state, odno, meta):
    """
    주문 메타 정보를 state의 orders_meta에 저장합니다.

    Parameters:
        state (dict): 현재 상태 딕셔너리
        odno (str): 주문번호
        meta (dict): 저장할 메타 정보
            - side (str): 'BUY' 또는 'SELL'
            - total_qty (int): 주문 수량
            - t_target (float): 체결 완료 시 증가할 T 목표값 (0.0, 0.5, 1.0 등)
            - is_additional (bool): 추가매수 여부 (True면 T 변화 없음)
            - processed_filled_qty (int): 이미 T에 반영된 체결 수량
    """
    normalized = str(odno)
    state.setdefault("orders_meta", {})[normalized] = meta
    print(f"[orders_meta 등록] odno={normalized} (원본={odno}), t_target={meta.get('t_target')}")


def get_order_meta(state, odno):
    """state의 orders_meta에서 odno 메타를 반환하거나 None."""
    return state.get("orders_meta", {}).get(str(odno))


def _order_real_kst_date(order):
    """주문이력의 실제 KST 날짜(YYYYMMDD)를 복원합니다.

    봇은 KST 장중(04:xx, 현지 ET 전일 15:xx)에 주문하므로 실제 KST 날짜는
    ord_dt 또는 ord_dt+1입니다. resolve_real_kst_from_ord가 ET round-trip으로
    복원하며, ord_tmd가 없거나 복원에 실패하면 ord_dt를 그대로 반환합니다
    (해당 브로커가 ord_dt=KST 날짜 컨벤션인 경우에도 동일하게 동작).
    """
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd", ""))
    if not ord_dt or not ord_tmd:
        return ord_dt
    # ord_dt가 이미 KST 날짜인 브로커(메리츠/TOSS)는 ET round-trip 복원을 건너뜁니다.
    # `_ord_dt_is_kst_date`는 메리츠/TOSS 어댑터가 설정하므로 다른 브로커는 영향 없음.
    if order.get("_ord_dt_is_kst_date"):
        return ord_dt
    resolved = resolve_real_kst_from_ord(ord_dt, ord_tmd)
    if resolved is None:
        return ord_dt
    return resolved.strftime("%Y%m%d")


def _order_us_session_date(order):
    """주문이 속한 미국(ET) 세션 날짜(YYYYMMDD)를 반환합니다.

    브로커별 ord_dt 컨벤션이 다릅니다 (KIS/LS/키움/NHPLUG=미국 영업일,
    MERITZ/TOSS=KST 날짜). KST 자정을 넘는 주문도 같은 세션으로 판정해야
    잔여 주문 취소 대상 선정이 정확합니다.

    - KIS 계열: ord_dt 자체가 미국 영업일 = 세션 날짜입니다.
      (ord_datetime_utc는 상태 호환상 하루 앞선 규칙을 유지하므로 쓰지 않습니다)
    - MERITZ/TOSS(`_ord_dt_is_kst_date`): ord_dt+ord_tmd를 KST로 해석해
      ET 날짜로 환산합니다.
    - 시각 정보가 부족하면 ord_datetime_utc의 ET 날짜를 씁니다.
    - 판단 불가하면 None을 반환합니다 (호출 측이 KST 날짜 비교로 대체).
    """
    ord_dt = str(order.get("ord_dt", "") or "")
    ord_tmd = str(order.get("ord_tmd", "") or "")
    et_tz = ZoneInfo("America/New_York")
    if order.get("_ord_dt_is_kst_date"):
        if ord_dt and ord_tmd:
            try:
                kst_dt = datetime.strptime(
                    ord_dt + ord_tmd.zfill(6), "%Y%m%d%H%M%S"
                ).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                return kst_dt.astimezone(et_tz).strftime("%Y%m%d")
            except Exception:
                pass
    elif ord_dt and len(ord_dt) == 8 and ord_dt.isdigit() and "ord_tmd" in order:
        return ord_dt
    odt_iso = order.get("ord_datetime_utc")
    if odt_iso:
        try:
            parsed = datetime.fromisoformat(str(odt_iso))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
            return parsed.astimezone(et_tz).strftime("%Y%m%d")
        except Exception:
            return None
    return None


def _is_reverse_order(state, odno, order=None):
    """주문 메타데이터상 리버스모드 주문인지 확인합니다."""
    meta = state.get("orders_meta", {}).get(str(odno), {})
    if order is not None and meta.get("submitted_at"):
        if _order_real_kst_date(order) != str(meta["submitted_at"])[:8]:
            return False
    return bool(meta.get("reverse_action"))


def _is_terminal_reverse_order(order):
    """브로커 표준 필드로 리버스 주문의 최종 상태를 판정합니다."""
    status = str(order.get("prcs_stat_name", "")).strip().upper()
    if status in {"미체결", "부분체결", "취소거부", "접수", "대기", "OPEN", "PARTIAL", "PENDING", "NEW"}:
        return False
    if status in {
        "체결", "취소", "거부", "만료", "종료", "완료",
        "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "CLOSED",
    }:
        return True
    remaining = order.get("nccs_qty")
    if remaining is None:
        return False
    try:
        return float(remaining) <= 0
    except (TypeError, ValueError):
        return False


def _auto_assume_reverse_expiry(meta, order):
    """데모 환경에서 이전 세션의 zero-fill 리버스 주문을 만료로 가정해 자동 종결합니다.

    데모(특히 키움 모의)는 일중 주문(LOC/LOO/MOC→LIMIT 자동 변환)이 마감 후
    만료돼도 상태를 "미체결"로 남겨 _is_terminal_reverse_order()가 영구 False가
    됩니다. 이 상태가 reconcile의 "전일 리버스 주문 미종결 → 신규 주문 보류"
    차단과 만나면 리버스모드가 무기한 동결됩니다.

    이를 막기 위해, 이전 미국 세션에서 체결 기회가 지나고 체결량/체결금액이
    0인 주문(BROKER_MODE=demo 한정)은 terminal_assumed=True로 자동 종결 처리합니다.
    늦은 체결은 reconcile의 odno 단위 processed_filled_qty 델타가 다음 RUN에
    그대로 반영하므로 상태가 소실되지 않습니다. 매수/매도 모두 대상입니다.
    """
    try:
        from config import BROKER_MODE
    except Exception:
        BROKER_MODE = ""
    if BROKER_MODE != "demo":
        return False
    if meta.get("terminal"):
        return False
    try:
        history_filled = int(float(order.get("ft_ccld_qty", "0") or 0))
    except (TypeError, ValueError):
        history_filled = 0
    if history_filled != 0:
        return False
    if int(meta.get("processed_filled_qty", 0) or 0) != 0:
        return False
    if float(meta.get("processed_filled_amount", 0.0) or 0.0) != 0.0:
        return False
    meta["terminal"] = True
    meta["terminal_assumed"] = True
    meta["terminal_assumed_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    meta["terminal_assumption_reason"] = "auto_expired_demo_day_order_after_session"
    return True


def reconcile_reverse_fills(state, order_history):
    """리버스모드 주문의 실제 누적 체결만 상태에 반영합니다.

    일반 T 갱신의 전역 시간 워터마크와 별도로 주문번호를 기준으로
    누적 체결수량을 비교합니다. 따라서 부분체결이 다음 실행에서 증가해도
    이미 반영한 수량을 다시 반영하지 않습니다.
    """
    hold_snapshot = deepcopy(state)
    orders_meta = state.get("orders_meta", {})
    history_by_odno = {}
    for order in order_history:
        odno = str(order.get("odno", ""))
        if not odno:
            continue
        history_by_odno.setdefault(odno, []).append(order)
    events = []
    marker_held_odnos: list[str] = []
    reverse_mode = state.setdefault("reverse_mode", {})
    active_cycle_id = reverse_mode.get("cycle_id", "")
    if not active_cycle_id:
        return

    def _order_for_meta(odno, meta):
        candidates = history_by_odno.get(str(odno), [])
        submitted_at = str(meta.get("submitted_at", ""))
        if submitted_at:
            candidates = [
                order for order in candidates
                if _order_real_kst_date(order) == submitted_at[:8]
            ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda order: float(order.get("ft_ccld_qty", "0") or 0),
        )

    for odno, meta in orders_meta.items():
        if not meta.get("reverse_action"):
            continue
        if meta.get("cycle_id") != active_cycle_id:
            continue
        order = _order_for_meta(odno, meta)
        if not order:
            submitted_at = str(meta.get("submitted_at", ""))
            today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            if meta.get("submitted_session", "") < today_session:
                reverse_mode["reconciliation_error"] = "reverse_order_history_missing"
                reverse_mode["reconciliation_only"] = True
                print(f"[경고] 리버스 주문 이력 누락: odno={odno} → 신규 주문을 보류합니다.")
            continue
        submitted_at = str(meta.get("submitted_at", ""))
        today_session = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if (
            meta.get("submitted_session", "")
            and meta.get("submitted_session", "") < today_session
            and not _is_terminal_reverse_order(order)
        ):
            if meta.get("terminal"):
                # 이미 종결(자동/수동 만료 가정 포함) 처리된 주문은 재차단하지 않습니다.
                pass
            elif _auto_assume_reverse_expiry(meta, order):
                print(
                    f"[경고] 이전 세션 미체결 리버스 주문을 만료로 가정합니다: "
                    f"odno={odno} → 자동 종결 처리 (데모 일중 주문)"
                )
            else:
                reverse_mode["reconciliation_error"] = "reverse_order_not_terminal"
                reverse_mode["reconciliation_only"] = True
                print(f"[경고] 전일 리버스 주문이 종결되지 않음: odno={odno} → 신규 주문을 보류합니다.")
        try:
            filled_qty = max(0, int(float(order.get("ft_ccld_qty", "0"))))
            total_qty = max(0, int(meta.get("total_qty", 0)))
        except (TypeError, ValueError):
            continue
        if total_qty <= 0:
            continue
        qty_marker_ok, processed_qty = _parse_processed_qty_marker(meta)
        if filled_qty > 0 and not qty_marker_ok and _fill_amount_is_available(order):
            # 마커 누락·무효 리버스 메타는 이미 반영된 체결인지 판단할 수 없으므로
            # 자동 증분 반영하지 않고 reconciliation을 보류합니다.
            marker_held_odnos.append(str(odno))
            print(
                f"  [경고] 리버스 주문 보류(마커 누락·무효): "
                f"odno={odno}, qty={order.get('ft_ccld_qty','0')}"
            )
            continue
        events.append((
            order.get("ord_datetime_utc", ""),
            str(odno),
            meta,
            order,
            filled_qty,
            processed_qty,
            total_qty,
        ))

    if marker_held_odnos and not events:
        # 마커 누락·무효 주문만 있고 반영할 정상 이벤트가 없으면 여기서 보류합니다.
        state.clear()
        state.update(hold_snapshot)
        state["_pending_amount_odnos"] = [str(odno) for odno in marker_held_odnos]
        state["_pending_marker_odnos"] = [str(odno) for odno in marker_held_odnos]
        _mark_settlement_hold(
            state,
            f"리버스 마커 누락·무효 주문 {len(marker_held_odnos)}건 "
            f"({', '.join([str(o) for o in marker_held_odnos[:5]])}) — 마커 보정 후 재실행 필요",
        )
        return

    if not events:
        return

    # 전역 T를 과거 이력으로 재생하지 않고, 마지막 저장 이후 새로 확인된
    # 체결분만 현재 T에 증분 반영합니다. 이력 조회가 일부 누락되어도
    # 이미 반영한 과거 체결을 되돌리지 않습니다.
    confirmed_sell_days = []
    confirmed_sell_proceeds = float(reverse_mode.get("cumulative_sell_proceeds", 0.0))
    net_invested = float(state.get("net_invested", 0.0))
    def _filled(order):
        try:
            return float(order.get("ft_ccld_qty", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    pending_amount_odnos = []
    for odno in marker_held_odnos:
        if str(odno) not in pending_amount_odnos:
            pending_amount_odnos.append(str(odno))
    for _, odno, meta, order, filled_qty, processed_qty, total_qty in sorted(events, key=lambda item: (item[0], item[1])):
        observed_fraction = min(filled_qty / total_qty, 1.0)
        action = meta.get("reverse_action")
        # 금액 미확정 체결은 마커/원장을 진행시키지 않고 전체 reconcile 을 보류합니다.
        if filled_qty > processed_qty and not _fill_amount_is_available(order):
            pending_amount_odnos.append(str(odno))
            continue
        delta_amount = 0.0
        try:
            cumulative_amount = float(order.get("ft_ccld_amt3", "0"))
            previous_amount = float(meta.get("processed_filled_amount", 0.0))
            delta_amount = max(0.0, cumulative_amount - previous_amount)
        except (TypeError, ValueError):
            pass
        if action == "sell":
            factor = float(meta.get("reverse_t_factor", 1.0))
            previous_fraction = min(float(meta.get("applied_fill_fraction", 0.0)), 1.0)
            # 누적 체결 비율은 **감소하지 않습니다**. 브로커 응답이 일시적으로
            # 적은 수량을 주더라도 이미 반영한 T를 되돌리지 않습니다.
            effective_fraction = max(previous_fraction, observed_fraction)
            delta_fraction = max(0.0, effective_fraction - previous_fraction)
            if not meta.get("reverse_base_t"):
                if previous_fraction > 0:
                    # 이미 일부 반영됐는데 기준선이 없으면 역산하지 않습니다.
                    _mark_settlement_hold(
                        state,
                        f"리버스 매도 {odno} 기준선(reverse_base_t)이 없고 "
                        f"{previous_fraction:.2f} 비율이 이미 반영됐습니다",
                    )
                    continue
                meta["reverse_base_t"] = float(state.get("T", 0.0))
                print(
                    f"  [정보] 리버스 주문 {odno} 에 reverse_base_t가 없습니다 "
                    f"(미반영 주문) → 현재 T를 기준선으로 사용합니다"
                )
            base_t = float(meta.get("reverse_base_t", state.get("T", 0.0)))
            # 마커(반영 비율)와 원장 항목이 서로 어긋나면 자동 보정하지 않고 보류합니다.
            existing_entries = [
                e for e in (_ensure_settlement_ledger(state).get("entries") or [])
                if e.get("kind") == "reverse_sell" and str(e.get("odno")) == str(odno)
            ]
            last_ledger_fraction = 0.0
            mismatch = False
            checked_ledger = False
            for e in existing_entries:
                if "applied_before" not in e or "fill_fraction" not in e:
                    continue
                checked_ledger = True
                fb = float(e.get("applied_before", 0.0) or 0.0)
                ff = float(e.get("fill_fraction", 0.0) or 0.0)
                last_ledger_fraction = max(last_ledger_fraction, ff)
                expected_entry_delta = round(
                    -base_t * (1.0 - factor) * max(0.0, ff - fb), 4
                )
                if abs(float(e.get("delta", 0.0) or 0.0) - expected_entry_delta) > 0.01:
                    mismatch = True
            if checked_ledger and last_ledger_fraction < previous_fraction - 1e-9:
                mismatch = True
            if mismatch:
                _mark_settlement_hold(
                    state,
                    f"리버스 매도 {odno} 마커(반영 {previous_fraction:.2f})와 "
                    f"원장(최대 {last_ledger_fraction:.2f})이 일치하지 않습니다",
                )
                continue
            ledger_entry = None
            t_delta = 0.0
            if delta_fraction > 1e-12:
                # 증가분만 새 seq 항목으로 기록합니다. 기존 항목을 덮어쓰면
                # 체결이 줄어든 응답에서 T가 역행합니다.
                delta = round(-base_t * (1.0 - factor) * delta_fraction, 4)
                _bootstrap_settlement_start(state, float(state.get("T", 0.0)))
                ledger_entry = _append_settlement_entry(
                    state, "reverse_sell", odno, delta=delta
                )
                ledger_entry["base_t"] = round(base_t, 4)
                ledger_entry["factor"] = factor
                ledger_entry["applied_before"] = previous_fraction
                ledger_entry["fill_fraction"] = effective_fraction
                state["T"] = max(0.0, _replay_settlement_ledger(state))
                t_delta = delta
            confirmed_sell_proceeds += delta_amount
            # 리버스 매도 체결 → 순투입 감소 (매도대금 회수)
            net_invested -= delta_amount
            if filled_qty > processed_qty or delta_amount > 0:
                state.setdefault("_reverse_settlement_deltas", []).append({
                    "odno": str(odno),
                    "side": "매도",
                    "qty_delta": max(0, filled_qty - processed_qty),
                    "amount_delta": delta_amount,
                    "reverse": True,
                    "reverse_base_t": float(meta.get("reverse_base_t", 0.0) or 0.0),
                    "reverse_factor": float(
                        meta.get("reverse_t_factor", 1.0)
                        if meta.get("reverse_t_factor") is not None else 1.0
                    ),
                    "fill_fraction": effective_fraction,
                    "fill_fraction_delta": delta_fraction,
                    "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                    "ledger_delta": t_delta,
                    "t_delta": t_delta,
                })
            terminal = _is_terminal_reverse_order(order)
            status = str(order.get("prcs_stat_name", ""))
            if filled_qty > 0 and terminal and "거부" not in status:
                confirmed_sell_days.append(int(meta.get("reverse_day", 0) or 0))
            if terminal:
                meta["terminal"] = True
        elif action == "buy":
            previous_fraction = min(float(meta.get("applied_fill_fraction", 0.0)), 1.0)
            effective_fraction = max(previous_fraction, observed_fraction)
            delta_fraction = max(0.0, effective_fraction - previous_fraction)
            target = float(meta.get("reverse_t_target", 0.0) or 0.0)
            existing_entries = [
                e for e in (_ensure_settlement_ledger(state).get("entries") or [])
                if e.get("kind") == "reverse_buy" and str(e.get("odno")) == str(odno)
            ]
            last_ledger_fraction = 0.0
            mismatch = False
            checked_ledger = False
            for e in existing_entries:
                if "applied_before" not in e or "fill_fraction" not in e:
                    continue
                checked_ledger = True
                fb = float(e.get("applied_before", 0.0) or 0.0)
                ff = float(e.get("fill_fraction", 0.0) or 0.0)
                last_ledger_fraction = max(last_ledger_fraction, ff)
                expected_entry_delta = round(target * max(0.0, ff - fb), 4)
                if abs(float(e.get("delta", 0.0) or 0.0) - expected_entry_delta) > 0.01:
                    mismatch = True
            if checked_ledger and last_ledger_fraction < previous_fraction - 1e-9:
                mismatch = True
            if mismatch:
                _mark_settlement_hold(
                    state,
                    f"리버스 매수 {odno} 마커(반영 {previous_fraction:.2f})와 "
                    f"원장(최대 {last_ledger_fraction:.2f})이 일치하지 않습니다",
                )
                continue
            ledger_entry = None
            t_delta = 0.0
            if target and delta_fraction > 1e-12:
                delta = round(target * delta_fraction, 4)
                _bootstrap_settlement_start(state, float(state.get("T", 0.0)))
                ledger_entry = _append_settlement_entry(
                    state, "reverse_buy", odno, delta=delta
                )
                ledger_entry["target"] = round(target, 4)
                ledger_entry["applied_before"] = previous_fraction
                ledger_entry["fill_fraction"] = effective_fraction
                state["T"] = _replay_settlement_ledger(state)
                t_delta = delta
            # 리버스 쿼터매수 체결 → 순투입 증가
            net_invested += delta_amount
            if filled_qty > processed_qty or delta_amount > 0:
                state.setdefault("_reverse_settlement_deltas", []).append({
                    "odno": str(odno),
                    "side": "매수",
                    "qty_delta": max(0, filled_qty - processed_qty),
                    "amount_delta": delta_amount,
                    "reverse": True,
                    "is_additional": True,
                    "reverse_t_target": target,
                    "fill_fraction": effective_fraction,
                    "fill_fraction_delta": delta_fraction,
                    "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                    "ledger_delta": t_delta,
                    "t_delta": t_delta,
                })
            if _is_terminal_reverse_order(order):
                meta["terminal"] = True

        meta["processed_filled_qty"] = max(
            int(meta.get("processed_filled_qty", 0)), filled_qty
        )
        meta["applied_fill_fraction"] = min(
            1.0,
            max(
                float(meta.get("applied_fill_fraction", 0.0)),
                observed_fraction,
            ),
        )
        try:
            meta["processed_filled_amount"] = max(
                float(meta.get("processed_filled_amount", 0.0)),
                float(order.get("ft_ccld_amt3", "0")),
            )
        except (TypeError, ValueError):
            meta["processed_filled_amount"] = 0.0

        if action == "sell":
            print(f"  → 리버스 매도 체결 반영: odno={odno}, ({filled_qty}/{total_qty})")
        elif action == "buy":
            print(f"  → 리버스 매수 체결 반영: odno={odno}, ({filled_qty}/{total_qty})")

    # 리버스 주문 체결 처리 후 last_updated 갱신
    # _apply_recent_history_dt는 리버스 주문을 필터링하므로,
    # 리버스만 처리된 상태에서 last_updated가 진하지 않으면 매 RUN 경고가 반복됩니다.
    if events:
        sorted_events = sorted(events, key=lambda item: (item[0], item[1]))
        latest_ord_dt = sorted_events[-1][0]
        latest_odno = sorted_events[-1][1]
        if latest_ord_dt:
            last_updated_str = state.get("last_updated", "")
            last_updated_dt = None
            if last_updated_str:
                try:
                    last_updated_dt = datetime.fromisoformat(last_updated_str)
                    if last_updated_dt.tzinfo is None:
                        last_updated_dt = last_updated_dt.replace(tzinfo=ZoneInfo("UTC"))
                    else:
                        last_updated_dt = last_updated_dt.astimezone(ZoneInfo("UTC"))
                except Exception:
                    pass
            try:
                latest_dt = datetime.fromisoformat(latest_ord_dt)
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=ZoneInfo("UTC"))
                else:
                    latest_dt = latest_dt.astimezone(ZoneInfo("UTC"))
                if last_updated_dt is None or latest_dt > last_updated_dt:
                    state["last_updated"] = latest_ord_dt
                    state["last_processed_ordno"] = latest_odno
                elif latest_dt == last_updated_dt and latest_odno:
                    prev_ordno = state.get("last_processed_ordno", "")
                    try:
                        updated = int(latest_odno) > int(prev_ordno or "0")
                    except (ValueError, TypeError):
                        updated = latest_odno > (prev_ordno or "")
                    if updated:
                        state["last_processed_ordno"] = latest_odno
            except Exception:
                pass

    if confirmed_sell_days:
        reverse_mode["active"] = True
        reverse_mode["day_count"] = max(
            int(reverse_mode.get("day_count", 0)),
            max(confirmed_sell_days),
        )
    reverse_mode["cumulative_sell_proceeds"] = round(confirmed_sell_proceeds, 2)
    state["T"] = round(float(state.get("T", 0.0)), 4)
    state["net_invested"] = round(max(0.0, net_invested), 2)
    reverse_mode["cumulative_sell_proceeds"] = round(confirmed_sell_proceeds, 2)
    cycle_meta = [
        meta for meta in orders_meta.values()
        if meta.get("reverse_action") and meta.get("cycle_id") == active_cycle_id
    ]
    if reverse_mode.get("reconciliation_only") and cycle_meta and all(meta.get("terminal") for meta in cycle_meta):
        if reverse_mode.get("active"):
            reverse_mode.pop("reconciliation_only", None)
            reverse_mode.pop("reconciliation_error", None)
        else:
            state["reverse_mode"] = {}
    if pending_amount_odnos:
        state.clear()
        state.update(hold_snapshot)
        state["_pending_amount_odnos"] = pending_amount_odnos
        marker_only = [str(o) for o in marker_held_odnos if str(o) in pending_amount_odnos]
        if marker_only:
            state["_pending_marker_odnos"] = marker_only
        _mark_settlement_hold(
            state,
            f"리버스 체결금액 미확정 또는 마커 누락·무효 주문 {len(pending_amount_odnos)}건 "
            f"({', '.join(pending_amount_odnos[:5])}) — 금액 확정 또는 마커 보정 후 재실행 필요",
        )
        return
    if state.get("_settlement_hold"):
        reason = state.get("_settlement_hold_reason", "리버스 정산 근거 부족")
        state.clear()
        state.update(hold_snapshot)
        _mark_settlement_hold(state, reason)
        return
    print(f"  → 리버스 체결 reconciliation 완료: T={state['T']}, day_count={reverse_mode.get('day_count', 0)}")


def _update_T_from_history_impl(symbol, state, order_history, balance_qty=None):
    """
    주문 이력을 바탕으로 T값을 업데이트합니다.

    last_updated 값에 따라 두 가지 모드로 동작합니다:

    [초기 모드] last_updated가 비어있을 때 (처음 실행 또는 업그레이드 직후)
      - 전체 이력을 처음부터 스캔하여 T값을 자동으로 추정합니다.
      - 순보유수량(net_qty)이 0이 되는 시점을 사이클 종료로 감지하여
        현재 진행 중인 사이클의 T값만 반영합니다.

    [일반 모드] last_updated가 있을 때
      - last_updated 날짜 이후의 체결 이력만 T값에 누적합니다.
      - 월요일(어제=일요일), 공휴일 다음날 등 어떤 경우에도 올바르게 동작합니다.
      - 최근 이력이 비고 T>0일 때 잔고 교차 검증(balance_qty)으로 리셋 여부를 판단:
          * 보유 중(balance_qty > 0): 이력 조회 누락/정지 복귀로 보고 T 유지 + 경고
          * 잔고 0: 잘못된 state 복구를 위해 전체 이력 재추정 fallback
          * 잔고 미확인(balance_qty is None): 보수적으로 T 유지

    Parameters:
        symbol (str): 종목 코드
        state (dict): 현재 상태 (T 포함) — 이 딕셔너리가 직접 수정됩니다
        order_history (list): get_overseas_order_history()의 반환값
        balance_qty (int|None): 브로커 get_balance()의 보유 수량.
            None(조회 실패/미확인)이면 보수적으로 리셋을 보류하고, 0이면 잔고 없음으로
            리셋 fallback을 허용하며, 0 초과면 보유 중으로 보아 리셋을 보류합니다.
            초기 모드(경로 1)에는 적용되지 않습니다.

    Returns:
        dict: 업데이트된 state (입력과 동일한 객체)
    """
    symbol = symbol.upper()
    state.pop("_settlement_mode", None)
    state.pop("_pending_amount_odnos", None)
    state.pop("_pending_marker_odnos", None)
    state["_reverse_settlement_deltas"] = []
    state["_settlement_deltas"] = []
    last_updated = state.get("last_updated", "")
    last_processed_ordno = state.get("last_processed_ordno", "")

    # last_updated는 UTC ISO 형식(권장) 또는 레거시 로컬 포맷("%Y-%m-%d %H:%M:%S")일 수 있습니다.
    last_updated_dt = None
    if last_updated:
        try:
            # ISO 포맷 파싱 시도
            last_updated_dt = datetime.fromisoformat(last_updated)
            if last_updated_dt.tzinfo is None:
                # 레거시로 저장된 경우 KST로 해석 후 UTC로 변환
                last_updated_dt = last_updated_dt.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(ZoneInfo("UTC"))
            else:
                last_updated_dt = last_updated_dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            try:
                # 레거시 포맷: "%Y-%m-%d %H:%M:%S" 를 KST로 간주하고 UTC로 변환
                last_updated_legacy = datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
                last_updated_dt = last_updated_legacy.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(ZoneInfo("UTC"))
            except Exception:
                print(f"[상태] {symbol} last_updated 파싱 실패: {last_updated} → 초기 모드로 처리")
                last_updated_dt = None

    # net_invested 마이그레이션: 이전 상태 파일에 필드가 없으면 이력에서 1회 백필합니다.
    # 리버스 주문은 아래 reconcile_reverse_fills()가 델타로 반영하므로 여기선 비리버스만 계산합니다.
    # 일반 모드에서는 last_updated 이전 체결만 기준으로 삼아 아래 _apply_recent_history_dt의
    # 증분 누적과 이중 가산이 없도록 합니다. 초기 모드(last_updated_dt=None)는 _infer가 재계산합니다.
    if state.get("_net_invested_missing"):
        state.pop("_net_invested_missing", None)
        backfilled = _compute_non_reverse_net_invested(
            state, order_history, cutoff_dt=last_updated_dt, last_processed_ordno=last_processed_ordno
        )
        if backfilled is not None:
            state["net_invested"] = backfilled
            print(f"[상태] {symbol} net_invested 마이그레이션 백필 → ${state['net_invested']:.2f}")
        else:
            state["net_invested"] = 0.0

    # 정산 시작 전 원장 상태를 점검합니다. 불일치하면 자동 adjust 를 만들지 않고
    # 기존 보류/원상복구 경로로 넘깁니다.
    # 전체 재추정(T-estimation-suspected-low, last_updated 없음)은 의도적 재구성이므로
    # 이 점검 대상이 아닙니다.
    intentional_rebuild = (
        state.get("balance_mismatch", {}).get("note") == "T-estimation-suspected-low"
    )
    if last_updated_dt is not None and not intentional_rebuild:
        ledger_ok, ledger_reason = _audit_settlement_start(state)
        if not ledger_ok:
            _mark_settlement_hold(state, ledger_reason)
            return state

    if last_updated_dt is None:
        # 초기 모드: 전체 이력에서 T를 처음부터 재계산합니다
        reverse_baseline_t = state.get("T", 0.0)
        active_cycle_id = state.get("reverse_mode", {}).get("cycle_id", "")
        has_reverse_meta = any(
            meta.get("reverse_action")
            and not meta.get("repair_archived")
            and meta.get("cycle_id") == active_cycle_id
            for meta in state.get("orders_meta", {}).values()
        )
        state = _infer_T_from_full_history(symbol, state, order_history)
        if has_reverse_meta:
            state["T"] = reverse_baseline_t
            # 리버스 기준 T로 되돌렸으므로 비리버스 재추정 원장은 폐기합니다.
            reset_settlement_ledger(state, reverse_baseline_t)
        reconcile_reverse_fills(state, order_history)
        if state.get("_settlement_hold"):
            return state
        state["_settlement_deltas"].extend(
            state.pop("_reverse_settlement_deltas", [])
        )
        return state

    # Safety net: T 오추정 상태에서 orders_meta가 있으면 full 재추정
    mismatch_note = state.get("balance_mismatch", {}).get("note")
    if mismatch_note == "T-estimation-suspected-low" and state.get("orders_meta"):
        print(f"[상태] {symbol} T 오추정 감지 → orders_meta({len(state['orders_meta'])}건) 활용 재추정")
        has_filled_history = False
        for order in order_history:
            try:
                if int(float(order.get("ft_ccld_qty", "0"))) > 0 and order.get("ord_datetime_utc"):
                    has_filled_history = True
                    break
            except Exception:
                continue

        if not has_filled_history and balance_qty is None:
            print(f"[경고] {symbol} T 재추정용 이력이 없고 잔고를 확인할 수 없어 재추정을 보류합니다.")
            return state
        if not has_filled_history and balance_qty > 0:
            print(f"[경고] {symbol} T 재추정용 이력이 없지만 보유 {balance_qty}주가 확인되어 재추정을 보류합니다.")
            return state

        candidate_state = deepcopy(state)
        reverse_baseline_t = candidate_state.get("T", 0.0)
        active_cycle_id = candidate_state.get("reverse_mode", {}).get("cycle_id", "")
        has_reverse_meta = any(
            meta.get("reverse_action")
            and not meta.get("repair_archived")
            and meta.get("cycle_id") == active_cycle_id
            for meta in candidate_state.get("orders_meta", {}).values()
        )
        candidate_state.pop("balance_mismatch", None)
        candidate_state = _infer_T_from_full_history(symbol, candidate_state, order_history)
        if has_reverse_meta:
            candidate_state["T"] = reverse_baseline_t
            reset_settlement_ledger(candidate_state, reverse_baseline_t)
        reconcile_reverse_fills(candidate_state, order_history)
        if candidate_state.get("_settlement_hold"):
            state.clear()
            state.update(candidate_state)
            return state
        candidate_state["_settlement_deltas"].extend(
            candidate_state.pop("_reverse_settlement_deltas", [])
        )
        inferred_T = float(candidate_state.get("T", 0.0) or 0.0)

        if inferred_T <= 0 and balance_qty is None:
            print(f"[경고] {symbol} T 재추정 결과가 0이지만 잔고를 확인할 수 없어 재추정을 보류합니다.")
            return state
        if inferred_T <= 0 and balance_qty > 0:
            print(f"[경고] {symbol} T 재추정 결과가 0이지만 보유 {balance_qty}주가 확인되어 T 재추정을 보류합니다.")
            return state

        state.clear()
        state.update(candidate_state)
        return state

    # 일반 모드: 리버스 주문은 주문번호 기준으로 먼저 반영한 뒤,
    # 나머지 주문만 last_updated_dt 이후 이력으로 처리합니다.
    #
    # 불확실한 정산(보유수량 기준 미확보)은 호출자가 저장하지 않아야 합니다.
    # 계산은 수행하되 _position_basis_unknown 플래그를 남기고,
    # 저장 직전에 trading_bot 이 이 플래그를 확인해 저장을 막습니다.
    reconcile_reverse_fills(state, order_history)
    if state.get("_settlement_hold"):
        return state
    updated_state, pending_amount_odnos = _apply_recent_history_dt(
        symbol, state, order_history, last_updated_dt, last_processed_ordno, balance_qty
    )
    if pending_amount_odnos:
        updated_state["_pending_amount_odnos"] = pending_amount_odnos
    return updated_state


def update_T_from_history(symbol, state, order_history, balance_qty=None):
    """T 갱신 진입점 — 정산 보류 시 상태를 원상복구합니다.

    적용 순서 근거가 없어 T를 재계산하면 안 되는 경우(`_settlement_hold`)에는
    T·순투입·마커·워터마크·원장 변경을 **하나도 저장하지 않습니다**.
    (호출부가 `_save_settlement_state()`로 저장을 막고, 여기서는 메모리도 되돌립니다)
    """
    state.pop("_settlement_hold", None)
    state.pop("_settlement_hold_reason", None)
    state.pop("_pending_marker_odnos", None)
    snapshot = deepcopy(state)
    result = _update_T_from_history_impl(symbol, state, order_history, balance_qty=balance_qty)
    if isinstance(result, dict) and result.get("_settlement_hold"):
        reason = result.get("_settlement_hold_reason", "정산 근거 부족")
        pending = list(result.get("_pending_amount_odnos") or [])
        marker_pending = list(result.get("_pending_marker_odnos") or [])
        result.clear()
        result.update(snapshot)
        _mark_settlement_hold(result, reason)
        if pending:
            result["_pending_amount_odnos"] = pending
        if marker_pending:
            result["_pending_marker_odnos"] = marker_pending
    return result


def _compute_net_qty_up_to(order_history, cutoff_dt):
    """
    cutoff_dt 이전(포함)의 체결 이력을 기반으로 순보유수량을 계산합니다.
    매도 체결이 쿼터매도/목표매도/전량매도인지 판별하기 위한 기준 수량 산출에 사용됩니다.
    """
    qty = 0
    for o in order_history:
        odt_iso = o.get("ord_datetime_utc")
        if not odt_iso:
            continue
        try:
            o_dt = datetime.fromisoformat(odt_iso)
            if o_dt.tzinfo is None:
                o_dt = o_dt.replace(tzinfo=ZoneInfo("UTC"))
            else:
                o_dt = o_dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            continue
        if o_dt > cutoff_dt:
            continue
        filled = int(float(o.get("ft_ccld_qty", "0")))
        if filled <= 0:
            continue
        # 수량 기준 계산에는 금액 확정이 필요 없습니다 (미확정 금액은 0으로 취급).
        side = o.get("sll_buy_dvsn_cd_name", "")
        if side == "매수":
            qty += filled
        elif side == "매도":
            qty -= filled
    return max(0, qty)


def _fill_amount(order):
    """주문 이력 항목의 **확정** 체결금액(USD)을 반환합니다.

    Returns:
        tuple: ``(amount, is_estimated)``

    정책 (2026-10-24, 추정→보류 전환):
      - ``ft_ccld_amt3`` 가 존재하고 0보다 크면 ``(amount, False)``.
      - 없거나 0이면 ``(0.0, True)`` — 평균가/현재가로 **추정하지 않습니다**.
        금액이 확정되지 않은 필수 체결은 정산을 보류하고 다음 실행에서
        브로커가 금액을 확정해 줄 때 정확히 1회 반영합니다.
      - 호출부는 반드시 튜플을 풀어 써야 합니다.
    """
    try:
        amount = float(order.get("ft_ccld_amt3", "0") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount > 0:
        return amount, False

    order["_fill_amount_unavailable"] = True
    print(
        f"  [경고] 체결금액 미확정(odno={order.get('odno', '')}, "
        f"qty={order.get('ft_ccld_qty', '0')}) → 정산 보류 대상"
    )
    return 0.0, True


def _fill_amount_is_available(order):
    """체결금액이 **확정**됐는지 (미확정을 0원으로 조용히 반영하지 않기 위한 가드)."""
    try:
        if float(order.get("ft_ccld_amt3", "0") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        if int(float(order.get("ft_ccld_qty", "0") or 0)) <= 0:
            return True  # 체결 자체가 없으면 금액 문제도 없음
    except (TypeError, ValueError):
        return True
    return False


def _has_unconfirmed_amount_fills(state, order_history):
    """`orders_meta` 등록 주문 중 **금액 미확정** 체결이 있는 odno 목록.

    - 이번 실행에서 새로 반영할 체결(누적 체결 > 반영 수량)이 금액 미확정이면 포함합니다.
    - 기존 캐시의 ``processed_filled_amount == 0`` 레거시 표식은 **변경하지 않습니다**
      (감사 도구가 별도로 식별). 금액이 확정된 체결은 여기에 포함되지 않습니다.
    """
    history_by_odno = {str(o.get("odno", "")): o for o in (order_history or [])}
    pending = []
    for odno, meta in (state.get("orders_meta") or {}).items():
        row = history_by_odno.get(str(odno))
        if row is None:
            continue
        if _fill_amount_is_available(row):
            continue
        try:
            filled = int(float(row.get("ft_ccld_qty", "0") or 0))
            processed = int((meta or {}).get("processed_filled_qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if filled > processed:
            pending.append(str(odno))
    return pending


def _legacy_amount_markers(state, order_history):
    """감사 전용 — 금액 확정 체결인데 processed_filled_amount=0 인 레거시 메타."""
    history_by_odno = {str(o.get("odno", "")): o for o in (order_history or [])}
    legacy = []
    for odno, meta in (state.get("orders_meta") or {}).items():
        row = history_by_odno.get(str(odno))
        if row is None or _is_reverse_order(state, str(odno), row):
            continue
        if not _fill_amount_is_available(row):
            continue
        if int((meta or {}).get("processed_filled_qty", 0) or 0) <= 0:
            continue
        if float((meta or {}).get("processed_filled_amount", 0.0) or 0.0) == 0.0:
            legacy.append(str(odno))
    return legacy


def _estimate_fill_amount(order):
    """FORCE_T_REINFERENCE 전용 — 평균가/현재가로 체결금액을 **예외적** 추정합니다."""
    try:
        qty = int(float(order.get("ft_ccld_qty", "0") or 0))
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return 0.0

    def _positive(value) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return number if number > 0 else 0.0

    price = _positive(order.get("ft_ccld_unpr3"))
    if price <= 0:
        price = _positive(
            order.get("last_price") or order.get("lastPrice") or order.get("last")
        )
    if price <= 0:
        return None
    return round(qty * price, 2)


def _inference_amount(order):
    """전체 재추정 경로의 금액 — 확정값 우선, FORCE 추정값 허용."""
    amount, estimated = _fill_amount(order)
    if not estimated:
        return amount
    inferred = order.get("_ft_ccld_amt_inferred")
    if inferred is not None:
        try:
            return float(inferred)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _compute_non_reverse_net_invested(state, order_history, cutoff_dt=None, last_processed_ordno=""):
    """
    현재 사이클의 비리버스 순투입 금액(Σ 매수체결금액 − Σ 매도체결금액, USD)을
    이력 전체에서 재계산합니다.

    - 전량매도(사이클 종료)를 만나면 0으로 리셋한 뒤 새 사이클부터 다시 누적합니다.
    - 리버스모드 주문은 reconcile_reverse_fills()가 별도 반영하므로 여기서 제외합니다.
    - 이력이 없으면 None을 반환합니다 (호출부가 기존 영속값을 유지하도록).
    - cutoff_dt가 주어지면 그 시각 이전 체결만 기준으로 삼습니다
      (마이그레이션 백필: _apply_recent_history_dt의 증분 누적과 중복을 피하기 위함).
    """
    def _before_cutoff(o):
        """_apply_recent_history_dt의 포함 규칙(o_dt > cutoff 또는 == cutoff & odno > last)과
        정확히 상보적인지 검사합니다. (백필은 _apply가 처리할 최근분을 제외해야 함)"""
        if cutoff_dt is None:
            return True
        odt_iso = o.get("ord_datetime_utc")
        if not odt_iso:
            return False
        try:
            o_dt = datetime.fromisoformat(odt_iso)
            if o_dt.tzinfo is None:
                o_dt = o_dt.replace(tzinfo=ZoneInfo("UTC"))
            else:
                o_dt = o_dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            return False
        if o_dt < cutoff_dt:
            return True
        if o_dt > cutoff_dt:
            return False
        odno = str(o.get("odno", ""))
        if not odno or not last_processed_ordno:
            return not bool(odno and odno == last_processed_ordno)
        try:
            return int(odno) <= int(last_processed_ordno)
        except Exception:
            return odno <= last_processed_ordno

    filled_orders = []
    for o in order_history:
        try:
            qty = int(float(o.get("ft_ccld_qty", "0")))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 or not o.get("ord_datetime_utc"):
            continue
        # 금액 미확정 체결도 수량은 포함하고 금액은 0으로 둡니다 (마이그레이션 규칙).
        if cutoff_dt is not None and not _before_cutoff(o):
            continue
        filled_orders.append(o)
    if not filled_orders:
        return None

    filled_orders.sort(key=lambda o: (o.get("ord_datetime_utc", ""), o.get("odno", "")))
    net_qty = 0
    net_invested = 0.0
    for o in filled_orders:
        if _is_reverse_order(state, str(o.get("odno", "")), o):
            continue
        side = o.get("sll_buy_dvsn_cd_name", "")
        qty = int(float(o.get("ft_ccld_qty", "0")))
        amt = _inference_amount(o)
        if side == "매수":
            net_qty += qty
            net_invested += amt
        elif side == "매도":
            if net_qty > 0 and qty >= net_qty:
                # 전량매도 → 사이클 종료 → 새 사이클 시작
                net_qty = 0
                net_invested = 0.0
            else:
                net_qty = max(0, net_qty - qty)
                net_invested -= amt
    return round(max(0.0, net_invested), 2)


def _infer_T_from_full_history(symbol, state, order_history):
    """
    전체 주문 이력을 처음부터 스캔하여 T값을 추정합니다.

    무상태 봇에서 4.0으로 업그레이드하거나 state.json이 없는 경우에 사용됩니다.
    순보유수량(net_qty)을 추적하여 전량매도(사이클 종료) 시점을 감지합니다.
    이전 사이클이 여러 번 있었어도 마지막 사이클의 T값만 반영합니다.

    ⚠️ 소액 시드 한계:
    1회 분할 금액 / 주가 ≤ 1 이면 정상 매수도 qty=1이 되어 추가매수와 구분이 불가합니다.
    이 경우 T가 실제보다 낮게 추정될 수 있습니다.
    함수 실행 후 경고가 출력되면 .state.json 의 "T" 값을 직접 확인하고 수정하세요.
    """
    # 보류 시 원상복구할 스냅샷 (금액 미확정 체결 발견 시 사용)
    inference_snapshot = deepcopy(state)
    # 전체 재추정은 처음부터 다시 계산하므로 이전 실행의 매도 원장을 버립니다.
    # (낡은 basis/factor가 남으면 새 분류와 섞여 T가 중복 조정됩니다)
    state["_settlement_mode"] = "full_reinference"
    state["sell_t_effects"] = {}
    state.pop("_position_basis_unknown", None)
    reset_settlement_ledger(state, 0.0)
    state["_settlement_deltas"] = []
    # ── 금액 미확정 필수 체결 게이트 ──────────────────────────────
    # 추정하지 않고 보류합니다. FORCE_T_REINFERENCE=true 인 **명시적 재추정**에서만
    # 평균가/현재가 기반 추정을 예외적으로 허용합니다.
    unconfirmed = []
    for o in order_history:
        try:
            qty = int(float(o.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0 and o.get("ord_datetime_utc") and not _fill_amount_is_available(o):
            unconfirmed.append(str(o.get("odno", "")))
    force_estimate = os.getenv("FORCE_T_REINFERENCE", "").strip().lower() == "true"
    if unconfirmed and not force_estimate:
        state.clear()
        state.update(inference_snapshot)
        state["_pending_amount_odnos"] = unconfirmed
        _mark_settlement_hold(
            state,
            f"전체 재추정 중 체결금액 미확정 주문 {len(unconfirmed)}건 "
            f"({', '.join(unconfirmed[:5])}) — 금액 확정 후 재실행 필요",
        )
        return state
    if unconfirmed and force_estimate:
        for o in order_history:
            try:
                qty = int(float(o.get("ft_ccld_qty", "0") or 0))
            except (TypeError, ValueError):
                continue
            if qty <= 0 or _fill_amount_is_available(o):
                continue
            estimated_amount = _estimate_fill_amount(o)
            if estimated_amount is None:
                state.clear()
                state.update(inference_snapshot)
                state["_pending_amount_odnos"] = unconfirmed
                _mark_settlement_hold(
                    state,
                    f"FORCE_T_REINFERENCE 추정 불가 주문 {o.get('odno', '')}",
                )
                return state
            o["_ft_ccld_amt_inferred"] = estimated_amount
            print(
                f"  [경고] FORCE_T_REINFERENCE: odno={o.get('odno', '')} "
                f"금액 미확정 → ${estimated_amount:.2f} 추정 허용"
            )
    # 체결 완료된 주문만 추출(타임스탬프가 있는 항목 우선)
    filled_orders = []
    for o in order_history:
        qty = int(float(o.get("ft_ccld_qty", "0")))
        dt_utc = o.get("ord_datetime_utc")
        if qty > 0 and dt_utc and (
            _fill_amount_is_available(o) or o.get("_ft_ccld_amt_inferred") is not None
        ):
            filled_orders.append(o)
        else:
            reasons = []
            if qty <= 0:
                reasons.append(f"체결수량={o.get('ft_ccld_qty','0')}")
            if not dt_utc:
                reasons.append("ord_datetime_utc 없음")
            if qty > 0 and dt_utc and not _fill_amount_is_available(o):
                reasons.append("체결금액 계산불가(체결금액/평균가/현재가 없음)")
            print(f"  [디버그] 초기모드 주문 제외: odno={o.get('odno','')}, "
                  f"ord_dt={o.get('ord_dt','')}, ord_tmd={o.get('ord_tmd','')}, "
                  f"side={o.get('sll_buy_dvsn_cd_name','')}, "
                  f"qty={o.get('ft_ccld_qty','0')}, 사유={'/'.join(reasons)}")

    if not filled_orders:
        print(f"[상태] {symbol} 초기 상태 - 이력 없음 → T=0으로 시작합니다")
        state["T"] = 0.0
        # 빈이력 첫RUN 워터마크 오염 방지: last_updated를 ""로 명시적으로 유지합니다.
        # (기존 동작과 동일 — save_state가 ""를 그대로 저장하므로 다음RUN도 초기모드 전체스캔)
        state["last_updated"] = ""
        state["last_processed_ordno"] = ""
        state["net_invested"] = float(state.get("net_invested", 0.0))
        return state

    # ord_datetime_utc로 정렬 (오래된 순)
    sorted_orders = sorted(
        filled_orders,
        key=lambda o: (o.get("ord_datetime_utc", ""), o.get("odno", ""))
    )

    # 전체 이력으로 다시 계산하는 체결은 이후 증분 정산의 기준점이 됩니다.
    # 정상·추가매수와 매도 모두 현재 누적 체결/금액을 반영 완료로 표시합니다.
    # 리버스 주문은 reconcile_reverse_fills가 별도 관리하므로 건드리지 않습니다.
    for order in sorted_orders:
        odno = str(order.get("odno", ""))
        meta = (state.get("orders_meta") or {}).get(odno)
        if not meta or meta.get("reverse_action") or not odno:
            continue
        try:
            meta["processed_filled_qty"] = max(
                int(meta.get("processed_filled_qty", 0) or 0),
                int(float(order.get("ft_ccld_qty", "0") or 0)),
            )
            meta["processed_filled_amount"] = max(
                float(meta.get("processed_filled_amount", 0.0) or 0.0),
                _inference_amount(order),
            )
        except (TypeError, ValueError):
            continue

    print(f"[상태] {symbol} 초기 상태 감지 → 전체 이력 {len(sorted_orders)}건에서 T 자동 추정 시작")

    T = 0.0
    net_qty = 0
    cycle_start_ord_dt = ""
    small_seed_days = 0  # qty=1 매수만 있어서 정상매수/추가매수 구분이 불가능한 날 수
    current_avg_price = 0.0  # running 평균단가 (가격 기반 추가매수 분류용)

    # 날짜(ord_dt)별로 그룹화하여 처리합니다.
    orders_by_date = defaultdict(list)
    for order in sorted_orders:
        ord_dt = order.get("ord_dt", "")
        if ord_dt:
            orders_by_date[ord_dt].append(order)

    for ord_dt in sorted(orders_by_date.keys()):
        day_orders = orders_by_date[ord_dt]

        orders_meta = state.get("orders_meta", {})
        reverse_sells = [
            o for o in day_orders
            if o.get("sll_buy_dvsn_cd_name") == "매도"
            and _is_reverse_order(state, o.get("odno", ""), o)
        ]
        reverse_buys = [
            o for o in day_orders
            if o.get("sll_buy_dvsn_cd_name") == "매수"
            and _is_reverse_order(state, o.get("odno", ""), o)
        ]
        for order in reverse_sells:
            net_qty = max(0, net_qty - int(float(order.get("ft_ccld_qty", "0"))))
        for order in reverse_buys:
            net_qty += int(float(order.get("ft_ccld_qty", "0")))
        # 일자 내 시간순 보유 궤적 (배율 0과 실제 청산 분리)
        _post_qty = {}
        _sim_qty = net_qty
        for _o in sorted(
            [
                o for o in day_orders
                if not _is_reverse_order(state, o.get("odno", ""), o)
            ],
            key=lambda x: (x.get("ord_datetime_utc", ""), str(x.get("odno", ""))),
        ):
            _qty = int(float(_o.get("ft_ccld_qty", "0") or 0))
            _side = _o.get("sll_buy_dvsn_cd_name")
            if _side == "매수":
                _sim_qty += _qty
            elif _side == "매도":
                _sim_qty = max(0, _sim_qty - _qty)
            _post_qty[str(_o.get("odno", ""))] = _sim_qty

        day_sells = [
            o for o in day_orders
            if o.get("sll_buy_dvsn_cd_name") == "매도"
            and not _is_reverse_order(state, o.get("odno", ""), o)
        ]
        day_buys = [
            o for o in day_orders
            if o.get("sll_buy_dvsn_cd_name") == "매수"
            and not _is_reverse_order(state, o.get("odno", ""), o)
        ]

        # 매도 처리: 보유수량 대비 비율로 쿼터매도 / 목표매도 / 전량매도 구분
        # 쿼터매도: 보유량의 ~25% 매도 → 비율 < 0.5 → T × 0.75
        # 목표매도: 보유량의 ~75% 매도 → 0.5 <= 비율 < 1.0 → T × 0.25
        # 전량매도: 보유량 100% → 비율 >= 1.0 → T = 0 (사이클 종료)
        for order in day_sells:
            sell_qty = int(float(order.get("ft_ccld_qty", "0")))
            cumulative_qty = sell_qty
            odno = str(order.get("odno", ""))
            t_before_effect = T
            if net_qty > 0:
                T, _applied = _apply_sell_t_effect(
                    state, T, net_qty, odno, cumulative_qty, allow_adjust=False
                )
                effects = state.get("sell_t_effects", {})
                entry = effects.get(odno) or {}
                basis = int(entry.get("basis") or net_qty)
                raw_factor = entry.get("factor", 1.0)
                factor = 1.0 if raw_factor is None else float(raw_factor)
                ratio = (cumulative_qty / basis) if basis else 0.0
                # 배율 0(누적 100% 매도)과 실제 보유 0은 다릅니다.
                # 중간 매수가 있으면 누적 매도 100%여도 보유가 남습니다.
                liquidated = _post_qty.get(odno, max(0, net_qty - sell_qty)) <= 0
                if _classify_sell_ratio(ratio) == 0.0 and liquidated:
                    if cycle_start_ord_dt and t_before_effect > 0:
                        state["_completed_cycle_start"] = f"{cycle_start_ord_dt[:4]}-{cycle_start_ord_dt[4:6]}-{cycle_start_ord_dt[6:8]}"
                    T = 0.0
                    cycle_start_ord_dt = ""
                    state["sell_t_effects"] = {}
            else:
                # 보유 기준선이 없는 매도 — 기존 동작(쿼터매도)을 원장에 기록합니다.
                _sync_settlement_ledger(state, T, allow_adjust=False)
                _append_settlement_entry(state, "sell", odno, basis=0, factor=0.75)
                T = _replay_settlement_ledger(state)
                basis = 0
                factor = 0.75
            ledger_entry = _find_settlement_entry(state, kind="sell", odno=odno)
            state["_settlement_deltas"].append({
                "odno": odno,
                "side": "매도",
                "qty_delta": sell_qty,
                "amount_delta": _inference_amount(order),
                "cumulative_qty": cumulative_qty,
                "prev_cumulative_qty": 0,
                "basis": int(basis),
                "factor": float(factor),
                "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                "t_delta": round(T - t_before_effect, 4),
                "full_reinference": True,
            })
            net_qty = max(0, net_qty - sell_qty)

        def _is_additional_buy(o, avg_price, net_qty_before=0):
            """return True if this buy is an additional (extra) buy that should NOT increment T"""
            odno = str(o.get("odno", ""))
            if odno and odno in orders_meta:
                return bool(orders_meta[odno].get("is_additional", False))

            qty = int(float(o.get("ft_ccld_qty", "0")))
            if qty > 1:
                return False

            # net_qty가 0인 상태에서의 첫 매수는 항상 정상매수 (사이클 시작)
            # (avg_price도 0이므로 가격 기반 분류가 불가능)
            if avg_price <= 0:
                return False

            fill_price = float(o.get("ft_ccld_unpr3", "0"))
            if fill_price > 0:
                fill_ratio = fill_price / avg_price
                if fill_ratio >= 0.95:
                    return False

            return True

        normal_buys     = []
        additional_buys = []
        for o in day_buys:
            is_add = _is_additional_buy(o, current_avg_price, net_qty)
            qty = int(float(o.get("ft_ccld_qty", "0")))
            fill_price = float(o.get("ft_ccld_unpr3", "0"))
            fill_ratio = (fill_price / current_avg_price) if current_avg_price > 0 and fill_price > 0 else 0.0
            odno = str(o.get("odno", ""))
            has_meta = "있음" if odno and odno in orders_meta else "없음"
            print(f"  [디버그] 매수 분류({ord_dt}): odno={odno}, "
                  f"qty={qty}, fill_price=${fill_price:.2f}, "
                  f"avg_price=${current_avg_price:.2f}, "
                  f"fill_ratio={fill_ratio:.4f}, "
                  f"orders_meta={has_meta}, "
                  f"분류={'추가매수' if is_add else '정상매수'}")
            if is_add:
                additional_buys.append(o)
            else:
                normal_buys.append(o)

        # 매수 체결은 있는데 정상 매수(qty>1)가 하나도 없는 날 → 소액 시드 의심
        if day_buys and not normal_buys:
            small_seed_days += 1

        for o in additional_buys:
            qty = int(float(o.get("ft_ccld_qty", "0")))
            fill_price = float(o.get("ft_ccld_unpr3", "0"))
            prev_net = net_qty
            net_qty += qty
            # T 효과는 없지만 금액·수량 검산 대상입니다. (검증기가 필수 체결로 확인)
            state["_settlement_deltas"].append({
                "odno": str(o.get("odno", "")),
                "side": "매수",
                "qty_delta": qty,
                "amount_delta": _inference_amount(o),
                "cumulative_qty": qty,
                "prev_cumulative_qty": 0,
                "basis": 0,
                "factor": 1.0,
                "is_additional": True,
                "t_delta": 0.0,
                "full_reinference": True,
            })
            if fill_price > 0 and prev_net > 0:
                current_avg_price = (current_avg_price * prev_net + fill_price * qty) / net_qty
            elif fill_price > 0:
                current_avg_price = fill_price

        if normal_buys:
            if T == 0:
                cycle_start_ord_dt = ord_dt
            meta_buys         = [o for o in normal_buys if orders_meta.get(str(o.get("odno", "")))]
            unregistered_buys = [o for o in normal_buys if not orders_meta.get(str(o.get("odno", "")))]
            for o in meta_buys:
                odno = str(o.get("odno", ""))
                meta = orders_meta[odno]
                qty = int(float(o.get("ft_ccld_qty", "0")))
                total_qty = int(meta.get("total_qty") or qty)
                delta_T = 0.0
                # 전체 재추정은 "처음부터" 다시 계산하므로 이미 반영된 분까지 포함합니다.
                # (processed_filled_qty 를 빼면 T 가 다시 낮아집니다 — 2026-10-22 실측)
                if total_qty > 0:
                    delta_T = round((qty / total_qty) * float(meta.get("t_target", 1.0)), 4)
                    T, ledger_entry = _apply_buy_t_effect(state, T, delta_T, odno, allow_adjust=False)
                else:
                    ledger_entry = None
                # 수량·금액 마커를 함께 확정해야 이후 증분 정산이 중복 가산하지 않습니다.
                meta["processed_filled_qty"] = qty
                meta["processed_filled_amount"] = max(
                    float(meta.get("processed_filled_amount", 0.0) or 0.0),
                    _inference_amount(o),
                )
                state["_settlement_deltas"].append({
                    "odno": odno,
                    "side": "매수",
                    "qty_delta": qty,
                    "amount_delta": _inference_amount(o),
                    "cumulative_qty": qty,
                    "prev_cumulative_qty": 0,
                    "basis": 0,
                    "factor": 1.0,
                    "t_target": float(meta.get("t_target", 1.0) or 0.0),
                    "total_qty": total_qty,
                    "is_additional": bool(meta.get("is_additional", False)),
                    "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                    "t_delta": delta_T,
                    "full_reinference": True,
                })
                fill_price = float(o.get("ft_ccld_unpr3", "0"))
                prev_net = net_qty
                net_qty += qty
                if fill_price > 0 and prev_net > 0:
                    current_avg_price = (current_avg_price * prev_net + fill_price * qty) / net_qty
                elif fill_price > 0:
                    current_avg_price = fill_price
            if unregistered_buys:
                buy_count = len(unregistered_buys)
                delta_T = 1.0 if buy_count >= 2 else 0.5
                total_buy_qty = sum(
                    int(float(o.get("ft_ccld_qty", "0"))) for o in unregistered_buys
                )
                remaining_delta = delta_T
                for idx, o in enumerate(unregistered_buys):
                    qty = int(float(o.get("ft_ccld_qty", "0")))
                    # 수량 비례로 배분하고 마지막 주문이 반올림 잔차를 가집니다.
                    if idx == len(unregistered_buys) - 1:
                        share = round(remaining_delta, 4)
                    else:
                        share = round((qty / total_buy_qty) * delta_T, 4) if total_buy_qty else 0.0
                        remaining_delta = round(remaining_delta - share, 4)
                    odno = str(o.get("odno", ""))
                    T, ledger_entry = _apply_buy_t_effect(state, T, share, odno, allow_adjust=False)
                    state["_settlement_deltas"].append({
                        "odno": odno,
                        "side": "매수",
                        "qty_delta": qty,
                        "amount_delta": _inference_amount(o),
                        "cumulative_qty": qty,
                        "prev_cumulative_qty": 0,
                        "basis": 0,
                        "factor": 1.0,
                        "t_target": float(delta_T),
                        "total_qty": total_buy_qty,
                        "ord_dt": ord_dt,
                        "is_additional": False,
                        "unregistered": True,
                        "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                        "t_delta": share,
                        "full_reinference": True,
                    })
                    fill_price = float(o.get("ft_ccld_unpr3", "0"))
                    prev_net = net_qty
                    net_qty += qty
                    if fill_price > 0 and prev_net > 0:
                        current_avg_price = (current_avg_price * prev_net + fill_price * qty) / net_qty
                    elif fill_price > 0:
                        current_avg_price = fill_price

    # 사이클 시작일 설정 (state에 아직 없는 경우만)
    if cycle_start_ord_dt and len(cycle_start_ord_dt) == 8 and not state.get("cycle_start_date"):
        cycle_start = f"{cycle_start_ord_dt[:4]}-{cycle_start_ord_dt[4:6]}-{cycle_start_ord_dt[6:8]}"
        state["cycle_start_date"] = cycle_start

    state["T"] = round(T, 4)

    # 비리버스 순투입은 이력에서 재계산하고, 리버스 주문은 호출부의
    # reconcile_reverse_fills()가 델타로 반영합니다.
    computed = _compute_non_reverse_net_invested(state, order_history)
    if computed is not None:
        state["net_invested"] = computed
        print(f"[상태] {symbol} 순투입(비리버스) 이력 재계산 → ${state['net_invested']:.2f}")
    else:
        state["net_invested"] = float(state.get("net_invested", 0.0))

    if T > 0:
        print(f"[상태] {symbol} T 추정 완료 → T={state['T']} (사이클 시작: {state.get('cycle_start_date', '알 수 없음')})")
        print("  ※ 자동 추정값입니다. 값이 틀리면 .state.json 파일에서 T를 직접 수정하세요.")
    else:
        if net_qty > 0:
            print(f"[상태] {symbol} 이력 스캔 결과 net_qty={net_qty}주이나 T=0입니다 (소액 시드로 인한 오추정 가능성)")
        else:
            print(f"[상태] {symbol} 이력 스캔 결과 현재 보유 없음 → T=0으로 시작합니다")

    # 소액 시드 경고: qty=1 매수만 있는 날이 있으면 T가 실제보다 낮을 수 있습니다
    if small_seed_days > 0:
        # 만약 이 날들을 정상매수로 재분류한다면 T에 최소 0.5씩 추가
        min_additional_T = small_seed_days * 0.5
        print("")
        print(f"[경고] {symbol} qty=1 매수만 체결된 날 {small_seed_days}일 발견됨")
        print("  → 1회 분할 금액으로 1주만 살 수 있는 소액 시드 환경일 수 있습니다")
        print("  → 정상 매수(T +0.5)가 추가매수로 잘못 분류되어 T가 낮게 추정되었을 수 있습니다")
        print(f"  → 현재 추정 T={state['T']}, 추정 범위: T={state['T']} ~ T={state['T'] + min_additional_T}")
        print(f"  → 위를 T 바로잡기 추정값으로 사용하려면 .state.json 에서 T를 {state['T'] + min_additional_T} 로 수정하세요")
        print("  ※ (위 값은 qty=1 매수만 발생한 모든 날을 정상매수로 가정한 최대 추정치입니다)")
        print("")
        state["_inference_diagnostic"] = {
            "small_seed_days": small_seed_days,
            "estimated_T": state["T"],
            "max_corrected_T": state["T"] + min_additional_T,
            "note": "small-seed detected; actual T may be between estimated_T and max_corrected_T",
        }

    # 초기 추정 시 처리한 가장 최신 주문의 타임스탬프/주문번호를 상태에 기록
    try:
        if sorted_orders:
            last_order = sorted_orders[-1]
            if last_order.get("ord_datetime_utc"):
                state["last_updated"] = last_order.get("ord_datetime_utc")
                state["last_processed_ordno"] = last_order.get("odno", "")
    except Exception:
        pass

    return state


def compute_position_from_history(order_history, cycle_start_date=None):
    """
    주문 이력을 시뮬레이션하여 현재 보유 수량과 추정 평단을 계산합니다.

    - order_history: get_overseas_order_history()가 반환한 리스트(최신순이든 상관없음)
    - cycle_start_date (optional): YYYY-MM-DD 형식으로 주면 그 날짜 이후의 이력만 사용

    Returns: dict {"net_qty": int, "avg_price": float, "buy_count": int, "sell_count": int}
    """
    # 필터: 체결수량(>0) 있고 ord_datetime_utc가 있는 항목만 사용
    filled = [
        o for o in order_history
        if int(float(o.get("ft_ccld_qty", "0"))) > 0
        and o.get("ord_datetime_utc")
    ]
    if cycle_start_date:
        # YYYY-MM-DD -> YYYYMMDD for comparison
        ymd = cycle_start_date.replace("-", "")
        filled = [o for o in filled if o.get("ord_dt", "") >= ymd]

    # 정렬: 오래된 순
    try:
        filled_sorted = sorted(filled, key=lambda o: (o.get("ord_datetime_utc", ""), o.get("odno", "")))
    except Exception:
        filled_sorted = filled

    lots = []  # list of (qty:int, price:float)
    buy_count = 0
    sell_count = 0

    for o in filled_sorted:
        side = o.get("sll_buy_dvsn_cd_name", "")
        qty = int(float(o.get("ft_ccld_qty", "0")))
        price = float(o.get("ft_ccld_unpr3", "0") or 0)

        if side == "매수":
            if qty > 0:
                lots.append({"qty": qty, "price": price})
                buy_count += 1

        elif side == "매도":
            remaining = qty
            sell_count += 1
            # FIFO 소거
            while remaining > 0 and lots:
                lot = lots[0]
                if lot["qty"] > remaining:
                    lot["qty"] -= remaining
                    remaining = 0
                else:
                    remaining -= lot["qty"]
                    lots.pop(0)
            # 만약 매도량이 더 큰 경우(이상상태) 그냥 무시: 잔여 마이너스는 처리 안함

    net_qty = sum(lot["qty"] for lot in lots) if lots else 0
    avg_price = 0.0
    if net_qty > 0:
        total_val = sum(lot["qty"] * lot["price"] for lot in lots)
        try:
            avg_price = total_val / net_qty
        except Exception:
            avg_price = 0.0

    return {"net_qty": int(net_qty), "avg_price": round(avg_price, 4), "buy_count": buy_count, "sell_count": sell_count}


def _classify_sell_ratio(ratio: float) -> float:
    """
    매도 비율에 따른 T 배수.

    - 쿼터매도 (보유량의 ~25%, 비율 < 0.5) → 0.75
    - 목표매도 (보유량의 ~75%, 0.5 <= 비율 < 1.0) → 0.25
    - 전량매도 (비율 >= 1.0) → 0.0 (사이클 종료)
    """
    if ratio >= 1.0:
        return 0.0
    if ratio >= 0.5:
        return 0.25
    return 0.75


def _ensure_settlement_ledger(state):
    """순서 원장의 **구조만** 정규화해 반환합니다.

    원장은 T 연산을 **적용 순서(seq)** 대로 보관하는 작은 로그입니다.
    - buy / reverse_sell / reverse_buy: delta 를 더합니다 (+T)
    - sell: factor 를 곱합니다 (×T)

    ⚠️ 여기서 현재 T를 start_T 로 채우거나 과거 효과를 원장에 추가하면 안 됩니다.
    (그렇게 하면 이미 반영된 레거시 마커와 겹쳐 T가 한 번 더 조정됩니다)
    start_T 는 실제 정산을 시작하는 헬퍼(`_bootstrap_settlement_start`)가
    "아직 원장에 아무 효과도 없는" 경우에만 명시적으로 설정합니다.
    """
    ledger = state.get("settlement_ledger")
    if not isinstance(ledger, dict):
        ledger = {}
        state["settlement_ledger"] = ledger
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        entries = []
        ledger["entries"] = entries
    max_seq = 0
    for entry in entries:
        try:
            max_seq = max(max_seq, int(entry.get("seq", 0) or 0))
        except (TypeError, ValueError):
            continue
    try:
        next_seq = int(ledger.get("next_seq", 0) or 0)
    except (TypeError, ValueError):
        next_seq = 0
    ledger["next_seq"] = max(next_seq, max_seq + 1, 1)
    return ledger


def _bootstrap_settlement_start(state, T):
    """원장에 아직 효과가 없을 때만 현재 T를 start_T 로 확정합니다.

    레거시 상태(마커만 있고 원장 없음)에서는 호출부가 보류/보존을 먼저 판정한
    뒤 이 함수를 호출해야 합니다.
    """
    ledger = _ensure_settlement_ledger(state)
    if not ledger.get("entries") and "start_T" not in ledger:
        ledger["start_T"] = round(float(T), 4)
    elif not ledger.get("entries") and ledger.get("start_T") is None:
        ledger["start_T"] = round(float(T), 4)
    return ledger


_SETTLEMENT_START_TOLERANCE = 0.005
_SETTLEMENT_ENTRY_KINDS = {"buy", "sell", "reverse_sell", "reverse_buy", "adjust"}


def _as_finite_float(value):
    """유한한 숫자(음수/0 포함)면 float, 그 외(불리언/문자열/NaN/inf)면 None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _as_non_negative_float(value):
    """숫자(0 포함)면 float, 그 외(불리언/문자열/NaN/inf/음수)면 None."""
    number = _as_finite_float(value)
    if number is None or number < 0:
        return None
    return number


def _settlement_ledger_entry_valid(entry):
    """원장 항목의 필수 필드 형식을 검사합니다 (0 으로 대체하지 않습니다)."""
    if not isinstance(entry, dict):
        return False
    try:
        int(entry.get("seq"))
    except (TypeError, ValueError):
        return False
    kind = entry.get("kind")
    if kind not in _SETTLEMENT_ENTRY_KINDS:
        return False
    if kind == "sell":
        return _as_non_negative_float(entry.get("factor")) is not None
    # reverse_sell/adjust 는 음수 delta 가 정상입니다 (T 감소).
    return _as_finite_float(entry.get("delta")) is not None


def _audit_settlement_start(state):
    """정산 시작 전 원장 상태를 분류하고, 필요한 경우에만 새 원장을 시작합니다.

    반환값 ``(ok, reason)``:
      - 원장 없음 → 기존 전환 규칙(마커/부트스트랩)에 맡깁니다.
      - 기준 없음 + 항목 없음 → 현재 T 를 start_T 로 새 원장 시작.
      - 유효한 기준 + 항목 없음 → 기준 T 와 현재 T 비교.
      - 항목 있음 → 재생값과 비교.
      - 항목 있지만 기준 없음/형식 오류 → 보류(0 대체 금지).
    """
    if "settlement_ledger" not in state or state.get("settlement_ledger") is None:
        return True, ""
    ledger = state.get("settlement_ledger")
    if not isinstance(ledger, dict):
        return False, "settlement_ledger 형식 오류"
    entries = ledger.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return False, "settlement_ledger.entries 형식 오류"
    current_t = _as_non_negative_float(state.get("T"))
    if current_t is None:
        return False, f"현재 T 형식 오류: {state.get('T')!r}"

    start_raw = ledger.get("start_T")
    has_start = "start_T" in ledger
    start_t = _as_non_negative_float(start_raw)
    if has_start and start_raw is not None and start_t is None:
        return False, f"원장 start_T 형식 오류: {start_raw!r}"

    if not entries:
        if start_t is not None:
            if abs(current_t - start_t) > _SETTLEMENT_START_TOLERANCE:
                return False, (
                    f"빈 원장 기준 T={start_t:.4f} 와 현재 T={current_t:.4f} 가 "
                    f"다릅니다 (허용 오차 {_SETTLEMENT_START_TOLERANCE})"
                )
            return True, ""
        # 기준 T 가 없고 항목도 없을 때만 새 원장을 시작합니다.
        ledger["start_T"] = round(current_t, 4)
        ledger["entries"] = []
        return True, ""

    if start_t is None:
        return False, "원장에 항목이 있지만 기준 T(start_T)가 없습니다"
    if not all(_settlement_ledger_entry_valid(entry) for entry in entries):
        return False, "원장 항목 형식 오류"
    replay = _replay_settlement_ledger(state)
    if abs(current_t - replay) > _SETTLEMENT_START_TOLERANCE:
        return False, (
            f"현재 T={current_t:.4f} 가 원장 재생값 {replay:.4f} 와 다릅니다 "
            f"(명시적 adjust 필요)"
        )
    return True, ""


def _mark_settlement_hold(state, reason):
    """적용 순서 근거가 없어 T를 재계산하면 안 되는 상태를 표시합니다."""
    state["_settlement_hold"] = True
    state["_settlement_hold_reason"] = reason
    print(f"[경고] 정산 보류: {reason}")


def reset_settlement_ledger(state, T=None):
    """외부에서 T를 확정 변경했을 때 원장을 새 기준(start_T)으로 재설정합니다.

    MAX_T 상한, 사이클 종료, balance_mismatch 자동 0, FORCE_T 처럼
    정산 로직 밖에서 T를 확정한 경우에는 과거 원장을 그대로 두면
    다음 재생에서 외부 보정이 덮어써지므로 반드시 호출합니다.
    """
    value = float(state.get("T", 0.0) if T is None else T)
    state["settlement_ledger"] = {
        "start_T": round(value, 4),
        "next_seq": 1,
        "entries": [],
    }
    state["sell_t_effects"] = {}
    return state["settlement_ledger"]


def record_settlement_t_adjustment(state, target_t, *, source="", at=None):
    """정산 밖에서 T를 복구한 경우 **명시적 조정 항목**으로 원장에 기록합니다.

    T-only 복구(예: STATE_REPAIR_TARGET_T)는 기존 원장 재생값을 바꾸지 않으므로
    다음 정산의 재생에서 조용히 덮어써질 수 있습니다. 복구 전 T·목표 T·조정량·
    시점·유형을 adjust 항목에 남겨 원장 재생값이 복구 T와 일치하게 합니다.
    매도/매수 마커(`sell_t_effects`)는 보존합니다.
    """
    ledger = _ensure_settlement_ledger(state)
    before_t = round(float(state.get("T", 0.0) or 0.0), 4)
    target = round(float(target_t), 4)
    # 레거시 상태(원장 없음/항목만 있고 start_T 없음)에서도 복구 후 다음 정산이
    # 보류되지 않도록 기준 T 를 먼저 확정합니다. start_T 가 없으면 재생 기준이
    # 0 이 되어 adjust delta 가 엉뚱하게 커지고, 다음 정산은 "기준 T 없음"으로
    # 보류됩니다.
    if not ledger.get("entries"):
        _bootstrap_settlement_start(state, before_t)
    elif ledger.get("start_T") is None:
        ledger["start_T"] = round(before_t, 4)
    replay = _replay_settlement_ledger(state)
    delta = round(target - replay, 4)
    entry = _append_settlement_entry(
        state,
        "adjust",
        "",
        delta=delta,
        before_T=before_t,
        target_T=target,
        replay_T=round(replay, 4),
        applied_at=at or datetime.now(ZoneInfo("UTC")).isoformat(),
        source=str(source or "explicit_t_recovery"),
    )
    state["T"] = target
    return entry


def _append_settlement_entry(state, kind, odno, **fields):
    """원장 끝에 새 항목을 추가하고 항목을 반환합니다."""
    ledger = _ensure_settlement_ledger(state)
    entry = {"seq": int(ledger["next_seq"]), "kind": kind, "odno": str(odno)}
    entry.update(fields)
    ledger["next_seq"] = entry["seq"] + 1
    ledger["entries"].append(entry)
    return entry


def _find_settlement_entry(state, kind=None, odno=None, seq=None):
    """원장에서 조건에 맞는 항목을 찾습니다 (없으면 None)."""
    ledger = _ensure_settlement_ledger(state)
    for entry in ledger.get("entries", []):
        if seq is not None:
            try:
                if int(entry.get("seq", -1)) != int(seq):
                    continue
            except (TypeError, ValueError):
                continue
        if kind is not None and entry.get("kind") != kind:
            continue
        if odno is not None and str(entry.get("odno", "")) != str(odno):
            continue
        return entry
    return None


def _replay_settlement_ledger(state):
    """원장을 start_T 부터 적용 순서대로 재생해 T를 계산합니다."""
    ledger = _ensure_settlement_ledger(state)
    try:
        T = float(ledger.get("start_T", 0.0) or 0.0)
    except (TypeError, ValueError):
        T = 0.0
    for entry in sorted(
        ledger.get("entries", []),
        key=lambda e: int(e.get("seq", 0) or 0),
    ):
        kind = entry.get("kind")
        try:
            if kind == "sell":
                factor = entry.get("factor", 1.0)
                T *= float(1.0 if factor is None else factor)
            else:
                # buy / reverse_sell / reverse_buy / adjust 는 덧셈 항목입니다.
                T += float(entry.get("delta", 0.0) or 0.0)
            T = round(T, 4)
        except (TypeError, ValueError):
            continue
    return round(T, 4)


def _sync_settlement_ledger(state, T, allow_adjust=True):
    """정산 시작점을 보장합니다.

    `allow_adjust=True` 는 **명시적 외부 변경 경로**(단위 테스트/수동 보정)에서
    현재 T와 원장 재생값의 차이를 `adjust` 로 보존합니다.
    정상 정산 경로는 `allow_adjust=False` 로 호출해 근거 없는 보정이
    자동으로 검증을 통과하지 않게 합니다.
    """
    ledger = _ensure_settlement_ledger(state)
    if not ledger.get("entries"):
        _bootstrap_settlement_start(state, T)
        return ledger
    if allow_adjust:
        replay = _replay_settlement_ledger(state)
        diff = round(float(T) - replay, 4)
        if abs(diff) > 1e-9:
            _append_settlement_entry(state, "adjust", "", delta=diff)
    return ledger


def _apply_buy_t_effect(state, T, delta, odno, allow_adjust=True):
    """매수(+) T 효과를 원장에 **새 항목**으로 기록하고 재생 결과를 반환합니다.

    같은 주문의 지연 체결 증가분을 기존 매수 항목에 누적하면, 그 사이에 적용된
    매도(×)보다 앞에서 더해져 소급 스케일됩니다. (예: T=10, +0.2, ×0.75 후
    +0.3 → 7.95 여야 하는데 누적하면 (10+0.5)×0.75=7.875)
    따라서 증가분마다 현재 순서 위치에 새 seq 항목을 추가합니다.
    """
    delta = round(float(delta), 4)
    if abs(delta) < 1e-12:
        return T, None
    _sync_settlement_ledger(state, T, allow_adjust=allow_adjust)
    entry = _append_settlement_entry(state, "buy", odno, delta=delta)
    new_T = _replay_settlement_ledger(state)
    state["T"] = new_T
    return new_T, entry


def _apply_sell_t_effect(state, T, net_qty, odno, cumulative_qty, basis_hint=None, allow_adjust=True):
    """
    매도 주문의 T 효과를 순서 원장에 반영하고 **누적 체결 기준**으로 판정합니다.

    - 첫 반영: 기준선(보유량)을 저장하고 원장에 sell 항목(factor)을 추가합니다.
    - 재분류: 같은 sell 항목의 factor 만 갱신한 뒤 start_T 부터 순서대로 재생합니다.
      (매수 +와 매도 ×의 순서가 보존되므로 중간 매수가 소급 스케일되지 않습니다)
    - 레거시 마커만 있고 매도 원장 항목이 없는 경우:
        * 같은 분류(factor 동일) → 이미 반영된 효과를 다시 곱하지 않고 T를 보존
        * 분류가 바뀜 → 적용 순서 근거가 없으므로 **보류**(재곱셈/역산 금지)

    Returns:
        tuple: (T, 반영여부)
    """
    effects = state.setdefault("sell_t_effects", {})
    entry = effects.get(str(odno)) or {}
    try:
        basis = int(entry.get("basis") or 0)
    except (TypeError, ValueError):
        basis = 0
    if basis <= 0 and basis_hint:
        basis = int(basis_hint)
    if basis <= 0:
        # 이 주문의 첫 체결 시점 보유량을 기준선으로 삼습니다.
        basis = int(net_qty)
    if basis <= 0:
        return T, False
    ratio = (cumulative_qty / basis) if basis else 0.0
    factor = _classify_sell_ratio(ratio)
    marker_factor = entry.get("factor")
    ledger_entry = _find_settlement_entry(state, kind="sell", odno=str(odno))
    if ledger_entry is not None:
        try:
            applied = float(ledger_entry.get("factor", 1.0))
        except (TypeError, ValueError):
            applied = 1.0
        if abs(factor - applied) < 1e-12:
            ledger_entry["cumulative_qty"] = int(cumulative_qty)
            effects[str(odno)] = {"basis": basis, "factor": factor}
            return T, False
    else:
        if marker_factor is not None:
            try:
                previous_factor = float(marker_factor)
            except (TypeError, ValueError):
                previous_factor = None
            if previous_factor is not None:
                if abs(factor - previous_factor) < 1e-12:
                    # 이미 반영된 같은 분류 — T를 다시 곱하지 않습니다.
                    effects[str(odno)] = {"basis": basis, "factor": factor}
                    return T, False
                _mark_settlement_hold(
                    state,
                    f"매도 {odno} 분류 변경({previous_factor}→{factor})에 대한 "
                    f"적용 순서 근거(원장)가 없습니다",
                )
                return T, False
    _sync_settlement_ledger(state, T, allow_adjust=allow_adjust)
    if ledger_entry is None:
        ledger_entry = _append_settlement_entry(
            state, "sell", odno, basis=basis, factor=factor,
            cumulative_qty=int(cumulative_qty),
        )
    else:
        ledger_entry["basis"] = basis
        ledger_entry["factor"] = factor
        ledger_entry["cumulative_qty"] = int(cumulative_qty)
    effects[str(odno)] = {"basis": basis, "factor": factor}
    new_T = _replay_settlement_ledger(state)
    state["T"] = new_T
    return new_T, True

def _fill_delta(meta, order, odno_meta=""):
    """
    주문의 체결 증가분(수량·금액)을 계산합니다. **메타를 변경하지 않습니다.**

    - 수량: 누적체결수량 − 반영된 수량 (정확)
    - 금액: 누적체결금액 − 반영된 금액 (확정값만)
      금액이 확정되지 않은 체결은 (수량, 0.0, True) 로 반환되어
      호출부가 정산을 **보류**합니다 (추정하지 않습니다).
      이전 버전 메타(반영금액 표식 없음)는 신규분만 비율로 계산해 1회 보정합니다.

    Returns:
        tuple: (신규체결수량, 신규체결금액, 금액미확정여부)
    """
    try:
        filled_now = int(float(order.get("ft_ccld_qty", "0") or 0))
    except (TypeError, ValueError):
        filled_now = 0
    amount_now, amount_estimated = _fill_amount(order)
    processed_qty = int((meta or {}).get("processed_filled_qty", 0) or 0)
    has_amount_marker = "processed_filled_amount" in (meta or {})
    processed_amount = float((meta or {}).get("processed_filled_amount", 0.0) or 0.0)
    delta_qty = max(filled_now - processed_qty, 0)
    if amount_estimated and delta_qty > 0:
        # 금액 미확정 — 호출부가 정산을 보류해야 합니다. 여기서는 반영하지 않습니다.
        return delta_qty, 0.0, True
    if has_amount_marker:
        delta_amount = amount_now - processed_amount
        if delta_amount < 0:
            # 브로커 누적금액이 역행했습니다 (정정 응답 등). 반영할 근거가 없으므로
            # 차감하지 않습니다 — 반복 차감으로 순투입금이 계속 내려가는 것을 막습니다.
            print(
                f"  → [경고] 누적체결금액이 반영분보다 작아 차감하지 않습니다 "
                f"(누적 ${amount_now:.2f} < 반영 ${processed_amount:.2f})"
            )
            delta_amount = 0.0
        estimated = False
    else:
        # 이전 메타: 반영된 수량에 해당하는 금액만 제외하고 신규분만 추정합니다.
        # (이전 버전이 반영한 금액 = 당시 누적금액 이므로, 동일 단가 가정하��
        #  반영 수량 비율로 복원하는 것이 기존 동작을 그대로 재현합니다)
        if delta_qty > 0 and filled_now > 0:
            delta_amount = amount_now * (delta_qty / filled_now)
            print(
                f"  → [경고] 주문 {odno_meta} 에 반영금액 표식이 없어 신규분 ${delta_amount:.2f} 를 "
                f"누적금액 비율로 추정했습니다 (1회)"
            )
        else:
            delta_amount = 0.0
        estimated = delta_qty > 0
    return delta_qty, delta_amount, estimated


def _commit_fill_markers(meta, order, applied_qty, applied_amount):
    """반영한 체결분�� 메타에 기록합니다 (다음 실행에서 중복 반영 방지)."""
    if meta is None:
        return
    try:
        filled_now = int(float(order.get("ft_ccld_qty", "0") or 0))
    except (TypeError, ValueError):
        filled_now = 0
    amount_now, amount_estimated = _fill_amount(order)
    if amount_estimated and filled_now > 0:
        # 금액 미확정 체결은 마커를 진행시키지 않습니다 (다음 실행에서 1회 반영).
        print(f"  [경고] 금액 미확정 주문 {order.get('odno', '')} 마커 갱신 보류")
        return
    meta["processed_filled_qty"] = max(int(meta.get("processed_filled_qty", 0) or 0), filled_now)
    meta["processed_filled_amount"] = max(
        float(meta.get("processed_filled_amount", 0.0) or 0.0), amount_now
    )
    if applied_qty:
        meta["processed_filled_qty"] = max(
            int(meta.get("processed_filled_qty", 0) or 0), filled_now
        )


def _parse_processed_qty_marker(meta):
    """
    processed_filled_qty 마커를 엄격히 판정합니다.

    - 키 누락, None, bool, 파싱 불가 값, 음수는 모두 무효로 분류합니다.
      (0으로 취급하면 이미 반영된 체결인지 판단할 수 없어 이중 반영 위험이 있습니다)
    - 명시적이고 유효한 0 이상 정수만 유효 마커로 인정합니다.
      (등록 직후 미반영 주문의 정상 상태)

    Returns:
        tuple: (유효 여부, 반영 수량 — 무효면 0)
    """
    if meta is None or "processed_filled_qty" not in meta:
        return False, 0
    raw = meta.get("processed_filled_qty")
    if raw is None or isinstance(raw, bool):
        return False, 0
    try:
        if isinstance(raw, float):
            # 소수 마커는 정수 마커로 절삭되면 안 됩니다 (예: 0.5 → 0 오인).
            if not raw.is_integer():
                return False, 0
            value = int(raw)
        else:
            value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return False, 0
    if value < 0:
        return False, 0
    return True, value


def _has_processed_qty_marker(meta):
    """
    processed_filled_qty 필드가 메타에 명시적이고 유효하게 존재하는지 확인합니다.

    필드가 누락되거나 무효한 경우(레거시/수동 편집 state.json)는 이미 반영된
    체결인지 여부를 판단할 수 없으므로 자동 복구 대상이 아님을 구분합니다.
    """
    valid, _ = _parse_processed_qty_marker(meta)
    return valid


def _apply_late_fill_deltas(state, order_history, skip_odnos):
    """
    워터마크보다 오래된 주문의 **누적 체결 증가분**을 계산합니다 (메타 변경 없음).

    전역 워터마크는 마지막으로 처리한 주문 시각입니다. 그 이전에 접수된 주문이
    나중에 추가 체결되면(부분체결 증가) 워터마크 기준으로 걸러내면서 반영분이
    영구히 사라집니다. 등록된 주문은 주문번호별 누적 체결량·금액으로 델타를 계산합니다.

    - recent_candidates 에 포함된 주문은 날짜 그룹 처리에서 반영되므로 건너뜁니다.
    - 등록 주문(orders_meta)이 아직 한 번도 반영되지 않았으면(processed=0),
      금액 미확정 보류로 워터마크가 앞서 나가도 영구 누락되지 않도록 복구합니다.
      (반영된 주문은 processed_filled_qty > 0 이므로 이 경로로 중복 반영되지 않습니다)
    - processed_filled_qty 필드가 누락된 레거시 메타는 이미 반영된 체결인지
      여부를 판단할 수 없으므로 자동 복구하지 않고 정산 보류 대상으로 반환합니다.

    Returns:
        tuple: (반영 대상 목록, 순수량 변화, 순금액 변화, 보류 대상 odno 목록)
    """
    orders_meta = state.get("orders_meta", {})
    if not orders_meta:
        return [], 0, 0.0, []
    history_by_odno = {str(o.get("odno", "")): o for o in (order_history or [])}
    pending: list[tuple[str, dict, dict, int, float]] = []
    held_odnos: list[str] = []
    net_qty = 0
    net_amount = 0.0
    for odno, meta in list(orders_meta.items()):
        if str(odno) in skip_odnos:
            continue
        row = history_by_odno.get(str(odno))
        if row is None:
            continue
        if not _fill_amount_is_available(row):
            print(
                f"  [경고] 지연 체결 odno={odno} 의 금액을 계산할 수 없어 "
                f"이번 정산에서 보류합니다 (다음 실행에서 금액 확정 시 반영)"
            )
            continue
        if _is_reverse_order(state, str(odno), row):
            # 리버스 주문은 reconcile_reverse_fills 가 담당합니다 (이중 반영 방지).
            continue
        has_marker, processed_qty = _parse_processed_qty_marker(meta)
        if processed_qty <= 0:
            try:
                filled_for_recovery = int(float(row.get("ft_ccld_qty", "0") or 0))
            except (TypeError, ValueError):
                filled_for_recovery = 0
            if filled_for_recovery <= 0:
                continue
            if not has_marker:
                # processed_filled_qty 필드가 누락된 레거시 메타는
                # 이미 반영된 체결인지 여부를 판단할 수 없으므로 자동 복구하지 않습니다.
                # 정산 보류 대상으로 남겨 다음 실행에서 감사/수동 확인을 받습니다.
                held_odnos.append(str(odno))
                print(
                    f"  [경고] 지연 체결 odno={odno} 는 processed_filled_qty 마커가 누락된 "
                    f"레거시 메타입니다. 자동 복구하지 않고 정산을 보류합니다."
                )
                continue
            # 등록된 봇 주문인데 아직 한 번도 반영되지 않은 체결입니다.
            # 이전 RUN 에서 금액 미확정으로 보류된 사이 워터마크가 뒤로 밀리면
            # "처리된 범위 밖"으로 영구 제외되므로, 금액이 확정된 지금 복구합니다.
            # (수량 마커가 0 이면 미반영이 확실 — 반영됐다면 processed_filled_qty > 0)
            print(
                f"  [상태] 지연 체결 복구: 워터마크 이전 미반영 등록 주문 "
                f"odno={odno} ({filled_for_recovery}주) → 이번 실행에서 반영"
            )
        dq, da, _est = _fill_delta(meta, row, str(odno))
        if dq <= 0 and abs(da) < 1e-9:
            continue
        pending.append((str(odno), meta, row, dq, da))
        if row.get("sll_buy_dvsn_cd_name") == "매도":
            net_qty -= dq
            net_amount -= da
        else:
            net_qty += dq
            net_amount += da
    return pending, net_qty, net_amount, held_odnos



def _apply_recent_history_dt(symbol, state, order_history, last_updated_dt, last_processed_ordno, balance_qty=None):
    """
    tz-aware한 최근 체결 반영 로직
    - order_history의 각 항목 `ord_datetime_utc`(ISO)를 파싱하여 UTC datetime으로 비교
    - 포함 조건: o_dt > last_updated_dt 또는 (o_dt == last_updated_dt 및 odno > last_processed_ordno)
    """
    apply_snapshot = deepcopy(state)
    pending_amount_odnos: list[str] = []
    pending_marker_odnos: list[str] = []
    recent_candidates = []

    for o in order_history:
        odt_iso = o.get("ord_datetime_utc")
        if not odt_iso:
            print(f"  [디버그] 최근모드 주문 제외(타임스탬프없음): "
                  f"odno={o.get('odno','')}, ord_dt={o.get('ord_dt','')}, "
                  f"ord_tmd={o.get('ord_tmd','')}, "
                  f"qty={o.get('ft_ccld_qty','0')}, "
                  f"side={o.get('sll_buy_dvsn_cd_name','')}")
            continue
        try:
            o_dt = datetime.fromisoformat(odt_iso)
            if o_dt.tzinfo is None:
                o_dt = o_dt.replace(tzinfo=ZoneInfo("UTC"))
            else:
                o_dt = o_dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            print(f"  [디버그] 최근모드 주문 제외(파싱실패): "
                  f"odno={o.get('odno','')}, odt_iso={odt_iso}")
            continue

        # 체결수량 없는 항목은 무시
        if int(float(o.get("ft_ccld_qty", "0"))) <= 0:
            print(f"  [디버그] 최근모드 주문 제외(체결수량0): "
                  f"odno={o.get('odno','')}, dt={odt_iso}, "
                  f"qty={o.get('ft_ccld_qty','0')}")
            continue

        # 리버스모드 주문은 주문번호 기반 reconciliation에서 처리합니다.
        if _is_reverse_order(state, o.get("odno", ""), o):
            continue

        odno = o.get("odno", "")
        include = False
        if o_dt > last_updated_dt:
            include = True
        elif o_dt == last_updated_dt:
            # 같은 시각이면 주문번호로 판별(숫자 비교 시도)
            try:
                if odno and last_processed_ordno:
                    include = int(odno) > int(last_processed_ordno)
                else:
                    include = bool(odno and odno != last_processed_ordno)
            except Exception:
                include = bool(odno and odno > last_processed_ordno)

        if include:
            recent_candidates.append((o_dt, odno, o))
        else:
            print(f"  [디버그] 최근모드 주문 제외(기간외): "
                  f"odno={o.get('odno','')}, o_dt={o_dt}, "
                  f"last_updated_dt={last_updated_dt}, "
                  f"odno={odno}, last_processed_ordno={last_processed_ordno}, "
                  f"qty={o.get('ft_ccld_qty','0')}, "
                  f"side={o.get('sll_buy_dvsn_cd_name','')}")

    # 이번 묶음에 포함된 체결의 금액이 확정됐는지 확인합니다.
    # processed_filled_qty 마커가 누락·무효인 등록 주문도 자동 반영하지 않고 보류합니다.
    for _o, odno_pending, order_pending in recent_candidates:
        if not _fill_amount_is_available(order_pending):
            if odno_pending and str(odno_pending) not in pending_amount_odnos:
                pending_amount_odnos.append(str(odno_pending))
            print(
                f"  [경고] 최근모드 주문 보류(체결금액 미확정): "
                f"odno={odno_pending}, qty={order_pending.get('ft_ccld_qty','0')}"
            )
            continue
        pending_meta = (state.get("orders_meta") or {}).get(str(odno_pending))
        if pending_meta is not None and not _has_processed_qty_marker(pending_meta):
            if odno_pending and str(odno_pending) not in pending_amount_odnos:
                pending_amount_odnos.append(str(odno_pending))
            if odno_pending and str(odno_pending) not in pending_marker_odnos:
                pending_marker_odnos.append(str(odno_pending))
            print(
                f"  [경고] 최근모드 주문 보류(마커 누락·무효): "
                f"odno={odno_pending}, qty={order_pending.get('ft_ccld_qty','0')}"
            )

    # 워터마크 이전 등록 주문의 지연 체결도 금액 확정이 필수입니다.
    # 마커 누락·무효 주문도 자동 반영하지 않고 보류합니다 (리버스 주문은 제외 —
    # 리버스 reconciliation이 별도 가드로 담당합니다).
    history_by_odno = {str(o.get("odno", "")): o for o in (order_history or [])}
    for odno, meta in (state.get("orders_meta") or {}).items():
        row = history_by_odno.get(str(odno))
        if row is None or not _fill_amount_is_available(row):
            if row is None:
                continue
            try:
                filled = int(float(row.get("ft_ccld_qty", "0") or 0))
                _, processed = _parse_processed_qty_marker(meta)
            except (TypeError, ValueError):
                continue
            if filled > processed and str(odno) not in pending_amount_odnos:
                pending_amount_odnos.append(str(odno))
            continue
        if _is_reverse_order(state, str(odno), row):
            continue
        try:
            filled = int(float(row.get("ft_ccld_qty", "0") or 0))
        except (TypeError, ValueError):
            continue
        if filled > 0 and not _has_processed_qty_marker(meta):
            if str(odno) not in pending_amount_odnos:
                pending_amount_odnos.append(str(odno))
            if str(odno) not in pending_marker_odnos:
                pending_marker_odnos.append(str(odno))
            print(
                f"  [경고] 지연 체결 주문 보류(마커 누락·무효): "
                f"odno={odno}, qty={row.get('ft_ccld_qty','0')}"
            )

    if pending_amount_odnos:
        state.clear()
        state.update(apply_snapshot)
        state["_pending_amount_odnos"] = pending_amount_odnos
        if pending_marker_odnos:
            state["_pending_marker_odnos"] = pending_marker_odnos
        _mark_settlement_hold(
            state,
            f"체결금액 미확정 또는 마커 누락·무효 주문 {len(pending_amount_odnos)}건 "
            f"({', '.join(pending_amount_odnos[:5])}) — 금액 확정 또는 마커 보정 후 재실행 필요",
        )
        return state, pending_amount_odnos

    # ── 1) 지연 체결 후보 (워터마크 이전 주문의 누적 체결 증가분) — 순수 계산 ──
    late_pending, late_net_qty, late_net_amount, late_held_odnos = _apply_late_fill_deltas(
        state, order_history, {str(odno) for _o, odno, _r in recent_candidates}
    )

    # processed_filled_qty 마커가 누락된 레거시 주문도 정산 보류 대상으로 처리합니다.
    if late_held_odnos:
        for odno in late_held_odnos:
            if odno not in pending_amount_odnos:
                pending_amount_odnos.append(odno)
            if odno not in pending_marker_odnos:
                pending_marker_odnos.append(odno)

    if pending_amount_odnos:
        state.clear()
        state.update(apply_snapshot)
        state["_pending_amount_odnos"] = pending_amount_odnos
        if pending_marker_odnos:
            state["_pending_marker_odnos"] = pending_marker_odnos
        _mark_settlement_hold(
            state,
            f"체결금액 미확정 또는 마커 누락 주문 {len(pending_amount_odnos)}건 "
            f"({', '.join(pending_amount_odnos[:5])}) — 금액 확정 또는 마커 보정 후 재실행 필요",
        )
        return state, pending_amount_odnos

    # ── 2) 이번 묶음의 반영분(수량·금액) 계산 — 순수 계산 ─────────────
    # 등록된 주문은 메타의 반영 마커와 비교해 **새로 늘어난 부분만** 계산합니다.
    # (부분체결이 나중에 늘어나도 중복 가산되지 않습니다)
    deltas: dict[str, tuple[int, float]] = {}
    batch_net_qty = 0
    for _o, odno, order in recent_candidates:
        meta = (state.get("orders_meta") or {}).get(str(odno))
        if meta is not None:
            dq, da, _est = _fill_delta(meta, order, str(odno))
        else:
            try:
                dq = int(float(order.get("ft_ccld_qty", "0") or 0))
            except (TypeError, ValueError):
                dq = 0
            da = _fill_amount(order)[0]
        deltas[str(odno)] = (dq, da)
        side = order.get("sll_buy_dvsn_cd_name", "")
        if side == "매수":
            batch_net_qty += dq
        elif side == "매도":
            batch_net_qty -= dq

    # ── 3) 매도 분류 기준선(순보유) 산출 ──────────────────────────
    # 이력 조회 구간이 보유 형성을 모두 담지 못하면(사이클 이전 매수·매도가 구간 밖이면)
    # 순보유수량이 과소 계산되어 쿼터매도가 목표매도로 잘못 분류됩니다.
    # (2026-10-21 실측: 실제 81주 → 이력 계산 30주 → 쿼터매도(20주)가 비율 0.67 로 오분류)
    #
    # 기준선 우선순위
    #   1) 브로커 실제 잔고 — **현재 시점** 보유량 → 이번 묶음 순변동을 빼서 기준 시점으로
    #   2) state.position_qty — **직전 워터마크 시점** 보유량 → 이미 기준 시점(지연체결분만 보정)
    #   3) 없음 → 기준 확보 실패 (매도 분류 불가 → 신규 주문 보류)
    position_anchor = None
    anchor_source = ""
    if balance_qty is not None:
        position_anchor = balance_qty - batch_net_qty
        anchor_source = "브로커 잔고"
    else:
        stored_qty = state.get("position_qty")
        if stored_qty is not None:
            try:
                position_anchor = int(stored_qty) + late_net_qty
                anchor_source = "저장 보유수량"
            except (TypeError, ValueError):
                position_anchor = None

    # ── 4) 매도 분류가 필요한데 기준이 없으면 보류 신호를 남깁니다 ──
    # 계산 자체는 계속 진행하되(기준선 없이 이력값으로), 이 결과는 **저장하면 안 됩니다**.
    # T·워터마크·반영 마커가 먼저 저장되면 다음 실행에서 재처리될 수 없기 때문입니다.
    # → trading_bot 이 state._position_basis_unknown 를 보고 저장·주문을 모두 보류합니다.
    sell_in_batch = any(
        order.get("sll_buy_dvsn_cd_name") == "매도"
        for _o, _odno, order in recent_candidates
    ) or any(
        row.get("sll_buy_dvsn_cd_name") == "매도" for _od, _m, row, _q, _a in late_pending
    )

    position_anchored = position_anchor is not None and position_anchor > 0
    if balance_qty is not None:
        # 워터마크 기준 보유량은 현재 잔고와 같습니다 (이번 체결은 곧 워터마크에 포함됨).
        # 체결이 없는 실행에서도 기준선이 생기도록 여기서 기록합니다.
        state["position_qty"] = int(balance_qty)
    if position_anchored:
        net_qty = position_anchor
        if position_anchor != _compute_net_qty_up_to(order_history, last_updated_dt):
            print(
                f"  → 보유수량 기준 보정: {anchor_source} 기준 {position_anchor}주 "
                f"— 매도 비율 판정용"
            )
    else:
        net_qty = _compute_net_qty_up_to(order_history, last_updated_dt)
        print(
            f"[경고] {symbol} 보유수량 기준을 확보하지 못했습니다 "
            f"(잔고 조회 실패 + 저장 보유수량 없음). 이력 계산 {net_qty}주를 기준선으로 "
            f"분류하되 매도 분류가 걸린 경우 신규 주문과 상태 저장을 보류합니다."
        )

    if not position_anchored:
        # 기준선이 없을 때만 이력값에 리버스 체결을 보정합니다.
        # (기준선이 있으면 브로커 잔고에 리버스 체결이 이미 반영되어 있어 중복 차감하지 않습니다)
        for order in order_history:
            odt_iso = order.get("ord_datetime_utc")
            if not odt_iso or not _is_reverse_order(state, order.get("odno", ""), order):
                continue
            try:
                reverse_dt = datetime.fromisoformat(odt_iso)
                if reverse_dt.tzinfo is None:
                    reverse_dt = reverse_dt.replace(tzinfo=ZoneInfo("UTC"))
                else:
                    reverse_dt = reverse_dt.astimezone(ZoneInfo("UTC"))
            except Exception:
                continue
            if reverse_dt <= last_updated_dt:
                continue
            filled = int(float(order.get("ft_ccld_qty", "0")))
            if order.get("sll_buy_dvsn_cd_name") == "매도":
                net_qty = max(0, net_qty - filled)
            elif order.get("sll_buy_dvsn_cd_name") == "매수":
                net_qty += filled

    # ── 5) 지연 체결 반영 (T·순투입금) ───────────────────────────
    # 적용 내역 기록 — 자동 검증이 원시 체결 데이터와 대조합니다.
    # 리버스 정산 내역을 **덮어쓰지 않고** 뒤에 이어 붙입니다.
    state["_settlement_deltas"].extend(state.pop("_reverse_settlement_deltas", []))

    T = float(state.get("T", 0.0))
    net_invested = float(state.get("net_invested", 0.0))
    # 지연 매도도 "배율 0"과 "실제 보유 0"을 구분해야 합니다.
    # 워터마크 이전 주문의 지연 체결을 시간순으로 재생해 주문 직후 보유량을 구합니다.
    # (net_qty 는 이미 지연 체결 순변동을 포함하므로 그만큼 되감아 시작합니다)
    _late_post_qty = {}
    _late_sim_qty = max(0, int(net_qty) - int(late_net_qty))
    for _odno, _meta, _row, _dq, _da in sorted(
        late_pending,
        key=lambda item: (
            str(item[2].get("ord_datetime_utc") or ""),
            str(item[0]),
        ),
    ):
        _side = _row.get("sll_buy_dvsn_cd_name")
        if _side == "매수":
            _late_sim_qty += _dq
        elif _side == "매도":
            _late_sim_qty = max(0, _late_sim_qty - _dq)
        _late_post_qty[str(_odno)] = _late_sim_qty
    for odno, meta, row, dq, da in late_pending:
        total_qty = int(meta.get("total_qty") or dq)
        t_target = float(meta.get("t_target", 0.0) or 0.0)
        t_before_late = T
        if row.get("sll_buy_dvsn_cd_name") == "매도":
            # 늦게 확인된 매도 — 누적 체결 기준으로 T 효과를 갱신합니다.
            # (일괄 반영과 부분 반영의 최종 결과가 같도록)
            cumulative_qty = int(float(row.get("ft_ccld_qty", "0") or 0))
            entry = (state.get("sell_t_effects") or {}).get(str(odno)) or {}
            basis = int(entry.get("basis") or 0)
            _raw_factor_before = entry.get("factor", 1.0)
            factor_before = (
                1.0 if _raw_factor_before is None else float(_raw_factor_before)
            )
            ledger_seq = None
            if basis > 0:
                T, _applied = _apply_sell_t_effect(
                    state, T, basis, odno, cumulative_qty, basis_hint=basis,
                    allow_adjust=False,
                )
                entry_now = (state.get("sell_t_effects") or {}).get(str(odno)) or {}
                ledger_entry = _find_settlement_entry(state, kind="sell", odno=str(odno))
                ledger_seq = ledger_entry.get("seq") if ledger_entry else None
                ratio = cumulative_qty / basis
                # 배율 0 은 "누적 매도 100%"일 뿐 사이클 종료가 아닙니다.
                # 주문 직후 실제 보유가 0 인 경우에만 종료합니다 (일반 매도 경로와 동일).
                _post_after = _late_post_qty.get(str(odno))
                _liquidated = _post_after is not None and _post_after <= 0
                if _classify_sell_ratio(ratio) == 0.0 and _liquidated:
                    completed_start = state.get("cycle_start_date", "")
                    if t_before_late > 0 and completed_start:
                        state["_completed_cycle_start"] = completed_start
                    state["cycle_start_date"] = ""
                    net_invested = 0.0
                    state["sell_t_effects"] = {}
                else:
                    # 보유가 남아 있으면 T 효과만 반영하고 순투입금만 차감합니다.
                    net_invested -= da
                    if _classify_sell_ratio(ratio) == 0.0:
                        print(
                            f"  [주의] 지연 매도 odno={odno} 누적 100%지만 "
                            f"보유 {max(0, int(_post_after or 0))}주가 남아 사이클을 종료하지 않습니다"
                        )
            else:
                # 기준선 없이 분류한 지연 매도는 신뢰할 수 없습니다 → 저장·주문 보류 신호
                # (기존 T를 되돌려 최초 기준을 역산하지 않습니다)
                state["_position_basis_unknown"] = True
                entry_now = {}
                print(
                    f"  [경고] 지연 매도 odno={odno} 의 보유수량 기준을 확보하지 못했습니다 "
                    f"→ 저장·신규 주문을 보류합니다"
                )
                net_invested -= da
            print(
                f"  → 지연 체결 반영: odno={odno} 매도 {dq}주 추가 "
                f"(누적 {cumulative_qty}주) → T={T}"
            )
            entry_now = (state.get("sell_t_effects") or {}).get(str(odno)) or {}
            state["_settlement_deltas"].append({
                "odno": str(odno),
                "side": "매도",
                "qty_delta": dq,
                "amount_delta": da,
                "cumulative_qty": cumulative_qty,
                "prev_cumulative_qty": max(cumulative_qty - dq, 0),
                "basis": int(entry_now.get("basis") or basis or 0),
                "factor": float(entry_now.get("factor", 0.0 if basis > 0 and _classify_sell_ratio(cumulative_qty / basis) == 0.0 else 1.0)),
                "factor_before": factor_before,
                "t_before": float(t_before_late),
                "ledger_seq": ledger_seq,
                "t_delta": round(T - t_before_late, 4),
                "late": True,
            })
        elif meta.get("is_additional"):
            net_invested += da
            print(f"  → 지연 체결 반영: odno={odno} 추가매수 {dq}주 → T 변경 없음")
            state["_settlement_deltas"].append({
                "odno": str(odno),
                "side": "매수",
                "qty_delta": dq,
                "amount_delta": da,
                "cumulative_qty": dq,
                "prev_cumulative_qty": 0,
                "basis": 0,
                "factor": 1.0,
                "is_additional": True,
                "t_delta": 0.0,
                "late": True,
            })
        else:
            delta_T = 0.0
            ledger_seq = None
            if total_qty > 0 and t_target:
                delta_T = round((dq / total_qty) * t_target, 4)
                T, ledger_entry = _apply_buy_t_effect(state, T, delta_T, odno, allow_adjust=False)
                ledger_seq = ledger_entry.get("seq") if ledger_entry else None
                print(
                    f"  → 지연 체결 반영: odno={odno} 추가체결 {dq}주 "
                    f"→ ΔT={delta_T} → T={T}"
                )
            net_invested += da
            state["_settlement_deltas"].append({
                "odno": str(odno),
                "side": "매수",
                "qty_delta": dq,
                "amount_delta": da,
                "cumulative_qty": dq,
                "prev_cumulative_qty": 0,
                "basis": 0,
                "factor": 1.0,
                "t_target": float(t_target),
                "total_qty": total_qty,
                "is_additional": False,
                "ledger_seq": ledger_seq,
                "t_delta": delta_T,
                "late": True,
            })
        _commit_fill_markers(meta, row, dq, da)

    state["T"] = T
    state["net_invested"] = round(max(0.0, net_invested), 2)
    late_applied = len(late_pending)

    if not recent_candidates and not late_applied:
        # 최근 이력이 없을 때의 처리.
        if state.get("T", 0) > 0:
            # 잔고 교차 검증으로 리셋 여부를 판별.
            # (조회 실패, 모의투자 체결 지연, 31일+ 정지 후 복귀 등에서 이력이 빈 리스트로
            #  반환될 수 있는데, 이때 T를 임의 리셋하면 대량 매수 사고로 이어진다.)
            if balance_qty is None:
                # 잔고 조회 실패/미확인 → 보수적으로 리셋 보류 (사고 방지 우선).
                print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 잔고 미확인 → T={state['T']} 유지 (보수적 보류)")
                print(f"[경고] {symbol} 이력이 없는데 잔고를 확인할 수 없어 T 리셋을 보류합니다. state.json과 잔고를 확인하세요.")
                return state, []
            if balance_qty > 0:
                # 보유 중 → 이력 조회 누락/정지 복귀로 보고 T 보존.
                # 리버스모드 진행 중이면 최근 체결이 전부 리버스 주문(reconciliation에서
                # 반영 완료)이라 일반 이력 경로 후보가 없는 것이 정상 → 경고 대신 안내만.
                if state.get("reverse_mode", {}).get("active"):
                    print(f"[상태] {symbol} {last_updated_dt.date()} 이후 일반모드 체결 없음 + 보유 {balance_qty}주 → T={state['T']} 유지 (리버스 체결은 reconciliation에서 반영)")
                else:
                    # 봇이 낸 주문이 이력에 보이면 조회 자체는 정상이고,
                    # 아직 체결이 없었을 뿐입니다 → 매일 나오는 경고가 되지 않도록 안내로 처리합니다.
                    # 봇 주문이 아예 이력에 없으면 조회 누락 가능성이 있으므로 경고를 유지합니다.
                    bot_order_ids = {str(k) for k in (state.get("orders_meta") or {})}
                    visible_bot_orders = any(
                        str(row.get("odno", "")) in bot_order_ids
                        for row in (order_history or [])
                    )
                    if visible_bot_orders:
                        print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 보유 {balance_qty}주 → T={state['T']} 유지 (주문은 이력에 보이나 아직 체결 안 됨)")
                    else:
                        print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 보유 {balance_qty}주 → T={state['T']} 유지 (이력 조회 누락/정지 복귀 가능)")
                        print(f"[경고] {symbol} 이력이 없는데 보유 중입니다. state.json과 잔고를 확인하세요.")
                return state, []
            # 잔고 0 → 잘못된 state.json(T>0, 잔고0, 이력0) 복구를 위해 전체 재추정.
            # 전체 이력도 비면 _infer_T_from_full_history가 T=0으로 리셋합니다.
            print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 잔고 0 → 전체 이력 재추정 시도 (T={state['T']})")
            reverse_baseline_t = state.get("T", 0.0)
            active_cycle_id = state.get("reverse_mode", {}).get("cycle_id", "")
            has_reverse_meta = any(
                meta.get("reverse_action")
                and not meta.get("repair_archived")
                and meta.get("cycle_id") == active_cycle_id
                for meta in state.get("orders_meta", {}).values()
            )
            state = _infer_T_from_full_history(symbol, state, order_history)
            if has_reverse_meta:
                state["T"] = reverse_baseline_t
                reset_settlement_ledger(state, reverse_baseline_t)
            reconcile_reverse_fills(state, order_history)
            state["_settlement_deltas"].extend(
                state.pop("_reverse_settlement_deltas", [])
            )
            return state, []
        print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 → T=0 유지")
        return state, []

    # 시간순, 주문번호순 정렬
    recent_candidates.sort(key=lambda tup: (tup[0], tup[1] or ""))

    # 적용 내역 기록 — 지연 체결분은 위에서 이미 기록했으므로 유지합니다.
    state.setdefault("_settlement_deltas", [])

    T = state.get("T", 0.0)
    additional_loc_odno = state.get("additional_loc_odno", [])
    net_invested = float(state.get("net_invested", 0.0))

    # ── 매도 분류 기준 수량(순보유) 산출 ──────────────────────────
    # 이력 조회 구간이 보유 형성을 모두 담지 못하면(사이클 이전 매수·매도가 구간 밖이면)
    # 순보유수량이 과소 계산되어 쿼터매도가 목표매도로 잘못 분류됩니다.
    # (2026-10-21 실측: 실제 81주 → 이력 계산 30주 → 쿼터매도(20주)가 비율 0.67 로 오분류)
    #
    # 기준선 우선순위
    #   1) 브로커 실제 잔고 — 가장 정확
    #   2) state 에 저장된 보유수량(직전 워터마크 시점) — 잔고 조회 실패 시 대체
    print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 {len(recent_candidates)}건 발견 → T값 업데이트 시작 (현재 T={T})")

    last_dt_processed = last_updated_dt
    last_ordno_processed = last_processed_ordno

    # 날짜(ord_dt)별로 그룹화하여 처리합니다
    orders_meta = state.get("orders_meta", {})
    orders_by_date = defaultdict(list)
    for o_dt, odno, order in recent_candidates:
        ord_dt = order.get("ord_dt", "")
        if ord_dt:
            orders_by_date[ord_dt].append((o_dt, odno, order))

    for ord_dt in sorted(orders_by_date.keys()):
        day_items = orders_by_date[ord_dt]
        day_items.sort(key=lambda tup: (tup[0], tup[1] or ""))

        for _, odno, order in day_items:
            if not _is_reverse_order(state, odno, order):
                continue
            filled = int(float(order.get("ft_ccld_qty", "0")))
            if order.get("sll_buy_dvsn_cd_name") == "매도":
                net_qty = max(0, net_qty - filled)
            elif order.get("sll_buy_dvsn_cd_name") == "매수":
                net_qty += filled

        # 일자 내 체결을 **시간순**으로 시뮬레이션해 각 주문 직후 보유량을 구합니다.
        # (매도/매수 처리 순서와 무관하게 실제 보유 궤적을 반영)
        _post_qty = {}
        _sim_qty = net_qty
        for _o_dt, _odno, _row in sorted(day_items, key=lambda x: (x[0], x[1] or "")):
            _delta = deltas.get(str(_odno), (0, 0.0))[0]
            _side = _row.get("sll_buy_dvsn_cd_name")
            if _side == "매수":
                _sim_qty += _delta
            elif _side == "매도":
                _sim_qty = max(0, _sim_qty - _delta)
            _post_qty[str(_odno)] = _sim_qty

        day_sells = [
            (o_dt, odno, o) for o_dt, odno, o in day_items
            if o.get("sll_buy_dvsn_cd_name") == "매도"
            and not _is_reverse_order(state, odno, o)
        ]
        day_buys = [
            (o_dt, odno, o) for o_dt, odno, o in day_items
            if o.get("sll_buy_dvsn_cd_name") == "매수"
            and not _is_reverse_order(state, odno, o)
        ]

        # 매도 처리: 보유수량 대비 비율로 쿼터매도 / 목표매도 / 전량매도 구분
        # 쿼터매도: 보유량의 ~25% → 비율 < 0.5 → T × 0.75
        # 목표매도: 보유량의 ~75% → 0.5 <= 비율 < 1.0 → T × 0.25
        # 전량매도: 보유량 100% → 비율 >= 1.0 → T = 0 (사이클 종료)
        for o_dt, odno, order in day_sells:
            meta = orders_meta.get(str(odno))
            sell_qty, sell_amt = deltas.get(str(odno), (0, 0.0))
            if sell_qty <= 0 and abs(sell_amt) < 1e-9:
                # 이미 반영된 매도 (부분체결 후 추가 체결 없음) → 재분류하지 않습니다.
                continue
            if not position_anchored:
                # 기준선 없이 분류한 매도는 신뢰할 수 없습니다 → 저장·주문 보류 신호
                state["_position_basis_unknown"] = True
            cumulative_qty = int(float(order.get("ft_ccld_qty", "0") or 0))
            kind = "쿼터매도"
            t_before_effect = T
            _old_entry = (state.get("sell_t_effects") or {}).get(str(odno)) or {}
            try:
                _factor_before = float(_old_entry.get("factor", 1.0))
            except (TypeError, ValueError):
                _factor_before = 1.0
            # 표시용 t_before 는 이번 적용 직전 T 입니다 (역산하지 않습니다).
            _t_before_base = t_before_effect
            liquidated = False
            if net_qty > 0:
                T, effect_applied = _apply_sell_t_effect(
                    state, T, net_qty, odno, cumulative_qty, allow_adjust=False
                )
                effects = state.get("sell_t_effects", {})
                entry = effects.get(str(odno)) or {}
                basis = int(entry.get("basis") or net_qty)
                ratio = (cumulative_qty / basis) if basis else 0.0
                kind = {0.75: "쿼터매도", 0.25: "목표매도", 0.0: "전량매도"}[
                    _classify_sell_ratio(ratio)
                ]
                # 배율 0 은 "누적 매도 100%"일 뿐 사이클 종료가 아닙니다.
                # 이번 신규 체결 후 실제 보유가 0인 경우에만 종료합니다.
                liquidated = _post_qty.get(str(odno), max(0, net_qty - sell_qty)) <= 0
                if kind == "전량매도" and liquidated:
                    completed_start = state.get("cycle_start_date", "")
                    if t_before_effect > 0 and completed_start:
                        state["_completed_cycle_start"] = completed_start
                    # 실제 전량매도 → 사이클 종료 (T·순투입금 0, 사이클 시작일 정리)
                    T = 0.0
                    state["cycle_start_date"] = ""
                    net_invested = 0.0
                print(
                    f"  → 매도 체결 ({ord_dt}): {kind} "
                    f"(비율={ratio:.2f}, 기준 {basis}주, 누적 {cumulative_qty}주, "
                    f"보유 {max(0, net_qty - sell_qty)}주) → T={T}"
                )
            else:
                # 보유 기준선이 없는 매도 — 기존 동작(쿼터매도)을 원장에 기록합니다.
                _sync_settlement_ledger(state, T, allow_adjust=False)
                _append_settlement_entry(state, "sell", odno, basis=0, factor=0.75)
                T = _replay_settlement_ledger(state)
                print(
                    f"  → 매도 체결 ({ord_dt}): 쿼터매도 (보유수량 불명) → T={T}"
                )
            # 순투입금 차감은 실제 체결 증가분만 (T 효과와 무관)
            # 실제 청산이면 위에서 0으로 확정했으므로 다시 차감하지 않습니다.
            if not (kind == "전량매도" and liquidated):
                net_invested -= sell_amt
            net_qty = max(0, net_qty - sell_qty)
            if meta is not None:
                _commit_fill_markers(meta, order, sell_qty, sell_amt)
            effects_now = state.get("sell_t_effects", {})
            entry_now = effects_now.get(str(odno)) or {}
            ledger_entry = _find_settlement_entry(state, kind="sell", odno=str(odno))
            state["_settlement_deltas"].append({
                "odno": str(odno),
                "side": "매도",
                "qty_delta": sell_qty,
                "amount_delta": sell_amt,
                "cumulative_qty": cumulative_qty,
                "prev_cumulative_qty": max(cumulative_qty - sell_qty, 0),
                "basis": int(entry_now.get("basis") or net_qty + sell_qty),
                "factor": 0.0 if kind == "전량매도" else float(entry_now.get("factor", 1.0)),
                "factor_before": float(_factor_before),
                "t_before": float(_t_before_base),
                "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                "t_delta": round(T - t_before_effect, 4),
            })
            if kind == "전량매도" and liquidated:
                state["sell_t_effects"] = {}
            if o_dt > last_dt_processed:
                last_dt_processed = o_dt
                last_ordno_processed = odno or last_ordno_processed
            elif o_dt == last_dt_processed and odno:
                last_ordno_processed = odno

        # 매수 처리
        # 우선순위: orders_meta.is_additional → additional_loc_odno(미등록) → orders_meta.t_target → 건수 폴백
        orders_meta = state.get("orders_meta", {})

        skip_buys   = [
            (o_dt, odno, o) for o_dt, odno, o in day_buys
            if str(odno) in additional_loc_odno or orders_meta.get(str(odno), {}).get("is_additional")
        ]
        normal_buys = [
            (o_dt, odno, o) for o_dt, odno, o in day_buys
            if str(odno) not in additional_loc_odno and not orders_meta.get(str(odno), {}).get("is_additional")
        ]

        for o_dt, odno, order in skip_buys:
            meta = orders_meta.get(str(odno))
            buy_qty, buy_amt = deltas.get(str(odno), (0, 0.0))
            if buy_qty <= 0 and abs(buy_amt) < 1e-9:
                continue
            net_qty += buy_qty
            net_invested += buy_amt
            if meta is not None:
                _commit_fill_markers(meta, order, buy_qty, buy_amt)
            state["_settlement_deltas"].append({
                "odno": str(odno),
                "side": "매수",
                "qty_delta": buy_qty,
                "amount_delta": buy_amt,
                "cumulative_qty": buy_qty,
                "prev_cumulative_qty": 0,
                "basis": 0,
                "factor": 1.0,
                "is_additional": True,
                "t_delta": 0.0,
            })
            print(f"  → 매수 체결 ({ord_dt}): 추가매수(odno={odno}) 제외 → T 변경 없음")
            if o_dt > last_dt_processed:
                last_dt_processed = o_dt
                last_ordno_processed = odno or last_ordno_processed
            elif o_dt == last_dt_processed and odno:
                last_ordno_processed = odno

        if normal_buys:
            if T == 0 and not state.get("cycle_start_date"):
                if len(ord_dt) == 8:
                    cycle_start = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                else:
                    cycle_start = ord_dt
                state["cycle_start_date"] = cycle_start
                print(f"  → 새 사이클 시작일 기록: {cycle_start}")

            # meta가 있는 주문: t_target 기반으로 각각 반영 (부분체결 비례 처리)
            meta_buys         = [(o_dt, odno, o) for o_dt, odno, o in normal_buys if orders_meta.get(str(odno))]
            unregistered_buys = [(o_dt, odno, o) for o_dt, odno, o in normal_buys if not orders_meta.get(str(odno))]
            if meta_buys:
                print(f"  [디버그] orders_meta 매칭: 정상매수 {len(meta_buys)}건")
            if unregistered_buys:
                print(f"  [디버그] orders_meta 미매칭(미등록매수): {len(unregistered_buys)}건")

            for o_dt, odno, order in meta_buys:
                meta = orders_meta[str(odno)]
                buy_qty, buy_amt = deltas.get(str(odno), (0, 0.0))
                if buy_qty <= 0 and abs(buy_amt) < 1e-9:
                    continue
                total_qty = int(meta.get("total_qty") or buy_qty)
                delta_T = 0.0
                ledger_seq = None
                if total_qty > 0:
                    delta_T = round(
                        (buy_qty / total_qty) * float(meta.get("t_target", 1.0)), 4
                    )
                    T, ledger_entry = _apply_buy_t_effect(state, T, delta_T, odno, allow_adjust=False)
                    ledger_seq = ledger_entry.get("seq") if ledger_entry else None
                    _commit_fill_markers(meta, order, buy_qty, buy_amt)
                    print(
                        f"  → 매수 체결 ({ord_dt}): odno={odno} "
                        f"t_target={meta.get('t_target')} "
                        f"부분체결({buy_qty}/{total_qty}) → ΔT={delta_T} → T={T}"
                    )
                net_qty += buy_qty
                net_invested += buy_amt
                state["_settlement_deltas"].append({
                    "odno": str(odno),
                    "side": "매수",
                    "qty_delta": buy_qty,
                    "amount_delta": buy_amt,
                    "cumulative_qty": buy_qty,
                    "prev_cumulative_qty": 0,
                    "basis": 0,
                    "factor": 1.0,
                    "t_target": float(meta.get("t_target", 1.0)),
                    "total_qty": total_qty,
                    "is_additional": False,
                    "ledger_seq": ledger_seq,
                    "t_delta": delta_T,
                })
                if o_dt > last_dt_processed:
                    last_dt_processed = o_dt
                    last_ordno_processed = odno or last_ordno_processed
                elif o_dt == last_dt_processed and odno:
                    last_ordno_processed = odno

            # meta가 없는 주문(미등록매수): 건수 기반 폴백
            if unregistered_buys:
                buy_count = len(unregistered_buys)
                delta_T = 1.0 if buy_count >= 2 else 0.5
                # 검증기가 T를 독립 재계산할 수 있게 주문별 기여분을 기록합니다.
                # (합계 수량 기준 비례 배분 — 합이 delta_T와 일치)
                _unreg_total = sum(
                    deltas.get(str(odno), (0, 0.0))[0] for _o, odno, _r in unregistered_buys
                )
                print(f"  → 매수 체결 ({ord_dt}): 미등록매수 {buy_count}건 → T += {delta_T} → T={T}")
                remaining_delta = delta_T
                for idx, (o_dt, odno, order) in enumerate(unregistered_buys):
                    buy_qty, buy_amt = deltas.get(str(odno), (0, 0.0))
                    net_qty += buy_qty
                    net_invested += buy_amt
                    if idx == len(unregistered_buys) - 1:
                        _share = round(remaining_delta, 4)
                    else:
                        _share = round(
                            (buy_qty / _unreg_total) * delta_T, 4
                        ) if _unreg_total > 0 else 0.0
                        remaining_delta = round(remaining_delta - _share, 4)
                    T, ledger_entry = _apply_buy_t_effect(state, T, _share, odno, allow_adjust=False)
                    state["_settlement_deltas"].append({
                        "odno": str(odno),
                        "side": "매수",
                        "qty_delta": buy_qty,
                        "amount_delta": buy_amt,
                        "cumulative_qty": buy_qty,
                        "prev_cumulative_qty": 0,
                        "basis": 0,
                        "factor": 1.0,
                        "t_target": float(delta_T),
                        "total_qty": int(_unreg_total),
                        "ord_dt": ord_dt,
                        "is_additional": False,
                        "ledger_seq": ledger_entry.get("seq") if ledger_entry else None,
                        "t_delta": _share,
                        "unregistered": True,
                    })
                    if o_dt > last_dt_processed:
                        last_dt_processed = o_dt
                        last_ordno_processed = odno or last_ordno_processed
                    elif o_dt == last_dt_processed and odno:
                        last_ordno_processed = odno

    state["T"] = round(T, 4)
    state["net_invested"] = round(max(0.0, net_invested), 2)
    # 워터마크 시점 보유수량 저장 — 다음 실행의 매도 비율 판정 기준선.
    # **워터마크 기준 보유량**입니다 (현재 잔고와 같습니다. 이번 묶음의 체결은
    # 이제 워터마크 안에 포함되므로, 다음 실행에서 다시 되감으면 안 됩니다).
    # 잔고 조회가 실패했으면 이번 묶음 반영 후 계산된 순보유로 기록합니다.
    if balance_qty is not None:
        state["position_qty"] = int(balance_qty)
    elif position_anchored:
        state["position_qty"] = int(net_qty)
    try:
        state["last_updated"] = last_dt_processed.isoformat()
        state["last_processed_ordno"] = last_ordno_processed or ""
    except Exception:
        pass

    print(f"[상태] {symbol} T값 업데이트 완료 → T={state['T']}, 순투입 ${state['net_invested']:.2f}")
    return state, []
