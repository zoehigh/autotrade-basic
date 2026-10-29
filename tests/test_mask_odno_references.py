"""`_mask_odno_references()` 라벨 참조 마스킹 회귀 테스트.

배경: 공개 로그 마스킹은 **주문번호 참조만** 바꾸고 수량·가격·에러코드·시각은 그대로
남겨야 진단이 가능합니다. 그런데 KIS 는 주문번호의 앞자리 0 을 떼어 반환하므로
주문번호가 `1`·`12`·`123` 같은 1~3자리가 될 수 있고, 라벨(`odno=`, `주문번호:`)이
붙은 참조에 최소 길이 제한을 두면 그 값이 그대로 공개 로그에 남습니다.

이 파일은 두 방향을 함께 고정합니다.
  - 양성: 라벨이 붙은 1~3자리 참조 + 합성번호(`YYYYMMDD:NNNN`)가 마스킹됩니다.
  - 음성: 라벨이 없는 수량(`수량: 1주`)·가격(`$11.01`)·에러코드(`IGW50002`, `5636`,
    `404`)·시각(`04:10:41`, `2026-10-21`)·비율(`20%`)은 **한 글자도** 바뀌지 않습니다.

마스킹 토큰은 `state.mask_odno()`(같은 salt)와 정확히 일치해야 하고, 주변 문구는
바이트 단위로 동일해야 합니다. 브로커 API·네트워크는 사용하지 않습니다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    sys.path.insert(0, str(_path))

import trading_bot  # noqa: E402
from state import mask_odno  # noqa: E402


def _mask(text):
    return trading_bot._mask_odno_references(text)


# ═══════════════════════════════════════════════════════════════════
# 양성 — 라벨이 붙은 참조는 길이와 무관하게 마스킹
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "text, label, odno_id",
    [
        ("odno=1", "odno=", "1"),
        ("odno=12", "odno=", "12"),
        ("odno=123", "odno=", "123"),
        ("주문번호: 1", "주문번호: ", "1"),
        ("주문번호: 123", "주문번호: ", "123"),
        ("예약주문번호: 12", "예약주문번호: ", "12"),
    ],
)
def test_short_labeled_reference_is_masked(text, label, odno_id):
    """1~3자리 주문번호도 라벨이 붙으면 마스킹됩니다 (`{4,}` 제한 회귀 방지)."""
    masked = _mask(text)
    assert masked == f"{label}{mask_odno(odno_id)}"


def test_synthetic_composite_reference_is_masked():
    """라벨 없는 합성번호(메리츠 `YYYYMMDD:주문번호`)도 주문 식별자입니다."""
    raw = "20261028:1234"
    assert _mask(raw) == mask_odno(raw)
    assert raw not in _mask(f"{raw} 주문이 거부되었습니다")


def test_mixed_sentence_masks_only_order_references():
    """섞인 문장에서 주문번호 참조만 바뀌고 나머지는 바이트 동일합니다."""
    text = (
        "주문 실패: odno=1 (수량: 1주, 금액: $11.01, 비율: 20%)\n"
        "주문번호: 123 예약주문번호: 12 시각: 04:10:41 코드: IGW50002"
    )
    masked = _mask(text)
    expected = (
        f"주문 실패: odno={mask_odno('1')} (수량: 1주, 금액: $11.01, 비율: 20%)\n"
        f"주문번호: {mask_odno('123')} 예약주문번호: {mask_odno('12')} "
        "시각: 04:10:41 코드: IGW50002"
    )
    assert masked == expected


def test_empty_and_none_are_returned_as_is():
    assert _mask("") == ""
    assert _mask(None) is None


# ═══════════════════════════════════════════════════════════════════
# 음성 — 라벨 없는 숫자는 절대 건드리지 않음
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "text",
    [
        "수량: 1주",
        "매수 1주",
        "$11.01",
        "IGW50002",
        "5636",
        "4062",
        "404",
        "04:10:41",
        "2026-10-21",
        "20%",
    ],
)
def test_unlabeled_numbers_are_byte_identical(text):
    """수량·가격·에러코드·HTTP코드·시각·비율은 라벨이 없으므로 그대로 남습니다."""
    masked = _mask(text)
    assert masked == text
    assert masked.encode("utf-8") == text.encode("utf-8")


def test_unlabeled_numbers_survive_inside_a_sentence():
    """문장 안에서도 라벨 없는 숫자는 그대로입니다 (부분 일치 없음)."""
    text = "수량: 1주 매수, 금액 $11.01, 코드 IGW50002/5636/404, 시각 04:10:41, 2026-10-21, 20%"
    assert _mask(text) == text


def test_mask_token_in_still_masks_only_labeled_field():
    """접수 알림 twin 빌더도 라벨 필드만 바꿉니다 (무제한 replace 금지 유지)."""
    text = "매수 1주\n수량: 1주\n주문번호: 1\n금액: $11.01"
    masked = trading_bot._mask_token_in(text, "1")
    assert masked == f"매수 1주\n수량: 1주\n주문번호: {mask_odno('1')}\n금액: $11.01"


# ── 멱등: 이미 마스킹된 토큰은 그대로 ──────────────────────────────
def test_already_masked_token_is_preserved():
    """`odno=ord#…`를 다시 마스킹해도 토큰이 바뀌지 않습니다 (이중 마스킹 방지)."""
    import trading_bot

    token = mask_odno("0000039445")
    text = f"취소 실패: odno={token} (잔량 확인 불가)"
    assert trading_bot._mask_odno_references(text) == text


def test_masking_is_idempotent_on_error_text():
    """정리된 에러를 요약 단계에서 다시 처리해도 결과가 같습니다."""
    import trading_bot

    once = trading_bot._mask_odno_references("미접수 확정: odno=0000039445, 수량 7주")
    twice = trading_bot._mask_odno_references(once)
    assert twice == once
    assert "0000039445" not in twice


def test_empty_marker_token_is_preserved():
    """`ord#-`(빈 값 표식)는 마스킹 대상이 아닙니다."""
    import trading_bot

    text = "last_processed_ordno=ord#-"
    assert trading_bot._mask_odno_references(text) == text


# ── 오탐 방지: 주문번호가 아닌 문자열은 그대로 ──────────────────────
@pytest.mark.parametrize("text", [
    "ordno is required",
    "odno=odno",
    "odno=None",
    "odno=unknown",
    "주문번호: 확인필요",
    "주문번호\n123",
    "odno\n456",
    "에러코드 odno",
])
def test_non_order_words_are_preserved(text):
    """라벨 뒤가 실제 주문번호 후보(숫자 포함)가 아니면 마스킹하지 않습니다."""
    assert _mask(text) == text


@pytest.mark.parametrize("raw", ["mock_order_123", "ord_001", "demo_loc_001", "A1"])
def test_alphanumeric_ids_with_digits_are_masked(raw):
    """영숫자 주문번호(숫자 포함)는 계속 마스킹합니다."""
    assert _mask(f"odno={raw}") == f"odno={mask_odno(raw)}"


def test_false_positives_leave_no_ledger_rows(monkeypatch):
    """오탐 문자열은 원장에 등록되지 않습니다 (불필요한 매핑 방지)."""
    import log_mask

    monkeypatch.setattr(log_mask, "_LEDGER", {})
    for text in ("ordno is required", "odno=None", "odno=unknown", "odno=odno"):
        assert _mask(text) == text
    assert log_mask._LEDGER == {}
