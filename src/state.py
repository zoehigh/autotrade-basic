# T값(매수 횟수)을 파일에 저장하고 불러오는 코드
# 무한매수법에서 T값은 "지금까지 몇 번이나 매수했는가"를 나타내는 숫자입니다.
# 프로그램이 종료되어도 T값을 잃지 않도록 JSON 파일에 보관합니다.
import json
import os
from datetime import datetime

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
        return {"T": 0.0, "last_updated": "", "cycle_start_date": "", "effective_seed": 0.0}

    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            all_states = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[상태] {symbol} 상태 파일 읽기 실패 ({e}) → T=0으로 시작합니다")
        return {"T": 0.0, "last_updated": "", "cycle_start_date": "", "effective_seed": 0.0}

    if symbol not in all_states:
        print(f"[상태] {symbol} 상태 기록 없음 → T=0으로 시작합니다")
        return {"T": 0.0, "last_updated": "", "cycle_start_date": "", "effective_seed": 0.0}

    state = all_states[symbol]
    T = float(state.get("T", 0.0))
    last_updated = state.get("last_updated", "")
    cycle_start_date = state.get("cycle_start_date", "")
    effective_seed = float(state.get("effective_seed", 0.0))
    print(f"[상태] {symbol} 상태 로드 완료 → T={T}, 마지막 갱신: {last_updated}")
    return {
        "T": T,
        "last_updated": last_updated,
        "cycle_start_date": cycle_start_date,
        "effective_seed": effective_seed,
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
    all_states[symbol] = {
        "T": float(state_dict.get("T", 0.0)),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_start_date": state_dict.get("cycle_start_date", ""),
        "effective_seed": float(state_dict.get("effective_seed", 0.0)),
    }

    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_states, f, ensure_ascii=False, indent=2)

    T = all_states[symbol]["T"]
    effective_seed = all_states[symbol]["effective_seed"]
    print(f"[상태] {symbol} 상태 저장 완료 → T={T}, effective_seed=${effective_seed:.2f}")


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
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 오늘 이미 갱신됐으면 건너뜁니다
    if last_updated.startswith(today_str):
        print(f"[상태] {symbol} 오늘 이미 T값이 갱신되어 있습니다 (T={state['T']})")
        return state

    if not last_updated:
        # 초기 모드: 전체 이력에서 T를 처음부터 재계산합니다
        return _infer_T_from_full_history(symbol, state, order_history)
    else:
        # 일반 모드: last_updated 이후의 이력만 T에 반영합니다
        return _apply_recent_history(symbol, state, order_history, last_updated)


def _infer_T_from_full_history(symbol, state, order_history):
    """
    전체 주문 이력을 처음부터 스캔하여 T값을 추정합니다.

    무상태 봇에서 4.0으로 업그레이드하거나 state.json이 없는 경우에 사용됩니다.
    순보유수량(net_qty)을 추적하여 전량매도(사이클 종료) 시점을 감지합니다.
    이전 사이클이 여러 번 있었어도 마지막 사이클의 T값만 반영합니다.
    """
    # 체결 완료된 주문만 추출하여 날짜/시간 오름차순(오래된 순) 정렬
    filled_orders = [
        o for o in order_history
        if int(float(o.get("ft_ccld_qty", "0"))) > 0
    ]

    if not filled_orders:
        print(f"[상태] {symbol} 초기 상태 - 이력 없음 → T=0으로 시작합니다")
        return state

    sorted_orders = sorted(
        filled_orders,
        key=lambda o: (o.get("ord_dt", ""), o.get("ord_tmd", ""))
    )

    print(f"[상태] {symbol} 초기 상태 감지 → 전체 이력 {len(sorted_orders)}건에서 T 자동 추정 시작")

    T = 0.0
    net_qty = 0          # 순보유수량: 매수 시 +, 매도 시 -
    cycle_start_ord_dt = ""

    for order in sorted_orders:
        buy_sell = order.get("sll_buy_dvsn_cd_name", "")
        qty = int(float(order.get("ft_ccld_qty", "0")))
        ord_dt = order.get("ord_dt", "")

        if buy_sell == "매수":
            if T == 0:
                cycle_start_ord_dt = ord_dt
            net_qty += qty
            T += 1.0

        elif buy_sell == "매도":
            net_qty -= qty
            T = T * 0.75

            # 순보유수량이 0 이하 = 전량매도 = 사이클 종료
            if net_qty <= 0:
                net_qty = 0
                T = 0.0
                cycle_start_ord_dt = ""

    # 사이클 시작일 설정 (state에 아직 없는 경우만)
    if cycle_start_ord_dt and len(cycle_start_ord_dt) == 8 and not state.get("cycle_start_date"):
        cycle_start = f"{cycle_start_ord_dt[:4]}-{cycle_start_ord_dt[4:6]}-{cycle_start_ord_dt[6:8]}"
        state["cycle_start_date"] = cycle_start

    state["T"] = round(T, 4)

    if T > 0:
        print(f"[상태] {symbol} T 추정 완료 → T={state['T']} (사이클 시작: {state.get('cycle_start_date', '알 수 없음')})")
        print("  ※ 자동 추정값입니다. 값이 틀리면 .state.json 파일에서 T를 직접 수정하세요.")
    else:
        print(f"[상태] {symbol} 이력 스캔 결과 현재 보유 없음 → T=0으로 시작합니다")

    return state


def _apply_recent_history(symbol, state, order_history, last_updated):
    """
    last_updated 날짜 이후의 체결 이력만 T값에 반영합니다.

    '어제' 날짜를 하드코딩하는 대신 last_updated 기준으로 필터링하므로
    월요일(어제=일요일 휴장), 공휴일 다음날 등의 상황을 자동으로 처리합니다.
    크래시로 save_state가 실패한 경우에도 누락된 매수/매도를 복구합니다.
    """
    # "2026-05-22 10:30:00" → "20260522" 형식으로 변환
    last_updated_yyyymmdd = last_updated[:10].replace("-", "")

    recent_orders = [
        o for o in order_history
        if o.get("ord_dt", "") > last_updated_yyyymmdd
        and int(float(o.get("ft_ccld_qty", "0"))) > 0
    ]

    if not recent_orders:
        print(f"[상태] {symbol} {last_updated[:10]} 이후 체결 내역 없음 → T값 변경 없음 (T={state['T']})")
        return state

    sorted_orders = sorted(
        recent_orders,
        key=lambda o: (o.get("ord_dt", ""), o.get("ord_tmd", ""))
    )

    T = state["T"]
    print(f"[상태] {symbol} {last_updated[:10]} 이후 체결 {len(sorted_orders)}건 발견 → T값 업데이트 시작 (현재 T={T})")

    for order in sorted_orders:
        buy_sell = order.get("sll_buy_dvsn_cd_name", "")
        ord_dt = order.get("ord_dt", "")

        if buy_sell == "매수":
            # 새 사이클의 첫 매수라면 사이클 시작일을 기록합니다
            if T == 0 and not state.get("cycle_start_date"):
                if len(ord_dt) == 8:
                    cycle_start = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                else:
                    cycle_start = ord_dt
                state["cycle_start_date"] = cycle_start
                print(f"  → 새 사이클 시작일 기록: {cycle_start}")

            # 추가매수 주문(급락 대비 1주씩)은 T값을 증가시키지 않습니다
            order_odno = order.get("odno", "")
            additional_loc_odno = state.get("additional_loc_odno", [])
            if order_odno and order_odno in additional_loc_odno:
                print(f"  → 매수 체결 ({ord_dt}): 추가매수 주문 제외 → T 변경 없음")
            else:
                T += 1.0
                print(f"  → 매수 체결 ({ord_dt}): T += 1 → T={T}")

        elif buy_sell == "매도":
            # 최종매도 감지 및 T 리셋은 trading_bot.py에서 position_qty 기준으로 처리합니다.
            T = T * 0.75
            print(f"  → 매도 체결 ({ord_dt}): T = T * 0.75 → T={T}")

    state["T"] = round(T, 4)
    print(f"[상태] {symbol} T값 업데이트 완료 → T={state['T']}")
    return state
