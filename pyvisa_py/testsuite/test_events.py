"""Unit tests for the pyvisa-py event handling subsystem.

These tests cover the core event primitives in ``events.py``, the high-level
library methods in ``highlevel.py``, and transport-specific SRQ logic for
VXI-11 (mocked).

"""

from __future__ import annotations

import ipaddress
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from pyvisa import constants, errors
from pyvisa.constants import StatusCode

from pyvisa_py.events import (
    EventContext,
    EventMechanismFlag,
    EventQueue,
    EventState,
    HandlerRegistry,
)
from pyvisa_py.highlevel import PyVisaLibrary
from pyvisa_py.protocols import vxi11


# ---------------------------------------------------------------------------
# EventContext
# ---------------------------------------------------------------------------


class TestEventContext:
    def test_defaults(self):
        ctx = EventContext(event_type=constants.EventType.service_request)
        assert ctx.event_type == constants.EventType.service_request
        assert ctx.status_byte == 0
        assert ctx.timestamp <= time.time()
        assert isinstance(ctx.context_id, int)
        assert 0 <= ctx.context_id < 2**32

    def test_context_id_randomness(self):
        ctx1 = EventContext(event_type=constants.EventType.service_request)
        ctx2 = EventContext(event_type=constants.EventType.service_request)
        # Extremely unlikely to collide on 32-bit random space
        assert ctx1.context_id != ctx2.context_id

    def test_explicit_values(self):
        ctx = EventContext(
            event_type=constants.EventType.io_completion,
            status_byte=0x42,
            timestamp=1234.5,
            context_id=99,
        )
        assert ctx.event_type == constants.EventType.io_completion
        assert ctx.status_byte == 0x42
        assert ctx.timestamp == 1234.5
        assert ctx.context_id == 99


# ---------------------------------------------------------------------------
# EventQueue
# ---------------------------------------------------------------------------


class TestEventQueue:
    def test_put_get_roundtrip(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.service_request)
        q.put(ctx)
        assert q.get(timeout_ms=None) is ctx

    def test_get_zero_timeout_empty(self):
        q = EventQueue()
        assert q.get(timeout_ms=0) is None

    def test_get_positive_timeout_returns_item(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.service_request)
        q.put(ctx)
        assert q.get(timeout_ms=100) is ctx

    def test_get_positive_timeout_blocks_then_none(self):
        q = EventQueue()
        start = time.time()
        result = q.get(timeout_ms=50)
        elapsed = time.time() - start
        assert result is None
        assert elapsed >= 0.04  # generous tolerance

    def test_get_none_blocks_forever(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.service_request)

        def delayed_put():
            time.sleep(0.05)
            q.put(ctx)

        t = threading.Thread(target=delayed_put)
        t.start()
        assert q.get(timeout_ms=None) is ctx
        t.join()

    def test_discard_all_matching_event_type(self):
        q = EventQueue()
        ctx_srq = EventContext(event_type=constants.EventType.service_request)
        ctx_io = EventContext(event_type=constants.EventType.io_completion)
        q.put(ctx_srq)
        q.put(ctx_io)
        q.discard_all(constants.EventType.service_request)
        assert q.get(timeout_ms=0) is ctx_io
        assert q.get(timeout_ms=0) is None

    def test_discard_all_none_clears_everything(self):
        q = EventQueue()
        q.put(EventContext(event_type=constants.EventType.service_request))
        q.put(EventContext(event_type=constants.EventType.io_completion))
        q.discard_all(None)
        assert q.get(timeout_ms=0) is None

    def test_get_matching_returns_matching_event(self):
        q = EventQueue()
        ctx_srq = EventContext(event_type=constants.EventType.service_request)
        ctx_io = EventContext(event_type=constants.EventType.io_completion)
        q.put(ctx_io)
        q.put(ctx_srq)
        assert (
            q.get_matching(constants.EventType.service_request, timeout_ms=0) is ctx_srq
        )
        assert q.get_matching(constants.EventType.io_completion, timeout_ms=0) is ctx_io

    def test_get_matching_non_matching_returns_none(self):
        q = EventQueue()
        q.put(EventContext(event_type=constants.EventType.io_completion))
        assert q.get_matching(constants.EventType.service_request, timeout_ms=0) is None

    def test_get_matching_blocks_until_match(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.service_request)

        def delayed_put():
            time.sleep(0.05)
            q.put(ctx)

        t = threading.Thread(target=delayed_put)
        t.start()
        assert (
            q.get_matching(constants.EventType.service_request, timeout_ms=None) is ctx
        )
        t.join()

    def test_get_matching_positive_timeout(self):
        q = EventQueue()
        start = time.time()
        result = q.get_matching(constants.EventType.service_request, timeout_ms=50)
        elapsed = time.time() - start
        assert result is None
        assert elapsed >= 0.04

    def test_get_matching_positive_timeout_event_arrives(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.service_request)

        def delayed_put():
            time.sleep(0.02)
            q.put(ctx)

        t = threading.Thread(target=delayed_put)
        t.start()
        assert (
            q.get_matching(constants.EventType.service_request, timeout_ms=200) is ctx
        )
        t.join()

    def test_get_matching_none_event_type_returns_any(self):
        q = EventQueue()
        ctx = EventContext(event_type=constants.EventType.io_completion)
        q.put(ctx)
        assert q.get_matching(None, timeout_ms=0) is ctx


# ---------------------------------------------------------------------------
# HandlerRegistry
# ---------------------------------------------------------------------------


class TestHandlerRegistry:
    def test_install_and_fire(self):
        reg = HandlerRegistry()
        calls = []

        def handler(sess, etype, cid, uhandle):
            calls.append((sess, etype, cid, uhandle))

        reg.install(constants.EventType.service_request, handler, "h1")
        reg.fire(constants.EventType.service_request, "session", 42)
        assert calls == [("session", constants.EventType.service_request, 42, "h1")]

    def test_multiple_handlers_fire(self):
        reg = HandlerRegistry()
        calls = []

        def h1(sess, etype, cid, uhandle):
            calls.append("h1")

        def h2(sess, etype, cid, uhandle):
            calls.append("h2")

        reg.install(constants.EventType.service_request, h1, None)
        reg.install(constants.EventType.service_request, h2, None)
        reg.fire(constants.EventType.service_request, "session", 1)
        assert set(calls) == {"h1", "h2"}

    def test_uninstall_by_identity_and_handle(self):
        reg = HandlerRegistry()

        def h1(*_):
            pass

        def h2(*_):
            pass

        reg.install(constants.EventType.service_request, h1, "a")
        reg.install(constants.EventType.service_request, h2, "b")
        assert reg.uninstall(constants.EventType.service_request, h1, "a") is True
        assert reg.uninstall(constants.EventType.service_request, h2, "wrong") is False
        assert reg.uninstall(constants.EventType.service_request, h2, "b") is True
        assert reg.uninstall(constants.EventType.service_request, h1, "a") is False

    def test_uninstall_with_none_user_handle(self):
        reg = HandlerRegistry()

        def h1(*_):
            pass

        reg.install(constants.EventType.service_request, h1, "any")
        assert reg.uninstall(constants.EventType.service_request, h1, None) is True
        assert reg.uninstall(constants.EventType.service_request, h1, None) is False

    def test_fire_catches_exceptions(self):
        reg = HandlerRegistry()
        calls = []

        def bad(*_):
            raise RuntimeError("boom")

        def good(*_):
            calls.append("good")

        reg.install(constants.EventType.service_request, bad, None)
        reg.install(constants.EventType.service_request, good, None)
        with pytest.warns(UserWarning, match="boom"):
            reg.fire(constants.EventType.service_request, "session", 1)
        assert calls == ["good"]

    def test_fire_no_handlers_noop(self):
        reg = HandlerRegistry()
        # Should not raise
        reg.fire(constants.EventType.service_request, "session", 1)


# ---------------------------------------------------------------------------
# EventState
# ---------------------------------------------------------------------------


class TestEventState:
    def test_enable_disable(self):
        st = EventState()
        st.enable(constants.EventType.service_request, constants.EventMechanism.queue)
        assert st.enabled[constants.EventType.service_request] is EventMechanismFlag.QUEUE
        assert st.is_queue_enabled(constants.EventType.service_request) is True
        assert st.is_handler_enabled(constants.EventType.service_request) is False
        st.enable(constants.EventType.service_request, constants.EventMechanism.handler)
        assert st.enabled[constants.EventType.service_request] is (
            EventMechanismFlag.QUEUE | EventMechanismFlag.HANDLER
        )
        assert st.is_handler_enabled(constants.EventType.service_request) is True
        st.disable(constants.EventType.service_request, constants.EventMechanism.queue)
        assert st.enabled[constants.EventType.service_request] is EventMechanismFlag.HANDLER
        assert st.is_queue_enabled(constants.EventType.service_request) is False
        assert st.is_handler_enabled(constants.EventType.service_request) is True
        st.disable(
            constants.EventType.service_request, constants.EventMechanism.handler
        )
        assert constants.EventType.service_request not in st.enabled
        assert st.any_enabled() is False

    def test_any_enabled(self):
        st = EventState()
        assert st.any_enabled() is False
        st.enable(constants.EventType.io_completion, constants.EventMechanism.queue)
        assert st.enabled[constants.EventType.io_completion] is EventMechanismFlag.QUEUE
        assert st.any_enabled() is True

    def test_disable_removes_empty_event_type(self):
        st = EventState()
        st.enable(constants.EventType.service_request, constants.EventMechanism.queue)
        assert st.enabled[constants.EventType.service_request] is EventMechanismFlag.QUEUE
        st.disable(constants.EventType.service_request, constants.EventMechanism.queue)
        # Internal dict should be clean
        assert constants.EventType.service_request not in st.enabled

    def test_enable_combined_bitmask(self):
        st = EventState()
        combined = constants.EventMechanism.queue | constants.EventMechanism.handler
        st.enable(constants.EventType.service_request, combined)
        assert st.enabled[constants.EventType.service_request] is (
            EventMechanismFlag.QUEUE | EventMechanismFlag.HANDLER
        )
        assert st.is_queue_enabled(constants.EventType.service_request) is True
        assert st.is_handler_enabled(constants.EventType.service_request) is True

    def test_disable_combined_bitmask(self):
        st = EventState()
        combined = constants.EventMechanism.queue | constants.EventMechanism.handler
        st.enable(constants.EventType.service_request, combined)
        st.disable(constants.EventType.service_request, combined)
        assert constants.EventType.service_request not in st.enabled
        assert st.is_queue_enabled(constants.EventType.service_request) is False
        assert st.is_handler_enabled(constants.EventType.service_request) is False
        assert st.any_enabled() is False

    def test_disable_all_clears_everything(self):
        st = EventState()
        st.enable(constants.EventType.service_request, constants.EventMechanism.queue)
        st.enable(constants.EventType.service_request, constants.EventMechanism.handler)
        assert st.enabled[constants.EventType.service_request] is (
            EventMechanismFlag.QUEUE | EventMechanismFlag.HANDLER
        )
        st.disable(constants.EventType.service_request, constants.EventMechanism.all)
        assert st.is_queue_enabled(constants.EventType.service_request) is False
        assert st.is_handler_enabled(constants.EventType.service_request) is False
        assert constants.EventType.service_request not in st.enabled


# ---------------------------------------------------------------------------
# highlevel.py  (mocked session)
# ---------------------------------------------------------------------------


@pytest.fixture
def lib_and_session():
    lib = PyVisaLibrary()
    sess = MagicMock()
    sess._event_state = EventState()
    sess._supported_event_types = {constants.EventType.service_request}
    sess._start_event_monitor.return_value = StatusCode.success
    session_id = lib._register(sess)
    sess._session_handle = session_id
    return lib, sess, session_id


class TestHighlevelEventMethods:
    def test_enable_event_delegates_and_starts_monitor(self, lib_and_session):
        lib, sess, sid = lib_and_session
        result = lib.enable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        assert result == StatusCode.success
        assert sess._event_state.is_queue_enabled(constants.EventType.service_request)
        sess._start_event_monitor.assert_called_once()

    def test_disable_event_delegates_and_stops_monitor(self, lib_and_session):
        lib, sess, sid = lib_and_session
        # First enable
        lib.enable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        # Then disable
        result = lib.disable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        assert result == StatusCode.success
        assert not sess._event_state.is_queue_enabled(
            constants.EventType.service_request
        )
        sess._stop_event_monitor.assert_called_once()

    def test_disable_event_does_not_stop_when_other_enabled(self, lib_and_session):
        lib, sess, sid = lib_and_session
        lib.enable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        lib.enable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.handler,
        )
        sess._start_event_monitor.reset_mock()
        sess._stop_event_monitor.reset_mock()
        lib.disable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        # Handler still enabled -> monitor should NOT be stopped
        sess._stop_event_monitor.assert_not_called()

    def test_discard_events_queue(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._event_state.queue.put(
            EventContext(event_type=constants.EventType.service_request)
        )
        result = lib.discard_events(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        assert result == StatusCode.success
        assert sess._event_state.queue.get(timeout_ms=0) is None

    def test_discard_events_all_mechanism(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._event_state.queue.put(
            EventContext(event_type=constants.EventType.service_request)
        )
        result = lib.discard_events(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.all,
        )
        assert result == StatusCode.success
        assert sess._event_state.queue.get(timeout_ms=0) is None

    def test_install_handler(self, lib_and_session):
        lib, sess, sid = lib_and_session

        def my_handler(*_):
            pass

        result = lib.install_handler(
            sid,
            constants.EventType.service_request,
            my_handler,
            "uh",
        )
        assert result == (my_handler, "uh", my_handler, StatusCode.success)
        handlers = sess._event_state.registry._handlers[
            constants.EventType.service_request
        ]
        assert handlers == [(my_handler, "uh")]

    def test_uninstall_handler_success(self, lib_and_session):
        lib, sess, sid = lib_and_session

        def my_handler(*_):
            pass

        sess._event_state.registry.install(
            constants.EventType.service_request, my_handler, "uh"
        )
        result = lib.uninstall_handler(
            sid,
            constants.EventType.service_request,
            my_handler,
            "uh",
        )
        assert result == StatusCode.success

    def test_uninstall_handler_not_installed_raises(self, lib_and_session):
        lib, sess, sid = lib_and_session

        def my_handler(*_):
            pass

        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.uninstall_handler(
                sid,
                constants.EventType.service_request,
                my_handler,
            )
        assert exc_info.value.error_code == StatusCode.error_handler_not_installed

    def test_wait_on_event_success(self, lib_and_session):
        lib, sess, sid = lib_and_session
        ctx = EventContext(
            event_type=constants.EventType.service_request, context_id=123
        )
        sess._event_state.queue.put(ctx)
        etype, ectx, status = lib.wait_on_event(
            sid, constants.EventType.service_request, 1000
        )
        assert etype == constants.EventType.service_request
        assert ectx == 123
        assert status == StatusCode.success

    def test_wait_on_event_timeout_raises(self, lib_and_session):
        lib, sess, sid = lib_and_session
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.wait_on_event(sid, constants.EventType.service_request, 50)
        assert exc_info.value.error_code == StatusCode.error_timeout

    def test_wait_on_event_zero_timeout_raises(self, lib_and_session):
        lib, sess, sid = lib_and_session
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.wait_on_event(sid, constants.EventType.service_request, 0)
        assert exc_info.value.error_code == StatusCode.error_timeout

    def test_wait_on_event_invalid_session(self, lib_and_session):
        lib, _, _ = lib_and_session
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.wait_on_event(999999, constants.EventType.service_request, 0)
        assert exc_info.value.error_code == StatusCode.error_invalid_object

    def test_enable_event_invalid_session(self, lib_and_session):
        lib, _, _ = lib_and_session
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.enable_event(
                999999,
                constants.EventType.service_request,
                constants.EventMechanism.queue,
            )
        assert exc_info.value.error_code == StatusCode.error_invalid_object

    def test_enable_event_unsupported_returns_error(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._supported_event_types = set()
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.enable_event(
                sid,
                constants.EventType.service_request,
                constants.EventMechanism.queue,
            )
        assert exc_info.value.error_code == StatusCode.error_invalid_event

    def test_enable_event_supported_returns_success(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._supported_event_types = {constants.EventType.service_request}
        result = lib.enable_event(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.queue,
        )
        assert result == StatusCode.success

    def test_enable_event_rollback_on_monitor_failure(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._supported_event_types = {constants.EventType.service_request}
        sess._start_event_monitor.return_value = StatusCode.error_io
        with pytest.raises(errors.VisaIOError) as exc_info:
            lib.enable_event(
                sid,
                constants.EventType.service_request,
                constants.EventMechanism.queue,
            )
        assert exc_info.value.error_code == StatusCode.error_io
        assert not sess._event_state.is_queue_enabled(
            constants.EventType.service_request
        )

    def test_discard_events_queue_and_handler_discards_queue(self, lib_and_session):
        lib, sess, sid = lib_and_session
        sess._event_state.queue.put(
            EventContext(event_type=constants.EventType.service_request)
        )
        combined = constants.EventMechanism.queue | constants.EventMechanism.handler
        result = lib.discard_events(
            sid,
            constants.EventType.service_request,
            combined,
        )
        assert result == StatusCode.success
        assert sess._event_state.queue.get(timeout_ms=0) is None

    def test_discard_events_handler_alone_does_not_discard_queue(self, lib_and_session):
        lib, sess, sid = lib_and_session
        ctx = EventContext(event_type=constants.EventType.service_request)
        sess._event_state.queue.put(ctx)
        result = lib.discard_events(
            sid,
            constants.EventType.service_request,
            constants.EventMechanism.handler,
        )
        assert result == StatusCode.success
        assert sess._event_state.queue.get(timeout_ms=0) is ctx


# ---------------------------------------------------------------------------
# VXI-11 SRQ flow (mocked transport)
# ---------------------------------------------------------------------------


class TestVxi11SrqFlow:
    @pytest.fixture
    def mock_vxi11_session(self):
        """Return a partially-initialised TCPIPInstrVxi11 with a mocked iface."""
        from pyvisa_py.tcpip import TCPIPInstrVxi11

        sess = MagicMock(spec=TCPIPInstrVxi11)
        sess._event_state = EventState()
        sess.link = 1
        sess.interface = MagicMock()
        sess.interface.create_intr_chan.return_value = 0
        sess.interface.device_enable_srq.return_value = 0
        sess.interface.destroy_intr_chan.return_value = 0
        sess._srq_server = None
        sess._srq_lifecycle_lock = threading.Lock()
        return sess

    def test_start_event_monitor_calls_enable(self, mock_vxi11_session):
        from pyvisa_py.tcpip import TCPIPInstrVxi11

        sess = mock_vxi11_session
        sess._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        sess.interface.sock.getsockname.return_value = ("192.168.1.2", 12345)

        # Patch SrqInterruptTCPServer so we don't bind a real TCP socket
        with patch("pyvisa_py.tcpip.vxi11.SrqInterruptTCPServer") as MockServer:
            mock_sock = MagicMock()
            mock_sock.getsockname.return_value = ("127.0.0.1", 65432)
            MockServer.return_value.sock = mock_sock

            result = TCPIPInstrVxi11._start_event_monitor(sess)

            assert result == StatusCode.success
            sess.interface.create_intr_chan.assert_called_once_with(
                int(ipaddress.IPv4Address("192.168.1.2")),
                65432,
                vxi11.DEVICE_INTR_PROG,
                vxi11.DEVICE_INTR_VERS,
                vxi11.DEVICE_TCP,
            )
            sess.interface.device_enable_srq.assert_called_once_with(
                sess.link, True, b"srq"
            )
            assert sess._event_state.monitor_thread is not None
            # Clean up
            sess._event_state.stop_flag.set()
            if sess._event_state.monitor_thread is not None:
                sess._event_state.monitor_thread.join(timeout=0.5)

    def test_start_event_monitor_create_intr_chan_error(self, mock_vxi11_session):
        from pyvisa_py.tcpip import TCPIPInstrVxi11

        sess = mock_vxi11_session
        sess._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        sess.interface.sock.getsockname.return_value = ("192.168.1.2", 12345)
        sess.interface.create_intr_chan.return_value = 8

        with patch("pyvisa_py.tcpip.vxi11.SrqInterruptTCPServer") as MockServer:
            mock_sock = MagicMock()
            mock_sock.getsockname.return_value = ("127.0.0.1", 65432)
            MockServer.return_value.sock = mock_sock

            result = TCPIPInstrVxi11._start_event_monitor(sess)

            assert result == StatusCode.error_nonsupported_operation
            assert sess._event_state.monitor_thread is None

    def test_stop_event_monitor_calls_disable(self, mock_vxi11_session):
        from pyvisa_py.tcpip import TCPIPInstrVxi11

        sess = mock_vxi11_session
        sess._event_state.monitor_thread = None
        TCPIPInstrVxi11._stop_event_monitor(sess)
        sess.interface.device_enable_srq.assert_called_once_with(sess.link, False, b"")
        sess.interface.destroy_intr_chan.assert_called_once()

    def test_fire_event_then_wait_on_event(self, lib_and_session):
        """Simulate an SRQ by calling _fire_event on a mocked session."""
        lib, sess, sid = lib_and_session
        sess._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        ctx = EventContext(
            event_type=constants.EventType.service_request,
            status_byte=0x50,
            context_id=9876,
        )
        # Use the real Session._fire_event logic via a partial call
        from pyvisa_py.sessions import Session

        Session._fire_event(sess, constants.EventType.service_request, ctx)
        etype, ectx, status = lib.wait_on_event(
            sid, constants.EventType.service_request, 1000
        )
        assert etype == constants.EventType.service_request
        assert ectx == 9876
        assert status == StatusCode.success

    def test_vxi11_fire_event_handler_mechanism(self, lib_and_session):
        lib, sess, sid = lib_and_session
        calls = []

        def my_handler(session, event_type, context_id, user_handle):
            calls.append((session, event_type, context_id, user_handle))

        sess._event_state.registry.install(
            constants.EventType.service_request, my_handler, "uh"
        )
        sess._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.handler
        )
        ctx = EventContext(
            event_type=constants.EventType.service_request, context_id=5555
        )
        from pyvisa_py.sessions import Session

        Session._fire_event(sess, constants.EventType.service_request, ctx)
        assert calls == [(sid, constants.EventType.service_request, 5555, "uh")]
