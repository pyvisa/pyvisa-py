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

from pyvisa import constants, rname
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
