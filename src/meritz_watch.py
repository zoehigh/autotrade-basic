# 메리츠 BUY LOC 가격 정정 감시기 — 순수 로직 (시간 창·설정·기록·중단 표식)
#
# 주기 실행(크론)은 KST 고정 시각으로 자주 돌려도 되고, "지금이 감시할 시간인지"는
# 이 모듈이 미국 동부시간(ET)으로 판단합니다 → 서머타임/동절기와 조기 마감이 자동 반영됩니다.
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MODE_DRY = "DRY"
MODE_APPLY = "APPLY"


class WatchConfigError(ValueError):
    """감시기 설정 오류 (종료 코드 2)."""


# ── 설정 ─────────────────────────────────────────────────────────────

def _parse_hhmm(name, raw):
    try:
        hour, minute = raw.strip().split(":")
        hour, minute = int(hour), int(minute)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
        return hour, minute
    except ValueError:
        raise WatchConfigError(f"{name} 형식 오류: {raw!r} (예: 06:00)")


def _parse_number(name, raw, cast, minimum):
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise WatchConfigError(f"{name} 숫자 형식 오류: {raw!r}")
    if value != value or value in (float("inf"), float("-inf")) or value < minimum:
        raise WatchConfigError(f"{name} 는 {minimum} 이상의 유한한 값이어야 합니다: {raw!r}")
    return value


def load_settings(env):
    """환경변수 → 설정 dict. 잘못된 값은 조용히 넘기지 않고 오류로 중단합니다."""
    mode = (env.get("REPRICE_WATCH_MODE") or MODE_DRY).strip().upper()
    if mode not in (MODE_DRY, MODE_APPLY):
        raise WatchConfigError(f"REPRICE_WATCH_MODE 는 DRY 또는 APPLY 만 허용합니다: {mode!r}")
    start = _parse_hhmm("REPRICE_WATCH_START_ET", env.get("REPRICE_WATCH_START_ET") or "06:00")
    end = _parse_hhmm("REPRICE_WATCH_END_ET", env.get("REPRICE_WATCH_END_ET") or "15:30")
    if start >= end:
        raise WatchConfigError("REPRICE_WATCH_START_ET 는 REPRICE_WATCH_END_ET 보다 이르어야 합니다")
    symbols = [s.strip().upper() for s in (env.get("REPRICE_WATCH_SYMBOLS") or "").split(",") if s.strip()]
    return {
        "mode": mode,
        "start_et": start,
        "end_et": end,
        "close_buffer_min": _parse_number("REPRICE_WATCH_CLOSE_BUFFER_MIN", env.get("REPRICE_WATCH_CLOSE_BUFFER_MIN") or "30", int, 0),
        "min_step": _parse_number("REPRICE_WATCH_MIN_STEP", env.get("REPRICE_WATCH_MIN_STEP") or "0.05", float, 0.01),
        "min_interval_min": _parse_number("REPRICE_WATCH_MIN_INTERVAL_MIN", env.get("REPRICE_WATCH_MIN_INTERVAL_MIN") or "20", int, 1),
        "max_amends_per_day": _parse_number("REPRICE_WATCH_MAX_AMENDS_PER_DAY", env.get("REPRICE_WATCH_MAX_AMENDS_PER_DAY") or "3", int, 1),
        # 한 번의 실행에서 종목당 시도할 최대 정정 수. 기본 1 이면 모든 시도가 최소 간격만큼 떨어집니다.
        "max_per_run": _parse_number("REPRICE_WATCH_MAX_PER_RUN", env.get("REPRICE_WATCH_MAX_PER_RUN") or "1", int, 1),
        "symbols": symbols,
    }


def apply_gate_problems(settings, config, env):
    """APPLY 로 실제 정정하기 위한 조건 중 부족한 것을 나열합니다 (DRY 는 항상 빈 목록)."""
    if settings["mode"] != MODE_APPLY:
        return []
    problems = []
    if config.BROKER != "meritz":
        problems.append("BROKER=meritz 가 아님")
    if config.TRADE_MODE != "LIVE":
        problems.append("TRADE_MODE=LIVE 가 아님")
    if env.get("ALLOW_BROKER_ORDERS") != "1":
        problems.append("ALLOW_BROKER_ORDERS=1 이 아님")
    return problems


# ── 감시 시간 창 (ET 기준) ───────────────────────────────────────────

def watch_window(now_utc, settings, calendar):
    """지금 감시해도 되는 시간인지 → (bool, 사유, ET 세션 날짜 YYYYMMDD).

    - 미국(NYSE) 영업일만, 세션 시작 설정(ET) ~ min(종료 설정, 정규장 마감 - 버퍼)
    - 조기 마감일은 마감 시각이 앞당겨져 창도 같이 줄어듭니다.
    - 달력을 읽지 못하면 열린 것으로 보지 않습니다 (fail-closed).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(ET)
    day = now_et.strftime("%Y-%m-%d")
    compact = now_et.strftime("%Y%m%d")
    try:
        if not calendar.is_session(day):
            return False, "미국 휴장일 또는 주말", compact
        close_et = calendar.session_close(day).tz_convert(ET).to_pydatetime()
    except Exception as error:  # 범위 밖 날짜·달력 오류
        return False, f"달력 조회 실패({str(error)[:60]}) — 감시하지 않음", compact
    start = now_et.replace(hour=settings["start_et"][0], minute=settings["start_et"][1], second=0, microsecond=0)
    configured_end = now_et.replace(hour=settings["end_et"][0], minute=settings["end_et"][1], second=0, microsecond=0)
    end = min(configured_end, close_et - timedelta(minutes=settings["close_buffer_min"]))
    if now_et < start:
        return False, f"감시 시작 전 (ET {now_et:%H:%M} < {start:%H:%M})", compact
    if now_et >= end:
        return False, f"감시 종료 후 (ET {now_et:%H:%M} >= {end:%H:%M})", compact
    return True, f"감시 창 안 (ET {now_et:%H:%M}, 종료 {end:%H:%M})", compact


# ── 기록(일일 정정 횟수·알림 중복 방지) — 매매 state 와 별개 ────────────

def _empty_record(session):
    return {"session": session, "symbols": {}, "notified": {}}


def load_record(path, session):
    """같은 ET 세션 기록만 인정합니다 (날짜가 바뀌면 새 기록). 손상 시 새 기록 + 경고 반환."""
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict):
            raise ValueError("dict 아님")
    except FileNotFoundError:
        return _empty_record(session), None
    except (OSError, ValueError) as error:
        return _empty_record(session), f"감시 기록을 읽을 수 없어 새로 시작합니다: {str(error)[:80]}"
    if record.get("session") != session:
        record = _empty_record(session)
    record.setdefault("symbols", {})
    record.setdefault("notified", {})
    return record, None


def save_record(path, record):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def quarantine_record(path, now_utc):
    """손상된 감시 기록을 지우지 않고 옆으로 치웁니다 (사람이 이전 횟수를 확인할 수 있게). 새 경로 또는 None."""
    target = f"{path}.corrupt-{now_utc:%Y%m%dT%H%M%S}"
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def record_recovered(record, symbol, entry_id, at_iso, linked, now_utc):
    """journal 로 복구한 정정을 일일 횟수·마지막 시도 시각에 반영합니다 → 새로 반영했으면 True.

    entry_id(journal 항목 식별자)별로 **한 번만** 반영합니다 — 복구 도중 재시작해도 중복 가산이 없습니다.
    접수된 정정(linked)만 amends 에 더하며, UNCERTAIN 으로 이미 시도 기록이 있어도 amends 는
    AMENDED 일 때만 늘었으므로 이중 가산이 없습니다.
    """
    seen = record.setdefault("recovered_ids", [])
    if entry_id in seen:
        return False
    seen.append(entry_id)
    info = record["symbols"].setdefault(symbol, {"amends": 0, "attempts": 0})
    if linked:
        info["amends"] = int(info.get("amends", 0)) + 1
    try:
        attempt_at = datetime.fromisoformat(at_iso) if at_iso else now_utc
    except ValueError:
        attempt_at = now_utc
    if attempt_at.tzinfo is None:
        attempt_at = attempt_at.replace(tzinfo=timezone.utc)
    previous = info.get("last_attempt_utc")
    if not previous or attempt_at > datetime.fromisoformat(previous):
        info["last_attempt_utc"] = attempt_at.isoformat()
    return True


def throttle_reason(record, symbol, settings, now_utc):
    """이 종목을 지금 정정해도 되는지 — 막아야 하면 사유, 아니면 None."""
    info = record["symbols"].get(symbol) or {}
    if int(info.get("amends", 0)) >= settings["max_amends_per_day"]:
        return f"일일 정정 한도 도달 ({info.get('amends')}/{settings['max_amends_per_day']})"
    last = info.get("last_attempt_utc")
    if last:
        elapsed = now_utc - datetime.fromisoformat(last)
        if elapsed < timedelta(minutes=settings["min_interval_min"]):
            wait = settings["min_interval_min"] - int(elapsed.total_seconds() // 60)
            return f"최소 간격 {settings['min_interval_min']}분 미경과 (약 {wait}분 남음)"
    return None


def record_attempt(record, symbol, now_utc, amended):
    info = record["symbols"].setdefault(symbol, {"amends": 0, "attempts": 0})
    info["attempts"] = int(info.get("attempts", 0)) + 1
    info["last_attempt_utc"] = now_utc.isoformat()
    if amended:
        info["amends"] = int(info.get("amends", 0)) + 1


def digest(*parts) -> str:
    """알림 중복 방지용 해시 (주문번호 원문은 저장하지 않고 해시만 남깁니다)."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def should_notify(record, key, value, now_utc, repeat_after_min=None):
    """같은 내용을 반복 알리지 않습니다. 새 내용이거나(repeat 설정 시) 오래됐으면 True 이고 기록도 갱신합니다."""
    previous = record["notified"].get(key)
    if previous and previous.get("value") == value:
        if repeat_after_min is None:
            return False
        if now_utc - datetime.fromisoformat(previous["at"]) < timedelta(minutes=repeat_after_min):
            return False
    record["notified"][key] = {"value": value, "at": now_utc.isoformat()}
    return True


# ── 중단 표식 ────────────────────────────────────────────────────────

def read_halt(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"reason": "중단 표식을 읽을 수 없음 (손상) — 안전을 위해 중단 상태로 봅니다"}


def write_halt(path, symbol, result, detail, now_utc):
    save_record(path, {"symbol": symbol, "result": result, "detail": detail[:200], "at": now_utc.isoformat()})
