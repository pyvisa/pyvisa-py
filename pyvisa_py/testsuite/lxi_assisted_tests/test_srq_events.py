# -*- coding: utf-8 -*-
"""SRQ tests scaffold for pyvisa-py over LXI fake instruments.

These tests are expected to fail until SRQ support is implemented for VXI-11
and HiSLIP in follow-up PRs. See docs/lxi_srq_test_plan.md.

CI is expected to provision instruments through the pyvisa-tester binary.
"""

import pytest

from . import require_lxi_assisted


@pytest.mark.xfail(
    reason="Tests-only scope: backend SRQ support pending. See docs/lxi_srq_test_plan.md"
)
@require_lxi_assisted
def test_vxi11_srq_queue_event():
    pytest.xfail("VXI-11 SRQ queue support will be added in a follow-up PR")


@pytest.mark.xfail(
    reason="Tests-only scope: backend SRQ support pending. See docs/lxi_srq_test_plan.md"
)
@require_lxi_assisted
def test_vxi11_srq_handler_event():
    pytest.xfail("VXI-11 SRQ handler support will be added in a follow-up PR")


@pytest.mark.xfail(
    reason="Tests-only scope: backend SRQ support pending. See docs/lxi_srq_test_plan.md"
)
@require_lxi_assisted
def test_hislip_srq_queue_event():
    pytest.xfail("HiSLIP SRQ queue support will be added in a follow-up PR")


@pytest.mark.xfail(
    reason="Tests-only scope: backend SRQ support pending. See docs/lxi_srq_test_plan.md"
)
@require_lxi_assisted
def test_hislip_srq_handler_event():
    pytest.xfail("HiSLIP SRQ handler support will be added in a follow-up PR")
