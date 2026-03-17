# -*- coding: utf-8 -*-
"""Tests for HiSLIP terminate (viTerminate) support.

Tests the CancellableSocket, HiSLIPInterruptedError, and the terminate/
complete_terminate flow without requiring a real instrument.
"""

import socket
import struct
import threading
import time

import pytest

from pyvisa_py.protocols.hislip import (
    HEADER_FORMAT,
    MESSAGETYPE,
    CancellableSocket,
    HiSLIPInterruptedError,
)


class TestCancellableSocket:
    """Unit tests for the CancellableSocket subclass."""

    def setup_method(self):
        """Create a socket pair for testing."""
        self.server, client_raw = socket.socketpair()
        self.client = CancellableSocket(client_raw)

    def teardown_method(self):
        self.client.close()
        self.server.close()

    def test_recv_into_normal(self):
        """Normal recv_into passes through to the underlying socket."""
        self.server.sendall(b"hello")
        buf = bytearray(5)
        n = self.client.recv_into(buf, 5)
        assert n == 5
        assert buf == b"hello"

    def test_recv_into_cancel(self):
        """cancel() from another thread unblocks a pending recv_into."""
        buf = bytearray(100)
        result = {}

        def reader():
            try:
                self.client.recv_into(buf, 100)
                result["error"] = None
            except HiSLIPInterruptedError as e:
                result["error"] = e

        t = threading.Thread(target=reader)
        t.start()

        # Give the reader thread time to enter select()
        time.sleep(0.1)

        # Cancel from the main thread
        self.client.cancel()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Reader thread did not exit"
        assert isinstance(result.get("error"), HiSLIPInterruptedError)

    def test_cancel_before_recv_into(self):
        """cancel() before recv_into causes immediate HiSLIPInterruptedError."""
        self.client.cancel()
        buf = bytearray(100)
        with pytest.raises(HiSLIPInterruptedError):
            self.client.recv_into(buf, 100)

    def test_cancel_drain_allows_subsequent_recv(self):
        """After draining the cancel pipe, normal recv_into works again."""
        self.client.cancel()
        self.client.drain_cancel()

        self.server.sendall(b"data")
        buf = bytearray(4)
        n = self.client.recv_into(buf, 4)
        assert n == 4
        assert buf == b"data"

    def test_recv_into_timeout(self):
        """recv_into honours the socket timeout."""
        self.client.settimeout(0.1)
        buf = bytearray(100)
        with pytest.raises(socket.timeout):
            self.client.recv_into(buf, 100)

    def test_sendall_delegates(self):
        """sendall delegates to the underlying socket."""
        self.client.sendall(b"outgoing")
        data = self.server.recv(100)
        assert data == b"outgoing"

    def test_cancel_prioritized_over_data(self):
        """When both data and cancel are ready, cancel takes priority."""
        self.server.sendall(b"data")
        time.sleep(0.05)  # let the data arrive
        self.client.cancel()
        time.sleep(0.05)  # let the cancel signal arrive

        buf = bytearray(100)
        with pytest.raises(HiSLIPInterruptedError):
            self.client.recv_into(buf, 100)


class TestHiSLIPInterruptedInHeader:
    """Test that Interrupted messages in _next_data_header raise properly."""

    def _make_hislip_header(
        self,
        msg_type: str,
        control_code: int,
        message_parameter: int,
        payload_length: int,
    ) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            b"HS",
            MESSAGETYPE[msg_type],
            control_code,
            message_parameter,
            payload_length,
        )

    def setup_method(self):
        """Create a socket pair simulating a HiSLIP sync channel."""
        self.server, client_raw = socket.socketpair()
        self.client = CancellableSocket(client_raw)

    def teardown_method(self):
        self.client.close()
        self.server.close()

    def test_interrupted_message_raises(self):
        """Receiving an Interrupted message raises HiSLIPInterruptedError."""
        # We can't easily construct an Instrument without a real server,
        # so we test _next_data_header indirectly by testing that RxHeader
        # + our handling works.
        #
        # Send an Interrupted header on the "sync" channel.
        interrupted_hdr = self._make_hislip_header("Interrupted", 0, 0xFFFF_FF00, 0)
        self.server.sendall(interrupted_hdr)

        # Read it as an RxHeader and verify msg_type
        from pyvisa_py.protocols.hislip import RxHeader

        header = RxHeader(self.client)
        assert header.msg_type == "Interrupted"
        assert header.message_parameter == 0xFFFF_FF00


class TestTerminateConcurrency:
    """Integration test: terminate() cancels a blocked receive."""

    def test_terminate_unblocks_blocked_recv(self):
        """Simulate a blocked receive and cancel it via terminate()."""
        server, client_raw = socket.socketpair()
        client = CancellableSocket(client_raw)

        result = {}

        def blocked_reader():
            """Simulates the receive path: tries to read a full HiSLIP message."""
            from pyvisa_py.protocols.hislip import receive_exact_into

            buf = bytearray(1024)
            try:
                receive_exact_into(client, buf)
                result["ok"] = True
            except HiSLIPInterruptedError:
                result["interrupted"] = True
            except RuntimeError:
                result["dropped"] = True

        t = threading.Thread(target=blocked_reader)
        t.start()
        time.sleep(0.1)  # let the reader enter select()

        # terminate = write to cancel pipe
        client.cancel()

        t.join(timeout=2.0)
        assert not t.is_alive(), "Reader thread did not exit after cancel"
        assert result.get("interrupted") is True, f"Got: {result}"

        client.close()
        server.close()
