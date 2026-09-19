import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_text() -> str:
    return (ROOT / "pyproject.toml").read_text()


def test_should_block_socket_construction_when_running_under_default_policy() -> None:
    # Given the default pytest policy.
    # When a test tries to construct an internet socket.
    with (
        pytest.warns(UserWarning, match="A test tried to use socket.socket."),
        pytest.raises(Exception) as exc_info,
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Then pytest-socket's concrete block error should be raised.
    assert exc_info.type.__name__ == "SocketBlockedError"


def test_should_expose_network_opt_in_and_disable_default_sockets_when_inspecting_metadata() -> (
    None
):
    # Given the project metadata.
    pyproject = _pyproject_text()

    # When the pytest and Poe contracts are inspected.
    required = (
        "--disable-socket",
        "--allow-unix-socket",
        "live_model: requires network access, explicit consent, and a provider API key",
        "network: requires network access and an explicit opt-in",
        (
            "test = \"pytest -m 'not live_model and not network and not temporal' "
            '--cov=src/agentic_saga --cov-branch --cov-fail-under=90"'
        ),
        "test-network = \"pytest -m 'network' --force-enable-socket\"",
    )

    # Then ordinary tests are network-disabled and network tests are explicitly opt-in.
    assert all(value in pyproject for value in required)
