# -*- coding: utf-8 -*-
"""Pending TCPIP SRQ coverage for pyvisa-py against pyvisa-tester."""

import pytest

from . import require_pyvisa_tester_assisted

pytestmark = [
    require_pyvisa_tester_assisted,
    pytest.mark.pyvisa_tester_assisted,
]


@pytest.mark.xfail(reason="VXI-11 SRQ queue support pending in pyvisa-py backend")
def test_vxi11_srq_queue_event_pending():
    pytest.xfail("VXI-11 SRQ queue support will be added in a follow-up PR")


@pytest.mark.xfail(reason="VXI-11 SRQ handler support pending in pyvisa-py backend")
def test_vxi11_srq_handler_event_pending():
    pytest.xfail("VXI-11 SRQ handler support will be added in a follow-up PR")


@pytest.mark.xfail(reason="HiSLIP SRQ queue support pending in pyvisa-py backend")
def test_hislip_srq_queue_event_pending():
    pytest.xfail("HiSLIP SRQ queue support will be added in a follow-up PR")


@pytest.mark.xfail(reason="HiSLIP SRQ handler support pending in pyvisa-py backend")
def test_hislip_srq_handler_event_pending():
    pytest.xfail("HiSLIP SRQ handler support will be added in a follow-up PR")
