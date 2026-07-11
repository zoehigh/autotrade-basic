"""
LS증권 해외주식 현재가호가(g3106) 조회 테스트.

목적:
- g3106 TR이 모의투자/실전 환경에서 정상 동작하는지 확인
- g3101(현재가 조회) 실패 시 대안으로 사용 가능한지 검증
- 10단위 호가 데이터 + 현재가 스냅샷 응답 구조 확인
- raw JSON 응답을 tests/output/ 에 덤프하여 분석

g3106 vs g3101:
- g3101: 현재가/open만 반환, 모의투자 미지원 (rsp_cd="")
- g3106: 현재가/open + 호가 10단계 반환, 모의투자 지원 여부 미확인
"""
import sys
from pathlib import Path

# src/ → broker.* 모듈, 프로젝트 루트 → tests 패키지
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.ls.adapter import LSBroker
from broker.ls.order_types import TR_ID_ORDERBOOK
from broker.ls.exchange import convert_exchange_code, build_symbol

from tests.brokers.base import dump_json, print_separator


def _raw_orderbook_request(
    broker: LSBroker,
    symbol: str,
    exchange: str,
    delaygb: str,
) -> dict:
    ls_exch, _ = convert_exchange_code(exchange)
    keysymbol = build_symbol(symbol, ls_exch)
    body = {
        "g3106InBlock": {
            "delaygb": delaygb,
            "keysymbol": keysymbol,
            "exchcd": ls_exch,
            "symbol": symbol.upper(),
        }
    }
    resp = broker._post(
        "/overseas-stock/market-data", tr_id=TR_ID_ORDERBOOK, body=body
    )
    return resp.json()


def _print_result(label: str, raw: dict, path: Path):
    rsp_cd = raw.get("rsp_cd", "")
    rsp_msg = raw.get("rsp_msg", "")
    out = raw.get("g3106OutBlock", {})

    print(f"\n--- {label} ---")
    print(f"  rsp_cd:  {rsp_cd!r}")
    print(f"  rsp_msg: {rsp_msg}")

    if rsp_cd == "":
        print("  ⚠️  rsp_cd='' — 모의투자 미지원 또는 응답 없음")
        print(f"  → raw JSON saved: {path}")
        return

    if rsp_cd != "00000":
        print("  ❌ 비정상 응답 코드")
        print(f"  → raw JSON saved: {path}")
        return

    if not out:
        print("  ❌ g3106OutBlock 없음")
        print(f"  → raw JSON saved: {path}")
        return

    # 현재가 정보
    print("\n  [현재가 정보]")
    print(f"  price:   {out.get('price')}")
    print(f"  open:    {out.get('open')}")
    print(f"  high:    {out.get('high')}")
    print(f"  low:     {out.get('low')}")
    print(f"  sign:    {out.get('sign')}")
    print(f"  change:  {out.get('change')}")
    print(f"  diff:    {out.get('diff')}")
    print(f"  volume:  {out.get('volume')}")

    # 호가 정보 (1~3단계만 출력, 전체는 JSON 참조)
    print("\n  [호가 정보 (상위 3단계)]")
    for i in range(1, 4):
        offerho = out.get(f"offerho{i}")
        bidho = out.get(f"bidho{i}")
        offerrem = out.get(f"offerrem{i}")
        bidrem = out.get(f"bidrem{i}")
        if offerho or bidho:
            print(f"    {i}차: 매도={offerho}({offerrem}) / 매수={bidho}({bidrem})")

    # 호가 총합
    print("\n  [호가 총합]")
    print(f"  매도 총수량: {out.get('offer')}")
    print(f"  매수 총수량: {out.get('bid')}")

    print("\n  ✅ g3106 정상 응답")
    print(f"  → raw JSON saved: {path}")


def _compare_g3101_g3106(broker: LSBroker, symbol: str, exchange: str):
    """g3101과 g3106의 현재가를 비교하여 일치하는지 확인."""
    print(f"\n--- g3101 vs g3106 현재가 비교 ({symbol}) ---")

    # g3101 호출
    from broker.ls.order_types import TR_ID_PRICE
    ls_exch, _ = convert_exchange_code(exchange)
    keysymbol = build_symbol(symbol, ls_exch)
    body_3101 = {
        "g3101InBlock": {
            "delaygb": "R",
            "keysymbol": keysymbol,
            "exchcd": ls_exch,
            "symbol": symbol.upper(),
        }
    }
    try:
        resp_3101 = broker._post(
            "/overseas-stock/market-data", tr_id=TR_ID_PRICE, body=body_3101
        )
        data_3101 = resp_3101.json()
        price_3101 = data_3101.get("g3101OutBlock", {}).get("price", "N/A")
    except Exception as e:
        price_3101 = f"ERROR: {e}"

    # g3106 호출
    body_3106 = {
        "g3106InBlock": {
            "delaygb": "R",
            "keysymbol": keysymbol,
            "exchcd": ls_exch,
            "symbol": symbol.upper(),
        }
    }
    try:
        resp_3106 = broker._post(
            "/overseas-stock/market-data", tr_id=TR_ID_ORDERBOOK, body=body_3106
        )
        data_3106 = resp_3106.json()
        price_3106 = data_3106.get("g3106OutBlock", {}).get("price", "N/A")
    except Exception as e:
        price_3106 = f"ERROR: {e}"

    print(f"  g3101 price: {price_3101}")
    print(f"  g3106 price: {price_3106}")

    if price_3101 == price_3106 and price_3101 not in ("N/A", "0"):
        print("  ✅ 가격 일치 — g3106이 g3101 대체 가능")
    elif price_3101 == "N/A" and price_3106 not in ("N/A", "0"):
        print("  ✅ g3101 실패, g3106 정상 — g3106이 대안으로 사용 가능")
    else:
        print("  ⚠️  가격 불일치 또는 둘 다 실패 — 추가 확인 필요")


def test_ls_orderbook_tqqq():
    print_separator("LS증권 TQQQ(NAS) 현재가호가(g3106) 조회")
    broker = LSBroker()

    for delaygb in ("R", "D"):
        try:
            raw = _raw_orderbook_request(broker, "TQQQ", "NAS", delaygb)
            path = dump_json(raw, f"ls_orderbook_tqqq_{delaygb}")
            _print_result(f"TQQQ NAS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ delaygb={delaygb} 실패: {e}")


def test_ls_orderbook_soxl():
    print_separator("LS증권 SOXL(AMS) 현재가호가(g3106) 조회")
    broker = LSBroker()

    for delaygb in ("R", "D"):
        try:
            raw = _raw_orderbook_request(broker, "SOXL", "AMS", delaygb)
            path = dump_json(raw, f"ls_orderbook_soxl_ams_{delaygb}")
            _print_result(f"SOXL AMS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ AMS delaygb={delaygb} 실패: {e}")

    # NYS로도 테스트 (LS는 AMS/NYS 모두 81)
    for delaygb in ("R", "D"):
        try:
            raw = _raw_orderbook_request(broker, "SOXL", "NYS", delaygb)
            path = dump_json(raw, f"ls_orderbook_soxl_nys_{delaygb}")
            _print_result(f"SOXL NYS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ NYS delaygb={delaygb} 실패: {e}")


def test_g3101_vs_g3106():
    """g3101과 g3106의 현재가를 비교합니다."""
    print_separator("g3101 vs g3106 현재가 비교")
    broker = LSBroker()

    for symbol, exchange in [("TQQQ", "NAS"), ("SOXL", "AMS")]:
        try:
            _compare_g3101_g3106(broker, symbol, exchange)
        except Exception as e:
            print(f"  ❌ {symbol} 비교 실패: {e}")


if __name__ == "__main__":
    from config import BROKER_MODE
    print(f" 현재 브로커 모드: {BROKER_MODE}")
    print(" (실전=real, 모의투자=demo)")

    test_ls_orderbook_tqqq()
    test_ls_orderbook_soxl()
    test_g3101_vs_g3106()
    print_separator("완료")