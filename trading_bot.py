"""
자동매매 봇 메인 실행 파일

이 프로그램은 다음 작업을 순서대로 수행합니다:
1. 환경변수에서 설정값을 읽어옵니다 (.env 파일)
2. 전략 함수를 실행하여 주문 목록을 생성하고 출력합니다
3. 생성된 주문을 실행합니다

프로그램 실행 중 발생하는 모든 에러는 catch되어 출력됩니다.
"""

import sys
import time

sys.path.append("src")

from datetime import datetime

from config import SYMBOLS, TRADE_MODE, COMMISSION_RATE, REINVEST
from strategy import 무한매수법_V4, adjust_price_to_tick
from state import load_state, save_state, update_T_from_history
from trader import (
    place_overseas_order,
    place_overseas_reservation_order,
    get_overseas_order_history,
    ReservationOrderRequired,
)
from notifier import notify


def generate_cycle_report(symbol, order_history, state, seed, commission_rate):
    """
    한 사이클이 종료되었을 때 수익 리포트를 생성합니다.

    사이클 시작일(cycle_start_date)부터 오늘까지의 체결 내역을 집계하여
    총 매수금액, 총 매도금액, 추정 수수료, 순수익금, 수익률을 계산합니다.

    Parameters:
        symbol (str): 종목 코드
        order_history (list): get_overseas_order_history()의 반환값
        state (dict): 현재 상태 (cycle_start_date, effective_seed 포함)
        seed (float): 이번 사이클에 사용된 시드 금액 (달러)
        commission_rate (float): 매매 수수료율 (예: 0.0025 = 0.25%)

    Returns:
        dict: 리포트 결과
            - total_buy_amount: 총 매수금액
            - total_sell_amount: 총 매도금액
            - estimated_commission: 추정 수수료 (매수 + 매도)
            - net_profit: 순수익금 (수수료 차감)
            - operational_return_pct: 운용 수익률 (%)
            - seed_return_pct: 시드 대비 수익률 (%, seed > 0 인 경우)
            - buy_count: 매수 체결 건수
            - sell_count: 매도 체결 건수
            - cycle_start_date: 사이클 시작일
            - cycle_end_date: 사이클 종료일 (오늘)
            - next_cycle_seed: 복리 재투자 시 다음 사이클 시드
    """
    cycle_start_date = state.get("cycle_start_date", "")
    cycle_end_date = datetime.now().strftime("%Y-%m-%d")

    # cycle_start_date를 YYYYMMDD 형식으로 변환하여 필터링에 사용
    if cycle_start_date:
        start_yyyymmdd = cycle_start_date.replace("-", "")
        cycle_orders = [
            o for o in order_history
            if o.get("ord_dt", "") >= start_yyyymmdd
            and int(float(o.get("ft_ccld_qty", "0"))) > 0
        ]
    else:
        # 사이클 시작일 정보가 없으면 체결된 전체 이력을 사용합니다
        cycle_orders = [
            o for o in order_history
            if int(float(o.get("ft_ccld_qty", "0"))) > 0
        ]

    total_buy_amount = 0.0
    total_sell_amount = 0.0
    buy_count = 0
    sell_count = 0

    for order in cycle_orders:
        qty = float(order.get("ft_ccld_qty", "0"))
        price = float(order.get("ft_ccld_unpr3", "0"))
        amount = qty * price

        if order.get("sll_buy_dvsn_cd_name") == "매수":
            total_buy_amount += amount
            buy_count += 1
        elif order.get("sll_buy_dvsn_cd_name") == "매도":
            total_sell_amount += amount
            sell_count += 1

    # 수수료는 매수와 매도 양방향 모두 부과됩니다
    estimated_commission = (total_buy_amount + total_sell_amount) * commission_rate
    net_profit = total_sell_amount - total_buy_amount - estimated_commission

    operational_return_pct = None
    if total_buy_amount > 0:
        operational_return_pct = (net_profit / total_buy_amount) * 100

    seed_return_pct = None
    if seed > 0:
        seed_return_pct = (net_profit / seed) * 100

    next_cycle_seed = (seed + net_profit) if seed > 0 else None

    return {
        "symbol": symbol,
        "cycle_start_date": cycle_start_date if cycle_start_date else "(기록 없음)",
        "cycle_end_date": cycle_end_date,
        "total_buy_amount": total_buy_amount,
        "total_sell_amount": total_sell_amount,
        "estimated_commission": estimated_commission,
        "commission_rate_pct": commission_rate * 100,
        "net_profit": net_profit,
        "operational_return_pct": operational_return_pct,
        "seed_return_pct": seed_return_pct,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "seed": seed,
        "next_cycle_seed": next_cycle_seed,
    }


def format_cycle_report_message(report):
    """
    사이클 리포트 dict를 텔레그램 메시지 문자열로 변환합니다.

    Parameters:
        report (dict): generate_cycle_report()의 반환값

    Returns:
        str: 텔레그램에 전송할 메시지 문자열
    """
    symbol = report["symbol"]
    start = report["cycle_start_date"]
    end = report["cycle_end_date"]
    buy_amt = report["total_buy_amount"]
    sell_amt = report["total_sell_amount"]
    commission = report["estimated_commission"]
    commission_pct = report["commission_rate_pct"]
    net = report["net_profit"]
    buy_cnt = report["buy_count"]
    sell_cnt = report["sell_count"]
    seed = report["seed"]
    next_seed = report["next_cycle_seed"]
    op_return = report["operational_return_pct"]
    seed_return = report["seed_return_pct"]

    profit_sign = "+" if net >= 0 else ""
    lines = [
        f"🏁 사이클 종료 — {symbol}",
        "",
        f"기간: {start} ~ {end}",
        f"매수 {buy_cnt}회 / 매도 {sell_cnt}회",
        "",
        f"총 매수금액:  ${buy_amt:>10,.2f}",
        f"총 매도금액:  ${sell_amt:>10,.2f}",
        f"추정 수수료:  ${commission:>10,.2f}  ({commission_pct:.2f}% × 매수·매도)",
        "─" * 36,
        f"순수익금:     ${profit_sign}{net:>9,.2f}",
    ]

    if op_return is not None:
        lines.append(f"운용 수익률:  {profit_sign}{op_return:.2f}%")

    if seed > 0 and seed_return is not None:
        lines.append(f"시드 대비:    {profit_sign}{seed_return:.2f}%  (시드 ${seed:,.0f})")

    if next_seed is not None:
        reinvest_label = "복리 적용" if REINVEST else "참고값"
        lines.append("")
        lines.append(f"다음 사이클 시드: ${next_seed:,.2f}  ({reinvest_label})")

    return "\n".join(lines)


def convert_exchange_code(exchange_code):
    """
    거래소 코드를 주문 API용 코드로 변환합니다.

    조회용 거래소 코드와 주문용 거래소 코드가 다릅니다.
    예: NAS (조회용) -> NASD (주문용)

    Parameters:
        exchange_code (str): 조회용 거래소 코드 (예: "NAS", "NYS")

    Returns:
        str: 주문용 거래소 코드 (예: "NASD", "NYSE")
    """
    exchange_map = {
        "NAS": "NASD",  # 나스닥
        "NYS": "NYSE",  # 뉴욕
        "AMS": "AMEX",  # 아멕스
        "HKS": "SEHK",  # 홍콩
        "TSE": "TKSE",  # 도쿄
        "SHS": "SHAA",  # 상해
        "SZS": "SZAA",  # 심천
    }

    return exchange_map.get(exchange_code, exchange_code)


def run_one_symbol(symbol_config):
    """
    단일 종목에 대해 전략을 실행하고 주문을 넣는 함수입니다.

    Parameters:
        symbol_config (dict): 종목별 설정
            - symbol (str): 종목 코드 (예: "TQQQ")
            - exchange (str): 거래소 코드 (예: "NAS")
            - splits (int): 분할 수
            - symbol_type (str): 종목 타입 (예: "TQQQ", "SOXL")
            - seed (float): 투입 시드 금액 (0이면 계좌 전체 사용)
    """
    symbol = symbol_config["symbol"]
    exchange = symbol_config["exchange"]
    splits = symbol_config["splits"]
    symbol_type = symbol_config["symbol_type"]
    seed = symbol_config["seed"]

    print(f"\n{'=' * 60}")
    print(f"종목 처리 시작: {symbol} ({exchange})")
    print(f"{'=' * 60}")

    # ── Step 1: T값 로드 및 어제 체결 반영 ──────────────────────
    print("\n[Step 1] T값 로드 중...")

    state = load_state(symbol)

    # 주문 이력 조회 기간을 상황에 맞게 계산합니다
    cycle_start_date = state.get("cycle_start_date", "")
    last_updated = state.get("last_updated", "")

    if not last_updated:
        # 초기 상태(처음 실행 또는 업그레이드 직후): 이전 이력에서 T를 추정하기 위해 넉넉하게 조회합니다
        # 5~6페이지 × 20건 = 약 100~120건을 확보합니다
        history_days = 90
    elif cycle_start_date:
        # 사이클 시작일 기준으로 전체 사이클 이력을 조회합니다
        try:
            start_dt = datetime.strptime(cycle_start_date, "%Y-%m-%d")
            days_since_start = (datetime.now() - start_dt).days + 5
            history_days = max(days_since_start, 30)
        except ValueError:
            history_days = 30
    else:
        history_days = 30

    order_history = get_overseas_order_history(symbol, exchange, days=history_days)
    state = update_T_from_history(symbol, state, order_history)

    T = state["T"]
    print(f"  현재 T값: {T}")

    # ── 복리 재투자: effective_seed가 있으면 env 시드 대신 사용 ──
    effective_seed = state.get("effective_seed", 0.0)
    if REINVEST and effective_seed > 0:
        seed = effective_seed
        print(f"  복리 재투자 적용: 이번 사이클 시드 = ${seed:.2f}")

    # ── Step 2: 전략 실행 ────────────────────────────────────────
    print("\n[Step 2] 전략 실행 중...")

    strategy_result = 무한매수법_V4(
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=seed,
        T=T,
    )

    print("✓ 전략 실행 완료")
    print(f"  현재가: ${strategy_result['last_price']}")
    print(f"  보유 수량: {strategy_result['position_qty']}주")
    print(f"  평단가: ${strategy_result['avg_price']}")
    print(f"  주문 가능 금액: ${strategy_result['orderable_cash']:.2f}")
    print(f"  T값: {T} / {splits}")
    if strategy_result['star_point']:
        print(f"  별지점: ${strategy_result['star_point']:.2f}")

    # ── 사이클 종료 감지 ─────────────────────────────────────────
    # T > 0인데 보유 수량이 0이면 최종매도가 체결된 것으로 판단합니다.
    position_qty = strategy_result["position_qty"]
    if T > 0 and position_qty == 0:
        print(f"\n{'=' * 60}")
        print(f"🏁 {symbol} 사이클 종료 감지 (T={T}, 보유수량=0)")
        print(f"{'=' * 60}")

        report = generate_cycle_report(
            symbol=symbol,
            order_history=order_history,
            state=state,
            seed=seed,
            commission_rate=COMMISSION_RATE,
        )

        report_message = format_cycle_report_message(report)
        print(f"\n{report_message}")
        notify(report_message)

        # 복리 재투자가 활성화된 경우 다음 사이클 시드를 state에 저장합니다.
        # 손실이 발생한 경우에도 변경된 시드를 저장합니다.
        if REINVEST and report["next_cycle_seed"] is not None:
            state["effective_seed"] = round(report["next_cycle_seed"], 2)
            print(f"  복리 재투자: 다음 사이클 시드 = ${state['effective_seed']:.2f}")
        else:
            state["effective_seed"] = 0.0

        # T 초기화 및 사이클 시작일 리셋
        state["T"] = 0.0
        state["cycle_start_date"] = ""
        save_state(symbol, state)

        print("  T값 초기화 완료. 다음 실행 시 새 사이클이 시작됩니다.")
        return

    orders = strategy_result["orders"]

    print(f"\n[Step 3] 생성된 주문 목록 ({len(orders)}개)")
    print("-" * 60)

    if len(orders) == 0:
        print("생성된 주문이 없습니다.")
        return

    for i, order in enumerate(orders, 1):
        print(f"\n주문 {i}:")
        print(f"  설명: {order['comment']}")
        print(f"  매수/매도: {order['side']}")
        print(f"  주문 유형: {order['order_type']}")
        print(f"  수량: {order['quantity']}주")
        if order["price"]:
            print(f"  가격: ${order['price']}")
        else:
            print("  가격: 시장가")

    print("\n[Step 4] 주문 실행 중...")
    print("-" * 60)

    order_exchange_code = convert_exchange_code(exchange)

    executed_orders = []
    reserved_orders = []
    failed_orders = []

    for i, order in enumerate(orders, 1):
        print(f"\n주문 {i}/{len(orders)} 실행: {order['comment']}")

        try:
            order_price = order["price"] if order["price"] else 0

            # BUY LOC 가격 보정: 현재가 대비 +20% 초과 시 브로커가 주문을 거부하므로 미리 조정합니다
            if order["side"] == "BUY" and order_price > 0:
                strategy_last_price = strategy_result.get("last_price", 0)
                if strategy_last_price > 0:
                    max_allowed_price = strategy_last_price * 1.19
                    if order_price > max_allowed_price:
                        corrected_price = adjust_price_to_tick(max_allowed_price)
                        print(f"  ⚠️ LOC 가격 보정: ${order_price:.2f} → ${corrected_price:.2f} (현재가 ${strategy_last_price:.2f} × 1.19 기준)")
                        notify(f"{symbol} ⚠️ LOC 가격 보정\n원가격: ${order_price:.2f}\n현재가: ${strategy_last_price:.2f}\n보정가: ${corrected_price:.2f}")
                        order_price = corrected_price

            result = place_overseas_order(
                symbol=symbol,
                exchange_code=order_exchange_code,
                order_type=order["order_type"],
                quantity=order["quantity"],
                price=order_price,
                side=order["side"],
                trade_mode=TRADE_MODE,
            )

            if result:
                executed_orders.append(
                    {
                        "comment": order["comment"],
                        "odno": result["odno"],
                        "ord_tmd": result["ord_tmd"],
                    }
                )

                # 추가매수 주문은 T값 증가 대상에서 제외되므로 주문번호를 상태에 기록합니다
                if "[추가매수]" in order.get("comment", ""):
                    state.setdefault("additional_loc_odno", []).append(result["odno"])

                print("✓ 주문 성공")

                message = f"""✅ 주문 성공 {symbol}

{order['comment']}
수량: {order['quantity']}주
주문번호: {result['odno']}
시각: {result['ord_tmd']}"""
                notify(message)
            else:
                print("✓ 주문 정보 출력 완료")

        except ReservationOrderRequired:
            print("ℹ️  정규장 외 시간 — 예약주문으로 접수합니다. (/trading/order-resv)")

            ord_dv = "usSell" if order["side"] == "SELL" else "usBuy"

            if TRADE_MODE == "DRY":
                print(
                    f"[DRY] 예약주문 정보: {symbol}, {order_exchange_code}, "
                    f"side={order['side']}, qty={order['quantity']}, price={order_price}"
                )
            else:
                try:
                    resv_result = place_overseas_reservation_order(
                        symbol=symbol,
                        exchange_code=order_exchange_code,
                        quantity=order["quantity"],
                        price=order_price,
                        ord_dv=ord_dv,
                    )
                    reserved_orders.append(
                        {
                            "comment": order["comment"],
                            "odno": resv_result["odno"],
                            "rsvn_ord_rcit_dt": resv_result["rsvn_ord_rcit_dt"],
                        }
                    )
                    print(f"✓ 예약주문 접수 완료 (주문번호: {resv_result['odno']})")

                    message = f"""📋 예약주문 접수 {symbol}

{order['comment']}
수량: {order['quantity']}주
예약주문번호: {resv_result['odno']}
접수일자: {resv_result['rsvn_ord_rcit_dt']}"""
                    notify(message)
                except Exception as reservation_error:
                    print(f"✗ 예약주문 실패: {str(reservation_error)}")
                    failed_orders.append(
                        {
                            "comment": order["comment"],
                            "error": f"예약주문 실패: {str(reservation_error)}",
                        }
                    )
                    notify(
                        f"{symbol} ⚠️ 예약주문 실패\n\n{order['comment']}\n에러: {str(reservation_error)}",
                        urgent=True,
                    )

        except Exception as error:
            print(f"✗ 주문 실패: {str(error)}")
            failed_orders.append(
                {
                    "comment": order["comment"],
                    "error": str(error),
                }
            )

            message = f"""⚠️ 주문 실패 {symbol}

{order['comment']}
에러: {str(error)}"""
            notify(message, urgent=True)
            continue

    print("\n" + "=" * 60)
    print(f"{symbol} 처리 완료")
    print("=" * 60)

    # ── Step 4: T값 저장 (LIVE 주문이 하나라도 성공한 경우에만) ──
    if TRADE_MODE == "LIVE" and len(executed_orders) > 0:
        save_state(symbol, state)

    if TRADE_MODE == "DRY":
        print("\n💡 DRY 모드로 실행되었습니다.")
        print("   실제 주문은 실행되지 않았으며, 주문 정보만 출력되었습니다.")
        print(f"   총 {len(orders)}개 주문이 처리되었습니다.")
        print("\n   실제 주문을 하려면 .env 파일에서 TRADE_MODE=LIVE로 설정하세요.")
    else:
        print("\n✓ LIVE 모드로 실행되었습니다.")
        print(f"   총 {len(orders)}개 주문 중:")
        print(f"   - 체결 성공: {len(executed_orders)}개")
        print(f"   - 예약 접수: {len(reserved_orders)}개")
        print(f"   - 실패:     {len(failed_orders)}개")

    if executed_orders:
        print("\n[체결 주문]")
        for order in executed_orders:
            print(f"  ✓ {order['comment']}: 주문번호 {order['odno']} (시각: {order['ord_tmd']})")

    if reserved_orders:
        print("\n[예약 주문] - 다음 정규장 시작 시 체결됩니다")
        for order in reserved_orders:
            print(
                f"  📋 {order['comment']}: 예약번호 {order['odno']} "
                f"(접수일자: {order['rsvn_ord_rcit_dt']})"
            )

    if failed_orders:
        print("\n[실패한 주문]")
        for order in failed_orders:
            print(f"  ✗ {order['comment']}: {order['error']}")


def main():
    """
    자동매매 봇의 메인 실행 함수입니다.

    SYMBOLS 설정에 있는 종목을 순서대로 처리합니다.
    한 종목이 실패해도 나머지 종목은 계속 처리됩니다.
    """
    try:
        print("\n" + "=" * 60)
        print("자동매매 봇 시작")
        print("=" * 60)

        notify("🚀 자동매매 시작")

        # ========================================
        # 설정 정보 출력
        # ========================================
        print("\n[설정 정보]")
        print(f"거래 모드: {TRADE_MODE}")
        print("종목 목록:")
        for cfg in SYMBOLS:
            seed_info = f", 시드: ${cfg['seed']:.0f}" if cfg['seed'] > 0 else ""
            print(f"  - {cfg['symbol']}({cfg['exchange']}): "
                  f"분할={cfg['splits']}, "
                  f"타입={cfg['symbol_type']}"
                  f"{seed_info}")

        # ========================================
        # 종목별 순차 처리
        # ========================================
        for symbol_config in SYMBOLS:
            try:
                run_one_symbol(symbol_config)
            except Exception as error:
                # 한 종목이 실패해도 나머지 종목은 계속 처리합니다
                symbol = symbol_config["symbol"]
                print(f"\n✗ {symbol} 처리 중 오류 발생: {str(error)}")
                notify(f"⚠️ {symbol} 오류\n\n{str(error)}", urgent=True)

            if len(SYMBOLS) > 1:
                time.sleep(1)

        print("\n프로그램을 정상적으로 종료합니다.")

    except Exception as error:
        print("\n" + "=" * 60)
        print("✗ 프로그램 실행 중 치명적 에러 발생")
        print("=" * 60)
        print(f"에러: {str(error)}")

        message = f"""🚨 치명적 에러 발생

{str(error)}"""
        notify(message, urgent=True)

        import traceback

        print("\n[상세 에러 정보]")
        print(traceback.format_exc())

        print("\n프로그램을 에러와 함께 종료합니다.")
        sys.exit(1)


if __name__ == "__main__":
    main()
