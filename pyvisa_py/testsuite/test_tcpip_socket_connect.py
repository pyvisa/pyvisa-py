# -*- coding: utf-8 -*-
"""Test opening TCPIP SOCKET resources against a refused or accepted connection.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import errno
import socket
from contextlib import closing

import pytest

from pyvisa import ResourceManager, constants, errors


@pytest.fixture
def refused_port():
    """A loopback port that is bound but not listening, so connects are refused.

    The socket is kept open for the duration of the test, which guarantees
    that no other process can take the port in the meantime.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


@pytest.fixture
def listening_port():
    """A loopback port that accepts connections."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]


def test_open_resource_fails_when_connection_is_refused(refused_port):
    """A refused connection must make open_resource fail.

    A refused non-blocking connect also makes the socket readable and
    writable, the failure being only reported through SO_ERROR. It used to be
    taken for a successful connection and the error only showed up on the first
    read or write.
    """
    with closing(ResourceManager("@py")) as rm:
        sessions = set(rm.visalib.sessions)
        with pytest.raises(errors.VisaIOError) as exc:
            rm.open_resource(f"TCPIP::127.0.0.1::{refused_port}::SOCKET")
        assert exc.value.error_code == constants.StatusCode.error_resource_not_found
        assert set(rm.visalib.sessions) == sessions


def test_open_resource_succeeds_when_connection_is_accepted(listening_port):
    with closing(ResourceManager("@py")) as rm:
        with rm.open_resource(f"TCPIP::127.0.0.1::{listening_port}::SOCKET") as inst:
            assert inst.session in rm.visalib.sessions


def test_open_resource_fails_when_connect_fails_synchronously(monkeypatch):
    """A connect error returned by connect_ex itself must make open_resource fail.

    When the error is reported synchronously (for example no route to host),
    SO_ERROR stays 0 and the unconnected socket is reported as ready.
    """

    class FailingSocket(socket.socket):
        def connect_ex(self, address):
            return errno.ENETUNREACH

    sock = FailingSocket(socket.AF_INET, socket.SOCK_STREAM)
    monkeypatch.setattr("pyvisa_py.tcpip.socket.socket", lambda *args, **kwargs: sock)
    with closing(ResourceManager("@py")) as rm:
        sessions = set(rm.visalib.sessions)
        with pytest.raises(errors.VisaIOError) as exc:
            rm.open_resource("TCPIP::127.0.0.1::5025::SOCKET", open_timeout=100)
        assert exc.value.error_code == constants.StatusCode.error_resource_not_found
        assert set(rm.visalib.sessions) == sessions
    assert sock.fileno() == -1
