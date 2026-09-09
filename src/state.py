# T값(매수 횟수)을 파일에 저장하고 불러오는 코드
# 무한매수법에서 T값은 "지금까지 몇 번이나 매수했는가"를 나타내는 숫자입니다.
# 프로그램이 종료되어도 T값을 잃지 않도록 JSON 파일에 보관합니다.
import hashlib
import json
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
    resolved = resolve_real_kst_from_ord(ord_dt, ord_tmd)
    if resolved is None:
        return ord_dt
    return resolved.strftime("%Y%m%d")


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
    orders_meta = state.get("orders_meta", {})
    history_by_odno = {}
    for order in order_history:
        odno = str(order.get("odno", ""))
        if not odno:
            continue
        history_by_odno.setdefault(odno, []).append(order)
    events = []
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
            processed_qty = max(0, int(meta.get("processed_filled_qty", 0)))
            total_qty = max(0, int(meta.get("total_qty", 0)))
        except (TypeError, ValueError):
            continue
        if total_qty <= 0:
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

    for _, odno, meta, order, filled_qty, processed_qty, total_qty in sorted(events, key=lambda item: (item[0], item[1])):
        fill_fraction = min(filled_qty / total_qty, 1.0)
        observed_fraction = min(filled_qty / total_qty, 1.0)
        delta_fraction = 0.0
        action = meta.get("reverse_action")
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
            delta_fraction = max(0.0, observed_fraction - previous_fraction)
            base_t = float(meta.get("reverse_base_t", state.get("T", 0.0)))
            state["T"] = float(state.get("T", 0.0)) - base_t * (1.0 - factor) * delta_fraction
            confirmed_sell_proceeds += delta_amount
            # 리버스 매도 체결 → 순투입 감소 (매도대금 회수)
            net_invested -= delta_amount
            terminal = _is_terminal_reverse_order(order)
            status = str(order.get("prcs_stat_name", ""))
            if filled_qty > 0 and terminal and "거부" not in status:
                confirmed_sell_days.append(int(meta.get("reverse_day", 0) or 0))
            if terminal:
                meta["terminal"] = True
        elif action == "buy":
            previous_fraction = min(float(meta.get("applied_fill_fraction", 0.0)), 1.0)
            delta_fraction = max(0.0, observed_fraction - previous_fraction)
            state["T"] = float(state.get("T", 0.0)) + float(meta.get("reverse_t_target", 0.0)) * delta_fraction
            # 리버스 쿼터매수 체결 → 순투입 증가
            net_invested += delta_amount
            if _is_terminal_reverse_order(order):
                meta["terminal"] = True

        meta["processed_filled_qty"] = max(
            int(meta.get("processed_filled_qty", 0)), filled_qty
        )
        meta["applied_fill_fraction"] = min(
            1.0,
            float(meta.get("applied_fill_fraction", 0.0)) + delta_fraction,
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
    print(f"  → 리버스 체결 reconciliation 완료: T={state['T']}, day_count={reverse_mode.get('day_count', 0)}")


def update_T_from_history(symbol, state, order_history, balance_qty=None):
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
        reconcile_reverse_fills(state, order_history)
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
        reconcile_reverse_fills(candidate_state, order_history)
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
    reconcile_reverse_fills(state, order_history)
    return _apply_recent_history_dt(symbol, state, order_history, last_updated_dt, last_processed_ordno, balance_qty)


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
        side = o.get("sll_buy_dvsn_cd_name", "")
        if side == "매수":
            qty += filled
        elif side == "매도":
            qty -= filled
    return max(0, qty)


def _fill_amount(order):
    """주문 이력 항목의 체결금액(USD)을 안전하게 파싱합니다."""
    try:
        return float(order.get("ft_ccld_amt3", "0") or 0)
    except (TypeError, ValueError):
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
        try:
            amt = float(o.get("ft_ccld_amt3", "0") or 0)
        except (TypeError, ValueError):
            amt = 0.0
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
    # 체결 완료된 주문만 추출(타임스탬프가 있는 항목 우선)
    filled_orders = []
    for o in order_history:
        qty = int(float(o.get("ft_ccld_qty", "0")))
        dt_utc = o.get("ord_datetime_utc")
        if qty > 0 and dt_utc:
            filled_orders.append(o)
        else:
            reasons = []
            if qty <= 0:
                reasons.append(f"체결수량={o.get('ft_ccld_qty','0')}")
            if not dt_utc:
                reasons.append("ord_datetime_utc 없음")
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
            if net_qty > 0:
                ratio = sell_qty / net_qty
                if ratio >= 1.0:
                    if cycle_start_ord_dt and T > 0:
                        state["_completed_cycle_start"] = f"{cycle_start_ord_dt[:4]}-{cycle_start_ord_dt[4:6]}-{cycle_start_ord_dt[6:8]}"
                    T = 0.0
                    cycle_start_ord_dt = ""
                elif ratio >= 0.5:
                    T = round(T * 0.25, 4)
                else:
                    T = round(T * 0.75, 4)
            else:
                T = round(T * 0.75, 4)
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
                processed = int(meta.get("processed_filled_qty", 0))
                new_filled = max(0, qty - processed)
                if new_filled > 0 and total_qty > 0:
                    delta_T = round((new_filled / total_qty) * float(meta.get("t_target", 1.0)), 4)
                    T = round(T + delta_T, 4)
                    meta["processed_filled_qty"] = processed + new_filled
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
                T = round(T + delta_T, 4)
                for o in unregistered_buys:
                    qty = int(float(o.get("ft_ccld_qty", "0")))
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
    filled = [o for o in order_history if int(float(o.get("ft_ccld_qty", "0"))) > 0 and o.get("ord_datetime_utc")]
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


def _apply_recent_history_dt(symbol, state, order_history, last_updated_dt, last_processed_ordno, balance_qty=None):
    """
    tz-aware한 최근 체결 반영 로직
    - order_history의 각 항목 `ord_datetime_utc`(ISO)를 파싱하여 UTC datetime으로 비교
    - 포함 조건: o_dt > last_updated_dt 또는 (o_dt == last_updated_dt 및 odno > last_processed_ordno)
    """
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

    if not recent_candidates:
        # 최근 이력이 없을 때의 처리.
        if state.get("T", 0) > 0:
            # 잔고 교차 검증으로 리셋 여부를 판별.
            # (조회 실패, 모의투자 체결 지연, 31일+ 정지 후 복귀 등에서 이력이 빈 리스트로
            #  반환될 수 있는데, 이때 T를 임의 리셋하면 대량 매수 사고로 이어진다.)
            if balance_qty is None:
                # 잔고 조회 실패/미확인 → 보수적으로 리셋 보류 (사고 방지 우선).
                print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 잔고 미확인 → T={state['T']} 유지 (보수적 보류)")
                print(f"[경고] {symbol} 이력이 없는데 잔고를 확인할 수 없어 T 리셋을 보류합니다. state.json과 잔고를 확인하세요.")
                return state
            if balance_qty > 0:
                # 보유 중 → 이력 조회 누락/정지 복귀로 보고 T 보존.
                # 리버스모드 진행 중이면 최근 체결이 전부 리버스 주문(reconciliation에서
                # 반영 완료)이라 일반 이력 경로 후보가 없는 것이 정상 → 경고 대신 안내만.
                if state.get("reverse_mode", {}).get("active"):
                    print(f"[상태] {symbol} {last_updated_dt.date()} 이후 일반모드 체결 없음 + 보유 {balance_qty}주 → T={state['T']} 유지 (리버스 체결은 reconciliation에서 반영)")
                else:
                    print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 + 보유 {balance_qty}주 → T={state['T']} 유지 (이력 조회 누락/정지 복귀 가능)")
                    print(f"[경고] {symbol} 이력이 없는데 보유 중입니다. state.json과 잔고를 확인하세요.")
                return state
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
            reconcile_reverse_fills(state, order_history)
            return state
        print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 → T=0 유지")
        return state

    # 시간순, 주문번호순 정렬
    recent_candidates.sort(key=lambda tup: (tup[0], tup[1] or ""))

    T = state.get("T", 0.0)
    additional_loc_odno = state.get("additional_loc_odno", [])
    net_invested = float(state.get("net_invested", 0.0))

    # 매도 분류를 위해 기준 시점의 순보유수량을 미리 계산합니다
    net_qty = _compute_net_qty_up_to(order_history, last_updated_dt)
    for order in order_history:
        odt_iso = order.get("ord_datetime_utc")
        if not odt_iso or not _is_reverse_order(state, order.get("odno", ""), order):
            continue
        try:
            reverse_dt = datetime.fromisoformat(odt_iso)
            if reverse_dt.tzinfo is None:
                reverse_dt = reverse_dt.replace(tzinfo=ZoneInfo("UTC"))
            filled = int(float(order.get("ft_ccld_qty", "0")))
        except (TypeError, ValueError):
            continue
        if reverse_dt <= last_updated_dt:
            continue
        if order.get("sll_buy_dvsn_cd_name") == "매도":
            net_qty = max(0, net_qty - filled)
        elif order.get("sll_buy_dvsn_cd_name") == "매수":
            net_qty += filled

    print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 {len(recent_candidates)}건 발견 → T값 업데이트 시작 (현재 T={T})")

    last_dt_processed = last_updated_dt
    last_ordno_processed = last_processed_ordno

    # 날짜(ord_dt)별로 그룹화하여 처리합니다
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
            sell_qty = int(float(order.get("ft_ccld_qty", "0")))
            try:
                sell_amt = float(order.get("ft_ccld_amt3", "0") or 0)
            except (TypeError, ValueError):
                sell_amt = 0.0
            if net_qty > 0:
                ratio = sell_qty / net_qty
                if ratio >= 1.0:
                    completed_start = state.get("cycle_start_date", "")
                    if T > 0 and completed_start:
                        state["_completed_cycle_start"] = completed_start
                    T = 0.0
                    state["cycle_start_date"] = ""
                    net_invested = 0.0
                    print(f"  → 매도 체결 ({ord_dt}): 전량매도 (비율={ratio:.2f}) → T=0")
                elif ratio >= 0.5:
                    T = round(T * 0.25, 4)
                    net_invested -= sell_amt
                    print(f"  → 매도 체결 ({ord_dt}): 목표매도 (비율={ratio:.2f}) → T={T}")
                else:
                    T = round(T * 0.75, 4)
                    net_invested -= sell_amt
                    print(f"  → 매도 체결 ({ord_dt}): 쿼터매도 (비율={ratio:.2f}) → T={T}")
            else:
                T = round(T * 0.75, 4)
                net_invested -= sell_amt
                print(f"  → 매도 체결 ({ord_dt}): 쿼터매도 (보유수량 불명) → T={T}")
            net_qty = max(0, net_qty - sell_qty)
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
            net_qty += int(float(order.get("ft_ccld_qty", "0")))
            net_invested += _fill_amount(order)
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
                qty = int(float(order.get("ft_ccld_qty", "0")))
                total_qty = int(meta.get("total_qty") or qty)
                processed = int(meta.get("processed_filled_qty", 0))
                new_filled = max(0, qty - processed)
                if new_filled > 0 and total_qty > 0:
                    delta_T = round((new_filled / total_qty) * float(meta.get("t_target", 1.0)), 4)
                    T = round(T + delta_T, 4)
                    meta["processed_filled_qty"] = processed + new_filled
                    print(f"  → 매수 체결 ({ord_dt}): odno={odno} t_target={meta.get('t_target')} 부분체결({new_filled}/{total_qty}) → ΔT={delta_T} → T={T}")
                net_qty += qty
                net_invested += _fill_amount(order)
                if o_dt > last_dt_processed:
                    last_dt_processed = o_dt
                    last_ordno_processed = odno or last_ordno_processed
                elif o_dt == last_dt_processed and odno:
                    last_ordno_processed = odno

            # meta가 없는 주문(미등록매수): 건수 기반 폴백
            if unregistered_buys:
                buy_count = len(unregistered_buys)
                delta_T = 1.0 if buy_count >= 2 else 0.5
                T = round(T + delta_T, 4)
                print(f"  → 매수 체결 ({ord_dt}): 미등록매수 {buy_count}건 → T += {delta_T} → T={T}")
                for o_dt, odno, order in unregistered_buys:
                    net_qty += int(float(order.get("ft_ccld_qty", "0")))
                    net_invested += _fill_amount(order)
                    if o_dt > last_dt_processed:
                        last_dt_processed = o_dt
                        last_ordno_processed = odno or last_ordno_processed
                    elif o_dt == last_dt_processed and odno:
                        last_ordno_processed = odno

    state["T"] = round(T, 4)
    state["net_invested"] = round(max(0.0, net_invested), 2)
    try:
        state["last_updated"] = last_dt_processed.isoformat()
        state["last_processed_ordno"] = last_ordno_processed or ""
    except Exception:
        pass

    print(f"[상태] {symbol} T값 업데이트 완료 → T={state['T']}, 순투입 ${state['net_invested']:.2f}")
    return state
