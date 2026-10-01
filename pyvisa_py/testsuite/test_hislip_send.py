"""Tests for HiSLIP write message termination."""

from unittest.mock import MagicMock

from pyvisa_py.protocols.hislip import HEADER_SIZE, Instrument


def make_instrument():
    instrument = Instrument.__new__(Instrument)
    instrument._max_msg_size = HEADER_SIZE + 4
    instrument._send_data_packet = MagicMock()
    instrument._send_data_end_packet = MagicMock()
    return instrument


def test_send_with_end_marks_final_packet():
    instrument = make_instrument()

    assert instrument.send(b"abcdef", send_end=True) == 6

    instrument._send_data_packet.assert_called_once_with(memoryview(b"abcd"))
    instrument._send_data_end_packet.assert_called_once_with(memoryview(b"ef"))


def test_send_without_end_uses_data_packets_only():
    instrument = make_instrument()

    assert instrument.send(b"abcdef", send_end=False) == 6

    assert instrument._send_data_packet.call_count == 2
    instrument._send_data_end_packet.assert_not_called()
