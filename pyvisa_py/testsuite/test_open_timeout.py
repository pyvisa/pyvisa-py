# -*- coding: utf-8 -*-
"""Test that a default (0) open_timeout does not collapse connect deadlines.

``ResourceManager.open_resource`` passes ``VI_TMO_IMMEDIATE`` (0) when the
caller supplies no ``open_timeout``. Every transport has to read that 0 as
2000 ms, per VPP-4.3 RECOMMENDATION 4.3.3, not as "give up immediately".

"""

import pytest

from pyvisa_py.common import DEFAULT_OPEN_TIMEOUT, connect_timeout
from pyvisa_py.protocols import hislip, rpc


@pytest.mark.parametrize(
    "open_timeout, expected",
    [(None, 2.0), (0, 2.0), (0.0, 2.0), (1234, 1.234)],
)
def test_connect_timeout(open_timeout, expected):
    assert connect_timeout(open_timeout) == expected


def test_default_open_timeout_matches_spec():
    """VPP-4.3 RECOMMENDATION 4.3.3 names 2000 ms."""
    assert DEFAULT_OPEN_TIMEOUT == 2000.0


@pytest.mark.parametrize("open_timeout, expected", [(None, 2.0), (0, 2.0), (2500, 2.5)])
def test_raw_tcp_client_connect_deadline(monkeypatch, open_timeout, expected):
    """RawTCPClient gives the handshake the default deadline, not 0 seconds."""
    seen = []

    def fake_connect(sock, host, port, timeout=0):
        seen.append(timeout)
        return True

    monkeypatch.setattr(rpc, "_connect", fake_connect)
    rpc.RawTCPClient("localhost", 1, 1, 1234, open_timeout)
    assert seen == [expected]


@pytest.mark.parametrize("open_timeout, expected", [(None, 2.0), (0, 2.0), (2500, 2.5)])
def test_hislip_connect_deadline(monkeypatch, open_timeout, expected):
    """hislip.Instrument applies open_timeout to both channel connections."""
    seen = []

    class FakeSocket:
        def settimeout(self, value):
            seen.append(value)

        def connect(self, address):
            raise _Connected()

        def setsockopt(self, *args):
            pass

    class _Connected(Exception):
        pass

    monkeypatch.setattr(hislip.socket, "socket", lambda *a, **kw: FakeSocket())
    with pytest.raises(_Connected):
        hislip.Instrument("localhost", open_timeout=open_timeout)
    # The deadline is set before connect() is attempted.
    assert seen == [expected]
