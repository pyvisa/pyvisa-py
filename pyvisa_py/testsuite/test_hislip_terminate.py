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
    HEADER_SIZE,
    MESSAGETYPE,
    MESSAGETYPE_STR,
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


class TestInstrumentReceive:
    """Test HiSLIP payload termination and buffering."""

    def setup_method(self):
        from pyvisa_py.protocols.hislip import Instrument

        self.server, client_raw = socket.socketpair()
        self.client = CancellableSocket(client_raw)
        self.instrument = object.__new__(Instrument)
        self.instrument._sync = self.client
        self.instrument._receiving = threading.Event()
        self.instrument._last_message_id = None
        self.instrument._msg_type = ""
        self.instrument._payload_remaining = 0
        self.instrument._rmt = 0
        self.instrument._pending_data = bytearray()
        self.instrument._last_read_rmt = False
        self.instrument._last_read_termchar = False

    def teardown_method(self):
        self.client.close()
        self.server.close()

    def _send_packet(self, msg_type, payload):
        header = struct.pack(
            HEADER_FORMAT,
            b"HS",
            MESSAGETYPE[msg_type],
            0,
            0xFFFF_FFFF,
            len(payload),
        )
        self.server.sendall(header + payload)

    def test_termchar_preserves_dataend_remainder(self):
        self._send_packet("DataEnd", b"abc\nrest")

        assert self.instrument.receive(4096, termination_char=ord("\n")) == b"abc\n"
        assert self.instrument._last_read_termchar is True
        assert self.instrument._last_read_rmt is False
        assert self.instrument.receive(4096) == b"rest"
        assert self.instrument._last_read_rmt is True

    def test_termchar_across_data_packets(self):
        self._send_packet("Data", b"abc")
        self._send_packet("DataEnd", b"def\nrest")

        assert self.instrument.receive(4096, termination_char=ord("\n")) == b"abcdef\n"
        assert self.instrument._last_read_termchar is True
        assert self.instrument._last_read_rmt is False
        assert self.instrument.receive(4096) == b"rest"
        assert self.instrument._last_read_rmt is True

    def test_suppress_end_continues_to_next_message(self):
        self._send_packet("DataEnd", b"abc")
        self._send_packet("DataEnd", b"def\n")

        data = self.instrument.receive(
            4096, termination_char=ord("\n"), suppress_end=True
        )

        assert data == b"abcdef\n"
        assert self.instrument._last_read_rmt is True
        assert self.instrument._last_read_termchar is True

    def test_dataend_and_termchar_are_both_reported(self):
        self._send_packet("DataEnd", b"abc\n")

        assert self.instrument.receive(4, termination_char=ord("\n")) == b"abc\n"
        assert self.instrument._last_read_rmt is True
        assert self.instrument._last_read_termchar is True


class TestAsyncChannelDispatcher:
    """Test the background async reader and request dispatcher."""

    def _recv_exact(self, sock, size):
        data = bytearray()
        while len(data) < size:
            data.extend(sock.recv(size - len(data)))
        return bytes(data)

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

    def test_async_service_request_fires_callback(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        events = []
        channel = AsyncChannel(client_raw, event_callback=events.append)

        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x42, 0, 0))

        deadline = time.time() + 2.0
        while not events and time.time() < deadline:
            time.sleep(0.01)

        assert events == [0x42]

        channel.close()
        server.close()

    def test_async_service_request_callback_can_make_async_request(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        client_raw.settimeout(0.5)
        callback_done = threading.Event()
        result = {}
        channel = None

        def callback(_status_byte):
            try:
                result["response"] = channel.request(
                    "AsyncStatusQuery",
                    0,
                    0,
                    expected_response="AsyncStatusResponse",
                )
            except Exception as exc:
                result["error"] = exc
            finally:
                callback_done.set()

        channel = AsyncChannel(client_raw, event_callback=callback)
        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x42, 0, 0))

        request_header = self._recv_exact(server, HEADER_SIZE)
        _, msg_type, _, _, payload_length = struct.unpack(HEADER_FORMAT, request_header)
        assert msg_type == MESSAGETYPE["AsyncStatusQuery"]
        assert payload_length == 0
        server.sendall(self._make_hislip_header("AsyncStatusResponse", 0x5A, 0, 0))

        assert callback_done.wait(timeout=2.0)
        assert "error" not in result
        assert result["response"].control_code == 0x5A

        channel.close()
        server.close()

    def test_async_service_requests_are_dispatched_in_order(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        events = []
        first_started = threading.Event()
        release_first = threading.Event()
        all_delivered = threading.Event()

        def callback(status_byte):
            events.append(status_byte)
            if status_byte == 0x41:
                first_started.set()
                release_first.wait(timeout=2.0)
            if len(events) == 3:
                all_delivered.set()

        channel = AsyncChannel(client_raw, event_callback=callback)
        for status_byte in (0x41, 0x42, 0x43):
            server.sendall(
                self._make_hislip_header("AsyncServiceRequest", status_byte, 0, 0)
            )

        assert first_started.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while channel._event_queue.qsize() < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert channel._event_queue.qsize() == 2
        assert events == [0x41]

        release_first.set()
        assert all_delivered.wait(timeout=2.0)
        assert events == [0x41, 0x42, 0x43]

        channel.close()
        server.close()

    def test_async_service_request_callback_exception_does_not_stop_dispatch(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        events = []
        second_delivered = threading.Event()

        def callback(status_byte):
            events.append(status_byte)
            if status_byte == 0x41:
                raise RuntimeError("callback failed")
            second_delivered.set()

        channel = AsyncChannel(client_raw, event_callback=callback)
        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x41, 0, 0))
        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x42, 0, 0))

        assert second_delivered.wait(timeout=2.0)
        assert events == [0x41, 0x42]

        channel.close()
        server.close()

    def test_close_stops_async_service_request_worker(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw, event_callback=lambda _status: None)
        event_thread = channel._event_thread
        assert event_thread is not None
        assert event_thread.is_alive()

        channel.close()

        assert not event_thread.is_alive()
        server.close()

    def test_async_service_request_callback_can_close_channel(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        callback_done = threading.Event()
        channel = None

        def callback(_status_byte):
            channel.close()
            callback_done.set()

        channel = AsyncChannel(client_raw, event_callback=callback)
        event_thread = channel._event_thread
        assert event_thread is not None
        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x42, 0, 0))

        assert callback_done.wait(timeout=2.0)
        event_thread.join(timeout=2.0)
        assert not event_thread.is_alive()

        server.close()

    def test_async_request_gets_dispatcher_response(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw)

        result = {}

        def responder():
            request_header = server.recv(1024)
            assert request_header
            response_header = self._make_hislip_header(
                "AsyncStatusResponse", 0x5A, 0, 0
            )
            server.sendall(response_header)

        responder_thread = threading.Thread(target=responder)
        responder_thread.start()

        result["response"] = channel.request(
            "AsyncStatusQuery", 0, 0, expected_response="AsyncStatusResponse"
        )

        responder_thread.join(timeout=2.0)
        assert not responder_thread.is_alive()
        assert result["response"].msg_type == "AsyncStatusResponse"
        assert result["response"].control_code == 0x5A

        channel.close()
        server.close()

    def test_concurrent_requests_with_same_response_type_complete_in_order(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw)
        results = {}
        errors = {}

        def request_status(request_id):
            try:
                response = channel.request(
                    "AsyncStatusQuery",
                    request_id,
                    0,
                    expected_response="AsyncStatusResponse",
                )
                results[request_id] = response.control_code
            except Exception as exc:
                errors[request_id] = exc

        threads = [
            threading.Thread(target=request_status, args=(request_id,))
            for request_id in range(3)
        ]
        for thread in threads:
            thread.start()

        request_ids = []
        for _ in threads:
            header = self._recv_exact(server, HEADER_SIZE)
            _, msg_type, control_code, _, payload_length = struct.unpack(
                HEADER_FORMAT, header
            )
            assert msg_type == MESSAGETYPE["AsyncStatusQuery"]
            assert payload_length == 0
            request_ids.append(control_code)

        for request_id in request_ids:
            server.sendall(
                self._make_hislip_header("AsyncStatusResponse", request_id + 0x40, 0, 0)
            )

        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert errors == {}
        assert results == {request_id: request_id + 0x40 for request_id in request_ids}

        channel.close()
        server.close()

    def test_concurrent_mixed_requests_route_interleaved_responses(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        events = []
        channel = AsyncChannel(client_raw, event_callback=events.append)
        results = {}

        requests = {
            "status": ("AsyncStatusQuery", "AsyncStatusResponse"),
            "lock": ("AsyncLockInfo", "AsyncLockInfoResponse"),
        }

        def make_request(name):
            msg_type, expected_response = requests[name]
            results[name] = channel.request(
                msg_type, 0, 0, expected_response=expected_response
            )

        threads = [
            threading.Thread(target=make_request, args=(name,)) for name in requests
        ]
        for thread in threads:
            thread.start()

        sent_types = []
        for _ in threads:
            header = self._recv_exact(server, HEADER_SIZE)
            _, msg_type, _, _, payload_length = struct.unpack(HEADER_FORMAT, header)
            assert payload_length == 0
            sent_types.append(MESSAGETYPE_STR[msg_type])

        server.sendall(self._make_hislip_header("AsyncServiceRequest", 0x44, 0, 0))
        response_for = {
            "AsyncStatusQuery": ("AsyncStatusResponse", 0x52, 0),
            "AsyncLockInfo": ("AsyncLockInfoResponse", 1, 3),
        }
        for sent_type in reversed(sent_types):
            response_type, control_code, message_parameter = response_for[sent_type]
            server.sendall(
                self._make_hislip_header(
                    response_type, control_code, message_parameter, 0
                )
            )

        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert events == [0x44]
        assert results["status"].control_code == 0x52
        assert results["lock"].control_code == 1
        assert results["lock"].message_parameter == 3

        channel.close()
        server.close()

    def test_timed_out_request_absorbs_its_late_response(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        client_raw.settimeout(0.05)
        channel = AsyncChannel(client_raw)

        with pytest.raises(socket.timeout):
            channel.request(
                "AsyncStatusQuery", 0, 0, expected_response="AsyncStatusResponse"
            )
        self._recv_exact(server, HEADER_SIZE)

        client_raw.settimeout(1.0)
        result = {}

        def request_status():
            result["response"] = channel.request(
                "AsyncStatusQuery", 0, 0, expected_response="AsyncStatusResponse"
            )

        thread = threading.Thread(target=request_status)
        thread.start()
        self._recv_exact(server, HEADER_SIZE)

        server.sendall(self._make_hislip_header("AsyncStatusResponse", 0x11, 0, 0))
        time.sleep(0.05)
        assert thread.is_alive()

        server.sendall(self._make_hislip_header("AsyncStatusResponse", 0x22, 0, 0))
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert result["response"].control_code == 0x22

        channel.close()
        server.close()

    @pytest.mark.parametrize("failure", ["close", "protocol"])
    def test_channel_failure_releases_all_pending_requests(self, failure):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw)
        errors = []

        def request_status():
            try:
                channel.request(
                    "AsyncStatusQuery",
                    0,
                    0,
                    expected_response="AsyncStatusResponse",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=request_status) for _ in range(2)]
        for thread in threads:
            thread.start()
        for _ in threads:
            self._recv_exact(server, HEADER_SIZE)

        if failure == "close":
            channel.close()
        else:
            server.sendall(b"XX" + b"\x00" * (HEADER_SIZE - 2))

        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert len(errors) == 2
        assert all(isinstance(error, RuntimeError) for error in errors)

        channel.close()
        server.close()

    def test_async_interrupted_aborts_all_pending_requests(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw)
        errors = []

        def request_status():
            try:
                channel.request(
                    "AsyncStatusQuery",
                    0,
                    0,
                    expected_response="AsyncStatusResponse",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=request_status) for _ in range(2)]
        for thread in threads:
            thread.start()
        for _ in threads:
            self._recv_exact(server, HEADER_SIZE)

        server.sendall(self._make_hislip_header("AsyncInterrupted", 0, 0xBEEF, 0))

        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert len(errors) == 2
        assert all(isinstance(error, HiSLIPInterruptedError) for error in errors)
        assert all(error.message_id == 0xBEEF for error in errors)

        channel.close()
        server.close()

    def test_concurrent_instrument_status_queries_preserve_sync_message_id(self):
        from pyvisa_py.protocols.hislip import AsyncChannel, Instrument

        server, client_raw = socket.socketpair()
        instrument = object.__new__(Instrument)
        instrument._async_channel = AsyncChannel(client_raw)
        instrument._rmt = 1
        instrument._message_id = 0xFFFF_FF00
        results = []

        threads = [
            threading.Thread(
                target=lambda: results.append(instrument.async_status_query())
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()

        for _ in threads:
            header = self._recv_exact(server, HEADER_SIZE)
            _, msg_type, control_code, message_parameter, payload_length = (
                struct.unpack(HEADER_FORMAT, header)
            )
            assert msg_type == MESSAGETYPE["AsyncStatusQuery"]
            assert control_code == 1
            assert message_parameter == 0xFFFF_FF00
            assert payload_length == 0

        server.sendall(self._make_hislip_header("AsyncStatusResponse", 0x31, 0, 0))
        server.sendall(self._make_hislip_header("AsyncStatusResponse", 0x32, 0, 0))

        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert sorted(results) == [0x31, 0x32]
        assert instrument._rmt == 0
        assert instrument._message_id == 0xFFFF_FF00

        instrument._async_channel.close()
        server.close()

    def test_async_interrupted_aborts_pending_request(self):
        from pyvisa_py.protocols.hislip import AsyncChannel

        server, client_raw = socket.socketpair()
        channel = AsyncChannel(client_raw)

        def responder():
            request_header = server.recv(1024)
            assert request_header
            server.sendall(self._make_hislip_header("AsyncInterrupted", 0, 0xBEEF, 0))

        responder_thread = threading.Thread(target=responder)
        responder_thread.start()

        with pytest.raises(HiSLIPInterruptedError) as excinfo:
            channel.request(
                "AsyncStatusQuery", 0, 0, expected_response="AsyncStatusResponse"
            )

        responder_thread.join(timeout=2.0)
        assert not responder_thread.is_alive()
        assert excinfo.value.message_id == 0xBEEF

        channel.close()
        server.close()


class TestInstrumentTerminate:
    """Test Instrument.terminate() and complete_terminate() via mocking."""

    def test_nodelay_controls_both_channels(self):
        from pyvisa_py.protocols.hislip import Instrument

        inst = object.__new__(Instrument)
        inst._sync = MagicMock()
        inst._async = MagicMock()
        inst._sync.getsockopt.return_value = 1

        assert inst.nodelay is True

        inst.nodelay = False
        inst._sync.setsockopt.assert_called_once_with(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, False
        )
        inst._async.setsockopt.assert_called_once_with(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, False
        )

    def test_terminate_calls_cancel(self):
        """Instrument.terminate() signals cancel only when receiving."""
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
        inst._pending_data = bytearray(b"unread")
        inst._last_read_rmt = True
        inst._last_read_termchar = True

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
        assert inst._pending_data == b""
        assert inst._last_read_rmt is False
        assert inst._last_read_termchar is False

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

    def _make_session(self, *, termchar_enabled=False, suppress_end_enabled=False):
        """Create a TCPIPInstrHiSLIP with a mocked HiSLIP Instrument."""
        from pyvisa import constants
        from pyvisa_py.tcpip import TCPIPInstrHiSLIP

        sess = object.__new__(TCPIPInstrHiSLIP)
        sess.interface = MagicMock()
        sess.interface._last_read_rmt = False
        sess.interface._last_read_termchar = False
        sess.attrs = {
            constants.ResourceAttribute.suppress_end_enabled: suppress_end_enabled,
            constants.ResourceAttribute.termchar_enabled: termchar_enabled,
            constants.ResourceAttribute.termchar: ord("\n"),
        }
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

    @pytest.mark.parametrize(
        ("count", "expected_status"),
        [(-1, "error_invalid_parameter"), (0, "success_max_count_read")],
    )
    def test_read_count_boundaries(self, count, expected_status):
        from pyvisa.constants import StatusCode

        sess = self._make_session()

        data, status = sess.read(count)

        assert data == b""
        assert status == getattr(StatusCode, expected_status)
        sess.interface.receive.assert_not_called()

    def test_read_success_rmt(self):
        """An unsuppressed RMT returns success."""
        from pyvisa.constants import StatusCode

        sess = self._make_session()
        sess.interface.receive.return_value = b"*IDN? response\n"
        sess.interface._last_read_rmt = True

        data, status = sess.read(4096)
        assert data == b"*IDN? response\n"
        assert status == StatusCode.success

    def test_read_success_termchar(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session(termchar_enabled=True)
        sess.interface.receive.return_value = b"response\n"
        sess.interface._last_read_termchar = True

        data, status = sess.read(4096)

        assert data == b"response\n"
        assert status == StatusCode.success_termination_character_read
        sess.interface.receive.assert_called_once_with(
            4096, termination_char=ord("\n"), suppress_end=False
        )

    def test_read_rmt_takes_priority_over_termchar(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session(termchar_enabled=True)
        sess.interface.receive.return_value = b"response\n"
        sess.interface._last_read_rmt = True
        sess.interface._last_read_termchar = True

        _data, status = sess.read(4096)

        assert status == StatusCode.success

    def test_read_suppressed_rmt_returns_termchar_status(self):
        from pyvisa.constants import StatusCode

        sess = self._make_session(termchar_enabled=True, suppress_end_enabled=True)
        sess.interface.receive.return_value = b"response\n"
        sess.interface._last_read_rmt = True
        sess.interface._last_read_termchar = True

        _data, status = sess.read(4096)

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
        sess.interface.receive.assert_called_once_with(
            4, termination_char=None, suppress_end=False
        )

    def test_async_service_request_callback_fires_event(self):
        from pyvisa import constants
        from pyvisa_py.events import EventContext
        from pyvisa_py.tcpip import TCPIPInstrHiSLIP

        sess = object.__new__(TCPIPInstrHiSLIP)
        sess._fire_event = MagicMock()

        TCPIPInstrHiSLIP._handle_async_service_request(sess, 0x44)

        sess._fire_event.assert_called_once()
        event_type, ctx = sess._fire_event.call_args.args
        assert event_type == constants.EventType.service_request
        assert isinstance(ctx, EventContext)
        assert ctx.context_id == 0x44

    def test_async_interrupted_callback_stores_message_id(self):
        from pyvisa_py.tcpip import TCPIPInstrHiSLIP

        sess = object.__new__(TCPIPInstrHiSLIP)
        sess._handle_async_interrupted(0x1234)

        assert sess._async_interrupted_message_id == 0x1234


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
