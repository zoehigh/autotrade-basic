"""
무인 실행 결과 전용 검증 도구 (read-only — 주문/상태 변경 없음).

    python ops/verify_run.py [YYYYMMDD]

검사 항목
  1) 해당 날짜의 실행 로그 — 모드, exit code, 실행 결과 집계
  2) 브로커 — 실제로 접수된 주문 (수량·가격), 체결
  3) state — T / 순투입 / 보유 / fence
  4) 정합성 — 매도 합계 = 보유량, 매수 가격 중복 없음, 주문 수 일치
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "src"))
os.chdir(APP_DIR)

from broker.meritz.adapter import MeritzBroker  # noqa: E402
from state import load_state  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
problems: list[str] = []
notes: list[str] = []


def problem(msg):
    problems.append(msg)
    print(f"  [!! ] {msg}")


def note(msg):
    notes.append(msg)
    print(f"  [참고] {msg}")


def ok(msg):
    print(f"  [OK ] {msg}")


def _load_settlement_evidence(symbol, run_id):
    """
    **검증 대상 실행의** 정산 근거 파일을 읽습니다 (없으면 None).

    가장 최신 파일을 쓰면 다른 실행(예: 이후 DRY)의 근거로 잘못 검증할 수 있으므로,
    실행 식별자(run_id)로 정확히 일치하는 파일만 사용합니다.
    """
    if not run_id:
        return None
    for path in sorted(Path(".").glob(f"settlement-{symbol}-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("run_id") or "") == run_id:
            return data
    return None


def _classify_sell_ratio(ratio: float) -> float:
    """정산 로직과 같은 분류 규칙 (독립 구현)."""
    if ratio >= 1.0:
        return 0.0
    if ratio >= 0.5:
        return 0.25
    return 0.75


def _ledger_entries(ledger):
    """원장 항목을 seq 순서로 반환합니다 (형식 오류는 호출부에서 검사)."""
    entries = (ledger or {}).get("entries") or []
    return sorted(entries, key=lambda e: int(e.get("seq", 0) or 0))


def _replay_ledger(ledger, default_start=None):
    """
    순서 원장을 **독립적으로** 재생합니다 (정산 헬퍼 미사용).

    - sell: T *= factor
    - buy/adjust/reverse_*: T += delta
    """
    entries = _ledger_entries(ledger)
    if ledger and "start_T" in ledger:
        t_value = float(ledger.get("start_T") or 0.0)
    else:
        t_value = float(default_start or 0.0)
    for entry in entries:
        kind = entry.get("kind")
        if kind == "sell":
            t_value *= float(entry.get("factor", 1.0) or 0.0)
        elif kind in ("buy", "adjust", "reverse_sell", "reverse_buy"):
            t_value += float(entry.get("delta", 0.0) or 0.0)
    return t_value


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _parse_qty_marker(meta):
    """`processed_filled_qty` 마커를 엄격히 판정합니다 (state.py 와 **독립 구현**).

    검증기는 정산 코드를 재사용하지 않습니다 (같은 오류를 그대로 통과시키지 않기 위해).
    - 키 누락·None·bool·파싱 불가·음수·소수(float) 는 무효입니다.
    - 명시적이고 유효한 0 이상 정수만 유효 마커로 인정합니다.

    Returns:
        tuple: (유효 여부, 반영 수량 — 무효면 0)
    """
    if not isinstance(meta, dict) or "processed_filled_qty" not in meta:
        return False, 0
    raw = meta.get("processed_filled_qty")
    if raw is None or isinstance(raw, bool):
        return False, 0
    try:
        if isinstance(raw, float) and not raw.is_integer():
            # 0.5 를 0 으로 절삭하면 "미반영"으로 오인되어 이중 가산됩니다.
            return False, 0
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return False, 0
    if value < 0:
        return False, 0
    return True, value


def _pending_marker_odnos(evidence):
    """마커 누락·무효로 **정산이 보류된** 주문번호 목록을 읽습니다."""
    raw = (evidence or {}).get("pending_marker_odnos") or []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    result = []
    for value in raw:
        if value is None:
            continue
        odno = str(value)
        if odno and odno not in result:
            result.append(odno)
    return result


def _adjustment_is_explicit(entry):
    """T-only 복구처럼 근거가 기록된 adjust 인지 확인합니다."""
    target = entry.get("target_T", entry.get("target"))
    before = entry.get("before_T", entry.get("before", entry.get("replay_T")))
    return (
        str(entry.get("kind")) == "adjust"
        and bool(entry.get("source") or entry.get("type") or entry.get("reason"))
        and target is not None
        and before is not None
        and bool(entry.get("applied_at") or entry.get("at") or entry.get("timestamp"))
    )


def _fill_after_watermark(fill, evidence):
    watermark = str(evidence.get("watermark_before") or "")
    if not watermark:
        return True
    ts = str(fill.get("ts") or "")
    if not ts:
        return True
    if ts > watermark:
        return True
    if ts < watermark:
        return False
    odno = str(fill.get("odno") or "")
    last_ordno = str(evidence.get("last_processed_ordno_before") or "")
    if not last_ordno:
        return True
    try:
        return int(odno) > int(last_ordno)
    except (TypeError, ValueError):
        return odno > last_ordno


def _unregistered_expected_shares(orders, fills):
    """미등록 매수의 T 기여분을 **건수 폴백**으로 독립 배분합니다.

    정산은 orders_meta 에 없는 정상매수를 주문일(ord_dt) 단위로 묶어
    건수 2건 이상이면 +1.0, 1건이면 +0.5 를 수량 비례로 나눕니다.
    검증기는 정산이 기록한 t_target/t_delta 를 신뢰하지 않고 이 규칙을
    다시 계산해 배분 오류를 검출합니다.
    """
    fill_by_odno = {str(f.get("odno")): f for f in (fills or [])}
    groups = {}
    for order in orders or []:
        if not order.get("unregistered") or order.get("reverse"):
            continue
        odno = str(order.get("odno") or "")
        day = str(order.get("ord_dt") or "")
        if not day:
            ts = str((fill_by_odno.get(odno) or {}).get("ts") or "")
            day = ts[:10]
        groups.setdefault(day, []).append(order)

    shares = {}
    for group in groups.values():
        total_delta = 1.0 if len(group) >= 2 else 0.5
        total_qty = sum(_as_int(order.get("qty_delta")) for order in group)
        remaining = total_delta
        for idx, order in enumerate(group):
            qty = _as_int(order.get("qty_delta"))
            if idx == len(group) - 1:
                share = round(remaining, 4)
            else:
                share = (
                    round((qty / total_qty) * total_delta, 4)
                    if total_qty else 0.0
                )
                remaining = round(remaining - share, 4)
            shares[str(order.get("odno") or "")] = share
    return shares


def _independent_odno_hash(odno, salt_hex):
    """공개 로그용 주문번호 가명 식별자 (state.mask_odno 와 **독립 구현**).

    `ord#` + HMAC-SHA256(salt, odno) 앞 10자리. salt 는 Evidence 의 `log_salt` 입니다.
    """
    import hashlib
    import hmac

    text = "" if odno is None else str(odno)
    if not text:
        return "ord#-"
    return "ord#" + hmac.new(
        bytes.fromhex(salt_hex), text.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:10]


def _evidence_salt_hex(evidence):
    """Evidence 의 log_salt(16바이트 hex)를 읽습니다. 없거나 형식이 틀리면 None."""
    raw = (evidence or {}).get("log_salt")
    if not isinstance(raw, str) or len(raw) != 32:
        return None
    try:
        bytes.fromhex(raw)
    except ValueError:
        return None
    return raw


def _collect_evidence_odnos(evidence):
    """Evidence 안의 모든 주문번호(원문)를 모읍니다 — 출력 마스킹 대상 목록."""
    found = set()

    def add(value):
        if value is not None and str(value):
            found.add(str(value))

    for key in ("fills", "orders", "skipped_unverified", "skipped_fills"):
        for item in evidence.get(key) or []:
            if isinstance(item, dict):
                add(item.get("odno"))
    for key in ("orders_meta_before", "orders_meta_after", "sell_t_effects", "sell_t_effects_before"):
        block = evidence.get(key)
        if isinstance(block, dict):
            for odno in block:
                add(odno)
    for key in ("pending_marker_odnos", "pending_amount_odnos"):
        for odno in evidence.get(key) or []:
            add(odno)
    add(evidence.get("last_processed_ordno_before"))
    return found


def _mask_problem_messages(problems, evidence):
    """검증 결과 메시지의 원문 주문번호를 가명 식별자로 바꿉니다 (공개 Actions 로그 보호).

    Evidence 자체(암호화 artifact)는 원문을 유지하므로 독립 검증은 영향받지 않습니다.
    salt 가 없으면 이번 실행의 임시 salt 를 씁니다 (대조는 불가, 노출은 차단).
    """
    salt = _evidence_salt_hex(evidence) or os.urandom(16).hex()
    odnos = sorted(_collect_evidence_odnos(evidence), key=len, reverse=True)
    masked = []
    for message in problems:
        text = str(message)
        for odno in odnos:
            text = re.sub(
                rf"(?<![\w.#]){re.escape(odno)}(?![\w.])",
                _independent_odno_hash(odno, salt),
                text,
            )
        masked.append(text)
    return masked


def _legacy_zero_marker_unproven(meta):
    """등록 메타의 수량 마커가 유효한 0 이면서 '미반영 증명'(버전+금액 0.0)이 없는지 판정합니다."""
    valid, qty = _parse_qty_marker(meta)
    if not valid or qty != 0:
        return False
    if meta.get("fill_marker_version") != 1:
        return True
    raw = meta.get("processed_filled_amount")
    if raw is None or isinstance(raw, bool):
        return True
    try:
        return abs(float(raw)) >= 1e-9
    except (TypeError, ValueError):
        return True


def _verify_skipped_unverified(evidence):
    """증명 불가로 '건너뛴' 과거 주문 감사 내역(skipped_unverified)을 독립 검증합니다.

    건너뛰기는 에러 없이 지나가므로 정당성과 누락을 따로 확인하지 않으면
    (1) 정상 신규 체결을 잘못 건너뛰거나 (2) 건너뛰었다고 적고 실제로는 반영하거나
    (3) 건너뛰지도 반영하지도 않은 주문이 조용히 사라질 수 있습니다.

    - 각 항목: 해시 일치, 사유 토큰, 워터마크 이전, 등록 메타의 레거시 0 마커(증명 불가),
      이력 체결수량 일치, 반영(orders)에 없음, 마커 불변
    - 누락: 같은 조건의 과거 체결이 건너뜀 목록에도 반영 목록에도 없으면 실패
    - 반영: 증명 불가 과거 주문이 반영됐으면 실패 (72건 재반영 사고의 직접 검출)
    """
    problems = []
    skipped = evidence.get("skipped_unverified") or []
    if not isinstance(skipped, list):
        return ["skipped_unverified 형식 오류 (목록이 아님)"]
    pre_meta = evidence.get("orders_meta_before")
    post_meta = evidence.get("orders_meta_after")
    if not isinstance(pre_meta, dict):
        # 구버전 근거: 판정 근거가 없으므로 건너뛴 항목이 있을 때만 검증 불가로 처리합니다.
        return ["skipped_unverified 검증 불가 (orders_meta_before 없음)"] if skipped else []
    salt = _evidence_salt_hex(evidence)
    if salt is None:
        if skipped:
            return ["log_salt 가 없거나 형식 오류입니다 (건너뜀 감사 해시 대조 불가 — 검증 불가)"]
        salt = os.urandom(16).hex()  # 건너뜀 항목이 없으면 메시지 라벨용 임시 salt 만 필요
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}
    applied = {
        str(o.get("odno")) for o in (evidence.get("orders") or []) if o.get("odno") is not None
    }
    listed = set()
    for entry in skipped:
        odno = str(entry.get("odno", "")) if isinstance(entry, dict) else ""
        if not odno:
            problems.append("skipped_unverified 항목에 odno 가 없습니다")
            continue
        label = _independent_odno_hash(odno, salt)
        listed.add(odno)
        if entry.get("odno_hash") != label:
            problems.append(f"{label} 건너뜀 항목의 odno_hash 가 주문번호와 일치하지 않습니다")
        if entry.get("reason") != "legacy_zero_marker_unproven":
            problems.append(f"{label} 건너뜀 사유가 알 수 없는 값입니다: {entry.get('reason')!r}")
        fill = fills.get(odno)
        if fill is None:
            problems.append(f"{label} 건너뜀 주문이 이력(fills)에 없습니다")
            continue
        if _fill_after_watermark(fill, evidence):
            problems.append(f"{label} 건너뜀 주문이 워터마크 이후 신규 체결입니다 (잘못된 건너뛰기)")
        if not _legacy_zero_marker_unproven(pre_meta.get(odno)):
            problems.append(f"{label} 건너뜀 주문의 반영 전 마커가 레거시 0 마커가 아닙니다")
        if _as_int(entry.get("filled_qty")) != _as_int(fill.get("qty")):
            problems.append(
                f"{label} 건너뜀 체결수량 불일치: 기록 {entry.get('filled_qty')} vs 이력 {fill.get('qty')}"
            )
        if odno in applied:
            problems.append(f"{label} 건너뛰었다고 기록됐지만 반영 내역(orders)에도 있습니다")
        if isinstance(post_meta, dict) and post_meta.get(odno) != pre_meta.get(odno):
            problems.append(f"{label} 건너뛴 주문의 마커가 변경됐습니다")

    for odno, fill in fills.items():
        meta = pre_meta.get(odno)
        if odno in listed or not isinstance(meta, dict):
            continue
        if _fill_after_watermark(fill, evidence):
            continue
        if not _legacy_zero_marker_unproven(meta) or _as_int(fill.get("qty")) <= 0:
            continue
        # 리버스 주문·금액 미확정은 다른 경로(reconcile / 금액 확정 대기)가 담당합니다.
        if fill.get("reverse_action") or meta.get("reverse_action"):
            continue
        if not fill.get("amount_confirmed", True):
            continue
        label = _independent_odno_hash(odno, salt)
        if odno in applied:
            problems.append(
                f"{label} 증명 불가 과거 주문(레거시 0 마커)이 재반영됐습니다 (이중 가산 의심)"
            )
        else:
            problems.append(
                f"{label} 증명 불가 과거 주문이 건너뜀 목록에도 반영 내역에도 없습니다 (감사 누락)"
            )
    return problems


def _verify_fill_markers(evidence):
    """체결 마커(processed_filled_qty/amount) 무결성을 검사합니다.

    - 마커는 절대값이므로 이번 실행에서 적용된 주문은
      `max(반영 전 마커, 실제 누적 체결)` 로만 전진해야 합니다.
    - 적용되지 않은 주문의 마커가 바뀌면 이중 가산/변조입니다.
    - 브로커 누적금액이 역행한 경우 정산은 차감하지 않으므로
      마커도 역행하면 안 됩니다 (차감 누락/이중 차감 검출).
    - **적용된 주문의 마커가 누락·무효면 실패**입니다. 마커가 없을 때 0 으로
      읽으면 "미반영"으로 오인되어 이중 가산을 통과시킵니다.
    - 마커 누락·무효로 정산이 **보류된** 주문이 있으면 통과시키지 않습니다.
    """
    problems = []
    pending_marker = _pending_marker_odnos(evidence)
    if pending_marker:
        # 보류는 "검증할 것이 없다"가 아니라 "이 실행의 정산을 신뢰할 수 없다"입니다.
        problems.append(
            f"마커 누락·무효 주문 {len(pending_marker)}건"
            f"({', '.join(pending_marker[:5])})이 있어 정산이 보류됐습니다 (검증 불가)"
        )
    post_meta = evidence.get("orders_meta_after")
    if not isinstance(post_meta, dict):
        # 구버전 근거에는 after 스냅샷이 없습니다 — 검증 불가로 실패시키지 않습니다.
        return problems
    pre_meta = evidence.get("orders_meta_before")
    if not isinstance(pre_meta, dict):
        pre_meta = {}
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}
    applied = {
        str(order.get("odno"))
        for order in (evidence.get("orders") or [])
        if order.get("odno") is not None
    }
    # 미등록 매수는 orders_meta 자체가 없으므로 마커 요구 대상이 아닙니다.
    unregistered = {
        str(order.get("odno"))
        for order in (evidence.get("orders") or [])
        if order.get("unregistered") and order.get("odno") is not None
    }

    for odno, after_meta in post_meta.items():
        odno = str(odno)
        if not isinstance(after_meta, dict):
            problems.append(f"{odno} 정산 후 체결 마커 형식 오류")
            continue
        before_meta = pre_meta.get(odno)
        if not isinstance(before_meta, dict):
            before_meta = {}
        marker_valid, before_qty = _parse_qty_marker(before_meta)
        before_amount = _as_float(before_meta.get("processed_filled_amount"))
        after_qty = _as_int(after_meta.get("processed_filled_qty"))
        after_amount = _as_float(after_meta.get("processed_filled_amount"))

        if after_qty < before_qty:
            problems.append(
                f"{odno} 체결 수량 마커가 역행했습니다 "
                f"(반영 전 {before_qty} → 후 {after_qty})"
            )
            continue
        if after_amount < before_amount - 0.005:
            problems.append(
                f"{odno} 체결 금액 마커가 역행했습니다 "
                f"(반영 전 ${before_amount:.2f} → 후 ${after_amount:.2f})"
            )
            continue

        fill = fills.get(odno)
        if fill is None:
            if after_qty != before_qty or abs(after_amount - before_amount) > 0.005:
                problems.append(
                    f"{odno} 이력에 없는 주문의 체결 마커가 변경됐습니다 "
                    f"(이중 가산/변조 의심)"
                )
            continue

        raw_qty = _as_int(fill.get("qty"))
        raw_amount = _as_float(fill.get("amount"))
        if odno not in applied:
            if (
                after_qty != before_qty
                or abs(after_amount - before_amount) > 0.005
            ):
                problems.append(
                    f"{odno} 적용되지 않은 주문의 체결 마커가 변경됐습니다 "
                    f"(이중 가산/변조 의심)"
                )
            continue
        expected_qty = max(before_qty, raw_qty)
        expected_amount = max(before_amount, raw_amount)
        if odno not in unregistered and odno in pre_meta and not marker_valid:
            # 마커 없음/무효를 0 으로 읽으면 "미반영"으로 오인되어
            # 이미 반영된 체결을 다시 가산하도록 통과시킵니다.
            # (orders_meta_before 에 항목 자체가 없는 경우는 미등록 매수이거나
            #  스냅샷 누락이라, 필수 체결 전량 대조가 보수적으로 검출합니다)
            problems.append(
                f"{odno} 적용된 주문의 체결 수량 마커"
                f"(orders_meta_before.processed_filled_qty="
                f"{before_meta.get('processed_filled_qty')!r}) 가 누락·무효입니다 "
                f"(반영 여부 판정 불가 — 검증 불가)"
            )
            continue
        if after_qty != expected_qty:
            problems.append(
                f"{odno} 체결 수량 마커 불일치: 기대 {expected_qty}주 vs 기록 {after_qty}주 "
                f"(이중 가산/누락)"
            )
        if abs(after_amount - expected_amount) > 0.02:
            problems.append(
                f"{odno} 체결 금액 마커 불일치: 기대 ${expected_amount:.2f} "
                f"vs 기록 ${after_amount:.2f} (이중 가산/누락)"
            )
        # 레거시 마커가 실제 누적 체결보다 큰 경우는 제외하고, 새로 초과하는 것만 봅니다.
        if after_qty > max(before_qty, raw_qty):
            problems.append(
                f"{odno} 체결 수량 마커가 실제 누적 체결 {raw_qty}주를 초과합니다"
            )
        if after_amount > max(before_amount, raw_amount) + 0.02:
            problems.append(
                f"{odno} 체결 금액 마커가 실제 누적금액 ${raw_amount:.2f}를 초과합니다"
            )
    return problems


def _required_settlement_fills(evidence):
    """원시 이력 + 정산 전 마커 + 워터마크로 이번에 **반드시 처리해야 할 체결**을 확정합니다.

    `min(주장수량, 잔량)` 같은 축소 계산을 하지 않습니다. 주문 근거가 적게
    주장하더라도 필수 체결 전체가 반영됐는지 비교합니다.

    Returns:
        tuple: (problems, required_list, expected_money)
    """
    problems = []
    mode = evidence.get("settlement_mode") or evidence.get("mode")
    fills = list(evidence.get("fills") or [])
    pre_meta = evidence.get("orders_meta_before") or {}
    orders = evidence.get("orders") or []
    order_by_odno = {}
    for order in orders:
        order_by_odno.setdefault(str(order.get("odno")), order)
    effects_before = evidence.get("sell_t_effects_before") or {}
    unreg_shares = _unregistered_expected_shares(orders, fills)
    pending_marker = _pending_marker_odnos(evidence)
    pending_marker_set = set(pending_marker)

    def _meta_for(odno):
        meta = dict(order_by_odno.get(odno) or {})
        meta.update(pre_meta.get(odno) or {})
        return meta

    def _sort_key(fill):
        return (str(fill.get("ts") or ""), str(fill.get("odno") or ""))

    required = []
    for fill in sorted(fills, key=_sort_key):
        odno = str(fill.get("odno") or "")
        raw_qty = _as_int(fill.get("qty"))
        raw_amount = _as_float(fill.get("amount"))
        meta = _meta_for(odno)
        side = str(fill.get("side") or meta.get("side") or "")
        if side in ("BUY", "SELL"):
            side = "매수" if side == "BUY" else "매도"
        if not side and "sll_buy_dvsn_cd_name" in meta:
            side = meta.get("sll_buy_dvsn_cd_name", "")
        if mode == "full_reinference":
            req_qty = raw_qty
            req_amount = raw_amount
        else:
            pre_qty = _as_int(meta.get("processed_filled_qty"))
            amount_marker = "processed_filled_amount" in meta
            pre_amount = _as_float(meta.get("processed_filled_amount"))
            watermark = str(evidence.get("watermark_before") or "")
            ts = str(fill.get("ts") or "")
            if (
                watermark
                and ts
                and ts > watermark
                and pre_qty > 0
                and pre_qty >= raw_qty
            ):
                problems.append(
                    f"{odno} 마커가 워터마크 이후 체결을 이미 반영한 것으로 기록됐습니다 "
                    f"(마커 앞당김 의심: {pre_qty}/{raw_qty})"
                )
            if pre_qty > 0 or pre_amount > 0:
                req_qty = max(0, raw_qty - pre_qty)
                if amount_marker:
                    req_amount = max(0.0, raw_amount - pre_amount)
                elif raw_qty > 0:
                    req_amount = raw_amount * (req_qty / raw_qty)
                else:
                    req_amount = 0.0
            elif _fill_after_watermark(fill, evidence):
                req_qty = raw_qty
                req_amount = raw_amount if amount_marker or "processed_filled_amount" not in meta else max(0.0, raw_amount - pre_amount)
            else:
                req_qty = 0
                req_amount = 0.0
        if req_qty <= 0 and req_amount <= 1e-9:
            required.append({
                "odno": odno, "side": side, "req_qty": 0, "req_amount": 0.0,
                "kind": None, "expects_entry": False,
                "pending_marker": odno in pending_marker_set,
            })
            continue

        is_additional = bool(meta.get("is_additional"))
        reverse_action = meta.get("reverse_action") or (
            "sell" if fill.get("reverse") and side == "매도" else
            "buy" if fill.get("reverse") and side == "매수" else ""
        )
        total_qty = _as_int(meta.get("total_qty") or raw_qty)
        t_target = _as_float(meta.get("t_target"))
        expected_delta = 0.0
        expected_factor = None
        kind = None
        expects_entry = False
        if side == "매수":
            if meta.get("unregistered"):
                # 미등록 매수는 정산 기록값이 아니라 건수 폴백 배분으로 검증합니다.
                expected_delta = unreg_shares.get(odno, 0.0)
                kind = "buy"
            elif reverse_action == "buy":
                applied = min(1.0, max(0.0, _as_float(meta.get("applied_fill_fraction"))))
                observed = (raw_qty / total_qty) if total_qty else applied
                effective = max(applied, min(1.0, observed))
                expected_delta = round(
                    _as_float(meta.get("reverse_t_target")) * max(0.0, effective - applied), 4
                )
                kind = "reverse_buy"
            elif not is_additional and total_qty > 0 and t_target:
                expected_delta = round((req_qty / total_qty) * t_target, 4)
                kind = "buy"
            expects_entry = abs(expected_delta) > 1e-9
        elif side == "매도":
            if reverse_action == "sell":
                applied = min(1.0, max(0.0, _as_float(meta.get("applied_fill_fraction"))))
                observed = (raw_qty / total_qty) if total_qty else applied
                effective = max(applied, min(1.0, observed))
                base_t = _as_float(meta.get("reverse_base_t"))
                factor = _as_float(meta.get("reverse_t_factor", 1.0))
                expected_delta = round(
                    -base_t * (1.0 - factor) * max(0.0, effective - applied), 4
                )
                kind = "reverse_sell"
                expects_entry = abs(expected_delta) > 1e-9
            else:
                marker = effects_before.get(odno) or {}
                basis = _as_int(marker.get("basis"))
                expected_factor = _classify_sell_ratio(
                    (raw_qty / basis) if basis else 0.0
                )
                old_factor = _as_float(marker.get("factor", 1.0))
                kind = "sell"
                expects_entry = req_qty > 0 or abs(expected_factor - old_factor) > 1e-9
        item = {
            "odno": odno,
            "side": side,
            "req_qty": req_qty,
            "req_amount": req_amount,
            "kind": kind,
            "expected_delta": expected_delta,
            "expected_factor": expected_factor,
            "expects_entry": expects_entry,
            "meta": meta,
            "raw_qty": raw_qty,
            "raw_amount": raw_amount,
            "ts": fill.get("ts"),
        }
        if odno in pending_marker_set:
            # 마커 누락·무효로 **보류된** 주문은 이번 실행에서 반영되면 안 됩니다.
            # 루프 상단에서 통째로 걸러내면 필수 체결 집합 자체가 달라져
            # 순보유/순투입 기대가 왜곡되므로, 항목은 남기고 기대만 무력화합니다.
            item.update({
                "req_qty": 0,
                "req_amount": 0.0,
                "kind": None,
                "expected_delta": 0.0,
                "expected_factor": None,
                "expects_entry": False,
                "pending_marker": True,
            })
        required.append(item)

    # 보류(마커 누락·무효) 주문은 반영되지 않아야 합니다 — 둘 다면 판정 불가입니다.
    applied_odnos = {str(order.get("odno")) for order in orders}
    for odno in pending_marker:
        if odno in applied_odnos:
            problems.append(
                f"{odno} 마커 누락·무효 보류 주문인데 적용 내역이 있습니다 "
                f"(보류와 적용이 동시에 존재 — 검증 불가)"
            )

    # 보유 궤적·기준선·순투입금은 **필수 체결을 시간순으로** 적용해 독립 계산합니다.
    position = None
    if mode == "full_reinference":
        position = 0
    else:
        balance = evidence.get("balance_qty")
        if isinstance(balance, int):
            net_required = 0
            for item in required:
                q = item["req_qty"]
                net_required += q if item["side"] == "매수" else -q
            position = balance - net_required
    money = 0.0 if mode == "full_reinference" else _as_float(
        evidence.get("net_invested_before")
    )
    before_sell_odnos = {
        str(e.get("odno"))
        for e in (evidence.get("ledger_before") or {}).get("entries") or []
        if e.get("kind") == "sell"
    }
    if mode == "full_reinference":
        before_sell_odnos = set()
    for item in required:
        if item["req_qty"] <= 0 and item["req_amount"] <= 1e-9:
            continue
        side = item["side"]
        if side == "매도" and item["kind"] == "sell":
            marker = effects_before.get(item["odno"]) or {}
            basis = _as_int(marker.get("basis"))
            if basis <= 0 and position is not None:
                basis = position
            item["basis"] = basis
            item["expected_factor"] = _classify_sell_ratio(
                (item["raw_qty"] / basis) if basis else 0.0
            )
            old_factor = _as_float(marker.get("factor", 1.0))
            if (
                marker
                and item["odno"] not in before_sell_odnos
                and abs(item["expected_factor"] - old_factor) < 1e-9
            ):
                # 레거시 마커만 있고 같은 분류 — 이미 반영된 효과를 다시 곱하지 않으므로
                # 새 원장 항목을 요구하지 않습니다.
                item["expects_entry"] = False
            else:
                item["expects_entry"] = True
        position_after = None
        if position is not None:
            position_after = (
                position + item["req_qty"] if side == "매수"
                else max(0, position - item["req_qty"])
            )
        if side == "매수":
            money += item["req_amount"]
        else:
            factor = item.get("expected_factor")
            if (
                item["kind"] == "sell"
                and factor is not None
                and abs(factor) < 1e-12
                and position_after == 0
            ):
                # 실제 보유 0인 전량매도만 사이클 종료(순투입 0)
                money = 0.0
            else:
                money -= item["req_amount"]
        if position_after is not None:
            position = position_after

    # ── 필수 체결 vs 주장된 적용 내역 (수량·금액 전부) ──
    req_qty_by_odno = {}
    req_amt_by_odno = {}
    for item in required:
        odno = item["odno"]
        req_qty_by_odno[odno] = req_qty_by_odno.get(odno, 0) + item["req_qty"]
        req_amt_by_odno[odno] = req_amt_by_odno.get(odno, 0.0) + item["req_amount"]
    act_qty_by_odno = {}
    act_amt_by_odno = {}
    for order in orders:
        odno = str(order.get("odno"))
        act_qty_by_odno[odno] = act_qty_by_odno.get(odno, 0) + _as_int(order.get("qty_delta"))
        act_amt_by_odno[odno] = act_amt_by_odno.get(odno, 0.0) + _as_float(order.get("amount_delta"))
    for odno in sorted(set(req_qty_by_odno) | set(act_qty_by_odno)):
        req_qty = req_qty_by_odno.get(odno, 0)
        req_amount = req_amt_by_odno.get(odno, 0.0)
        act_qty = act_qty_by_odno.get(odno, 0)
        act_amount = act_amt_by_odno.get(odno, 0.0)
        if act_qty != req_qty or abs(act_amount - req_amount) > 0.02:
            problems.append(
                f"{odno} 필수 체결 불일치: 반드시 처리 {req_qty}주/${req_amount:.2f} "
                f"vs 적용 {act_qty}주/${act_amount:.2f} (누락/부분반영/과다)"
            )

    # ── 기대 T효과가 있는데 원장 항목이 없거나, 기대에 없는 원장 항목 ──
    before_seqs = {str(e.get("seq")) for e in (evidence.get("ledger_before") or {}).get("entries") or []}
    if mode == "full_reinference":
        before_seqs = set()
    after_entries = (evidence.get("ledger_after") or {}).get("entries") or []
    new_entries = [e for e in after_entries if str(e.get("seq")) not in before_seqs]
    for item in required:
        if not item.get("expects_entry"):
            continue
        odno = item["odno"]
        kind = item.get("kind")
        if kind == "sell":
            entry = next(
                (e for e in after_entries
                 if e.get("kind") == "sell" and str(e.get("odno")) == odno),
                None,
            )
            if entry is None:
                problems.append(f"{odno} 필수 체결의 매도 원장 항목이 없습니다 (누락)")
            else:
                expected_factor = item.get("expected_factor")
                if expected_factor is not None and abs(
                    _as_float(entry.get("factor")) - expected_factor
                ) > 1e-9:
                    problems.append(
                        f"{odno} 매도 배율이 필수 체결 기준과 다릅니다: "
                        f"기록 {entry.get('factor')} vs 독립 {expected_factor}"
                    )
        else:
            total = sum(
                _as_float(e.get("delta"))
                for e in new_entries
                if e.get("kind") == kind and str(e.get("odno")) == odno
            )
            if abs(total - item.get("expected_delta", 0.0)) > 0.015:
                problems.append(
                    f"{odno} 필수 T효과 원장 불일치: 기대 {item.get('expected_delta')} "
                    f"vs 원장 {total} (누락/부분반영)"
                )

    expected_entry_keys = {
        (item["kind"], item["odno"])
        for item in required
        if item.get("expects_entry")
    }
    for entry in new_entries:
        kind = entry.get("kind")
        if kind == "adjust":
            continue
        if kind not in ("sell", "buy", "reverse_sell", "reverse_buy"):
            continue
        key = (kind, str(entry.get("odno")))
        if key not in expected_entry_keys:
            problems.append(
                f"원장 항목({entry.get('seq')}, {kind}, {entry.get('odno')})가 "
                f"필수 체결 기대 집합에 없습니다"
            )
    return problems, required, round(max(0.0, money), 2)


def _independent_expected_ordered(evidence):
    """정산 후 원장을 신뢰하지 않고 **정산 전 원장 + 원시 체결 + 이전 마커**로 재구성합니다.

    - 매수/리버스: 이번 실행의 **증가분**만 새 항목으로 계산합니다.
      기존 항목의 delta 변경은 오류입니다.
    - 정상 매도: 보유수량 기준선과 누적 체결 비율로 factor 를 독립 계산합니다.
      기존 sell 항목의 factor 갱신(재분류)만 허용합니다.
    - adjust 항목은 근거가 없으므로 오류입니다.
    - full_reinference 는 T=0·순투입=0 에서 시작합니다(기존 state 금액 무시).

    Returns:
        tuple: (problems, expected_ledger, expected_money)
    """
    problems: list[str] = []
    mode = evidence.get("settlement_mode") or evidence.get("mode")
    if mode not in ("incremental", "full_reinference"):
        problems.append("정산 모드가 없거나 알 수 없습니다 (검증 불가)")
        return problems, None, None
    t_before = evidence.get("T_before")
    if t_before is None:
        problems.append("정산 전 T 기준이 근거에 없습니다 (검증 불가)")
        return problems, None, None
    ledger_before = evidence.get("ledger_before") or {}
    ledger_after = evidence.get("ledger_after")
    if not isinstance(ledger_after, dict):
        problems.append("순서 원장(ledger_after)이 없습니다 (검증 불가)")
        return problems, None, None
    if "start_T" not in ledger_after:
        if ledger_after.get("entries"):
            problems.append("순서 원장(ledger_after)에 start_T 가 없습니다 (검증 불가)")
            return problems, None, None
        # T 효과가 전혀 없는 실행(추가매수만/레거시 동일분류)은 빈 원장이 정상입니다.
        ledger_after = dict(ledger_after)
        ledger_after["start_T"] = 0.0 if mode == "full_reinference" else _as_float(t_before)
        evidence = dict(evidence)
        evidence["ledger_after"] = ledger_after
    required_problems, _required, expected_money = _required_settlement_fills(evidence)
    problems.extend(required_problems)
    before_entries = (
        {}
        if mode == "full_reinference"
        else {str(e.get("seq")): e for e in (ledger_before.get("entries") or [])}
    )
    after_entries = {
        str(e.get("seq")): e for e in (ledger_after.get("entries") or [])
    }
    orders = evidence.get("orders") or []
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}
    pre_meta = evidence.get("orders_meta_before") or {}
    unreg_shares = _unregistered_expected_shares(
        orders, evidence.get("fills") or []
    )
    order_by_seq = {}
    for order in orders:
        seq = order.get("ledger_seq")
        if seq is not None:
            order_by_seq[str(seq)] = order

    # 정산 전 보유수량 → 이번 묶음의 순변동을 되감아 "묶음 시작" 보유량을 구합니다.
    position = None
    balance = evidence.get("balance_qty")
    if mode == "full_reinference":
        position = 0
    elif isinstance(balance, int):
        net_batch = 0
        for order in orders:
            qty = _as_int(order.get("qty_delta"))
            net_batch += qty if order.get("side") == "매수" else -qty
        position = balance - net_batch

    start_t = 0.0 if mode == "full_reinference" else _as_float(
        ledger_before.get("start_T", t_before)
    )
    expected_entries: dict[str, dict] = {
        seq: dict(entry) for seq, entry in before_entries.items()
    }
    reverse_expected_seen = set()
    for seq in sorted(after_entries, key=lambda value: int(value)):
        actual = after_entries[seq]
        kind = actual.get("kind")
        old = before_entries.get(seq)
        order = order_by_seq.get(seq)
        if kind == "adjust":
            if _adjustment_is_explicit(actual):
                # T-only 복구 등 근거가 기록된 명시적 조정은 허용합니다.
                if actual.get("replay_T") is not None and actual.get("target_T") is not None:
                    expected_delta = (
                        _as_float(actual.get("target_T"))
                        - _as_float(actual.get("replay_T"))
                    )
                    if abs(_as_float(actual.get("delta")) - expected_delta) > 0.005:
                        problems.append(
                            f"원장({seq}) 명시적 adjust 의 delta 가 기록과 다릅니다"
                        )
                expected_entries[seq] = dict(actual)
                continue
            problems.append(f"원장({seq}) 근거 없는 adjust 항목")
            expected_entries[seq] = dict(actual)
            continue
        if old is not None and kind != "sell":
            # 기존 매수/리버스 항목은 변경 금지 — 실제 값은 비교 단계에서 검사합니다.
            expected_entries[seq] = dict(old)
            continue
        if order is None:
            # 새 항목인데 주문 근거가 없으면 구조 검사에서 누락으로 잡힙니다.
            expected_entries[seq] = dict(actual)
            continue
        odno = str(order.get("odno"))
        side = order.get("side")
        raw = fills.get(odno) or {}
        meta = pre_meta.get(odno) or {}
        if side == "매도" and not order.get("reverse"):
            cumulative = _as_int(raw.get("qty"))
            marker = (
                {}
                if mode == "full_reinference"
                else (evidence.get("sell_t_effects_before") or {}).get(odno) or {}
            )
            basis = _as_int(marker.get("basis"))
            if basis <= 0:
                basis = position if position is not None else _as_int(order.get("basis"))
            expected_factor = _classify_sell_ratio(
                (cumulative / basis) if basis else 0.0
            )
            entry = dict(expected_entries.get(seq) or actual)
            entry["kind"] = "sell"
            entry["odno"] = odno
            entry["factor"] = expected_factor
            entry["basis"] = basis
            entry["cumulative_qty"] = cumulative
            expected_entries[seq] = entry
            if position is not None:
                position = max(0, position - _as_int(order.get("qty_delta")))
            continue
        if side == "매수" and not order.get("reverse"):
            qty = _as_int(order.get("qty_delta"))
            if order.get("unregistered"):
                # 미등록 매수: 정산의 t_target 을 신뢰하지 않고 건수 폴백으로 배분합니다.
                delta = unreg_shares.get(odno, 0.0)
            else:
                total_qty = _as_int(meta.get("total_qty") or order.get("total_qty"))
                t_target = _as_float(
                    meta.get("t_target", order.get("t_target", 0.0))
                )
                delta = round((qty / total_qty) * t_target, 4) if total_qty > 0 else 0.0
            entry = dict(expected_entries.get(seq) or actual)
            entry.update({"kind": "buy", "odno": odno, "delta": delta})
            expected_entries[seq] = entry
            if position is not None:
                position += qty
            continue
        key = ("reverse", odno)
        if key in reverse_expected_seen:
            # 한 주문의 증가분은 한 항목에만 계산합니다(추가 항목은 0).
            expected_entries[seq] = dict(expected_entries.get(seq) or actual)
            continue
        reverse_expected_seen.add(key)
        total_qty = _as_int(meta.get("total_qty") or order.get("total_qty"))
        raw_qty = _as_int(raw.get("qty"))
        applied = min(1.0, max(0.0, _as_float(meta.get("applied_fill_fraction"))))
        observed = (raw_qty / total_qty) if total_qty else applied
        effective = max(applied, min(1.0, observed))
        fraction_delta = max(0.0, effective - applied)
        if order.get("reverse") and side == "매도":
            base_t = _as_float(meta.get("reverse_base_t", order.get("reverse_base_t")))
            factor = _as_float(meta.get("reverse_t_factor", order.get("reverse_factor", 1.0)))
            delta = round(-base_t * (1.0 - factor) * fraction_delta, 4)
        else:
            target = _as_float(meta.get("reverse_t_target", order.get("reverse_t_target")))
            delta = round(target * fraction_delta, 4)
        entry = dict(expected_entries.get(seq) or actual)
        expected_entries[seq] = {
            "seq": int(seq),
            "kind": "reverse_sell" if side == "매도" else "reverse_buy",
            "odno": odno,
            "delta": delta,
        }
        if position is not None:
            position = (
                max(0, position - _as_int(order.get("qty_delta")))
                if side == "매도"
                else position + _as_int(order.get("qty_delta"))
            )

    expected_ledger = {
        "start_T": round(start_t, 4),
        "entries": [
            expected_entries[seq]
            for seq in sorted(expected_entries, key=lambda value: int(value))
        ],
    }

    # 순투입금 — 필수 체결 전체(추가매수/T효과 0 포함)를 보유 궤적으로 독립 계산합니다.
    return problems, expected_ledger, expected_money


def _expected_ledger_problems(expected_ledger, actual_ledger):
    """재구성한 기대 원장과 실제 원장을 비교합니다 (기존/신규 항목 모두)."""
    problems = []
    expected = {
        str(e.get("seq")): e for e in (expected_ledger or {}).get("entries") or []
    }
    actual = {
        str(e.get("seq")): e for e in (actual_ledger or {}).get("entries") or []
    }
    for seq, exp in expected.items():
        act = actual.get(seq)
        if act is None:
            problems.append(f"원장({seq}) 누락")
            continue
        if exp.get("kind") == "sell":
            if abs(_as_float(exp.get("factor")) - _as_float(act.get("factor"))) > 1e-9:
                problems.append(
                    f"원장({seq}) 매도 배율 불일치: 기록 {act.get('factor')} "
                    f"vs 독립 계산 {exp.get('factor')}"
                )
            if exp.get("basis") is not None and abs(
                _as_int(exp.get("basis")) - _as_int(act.get("basis"))
            ) > 1:
                problems.append(
                    f"원장({seq}) 매도 기준선 불일치: 기록 {act.get('basis')} "
                    f"vs 독립 계산 {exp.get('basis')}"
                )
        else:
            for key in ("delta", "base_t", "factor", "target"):
                if key in exp and abs(
                    _as_float(exp.get(key)) - _as_float(act.get(key))
                ) > 0.01:
                    problems.append(
                        f"원장({seq}) {key} 불일치: 기록 {act.get(key)} "
                        f"vs 독립 계산 {exp.get(key)}"
                    )
    return problems


def _recompute_expected_ordered(evidence):
    """
    순서 원장(evidence_version 2)을 재생해 T/순투입금을 독립 검증합니다.

    evidence 의 t_delta/downstream_factor 를 신뢰하지 않고, 원장 항목과
    원시 누적 체결만으로 순서를 재구성합니다.
    """
    problems: list[str] = []
    if evidence.get("basis_unknown"):
        problems.append("보유수량 기준 미확보로 정산이 보류됐습니다 (검증 불가)")
    problems.extend(_verify_fill_markers(evidence))
    problems.extend(_verify_skipped_unverified(evidence))
    t_before = evidence.get("T_before")
    money_before = evidence.get("net_invested_before")
    if t_before is None or money_before is None:
        return ["정산 전 T/순투입금 기준이 근거에 없습니다 (검증 불가)"], None, None
    t_before = float(t_before)
    money = float(money_before)

    ledger_before = evidence.get("ledger_before")
    ledger_after = evidence.get("ledger_after")
    if not isinstance(ledger_after, dict):
        return ["순서 원장(ledger_after)이 없습니다 (검증 불가)"], None, None
    if "start_T" not in ledger_after:
        if ledger_after.get("entries"):
            return ["순서 원장(ledger_after)에 start_T 가 없습니다 (검증 불가)"], None, None
        ledger_after = dict(ledger_after)
        ledger_after["start_T"] = (
            0.0
            if (evidence.get("settlement_mode") or evidence.get("mode")) == "full_reinference"
            else _as_float(t_before)
        )
        evidence = dict(evidence)
        evidence["ledger_after"] = ledger_after
    if ledger_before is None:
        ledger_before = {}
    if not isinstance(ledger_before, dict):
        return ["순서 원장(ledger_before) 형식 오류 (검증 불가)"], None, None

    # ── 원장 자체 형식/순서 검증 ────────────────────────────────
    for label, ledger in (("before", ledger_before), ("after", ledger_after)):
        entries = ledger.get("entries") or []
        seqs = []
        for entry in entries:
            try:
                seqs.append(int(entry.get("seq")))
            except (TypeError, ValueError):
                problems.append(f"원장({label}) seq 형식 오류")
        if len(seqs) != len(set(seqs)):
            problems.append(f"원장({label}) seq 중복")
        if seqs != sorted(seqs):
            problems.append(f"원장({label}) 적용 순서가 seq 순서와 다릅니다")
        after_seqs = [int(e.get("seq", 0) or 0) for e in entries]
        if after_seqs and after_seqs != list(
            range(after_seqs[0], after_seqs[0] + len(after_seqs))
        ):
            problems.append(f"원장({label}) seq 연속성 오류")

    mode = evidence.get("settlement_mode") or evidence.get("mode")
    if mode not in ("incremental", "full_reinference"):
        problems.append("정산 모드가 없거나 알 수 없습니다 (검증 불가)")
        return problems, None, None
    independent_problems, expected_ledger, expected_money = (
        _independent_expected_ordered(evidence)
    )
    problems.extend(independent_problems)
    if mode == "full_reinference":
        # 전체 재추정은 T=0에서 시작해야 합니다 (과거 원장을 승계하지 않음).
        if abs(float(ledger_after.get("start_T") or 0.0)) > 1e-9:
            problems.append("전체 재추정 원장의 start_T 가 0이 아닙니다")
    else:
        replay_before = _replay_ledger(ledger_before, default_start=t_before)
        if abs(replay_before - t_before) > 0.01:
            problems.append(
                f"정산 전 원장 재생값 {replay_before:.4f} ≠ 기록 T_before {t_before:.4f}"
            )
        if ledger_before and abs(
            float(ledger_after.get("start_T") or 0.0)
            - float(ledger_before.get("start_T") or 0.0)
        ) > 1e-9:
            problems.append("원장 start_T 가 정산 중 변경됐습니다")

    before_by_seq = (
        {}
        if mode == "full_reinference"
        else {str(e.get("seq")): e for e in (ledger_before.get("entries") or [])}
    )
    after_by_seq = {
        str(e.get("seq")): e for e in (ledger_after.get("entries") or [])
    }
    # 기존 원장 항목은 사라지면 안 됩니다 (누락 검출).
    for seq, old in before_by_seq.items():
        if seq not in after_by_seq:
            problems.append(f"원장({seq}) 항목 누락")
    # 매도 재분류 외 기존 항목 변조 금지.
    for seq, old in before_by_seq.items():
        new = after_by_seq.get(seq)
        if not new:
            continue
        if old.get("kind") != new.get("kind"):
            problems.append(f"원장({seq}) kind 변조")
        if old.get("kind") != "sell":
            for key in ("delta", "base_t", "factor", "target", "fill_fraction"):
                if old.get(key) != new.get(key):
                    problems.append(f"매도/매수 원장({seq}) {key} 변조")
    for seq, entry in after_by_seq.items():
        kind = entry.get("kind")
        if kind in ("buy", "reverse_buy") and "factor" in entry:
            problems.append(f"매도/매수 원장({seq}) factor 필드 변조")
        if kind == "sell" and "delta" in entry:
            problems.append(f"매도/매수 원장({seq}) delta 필드 변조")

    orders = evidence.get("orders") or []
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}
    seen_seq: dict[str, dict] = {}
    applied_seq = []
    previous_new_seq = -1
    new_seqs = set(after_by_seq) - set(before_by_seq)
    for order in orders:
        odno = str(order.get("odno"))
        seq = order.get("ledger_seq")
        if seq is None:
            continue
        key = str(seq)
        applied_seq.append(seq)
        if key in seen_seq:
            problems.append(f"주문 {odno} ledger_seq 중복 ({seq})")
            continue
        seen_seq[key] = order
        entry = after_by_seq.get(key)
        if entry is None:
            problems.append(f"주문 {odno} 의 원장 항목({seq}) 누락")
            continue
        expected_kind = "sell" if order.get("side") == "매도" else "buy"
        if order.get("reverse"):
            expected_kind = "reverse_sell" if order.get("side") == "매도" else "reverse_buy"
        if entry.get("kind") != expected_kind:
            problems.append(f"주문 {odno} 원장 kind 불일치")
        if key in new_seqs:
            try:
                seq_int = int(seq)
            except (TypeError, ValueError):
                seq_int = previous_new_seq
            if seq_int < previous_new_seq:
                problems.append(f"주문 {odno} 적용 순서가 원장 순서와 다릅니다")
            previous_new_seq = max(previous_new_seq, seq_int)

        raw = fills.get(odno) or {}
        raw_qty = int(raw.get("qty") or 0)
        raw_amount = float(raw.get("amount") or 0.0)
        qty_delta = int(order.get("qty_delta") or 0)
        amount_delta = float(order.get("amount_delta") or 0.0)
        if qty_delta > raw_qty:
            problems.append(
                f"{odno} 적용 수량 {qty_delta}주 가 누적 체결 {raw_qty}주 를 초과합니다"
            )
        if order.get("side") == "매수" and amount_delta > raw_amount + 0.01:
            problems.append(
                f"{odno} 적용 금액 ${amount_delta:.2f} 가 누적금액 "
                f"${raw_amount:.2f} 를 초과합니다"
            )

        if entry.get("kind") == "sell":
            factor = float(order.get("factor", entry.get("factor", 1.0)) or 0.0)
            if abs(factor - float(entry.get("factor", 1.0) or 0.0)) > 1e-9:
                problems.append(f"{odno} 매도 배율이 원장과 다릅니다")
            basis = int(order.get("basis") or 0)
            cumulative = int(order.get("cumulative_qty") or 0)
            if "basis" in entry and basis and abs(float(entry.get("basis") or 0) - basis) > 1:
                problems.append(f"{odno} 매도 기준선이 원장과 다릅니다")
            if basis > 0 and cumulative > 0:
                expected_factor = _classify_sell_ratio(cumulative / basis)
                if abs(expected_factor - factor) > 1e-6:
                    problems.append(
                        f"{odno} 매도 분류 불일치: 적용 배율 {factor} "
                        f"vs 기준 {basis}주·누적 {cumulative}주 기준 {expected_factor}"
                    )
            # 신규 매도 항목은 seq 직전까지 재생한 T에서 기여분을 독립 계산합니다.
            if str(seq) in new_seqs:
                before_entries = [
                    e for e in (ledger_after.get("entries") or [])
                    if int(e.get("seq", 0) or 0) < int(seq)
                ]
                t_at_entry = _replay_ledger(
                    {"start_T": ledger_after.get("start_T"), "entries": before_entries},
                    default_start=0.0,
                )
                expected_delta = t_at_entry * (factor - 1.0)
                recorded = float(order.get("t_delta") or 0.0)
                if abs(recorded - expected_delta) > 0.01:
                    problems.append(
                        f"{odno} T 델타 불일치: 기록 {recorded:.4f} "
                        f"vs 독립 재계산 {expected_delta:.4f}"
                    )
        elif entry.get("kind") in ("buy", "reverse_buy", "reverse_sell"):
            old_entry = before_by_seq.get(str(seq)) or {}
            expected_delta = float(entry.get("delta", 0.0) or 0.0) - float(
                old_entry.get("delta", 0.0) or 0.0
            )
            recorded = float(order.get("t_delta") or 0.0)
            if abs(recorded - expected_delta) > 0.01:
                problems.append(
                    f"{odno} T 델타 불일치: 기록 {recorded:.4f} "
                    f"vs 독립 재계산 {expected_delta:.4f}"
                )

    if evidence.get("applied_seq") is not None:
        recorded_applied = [
            seq for seq in evidence.get("applied_seq") if seq is not None
        ]
        if recorded_applied != applied_seq:
            problems.append("적용 순서(applied_seq)가 주문 순서와 다릅니다")

    # 새 원장 항목이 주문 근거와 연결되지 않으면 누락입니다.
    for seq in new_seqs:
        entry = after_by_seq[seq]
        if entry.get("kind") in ("sell", "buy", "reverse_sell", "reverse_buy"):
            if seq not in seen_seq:
                problems.append(f"원장 항목({seq}, {entry.get('kind')})에 대응하는 주문 근거 누락")

    if expected_ledger is not None:
        problems.extend(_expected_ledger_problems(expected_ledger, ledger_after))

    expected_t = (
        _replay_ledger(expected_ledger)
        if expected_ledger is not None
        else _replay_ledger(ledger_after)
    )
    recorded_t = evidence.get("T_after")
    if recorded_t is not None and abs(float(recorded_t) - expected_t) > 0.01:
        problems.append(
            f"T 불일치: 정산 결과 {float(recorded_t):.4f} vs 근거 재계산 {expected_t:.4f}"
        )

    # T 델타 합계 = 실제 T 변화량 (개별 t_delta 변조 검출)
    total_delta = sum(float(o.get("t_delta") or 0.0) for o in orders)
    start_for_delta = 0.0 if mode == "full_reinference" else t_before
    if abs(total_delta - (expected_t - start_for_delta)) > 0.01:
        problems.append(
            f"T 델타 불일치: 근거 합계 {total_delta:.4f} vs 실제 변화 "
            f"{expected_t - start_for_delta:.4f}"
        )

    # 순투입금 — 검증기가 정산 후 금액을 신뢰하지 않고 원시 체결/마커로 재계산합니다.
    if expected_money is not None:
        money = expected_money
    recorded_money = evidence.get("net_invested_after")
    if recorded_money is not None and abs(float(recorded_money) - money) > 0.05:
        problems.append(
            f"순투입금 불일치: 정산 결과 ${float(recorded_money):.2f} "
            f"vs 근거 재계산 ${money:.2f}"
        )
    return problems, round(expected_t, 4), money


def _recompute_expected(evidence):
    """
    정산 근거를 **독립적으로 대조**합니다.

    2026-10-22 T 가 13.375 → 3.3438 로 잘못 갱신됐는데 검증은 "이상 없음"을 보고했습니다.
    근거에 기록된 주문별 적용 내역을 원시 체결 데이터와 대조하고,
    (1) 매도 분류(배율)를 기준선에서 **독립 계산**해 적용 배율과 비교하고,
    (2) T 변화·순투입금 산술을 기록된 델타로 **재계산**해 실제 결과와 비교합니다.

    Returns:
        tuple: (문제 목록, 예상 T, 예상 순투입금)
    """
    problems: list[str] = []
    if not evidence:
        return ["정산 근거 파일 없음 — 검증 불가"], None, None
    if evidence.get("basis_unknown"):
        problems.append("보유수량 기준 미확보로 정산이 보류됐습니다 (검증 불가)")

    t_before = evidence.get("T_before")
    money_before = evidence.get("net_invested_before")
    if t_before is None or money_before is None:
        problems.append("정산 전 T/순투입금 기준이 근거에 없습니다 (검증 불가)")
        return problems, None, None

    t_value = float(t_before)
    money = float(money_before)
    orders = evidence.get("orders") or []
    fills = {str(f.get("odno")): f for f in (evidence.get("fills") or [])}
    unreg_shares = _unregistered_expected_shares(
        orders, evidence.get("fills") or []
    )
    problems.extend(_verify_fill_markers(evidence))
    problems.extend(_verify_skipped_unverified(evidence))

    # 묶음 직전 보유량 (독립 계산) — 매도 기준선 대조에 사용
    position = None
    balance = evidence.get("balance_qty")
    if isinstance(balance, int):
        net_batch = 0
        for o in orders:
            q = int(o.get("qty_delta") or 0)
            net_batch += q if o.get("side") == "매수" else -q
        position = balance - net_batch

    # 같은 evidence 안에서 같은 매도 주문이 반복되면(부분체결) 최초 기준을 추적합니다.
    sell_seen: dict[str, tuple[float, float]] = {}
    for o in orders:
        odno = str(o.get("odno"))
        side = o.get("side")
        qty_delta = int(o.get("qty_delta") or 0)
        amount_delta = float(o.get("amount_delta") or 0)
        raw = fills.get(odno) or {}
        raw_qty = int(raw.get("qty") or 0)
        raw_amount = float(raw.get("amount") or 0)

        # ① 적용 델타는 원시 누적 체결을 넘을 수 없습니다
        if qty_delta > raw_qty:
            problems.append(
                f"{odno} 적용 수량 {qty_delta}주 가 누적 체결 {raw_qty}주 를 초과합니다"
            )
        if side == "매수" and amount_delta > raw_amount + 0.01:
            problems.append(
                f"{odno} 적용 금액 ${amount_delta:.2f} 가 누적금액 "
                f"${raw_amount:.2f} 를 초과합니다"
            )

        if side == "매수":
            money += amount_delta
            if position is not None:
                position += qty_delta
            # T 변화 독립 재계산
            if o.get("unregistered"):
                # 미등록 매수: 정산의 t_target/t_delta 를 신뢰하지 않고
                # 건수 폴백 분배를 재구현해 검증합니다.
                expected_share = unreg_shares.get(odno, 0.0)
                t_value += expected_share
                recorded_share = float(o.get("t_delta") or 0.0)
                if abs(recorded_share - expected_share) > 0.01:
                    problems.append(
                        f"{odno} 미등록매수 T 배분 불일치: 기록 {recorded_share:.4f} "
                        f"vs 독립 계산 {expected_share:.4f}"
                    )
            elif o.get("reverse"):
                t_value += float(o.get("reverse_t_target") or 0.0) * float(o.get("fill_fraction_delta") or 0.0)
                if position is not None:
                    position += qty_delta
            else:
                t_target = float(o.get("t_target") or 0.0)
                total_qty = int(o.get("total_qty") or 0)
                if not o.get("is_additional") and total_qty > 0 and t_target > 0:
                    t_value += (qty_delta / total_qty) * t_target
        else:
            if o.get("reverse"):
                base_t = float(o.get("reverse_base_t") or 0.0)
                factor = float(o.get("reverse_factor", 1.0))
                # 첫 적용은 정산 전 T와 identity factor(1.0)에서 독립 재계산합니다.
                # (evidence의 t_before/factor_before를 신뢰하지 않습니다)
                tb = t_value
                fb = 1.0
                base = factor
                downstream = 1.0
                t_value = max(0.0, t_value + tb * (base - fb) * downstream)
                money -= amount_delta
                if position is not None:
                    position = max(0, position - qty_delta)
                continue
            # ② 매도 분류를 기준선에서 독립 계산해 적용 배율과 대조
            basis = int(o.get("basis") or 0)
            cumulative = int(o.get("cumulative_qty") or 0)
            applied = float(o.get("factor", 1.0))
            # 첫 체결이 이번 묶음에 있으면 기준선을 독립 계산해 대조합니다.
            # (이전 실행에서 일부 체결된 주문은 기준선이 이미 확정되어 있으므로 기록값을 씁니다)
            prev_cumulative = int(o.get("prev_cumulative_qty") or 0)
            check_basis = basis
            if prev_cumulative == 0 and position is not None:
                check_basis = int(position)
                if basis and abs(basis - check_basis) > 1:
                    problems.append(
                        f"{odno} 보유수량 기준 불일치: 기록 {basis}주 vs 독립 계산 {check_basis}주"
                    )
            expected_factor = None
            if check_basis > 0 and cumulative > 0:
                expected_factor = _classify_sell_ratio(cumulative / check_basis)
                if abs(expected_factor - applied) > 1e-6:
                    problems.append(
                        f"{odno} 매도 분류 불일치: 적용 배율 {applied} "
                        f"vs 기준 {check_basis}주·누적 {cumulative}주 기준 {expected_factor} "
                        f"— T 분류 오류 가능"
                    )
            # ③ T 변화 독립 재계산 — 근거의 t_before/factor_before로 매도 기여분만 조정
            # (기록된 t_delta를 그대로 합산하면 근거 오류를 그대로 통과시킵니다)
            raw_before = o.get("t_before")
            raw_factor_before = o.get("factor_before")
            try:
                fb = float(raw_factor_before) if raw_factor_before is not None else None
            except (TypeError, ValueError):
                fb = None
            try:
                tb = float(raw_before) if raw_before is not None else None
            except (TypeError, ValueError):
                tb = None
            if prev_cumulative == 0:
                # 첫 적용은 기록된 기준값을 신뢰하지 않고, 순서대로 재생한
                # 실행 전 T와 identity factor(1.0)에서 독립 재계산합니다.
                tb, fb = t_value, 1.0
            elif fb is None or tb is None:
                if odno in sell_seen:
                    tb, _ = sell_seen[odno]
                    fb = sell_seen[odno][1]
                elif prev_cumulative == 0:
                    tb, fb = t_value, applied
                else:
                    problems.append(
                        f"{odno} 매도 근거에 t_before/factor_before가 없어 "
                        f"T를 독립 재계산할 수 없습니다 (검증 불가 항목)"
                    )
                    tb, fb = t_value, applied
            assert tb is not None and fb is not None  # 위 분기에서 모두 확정됨
            t_old = t_value
            if expected_factor == 0.0:
                # 전량매도 — 정산은 T=0·순투입금=0으로 확정합니다
                t_value = 0.0
                money = 0.0
            else:
                base = expected_factor if expected_factor is not None else applied
                downstream = float(o.get("downstream_factor", 1.0) or 0.0)
                t_value = t_value + tb * (base - fb) * downstream
                # 첫 적용은 후속 배율을 1로 확정합니다.
                if prev_cumulative == 0:
                    downstream = 1.0
                money -= amount_delta
            sell_seen[odno] = (tb, applied)
            # 기록 델타와 독립 재계산 값이 다르면 근거 자체가 불일치합니다
            # (전량매도는 T=0 확정이므로 별도 비교하지 않습니다)
            if expected_factor != 0.0:
                recorded = float(o.get("t_delta") or 0.0)
                # 첫 적용은 identity factor에서 독립 재계산하므로 recorded와 비교하지 않습니다.
                if prev_cumulative != 0 or recorded != 0:
                    if abs(recorded - (t_value - t_old)) > 0.01:
                        problems.append(
                            f"{odno} T 델타 불일치: 기록 {recorded:.4f} "
                            f"vs 독립 재계산 {t_value - t_old:.4f}"
                        )
            if position is not None:
                position = max(0, position - qty_delta)
    return problems, round(t_value, 4), round(money, 2)


def _verify_evidence_ordered(evidence):
    """순서 원장 경로 — 재계산이 실제 값 대조까지 수행합니다."""
    problems, _expected_t, _expected_money = _recompute_expected_ordered(evidence)
    return problems


def _verify_evidence(evidence):
    """정산 근거로 예상 T/순투입금을 계산해 실제 값과 대조합니다."""
    orders = (evidence or {}).get("orders") or []
    ledger_after = (evidence or {}).get("ledger_after") or {}
    has_ledger_entries = bool(
        isinstance(ledger_after, dict) and ledger_after.get("entries")
    )
    if evidence and (
        evidence.get("evidence_version") == 2
        or has_ledger_entries
        or any(o.get("ledger_seq") is not None for o in orders)
    ):
        # 버전 없는/알 수 없는 근거를 레거시 경로로 조용히 통과시키지 않습니다.
        if evidence.get("evidence_version") != 2:
            return ["정산 근거 버전(evidence_version)이 2가 아닙니다 (검증 불가)"]
        return _verify_evidence_ordered(evidence)
    problems, expected_t, expected_money = _recompute_expected(evidence)
    if expected_t is None:
        return problems
    actual_t = evidence.get("T_after")
    actual_money = evidence.get("net_invested_after")
    if actual_t is not None and abs(float(actual_t) - expected_t) > 0.01:
        problems.append(
            f"T 불일치: 정산 결과 {float(actual_t):.4f} vs 근거 재계산 {expected_t:.4f}"
        )
    if actual_money is not None and abs(float(actual_money) - expected_money) > 0.05:
        problems.append(
            f"순투입금 불일치: 정산 결과 ${float(actual_money):.2f} "
            f"vs 근거 재계산 ${expected_money:.2f}"
        )
    return problems


def _symbol_executed_count(text, symbol):
    """로그의 **종목별** 실행 결과 라인에서 성공 건수를 읽습니다.

    실제 출력 형식 (trading_bot.py `[실행 결과]`):
        ``  {symbol}: 성공 N건 / 예약 N건 / 실패 N건``
    전체 합계(`합계: ...`)나 다른 종목 라인을 부분 매칭하지 않도록
    줄 시작 + 정확한 심볼 + 콜론으로 고정합니다.

    Returns:
        tuple: (라인 발견 여부, 성공 건수)
    """
    line = re.compile(
        rf"^[ \t]*{re.escape(symbol)}:[ \t]*성공[ \t]+(\d+)건"
        rf"[ \t]*/[ \t]*예약[ \t]+\d+건"
        rf"[ \t]*/[ \t]*실패[ \t]+\d+건[ \t]*$",
        re.MULTILINE,
    )
    match = line.search(text or "")
    if not match:
        return False, 0
    return True, int(match.group(1))


def _verify_t_transitions(text, symbol, rows, balance_qty):
    """로그의 T 전이를 실제 정산 출력 형식 그대로 해석해 검증합니다.

    지원하는 실제 로그 형식 (state.py / trading_bot.py):
      - 일반매수: ``→ 매수 체결 (dt): odno=.. 부분체결(a/b) → ΔT=x → T=y``
      - 미등록매수: ``→ 매수 체결 (dt): 미등록매수 N건 → T += x → T=y``
      - 추가매수: ``→ 매수 체결 (dt): 추가매수(odno=..) 제외 → T 변경 없음``
      - 매도: ``→ 매도 체결 (dt): 쿼터매도 (비율=0.25, 기준 100주, 누적 25주, 보유 75주) → T=y``
        (기존 단순 형식 ``(비율=0.25)`` / ``(비율=0.25, 보유 75주)`` 도 지원)
      - 지연매수: ``→ 지연 체결 반영: .. → ΔT=x → T=y``
      - 지연매도: ``→ 지연 체결 반영: .. 매도 N주 추가 (누적 M주) → T=y``
      - 리버스: ``→ 리버스 매도/매수 체결 반영: odno=.., (a/b)`` — T 가 로그에 없으므로
        전이 재생에서는 제외하고 근거 파일(evidence) 검증에 맡깁니다.

    해석할 수 없는 체결 형식은 정상으로 넘기지 않고 "로그 검증 불가"로 표시합니다.
    매도 로그 자체가 기준·누적·보유를 담으므로, 워터마크/잔고로 비율을 다시 계산하는
    중복 검산은 하지 않습니다.
    """
    problems: list[str] = []

    # 종목별 섹션만 봅니다 (전이 로그에는 종목명이 없어 섹션으로 구분합니다).
    section = text
    marker = f"종목 처리 시작: {symbol} "
    section_start = text.find(marker)
    if section_start != -1:
        section = text[section_start:]
        nxt = section.find("종목 처리 시작: ", len(marker))
        if nxt != -1:
            section = section[:nxt]

    m_before = re.search(
        rf"\[상태\] {re.escape(symbol)} 상태 로드 완료 → T=([0-9.]+), 마지막 갱신: (\S+)",
        section,
    )
    m_after = re.search(
        rf"\[상태\] {re.escape(symbol)} T값 업데이트 완료 → T=([0-9.]+)",
        section,
    )
    if not m_before or not m_after:
        # 정산이 수행되지 않은 실행(휴장/조기 종료)은 비교할 전이가 없습니다.
        return problems
    t_before = float(m_before.group(1))
    t_after = float(m_after.group(1))

    number = r"-?[0-9]+(?:\.[0-9]+)?"
    buy_delta_re = re.compile(
        rf"→ 매수 체결 \([^)]*\):.*?→ ΔT=({number}) → T=({number})"
    )
    buy_count_re = re.compile(
        rf"→ 매수 체결 \([^)]*\):.*?→ T \+= ({number}) → T=({number})"
    )
    sell_re = re.compile(
        rf"→ 매도 체결 \([^)]*\):\s*(\S+)\s+\(([^)]*)\)\s*→\s*T=({number})"
    )
    late_buy_re = re.compile(
        rf"→ 지연 체결 반영:.*?→ ΔT=({number}) → T=({number})"
    )
    late_sell_re = re.compile(
        rf"→ 지연 체결 반영:.*?매도\s+[0-9]+주 추가.*?→\s*T=({number})"
    )
    reverse_re = re.compile(r"→ 리버스 (?:매도|매수) 체결 반영:")
    reverse_format_re = re.compile(
        rf"→ 리버스 (?:매도|매수) 체결 반영:\s*odno=\S+,\s*\({number}/{number}\)"
    )
    no_change_re = re.compile(r"T 변경 없음")

    saw_reverse = bool(reverse_re.search(section))
    saw_t_transition = False
    t_running = t_before

    for line in section.splitlines():
        if reverse_re.search(line):
            if not reverse_format_re.search(line):
                problems.append(
                    f"{symbol} 로그 검증 불가: 해석할 수 없는 리버스 체결 형식 — {line.strip()}"
                )
            # 리버스 T 변화는 로그에 T 값이 없습니다 — 근거 파일에서 검증합니다.
            continue

        if "→ 매도 체결" in line:
            m_sell = sell_re.search(line)
            if not m_sell:
                problems.append(
                    f"{symbol} 로그 검증 불가: 해석할 수 없는 매도 체결 형식 — {line.strip()}"
                )
                continue
            details = m_sell.group(2)
            has_ratio = "비율=" in details
            if not has_ratio and "보유수량 불명" not in details:
                problems.append(
                    f"{symbol} 로그 검증 불가: 매도 기준/비율 정보가 없습니다 — {line.strip()}"
                )
                continue
            t_running = float(m_sell.group(3))
            saw_t_transition = True

            ratio_m = re.search(r"비율=([0-9.]+)", details)
            basis_m = re.search(r"기준\s+([0-9]+)주", details)
            cumulative_m = re.search(r"누적\s+([0-9]+)주", details)
            if ratio_m and basis_m and cumulative_m:
                basis = int(basis_m.group(1))
                cumulative = int(cumulative_m.group(1))
                if basis > 0:
                    expected_ratio = cumulative / basis
                    if abs(float(ratio_m.group(1)) - expected_ratio) > 0.011:
                        problems.append(
                            f"{symbol} 매도 비율 불일치: 로그 비율={ratio_m.group(1)} "
                            f"vs 누적 {cumulative}주/기준 {basis}주 = {expected_ratio:.2f}"
                        )
            continue

        if "→ 매수 체결" in line:
            m_buy = buy_delta_re.search(line) or buy_count_re.search(line)
            if m_buy:
                delta_t = float(m_buy.group(1))
                logged_t = float(m_buy.group(2))
                # 리버스 체결이 앞서 T를 바꿨지만 로그에는 T 가 없으므로
                # 그 경우에만 ΔT 가산 일치 검사를 생략합니다.
                if not saw_reverse and abs(t_running + delta_t - logged_t) > 0.01:
                    problems.append(
                        f"{symbol} 로그 전이 T 불일치: 이전 T={t_running} + ΔT={delta_t} "
                        f"≠ 기록 T={logged_t}"
                    )
                t_running = logged_t
                saw_t_transition = True
                continue
            if no_change_re.search(line) and "추가매수" in line:
                continue
            problems.append(
                f"{symbol} 로그 검증 불가: 해석할 수 없는 매수 체결 형식 — {line.strip()}"
            )
            continue

        if "→ 지연 체결 반영" in line:
            m_late_buy = late_buy_re.search(line)
            if m_late_buy:
                delta_t = float(m_late_buy.group(1))
                logged_t = float(m_late_buy.group(2))
                if not saw_reverse and abs(t_running + delta_t - logged_t) > 0.01:
                    problems.append(
                        f"{symbol} 로그 전이 T 불일치(지연): 이전 T={t_running} "
                        f"+ ΔT={delta_t} ≠ 기록 T={logged_t}"
                    )
                t_running = logged_t
                saw_t_transition = True
                continue
            m_late_sell = late_sell_re.search(line)
            if m_late_sell:
                t_running = float(m_late_sell.group(1))
                saw_t_transition = True
                continue
            if no_change_re.search(line) and "추가매수" in line:
                continue
            problems.append(
                f"{symbol} 로그 검증 불가: 해석할 수 없는 지연 체결 형식 — {line.strip()}"
            )
            continue

    if saw_t_transition:
        if abs(t_running - t_after) > 0.0001:
            problems.append(
                f"{symbol} 로그 내 T 전이({t_running}) ≠ 최종 T({t_after})"
            )
    elif not saw_reverse and abs(t_before - t_after) > 0.0001:
        problems.append(
            f"{symbol} 로그에 T 변경 전이가 없습니다 (T={t_before} → {t_after}) — 검증 불가"
        )
    return problems


def verify_evidence_file(
    evidence_path,
    broker,
    mode,
    run_id=None,
    *,
    retry_num=None,
    code_version=None,
    broker_mode=None,
    final_state=None,
    max_t=None,
):
    """정산 근거 파일 1개를 **계좌 API 호출 없이** 검증합니다.

    브로커/모드/run_id/retry_num/code_version/broker_mode 메타가 기대값과
    다르면 검증 불가로 실패합니다.
    `settlement_mode` 별 분기:
      - full_reinference: 원장 start_T == 0 강제
      - incremental: ledger_before 재생값 == T_before 대조

    `final_state` 가 주어지면 **최종 저장 상태**(MAX_T 보정·사이클 후처리 이후)와
    evidence 의 T_after/net_invested_after 를 대조합니다.

    Returns:
        tuple: (ok: bool, problems: list[str])
    """
    path = Path(evidence_path)
    problems: list[str] = []
    if not path.exists():
        return False, [f"정산 근거 파일 없음: {path}"]
    try:
        evidence = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return False, [f"정산 근거 파일 파싱 실패: {error}"]

    if not isinstance(evidence, dict):
        return False, ["정산 근거 형식 오류 (dict 아님)"]

    ev_broker = str(evidence.get("broker") or "")
    if not ev_broker:
        problems.append("정산 근거에 broker 필드가 없습니다")
    elif broker and ev_broker.lower() != str(broker).lower():
        problems.append(f"broker 불일치: 근거={ev_broker}, 기대={broker}")

    ev_mode = str(evidence.get("mode") or "")
    if not mode:
        # 실행 job 이 모드를 확정하지 못한 경우(조기 실패) 통과시키지 않습니다.
        problems.append("실행 모드가 전달되지 않았습니다 (검증 불가)")
    if not ev_mode:
        problems.append("정산 근거에 mode 필드가 없습니다")
    elif mode and ev_mode.upper() != str(mode).upper():
        problems.append(f"mode 불일치: 근거={ev_mode}, 기대={mode}")

    if run_id is not None and str(evidence.get("run_id") or "") != str(run_id):
        problems.append(
            f"run_id 불일치: 근거={evidence.get('run_id') or '(없음)'}, 기대={run_id}"
        )
    if retry_num is not None and str(evidence.get("retry_num") or "") != str(retry_num):
        problems.append(
            f"retry_num 불일치: 근거={evidence.get('retry_num') or '(없음)'}, "
            f"기대={retry_num}"
        )
    if code_version is not None:
        ev_version = str(evidence.get("code_version") or "")
        if not ev_version:
            problems.append("정산 근거에 code_version 필드가 없습니다")
        elif ev_version != str(code_version):
            problems.append(
                f"code_version 불일치: 근거={ev_version}, 기대={code_version}"
            )
    if broker_mode is not None:
        ev_broker_mode = str(evidence.get("broker_mode") or "")
        if not ev_broker_mode:
            problems.append("정산 근거에 broker_mode 필드가 없습니다")
        elif ev_broker_mode.lower() != str(broker_mode).lower():
            problems.append(
                f"broker_mode 불일치: 근거={ev_broker_mode}, 기대={broker_mode}"
            )

    if final_state is not None:
        expected_T = _as_float(evidence.get("T_after"), 0.0)
        limit = max_t if max_t is not None else evidence.get("max_t")
        if limit is not None:
            try:
                expected_T = min(expected_T, float(limit))
            except (TypeError, ValueError):
                problems.append("evidence max_t 값이 숫자가 아닙니다")
        final_T = _as_float(final_state.get("T"), 0.0)
        if abs(final_T - expected_T) > 1e-6:
            problems.append(
                f"최종 저장 T 불일치: evidence 기대={expected_T:.4f}, "
                f"state={final_T:.4f} (MAX_T/후처리 반영)"
            )
        final_net = _as_float(final_state.get("net_invested"), 0.0)
        expected_net = _as_float(evidence.get("net_invested_after"), 0.0)
        if abs(final_net - expected_net) > 0.01:
            problems.append(
                f"최종 저장 순투입금 불일치: evidence={expected_net:.2f}, "
                f"state={final_net:.2f}"
            )

    settlement_mode = evidence.get("settlement_mode")
    if settlement_mode not in ("incremental", "full_reinference"):
        problems.append("settlement_mode 가 없거나 알 수 없습니다 (검증 불가)")
    else:
        ledger_after = evidence.get("ledger_after")
        if not isinstance(ledger_after, dict):
            problems.append("ledger_after 가 없습니다 (검증 불가)")
        elif settlement_mode == "full_reinference":
            if abs(_as_float(ledger_after.get("start_T"), 0.0)) > 1e-9:
                problems.append("전체 재추정 원장의 start_T 가 0이 아닙니다")
        else:
            ledger_before = evidence.get("ledger_before")
            if isinstance(ledger_before, dict):
                replay = _replay_ledger(
                    ledger_before, default_start=_as_float(evidence.get("T_before"), 0.0)
                )
                if abs(replay - _as_float(evidence.get("T_before"), 0.0)) > 0.01:
                    problems.append(
                        f"정산 전 원장 재생값 {replay:.4f} ≠ T_before "
                        f"{_as_float(evidence.get('T_before'), 0.0):.4f}"
                    )

    problems.extend(_verify_evidence(evidence))
    # 중복 메시지는 제거합니다.
    deduped = list(dict.fromkeys(_mask_problem_messages(problems, evidence)))
    return not deduped, deduped


def write_final_snapshot(
    path,
    state_file,
    *,
    broker,
    broker_mode,
    trade_mode,
    run_id,
    retry_num="0",
    code_version="",
    symbols=None,
):
    """최종 저장 상태 스냅샷을 기록합니다 (검증기가 evidence 와 대조).

    `.state.json` 은 사이클 후처리/MAX_T 보정이 끝난 **최종 저장 상태**입니다.
    DRY 처럼 저장하지 않는 모드에서는 파일이 이전 상태일 수 있으므로,
    스냅샷에 trade_mode 를 남겨 검증기가 최종값 대조 여부를 판단합니다.
    """
    raw_states = {}
    state_path = Path(state_file)
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text())
            if isinstance(loaded, dict):
                raw_states = loaded
        except (OSError, json.JSONDecodeError):
            raw_states = {}

    states = {}
    for symbol, data in raw_states.items():
        if not isinstance(data, dict):
            continue
        states[str(symbol).upper()] = {
            "T": _as_float(data.get("T"), 0.0),
            "net_invested": _as_float(data.get("net_invested"), 0.0),
            "last_updated": data.get("last_updated", ""),
            "cycle_start_date": data.get("cycle_start_date", ""),
            "position_qty": data.get("position_qty"),
        }

    if symbols is None:
        expected_symbols = sorted(states)
    else:
        expected_symbols = [
            str(s).split(":")[0].strip().upper()
            for s in symbols
            if str(s).strip()
        ]

    payload = {
        "snapshot_version": 1,
        "broker": str(broker or ""),
        "broker_mode": str(broker_mode or ""),
        "trade_mode": str(trade_mode or ""),
        "run_id": str(run_id or ""),
        "retry_num": str(retry_num or ""),
        "code_version": str(code_version or ""),
        "symbols": expected_symbols,
        "states": states,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def verify_evidence_dir(
    evidence_dir,
    broker,
    mode,
    run_id=None,
    *,
    retry_num=None,
    code_version=None,
    broker_mode=None,
    snapshot_path=None,
):
    """evidence 디렉터리 전체를 검증합니다 (종목 누락·스냅샷 메타 포함).

    Returns:
        tuple: (ok: bool, problems: list[str])
    """
    problems: list[str] = []
    files = sorted(Path(evidence_dir).glob("settlement-*.json"))
    if not files:
        return False, ["정산 근거 파일이 없습니다 — 검증 불가"]

    snapshot = None
    if snapshot_path is None:
        candidates = sorted(Path(evidence_dir).glob("final-settlement-*.json"))
        snapshot_path = candidates[-1] if candidates else None
    if snapshot_path is None:
        problems.append("최종 저장 스냅샷이 없습니다 — 검증 불가")
    else:
        try:
            snapshot = json.loads(Path(snapshot_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"최종 저장 스냅샷 파싱 실패: {error}")
            snapshot = None

    if snapshot is not None:
        for key, expected in (
            ("broker", broker),
            ("trade_mode", mode),
            ("run_id", run_id),
            ("retry_num", retry_num),
            ("code_version", code_version),
            ("broker_mode", broker_mode),
        ):
            if expected is None:
                continue
            got = str(snapshot.get(key) or "")
            if got.lower() != str(expected).lower():
                problems.append(
                    f"스냅샷 {key} 불일치: 스냅샷={got or '(없음)'}, 기대={expected}"
                )

    evidence_symbols = set()
    # 재시도로 같은 종목 evidence 가 여러 개면 **가장 최근 파일**만 최종값과 대조합니다.
    # (이전 회차 파일은 그 시점의 중간 상태라 최종 스냅샷과 다를 수 있음)
    evidence_by_symbol = {}
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{path.name}: 파싱 실패 — {error}")
            continue
        symbol = str(data.get("symbol") or "").upper()
        if symbol:
            evidence_symbols.add(symbol)
            evidence_by_symbol[symbol] = (path, data)

    if snapshot is not None:
        expected_symbols = {
            str(s).upper() for s in (snapshot.get("symbols") or [])
        }
        missing = sorted(expected_symbols - evidence_symbols)
        extra = sorted(evidence_symbols - expected_symbols)
        if missing:
            problems.append(f"종목별 evidence 누락: {', '.join(missing)}")
        if extra:
            problems.append(f"스냅샷에 없는 종목 evidence: {', '.join(extra)}")

    live_snapshot = snapshot is not None and str(
        snapshot.get("trade_mode") or ""
    ).upper() == "LIVE"
    for symbol, (path, _data) in evidence_by_symbol.items():
        final_state = None
        if live_snapshot and snapshot is not None:
            final_state = (snapshot.get("states") or {}).get(symbol)
            if final_state is None:
                problems.append(f"스냅샷에 {symbol} 최종 상태가 없습니다")
        ok_file, file_problems = verify_evidence_file(
            path,
            broker,
            mode,
            run_id=run_id,
            retry_num=retry_num,
            code_version=code_version,
            broker_mode=broker_mode,
            final_state=final_state,
        )
        if not ok_file:
            problems.extend(f"{path.name}: {p}" for p in file_problems)

    deduped = list(dict.fromkeys(problems))
    return not deduped, deduped


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    expect_live = "--expect-live" in sys.argv
    target = args[0] if args else datetime.now(KST).strftime("%Y%m%d")
    logs = sorted(Path("logs").glob(f"run-{target}-*.log"))
    if not logs:
        print(f"logs/run-{target}-*.log 없음 — 실행되지 않았습니다.")
        sys.exit(2)
    # 검사 대상은 **해당 날짜의 가장 최근 LIVE 로그**입니다.
    # (나중에 DRY 가 추가로 돌더라도 LIVE 결과를 계속 검사할 수 있습니다)
    live_logs = [p for p in logs if "mode=LIVE" in p.read_text(errors="ignore")]
    log_path = live_logs[-1] if live_logs else logs[-1]

    print(f"=== 실행 로그 검증 ({target}) ===")
    text = log_path.read_text()
    ok(f"로그: logs/{log_path.name}")
    mode = "DRY" if "mode=DRY" in text else "LIVE"
    if expect_live and not live_logs:
        # 오늘 LIVE 실행 로그가 전혀 없으면 예약 실행이 실패한 것입니다.
        problem(
            f"예약 LIVE 실행 로그가 없습니다 (최근 로그: {log_path.name}, mode={mode})"
        )
    exit_code = None
    m_run = re.search(r"\[launcher\] start .*", text)
    run_id = ""
    if m_run:
        run_id = Path(log_path).stem
    m = re.search(r"\[launcher\] exit=(\d+)", text)
    if m:
        exit_code = int(m.group(1))
        (ok if exit_code == 0 else problem)(f"런처 exit={exit_code}")
    else:
        problem("런처 종료 코드 기록이 없습니다 (비정상 종료 가능)")

    total = re.search(r"합계: 성공 (\d+)건 / 예약 (\d+)건 / 실패 (\d+)건", text)
    executed = reserved = failed = 0
    if total:
        executed, reserved, failed = (int(g) for g in total.groups())
        (ok if failed == 0 else problem)(
            f"실행 결과: 성공 {executed}건 / 예약 {reserved}건 / 실패 {failed}건"
        )
    else:
        note("실행 결과 집계 없음 (휴장일 조기 종료 등)")

    if mode == "LIVE" and exit_code == 0 and executed == 0 and reserved == 0:
        if "신규 주문을 생성하지 않습니다" in text:
            note("주문 0건 — 리버스 정산 대기(정상)")
        else:
            problem("LIVE 인데 접수된 주문이 0건 — 원인 확인 필요")

    print(f"\n=== 브로커·state 검증 ===")
    if mode == "DRY":
        note("DRY 실행 — 주문 접수가 없으므로 브로커 주문 검증은 LIVE 실행 대상입니다")
    broker = MeritzBroker()
    for sym, exch in (("TQQQ", "NAS"), ("SOXL", "AMS")):
        print(f"\n[{sym}]")
        st = load_state(sym)
        bal = broker.get_balance(sym, exch)
        rows = broker.get_order_history(sym, exch, days=5)
        live = [
            r for r in rows
            if str(r.get("ord_dt", "")).startswith(target)
            and r.get("prcs_stat_name") == "미체결"
        ]
        live.sort(key=lambda r: r.get("_order_price") or 0)
        # 브로커 주문번호는 공개 로그(Actions 콘솔)에 원문으로 남기지 않습니다.
        # 같은 실행의 Evidence(암호화 artifact)에 log_salt 가 있으므로 가명 식별자로 대조할 수 있습니다.
        evidence = _load_settlement_evidence(sym, run_id)
        odno_mask = _evidence_salt_hex(evidence) or os.urandom(16).hex()
        for r in live:
            print(
                f"    {_independent_odno_hash(r.get('odno'), odno_mask)} "
                f"{r.get('sll_buy_dvsn_cd_name')} "
                f"{r.get('ft_ord_qty'):>3s}주 @{r.get('_order_price')}"
            )
        filled = [
            r for r in rows
            if str(r.get("ord_dt", "")).startswith(target)
            and int(float(r.get("ft_ccld_qty", "0") or 0)) > 0
        ]
        for r in filled:
            print(
                f"    체결: {_independent_odno_hash(r.get('odno'), odno_mask)} "
                f"{r.get('sll_buy_dvsn_cd_name')} "
                f"{r.get('ft_ccld_qty')}주 @{r.get('ft_ccld_unpr3')}"
            )

        buys = [r.get("_order_price") for r in live if r.get("sll_buy_dvsn_cd_name") == "매수"]
        sells = sum(int(float(r.get("ft_ord_qty", 0) or 0))
                    for r in live if r.get("sll_buy_dvsn_cd_name") == "매도")
        if len(buys) != len(set(buys)):
            problem(f"{sym} 매수 가격 중복: {sorted(buys)}")
        else:
            ok(f"{sym} 매수 가격 중복 없음 ({len(buys)}건)")
        if sells > bal.quantity:
            problem(f"{sym} 매도 합계 {sells}주 > 보유 {bal.quantity}주")
        else:
            ok(f"{sym} 매도 합계 {sells}주 ≤ 보유 {bal.quantity}주")
        for msg in _verify_t_transitions(text, sym, rows, bal.quantity):
            problem(msg)
        # "접수된 주문이 이력에 없습니다"는 **그 종목의** 성공 건수로 판정합니다.
        # 전체 합계를 쓰면 다른 종목이 성공했을 때 이 종목 누락을 놓칩니다.
        sym_line_found, sym_executed = _symbol_executed_count(text, sym)
        if mode == "LIVE" and not sym_line_found:
            note(
                f"{sym} 종목별 실행 결과 라인이 없습니다 — "
                f"접수 건수는 0 으로 보고 검사합니다"
            )
        # evidence 는 위(브로커 주문 출력)에서 run_id 로 이미 읽었습니다 — 같은 파일을 재사용합니다.
        if evidence is None:
            problem(
                f"{sym} 정산 근거 파일 없음 (run_id={run_id or '없음'}) — T 검산 불가"
            )
        else:
            for msg in _verify_evidence(evidence):
                problem(msg)
        if mode == "LIVE" and sym_executed and len(live) + len(filled) == 0:
            problem(f"{sym} 접수된 주문이 이력에 없습니다")
        print(
            f"    state: T={st.get('T')} net_invested=${st.get('net_invested')} "
            f"({st.get('net_invested_status')}) 보유={bal.quantity}주 "
            f"fence={'있음' if (st.get('pending_order_intent') or st.get('pending_order_batch')) else '없음'}"
        )
        if st.get("pending_order_intent") or st.get("pending_order_batch"):
            problem(f"{sym} 주문 fence가 남아 있습니다")
        if st.get("balance_mismatch"):
            problem(f"{sym} balance_mismatch: {st.get('balance_mismatch')}")
        if st.get("net_invested_status") != "valid":
            problem(f"{sym} net_invested_status={st.get('net_invested_status')}")

    print("\n" + "=" * 66)
    if problems:
        print(f"판정: 이상 {len(problems)}건 — 확인 필요")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("판정: 이상 없음")
    for n in notes:
        print(f"  - {n}")
    sys.exit(0)


if __name__ == "__main__":
    main()
