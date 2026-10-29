"""알림 채널 분리(개인 채팅 원문 / 공용 마스킹) + 텔레그램 실패 로그 보호 테스트 (mock 전용).

검증 항목:
  (b) 개인 채팅이면 `telegram_message`(원문 주문번호 포함)가 전송되고, stdout 에는 남지 않는다.
  (c) 조용한 시간·전송 실패·재시도·예외 경로의 stdout/stderr 에 본문·주문번호·토큰·chat id·URL 이 없다.
  (d) 개인 채팅이 아니면(목록에 없음 / 환경변수 없음 / chat id 없음) 마스킹된 `message` 만 전송된다.

실제 텔레그램 API 는 호출하지 않습니다 — `telegram._TELEGRAM_SESSION.post` 를 대체합니다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import log_mask  # noqa: E402
import notifier  # noqa: E402
import telegram  # noqa: E402

RAW = "0000039445"
BODY_MARKER = "BODY-CONTENT-MARKER"
BOT_TOKEN = "123456:FAKE-BOT-TOKEN"
CHAT_ID = "1122334455"


def _messages():
    """마스킹된 공용 메시지 + 원문이 든 개인 메시지 (trading_bot 접수 알림과 같은 형태)."""
    telegram_message = f"✅ 주문 성공 TQQQ\n\n매수 1주\n주문번호: {RAW}\n{BODY_MARKER}"
    message = telegram_message.replace(RAW, log_mask.mask_odno(RAW))
    return message, telegram_message


@pytest.fixture
def sent(monkeypatch):
    """`notifier.send_telegram` 을 대체해 실제 전송 내용을 기록합니다."""
    captured = []

    def fake_send(text):
        captured.append(text)
        return True

    monkeypatch.setattr(notifier, "send_telegram", fake_send)
    return captured


# ── (b) 개인 채팅 = 원문 ─────────────────────────────────────────────
def test_personal_chat_gets_raw_but_stdout_stays_masked(monkeypatch, capsys, sent):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", f"{CHAT_ID},-1001234567890")
    monkeypatch.setenv("QUIET_HOURS", "false")
    message, telegram_message = _messages()

    assert notifier.notify(message, telegram_message=telegram_message) is True
    out = capsys.readouterr()

    assert sent == [telegram_message], "개인 채팅에는 원문 주문번호가 담긴 본문이 가야 합니다"
    assert RAW in sent[0]
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert BOT_TOKEN not in stream and CHAT_ID not in stream


# ── (d) 개인 아님 = 마스킹 ────────────────────────────────────────────
def test_non_personal_chat_gets_masked(monkeypatch, capsys, sent):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", "-1009876543210")  # 다른 채팅만 허용
    monkeypatch.setenv("QUIET_HOURS", "false")
    message, telegram_message = _messages()

    notifier.notify(message, telegram_message=telegram_message)
    assert sent == [message]
    assert RAW not in sent[0] and log_mask.mask_odno(RAW) in sent[0]


def test_unset_allowlist_means_not_personal(monkeypatch, sent):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.delenv("TELEGRAM_PERSONAL_CHAT_IDS", raising=False)
    monkeypatch.setenv("QUIET_HOURS", "false")
    message, telegram_message = _messages()

    notifier.notify(message, telegram_message=telegram_message)
    assert sent == [message] and RAW not in sent[0]


def test_missing_chat_id_means_not_personal(monkeypatch, sent):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", CHAT_ID)
    monkeypatch.setenv("QUIET_HOURS", "false")
    message, telegram_message = _messages()

    notifier.notify(message, telegram_message=telegram_message)
    assert sent == [message] and RAW not in sent[0]


# ── (c) 조용한 시간: 본문을 출력하지 않는다 ──────────────────────────
def test_quiet_hours_prints_no_body(monkeypatch, capsys, sent):
    monkeypatch.setenv("QUIET_HOURS", "true")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("TELEGRAM_PERSONAL_CHAT_IDS", CHAT_ID)
    monkeypatch.setattr(notifier, "_is_quiet_hours", lambda: True)
    message, telegram_message = _messages()

    assert notifier.notify(message, telegram_message=telegram_message) is False
    assert sent == []
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert log_mask.mask_odno(RAW) not in stream
        assert BOT_TOKEN not in stream and CHAT_ID not in stream
        assert "api.telegram.org" not in stream
    assert "건너뜀" in out.out


# ── (c) 전송 실패: 응답 본문·URL·토큰을 남기지 않는다 ────────────────
class _Resp:
    def __init__(self, status_code, text="", json_payload=None):
        self.status_code = status_code
        self.text = text
        self._json = json_payload or {}

    def json(self):
        return self._json


def _set_telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setattr(telegram.time, "sleep", lambda *_a, **_k: None)


def test_telegram_4xx_failure_logs_no_body_or_url(monkeypatch, capsys):
    _set_telegram_env(monkeypatch)
    # 텔레그램은 실패 시 요청 본문(message)을 되돌려주기도 합니다 — 그대로 두면 공개 로그에 남습니다.
    monkeypatch.setattr(
        telegram._TELEGRAM_SESSION, "post",
        lambda *a, **k: _Resp(400, text=f"Bad Request: {BODY_MARKER} {RAW}"),
    )
    message, _raw_message = _messages()

    assert telegram.send_telegram(message) is False
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert BOT_TOKEN not in stream and CHAT_ID not in stream
        assert "api.telegram.org" not in stream
        assert "Bad Request" not in stream
    assert "HTTP 400" in out.out


def test_telegram_5xx_retry_logs_no_body_or_url(monkeypatch, capsys):
    _set_telegram_env(monkeypatch)
    monkeypatch.setattr(
        telegram._TELEGRAM_SESSION, "post", lambda *a, **k: _Resp(502, text=BODY_MARKER)
    )
    message, _raw_message = _messages()

    assert telegram.send_telegram(message) is False
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert BOT_TOKEN not in stream and "api.telegram.org" not in stream
    assert "재시도" in out.out and "/3회" in out.out


def test_telegram_exception_logs_type_only(monkeypatch, capsys):
    _set_telegram_env(monkeypatch)

    def boom(*_a, **_k):
        # 예외 문자열에는 요청 URL(=봇 토큰)과 본문이 들어갈 수 있습니다.
        raise RuntimeError(
            f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed: {BODY_MARKER} {RAW}"
        )

    monkeypatch.setattr(telegram._TELEGRAM_SESSION, "post", boom)
    message, _raw_message = _messages()

    assert telegram.send_telegram(message) is False
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert BOT_TOKEN not in stream and "api.telegram.org" not in stream
    assert "RuntimeError" in out.out


def test_telegram_timeout_logs_no_body(monkeypatch, capsys):
    import requests

    _set_telegram_env(monkeypatch)

    def timeout(*_a, **_k):
        raise requests.exceptions.Timeout(f"timeout {BODY_MARKER} {RAW}")

    monkeypatch.setattr(telegram._TELEGRAM_SESSION, "post", timeout)
    message, _raw_message = _messages()

    assert telegram.send_telegram(message) is False
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert RAW not in stream and BODY_MARKER not in stream
        assert BOT_TOKEN not in stream
    assert "시간 초과" in out.out


# ── 접수 twin: 라벨 있는 주문번호 필드만 치환 ────────────────────────
def test_mask_token_in_replaces_only_labeled_order_number_field():
    """주문번호가 수량·가격·시각과 겹쳐도 라벨 필드만 마스킹합니다 (KIS trim `1` 대비)."""
    import trading_bot

    token = log_mask.mask_odno("1")
    telegram_message = (
        "✅ 주문 성공 TQQQ\n\n매수 1주\n수량: 1주\n주문번호: 1\n"
        "시각: 2026-10-21 01:10:41\n금액: $11.01"
    )
    message = trading_bot._mask_token_in(telegram_message, "1")

    assert f"주문번호: {token}" in message
    assert "수량: 1주" in message and "매수 1주" in message
    assert "01:10:41" in message and "$11.01" in message
    assert "주문번호: 1\n" not in message


def test_mask_token_in_prefers_reservation_label():
    """`예약주문번호:`가 `주문번호:` 부분일치로 이중 치환되지 않습니다."""
    import trading_bot

    token = log_mask.mask_odno("0000077777")
    telegram_message = f"📋 예약주문 접수 TQQQ\n\n예약주문번호: 0000077777\n수량: 2주"
    message = trading_bot._mask_token_in(telegram_message, "0000077777")

    assert f"예약주문번호: {token}" in message
    assert "0000077777" not in message
