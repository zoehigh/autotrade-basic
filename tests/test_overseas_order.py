"""
해외주식 주문 테스트 스크립트

이 스크립트는 해외주식 주문 함수를 테스트합니다.
- TQQQ 종목을 LIMIT 주문과 LOC 주문으로 테스트
- 현재가 API를 호출하여 실제 가격으로 주문
- TRADE_MODE에 따라 DRY 또는 LIVE 모드로 실행
"""

import sys
sys.path.append("src")

from broker import create_broker
from config import SYMBOLS, TRADE_MODE


TEST_SYMBOL = SYMBOLS[0]["symbol"]
TEST_EXCHANGE = SYMBOLS[0]["exchange"]


def test_overseas_order():
    """
    해외주식 주문 테스트를 실행합니다.
    
    1. TQQQ의 현재가를 조회합니다
    2. LIMIT 주문 (지정가) 테스트
    3. LOC 주문 (장마감지정가) 테스트
    """
    
    print("\n" + "="*60)
    print("해외주식 주문 테스트 시작")
    print("="*60)
    
    # 환경변수 확인
    print(f"\n[설정 정보]")
    print(f"종목 코드: {TEST_SYMBOL}")
    print(f"거래소: {TEST_EXCHANGE}")
    print(f"거래 모드: {TRADE_MODE}")
    
    try:
        # Step 1: 현재가 조회
        print(f"\n[Step 1] {TEST_SYMBOL} 현재가 조회 중...")
        
        broker = create_broker()
        order_exchange_code = broker.exchange_code(TEST_EXCHANGE)
        
        price_data = broker.get_stock_price(TEST_SYMBOL, TEST_EXCHANGE)
        current_price = price_data.last
        
        if current_price == 0:
            print("현재가 조회에 실패했습니다.")
            return
        
        print(f"✓ 현재가: ${current_price:.2f}")
        print(f"  시가: ${price_data.open:.2f}")
        
        # Step 2: LIMIT 주문 테스트
        print(f"\n[Step 2] LIMIT 주문 (지정가) 테스트")
        print("-" * 60)
        
        try:
            result_limit = broker.place_order(
                TEST_SYMBOL,
                order_exchange_code,
                "BUY",
                1,
                current_price,
                "LIMIT",
            )
            
            if result_limit:
                print(f"✓ LIMIT 주문 성공")
                print(f"  주문번호: {result_limit.order_id}")
            else:
                print(f"✓ LIMIT 주문 정보 출력 완료 (DRY 모드)")
                
        except Exception as e:
            print(f"✗ LIMIT 주문 실패: {str(e)}")
        
        # Step 3: LOC 주문 테스트
        print(f"\n[Step 3] LOC 주문 (장마감지정가) 테스트")
        print("-" * 60)
        
        try:
            result_loc = broker.place_order(
                TEST_SYMBOL,
                order_exchange_code,
                "BUY",
                1,
                current_price,
                "LOC",
            )
            
            if result_loc:
                print(f"✓ LOC 주문 성공")
                print(f"  주문번호: {result_loc.order_id}")
            else:
                print(f"✓ LOC 주문 정보 출력 완료 (DRY 모드)")
                
        except Exception as e:
            print(f"✗ LOC 주문 실패: {str(e)}")
        
        # 결과 요약
        print(f"\n" + "="*60)
        print("테스트 완료")
        print("="*60)
        
        if TRADE_MODE == "DRY":
            print("\n💡 DRY 모드로 실행되었습니다.")
            print("   실제 주문은 실행되지 않았으며, 주문 정보만 출력되었습니다.")
            print("   실제 주문을 하려면 .env 파일에서 TRADE_MODE=LIVE로 설정하세요.")
        else:
            print("\n⚠️  LIVE 모드로 실행되었습니다.")
            print("   실제 주문이 실행되었습니다. 주문 내역을 확인하세요.")
        
    except Exception as e:
        print(f"\n✗ 테스트 중 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_overseas_order()
