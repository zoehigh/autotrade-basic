# API 키, 환경변수 등 설정값을 관리하는 파일
import os
from dotenv import load_dotenv

# .env 파일에서 환경변수 읽기
load_dotenv()

# 증권사 선택
# 환경변수 BROKER로 사용할 증권사를 선택합니다.
# 지원: kis(기본값), kiwoom, ls, toss(예정)
# .env 예: BROKER=kis
BROKER = os.getenv("BROKER", "kis").strip().lower()

# 브로커 모드 (demo/real)
# 환경변수 BROKER_MODE (우선) 또는 KIS_MODE(하위호환)를 읽습니다.
# 기본값은 demo(모의)입니다. .env 예: BROKER_MODE=real
BROKER_MODE = os.getenv("BROKER_MODE") or os.getenv("KIS_MODE", "demo")
BROKER_MODE = BROKER_MODE.strip().lower()


def _get_broker_config(broker_name: str) -> dict:
    """브로커별 설정을 반환합니다."""
    configs = {
        "kis": {
            "app_key": os.getenv("KIS_APP_KEY", ""),
            "app_secret": os.getenv("KIS_APP_SECRET", ""),
            "account_no": os.getenv("KIS_ACCOUNT_NO", ""),
            "domain": (
                "https://openapi.koreainvestment.com:9443"
                if BROKER_MODE == "real"
                else "https://openapivts.koreainvestment.com:29443"
            ),
            "acnt_prdt_cd": "01",
        },
        "kiwoom": {
            "app_key": os.getenv("KIWOOM_APP_KEY", ""),
            "app_secret": os.getenv("KIWOOM_APP_SECRET", ""),
            "domain": (
                "https://api.kiwoom.com"
                if BROKER_MODE == "real"
                else "https://mockapi.kiwoom.com"
            ),
            "acnt_prdt_cd": "",
        },
        "ls": {
            "app_key": os.getenv("LS_APP_KEY", ""),
            "app_secret": os.getenv("LS_APP_SECRET", ""),
            "domain": "https://openapi.ls-sec.co.kr:8080",
            "acnt_prdt_cd": "",
        },
    }
    return configs.get(broker_name, {})


BROKER_CONFIG = _get_broker_config(BROKER)

# 계좌번호 확인
if BROKER == "kis" and not BROKER_CONFIG.get("account_no", ""):
    print("경고: BROKER=kis 이지만 KIS_ACCOUNT_NO가 설정되어 있지 않습니다.")

# ── 키움/LS/토스 증권 API 설정 (BROKER_CONFIG에서 관리) ──

# HTTP 타임아웃 설정 (초)
# connect_timeout: 연결 시도 제한 시간
# read_timeout: 응답 수신 제한 시간
# .env 예: CONNECT_TIMEOUT=5, READ_TIMEOUT=30
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT") or os.getenv("KIS_CONNECT_TIMEOUT") or "10")
READ_TIMEOUT = int(os.getenv("READ_TIMEOUT") or os.getenv("KIS_READ_TIMEOUT") or "30")
HTTP_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# 종목 정보
# 여러 종목을 매매하려면 SYMBOLS 환경변수를 사용하세요.
# 사용법: SYMBOLS=TQQQ:NAS,SOXL:AMS
# 단일 종목 방식(SYMBOL, EXCHANGE)은 더 이상 지원하지 않습니다.
#
# 종목별 세부 설정 (환경변수 이름 규칙: {종목코드}_{설정명})
# 예시:
#   TQQQ_SPLITS=40        → TQQQ 분할 수
#   TQQQ_SYMBOL_TYPE=TQQQ → 별지점 공식 타입
#   TQQQ_SEED=10000       → TQQQ에 투입할 시드 (달러, 0이면 계좌 전체 사용)
#   TQQQ_ADDITIONAL_LOC_LEVELS=3  → TQQQ 급락 대비 추가 LOC 단계 수
#   SOXL_SPLITS=20
#   SOXL_SYMBOL_TYPE=SOXL
#   SOXL_SEED=5000
#   SOXL_ADDITIONAL_LOC_LEVELS=3
def _parse_symbols():
	"""
	환경변수에서 종목 목록을 읽어 종목별 설정 dict 리스트로 반환합니다.

	반환 형태:
	  [
	    {
	      "symbol": "TQQQ", "exchange": "NAS",
	      "splits": 40,
	      "symbol_type": "TQQQ",
	      "seed": 10000  # 0이면 계좌 전체 사용
	    },
	    ...
	  ]

	설정 우선순위:
	  1. SYMBOLS=TQQQ:NAS,SOXL:AMS  (복수 종목)
	  2. 기본값: TQQQ(나스닥) + SOXL(아멕스)

	왜 이렇게 바꿨나요?
	  - 단일 변수(SYMBOL, EXCHANGE)와 미사용 변수(TAKE_PROFIT, BIG_BUY_RANGE)를 제거해
	    설정 혼선을 줄였습니다.
	  - 이제 종목별 설정만 보고도 실제 동작을 바로 이해할 수 있습니다.
	"""
	raw = os.getenv("SYMBOLS", "").strip()
	pairs = []
	if raw:
		for item in raw.split(","):
			item = item.strip()
			if not item:
				continue
			if ":" not in item:
				raise ValueError(
					f"잘못된 SYMBOLS 형식: '{item}'. 예시: SYMBOLS=TQQQ:NAS,SOXL:AMS"
				)
			sym, exch = item.split(":", 1)
			sym = sym.strip().upper()
			exch = exch.strip().upper()
			if not sym or not exch:
				raise ValueError(
					f"잘못된 SYMBOLS 항목: '{item}'. 종목코드와 거래소코드를 모두 입력하세요."
				)
			pairs.append((sym, exch))

	if not pairs:
		# 기본값: TQQQ(나스닥) + SOXL
		# SOXL 상장 거래소 분류가 브로커마다 다릅니다:
		#   - KIS: AMEX (AMS)
		#   - Kiwoom: NYSE (NYS)
		# LS/Toss 등 미구현 브로커는 거래소 코드 체계가 정해지지 않았으므로
		# 명시적으로 SYMBOLS 환경변수를 설정해야 합니다.
		if BROKER == "kiwoom":
			pairs = [("TQQQ", "NAS"), ("SOXL", "NYS")]
		else:
			if BROKER != "kis":
				print(
					f"경고: BROKER={BROKER}인데 SYMBOLS가 설정되지 않아 "
					f"KIS 기본값(TQQQ:NAS,SOXL:AMS)을 사용합니다. "
					f"브로커별 거래소 코드가 다를 수 있으니 SYMBOLS를 명시적으로 설정하세요."
				)
			pairs = [("TQQQ", "NAS"), ("SOXL", "AMS")]

	result = []
	for sym, exch in pairs:
		result.append({
			"symbol": sym,
			"exchange": exch,
			# V4는 종목별 분할 수를 직접 사용합니다.
			"splits": int(os.getenv(f"{sym}_SPLITS") or "40"),
			# 시드: 이 종목에 투입할 최대 금액 (달러). 0이면 계좌 전체 주문가능금액 사용
			"seed": float(os.getenv(f"{sym}_SEED") or "0"),
			# 별지점 공식 선택용 종목 타입: "TQQQ" 또는 "SOXL"
			# - TQQQ: 20분할 별% = (15-1.5T)%, 40분할 별% = (15-0.75T)%
			# - SOXL: 20분할 별% = (20-2T)%, 40분할 별% = (20-T)%
			# 미설정 시 종목코드를 그대로 사용 (TQQQ → "TQQQ", SOXL → "SOXL")
			"symbol_type": (os.getenv(f"{sym}_SYMBOL_TYPE") or sym).strip().upper(),
			# 급락 대비 추가 LOC 주문 단계 수
			# 종목별 설정({SYMBOL}_ADDITIONAL_LOC_LEVELS) → 글로벌(ADDITIONAL_LOC_LEVELS) → 기본값 3
			"additional_loc_levels": int(
				os.getenv(f"{sym}_ADDITIONAL_LOC_LEVELS")
				or os.getenv("ADDITIONAL_LOC_LEVELS")
				or "3"
			),
		})
	return result

SYMBOLS = _parse_symbols()

# 계좌 정보
ACNT_PRDT_CD = BROKER_CONFIG.get("acnt_prdt_cd", "01")  # 계좌상품코드 (상품코드)

# 거래 모드
# 환경변수에서 값을 읽어 대문자로 정규화하고 유효성 검사 수행
_trade_mode_raw = os.getenv("TRADE_MODE") or ""
_trade_mode = _trade_mode_raw.strip().upper()
if _trade_mode not in ("DRY", "LIVE"):
	if _trade_mode_raw:
		print(f"경고: 잘못된 TRADE_MODE 값('{_trade_mode_raw}')이 감지되어 'DRY'로 설정합니다.")
	TRADE_MODE = "DRY"
else:
	TRADE_MODE = _trade_mode

# 매매 수수료율
# 한국투자증권 해외주식 기본 수수료: 0.25% (계좌/이벤트에 따라 다를 수 있음)
# .env 예: COMMISSION_RATE=0.0025
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE") or "0.0025")

# 사이클 수익 복리 재투자 여부
# true (기본값): 사이클 종료 후 순수익을 다음 사이클 시드에 자동으로 합산합니다
# 설정 해제 시 false: 매 사이클 동일한 시드로 운용
# .env 예: REINVEST=false
_reinvest_raw = (os.getenv("REINVEST") or "true").strip().lower()
REINVEST = _reinvest_raw == "true"

# Finnhub API 키 (선택 — LS 모의투자 전용 fallback)
# LS 모의투자 환경은 g3101 해외주식 현재가 조회를 지원하지 않으므로,
# Finnhub 무료 API로 대체합니다. 실전 모드에서는 사용되지 않습니다.
# 발급: https://finnhub.io/register (이메일만 있음, 카드 불필요)
# Free tier: 60 calls/min, 실시간 US 시세, 개인용 무료
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

# ── 하위호환 alias (기존 import 경로 유지) ──
KIS_MODE = BROKER_MODE
KIS_DOMAIN = BROKER_CONFIG.get("domain", "")
KIS_APP_KEY = BROKER_CONFIG.get("app_key", "")
KIS_APP_SECRET = BROKER_CONFIG.get("app_secret", "")
KIS_ACCOUNT_NO = BROKER_CONFIG.get("account_no", "")
KIS_TIMEOUT = HTTP_TIMEOUT
KIS_CONNECT_TIMEOUT = CONNECT_TIMEOUT
KIS_READ_TIMEOUT = READ_TIMEOUT


