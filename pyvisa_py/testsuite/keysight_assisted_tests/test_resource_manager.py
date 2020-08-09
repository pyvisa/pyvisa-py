# -*- coding: utf-8 -*-
"""Test the Resource manager.

"""
from pyvisa.testsuite.keysight_assisted_tests import require_virtual_instr
from pyvisa.testsuite.keysight_assisted_tests.test_resource_manager import (
    TestResourceManager as BaseTestResourceManager,
    TestResourceParsing as BaseTestResourceParsing,
)


@require_virtual_instr
class TestPyResourceManager(BaseTestResourceManager):
    """
    """

    pass


@require_virtual_instr
class TestPyResourceParsing(BaseTestResourceParsing):
    """
    """

    pass
