"""Tests for opening VXI-11 resources."""

from unittest.mock import ANY, MagicMock, patch

import pytest
from pyvisa import constants, rname

from pyvisa_py.tcpip import TCPIPInstrVxi11


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

    client.create_link.assert_called_once_with(
        ANY, 0, 0, parsed.lan_device_name
    )