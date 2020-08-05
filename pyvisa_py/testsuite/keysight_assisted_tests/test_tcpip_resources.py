# -*- coding: utf-8 -*-
"""Test the TCPIP based resources.

"""
from pyvisa.testsuite.keysight_assisted_tests import require_virtual_instr
from pyvisa.testsuite.keysight_assisted_tests.test_tcpip_resources import (
    TestTCPIPInstr as TCPIPInstrBaseTest,
    TestTCPIPSocket as TCPIPSocketBaseTest,
)


@require_virtual_instr
class TestTCPIPInstr(TCPIPInstrBaseTest):
    """Test pyvisa-py against a TCPIP INSTR resource.

    """

    #: Type of resource being tested in this test case.
    #: See RESOURCE_ADDRESSES in the __init__.py file of this package for
    #: acceptable values
    RESOURCE_TYPE = "TCPIP::INSTR"

    #: Minimal timeout value accepted by the resource. When setting the timeout
    #: to VI_TMO_IMMEDIATE, Visa (Keysight at least) may actually use a
    #: different value depending on the values supported by the resource.
    MINIMAL_TIMEOUT = 1


@require_virtual_instr
class TestTCPIPSocket(TCPIPSocketBaseTest):
    """Test pyvisa-py against a TCPIP SOCKET resource.

    """

    #: Type of resource being tested in this test case.
    #: See RESOURCE_ADDRESSES in the __init__.py file of this package for
    #: acceptable values
    RESOURCE_TYPE = "TCPIP::SOCKET"

    #: Minimal timeout value accepted by the resource. When setting the timeout
    #: to VI_TMO_IMMEDIATE, Visa (Keysight at least) may actually use a
    #: different value depending on the values supported by the resource.
    MINIMAL_TIMEOUT = 1
