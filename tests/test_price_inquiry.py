# 해외주식 현재가 조회 테스트 스크립트
import sys
from pathlib import Path

# 부모 디렉토리(src)를 Python 경로에 추가하여 모듈을 import 가능하게 함
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from trader import get_overseas_stock_price
from kis_session import KISSession


def test_tqqq_price_inquiry():
    """
    TQQQ(나스닥 100 3배 레버리지 ETF)의 현재가를 조회하는 테스트입니다.
    
    이 테스트는:
    1. 인증 토큰 획득 (캐싱 포함)
    2. 해외주식 현재가 API 호출
    3. 응답 데이터 검증
    4. 주요 정보 출력
    """
    
    print("=" * 70)
    print("TQQQ(나스닥 100 3배 레버리지 ETF) 현재가 조회 테스트")
    print("=" * 70)
    
    try:
        # Step 1: 현재가 조회
        print("\n[Step 1] API에서 TQQQ 현재가 조회 중...")
        session = KISSession()
        price_data = get_overseas_stock_price(session, symbol="TQQQ", exchange_code="NAS")
        
        # Step 2: 응답 데이터 검증
        print("[Step 2] 응답 데이터 검증 중...")
        
        required_fields = ["rsym", "last", "open", "high", "low", "base", "tvol"]
        missing_fields = [field for field in required_fields if field not in price_data]
        
        if missing_fields:
            print(f"❌ 응답에 필수 필드가 부족합니다: {missing_fields}")
            return False
        
        # Step 3: 현재가 정보 출력
        print("\n✅ 현재가 조회 성공!")
        print("\n[TQQQ 시세 정보]")
        print(f"- 종목코드: {price_data.get('rsym', 'N/A')}")
        print(f"- 현재가: ${price_data.get('last', 'N/A')}")
        print(f"- 시가: ${price_data.get('open', 'N/A')}")
        print(f"- 고가: ${price_data.get('high', 'N/A')}")
        print(f"- 저가: ${price_data.get('low', 'N/A')}")
        print(f"- 전일 종가: ${price_data.get('base', 'N/A')}")
        print(f"- 거래량: {price_data.get('tvol', 'N/A')} 주")
        print(f"- 거래대금: ${price_data.get('tamt', 'N/A')}")
        
        # Step 4: 추가 정보 출력 (있을 경우)
        print("\n[추가 정보]")
        if price_data.get('perx'):
            print(f"- PER: {price_data.get('perx')}")
        if price_data.get('epsx'):
            print(f"- EPS: ${price_data.get('epsx')}")
        if price_data.get('t_xprc'):
            print(f"- 원환산 당일 가격: ₩{price_data.get('t_xprc')}")
        
        print("\n" + "=" * 70)
        print("✅ 모든 테스트를 통과했습니다!")
        print("=" * 70)
        
        return True
    
    except Exception as e:
        print(f"\n❌ 테스트 실패")
        print(f"오류: {str(e)}")
        print("\n[해결 방법]")
        print("1. 인증 정보가 올바른지 확인")
        print("   - .env 파일에서 KIS_APP_KEY와 KIS_APP_SECRET 확인")
        print("2. API 호출 시간이 올바른지 확인")
        print("   - 해외주식 시장이 개장 중인지 확인")
        print("3. 네트워크 연결 상태 확인")
        print("4. 한국투자증권 API 상태 확인")
        print("5. EGW00133 오류는 자동으로 1분 대기 후 재시도됩니다")
        
        return False


if __name__ == "__main__":
    success = test_tqqq_price_inquiry()
    sys.exit(0 if success else 1)
