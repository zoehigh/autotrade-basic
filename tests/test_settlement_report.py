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
10. **과거 세션 미발송분 이월 재전송**: 전날 발송 실패분은 다음날 리포트 키로 조회되지
    않으므로, 새 리포트 발송 전에 계좌·종목별 `pending` 을 훑어 **원래 정산 세션을 명시한
    별도 메시지**(오래된 세션부터)로 재전송해야 한다 (오늘 리포트와 메시지 분리).
11. **같은 세션 정정 알림**: 같은 세션 리포트를 이미 보냈더라도 뒤늦은 추가 체결로 내용이
    실질적으로 달라지면 `📌 정산 정정` 을 별도 발송하고 리비전/내용 해시를 기록한다.
    내용이 그대로면 기존처럼 억제한다.

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


# ── 4-1b. 과거 세션 미발송분 이월 재전송 (backlog) ────────────────
class TestPreviousSessionBacklog:
    """전날 발송 실패분은 다음날 리포트의 키로는 조회되지 않습니다.

    `pending` 키에 정산 세션이 들어 있어서, 같은 세션만 보는 `restore_pending()` 으로는
    절대 되살아나지 않습니다. 새 리포트를 보내기 전에 **계좌·종목별 과거 pending** 을
    직접 훑어 원래 세션을 명시한 별도 메시지로 재전송해야 합니다.
    """

    PREV = "2026-10-28"

    def _report(
        self,
        *,
        session=SESSION,
        symbol="TQQQ",
        qty=2,
        amount=20.0,
        T_before=3.0,
        T=3.5,
        net=320.0,
        position=14,
        account="hmac:acct",
    ):
        settled = _settled(
            symbol=symbol,
            evidence=_evidence(
                orders=[{"odno": "A1", "side": "매수", "qty_delta": qty, "amount_delta": amount}],
                T_before=T_before,
                balance_qty=position,
            ),
            T=T,
            state={"net_invested": net, "position_qty": position},
            session=session,
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint=account
        )

    def test_failed_report_is_resent_next_session(self):
        """전날 실패분이 다음날 **별도 메시지**로 재전송된다 (원래 세션 명시)."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        first = sr.send_settlement_report(
            [self._report(session=self.PREV)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert first["failed"] == ["TQQQ"]

        calls = []
        today = self._report(
            qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15
        )
        result = sr.send_settlement_report(
            [today], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert result["resent_previous"] == ["TQQQ"], "전날 미발송분이 재전송되어야 합니다"
        assert result["sent"] == ["TQQQ"]
        assert len(calls) == 2, "과거 재전송과 오늘 리포트는 서로 다른 메시지입니다"
        backlog_message, daily_message = calls
        assert backlog_message.startswith("📨 미발송 정산 재전송")
        assert "정산 세션: 2026-10-28 (원래 정산 세션)" in backlog_message
        assert "매수 2주 / $20.00" in backlog_message, "전날 실제 반영분을 보냅니다"
        assert "오늘 정산 아님" in backlog_message
        assert daily_message.startswith("📊 일일 정산 리포트")
        assert "정산 세션: 2026-10-29" in daily_message
        assert "매수 1주 / $10.00" in daily_message
        assert "매수 2주" not in daily_message, "오늘 리포트에 전날 내용이 섞이면 안 됩니다"

        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}, "재전송 성공 후 미발송 payload 는 정리됩니다"

    def test_multiple_sessions_are_sent_oldest_first(self):
        _seed_state("TQQQ")
        for session, qty in (("2026-10-26", 1), ("2026-10-27", 2)):
            sr.send_settlement_report(
                [self._report(session=session, qty=qty, amount=qty * 10.0)],
                trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct",
                notify_func=lambda m: False,
            )
        calls = []
        sr.send_settlement_report(
            [self._report(qty=3, amount=30.0)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        backlog_messages = [m for m in calls if m.startswith("📨")]
        assert len(backlog_messages) == 2
        assert "2026-10-26" in backlog_messages[0], "오래된 세션부터 재전송합니다"
        assert "2026-10-27" in backlog_messages[1]
        assert calls[-1].startswith("📊"), "오늘 리포트는 과거 재전송 뒤에 보냅니다"

    def test_other_account_pending_is_not_resent(self):
        """다른 계좌의 미발송분을 이 계좌 채널로 보내면 안 됩니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        sr.send_settlement_report(
            [self._report(session=self.PREV, account="hmac:other")], trade_mode="LIVE",
            broker="kis", account_fingerprint="hmac:other", notify_func=lambda m: False,
        )
        calls = []
        result = sr.send_settlement_report(
            [self._report(qty=1, amount=10.0)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["resent_previous"] == []
        assert len(calls) == 1, "오늘 리포트만 나갑니다"
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert len(store["pending"]) == 1, "다른 계좌의 미발송분은 건드리지 않습니다"

    def test_backlog_failure_keeps_pending_and_sends_today(self):
        """과거 재전송이 실패해도 오늘 리포트는 나가고, 내용은 다음 실행으로 이월됩니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        sr.send_settlement_report(
            [self._report(session=self.PREV)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        calls = []

        def flaky(message):
            calls.append(message)
            return message.startswith("📊")  # 오늘 리포트만 성공

        result = sr.send_settlement_report(
            [self._report(qty=1, amount=10.0)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=flaky,
        )
        assert result["resent_previous_failed"] == ["TQQQ"]
        assert result["sent"] == ["TQQQ"], "한 메시지 실패가 다른 메시지를 막지 않습니다"

        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        pending = list(store["pending"].values())
        assert len(pending) == 1, "과거 미발송분은 다음 실행에서 다시 시도합니다"
        assert pending[0]["attempts"] == 2
        assert pending[0]["session_key"] == self.PREV
        assert pending[0]["payload"]["buy_qty"] == 2

    def test_pending_is_not_resent_when_session_already_sent(self):
        """같은 세션 발송 성공 이력이 있으면 잔여 pending 은 재전송하지 않습니다."""
        store = {
            "sent": {"abc": {"at": "2026-10-28T10:00:00Z", "session_key": self.PREV}},
            "pending": {
                "abc": {
                    "at": "2026-10-28T09:00:00Z",
                    "symbol": "TQQQ",
                    "session_key": self.PREV,
                    "broker": "kis",
                    "account": "hmac:acct",
                    "payload": {"symbol": "TQQQ", "committed": True, "buy_qty": 2},
                }
            },
        }
        groups = sr.pending_backlog(
            store, "TQQQ", broker="kis", account_fingerprint="hmac:acct",
            current_keys=(SESSION,),
        )
        assert groups == []

    def test_legacy_pending_without_account_is_skipped(self):
        """계좌 구분이 없는(구버전) 항목은 다른 계좌 내용일 수 있어 재전송하지 않습니다."""
        store = {
            "pending": {
                "abc": {
                    "at": "2026-10-28T09:00:00Z",
                    "symbol": "TQQQ",
                    "session_key": self.PREV,
                    "payload": {"symbol": "TQQQ", "committed": True, "buy_qty": 2},
                }
            }
        }
        assert sr.pending_backlog(
            store, "TQQQ", broker="kis", account_fingerprint="hmac:acct",
            current_keys=(SESSION,),
        ) == []


# ── 4-1c. 같은 세션 정정 알림 (correction) ───────────────────────
class TestSameSessionCorrection:
    """같은 세션 리포트를 이미 보냈어도, 내용이 실질적으로 달라지면 알려야 합니다.

    뒤늦은 추가 체결(지연 체결)이 반영된 경우가 대표적입니다. 무조건 억제하면 그
    반영분을 영영 알릴 수 없고, 그대로 다시 보내면 중복처럼 보입니다.
    """

    def _report(self, *, symbol="TQQQ", qty=2, amount=20.0, T_before=3.0, T=3.5,
                net=320.0, position=14, late=False):
        settled = _settled(
            symbol=symbol,
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": qty,
                    "amount_delta": amount, "late": late,
                }],
                T_before=T_before,
                balance_qty=position,
            ),
            T=T,
            state={"net_invested": net, "position_qty": position},
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def test_late_fill_after_send_sends_correction(self):
        """발송 후 추가 체결이 반영되면 `📌 정산 정정` 이 별도로 나갑니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        calls = []
        first = sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert first["sent"] == ["TQQQ"]

        late = self._report(
            qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15, late=True
        )
        result = sr.send_settlement_report(
            [late], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert result["corrected"] == ["TQQQ"]
        assert result["sent"] == [], "정정은 일일 리포트가 아니라 별도 메시지입니다"
        assert len(calls) == 2
        correction = calls[1]
        assert correction.startswith("📌 정산 정정")
        assert "이미 발송된 세션의 추가 반영분" in correction
        assert "추가 반영분: 매수 1주 / $10.00" in correction, "직전 이후 추가분만 담습니다"
        assert "리비전: #2" in correction
        assert "지연 체결 1건" in correction

        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        entry = store["sent"][sr.report_id("kis", "hmac:acct", "TQQQ", SESSION)]
        assert entry["revision"] == 2, "정정은 리비전을 올려 기록합니다"
        assert entry["correction"] is True
        assert entry["delta_hash"] == sr.report_delta_hash(late), "내용 해시를 갱신합니다"

    def test_unchanged_content_is_suppressed_after_correction(self):
        """정정 뒤 반영분이 없고 확정 상태도 같으면 다시 알리지 않습니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        calls = []
        sender = lambda m: (calls.append(m), True)[1]
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=sender,
        )
        sr.send_settlement_report(
            [self._report(qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15)],
            trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct", notify_func=sender,
        )
        assert len(calls) == 2

        empty = self._report(
            qty=0, amount=0.0, T_before=3.75, T=3.75, net=330.0, position=15
        )
        result = sr.send_settlement_report(
            [empty], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=sender,
        )
        assert result["skipped"] == ["already_sent"]
        assert len(calls) == 2, "델타 0 재보고는 억제합니다"

    def test_correction_and_new_symbol_are_separate_messages(self):
        """정정 대상은 일일 리포트 본문에서 빠지고 정정 메시지로만 나갑니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        _seed_state("SOXL", T=1.0, net_invested=100.0, position_qty=5)
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )
        calls = []
        result = sr.send_settlement_report(
            [
                self._report(qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15),
                self._report(symbol="SOXL", qty=1, amount=5.0, T_before=1.0, T=1.5,
                             net=105.0, position=6),
            ],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["SOXL"]
        assert result["corrected"] == ["TQQQ"]
        daily, correction = calls
        assert "■ SOXL" in daily
        assert "■ TQQQ" not in daily, "정정 대상은 일일 리포트에 중복 표기하지 않습니다"
        assert "같은 세션 추가 반영분 1건" in daily
        assert "■ TQQQ" in correction

    def test_correction_failure_keeps_content_for_retry(self):
        """정정 알림 발송이 실패해도 내용이 사라지지 않고 다음 실행에서 재시도됩니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )
        late = self._report(
            qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15, late=True
        )
        failed = sr.send_settlement_report(
            [late], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert failed["corrections_failed"] == ["TQQQ"]
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert len(store["pending"]) == 1, "정정 내용이 state 에 보관됩니다"

        calls = []
        empty = self._report(
            qty=0, amount=0.0, T_before=3.75, T=3.75, net=330.0, position=15
        )
        result = sr.send_settlement_report(
            [empty], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["corrected"] == ["TQQQ"]
        assert "매수 1주 / $10.00" in calls[0], "보관해 둔 정정 내용을 그대로 보냅니다"
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}


# ── 4-1d. 보고 계층 P2: 합계 범위 · 정정 실패분 재발송 · pending 다중 보존 ──
class TestDailyTotalsScope:
    """일일 리포트 **합계**는 본문에 실제로 그린 종목만 더해야 합니다.

    본문에서 생략한 종목(이미 발송·정정 대상)을 합계에 넣으면 같은 세션 반영분이
    두 번 더해집니다 (본문 1회 + 합계 1회).
    """

    def _report(self, *, symbol="TQQQ", qty=2, amount=20.0, T_before=3.0, T=3.5,
                net=320.0, position=14, late=False, session=SESSION):
        settled = _settled(
            symbol=symbol,
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": qty,
                    "amount_delta": amount, "late": late,
                }],
                T_before=T_before,
                balance_qty=position,
            ),
            T=T,
            state={"net_invested": net, "position_qty": position},
            session=session,
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def test_correction_symbol_is_not_summed_when_omitted(self):
        """정정 대상(본문 생략)의 델타가 일일 합계에 더해지면 안 됩니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        _seed_state("SOXL", T=1.0, net_invested=100.0, position_qty=5)
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )

        calls = []
        result = sr.send_settlement_report(
            [
                # 같은 세션 뒤늦은 체결 → 정정 대상 (일일 본문에서 생략)
                self._report(qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0,
                             position=15, late=True),
                # 본문에 실제로 그려지는 신규 종목
                self._report(symbol="SOXL", qty=1, amount=5.0, T_before=1.0, T=1.5,
                             net=105.0, position=6),
            ],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["corrected"] == ["TQQQ"]
        daily, correction = calls
        assert "합계: 완료 1 / 보류 0 종목\n  매수 1주 / $5.00" in daily, (
            "본문에서 생략한 정정 대상 델타가 합계에 더해지면 안 됩니다"
        )
        assert "매수 3주" not in daily, "합계에 정정 델타가 이중 집계되었습니다"
        assert "매수 1주 / $10.00" in correction, "정정 메시지는 자기 델타를 합산합니다"

    def test_suppressed_symbol_is_not_summed_into_daily_total(self):
        """중복 억제로 본문에서 빠진 종목(비정정)도 합계에서 빠져야 합니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        _seed_state("SOXL", T=1.0, net_invested=100.0, position_qty=5)
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )

        calls = []
        result = sr.send_settlement_report(
            [
                self._report(),  # 같은 내용 → 억제 (본문·합계 모두 제외)
                self._report(symbol="SOXL", qty=1, amount=5.0, T_before=1.0, T=1.5,
                             net=105.0, position=6),
            ],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["sent"] == ["SOXL"]
        assert len(calls) == 1
        assert "합계: 완료 1 / 보류 0 종목\n  매수 1주 / $5.00" in calls[0]
        assert "매수 3주" not in calls[0], "억제된 종목이 합계에 다시 더해졌습니다"


class TestCorrectionResendAfterSessionChange:
    """정정 알림 발송이 실패한 payload 는 세션이 넘어가도 재발송되어야 합니다.

    건너뛰기 판정이 "pending 키가 발송 이력에 있다"이면, 실패한 정정 내용(=발송 이력과
    다른 지문)이 세션 전진 후 영영 재발송되지 않습니다. 판정은 **내용 지문 비교**여야
    합니다 — 지문이 같으면 잔여물(중복), 다르면 아직 못 보낸 추가 반영분입니다.
    """

    PREV = "2026-10-28"

    def _report(self, *, symbol="TQQQ", qty=2, amount=20.0, T_before=3.0, T=3.5,
                net=320.0, position=14, late=False, session=SESSION):
        settled = _settled(
            symbol=symbol,
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": qty,
                    "amount_delta": amount, "late": late,
                }],
                T_before=T_before,
                balance_qty=position,
            ),
            T=T,
            state={"net_invested": net, "position_qty": position},
            session=session,
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def _store_with_pending(self, report, *, sent_hashes=None):
        """발송 이력 + 같은 멱등 키의 pending (정정 실패 잔여물 재현)."""
        key = sr.report_id("kis", "hmac:acct", report.symbol, report.session_key)
        sent = {"at": "2026-10-28T10:00:00Z", "session_key": self.PREV}
        if sent_hashes is not None:
            sent.update(sent_hashes)
        return {
            "sent": {key: sent},
            "pending": {
                key: {
                    "at": "2026-10-28T09:00:00Z",
                    "symbol": report.symbol,
                    "session_key": self.PREV,
                    "broker": "kis",
                    "account": "hmac:acct",
                    "report_id": key,
                    "payload": sr._report_payload(report),
                }
            },
        }

    def test_divergent_pending_is_resent_even_if_key_was_sent(self):
        """키가 발송 이력에 있어도 내용 지문이 다르면(정정 실패분) 재전송합니다."""
        report = self._report(session=self.PREV, qty=1, amount=10.0,
                              T_before=3.5, T=3.75, net=330.0, position=15, late=True)
        sent_content = self._report(session=self.PREV)  # 이미 나간 내용(2주/$20)
        store = self._store_with_pending(
            report, sent_hashes=sr._report_hashes(sent_content)
        )

        groups = sr.pending_backlog(
            store, "TQQQ", broker="kis", account_fingerprint="hmac:acct",
            current_keys=(SESSION,),
        )
        assert len(groups) == 1, "발송 이력과 다른 지문의 미발송분은 재전송 대상입니다"
        assert groups[0]["reports"][0].buy_qty == 1
        assert groups[0]["reports"][0].buy_amount == 10.0

    def test_same_content_leftover_is_not_resent(self):
        """지문이 같은 잔여물은 중복이므로 재전송하지 않습니다 (기존 억제 유지)."""
        report = self._report(session=self.PREV)
        store = self._store_with_pending(report, sent_hashes=sr._report_hashes(report))
        assert sr.pending_backlog(
            store, "TQQQ", broker="kis", account_fingerprint="hmac:acct",
            current_keys=(SESSION,),
        ) == []

    def test_failed_correction_is_resent_next_session(self):
        """전날 실패한 정정 알림이 다음 세션에 별도 메시지로 재전송됩니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        # 전날: 일일 리포트 발송 성공
        first = sr.send_settlement_report(
            [self._report(session=self.PREV)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )
        assert first["sent"] == ["TQQQ"]
        # 전날: 뒤늦은 체결 정정 → 발송 실패 → pending
        failed = sr.send_settlement_report(
            [self._report(session=self.PREV, qty=1, amount=10.0, T_before=3.5,
                          T=3.75, net=330.0, position=15, late=True)],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert failed["corrections_failed"] == ["TQQQ"]
        assert len(_read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]["pending"]) == 1

        # 다음 세션: 정정 실패분이 별도 메시지로 재전송되어야 합니다
        calls = []
        result = sr.send_settlement_report(
            [self._report(qty=1, amount=5.0)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert result["resent_previous"] == ["TQQQ"], "세션이 넘어가도 재발송해야 합니다"
        assert result["sent"] == ["TQQQ"]
        assert len(calls) == 2, "과거 재전송과 오늘 리포트는 서로 다른 메시지입니다"
        assert calls[0].startswith("📨 미발송 정산 재전송")
        assert "2026-10-28" in calls[0]
        assert "매수 1주 / $10.00" in calls[0], "실패한 정정 내용이 그대로 나가야 합니다"
        assert calls[1].startswith("📊 일일 정산 리포트")
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}


class TestMultiplePendingPayloads:
    """같은 세션의 서로 다른 payload 는 서로를 덮어쓰지 않아야 합니다.

    하나의 pending 키에 덮어쓰면 1차 실패분과 정정분(또는 정정 #N + #N+1) 중 하나가
    사라져 그 반영분이 영영 알려지지 않습니다.
    """

    def _report(self, *, qty=2, amount=20.0, T_before=3.0, T=3.5, net=320.0,
                position=14, late=False):
        settled = _settled(
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": qty,
                    "amount_delta": amount, "late": late,
                }],
                T_before=T_before,
                balance_qty=position,
            ),
            T=T,
            state={"net_invested": net, "position_qty": position},
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def _pending(self):
        return _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]["pending"]

    def test_failed_retry_and_correction_are_both_kept(self):
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        # 1차: 매수 2주/$20 발송 실패
        sr.send_settlement_report(
            [self._report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert len(self._pending()) == 1

        # 2차: 1차 실패분 재전송 + 뒤늦은 추가 체결(1주/$10) 정정 → 둘 다 실패
        result = sr.send_settlement_report(
            [self._report(qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0,
                          position=15, late=True)],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert result["failed"] == ["TQQQ"]
        assert result["corrections_failed"] == ["TQQQ"]

        pending = self._pending()
        assert len(pending) == 2, "서로 다른 payload 가 서로를 덮어썼습니다"
        quantities = sorted(entry["payload"]["buy_qty"] for entry in pending.values())
        assert quantities == [1, 2], "1차 실패분(2주)과 정정분(1주)이 모두 보존됩니다"
        same_content = [
            entry for entry in pending.values() if entry["payload"]["buy_qty"] == 2
        ][0]
        assert same_content["attempts"] == 2, "같은 내용은 같은 자리에서 시도 횟수만 누적"

        # 3차: 보관된 내용을 차례로 전달 — 하나도 사라지지 않고 모두 나갑니다
        calls = []
        empty = self._report(qty=0, amount=0.0, T_before=3.75, T=3.75, net=330.0,
                             position=15)
        third = sr.send_settlement_report(
            [empty], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert third["sent"] == ["TQQQ"], "1차 실패분을 먼저 재전송합니다"
        assert len(self._pending()) == 1, "남은 정정분은 다음 실행으로 이어집니다"

        fourth = sr.send_settlement_report(
            [empty], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )
        assert fourth["corrected"] == ["TQQQ"], "남은 정정분이 정정 알림으로 나갑니다"
        assert self._pending() == {}
        delivered = "\n".join(calls)
        assert "매수 2주" in delivered and "매수 1주" in delivered, (
            "보관한 두 payload 가 모두 전달되어야 합니다"
        )


# ── 4-1f. 보고 계층 잔여 P2/P3 ────────────────────────────────────
def _report_for(
    symbol="TQQQ",
    *,
    qty=2,
    amount=20.0,
    T_before=3.0,
    T=3.5,
    net=320.0,
    position=14,
    late=False,
    session=SESSION,
    account="hmac:acct",
):
    """회귀 테스트용 커밋된 리포트 (델타·확정 상태를 자유롭게 지정)."""
    settled = _settled(
        symbol=symbol,
        evidence=_evidence(
            orders=[{
                "odno": "A1", "side": "매수", "qty_delta": qty,
                "amount_delta": amount, "late": late,
            }],
            T_before=T_before,
            balance_qty=position,
        ),
        T=T,
        state={"net_invested": net, "position_qty": position},
        session=session,
    )
    return sr.build_symbol_report(
        settled, committed=True, broker="kis", account_fingerprint=account
    )


class TestOversizedMessageSplit:
    """메시지가 길이 한도를 넘으면 **나눠 보내고**, 실제 전송된 덩어리만 완료로 표시합니다.

    잘린 채 "전량 발송 완료"로 기록하면 잘려 나간 반영분을 다시 알릴 방법이 없습니다.
    """

    SYMBOLS = ["TQQQ", "SOXL", "SPXL", "TECL", "UDOW", "FNGU"]

    def _seed_failed_corrections(self):
        """종목마다 '이미 발송된 내용 + 아직 못 보낸 정정 payload' 를 만들어 둡니다."""
        for symbol in self.SYMBOLS:
            _seed_state(symbol, T=3.6, net_invested=330.0, position_qty=15)
            first = sr.send_settlement_report(
                [_report_for(symbol)], trade_mode="LIVE", broker="kis",
                account_fingerprint="hmac:acct", notify_func=lambda m: True,
            )
            assert first["sent"] == [symbol]
            failed = sr.send_settlement_report(
                [_report_for(symbol, qty=1, amount=10.0, T_before=3.5, T=3.6,
                             net=330.0, position=15)],
                trade_mode="LIVE", broker="kis",
                account_fingerprint="hmac:acct", notify_func=lambda m: False,
            )
            assert failed["corrections_failed"] == [symbol]

    def _empty_reports(self):
        """재시도 실행의 델타 0 리포트 (정정 경로만 타게 합니다)."""
        return [
            _report_for(symbol, qty=0, amount=0.0, T_before=3.6, T=3.6,
                        net=330.0, position=15)
            for symbol in self.SYMBOLS
        ]

    def test_oversized_correction_is_split_into_multiple_messages(self, monkeypatch):
        self._seed_failed_corrections()
        monkeypatch.setattr(sr, "MAX_MESSAGE_CHARS", 420)

        calls = []
        result = sr.send_settlement_report(
            self._empty_reports(), trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert len(calls) > 1, "한도를 넘는 정정 메시지는 나눠 보내야 합니다"
        assert all(message.startswith("📌 정산 정정") for message in calls)
        assert all(len(message) <= sr.MAX_MESSAGE_CHARS for message in calls)
        assert not any("…(생략)" in message for message in calls), "잘린 메시지가 없어야 합니다"
        assert sorted(result["corrected"]) == sorted(self.SYMBOLS)
        rendered = "\n".join(calls)
        for symbol in self.SYMBOLS:
            assert f"■ {symbol}" in rendered, f"{symbol} 반영분이 어느 메시에도 없습니다"
            store = _read_state_file()[symbol][sr.REPORT_STATE_KEY]
            assert store["pending"] == {}, f"{symbol} 미발송 payload 가 정리되어야 합니다"

    def test_only_sent_chunks_are_marked_complete(self, monkeypatch):
        """잘려 나간(= 보내지 못한) 덩어리는 완료로 표시하지 않고 pending 에 남깁니다."""
        self._seed_failed_corrections()
        monkeypatch.setattr(sr, "MAX_MESSAGE_CHARS", 420)

        calls = []

        def first_only(message):
            calls.append(message)
            return len(calls) == 1  # 첫 덩어리만 성공

        result = sr.send_settlement_report(
            self._empty_reports(), trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=first_only,
        )

        delivered = [s for s in self.SYMBOLS if f"■ {s}" in calls[0]]
        undelivered = [s for s in self.SYMBOLS if s not in delivered]
        assert delivered, "첫 덩어리에 최소 한 종목이 있어야 합니다"
        assert undelivered, "여러 덩어리로 나뉘어야 부분 전송을 재현할 수 있습니다"

        assert result["corrected"] == delivered, "전송된 덩어리만 완료로 기록합니다"
        assert sorted(result["corrections_failed"]) == sorted(undelivered)
        for symbol in delivered:
            assert _read_state_file()[symbol][sr.REPORT_STATE_KEY]["pending"] == {}
        for symbol in undelivered:
            pending = _read_state_file()[symbol][sr.REPORT_STATE_KEY]["pending"]
            assert len(pending) == 1, f"{symbol} 미전송 내용은 pending 으로 남아야 합니다"


class TestPendingPreservation:
    """`pending`(미발송 내용)은 **슬롯 개수만 보고** 지우면 안 됩니다."""

    def test_unsent_payloads_survive_slot_count(self):
        store = {}
        count = sr.MAX_PENDING_ENTRIES + 3
        for index in range(count):
            store = sr._apply_mark(
                store,
                _report_for(qty=index + 1, amount=(index + 1) * 10.0,
                            T=3.5 + index * 0.1, net=320.0 + index),
                ok=False,
                at_iso=f"2026-10-29T09:{index:02d}:00Z",
            )
        assert len(store["pending"]) == count, (
            "아직 못 보낸 payload 는 슬롯 상한 때문에 조용히 지우면 안 됩니다"
        )

    def test_only_already_sent_duplicates_are_removed(self):
        same = _report_for(qty=2, amount=20.0, T=3.5, net=320.0, position=14)
        divergent = _report_for(qty=5, amount=50.0, T=3.9, net=350.0, position=18)
        store = sr._apply_mark({}, same, ok=False, at_iso="2026-10-29T09:00:00Z")
        store = sr._apply_mark(store, divergent, ok=False, at_iso="2026-10-29T10:00:00Z")

        # 같은 내용이 뒤에 발송 성공한 상황 — pending 잔여물은 중복이므로 정리 대상입니다.
        store["sent"] = {
            same.report_id: {
                "at": "2026-10-29T10:05:00Z",
                **sr._report_hashes(same),
            }
        }
        pruned = sr._prune(store)

        assert len(pruned["pending"]) == 1
        leftover = list(pruned["pending"].values())[0]
        assert leftover["payload"]["buy_qty"] == 5, "아직 못 보낸 내용은 보존되어야 합니다"

    def test_hard_cap_drops_oldest_created_first(self):
        """하드 상한(`MAX_PENDING_HARD_CAP`)을 넘으면 **생성 시각이 오래된 것부터** 정리합니다.

        최근 것을 지우면 방금 실패한 반영분이 사라지고, 오래된 것만 남아 재전송 순서가
        뒤집힙니다. 정리 기준은 마지막 시도 시각(`at`)이 아니라 생성 시각(`created_at`)입니다.
        """
        total = sr.MAX_PENDING_HARD_CAP + 3
        store = {}
        for index in range(total):
            store = sr._apply_mark(
                store,
                _report_for(qty=index + 1, amount=(index + 1) * 10.0,
                            T=3.5 + index * 0.1, net=320.0 + index),
                ok=False,
                at_iso=f"2026-10-29T09:{index:02d}:00Z",
            )

        kept = sr._prune(store)["pending"]
        assert len(kept) == sr.MAX_PENDING_HARD_CAP, "상한까지만 보관합니다"
        remaining = sorted(entry["payload"]["buy_qty"] for entry in kept.values())
        assert remaining == list(range(4, total + 1)), (
            "가장 오래된(생성 시각 기준) 3건이 정리되고 최근 것만 남아야 합니다"
        )

    # ── 운영 경고 + 삭제 대신 아카이브 보존 ──────────────────────────
    def _fill_pending(self, count):
        """서로 다른 payload `count` 건을 `_apply_mark()` 로 pending 에 쌓습니다.

        시각은 분 단위로 증가시켜 **생성 시각 정렬**이 실제와 같게 동작하도록 합니다
        (60분을 넘어가면 시(hour)를 올립니다).
        """
        store = {}
        for index in range(1, count + 1):
            at_iso = f"2026-10-29T{9 + index // 60:02d}:{index % 60:02d}:00Z"
            store = sr._apply_mark(
                store,
                _report_for(
                    qty=index,
                    amount=index * 10.0,
                    T=3.5 + index * 0.1,
                    net=320.0 + index,
                ),
                ok=False,
                at_iso=at_iso,
            )
        return store

    def _fill_pending_at_once(self, count, monkeypatch):
        """state 로드처럼 여러 건을 **한꺼번에** pending 에 채웁니다 (일괄 정리 경로).

        `_apply_mark()` 는 저장할 때마다 `_prune()` 을 부르므로, 그 경로로 쌓으면 한 번에
        1건씩만 밀려나 "아카이브 상한(20건)을 넘는 일괄 이동"이 재현되지 않습니다
        (수정 전 버그가 이 경로에서만 드러납니다). 실제 운영에서는 캐시에서 읽은 state
        처럼 이미 쌓인 pending 을 한 번에 정리하므로, **쌓는 동안의 prune 만** 건너뜁니다.
        """
        with monkeypatch.context() as patch:
            patch.setattr(sr, "_prune", lambda store: store)
            store = {}
            for index in range(1, count + 1):
                at_iso = f"2026-10-29T{9 + index // 60:02d}:{index % 60:02d}:00Z"
                store = sr._apply_mark(
                    store,
                    _report_for(
                        qty=index,
                        amount=index * 10.0,
                        T=3.5 + index * 0.1,
                        net=320.0 + index,
                    ),
                    ok=False,
                    at_iso=at_iso,
                )
        return store

    def test_over_target_warns_but_keeps_everything(self, capsys, monkeypatch):
        """목표치(`MAX_PENDING_ENTRIES`) 초과는 **운영 경고만** — 내용·알림은 그대로."""
        sent = []
        monkeypatch.setattr(sr, "notify", lambda *a, **k: sent.append(a) or True)
        count = sr.MAX_PENDING_ENTRIES + 2
        store = self._fill_pending(count)
        capsys.readouterr()  # 쌓는 과정의 출력은 버립니다 (경고 조건만 봅니다)

        pruned = sr._prune(store)
        out = capsys.readouterr().out

        assert len(pruned["pending"]) == count, (
            "목표치를 넘어도 미발송 payload 는 지우지 않습니다"
        )
        assert "[경고]" in out and "목표치" in out, (
            "목표치 초과는 조용히 넘기지 말고 운영 경고를 남겨야 합니다"
        )
        assert str(sr.MAX_PENDING_ENTRIES) in out
        assert sr.archived_pending_entries(pruned) == [], (
            "목표치 초과는 보존 대상 — 아카이브로 옮기지 않습니다"
        )
        assert sent == [], "경고는 로그 전용입니다 (텔레그램 발송 금지)"

    def test_warning_log_has_no_raw_odno(self, capsys):
        """경고 로그에는 가명/비민감 정보만 — 원문 주문번호·pending 키 금지."""
        store = self._fill_pending(sr.MAX_PENDING_ENTRIES + 1)
        key = list(store["pending"])[0]
        # payload 에 주문번호가 섞여 들어온 상황 (호출측이 원문을 넣었을 수 있음)
        store["pending"][key]["payload"]["odno"] = "12345678"
        capsys.readouterr()

        sr._prune(store)
        out = capsys.readouterr().out

        assert "[경고]" in out
        assert "12345678" not in out, "공개 로그에 원문 주문번호를 남기면 안 됩니다"
        assert key not in out, "pending 키(내부 식별자)도 로그에 쓰지 않습니다"

    def test_hard_cap_archives_instead_of_dropping(self, capsys):
        """하드 상한에서 밀려난 payload 는 **삭제가 아니라 아카이브**로 보존합니다."""
        total = sr.MAX_PENDING_HARD_CAP + 3
        store = self._fill_pending(total)
        out = capsys.readouterr().out

        kept = sr._prune(store)["pending"]
        assert len(kept) == sr.MAX_PENDING_HARD_CAP, "상한은 유지합니다 (state 폭증 방지)"
        assert "pruned_archive" in out, "삭제 대신 아카이브로 옮겼음을 경고해야 합니다"

        archived = sr.archived_pending_entries(store)
        assert [entry["payload"]["buy_qty"] for entry in archived] == [1, 2, 3], (
            "밀려난 3건이 아카이브에 남아야 합니다 (다음 실행에서 확인 가능)"
        )
        restored = sr._report_from_payload(archived[0]["payload"])
        assert restored is not None and restored.buy_qty == 1, (
            "아카이브 payload 는 원래 리포트로 되살릴 수 있어야 합니다"
        )
        assert archived[0]["archived_at"], "언제 옮겼는지 남겨야 추적할 수 있습니다"
        archive = store["pruned_archive"]
        assert archive["archive_limit"] == sr.MAX_PRUNED_ARCHIVE_ENTRIES
        assert archive["pruned_total"] == 3
        # state 파일에 저장돼도(JSON round-trip) 그대로 확인할 수 있어야 합니다.
        reloaded = json.loads(json.dumps(store))
        assert len(sr.archived_pending_entries(reloaded)) == 3

    def test_pruned_archive_keeps_count_cap(self):
        """아카이브도 상한이 있어 state 가 무한정 커지지 않습니다."""
        over = sr.MAX_PRUNED_ARCHIVE_ENTRIES + 5
        total = sr.MAX_PENDING_HARD_CAP + over
        store = self._fill_pending(total)

        archived = sr.archived_pending_entries(store)
        assert len(archived) == sr.MAX_PRUNED_ARCHIVE_ENTRIES, (
            "아카이브는 건수 상한까지만 보관합니다"
        )
        # 최근 것 위주로 보관 — 가장 오래된 5건은 상한 때문에 아카이브에서도 빠집니다.
        assert archived[0]["payload"]["buy_qty"] == over - sr.MAX_PRUNED_ARCHIVE_ENTRIES + 1
        assert archived[-1]["payload"]["buy_qty"] == over
        assert store["pruned_archive"]["pruned_total"] == over, (
            "누적 정리 건수는 아카이브 상한과 무관하게 계속 셉니다"
        )

    def test_bulk_move_over_cap_keeps_newest_and_orders_return(self, capsys, monkeypatch):
        """**20건 초과 일괄 이동**에서도 최신 것부터 보관·오래된 것부터 정리 (재현).

        `_prune_pending()` 은 최신순 정렬 목록의 꼬리를 넘기므로 넘어온 목록은 "최신순"입니다.
        그 순서를 그대로 이어 붙여 상한을 자르면 **가장 최근 항목이 먼저 잘려** 오래된
        것만 아카이브에 남습니다 (최근 우선 보관 정책 위반). 생성 시각으로 다시 정렬해야
        최신이 보존되고, 반환 순서도 문서(오래된 순)와 일치합니다.
        """
        over = sr.MAX_PRUNED_ARCHIVE_ENTRIES + 5  # 아카이브 상한을 넘는 일괄 이동분
        total = sr.MAX_PENDING_HARD_CAP + over
        store = self._fill_pending_at_once(total, monkeypatch)
        assert len(store["pending"]) == total, "일괄 정리 전 상태를 먼저 확인합니다"
        capsys.readouterr()

        kept = sr._prune(store)["pending"]
        capsys.readouterr()  # 경고 출력은 이 테스트의 관심사가 아닙니다

        assert len(kept) == sr.MAX_PENDING_HARD_CAP, "pending 상한은 유지합니다"
        assert sorted(entry["payload"]["buy_qty"] for entry in kept.values()) == list(
            range(over + 1, total + 1)
        ), "pending 에는 최신 40건이 남아야 합니다"

        archived = sr.archived_pending_entries(store)
        archived_qty = [entry["payload"]["buy_qty"] for entry in archived]
        assert archived_qty == list(
            range(over - sr.MAX_PRUNED_ARCHIVE_ENTRIES + 1, over + 1)
        ), (
            "아카이브는 밀려난 것 중 **최신 20건**을 생성 시각(오래된 순)으로 보관해야 합니다 "
            "(수정 전에는 최신 항목이 먼저 잘려 오래된 것만 남았습니다)"
        )
        assert archived_qty[-1] == over, "가장 최근에 밀려난 항목이 살아남아야 합니다"
        created = [str(entry["created_at"]) for entry in archived]
        assert created == sorted(created), "반환 순서는 생성 시각 오름차순(오래된 순)입니다"
        assert store["pruned_archive"]["pruned_total"] == over, (
            "상한으로 아카이브에 못 들어간 건도 누적 정리 건수에는 포함합니다"
        )

        # 아카이브가 이미 찬 뒤 다시 정리해도 최신 우선 보관이 유지돼야 합니다.
        # 이번 실행에서 새로 밀려나는 건 pending 의 **가장 오래된** 항목(qty=over+1)입니다.
        store = sr._apply_mark(
            store,
            _report_for(qty=999, amount=9990.0, T=99.0, net=9999.0),
            ok=False,
            at_iso="2026-10-30T09:00:00Z",
        )
        rearchived = sr.archived_pending_entries(store)
        assert len(rearchived) == sr.MAX_PRUNED_ARCHIVE_ENTRIES
        assert [entry["payload"]["buy_qty"] for entry in rearchived] == list(
            range(over - sr.MAX_PRUNED_ARCHIVE_ENTRIES + 2, over + 2)
        ), (
            "이미 찬 아카이브에 새 정리분을 합쳐도 가장 오래된 1건만 빠지고 "
            "최신 것이 남아야 합니다"
        )
        assert [str(e["created_at"]) for e in rearchived] == sorted(
            str(e["created_at"]) for e in rearchived
        ), "이전 아카이브와 합쳐도 생성 시각 순서가 유지돼야 합니다"

    def test_hard_cap_log_reports_archived_count(self, capsys, monkeypatch):
        """경고 로그는 "정리 N건"과 "아카이브 보관 M건"을 **구분해서** 씁니다.

        상한 때문에 아카이브에도 못 들어간 건이 있는데 "N건을 옮겼습니다"로만 쓰면
        전부 보관된 것처럼 읽힙니다 — 실제 보관 건수(반환값)를 로그에 표면화합니다.
        """
        over = sr.MAX_PRUNED_ARCHIVE_ENTRIES + 5  # 아카이브 상한을 넘는 일괄 이동분
        total = sr.MAX_PENDING_HARD_CAP + over
        store = self._fill_pending_at_once(total, monkeypatch)
        capsys.readouterr()

        sr._prune(store)
        out = capsys.readouterr().out

        assert (
            f"{over}건을 정리(아카이브 보관 {sr.MAX_PRUNED_ARCHIVE_ENTRIES}건)" in out
        ), (
            "정리 건수와 실제 아카이브 보관 건수를 함께 표기해야 합니다 "
            f"(출력: {out!r})"
        )
        assert f"아카이브 {sr.MAX_PRUNED_ARCHIVE_ENTRIES}건/" in out
        assert "옮겼습니다" not in out, (
            "상한 때문에 못 들어간 건까지 옮긴 것처럼 읽히는 문구는 쓰지 않습니다"
        )

    def test_archive_pruned_payloads_returns_survivor_count(self, capsys):
        """반환값은 **상한 적용 후 실제로 보관된** 이번 추가분만 셉니다."""
        dropped = [
            (
                f"key-{index}",
                {
                    "payload": sr._report_payload(
                        _report_for(
                            qty=index, amount=index * 10.0, T=3.5, net=320.0 + index
                        )
                    ),
                    "created_at": f"2026-10-29T09:{index:02d}:00Z",
                },
            )
            for index in range(1, sr.MAX_PRUNED_ARCHIVE_ENTRIES + 6)
        ]
        store = {}

        archived_now = sr._archive_pruned_payloads(store, dropped)
        capsys.readouterr()

        assert archived_now == sr.MAX_PRUNED_ARCHIVE_ENTRIES, (
            "상한을 넘는 건은 보관 건수에 포함되지 않습니다"
        )
        assert store["pruned_archive"]["pruned_total"] == len(dropped), (
            "누적 정리 건수는 상한과 무관하게 전부 셉니다"
        )

        # 아카이브가 이미 최신으로 가득 찬 상태에서 **더 오래된** 건은 보관되지 않습니다.
        older = [
            (
                "key-old",
                {
                    "payload": sr._report_payload(
                        _report_for(qty=999, amount=9990.0, T=9.9, net=9999.0)
                    ),
                    "created_at": "2026-10-01T00:00:00Z",
                },
            )
        ]
        assert sr._archive_pruned_payloads(store, older) == 0, (
            "아카이브 상한이 이미 찼고 더 오래된 건이면 보관 건수는 0 입니다"
        )
        assert store["pruned_archive"]["pruned_total"] == len(dropped) + 1


class TestLatestPendingComparison:
    """정정 여부 판단은 **가장 최근** 보관분과 비교해야 합니다 (델타 0 정정 소음 방지)."""

    def test_delta_zero_retry_is_not_reported_as_correction(self):
        _seed_state("TQQQ", T=3.6, net_invested=330.0, position_qty=15)
        older = _report_for(qty=2, amount=20.0, T_before=3.4, T=3.5,
                            net=320.0, position=14)
        newest = _report_for(qty=1, amount=10.0, T_before=3.5, T=3.6,
                             net=330.0, position=15)
        store = sr._apply_mark({}, older, ok=False, at_iso="2026-10-29T09:00:00Z")
        store = sr._apply_mark(store, newest, ok=False, at_iso="2026-10-29T10:00:00Z")
        state_module.save_state("TQQQ", {
            "T": 3.6,
            "last_updated": "2026-10-29T10:00:00+00:00",
            "net_invested": 330.0,
            "net_invested_status": "valid",
            "orders_meta": {},
            "position_qty": 15,
            sr.REPORT_STATE_KEY: store,
        })

        calls = []
        result = sr.send_settlement_report(
            [_report_for(qty=0, amount=0.0, T_before=3.6, T=3.6, net=330.0, position=15)],
            trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert result["corrected"] == [], (
            "가장 최근 보관분과 같은 내용을 델타 0 정정으로 다시 알리면 안 됩니다"
        )
        assert len(calls) == 1, "오래된 미발송분 재전송 1건만 나갑니다"
        assert calls[0].startswith("📊 일일 정산 리포트")
        assert "매수 2주 / $20.00" in calls[0], "재전송은 오래된 미발송분을 그대로 보냅니다"
        assert "1차 발송 실패분 재전송" in calls[0]

    def test_retry_of_older_payload_does_not_become_latest(self):
        """오래된 payload(A)의 **재시도 실패**가 "최신 내용" 판정을 뒤집으면 안 됩니다.

        재현 (codex P2)
          pending A(T=3.5) → pending B(T=3.6) → **A 재시도 실패**(마지막 시도 시각만 갱신)
          → 델타 0 C(T=3.6)는 B 와 같은 내용이므로 정정을 내면 안 됩니다.

        정렬 기준이 `at`(마지막 시도 시각)이면 A 가 "가장 최근"으로 둔갑해 C 를 A 와
        비교하고(확정 T·순투입·보유가 다름) → 델타 0 오정정이 발송됩니다.
        """
        _seed_state("TQQQ", T=3.6, net_invested=330.0, position_qty=15)
        older = _report_for(qty=2, amount=20.0, T_before=3.4, T=3.5,
                            net=320.0, position=14)
        newest = _report_for(qty=1, amount=10.0, T_before=3.5, T=3.6,
                             net=330.0, position=15)
        store = sr._apply_mark({}, older, ok=False, at_iso="2026-10-29T09:00:00Z")
        store = sr._apply_mark(store, newest, ok=False, at_iso="2026-10-29T10:00:00Z")
        # 오래된 A 를 다시 시도했지만 또 실패 — 마지막 시도 시각만 갱신됩니다.
        store = sr._apply_mark(store, older, ok=False, at_iso="2026-10-29T11:00:00Z")
        state_module.save_state("TQQQ", {
            "T": 3.6,
            "last_updated": "2026-10-29T10:00:00+00:00",
            "net_invested": 330.0,
            "net_invested_status": "valid",
            "orders_meta": {},
            "position_qty": 15,
            sr.REPORT_STATE_KEY: store,
        })

        calls = []
        result = sr.send_settlement_report(
            [_report_for(qty=0, amount=0.0, T_before=3.6, T=3.6, net=330.0, position=15)],
            trade_mode="LIVE", broker="kis", account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert result["corrected"] == [], (
            "A 재시도 실패로 A 가 최신으로 둔갑해 델타 0 오정정이 나갔습니다"
        )
        assert len(calls) == 1, "오래된 미발송분(A) 재전송 1건만 나갑니다"
        assert calls[0].startswith("📊 일일 정산 리포트")
        assert "매수 2주 / $20.00" in calls[0], "재전송은 A 내용을 그대로 보냅니다"
        assert "매수 0주" not in calls[0], "델타 0 정정이 섞였습니다"

    def test_created_at_is_immutable_and_at_tracks_last_attempt(self):
        """생성 시각(불변)과 마지막 시도 시각(갱신)을 분리해 기록합니다."""
        report = _report_for(qty=2, amount=20.0, T_before=3.4, T=3.5,
                             net=320.0, position=14)
        store = sr._apply_mark({}, report, ok=False, at_iso="2026-10-29T09:00:00Z")
        store = sr._apply_mark(store, report, ok=False, at_iso="2026-10-29T11:00:00Z")

        entry = list(store["pending"].values())[0]
        assert entry["created_at"] == "2026-10-29T09:00:00Z", (
            "재시도로 payload 생성 시각이 바뀌면 정렬·최신 선택 기준이 흔들립니다"
        )
        assert entry["at"] == "2026-10-29T11:00:00Z", "마지막 시도 시각은 갱신됩니다"
        assert entry["attempts"] == 2, "같은 내용은 같은 자리에서 시도 횟수만 누적합니다"
        assert sr._pending_created_at(entry) == "2026-10-29T09:00:00Z"

    def test_legacy_pending_without_created_at_falls_back_to_at(self):
        """`created_at` 이 없는 구버전 항목은 `at` 으로 정렬·선택합니다 (형식 호환)."""
        report = _report_for(qty=2, amount=20.0, T_before=3.4, T=3.5,
                             net=320.0, position=14)
        store = {
            "pending": {
                report.report_id: {
                    "at": "2026-10-29T09:00:00Z",  # 구버전: 생성 시각 필드가 없습니다
                    "symbol": "TQQQ",
                    "session_key": report.session_key,
                    "report_id": report.report_id,
                    "payload": sr._report_payload(report),
                }
            }
        }
        entries = sr._pending_entries(store, report.report_id)
        assert [key for key, _ in entries] == [report.report_id], "구버전 키도 조회됩니다"
        assert sr._pending_created_at(entries[0][1]) == "2026-10-29T09:00:00Z"
        restored = sr.restore_pending(store, report)
        assert restored is not None and restored.buy_qty == 2

        # 구버전 항목을 다시 시도해도 **원래 시각을 생성 시각으로 승계**해야 합니다.
        store = sr._apply_mark(store, report, ok=False, at_iso="2026-10-29T11:00:00Z")
        entry = list(store["pending"].values())[0]
        assert entry["created_at"] == "2026-10-29T09:00:00Z"
        assert entry["at"] == "2026-10-29T11:00:00Z"


class TestCorrectionSectionMerge:
    """한 정정 메시지에 같은 종목이 여러 번 실리면 **섹션 하나로 병합**합니다."""

    def test_same_symbol_is_rendered_once_with_summed_delta(self):
        first = _report_for(qty=2, amount=20.0, T_before=3.4, T=3.5,
                            net=320.0, position=14)
        second = _report_for(qty=1, amount=10.0, T_before=3.5, T=3.6,
                             net=330.0, position=15, late=True)
        other = _report_for("SOXL", qty=1, amount=5.0, T_before=1.0, T=1.5,
                            net=105.0, position=6)
        for report in (first, second, other):
            sr._mark_correction(report, 2)

        message = sr.format_correction_message(
            sr.build_daily_report([first, second, other])
        )

        assert message.count("■ TQQQ") == 1, "같은 종목 섹션이 반복되면 안 됩니다"
        assert message.count("■ SOXL") == 1
        assert "추가 반영분: 매수 3주 / $30.00" in message, "같은 종목은 델타를 합쳐 그립니다"
        assert "T 3.4 → 3.6" in message, "시작점은 가장 오래된 값, 확정값은 최신 값입니다"
        assert "확정 보유: 15주" in message
        assert "지연 체결 1건" in message
        assert "합계: 매수 4주 / $35.00" in message, (
            "병합은 표시만 바꾸므로 합계 정합은 그대로여야 합니다"
        )


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


# ── 4-1g. 보고 계층 P2/P3 — 실행 플래그 지문 · 발송이력 기준 비교 · 리비전/안내 ──
class TestPerRunFlagNotInStateHash:
    """`full_reinference` 는 **이번 실행 방식** 플래그라 확정 상태 지문에 넣으면 안 됩니다.

    넣으면 초기 전체 재추정 SETTLE 뒤 같은 세션의 다음 실행(델타 0, T·순투입·보유 동일)이
    플래그만 true→false 로 바뀌어 델타 0 정정이 나갑니다.
    """

    def _reinference_report(self):
        settled = _settled(
            evidence=_evidence(
                orders=[{
                    "odno": "A1", "side": "매수", "qty_delta": 20,
                    "amount_delta": 760.0, "full_reinference": True,
                }],
                T_before=0.0,
                balance_qty=14,
            ),
            T=3.5,
            state={"net_invested": 320.0, "position_qty": 14},
        )
        return sr.build_symbol_report(
            settled, committed=True, broker="kis", account_fingerprint="hmac:acct"
        )

    def test_same_session_after_reinference_is_not_corrected(self):
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        calls = []
        sender = lambda m: (calls.append(m), True)[1]
        first = sr.send_settlement_report(
            [self._reinference_report()], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=sender,
        )
        assert first["sent"] == ["TQQQ"]
        assert "전체 재추정" in calls[0]

        # 같은 세션 재SETTLE — 반영분 0, T/순투입/보유 동일 (실행 방식 플래그만 다름)
        result = sr.send_settlement_report(
            [_report_for(qty=0, amount=0.0, T_before=3.5, T=3.5, net=320.0, position=14)],
            trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=sender,
        )

        assert result["corrected"] == [], (
            "실행 방식 플래그(full_reinference)만 다른 델타 0 재보고는 정정 대상이 아닙니다"
        )
        assert result["skipped"] == ["already_sent"]
        assert len(calls) == 1, "같은 세션 델타 0 재보고는 다시 보내지 않습니다"

    def test_state_hash_ignores_per_run_flags(self):
        """지문은 지속적 정산값(T·순투입·보유)만 반영합니다."""
        _seed_state("TQQQ", T=3.5, net_invested=320.0, position_qty=14)
        base = _report_for(qty=0, amount=0.0, T_before=3.0, T=3.5, net=320.0, position=14)
        same_state = _report_for(qty=0, amount=0.0, T_before=0.0, T=3.5, net=320.0,
                                 position=14)
        same_state.full_reinference = True

        assert sr.report_state_hash(base) == sr.report_state_hash(same_state), (
            "full_reinference 는 per-run 값이라 확정 상태 지문에 들어가면 안 됩니다"
        )
        other_t = _report_for(qty=0, amount=0.0, T_before=3.0, T=3.75, net=320.0,
                              position=14)
        assert sr.report_state_hash(base) != sr.report_state_hash(other_t), (
            "지속적 정산값(T)이 다르면 지문도 달라야 합니다"
        )


class TestSentNewerThanPending:
    """비교 기준은 **가장 최근에 알려진 내용** — 발송 이력이 보관 payload 보다 최신일 수 있습니다.

    재현 시퀀스
      Run1: 일일 A 발송 실패 → pending A
      Run2: 되살린 A 재전송 실패 + 추가 반영분 B 정정 **성공** (sent = B)
      Run3: 델타 0 C — B 와 같은 내용이면 추가 정정 금지 (A 는 여전히 미발송이라 재전송)
    """

    A = dict(qty=2, amount=20.0, T_before=3.0, T=3.5, net=320.0, position=14)
    B = dict(qty=1, amount=10.0, T_before=3.5, T=3.75, net=330.0, position=15)
    C = dict(qty=0, amount=0.0, T_before=3.75, T=3.75, net=330.0, position=15)

    def test_sent_newer_than_pending_blocks_delta_zero_correction(self):
        _seed_state("TQQQ", T=3.75, net_invested=330.0, position_qty=15)

        # Run1 — 일일 A 발송 실패 (pending A)
        first = sr.send_settlement_report(
            [_report_for(**self.A)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert first["failed"] == ["TQQQ"]

        # Run2 — 되살린 A 재전송 실패, 추가 반영분 B 정정 성공
        second_calls = []

        def run2_sender(message):
            second_calls.append(message)
            return message.startswith("📌 정산 정정")  # 일일 실패 / 정정 성공

        second = sr.send_settlement_report(
            [_report_for(**self.B)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=run2_sender,
        )
        assert second["failed"] == ["TQQQ"], "되살린 1차 실패분은 여전히 실패"
        assert second["corrected"] == ["TQQQ"], "추가 반영분 B 는 정정으로 나갑니다"
        sent = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]["sent"]
        entry = sent[sr.report_id("kis", "hmac:acct", "TQQQ", SESSION)]
        assert entry["delta_hash"] == sr.report_delta_hash(_report_for(**self.B))

        # Run3 — 델타 0 C: B 와 같은 내용이므로 C 정정을 내보내면 안 됩니다.
        calls = []
        result = sr.send_settlement_report(
            [_report_for(**self.C)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert len(result["corrected"]) == 1, (
            "미발송분 A 재전송 1건만 정정으로 나가야 합니다 (델타 0 C 는 오발송)"
        )
        assert len(calls) == 1
        correction = calls[0]
        assert correction.startswith("📌 정산 정정")
        assert "추가 반영분: 매수 2주 / $20.00" in correction, "A(미발송분) 내용이 나갑니다"
        assert "매수 0주" not in correction, (
            "발송 이력(정정 B)보다 오래된 보관 payload 와 비교해 델타 0 정정을 내보냈습니다"
        )

    def test_old_payload_resend_does_not_regress_sent_snapshot(self):
        """오래된 payload 재전송이 **성공**해도 최신 스냅샷 지문은 과거로 돌아가면 안 됩니다.

        재현 시퀀스 (codex P2)
          Run1: 일일 A 발송 실패 → pending A
          Run2: 되살린 A 재전송 실패 + 추가 반영분 B 정정 **성공** (발송 이력 = B)
          Run3: 델타 0 C — 미발송 A 재전송 **성공** (발송할 때 쓴 payload 는 A)
          Run4: 델타 0 C — 상태는 여전히 B

        Run3 이 A(오래된 내용)를 성공적으로 보냈다고 해서 비교 기준선까지 A 로 되돌리면,
        Run4 가 "B 와 같은 내용"을 **새 정보**로 오판해 델타 0 정정을 냅니다. 발송한 payload
        기록과 최신 스냅샷 기준선을 분리 추적해야 합니다 (중복 방지는 그대로 유지).
        """
        _seed_state("TQQQ", T=3.75, net_invested=330.0, position_qty=15)

        # Run1 — 일일 A 발송 실패 (pending A)
        first = sr.send_settlement_report(
            [_report_for(**self.A)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )
        assert first["failed"] == ["TQQQ"]

        # Run2 — 되살린 A 재전송 실패, 추가 반영분 B 정정 성공
        def run2_sender(message):
            return message.startswith("📌 정산 정정")

        second = sr.send_settlement_report(
            [_report_for(**self.B)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=run2_sender,
        )
        assert second["corrected"] == ["TQQQ"]

        # Run3 — 미발송 A 재전송 성공 (이때 발송한 payload 는 A = 오래된 내용)
        third = sr.send_settlement_report(
            [_report_for(**self.C)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: True,
        )
        assert third["corrected"] == ["TQQQ"], "미발송 A 재전송 1건이 나갑니다"
        store = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]
        assert store["pending"] == {}, "A 재전송 성공 후 미발송분은 정리됩니다"
        entry = store["sent"][sr.report_id("kis", "hmac:acct", "TQQQ", SESSION)]
        assert entry["delta_hash"] == sr.report_delta_hash(_report_for(**self.B)), (
            "오래된 payload(A) 재전송 성공이 최신 스냅샷 기준선을 과거로 되돌렸습니다"
        )
        sent_contents = {item["delta_hash"] for item in entry["payloads"]}
        assert sent_contents == {
            sr.report_delta_hash(_report_for(**self.A)),
            sr.report_delta_hash(_report_for(**self.B)),
        }, "발송한 payload 기록(A·B)은 기준선과 별도로 보존되어야 합니다"

        # Run4 — 델타 0 C: 상태는 B 그대로이므로 다시 알릴 내용이 없습니다.
        calls = []
        fourth = sr.send_settlement_report(
            [_report_for(**self.C)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        assert fourth["corrected"] == [], (
            "오래된 payload(A) 재전송 성공이 기준선을 과거로 되돌려 "
            "델타 0 정정이 나갔습니다 (발송 payload 기록과 최신 스냅샷 기준선 분리 필요)"
        )
        assert fourth["skipped"] == ["already_sent"], "새 정보가 없으면 발송하지 않습니다"
        assert calls == [], "델타 0 재보고는 어떤 메시지도 보내지 않습니다"


class TestCorrectionRevisionConsistency:
    """메시지에 표기한 리비전과 state 에 기록된 리비전은 같아야 합니다.

    `restore_pending()` 경로는 리비전을 `2` 로 하드코딩해 왔는데, 발송 순서(일일 → 정정)가
    어긋나면(일일 실패) state 에는 `1` 이 기록되어 다음 정정 번호가 건너뛴 것처럼 보입니다.
    """

    def test_revision_follows_persist_rule_on_restore_path(self):
        _seed_state("TQQQ", T=3.75, net_invested=330.0, position_qty=15)

        # Run1 — 일일 A 발송 실패
        sr.send_settlement_report(
            [_report_for(**TestSentNewerThanPending.A)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )

        # Run2 — 일일(되살린 A) 실패 + 정정 B 성공
        calls = []

        def run2_sender(message):
            calls.append(message)
            return message.startswith("📌 정산 정정")

        result = sr.send_settlement_report(
            [_report_for(**TestSentNewerThanPending.B)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=run2_sender,
        )
        assert result["corrected"] == ["TQQQ"]

        correction = [m for m in calls if m.startswith("📌 정산 정정")][0]
        entry = _read_state_file()["TQQQ"][sr.REPORT_STATE_KEY]["sent"][
            sr.report_id("kis", "hmac:acct", "TQQQ", SESSION)
        ]
        assert f"리비전: #{entry['revision']}" in correction, (
            "메시지 리비전과 state 리비전이 어긋났습니다"
        )
        assert entry["revision"] == 2, "첫 기록(1) + 이번 정정(2)"

    def test_restore_path_daily_message_announces_correction(self):
        """되살린 내용으로 일일 리포트를 그리면서도 정정 안내를 빠뜨리지 않습니다."""
        _seed_state("TQQQ", T=3.75, net_invested=330.0, position_qty=15)
        sr.send_settlement_report(
            [_report_for(**TestSentNewerThanPending.A)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct", notify_func=lambda m: False,
        )

        calls = []
        sr.send_settlement_report(
            [_report_for(**TestSentNewerThanPending.B)], trade_mode="LIVE", broker="kis",
            account_fingerprint="hmac:acct",
            notify_func=lambda m: (calls.append(m), True)[1],
        )

        daily = [m for m in calls if m.startswith("📊 일일 정산 리포트")]
        assert daily, "되살린 미발송분을 일일 리포트로 보냅니다"
        assert "매수 2주 / $20.00" in daily[0], "1차 실패분 내용을 그대로 재전송합니다"
        assert "같은 세션 추가 반영분 1건" in daily[0], (
            "일일 리포트와 함께 정정이 나갔다는 안내가 빠졌습니다"
        )


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
