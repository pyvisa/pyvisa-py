# -*- coding: utf-8 -*-
"""Keysight-assisted TCPIP keepalive checks for the py backend."""

import socket

import pytest

from pyvisa.constants import ResourceAttribute

from . import keysight_tcpip_address, require_keysight_virtual_instr

pytestmark = [
    require_keysight_virtual_instr,
    pytest.mark.pyvisa_keysight_assisted,
]


def test_keepalive_attribute_vxi11_roundtrip():
    from pyvisa import ResourceManager

    resource_name = keysight_tcpip_address()
    rm = ResourceManager("@py")
    try:
        instr = rm.open_resource(resource_name)
        try:
            session = instr.visalib.sessions[instr.session]
            assert session.keepalive is False
            instr.set_visa_attribute(ResourceAttribute.tcpip_keepalive, True)
            assert session.keepalive is True
            assert (
                session.interface.sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_KEEPALIVE
                )
                == 1
            )

            instr.set_visa_attribute(ResourceAttribute.tcpip_keepalive, False)
            assert session.keepalive is False
            assert (
                session.interface.sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_KEEPALIVE
                )
                == 0
            )
        finally:
            instr.close()
    finally:
        rm.close()
