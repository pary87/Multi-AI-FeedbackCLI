"""Keep the app private to this computer's own browser."""

from __future__ import annotations


class LocalOnly:
    """Refuse any request whose Host or Origin is not this computer.

    The app is bound to 127.0.0.1, so other computers cannot reach it. This also
    stops a web page you visit from talking to it through DNS rebinding or a
    cross-site form, since those carry a foreign Host or Origin header.
    """

    def __init__(self, app_, port: int):
        self.app = app_
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            host = headers.get("host", "")
            origin = headers.get("origin")
            if host not in self.hosts or (origin is not None and origin not in self.origins):
                if scope["type"] == "websocket":
                    await receive()
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = b"Council only answers requests from this computer's own browser."
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
