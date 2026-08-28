# -*- coding: utf-8 -*-
"""Additional Attributes for specific use with the pyvisa-py package.

For additional information and VISA attributes see pyvisa.constants

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.
"""

from pyvisa import constants
from pyvisa.attributes import (
    AttrVI_ATTR_TCPIP_KEEPALIVE as former_keepalive,
    BooleanAttribute,
)


class AttrVI_ATTR_TCPIP_KEEPALIVE(former_keepalive):
    """Requests that a TCP/IP provider enable the use of keep-alive packets.

    Altering the standard PyVISA attribute to also work on INSTR sessions as
    they are using sockets in pyvisa-py as well.

    After the system detects that a connection was dropped, VISA returns a lost
    connection error code on subsequent I/O calls on the session. The time required
    for the system to detect that the connection was dropped is dependent on the
    system and is not settable.

    """

    resources = [
        (constants.InterfaceType.tcpip, "SOCKET"),
        (constants.InterfaceType.tcpip, "INSTR"),
        (constants.InterfaceType.vicp, "INSTR"),
    ]


# force the definition of the attribute in pyvisa.constants to be able to use it in pyvisa-py
if not hasattr(constants, "VI_KTATTR_LOCKWAIT"):
    constants.VI_KTATTR_LOCKWAIT = 0x0FFF002B  # type: ignore[attr-defined]
    constants.ResourceAttribute.lockwait = constants.VI_KTATTR_LOCKWAIT  # type: ignore[attr-defined]

    class AttrVI_KTATTR_LOCKWAIT(BooleanAttribute):
        resources = [
            (constants.InterfaceType.tcpip, "INSTR"),
        ]

        py_name = ""

        visa_name = "VI_KTATTR_LOCKWAIT"

        visa_type = "ViBoolean"

        default = False

        read, write, local = True, True, True
