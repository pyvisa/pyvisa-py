"""Test USB opening errors without communicating with an instrument."""

import errno
from contextlib import closing

import pytest

import pyvisa
from pyvisa.constants import InterfaceType, StatusCode
from pyvisa_py.sessions import Session

usb = pytest.importorskip("usb")
pytestmark = pytest.mark.skipif(
    (InterfaceType.usb, "INSTR") not in dict(Session.iter_valid_session_classes()),
    reason="PyUSB and a usable backend are required",
)


@pytest.mark.parametrize("error_number", [errno.EBUSY, errno.EIO])
def test_usb_open_error(monkeypatch, error_number):
    error = usb.core.USBError("USB interface unavailable", errno=error_number)
    session_class = Session.get_session_class(InterfaceType.usb, "INSTR")

    def fail_open(*args):
        raise error

    monkeypatch.setattr(session_class, "_intf_cls", fail_open)
    with closing(pyvisa.ResourceManager("@py")) as resource_manager:
        sessions_before = set(resource_manager.visalib.sessions)
        expected = (
            pyvisa.VisaIOError if error_number == errno.EBUSY else usb.core.USBError
        )

        with pytest.raises(expected) as exc:
            resource_manager.open_resource("USB0::0x1234::0x5678::TEST::INSTR")

        if error_number == errno.EBUSY:
            assert exc.value.error_code == StatusCode.error_resource_busy
        else:
            assert exc.value is error
        assert set(resource_manager.visalib.sessions) == sessions_before
