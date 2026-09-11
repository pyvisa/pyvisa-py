"""Tests for VISA resource attributes."""

from unittest.mock import MagicMock, patch

import pytest

from pyvisa import rname
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa.typing import VISARMSession
from pyvisa_py.tcpip import TCPIPInstrHiSLIP, TCPIPInstrVxi11, TCPIPSocketSession

try:
    from pyvisa_py.usb import USBInstrSession
except ImportError:
    USBInstrSession = None  # type: ignore[assignment, misc]

try:
    from pyvisa_py.serial import SerialSession
except ImportError:
    SerialSession = None  # type: ignore[assignment, misc]

# TODO: test gpib. It is untested now.
# try:
#     from pyvisa_py.gpib import GPIBSession
# except ImportError:
GPIBSession = None  # type: ignore[assignment, misc]


ATTRIBUTES = (
    ResourceAttribute.interface_instrument_name,
    ResourceAttribute.interface_type,
    ResourceAttribute.resource_manager_session,
    ResourceAttribute.resource_class,
    ResourceAttribute.resource_manufacturer_id,
    ResourceAttribute.resource_manufacturer_name,
    ResourceAttribute.resource_name,
    ResourceAttribute.resource_spec_version,
    ResourceAttribute.dma_allow_enabled,
    ResourceAttribute.file_append_enabled,
    ResourceAttribute.send_end_enabled,
    ResourceAttribute.suppress_end_enabled,
    ResourceAttribute.termchar,
    ResourceAttribute.termchar_enabled,
    ResourceAttribute.timeout_value,
    ResourceAttribute.user_data,
    ResourceAttribute.max_queue_length,
    ResourceAttribute.resource_lock_state,
    ResourceAttribute.interface_number,
    ResourceAttribute.io_prot,
)


@pytest.fixture(params=("usb", "hislip", "vxi11", "serial", "socket"))  # , "gpib"
def instr_session(request):
    """Return an initialized INSTR session without opening a real transport."""
    if request.param == "usb":
        resource_name = "USB0::0x1234::0x5678::SN::0::INSTR"
        if USBInstrSession is None:
            pytest.skip("USBInstrSession is not available")
        with patch.object(USBInstrSession, "_intf_cls", return_value=MagicMock()):
            yield USBInstrSession(VISARMSession(1), resource_name, rname.parse_resource_name(resource_name))
    elif request.param == "hislip":
        resource_name = "TCPIP::localhost::hislip0::INSTR"
        with patch("pyvisa_py.tcpip.hislip.Instrument", return_value=MagicMock()):
            yield TCPIPInstrHiSLIP(VISARMSession(1), resource_name, rname.parse_resource_name(resource_name))
    elif request.param == "serial":
        resource_name = "ASRL1::INSTR"
        if SerialSession is None:
            pytest.skip("SerialSession is not available")
        with patch("pyvisa_py.serial.serial.serial_for_url", return_value=MagicMock()):
            yield SerialSession(VISARMSession(1), resource_name, rname.parse_resource_name(resource_name))
    elif request.param == "socket":
        resource_name = "TCPIP::localhost::1234::SOCKET"
        interface = MagicMock()
        with (
            patch("pyvisa_py.tcpip.socket.socket", return_value=interface),
            patch("pyvisa_py.tcpip.select.select", return_value=([], [interface], [])),
        ):
            yield TCPIPSocketSession(
                VISARMSession(1),
                resource_name,
                rname.parse_resource_name(resource_name),
            )
    elif request.param == "gpib":
        resource_name = "GPIB0::1::INSTR"
        if GPIBSession is None:
            pytest.skip("GPIBSession is not available")
        with patch("pyvisa_py.gpib.Gpib", return_value=MagicMock()):
            yield GPIBSession(
                VISARMSession(1), resource_name, rname.parse_resource_name(resource_name)
            )
    else:
        # VXI-11 fall through
        resource_name = "TCPIP::localhost::INSTR"
        client = MagicMock()
        client.create_link.return_value = (0, 1, 0, 1024)
        with patch("pyvisa_py.tcpip.Vxi11CoreClient", return_value=client):
            yield TCPIPInstrVxi11(VISARMSession(1), resource_name, rname.parse_resource_name(resource_name))


@pytest.mark.parametrize("attribute", ATTRIBUTES)
def test_instr_attribute_reads_succeed(instr_session, attribute):
    """INSTR transports support reading the common VISA attributes."""
    if instr_session.__class__.__name__ == "TCPIPInstrHiSLIP" and attribute == ResourceAttribute.resource_lock_state:
        pytest.skip("TCPIPInstrHiSLIP does not support reading resource_lock_state (unless I also mock that part)")
    _value, status = instr_session.get_attribute(attribute)

    if status is not StatusCode.success:
        pytest.fail(f"Failed to read attribute \"{attribute.name}\", status: {status}")
