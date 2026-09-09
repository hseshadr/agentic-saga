from __future__ import annotations

from contextlib import ExitStack, closing
from http.client import HTTPConnection
from pathlib import Path
from socket import SO_LINGER, SOL_SOCKET, socket
from struct import pack
from threading import Thread
from threading import enumerate as enumerate_threads
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agentic_saga.contracts.trace import RunTrace
from agentic_saga.demo.assets import materialize_recorder_site
from agentic_saga.demo.server import RecorderServer, RecorderServerError, serve_recorder

pytestmark = pytest.mark.enable_socket


def _site(tmp_path: Path) -> Path:
    trace = RunTrace.model_validate_json(
        Path("examples/ecommerce/flight-recorder/traces/happy-path.json").read_bytes(), strict=True
    )
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": trace})
    return directory


def test_should_serve_only_loopback_with_security_headers(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When
    with (
        serve_recorder(directory, port=0) as server,
        closing(
            urlopen(f"{server.url}/index.html")  # noqa: S310
        ) as response,
    ):
        status = response.status
        headers = response.headers

    # Then
    assert server.host == "127.0.0.1"
    assert status == 200
    assert headers["Content-Security-Policy"] == (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; font-src 'self'; script-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Server"] is None


def test_should_return_no_body_when_request_is_head(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When
    with (
        serve_recorder(directory) as server,
        closing(
            urlopen(Request(f"{server.url}/index.html", method="HEAD"))  # noqa: S310
        ) as response,
    ):
        status = response.status
        body = response.read()

    # Then
    assert status == 200
    assert body == b""


def test_should_reject_path_escape_when_serving(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with serve_recorder(directory) as server:
        _assert_rejected(f"{server.url}/%2e%2e/pyproject.toml", 404)


def test_should_reject_non_read_request_when_serving(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with serve_recorder(directory) as server:
        request = Request(f"{server.url}/index.html", method="POST")  # noqa: S310
        _assert_rejected(request, 405)


def test_should_not_list_directories_when_serving(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with serve_recorder(directory) as server:
        _assert_rejected(f"{server.url}/traces/", 404)


@pytest.mark.parametrize("suffix", ["?query", "%00", "bad%5Cpath"])
def test_should_reject_unsafe_path_when_serving(tmp_path: Path, suffix: str) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with serve_recorder(directory) as server:
        _assert_rejected(f"{server.url}/index.html{suffix}", 404)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS"])
def test_should_reject_write_method_when_serving(tmp_path: Path, method: str) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with serve_recorder(directory) as server:
        request = Request(f"{server.url}/index.html", method=method)  # noqa: S310
        _assert_rejected(request, 405)


def test_should_reject_unknown_or_oversized_file_when_serving(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)
    (directory / "unknown.txt").write_text("not served")
    (directory / "oversized.json").write_bytes(b"x" * (8 * 1024 * 1024 + 1))

    # When / Then
    with serve_recorder(directory) as server:
        for name in ("unknown.txt", "oversized.json"):
            _assert_rejected(f"{server.url}/{name}", 404)


def test_should_reject_excessive_request_headers_when_serving(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)
    headers = {f"X-Header-{number}": "x" for number in range(41)}

    # When / Then
    with serve_recorder(directory) as server:
        request = Request(f"{server.url}/index.html", headers=headers)  # noqa: S310
        _assert_rejected(request, 431)


def _assert_rejected(request: str | Request, status: int) -> None:
    with pytest.raises(HTTPError) as error:
        urlopen(request)  # noqa: S310
    try:
        assert error.value.code == status
    finally:
        error.value.close()


@pytest.mark.parametrize(
    "host",
    ["attacker.example", "127.0.0.1", "127.0.0.1:abc", "localhost:{port}"],
)
def test_should_reject_foreign_or_malformed_host_when_serving(tmp_path: Path, host: str) -> None:
    # Given
    with serve_recorder(_site(tmp_path)) as server:
        supplied_host = host.format(port=server.port)

        # When
        status = _request_status(server, (("Host", supplied_host),), skip_host=True)

    # Then
    assert status == 421


@pytest.mark.parametrize("hosts", [(), (("Host", "first.example"), ("Host", "second.example"))])
def test_should_reject_missing_or_duplicate_host_when_serving(
    tmp_path: Path, hosts: tuple[tuple[str, str], ...]
) -> None:
    # Given / When
    with serve_recorder(_site(tmp_path)) as server:
        status = _request_status(server, hosts, skip_host=True)

    # Then
    assert status == 421


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "https://{authority}"])
def test_should_reject_foreign_origin_when_serving(tmp_path: Path, origin: str) -> None:
    # Given
    with serve_recorder(_site(tmp_path)) as server:
        supplied_origin = origin.format(authority=f"{server.host}:{server.port}")

        # When
        status = _request_status(server, (("Origin", supplied_origin),))

    # Then
    assert status == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none "])
def test_should_reject_non_local_fetch_metadata_when_serving(tmp_path: Path, site: str) -> None:
    # Given / When
    with serve_recorder(_site(tmp_path)) as server:
        status = _request_status(server, (("Sec-Fetch-Site", site),))

    # Then
    assert status == 403


def test_should_allow_same_origin_browser_read_when_serving(tmp_path: Path) -> None:
    # Given
    with serve_recorder(_site(tmp_path)) as server:
        authority = f"{server.host}:{server.port}"
        headers = (("Origin", f"http://{authority}"), ("Sec-Fetch-Site", "same-origin"))

        # When
        status = _request_status(server, headers)

    # Then
    assert status == 200


def test_should_allow_direct_browser_navigation_when_serving(tmp_path: Path) -> None:
    # Given / When
    with serve_recorder(_site(tmp_path)) as server:
        status = _request_status(server, (("Sec-Fetch-Site", "none"),))

    # Then
    assert status == 200


@pytest.mark.parametrize("name", ["Origin", "Sec-Fetch-Site"])
def test_should_reject_ambiguous_browser_metadata_when_serving(tmp_path: Path, name: str) -> None:
    # Given / When
    with serve_recorder(_site(tmp_path)) as server:
        value = f"http://{server.host}:{server.port}" if name == "Origin" else "same-origin"
        headers = ((name, value), (name, value))
        status = _request_status(server, headers)

    # Then
    assert status == 403


def _request_status(
    server: RecorderServer,
    headers: tuple[tuple[str, str], ...],
    *,
    skip_host: bool = False,
) -> int:
    connection = HTTPConnection(server.host, server.port, timeout=2)
    connection.putrequest("GET", "/index.html", skip_host=skip_host)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    status = response.status
    response.read()
    connection.close()
    return status


def test_should_reject_invalid_port_when_starting_server(tmp_path: Path) -> None:
    # Given
    directory = _site(tmp_path)

    # When / Then
    with pytest.raises(RecorderServerError):
        serve_recorder(directory, port=65536)


def test_should_allow_idempotent_concurrent_shutdown_when_serving(tmp_path: Path) -> None:
    # Given
    server = serve_recorder(_site(tmp_path))
    first = Thread(target=server.shutdown)
    second = Thread(target=server.shutdown)

    # When
    first.start()
    second.start()
    first.join()
    second.join()

    # Then
    assert not first.is_alive()
    assert not second.is_alive()


def test_should_expire_incomplete_headers_within_hard_deadline(tmp_path: Path) -> None:
    # Given
    with serve_recorder(_site(tmp_path)) as server, closing(_partial_request(server)) as client:
        started = monotonic()

        # When
        response = client.recv(1)

    # Then
    assert response == b""
    assert monotonic() - started < 2.5


def test_should_reject_connections_above_concurrent_handler_ceiling(tmp_path: Path) -> None:
    # Given
    with serve_recorder(_site(tmp_path)) as server, ExitStack() as stack:
        clients = [stack.enter_context(closing(_partial_request(server))) for _ in range(8)]
        overflow = stack.enter_context(closing(_partial_request(server)))

        # When / Then
        rejected_during_send = _finish_headers(overflow)
        assert rejected_during_send or _connection_is_closed(overflow)
        _complete_partial_requests(clients)


def test_should_release_incomplete_requests_and_threads_on_shutdown(tmp_path: Path) -> None:
    # Given
    server = serve_recorder(_site(tmp_path))
    with ExitStack() as stack:
        clients = [stack.enter_context(closing(_partial_request(server))) for _ in range(3)]
        assert _request_status(server, ()) == 200

        # When
        server.shutdown()
        responses = [_connection_is_closed(client) for client in clients]

    # Then
    assert all(responses)
    assert not _recorder_threads()


def test_should_ignore_reset_client_without_stderr_noise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given / When
    with serve_recorder(_site(tmp_path)) as server:
        _reset_request(server)
        sleep(0.1)

    # Then
    assert capsys.readouterr().err == ""


def _partial_request(server: RecorderServer) -> socket:
    client = socket()
    try:
        client.settimeout(3)
        client.connect((server.host, server.port))
        authority = f"{server.host}:{server.port}"
        client.sendall(f"GET /index.html HTTP/1.1\r\nHost: {authority}".encode())
    except OSError:
        client.close()
        raise
    return client


def _complete_request(server: RecorderServer) -> socket:
    client = _partial_request(server)
    try:
        client.sendall(b"\r\n\r\n")
    except OSError:
        client.close()
        raise
    return client


def _finish_headers(client: socket) -> bool:
    try:
        client.sendall(b"\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        return True
    return False


def _complete_partial_requests(clients: list[socket]) -> None:
    for client in clients:
        client.sendall(b"\r\n\r\n")
        assert client.recv(64).startswith(b"HTTP/1.0 200")


def _connection_is_closed(client: socket) -> bool:
    try:
        return client.recv(1) == b""
    except ConnectionResetError:
        return True


def _recorder_threads() -> list[Thread]:
    prefixes = ("saga-recorder-request", "saga-recorder-deadline")
    return [thread for thread in enumerate_threads() if thread.name.startswith(prefixes)]


def _reset_request(server: RecorderServer) -> None:
    client = _complete_request(server)
    try:
        client.setsockopt(SOL_SOCKET, SO_LINGER, pack("ii", 1, 0))
    finally:
        client.close()
