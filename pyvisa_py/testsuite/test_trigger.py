"""Unit tests for software trigger support across transport sessions."""

from unittest.mock import MagicMock

import pytest

from pyvisa import constants

have_serial = True
try:
    from pyvisa_py.serial import SerialSession
except ImportError:
    have_serial = False

from pyvisa_py.tcpip import (  # noqa: E402
    TCPIPInstrHiSLIP,
    TCPIPInstrVxi11,
    TCPIPSocketSession,
)


@pytest.mark.parametrize(
    "protocol, expected_status, should_trigger",
    [
        (constants.TriggerProtocol.default, constants.StatusCode.success, True),
        (
            constants.TriggerProtocol.on,
            constants.StatusCode.error_nonsupported_operation,
            False,
        ),
    ],
)
def test_hislip_assert_trigger(protocol, expected_status, should_trigger):
    session = object.__new__(TCPIPInstrHiSLIP)
    session.interface = MagicMock()

    assert session.assert_trigger(protocol) == expected_status

    if should_trigger:
        session.interface.trigger.assert_called_once_with()
    else:
        session.interface.trigger.assert_not_called()


@pytest.mark.parametrize(
    "protocol, expected_status, should_trigger",
    [
        (constants.TriggerProtocol.default, constants.StatusCode.success, True),
        (
            constants.TriggerProtocol.on,
            constants.StatusCode.error_nonsupported_operation,
            False,
        ),
    ],
)
def test_vxi11_assert_trigger(protocol, expected_status, should_trigger):
    session = object.__new__(TCPIPInstrVxi11)
    session.interface = MagicMock()
    session.link = 1
    session._io_timeout = 2000
    session._adapt_flags_and_lock_timeout = MagicMock(return_value=(0, 0))
    session.interface.device_trigger.return_value = 0

    assert session.assert_trigger(protocol) == expected_status

    if should_trigger:
        session.interface.device_trigger.assert_called_once_with(1, 0, 0, 2000)
    else:
        session.interface.device_trigger.assert_not_called()


@pytest.mark.parametrize(
    "session_class",
    [SerialSession, TCPIPSocketSession] if have_serial else [TCPIPSocketSession],
)
@pytest.mark.parametrize(
    "io_prot",
    [constants.VI_PROT_NORMAL, constants.VI_PROT_4882_STRS],
)
@pytest.mark.parametrize(
    "protocol",
    [constants.TriggerProtocol.default, constants.TriggerProtocol.on],
)
def test_serial_and_socket_assert_trigger(protocol, io_prot, session_class):
    session = object.__new__(session_class)
    session.attrs = {constants.ResourceAttribute.io_prot: io_prot}
    session.write = MagicMock(return_value=(5, constants.StatusCode.success))

    expected_status = (
        constants.StatusCode.success
        if (
            protocol == constants.TriggerProtocol.default
            and io_prot == constants.VI_PROT_4882_STRS
        )
        else constants.StatusCode.error_nonsupported_operation
    )

    assert session.assert_trigger(protocol) == expected_status

    if expected_status == constants.StatusCode.success:
        session.write.assert_called_once_with(b"*TRG\n")
    else:
        session.write.assert_not_called()
