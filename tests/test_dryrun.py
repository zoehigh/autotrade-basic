#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
무한매수법 V4 DryRun 테스트 스크립트

실제 주문을 실행하지 않고, 전략에 따른 예상 주문 목록을 출력합니다.
환경변수를 통해 설정값을 받아서 전략을 실행합니다.
"""

import sys
import os

# src 디렉터리를 Python 경로에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy import 무한매수법_V4
from config import SYMBOLS


DEFAULT_SYMBOL_CONFIG = SYMBOLS[0]


def main():
    """
    DryRun 메인 함수

    환경변수에서 설정값을 읽어서 전략을 실행하고 결과를 출력합니다.
    """

    # ========================================
    # 환경변수 읽기
    # ========================================

    symbol = os.getenv("TEST_SYMBOL", DEFAULT_SYMBOL_CONFIG["symbol"])
    exchange_code = os.getenv("TEST_EXCHANGE", DEFAULT_SYMBOL_CONFIG["exchange"])
    splits = int(os.getenv("TEST_SPLITS", str(DEFAULT_SYMBOL_CONFIG["splits"])))
    symbol_type = os.getenv("TEST_SYMBOL_TYPE", DEFAULT_SYMBOL_CONFIG["symbol_type"])
    seed = float(os.getenv("TEST_SEED", str(DEFAULT_SYMBOL_CONFIG["seed"])))
    T = float(os.getenv("TEST_T", "0"))

    # ========================================
    # 전략 실행
    # ========================================

    print("\n" + "="*60)
    print("[DRY RUN] 무한매수법 V4")
    print("="*60 + "\n")

    try:
        result = 무한매수법_V4(
            symbol=symbol,
            exchange_code=exchange_code,
            splits=splits,
            symbol_type=symbol_type,
            seed=seed,
            T=T,
        )

        # ========================================
        # 결과 출력
        # ========================================

        print(f"종목/거래소: {result['symbol']}/{result['exchange']}")
        print(f"거래가능여부: {'가능' if result['tradable'] else '불가능'}")
        print()

        print(f"시가: ${result['open_price']:.2f}")
        print(f"현재가: ${result['last_price']:.2f}")
        print()

        print(f"보유수량: {result['position_qty']}주")
        if result['avg_price'] > 0:
            print(f"평단가: ${result['avg_price']:.2f}")
        else:
            print(f"평단가: None (포지션 없음)")
        print()

        print(f"주문가능금액: ${result['orderable_cash']:.2f}")
        print(f"사용 splits: {splits}")
        print(f"사용 symbol_type: {symbol_type}")
        print(f"사용 seed: ${seed:.2f}")
        print(f"사용 T: {T}")
        print()

        print(f"unit_qty: {result['unit_qty']}주")
        if result['star_point']:
            print(f"star_point: ${result['star_point']:.2f}")
        else:
            print("star_point: None")
        print()

        print("계산된 기준가:")
        if result['take_profit_price']:
            print(f"  - 익절가: ${result['take_profit_price']:.2f}")
        else:
            print(f"  - 익절가: None (포지션 없음)")
        if result['star_buy_price']:
            print(f"  - 별지점 매수 기준가: ${result['star_buy_price']:.2f}")
        else:
            print("  - 별지점 매수 기준가: None")
        print()

        print("예상 주문:")
        if result['orders']:
            for order in result['orders']:
                side = order['side']
                qty = order['quantity']
                price = order['price']
                order_type = order['order_type']
                comment = order['comment']
                
                if price is not None:
                    print(f"  - {side} {qty}주 @ ${price:.2f} ({order_type}) # {comment}")
                else:
                    print(f"  - {side} {qty}주 ({order_type}) # {comment}")
        else:
            print("  - 예상 주문 없음")

        print()
        print("="*60)
        print("[DRY RUN 완료]")
        print("="*60 + "\n")

    except Exception as e:
        print(f"\n❌ 에러 발생: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
