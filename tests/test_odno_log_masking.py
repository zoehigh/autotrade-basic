"""주문번호 마스킹 회귀 테스트 — 정적 가드 + 마스킹 함수 성질 + 단일 salt 불변식.

범위: 공개 GitHub Actions 로그·알림·예외 메시지에 원문 주문번호가 남지 않아야 하는 **모든 경로**.
  - 정적 가드: 브로커 어댑터 6종 + src/broker/base.py + src/state.py + trading_bot.py
    + ops/verify_run.py 의 print/notify/예외 f-string
  - 접수 알림의 원문(`telegram_message`)은 허용된 줄 패턴에서만, 마스킹된 twin 과 함께
  - salt 는 프로세스당 하나 (`log_mask` 단일 모듈 객체)

동작(behavior) 테스트는 별도 모듈에 있습니다:
  - `tests/test_odno_masking_adapters.py` — 어댑터별 이력/주문/취소 로그
  - `tests/test_odno_masking_channels.py` — 개인/공용 알림 채널, 텔레그램 실패·조용한 시간
  - `tests/test_odno_masking_ledger.py` — 주문번호 매핑 원장 + Evidence 암호화 왕복
  - `tests/test_odno_masking_no_behavior_change.py` — 마스킹이 주문/정산/state 저장을 바꾸지 않음

Evidence(비공개 암호화 artifact)는 독립 검증을 위해 원문 주문번호와 salt 를 유지합니다.
남은 예외는 `_EXEMPTIONS` 에 **사유와 함께** 기록합니다 (현재 0건).
"""
import ast
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import trading_bot  # noqa: E402
from state import log_salt_hex, mask_odno  # noqa: E402

RAW_A, RAW_B, RAW_C = "0000039445", "0000039446", "0000039447"


# ── 마스킹 함수 성질 ─────────────────────────────────────────────────
def test_mask_is_stable_within_run_and_hides_raw():
    token = mask_odno(RAW_A)
    assert token == mask_odno(RAW_A)
    assert token.startswith("ord#") and RAW_A not in token
    assert mask_odno([RAW_A, RAW_B]) == f"[{mask_odno(RAW_A)}, {mask_odno(RAW_B)}]"


def test_mask_differs_between_runs():
    """실행(프로세스)마다 salt 가 달라 다른 실행의 로그와 연결·사전 대입이 불가능합니다."""
    code = "import sys; sys.path.insert(0,'src'); from state import mask_odno, log_salt_hex; print(mask_odno('39445'), log_salt_hex())"
    outs = [
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.split()
        for _ in range(2)
    ]
    assert outs[0][0] != outs[1][0]
    assert outs[0][1] != outs[1][1]


def test_salt_is_not_in_mask_output():
    assert log_salt_hex() not in mask_odno(RAW_A)


# ── 보류 경로: stdout / 알림 / 예외 ─────────────────────────────────
def _capture_save_settlement(monkeypatch, state):
    notified = []
    monkeypatch.setattr(trading_bot, "notify", lambda message, urgent=False: notified.append(message))
    return notified


def test_pending_amount_and_marker_hold_masks_everywhere(monkeypatch, capsys):
    notified = _capture_save_settlement(monkeypatch, None)
    state = {
        "_pending_amount_odnos": [RAW_A, RAW_B],
        "_pending_marker_odnos": [RAW_B],
    }
    with pytest.raises(trading_bot.SymbolScopedError) as exc:
        trading_bot._save_settlement_state("TQQQ", state)
    out = capsys.readouterr().out
    for text in (out, str(exc.value), "\n".join(notified)):
        assert RAW_A not in text and RAW_B not in text, text
        assert "ord#" in text


def test_skipped_unverified_log_is_masked(monkeypatch, capsys):
    _capture_save_settlement(monkeypatch, None)
    state = {
        "_unverified_skipped_odnos": [RAW_C],
        "_unverified_skipped_audit": [{"odno": RAW_C, "odno_hash": mask_odno(RAW_C)}],
    }
    assert trading_bot._save_settlement_state("TQQQ", state) is True
    out = capsys.readouterr().out
    assert RAW_C not in out and mask_odno(RAW_C) in out


def test_evidence_keeps_raw_odno_and_salt_for_independent_verification():
    """공개 로그는 마스킹하지만, 비공개 Evidence 는 원문 + salt 를 유지해야 대조할 수 있습니다."""
    state = {
        "T": 1.0, "net_invested": 0.0, "last_updated": "", "orders_meta": {},
        "_unverified_skipped_audit": [
            {"odno": RAW_A, "odno_hash": mask_odno(RAW_A), "filled_qty": 1, "ts": "", "reason": "x"}
        ],
        "_pending_marker_odnos": [RAW_B],
    }
    ev = trading_bot.build_settlement_evidence("TQQQ", deepcopy(state), [], mode="DRY", run_id="r", balance_qty=0)
    assert ev["skipped_unverified"][0]["odno"] == RAW_A
    assert ev["pending_marker_odnos"] == [RAW_B]
    assert ev["log_salt"] == log_salt_hex()
    assert mask_odno(RAW_A, ev["log_salt"]) == ev["skipped_unverified"][0]["odno_hash"]


# ── 정적 가드: 새 출력이 원문 주문번호를 찍지 않도록 ───────────────────
# 스캔 범위(공개 Actions 로그에 남는 경로 전부):
#   브로커 어댑터 6종 + src/broker/base.py(DRY 취소 로그) + src/state.py
#   + trading_bot.py + ops/verify_run.py(CLI 출력)
_SCANNED_FILES = [
    "src/broker/base.py",
    "src/broker/kis/adapter.py",
    "src/broker/kiwoom/adapter.py",
    "src/broker/ls/adapter.py",
    "src/broker/toss/adapter.py",
    "src/broker/nhplug/adapter.py",
    "src/broker/meritz/adapter.py",
    "src/state.py",
    "trading_bot.py",
    "ops/verify_run.py",
]

# 공개 출력 sink
_SINKS = {"print", "notify"}
# 예외 메시지(sink) — 공개 로그·Actions 오류 메시지에 그대로 찍힙니다
_EXCEPTION_SINKS = {
    "RuntimeError", "SymbolScopedError", "GlobalScopedError", "ValueError",
    "TypeError", "KeyError", "Exception", "BrokerError", "OrderError",
    "OrderNotAcceptedError", "AuthError",
}

# 마스킹된 값으로 인정하는 래퍼 함수 (이 안의 식은 원문이 아닙니다)
_MASK_WRAPPERS = {
    "mask_odno",              # log_mask (단일 salt)
    "_mask_odno_references",  # trading_bot: 자유 텍스트(브로커 사유) 안의 참조만
    "_mask_token_in",         # trading_bot: 접수 알림 twin 메시지
    "_safe_error_text",       # trading_bot: 예외 메시지 정리
    "_independent_odno_hash", # ops/verify_run: salt 로 독립 계산
}
# 개수/존재 여부만 출력하는 안전한 호출 (주문번호 자체는 남지 않음)
_SAFE_CALLS = {"len", "bool"}
# 사전 조회 — 키가 주문번호 필드면 원문이 그대로 나갑니다
_DICT_GETTERS = {"get", "pop", "setdefault"}

# 주문번호 식별자 이름: odno / ordno / ord_no / oder_no / orr_no / order_id ...
# (밑줄·대소문자를 무시하고 비교: orderId, order_ids, last_processed_ordno 모두 포함)
_ID_TOKEN = re.compile(r"(odno|oderno|ordno|orrno|orderid)", re.IGNORECASE)
# 주문 dict/목록 자체를 통째로 찍으면 주문번호가 새어 나갑니다
_WHOLE_ORDER_NAMES = {"order", "orders"}

# 남은 예외 — **이유가 적힌 것만** 허용합니다. (줄 번호가 아니라 식/문구로 식별)
_EXEMPTIONS = {
}


def _id_like(name):
    """주문번호를 담는 이름인지 판정합니다 (`orderId`/`order_ids`/`orr_no`/`ODNO` 등).

    밑줄·대소문자를 지운 뒤 비교하므로 `order_id`↔`orderId`, `ord_no`↔`ordno` 가 같은
    식별자로 취급됩니다 (브로커마다 표기가 다릅니다).
    """
    if not name:
        return False
    normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return bool(_ID_TOKEN.search(normalized))


def _leaks_in_expression(expr):
    """표현식 안에서 마스킹되지 않은 주문번호 참조를 찾습니다 → [(lineno, 표현식)]."""
    found = []

    def visit(node):
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if func_name in _MASK_WRAPPERS or func_name in _SAFE_CALLS:
                return
            if func_name in _DICT_GETTERS:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _id_like(arg.value):
                        found.append((node.lineno, ast.unparse(node)))
                        return
                for arg in node.args:
                    visit(arg)
                return
            for child in ast.iter_child_nodes(node):
                visit(child)
            return
        if isinstance(node, ast.Attribute):
            # result.order_id / item.ODNO 등
            if _id_like(node.attr):
                found.append((node.lineno, ast.unparse(node)))
            return
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                # order['order_id'] 는 누출, order['comment'] 는 안전
                if _id_like(key.value):
                    found.append((node.lineno, ast.unparse(node)))
                return
            if isinstance(key, ast.Name) and _id_like(key.id):
                found.append((node.lineno, ast.unparse(node)))
                return
            visit(node.value)  # orders[0] → orders 자체 출력으로 판정
            return
        if isinstance(node, ast.Name):
            if _id_like(node.id) or node.id.lower() in _WHOLE_ORDER_NAMES:
                found.append((node.lineno, ast.unparse(node)))
            return
        if isinstance(node, ast.IfExp):
            # 조건(`x if last_ordno else ''`)은 출력값이 아닙니다
            visit(node.body)
            visit(node.orelse)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(expr)
    return found


def _sink_arguments(node):
    """sink 호출/예외에서 '출력되는' 인자 목록을 반환합니다 (없으면 빈 목록)."""
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if name in _SINKS:
            return list(node.args) + [keyword.value for keyword in node.keywords]
        return []
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        name = getattr(node.exc.func, "id", "") or getattr(node.exc.func, "attr", "")
        if name in _EXCEPTION_SINKS:
            return list(node.exc.args)
    return []


def _parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _scope_of(node, parents):
    """가장 가까운 함수 스코프(모듈 레벨이면 None)를 반환합니다."""
    current = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _scope_masked_names(tree, parents):
    """스코프별 '마스킹된 값을 배정받은 변수 이름' 집합."""
    scopes = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        wrapped = any(
            isinstance(inner, ast.Call)
            and (getattr(inner.func, "id", "") or getattr(inner.func, "attr", ""))
            in _MASK_WRAPPERS
            for inner in ast.walk(node.value)
        )
        if not wrapped:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bucket = scopes.setdefault(_scope_of(node, parents), set())
        for target in targets:
            if isinstance(target, ast.Name):
                bucket.add(target.id)
    return scopes


def scan_source(source, relpath="<memory>"):
    """소스에서 마스킹되지 않은 주문번호 출력을 찾습니다 → 위반 메시지 목록."""
    tree = ast.parse(source)
    lines = source.splitlines()
    parents = _parent_map(tree)
    scopes = _scope_masked_names(tree, parents)
    violations = []

    for node in ast.walk(tree):
        for arg in _sink_arguments(node):
            masked = scopes.get(_scope_of(node, parents), set())
            for lineno, text in _leaks_in_expression(arg):
                if text in masked:  # 마스킹된 값을 배정받은 변수
                    continue
                if (relpath, text) in _EXEMPTIONS:
                    continue
                violations.append(
                    f"{relpath}:{lineno}: {text}  ← {lines[lineno - 1].strip()[:100]}"
                )
    return sorted(set(violations))


@pytest.mark.parametrize("relpath", _SCANNED_FILES)
def test_no_raw_odno_in_public_outputs(relpath):
    """브로커 어댑터·상태·파이프라인·검증기의 공개 출력에 원문 주문번호가 없어야 합니다."""
    source = (ROOT / relpath).read_text(encoding="utf-8")
    violations = scan_source(source, relpath)
    assert not violations, (
        "원문 주문번호 출력 발견 (mask_odno / _mask_odno_references 사용):\n"
        + "\n".join(violations)
    )


def test_all_scanned_files_exist():
    missing = [rel for rel in _SCANNED_FILES if not (ROOT / rel).is_file()]
    assert not missing, f"정적 가드 스캔 대상이 없습니다: {missing}"


# 가드 자체 검증(양성/음성 대조) — 탐지기가 조용히 무력화되면 이 테스트가 먼저 깨집니다.
_UNMASKED_SAMPLES = {
    "attribute": 'print(f"주문번호: {result.order_id}")',
    "subscript": 'print(f"주문번호: {order[\'order_id\']}")',
    "dict_get": 'print(f"odno={output.get(\'ODNO\')}")',
    "camelCase": 'print(f"order={raw.get(\'orderId\')}")',
    "list_var": "print(order_ids)",
    "whole_dict": "print(order)",
    "raise_sink": 'raise BrokerError(f"취소 실패: {item.get(\'odno\')}")',
    "notify_sink": "notify(f\'주문번호 {odno}\')",
}
_MASKED_SAMPLES = {
    "wrapped": 'print(f"주문번호: {mask_odno(result.order_id)}")',
    "wrapped_free_text": "print(_mask_odno_references(str(error)))",
    "bound_variable": (
        "def f(telegram_message, order_id):\n"
        "    masked_reason = _mask_odno_references(telegram_message)\n"
        "    print(f'사유: {masked_reason}')\n"
    ),
    "mask_bound_twin": (
        "def f(telegram_message, order_id):\n"
        "    message = _mask_token_in(telegram_message, order_id)\n"
        "    notify(message, telegram_message=telegram_message)\n"
    ),
    "len_only": "print(f'보류 {len(pending_amount_odnos)}건')",
    "safe_field": 'print(f"설명: {order[\'comment\']}")',
    "conditional_test": "print(f\"{mask_odno(last_ordno) if last_ordno else ''}\")",
}


@pytest.mark.parametrize("name", sorted(_UNMASKED_SAMPLES))
def test_guard_catches_unmasked_variants(name):
    violations = scan_source(_UNMASKED_SAMPLES[name])
    assert violations, f"가드가 마스킹 누락을 놓쳤습니다: {name}"


@pytest.mark.parametrize("name", sorted(_MASKED_SAMPLES))
def test_guard_allows_masked_variants(name):
    violations = scan_source(_MASKED_SAMPLES[name])
    assert not violations, f"가드가 정상 코드를 오탐했습니다: {name} → {violations}"


def test_guard_catches_temporarily_unmasked_adapter_line(tmp_path):
    """실제 어댑터 소스에서 한 줄만 마스킹을 풀어도 가드가 잡아야 합니다 (음성 대조)."""
    relpath = "src/broker/kis/adapter.py"
    source = (ROOT / relpath).read_text(encoding="utf-8")
    masked_line = 'print(f"주문번호: {mask_odno(order_id)}")'
    unmasked_line = 'print(f"주문번호: {order_id}")'
    assert masked_line in source
    assert not scan_source(source, relpath), "현재 소스는 통과해야 합니다"

    tampered = tmp_path / "adapter.py"
    tampered.write_text(source.replace(masked_line, unmasked_line), encoding="utf-8")
    violations = scan_source(tampered.read_text(encoding="utf-8"), relpath)
    assert violations, "마스킹을 푼 줄을 가드가 잡지 못했습니다"
    assert "order_id" in violations[0]


def test_no_remaining_exemptions():
    """공개 출력의 원문 주문번호 예외는 0건이어야 합니다 (예외가 생기면 사유와 함께 등록)."""
    assert _EXEMPTIONS == {}, sorted(_EXEMPTIONS)


# ── 접수 알림의 원문(telegram_message)은 twin 마스킹과 함께만 존재 ─────
# `ast.unparse` 는 f-string 을 한 줄로 만들고 줄바꿈을 `\n`(2글자)로 남깁니다.
# 그래서 일반 주문 패턴은 앞의 줄바꿈까지 포함해 예약주문번호(`\n예약주문번호: …`)와 구분합니다.
_RAW_TELEGRAM_PATTERNS = (
    "예약주문번호: {result.order_id}",    # 예약주문 접수 알림
    r"\n주문번호: {result.order_id}",     # 일반 주문 접수 알림
)


def _sibling_block(node, parents):
    """같은 블록(리스트)에 속한 문장들을 반환합니다."""
    parent = parents.get(node)
    while parent is not None:
        for _field, value in ast.iter_fields(parent):
            if isinstance(value, list) and node in value:
                return value
        node, parent = parent, parents.get(parent)
    return []


def _assign_target(statement, name):
    if isinstance(statement, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in statement.targets)
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return isinstance(statement.target, ast.Name) and statement.target.id == name
    return False


def test_raw_telegram_message_sites_are_explicitly_paired_with_masked_twin():
    """개인 채팅용 원문(`telegram_message`)은 **허용된 줄 패턴**에서만 만들고,
    같은 블록에서 `message = _mask_token_in(telegram_message, <주문번호>)` 로
    마스킹된 twin 을 반드시 함께 만든 뒤 `notify(message, telegram_message=...)` 로 보냅니다.

    (실수로 새 원문 사이트를 추가하거나 twin 을 지우면 이 테스트가 깨집니다.)
    """
    tree = ast.parse((ROOT / "trading_bot.py").read_text(encoding="utf-8"))
    parents = _parent_map(tree)

    raw_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and _assign_target(node, "telegram_message")
        and isinstance(node.value, ast.JoinedStr)
    ]
    assert len(raw_sites) == len(_RAW_TELEGRAM_PATTERNS), (
        f"원문 telegram_message 사이트가 {len(raw_sites)}곳입니다 — 허용 목록"
        f"({len(_RAW_TELEGRAM_PATTERNS)}곳)과 맞추고 사유를 기록하세요: "
        f"{[line for line in (ast.unparse(site.value).splitlines()[-1] for site in raw_sites)]}"
    )

    for site in raw_sites:
        site_source = ast.unparse(site.value)
        matched = [pattern for pattern in _RAW_TELEGRAM_PATTERNS if pattern in site_source]
        assert matched, (
            "허용되지 않은 원문 알림 사이트입니다 — 원문은 예약주문/일반주문 접수 알림에서만 "
            f"허용합니다:\n{site_source[:200]}"
        )
        # 패턴은 서로 달라야 어느 사이트인지 구분됩니다 (예약주문번호 vs 주문번호)
        assert len(matched) == 1

        block = _sibling_block(site, parents)
        twins = [
            statement for statement in block
            if isinstance(statement, ast.Assign)
            and _assign_target(statement, "message")
            and isinstance(statement.value, ast.Call)
            and (getattr(statement.value.func, "id", "") or getattr(statement.value.func, "attr", ""))
            == "_mask_token_in"
            and len(statement.value.args) == 2
            and isinstance(statement.value.args[0], ast.Name)
            and statement.value.args[0].id == "telegram_message"
        ]
        assert twins, f"원문 사이트({matched[0]})에 마스킹된 twin(message)이 없습니다"
        twin_source = ast.unparse(twins[0].value)
        assert "order_id" in twin_source, (
            f"twin 이 주문번호를 마스킹하지 않습니다: {twin_source}"
        )

        notify_calls = [
            statement for statement in block
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and (getattr(statement.value.func, "id", "") or getattr(statement.value.func, "attr", ""))
            == "notify"
        ]
        assert notify_calls, f"원문 사이트({matched[0]})에서 notify 호출을 찾지 못했습니다"
        call = notify_calls[0].value
        assert call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "message", (
            "notify 첫 인자는 마스킹된 message 변수여야 합니다 (원문 telegram_message 금지)"
        )
        assert any(
            keyword.arg == "telegram_message"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "telegram_message"
            for keyword in call.keywords
        ), "개인 채팅용 telegram_message 를 함께 넘기지 않습니다"


# ── 단일 salt 불변식: 상태·어댑터·원장이 같은 모듈 객체를 써야 함 ────────
def test_single_salt_is_shared_by_state_and_adapters():
    """salt 는 프로세스당 하나여야 합니다.

    `log_mask` 를 `log_mask` 와 `src.log_mask` 두 이름으로 import 하면 파이썬이
    **별도 모듈 객체**를 만들어 salt 가 두 개가 되고, state 로그 토큰과 어댑터 로그 토큰이
    서로 달라져 공개 로그를 대조할 수 없습니다 (원장도 갈라집니다).
    """
    import log_mask
    import state
    from broker import base as base_module
    from broker.kis import adapter as kis_adapter
    from broker.kiwoom import adapter as kiwoom_adapter
    from broker.ls import adapter as ls_adapter
    from broker.toss import adapter as toss_adapter
    from broker.nhplug import adapter as nhplug_adapter
    from broker.meritz import adapter as meritz_adapter

    # 1) salt 는 같은 객체
    assert state._LOG_SALT is log_mask._LOG_SALT
    assert log_salt_hex() == log_mask.log_salt_hex()

    # 2) 모든 어댑터가 같은 mask_odno 함수(=같은 salt·같은 원장)를 씁니다
    for module in (
        base_module, kis_adapter, kiwoom_adapter, ls_adapter,
        toss_adapter, nhplug_adapter, meritz_adapter,
    ):
        assert module.mask_odno is log_mask.mask_odno, module.__name__

    # 3) state 쪽 토큰과 어댑터 쪽 토큰이 동일 (대조 가능)
    assert state.mask_odno(RAW_A) == kis_adapter.mask_odno(RAW_A)

    # 4) 같은 파일이 두 모듈 이름으로 로드되지 않았습니다
    duplicates = sorted(name for name in sys.modules if name.endswith("log_mask"))
    assert duplicates == ["log_mask"], duplicates
    assert sys.modules["log_mask"] is log_mask
    assert Path(log_mask.__file__).resolve() == (ROOT / "src" / "log_mask.py").resolve()


def test_no_source_imports_src_log_mask():
    """`src.log_mask` import 는 salt·원장을 둘로 쪼개므로 소스에서 금지합니다.

    주석에 경고 문구로 등장하는 것은 괜찮고, **실제 import 문**만 검사합니다.
    """
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            else:
                continue
            if any(name == "src.log_mask" or name.endswith(".log_mask") for name in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "src" and any(
                alias.name == "log_mask" for alias in node.names
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"src.log_mask import 금지 (salt 분리 위험): {offenders}"
