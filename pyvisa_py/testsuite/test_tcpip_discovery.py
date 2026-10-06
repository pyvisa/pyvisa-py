"""Test TCPIP resource discovery when the mDNS socket cannot be bound."""

import errno
import types

import pytest

from pyvisa_py import tcpip


def _unbindable_zeroconf(*args, **kwargs):
    # What zeroconf raises when another mDNS stack (e.g. avahi) holds the port
    raise OSError(errno.EADDRINUSE, "Address already in use")


@pytest.fixture
def unbindable_zeroconf(monkeypatch):
    fake = types.SimpleNamespace(ServiceListener=object, Zeroconf=_unbindable_zeroconf)
    monkeypatch.setattr(tcpip, "zeroconf", fake)


def test_get_services_port_in_use(unbindable_zeroconf):
    with pytest.warns(UserWarning, match="Address already in use"):
        assert tcpip.get_services("_hislip._tcp.local.", wait_time=0) == {}


@pytest.mark.parametrize(
    "session",
    [tcpip.TCPIPInstrHiSLIP, tcpip.TCPIPInstrVicp, tcpip.TCPIPSocketSession],
)
def test_list_resources_port_in_use(unbindable_zeroconf, session):
    with pytest.warns(UserWarning, match="Address already in use"):
        assert session.list_resources(wait_time=0) == []
