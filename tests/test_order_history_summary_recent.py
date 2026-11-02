"""[주문이력 요약]이 가장 최근 주문을 보여주는지 검증합니다 (네트워크 없음).

배경: 일부 어댑터는 날짜를 오래된 순으로 이어 붙여 이력을 만든다. 앞에서 100건만 자르면
이력이 100건을 넘을 때 최신 주문이 요약에서 빠져, 로그만으로는 주문 접수 여부를 확인할 수 없었다
(Kiwoom 모의 SOXL 184건 → 9/17~10/07 만 출력, 2026-11-02 실측). 표시 전용 변경이며 정산은 영향 없음.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from broker.market_utils import select_recent_history

ADAPTERS = ["kis", "kiwoom", "ls", "toss", "nhplug", "meritz"]
SRC = Path(__file__).parent.parent / "src" / "broker"


def _row(dt, tmd, odno):
    return {"ord_dt": dt, "ord_tmd": tmd, "odno": odno}


def test_오래된순_이력에서_최신_n건을_시간순으로_돌려준다():
    hist = [_row(f"202610{d:02d}", "041600", str(d)) for d in range(1, 29)]
    out = select_recent_history(hist, 5)
    assert [x["odno"] for x in out] == ["24", "25", "26", "27", "28"]


def test_최신순_이력에서도_같은_결과():
    hist = [_row(f"202610{d:02d}", "041600", str(d)) for d in range(28, 0, -1)]
    out = select_recent_history(hist, 3)
    assert [x["odno"] for x in out] == ["26", "27", "28"]


def test_같은날_시각순과_동률은_원래_순서_유지():
    hist = [
        _row("20261030", "041630", "c"),
        _row("20261030", "041600", "a"),
        _row("20261030", "041600", "b"),
    ]
    assert [x["odno"] for x in select_recent_history(hist, 3)] == ["a", "b", "c"]


def test_ord_tmd_자리수_보정():
    hist = [_row("20261030", "41600", "x"), _row("20261030", "041601", "y")]
    assert [x["odno"] for x in select_recent_history(hist, 2)] == ["x", "y"]


def test_ord_dt_없으면_kst_iso로_정렬하고_시각불명은_가장_오래된_것():
    hist = [
        {"odno": "late", "ord_datetime_kst": "2026-10-31T04:16:30+09:00"},
        {"odno": "unknown"},
        {"odno": "early", "ord_datetime_kst": "2026-10-30T04:16:30+09:00"},
    ]
    out = select_recent_history(hist, 2)
    assert [x["odno"] for x in out] == ["early", "late"]


def test_미국영업일_안에서_KST_자정을_넘기면_실제_시각순():
    """KIS/LS/키움: 같은 ord_dt 에서 KST 23:00(전날 밤) 주문이 다음날 04:00 주문보다 앞선다 (Codex 리뷰 P2)."""
    hist = [
        _row("20261030", "040000", "next-morning"),  # 실제 KST 10/31 04:00
        _row("20261030", "230000", "prev-night"),    # 실제 KST 10/30 23:00
    ]
    out = select_recent_history(hist, 1)
    assert [x["odno"] for x in out] == ["next-morning"]
    assert [x["odno"] for x in select_recent_history(hist, 2)] == ["prev-night", "next-morning"]


def test_KST일자_브로커는_복원하지_않고_ord_dt를_그대로_쓴다():
    hist = [
        {"ord_dt": "20261031", "ord_tmd": "040000", "odno": "b", "_ord_dt_is_kst_date": True},
        {"ord_dt": "20261030", "ord_tmd": "230000", "odno": "a", "_ord_dt_is_kst_date": True},
    ]
    assert [x["odno"] for x in select_recent_history(hist, 2)] == ["a", "b"]


def test_n이_전체보다_크면_전부_n이_0이하면_빈목록():
    hist = [_row("20261030", "041600", "a")]
    assert len(select_recent_history(hist, 100)) == 1
    assert select_recent_history(hist, 0) == []
    assert select_recent_history([], 5) == []


def test_원본_리스트는_바뀌지_않는다():
    hist = [_row("20261031", "041600", "b"), _row("20261030", "041600", "a")]
    snapshot = [dict(x) for x in hist]
    select_recent_history(hist, 1)
    assert hist == snapshot


def test_모든_어댑터가_앞에서_자르지_않고_헬퍼를_쓴다():
    """정적 가드: `order_history[:n]` 로 요약을 자르는 코드가 다시 생기지 않게 한다."""
    for name in ADAPTERS:
        text = (SRC / name / "adapter.py").read_text(encoding="utf-8")
        assert "order_history[:n]" not in text, name
        assert "select_recent_history(order_history, n)" in text, name
        assert re.search(r"import \(?[^)]*select_recent_history|select_recent_history,", text), name
