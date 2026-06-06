#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
무한매수법 V4 전략 미리보기 CLI

실제 주문이나 상태(state.json)를 전혀 건드리지 않고,
무한매수법_V4() 함수가 생성하는 주문 목록만 출력합니다.

trading_bot.py(전체 파이프라인)와 달리 체결 이력 반영, 잔고 교차검증,
사이클 종료 감지, 텔레그램 알림 등을 생략합니다.
전략 파라미터에 따른 what-if 시나리오 검증용입니다.

환경변수 (모두 선택사항):
    TEST_SYMBOL                종목코드 (기본값: SYMBOLS[0])
    TEST_EXCHANGE              거래소코드
    TEST_SPLITS                분할 수
    TEST_SYMBOL_TYPE           종목 타입 (TQQQ / SOXL)
    TEST_SEED                  시드 금액 (USD)
    TEST_T                     T값 오버라이드 (기본값: 0)
    TEST_ADDITIONAL_LOC_LEVELS 추가매수 LOC 단계 수

예시:
    TEST_T=3 TEST_SEED=9000 TEST_SPLITS=20 uv run python tests/test_dryrun.py
"""

import sys
import os

# src 디렉터리를 Python 경로에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy import 무한매수법_V4
from config import SYMBOLS


# 기본값: 설정 파일의 첫 번째 종목
DEFAULT_SYMBOL_CONFIG = SYMBOLS[0]


def main():
    """
    환경변수로 설정값을 덮어쓰고 전략을 실행한 뒤 예상 주문을 출력합니다.
    """

    # ── 환경변수 읽기 (미설정 시 SYMBOLS[0]의 값을 기본값으로 사용) ──────
    symbol = os.getenv("TEST_SYMBOL", DEFAULT_SYMBOL_CONFIG["symbol"])
    exchange_code = os.getenv("TEST_EXCHANGE", DEFAULT_SYMBOL_CONFIG["exchange"])
    splits = int(os.getenv("TEST_SPLITS", str(DEFAULT_SYMBOL_CONFIG["splits"])))
    symbol_type = os.getenv("TEST_SYMBOL_TYPE", DEFAULT_SYMBOL_CONFIG["symbol_type"])
    seed = float(os.getenv("TEST_SEED", str(DEFAULT_SYMBOL_CONFIG["seed"])))
    T = float(os.getenv("TEST_T", "0"))
    additional_loc_levels = int(
        os.getenv("TEST_ADDITIONAL_LOC_LEVELS",
                   str(DEFAULT_SYMBOL_CONFIG.get("additional_loc_levels", 3)))
    )

    # ── 전략 실행 ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("[DRY RUN] 무한매수법 V4")
    print("=" * 60 + "\n")

    try:
        result = 무한매수법_V4(
            symbol=symbol,
            exchange_code=exchange_code,
            splits=splits,
            symbol_type=symbol_type,
            seed=seed,
            T=T,
            additional_loc_levels=additional_loc_levels,
        )

        # ── 결과 출력 ──────────────────────────────────────────────────
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
            print("평단가: None (포지션 없음)")
        print()

        print(f"주문가능금액: ${result['orderable_cash']:.2f}")
        print(f"사용 splits: {splits}")
        print(f"사용 symbol_type: {symbol_type}")
        print(f"사용 seed: ${seed:.2f}")
        print(f"사용 T: {T}")
        print()

        print(f"1회 매수금: ${result['unit_amount']:.2f}")
        print(f"1회 매수 수량: {result['unit_qty']}주")
        if result['star_point']:
            print(f"별지점: ${result['star_point']:.2f}")
        else:
            print("별지점: None")
        print()

        print("계산된 기준가:")
        if result['take_profit_price']:
            print(f"  - 익절가: ${result['take_profit_price']:.2f}")
        else:
            print("  - 익절가: None (포지션 없음)")
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
        print("=" * 60)
        print("[DRY RUN 완료]")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\n❌ 에러 발생: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
