# -*- coding: utf-8 -*-
"""Additional PyVISA-py constants.

This file is an addition to the pyvisa package and introduces new constanst
to that have a specific use in pyvisa-py. For standard constants look in
pyvisa.constants

:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.
"""

# This is no standard and VI_ATTR_TCPIP_KEEPALIVE+1 as it was not taken
VI_ATTR_TCPIP_KEEPALIVE_VXI11 = 0x3FFF019C

@enum.unique
class ResourceAttribute(enum.IntEnum):
    """The possible attributes of VISA resources."""
    # TCPIP specific attributes
    vxi11_use_keepalive = VI_ATTR_TCPIP_KEEPALIVE_VXI11