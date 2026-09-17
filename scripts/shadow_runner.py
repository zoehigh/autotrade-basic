#!/usr/bin/env python3
"""섀도우 v1.2 독립 러너 — 로컬 전용, GH Actions 미지원.

실제 state.json / save_state / GH 캐시 / 실제 주문 / 텔레그램에 절대 닿지 않는
독립 가상 원장(섀도우)을 종목별로 순차 실행합니다. trading_bot.py에서 import하지
않는 standalone 스크립트입니다.

2단계 실행 (v1.2):
    generate — 전략 호출 → 의도(intent)만 기록 (체결/회계 없음)
    settle   — pending 의도를 실제 일봉 종가와 대조해 체결/만료 처리

사용법:
    uv run python scripts/shadow_runner.py --symbol TQQQ --exchange NAS
    uv run python scripts/shadow_runner.py --phase settle --symbol TQQQ --exchange NAS
    uv run python scripts/shadow_runner.py --snapshot-dir .shadow
    uv run python scripts/shadow_runner.py --symbol TQQQ --exchange NAS --snapshot-dir /tmp/shadow

환경변수:
    SYMBOLS / {SYMBOL}_SEED / {SYMBOL}_SPLITS — 종목 설정 (config.py 규칙)
    SHADOW_FEE_RATE     — 수수료율 (기본: COMMISSION_RATE=0.0025)
    SHADOW_SLIPPAGE_BPS — 슬리피지 bps (기본: 0)
    BROKER / BROKER_MODE — 시세/잔고 조회용 브로커 (DRY 래핑, 주문 없음)

cron 예시 (평일, 로컬에서만 — 주말은 cron 요일로 제외):
    생성(프리장, 실전 슬롯): 0 17 * * 1-5 (서머) / 0 18 * * 1-5 (동절기)
        cd /path/to/autotrade-basic && uv run python scripts/shadow_runner.py --phase generate >> .shadow/runner.log 2>&1
    정산(익일 아침, 전일 종가 확정 후):
        0 7 * * 2-6 cd /path/to/autotrade-basic && uv run python scripts/shadow_runner.py --phase settle >> .shadow/runner.log 2>&1
    30 7 * * 1-5 cd /path/to/autotrade-basic && uv run python scripts/shadow_runner.py --phase settle >> .shadow/runner.log 2>&1
미국 휴장일은 generate가 시작 시 is_trading_day()로 감지해 원장 기록 없이 종료합니다.
settle은 거래일 체크 없이 실행되며, 대기 의도/종가가 없으면 로그만 남기고 exit 0입니다.

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
from shadow import generate_symbol, settle_symbol


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
        description="섀도우 v1.2 독립 러너 (로컬 전용, GH Actions 미지원)"
    )
    parser.add_argument("--symbol", help="종목코드 (예: TQQQ). 미지정 시 SYMBOLS 전체")
    parser.add_argument("--exchange", help="거래소 (예: NAS). --symbol과 함께 사용")
    parser.add_argument(
        "--phase",
        choices=["generate", "settle"],
        default="generate",
        help="실행 단계: generate(의도 기록) | settle(종가 대조 체결) (기본: generate)",
    )
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
    print(f"섀도우 v1.2 러너 시작 ({args.phase} 단계 — SIMULATION ONLY, 실제 주문/상태 무관)")
    print("=" * 60)

    try:
        if args.phase == "generate":
            # ── 휴장일 조기 종료 (원장·스냅샷 기록 없음) ──
            try:
                trading_day = broker.is_trading_day()
            except Exception as e:
                print(f"[shadow] 거래일 확인 실패 — 종료합니다: {e}")
                return 0
            if not trading_day:
                print("[shadow] 휴장일 — 원장 기록 없이 종료합니다. (exit 0)")
                return 0
            for symbol_config in targets:
                generate_symbol(broker, symbol_config, snapshot_dir=args.snapshot_dir)
        else:
            # ── settle: 거래일 체크 없이 종가 대조 체결 ──
            #    대기 의도/종가가 없으면(휴장 등) 로그만 남기고 exit 0
            for symbol_config in targets:
                settle_symbol(broker, symbol_config, snapshot_dir=args.snapshot_dir)
    finally:
        broker.close()

    print("\n섀도우 러너 정상 종료 (exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())