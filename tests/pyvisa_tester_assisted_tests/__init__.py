# -*- coding: utf-8 -*-
"""pyvisa-tester-assisted tests for pyvisa-py."""

from __future__ import annotations

import os

import pytest

_TESTER_AVAILABLE = os.environ.get("PYVISA_TESTER_ASSISTED") == "1"

require_pyvisa_tester_assisted = pytest.mark.skipif(
    not _TESTER_AVAILABLE,
    reason=(
        "Requires pyvisa-tester-assisted endpoints. "
        "Set PYVISA_TESTER_ASSISTED=1."
    ),
)
