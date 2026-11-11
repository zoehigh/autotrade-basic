# 감시기 DRY 용 읽기 전용 브로커 — 실제 브로커 호출 전에 주문 변경 경로를 막습니다.
#
# 두 겹으로 막습니다 (어느 한쪽이 뚫려도 다른 쪽이 막도록 서로 독립):
#   1) 메서드 허용 목록: 감시기가 쓰는 조회 메서드만 통과. place_order/modify_order/cancel_order 는 물론
#      허용 목록에 없는 모든 속성(미래에 추가될 주문 변경 메서드 포함)이 호출 전에 예외가 됩니다.
#   2) 전송 계층: 브로커의 HTTP 세션(`_session`)의 POST 를 막습니다. 어댑터의 주문·정정·취소는
#      모두 `_session.post` 를 거칩니다.
#
# 인증 토큰 발급은 막지 않습니다: 토큰은 `broker/meritz/auth.py` 가 `requests.post` 로 **세션과 별개로** 요청하므로
# 위 전송 계층 차단(세션 POST)의 대상이 아닙니다. (모든 POST 를 막으면 인증까지 막혀 조회도 못 하게 됩니다.)
#
# 이 모듈은 DRY 감시기에서만 쓰입니다. LIVE 매매 경로(trading_bot.py)와 어댑터는 변경하지 않습니다.


class ReadOnlyViolation(RuntimeError):
    """읽기 전용 모드에서 주문 변경(또는 허용되지 않은) 호출이 시도되었습니다."""


# 감시기(ops/meritz_reprice_watch.py)가 브로커에서 쓰는 메서드 전부. 새 메서드가 필요하면 여기에 의식적으로 추가합니다.
ALLOWED = frozenset({"exchange_code", "get_order_history", "get_stock_price", "close"})


def _blocked_post(*_args, **_kwargs):
    raise ReadOnlyViolation("읽기 전용 모드: POST 요청(주문·정정·취소)이 차단되었습니다")


class ReadOnlyBroker:
    """허용된 조회 메서드만 실제 브로커로 넘기는 얇은 프록시."""

    __slots__ = ("_inner",)

    def __init__(self, broker):
        session = getattr(broker, "_session", None)
        if session is not None:
            # 전송 계층 차단을 설치하지 못하면 읽기 전용을 보장할 수 없으므로 시작 자체를 거부합니다 (fail-closed).
            try:
                session.post = _blocked_post
            except (AttributeError, TypeError) as error:
                raise ReadOnlyViolation(f"브로커 세션의 POST 를 차단하지 못했습니다: {error}") from error
            if getattr(session, "post", None) is not _blocked_post:
                raise ReadOnlyViolation("브로커 세션의 POST 차단이 적용되지 않았습니다")
        object.__setattr__(self, "_inner", broker)

    def __getattr__(self, name):
        if name in ALLOWED:
            return getattr(self._inner, name)
        raise ReadOnlyViolation(f"읽기 전용 모드에서 허용되지 않은 브로커 접근: {name}")

    def __setattr__(self, name, value):
        raise ReadOnlyViolation("읽기 전용 브로커는 수정할 수 없습니다")
