"""
일일 정산 리포트(`src/settlement_report.py`) + trading_bot SETTLE 후크 계약 테스트.

검증 범위
1. 집계는 **델타만** (`qty_delta`/`amount_delta`) — 부분체결/지연체결/전체 재추정에서
   `cumulative_qty` 를 쓰면 중복 집계된다.
2. 사이클 종료(전량매도)에서 T 리셋과 매도 반영분이 그대로 보인다.
3. 커밋되지 않은 정산(보류·미확정)은 성공 리포트를 만들지 않는다.
4. 멱등: 같은 (broker, 계좌지문, 종목, 정산세션, 버전)은 1회만 발송된다.
5. 발송 실패는 정산 상태(T/순투입/원장)를 바꾸지 않고, 재시도 가능 상태로 남는다.
   - 실패한 리포트의 **내용(payload)** 은 state 에 보존되어, 델타 0인 재시도 실행에서도
     실제 반영분을 그대로 다시 보낸다 (`pending`).
6. 후크 예외는 SETTLE 실패 집계에 영향을 주지 않는다.
7. 정산 세션이 없는 리포트(예외/캘린더 미확인)도 **의도 세션에 바인딩**되어 같은 사유가
   날짜를 넘어 억제되지 않는다 (세션 미상이면 UTC 날짜 버킷).
8. 계좌 지문이 없으면 발송/기록 없이 건너뛴다 (계좌 간 멱등 키 공유 방지).
9. 요약/본문 표기: 전체 재추정 포함 여부, 금액 일부 추정 여부.

네트워크/브로커/실제 state 파일은 사용하지 않습니다.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
for path in (_repo_root, _src_path):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import settlement_report as sr
import state as state_module
import trading_bot

SESSION = "2026-10-29"


@pytest.fixture(autouse=True)
def _report_env(tmp_path, monkeypatch):
    """전역 잠금/작업 디렉터리 격리 — 저장소 루트에 산출물을 만들지 않습니다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.delenv("RUN_LOCK_HELD", raising=False)
    monkeypatch.delenv("RUN_LOCK_FD", raising=False)


def _settled(
    symbol="TQQQ",
    *,
    exchange="NAS",
    evidence=None,
    T=0.0,
    state=None,
    halt_reason="",
    run_id="run-test",
    session=SESSION,
):
    return SimpleNamespace(
        symbol=symbol,
        exchange=exchange,
        settled_session=session,
        settlement_run_id=run_id,
        evidence_path="",
        state=state if state is not None else {},
        T=T,
        evidence=evidence,
        halt_reason=halt_reason,
    )


def _evidence(**overrides):
    payload = {
        "orders": [],
        "T_before": 0.0,
        "T_after": 0.0,
        "net_invested_before": 0.0,
        "net_invested_after": 0.0,
        "balance_qty": 0,
        "pending_amount_odnos": [],
        "pending_marker_odnos": [],
        "skipped_fills": [],
        "post_processing": {},
    }
    payload.update(overrides)
    return payload


def _seed_state(symbol="TQQQ", **overrides):
    """state 파일에 종목 레코드를 만들어 둡니다 (발송 상태 기록 대상)."""
    record = {
        "T": 1.0,
        "last_updated": "2026-10-29T10:00:00+00:00",
        "net_invested": 100.0,
        "net_invested_status": "valid",
        "orders_meta": {},
    }
    record.update(overrides)
    state_module.save_state(symbol, record)
    return record


def _read_state_file():
    return json.loads(Path(state_module._STATE_FILE).read_text(encoding="utf-8"))


# ── 1. 델타 집계 ──────────────────────────────────────────────────
class TestDeltaTotals:
    def test_partial_fill_counts_only_this_run_delta(self):
        """누적 7주 중 이번에 새로 반영된 2주만 집계된다 (cumulative_qty 사용 금지)."""
        settled = _settled(
            evidence=_evidence(
                orders=[
                    {
                        "odno": "A1",
                        "side": "매수",
                        "qty_delta": 2,
                        "amount_delta": 20.0,
                        "cumulative_qty": 7,
                        "prev_cumulative_qty": 5,
                    }
                ],
                T_before=1.0,
                net_invested_before=100.0,
            ),
            T=1.5,
            state={"net_invested": 120.0},
        )
        report = sr.build_symbol_report(settled, committed=True)

        assert report.buy_qty == 2, "cumulative_qty(7)가 아니라 델타(2)여야 합니다"
        assert report.buy_amount == 20.0
        assert report.sell_qty == 0
        assert report.t_before == 1.0 and report.t_after == 1.5
        assert report.net_before == 100.0 and report.net_after == 120.0

    def test_late_fill_is_counted_and_flagged(self):
        """지연 체결(부분체결 증가)도 델타로 집계되고 별도 건수로 표시된다."""
        settled = _settled(
            evidence=_evidence(
                orders=[
                    {
                        "odno": "A1",
                        "side": "매수",
                        "qty_delta": 3,
                        "amount_delta": 30.0,
                        "cumulative_qty": 3,
                        "late": True,
                    },
                    {
                        "odno": "A2",
                        "side": "매도",
                        "qty_delta": 1,
                        "amount_delta": 12.5,
                        "cumulative_qty": 2,
                        "late": True,
                    },
                ],
                T_before=2.0,
            ),
            T=2.4,
            state={"net_invested": 117.5},
        )
        report = sr.build_symbol_report(settled, committed=True)
        message = sr.format_report_message(sr.build_daily_report([report]))

        assert (report.buy_qty, report.buy_amount) == (3, 30.0)
        assert (report.sell_qty, report.sell_amount) == (1, 12.5)
        assert report.late_fill_count == 2
        assert "지연 체결 2건 반영" in message

    def test_full_reinference_flagged_and_never_uses_cumulative(self):
        """초기 전체 재추정은 전 기간 델타를 합산하되 '전체 재추정'으로 표시한다."""
        settled = _settled(
            evidence=_evidence(
                orders=[
                    {
                        "odno": "A1",
                        "side": "매수",
                        "qty_delta": 10,
                        "amount_delta": 500.0,
                        "cumulative_qty": 10,
                        "full_reinference": True,
                        "t_target": 1.0,
                        "total_qty": 10,
                    },
                    {
                        "odno": "A2",
                        "side": "매수",
                        "qty_delta": 5,
                        "amount_delta": 260.0,
                        "cumulative_qty": 5,
                        "full_reinference": True,
                        "is_additional": True,
                    },
                ],
                T_before=0.0,
            ),
            T=1.0,
            state={"net_invested": 760.0},
        )
        report = sr.build_symbol_report(settled, committed=True)
        message = sr.format_report_message(sr.build_daily_report([report]))

        assert report.buy_qty == 15
        assert report.buy_amount == 760.0
        assert report.full_reinference is True
        assert "전체 재추정" in message

    def test_cycle_end_sell_and_t_reset(self):
        """사이클 종료(전량매도) — 매도 반영분과 T 0 리셋이 최종 후보 기준으로 보인다."""
        settled = _settled(
            evidence=_evidence(
                orders=[
                    {
                        "odno": "S1",
                        "side": "매도",
                        "qty_delta": 40,
                        "amount_delta": 4400.0,
                        "cumulative_qty": 40,
                        "factor": 0.0,
                        "t_delta": -4.0,
                    }
                ],
                T_before=4.0,
                net_invested_before=4000.0,
                post_processing={"final_candidate": {"T": 0.0, "net_invested": 0.0}},
                balance_qty=0,
            ),
            T=0.0,
            state={"net_invested": 0.0, "position_qty": 0},
        )
        report = sr.build_symbol_report(settled, committed=True)
        message = sr.format_report_message(sr.build_daily_report([report]))

        assert report.sell_qty == 40
        assert report.sell_amount == 4400.0
        assert report.t_before == 4.0 and report.t_after == 0.0
        assert report.net_after == 0.0
        assert "T 4 → 0" in message

    def test_message_never_claims_previous_day_fill(self):
        """`ord_dt` 는 주문일이라 '전일 체결'로 단정하지 않는다."""
        settled = _settled(
            evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}]),
            T=1.0,
        )
        message = sr.format_report_message(sr.build_daily_report([sr.build_symbol_report(settled, committed=True)]))
        assert "전일" not in message
        assert "이번 정산 반영분" in message

    def test_empty_orders_is_zero_not_error(self):
        report = sr.build_symbol_report(_settled(evidence=_evidence()), committed=True)
        assert report.buy_qty == 0 and report.buy_amount == 0.0
        assert report.sell_qty == 0 and report.sell_amount == 0.0


# ── 2. 보류/미확정 → 성공 리포트 금지 ────────────────────────────
class TestHoldSuppression:
    def test_halt_reason_produces_hold_only(self):
        settled = _settled(symbol="SOXL", exchange="AMS", halt_reason="balance_unavailable")
        report = sr.build_symbol_report(settled, committed=False)
        message = sr.format_report_message(sr.build_daily_report([report]))

        assert report.is_hold is True
        assert report.outcome == "hold"
        assert "보류" in message
        assert "신규 주문 차단" in message
        section = sr._format_symbol_section(report)
        assert "이번 정산 반영분" not in section
        assert "반영 완료" not in section

    def test_uncommitted_evidence_is_not_success(self):
        """DRY/보류/승격 실패(committed=false)는 성공 리포트가 아니다."""
        settled = _settled(evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}]))
        report = sr.build_symbol_report(settled, committed=False)
        assert report.is_hold is True
        assert report.buy_qty == 0, "커밋되지 않은 정산의 반영분을 성공처럼 표시하면 안 됩니다"
        assert sr.REASON_UNCOMMITTED[:6] in report.halt_reason

    def test_settlement_exception_reports_reason(self):
        report = sr.build_symbol_report(None, symbol="TQQQ", exchange="NAS", error="boom")
        assert report.is_hold is True
        assert "boom" in report.halt_reason

    def test_summary_counts_completed_and_hold(self):
        ok = sr.build_symbol_report(
            _settled(evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}])),
            committed=True,
        )
        held = sr.build_symbol_report(_settled(symbol="SOXL", halt_reason="history_not_settled"))
        daily = sr.build_daily_report([ok, held], now=None)

        assert daily["completed_count"] == 1
        assert daily["hold_count"] == 1
        assert daily["buy_qty"] == 2
        message = sr.format_report_message(daily)
        assert "완료 1 / 보류 1" in message


# ── 3. 멱등 식별자 & 중복 방지 ───────────────────────────────────
class TestDedup:
    def test_report_id_components_matter(self):
        base = sr.report_id("kis", "hmac:1", "TQQQ", SESSION)
        assert base == sr.report_id("KIS", "hmac:1", "tqqq", SESSION)  # 정규화
        assert base != sr.report_id("kis", "hmac:2", "TQQQ", SESSION)
        assert base != sr.report_id("ls", "hmac:1", "TQQQ", SESSION)
        assert base != sr.report_id("kis", "hmac:1", "SOXL", SESSION)
        assert base != sr.report_id("kis", "hmac:1", "TQQQ", "2026-10-28")

    def _report(self, symbol="TQQQ"):
        settled = _settled(
            symbol=symbol,
            evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}], T_before=1.0),
            T=1.5,
            state={"net_invested": 120.0},
        )
        return sr.build_symbol_report(settled, committed=True, broker="kis", account_fingerprint="hmac:acct")

    def test_same_session_sent_once(self):
        _seed_state("TQQQ")
        calls = []

        def fake_notify(message):
            calls.append(message)
            return True

        first = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=fake_notify,
        )
        second = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=fake_notify,
        )

        assert first["sent"] == ["TQQQ"]
        assert second["skipped"] == ["already_sent"]
        assert len(calls) == 1, "같은 정산 세션은 1회만 발송해야 합니다"
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert len(store["sent"]) == 1

    def test_next_session_sends_again(self):
        _seed_state("TQQQ")
        calls = []
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        next_session = sr.build_symbol_report(
            _settled(
                evidence=_evidence(orders=[{"odno": "A2", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}]),
                session="2026-10-30",
            ),
            committed=True, broker="kis", account_fingerprint="hmac:acct",
        )
        result = sr.send_settlement_report(
            [next_session], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["TQQQ"]
        assert len(calls) == 2

    def test_different_account_sends_again(self):
        _seed_state("TQQQ")
        calls = []

        def notify_ok(message):
            calls.append(message)
            return True

        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=notify_ok,
        )
        other_account = sr.build_symbol_report(
            _settled(
                evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}]),
            ),
            committed=True, broker="kis", account_fingerprint="hmac:other",
        )
        result = sr.send_settlement_report(
            [other_account], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:other", notify_func=notify_ok,
        )
        assert result["sent"] == ["TQQQ"], "다른 계좌 지문은 별개 리포트입니다"

    def test_hold_sent_once_then_success_still_sends(self):
        """보류 알림은 성공 리포트를 막지 않는다 (보류→다음 정산 성공 시나리오)."""
        _seed_state("TQQQ")
        calls = []
        held = sr.build_symbol_report(
            _settled(halt_reason="after_hours_not_ended"),
            committed=False, broker="kis", account_fingerprint="hmac:acct",
        )
        first = sr.send_settlement_report(
            [held], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert first["sent"] == ["TQQQ"]

        again = sr.send_settlement_report(
            [sr.build_symbol_report(_settled(halt_reason="after_hours_not_ended"), committed=False,
                                    broker="kis", account_fingerprint="hmac:acct")],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert again["skipped"] == ["already_sent"], "같은 사유의 보류 알림은 반복하지 않습니다"

        success = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert success["sent"] == ["TQQQ"], "보류 뒤 커밋된 정산은 성공 리포트를 보내야 합니다"
        assert len(calls) == 2

    def test_dry_mode_never_sends(self):
        calls = []
        result = sr.send_settlement_report(
            [self._report()], trade_mode="DRY", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["skipped"] == ["not_live"]
        assert calls == []

    def test_already_sent_symbol_is_not_rendered_again(self):
        """같은 세션에서 새 종목만 추가 발송될 때 기존 종목 본문을 반복하지 않습니다."""
        old = self._report("TQQQ")
        old.already_sent = True
        new = self._report("SOXL")
        message = sr.format_report_message(sr.build_daily_report([old, new]))

        assert "■ SOXL" in message
        assert "■ TQQQ" not in message
        assert "이미 발송된 종목 1건" in message
        assert "완료 2" in message, "요약은 이번 실행 전체 기준입니다"

    def test_missing_telegram_config_skips_without_state_write(self):
        """텔레그램 미설정이면 발송도, 발송 상태 기록도 하지 않는다."""
        _seed_state("TQQQ")
        before = _read_state_file()
        result = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct"
        )
        assert result["skipped"] == ["telegram_not_configured"]
        assert _read_state_file() == before


# ── 4. 발송 실패가 정산에 영향을 주지 않는다 ─────────────────────
class TestSendFailureIsolation:
    def _report(self):
        settled = _settled(
            evidence=_evidence(
                orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}],
                T_before=3.0,
            ),
            T=3.5,
            state={"net_invested": 300.0, "position_qty": 12},
        )
        return sr.build_symbol_report(settled, committed=True, broker="kis", account_fingerprint="hmac:acct")

    def test_send_exception_leaves_settlement_state_untouched(self):
        record = _seed_state("TQQQ", T=3.5, net_invested=300.0, position_qty=12)

        def exploding_notify(message):
            raise RuntimeError("telegram down")

        result = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=exploding_notify,
        )
        assert result["failed"] == ["TQQQ"]

        saved = _read_state_file()["TQQQ"]
        assert saved["T"] == record["T"]
        assert saved["net_invested"] == record["net_invested"]
        assert saved["position_qty"] == record["position_qty"]
        assert saved["orders_meta"] == record["orders_meta"]
        store = saved[sr.REPORT_STATE_KEY]
        assert store["sent"] == {}, "실패한 발송은 '발송됨'으로 기록하면 안 됩니다"
        assert store["last_failure"]["retryable"] is True

    def test_false_return_is_retryable(self):
        _seed_state("TQQQ")
        calls = []
        first = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert first["failed"] == ["TQQQ"]
        second = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert second["sent"] == ["TQQQ"], "실패한 발송은 다음 실행에서 재시도되어야 합니다"
        assert len(calls) == 1

    def test_state_write_failure_is_swallowed(self, monkeypatch):
        _seed_state("TQQQ")

        def broken_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(state_module, "save_states", broken_save)
        calls = []
        result = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["TQQQ"], "발송은 성공했으므로 정산 흐름은 계속됩니다"
        assert len(calls) == 1


# ── 4-1. 미발송 내용 보존(재시도) ──────────────────────────────────
class TestRetryPayload:
    """발송 실패 후 재시도는 **실제 정산 내용**을 보내야 합니다.

    정산은 이미 커밋됐으므로 재시도 실행의 리포트는 델타가 0입니다. 내용을 저장하지
    않으면 "재시도 성공"만 남고 운영자는 반영분을 영영 못 봅니다.
    """

    def _committed_report(self, session=SESSION):
        settled = _settled(
            evidence=_evidence(
                orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}],
                T_before=3.0,
            ),
            T=3.5,
            state={"net_invested": 320.0, "position_qty": 14},
            session=session,
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def _empty_retry_report(self, session=SESSION):
        """재시도 실행에서 실제로 만들어지는 리포트 — 델타 0(빈 리포트)."""
        settled = _settled(
            evidence=_evidence(T_before=3.5),
            T=3.5,
            state={"net_invested": 320.0, "position_qty": 14},
            session=session,
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def test_failed_report_content_is_stored_then_resent(self):
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        first = sr.send_settlement_report(
            [self._committed_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert first["failed"] == ["TQQQ"]

        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["sent"] == {}, "실패는 발송됨으로 기록되면 안 됩니다"
        pending = list(store["pending"].values())
        assert len(pending) == 1, "미발송 payload 가 state 에 남아야 합니다"
        assert pending[0]["payload"]["buy_qty"] == 2
        assert pending[0]["payload"]["buy_amount"] == 20.0
        assert pending[0]["attempts"] == 1

        calls = []
        second = sr.send_settlement_report(
            [self._empty_retry_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert second["sent"] == ["TQQQ"]
        assert second["retried"] == ["TQQQ"], "재전송임을 결과에 남깁니다"
        assert len(calls) == 1
        message = calls[0]
        assert "매수 2주 / $20.00" in message, "재시도는 1차 실패분의 실제 반영분을 보냅니다"
        assert "1차 발송 실패분 재전송" in message, "재전송임을 본문에도 표시합니다"

        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}, "발송 성공 후 미발송 payload 는 지웁니다"
        assert list(store["sent"].values())[0]["retried"] is True

    def test_second_failure_keeps_content_and_counts_attempts(self):
        _seed_state("TQQQ")
        for _ in range(2):
            sr.send_settlement_report(
                [self._committed_report()], trade_mode="LIVE", broker="kis",
                account_fingerprint="hmac:acct", notify_func=lambda m: False,
            )
        pending = list(_read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]["pending"].values())
        assert len(pending) == 1
        assert pending[0]["attempts"] == 2
        assert pending[0]["payload"]["buy_qty"] == 2, "실패가 반복돼도 내용은 보존됩니다"

    def test_hold_failure_stores_no_pending(self):
        """보류 알림은 재시도 때 같은 사유로 재생성되므로 payload 를 저장하지 않습니다."""
        _seed_state("TQQQ")
        held = sr.build_symbol_report(
            _settled(halt_reason="after_hours_not_ended"),
            committed=False, broker="kis", account_fingerprint="hmac:acct",
        )
        sr.send_settlement_report(
            [held], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}
        assert store["last_failure"]["pending_saved"] is False
        assert store["last_failure"]["retryable"] is True, "last_failure 는 감사용 재시도 표식"

    def test_pending_does_not_override_hold(self):
        """1차 실패분(pending)이 있어도 이번 실행이 보류면 성공 리포트로 되살리지 않습니다."""
        _seed_state("TQQQ")
        sr.send_settlement_report(
            [self._committed_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        held = sr.build_symbol_report(
            _settled(halt_reason="history_not_settled"),
            committed=False, broker="kis", account_fingerprint="hmac:acct",
        )
        calls = []
        result = sr.send_settlement_report(
            [held], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["TQQQ"]
        assert "보류 (신규 주문 차단)" in calls[0]
        assert "반영 완료" not in calls[0], "보류를 성공 리포트로 되살리면 안 됩니다"
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert len(store["pending"]) == 1, "보류 발송은 미발송 payload 를 지우지 않습니다"


    def test_corrupt_pending_falls_back_to_current_report(self):
        """손상된 payload 로 엉뚱한 종목/보류 화면이 나가면 안 됩니다."""
        _seed_state("TQQQ")
        sr.send_settlement_report(
            [self._committed_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        key = list(store["pending"])[0]
        store["pending"][key]["payload"]["symbol"] = "SOXL"  # 손상 시뮬레이션
        state_module.save_state("TQQQ", {
            "T": 3.5, "last_updated": "2026-10-29T10:00:00+00:00",
            "net_invested": 320.0, "net_invested_status": "valid", "orders_meta": {},
            sr.REPORT_STATE_KEY: store,
        })

        calls = []
        result = sr.send_settlement_report(
            [self._empty_retry_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["TQQQ"]
        assert "■ SOXL" not in calls[0], "손상 payload 의 종목으로 그리면 안 됩니다"
        assert "반영 완료" in calls[0]


# ── 4-2. 정산 세션 바인딩 (날짜 넘는 억제 방지) ──────────────────
class TestSessionBinding:
    def _exception_report(self, session=None, now=None):
        """정산 예외(`settled=None`) 리포트 — 결과 객체에 정산 세션이 없습니다."""
        return sr.build_symbol_report(
            None, symbol="TQQQ", exchange="NAS", error="boom",
            broker="kis", account_fingerprint="hmac:acct",
            settled_session=session, now=now,
        )

    def test_exception_hold_is_reported_each_session(self):
        _seed_state("TQQQ")
        calls = []

        def notify_ok(message):
            calls.append(message)
            return True

        day1 = sr.send_settlement_report(
            [self._exception_report("2026-10-28")], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=notify_ok,
        )
        day2 = sr.send_settlement_report(
            [self._exception_report("2026-10-29")], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=notify_ok,
        )
        assert day1["sent"] == ["TQQQ"] and day2["sent"] == ["TQQQ"]
        assert len(calls) == 2, "같은 사유라도 정산 세션이 다르면 다시 알립니다"

    def test_same_exception_reason_same_session_is_suppressed(self):
        _seed_state("TQQQ")
        calls = []
        sr.send_settlement_report(
            [self._exception_report("2026-10-29")], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        again = sr.send_settlement_report(
            [self._exception_report("2026-10-29")], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert again["skipped"] == ["already_sent"]
        assert len(calls) == 1

    def test_unbound_sessions_would_collide_but_are_bound(self):
        """세션을 안 넘기면(구 동작) 예외 리포트 키가 서로 같아 억제됐습니다."""
        bound = self._exception_report("2026-10-28")
        bound_next = self._exception_report("2026-10-29")
        assert bound.report_id != bound_next.report_id
        assert bound.session_key == "2026-10-28"

        # 세션을 모르면(캘린더 미확인) 날짜 버킷으로 하루 단위 억제합니다.
        day1 = self._exception_report(now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
        day2 = self._exception_report(now=datetime(2026, 10, 29, 12, tzinfo=timezone.utc))
        assert day1.session_key == "unknown:2026-10-28"
        assert day1.report_id != day2.report_id, "날짜가 바뀌면 다시 알립니다"
        assert day1.settled_session == "", "표시용 세션은 '미확인'으로 남깁니다"

    def test_calendar_unavailable_hold_binds_intended_session(self):
        """`session_calendar_unavailable` 는 settled_session="" 이라 바인딩이 필요합니다."""
        settled = _settled(halt_reason="session_calendar_unavailable", session="")
        report = sr.build_symbol_report(
            settled, committed=False, broker="kis", account_fingerprint="hmac:acct",
            settled_session="2026-10-29",
        )
        assert report.settled_session == ""
        assert report.session_key == "2026-10-29"
        assert report.report_id == sr.report_id("kis", "hmac:acct", "TQQQ", "2026-10-29")

    def test_actual_settled_session_wins_over_hint(self):
        settled = _settled(halt_reason="history_not_settled", session="2026-10-28")
        report = sr.build_symbol_report(
            settled, committed=False, broker="kis", account_fingerprint="hmac:acct",
            settled_session="2026-10-29",
        )
        assert report.session_key == "2026-10-28", "실제 정산 세션이 우선입니다"


# ── 4-3. 계좌 지문 누락 ──────────────────────────────────────────
class TestAccountFingerprint:
    def test_missing_fingerprint_skips_and_writes_nothing(self):
        """지문이 비면 계좌 구분이 사라져 다른 계좌 리포트를 억제합니다 → 발송 금지."""
        _seed_state("TQQQ")
        before = _read_state_file()
        calls = []
        report = sr.build_symbol_report(
            _settled(evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}])),
            committed=True, broker="kis", account_fingerprint="",
        )
        result = sr.send_settlement_report(
            [report], trade_mode="LIVE", broker="kis", account_fingerprint="",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["skipped"] == ["account_fingerprint_missing"]
        assert calls == []
        assert _read_state_file() == before


# ── 4-4. 요약 표기 (전체 재추정 / 금액 추정) ─────────────────────
class TestSummaryNotes:
    def test_summary_declares_full_reinference(self):
        settled = _settled(
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": 15,
                    "amount_delta": 760.0, "full_reinference": True,
                }],
            ),
            T=1.0,
        )
        report = sr.build_symbol_report(settled, committed=True)
        daily = sr.build_daily_report([report])
        message = sr.format_report_message(daily)

        assert daily["full_reinference_symbols"] == ["TQQQ"]
        assert "전체 재추정 포함: TQQQ" in message, "합계가 하루치처럼 보이지 않게 요약에 명시"
        assert "전 기간 반영분이 합계에 포함" in message

    def test_legacy_marker_amount_is_marked_as_estimated(self):
        """반영금액 표식이 없는 레거시 메타의 델타 금액은 추정치일 수 있습니다."""
        settled = _settled(
            evidence=_evidence(
                orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}],
                orders_meta_before={"A1": {"processed_filled_qty": 0}},
            ),
            T=1.0,
        )
        report = sr.build_symbol_report(settled, committed=True)
        message = sr.format_report_message(sr.build_daily_report([report]))

        assert report.estimated_amount_count == 1
        assert "일부 추정" in message
        assert "확정 금액 아님" in message

    def test_fill_flagged_estimate_is_marked(self):
        settled = _settled(
            evidence=_evidence(
                orders=[{"odno": "A9", "side": "매도", "qty_delta": 1, "amount_delta": 12.0}],
                orders_meta_before={"A9": {"processed_filled_amount": 0.0}},
                fills=[{"odno": "A9", "amount_estimated": True}],
            ),
            T=1.0,
        )
        report = sr.build_symbol_report(settled, committed=True)
        assert report.estimated_amount_count == 1

    def test_confirmed_amount_is_not_marked(self):
        settled = _settled(
            evidence=_evidence(
                orders=[{"odno": "A1", "side": "매수", "qty_delta": 2, "amount_delta": 20.0}],
                orders_meta_before={"A1": {"processed_filled_amount": 0.0}},
                fills=[{"odno": "A1", "amount_estimated": False, "amount_confirmed": True}],
            ),
            T=1.0,
        )
        report = sr.build_symbol_report(settled, committed=True)
        message = sr.format_report_message(sr.build_daily_report([report]))
        assert report.estimated_amount_count == 0
        assert "일부 추정" not in message


# ── 4-5. 조용한 시간 (기본 발송자는 urgent) ──────────────────────
class TestQuietHours:
    def test_default_sender_is_urgent(self, monkeypatch):
        """조용한 시간 건너뜀(False)을 실패로 오인하지 않도록 urgent 로 보냅니다."""
        _seed_state("TQQQ")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        captured = {}

        def fake_notify(message, **kwargs):
            captured["message"] = message
            captured.update(kwargs)
            return True

        monkeypatch.setattr(sr, "notify", fake_notify)
        report = sr.build_symbol_report(
            _settled(evidence=_evidence(orders=[{"odno": "A1", "side": "매수", "qty_delta": 1, "amount_delta": 10.0}])),
            committed=True, broker="kis", account_fingerprint="hmac:acct",
        )
        result = sr.send_settlement_report(
            [report], trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct"
        )
        assert result["sent"] == ["TQQQ"]
        assert captured.get("urgent") is True, "조용한 시간에 건너뛰면 재시도가 무한 반복됩니다"


# ── 5. state 영속화 (캐시 정책 정합) ─────────────────────────────
class TestStatePersistence:
    def test_record_persists_report_key(self):
        record = state_module._state_record({"T": 0.0, sr.REPORT_STATE_KEY: {"sent": {"x": {"at": "t"}}}})
        assert record[sr.REPORT_STATE_KEY]["sent"]["x"]["at"] == "t"

    def test_report_key_not_in_canonical_hash(self):
        """발송 기록 때문에 복구용 CAS 지문이 흔들리면 안 됩니다."""
        state = {"T": 1.0, "net_invested": 100.0}
        before = state_module.canonical_state_hash(state)
        state[sr.REPORT_STATE_KEY] = {"sent": {"abc": {"at": "2026-10-29T10:00:00Z"}}}
        assert state_module.canonical_state_hash(state) == before

    def test_store_is_pruned(self):
        store = {"sent": {}}
        for i in range(sr.MAX_SENT_ENTRIES + 5):
            store = sr._apply_mark(store, SimpleNamespace(
                symbol="TQQQ", settled_session=SESSION, broker="kis", halt_reason="",
                is_hold=False, report_id=f"id{i:02d}",
            ), ok=True, at_iso=f"2026-10-29T10:{i:02d}:00Z")
        assert len(store["sent"]) == sr.MAX_SENT_ENTRIES

    def test_unknown_symbol_state_is_not_created(self):
        """state 에 없는 종목은 새 레코드를 만들지 않습니다 (파일 부풀리기 방지)."""
        report = sr.build_symbol_report(
            _settled(symbol="NEW", evidence=_evidence()), committed=True,
            broker="kis", account_fingerprint="hmac:acct",
        )
        sr.send_settlement_report(
            [report], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )
        data = _read_state_file() if Path(state_module._STATE_FILE).exists() else {}
        assert "NEW" not in data


# ── 6. trading_bot SETTLE 후크 ───────────────────────────────────
class TestSettleHook:
    def _entry(self, settled=None, symbol="TQQQ", error=""):
        return {"symbol": symbol, "exchange": "NAS", "settled": settled, "error": error}

    def test_live_hook_masks_odno_and_builds_reports(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(
            trading_bot, "_broker_identity",
            lambda: {"broker": "kis", "account_id": "hmac:acct", "account_display": "****1234"},
        )
        monkeypatch.setattr(
            trading_bot, "send_settlement_report",
            lambda reports, **kwargs: captured.setdefault("reports", reports),
        )
        settled = _settled(halt_reason="orders_meta 주문 odno=4615 이 이력에 없습니다")
        trading_bot._notify_settlement_report([self._entry(settled)])

        report = captured["reports"][0]
        assert "4615" not in report.halt_reason
        assert "ord#" in report.halt_reason

    def test_dry_mode_hook_skips(self, monkeypatch):
        called = []
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "DRY")
        monkeypatch.setattr(trading_bot, "send_settlement_report", lambda *a, **k: called.append(a))
        trading_bot._notify_settlement_report([self._entry(_settled(evidence=_evidence()))])
        assert called == []

    def test_committed_flag_requires_committed_evidence(self, monkeypatch, tmp_path):
        """커밋된 근거(committed=true, run_id 일치)만 성공 리포트 대상입니다."""
        committed_path = tmp_path / "settlement-TQQQ-1.json"
        committed_path.write_text(
            json.dumps({"run_id": "run-1", "committed": True}), encoding="utf-8"
        )
        not_committed = tmp_path / "settlement-TQQQ-2.json"
        not_committed.write_text(
            json.dumps({"run_id": "run-2", "committed": False}), encoding="utf-8"
        )
        settled_ok = _settled(run_id="run-1")
        settled_ok.evidence_path = str(committed_path)
        settled_bad = _settled(run_id="run-2")
        settled_bad.evidence_path = str(not_committed)
        mismatched = _settled(run_id="run-3")
        mismatched.evidence_path = str(committed_path)

        assert trading_bot._settlement_evidence_committed(settled_ok) is True
        assert trading_bot._settlement_evidence_committed(settled_bad) is False
        assert trading_bot._settlement_evidence_committed(mismatched) is False

    def test_identity_failure_skips_send(self, monkeypatch):
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        called = []

        def raise_identity():
            raise RuntimeError("ACCOUNT_FINGERPRINT_SECRET 없음")

        monkeypatch.setattr(trading_bot, "_broker_identity", raise_identity)
        monkeypatch.setattr(trading_bot, "send_settlement_report", lambda *a, **k: called.append(a))
        trading_bot._notify_settlement_report([self._entry(_settled(evidence=_evidence()))])
        assert called == []

    def test_non_trading_day_is_not_reported(self, monkeypatch):
        """휴장은 '정산 보류'가 아니므로 리포트/알림 대상이 아닙니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        called = []
        monkeypatch.setattr(
            trading_bot, "_broker_identity",
            lambda: {"broker": "kis", "account_id": "hmac:acct"},
        )
        monkeypatch.setattr(
            trading_bot, "send_settlement_report",
            lambda reports, **kwargs: called.append(list(reports)),
        )
        trading_bot._notify_settlement_report(
            [self._entry(_settled(halt_reason="non_trading_day"))]
        )
        assert called == [[]], "휴장 종목은 리포트 목록에 들어가면 안 됩니다"

    def test_exception_entry_binds_intended_session(self, monkeypatch):
        """정산 예외(settled=None)도 후크가 의도한 정산 세션을 붙여 억제 키를 만듭니다."""
        monkeypatch.setattr(trading_bot, "TRADE_MODE", "LIVE")
        monkeypatch.setattr(
            trading_bot, "_broker_identity",
            lambda: {"broker": "kis", "account_id": "hmac:acct"},
        )
        monkeypatch.setattr(
            trading_bot, "settlement_sessions", lambda *a, **k: ("2026-10-29", "2026-10-30")
        )
        captured = {}

        def capture(reports, **kwargs):
            captured.setdefault("reports", []).extend(reports)
            captured.update(kwargs)

        monkeypatch.setattr(trading_bot, "send_settlement_report", capture)
        trading_bot._notify_settlement_report([self._entry(None, error="boom")])

        report = captured["reports"][0]
        assert report.settled_session == ""
        assert report.session_key == "2026-10-29", "결과 객체에 세션이 없어도 바인딩해야 합니다"
        assert captured["settled_session"] == "2026-10-29"

    def test_hook_exception_does_not_change_settle_failures(self, monkeypatch, tmp_path):
        """발송 예외는 SETTLE 실패 집계에 영향을 주지 않습니다."""
        monkeypatch.setenv("RUN_PHASE", "SETTLE")
        monkeypatch.setattr(trading_bot, "SYMBOLS", [{"symbol": "TQQQ", "exchange": "NAS", "splits": 20, "symbol_type": "TQQQ", "seed": 100.0}])
        monkeypatch.setattr(
            trading_bot, "settle_positions",
            lambda broker, config: _settled(halt_reason="history_not_settled"),
        )

        def explode(entries):
            raise RuntimeError("telegram client crashed")

        monkeypatch.setattr(trading_bot, "_notify_settlement_report", explode)
        assert trading_bot._dispatch_pipeline_phase("SETTLE", _blank_broker()) == 1


class TestEvidenceContract:
    """실제 evidence 생성기(`build_settlement_evidence`)의 산출물이 그대로 집계됩니다."""

    def test_evidence_orders_drive_report_totals(self):
        state = {
            "T": 1.5,
            "net_invested": 120.0,
            "position_qty": 7,
            "settlement_ledger": {"entries": [], "seq": 0, "base_t": 1.0},
            "_settlement_deltas": [
                {
                    "odno": "A1",
                    "side": "매수",
                    "qty_delta": 2,
                    "amount_delta": 20.0,
                    "cumulative_qty": 7,
                    "prev_cumulative_qty": 5,
                    "t_delta": 0.5,
                }
            ],
        }
        evidence = trading_bot.build_settlement_evidence(
            "TQQQ",
            state,
            [],
            mode="LIVE",
            run_id="run-x",
            balance_qty=7,
            before={"T": 1.0, "net_invested": 100.0},
        )
        report = sr.build_symbol_report(_settled(evidence=evidence), committed=True)

        assert report.buy_qty == 2, "누적 체결(7)이 아니라 이번 델타(2)여야 합니다"
        assert report.buy_amount == 20.0
        assert report.t_before == 1.0
        assert report.position_qty == 7
        assert report.net_before == 100.0
        assert report.net_after == 120.0


def _blank_broker():
    """SETTLE 디스패치가 요구하는 최소 인터페이스만 가진 가짜 브로커."""
    from unittest.mock import MagicMock

    broker = MagicMock()
    broker.name = "mockbroker"
    return broker
