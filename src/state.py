# T값(매수 횟수)을 파일에 저장하고 불러오는 코드
# 무한매수법에서 T값은 "지금까지 몇 번이나 매수했는가"를 나타내는 숫자입니다.
# 프로그램이 종료되어도 T값을 잃지 않도록 JSON 파일에 보관합니다.
import json
import os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

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
            "last_processed_ordno": "",
            "additional_loc_odno": [],
            "orders_meta": {},
            "balance_mismatch": {},
            "state_version": "v2",
        }

    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            all_states = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[상태] {symbol} 상태 파일 읽기 실패 ({e}) → T=0으로 시작합니다")
        return {"T": 0.0, "last_updated": "", "cycle_start_date": "", "effective_seed": 0.0, "last_processed_ordno": ""}

    if symbol not in all_states:
        print(f"[상태] {symbol} 상태 기록 없음 → T=0으로 시작합니다")
        return {
            "T": 0.0,
            "last_updated": "",
            "cycle_start_date": "",
            "effective_seed": 0.0,
            "last_processed_ordno": "",
            "additional_loc_odno": [],
            "orders_meta": {},
            "balance_mismatch": {},
            "state_version": "v2",
        }

    state = all_states[symbol]
    T = float(state.get("T", 0.0))
    last_updated = state.get("last_updated", "")
    cycle_start_date = state.get("cycle_start_date", "")
    effective_seed = float(state.get("effective_seed", 0.0))
    last_processed_ordno = state.get("last_processed_ordno", "")
    additional_loc_odno = state.get("additional_loc_odno", [])
    orders_meta = state.get("orders_meta", {})
    balance_mismatch = state.get("balance_mismatch", {})
    state_version = state.get("state_version", "v1")

    print(f"[상태] {symbol} 상태 로드 완료 → T={T}, 마지막 갱신: {last_updated}")
    return {
        "T": T,
        "last_updated": last_updated,
        "cycle_start_date": cycle_start_date,
        "effective_seed": effective_seed,
        "last_processed_ordno": last_processed_ordno,
        "additional_loc_odno": additional_loc_odno,
        "orders_meta": orders_meta,
        "balance_mismatch": balance_mismatch,
        "state_version": state_version,
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
        except (json.JSONDecodeError, OSError):
            # 파일이 깨진 경우 새로 덮어씁니다
            all_states = {}

    # 이 종목 상태 업데이트
    # state_dict에 이미 'last_updated' 값이 있으면 그 값을 우선 사용합니다 (UTC ISO 권장).
    last_updated_val = state_dict.get("last_updated")
    if not last_updated_val:
        # 기본값: 현재 UTC 시각 ISO
        last_updated_val = datetime.now(ZoneInfo("UTC")).isoformat()

    # Allow optional new fields to be persisted for diagnostic purposes
    all_states[symbol] = {
        "T": float(state_dict.get("T", 0.0)),
        "last_updated": last_updated_val,
        "cycle_start_date": state_dict.get("cycle_start_date", ""),
        "effective_seed": float(state_dict.get("effective_seed", 0.0)),
        "last_processed_ordno": state_dict.get("last_processed_ordno", ""),
        "additional_loc_odno": state_dict.get("additional_loc_odno", []),
        "orders_meta": state_dict.get("orders_meta", {}),
        "balance_mismatch": state_dict.get("balance_mismatch", {}),
        "state_version": state_dict.get("state_version", "v2"),
    }

    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_states, f, ensure_ascii=False, indent=2)

    T = all_states[symbol]["T"]
    effective_seed = all_states[symbol]["effective_seed"]
    last_upd = all_states[symbol].get("last_updated", "")
    last_ordno = all_states[symbol].get("last_processed_ordno", "")
    mismatch = all_states[symbol].get("balance_mismatch")
    mismatch_flag = "YES" if mismatch else "NO"
    print(f"[상태] {symbol} 상태 저장 완료 → T={T}, effective_seed=${effective_seed:.2f}, last_updated={last_upd}, last_processed_ordno={last_ordno}, balance_mismatch={mismatch_flag}")


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
    state.setdefault("orders_meta", {})[str(odno)] = meta


def get_order_meta(state, odno):
    """state의 orders_meta에서 odno 메타를 반환하거나 None."""
    return state.get("orders_meta", {}).get(str(odno))


def update_T_from_history(symbol, state, order_history):
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

    Parameters:
        symbol (str): 종목 코드
        state (dict): 현재 상태 (T 포함) — 이 딕셔너리가 직접 수정됩니다
        order_history (list): get_overseas_order_history()의 반환값

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

    if last_updated_dt is None:
        # 초기 모드: 전체 이력에서 T를 처음부터 재계산합니다
        return _infer_T_from_full_history(symbol, state, order_history)

    # Safety net: T 오추정 상태에서 orders_meta가 있으면 full 재추정
    mismatch_note = state.get("balance_mismatch", {}).get("note")
    if mismatch_note == "T-estimation-suspected-low" and state.get("orders_meta"):
        print(f"[상태] {symbol} T 오추정 감지 → orders_meta({len(state['orders_meta'])}건) 활용 재추정")
        state.pop("balance_mismatch", None)
        return _infer_T_from_full_history(symbol, state, order_history)

    # 일반 모드: last_updated_dt 이후의 이력만 T에 반영합니다
    return _apply_recent_history_dt(symbol, state, order_history, last_updated_dt, last_processed_ordno)


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

        day_sells = [o for o in day_orders if o.get("sll_buy_dvsn_cd_name") == "매도"]
        day_buys  = [o for o in day_orders if o.get("sll_buy_dvsn_cd_name") == "매수"]

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

        orders_meta = state.get("orders_meta", {})

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
            meta_buys   = [o for o in normal_buys if orders_meta.get(str(o.get("odno", "")))]
            legacy_buys = [o for o in normal_buys if not orders_meta.get(str(o.get("odno", "")))]
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
            if legacy_buys:
                buy_count = len(legacy_buys)
                delta_T = 1.0 if buy_count >= 2 else 0.5
                T = round(T + delta_T, 4)
                for o in legacy_buys:
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

    net_qty = sum(l["qty"] for l in lots) if lots else 0
    avg_price = 0.0
    if net_qty > 0:
        total_val = sum(l["qty"] * l["price"] for l in lots)
        try:
            avg_price = total_val / net_qty
        except Exception:
            avg_price = 0.0

    return {"net_qty": int(net_qty), "avg_price": round(avg_price, 4), "buy_count": buy_count, "sell_count": sell_count}


def _apply_recent_history_dt(symbol, state, order_history, last_updated_dt, last_processed_ordno):
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
        # 출력은 기존처럼 날짜(문자열)로 간단 표시
        print(f"[상태] {symbol} {last_updated_dt.date()} 이후 체결 내역 없음 → T값 변경 없음 (T={state['T']})")
        return state

    # 시간순, 주문번호순 정렬
    recent_candidates.sort(key=lambda tup: (tup[0], tup[1] or ""))

    T = state.get("T", 0.0)
    additional_loc_odno = state.get("additional_loc_odno", [])

    # 매도 분류를 위해 기준 시점의 순보유수량을 미리 계산합니다
    net_qty = _compute_net_qty_up_to(order_history, last_updated_dt)

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

        day_sells = [(o_dt, odno, o) for o_dt, odno, o in day_items if o.get("sll_buy_dvsn_cd_name") == "매도"]
        day_buys  = [(o_dt, odno, o) for o_dt, odno, o in day_items if o.get("sll_buy_dvsn_cd_name") == "매수"]

        # 매도 처리: 보유수량 대비 비율로 쿼터매도 / 목표매도 / 전량매도 구분
        # 쿼터매도: 보유량의 ~25% → 비율 < 0.5 → T × 0.75
        # 목표매도: 보유량의 ~75% → 0.5 <= 비율 < 1.0 → T × 0.25
        # 전량매도: 보유량 100% → 비율 >= 1.0 → T = 0 (사이클 종료)
        for o_dt, odno, order in day_sells:
            sell_qty = int(float(order.get("ft_ccld_qty", "0")))
            if net_qty > 0:
                ratio = sell_qty / net_qty
                if ratio >= 1.0:
                    completed_start = state.get("cycle_start_date", "")
                    if T > 0 and completed_start:
                        state["_completed_cycle_start"] = completed_start
                    T = 0.0
                    state["cycle_start_date"] = ""
                    print(f"  → 매도 체결 ({ord_dt}): 전량매도 (비율={ratio:.2f}) → T=0")
                elif ratio >= 0.5:
                    T = round(T * 0.25, 4)
                    print(f"  → 매도 체결 ({ord_dt}): 목표매도 (비율={ratio:.2f}) → T={T}")
                else:
                    T = round(T * 0.75, 4)
                    print(f"  → 매도 체결 ({ord_dt}): 쿼터매도 (비율={ratio:.2f}) → T={T}")
            else:
                T = round(T * 0.75, 4)
                print(f"  → 매도 체결 ({ord_dt}): 쿼터매도 (보유수량 불명) → T={T}")
            net_qty = max(0, net_qty - sell_qty)
            if o_dt > last_dt_processed:
                last_dt_processed = o_dt
                last_ordno_processed = odno or last_ordno_processed
            elif o_dt == last_dt_processed and odno:
                last_ordno_processed = odno

        # 매수 처리
        # 우선순위: orders_meta.is_additional → additional_loc_odno(레거시) → orders_meta.t_target → 건수 폴백
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
            meta_buys   = [(o_dt, odno, o) for o_dt, odno, o in normal_buys if orders_meta.get(str(odno))]
            legacy_buys = [(o_dt, odno, o) for o_dt, odno, o in normal_buys if not orders_meta.get(str(odno))]

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
                if o_dt > last_dt_processed:
                    last_dt_processed = o_dt
                    last_ordno_processed = odno or last_ordno_processed
                elif o_dt == last_dt_processed and odno:
                    last_ordno_processed = odno

            # meta가 없는 주문(레거시): 건수 기반 폴백
            if legacy_buys:
                buy_count = len(legacy_buys)
                delta_T = 1.0 if buy_count >= 2 else 0.5
                T = round(T + delta_T, 4)
                print(f"  → 매수 체결 ({ord_dt}): 레거시 {buy_count}건 → T += {delta_T} → T={T}")
                for o_dt, odno, order in legacy_buys:
                    net_qty += int(float(order.get("ft_ccld_qty", "0")))
                    if o_dt > last_dt_processed:
                        last_dt_processed = o_dt
                        last_ordno_processed = odno or last_ordno_processed
                    elif o_dt == last_dt_processed and odno:
                        last_ordno_processed = odno

    state["T"] = round(T, 4)
    try:
        state["last_updated"] = last_dt_processed.isoformat()
        state["last_processed_ordno"] = last_ordno_processed or ""
    except Exception:
        pass

    print(f"[상태] {symbol} T값 업데이트 완료 → T={state['T']}")
    return state

