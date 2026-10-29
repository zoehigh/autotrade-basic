# API 키, 환경변수 등 설정값을 관리하는 파일
import math
import os
from dotenv import load_dotenv

# .env 파일에서 환경변수 읽기
load_dotenv()

# 증권사 선택
# 환경변수 BROKER로 사용할 증권사를 선택합니다.
# 지원: kis(기본값), kiwoom, ls, toss, nhplug, meritz
# .env 예: BROKER=kis
BROKER = os.getenv("BROKER", "kis").strip().lower()

# 브로커 모드 (demo/real)
# 환경변수 BROKER_MODE (우선) 또는 KIS_MODE(하위호환)를 읽습니다.
# 기본값은 demo(모의)입니다. .env 예: BROKER_MODE=real
BROKER_MODE = os.getenv("BROKER_MODE") or os.getenv("KIS_MODE", "demo")
BROKER_MODE = BROKER_MODE.strip().lower()


# 메리츠증권 Open API 운영 서버 기본 주소 (공식 예제 기본값).
# MERITZ_BASE_URL 미설정·빈 값이면 이 주소를 사용합니다.
MERITZ_DEFAULT_BASE_URL = "https://openapi.imeritz.com:9443"


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
        "toss": {
            "client_id": os.getenv("TOSS_APP_KEY", ""),
            "client_secret": os.getenv("TOSS_APP_SECRET", ""),
            "account_seq": os.getenv("TOSS_ACCOUNT_SEQ", ""),
            "domain": "https://openapi.tossinvest.com",
        },
        "nhplug": {
            "app_key": os.getenv("NHPLUG_APP_KEY", ""),
            "app_secret": os.getenv("NHPLUG_APP_SECRET", ""),
            "account_no": os.getenv("NHPLUG_ACCT_NO", ""),
            # 01:실전, 03:모의 (기본값: BROKER_MODE 기반)
            "acct_type": (
                os.getenv("NHPLUG_ACCT_TYPE", "").strip()
                or ("01" if BROKER_MODE == "real" else "03")
            ),
            # NHPLUG_BASE_URL 미설정 시 BROKER_MODE 기반 자동 선택
            "domain": (
                os.getenv("NHPLUG_BASE_URL", "").strip()
                or (
                    "https://api.nhplug.com:8443"
                    if BROKER_MODE == "real"
                    else "https://moapi.nhplug.com:8443"
                )
            ),
            # 토큰 발급 전용 운영 도메인 — 모의투자 서버(moapi)는 토큰 발급을
            # 지원하지 않아 403을 반환하므로, 토큰 발급은 항상 운영 도메인에서 수행합니다.
            "live_domain": "https://api.nhplug.com:8443",
        },
        "meritz": {
            "app_key": os.getenv("MERITZ_APP_KEY", ""),
            "app_secret": os.getenv("MERITZ_APP_SECRET", ""),
            # 계좌번호 없음 — AppKey/AppSecret만으로 계좌가 식별됩니다
            # 미설정·빈 값이면 운영 서버 기본 주소 사용
            "base_url": os.getenv("MERITZ_BASE_URL", "").strip() or MERITZ_DEFAULT_BASE_URL,
            # 단말 식별용 MAC 주소(12자리 HEX) — 미설정 시 uuid.getnode() 유도
            "mac_address": os.getenv("MERITZ_MAC_ADDRESS", "").strip(),
        },
    }
    return configs.get(broker_name, {})


BROKER_CONFIG = _get_broker_config(BROKER)

# 계좌번호 확인
if BROKER == "kis" and not BROKER_CONFIG.get("account_no", ""):
    print("경고: BROKER=kis 이지만 KIS_ACCOUNT_NO가 설정되어 있지 않습니다.")
if BROKER == "toss" and not BROKER_CONFIG.get("account_seq", ""):
    print("경고: BROKER=toss 이지만 TOSS_ACCOUNT_SEQ가 설정되어 있지 않습니다.")
if BROKER == "nhplug" and not BROKER_CONFIG.get("app_key", ""):
    print("경고: BROKER=nhplug 이지만 NHPLUG_APP_KEY가 설정되어 있지 않습니다.")
if BROKER == "nhplug" and not BROKER_CONFIG.get("account_no", ""):
    print("경고: BROKER=nhplug 이지만 NHPLUG_ACCT_NO가 설정되어 있지 않습니다.")
if BROKER == "meritz" and not BROKER_CONFIG.get("app_key", ""):
    print("경고: BROKER=meritz 이지만 MERITZ_APP_KEY가 설정되어 있지 않습니다.")
if BROKER == "meritz" and not BROKER_CONFIG.get("app_secret", ""):
    print("경고: BROKER=meritz 이지만 MERITZ_APP_SECRET이 설정되어 있지 않습니다.")

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
#   TQQQ_SEED=10000       → TQQQ에 투입할 시드 (달러, 필수)
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
	      "seed": 10000  # 달러 금액 (필수)
	    },
	    ...
	  ]

	설정 우선순위:
	  1. SYMBOLS=TQQQ:NAS,SOXL:AMS  (복수 종목)
	  2. 기본값: TQQQ(나스닥) + SOXL(아멕스)

	시드(seed) 설정:
	  - 모든 종목에 대해 시드 설정이 필수입니다.
	  - 달러 금액만 허용 (예: 10000, 5000)
	  - FULL은 지원하지 않습니다 (예측 불가능한 잔고 변동으로 인한 위험)

	왜 이렇게 바꿨나요?
	  - 단일 변수(SYMBOL, EXCHANGE)와 미사용 변수(TAKE_PROFIT, BIG_BUY_RANGE)를 제거해
	    설정 혼선을 줄였습니다.
	  - 이제 종목별 설정만 보고도 실제 동작을 바로 이해할 수 있습니다.
	  - 시드 미설정 시 묵시적으로 계좌 전체를 사용하는 위험을 제거했습니다.
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
		# 키움은 SOXL 거래소가 NYSE(NYS)로 다르므로 브로커별 분기
		if BROKER == "kiwoom":
			pairs = [("TQQQ", "NAS"), ("SOXL", "NYS")]
		else:
			pairs = [("TQQQ", "NAS"), ("SOXL", "AMS")]

	result = []

	for sym, exch in pairs:
		# 시드(seed) 파싱 — 필수 (달러 금액만 허용)
		seed_raw = os.getenv(f"{sym}_SEED", "").strip()
		if not seed_raw:
			raise ValueError(
				f"{sym}_SEED가 설정되지 않았습니다.\n"
				f"  .env에 달러 금액을 추가하세요:\n"
				f"    {sym}_SEED=10000   # 이 종목에 투자할 최대 금액"
			)

		if seed_raw.upper() == "FULL":
			raise ValueError(
				f"FULL은 지원하지 않습니다.\n"
				f"  계좌 잔고는 실시간으로 변동되므로 예측 불가능합니다.\n"
				f"  대신 명확한 달러 금액을 지정하세요:\n"
				f"    {sym}_SEED=10000   # 이 종목에 투자할 최대 금액"
			)

		seed = float(seed_raw)
		if seed <= 0:
			raise ValueError(
				f"{sym}_SEED는 0보다 커야 합니다 (입력값: {seed_raw})"
			)

		# T값 강제 설정 (env var, 1회성 보정용)
		force_t = os.getenv(f"{sym}_FORCE_T", "").strip()
		max_t_raw = os.getenv(f"{sym}_MAX_T", "").strip()
		splits = int(os.getenv(f"{sym}_SPLITS") or "40")

		result.append({
			"symbol": sym,
			"exchange": exch,
			# V4는 종목별 분할 수를 직접 사용합니다.
			"splits": splits,
			"seed": seed,
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
			# T값 강제 설정 (환경변수, 1회성 보정)
			# {SYMBOL}_FORCE_T: 설정 시 state.json의 T를 이 값으로 덮어씁니다
			"force_t": float(force_t) if force_t else None,
			# {SYMBOL}_MAX_T: T 자동추정 결과의 상한선 (기본값: splits — 리버스모드 진입 위해 T=splits 허용)
			"max_t": float(max_t_raw) if max_t_raw else splits,
		})

	return result

SYMBOLS = _parse_symbols()

# 계좌 정보
# ACNT_PRDT_CD는 파일 하단의 하위호환 alias 섹션에서 "01" 고정값으로 정의합니다 (KIS 전용).

# 거래 모드
# 환경변수에서 값을 읽어 대문자로 정규화하고 유효성 검사 수행
_trade_mode_raw = os.getenv("TRADE_MODE") or ""
_trade_mode = _trade_mode_raw.strip().upper()
if _trade_mode not in ("DRY", "LIVE", "REPAIR"):
	if _trade_mode_raw:
		print(f"경고: 잘못된 TRADE_MODE 값('{_trade_mode_raw}')이 감지되어 'DRY'로 설정합니다.")
	TRADE_MODE = "DRY"
else:
	TRADE_MODE = _trade_mode

# ── 파이프라인 상수 (SETTLE | PREVIEW | APPROVE | EXECUTE | VERIFY) ──────────
# 이 절은 파이프라인 설정의 **유일한 정의처**입니다. pipeline.py / broker/__init__.py
# 는 env 를 다시 읽지 말고 여기의 값을 가져다 씁니다.
# 숫자 값이 잘못되면 어느 환경변수인지 알려주며 중단합니다 (무메시지 traceback 방지).


def _env_int(name: str, default: int) -> int:
	"""환경변수를 정수로 읽습니다. 값이 잘못되면 변수명을 알려주고 중단합니다."""
	raw = os.getenv(name)
	if raw is None or not raw.strip():
		return default
	try:
		return int(raw.strip())
	except ValueError:
		raise ValueError(
			f"[config] 환경변수 {name} 는 정수여야 합니다 (현재 값: '{raw}')"
		) from None


def _env_float(name: str, default: float, nonneg: bool = False) -> float:
	"""환경변수를 실수로 읽습니다. 값이 잘못되면 변수명을 알려주고 중단합니다.

	`float("nan")`·`float("inf")` 는 **문자열 파싱에 성공**하므로 파싱만으로는
	막을 수 없습니다. 그런 값은 비교를 무력화합니다 — 예를 들어 지연이 NaN 이면
	`if delay > 0` 이 거짓이 되어 재조회 없이 게이트를 통과하고, 잠금 대기
	시간이 NaN/Infinity 면 `time.monotonic() >= deadline` 이 영원히 거짓이라
	대기 루프가 끝나지 않습니다. 그래서 **모든 경로에서 유한한 값만** 허용하고,
	`nonneg=True` 는 여기에 0 이상 조건을 더합니다.
	기본값은 바꾸지 않고 **오타만 중단**시킵니다 (숫자 오타는 변수명과 함께 중단).
	"""
	raw = os.getenv(name)
	if raw is None or not raw.strip():
		return default
	try:
		value = float(raw.strip())
	except ValueError:
		raise ValueError(
			f"[config] 환경변수 {name} 는 숫자여야 합니다 (현재 값: '{raw}')"
		) from None
	if not math.isfinite(value):
		raise ValueError(
			f"[config] 환경변수 {name} 는 유한한 숫자여야 합니다 "
			f"(nan/infinity 불가, 현재 값: '{raw}')"
		)
	if nonneg and value < 0:
		raise ValueError(
			f"[config] 환경변수 {name} 는 0 이상이어야 합니다 (현재 값: '{raw}')"
		)
	return value


# 기본값 — env 미설정 시 쓰는 값 (여기서만 정의합니다)
DEFAULT_APPROVAL_TTL_SECONDS = 32400
DEFAULT_APPROVAL_MIN_TTL_SECONDS = 28800
DEFAULT_PLAN_DIR = "plans"
DEFAULT_EXECUTION_PRICE_TOLERANCE_ABS = 0.02
DEFAULT_EXECUTION_PRICE_TOLERANCE_PCT = 0.005
DEFAULT_EXECUTION_QTY_TOLERANCE = 0
DEFAULT_HISTORY_SETTLEDNESS_DELAY_SECONDS = 1.0
DEFAULT_RECENT_ORDER_GUARD_MINUTES = 15
DEFAULT_RUN_LOCK_TIMEOUT_SECONDS = 300.0

# 파이프라인 단계 (SETTLE | PREVIEW | APPROVE | EXECUTE | VERIFY)
# 미설정("")이면 기존 통합 실행(정산→계획→주문)으로 동작합니다.
# 조합 검증은 broker.create_broker() / validate_run_phase() 가 브로커 생성 전에 수행합니다.
RUN_PHASE = os.getenv("RUN_PHASE", "").strip().upper()

# 승인 파일이 유효한 시간(초) — APPROVE 후 EXECUTE 전까지의 최대 지연
# 운영 스케줄(SETTLE→PREVIEW→APPROVE→EXECUTE) 간격(약 7.5h)을 덮도록 기본 9h.
APPROVAL_TTL_SECONDS = _env_int("APPROVAL_TTL_SECONDS", DEFAULT_APPROVAL_TTL_SECONDS)
# 너무 짧은 TTL 은 운영 간격을 못 덮으므로 승인 생성 시 거부합니다.
APPROVAL_MIN_TTL_SECONDS = _env_int(
	"APPROVAL_MIN_TTL_SECONDS", DEFAULT_APPROVAL_MIN_TTL_SECONDS
)

# 승인 계획 파일 저장 위치 (PREVIEW 산출물)
PLAN_DIR = os.getenv("PLAN_DIR") or DEFAULT_PLAN_DIR

# EXECUTE 재확인 허용 오차 (승인 시점 대비 현재 재계산 계획)
# 가격: max(절대값, 승인가 × 비율) / 수량: 절대값(기본 0 = 정확히 일치)
# 허용오차는 `normalize_tolerance_policy()` 가 유한·비음수만 통과시키므로,
# env 가 nan/inf/음수면 그 값이 **정책 기본값까지 오염**됩니다 (전 플랜 거부).
# 그래서 여기서 읽을 때 변수명을 알려주며 중단합니다.
EXECUTION_PRICE_TOLERANCE_ABS = _env_float(
	"EXECUTION_PRICE_TOLERANCE_ABS",
	DEFAULT_EXECUTION_PRICE_TOLERANCE_ABS,
	nonneg=True,
)
EXECUTION_PRICE_TOLERANCE_PCT = _env_float(
	"EXECUTION_PRICE_TOLERANCE_PCT",
	DEFAULT_EXECUTION_PRICE_TOLERANCE_PCT,
	nonneg=True,
)
EXECUTION_QTY_TOLERANCE = _env_int(
	"EXECUTION_QTY_TOLERANCE", DEFAULT_EXECUTION_QTY_TOLERANCE
)

# 지연 이력(메리츠 등) 정착성 확인 — 반복 조회로 이력이 바뀌면 정산을 보류합니다.
HISTORY_SETTLEDNESS_CHECK = os.getenv("HISTORY_SETTLEDNESS_CHECK", "").strip().lower()
# 재조회 지연은 **정산 게이트** 값입니다. NaN 이면 `delay > 0` 이 거짓이 되어
# 재조회 없이 통과하므로 유한·비음수만 허용합니다 (음수/NaN 은 변수명과 함께 중단).
HISTORY_SETTLEDNESS_DELAY_SECONDS = _env_float(
	"HISTORY_SETTLEDNESS_DELAY_SECONDS",
	DEFAULT_HISTORY_SETTLEDNESS_DELAY_SECONDS,
	nonneg=True,
)
RECENT_ORDER_GUARD_MINUTES = _env_int(
	"RECENT_ORDER_GUARD_MINUTES", DEFAULT_RECENT_ORDER_GUARD_MINUTES
)

# ── 호출 시점 env 를 읽는 함수 ──────────────────────────────────────────────
# 아래 값들은 **실행 시점의 env** 를 반영해야 합니다 (검증 게이트·테스트에서 env 를
# 바꿔 끼우기 때문). 규칙(이름·기본값·정규화)은 위 상수와 같은 곳에서만 정의합니다.


def read_run_phase() -> str:
	"""호출 시점 env 의 RUN_PHASE (공백 제거 + 대문자, 미설정이면 "")."""
	return os.getenv("RUN_PHASE", "").strip().upper()


def read_history_settledness_flag() -> str:
	"""호출 시점 env 의 HISTORY_SETTLEDNESS_CHECK (공백 제거 + 소문자)."""
	return os.getenv("HISTORY_SETTLEDNESS_CHECK", "").strip().lower()


def read_history_settledness_delay_seconds() -> float:
	"""호출 시점 env 의 이력 재조회 지연(초). 잘못된 값이면 변수명과 함께 중단.

	정산 게이트를 좌우하므로 **유한한 0 이상** 값만 허용합니다 (NaN 이면
	재조회 없이 정착성 검사가 통과합니다).
	"""
	return _env_float(
		"HISTORY_SETTLEDNESS_DELAY_SECONDS",
		DEFAULT_HISTORY_SETTLEDNESS_DELAY_SECONDS,
		nonneg=True,
	)


def read_run_lock_timeout_seconds() -> float:
	"""호출 시점 env 의 전역 실행 잠금 대기 시간(초). 잘못된 값이면 중단.

	NaN/Infinity 는 대기 마감(`time.monotonic() >= deadline`)을 영원히 거짓으로
	만들어 잠금 루프가 끝나지 않으므로 유한한 0 이상 값만 허용합니다.
	"""
	return _env_float(
		"RUN_LOCK_TIMEOUT_SECONDS",
		DEFAULT_RUN_LOCK_TIMEOUT_SECONDS,
		nonneg=True,
	)


def read_recent_order_guard_minutes() -> int:
	"""호출 시점 env 의 최근 미종결 주문 guard 시간(분)."""
	return _env_int(
		"RECENT_ORDER_GUARD_MINUTES", DEFAULT_RECENT_ORDER_GUARD_MINUTES
	)

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

# T값 강제 재추정 플래그
# true로 설정 시 state.json의 last_updated를 초기화하여 전체 주문 이력에서 T를 재추정합니다.
# GitHub Actions에서 state 캐시가 깨졌거나 state 없이 시작한 경우 사용합니다.
# .env 예: FORCE_T_REINFERENCE=true
FORCE_T_REINFERENCE = os.getenv("FORCE_T_REINFERENCE", "").strip().lower() == "true"

# REPAIR 모드 액션 지정
# TRADE_MODE=REPAIR에서 수행할 작업을 지정합니다.
# 단일 값만 허용하며, 미지정 또는 복수 지정 시 sys.exit(1)으로 즉시 중단합니다.
# 사용 가능한 값:
#   DIAGNOSTIC, REVERSE_AUDIT, REVERSE_RECONCILE, REVERSE_RESET, REVERSE_T_FIX,
#   FENCE_CLEAR, NET_INVESTED_REPAIR, ASSUME_EXPIRY, REINFERENCE, FORCE_T
# .env 예: REPAIR_ACTION=DIAGNOSTIC
REPAIR_ACTION = os.getenv("REPAIR_ACTION", "").strip().upper()

# Finnhub API 키 (선택 — LS 모의투자 전용 fallback)
# LS 모의투자 환경은 g3101 해외주식 현재가 조회를 지원하지 않으므로,
# Finnhub 무료 API로 대체합니다. 실전 모드에서는 사용되지 않습니다.
# 발급: https://finnhub.io/register (이메일만 있음, 카드 불필요)
# Free tier: 60 calls/min, 실시간 US 시세, 개인용 무료
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

# 주문이력 상세 출력 여부
# true: LIVE 모드에서도 주문 이력 요약([주문이력 요약] 섹션)을 출력합니다.
# DRY 모드는 항상 출력합니다.
# .env 예: ORDER_HISTORY_VERBOSE=true
ORDER_HISTORY_VERBOSE = os.getenv("ORDER_HISTORY_VERBOSE", "false").strip().lower() == "true"

# LS 모의투자 API 버그 우회 플래그
# LS 모의투자 환경에서는 다음 API 문제가 발생합니다:
#   1. COSOQ00201(잔고조회): IGW40014 서버 고정폭 변환 오류
#   2. COSAQ00102(체결내역): 모의투자 미지원 (01900)
# 이 플래그가 true이면:
#   - 잔고 조회 실패 시 예외 대신 None 반환 → 보수적 T 유지
#   - 체결내역 조회 실패 시 빈 리스트 반환
# GitHub Actions: ls-demo 환경에서 자동으로 true 설정
# .env 예: LS_DEMO_BYPASS_BUGS=true
LS_DEMO_BYPASS_BUGS = os.getenv("LS_DEMO_BYPASS_BUGS", "false").strip().lower() == "true"

# ── 하위호환 alias (기존 import 경로 유지) ──
# 주의: BROKER_CONFIG 경유가 아니라 KIS_* 환경변수에서 직접 읽습니다.
#   → .env의 BROKER 값이 다른 브로커여도 KIS 자격증명이 설정돼 있으면
#     KISBroker/KIS auth가 정상 동작합니다 (BROKER 값과 무관).
#   BROKER=kis일 때는 기존과 동일한 값이므로 런타임 봇 동작은 불변입니다.
KIS_MODE = BROKER_MODE
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
KIS_DOMAIN = (
    "https://openapi.koreainvestment.com:9443"
    if BROKER_MODE == "real"
    else "https://openapivts.koreainvestment.com:29443"
)
KIS_TIMEOUT = HTTP_TIMEOUT
KIS_CONNECT_TIMEOUT = CONNECT_TIMEOUT
KIS_READ_TIMEOUT = READ_TIMEOUT
ACNT_PRDT_CD = "01"  # KIS 계좌상품코드 (고정값, BROKER_CONFIG 경유 제거)


