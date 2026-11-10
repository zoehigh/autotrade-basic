"""메리츠 보정 BUY LOC 사후 재조정 테스트 (mock 전용, 주문·정정 호출 없음)."""
import os
import sys
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meritz_reprice as rp
from broker.base import OrderError, OrderNotAcceptedError
from strategy import 무한매수법_V4

NOW = datetime.now(ZoneInfo("Asia/Seoul"))
ET_TODAY = NOW.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
OLD = f"{NOW.strftime('%Y%m%d')}:0000100"
NEW = f"{NOW.strftime('%Y%m%d')}:0000200"


def _row(odno=OLD, status="미체결", filled=0, qty=7, price=70.0, cls="32", rctf="0", orgl=""):
    return {
        "odno": odno, "ord_dt": NOW.strftime("%Y%m%d"), "ord_tmd": NOW.strftime("%H%M%S"),
        "_ord_dt_is_kst_date": True, "sll_buy_dvsn_cd_name": "매수",
        "ft_ord_qty": str(qty), "ft_ccld_qty": str(filled),
        "nccs_qty": str(qty - filled), "prcs_stat_name": status,
        "_order_price": price, "_oder_cls_code": cls, "_rctf_code": rctf,
        "_plain_orgl_oder_no": orgl,
        "_oder_date": NOW.strftime("%Y%m%d"), "_is_rejected": status == "거부",
        "_is_cancel": status == "취소", "_ordered_qty": qty,
    }


def _state(**meta_extra):
    meta = {"side": "BUY", "total_qty": 7, "t_target": 1.0, "is_additional": False,
            "processed_filled_qty": 0, "processed_filled_amount": 0.0,
            "fill_marker_version": 1, "original_price": 73.69, "order_price": 70.0}
    meta.update(meta_extra)
    return {"orders_meta": {OLD: meta}, "T": 5.0, "net_invested": 1000.0}


def _plan(state, row, last=65.0, overrides=None):
    return rp.build_plan(state, [row] if row else [], last, overrides or {}, ET_TODAY)[0]


# ── 계획: 허용 범위 안에서만 올리고 원래 매수가는 넘지 않음 ───────────────

def test_amend_raises_to_original_when_allowed():
    item = _plan(_state(), _row(), last=65.0)  # 65*1.19=77.35 > 73.69
    assert item["action"] == rp.ACTION_AMEND and item["target_price"] == 73.69


def test_amend_limited_by_current_price_cap_never_exceeds_original():
    item = _plan(_state(), _row(price=60.0), last=62.0)  # 62*1.19=73.78 → 73.69 가 상한
    assert item["target_price"] <= 73.69
    item = _plan(_state(original_price=90.0), _row(price=60.0), last=62.0)
    assert item["target_price"] == 73.78 and item["target_price"] < 90.0


def test_noop_when_already_high_enough():
    assert _plan(_state(), _row(price=73.69))["action"] == rp.ACTION_NOOP


def test_override_original_for_orders_without_meta_price():
    state = _state()
    del state["orders_meta"][OLD]["original_price"]
    assert _plan(state, _row())["action"] == rp.ACTION_SKIP
    assert _plan(state, _row(), overrides={OLD: 73.69})["action"] == rp.ACTION_AMEND


@pytest.mark.parametrize("row_kwargs", [
    {"status": "부분체결", "filled": 2},
    {"status": "체결", "filled": 7},
    {"status": "취소"},
    {"cls": "01"},
    {"qty": 5},
])
def test_skip_unsafe_orders(row_kwargs):
    assert _plan(_state(), _row(**row_kwargs))["action"] == rp.ACTION_SKIP


def test_skip_additional_reverse_sell_and_missing_row():
    assert _plan(_state(is_additional=True), _row())["action"] == rp.ACTION_SKIP
    assert _plan(_state(reverse_action="x"), _row())["action"] == rp.ACTION_SKIP
    assert _plan(_state(side="SELL"), _row())["action"] == rp.ACTION_SKIP
    assert _plan(_state(), None)["action"] == rp.ACTION_SKIP


def test_skip_already_applied_fills_and_previous_session():
    assert _plan(_state(processed_filled_qty=1), _row())["action"] == rp.ACTION_SKIP
    old_row = _row()
    old_row["ord_dt"] = "20200101"
    assert _plan(_state(), old_row)["action"] == rp.ACTION_SKIP


def test_rejected_buy_loc_is_listed_but_never_a_target():
    plan = rp.build_plan({"orders_meta": {}}, [_row(odno="x:1", status="거부")], 65.0, {}, ET_TODAY)
    assert plan[0]["action"] == rp.ACTION_REORDER


# ── 실행: 거부 / 타임아웃 / 연결 / 중복 ──────────────────────────────

class FakeBroker:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def modify_order(self, odno, symbol, exchange, side, qty, price, order_type):
        self.calls.append((odno, qty, price, order_type))
        if self.error:
            raise self.error
        return NEW


def _apply(broker, state, tmp_path, saved, history_fn=None):
    item = _plan(state, _row())
    journal = str(tmp_path / "j.json")
    result = rp.apply_amend(broker, "SOXL", "NY", item, state, journal,
                            lambda s, st: saved.append(deepcopy(st)),
                            history_fn or (lambda: [_row(odno=NEW, rctf="1", orgl="100", price=73.69)]),
                            verify_wait=0)
    return result, journal


def test_apply_success_moves_meta_and_keeps_settlement_markers(tmp_path):
    state, saved = _state(processed_filled_qty=0), []
    before = deepcopy(state)
    result, journal = _apply(FakeBroker(), state, tmp_path, saved)
    assert result == "AMENDED" and rp.read_journal(journal) == []
    assert OLD not in state["orders_meta"]
    meta = state["orders_meta"][NEW]
    assert meta["total_qty"] == 7 and meta["order_price"] == 73.69 and meta["amended_from"] == OLD
    # T·순투입·정산 표식은 그대로 (중복 정산 방지)
    assert state["T"] == before["T"] and state["net_invested"] == before["net_invested"]
    assert meta["processed_filled_qty"] == 0 and meta["fill_marker_version"] == 1
    assert len(saved) == 1


def test_apply_rejection_leaves_order_and_state_untouched(tmp_path):
    state, saved = _state(), []
    result, journal = _apply(FakeBroker(OrderNotAcceptedError("5636")), state, tmp_path, saved)
    assert result == "REJECTED" and OLD in state["orders_meta"] and not saved
    assert rp.read_journal(journal) == []


def test_apply_timeout_keeps_journal_and_does_not_reorder(tmp_path):
    broker, saved = FakeBroker(OrderError("timeout")), []
    result, journal = _apply(broker, _state(), tmp_path, saved)
    assert result == "UNCERTAIN" and len(broker.calls) == 1 and not saved
    assert len(rp.read_journal(journal)) == 1


def test_link_failure_keeps_journal(tmp_path):
    state = _state()
    item = _plan(state, _row())
    journal = str(tmp_path / "j.json")

    def boom(symbol, st):
        raise OSError("disk")
    ok_history = lambda: [_row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    assert rp.apply_amend(FakeBroker(), "SOXL", "NY", item, state, journal, boom,
                          ok_history, verify_wait=0) == "LINK_FAILED"
    assert len(rp.read_journal(journal)) == 1


def test_restart_resolves_journal_from_history_once(tmp_path):
    state = _state()
    entries = [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69}]
    history = [_row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    left, _ = rp.resolve_journal(entries, history, state)
    assert left == [] and NEW in state["orders_meta"] and OLD not in state["orders_meta"]
    # 한 번 더 실행해도 안전 (이미 연결됨)
    left, _ = rp.resolve_journal(entries, history, state)
    assert left == []


def test_restart_keeps_uncertain_when_history_has_no_or_many_matches():
    entries = [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69}]
    assert rp.resolve_journal(entries, [], _state())[0] == entries
    two = [_row(odno=NEW, rctf="1", orgl="100", price=73.69), _row(odno="x:3", rctf="1", orgl="100", price=73.69)]
    assert rp.resolve_journal(entries, two, _state())[0] == entries


def test_second_run_after_amend_is_noop():
    state = _state()
    rp.relink_amended_order(state, OLD, NEW, 7, 73.69)
    row = _row(odno=NEW, rctf="1", orgl="100", price=73.69)
    items = rp.build_plan(state, [row], 65.0, {}, ET_TODAY)
    assert [i["action"] for i in items] == [rp.ACTION_NOOP]


# ── 전략: 보정된 주문에만 원래 매수가가 남음 ─────────────────────────

def test_strategy_records_original_price_only_when_capped(monkeypatch):
    from test_buy_price_cap import _soxl_late_half_orders
    capped = _soxl_late_half_orders(monkeypatch, last_price=93.71)
    normal = [o for o in capped["orders"] if o["side"] == "BUY" and "[추가매수]" not in o["comment"]][0]
    assert normal["price"] == 111.51 and normal["original_price"] > normal["price"]
    # 상한에 걸리지 않으면 기록하지 않음 (재조정 대상 아님)
    free = _soxl_late_half_orders(monkeypatch, last_price=120.0)
    normal = [o for o in free["orders"] if o["side"] == "BUY" and "[추가매수]" not in o["comment"]][0]
    assert not normal.get("original_price")
    # 가격 공식은 변하지 않음 (기존 테스트와 동일한 보정가)
    assert capped["price_cap_notes"]


# ── CLI: 기본은 조회 전용, 안전장치 없이는 정정 거부 ──────────────────

class _ReadOnlyBroker(FakeBroker):
    def exchange_code(self, ex):
        return "NY"

    def get_order_history(self, *a, **k):
        return [_row()]

    def get_stock_price(self, *a, **k):
        class P:
            last = 65.0
        return P()


def _cli(monkeypatch, tmp_path, argv, broker):
    import meritz_buy_loc_reprice as cli
    import state as st
    monkeypatch.setattr(cli, "bot_is_running", lambda: False)
    st.save_state("SOXL", _state())
    return cli.main(argv, broker=broker)


def test_cli_default_is_plan_only(monkeypatch, tmp_path):
    broker = _ReadOnlyBroker()
    _cli(monkeypatch, tmp_path, ["--symbol", "SOXL"], broker)
    assert broker.calls == []


def test_cli_apply_refused_without_env_gate(monkeypatch, tmp_path):
    monkeypatch.delenv("ALLOW_BROKER_ORDERS", raising=False)
    broker = _ReadOnlyBroker()
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"], broker)
    assert broker.calls == []


# ── 리뷰 반영: 복구 검증 강화 / 조회 모드 비파괴 / 정산 보류 / 입력 검증 ───────

_ENTRY = {"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69}


@pytest.mark.parametrize("change", [
    {"_oder_date": "20200101"}, {"_ordered_qty": 3},
    {"price": 60.0}, {"sll_buy_dvsn_cd_name": "매도"}, {"_oder_cls_code": "01"},
])
def test_journal_resolution_rejects_mismatched_rows(change):
    row = _row(odno=NEW, rctf="1", orgl="100", price=73.69,
               status=change.pop("status", "미체결"))
    if "price" in change:
        row["_order_price"] = change.pop("price")
    row.update(change)
    state = _state()
    left, _ = rp.resolve_journal([dict(_ENTRY)], [row], state)
    assert left and OLD in state["orders_meta"]


def test_query_mode_does_not_touch_journal_or_state(monkeypatch, tmp_path):
    import state as st
    import meritz_buy_loc_reprice as cli
    broker = _ReadOnlyBroker()
    broker.get_order_history = lambda *a, **k: [_row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    import run_lock
    journal = tmp_path / ".reprice_journal.json"
    assert run_lock.journal_path() == str(journal)
    rp.write_journal(str(journal), [dict(_ENTRY)])
    before = journal.read_text()
    _cli(monkeypatch, tmp_path, ["--symbol", "SOXL"], broker)
    assert journal.read_text() == before
    assert OLD in st.load_state("SOXL")["orders_meta"]


def test_unresolved_journal_holds_bot_settlement(tmp_path):
    path = str(tmp_path / "j.json")
    assert rp.unresolved_symbols(path) == set()
    rp.write_journal(path, [dict(_ENTRY)])
    assert "SOXL" in rp.unresolved_symbols(path)
    (tmp_path / "bad.json").write_text("{broken")
    assert rp.unresolved_symbols(str(tmp_path / "bad.json")) == {"*"}


def test_run_one_symbol_source_checks_journal_before_settlement():
    src = open(os.path.join(os.path.dirname(__file__), "..", "trading_bot.py"), encoding="utf-8").read()
    assert src.index("unresolved_symbols()") < src.index("update_T_from_history(symbol, state, order_history")


@pytest.mark.parametrize("bad", ["inf", "nan", "-1", "0", "abc"])
def test_original_price_must_be_finite_positive(bad):
    import meritz_buy_loc_reprice as cli
    with pytest.raises(SystemExit):
        cli.parse_overrides([f"{OLD}={bad}"])
    state = _state()
    del state["orders_meta"][OLD]["original_price"]
    assert _plan(state, _row(), overrides={OLD: float(bad) if bad != "abc" else 0})["action"] == rp.ACTION_SKIP


def test_apply_requires_shared_run_lock(monkeypatch, tmp_path):
    import fcntl
    import meritz_buy_loc_reprice as cli
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    import config
    monkeypatch.setattr(config, "BROKER", "meritz")
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")
    run_lock = tmp_path / ".run.lock"
    held = open(run_lock, "w")
    fcntl.flock(held, fcntl.LOCK_EX)  # 런처가 쥐고 있는 상황
    broker = _ReadOnlyBroker()
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"], broker)
    assert broker.calls == []
    held.close()


# ── 2차 리뷰 반영: 봇 본체 락 / 경로 일원화 ─────────────────────────

def test_paths_are_derived_from_state_file_dir(tmp_path):
    import run_lock
    for path in (run_lock.run_lock_path(), run_lock.bot_lock_path(), run_lock.journal_path()):
        assert os.path.dirname(path) == str(tmp_path)


def test_bot_main_refuses_to_start_while_lock_is_held(tmp_path, capsys):
    import fcntl
    import trading_bot
    import run_lock
    held = open(run_lock.bot_lock_path(), "a")
    fcntl.flock(held, fcntl.LOCK_EX)  # 정정 도구 또는 다른 봇이 실행 중
    try:
        with pytest.raises(SystemExit) as exc:
            trading_bot.main()
        assert exc.value.code == 1
    finally:
        held.close()


def test_apply_refused_while_bot_lock_is_held(monkeypatch, tmp_path):
    import fcntl
    import config
    import run_lock
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    monkeypatch.setattr(config, "BROKER", "meritz")
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")
    held = open(run_lock.bot_lock_path(), "a")
    fcntl.flock(held, fcntl.LOCK_EX)  # 직접 실행된 봇
    broker = _ReadOnlyBroker()
    try:
        with pytest.raises(SystemExit):
            _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"], broker)
    finally:
        held.close()
    assert broker.calls == []


# ── 거부된 정정 행: 원주문 생존 여부를 이력으로 판별할 수 없음 → 보류 ─────────

def _rejected_child_history():
    child = _row(odno=NEW, status="거부", rctf="1", orgl="100", price=73.69)
    return [_row(status="정정됨"), child]  # 이력은 원주문을 '정정됨'으로 보여 줌


def test_plan_holds_original_when_a_rejected_amend_row_exists():
    plan = rp.build_plan(_state(), _rejected_child_history(), 65.0, {}, ET_TODAY)
    item = next(i for i in plan if i["odno"] == OLD)
    assert item["action"] == rp.ACTION_SKIP and "판별할 수 없음" in item["reason"]


def test_plan_does_not_touch_already_amended_order():
    history = [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]
    item = next(i for i in rp.build_plan(_state(), history, 65.0, {}, ET_TODAY) if i["odno"] == OLD)
    assert item["action"] == rp.ACTION_SKIP and "이미 정정" in item["reason"]


def test_late_rejection_keeps_original_meta_and_does_not_relink(tmp_path):
    state, saved = _state(), []
    rejected = lambda: [_row(odno=NEW, status="거부", rctf="1", orgl="100", price=73.69)]
    result, journal = _apply(FakeBroker(), state, tmp_path, saved, history_fn=rejected)
    assert result == "REJECTED_LATE"
    assert OLD in state["orders_meta"] and NEW not in state["orders_meta"] and not saved
    # 지우지 않고 표식을 남긴다: 원주문 생존 불명 → 봇이 해당 종목을 보류하도록
    entries = rp.read_journal(journal)
    assert len(entries) == 1 and entries[0]["review_required"] == "REJECTED_LATE" and entries[0]["old_odno"] == OLD


@pytest.mark.parametrize("history_fn", [lambda: [], lambda: (_ for _ in ()).throw(OSError("x"))])
def test_unverified_amend_keeps_journal_and_stops(tmp_path, history_fn):
    state, saved = _state(), []
    result, journal = _apply(FakeBroker(), state, tmp_path, saved, history_fn=history_fn)
    assert result == "UNCERTAIN" and OLD in state["orders_meta"] and not saved
    assert len(rp.read_journal(journal)) == 1


def test_journal_resolution_with_only_rejected_child_does_not_relink():
    state = _state()
    left, messages = rp.resolve_journal([dict(_ENTRY)], _rejected_child_history(), state)
    # 확인 없이는 정리하지 않고 표식을 붙여 보류 유지
    assert len(left) == 1 and left[0]["review_required"] == "REJECTED_LATE"
    assert OLD in state["orders_meta"] and NEW not in state["orders_meta"]
    # 사람이 원주문을 확인했다고 명시해야 정리
    left, messages = rp.resolve_journal([dict(_ENTRY)], _rejected_child_history(), state, original_checked=True)
    assert left == [] and OLD in state["orders_meta"] and NEW not in state["orders_meta"]
    assert any("판별 불가" in m for m in messages)


def test_journal_resolution_mixed_accepted_and_rejected_stays_uncertain():
    history = _rejected_child_history() + [_row(odno="x:9", rctf="1", orgl="100", price=73.69)]
    left, _ = rp.resolve_journal([dict(_ENTRY)], history, _state())
    assert left


def test_real_shape_rejected_then_accepted_amend_rows():
    """운영 이력 실측 형태: 거부된 정정 행 뒤에 별개의 정정이 접수된 경우도 보류로 처리."""
    plan = rp.build_plan(_state(), _rejected_child_history() +
                         [_row(odno="x:8", rctf="1", orgl="100", price=70.0)], 65.0, {}, ET_TODAY)
    assert next(i for i in plan if i["odno"] == OLD)["action"] == rp.ACTION_SKIP


# ── Codex 재리뷰 반영 ───────────────────────────────────────────────

def _apply_env(monkeypatch):
    import config
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    monkeypatch.setattr(config, "BROKER", "meritz")
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")


class _JournalBroker(_ReadOnlyBroker):
    def get_order_history(self, *a, **k):
        return [_row(status="정정됨"), _row(odno=NEW, rctf="1", orgl="100", price=73.69)]


@pytest.mark.parametrize("confirm", [[], ["--confirm", "WRONG"], ["--confirm", "REPRICE TQQQ"]])
def test_journal_recovery_requires_symbol_confirmation(monkeypatch, tmp_path, confirm):
    import run_lock
    import state as st
    _apply_env(monkeypatch)
    rp.write_journal(run_lock.journal_path(), [dict(_ENTRY)])
    journal_before = open(run_lock.journal_path()).read()
    broker = _JournalBroker()
    code = _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply"] + confirm, broker)
    assert code == 1 and broker.calls == []
    assert OLD in st.load_state("SOXL")["orders_meta"]          # state 불변
    assert open(run_lock.journal_path()).read() == journal_before  # journal 불변


def test_journal_recovery_runs_with_matching_confirmation(monkeypatch, tmp_path):
    import run_lock
    import state as st
    _apply_env(monkeypatch)
    rp.write_journal(run_lock.journal_path(), [dict(_ENTRY)])
    _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"], _JournalBroker())
    metas = st.load_state("SOXL")["orders_meta"]
    assert NEW in metas and OLD not in metas
    assert rp.read_journal(run_lock.journal_path()) == []


def test_override_original_price_survives_amend_for_later_repricing(tmp_path):
    state = _state()
    del state["orders_meta"][OLD]["original_price"]       # 구 주문: 메타에 원래가 없음
    item = _plan(state, _row(), overrides={OLD: 73.69})
    assert item["action"] == rp.ACTION_AMEND and item["original_price"] == 73.69
    saved = []
    result = rp.apply_amend(FakeBroker(), "SOXL", "NY", item, state, str(tmp_path / "j.json"),
                            lambda s, st: saved.append(1),
                            lambda: [_row(odno=NEW, rctf="1", orgl="100", price=73.69)], verify_wait=0)
    assert result == "AMENDED"
    assert state["orders_meta"][NEW]["original_price"] == 73.69
    # 다음 실행: --original 없이도 계획 가능 (현재가 상한 때문에 아직 낮은 경우 재상향)
    row = _row(odno=NEW, rctf="1", orgl="100", price=66.0)
    item = rp.build_plan(state, [row], 62.0, {}, ET_TODAY)[0]
    assert item["action"] == rp.ACTION_AMEND and item["target_price"] == 73.69


def test_journal_resolution_also_restores_original_price():
    state = _state()
    del state["orders_meta"][OLD]["original_price"]
    entry = dict(_ENTRY, original_price=73.69)
    rp.resolve_journal([entry], [_row(odno=NEW, rctf="1", orgl="100", price=73.69)], state)
    assert state["orders_meta"][NEW]["original_price"] == 73.69


# ── 2차 Codex 재검토 반영 ───────────────────────────────────────────

def _pending_journal():
    import run_lock
    rp.write_journal(run_lock.journal_path(), [dict(_ENTRY)])


def _no_broker(monkeypatch):
    import trading_bot

    def boom(*a, **k):
        raise AssertionError("journal 보류 중에는 브로커를 만들면 안 됩니다")
    monkeypatch.setattr(trading_bot, "create_broker", boom)
    return trading_bot


def test_pending_journal_helper_is_symbol_scoped_and_fails_closed(tmp_path):
    import trading_bot
    import run_lock
    assert not trading_bot._pending_amend_journal("SOXL")
    _pending_journal()
    assert trading_bot._pending_amend_journal("soxl") and not trading_bot._pending_amend_journal("TQQQ")
    open(run_lock.journal_path(), "w").write("{broken")
    assert trading_bot._pending_amend_journal("TQQQ")  # 읽을 수 없으면 모든 종목 보류


@pytest.mark.parametrize("path", ["reconcile", "reinference", "force_t", "net_invested"])
def test_maintenance_settlement_paths_stop_before_broker_when_journal_pending(monkeypatch, path):
    trading_bot = _no_broker(monkeypatch)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [{"symbol": "SOXL", "exchange": "AMS", "force_t": 10}])
    _pending_journal()
    if path == "net_invested":
        for key, value in {"SYMBOL": "SOXL", "TARGET": "1", "EXPECT_HASH": "x",
                           "EXPECT_NET_INVESTED": "0", "EXPECT_STATUS": "unresolved"}.items():
            monkeypatch.setenv(f"STATE_NET_INVESTED_REPAIR_{key}", value)
    fn = {"reconcile": trading_bot._reverse_reconcile_only, "reinference": trading_bot._repair_reinference,
          "force_t": trading_bot._repair_force_t, "net_invested": trading_bot._net_invested_repair_only}[path]
    with pytest.raises(SystemExit) as exc:
        fn()
    assert exc.value.code == 1


def test_maintenance_path_not_blocked_for_other_symbol(monkeypatch):
    trading_bot = _no_broker(monkeypatch)
    monkeypatch.setattr(trading_bot, "SYMBOLS", [{"symbol": "TQQQ", "exchange": "NAS"}])
    _pending_journal()  # SOXL 만 보류
    # TQQQ 만 대상이면 게이트를 통과해 브로커 생성(여기서는 AssertionError)까지 진행해야 함
    with pytest.raises(AssertionError):
        trading_bot._reverse_reconcile_only()


def test_cancelled_amendment_row_still_resolves_journal_and_keeps_markers():
    state = _state(processed_filled_qty=0)
    cancelled = _row(odno=NEW, status="취소", filled=2, rctf="1", orgl="100", price=73.69)
    cancelled["_is_cancel"] = True  # 어댑터가 이후 취소를 정정 행에 표시한 형태 (부분체결 2주 포함)
    left, messages = rp.resolve_journal([dict(_ENTRY)], [_row(status="정정됨"), cancelled], state)
    assert left == [] and NEW in state["orders_meta"] and OLD not in state["orders_meta"]
    meta = state["orders_meta"][NEW]
    assert meta["processed_filled_qty"] == 0 and meta["fill_marker_version"] == 1  # 체결분은 이후 정산이 반영


# ── REJECTED_LATE 보완: 표식 유지 → 봇 보류 → 확인 후에만 정리 (수동 도구 + 실제 run_one_symbol 경로) ──────

def _late_rejected_world(monkeypatch, tmp_path, extra_args=()):
    """수동 도구로 정정했더니 이력에 '거부'로 남은 상황을 CLI 로 재현합니다."""
    import run_lock
    import state as st
    import config
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    monkeypatch.setattr(config, "BROKER", "meritz")
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")
    st.save_state("SOXL", _state())

    class LateRejectBroker(_ReadOnlyBroker):
        def __init__(self):
            super().__init__()
            self.modified = False

        def get_order_history(self, *a, **k):
            if not self.modified:
                return [_row()]
            return [_row(status="정정됨"), _row(odno=NEW, status="거부", rctf="1", orgl="100", price=73.69)]

        def modify_order(self, odno, symbol, exchange, side, qty, price, order_type):
            self.calls.append((odno, qty, price, order_type))
            self.modified = True
            return NEW

    broker = LateRejectBroker()
    import meritz_buy_loc_reprice as cli
    monkeypatch.setattr(cli.time if hasattr(cli, "time") else rp.time, "sleep", lambda s: None)
    code = _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL", *extra_args], broker)
    return code, broker, run_lock


def test_manual_tool_stops_everything_and_keeps_marker_on_late_rejection(monkeypatch, tmp_path):
    code, broker, run_lock = _late_rejected_world(monkeypatch, tmp_path)
    assert code == 1 and len(broker.calls) == 1                                  # 정정 시도는 1회뿐, 즉시 전체 중단
    entries = rp.read_journal(run_lock.journal_path())
    assert len(entries) == 1 and entries[0]["review_required"] == "REJECTED_LATE"
    import trading_bot
    assert trading_bot._pending_amend_journal("SOXL") and "SOXL" in rp.unresolved_symbols()


def test_manual_tool_refuses_to_clear_marker_without_original_checked(monkeypatch, tmp_path):
    code, broker, run_lock = _late_rejected_world(monkeypatch, tmp_path)
    before = open(run_lock.journal_path()).read()
    broker.calls.clear()
    # 확인 문구는 맞지만 --original-checked 없음 → 정리하지 않는다
    code = _cli_again(monkeypatch, tmp_path, broker, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"])
    assert code == 1 and open(run_lock.journal_path()).read() == before and broker.calls == []


def test_manual_tool_clears_marker_only_with_original_checked_and_confirm(monkeypatch, tmp_path):
    code, broker, run_lock = _late_rejected_world(monkeypatch, tmp_path)
    # --original-checked 만으로는 안 됨 (확인 문구 없음 → 복구는 쓰기 작업)
    _cli_again(monkeypatch, tmp_path, broker, ["--symbol", "SOXL", "--apply", "--original-checked"])
    assert len(rp.read_journal(run_lock.journal_path())) == 1
    # 둘 다 있을 때만 정리 (메타는 원주문 그대로 유지)
    _cli_again(monkeypatch, tmp_path, broker, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL", "--original-checked"])
    import state as st
    assert rp.read_journal(run_lock.journal_path()) == []
    assert OLD in st.load_state("SOXL")["orders_meta"] and NEW not in st.load_state("SOXL")["orders_meta"]
    import trading_bot
    assert not trading_bot._pending_amend_journal("SOXL")


def _unmarked_journal_world(monkeypatch, tmp_path):
    """표식 없는 journal(타임아웃·종료로 남음) + 이후 이력에 거부 행 발견 상황."""
    import run_lock
    import state as st
    import config
    monkeypatch.setenv("ALLOW_BROKER_ORDERS", "1")
    monkeypatch.setattr(config, "BROKER", "meritz")
    monkeypatch.setattr(config, "TRADE_MODE", "LIVE")
    st.save_state("SOXL", _state())
    rp.write_journal(run_lock.journal_path(), [dict(_ENTRY)])

    class B(_ReadOnlyBroker):
        def get_order_history(self, *a, **k):
            return _rejected_child_history()

    return B(), run_lock


def test_unmarked_journal_with_newly_found_rejection_needs_original_checked(monkeypatch, tmp_path):
    broker, run_lock = _unmarked_journal_world(monkeypatch, tmp_path)
    code = _cli_again(monkeypatch, tmp_path, broker, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"])
    entries = rp.read_journal(run_lock.journal_path())
    assert code == 1 and len(entries) == 1 and entries[0]["review_required"] == "REJECTED_LATE"
    import trading_bot
    assert trading_bot._pending_amend_journal("SOXL")           # 봇 보류 유지
    code = _cli_again(monkeypatch, tmp_path, broker,
                      ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL", "--original-checked"])
    assert code == 0 and rp.read_journal(run_lock.journal_path()) == []


def test_unmarked_journal_query_mode_never_clears(monkeypatch, tmp_path):
    broker, run_lock = _unmarked_journal_world(monkeypatch, tmp_path)
    before = open(run_lock.journal_path()).read()
    _cli_again(monkeypatch, tmp_path, broker, ["--symbol", "SOXL", "--original-checked"])
    assert open(run_lock.journal_path()).read() == before


def _cli_again(monkeypatch, tmp_path, broker, argv):
    import meritz_buy_loc_reprice as cli
    monkeypatch.setattr(cli, "bot_is_running", lambda: False)
    return cli.main(argv, broker=broker)


def test_run_one_symbol_stops_before_settlement_and_orders_when_late_rejection_marker_exists(monkeypatch, tmp_path):
    """봇의 실제 run_one_symbol 경로: REJECTED_LATE 표식이 있으면 정산·취소 정리·신규 주문 전에 중단."""
    import run_lock
    from unittest.mock import MagicMock
    import trading_bot
    from test_fence_scope import _make_state, _symbol_config
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69,
                                                "review_required": "REJECTED_LATE"}])
    monkeypatch.setattr(trading_bot, "load_state", lambda symbol: _make_state())
    for name in ("update_T_from_history", "save_state", "무한매수법_V4"):
        monkeypatch.setattr(trading_bot, name, MagicMock(side_effect=AssertionError(f"{name} 호출 금지")))
    broker = MagicMock()
    broker.get_order_history.return_value = []
    with pytest.raises(trading_bot.SymbolScopedError, match="미해결 정정 journal"):
        trading_bot.run_one_symbol(broker, _symbol_config("SOXL"))
    broker.place_order.assert_not_called()
    broker.cancel_order.assert_not_called()


def test_run_one_symbol_does_not_stop_other_symbols_or_clean_state(monkeypatch, tmp_path):
    import run_lock
    import trading_bot
    rp.write_journal(run_lock.journal_path(), [{"symbol": "SOXL", "old_odno": OLD, "qty": 7, "target_price": 73.69,
                                                "review_required": "REJECTED_LATE"}])
    assert not trading_bot._pending_amend_journal("TQQQ")
