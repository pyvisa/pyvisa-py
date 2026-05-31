# -*- coding: utf-8 -*-
"""Hardware-gated tests for the NI GPIB-ENET/100 driver.

These tests only run when a real GPIB-ENET/100 bridge (and optionally an
instrument on its bus) is reachable. Configuration is via environment
variables:

* ``PYVISA_TEST_NIENET100_HOST`` (required)
    IP or hostname of the bridge. Without it every test in this package
    skips cleanly.

* ``PYVISA_TEST_GPIB_PAD`` (required for instrument tests)
    Primary GPIB address (0-30) of the instrument under test.

* ``PYVISA_TEST_GPIB_SAD`` (optional)
    Secondary GPIB address (0-30). Defaults to none.

* ``PYVISA_TEST_IDN_VENDOR`` (optional)
    A substring that must appear in the instrument's ``*IDN?`` response
    — gives the IDN round-trip test a meaningful assertion when set.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import os
from typing import Optional

import pytest

#: Bridge IP/hostname, or ``None`` when not configured.
HOST: Optional[str] = os.environ.get("PYVISA_TEST_NIENET100_HOST") or None

#: Instrument primary address as int, or ``None`` when not configured.
_pad_env = os.environ.get("PYVISA_TEST_GPIB_PAD")
PAD: Optional[int] = int(_pad_env) if _pad_env else None

#: Instrument secondary address as int, or ``None`` when not configured.
_sad_env = os.environ.get("PYVISA_TEST_GPIB_SAD")
SAD: Optional[int] = int(_sad_env) if _sad_env else None

#: Optional substring that must appear in the ``*IDN?`` response.
IDN_VENDOR: Optional[str] = os.environ.get("PYVISA_TEST_IDN_VENDOR") or None


#: Skip a test when no bridge is configured.
require_bridge = pytest.mark.skipif(
    HOST is None,
    reason="set PYVISA_TEST_NIENET100_HOST to a reachable bridge IP",
)

#: Skip a test when no instrument primary address is configured.
require_instrument = pytest.mark.skipif(
    HOST is None or PAD is None,
    reason=(
        "set PYVISA_TEST_NIENET100_HOST and PYVISA_TEST_GPIB_PAD to enable "
        "instrument-level tests"
    ),
)
