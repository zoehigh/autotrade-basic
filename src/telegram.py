"""
텔레그램으로 메시지를 전송하는 모듈

이 파일은 텔레그램 봇을 통해 사용자에게 
알림 메시지를 보내는 역할을 합니다.
"""

import os
import requests
from dotenv import load_dotenv

# .env 파일에서 환경변수 읽기
load_dotenv()

# 텔레그램 API용 세션 (connection pooling)
_TELEGRAM_SESSION = requests.Session()


def send_telegram(message: str) -> bool:
    """
    텔레그램으로 메시지를 전송합니다.
    
    Args:
        message: 전송할 메시지 내용
        
    Returns:
        bool: 전송 성공 시 True, 실패 시 False
    """
    # 환경변수에서 봇 토큰과 채팅 ID를 가져옵니다
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    # 환경변수가 설정되어 있지 않으면 에러 메시지 출력
    if not bot_token or not chat_id:
        print("❌ 텔레그램 설정이 없습니다. TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 확인하세요.")
        return False
    
    # 텔레그램 API 주소
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    # 전송할 데이터
    data = {
        "chat_id": chat_id,
        "text": message
    }
    
    try:
        # 메시지 전송
        response = _TELEGRAM_SESSION.post(url, data=data, timeout=10)
        
        # 응답 확인
        if response.status_code == 200:
            print("✅ 텔레그램 메시지 전송 성공")
            return True
        else:
            print(f"❌ 텔레그램 메시지 전송 실패: {response.status_code}")
            print(f"   응답 내용: {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        print("❌ 텔레그램 메시지 전송 시간 초과")
        return False
    except Exception as e:
        print(f"❌ 텔레그램 메시지 전송 중 오류 발생: {e}")
        return False
