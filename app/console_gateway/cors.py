from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ALLOWED_METHODS = "GET, OPTIONS"
_ALLOWED_HEADERS = "Authorization"
_PREFLIGHT_MAX_AGE_SECONDS = "600"


class OriginCorsMiddleware:
    """허용 Origin(`app.state.console_allowed_origins`, 요청 때 읽는다)에만 CORS 를 연다.

    Gateway 는 쿠키를 쓰지 않고 Bearer ticket 만 받으므로 credentials 는 열지 않는다. 설정은
    앱이 만들어진 뒤(lifespan)에 읽혀서 Starlette 의 CORSMiddleware 를 쓰지 못한다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        allowed: frozenset[str] = scope["app"].state.console_allowed_origins
        if origin is None or origin not in allowed:
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": _ALLOWED_METHODS,
                    "Access-Control-Allow-Headers": _ALLOWED_HEADERS,
                    "Access-Control-Max-Age": _PREFLIGHT_MAX_AGE_SECONDS,
                    "Vary": "Origin",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Access-Control-Allow-Origin"] = origin
                response_headers.append("Vary", "Origin")
            await send(message)

        await self.app(scope, receive, send_with_cors)
