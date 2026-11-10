# 메리츠 보정된 일반모드 BUY LOC 사후 재조정 (긴급 hotfix)
#
# 배경: 주문 시점 현재가 ×1.19 상한으로 낮춘 BUY LOC 는, 이후 가격이 올라
#       원래 전략 매수가 이하인데도 보정가를 넘으면 체결되지 않습니다.
# 해법: 최신 시세의 허용 범위(현재가 ×1.19) 안에서 정정 가격을 올리되,
#       원래 전략 매수가는 절대 넘지 않습니다.
#
# 범위 (이번 긴급 패치): 보정된 일반모드 BUY LOC 의 "가격 정정"만 다룹니다.
#   - 수량 합산·동적 추가매수·전략 공식은 건드리지 않습니다.
#   - 부분체결 주문은 정정하지 않습니다 (정정 수량의 의미가 부분체결 시 미검증).
#   - 접수 여부가 불확실하면 자동 재주문하지 않습니다.
import json
import math
import os
import time
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from broker.base import OrderNotAcceptedError
import run_lock
from state import mask_odno, _order_us_session_date
from strategy import cap_buy_loc_price, adjust_price_to_tick

# 계획 항목의 action 값
ACTION_AMEND = "AMEND"                # 기존 접수 주문을 가격 정정
ACTION_NOOP = "NOOP"                  # 이미 충분히 높음 (할 일 없음)
ACTION_SKIP = "SKIP"                  # 안전 조건 미충족 — 건드리지 않음
ACTION_REORDER = "REORDER_CANDIDATE"  # 거부된 주문 (참고용 — 이 도구는 재주문하지 않음)

_LOC_CLS_CODE = "32"



def is_valid_price(value) -> bool:
    """유한한 양수 가격인지 확인합니다 (inf/nan 거부)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def unresolved_symbols(path=None) -> set:
    """미해결 정정 journal 이 남아 있는 종목 집합 (봇이 정산 전에 확인)."""
    try:
        return {str(e.get("symbol", "")).upper() for e in read_journal(path or run_lock.journal_path())}
    except (OSError, ValueError):
        # journal 을 읽을 수 없으면 안전하게 모든 종목을 보류 대상으로 봅니다
        return {"*"}


def _plain_odno(odno) -> str:
    """합성 주문번호 'YYYYMMDD:0000123' 에서 주문번호 숫자만 꺼냅니다."""
    tail = str(odno or "").split(":")[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return str(int(digits)) if digits else ""


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _skip(odno, reason):
    return {"action": ACTION_SKIP, "odno": odno, "reason": reason}


def find_amend_rows(history, odno):
    """odno 를 원주문으로 하는 같은 날짜의 정정 행들 (거부 포함)."""
    plain = _plain_odno(odno)
    day = str(odno).split(":")[0]
    return [
        row for row in (history or [])
        if row.get("_rctf_code") == "1"
        and row.get("_plain_orgl_oder_no") == plain
        and row.get("_oder_date") == day
    ]


def plan_order(odno, meta, row, last_price, overrides, today_session, amend_rows=()):
    """주문 1건의 재조정 계획을 만듭니다 (조회만, 부작용 없음)."""
    if meta.get("side") != "BUY":
        return _skip(odno, "매수 주문 아님")
    if meta.get("reverse_action"):
        return _skip(odno, "리버스 주문은 대상 아님")
    if meta.get("is_additional"):
        return _skip(odno, "추가매수 주문은 대상 아님")
    if row is None:
        return _skip(odno, "이력에 주문 행 없음 (상태 불확실)")

    session = _order_us_session_date(row)
    if session is None or session != today_session:
        return _skip(odno, "오늘 미국 세션 주문이 아님 (만료 또는 판별 불가)")

    # 거부된 정정 행이 있으면 이력은 원주문을 '정정됨·잔량 0'으로 보여 주지만,
    # 정정이 거부됐다면 원주문이 살아 있을 수 있습니다. 이력만으로 판별할 수 없으므로 보류합니다.
    if any(r.get("_is_rejected") for r in amend_rows):
        return _skip(odno, "거부된 정정 행이 있어 원주문 생존 여부를 판별할 수 없음 — 보류 (앱/HTS 에서 직접 확인)")
    if amend_rows:
        return _skip(odno, "이미 정정된 주문 (정정 후 주문번호로 관리)")

    original = meta.get("original_price") or overrides.get(odno)
    if not is_valid_price(original):
        return _skip(odno, "원래 전략 매수가를 알 수 없거나 유효하지 않음 (--original 확인)")
    original = float(original)

    if row.get("_oder_cls_code") != _LOC_CLS_CODE:
        return _skip(odno, "LOC 주문이 아님")
    if row.get("sll_buy_dvsn_cd_name") != "매수":
        return _skip(odno, "이력상 매수 주문이 아님")

    status = str(row.get("prcs_stat_name") or "")
    filled = _to_int(row.get("ft_ccld_qty"))
    remaining = _to_int(row.get("nccs_qty"))
    ordered = _to_int(row.get("ft_ord_qty"))
    if status == "부분체결" or (filled or 0) > 0:
        return _skip(odno, "부분체결 주문은 정정하지 않음 (수동 확인 필요)")
    if status != "미체결":
        return _skip(odno, f"미체결 상태가 아님 ({status or '상태 없음'})")
    if filled is None or remaining is None or ordered is None:
        return _skip(odno, "체결·잔량 수치를 읽을 수 없음")
    if int(meta.get("processed_filled_qty") or 0) > 0:
        return _skip(odno, "이미 체결이 반영된 주문")
    if ordered <= 0 or remaining != ordered:
        return _skip(odno, "잔량이 주문수량과 다름")
    if int(meta.get("total_qty") or 0) != ordered:
        return _skip(odno, "state 수량과 이력 수량이 다름")

    current_price = float(row.get("_order_price") or 0)
    if current_price <= 0:
        return _skip(odno, "이력에서 현재 주문가를 읽을 수 없음")
    if not last_price or last_price <= 0:
        return _skip(odno, "최신 시세를 읽을 수 없음")

    # 허용 상한(현재가 ×1.19)과 원래 전략 매수가 중 낮은 쪽이 목표가
    target, _capped = cap_buy_loc_price(original, last_price)
    target = adjust_price_to_tick(target)
    if target > original + 1e-9:  # 방어: 원래 매수가는 절대 넘지 않음
        return _skip(odno, "목표가가 원래 매수가를 초과 (계산 오류 방지)")

    item = {
        "odno": odno, "qty": ordered, "current_price": current_price,
        "original_price": original, "target_price": target, "last_price": last_price,
    }
    if target <= current_price + 1e-9:
        return {**item, "action": ACTION_NOOP, "reason": "현재 주문가가 이미 목표가 이상"}
    return {**item, "action": ACTION_AMEND, "reason": "보정가를 목표가까지 상향 정정"}


def build_plan(state, history, last_price, overrides, today_session):
    """종목 1개의 전체 계획을 만듭니다. 거부된 오늘 BUY LOC 는 참고용으로만 나열합니다."""
    by_odno = {str(row.get("odno")): row for row in (history or [])}
    plan = []
    for odno, meta in (state.get("orders_meta") or {}).items():
        plan.append(plan_order(str(odno), meta or {}, by_odno.get(str(odno)),
                               last_price, overrides, today_session,
                               amend_rows=find_amend_rows(history, str(odno))))
    known = {item["odno"] for item in plan}
    for odno, row in by_odno.items():
        if odno in known or row.get("prcs_stat_name") != "거부":
            continue
        if row.get("sll_buy_dvsn_cd_name") != "매수" or row.get("_oder_cls_code") != _LOC_CLS_CODE:
            continue
        if _order_us_session_date(row) != today_session:
            continue
        plan.append({
            "action": ACTION_REORDER, "odno": odno,
            "reason": "브로커가 거부한 주문 — 접수 안 됨. 재주문은 이 도구가 하지 않음 (수동 판단)",
        })
    return plan


def relink_amended_order(state, old_odno, new_odno, qty, new_price, original_price=None):
    """정정으로 바뀐 주문번호로 orders_meta 키를 옮깁니다 (재실행해도 안전).

    체결 반영 표식(processed_*)은 그대로 이어받아 T·순투입이 중복 정산되지 않게 합니다.
    """
    metas = state.setdefault("orders_meta", {})
    if old_odno not in metas:
        if new_odno in metas:
            return False  # 이미 옮겨짐 — 재시작/중복 실행
        raise RuntimeError("원 주문 메타도 새 주문 메타도 없어 연결할 수 없습니다")
    if new_odno in metas:
        raise RuntimeError("새 주문번호 메타가 이미 존재해 덮어쓸 수 없습니다")
    meta = deepcopy(metas.pop(old_odno))
    meta["total_qty"] = int(qty)
    meta["order_price"] = float(new_price)
    meta["amended_from"] = old_odno
    # --original 로 지정한 구 주문도 다음 재조정이 가능하도록 원래 매수가를 새 메타에 남깁니다
    if original_price and not meta.get("original_price") and is_valid_price(original_price):
        meta["original_price"] = float(original_price)
    metas[new_odno] = meta
    return True


# ── 정정 journal: 정정 호출 직전에 기록하고, state 연결이 끝나면 지웁니다 ─────────────

def read_journal(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_journal(path, entries):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _amend_row_matches(row, entry):
    """정정 행이 journal 항목의 정정 건과 같은 주문인지 (거부 여부는 따로 판단)."""
    return (
        row.get("_rctf_code") == "1"
        and row.get("_plain_orgl_oder_no") == _plain_odno(entry["old_odno"])
        and row.get("_oder_date") == str(entry["old_odno"]).split(":")[0]
        # 정정 접수 뒤 그 정정 주문이 취소되면 어댑터가 정정 행 자체를 _is_cancel 로 표시합니다.
        # 취소 여부와 무관하게 같은 정정 건으로 인식해야 journal 이 풀립니다 (부분체결분 보존).
        and row.get("sll_buy_dvsn_cd_name") == "매수"
        and row.get("_oder_cls_code") == _LOC_CLS_CODE
        and row.get("_ordered_qty") == int(entry["qty"])
        and abs(float(row.get("_order_price") or 0) - float(entry["target_price"])) < 0.005
    )


def resolve_journal(entries, history, state):
    """미해결 journal 을 이력으로 확인해 state 에 연결합니다.

    - 접수된 정정 행이 정확히 1개 → 새 주문번호로 연결
    - 접수된 행이 없고 거부된 정정 행만 있음 → 연결하지 않고 항목만 정리
      (원주문 메타는 그대로 유지: 원주문이 살아 있을 수 있어 체결이 정상 정산되도록)
    - 그 외(0개, 2개 이상, 접수+거부 혼재) → 불확실로 남김, 자동 재주문 없음
    Returns: (남은 entries, 메시지 목록)
    """
    remaining, messages = [], []
    for entry in entries:
        accepted = [r for r in (history or []) if _amend_row_matches(r, entry) and not r.get("_is_rejected")]
        rejected = [r for r in (history or []) if _amend_row_matches(r, entry) and r.get("_is_rejected")]
        if len(accepted) == 1 and not rejected:
            relink_amended_order(
                state, entry["old_odno"], str(accepted[0]["odno"]),
                entry["qty"], entry["target_price"], entry.get("original_price"),
            )
            messages.append(f"연결 완료: {mask_odno(entry['old_odno'])} → {mask_odno(accepted[0]['odno'])}")
        elif not accepted and rejected:
            messages.append(
                f"정정 거부 확인: {mask_odno(entry['old_odno'])} — 연결하지 않음. "
                f"원주문 생존 여부는 이력으로 판별 불가(보류, 앱/HTS 확인 필요)"
            )
        else:
            remaining.append(entry)
            messages.append(
                f"불확실 유지(접수 {len(accepted)}건/거부 {len(rejected)}건): "
                f"{mask_odno(entry['old_odno'])} — 자동 재주문 안 함"
            )
    return remaining, messages


def verify_new_order(history_fn, new_odno, tries=3, wait=2.0):
    """정정 직후 이력에서 새 주문의 접수 여부를 확인합니다.

    Returns: "ACCEPTED" | "REJECTED" | "UNKNOWN" (이력 지연·조회 실패·상태 불명)
    """
    for attempt in range(tries):
        if attempt:
            time.sleep(wait)
        try:
            rows = history_fn()
        except Exception:
            continue
        row = next((r for r in rows if str(r.get("odno")) == str(new_odno)), None)
        if row is None:
            continue
        if row.get("_is_rejected"):
            return "REJECTED"
        if row.get("prcs_stat_name") in ("미체결", "부분체결", "체결"):
            return "ACCEPTED"
    return "UNKNOWN"


def apply_amend(broker, symbol, exchange_code, item, state, journal_path, save_fn,
                history_fn, verify_wait=2.0):
    """정정 1건을 실행합니다. 결과 문자열을 반환합니다.

    결과:
      "AMENDED"        정정 접수 확인 + state 연결 완료
      "REJECTED"       브로커가 API 응답으로 미접수 확정 (주문은 그대로 살아 있음)
      "REJECTED_LATE"  API 는 접수했으나 이력에 거부로 남음 — 메타 이동 안 함, 원주문 생존 불명(보류).
                       journal 항목을 **지우지 않고** review_required="REJECTED_LATE" 표식을 남깁니다 —
                       어댑터가 거부된 정정 행도 원주문을 '정정됨·잔량 0'으로 보여 주므로, 살아 있는 원주문을
                       봇의 잔여 주문 취소가 건너뛰어 신규 주문과 중복될 수 있어 해당 종목의 봇 정산·주문을 막습니다.
                       사람이 원주문 상태를 확인한 뒤 수동 도구(--original-checked)로만 정리합니다.
      "UNCERTAIN"      접수 여부 불확실 — journal 유지, 호출부는 즉시 중단해야 함
      "LINK_FAILED"    정정은 접수됐지만 state 저장 실패 — journal 유지, 즉시 중단
    """
    old_odno = item["odno"]
    entries = read_journal(journal_path)
    entries.append({
        "symbol": symbol, "old_odno": old_odno, "qty": item["qty"],
        "target_price": item["target_price"], "original_price": item.get("original_price"),
        "at": datetime.now(ZoneInfo("UTC")).isoformat(),
    })
    write_journal(journal_path, entries)  # 호출 전에 먼저 기록
    others = [e for e in entries if e["old_odno"] != old_odno]

    try:
        new_odno = broker.modify_order(
            old_odno, symbol, exchange_code, "BUY", item["qty"], item["target_price"], "LOC"
        )
    except OrderNotAcceptedError as error:
        # 브로커가 접수하지 않았음이 확정 — 원 주문은 그대로, journal 항목 제거
        write_journal(journal_path, others)
        print(f"  ✗ 정정 미접수 확정: {mask_odno(old_odno)} — {str(error)[:120]}")
        return "REJECTED"
    except Exception as error:
        print(f"  ⚠️ 정정 접수 여부 불확실: {mask_odno(old_odno)} — {str(error)[:120]}")
        return "UNCERTAIN"

    # API 응답만으로 연결하지 않습니다 — 실제로 이력에 접수로 남았는지 확인합니다.
    verdict = verify_new_order(history_fn, new_odno, wait=verify_wait)
    if verdict == "REJECTED":
        entries[-1]["review_required"] = "REJECTED_LATE"  # 이번 항목에만 표식 (others 는 그대로)
        write_journal(journal_path, entries)
        print(f"  ⚠️ 정정이 이력에 '거부'로 남음: {mask_odno(old_odno)} → {mask_odno(new_odno)}. "
              f"메타를 옮기지 않았습니다. 원주문 생존 여부를 판별할 수 없어 journal 에 표식을 남기고 "
              f"봇의 해당 종목 정산·주문을 보류합니다. 앱/HTS 에서 확인·정리한 뒤 "
              f"수동 도구 --apply --confirm ... --original-checked 로 해소하세요")
        return "REJECTED_LATE"
    if verdict == "UNKNOWN":
        print(f"  ⚠️ 정정 후 이력에서 새 주문을 확인하지 못함: {mask_odno(old_odno)} → "
              f"{mask_odno(new_odno)}. journal 을 유지하고 중단합니다")
        return "UNCERTAIN"

    try:
        relink_amended_order(state, old_odno, str(new_odno), item["qty"], item["target_price"],
                             item.get("original_price"))
        save_fn(symbol, state)
    except Exception as error:
        print(f"  ✗ 정정은 접수됐지만 state 연결 실패: {mask_odno(old_odno)} → "
              f"{mask_odno(new_odno)} — {str(error)[:120]}")
        return "LINK_FAILED"

    write_journal(journal_path, others)
    print(f"  ✓ 정정 완료: {mask_odno(old_odno)} → {mask_odno(new_odno)} "
          f"(${item['current_price']:.2f} → ${item['target_price']:.2f})")
    return "AMENDED"
