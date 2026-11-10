# 실행 락·journal 경로 — 모두 .state.json 이 있는 디렉터리에서 파생합니다.
# (환경변수로 따로 지정하면 락·journal·state 가 서로 다른 체크아웃을 가리킬 수 있어 금지)
import fcntl
import os

import state as _state

_handles = []  # 프로세스가 끝날 때까지 락을 유지합니다


def _state_dir() -> str:
    return os.path.dirname(os.path.abspath(_state._STATE_FILE))


def run_lock_path() -> str:
    """운영 런처(ops/run_daily.sh)가 쓰는 락 — 런처 시작 구간을 막습니다."""
    return os.path.join(_state_dir(), ".run.lock")


def bot_lock_path() -> str:
    """봇 본체(trading_bot.main)가 실행 중 잡는 락 — 직접 실행·런처 실행 모두 막습니다."""
    return os.path.join(_state_dir(), ".bot.lock")


def watch_lock_path() -> str:
    """감시기(ops/meritz_reprice_watch.py) 중복 실행 방지 락."""
    return os.path.join(_state_dir(), ".watch.lock")


def watch_state_path() -> str:
    """감시기 기록(일일 정정 횟수·알림 중복 방지). 매매 state 와 별개 파일입니다."""
    return os.path.join(_state_dir(), ".watch_state.json")


def watch_halt_path() -> str:
    """감시기 중단 표식 — 있으면 APPLY 모드는 정정하지 않습니다 (사람이 원인 해소 후 삭제)."""
    return os.path.join(_state_dir(), ".watch_halt.json")


def journal_path() -> str:
    return os.path.join(_state_dir(), ".reprice_journal.json")


def acquire_nonblocking(path: str):
    """락을 즉시 시도합니다. 이미 잡혀 있으면 RuntimeError."""
    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f"다른 실행이 락을 쥐고 있습니다: {path}")
    return handle


def acquire_bot_lock():
    """봇 본체 락을 잡아 프로세스 수명 동안 유지합니다."""
    handle = acquire_nonblocking(bot_lock_path())
    _handles.append(handle)
    return handle
