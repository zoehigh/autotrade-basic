"""
해외주식 주문체결내역 조회 API 디버깅 테스트

전체 거래 내역을 조회하여 응답 형식을 확인합니다.
"""

import sys
import os
from pathlib import Path

# src 디렉토리를 Python 경로에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from config import KIS_ACCOUNT_NO, ACNT_PRDT_CD, KIS_APP_KEY, KIS_APP_SECRET, KIS_DOMAIN
from broker.kis.auth import get_access_token
import requests
from datetime import datetime, timedelta


def test_all_order_history_debug():
    """
    전체 주문체결내역을 조회하여 응답 형식을 확인합니다.
    """
    
    print("=" * 100)
    print("해외주식 주문체결내역 전체 조회 (디버깅)")
    print("=" * 100)
    
    try:
        # Step 1: 접근 토큰 획득
        token_data = get_access_token()
        access_token = token_data["access_token"]
        
        # Step 2: 날짜 계산
        today = datetime.now()
        start_date = today - timedelta(days=30)
        
        ord_end_dt = today.strftime("%Y%m%d")
        ord_strt_dt = start_date.strftime("%Y%m%d")
        
        print(f"\n조회 기간: {ord_strt_dt} ~ {ord_end_dt}\n")
        
        # Step 3: API 호출
        url = f"{KIS_DOMAIN}/uapi/overseas-stock/v1/trading/inquire-ccnl"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "TTTS3035R"
        }
        
        params = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "PDNO": "%",
            "ORD_STRT_DT": ord_strt_dt,
            "ORD_END_DT": ord_end_dt,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "01",
            "OVRS_EXCG_CD": "%",  # 전체 거래소
            "SORT_SQN": "AS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": ""
        }
        
        response = requests.get(
            url, 
            headers=headers, 
            params=params, 
            verify=False
        )
        response.raise_for_status()
        
        response_data = response.json()
        
        # Step 4: 응답 확인
        if response_data.get("rt_cd") != "0":
            msg = response_data.get("msg1", "알 수 없는 에러")
            print(f"❌ API 호출 실패: {msg}")
            return False
        
        output = response_data.get("output", [])
        
        if not output:
            print("⚠️ 조회된 체결내역이 없습니다.")
            return True
        
        print(f"✅ 조회된 전체 거래: {len(output)}건\n")
        
        # Step 5: 모든 종목명 출력
        print("=" * 100)
        print("조회된 종목들:")
        print("=" * 100)
        
        product_names = {}
        for item in output:
            prdt_name = item.get("prdt_name", "")
            if prdt_name not in product_names:
                product_names[prdt_name] = 0
            product_names[prdt_name] += 1
        
        for idx, (prdt_name, count) in enumerate(product_names.items(), 1):
            print(f"{idx}. {prdt_name} - {count}건")
        
        # Step 6: 첫 5건의 상세 정보
        print("\n" + "=" * 100)
        print("최신 5건의 상세 정보:")
        print("=" * 100)
        
        for idx, item in enumerate(output[:5], 1):
            print(f"\n[{idx}번째]")
            print(f"  주문일자: {item.get('ord_dt')}")
            print(f"  주문시각: {item.get('ord_tmd')}")
            print(f"  종목명: {item.get('prdt_name')}")
            print(f"  매도/매수: {item.get('sll_buy_dvsn_cd_name')}")
            print(f"  주문수량: {item.get('ft_ord_qty')}")
            print(f"  체결수량: {item.get('ft_ccld_qty')}")
            print(f"  체결단가: {item.get('ft_ccld_unpr3')}")
            print(f"  체결금액: {item.get('ft_ccld_amt3')}")
            print(f"  거래소: {item.get('ovrs_excg_cd')}")
            print(f"  상태: {item.get('prcs_stat_name')}")
        
        print("\n" + "=" * 100)
        print("✅ 디버깅 완료")
        print("=" * 100)
        
        return True
    
    except Exception as e:
        print(f"❌ 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_all_order_history_debug()
    sys.exit(0 if success else 1)
