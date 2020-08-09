# -*- coding: utf-8 -*-
"""Test the TCPIP based resources.

"""
import pytest
from pyvisa.testsuite.keysight_assisted_tests import copy_func, require_virtual_instr
from pyvisa.testsuite.keysight_assisted_tests.test_tcpip_resources import (
    TestTCPIPInstr as TCPIPInstrBaseTest,
)

# from pyvisa.testsuite.keysight_assisted_tests.test_tcpip_resources import (
#     TestTCPIPSocket as TCPIPSocketBaseTest,
# )


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
    MINIMAL_TIMEOUT = 0  # XXX should we try to have this match VISA ?

    test_wrapping_handler = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_wrapping_handler)
    )

    test_managing_visa_handler = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_managing_visa_handler)
    )

    test_wait_on_event = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_wait_on_event)
    )

    test_wait_on_event_timeout = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_wait_on_event_timeout)
    )

    test_getting_unknown_buffer = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_getting_unknown_buffer)
    )

    test_manual_async_read = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_manual_async_read)
    )

    test_uninstall_all_handlers = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_uninstall_all_handlers)
    )

    test_handler_clean_up_on_resource_del = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_handler_clean_up_on_resource_del)
    )

    test_uninstalling_missing_visa_handler = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_uninstalling_missing_visa_handler)
    )

    test_handling_invalid_handler = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_handling_invalid_handler)
    )

    test_write_raw_read_bytes = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_write_raw_read_bytes)
    )

    test_io_prot_attr = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_io_prot_attr)
    )

    test_shared_locking = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_shared_locking)
    )

    test_timeout = pytest.mark.xfail(copy_func(TCPIPInstrBaseTest.test_timeout))

    test_attribute_handling = pytest.mark.xfail(
        copy_func(TCPIPInstrBaseTest.test_attribute_handling)
    )


# XXX requires to support clear.
# @require_virtual_instr
# class TestTCPIPSocket(TCPIPSocketBaseTest):
#     """Test pyvisa-py against a TCPIP SOCKET resource.

#     """

#     #: Type of resource being tested in this test case.
#     #: See RESOURCE_ADDRESSES in the __init__.py file of this package for
#     #: acceptable values
#     RESOURCE_TYPE = "TCPIP::SOCKET"

#     #: Minimal timeout value accepted by the resource. When setting the timeout
#     #: to VI_TMO_IMMEDIATE, Visa (Keysight at least) may actually use a
#     #: different value depending on the values supported by the resource.
#     MINIMAL_TIMEOUT = 1
