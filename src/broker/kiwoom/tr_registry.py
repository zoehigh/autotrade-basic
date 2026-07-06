"""
키움증권 TR 레지스트리 — TR ID별 (method, path) 매핑.

엔드포인트 그룹 (정정된 구조):
  /api/us/ordr     — 주문 (매수/매도/정정/취소)
  /api/us/acnt     — 계좌 (잔고/거래내역/예수금)
  /api/us/mrkcond  — 시세 (현재가/호가)

환전(/api/us/exchange)은 현재 프로젝트에서 불필요하므로 제외.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TrEndpoint:
    method: str   # "POST"
    path: str     # "/api/us/ordr"


# ── 미국주식 TR 엔드포인트 ──────────────────────────────────────────
_REGISTRY: dict[str, TrEndpoint] = {
    # 주문 — /api/us/ordr
    "ust20000": TrEndpoint("POST", "/api/us/ordr"),   # 매수
    "ust20001": TrEndpoint("POST", "/api/us/ordr"),   # 매도
    "ust20002": TrEndpoint("POST", "/api/us/ordr"),   # 정정
    "ust20003": TrEndpoint("POST", "/api/us/ordr"),   # 취소

    # 계좌 — /api/us/acnt
    "ust21070": TrEndpoint("POST", "/api/us/acnt"),   # 잔고
    "ust21100": TrEndpoint("POST", "/api/us/acnt"),   # 거래내역
    "ust21110": TrEndpoint("POST", "/api/us/acnt"),   # 예수금

    # 시세 — /api/us/mrkcond
    "usa20100": TrEndpoint("POST", "/api/us/mrkcond"), # 현재가
    "usa20101": TrEndpoint("POST", "/api/us/mrkcond"), # 10호가
}


def get_endpoint(tr_id: str) -> tuple[str, str]:
    """TR ID → (method, path) 반환."""
    ep = _REGISTRY.get(tr_id)
    if ep is None:
        raise KeyError(f"등록되지 않은 TR ID: {tr_id}")
    return ep.method, ep.path


# ── 기능별 TR ID 상수 (KIS의 order_types.py와 대응) ────────────────
TR_BUY       = "ust20000"
TR_SELL      = "ust20001"
TR_CORRECT   = "ust20002"
TR_CANCEL    = "ust20003"
TR_BALANCE   = "ust21070"
TR_HISTORY   = "ust21100"
TR_DEPOSIT   = "ust21110"
TR_PRICE     = "usa20100"
TR_ORDERBOOK = "usa20101"
