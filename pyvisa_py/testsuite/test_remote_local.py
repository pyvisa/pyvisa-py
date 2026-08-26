# -*- coding: utf-8 -*-
"""Unit tests for VXI11 and hislip remote/local operations."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyvisa import constants
from pyvisa_py.tcpip import TCPIPInstrHiSLIP, TCPIPInstrVxi11


@pytest.mark.parametrize(
    "mode, expected_method",
    [
        (constants.RENLineOperation.address_gtl, "device_local"),
        (constants.RENLineOperation.asrt_address, "device_remote"),
        (constants.RENLineOperation.asrt_address_llo, "device_remote"),
        (constants.RENLineOperation.deassert_gtl, "device_local"),
    ],
)
def test_vxi11_gpib_control_ren_calls_expected_device_method(mode, expected_method):
    session = object.__new__(TCPIPInstrVxi11)
    session.interface = MagicMock()
    session.link = 7
    session.lock_timeout = 1234
    session._io_timeout = 5678

    session.interface.device_local.return_value = 0
    session.interface.device_remote.return_value = 0

    assert session.gpib_control_ren(mode) == constants.StatusCode.success

    getattr(session.interface, expected_method).assert_called_once_with(
        session.link, 0, session.lock_timeout, session._io_timeout
    )

    if expected_method == "device_remote":
        session.interface.device_local.assert_not_called()
    else:
        session.interface.device_remote.assert_not_called()


@pytest.mark.parametrize("invalid_mode", [-1, 999, "bogus", None, object()])
def test_vxi11_gpib_control_ren_rejects_unsupported_modes(invalid_mode):
    session = object.__new__(TCPIPInstrVxi11)
    session.interface = MagicMock()
    session.link = 7
    session.lock_timeout = 1234
    session._io_timeout = 5678

    assert session.gpib_control_ren(invalid_mode) == (
        constants.StatusCode.error_nonsupported_operation
    )
    session.interface.device_local.assert_not_called()
    session.interface.device_remote.assert_not_called()


@pytest.mark.parametrize(
    "mode, expected_method",
    [
        (constants.RENLineOperation.address_gtl, "justGTL"),
        (constants.RENLineOperation.asrt, "enableRemote"),
        (constants.RENLineOperation.asrt_address, "enableAndGotoRemote"),
        (constants.RENLineOperation.asrt_address_llo, "enableAndGTRLLO"),
        (constants.RENLineOperation.asrt_llo, "enableAndLockoutLocal"),
        (constants.RENLineOperation.deassert, "disableRemote"),
        (constants.RENLineOperation.deassert_gtl, "disableAndGTL"),
    ],
)
def test_hislip_gpib_control_ren_calls_expected_interface_method(mode, expected_method):
    session = object.__new__(TCPIPInstrHiSLIP)
    session.interface = MagicMock()

    assert session.gpib_control_ren(mode) == constants.StatusCode.success
    session.interface.async_remote_local_control.assert_called_once_with(
        expected_method
    )


@pytest.mark.parametrize("invalid_mode", [-1, 999, "bogus", None, object()])
def test_hislip_gpib_control_ren_rejects_unsupported_modes(invalid_mode):
    session = object.__new__(TCPIPInstrHiSLIP)
    session.interface = MagicMock()

    assert session.gpib_control_ren(invalid_mode) == (
        constants.StatusCode.error_nonsupported_operation
    )
    session.interface.async_remote_local_control.assert_not_called()
