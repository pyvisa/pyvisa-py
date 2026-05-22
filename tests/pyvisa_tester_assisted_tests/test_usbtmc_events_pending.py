# -*- coding: utf-8 -*-
"""Pending USBTMC VISA event coverage for pyvisa-py."""

import pytest


@pytest.mark.xfail(
    reason=(
        "USBTMC queue event support is pending in pyvisa-py. "
        "Tracking: https://github.com/pyvisa/pyvisa-py/issues?q=is%3Aissue+is%3Aopen+USBTMC+event"
    )
)
def test_usbtmc_srq_queue_event_pending():
    pytest.xfail("USBTMC SRQ queue events will be added in a follow-up backend PR")


@pytest.mark.xfail(
    reason=(
        "USBTMC handler event support is pending in pyvisa-py. "
        "Tracking: https://github.com/pyvisa/pyvisa-py/issues?q=is%3Aissue+is%3Aopen+service+request+usb"
    )
)
def test_usbtmc_srq_handler_event_pending():
    pytest.xfail("USBTMC SRQ handler events will be added in a follow-up backend PR")
