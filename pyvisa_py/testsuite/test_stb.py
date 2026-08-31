"""Unit tests for software trigger support across transport sessions."""

from unittest.mock import MagicMock

import pytest

from pyvisa import constants

try:
    from pyvisa_py.serial import SerialSession
except ImportError:
    pass

from pyvisa_py.tcpip import (
    TCPIPInstrHiSLIP,
    TCPIPInstrVxi11,
    TCPIPSocketSession,
)


class TestSTB:
    @pytest.mark.parametrize(
        "io_protocol, expected_result",
        [
            (
                constants.VI_PROT_NORMAL,
                (0, constants.StatusCode.error_invalid_setup),
            ),
            (constants.VI_PROT_4882_STRS, (42, constants.StatusCode.success)),
        ],
    )
    @pytest.mark.skipif(
        "SerialSession" not in globals(), reason="PySerial is not installed"
    )
    def test_serial_read_stb(self, io_protocol, expected_result):
        """Read a serial status byte only with the 488.2 protocol."""
        session = object.__new__(SerialSession)
        session.attrs = {constants.ResourceAttribute.io_prot: io_protocol}
        session.write = MagicMock(return_value=(6, constants.StatusCode.success))
        session.read = MagicMock(return_value=(b"42", constants.StatusCode.success))

        assert session.read_stb() == expected_result

        if io_protocol == constants.VI_PROT_4882_STRS:
            session.write.assert_called_once_with(b"*STB?\n")
            session.read.assert_called_once_with(100)
        else:
            session.write.assert_not_called()
            session.read.assert_not_called()

    def test_vxi11_read_stb(self):
        """Read a status byte using VXI-11."""
        session = object.__new__(TCPIPInstrVxi11)
        session.link = 123
        session._io_timeout = 456
        session._adapt_flags_and_lock_timeout = MagicMock(return_value=(7, 789))
        session.interface = MagicMock()
        session.interface.device_read_stb.return_value = (0, 42)

        assert session.read_stb() == (42, constants.StatusCode.success)

        session._adapt_flags_and_lock_timeout.assert_called_once_with(0)
        session.interface.device_read_stb.assert_called_once_with(123, 7, 789, 456)

    def test_hislip_read_stb(self):
        """Read a status byte using HiSLIP's asynchronous status query."""
        session = object.__new__(TCPIPInstrHiSLIP)
        session.interface = MagicMock()
        session.interface.async_status_query.return_value = 42

        assert session.read_stb() == (42, constants.StatusCode.success)

        session.interface.async_status_query.assert_called_once_with()

    @pytest.mark.parametrize(
        "io_protocol, expected_result",
        [
            (
                constants.VI_PROT_NORMAL,
                (0, constants.StatusCode.error_invalid_setup),
            ),
            (constants.VI_PROT_4882_STRS, (42, constants.StatusCode.success)),
        ],
    )
    def test_socket_read_stb(self, io_protocol, expected_result):
        """Read a socket status byte only with the 488.2 protocol."""
        session = object.__new__(TCPIPSocketSession)
        session.attrs = {constants.ResourceAttribute.io_prot: io_protocol}
        session.write = MagicMock(return_value=(6, constants.StatusCode.success))
        session.read = MagicMock(return_value=(b"42", constants.StatusCode.success))

        assert session.read_stb() == expected_result

        if io_protocol == constants.VI_PROT_4882_STRS:
            session.write.assert_called_once_with(b"*STB?\n")
            session.read.assert_called_once_with(100)
        else:
            session.write.assert_not_called()
            session.read.assert_not_called()
