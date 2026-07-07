"""
해외주식 잔고 조회 API 테스트

이 테스트는 해외주식 잔고 API를 호출하여
특정 종목의 보유 수량과 평단가를 확인합니다.
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


def test_overseas_balance():
    """
    해외주식 잔고 조회 API 호출 테스트
    
    테스트 내용:
    - 환경변수 SYMBOLS의 첫 번째 종목을 사용하여 API 호출
    - 보유 수량과 평단가 확인
    - 모든 응답 필드 출력
    """
    
    print("=" * 80)
    print("해외주식 잔고 조회 API 테스트")
    print(f"종목 코드: {TEST_SYMBOL} | 거래소: {TEST_EXCHANGE}")
    print("=" * 80)
    
    try:
        # API 호출
        broker = KISBroker()
        result = broker.get_balance(TEST_SYMBOL, TEST_EXCHANGE)
        
        # 결과 검증
        if result is None:
            print("\n⚠️ 해당 종목의 보유 잔고가 없습니다.")
            print("종목 코드 또는 거래소 코드를 확인해주세요.")
            return True
        
        print("\n✅ API 호출 성공!\n")
        
        # 핵심 정보 출력
        print("📊 보유 정보:")
        print("-" * 80)
        print(f"  보유 수량 (quantity):        {result.quantity} 주")
        print(f"  평단가 (avg_price):          ${result.avg_price:.2f}")
        
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
    success = test_overseas_balance()
    sys.exit(0 if success else 1)
