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

from config import SYMBOLS, TRADE_MODE
from strategy import 무한매수법_V4
from state import load_state, save_state, update_T_from_history
from trader import (
    place_overseas_order,
    place_overseas_reservation_order,
    get_overseas_order_history,
    ReservationOrderRequired,
)
from telegram import send_telegram


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
            - take_profit (float): 익절률
            - big_buy_range (float): 큰수 상승률
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
    print(f"\n[Step 1] T값 로드 중...")

    state = load_state(symbol)

    order_history = get_overseas_order_history(symbol, exchange, days=30)
    state = update_T_from_history(symbol, state, order_history)

    T = state["T"]
    print(f"  현재 T값: {T}")

    # ── Step 2: 전략 실행 ────────────────────────────────────────
    print(f"\n[Step 2] 전략 실행 중...")

    strategy_result = 무한매수법_V4(
        symbol=symbol,
        exchange_code=exchange,
        splits=splits,
        symbol_type=symbol_type,
        seed=seed,
        T=T,
    )

    print(f"✓ 전략 실행 완료")
    print(f"  현재가: ${strategy_result['last_price']}")
    print(f"  보유 수량: {strategy_result['position_qty']}주")
    print(f"  평단가: ${strategy_result['avg_price']}")
    print(f"  주문 가능 금액: ${strategy_result['orderable_cash']:.2f}")
    print(f"  T값: {T} / {splits}")
    if strategy_result['star_point']:
        print(f"  별지점: ${strategy_result['star_point']:.2f}")

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

    print(f"\n[Step 4] 주문 실행 중...")
    print("-" * 60)

    order_exchange_code = convert_exchange_code(exchange)

    executed_orders = []
    reserved_orders = []
    failed_orders = []

    for i, order in enumerate(orders, 1):
        print(f"\n주문 {i}/{len(orders)} 실행: {order['comment']}")

        try:
            order_price = order["price"] if order["price"] else 0

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
                print("✓ 주문 성공")

                message = f"""✅ 주문 성공

{order['comment']}
수량: {order['quantity']}주
주문번호: {result['odno']}
시각: {result['ord_tmd']}"""
                send_telegram(message)
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

                    message = f"""📋 예약주문 접수

{order['comment']}
수량: {order['quantity']}주
예약주문번호: {resv_result['odno']}
접수일자: {resv_result['rsvn_ord_rcit_dt']}"""
                    send_telegram(message)
                except Exception as reservation_error:
                    print(f"✗ 예약주문 실패: {str(reservation_error)}")
                    failed_orders.append(
                        {
                            "comment": order["comment"],
                            "error": f"예약주문 실패: {str(reservation_error)}",
                        }
                    )
                    send_telegram(
                        f"⚠️ 예약주문 실패\n\n{order['comment']}\n에러: {str(reservation_error)}"
                    )

        except Exception as error:
            print(f"✗ 주문 실패: {str(error)}")
            failed_orders.append(
                {
                    "comment": order["comment"],
                    "error": str(error),
                }
            )

            message = f"""⚠️ 주문 실패

{order['comment']}
에러: {str(error)}"""
            send_telegram(message)
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

        send_telegram("🚀 자동매매 시작")

        # ========================================
        # 설정 정보 출력
        # ========================================
        print(f"\n[설정 정보]")
        print(f"거래 모드: {TRADE_MODE}")
        print(f"종목 목록:")
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
                send_telegram(f"⚠️ {symbol} 오류\n\n{str(error)}")

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
        send_telegram(message)

        import traceback

        print("\n[상세 에러 정보]")
        print(traceback.format_exc())

        print("\n프로그램을 에러와 함께 종료합니다.")
        sys.exit(1)


if __name__ == "__main__":
    main()
