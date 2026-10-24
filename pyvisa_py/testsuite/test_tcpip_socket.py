# -*- coding: utf-8 -*-
"""Test TCPIP SOCKET reads against a loopback server."""

import socket
import threading
import time

import pytest

from pyvisa import ResourceManager, constants, errors


@pytest.fixture
def closing_server():
    """Accept one connection, send an unterminated reply and close it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.recv(100)
        conn.sendall(b"partial")
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    thread.join(5)
    server.close()


def test_read_reports_connection_lost_when_peer_closes(closing_server):
    rm = ResourceManager("@py")
    try:
        inst = rm.open_resource(
            f"TCPIP::127.0.0.1::{closing_server}::SOCKET",
            read_termination="\n",
            timeout=2000,
        )
        inst.write("*IDN?")
        start = time.monotonic()
        with pytest.raises(errors.VisaIOError) as exc_info:
            inst.read()
        assert exc_info.value.error_code == constants.StatusCode.error_connection_lost
        # Without the check the read spins until the timeout, and forever
        # with an infinite timeout.
        assert time.monotonic() - start < 1.0
    finally:
        rm.close()
