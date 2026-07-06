# 해외주식 현재가 조회 테스트 스크립트
import sys
from pathlib import Path

# 부모 디렉토리(src)를 Python 경로에 추가하여 모듈을 import 가능하게 함
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from broker.kis.adapter import KISBroker


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
        broker = KISBroker()
        price_data = broker.get_stock_price("TQQQ", "NAS")
        
        # Step 2: 응답 데이터 검증
        print("[Step 2] 응답 데이터 검증 중...")
        assert price_data.last > 0, "현재가(last)가 0입니다"
        assert price_data.open > 0, "시가(open)가 0입니다"
        
        # Step 3: 현재가 정보 출력
        print("\n✅ 현재가 조회 성공!")
        print("\n[TQQQ 시세 정보]")
        print(f"- 현재가: ${price_data.last:.2f}")
        print(f"- 시가: ${price_data.open:.2f}")
        
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
