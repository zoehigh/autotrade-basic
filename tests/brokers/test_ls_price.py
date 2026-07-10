"""
LS증권 해외주식 현재가(g3101) 조회 테스트.

목적:
- `delaygb=R`(실시간)과 `delaygb=D`(지연) 파라미터에 따른 응답 비교
- 모의투자 환경에서 g3101 TR이 정상 동작하는지 확인
- raw JSON 응답을 tests/output/ 에 덤프하여 분석
"""
import sys
from pathlib import Path

# src/ → broker.* 모듈, 프로젝트 루트 → tests 패키지
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.ls.adapter import LSBroker
from broker.ls.order_types import TR_ID_PRICE
from broker.ls.exchange import convert_exchange_code, build_symbol

from tests.brokers.base import dump_json, print_separator


def _raw_price_request(
    broker: LSBroker,
    symbol: str,
    exchange: str,
    delaygb: str,
) -> dict:
    ls_exch, _ = convert_exchange_code(exchange)
    keysymbol = build_symbol(symbol, ls_exch)
    body = {
        "g3101InBlock": {
            "delaygb": delaygb,
            "keysymbol": keysymbol,
            "exchcd": ls_exch,
            "symbol": symbol.upper(),
        }
    }
    resp = broker._post(
        "/overseas-stock/market-data", tr_id=TR_ID_PRICE, body=body
    )
    return resp.json()


def _raw_price_request_raw_keysymbol(
    broker: LSBroker,
    symbol: str,
    keysymbol: str,
    exchcd: str,
    delaygb: str,
) -> dict:
    """keysymbol을 직접 지정하여 API 호출 (포맷 테스트용)."""
    body = {
        "g3101InBlock": {
            "delaygb": delaygb,
            "keysymbol": keysymbol,
            "exchcd": exchcd,
            "symbol": symbol.upper(),
        }
    }
    resp = broker._post(
        "/overseas-stock/market-data", tr_id=TR_ID_PRICE, body=body
    )
    return resp.json()


def _print_result(label: str, raw: dict, path: Path):
    rsp_cd = raw.get("rsp_cd", "")
    rsp_msg = raw.get("rsp_msg", "")
    out = raw.get("g3101OutBlock", {})
    print(f"\n--- {label} ---")
    print(f"  rsp_cd:  {rsp_cd!r}")
    print(f"  rsp_msg: {rsp_msg}")
    if out:
        print(f"  open:    {out.get('open')}")
        print(f"  price:   {out.get('price')}")
        print(f"  high:    {out.get('high')}")
        print(f"  low:     {out.get('low')}")
        print(f"  sign:    {out.get('sign')}")
    print(f"  → raw JSON saved: {path}")


def test_ls_price_tqqq():
    print_separator("LS증권 TQQQ(NAS) 현재가 조회")
    broker = LSBroker()

    for delaygb in ("R", "D"):
        try:
            raw = _raw_price_request(broker, "TQQQ", "NAS", delaygb)
            path = dump_json(raw, f"ls_tqqq_{delaygb}")
            _print_result(f"TQQQ NAS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ delaygb={delaygb} 실패: {e}")

    # keysymbol 포맷 테스트: 접두어 없이 순수 티커만
    for ks in ("TQQQ", "82TQQQ"):
        for ex in ("82", "81"):
            label = f"TQQQ keysymbol={ks!r} exchcd={ex} delaygb=R"
            try:
                raw = _raw_price_request_raw_keysymbol(broker, "TQQQ", ks, ex, "R")
                path = dump_json(raw, f"ls_tqqq_ks{ks}_ex{ex}")
                _print_result(label, raw, path)
            except Exception as e:
                print(f"  ❌ {label} 실패: {e}")


def test_ls_price_soxl():
    print_separator("LS증권 SOXL(AMS/NYS) 현재가 조회")
    broker = LSBroker()

    for delaygb in ("R", "D"):
        try:
            raw = _raw_price_request(broker, "SOXL", "AMS", delaygb)
            path = dump_json(raw, f"ls_soxl_ams_{delaygb}")
            _print_result(f"SOXL AMS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ AMS delaygb={delaygb} 실패: {e}")

    # NYS로도 테스트 (LS는 AMS/NYS 모두 81)
    for delaygb in ("R", "D"):
        try:
            raw = _raw_price_request(broker, "SOXL", "NYS", delaygb)
            path = dump_json(raw, f"ls_soxl_nys_{delaygb}")
            _print_result(f"SOXL NYS delaygb={delaygb}", raw, path)
        except Exception as e:
            print(f"  ❌ NYS delaygb={delaygb} 실패: {e}")


if __name__ == "__main__":
    test_ls_price_tqqq()
    test_ls_price_soxl()
    print_separator("완료")
