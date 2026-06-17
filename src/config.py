# API 키, 환경변수 등 설정값을 관리하는 파일
import os
from dotenv import load_dotenv

# .env 파일에서 환경변수 읽기
load_dotenv()

# 한국투자증권 API 설정
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
# 계좌번호는 KIS_ACCOUNT_NO 하나로 관리합니다.
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")

# 한국투자증권 API 엔드포인트
# 환경변수 `KIS_MODE`로 demo(모의) 또는 real(실전) 환경을 선택합니다.
# 기본값은 demo(모의)입니다. .env 예: KIS_MODE=real
KIS_MODE = os.getenv("KIS_MODE", "demo").strip().lower()
if KIS_MODE == "real":
	KIS_DOMAIN = "https://openapi.koreainvestment.com:9443"
else:
	KIS_DOMAIN = "https://openapivts.koreainvestment.com:29443"

if not KIS_ACCOUNT_NO:
	print(f"경고: KIS_MODE={KIS_MODE} 이지만 KIS_ACCOUNT_NO가 설정되어 있지 않습니다.")

# HTTP 타임아웃 설정 (초)
# connect_timeout: 연결 시도 제한 시간
# read_timeout: 응답 수신 제한 시간
# .env 예: KIS_CONNECT_TIMEOUT=5, KIS_READ_TIMEOUT=30
KIS_CONNECT_TIMEOUT = int(os.getenv("KIS_CONNECT_TIMEOUT") or "10")
KIS_READ_TIMEOUT = int(os.getenv("KIS_READ_TIMEOUT") or "30")
KIS_TIMEOUT = (KIS_CONNECT_TIMEOUT, KIS_READ_TIMEOUT)

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
		# 기본값: TQQQ(나스닥) + SOXL(아멕스)
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
			"symbol_type": os.getenv(f"{sym}_SYMBOL_TYPE", sym).strip().upper(),
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
ACNT_PRDT_CD = "01"  # 계좌상품코드 (상품코드)

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
# true: 사이클 종료 후 순수익을 다음 사이클 시드에 자동으로 합산합니다
# false (기본값): 매 사이클 동일한 시드로 운용합니다
# .env 예: REINVEST=true
_reinvest_raw = os.getenv("REINVEST", "false").strip().lower()
REINVEST = _reinvest_raw == "true"
