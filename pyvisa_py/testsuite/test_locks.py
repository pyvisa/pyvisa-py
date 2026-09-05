"""Tests for locking resources."""

from unittest.mock import ANY, MagicMock, patch

import pytest

from pyvisa import constants, errors, rname
from pyvisa.typing import VISARMSession
from pyvisa_py import highlevel
from pyvisa_py.tcpip import TCPIPInstrHiSLIP, TCPIPInstrVxi11


@pytest.mark.parametrize(
    "open_timeout, expected_lock_timeout",
    [
        (0, 0),
        (2500, 2500),
        (constants.VI_TMO_INFINITE, 2**32 - 1),
    ],
)
def test_open_with_exclusive_lock_passes_lock_to_create_link(
    open_timeout, expected_lock_timeout
):
    resource_name = "TCPIP::localhost::INSTR"
    parsed = rname.parse_resource_name(resource_name)
    client = MagicMock()
    client.create_link.return_value = (0, 1, 0, 1024)

    with patch("pyvisa_py.tcpip.Vxi11CoreClient", return_value=client):
        TCPIPInstrVxi11(
            1,
            resource_name,
            parsed,
            constants.AccessModes.exclusive_lock,
            open_timeout,
        )

    client.create_link.assert_called_once_with(
        ANY, 1, expected_lock_timeout, parsed.lan_device_name
    )


def test_open_without_exclusive_lock_passes_lock_to_create_link():
    resource_name = "TCPIP::localhost::INSTR"
    parsed = rname.parse_resource_name(resource_name)
    client = MagicMock()
    client.create_link.return_value = (0, 1, 0, 1024)

    with patch("pyvisa_py.tcpip.Vxi11CoreClient", return_value=client):
        TCPIPInstrVxi11(
            1,
            resource_name,
            parsed,
            constants.AccessModes.no_lock,
            1234,
        )

    client.create_link.assert_called_once_with(ANY, 0, 0, parsed.lan_device_name)


@pytest.mark.parametrize(
    "lockwait, expected_flags",
    [
        (0, 0x8),
        (1, 0x9),
    ],
)
def test_write_sets_device_write_flags_and_lock_timeout(lockwait, expected_flags):
    resource_name = "TCPIP::localhost::INSTR"
    parsed = rname.parse_resource_name(resource_name)
    client = MagicMock()
    client.create_link.return_value = (0, 1, 0, 1024)
    client.device_write.return_value = (0, 3)

    with patch("pyvisa_py.tcpip.Vxi11CoreClient", return_value=client):
        session = TCPIPInstrVxi11(
            1,
            resource_name,
            parsed,
            constants.AccessModes.no_lock,
            1234,
        )

    session.attrs[constants.ResourceAttribute.lockwait] = lockwait  # type: ignore[attr-defined]
    if lockwait:
        expected_lock_timeout = session._io_timeout
    else:
        expected_lock_timeout = constants.VI_TMO_IMMEDIATE
    status = session.write(b"abc")

    assert status == (3, constants.StatusCode.success)
    args, _ = client.device_write.call_args
    assert args[0] == session.link
    assert args[1] == session._io_timeout
    assert args[2] == expected_lock_timeout
    assert args[3] == expected_flags
    assert args[4] == b"abc"


@pytest.mark.parametrize(
    "lock_type, timeout, expected_flags",
    [
        (constants.Lock.exclusive, 0, 0x0),
        (constants.Lock.exclusive, 1000, 0x1),
        (constants.Lock.shared, 0, None),
        (constants.Lock.shared, 1000, None),
    ],
)
def test_highlevel_lock_sets_vxi11_device_lock_flags_and_timeout(
    lock_type, timeout, expected_flags
):
    resource_name = "TCPIP::localhost::INSTR"
    parsed = rname.parse_resource_name(resource_name)
    client = MagicMock()
    client.create_link.return_value = (0, 1, 0, 1024)
    client.device_lock.return_value = 0

    with patch("pyvisa_py.tcpip.Vxi11CoreClient", return_value=client):
        session = TCPIPInstrVxi11(
            1,
            resource_name,
            parsed,
            constants.AccessModes.no_lock,
            1234,
        )

    library = highlevel.PyVisaLibrary()
    library.sessions = {1: session}

    if lock_type == constants.Lock.shared:
        with pytest.raises(errors.VisaIOError):
            library.lock(1, lock_type, timeout)
        client.device_lock.assert_not_called()
        return

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (constants.VI_NO_LOCK, constants.StatusCode.success)

    key, status = library.lock(1, lock_type, timeout)

    assert key == ""
    assert status == constants.StatusCode.success
    client.device_lock.assert_called_once_with(session.link, expected_flags, timeout)

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (
        constants.VI_EXCLUSIVE_LOCK,
        constants.StatusCode.success,
    )

    client.device_unlock.return_value = 0
    assert library.unlock(1) == constants.StatusCode.success
    client.device_unlock.assert_called_once_with(session.link)

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (constants.VI_NO_LOCK, constants.StatusCode.success)


def test_hislip_lock_updates_resource_lock_state():
    session = object.__new__(TCPIPInstrHiSLIP)
    session.attrs = {}
    session.interface = MagicMock()
    session.interface.async_lock_info.side_effect = [0, 1, 0]
    session.interface.async_lock_request.return_value = "success"
    session.interface.async_lock_release.return_value = "success"

    library = highlevel.PyVisaLibrary()
    library.sessions = {1: session}

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (constants.VI_NO_LOCK, constants.StatusCode.success)

    key, status = library.lock(1, constants.Lock.exclusive, 1000)
    assert (key, status) == ("", constants.StatusCode.success)
    session.interface.async_lock_request.assert_called_once_with(1000, "")

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (
        constants.VI_EXCLUSIVE_LOCK,
        constants.StatusCode.success,
    )

    assert library.unlock(1) == constants.StatusCode.success
    session.interface.async_lock_release.assert_called_once_with("")

    state, status = library.get_attribute(1, constants.VI_ATTR_RSRC_LOCK_STATE)
    assert (state, status) == (constants.VI_NO_LOCK, constants.StatusCode.success)


def test_open_hislip_with_exclusive_lock_failure_closes_connection():
    resource_name = "TCPIP::localhost::hislip0::INSTR"
    parsed = rname.parse_resource_name(resource_name)
    mock_instrument = MagicMock()
    # Return a response that indicates lock request failure
    mock_instrument.async_lock_request.return_value = "failure"

    with patch("pyvisa_py.tcpip.hislip.Instrument", return_value=mock_instrument):
        with pytest.raises(RuntimeError, match="Failed to acquire exclusive lock"):
            TCPIPInstrHiSLIP(
                VISARMSession(1),
                resource_name,
                parsed,
                access_mode=constants.AccessModes.exclusive_lock,
                open_timeout=1000,
            )

    mock_instrument.async_lock_request.assert_called_once()
    mock_instrument.close.assert_called_once()
