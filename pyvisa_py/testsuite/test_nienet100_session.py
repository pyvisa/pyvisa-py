# -*- coding: utf-8 -*-
"""Offline unit tests for the NI GPIB-ENET/100 session layer.

These cover :mod:`pyvisa_py.nienet100` (the pyvisa Session classes) without
touching the network or a real bridge: the session is built with
``__new__`` and a fake interface so only the session-to-interface plumbing is
exercised. Hardware-gated session tests live in ``nienet100_assisted_tests``.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import pytest

from pyvisa.constants import StatusCode

# The session module raises ImportError on load when the upstream pyvisa
# additions it depends on (InterfaceType.ni_enet100_tcpip) are missing; skip
# the whole module cleanly in that case, mirroring the assisted suite.
try:
    from pyvisa_py import nienet100 as ni
    from pyvisa_py.protocols import nienet100 as proto
except ImportError as _import_err:  # pragma: no cover - depends on pyvisa version
    pytestmark = pytest.mark.skip(
        reason="pyvisa-py NI GPIB-ENET/100 session layer unavailable: %s" % _import_err
    )


class _FakeInterface:
    """Minimal stand-in for EnetConnection that records ibsic calls."""

    def __init__(self, raise_err: int | None = None) -> None:
        self.ibsic_calls = 0
        self._raise_err = raise_err

    def ibsic(self) -> None:
        self.ibsic_calls += 1
        if self._raise_err is not None:
            raise proto.NIEnet100IOError(proto.STA_ERR, self._raise_err, "ibsic")


def _make_intfc_session(interface) -> ni._NIEnet100IntfcSession:
    # Bypass Session.__init__ (which needs a live ResourceManager); gpib_send_ifc
    # only touches self.interface.
    session = ni._NIEnet100IntfcSession.__new__(ni._NIEnet100IntfcSession)
    session.interface = interface
    return session


def test_gpib_send_ifc_delegates_to_ibsic():
    fake = _FakeInterface()
    session = _make_intfc_session(fake)
    assert session.gpib_send_ifc() == StatusCode.success
    assert fake.ibsic_calls == 1


def test_gpib_send_ifc_maps_wire_error():
    # A wire-level iberr (here ECIC = not controller-in-charge) must surface as
    # the matching VISA status, not raise.
    fake = _FakeInterface(raise_err=proto.ERR_ECIC)
    session = _make_intfc_session(fake)
    assert session.gpib_send_ifc() == StatusCode.error_not_cic


def test_gpib_send_ifc_without_interface_is_connection_lost():
    session = _make_intfc_session(None)
    assert session.gpib_send_ifc() == StatusCode.error_connection_lost
