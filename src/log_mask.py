# 공개 로그용 주문번호 마스킹 유틸 — 브로커 어댑터·상태 관리 코드가 함께 사용합니다.
#
# 주문번호는 짧은 숫자라 salt 없는 해시(예: SHA-256 앞 8자리)는 전수 대입으로 복원됩니다.
# 그래서 salt 를 **실행(프로세스)마다 무작위**로 만들고, 공개 로그에는 salt 를 출력하지 않습니다.
# salt 는 비공개(암호화) Evidence 에만 기록되므로, 공개 로그만으로는 원문을 복원할 수 없고
# 다른 실행의 로그와 같은 주문을 연결할 수도 없습니다.
#
# 이 모듈은 표준 라이브러리만 사용합니다 (state.py / broker 어댑터 어디서 import 해도
# 순환 의존이 생기지 않도록 하기 위함입니다). salt 는 이 모듈 전역에 **하나만** 존재합니다.
#
# ⚠️ import 경로는 반드시 `from log_mask import ...` **하나로 통일**하세요.
#    (`src` 디렉터리가 sys.path 에 있는 실행 컨텍스트 — trading_bot.py, tests/conftest.py 기준)
#    같은 파일을 `src.log_mask` 로도 import 하면 파이썬이 **별도 모듈 객체**로 두 번 로드해
#    salt 가 두 개 생기고, state 로그와 어댑터 로그의 토큰이 서로 달라집니다.
import hashlib
import hmac
import os

# 이번 실행의 마스킹 키. 프로세스마다 새로 만들어지며 파일로 저장하지 않습니다(비밀키 아님).
_LOG_SALT = os.urandom(16)


def log_salt_hex():
    """이번 실행의 로그 마스킹 salt (Evidence 에만 기록 — 독립 검증기가 대조에 사용)."""
    return _LOG_SALT.hex()


def mask_odno(odno, salt_hex=None):
    """공개 로그용 주문번호 가명 식별자: `ord#` + HMAC-SHA256(salt, odno) 앞 10자리.

    같은 실행 안에서는 같은 번호가 항상 같은 값이라 로그끼리 대조할 수 있습니다.
    실제 주문번호는 공개 GitHub Actions 로그·예외 메시지·알림에 남기지 않습니다.
    목록(list/tuple/set)을 주면 각 원소를 마스킹한 `[a, b]` 문자열을 돌려줍니다.

    Parameters:
        odno (str | int | list | tuple | set | None): 마스킹할 주문번호(들)
        salt_hex (str, optional): 다른 salt 로 대조할 때만 사용 (검증기·테스트 전용)

    Returns:
        str: `ord#xxxxxxxxxx` 형식의 가명 식별자
    """
    if isinstance(odno, (list, tuple, set, frozenset)):
        return "[" + ", ".join(mask_odno(o, salt_hex) for o in odno) + "]"
    text = "" if odno is None else str(odno)
    if not text:
        return "ord#-"
    key = bytes.fromhex(salt_hex) if salt_hex else _LOG_SALT
    return "ord#" + hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:10]
