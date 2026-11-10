"""메리츠 보정 BUY LOC 사후 재조정 테스트 (mock 전용, 주문·정정 호출 없음)."""
import os
import sys
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops"))

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


def _apply(broker, state, tmp_path, saved):
    item = _plan(state, _row())
    journal = str(tmp_path / "j.json")
    result = rp.apply_amend(broker, "SOXL", "NY", item, state, journal,
                            lambda s, st: saved.append(deepcopy(st)))
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
    assert rp.apply_amend(FakeBroker(), "SOXL", "NY", item, state, journal, boom) == "LINK_FAILED"
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
    monkeypatch.setattr(cli, "JOURNAL_PATH", str(tmp_path / "j.json"))
    monkeypatch.setattr(cli, "LOCK_PATH", str(tmp_path / "l.lock"))
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
    {"_oder_date": "20200101"}, {"status": "거부"}, {"_ordered_qty": 3},
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
    journal = tmp_path / "j.json"
    rp.write_journal(str(journal), [dict(_ENTRY)])
    before = journal.read_text()
    monkeypatch.setattr(cli, "JOURNAL_PATH", str(journal))
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
    run_lock = tmp_path / "run.lock"
    monkeypatch.setattr(cli, "RUN_LOCK_PATH", str(run_lock))
    held = open(run_lock, "w")
    fcntl.flock(held, fcntl.LOCK_EX)  # 런처가 쥐고 있는 상황
    broker = _ReadOnlyBroker()
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, ["--symbol", "SOXL", "--apply", "--confirm", "REPRICE SOXL"], broker)
    assert broker.calls == []
    held.close()
