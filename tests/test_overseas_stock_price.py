"""
해외주식 현재가 조회 기능 테스트

이 테스트는 해외주식 현재가상세 API를 호출하여
시가(open)와 현재가(last) 정보가 정상적으로 반환되는지 확인합니다.
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


def test_get_overseas_stock_price():
    """
    해외주식 현재가상세 API 호출 테스트
    
    테스트 내용:
    - 환경변수 SYMBOLS의 첫 번째 종목을 사용하여 API 호출
    - 응답에 시가(open)와 현재가(last) 정보가 포함되어 있는지 확인
    - 응답에서 필요한 필드를 추출하여 출력
    """
    
    print("=" * 60)
    print(f"해외주식 현재가 조회 테스트 시작")
    print(f"종목 코드: {TEST_SYMBOL} | 거래소: {TEST_EXCHANGE}")
    print("=" * 60)
    
    try:
        # API 호출
        broker = KISBroker()
        result = broker.get_stock_price(TEST_SYMBOL, TEST_EXCHANGE)
        
        # 결과 검증
        assert result.last > 0, "현재가(last)가 0입니다"
        assert result.open > 0, "시가(open)가 0입니다"
        
        # 결과 출력
        print("\n✅ 테스트 성공!")
        print("\n📊 조회 결과:")
        print(f"  - 시가 (open): ${result.open:.2f}")
        print(f"  - 현재가 (last): ${result.last:.2f}")
        
        print("\n" + "=" * 60)
        print("전체 응답 데이터:")
        print("=" * 60)
        print(f"open: ${result.open:.2f}")
        print(f"last: ${result.last:.2f}")
        
        return True
    
    except Exception as e:
        print(f"❌ 테스트 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_get_overseas_stock_price()
    sys.exit(0 if success else 1)
