"""Tests for the ONC RPC layer used by VXI-11.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
import struct
import threading
import time
from contextlib import closing
from unittest.mock import MagicMock

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


def _fragment(payload: bytes, last: bool = True) -> bytes:
    """Frame payload as one ONC RPC record-marking fragment."""
    return struct.pack(">I", len(payload) | (0x80000000 if last else 0)) + payload


@pytest.fixture
def peer():
    """A connected socket pair: (our end, the remote end)."""
    ours, theirs = socket.socketpair()
    yield ours, theirs
    ours.close()
    theirs.close()


def _run_in_thread(target, timeout=2.0):
    """Run target in a daemon thread; return (finished, result or exception)."""
    outcome = {}

    def runner():
        try:
            outcome["result"] = target()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), outcome


@pytest.mark.parametrize(
    "sent",
    [
        b"",
        b"\x80\x00",
        _fragment(b"abcd", last=False),
        struct.pack(">I", 0x80000008) + b"abc",
    ],
    ids=["nothing", "partial-header", "non-final-fragment", "partial-fragment"],
)
def test_recvrecord_raises_eof_promptly_when_peer_closes(peer, sent):
    """A closed connection is reported at once, not after the timeout.

    select() reports a closed socket as readable and recv() then returns
    b"". That used to be treated as "no data yet", so the loop spun until
    the timeout and raised socket.timeout, making a dropped connection look
    like a slow instrument.
    """
    ours, theirs = peer
    theirs.sendall(sent)
    theirs.close()

    start = time.monotonic()
    with pytest.raises(EOFError):
        rpc._recvrecord(ours, 5.0)
    assert time.monotonic() - start < 1.0


def test_recvrecord_without_timeout_raises_eof_when_peer_closes(peer):
    """With no timeout (as TCPServer.session uses), EOF must still end the call."""
    ours, theirs = peer
    theirs.close()

    finished, outcome = _run_in_thread(lambda: rpc._recvrecord(ours, None))

    assert finished, "_recvrecord kept waiting on a closed connection"
    assert isinstance(outcome.get("error"), EOFError)


def test_recvrecord_returns_complete_record_sent_before_close(peer):
    ours, theirs = peer
    theirs.sendall(_fragment(b"abcd"))
    theirs.close()

    assert rpc._recvrecord(ours, 5.0) == b"abcd"


def test_recvrecord_keeps_min_packages_workaround_on_close(peer):
    """A close after the requested packages still returns what arrived.

    min_packages exists for instruments that never send the final fragment
    of a portmapper reply; that must keep working if they also close.
    """
    ours, theirs = peer
    theirs.sendall(_fragment(b"abcd", last=False))
    theirs.close()

    assert rpc._recvrecord(ours, 5.0, min_packages=1) == b"abcd"


def test_recvrecord_still_times_out_when_peer_is_silent(peer):
    ours, _theirs = peer

    with pytest.raises(socket.timeout):
        rpc._recvrecord(ours, 0.2)


def test_tcp_server_session_ends_when_client_disconnects(peer):
    """TCPServer.session relies on _recvrecord raising EOFError to exit."""
    ours, theirs = peer
    server = object.__new__(rpc.TCPServer)
    server.handle = MagicMock()
    theirs.close()

    finished, outcome = _run_in_thread(lambda: server.session((ours, ("127.0.0.1", 0))))

    assert finished, "TCPServer.session kept running after the client left"
    assert "error" not in outcome
    server.handle.assert_not_called()
