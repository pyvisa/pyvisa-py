"""Tests for the ONC RPC layer used by VXI-11.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
from contextlib import closing

import pytest

from pyvisa import ResourceManager, constants, errors
from pyvisa_py.protocols import rpc


@pytest.fixture
def closed_port():
    """A port on the loopback interface with nothing listening on it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture
def listening_port():
    """A port that accepts connections. Yields (port, server socket)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server.getsockname()[1], server
    server.close()


def test_connect_reports_failure_for_a_refused_port(closed_port):
    """A refused connection must not be reported as a connected socket.

    connect_ex is non-blocking here, so the result arrives through select.
    A refused connection makes the socket ready with SO_ERROR set, which used
    to be taken for success. The first send then raised BrokenPipeError, and
    that left the VISA call as an OSError rather than a VISA status.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert rpc._connect(sock, "127.0.0.1", closed_port, 2.0) is False
        assert sock.fileno() == -1
    finally:
        sock.close()


def test_connect_reports_success_for_a_listening_port(listening_port):
    port, _server = listening_port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert rpc._connect(sock, "127.0.0.1", port, 2.0) is True
        assert sock.getblocking()
        assert sock.fileno() != -1
    finally:
        sock.close()


def test_client_raises_rpcerror_for_a_refused_port(closed_port):
    """RawTCPClient turns a failed connect into RPCError, which callers map."""
    with pytest.raises(rpc.RPCError):
        rpc.RawTCPClient("127.0.0.1", 0x0607AF, 1, closed_port, open_timeout=2000)


@pytest.fixture(
    params=[-1, 65536, None], ids=["negative-port", "large-port", "dns-error"]
)
def synchronous_failure_socket(request, monkeypatch):
    """Keep real socket lifecycle behavior while controlling DNS failure."""

    class FailingSocket(socket.socket):
        def connect_ex(self, address):
            raise socket.gaierror(
                socket.EAI_NONAME, "controlled name resolution failure"
            )

    port = request.param
    if port is None:
        sock = FailingSocket(socket.AF_INET, socket.SOCK_STREAM)
        port = 1234
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    monkeypatch.setattr(rpc.socket, "socket", lambda *args, **kwargs: sock)
    try:
        yield sock, port
    finally:
        sock.close()


def test_connect_reports_synchronous_failure(synchronous_failure_socket):
    sock, port = synchronous_failure_socket
    assert rpc._connect(sock, "127.0.0.1", port, 0.1) is False
    assert sock.fileno() == -1


def test_client_raises_rpcerror_for_synchronous_failure(synchronous_failure_socket):
    sock, port = synchronous_failure_socket
    with pytest.raises(rpc.RPCError, match="can't connect to server"):
        rpc.RawTCPClient("127.0.0.1", 0x0607AF, 1, port, open_timeout=100)
    assert sock.fileno() == -1


def test_resource_manager_reports_synchronous_failure(synchronous_failure_socket):
    sock, port = synchronous_failure_socket
    with closing(ResourceManager("@py")) as manager:
        sessions = set(manager.visalib.sessions)
        with pytest.raises(errors.VisaIOError) as exc:
            manager.open_resource(
                f"TCPIP0::127.0.0.1,{port}::inst0::INSTR", open_timeout=100
            )
        assert exc.value.error_code == constants.StatusCode.error_resource_not_found
        assert set(manager.visalib.sessions) == sessions
    assert sock.fileno() == -1
