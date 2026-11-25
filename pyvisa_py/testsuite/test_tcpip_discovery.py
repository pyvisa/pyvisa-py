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


def test_vxi11_discovery_closes_port_mappers(monkeypatch):
    created = []

    class FakePortMapper:
        def __init__(self, address):
            self.address = address
            self.closed = False
            created.append(self)

        def set_timeout(self, timeout):
            assert timeout == 0

        def send_port(self, args):
            if self.address == "198.51.100.255":
                raise tcpip.rpc.RPCError("no response")

        def recv_port(self, args):
            if self.address == "203.0.113.255":
                raise tcpip.rpc.RPCError("malformed response")
            return [(1, ("192.0.2.42", 12345))]

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        tcpip,
        "psutil",
        types.SimpleNamespace(
            net_if_addrs=lambda: {
                "one": [
                    types.SimpleNamespace(
                        family=tcpip.socket.AF_INET,
                        address="192.0.2.1",
                        netmask="255.255.255.0",
                    )
                ],
                "two": [
                    types.SimpleNamespace(
                        family=tcpip.socket.AF_INET,
                        address="198.51.100.1",
                        netmask="255.255.255.0",
                    )
                ],
                "three": [
                    types.SimpleNamespace(
                        family=tcpip.socket.AF_INET,
                        address="203.0.113.1",
                        netmask="255.255.255.0",
                    )
                ],
            }
        ),
    )
    monkeypatch.setattr(tcpip.rpc, "BroadcastUDPPortMapperClient", FakePortMapper)
    monkeypatch.setattr(tcpip.time, "sleep", lambda _: None)

    assert tcpip.TCPIPInstrVxi11.list_resources() == ["TCPIP::192.0.2.42::INSTR"]
    assert len(created) == 3
    assert all(pmap.closed for pmap in created)
