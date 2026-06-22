# -*- coding: utf-8 -*-
"""Sessions for NI GPIB-ENET/100 Ethernet-to-GPIB bridges.

The bridge speaks a proprietary TCP protocol on ports 5000 (main),
5005 (control) and 5015 (companion) — see :mod:`pyvisa_py.protocols.nienet100`.
This module wires that protocol into pyvisa-py as two session types:

- ``NI-ENET100-TCPIP<n>::<host>::INTFC`` — binds board number ``n`` to the
  given box and keeps a connection open as a connectivity sentinel.
- ``GPIB<n>::<pad>[::<sad>]::INSTR`` — routed to this module when board
  ``n`` was registered as a NIENET100 board (the dispatch hook lives in
  :mod:`pyvisa_py.gpib_dispatch`). Each INSTR session owns its own TCP
  connection to the box; the spec recommends per-resource TCP sessions over
  sharing one connection with multi-PAD bracket switching.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from typing import Any, ClassVar

from pyvisa import attributes, constants, rname
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa.typing import VISARMSession

from .common import LOGGER
from .protocols import nienet100
from .sessions import OpenError, Session, UnknownAttribute

# Resolve the required pyvisa names early so a missing upstream PR produces
# an ImportError that highlevel.py logs at debug level (mirrors how vicp
# falls back when pyvicp is not installed). Users opening NI-ENET100-TCPIP
# resources then see a clean "No class registered" error instead of a
# cryptic AttributeError during session creation.
#
# TODO(pre-release): drop this runtime guard once pyvisa-py pins a minimum
# pyvisa version that ships the ni_enet100_tcpip definitions; the version
# requirement then makes the check redundant.
try:
    _IFACE_NIENET100_TCPIP = constants.InterfaceType.ni_enet100_tcpip
except AttributeError as e:
    raise ImportError(
        "pyvisa-py NI GPIB-ENET/100 support requires pyvisa with "
        "some definitions specific to nienet100; please update pyvisa."
    ) from e


class _NIEnet100IntfcSession(Session):
    """Common base for NI GPIB-ENET/100 INTFC sessions.

    Holds the class-level ``boards`` registry that the GPIB dispatch hook
    in :mod:`pyvisa_py.gpib_dispatch` consults to route ``GPIB<n>::*::INSTR``
    resources through the appropriate bridge.

    The INTFC owns its own :class:`~pyvisa_py.protocols.nienet100.EnetConnection`
    for the session lifetime. The connection acts as a connectivity sentinel
    (the box rejects Device-I/O on stale sessions, so an open socket is a
    reliable health signal). INSTR sessions do **not** share this connection;
    they each open their own.

    """

    #: Maps board number (as parsed string) -> INTFC session instance.
    #: Populated on open, cleared on close. The GPIB dispatch hook reads
    #: this to find the bridge for a given ``GPIB<n>::*::INSTR`` resource.
    #: Key is a string to mirror :class:`rname.GPIBInstr.board`, so dispatch
    #: lookups with ``parsed.board`` match without conversion.
    boards: ClassVar[dict[str, "_NIEnet100IntfcSession"]] = {}

    #: The long-lived connection to the bridge. ``None`` before
    #: ``after_parsing`` runs successfully and after ``close``.
    interface: nienet100.EnetConnection | None

    def _get_attribute(self, attribute: ResourceAttribute) -> tuple[Any, StatusCode]:
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        raise UnknownAttribute(attribute)

    def close(self) -> StatusCode:
        # Always deregister; if open partially failed there may be no entry.
        board = getattr(self.parsed, "board", None)
        if board is not None:
            self.boards.pop(board, None)
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception as e:
                LOGGER.debug("error closing NI GPIB-ENET/100 connection: %s", e)
            self.interface = None
        return StatusCode.success


@Session.register(_IFACE_NIENET100_TCPIP, "INTFC")
class NIEnet100TCPIPIntfcSession(_NIEnet100IntfcSession):
    """Session for ``NI-ENET100-TCPIP<board>::<host>::INTFC`` resources."""

    # Override parsed to take into account the fact that this class is only
    # used for a specific kind of resource.
    parsed: rname.NIEnet100TCPIPIntfc  # type: ignore[name-defined]

    @classmethod
    def get_low_level_info(cls) -> str:
        return "via pure-Python NI GPIB-ENET/100 protocol"

    @staticmethod
    def list_resources() -> list[str]:
        """Discover bridges on the local broadcast domain and emit resource
        strings for each one found.

        Each discovered bridge gets a distinct board number (0-indexed by
        sort order of IP) so that ``open_resource`` calls on the returned
        strings can all succeed against the same host. Cross-subnet
        bridges do not surface here — for those the user must supply the
        resource string explicitly with the box IP and an arbitrary board
        number.

        Returns an empty list (rather than raising) on any discovery
        error — typically a bind conflict or a missing broadcast route.

        """
        # Local import keeps the top-level imports tidy and isolates the
        # UDP code path from sessions that never call list_resources.
        from .protocols import nienet100_discovery

        try:
            boxes = nienet100_discovery.discover(timeout=1.0)
        except OSError as e:
            LOGGER.debug("NI GPIB-ENET/100 discovery failed: %s", e)
            return []
        return [
            "NI-ENET100-TCPIP%d::%s::INTFC" % (i, box.ip) for i, box in enumerate(boxes)
        ]

    def __init__(
        self,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: rname.ResourceName | None = None,
        open_timeout: int | None = None,
    ) -> None:
        self.interface = None
        super().__init__(resource_manager_session, resource_name, parsed, open_timeout)

    def after_parsing(self) -> None:
        # pyvisa open_timeout is in milliseconds; convert to seconds for the
        # socket layer. The default of 10 s mirrors what VXI-11 uses.
        if self.open_timeout is None:
            connect_timeout_s = 10.0
        else:
            connect_timeout_s = max(self.open_timeout / 1000.0, 0.001)

        host = self.parsed.host_address
        try:
            self.interface = nienet100.EnetConnection(
                host,
                open_timeout=connect_timeout_s,
                timeout=connect_timeout_s,
            )
            self.interface.open()
        except Exception as e:
            LOGGER.exception(
                "Failed to open NI GPIB-ENET/100 at %s for board %s",
                host,
                self.parsed.board,
            )
            if self.interface is not None:
                try:
                    self.interface.close()
                except Exception:
                    pass
                self.interface = None
            raise OpenError() from e

        self.boards[self.parsed.board] = self

        self.attrs[ResourceAttribute.interface_number] = self.parsed.board
        self.attrs[ResourceAttribute.tcpip_address] = host
        self.attrs[ResourceAttribute.tcpip_hostname] = host
        self.attrs[ResourceAttribute.tcpip_port] = nienet100.PORT_MAIN


class NIEnet100InstrSession(Session):
    """Session for ``GPIB<n>::<pad>[::<sad>]::INSTR`` routed through a
    GPIB-ENET/100 bridge.

    This class is **not** decorated with ``@Session.register`` directly.
    The central dispatcher in :mod:`pyvisa_py.gpib_dispatch` owns the
    ``(gpib, "INSTR")`` slot and instantiates this class when the
    resource's board number is registered to a NI GPIB-ENET/100 (see
    :attr:`_NIEnet100IntfcSession.boards`); otherwise another backend
    resolver (Prologix or native linux-gpib / gpib-ctypes) handles it.

    Each INSTR session owns its own :class:`EnetConnection` and its own
    bracket — INSTRs do not share TCP sockets with the INTFC or with one
    another. This mirrors the wire spec's recommended pattern and lets
    multiple instruments on the same bridge operate without a shared
    cross-resource lock.

    """

    # We don't decorate this class with Session.register() because we don't
    # want it to be registered in the _session_classes array, but we still
    # need to define session_type to make the set_attribute machinery work.
    session_type = (constants.InterfaceType.gpib, "INSTR")

    # Override parsed to take into account the fact that this class is only
    # used for a specific kind of resource.
    parsed: rname.GPIBInstr

    #: The per-session bridge connection. ``None`` before ``after_parsing``
    #: succeeds and after ``close``. Bracket lifecycle is tracked inside
    #: the connection itself, so ``close()`` releases any open bracket
    #: even when ``after_parsing`` fails mid-way (e.g., a wire error after
    #: Frame F was acked).
    interface: nienet100.EnetConnection | None

    def __init__(
        self,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: rname.ResourceName | None = None,
        open_timeout: int | None = None,
    ) -> None:
        self.interface = None
        #: Holds the tail of a wire message for which the caller's max-count was
        #: too small to consume. The next read() drains this before going
        #: back to the wire, so no bytes are lost between calls.
        self._read_buffer = bytearray()
        super().__init__(resource_manager_session, resource_name, parsed, open_timeout)

    def after_parsing(self) -> None:
        try:
            intfc = _NIEnet100IntfcSession.boards[self.parsed.board]
        except KeyError as e:
            raise OpenError() from e

        if self.open_timeout is None:
            connect_timeout_s = 10.0
        else:
            connect_timeout_s = max(self.open_timeout / 1000.0, 0.001)

        pad = int(self.parsed.primary_address)
        sad_raw = self.parsed.secondary_address
        sad = int(sad_raw) + 0x60 if sad_raw is not None else 0

        # intfc.parsed is a NIEnet100TCPIPIntfc at runtime, but pyvisa does
        # not yet export that rname type (see the name-defined ignore on the
        # INTFC class), so mypy only sees the ResourceName base here.
        host = intfc.parsed.host_address  # type: ignore[attr-defined]
        try:
            self.interface = nienet100.EnetConnection(
                host,
                open_timeout=connect_timeout_s,
                timeout=self.timeout if self.timeout else connect_timeout_s,
            )
            self.interface.open()
            self.interface.open_gpib_session(
                primary_address=pad,
                secondary_address=sad,
                tmo_code=nienet100.seconds_to_tmo_code(self.timeout)
                if self.timeout
                else nienet100.TMO_10s,
            )
        except Exception as e:
            LOGGER.exception(
                "Failed to open GPIB-ENET/100 session to %s pad=%d sad=%d",
                host,
                pad,
                sad,
            )
            self._cleanup_interface()
            raise OpenError() from e

        self.attrs[ResourceAttribute.interface_number] = self.parsed.board
        self.attrs[ResourceAttribute.gpib_primary_address] = pad
        self.attrs[ResourceAttribute.gpib_secondary_address] = (
            (sad - 0x60) if sad else constants.VI_NO_SEC_ADDR
        )
        self.attrs[ResourceAttribute.tcpip_address] = host
        self.attrs[ResourceAttribute.tcpip_hostname] = host
        self.attrs[ResourceAttribute.tcpip_port] = nienet100.PORT_MAIN
        for name in ("SEND_END_EN", "TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def _cleanup_interface(self) -> None:
        if self.interface is not None:
            # close() handles bracket cleanup internally based on the
            # connection's own _bracket_open flag — no need to gate the
            # call here.
            try:
                self.interface.close()
            except Exception as e:
                LOGGER.debug("error closing GPIB-ENET/100 connection: %s", e)
            self.interface = None
        # Discard any buffered-but-unread response so it cannot leak into a
        # subsequent session that reuses this object.
        self._read_buffer.clear()

    def close(self) -> StatusCode:
        self._cleanup_interface()
        return StatusCode.success

    # --- I/O ------------------------------------------------------------

    def write(self, data: bytes) -> tuple[int, StatusCode]:
        if self.interface is None:
            return 0, StatusCode.error_connection_lost
        # A new write starts a fresh exchange: drop any buffered-but-unread
        # bytes left over from a partial read of a previous response so the
        # stale tail cannot be prepended to the reply for this command.
        self._read_buffer.clear()
        try:
            written = self.interface.ibwrt(data)
        except nienet100.NIEnet100IOError as e:
            return 0, _map_iberr_to_status(e.err)
        return written, StatusCode.success

    def read(self, count: int) -> tuple[bytes, StatusCode]:
        if self.interface is None:
            return b"", StatusCode.error_connection_lost

        # The wire-level ibrd always reads a whole message (until EOI/EOS).
        # We cache it here and hand it out in <= count-byte slices, keeping
        # any remainder for the next call — this is what lets a caller read
        # a response one byte at a time without losing the tail.
        if not self._read_buffer:
            # Propagate the pyvisa session timeout to the wire-level ibrd as
            # tmo_ms. self.timeout is in seconds; None means infinite (no
            # ceiling) — fall back to the wire layer's default in that case.
            if self.timeout is None:
                tmo_ms = nienet100.DEFAULT_IBRD_TMO_MS
            else:
                tmo_ms = max(int(self.timeout * 1000), 1)
            try:
                self._read_buffer.extend(self.interface.ibrd(tmo_ms=tmo_ms))
            except nienet100.NIEnet100IOError as e:
                return b"", _map_iberr_to_status(e.err)

        # More remains buffered than requested: hand back exactly `count`
        # bytes and keep the rest. Per VISA this is success_max_count_read.
        if count < len(self._read_buffer):
            chunk = bytes(self._read_buffer[:count])
            del self._read_buffer[:count]
            return chunk, StatusCode.success_max_count_read

        # The caller's count covers the rest of the message; drain the
        # buffer and report end-of-message (termination char or success).
        chunk = bytes(self._read_buffer)
        self._read_buffer.clear()

        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        if term_en and term_char is not None and chunk and chunk[-1] == term_char:
            return chunk, StatusCode.success_termination_character_read
        return chunk, StatusCode.success

    def clear(self) -> StatusCode:
        if self.interface is None:
            return StatusCode.error_connection_lost
        # Drop any buffered-but-unread response: after a device clear the old
        # tail is stale and must not leak into the next read().
        self._read_buffer.clear()
        try:
            self.interface.ibclr()
        except nienet100.NIEnet100IOError as e:
            return _map_iberr_to_status(e.err)
        return StatusCode.success

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        if protocol != constants.VI_TRIG_PROT_DEFAULT:
            return StatusCode.error_nonsupported_operation
        if self.interface is None:
            return StatusCode.error_connection_lost
        try:
            self.interface.ibtrg()
        except nienet100.NIEnet100IOError as e:
            return _map_iberr_to_status(e.err)
        return StatusCode.success

    def read_stb(self) -> tuple[int, StatusCode]:
        if self.interface is None:
            return 0, StatusCode.error_connection_lost
        try:
            stb = self.interface.ibrsp()
        except nienet100.NIEnet100IOError as e:
            return 0, _map_iberr_to_status(e.err)
        return stb, StatusCode.success

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        # ibloc covers VI_GPIB_REN_DEASSERT_GTL (Go-To-Local). The remaining
        # six REN modes (assert/deassert REN, with optional address and/or
        # LLO) need an ibsre verb that drives the REN line; that wire frame
        # is not yet reverse-engineered, so they currently surface as
        # error_nonsupported_operation (the documented pyvisa contract for
        # backends that cannot honour a given REN mode).
        if mode != constants.VI_GPIB_REN_DEASSERT_GTL:
            return StatusCode.error_nonsupported_operation
        if self.interface is None:
            return StatusCode.error_connection_lost
        try:
            self.interface.ibloc()
        except nienet100.NIEnet100IOError as e:
            return _map_iberr_to_status(e.err)
        return StatusCode.success

    # --- timeout plumbing ----------------------------------------------

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super()._set_timeout(attribute, value)
        if self.interface is not None:
            # The bridge rejects the IbcTMO property setter ('P 03') once a
            # bracket is open, so the wire-level timeout is delivered via
            # the per-call tmo_ms argument of ibrd (see read() below). The
            # socket-level timeout is a hard ceiling above the wire timeout
            # so the bridge always surfaces its own timeout first.
            #
            # The bridge has a built-in minimum delay (observed ~3 s
            # against a real GPIB-ENET/100) before it reports a timeout to
            # the host, regardless of the per-call tmo_ms value, so the
            # socket ceiling needs generous headroom above the configured
            # wire timeout. Without it, short pyvisa timeouts (e.g. 200 ms)
            # trip the socket before the bridge ever responds.
            if self.timeout is None:
                self.interface.set_socket_timeout(None)
            else:
                self.interface.set_socket_timeout(max(self.timeout + 5.0, 8.0))
        return status

    def _get_attribute(self, attribute: ResourceAttribute) -> tuple[Any, StatusCode]:
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        raise UnknownAttribute(attribute)


def _map_iberr_to_status(iberr: int) -> StatusCode:
    """Translate a wire-level iberr code into a pyvisa StatusCode."""
    if iberr == nienet100.ERR_EABO:
        return StatusCode.error_timeout
    if iberr == nienet100.ERR_ENOL:
        return StatusCode.error_no_listeners
    if iberr == nienet100.ERR_ECIC:
        return StatusCode.error_not_cic
    if iberr == nienet100.ERR_EARG:
        return StatusCode.error_invalid_mode
    if iberr == nienet100.ERR_ESAC:
        return StatusCode.error_nonsupported_operation
    return StatusCode.error_system_error
