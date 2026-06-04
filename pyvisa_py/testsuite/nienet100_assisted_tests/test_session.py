# -*- coding: utf-8 -*-
"""Session-level hardware tests for the NI GPIB-ENET/100 driver.

Drives the bridge through the full pyvisa stack: a ``ResourceManager``
opens the ``NI-ENET100-TCPIP::INTFC`` interface, then a
``GPIB<n>::<pad>::INSTR`` is dispatched via the wrap-dispatcher in
``pyvisa_py.nienet100`` to ``NIEnet100InstrSession``. This requires the
``InterfaceType.ni_enet100_tcpip`` and ``NIEnet100TCPIPIntfc`` additions
in upstream pyvisa — when those are missing, the whole module skips
cleanly.

See the package ``__init__`` docstring for environment-variable setup.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from typing import Iterator

import pytest

import pyvisa
from pyvisa import constants
from pyvisa.errors import VisaIOError

from . import HOST, IDN_VENDOR, PAD, SAD, TERM, require_bridge, require_instrument

# Skip the entire module if the upstream pyvisa changes that NIENET100
# depends on (InterfaceType.ni_enet100_tcpip + rname.NIEnet100TCPIPIntfc)
# are not in place. The pyvisa_py.nienet100 module raises ImportError on
# load when they are missing.
try:
    from pyvisa_py import nienet100 as _ni
except ImportError as _import_err:
    pytestmark = pytest.mark.skip(
        reason="pyvisa-py NI GPIB-ENET/100 session layer unavailable: %s" % _import_err
    )


# --- fixtures --------------------------------------------------------------


@pytest.fixture(scope="module")
def rm() -> Iterator[pyvisa.ResourceManager]:
    """Module-scoped pyvisa ResourceManager bound to the @py backend."""
    manager = pyvisa.ResourceManager("@py")
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture(scope="module")
def intfc(rm: pyvisa.ResourceManager):
    """Open the bridge INTFC once per module so all INSTR tests share it.

    Binding the INTFC to board 0 also registers it in the dispatch table
    so subsequent ``GPIB0::*::INSTR`` opens route through the bridge.

    """
    resource = "NI-ENET100-TCPIP0::%s::INTFC" % HOST
    session = rm.open_resource(resource)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def inst(rm: pyvisa.ResourceManager, intfc):
    """Per-test INSTR session against the configured PAD/SAD on board 0.

    Depends on ``intfc`` so the bridge binding is in place before the
    GPIB dispatch hook fires. The session timeout is set to 3 s by
    default; tests that need a different value override it directly.

    """
    if SAD is None:
        resource = "GPIB0::%d::INSTR" % PAD
    else:
        resource = "GPIB0::%d::%d::INSTR" % (PAD, SAD)
    session = rm.open_resource(resource)
    session.timeout = 3000
    session.write_termination = TERM
    session.read_termination = TERM
    try:
        yield session
    finally:
        session.close()


# --- bridge-only tests (no instrument required) ---------------------------


@require_bridge
def test_list_resources_includes_bridge(rm: pyvisa.ResourceManager):
    """Discovery via the resource manager should surface our bridge.

    The default pyvisa query is ``?*::INSTR``; pass ``?*::INTFC`` so our
    bridge resource (an INTFC, not an INSTR) is not filtered out. Match
    by resolved IP because discovery emits IPs while ``HOST`` may be a
    hostname.

    """
    import socket as _socket

    host_ip = _socket.gethostbyname(HOST)
    resources = rm.list_resources("?*::INTFC")
    matches = [r for r in resources if host_ip in r and "NI-ENET100" in r]
    assert matches, "no NI-ENET100 resource for %r (%s) in rm.list_resources() = %r" % (
        HOST,
        host_ip,
        resources,
    )


@require_bridge
def test_intfc_open_registers_board(intfc):
    """Opening the INTFC must register board 0 in the dispatch table so
    GPIB0::*::INSTR resolves to the bridge.

    Board keys mirror ``rname.GPIBInstr.board`` (a string), so the lookup
    that the GPIB dispatch hook does with ``parsed.board`` matches.

    """
    boards = _ni._NIEnet100IntfcSession.boards
    assert "0" in boards, "INTFC did not register board 0: boards=%r" % (
        list(boards.keys()),
    )


# --- instrument tests (need PYVISA_TEST_GPIB_PAD) -------------------------


@require_instrument
def test_idn_query_via_pyvisa(inst):
    """*IDN? through the standard pyvisa Resource.query() API."""
    response = inst.query("*IDN?")
    assert response, "*IDN? returned empty string"
    if IDN_VENDOR:
        assert IDN_VENDOR.lower() in response.lower(), "IDN_VENDOR=%r not in %r" % (
            IDN_VENDOR,
            response,
        )


@require_instrument
def test_clear_via_pyvisa(inst):
    """Resource.clear() must complete without raising."""
    inst.clear()


@require_instrument
def test_read_stb_via_pyvisa(inst):
    """Resource.read_stb() must return a byte-sized integer."""
    stb = inst.read_stb()
    assert 0 <= stb <= 0xFF


@require_instrument
def test_assert_trigger_via_pyvisa(inst):
    """Resource.assert_trigger() must accept the default protocol."""
    inst.assert_trigger()


@require_instrument
def test_timeout_raises_visa_error_timeout(inst):
    """A read with no preceding write hits the box timeout and surfaces
    as VisaIOError with StatusCode.error_timeout."""
    inst.timeout = 200  # 200 ms — short enough that the test is brisk
    with pytest.raises(VisaIOError) as excinfo:
        inst.read()
    assert excinfo.value.error_code == constants.StatusCode.error_timeout, (
        "expected error_timeout, got %r" % excinfo.value.error_code
    )


@require_instrument
def test_repeated_query_keeps_session_healthy(inst):
    """Three back-to-back queries must all succeed — guards against
    accidental state leakage between operations (e.g. unread bytes left
    in the chunk stream or a bracket that closed mid-test)."""
    for _ in range(3):
        response = inst.query("*IDN?")
        assert response
