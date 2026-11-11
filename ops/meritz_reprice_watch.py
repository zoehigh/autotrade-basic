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
        sent = notify_fn(message, urgent=urgent)
    except Exception as error:
        _log(f"알림 발송 실패: {str(error)[:100]}")
        return False
    if sent is False:  # 실제 notifier 는 실패·건너뜀(조용한 시간)에 False 를 돌려줍니다
        _log("알림이 발송되지 않았습니다 (실패 또는 조용한 시간) — 다음 실행에서 다시 시도")
        return False
    return True


def _notify_once(record, key, value, now_utc, notify_fn, message, urgent=False, repeat_after_min=None):
    """같은 내용은 반복하지 않되, **발송에 성공했을 때만** 기록합니다 (실패하면 다음 실행에서 재시도)."""
    if not mw.notify_due(record, key, value, now_utc, repeat_after_min):
        return False
    if not _safe_notify(notify_fn, message, urgent=urgent):
        return False
    mw.mark_notified(record, key, value, now_utc)
    return True


def _fmt_item(symbol, item):
    return (f"{symbol} {mask_odno(item['odno'])} {item['qty']}주 "
            f"${item['current_price']:.2f} → ${item['target_price']:.2f} "
            f"(원래 ${item['original_price']:.2f}, 현재가 ${item['last_price']:.2f})")


def _entry_id(entry):
    """journal 항목 식별자 — 복구 횟수를 중복 없이 기록하기 위한 키."""
    return f"{entry.get('old_odno')}|{entry.get('at') or entry.get('target_price')}"


def _mark_journal_review(old_odno, reason):
    """journal 항목에 '사람 확인 필요' 표식을 남깁니다 (halt 파일을 못 써도 자동 복구가 막히도록)."""
    journal_file = run_lock.journal_path()
    entries = rp.read_journal(journal_file)
    changed = False
    for entry in entries:
        if entry.get("old_odno") == old_odno and not entry.get("review_required"):
            entry["review_required"] = reason
            changed = True
    if changed:
        rp.write_journal(journal_file, entries)
    return changed


def _halt(symbol, result, detail, now_utc, notify_fn, review_odno=None):
    """중단 표식을 만들고 긴급 알림을 보냅니다. 표식 저장이 실패해도 알림과 중단(반환)은 유지합니다.

    review_odno 가 있으면 해당 journal 항목에도 '사람 확인 필요'를 남깁니다. halt 파일 저장이
    실패해도 이 표식이 남아 있으면 다음 APPLY 가 자동 복구하지 않고 중단을 다시 만듭니다.
    """
    problems = []
    try:
        mw.write_halt(run_lock.watch_halt_path(), symbol, result, detail, now_utc)
    except OSError as error:
        problems.append(f"중단 표식(.watch_halt.json) 저장 실패({str(error)[:50]})")
        _log(f"중단 표식 저장 실패: {error}")
    if review_odno:
        try:
            _mark_journal_review(review_odno, result)
        except (OSError, ValueError) as error:
            problems.append(f"journal 확인 표식 저장 실패({str(error)[:50]})")
            _log(f"journal 확인 표식 저장 실패: {error}")
    warning = ""
    if problems:
        warning = "\n⚠️ " + " / ".join(problems)
        if len(problems) == (2 if review_odno else 1):  # 지속 근거가 하나도 남지 않은 경우
            warning += " — 다음 실행이 막히지 않을 수 있으니 직접 .watch_halt.json 을 만들어 주세요"
    return _safe_notify(
        notify_fn,
        f"🛑 메리츠 정정 감시 중단 {symbol}\n결과: {result}\n{detail}\n"
        f"추가 정정을 하지 않습니다. 원인 확인 후 .watch_halt.json 을 삭제해야 재개됩니다 "
        f"(docs/meritz-reprice-watch.md).{warning}",
        urgent=True,
    )


def _register_halt_notified(record, now_utc, delivered=True):
    """방금 **성공적으로** 보낸 중단 알림을 반복 방지 기록에 등록합니다 (다음 실행의 재알림은 6시간 뒤부터).
    발송에 실패했다면 등록하지 않아 다음 실행이 중단 알림을 다시 시도합니다."""
    halt = mw.read_halt(run_lock.watch_halt_path())
    if halt and delivered:
        mw.should_notify(record, "halt_reminder", mw.digest(halt.get("at"), halt.get("result")), now_utc, repeat_after_min=360)


def _handle_journal(apply, symbol, state, history, now_utc, notify_fn, record):
    """미해결 정정 journal 처리. 계속 진행해도 되면 True."""
    journal_file = run_lock.journal_path()
    entries = [e for e in rp.read_journal(journal_file) if e["symbol"] == symbol]
    if not entries:
        return True
    _log(f"{symbol} 미해결 정정 journal {len(entries)}건 — 이력으로 확인")
    if apply:
        review = [e for e in entries if e.get("review_required")]
        if review:
            # 이전 실행이 위험 결과로 끝나면서 남긴 표식 — 사람이 확인하기 전에는 자동 복구하지 않습니다.
            _log(f"{symbol} 사람 확인이 필요한 journal {len(review)}건 ({review[0].get('review_required')}) — 자동 복구하지 않음")
            if not mw.read_halt(run_lock.watch_halt_path()):
                delivered = _halt(symbol, "JOURNAL_REVIEW_REQUIRED", f"이전 실행의 위험 결과({review[0].get('review_required')})가 확인되지 않았습니다", now_utc, notify_fn)
                _register_halt_notified(record, now_utc, delivered)
            return False
        # (1) 사본으로 해소 결과를 먼저 계산합니다 (state·journal 은 아직 건드리지 않음)
        metas_before = set((state.get("orders_meta") or {}).keys())
        preview = deepcopy(state)
        left, messages = rp.resolve_journal(entries, history, preview)
        for message in messages:
            _log("  " + message)
        resolved = [e for e in entries if e not in left]
        # (2) 횟수를 **먼저** 기록합니다. 항목 id 로 한 번만 반영하므로 이후 단계에서 종료돼도 재시작이 안전하고,
        #     기록이 끝나기 전에는 state·journal 근거를 건드리지 않습니다.
        counted = False
        preview_metas = preview.get("orders_meta") or {}
        for entry in resolved:
            old = entry["old_odno"]
            # 연결됨 = 구 주문번호 메타가 새 번호로 옮겨졌다. 정정 직후 종료돼 이미 옮겨진 상태로 재시작한 경우에는
            # 구 키가 처음부터 없으므로 옮겨진 메타의 amended_from 으로 판정한다. (거부만 확인된 항목은 구 키가 그대로 남음)
            linked = (old in metas_before and old not in preview_metas) or any(
                (m or {}).get("amended_from") == old for m in preview_metas.values())
            counted |= mw.record_recovered(record, symbol, _entry_id(entry), entry.get("at"), linked, now_utc)
        if counted:
            try:
                mw.save_record(run_lock.watch_state_path(), record)
            except OSError as error:
                _halt(symbol, "RECORD_SAVE_FAILED", f"복구된 정정의 횟수 기록 저장 실패: {str(error)[:80]}", now_utc, notify_fn)
                return False
        # (3) 실제 state 반영·저장 → (4) 성공한 뒤에만 journal 을 줄입니다
        if resolved:
            rp.resolve_journal(entries, history, state)
            st.save_state(symbol, state)
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
    _notify_once(record, f"journal:{symbol}", mw.digest(len(entries), len(left)), now_utc, notify_fn,
                 f"⚠️ [DRY] {symbol} 미해결 정정 journal {len(entries)}건 (복구 가능 {len(entries) - len(left)}건). "
                 f"봇이 이 종목 정산을 보류합니다. APPLY 또는 ops/meritz_buy_loc_reprice.py 로 해소 필요",
                 urgent=True, repeat_after_min=360)
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
    if holds:
        _notify_once(record, f"hold:{symbol}", mw.digest(*sorted(h["odno"] for h in holds)), now_utc, notify_fn,
                     f"⚠️ {symbol} 거부된 정정 행이 있는 주문 {len(holds)}건 — 원주문 생존 여부를 이력으로 "
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
        lines = "\n".join(_fmt_item(symbol, i) for i in amendable)
        _notify_once(record, f"plan:{symbol}", mw.digest(*[(i["odno"], i["target_price"]) for i in amendable]),
                     now_utc, notify_fn, f"📈 [DRY] {symbol} 가격 정정 후보 {len(amendable)}건 (실행하지 않음)\n{lines}")
        return "ok"

    blocked = mw.throttle_reason(record, symbol, settings, now_utc)
    if blocked:
        _log(f"{symbol} 정정 보류: {blocked}")
        if "한도" in blocked:
            _notify_once(record, f"cap:{symbol}", blocked, now_utc, notify_fn,
                         f"⚠️ {symbol} {blocked} — 오늘은 더 정정하지 않습니다")
        return "ok"

    journal_file = run_lock.journal_path()
    # 실행당 종목별 시도 수를 제한해(기본 1) 모든 시도가 최소 간격만큼 떨어지게 합니다.
    # 후보가 여럿이면 가격 차이가 큰 것부터 처리하고, 나머지는 다음 실행으로 넘깁니다.
    amendable.sort(key=lambda i: i["target_price"] - i["current_price"], reverse=True)
    if len(amendable) > settings["max_per_run"]:
        _log(f"{symbol} 후보 {len(amendable)}건 중 이번 실행에서는 {settings['max_per_run']}건만 시도합니다 (나머지는 다음 실행)")
    attempted = 0
    for item in amendable:
        if attempted >= settings["max_per_run"]:
            break
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
        # journal 항목은 후속 기록(횟수·중단 표식)이 모두 끝난 뒤 이 함수가 직접 지웁니다 (defer).
        result = rp.apply_amend(broker, symbol, exchange_code, fresh, state, journal_file, st.save_state,
                                history_fn, defer_journal_cleanup=True)
        attempted += 1
        entry = next((e for e in reversed(rp.read_journal(journal_file))
                      if e.get("symbol") == symbol and e.get("old_odno") == fresh["odno"]), None)
        dangerous = result in ("REJECTED_LATE", "UNCERTAIN", "LINK_FAILED")
        # 위험 결과의 중단 표식을 **먼저** 만듭니다 — 아래 기록 저장이 실패해도 중단은 유지돼야 합니다.
        # (REJECTED_LATE 도 journal 항목이 남아 있어 '사람 확인 필요' 표식을 지속 근거로 남길 수 있음)
        if dangerous:
            delivered = _halt(symbol, result, _fmt_item(symbol, fresh), now_utc, notify_fn, review_odno=fresh["odno"])
            _register_halt_notified(record, now_utc, delivered)
        mw.record_attempt(record, symbol, now_utc, amended=False)
        if result == "AMENDED":
            # 복구 경로와 같은 항목 id 로 한 번만 센다 — 아래 저장 전에 종료돼도 journal 이 남아 있어 복구가 센다.
            if entry:
                mw.record_recovered(record, symbol, _entry_id(entry), entry.get("at"), True, now_utc)
            else:
                mw.record_attempt(record, symbol, now_utc, amended=True)
        try:
            mw.save_record(run_lock.watch_state_path(), record)  # 시도 직후 기록 (중단돼도 횟수 유지)
        except OSError as error:
            _log(f"감시 기록 저장 실패: {error}")
            if not dangerous:  # 횟수·간격을 신뢰할 수 없으므로 더 정정하지 않습니다 (journal 은 그대로 남김)
                _halt(symbol, "RECORD_SAVE_FAILED", f"정정 후 감시 기록 저장 실패: {str(error)[:80]}", now_utc, notify_fn)
            return "halt"
        if result == "AMENDED":
            try:
                rp.drop_journal_entry(journal_file, fresh["odno"])  # 기록이 끝난 뒤에만 복구 근거를 지웁니다
            except OSError as error:
                _log(f"journal 항목 제거 실패(다음 실행의 복구가 정리합니다): {error}")
        if dangerous:
            return "halt"
        if result == "AMENDED":
            _safe_notify(notify_fn, f"✅ {symbol} 가격 정정 완료\n{_fmt_item(symbol, fresh)}")
        elif result == "REJECTED":
            _safe_notify(notify_fn, f"⚠️ {symbol} 정정 미접수 (원주문 유지)\n{_fmt_item(symbol, fresh)}")
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
    apply = settings["mode"] == mw.MODE_APPLY
    if warning:
        _log(warning)
        # 손상된 기록은 지우지 않고 옆으로 치웁니다 (이전 정정 횟수를 사람이 확인할 수 있게).
        moved = mw.quarantine_record(run_lock.watch_state_path(), now_utc)
        _log(f"손상된 기록을 보존했습니다: {os.path.basename(moved) if moved else '(이동 실패)'}")
        # DRY 든 APPLY 든 **중단 표식**을 남깁니다: DRY 가 손상 파일을 치우고 새 기록을 저장해도,
        # 이후 APPLY 가 '정상적인 새 기록'으로 오해해 한도를 초기화한 채 정정하지 않도록 사람 확인을 요구합니다.
        if not mw.read_halt(run_lock.watch_halt_path()):
            delivered = _halt("*", "RECORD_CORRUPT", f"감시 기록을 읽을 수 없어 APPLY 를 보류합니다: {warning[:80]}", now_utc, notify_fn)
            _register_halt_notified(record, now_utc, delivered)
        try:
            mw.save_record(run_lock.watch_state_path(), record)
        except OSError:
            pass
        if apply:
            return 1
    halt = mw.read_halt(run_lock.watch_halt_path())
    if apply and halt:
        _log(f"중단 표식이 있어 정정하지 않습니다: {halt.get('symbol')} {halt.get('result')} {halt.get('reason', '')}")
        _notify_once(record, "halt_reminder", mw.digest(halt.get("at"), halt.get("result")), now_utc, notify_fn,
                     f"🛑 메리츠 정정 감시가 중단 상태입니다 ({halt.get('symbol')} {halt.get('result')}). "
                     f"원인 확인 후 .watch_halt.json 삭제 필요", urgent=True, repeat_after_min=360)
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
        _notify_once(record, "error", mw.digest(text), now_utc, notify_fn,
                     f"⚠️ 메리츠 정정 감시 오류 ({settings['mode']}): {text}", urgent=True, repeat_after_min=60)
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
