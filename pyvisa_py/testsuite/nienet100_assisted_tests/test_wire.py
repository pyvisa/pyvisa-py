# -*- coding: utf-8 -*-
"""Wire-level hardware tests for the NI GPIB-ENET/100 driver.

Drives :class:`~pyvisa_py.protocols.nienet100.EnetConnection` against a
real bridge. Does **not** go through pyvisa-py's session layer, so these
tests pass without requiring the upstream ``InterfaceType.ni_enet100_tcpip``
addition in pyvisa. Useful for first-light validation against new hardware
and for catching wire-protocol regressions independently of the session
layer.

See the package ``__init__`` docstring for environment-variable setup.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
import time
from typing import Iterator

import pytest

from pyvisa_py.protocols import nienet100, nienet100_discovery

from . import (
    HOST,
    IDN_VENDOR,
    PAD,
    SAD,
    require_bridge,
    require_instrument,
)


def _resolve_host_ip() -> str:
    """Resolve ``HOST`` to a dotted-quad IP for comparison against
    discovery results, which always carry the bridge's IP. Falls back to
    ``HOST`` as-is when DNS resolution fails (e.g. NetBIOS-only names on
    a locked-down Windows box) so the test surfaces a meaningful diff
    rather than a gaierror."""
    try:
        return socket.gethostbyname(HOST)
    except socket.gaierror:
        return HOST


# --- bridge-only tests (no instrument required) ---------------------------


@require_bridge
def test_discovery_finds_configured_bridge():
    """The configured bridge must surface in a broadcast scan."""
    expected_ip = _resolve_host_ip()
    boxes = nienet100_discovery.discover(timeout=2.0)
    assert boxes, "no bridges replied to broadcast"
    ips = [b.ip for b in boxes]
    assert expected_ip in ips, (
        "configured bridge %r (resolved to %r) not in discovered set %r"
        % (HOST, expected_ip, ips)
    )


@require_bridge
def test_unicast_discovery_against_configured_bridge():
    """Unicast probe to the known IP should return exactly that one box."""
    expected_ip = _resolve_host_ip()
    boxes = nienet100_discovery.discover(
        timeout=2.0,
        broadcast_addr=HOST,
        port=nienet100_discovery.PORT_UNICAST,
    )
    ips = [b.ip for b in boxes]
    assert expected_ip in ips, "unicast probe to %r (resolved to %r) returned %r" % (
        HOST,
        expected_ip,
        ips,
    )


@require_bridge
def test_open_and_close_main_companion():
    """Main + companion sockets and companion hello must round-trip."""
    conn = nienet100.EnetConnection(HOST, open_timeout=5.0, timeout=5.0)
    conn.open()
    try:
        assert conn.main is not None
        assert conn.companion is not None
    finally:
        conn.close()
    assert conn.main is None and conn.companion is None


# --- instrument tests (need PYVISA_TEST_GPIB_PAD) -------------------------


@pytest.fixture
def opened_session() -> Iterator[nienet100.EnetConnection]:
    """Yield an EnetConnection with the full open sequence done.

    Cleans up sockets unconditionally even if the test body raises so a
    failing test does not leave stale state on the bridge.
    """
    conn = nienet100.EnetConnection(HOST, open_timeout=5.0, timeout=5.0)
    conn.open()
    try:
        conn.open_gpib_session(
            primary_address=PAD,
            secondary_address=SAD or 0,
            tmo_code=nienet100.TMO_3s,
        )
    except Exception:
        conn.close()
        raise
    try:
        yield conn
    finally:
        try:
            conn.close_gpib_session()
        finally:
            conn.close()


@require_instrument
def test_idn_query_round_trip(opened_session: nienet100.EnetConnection):
    """*IDN? must return non-empty bytes; if a vendor substring was
    configured, it must appear in the response."""
    written = opened_session.ibwrt(b"*IDN?\n")
    assert written == 6
    response = opened_session.ibrd()
    assert response, "*IDN? returned empty payload"
    text = response.decode("ascii", errors="replace")
    if IDN_VENDOR:
        assert IDN_VENDOR.lower() in text.lower(), "IDN_VENDOR=%r not in %r" % (
            IDN_VENDOR,
            text,
        )


@require_instrument
def test_clear_round_trip(opened_session: nienet100.EnetConnection):
    """ibclr against the addressed device must complete without error."""
    opened_session.ibclr()


@require_instrument
def test_read_stb_round_trip(opened_session: nienet100.EnetConnection):
    """ibrsp must return a single STB byte (0-255)."""
    stb = opened_session.ibrsp()
    assert 0 <= stb <= 0xFF


@require_instrument
def test_trigger_round_trip(opened_session: nienet100.EnetConnection):
    """ibtrg must complete without error. The bus instrument may or may
    not actually trigger anything — we only assert the verb is accepted."""
    opened_session.ibtrg()


@require_instrument
def test_timeout_surfaces_as_iberr_eabo(
    opened_session: nienet100.EnetConnection,
):
    """A read with no preceding write should hit the box's TMO and raise
    NIEnet100IOError with iberr=EABO (6) once the configured timeout
    fires. Uses a short box-side timeout (T100ms) so the test does not
    spend seconds waiting."""
    opened_session.set_io_timeout(nienet100.TMO_100ms)
    started = time.monotonic()
    try:
        with pytest.raises(nienet100.NIEnet100IOError) as excinfo:
            opened_session.ibrd()
        assert excinfo.value.err == nienet100.ERR_EABO, (
            "expected EABO (timeout), got iberr=%d" % excinfo.value.err
        )
    finally:
        # Restore a sane timeout so the fixture teardown does not stall.
        opened_session.set_io_timeout(nienet100.TMO_3s)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, (
        "timeout took %.1fs — much longer than the configured 100 ms" % elapsed
    )


@require_instrument
def test_idn_query_with_ibwait_for_completion(
    opened_session: nienet100.EnetConnection,
):
    """Demonstrate the ibwait code path against real hardware: after
    *IDN?, poll for the operation-complete bit. This exercises the
    lazy wait-socket opening, the async-register frame, and one
    ibwait round-trip."""
    opened_session.ibwrt(b"*IDN?\n")
    # CMPL (0x0100) is set by the box's reply to *IDN?. We give the box
    # up to 2 s to complete via the configured IbcTMO; ibwait blocks until
    # one of the mask bits surfaces or the box times out.
    sta = opened_session.ibwait(nienet100.STA_CMPL | nienet100.STA_TIMO)
    assert sta & (nienet100.STA_CMPL | nienet100.STA_TIMO), (
        "ibwait returned without CMPL or TIMO: sta=0x%04x" % sta
    )
    # Drain the response so the bus is clean for subsequent tests.
    if sta & nienet100.STA_CMPL:
        opened_session.ibrd()
