#!/usr/bin/env python3
"""섀도우 v1.1 독립 러너 — 로컬 전용, GH Actions 미지원.

실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 절대 닿지 않는
독립 가상 원장(섀도우)을 종목별로 순차 실행합니다. trading_bot.py에서 import하지
않는 standalone 스크립트입니다.

사용법:
    uv run python scripts/shadow_runner.py --symbol TQQQ --exchange NAS
    uv run python scripts/shadow_runner.py --snapshot-dir .shadow
    uv run python scripts/shadow_runner.py --symbol TQQQ --exchange NAS --snapshot-dir /tmp/shadow

환경변수:
    SYMBOLS / {SYMBOL}_SEED / {SYMBOL}_SPLITS — 종목 설정 (config.py 규칙)
    SHADOW_FEE_RATE     — 수수료율 (기본: COMMISSION_RATE=0.0025)
    SHADOW_SLIPPAGE_BPS — 슬리피지 bps (기본: 0)
    BROKER / BROKER_MODE — 시세/잔고 조회용 브로커 (DRY 래핑, 주문 없음)

cron 예시 (매일 07:00 KST, 로컬에서만):
    0 7 * * * cd /path/to/autotrade-basic && uv run python scripts/shadow_runner.py >> .shadow/runner.log 2>&1

⚠️  실계좌 파생값(수수료율 등)이 포함되므로 .shadow/ artifact를 장기보관하지 마세요.
"""
import argparse
import os
import sys

# repo 루트 + src 경로 추가 (config/broker/shadow import용)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(_REPO_ROOT, "src")
for _path in (_REPO_ROOT, _SRC_PATH):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from broker import create_broker
from config import SYMBOLS
from shadow import run_shadow_symbol


def _build_symbol_config(symbol, exchange):
    """환경변수 {SYMBOL}_SEED/_SPLITS 기반 종목 설정을 구성합니다."""
    seed_raw = os.getenv(f"{symbol}_SEED", "").strip()
    if not seed_raw:
        raise ValueError(
            f"{symbol}_SEED가 설정되지 않았습니다. .env에 달러 금액을 추가하세요."
        )
    seed = float(seed_raw)
    if seed <= 0:
        raise ValueError(f"{symbol}_SEED는 0보다 커야 합니다 (입력값: {seed_raw})")

    splits = int(os.getenv(f"{symbol}_SPLITS") or "40")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "splits": splits,
        "symbol_type": (os.getenv(f"{symbol}_SYMBOL_TYPE") or symbol).strip().upper(),
        "seed": seed,
        "additional_loc_levels": int(
            os.getenv(f"{symbol}_ADDITIONAL_LOC_LEVELS")
            or os.getenv("ADDITIONAL_LOC_LEVELS")
            or "3"
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="섀도우 v1.1 독립 러너 (로컬 전용, GH Actions 미지원)"
    )
    parser.add_argument("--symbol", help="종목코드 (예: TQQQ). 미지정 시 SYMBOLS 전체")
    parser.add_argument("--exchange", help="거래소 (예: NAS). --symbol과 함께 사용")
    parser.add_argument(
        "--snapshot-dir",
        default=".shadow",
        help="스냅샷/원장 저장 디렉터리 (기본: .shadow)",
    )
    args = parser.parse_args()

    # ── 대상 종목 결정 ──
    if args.symbol:
        if not args.exchange:
            parser.error("--symbol 사용 시 --exchange도 지정해야 합니다.")
        targets = [_build_symbol_config(args.symbol.upper(), args.exchange.upper())]
    else:
        targets = list(SYMBOLS)  # config.py 파싱 (시드 필수 검증 포함)

    # ── 브로커 생성 (DRY 래핑 — 주문/상태 저장 없음) ──
    broker = create_broker()

    print("=" * 60)
    print("섀도우 v1.1 러너 시작 (SIMULATION ONLY — 실제 주문/상태 무관)")
    print("=" * 60)

    try:
        for symbol_config in targets:
            run_shadow_symbol(broker, symbol_config, snapshot_dir=args.snapshot_dir)
    finally:
        broker.close()

    print("\n섀도우 러너 정상 종료 (exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())