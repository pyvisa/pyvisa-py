# -*- coding: utf-8 -*-
"""Test the Resource manager.

"""
import pytest
from pyvisa.testsuite.keysight_assisted_tests import copy_func, require_virtual_instr
from pyvisa.testsuite.keysight_assisted_tests.test_resource_manager import (
    TestResourceManager as BaseTestResourceManager,
)
from pyvisa.testsuite.keysight_assisted_tests.test_resource_manager import (
    TestResourceParsing as BaseTestResourceParsing,
)


@require_virtual_instr
class TestPyResourceManager(BaseTestResourceManager):
    """
    """

    test_list_resource = pytest.mark.xfail(
        copy_func(BaseTestResourceManager.test_list_resource)
    )

    test_last_status = pytest.mark.xfail(
        copy_func(BaseTestResourceManager.test_last_status)
    )

    test_opening_resource_with_lock = pytest.mark.xfail(
        copy_func(BaseTestResourceManager.test_opening_resource_with_lock)
    )


@require_virtual_instr
class TestPyResourceParsing(BaseTestResourceParsing):
    """
    """

    pass
