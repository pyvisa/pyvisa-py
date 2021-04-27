# -*- coding: utf-8 -*-
"""Additional Attributes for specific use with the pyvisa-py package.

For additional information and VISA attributes see pyvisa.constants

:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.
"""

from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    SupportsBytes,
    SupportsInt,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
)

from pyvisa import constants
from pyvisa.attributes import (
    AllSessionTypes,
    AttributesByID,
    AttributesPerResource,
    BooleanAttribute,
)

import .constants

# Copy of PyVisa attribute architecture
#: Map resource to attribute
AttributesPerResource: DefaultDict[
    Union[
        Tuple[constants.InterfaceType, str], Type[AllSessionTypes], constants.EventType
    ],
    Set[Type["Attribute"]],
] = defaultdict(set)

#: Map id to attribute
AttributesByID: Dict[int, Type["Attribute"]] = dict()


class AttrPyVI_ATTR_TCPIP_KEEPALIVE_VXI11(BooleanAttribute):
    """Requests that a TCP/IP provider enable the use of keep-alive packets.

    This is not limited to VXI11 Instrumets. Use VI_ATTR_TCPIP_KEEPALIVE
    for socket type connections. This is a workaround for sockets beeing
    collected by idle socket garbage collection such as docker utilizes.

    After the system detects that a connection was dropped, VISA returns a lost
    connection error code on subsequent I/O calls on the session. The time required
    for the system to detect that the connection was dropped is dependent on the
    system and is not settable.

    """
    resources = [
        (constants.InterfaceType.tcpip, "INSTR")
    ]

    py_name = "vxi11_use_keepalive"

    visa_name = "VI_ATTR_TCPIP_KEEPALIVE_VXI11"

    visa_type = "ViBoolean"

    default = False

    read, write, local = True, True, True

    @classmethod
    def __init_subclass__(cls, **kwargs):
        """Register the subclass with the supported resources."""
        super().__init_subclass__(**kwargs)

        if not cls.__name__.startswith("AttrPyVI_"):
            return

        cls.attribute_id = getattr(constants, cls.visa_name)
        # Check that the docstring are populated before extending them
        # Cover the case of running with Python with -OO option
        if cls.__doc__ is not None:
            cls.redoc()
        if cls.resources is AllSessionTypes:
            AttributesPerResource[AllSessionTypes].add(cls)
        else:
            for res in cls.resources:
                AttributesPerResource[res].add(cls)
        AttributesByID[cls.attribute_id] = cls
