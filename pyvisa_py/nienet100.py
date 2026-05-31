# -*- coding: utf-8 -*-
"""Sessions for NI GPIB-ENET/100 Ethernet-to-GPIB bridges.

The bridge speaks a proprietary TCP protocol on ports 5000 / 5003 / 5005
/ 5015 (see :mod:`pyvisa_py.protocols.nienet100`). This module wires that
protocol into pyvisa-py as two session types:

- ``NIENET100-TCPIP<n>::<host>::INTFC`` — binds board number ``n`` to the
  given box and keeps a connection open as a connectivity sentinel.
- ``GPIB<n>::<pad>[::<sad>]::INSTR`` — dispatched here when board ``n`` was
  previously registered as a NIENET100 board (the dispatch hook lives in
  :mod:`pyvisa_py.gpib`). Each INSTR session owns its own TCP connection
  to the box; the spec recommends per-resource TCP sessions over sharing
  one connection with multi-PAD bracket switching.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pyvisa import attributes, constants, rname
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa.typing import VISARMSession

from .common import LOGGER
from .protocols import nienet100
from .sessions import OpenError, Session, UnknownAttribute

# Resolve the required pyvisa names early so a missing upstream PR produces
# an ImportError that highlevel.py logs at debug level (mirrors how vicp
# falls back when pyvicp is not installed). Users opening NIENET100-TCPIP
# resources then see a clean "No class registered" error instead of a
# cryptic AttributeError during session creation.
try:
    _IFACE_NIENET100_TCPIP = constants.InterfaceType.ni_enet100_tcpip
except AttributeError as e:
    raise ImportError(
        "pyvisa-py NI GPIB-ENET/100 support requires pyvisa with "
        "InterfaceType.ni_enet100_tcpip; please update pyvisa."
    ) from e

try:
    _RNAME_NIENET100_TCPIP_INTFC = rname.NIEnet100TCPIPIntfc
except AttributeError as e:
    raise ImportError(
        "pyvisa-py NI GPIB-ENET/100 support requires pyvisa with "
        "rname.NIEnet100TCPIPIntfc; please update pyvisa."
    ) from e


class _NIEnet100IntfcSession(Session):
    """Common base for NI GPIB-ENET/100 INTFC sessions.

    Holds the class-level ``boards`` registry that the GPIB dispatch hook
    in :mod:`pyvisa_py.gpib` consults to route ``GPIB<n>::*::INSTR``
    resources through the appropriate bridge.

    The INTFC owns its own :class:`~pyvisa_py.protocols.nienet100.EnetConnection`
    for the session lifetime. The connection acts as a connectivity sentinel
    (the box rejects Device-I/O on stale sessions, so an open socket is a
    reliable health signal). INSTR sessions do **not** share this connection;
    they each open their own — per the wire spec's recommendation.
    """

    #: Maps board number -> INTFC session instance. Populated on open,
    #: cleared on close. The GPIB dispatch hook reads this to find the
    #: bridge for a given ``GPIB<n>::*::INSTR`` resource.
    boards: ClassVar[Dict[int, "_NIEnet100IntfcSession"]] = {}

    #: The long-lived connection to the bridge. ``None`` before
    #: ``after_parsing`` runs successfully and after ``close``.
    interface: Optional[nienet100.EnetConnection]

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        raise UnknownAttribute(attribute)

    def close(self) -> StatusCode:
        # Always deregister; if open partially failed there may be no entry.
        self.boards.pop(getattr(self.parsed, "board", None), None)
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception as e:
                LOGGER.debug("error closing NI GPIB-ENET/100 connection: %s", e)
            self.interface = None
        return StatusCode.success


@Session.register(_IFACE_NIENET100_TCPIP, "INTFC")
class NIEnet100TCPIPIntfcSession(_NIEnet100IntfcSession):
    """Session for ``NIENET100-TCPIP<board>::<host>::INTFC`` resources."""

    # Override parsed to take into account the fact that this class is only
    # used for a specific kind of resource.
    parsed: rname.NIEnet100TCPIPIntfc  # type: ignore[name-defined]

    @classmethod
    def get_low_level_info(cls) -> str:
        return "via pure-Python NI GPIB-ENET/100 protocol"

    @staticmethod
    def list_resources() -> List[str]:
        # TODO: implement Schiene-A UDP discovery on port 44515 to populate
        # this list with reachable bridges. Returning an empty list keeps
        # the resource type usable when the user supplies an explicit string.
        return []

    def __init__(
        self,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: Optional[rname.ResourceName] = None,
        open_timeout: Optional[int] = None,
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
    The wrapping dispatcher at the bottom of this module owns the
    ``(gpib, "INSTR")`` slot in the dispatch table — it consults
    :attr:`_NIEnet100IntfcSession.boards` and instantiates this class
    when the resource's board number is registered to a bridge, or
    falls back to the previous dispatcher (linux-gpib / gpib-ctypes via
    ``GPIBSessionDispatch``) otherwise.

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
    #: succeeds and after ``close``.
    interface: Optional[nienet100.EnetConnection]

    #: Set to True after open_gpib_session succeeds; gates close to avoid
    #: sending a bracket-close on a connection that never opened a bracket.
    _bracket_open: bool

    def __init__(
        self,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: Optional[rname.ResourceName] = None,
        open_timeout: Optional[int] = None,
    ) -> None:
        self.interface = None
        self._bracket_open = False
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

        host = intfc.parsed.host_address
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
            self._bracket_open = True
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
            try:
                if self._bracket_open:
                    self.interface.close_gpib_session()
            except Exception as e:
                LOGGER.debug("error closing GPIB bracket on cleanup: %s", e)
            try:
                self.interface.close()
            except Exception as e:
                LOGGER.debug("error closing GPIB-ENET/100 connection: %s", e)
            self.interface = None
            self._bracket_open = False

    def close(self) -> StatusCode:
        self._cleanup_interface()
        return StatusCode.success

    # --- I/O ------------------------------------------------------------

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        if self.interface is None:
            return 0, StatusCode.error_connection_lost
        try:
            written = self.interface.ibwrt(data)
        except nienet100.NIEnet100IOError as e:
            return 0, _map_iberr_to_status(e.err)
        return written, StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        if self.interface is None:
            return b"", StatusCode.error_connection_lost
        try:
            data = self.interface.ibrd()
        except nienet100.NIEnet100IOError as e:
            return b"", _map_iberr_to_status(e.err)

        # The wire-level ibrd always reads the full message (until EOI/EOS);
        # truncate here if the caller's max-count is smaller. Extra bytes
        # are dropped — ibrd has no resume semantics, so a caller that
        # supplied too small a count loses the tail.
        if len(data) > count:
            return bytes(data[:count]), StatusCode.success_max_count_read

        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        if term_en and term_char is not None and data and data[-1] == term_char:
            return bytes(data), StatusCode.success_termination_character_read
        return bytes(data), StatusCode.success

    def clear(self) -> StatusCode:
        if self.interface is None:
            return StatusCode.error_connection_lost
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

    def read_stb(self) -> Tuple[int, StatusCode]:
        if self.interface is None:
            return 0, StatusCode.error_connection_lost
        try:
            stb = self.interface.ibrsp()
        except nienet100.NIEnet100IOError as e:
            return 0, _map_iberr_to_status(e.err)
        return stb, StatusCode.success

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        # ibloc covers VI_GPIB_REN_DEASSERT_GTL (Go-To-Local). Other REN
        # operations require board-level verbs (ibsre/ibsic) that are not
        # yet implemented in the wire layer — TODO once those land.
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
            # Wire-level IbcTMO is a discrete code; socket-level timeout is
            # a hard ceiling that should be slightly larger than the box
            # timeout so the box always reports its own timeout first.
            if self.timeout is None:
                self.interface.set_socket_timeout(None)
                self.interface.set_io_timeout(nienet100.TMO_NONE)
            else:
                tmo_code = nienet100.seconds_to_tmo_code(self.timeout)
                self.interface.set_io_timeout(tmo_code)
                self.interface.set_socket_timeout(self.timeout + 1.0)
        return status

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
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


# --- (gpib, INSTR) dispatch hook --------------------------------------------
# Bridge dispatch lives here (not in gpib.py) so it works on systems where
# gpib.py fails to import because neither linux-gpib nor gpib-ctypes is
# installed — exactly the configuration most GPIB-ENET/100 users run.
#
# We save the previously registered dispatcher (GPIBSessionDispatch when
# gpib.py loaded; ``None`` otherwise) and delegate to it when the resource's
# board is not bound to a NIENET100 bridge. This keeps Prologix and
# linux-gpib paths working unchanged.

# Save and pop the existing registration (typically GPIBSessionDispatch from
# gpib.py) so that @Session.register below does not log the "already
# registered, overwriting" warning. Our overwrite is deliberate.
_PREV_GPIB_INSTR_CLS = Session._session_classes.pop(
    (constants.InterfaceType.gpib, "INSTR"), None
)


@Session.register(constants.InterfaceType.gpib, "INSTR")
class _GPIBInstrDispatch(Session):
    """Dispatch GPIB::INSTR resources, with NI GPIB-ENET/100 as a bridge."""

    def __new__(  # type: ignore[misc]
        cls,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: Optional[rname.ResourceName] = None,
        open_timeout: Optional[int] = None,
    ) -> Session:
        if parsed is None:
            parsed = rname.parse_resource_name(resource_name)

        if parsed.board in _NIEnet100IntfcSession.boards:
            return NIEnet100InstrSession(
                resource_manager_session, resource_name, parsed, open_timeout
            )

        if _PREV_GPIB_INSTR_CLS is not None:
            return _PREV_GPIB_INSTR_CLS(
                resource_manager_session, resource_name, parsed, open_timeout
            )

        raise OpenError(StatusCode.error_resource_not_found)
