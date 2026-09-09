from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from socket import SHUT_RDWR, socket
from socketserver import BaseServer
from threading import BoundedSemaphore, Lock, Thread, Timer, current_thread
from typing import Final
from urllib.parse import unquote, urlsplit

_CSP: Final[str] = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; font-src 'self'; script-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
_MAX_PATH: Final[int] = 240
_MAX_HEADERS: Final[int] = 40
_MAX_HEADER_BYTES: Final[int] = 16 * 1024
_MAX_FILE_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_PORT: Final[int] = 65535
_MAX_CONCURRENT_HANDLERS: Final[int] = 8
_READ_DEADLINE_SECONDS: Final[float] = 1.0
_LOOPBACK_HOST: Final[str] = "127.0.0.1"
_MIME: Final[dict[str, str]] = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
type _AcceptedRequest = socket | tuple[bytes, socket]


class RecorderServerError(ValueError):
    """Raised when a recorder server cannot be safely started."""


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = _MAX_CONCURRENT_HANDLERS

    def configure_limits(self) -> None:
        self._handler_slots = BoundedSemaphore(_MAX_CONCURRENT_HANDLERS)
        self._active_lock = Lock()
        self._active_requests: set[socket] = set()
        self._read_timers: dict[socket, Timer] = {}

    def verify_request(self, request: _AcceptedRequest, client_address: tuple[str, int]) -> bool:
        del client_address
        if not self._handler_slots.acquire(blocking=False):
            return False
        self._track_request(_request_socket(request))
        return True

    def process_request_thread(
        self, request: _AcceptedRequest, client_address: tuple[str, int]
    ) -> None:
        current_thread().name = "saga-recorder-request"
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish_request(request)

    def complete_read(self, request: _AcceptedRequest) -> None:
        request_socket = _request_socket(request)
        with self._active_lock:
            timer = self._read_timers.pop(request_socket, None)
        if timer is not None:
            timer.cancel()
            timer.join()

    def close_active_requests(self) -> None:
        with self._active_lock:
            requests = tuple(self._active_requests)
        for request in requests:
            self._expire_request(request)

    def _finish_request(self, request: _AcceptedRequest) -> None:
        self.complete_read(request)
        with self._active_lock:
            self._active_requests.discard(_request_socket(request))
        self._handler_slots.release()

    def _track_request(self, request: socket) -> None:
        timer = Timer(_READ_DEADLINE_SECONDS, self._expire_request, args=(request,))
        timer.name = "saga-recorder-deadline"
        with self._active_lock:
            self._active_requests.add(request)
            self._read_timers[request] = timer
        timer.start()

    @staticmethod
    def _expire_request(request: _AcceptedRequest) -> None:
        try:
            _request_socket(request).shutdown(SHUT_RDWR)
        except OSError:
            return

    @property
    def authority(self) -> str:
        return f"{_LOOPBACK_HOST}:{self.server_port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"


@dataclass(frozen=True)
class RecorderServer:
    """Own a loopback-only Flight Recorder server and its shutdown lifecycle."""

    _httpd: _LoopbackServer
    _thread: Thread

    @property
    def host(self) -> str:
        return _LOOPBACK_HOST

    @property
    def port(self) -> int:
        return int(self._httpd.server_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def wait(self) -> None:
        self._thread.join()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.close_active_requests()
        self._httpd.server_close()
        self._thread.join()

    def __enter__(self) -> RecorderServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()


class RecorderRequestHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        directory: str,
        verbose: bool,
    ) -> None:
        self._root = Path(directory).resolve(strict=True)
        self._verbose = verbose
        super().__init__(request, client_address, server, directory=directory)

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        self._loopback_server().complete_read(self.request)
        return parsed and self._headers_are_bounded() and self._request_metadata_is_safe()

    def handle(self) -> None:
        try:
            super().handle()
        except ConnectionError:
            return

    def send_response(self, code: int, message: str | None = None) -> None:
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def _headers_are_bounded(self) -> bool:
        size = sum(len(name) + len(value) for name, value in self.headers.items())
        if len(self.headers) <= _MAX_HEADERS and size <= _MAX_HEADER_BYTES:
            return True
        self.send_error(431)
        return False

    def _request_metadata_is_safe(self) -> bool:
        server = self._loopback_server()
        if self.headers.get_all("Host", []) != [server.authority]:
            self.send_error(421)
            return False
        if not self._browser_metadata_is_safe(server):
            self.send_error(403)
            return False
        return True

    def _browser_metadata_is_safe(self, server: _LoopbackServer) -> bool:
        origins = self.headers.get_all("Origin", [])
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
        origin_is_safe = not origins or origins == [server.origin]
        fetch_is_safe = not fetch_sites or fetch_sites in (["same-origin"], ["none"])
        return origin_is_safe and fetch_is_safe

    def _loopback_server(self) -> _LoopbackServer:
        if not isinstance(self.server, _LoopbackServer):
            raise RecorderServerError("recorder server is invalid")
        return self.server

    def do_GET(self) -> None:
        self._serve(False)

    def do_HEAD(self) -> None:
        self._serve(True)

    def do_POST(self) -> None:
        self.send_error(405)

    do_PUT = do_POST  # noqa: N815
    do_DELETE = do_POST  # noqa: N815
    do_PATCH = do_POST  # noqa: N815
    do_OPTIONS = do_POST  # noqa: N815

    def _serve(self, head_only: bool) -> None:
        target = self._target()
        if target is None:
            self.send_error(404)
            return
        self._send_file(target, head_only)

    def _target(self) -> Path | None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment or "\x00" in parsed.path or len(parsed.path) > _MAX_PATH:
            return None
        relative = PurePosixPath(unquote(parsed.path).lstrip("/"))
        return self._safe_file(relative)

    def _safe_file(self, relative: PurePosixPath) -> Path | None:
        parts = relative.parts or ("index.html",)
        if not _has_safe_parts(parts):
            return None
        target = self._root.joinpath(*parts)
        return target if _is_safe_file(target, self._root) else None

    def _send_file(self, target: Path, head_only: bool) -> None:
        mime = _MIME.get(target.suffix)
        size = target.stat().st_size
        if mime is None or size > _MAX_FILE_BYTES:
            self.send_error(404)
            return
        body = b"" if head_only else target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if self._verbose:
            super().log_message(format, *args)


def _has_symlink(target: Path, root: Path) -> bool:
    current = target
    while current != root:
        if current.is_symlink():
            return True
        current = current.parent
    return root.is_symlink()


def _request_socket(request: _AcceptedRequest) -> socket:
    return request[1] if isinstance(request, tuple) else request


def _has_safe_parts(parts: tuple[str, ...]) -> bool:
    return all(_is_safe_path_part(part) for part in parts)


def _is_safe_path_part(part: str) -> bool:
    return part not in {"", ".", ".."} and "\\" not in part


def _is_safe_file(target: Path, root: Path) -> bool:
    return _is_regular_file(target, root) and _is_within(target.resolve(), root)


def _is_regular_file(target: Path, root: Path) -> bool:
    return target.is_file() and not _has_symlink(target, root)


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def serve_recorder(directory: Path, *, port: int = 0, verbose: bool = False) -> RecorderServer:
    """Serve a materialized recorder directory on loopback until shut down."""
    root = _require_directory(directory)
    _require_port(port)
    handler = partial(RecorderRequestHandler, directory=str(root), verbose=verbose)
    httpd = _LoopbackServer((_LOOPBACK_HOST, port), handler)
    httpd.configure_limits()
    thread = Thread(target=httpd.serve_forever, name="saga-flight-recorder", daemon=True)
    thread.start()
    return RecorderServer(httpd, thread)


def _require_directory(directory: Path) -> Path:
    root = directory.resolve(strict=True)
    if directory.is_symlink() or not root.is_dir():
        raise RecorderServerError("recorder directory is unsafe")
    return root


def _require_port(port: int) -> None:
    if type(port) is not int or not 0 <= port <= _MAX_PORT:
        raise RecorderServerError("recorder port is invalid")


__all__ = ["RecorderServer", "RecorderServerError", "serve_recorder"]
