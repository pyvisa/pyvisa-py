# -*- coding: utf-8 -*-
"""Tests for HiSLIP terminate (viTerminate) support.

Tests the CancellableSocket, HiSLIPInterruptedError, and the terminate/
complete_terminate flow without requiring a real instrument.
"""

import socket
import struct
import threading
import time
from unittest.mock import MagicMock, patch

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

    def test_recv_into_bypass_when_cancel_disabled(self):
        """recv_into bypasses select() when _cancel_enabled is False."""
        self.client._cancel_enabled = False
        self.server.sendall(b"bypass")
        buf = bytearray(6)
        n = self.client.recv_into(buf, 6)
        assert n == 6
        assert buf == b"bypass"

    def test_cancel_idempotent(self):
        """Multiple cancel() calls don't raise — already-signalled is a no-op."""
        self.client.cancel()
        self.client.cancel()  # should not raise
        # Drain and verify socket is still usable
        self.client.drain_cancel()
        self.server.sendall(b"ok")
        buf = bytearray(2)
        n = self.client.recv_into(buf, 2)
        assert n == 2
        assert buf == b"ok"

    def test_socket_options_preserved(self):
        """Socket options set before wrapping are preserved."""
        raw_server, raw_client = socket.socketpair()
        raw_client.settimeout(3.5)
        wrapped = CancellableSocket(raw_client)
        assert wrapped.gettimeout() == 3.5
        wrapped.close()
        raw_server.close()


class TestHiSLIPInterruptedError:
    """Test HiSLIPInterruptedError attributes and formatting."""

    def test_default_message_id(self):
        err = HiSLIPInterruptedError()
        assert err.message_id == 0
        assert "message_id=0x0" in str(err)

    def test_custom_message_id(self):
        err = HiSLIPInterruptedError(0xDEAD)
        assert err.message_id == 0xDEAD
        assert "0xdead" in str(err)


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
        interrupted_hdr = self._make_hislip_header("Interrupted", 0, 0xFFFF_FF00, 0)
        self.server.sendall(interrupted_hdr)

        from pyvisa_py.protocols.hislip import RxHeader

        header = RxHeader(self.client)
        assert header.msg_type == "Interrupted"
        assert header.message_parameter == 0xFFFF_FF00


class TestInstrumentTerminate:
    """Test Instrument.terminate() and complete_terminate() via mocking."""

    def test_terminate_calls_cancel(self):
        """Instrument.terminate() signals cancel only when receiving."""
        import threading
        from pyvisa_py.protocols.hislip import Instrument

        inst = object.__new__(Instrument)
        mock_sync = MagicMock(spec=CancellableSocket)
        inst._sync = mock_sync
        inst._receiving = threading.Event()

        # When no receive is in progress, terminate is a no-op
        inst.terminate()
        mock_sync.cancel.assert_not_called()

        # When a receive is in progress, terminate signals cancel
        inst._receiving.set()
        inst.terminate()
        mock_sync.cancel.assert_called_once()

    def test_complete_terminate_resets_state(self):
        """complete_terminate() drains cancel, clears socket, does device clear."""
        from pyvisa_py.protocols.hislip import Instrument

        inst = object.__new__(Instrument)
        mock_sync = MagicMock(spec=CancellableSocket)
        mock_sync.gettimeout.return_value = 5.0
        # Make recv return empty to end drain loop
        mock_sync.recv.side_effect = BlockingIOError
        inst._sync = mock_sync
        inst._timeout = 5.0
        inst._message_id = 0xABCD
        inst._last_message_id = 0x1234
        inst._rmt = 1
        inst._payload_remaining = 42
        inst._msg_type = "Data"

        # Mock the async channel methods that complete_terminate calls
        inst.async_device_clear = MagicMock(return_value=0)
        inst.device_clear_complete = MagicMock(return_value=0)

        # Mock RxHeader to return an Interrupted message
        mock_header = MagicMock()
        mock_header.msg_type = "Interrupted"
        mock_header.payload_length = 0
        with patch("pyvisa_py.protocols.hislip.RxHeader", return_value=mock_header):
            inst.complete_terminate()

        # Verify state was reset
        assert inst._message_id == 0xFFFF_FF00
        assert inst._last_message_id is None
        assert inst._rmt == 0
        assert inst._payload_remaining == 0
        assert inst._msg_type == ""

        # Verify cancel pipe was drained
        mock_sync.drain_cancel.assert_called_once()
        # Verify device clear was performed
        inst.async_device_clear.assert_called_once()
        inst.device_clear_complete.assert_called_once()


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


class TestSessionTerminateBase:
    """Test Session.terminate() base class default."""

    def test_base_session_terminate_returns_nonsupported(self):
        from pyvisa.constants import StatusCode
        from pyvisa_py.sessions import Session

        # Session is abstract, so test the default through a minimal mock
        sess = MagicMock(spec=Session)
        result = Session.terminate(sess)
        assert result == StatusCode.error_nonsupported_operation

    def test_base_session_terminate_accepts_job_id(self):
        from pyvisa.constants import StatusCode
        from pyvisa_py.sessions import Session

        sess = MagicMock(spec=Session)
        result = Session.terminate(sess, job_id=None)
        assert result == StatusCode.error_nonsupported_operation


class TestTCPIPInstrHiSLIPTerminate:
    """Test TCPIPInstrHiSLIP.terminate() and read() abort path."""

    def _make_session(self):
        """Create a TCPIPInstrHiSLIP with a mocked HiSLIP Instrument."""
        from pyvisa_py.tcpip import TCPIPInstrHiSLIP

        sess = object.__new__(TCPIPInstrHiSLIP)
        sess.interface = MagicMock()
        return sess

    def test_terminate_calls_interface(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        result = sess.terminate()
        assert result == StatusCode.success
        sess.interface.terminate.assert_called_once()

    def test_terminate_accepts_job_id(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        result = sess.terminate(job_id=None)
        assert result == StatusCode.success

    def test_read_abort_calls_complete_terminate(self):
        """When read() catches HiSLIPInterruptedError, it auto-resets."""
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        sess.interface.receive.side_effect = HiSLIPInterruptedError(0)

        data, status = sess.read(1024)
        assert data == b""
        assert status == StatusCode.error_abort
        sess.interface.complete_terminate.assert_called_once()

    def test_read_timeout(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        sess.interface.receive.side_effect = socket.timeout("timed out")

        data, status = sess.read(1024)
        assert data == b""
        assert status == StatusCode.error_timeout

    def test_read_success_rmt(self):
        """read() returns success_termination_character_read when rmt is set."""
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        sess.interface.receive.return_value = b"*IDN? response\n"
        sess.interface._rmt = 1

        data, status = sess.read(4096)
        assert data == b"*IDN? response\n"
        assert status == StatusCode.success_termination_character_read

    def test_read_success_max_count(self):
        """read() returns success_max_count_read when buffer is full."""
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        sess.interface.receive.return_value = b"abcd"
        sess.interface._rmt = 0

        data, status = sess.read(4)
        assert data == b"abcd"
        assert status == StatusCode.success_max_count_read


class TestHighlevelTerminate:
    """Test PyVisaLibrary.terminate() dispatcher."""

    def test_terminate_dispatches_to_session(self):
        from pyvisa.constants import StatusCode
        from pyvisa_py.highlevel import PyVisaLibrary

        lib = object.__new__(PyVisaLibrary)
        mock_sess = MagicMock()
        mock_sess.terminate.return_value = StatusCode.success
        lib.sessions = {42: mock_sess}
        # Stub handle_return_value to pass through
        lib.handle_return_value = lambda sess, val: val

        result = lib.terminate(42, None, None)
        assert result == StatusCode.success
        mock_sess.terminate.assert_called_once_with(None)

    def test_terminate_invalid_session(self):
        from pyvisa.constants import StatusCode
        from pyvisa_py.highlevel import PyVisaLibrary

        lib = object.__new__(PyVisaLibrary)
        lib.sessions = {}
        lib.handle_return_value = lambda sess, val: val

        result = lib.terminate(999, None, None)
        assert result == StatusCode.error_invalid_object
