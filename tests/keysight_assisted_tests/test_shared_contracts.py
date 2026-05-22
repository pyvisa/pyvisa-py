# -*- coding: utf-8 -*-
"""Consume shared PyVISA backend contracts against Keysight."""

import pytest

from pyvisa.testing.contracts.test_binary_value_contracts import *  # noqa: F403
from pyvisa.testing.contracts.test_identity_contract import *  # noqa: F403
from pyvisa.testing.contracts.test_keysight_tcpip_contracts import *  # noqa: F403
from pyvisa.testing.contracts.test_resource_manager_contracts import *  # noqa: F403
from pyvisa.testing.contracts.test_srq_event_contracts import *  # noqa: F403

from . import require_keysight_virtual_instr

pytestmark = [
    require_keysight_virtual_instr,
    pytest.mark.pyvisa_keysight_assisted,
]
