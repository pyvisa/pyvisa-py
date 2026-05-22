# -*- coding: utf-8 -*-
"""Keysight-assisted tests for pyvisa-py."""

from __future__ import annotations

import os

import pytest

_KEYSIGHT_AVAILABLE = "PYVISA_KEYSIGHT_VIRTUAL_INSTR" in os.environ

require_keysight_virtual_instr = pytest.mark.skipif(
    not _KEYSIGHT_AVAILABLE,
    reason=(
        "Requires the Keysight virtual instrument. "
        "Set PYVISA_KEYSIGHT_VIRTUAL_INSTR."
    ),
)


def keysight_tcpip_address() -> str:
    """Return the Keysight TCPIP INSTR resource address for the current mode."""
    setting = os.environ.get("PYVISA_KEYSIGHT_VIRTUAL_INSTR")
    if setting == "0":
        return "TCPIP::127.0.0.1::INSTR"
    return "TCPIP::192.168.0.2::INSTR"
