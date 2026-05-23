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
        return {"T": 0.0, "last_updated": ""}

    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            all_states = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[상태] {symbol} 상태 파일 읽기 실패 ({e}) → T=0으로 시작합니다")
        return {"T": 0.0, "last_updated": ""}

    if symbol not in all_states:
        print(f"[상태] {symbol} 상태 기록 없음 → T=0으로 시작합니다")
        return {"T": 0.0, "last_updated": ""}

    state = all_states[symbol]
    T = float(state.get("T", 0.0))
    last_updated = state.get("last_updated", "")
    print(f"[상태] {symbol} 상태 로드 완료 → T={T}, 마지막 갱신: {last_updated}")
    return {"T": T, "last_updated": last_updated}


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
    }

    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_states, f, ensure_ascii=False, indent=2)

    T = all_states[symbol]["T"]
    print(f"[상태] {symbol} 상태 저장 완료 → T={T}")


def update_T_from_history(symbol, state, order_history):
    """
    어제 체결된 주문 이력을 바탕으로 T값을 업데이트합니다.

    매일 봇이 실행될 때, 전날 체결된 주문이 T값에 반영되지 않은 경우 자동으로 보정합니다.
    이미 오늘 이후 업데이트된 상태라면 아무것도 하지 않습니다.

    T값 계산 규칙 (무한매수법 V4.0):
      - 1회 매수 체결 (comment에 "1회" 포함) → T += 1
      - 절반 매수 체결 (comment에 "절반" 포함) → T += 0.5
      - 쿼터매도 체결 (comment에 "쿼터" 포함) → T = T * 0.75
      - 지정가 최종매도 체결 (comment에 "최종매도" 포함) → T 리셋 처리는 다음 매수 시 적용

    Parameters:
        symbol (str): 종목 코드
        state (dict): 현재 상태 (T 포함) — 이 딕셔너리가 직접 수정됩니다
        order_history (list): get_overseas_order_history()의 반환값

    Returns:
        dict: 업데이트된 state (입력과 동일한 객체)
    """
    symbol = symbol.upper()

    # 마지막 업데이트가 오늘이면 이미 반영된 것으로 간주하고 건너뜁니다
    last_updated = state.get("last_updated", "")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if last_updated.startswith(today_str):
        print(f"[상태] {symbol} 오늘 이미 T값이 갱신되어 있습니다 (T={state['T']})")
        return state

    # 어제 날짜 계산
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 어제 체결된 매수/매도 이력만 추출
    yesterday_orders = [
        o for o in order_history
        if o.get("ord_dt", "") == yesterday and int(float(o.get("ft_ccld_qty", "0"))) > 0
    ]

    if not yesterday_orders:
        print(f"[상태] {symbol} 어제({yesterday}) 체결 내역 없음 → T값 변경 없음 (T={state['T']})")
        return state

    T = state["T"]
    print(f"[상태] {symbol} 어제 체결 {len(yesterday_orders)}건 발견 → T값 업데이트 시작 (현재 T={T})")

    for order in yesterday_orders:
        buy_sell = order.get("sll_buy_dvsn_cd_name", "")
        comment = order.get("prcs_stat_name", "")

        if buy_sell == "매수":
            # 주문 코멘트로 1회 매수인지 절반 매수인지 구분
            # strategy.py에서 order의 comment 필드를 prcs_stat_name에 저장하지 않으므로
            # 현재는 모든 매수를 "1회 매수"로 처리합니다.
            # (추후 strategy.py의 반환값에 comment를 별도 저장하는 구조로 개선 가능)
            T += 1.0
            print(f"  → 매수 체결: T += 1 → T={T}")

        elif buy_sell == "매도":
            # 쿼터매도: 보유수량의 1/4 매도
            # 지정가 최종매도: 나머지 전량 매도
            # 현재는 모든 매도를 쿼터매도로 처리합니다.
            T = T * 0.75
            print(f"  → 매도 체결: T = T * 0.75 → T={T}")

    state["T"] = round(T, 4)
    print(f"[상태] {symbol} T값 업데이트 완료 → T={state['T']}")
    return state
