"""'리버스 주문 이력 누락' 진단 출력 — 이력에 정말 없는지 / 있는데 날짜 대조에서 탈락했는지 구분합니다 (로그 전용)."""
from state import _missing_reverse_order_diag, reconcile_reverse_fills


def _meta(**over):
    meta = {"side": "BUY", "total_qty": 3, "processed_filled_qty": 0, "reverse_action": "buy", "reverse_day": 4,
            "cycle_id": "C1", "submitted_at": "20261117052153", "submitted_session": "2026-11-16"}
    meta.update(over)
    return meta


def test_diag_when_order_absent_from_history():
    line = _missing_reverse_order_diag("ord#abc", _meta(), [])
    assert "이력의 같은 주문번호 행: 0건 (없음)" in line
    assert "방향=BUY 수량=3 리버스일차=4" in line and "제출=20261117052153 세션=2026-11-16" in line
    assert "ord#abc" in line and "RAW-ODNO" not in line                    # 호출자가 준 가명 식별자만 출력


def test_diag_when_order_present_but_date_check_fails():
    rows = [{"odno": "RAW-ODNO-2", "ord_dt": "20260101", "ft_ccld_qty": "0"}]
    line = _missing_reverse_order_diag("ord#def", _meta(), rows)
    assert "같은 주문번호 행: 1건" in line and "ord_dt=20260101" in line and "날짜대조=탈락" in line


def test_reconcile_prints_diag_line_with_the_warning(capsys, monkeypatch):
    import config
    monkeypatch.setattr(config, "BROKER_MODE", "real")                     # 자동 만료 가정 경로를 타지 않게
    state = {"T": 12.0, "reverse_mode": {"active": True, "cycle_id": "C1", "day_count": 3},
             "orders_meta": {"MISSING-1": _meta()}}
    reconcile_reverse_fills(state, [{"odno": "OTHER", "ord_dt": "20261116", "ft_ccld_qty": "1"}])
    out = capsys.readouterr().out
    assert "리버스 주문 이력 누락" in out and "[진단] 누락 주문" in out and "MISSING-1" not in out
    assert state["reverse_mode"]["reconciliation_error"] == "reverse_order_history_missing"


def test_reconcile_collects_missing_orders_for_dry_probe_and_take_clears(monkeypatch):
    """누락 주문은 DRY 진단 조회가 읽어 가도록 메모리에만 모읍니다 (state 에는 쓰지 않음)."""
    import config
    import state as state_module
    monkeypatch.setattr(config, "BROKER_MODE", "real")
    state_module._DIAG_MISSING_REVERSE.clear()
    state = {"T": 12.0, "reverse_mode": {"active": True, "cycle_id": "C1", "day_count": 3},
             "orders_meta": {"M-1": _meta(), "M-2": _meta(submitted_at="20261117052155")}}
    reconcile_reverse_fills(state, [{"odno": "OTHER", "ord_dt": "20261116", "ft_ccld_qty": "1"}])
    assert "_DIAG_MISSING_REVERSE" not in str(state)                              # state 오염 없음
    items = state_module.take_missing_reverse_diag()
    assert sorted(i["odno"] for i in items) == ["M-1", "M-2"]
    assert all(i["session"] == "2026-11-16" for i in items)
    assert state_module.take_missing_reverse_diag() == []                         # 한 번 가져가면 비워짐
