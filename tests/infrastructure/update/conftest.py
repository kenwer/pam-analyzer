"""Fixtures for the update feed and download tests."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@dataclass
class Route:
    body: bytes
    status: int = 200
    content_type: str = "application/octet-stream"


@dataclass
class FakeServer:
    """A local HTTP server whose answers each test configures.

    honor_range: answer "Range: bytes=N-" with 206, else ignore it (200).
    cut_after: on the next request only, send this many body bytes, then drop the connection.
    """

    port: int = 0
    routes: dict[str, Route] = field(default_factory=dict)
    honor_range: bool = True
    cut_after: int | None = None
    requests: list[dict[str, str]] = field(default_factory=list)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def _handler(server_state: FakeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802  http.server API
            server_state.requests.append({"path": self.path, **dict(self.headers)})
            route = server_state.routes.get(self.path)
            if route is None:
                self.send_error(404)
                return
            body, status = route.body, route.status
            start = 0
            range_header = self.headers.get("Range")
            if status == 200 and range_header and server_state.honor_range:
                start = int(range_header.removeprefix("bytes=").rstrip("-"))
                status = 206
            chunk = body[start:]
            self.send_response(status)
            self.send_header("Content-Type", route.content_type)
            self.send_header("Content-Length", str(len(chunk)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.end_headers()
            if server_state.cut_after is not None:
                cut, server_state.cut_after = server_state.cut_after, None
                self.wfile.write(chunk[:cut])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(chunk)

    return Handler


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        pass  # a client hanging up mid-body is what the cancel tests do


@pytest.fixture
def local_http_server() -> Iterator[FakeServer]:
    state = FakeServer()
    httpd = _QuietServer(("127.0.0.1", 0), _handler(state))
    state.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


def no_progress(_done: int, _total: int) -> None:
    pass


def never() -> bool:
    return False
