"""HiSLIP response MessageID validation over real sockets."""

import socket
import threading

import pytest

from pyvisa import constants
from pyvisa_py.protocols.hislip import CancellableSocket, Instrument, send_msg
from pyvisa_py.tcpip import TCPIPInstrHiSLIP


@pytest.fixture
def link():
    server, raw = socket.socketpair()
    client = CancellableSocket(raw)
    client.settimeout(0.2)
    instrument = object.__new__(Instrument)
    instrument._sync = client
    instrument._receiving = threading.Event()
    instrument._receive_state_lock = threading.Lock()
    instrument.last_message_id = 0x1234
    instrument._msg_type = ""
    instrument._payload_remaining = 0
    instrument._rmt = 0
    instrument._last_read_rmt = False
    instrument._last_read_termchar = False
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
        constants.ResourceAttribute.suppress_end_enabled: False,
        constants.ResourceAttribute.termchar_enabled: False,
        constants.ResourceAttribute.termchar: ord("\n"),
    }
    return session


@pytest.mark.parametrize("payload", [b"stale", b""])
def test_wildcard_dataend_is_discarded(link, session, payload):
    instrument, server = link
    send_msg(server, "DataEnd", 0, 0xFFFF_FFFF, payload)
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"fresh")

    assert session.read(100) == (b"fresh", constants.StatusCode.success)


@pytest.mark.parametrize("kind", ["Data", "DataEnd"])
@pytest.mark.parametrize("payload", [b"wrong", b""])
def test_mismatched_id_discards_buffered_prefix(link, session, kind, payload):
    instrument, server = link
    send_msg(server, "Data", 0, instrument.last_message_id, b"prefix-")
    send_msg(server, kind, 0, 0x2222, payload)
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"fresh")

    assert session.read(100) == (b"fresh", constants.StatusCode.success)


def test_wildcard_data_is_valid(link, session):
    instrument, server = link
    send_msg(server, "Data", 0, 0xFFFF_FFFF, b"prefix-")
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"fresh")

    assert session.read(100) == (b"prefix-fresh", constants.StatusCode.success)


def test_stale_messages_do_not_consume_requested_count(link, session):
    instrument, server = link
    send_msg(server, "Data", 0, instrument.last_message_id, b"old")
    send_msg(server, "Data", 0, 0x2222, b"wrong")
    send_msg(server, "DataEnd", 0, 0xFFFF_FFFF, b"also-wrong")
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"abcdef")

    assert session.read(4) == (b"abcd", constants.StatusCode.success_max_count_read)
    assert not instrument._last_read_rmt
    assert session.read(4) == (b"ef", constants.StatusCode.success)


def test_stale_message_terminators_are_not_delivered(link, session):
    instrument, server = link
    session.attrs[constants.ResourceAttribute.termchar_enabled] = True
    send_msg(server, "Data", 0, instrument.last_message_id, b"old")
    send_msg(server, "DataEnd", 0, 0x2222, b"wrong\n")
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"fresh\nrest")

    assert session.read(100) == (
        b"fresh\n",
        constants.StatusCode.success_termination_character_read,
    )
    assert not instrument._last_read_rmt
    assert session.read(100) == (b"rest", constants.StatusCode.success)


def test_discarded_end_does_not_override_count_status(link, session):
    instrument, server = link
    session.attrs[constants.ResourceAttribute.suppress_end_enabled] = True
    send_msg(server, "DataEnd", 0, instrument.last_message_id, b"old")
    send_msg(server, "Data", 0, 0x2222, b"wrong")
    send_msg(server, "Data", 0, instrument.last_message_id, b"fresh")

    assert session.read(5) == (b"fresh", constants.StatusCode.success_max_count_read)
    assert not instrument._last_read_rmt
    assert instrument._rmt == 0


def test_discard_preserves_previously_delivered_rmt(link, session):
    instrument, server = link
    instrument._rmt = 1
    send_msg(server, "DataEnd", 0, 0x2222, b"wrong")
    send_msg(server, "Data", 0, instrument.last_message_id, b"fresh")

    assert session.read(5) == (b"fresh", constants.StatusCode.success_max_count_read)
    assert not instrument._last_read_rmt
    assert instrument._rmt == 1
