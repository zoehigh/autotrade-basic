"""
해외주식 주문체결내역 조회 API 테스트 (특정 종목)

이 테스트는 해외주식 주문체결내역 API를 호출하여
특정 종목의 최근 30일 체결내역을 조회합니다.
"""

import sys
import os
from pathlib import Path

# src 디렉토리를 Python 경로에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from trader import get_overseas_order_history
from config import SYMBOLS


TEST_SYMBOL = SYMBOLS[0]["symbol"]
TEST_EXCHANGE = SYMBOLS[0]["exchange"]


def format_order_type(sll_buy_dvsn_cd_name):
    """
    주문 유형을 이모지와 함께 표시합니다.
    """
    if "매수" in sll_buy_dvsn_cd_name:
        return f"🟢 {sll_buy_dvsn_cd_name}"
    elif "매도" in sll_buy_dvsn_cd_name:
        return f"🔴 {sll_buy_dvsn_cd_name}"
    else:
        return sll_buy_dvsn_cd_name


def test_overseas_symbol_order_history():
    """
    특정 종목의 해외주식 주문체결내역 조회 API 호출 테스트
    
    테스트 내용:
    - 환경변수 SYMBOLS의 첫 번째 종목을 사용하여 API 호출
    - 최근 30일의 특정 종목 체결내역 조회
    - 매도/매수 여부와 체결수량 표시
    - 최신 내역이 먼저 표시되는지 확인
    """
    
    print("=" * 100)
    print("해외주식 주문체결내역 조회 API 테스트 (특정 종목)")
    print(f"종목 코드: {TEST_SYMBOL} | 거래소: {TEST_EXCHANGE} | 조회 기간: 최근 30일")
    print("=" * 100)
    
    try:
        # API 호출
        order_history = get_overseas_order_history(symbol=TEST_SYMBOL, exchange_code=TEST_EXCHANGE, days=30)
        
        # 결과 검증
        if not order_history:
            print("\n⚠️ 해당 기간에 해당 종목의 체결내역이 없습니다.")
            print(f"조회한 종목코드: {TEST_SYMBOL}")
            print("종목 코드를 확인해주세요.")
            return True  # 정상적인 경우 (체결내역이 없음)
        
        print(f"\n✅ API 호출 성공! (총 {len(order_history)}건)\n")
        
        # 테이블 헤더
        print(f"{'#':<3} {'주문일자':<12} {'시간':<8} {'종목명':<30} {'매도/매수':<15} {'주문수':<8} {'체결수':<8} {'체결가':<15} {'체결금액':<15} {'상태':<10}")
        print("-" * 140)
        
        # 체결내역 출력
        for idx, order in enumerate(order_history, 1):
            ord_dt = order.get("ord_dt", "")
            ord_tmd = order.get("ord_tmd", "")
            prdt_name = order.get("prdt_name", "")[:30]  # 이름 길이 제한
            sll_buy_dvsn = format_order_type(order.get("sll_buy_dvsn_cd_name", ""))
            ft_ord_qty = order.get("ft_ord_qty", "0")
            ft_ccld_qty = order.get("ft_ccld_qty", "0")  # 체결수량 (핵심)
            ft_ccld_unpr3 = order.get("ft_ccld_unpr3", "0")
            ft_ccld_amt3 = order.get("ft_ccld_amt3", "0")
            prcs_stat_name = order.get("prcs_stat_name", "")
            
            print(f"{idx:<3} {ord_dt:<12} {ord_tmd:<8} {prdt_name:<30} {sll_buy_dvsn:<15} {ft_ord_qty:<8} {ft_ccld_qty:<8} {ft_ccld_unpr3:<15} {ft_ccld_amt3:<15} {prcs_stat_name:<10}")
        
        # 통계 정보
        print("\n" + "=" * 100)
        print("📊 통계 정보:")
        print("=" * 100)
        
        total_buy_qty = sum(int(order.get("ft_ccld_qty", "0")) 
                            for order in order_history 
                            if "매수" in order.get("sll_buy_dvsn_cd_name", ""))
        total_sell_qty = sum(int(order.get("ft_ccld_qty", "0")) 
                             for order in order_history 
                             if "매도" in order.get("sll_buy_dvsn_cd_name", ""))
        
        print(f"  종목 코드: {TEST_SYMBOL}")
        print(f"  총 매수 수량: {total_buy_qty} 주")
        print(f"  총 매도 수량: {total_sell_qty} 주")
        print(f"  총 체결 건수: {len(order_history)} 건")
        
        # 최신 거래
        if order_history:
            latest = order_history[0]
            print(f"\n  가장 최신 거래: {latest.get('ord_dt')} {latest.get('ord_tmd')} - {latest.get('sll_buy_dvsn_cd_name')} {latest.get('ft_ccld_qty')}주 @ {latest.get('ft_ccld_unpr3')}")
        
        # 전체 응답 데이터 (첫 3건만)
        print("\n" + "=" * 100)
        print("📋 상세 응답 데이터 (최신 3건):")
        print("=" * 100)
        
        for idx, order in enumerate(order_history[:3], 1):
            print(f"\n[{idx}번째 거래]")
            for key, value in sorted(order.items()):
                print(f"  {key:30s}: {value}")
        
        print("\n" + "=" * 100)
        print("✅ 테스트 완료")
        print("=" * 100)
        
        return True
    
    except Exception as e:
        print(f"❌ 테스트 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_overseas_symbol_order_history()
    sys.exit(0 if success else 1)
