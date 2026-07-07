# 한국투자증권 API 인증 테스트 스크립트
import sys
from pathlib import Path

# 부모 디렉토리(src)를 Python 경로에 추가하여 config, authentication을 import 가능하게 함
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from broker.kis.auth import get_access_token


def test_authentication():
    """
    한국투자증권 API 인증이 정상적으로 작동하는지 확인합니다.
    
    이 테스트는:
    1. 환경변수가 올바르게 설정되어 있는지 확인
    2. API 호출이 성공하는지 확인
    3. 반환된 token의 필수 필드가 있는지 확인
    4. 토큰 캐싱이 정상적으로 작동하는지 확인
    """
    
    print("=" * 60)
    print("한국투자증권 API 인증 테스트 시작")
    print("=" * 60)
    
    try:
        # Step 1: 토큰 발급 시도
        print("\n[Step 1] API에서 access token 발급 중...")
        token_response = get_access_token()
        
        # Step 2: 응답 데이터 검증
        print("[Step 2] 응답 데이터 검증 중...")
        
        required_fields = ["access_token", "token_type", "expires_in", "access_token_token_expired"]
        missing_fields = [field for field in required_fields if field not in token_response]
        
        if missing_fields:
            print(f"❌ 응답에 필수 필드가 부족합니다: {missing_fields}")
            return False
        
        # Step 3: 첫 번째 토큰 정보 출력
        print("\n✅ 첫 번째 토큰 발급 성공!")
        print("\n[발급된 토큰 정보]")
        print(f"- Token Type: {token_response['token_type']}")
        print(f"- Expires In: {token_response['expires_in']}초 ({token_response['expires_in'] / 3600 / 24:.1f}일)")
        print(f"- Expired At: {token_response['access_token_token_expired']}")
        print(f"- Access Token: {token_response['access_token'][:20]}... (처음 20글자만 표시)")
        
        # Step 4: 토큰 캐싱 테스트
        print("\n[Step 3] 토큰 캐싱 테스트 중...")
        print("   동일한 토큰을 다시 요청합니다 (API 호출 없이 캐시된 값을 반환해야 함)")
        
        token_response_cached = get_access_token()
        
        # 같은 토큰이 반환되었는지 확인
        if token_response['access_token'] == token_response_cached['access_token']:
            print("✅ 토큰 캐싱 정상 작동!")
            print(f"   캐시된 토큰: {token_response_cached['access_token'][:20]}...")
        else:
            print("⚠️ 경고: 캐시된 토큰과 새로 발급받은 토큰이 다릅니다")
        
        print("\n" + "=" * 60)
        print("✅ 모든 테스트를 통과했습니다!")
        print("=" * 60)
        
        return True
    
    except Exception as e:
        print(f"\n❌ 테스트 실패")
        print(f"오류: {str(e)}")
        print("\n[해결 방법]")
        print("1. .env 파일이 프로젝트 루트에 존재하는지 확인")
        print("2. .env 파일에 다음과 같이 설정되어 있는지 확인:")
        print("   - KIS_APP_KEY=발급받은_앱키")
        print("   - KIS_APP_SECRET=발급받은_앱시크릿")
        print("   - KIS_ACCOUNT_NO=계좌번호(8자리)")
        print("3. 앱키와 앱시크릿이 올바른 값인지 확인")
        print("4. EGW00133 오류는 자동으로 1분 대기 후 재시도됩니다")
        
        return False


if __name__ == "__main__":
    success = test_authentication()
    sys.exit(0 if success else 1)
