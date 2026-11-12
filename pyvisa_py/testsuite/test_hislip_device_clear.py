"""Tests for HiSLIP device-clear synchronization and feature negotiation."""

import socket
import threading
from unittest.mock import MagicMock

import pytest

from pyvisa import constants
from pyvisa_py.protocols.hislip import Instrument, RxHeader, send_msg
from pyvisa_py.tcpip import TCPIPInstrHiSLIP


def test_device_clear_discards_pending_messages_and_uses_final_features():
    server, client = socket.socketpair()
    server.settimeout(1)
    client.settimeout(1)
    try:
        send_msg(server, "DataEnd", 0, 0xFFFF_FEFE, b"stale response")
        received_messages = []

        def acknowledge_device_clear():
            received_messages.append(RxHeader(server))
            send_msg(server, "DeviceClearAcknowledge", 1, 0, b"")

        server_thread = threading.Thread(target=acknowledge_device_clear)
        server_thread.start()

        instrument = object.__new__(Instrument)
        instrument.sync_lock = threading.RLock()
        instrument._sync = client
        instrument._async_channel = MagicMock()
        instrument._message_id = 0xFFFF_FF00
        instrument._feature_bitmap = 0
        instrument._rmt = 0
        instrument._payload_remaining = 0
        instrument._msg_type = ""
        instrument._last_read_rmt = False
        instrument._last_read_termchar = False
        instrument._interrupt_condition = threading.Condition(threading.RLock())
        instrument._async_interrupted_seen = False
        instrument._sync_interrupted_seen = False
        instrument.async_device_clear = MagicMock(return_value=0x03)

        instrument.device_clear()
        server_thread.join(timeout=1)

        assert instrument.feature_bitmap == 0x01
        instrument.async_device_clear.assert_called_once_with()
        assert not server_thread.is_alive()
        assert len(received_messages) == 1
        sent_message = received_messages[0]
        assert sent_message.msg_type == "DeviceClearComplete"
        assert sent_message.control_code == 0x03
        assert sent_message.message_parameter == 0
        assert sent_message.payload_length == 0
        assert instrument._message_id == 0xFFFF_FF00
        assert instrument.last_message_id is None
    finally:
        server.close()
        client.close()


def test_device_clear_discards_partially_read_message():
    server, client = socket.socketpair()
    server.settimeout(1)
    client.settimeout(1)
    try:
        send_msg(server, "DataEnd", 0, 0xFFFF_FFFF, b"partial-and-remaining-payload")
        received_messages = []

        def acknowledge_device_clear():
            received_messages.append(RxHeader(server))
            send_msg(server, "DeviceClearAcknowledge", 0, 0, b"")

        server_thread = threading.Thread(target=acknowledge_device_clear)
        server_thread.start()

        instrument = object.__new__(Instrument)
        instrument.sync_lock = threading.RLock()
        instrument._sync = client
        instrument._async_channel = MagicMock()
        instrument._message_id = 0xFFFF_FF00
        instrument._last_message_id = 0xFFFF_FF00
        instrument._feature_bitmap = 0
        instrument._rmt = 0
        instrument._payload_remaining = 0
        instrument._msg_type = ""
        instrument._last_read_rmt = False
        instrument._last_read_termchar = False
        instrument._receiving = threading.Event()
        instrument._receive_state_lock = threading.Lock()
        instrument._interrupt_condition = threading.Condition(threading.RLock())
        instrument._async_interrupted_seen = False
        instrument._sync_interrupted_seen = False
        instrument.async_device_clear = MagicMock(return_value=0)

        assert instrument.receive(7) == b"partial"
        assert instrument._payload_remaining > 0

        instrument.device_clear()
        server_thread.join(timeout=1)

        assert not server_thread.is_alive()
        assert len(received_messages) == 1
        assert received_messages[0].msg_type == "DeviceClearComplete"
        assert instrument._payload_remaining == 0
        assert instrument._msg_type == ""
        assert instrument.last_message_id is None
    finally:
        server.close()
        client.close()


@pytest.mark.parametrize("operation", ["send", "trigger"])
@pytest.mark.parametrize("termination_char", [None, ord("\n")])
def test_send_and_trigger_discard_partially_read_message(operation, termination_char):
    server, client = socket.socketpair()
    server.settimeout(1)
    client.settimeout(1)
    try:
        send_msg(server, "DataEnd", 0, 0xFFFF_FFFF, b"partial\nremaining-payload")

        instrument = object.__new__(Instrument)
        instrument.sync_lock = threading.RLock()
        instrument._sync = client
        instrument._max_msg_size = 4096
        instrument._message_id = 0xFFFF_FF00
        instrument._last_message_id = 0xFFFF_FF00
        instrument._rmt = 0
        instrument._payload_remaining = 0
        instrument._msg_type = ""
        instrument._last_read_rmt = False
        instrument._last_read_termchar = False
        instrument._receiving = threading.Event()
        instrument._receive_state_lock = threading.Lock()
        instrument._interrupt_condition = threading.Condition(threading.RLock())
        instrument._async_interrupted_seen = False
        instrument._sync_interrupted_seen = False

        count = 7 if termination_char is None else 4096
        expected = b"partial" if termination_char is None else b"partial\n"
        assert instrument.receive(count, termination_char=termination_char) == expected
        assert instrument._payload_remaining > 0

        if operation == "send":
            instrument.send(b"*IDN?\n")
        else:
            instrument.trigger()
        sent_header = RxHeader(server)
        assert sent_header.msg_type == ("DataEnd" if operation == "send" else "Trigger")
        if sent_header.payload_length:
            assert server.recv(sent_header.payload_length) == b"*IDN?\n"

        send_msg(server, "DataEnd", 0, sent_header.message_parameter, b"fresh\n")
        assert instrument.receive(4096) == b"fresh\n"
    finally:
        server.close()
        client.close()


def test_device_clear_does_not_discard_fatal_error():
    server, client = socket.socketpair()
    server.settimeout(1)
    client.settimeout(1)
    try:
        send_msg(server, "FatalError", 0, 0, b"device failed")

        instrument = object.__new__(Instrument)
        instrument.sync_lock = threading.RLock()
        instrument._payload_remaining = 0
        instrument._sync = client

        with pytest.raises(RuntimeError, match="HiSLIP fatal error"):
            instrument.device_clear_complete(0)

        server.settimeout(0.05)
        with pytest.raises(socket.timeout):
            server.recv(1)
    finally:
        server.close()
        client.close()


@pytest.mark.parametrize(
    ("feature_bitmap", "expected_overlap"),
    [
        (0, constants.VI_FALSE),
        (0x01, constants.VI_TRUE),
    ],
)
def test_session_uses_final_overlap_feature(feature_bitmap, expected_overlap):
    session = object.__new__(TCPIPInstrHiSLIP)
    session.interface = MagicMock(feature_bitmap=feature_bitmap)
    session.attrs = {}

    session._update_feature_attributes()

    assert (
        session.attrs[constants.ResourceAttribute.tcpip_hislip_overlap_enable]
        == expected_overlap
    )
