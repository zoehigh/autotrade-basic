"""
SOXL 종목 특정 조회 테스트
"""

import sys
import os
from pathlib import Path

# src 디렉토리를 Python 경로에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from trader import get_overseas_order_history
from kis_session import KISSession


def test_tqqq_order_history():
    """
    TQQQ 종목의 체결내역 조회 테스트
    """
    
    print("=" * 100)
    print("TQQQ 종목 체결내역 조회 테스트")
    print("=" * 100)
    
    try:
        # TQQQ/NAS로 조회
        session = KISSession()
        order_history = get_overseas_order_history(session, symbol="TQQQ", exchange_code="NAS", days=30)
        
        if not order_history:
            print("\n⚠️ TQQQ 종목의 체결내역이 없습니다.")
            print("\n현재 계좌에 있는 거래 내역을 확인하려면:")
            print("1. 전체 거래 내역 조회 (test_order_history_debug.py) 참고")
            print("2. 실제 보유 중인 종목이 무엇인지 확인")
            return True
        
        print(f"\n✅ 조회 성공! (총 {len(order_history)}건)\n")
        
        for idx, order in enumerate(order_history, 1):
            print(f"{idx}. {order.get('ord_dt')} {order.get('ord_tmd')} - {order.get('sll_buy_dvsn_cd_name')} {order.get('ft_ccld_qty')}주 @ {order.get('ft_ccld_unpr3')} ({order.get('prdt_name')})")
        
        return True
    
    except Exception as e:
        print(f"❌ 실패: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_tqqq_order_history()
    sys.exit(0 if success else 1)
