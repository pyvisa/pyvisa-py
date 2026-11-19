"""Tests for atomic HiSLIP synchronous operations on one session."""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from pyvisa import constants
from pyvisa_py.protocols.hislip import (
    HEADER_SIZE,
    CancellableSocket,
    HiSLIPInterruptedError,
    Instrument,
    RxHeader,
    receive_exact,
    send_msg,
)
from pyvisa_py.tcpip import TCPIPInstrHiSLIP


@pytest.fixture
def link():
    server, raw_client = socket.socketpair()
    server.settimeout(1)
    client = CancellableSocket(raw_client)
    client.settimeout(1)
    instrument = object.__new__(Instrument)
    instrument._sync = client
    instrument.sync_lock = threading.RLock()
    instrument._timeout = 1.0
    instrument._max_msg_size = HEADER_SIZE + 4
    instrument._message_id = 0xFFFF_FF00
    instrument.last_message_id = 0xFFFF_FEFE
    instrument._feature_bitmap = 0
    instrument._receiving = threading.Event()
    instrument._receive_state_lock = threading.Lock()
    instrument._interrupt_condition = threading.Condition(threading.RLock())
    instrument._async_interrupted_seen = False
    instrument._sync_interrupted_seen = False
    instrument._async_channel = MagicMock()
    try:
        yield instrument, server
    finally:
        client.close()
        server.close()


@pytest.fixture
def session(link):
    instrument, _ = link
    session = object.__new__(TCPIPInstrHiSLIP)
    session.interface = instrument
    session.attrs = {
        constants.ResourceAttribute.send_end_enabled: True,
        constants.ResourceAttribute.suppress_end_enabled: False,
        constants.ResourceAttribute.termchar_enabled: False,
        constants.ResourceAttribute.termchar: ord("\n"),
    }
    return session


def start_operation(executor, operation):
    started = threading.Event()
    finished = threading.Event()

    def run():
        started.set()
        try:
            return operation()
        finally:
            finished.set()

    future = executor.submit(run)
    assert started.wait(timeout=1)
    return future, finished


def test_fragmented_writes_do_not_interleave(link, monkeypatch):
    instrument, server = link
    first_packet_sent = threading.Event()
    release_first = threading.Event()
    send_packet = instrument._send_data_packet

    def paused_send(payload):
        send_packet(payload)
        first_packet_sent.set()
        assert release_first.wait(timeout=2)

    monkeypatch.setattr(instrument, "_send_data_packet", paused_send)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(instrument.send, b"AAAAaaaa")
        try:
            assert first_packet_sent.wait(timeout=1)
            second, second_finished = start_operation(
                executor, lambda: instrument.send(b"BBBB")
            )
            assert not second_finished.wait(timeout=0.05)
        finally:
            release_first.set()

        assert first.result(timeout=1) == 8
        assert second.result(timeout=1) == 4

    messages = []
    for _ in range(3):
        header = RxHeader(server)
        payload = receive_exact(server, header.payload_length)
        messages.append((header.msg_type, header.message_parameter, payload))
    assert messages == [
        ("Data", 0xFFFF_FF00, b"AAAA"),
        ("DataEnd", 0xFFFF_FF02, b"aaaa"),
        ("DataEnd", 0xFFFF_FF04, b"BBBB"),
    ]


@pytest.mark.parametrize("operation", ["receive", "send", "trigger", "device_clear"])
def test_sync_operations_wait_for_active_receive(link, monkeypatch, operation):
    instrument, server = link
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"first")
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"second")
    receiving = threading.Event()
    release_first = threading.Event()
    next_header = instrument._next_data_header

    def paused_header():
        if not receiving.is_set():
            receiving.set()
            assert release_first.wait(timeout=2)
        return next_header()

    monkeypatch.setattr(instrument, "_next_data_header", paused_header)
    monkeypatch.setattr(instrument, "_perform_device_clear", MagicMock())
    operations = {
        "receive": lambda: instrument.receive(6),
        "send": lambda: instrument.send(b"next"),
        "trigger": instrument.trigger,
        "device_clear": instrument.device_clear,
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(instrument.receive, 5)
        try:
            assert receiving.wait(timeout=1)
            second, second_finished = start_operation(executor, operations[operation])
            assert not second_finished.wait(timeout=0.05)
        finally:
            release_first.set()

        assert first.result(timeout=1) == b"first"
        result = second.result(timeout=1)
        if operation == "receive":
            assert result == b"second"
        elif operation == "send":
            assert result == 4
        else:
            assert result is None


def test_read_status_is_captured_before_another_operation(link, session, monkeypatch):
    instrument, server = link
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"done")
    received = threading.Event()
    release_read = threading.Event()
    receive = instrument.receive

    def paused_receive(*args, **kwargs):
        data = receive(*args, **kwargs)
        received.set()
        assert release_read.wait(timeout=2)
        return data

    monkeypatch.setattr(instrument, "receive", paused_receive)
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(session.read, 4)
        try:
            assert received.wait(timeout=1)
            write, write_finished = start_operation(
                executor, lambda: session.write(b"next")
            )
            assert not write_finished.wait(timeout=0.05)
        finally:
            release_read.set()

        assert read.result(timeout=1) == (b"done", constants.StatusCode.success)
        assert write.result(timeout=1) == (4, constants.StatusCode.success)


def test_abort_recovery_finishes_before_another_operation(link, session, monkeypatch):
    instrument, _ = link
    recovering = threading.Event()
    release_recovery = threading.Event()
    monkeypatch.setattr(
        instrument, "receive", MagicMock(side_effect=HiSLIPInterruptedError())
    )

    def recover():
        recovering.set()
        assert release_recovery.wait(timeout=2)

    monkeypatch.setattr(instrument, "complete_terminate", recover)
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(session.read, 4)
        try:
            assert recovering.wait(timeout=1)
            write, write_finished = start_operation(
                executor, lambda: session.write(b"next")
            )
            assert not write_finished.wait(timeout=0.05)
        finally:
            release_recovery.set()

        assert read.result(timeout=1) == (b"", constants.StatusCode.error_abort)
        assert write.result(timeout=1) == (4, constants.StatusCode.success)


def test_device_clear_finishes_before_another_write(link, monkeypatch):
    instrument, _ = link
    clearing = threading.Event()
    release_clear = threading.Event()

    def clear():
        clearing.set()
        assert release_clear.wait(timeout=2)

    monkeypatch.setattr(instrument, "_perform_device_clear", clear)
    with ThreadPoolExecutor(max_workers=2) as executor:
        device_clear = executor.submit(instrument.device_clear)
        try:
            assert clearing.wait(timeout=1)
            write, write_finished = start_operation(
                executor, lambda: instrument.send(b"next")
            )
            assert not write_finished.wait(timeout=0.05)
        finally:
            release_clear.set()

        device_clear.result(timeout=1)
        assert write.result(timeout=1) == 4
        assert instrument.last_message_id == 0xFFFF_FF00
        assert instrument._message_id == 0xFFFF_FF02


def test_terminate_can_cancel_a_serialised_receive(link, monkeypatch):
    instrument, _ = link
    receiving = threading.Event()
    next_header = instrument._next_data_header

    def read_header():
        receiving.set()
        return next_header()

    monkeypatch.setattr(instrument, "_next_data_header", read_header)
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(instrument.receive, 4)
        assert receiving.wait(timeout=1)
        executor.submit(instrument.terminate).result(timeout=0.5)
        with pytest.raises(HiSLIPInterruptedError):
            read.result(timeout=0.5)
    assert not instrument._receiving.is_set()


@pytest.mark.parametrize("error", [socket.timeout, RuntimeError])
def test_read_failure_releases_the_operation_lock(link, session, monkeypatch, error):
    instrument, _ = link
    monkeypatch.setattr(
        instrument, "_next_data_header", MagicMock(side_effect=error("read failed"))
    )
    if error is socket.timeout:
        assert session.read(4) == (b"", constants.StatusCode.error_timeout)
    else:
        with pytest.raises(RuntimeError, match="read failed"):
            session.read(4)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(instrument.send, b"next").result(timeout=1) == 4


@pytest.mark.parametrize("operation", ["send", "trigger"])
def test_unparsed_messages_are_not_blindly_flushed(link, operation):
    instrument, server = link
    send_msg(server, "Data", 0, 0xFFFF_FFFF, b"unparsed-")
    if operation == "send":
        instrument.send(b"next")
    else:
        instrument.trigger()
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"response")

    assert instrument.receive(4096) == b"unparsed-response"
