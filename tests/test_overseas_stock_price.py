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

from trader import get_overseas_stock_price
from config import SYMBOLS
from kis_session import KISSession


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
        session = KISSession()
        result = get_overseas_stock_price(session, symbol=TEST_SYMBOL, exchange_code=TEST_EXCHANGE)
        
        # 결과 검증
        if not result:
            print("❌ 테스트 실패: API 응답이 비어있습니다.")
            return False
        
        # 필수 필드 확인
        required_fields = ["open", "last"]
        missing_fields = [field for field in required_fields if field not in result]
        
        if missing_fields:
            print(f"❌ 테스트 실패: 필수 필드가 누락되었습니다: {missing_fields}")
            print(f"응답에 포함된 필드: {list(result.keys())}")
            return False
        
        # 결과 출력
        print("\n✅ 테스트 성공!")
        print("\n📊 조회 결과:")
        print(f"  - 종목 코드: {result.get('rsym', 'N/A')}")
        print(f"  - 시가 (open): {result.get('open', 'N/A')}")
        print(f"  - 현재가 (last): {result.get('last', 'N/A')}")
        print(f"  - 고가 (high): {result.get('high', 'N/A')}")
        print(f"  - 저가 (low): {result.get('low', 'N/A')}")
        print(f"  - 전일 종가 (base): {result.get('base', 'N/A')}")
        print(f"  - 거래량 (tvol): {result.get('tvol', 'N/A')}")
        print(f"  - 원환산 당일 가격 (t_xprc): {result.get('t_xprc', 'N/A')}")
        
        print("\n" + "=" * 60)
        print("전체 응답 데이터:")
        print("=" * 60)
        for key, value in result.items():
            print(f"{key}: {value}")
        
        return True
    
    except Exception as e:
        print(f"❌ 테스트 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_get_overseas_stock_price()
    sys.exit(0 if success else 1)
