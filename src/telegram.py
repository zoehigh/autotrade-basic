"""
텔레그램으로 메시지를 전송하는 모듈

이 파일은 텔레그램 봇을 통해 사용자에게 
알림 메시지를 보내는 역할을 합니다.
"""

import os
import time

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
    
    # 일시적 네트워크 오류(타임아웃/연결 오류/서버 5xx)에 대해 재시도합니다.
    # 재시도로 해결되지 않는 오류(4xx 설정 오류 등)는 즉시 실패 처리합니다.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            # 메시지 전송
            response = _TELEGRAM_SESSION.post(url, data=data, timeout=10)

            # 응답 확인
            if response.status_code == 200:
                print("✅ 텔레그램 메시지 전송 성공")
                return True
            if response.status_code >= 500 and attempt < max_attempts:
                print(f"⚠️ 텔레그램 서버 오류(HTTP {response.status_code}) — {attempt}/{max_attempts}회 재시도")
                time.sleep(1)
                continue
            # 응답 본문은 출력하지 않습니다. (설정에 따라 메시지 원문이 되돌아올 수 있습니다)
            error_kind = "서버 오류(5xx)" if response.status_code >= 500 else "요청 오류(4xx)"
            print(
                f"❌ 텔레그램 메시지 전송 실패: HTTP {response.status_code} {error_kind} "
                f"— {attempt}/{max_attempts}회 시도"
            )
            return False

        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                print(f"⚠️ 텔레그램 전송 시간 초과 — {attempt}/{max_attempts}회 재시도")
                time.sleep(1)
                continue
            print(f"❌ 텔레그램 메시지 전송 시간 초과 — {attempt}/{max_attempts}회 시도 실패")
            return False
        except Exception as e:
            # 예외 문자열(str(e))에는 요청 URL이 들어 있을 수 있고 그 URL에는 봇 토큰이 포함됩니다.
            # 따라서 예외 **타입 이름만** 출력하고 str(e)는 절대 출력하지 않습니다.
            error_type = type(e).__name__
            if attempt < max_attempts:
                print(
                    f"⚠️ 텔레그램 전송 중 오류 발생({error_type}) — "
                    f"{attempt}/{max_attempts}회 재시도"
                )
                time.sleep(1)
                continue
            print(
                f"❌ 텔레그램 메시지 전송 중 오류 발생({error_type}) — "
                f"{attempt}/{max_attempts}회 시도 실패"
            )
            return False

    return False
