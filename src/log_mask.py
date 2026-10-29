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
#
# ── 주문번호 매핑 원장(ledger) ────────────────────────────────────────
# 공개 로그에는 `ord#...` 가명 토큰만 남습니다. 그런데 암호화 Evidence 만으로는
# 다음 경우 토큰을 원문으로 되돌릴 수 없습니다.
#   1) 정산 근거(settlement-*.json)는 place_order **이전**에 기록되므로
#      같은 실행에서 새로 접수한 주문번호가 근거에 없습니다.
#   2) STATE_DIAGNOSTIC_ONLY 등 진단 모드는 정산 근거를 만들지 않습니다.
#   3) Evidence artifact 는 14일 뒤 만료됩니다.
#   4) state 백업(.state.json)에는 원문 주문번호가 있지만 **salt 가 없습니다**.
# 그래서 "이번 실행에서 실제로 마스킹한 (토큰 → 원문)" 매핑을 최소한으로 남깁니다.
# 원장은 정산 근거와 **같은 게이팅**으로 같은 암호화 Evidence 에 들어갑니다.
# atexit 훅은 두지 않습니다 — 기록 시점을 호출부가 정확히 정하는 편이
# 테스트하기 쉽고, 실패를 조용히 삼키지 않습니다.
import hashlib
import hmac
import json
import os
import re

# 이번 실행의 마스킹 키. 프로세스마다 새로 만들어지며 파일로 저장하지 않습니다(비밀키 아님).
_LOG_SALT = os.urandom(16)

# RUN_ID 가 없을 때(로컬 실행) 프로세스마다 다른 원장 파일명을 쓰기 위한 태그.
# 같은 프로세스의 여러 flush 는 같은 파일을 덮어써 누적하고, 다른 프로세스(다른 salt)의
# 매핑은 지우지 않습니다. 진단 모드는 정산 근거를 남기지 않으므로, 로컬에서 진단을
# 두 번 하면 첫 실행의 `odno-map-local.jsonl` 이 두 번째 실행에 지워져 복원이
# 불가능해집니다 (O_TRUNC + 프로세스별 salt 상이). 파일명에만 쓰고 키로 쓰지 않습니다.
_LOCAL_TAG = f"pid{os.getpid()}-{os.urandom(4).hex()}"

# 이번 실행에서 마스킹한 (토큰 → 원문 주문번호). flush_odno_ledger() 로만 파일이 됩니다.
_LEDGER = {}

# 파일명에 넣을 수 없는 문자를 제거합니다 (RUN_ID 는 외부 입력일 수 있음).
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_-]")


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
    # 원장에는 **이번 실행 salt 로 만든 토큰만** 기록합니다 (다른 salt 로 재계산한
    # 검증기 토큰을 섞으면 공개 로그와 대조되지 않는 행이 생깁니다).
    # 빈 문자열도 `ord#-` 로 매핑해 둡니다 (로그의 `last_processed_ordno=ord#-` 가
    # "빈 값"인지 실제 주문인지 구분할 수 있게).
    if odno is None:
        return "ord#-"
    if not text:
        if salt_hex is None:
            _LEDGER.setdefault("ord#-", "")
        return "ord#-"
    key = bytes.fromhex(salt_hex) if salt_hex else _LOG_SALT
    token = "ord#" + hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:10]
    if salt_hex is None:
        _LEDGER.setdefault(token, text)
    return token


def _ledger_run_id():
    """Evidence 와 같은 실행 식별자(RUN_ID)를 파일명에 쓸 수 있게 정리합니다.

    RUN_ID 가 있으면 그대로 쓰고, 없으면(로컬 실행) 프로세스 고유 태그를 붙여
    이전 로컬 실행의 원장을 덮어쓰지 않습니다 (`odno-map-local-pid…-….jsonl`).
    """
    safe = _UNSAFE_FILENAME.sub("", (os.getenv("RUN_ID") or "").strip())
    if safe:
        return safe
    return f"local-{_LOCAL_TAG}"


def flush_odno_ledger(path=None):
    """이번 실행의 주문번호 매핑 원장을 JSONL(0600)로 기록합니다.

    정산 근거(`settlement-*.json`)와 **같은 게이팅**으로, 같은(암호화) Evidence 묶음에
    들어가도록 `odno-map-<run_id>.jsonl` 을 작업 디렉터리에 남깁니다. 평문이므로
    공개 Artifact 에 직접 올리면 안 됩니다 (workflow 는 gpg 암호화본만 업로드).

    행 형식: `{run_id, retry_num, broker, token, odno_raw, odno_normalized}`.
    `odno_normalized` 는 어댑터가 이미 정규화한 값을 **그대로** 씁니다
    (`YYYYMMDD:주문번호` 같은 합성 번호를 쪼개지 않습니다).

    Parameters:
        path (str, optional): 저장 경로 (기본 `odno-map-<run_id>.jsonl`, 테스트용)

    Returns:
        str | None: 저장한 경로, 기록할 매핑이 없으면 None
    """
    if not _LEDGER:
        return None

    run_id = _ledger_run_id()
    retry_num = (os.getenv("RETRY_NUM") or "").strip() or "0"
    broker = (os.getenv("BROKER") or "").strip()
    target = path or f"odno-map-{run_id}.jsonl"
    rows = len(_LEDGER)
    try:
        # 0600: self-hosted runner 의 다른 사용자가 원문 주문번호를 읽지 못하게 합니다.
        # (umask 는 비트를 지우기만 하므로 0600 은 유지되지만, 기존 파일이 넓게
        #  열려 있을 수 있어 chmod 로 한 번 더 고정합니다.)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for token, odno_raw in _LEDGER.items():
                handle.write(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "retry_num": retry_num,
                            "broker": broker,
                            "token": token,
                            "odno_raw": odno_raw,
                            "odno_normalized": odno_raw,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.chmod(target, 0o600)
    except OSError as error:
        # 실패 경고에는 **원문 주문번호를 쓰지 않습니다** (건수와 사유만).
        print(
            f"[경고] 주문번호 매핑 원장 기록 실패 ({rows}건, "
            f"{error.strerror or 'unknown'}) — 공개 로그에는 원문을 남기지 않습니다"
        )
        return None

    print(f"[정보] 주문번호 매핑 원장 기록 완료: {target} ({rows}건)")
    return target
