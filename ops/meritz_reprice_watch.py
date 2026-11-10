#!/usr/bin/env python3
"""메리츠 보정 BUY LOC 가격 정정 감시기 (주기 실행용, 크론 등록은 사람이 합니다).

한 번 실행 = 한 번 점검 후 종료합니다. 크론이 반복 호출합니다.

기본은 **DRY** — 조회·계획·알림만 하고 주문/정정/state 쓰기를 하지 않습니다.
실제 정정(APPLY)은 아래 3가지가 모두 필요합니다 (하나라도 없으면 실행 거부, DRY 로 낮추지 않음).
  REPRICE_WATCH_MODE=APPLY   TRADE_MODE=LIVE   ALLOW_BROKER_ORDERS=1   (+ BROKER=meritz)

종료 코드: 0 정상/건너뜀, 1 오류·중단·보류(알림 발송), 2 설정 오류.
"""
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "ops")]

import meritz_reprice as rp  # noqa: E402
import meritz_watch as mw  # noqa: E402
import run_lock  # noqa: E402
import state as st  # noqa: E402
from state import mask_odno  # noqa: E402



def _log(message):
    print(f"[watch {datetime.now(timezone.utc):%H:%M:%SZ}] {message}", flush=True)


def _safe_notify(notify_fn, message, urgent=False):
    """알림 실패가 감시 자체를 죽이지 않게 합니다 (실패는 로그로 남김)."""
    try:
        notify_fn(message, urgent=urgent)
    except Exception as error:
        _log(f"알림 발송 실패: {str(error)[:100]}")


def _fmt_item(symbol, item):
    return (f"{symbol} {mask_odno(item['odno'])} {item['qty']}주 "
            f"${item['current_price']:.2f} → ${item['target_price']:.2f} "
            f"(원래 ${item['original_price']:.2f}, 현재가 ${item['last_price']:.2f})")


def _halt(symbol, result, detail, now_utc, notify_fn):
    mw.write_halt(run_lock.watch_halt_path(), symbol, result, detail, now_utc)
    _safe_notify(
        notify_fn,
        f"🛑 메리츠 정정 감시 중단 {symbol}\n결과: {result}\n{detail}\n"
        f"추가 정정을 하지 않습니다. 원인 확인 후 .watch_halt.json 을 삭제해야 재개됩니다 "
        f"(docs/meritz-reprice-watch.md).",
        urgent=True,
    )


def _handle_journal(apply, symbol, state, history, now_utc, notify_fn, record):
    """미해결 정정 journal 처리. 계속 진행해도 되면 True."""
    journal_file = run_lock.journal_path()
    entries = [e for e in rp.read_journal(journal_file) if e["symbol"] == symbol]
    if not entries:
        return True
    _log(f"{symbol} 미해결 정정 journal {len(entries)}건 — 이력으로 확인")
    if apply:
        left, messages = rp.resolve_journal(entries, history, state)
        for message in messages:
            _log("  " + message)
        if len(left) < len(entries):
            st.save_state(symbol, state)  # state 저장에 성공한 뒤에만 journal 을 줄입니다
        others = [e for e in rp.read_journal(journal_file) if e["symbol"] != symbol]
        rp.write_journal(journal_file, others + left)
        if not left:
            return True
        _halt(symbol, "JOURNAL_UNRESOLVED", f"이력으로 해소하지 못한 정정 journal {len(left)}건 — 자동 재주문 안 함", now_utc, notify_fn)
        return False
    # DRY: 사본으로만 확인하고 아무것도 쓰지 않습니다
    left, messages = rp.resolve_journal(entries, history, deepcopy(state))
    for message in messages:
        _log("  [DRY, 저장 안 함] " + message)
    if mw.should_notify(record, f"journal:{symbol}", mw.digest(len(entries), len(left)), now_utc, repeat_after_min=360):
        _safe_notify(notify_fn, f"⚠️ [DRY] {symbol} 미해결 정정 journal {len(entries)}건 (복구 가능 {len(entries) - len(left)}건). "
                                f"봇이 이 종목 정산을 보류합니다. APPLY 또는 ops/meritz_buy_loc_reprice.py 로 해소 필요", urgent=True)
    return False


def _watch_symbol(settings, broker, cfg, record, now_utc, session, notify_fn):
    """종목 1개 점검. 반환: "ok" | "error"(보고 후 계속) | "halt"(전체 중단)."""
    symbol = cfg["symbol"]
    apply = settings["mode"] == mw.MODE_APPLY
    exchange_code = broker.exchange_code(cfg["exchange"])
    state = st.load_state(symbol)
    history = broker.get_order_history(symbol, exchange_code, days=30, verbose=False)

    if not _handle_journal(apply, symbol, state, history, now_utc, notify_fn, record):
        return "halt" if apply else "error"

    last = broker.get_stock_price(symbol, exchange_code).last
    plan = rp.build_plan(state, history, last, {}, session)

    holds = [i for i in plan if i["action"] == rp.ACTION_SKIP and "보류" in i["reason"]]
    if holds and mw.should_notify(record, f"hold:{symbol}", mw.digest(*sorted(h["odno"] for h in holds)), now_utc):
        _safe_notify(notify_fn, f"⚠️ {symbol} 거부된 정정 행이 있는 주문 {len(holds)}건 — 원주문 생존 여부를 이력으로 "
                                f"판별할 수 없어 보류합니다. 앱/HTS 에서 확인하세요", urgent=False)

    amendable, small = [], []
    for item in plan:
        if item["action"] != rp.ACTION_AMEND:
            continue
        (amendable if item["target_price"] - item["current_price"] >= settings["min_step"] - 1e-9 else small).append(item)
    for item in small:
        _log(f"{symbol} 최소 변경폭(${settings['min_step']}) 미만이라 건너뜀: {_fmt_item(symbol, item)}")
    if not amendable:
        _log(f"{symbol} 정정 대상 없음 (현재가 ${last:.2f})")
        return "ok"

    for item in amendable:
        _log(("[DRY] 정정 후보 " if not apply else "정정 후보 ") + _fmt_item(symbol, item))

    if not apply:
        if mw.should_notify(record, f"plan:{symbol}", mw.digest(*[(i["odno"], i["target_price"]) for i in amendable]), now_utc):
            lines = "\n".join(_fmt_item(symbol, i) for i in amendable)
            _safe_notify(notify_fn, f"📈 [DRY] {symbol} 가격 정정 후보 {len(amendable)}건 (실행하지 않음)\n{lines}")
        return "ok"

    blocked = mw.throttle_reason(record, symbol, settings, now_utc)
    if blocked:
        _log(f"{symbol} 정정 보류: {blocked}")
        if "한도" in blocked and mw.should_notify(record, f"cap:{symbol}", blocked, now_utc):
            _safe_notify(notify_fn, f"⚠️ {symbol} {blocked} — 오늘은 더 정정하지 않습니다")
        return "ok"

    journal_file = run_lock.journal_path()
    for item in amendable:
        if int((record["symbols"].get(symbol) or {}).get("amends", 0)) >= settings["max_amends_per_day"]:
            _log(f"{symbol} 일일 정정 한도 도달 — 남은 후보는 건너뜁니다")
            break
        # 정정 직전 재조회·재계획 (그 사이 체결·시세 변동 반영)
        fresh_history = broker.get_order_history(symbol, exchange_code, days=30, verbose=False)
        fresh_last = broker.get_stock_price(symbol, exchange_code).last
        fresh_plan = rp.build_plan(state, fresh_history, fresh_last, {}, session)
        fresh = next((p for p in fresh_plan if p["odno"] == item["odno"]), None)
        if (not fresh or fresh["action"] != rp.ACTION_AMEND
                or fresh["target_price"] - fresh["current_price"] < settings["min_step"] - 1e-9):
            _log(f"  재확인에서 정정 불필요/불가: {mask_odno(item['odno'])}")
            continue
        history_fn = lambda: broker.get_order_history(symbol, exchange_code, days=30, verbose=False)
        result = rp.apply_amend(broker, symbol, exchange_code, fresh, state, journal_file, st.save_state, history_fn)
        mw.record_attempt(record, symbol, now_utc, amended=(result == "AMENDED"))
        mw.save_record(run_lock.watch_state_path(), record)  # 시도 직후 기록 (중단돼도 횟수 유지)
        if result == "AMENDED":
            _safe_notify(notify_fn, f"✅ {symbol} 가격 정정 완료\n{_fmt_item(symbol, fresh)}")
        elif result == "REJECTED":
            _safe_notify(notify_fn, f"⚠️ {symbol} 정정 미접수 (원주문 유지)\n{_fmt_item(symbol, fresh)}")
        else:  # REJECTED_LATE / UNCERTAIN / LINK_FAILED
            _halt(symbol, result, _fmt_item(symbol, fresh), now_utc, notify_fn)
            return "halt"
    return "ok"


def run_once(env, now_utc, config, broker_factory, notify_fn, calendar):
    """한 번 점검합니다 → 종료 코드. 모든 외부 의존(브로커·알림·달력)은 주입받습니다."""
    try:
        settings = mw.load_settings(env)
    except mw.WatchConfigError as error:
        _log(f"설정 오류: {error}")
        return 2
    if config.BROKER != "meritz":
        _log(f"BROKER=meritz 가 아니라 실행하지 않습니다 (현재 {config.BROKER})")
        return 2
    problems = mw.apply_gate_problems(settings, config, env)
    if problems:
        _log("APPLY 실행 거부 (DRY 로 낮추지 않습니다): " + ", ".join(problems))
        return 2

    inside, why, session = mw.watch_window(now_utc, settings, calendar)
    _log(f"모드={settings['mode']} {why}")
    if not inside:
        return 0

    handles = []
    try:
        for path in (run_lock.watch_lock_path(), run_lock.run_lock_path(), run_lock.bot_lock_path()):
            try:
                handles.append(run_lock.acquire_nonblocking(path))
            except RuntimeError:
                _log(f"다른 실행(감시기·봇·런처·정정 도구)이 진행 중이라 이번에는 건너뜁니다: {os.path.basename(path)}")
                return 0
        return _run_locked(settings, now_utc, session, config, broker_factory, notify_fn)
    finally:
        for handle in handles:
            handle.close()


def _run_locked(settings, now_utc, session, config, broker_factory, notify_fn):
    record, warning = mw.load_record(run_lock.watch_state_path(), session)
    if warning:
        _log(warning)
    apply = settings["mode"] == mw.MODE_APPLY
    halt = mw.read_halt(run_lock.watch_halt_path())
    if apply and halt:
        _log(f"중단 표식이 있어 정정하지 않습니다: {halt.get('symbol')} {halt.get('result')} {halt.get('reason', '')}")
        if mw.should_notify(record, "halt_reminder", mw.digest(halt.get("at"), halt.get("result")), now_utc, repeat_after_min=360):
            _safe_notify(notify_fn, f"🛑 메리츠 정정 감시가 중단 상태입니다 ({halt.get('symbol')} {halt.get('result')}). "
                                    f"원인 확인 후 .watch_halt.json 삭제 필요", urgent=True)
        mw.save_record(run_lock.watch_state_path(), record)
        return 1

    symbols = [c for c in config.SYMBOLS if not settings["symbols"] or c["symbol"] in settings["symbols"]]
    if not symbols:
        _log("대상 종목이 없습니다")
        return 2

    broker = broker_factory()
    exit_code = 0
    try:
        for cfg in symbols:
            outcome = _watch_symbol(settings, broker, cfg, record, now_utc, session, notify_fn)
            if outcome == "halt":
                exit_code = 1
                break
            if outcome == "error":
                exit_code = 1
    except Exception as error:  # 조회 실패 등 — 정정은 하지 않은 상태에서의 오류
        text = f"{type(error).__name__}: {str(error)[:120]}"
        _log(f"오류: {text}")
        if mw.should_notify(record, "error", mw.digest(text), now_utc, repeat_after_min=60):
            _safe_notify(notify_fn, f"⚠️ 메리츠 정정 감시 오류 ({settings['mode']}): {text}", urgent=True)
        exit_code = 1
    finally:
        try:
            mw.save_record(run_lock.watch_state_path(), record)
        except OSError as error:
            _log(f"감시 기록 저장 실패: {error}")
        close = getattr(broker, "close", None)
        if close:
            close()
    return exit_code


def main():
    import config
    from broker.market_utils import _get_xnys_calendar

    def broker_factory():
        from broker.meritz.adapter import MeritzBroker
        return MeritzBroker()

    def notify_fn(message, urgent=False):
        from notifier import notify
        notify(message, urgent=urgent)

    return run_once(os.environ, datetime.now(timezone.utc), config, broker_factory, notify_fn, _get_xnys_calendar())


if __name__ == "__main__":
    sys.exit(main())
