# -*- coding: utf-8 -*-
"""LXI-assisted SRQ tests for pyvisa-py.

This package intentionally carries tests-only scaffolding.
Actual VXI-11 and HiSLIP SRQ backend support is planned for follow-up PRs.
See docs/lxi_srq_test_plan.md.
"""

import os

import pytest

_LXI_AVAILABLE = "PYVISA_LXI_ASSISTED" in os.environ

require_lxi_assisted = pytest.mark.skipif(
    not _LXI_AVAILABLE,
    reason="Requires LXI fake instruments. Set PYVISA_LXI_ASSISTED.",
)

RESOURCE_ADDRESSES = {
    "TCPIP::INSTR": os.environ.get(
        "PYVISA_LXI_VXI11_ADDR", "TCPIP::127.0.0.1::inst0::INSTR"
    ),
    "TCPIP::HISLIP": os.environ.get(
        "PYVISA_LXI_HISLIP_ADDR", "TCPIP::127.0.0.1::hislip0::INSTR"
    ),
}
