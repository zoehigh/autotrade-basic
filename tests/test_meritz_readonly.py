"""감시기 DRY 읽기 전용 방어선 테스트.

검증 범위
  1) 프록시: 허용 목록(조회)만 통과, 주문 변경·미지정 접근은 호출 전에 차단
  2) 실제 어댑터(MeritzBroker)+가짜 세션: 주문·정정·취소 POST 는 세션에 도달하지 못하고, 조회(GET)와 **인증 토큰 발급은 동작**
  3) 감시기 통합: DRY 는 읽기 전용 브로커만 사용 / 위반 시 중단 표식 / 차단 설치 실패 시 fail-closed / APPLY 는 변경 없음
  4) state 불변 검사: 락을 잡은 실행 전후만 비교 (정상 LIVE 의 갱신은 DRY 변경으로 오인하지 않음)
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))

import meritz_reprice_watch as watch
import run_lock
import state as st
from broker.meritz import auth as meritz_auth
from meritz_readonly import ALLOWED, ReadOnlyBroker, ReadOnlyViolation
from test_meritz_broker import BASE_URL, FakeResponse, make_broker, ok, price_ok  # noqa: F401
from test_meritz_reprice_watch import (  # noqa: F401
    APPLY_ENV, CONFIG_DRY, CONFIG_LIVE, ENV, FakeBroker, NOW, Notifier, OpenCalendar, run, world,
)


# ── 1) 프록시 ────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.calls = []

    def exchange_code(self, ex):
        self.calls.append("exchange_code")
        return "NY"

    def get_order_history(self, *a, **k):
        self.calls.append("get_order_history")
        return []

    def get_stock_price(self, *a, **k):
        self.calls.append("get_stock_price")
        return "p"

    def close(self):
        self.calls.append("close")

    def place_order(self, *a, **k):
        self.calls.append("place_order")

    def modify_order(self, *a, **k):
        self.calls.append("modify_order")

    def cancel_order(self, *a, **k):
        self.calls.append("cancel_order")

    def get_balance(self, *a, **k):
        self.calls.append("get_balance")


def test_allowlist_passes_only_the_watcher_query_methods():
    inner = _Recorder()
    ro = ReadOnlyBroker(inner)
    assert ro.exchange_code("AMS") == "NY" and ro.get_order_history("S", "NY") == [] and ro.get_stock_price("S", "NY") == "p"
    ro.close()
    assert inner.calls == ["exchange_code", "get_order_history", "get_stock_price", "close"]
    assert ALLOWED == {"exchange_code", "get_order_history", "get_stock_price", "close"}


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order", "get_balance",
                                  "_request", "_session", "some_future_order_method"])
def test_everything_else_is_blocked_before_reaching_the_broker(name):
    inner = _Recorder()
    ro = ReadOnlyBroker(inner)
    with pytest.raises(ReadOnlyViolation):
        getattr(ro, name)
    assert inner.calls == []                                   # 실제 브로커 메서드는 호출되지 않음


def test_proxy_cannot_be_modified_or_unwrapped():
    ro = ReadOnlyBroker(_Recorder())
    with pytest.raises(ReadOnlyViolation):
        ro.modify_order = lambda *a, **k: None
    with pytest.raises(ReadOnlyViolation):
        ro._inner = None


# ── 2) 실제 어댑터 + 가짜 세션: POST 차단 / GET·인증 허용 ───────────────

def test_order_cancel_modify_posts_never_reach_the_session(make_broker):
    broker, session = make_broker([ok()] * 6)
    ro = ReadOnlyBroker(broker)
    for call in (lambda: ro.modify_order("20261111:0000100", "SOXL", "NY", "BUY", 7, 73.69, "LOC"),
                 lambda: ro.place_order("SOXL", "NY", "BUY", 1, 70.0, "LOC"),
                 lambda: ro.cancel_order("20261111:0000100", "SOXL", "NY")):
        with pytest.raises(ReadOnlyViolation):
            call()
    assert session.calls == []                                 # 요청이 하나도 나가지 않음


def test_transport_guard_blocks_post_even_if_the_inner_broker_is_used_directly(make_broker):
    """프록시를 우회해 내부 브로커의 주문 메서드를 직접 불러도 전송 계층에서 막힌다 (두 번째 방어선)."""
    broker, session = make_broker([ok()] * 6)
    ReadOnlyBroker(broker)                                      # 세션 POST 차단 설치
    for call in (lambda: broker.modify_order("20261111:0000100", "SOXL", "NY", "BUY", 7, 73.69, "LOC"),
                 lambda: broker.place_order("SOXL", "NY", "BUY", 1, 70.0, "LOC"),
                 lambda: broker.cancel_order("20261111:0000100", "SOXL", "NY")):
        try:
            call()
        except Exception:
            pass                                                # 어떤 예외든 상관없음 — 요청이 나가지 않는 것이 요건
    assert [c for c in session.calls if c["method"] == "POST"] == []


def test_queries_still_work_through_the_guard(make_broker):
    broker, session = make_broker([ok(rsp_cd="5766", data=[]), ok(rsp_cd="5766", data=[]), price_ok()])
    ro = ReadOnlyBroker(broker)
    assert ro.get_order_history("TQQQ", "NAS") == []
    assert ro.get_stock_price("TQQQ", "NAS").last == 72.5
    assert session.calls and all(c["method"] == "GET" for c in session.calls)


def test_auth_token_issue_is_not_blocked_by_the_post_guard(make_broker, monkeypatch):
    """모든 POST 를 막으면 인증까지 막힌다 — 토큰 발급(requests.post)은 허용되고 세션 POST 만 막혀야 한다."""
    broker, session = make_broker([])
    ReadOnlyBroker(broker)
    posted = []

    def fake_post(url, **kwargs):
        posted.append(url)
        return FakeResponse({"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr("broker.meritz.auth.requests.post", fake_post)
    meritz_auth.invalidate_token()
    assert meritz_auth.get_access_token(BASE_URL, "k", "s") == "tok"
    assert len(posted) == 1 and posted[0].endswith("/oauth2/token")      # 인증 POST 는 나감
    with pytest.raises(ReadOnlyViolation):
        session.post("/trading/v1/overseas/orders/modify", "tok", json_body={})   # 세션 POST 는 차단
    meritz_auth.invalidate_token()


# ── 3) 감시기 통합 ───────────────────────────────────────────────────

class _SessionStub:
    def __init__(self):
        self.posts = []

    def get(self, *a, **k):
        raise AssertionError("not used")

    def post(self, *a, **k):
        self.posts.append(a)


def test_dry_run_installs_post_block_on_the_real_session_and_sends_no_post(world):
    world.broker._session = _SessionStub()
    code, _ = run(world)
    assert code == 0 and world.broker.calls == []
    assert world.broker._session.post.__name__ == "_blocked_post"       # 감시기가 실행 중 차단을 설치함
    assert world.broker._session.posts == []


def test_dry_watcher_attempting_modify_is_blocked_halts_and_sends_nothing(world, monkeypatch):
    """(가정) 감시기 코드의 결함으로 DRY 에서 정정을 호출하려 해도 실제 브로커에는 도달하지 않는다."""
    def buggy_watch_symbol(settings, broker, cfg, record, now_utc, session, notify_fn):
        broker.modify_order("20261111:0000100", cfg["symbol"], "NY", "BUY", 7, 73.69, "LOC")
        return "ok"

    monkeypatch.setattr(watch, "_watch_symbol", buggy_watch_symbol)
    code, _ = run(world)
    assert code == 1 and world.broker.calls == []
    halt = json.load(open(run_lock.watch_halt_path()))
    assert halt["result"] == "READONLY_VIOLATION"
    assert any(urgent for urgent, _m in world.notifier.sent)
    assert not os.path.exists(run_lock.journal_path())


def test_dry_fails_closed_when_the_post_block_cannot_be_installed(world):
    class FrozenSession:
        __slots__ = ()                                              # 속성 설정 불가 → 차단 설치 실패

        def post(self, *a, **k):
            return None

    world.broker._session = FrozenSession()
    queried = []
    world.broker.get_order_history = lambda *a, **k: queried.append(1) or []
    code, _ = run(world)
    assert code == 1 and queried == []                                # 조회조차 하지 않음
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "READONLY_VIOLATION"


def test_apply_mode_is_not_wrapped_and_still_amends(world):
    code, _ = run(world, env=APPLY_ENV, config=CONFIG_LIVE)
    assert code == 0 and len(world.broker.calls) == 1               # APPLY 경로는 변경 없음 (프록시 미적용)


# ── 4) state 불변 검사 ───────────────────────────────────────────────

def test_state_change_by_a_normal_live_run_between_dry_runs_is_not_blamed_on_dry(world):
    code, _ = run(world)
    assert code == 0
    # 정상 LIVE 가 (DRY 실행 사이에) state 를 갱신했다 — 락이 풀려 있던 시간의 변경
    state = st.load_state("SOXL")
    state["T"] = float(state.get("T", 0)) + 1.0
    st.save_state("SOXL", state)
    code, _ = run(world)
    assert code == 0 and not os.path.exists(run_lock.watch_halt_path())


def test_state_change_during_a_locked_dry_run_is_detected_and_halts(world):
    def mutating_history(*a, **k):
        state = st.load_state("SOXL")
        state["T"] = float(state.get("T", 0)) + 1.0
        st.save_state("SOXL", state)                                 # 락 보유 중 state 가 바뀜
        return world.broker.history

    world.broker.get_order_history = mutating_history
    code, _ = run(world)
    assert code == 1
    assert json.load(open(run_lock.watch_halt_path()))["result"] == "STATE_CHANGED_IN_DRY"
    assert any(urgent for urgent, _m in world.notifier.sent)


def test_unchanged_state_is_logged_with_hash_prefix(world, capsys):
    run(world)
    assert "state 불변 확인" in capsys.readouterr().out
