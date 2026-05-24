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

from trader import get_overseas_stock_quotation
from config import SYMBOLS


TEST_SYMBOL = SYMBOLS[0]["symbol"]
TEST_EXCHANGE = SYMBOLS[0]["exchange"]


def is_order_available(ordy_value):
    """
    매수가능여부 값을 분석하여 주문 가능 여부를 판단합니다.
    
    Parameters:
        ordy_value (str): API에서 반환한 ordy 필드값
    
    Returns:
        bool: 주문 가능하면 True, 불가능하면 False
    """
    if not ordy_value:
        return False
    
    # 문자열에서 '가능' 포함 여부 확인
    ordy_str = str(ordy_value).strip()
    
    # '가능'이 포함되어 있고 '불가'는 포함되지 않은 경우
    if '가능' in ordy_str and '불가' not in ordy_str:
        return True
    
    return False


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
        result = get_overseas_stock_quotation(symbol=TEST_SYMBOL, exchange_code=TEST_EXCHANGE)
        
        # 결과 검증
        if not result:
            print("❌ 테스트 실패: API 응답이 비어있습니다.")
            return False
        
        # 필수 필드 확인
        required_fields = ["ordy", "last"]
        missing_fields = [field for field in required_fields if field not in result]
        
        if missing_fields:
            print(f"❌ 테스트 실패: 필수 필드가 누락되었습니다: {missing_fields}")
            return False
        
        print("\n✅ API 호출 성공!\n")
        
        # 주문 가능 여부 (가장 중요한 정보)
        ordy = result.get("ordy", "")
        is_available = is_order_available(ordy)
        
        print("🔔 주문 가능 여부:")
        print("-" * 80)
        print(f"  매수가능여부 (ordy): {ordy}")
        
        if is_available:
            print(f"  ✅ 주문 가능 상태입니다!")
        else:
            print(f"  ❌ 주문 불가 상태입니다!")
        
        # 기본 정보
        print("\n📊 기본 정보:")
        print("-" * 80)
        print(f"  실시간조회종목코드 (rsym):     {result.get('rsym', 'N/A')}")
        print(f"  소수점자리수 (zdiv):          {result.get('zdiv', 'N/A')}")
        
        # 가격 정보
        print("\n💰 가격 정보:")
        print("-" * 80)
        print(f"  현재가 (last):                {result.get('last', 'N/A')}")
        print(f"  전일 종가 (base):             {result.get('base', 'N/A')}")
        print(f"  대비 (diff):                  {result.get('diff', 'N/A')}")
        print(f"  등락율 (rate):                {result.get('rate', 'N/A')}")
        
        # 거래량 정보
        print("\n📈 거래량 정보:")
        print("-" * 80)
        print(f"  당일 거래량 (tvol):           {result.get('tvol', 'N/A')}")
        print(f"  당일 거래대금 (tamt):         {result.get('tamt', 'N/A')}")
        print(f"  전일 거래량 (pvol):           {result.get('pvol', 'N/A')}")
        
        # 전체 응답 데이터
        print("\n" + "=" * 80)
        print("📋 전체 응답 데이터:")
        print("=" * 80)
        for key, value in sorted(result.items()):
            print(f"  {key:20s}: {value}")
        
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
