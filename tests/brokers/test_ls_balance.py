"""
LS증권 해외주식 종합잔고평가(COSOQ00201) 조회 테스트.

목적:
- COSOQ00201 TR raw 응답 확인 (RecCnt="00001" 문자열 포맷)
- OutBlock4(종목별 보유), OutBlock3(통화별 주문가능액) 구조 분석
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from broker.ls.adapter import LSBroker
from broker.ls.order_types import TR_ID_BALANCE
from tests.brokers.base import dump_json, print_separator


def _raw_balance_request(
    broker: LSBroker,
    symbol: str,
    exchange: str,
) -> dict:
    body = {
        "COSOQ00201InBlock1": {
            "RecCnt": 1,
            "BaseDt": "",
            "CrcyCode": "USD",
            "AstkBalTpCode": "00",
        }
    }
    print(f"  요청 body: {body}")
    resp = broker._post(
        "/overseas-stock/accno", tr_id=TR_ID_BALANCE, body=body
    )
    raw_text = resp.text
    print(f"  응답 raw text (첫 500자): {raw_text[:500]}")
    return resp.json()


def _print_result(label: str, raw: dict, path: Path):
    rsp_cd = raw.get("rsp_cd", "")
    rsp_msg = raw.get("rsp_msg", "")

    print(f"\n--- {label} ---")
    print(f"  rsp_cd:  {rsp_cd!r}")
    print(f"  rsp_msg: {rsp_msg}")

    # OutBlock1: 계좌 기본 정보
    ob1 = raw.get("COSOQ00201OutBlock1", {})
    print(f"  OutBlock1: AcntNo={ob1.get('AcntNo')}, BaseDt={ob1.get('BaseDt')}")

    # OutBlock2: 종합 평가 (dict 또는 array)
    ob2 = raw.get("COSOQ00201OutBlock2", {})
    if isinstance(ob2, list):
        print(f"  OutBlock2 (array): {len(ob2)} 항목")
        for i, item in enumerate(ob2):
            print(f"    [{i}] ErnRat={item.get('ErnRat')}, WonEvalSumAmt={item.get('WonEvalSumAmt')}")
    elif isinstance(ob2, dict) and ob2:
        print(f"  OutBlock2 (dict): ErnRat={ob2.get('ErnRat')}, WonEvalSumAmt={ob2.get('WonEvalSumAmt')}")

    # OutBlock3: 통화별 잔고
    ob3 = raw.get("COSOQ00201OutBlock3", [])
    if isinstance(ob3, list):
        print(f"  OutBlock3: {len(ob3)} 항목")
        for i, item in enumerate(ob3):
            print(f"    [{i}] CrcyCode={item.get('CrcyCode')}, FcurrDps={item.get('FcurrDps')}, "
                  f"FcurrOrdAbleAmt={item.get('FcurrOrdAbleAmt')}")
    elif isinstance(ob3, dict) and ob3:
        print(f"  OutBlock3 (dict): CrcyCode={ob3.get('CrcyCode')}, "
              f"FcurrOrdAbleAmt={ob3.get('FcurrOrdAbleAmt')}")

    # OutBlock4: 종목별 보유
    ob4 = raw.get("COSOQ00201OutBlock4", [])
    if isinstance(ob4, list):
        print(f"  OutBlock4: {len(ob4)} 항목")
        for i, item in enumerate(ob4):
            print(f"    [{i}] ShtnIsuNo={item.get('ShtnIsuNo')}, "
                  f"AstkBalQty={item.get('AstkBalQty')}, "
                  f"FcstckUprc={item.get('FcstckUprc')}")
    elif isinstance(ob4, dict) and ob4:
        print(f"  OutBlock4 (dict): ShtnIsuNo={ob4.get('ShtnIsuNo')}, "
              f"AstkBalQty={ob4.get('AstkBalQty')}")

    print(f"  → raw JSON saved: {path}")


def test_ls_balance_cosoq00201():
    print_separator("LS증권 COSOQ00201 잔고 조회")
    broker = LSBroker()

    for symbol, exchange in [("TQQQ", "NAS"), ("SOXL", "AMS")]:
        try:
            raw = _raw_balance_request(broker, symbol, exchange)
            path = dump_json(raw, f"ls_cosoq00201_{symbol.lower()}")
            _print_result(f"{symbol} {exchange} COSOQ00201", raw, path)
        except Exception as e:
            print(f"  ❌ {symbol} 실패: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    test_ls_balance_cosoq00201()
    print_separator("완료")
