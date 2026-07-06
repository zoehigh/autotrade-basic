"""
해외주식 현재체결가 API 호출 테스트

이 테스트는 해외주식 현재체결가 API를 호출하여
매수주문 가능 종목 여부(ordy)를 포함한 정보를 확인합니다.
"""

import sys
import os
from pathlib import Path

# src 디렉토리를 Python 경로에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from broker.kis.adapter import KISBroker
from config import SYMBOLS


TEST_SYMBOL = SYMBOLS[0]["symbol"]
TEST_EXCHANGE = SYMBOLS[0]["exchange"]


def test_overseas_stock_quotation():
    """
    해외주식 현재체결가 API 호출 테스트
    
    테스트 내용:
    - 환경변수 SYMBOLS의 첫 번째 종목을 사용하여 API 호출
    - 매수가능여부(ordy) 정보 확인
    - 모든 응답 필드 출력
    """
    
    print("=" * 80)
    print("해외주식 현재체결가 API 호출 테스트")
    print(f"종목 코드: {TEST_SYMBOL} | 거래소: {TEST_EXCHANGE}")
    print("=" * 80)
    
    try:
        # API 호출
        broker = KISBroker()
        result = broker.get_stock_quotation(TEST_SYMBOL, TEST_EXCHANGE)
        
        print("\n✅ API 호출 성공!\n")
        
        # 주문 가능 여부
        print("🔔 주문 가능 여부:")
        print("-" * 80)
        if result.tradable:
            print(f"  ✅ 주문 가능 상태입니다!")
        else:
            print(f"  ❌ 주문 불가 상태입니다!")
        
        # 기본 정보
        print("\n📊 기본 정보:")
        print("-" * 80)
        
        # 가격 정보
        print("\n💰 가격 정보:")
        print("-" * 80)
        print(f"  현재가 (last):                ${result.last:.2f}")
        
        print("\n" + "=" * 80)
        print("✅ 테스트 완료")
        print("=" * 80)
        
        return True
    
    except Exception as e:
        print(f"❌ 테스트 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_overseas_stock_quotation()
    sys.exit(0 if success else 1)
