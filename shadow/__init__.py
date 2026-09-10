"""섀도우 원장 (Shadow Ledger) — 실제 state.json과 완전히 독립된 가상 원장.

DRY 모드 + SHADOW_LOG=true 일 때 trading_bot이 호출합니다.
실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 절대 닿지 않습니다.
"""
from shadow.ledger import run_shadow_symbol

__all__ = ["run_shadow_symbol"]