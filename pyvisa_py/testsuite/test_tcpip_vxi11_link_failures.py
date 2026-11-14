# -*- coding: utf-8 -*-
"""Reproductions for how TCPIPInstrVxi11 reports a failed VXI-11 link.

These tests drive a real CoreClient over a loopback socket and make the
"instrument" end of the connection fail in different ways. Each test asserts
the behaviour a VISA user needs: a status code that identifies a lost
connection rather than a timeout or a raw OSError.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import rpc, vxi11
from pyvisa_py.tcpip import TCPIPInstrVxi11

#: VISA I/O timeout used by the sessions under test, in ms.
IO_TIMEOUT_MS = 500


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _drain_one_record(sock: socket.socket) -> None:
    """Consume one complete ONC RPC record from the client.

    Closing a socket that still has unread data makes Linux send an RST
    instead of a FIN, so the fake instrument must read the request in full
    before closing if it wants a clean close.
    """
    last = False
    while not last:
        (header,) = struct.unpack(">I", _recv_exact(sock, 4))
        last = bool(header & 0x80000000)
        _recv_exact(sock, header & 0x7FFFFFFF)


def _reset(sock: socket.socket) -> None:
    """Close a socket with an RST instead of a FIN."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


@pytest.fixture
def link():
    """A CoreClient connected to a fake instrument over loopback.

    Yields ``(client, instrument_socket)``. The portmapper lookup done by
    ``CoreClient.__init__`` is skipped by building the client by hand.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    client = object.__new__(vxi11.CoreClient)
    rpc.Client.__init__(
        client, host, vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS, port
    )
    client.packer = vxi11.Vxi11Packer()
    client.unpacker = vxi11.Vxi11Unpacker(b"")
    client.sock = socket.create_connection((host, port))
    client.timeout = 4.0

    instrument, _ = server.accept()
    try:
        yield client, instrument
    finally:
        client.sock.close()
        instrument.close()
        server.close()


def _make_session(interface) -> TCPIPInstrVxi11:
    sess = object.__new__(TCPIPInstrVxi11)
    sess.interface = interface
    sess.link = 1
    sess.max_recv_size = 1024
    sess._io_timeout = IO_TIMEOUT_MS
    sess.timeout = IO_TIMEOUT_MS / 1000
    sess.attrs = {
        ResourceAttribute.termchar_enabled: False,
        ResourceAttribute.termchar: ord("\n"),
        ResourceAttribute.suppress_end_enabled: False,
        ResourceAttribute.send_end_enabled: True,
        ResourceAttribute.lockwait: 0,  # type: ignore[attr-defined]
    }
    return sess


def test_read_reports_connection_lost_when_instrument_closes(link):
    client, instrument = link

    def instrument_closes_after_request():
        _drain_one_record(instrument)  # the DEVICE_READ call
        instrument.close()  # FIN, no reply

    worker = threading.Thread(target=instrument_closes_after_request)
    worker.start()

    start = time.monotonic()
    data, status = _make_session(client).read(10)
    elapsed = time.monotonic() - start
    worker.join()

    assert data == b""
    assert status == StatusCode.error_connection_lost, (
        f"got {status!r} after {elapsed:.2f}s"
    )


def test_read_reports_connection_lost_when_instrument_resets(link):
    client, instrument = link

    def instrument_resets_after_request():
        _drain_one_record(instrument)
        _reset(instrument)

    worker = threading.Thread(target=instrument_resets_after_request)
    worker.start()
    try:
        _, status = _make_session(client).read(10)
    finally:
        worker.join()

    assert status == StatusCode.error_connection_lost


def test_write_reports_connection_lost_when_link_was_reset(link):
    client, instrument = link
    _reset(instrument)
    time.sleep(0.05)  # let the RST reach the client socket

    _, status = _make_session(client).write(b"*IDN?\n")

    assert status == StatusCode.error_connection_lost
